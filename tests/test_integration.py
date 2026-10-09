import base64
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import server
from workspace_db import WorkspaceDB


class FakeCollector:
    def __init__(self):
        self.calls = []
        self._config = {
            "feeds": [],
            "enabled": False,
            "interval_minutes": 60,
            "last_run": None,
            "next_run": None,
            "last_error": None,
        }
        self.items = []

    def config(self):
        self.calls.append(("config",))
        return dict(self._config)

    def configure(self, payload):
        self.calls.append(("configure", payload))
        if payload.get("enabled") and payload.get("authorized") is not True:
            raise ValueError("authorization_required")
        self._config = {
            **self._config,
            "enabled": bool(payload.get("enabled", False)),
            "feeds": payload.get("feeds", []),
            "interval_minutes": payload.get("interval_minutes", 60),
        }
        return self.config()

    def run_once(self):
        self.calls.append(("run_once",))
        return list(self.items)

    def inbox(self, status=None):
        self.calls.append(("inbox", status))
        return [item for item in self.items if status is None or item.get("status") == status]

    def review(self, item_id, status):
        self.calls.append(("review", item_id, status))
        for item in self.items:
            if item.get("id") == item_id:
                item["status"] = status
                item["facts_approved"] = False
                return dict(item)
        raise KeyError("item_not_found")


class IntegrationServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = WorkspaceDB(cls.tmp.name)
        cls.collector = FakeCollector()
        cls.original_db = server.WORKSPACE_DB
        cls.original_collector = server.COLLECTOR
        server.WORKSPACE_DB = cls.db
        server.COLLECTOR = cls.collector
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server.WORKSPACE_DB = cls.original_db
        server.COLLECTOR = cls.original_collector
        cls.db.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.db._conn.execute("DELETE FROM audit")
        self.db._conn.execute("DELETE FROM facts")
        self.db._conn.execute("DELETE FROM evidence")
        self.db._conn.execute("DELETE FROM sources")
        self.db._conn.commit()
        self.collector.items = []
        self.collector.calls.clear()

    def request(self, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def test_environment_overrides_are_application_state_only(self):
        self.assertTrue(server.STATE_DIR.name == "workspace_state")
        self.assertTrue(server.COLLECTOR_STATE_DIR.name == "collector_state")
        self.assertNotIn("/api/", str(server.STATE_DIR))
        self.assertNotIn("/api/", str(server.COLLECTOR_STATE_DIR))

    def test_workspace_ingest_and_strict_base64_errors_are_safe(self):
        status, payload = self.request(
            "POST",
            "/api/ingest",
            {
                "filename": "notes.txt",
                "content_base64": base64.b64encode(b"Revenue grew 20%\n").decode("ascii"),
                "ocr": False,
                "company_id": "demo",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["source"]["filename"], "notes.txt")
        self.assertEqual(payload["pending"], 0)

        for encoded in ("not base64", "!!!!", "YWJj\n"):
            status, error = self.request(
                "POST",
                "/api/ingest",
                {"filename": "notes.txt", "content_base64": encoded},
            )
            self.assertEqual(status, 400)
            self.assertEqual(error, {"error": "validation_error"})
            self.assertNotIn(encoded, json.dumps(error))

        status, workspace = self.request("GET", "/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(set(workspace), {"sources", "evidence", "facts", "audit", "stats"})
        self.assertEqual(workspace["stats"]["sources"], 1)

    def test_fact_routes_never_auto_approve_and_review_requires_reason(self):
        imported = self.db.import_bytes("source.txt", b"Revenue 100")
        evidence_id = imported["evidence"][0]["id"]
        fact = {
            "company_id": "demo",
            "metric_key": "revenue",
            "value": 100,
            "unit": "USD million",
            "currency": "USD",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "period_type": "FY",
            "accounting_basis": "GAAP",
            "scope": "consolidated",
            "evidence_id": evidence_id,
        }
        status, response = self.request("POST", "/api/facts", fact)
        self.assertEqual(status, 201)
        self.assertEqual(response["fact"]["status"], "pending")
        self.assertNotIn("approved", response["fact"]["status"])

        status, error = self.request("POST", "/api/facts/review", {"id": response["fact"]["id"], "status": "accepted"})
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "validation_error"})

        status, reviewed = self.request(
            "POST",
            "/api/facts/review",
            {"id": response["fact"]["id"], "status": "accepted", "reason": "manual primary-source check"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["fact"]["status"], "accepted")

        status, facts = self.request("GET", "/api/facts?company_id=demo")
        self.assertEqual(status, 200)
        self.assertEqual(facts["facts"][0]["company_id"], "demo")
        status, audit = self.request("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertTrue(any(event["action"] == "review" for event in audit["audit"]))

    def test_no_llm_and_calculation_routes(self):
        status, error = self.request("POST", "/api/llm/draft", {"evidence": []})
        self.assertEqual(status, 403)
        self.assertEqual(error, {"error": "llm_disabled"})

        status, result = self.request("POST", "/api/calculate", {"operation": "growth", "current": 120, "previous": 100})
        self.assertEqual(status, 200)
        self.assertEqual(result["operation"], "growth")
        self.assertAlmostEqual(result["value"], 0.2)

    def test_verified_facts_are_separate_and_include_source_metadata(self):
        status, result = self.request("GET", "/api/verified-facts")
        self.assertEqual(status, 200)
        self.assertTrue(result["facts"])
        first = result["facts"][0]
        self.assertIn(first["status"], {"verified", "manual_primary_source"})
        self.assertIn("source", first)
        self.assertIn("source_url", first["source"])
        self.assertIn("url", first["source"])
        self.assertIn("title", first["source"])

        status, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn("verified_facts", config)
        self.assertNotIn("api_key", json.dumps(config))

    def test_strict_verified_loader_filters_unverified_rows(self):
        original = server.load_json
        try:
            server.load_json = lambda _path, _fallback: [
                {"metric_key": "ok", "status": "verified", "source_url": "https://example.test/ok"},
                {"metric_key": "legacy", "source_url": "https://example.test/legacy"},
                {"metric_key": "bad", "status": "pending", "source_url": "https://example.test/bad"},
                {"metric_key": "invalid", "status": 1, "source_url": "https://example.test/invalid"},
            ]
            facts = server.load_verified_facts()
        finally:
            server.load_json = original
        self.assertEqual([fact["metric_key"] for fact in facts], ["ok", "legacy"])
        self.assertEqual(facts[0]["source"]["url"], "https://example.test/ok")
        self.assertEqual(facts[1]["status"], "manual_primary_source")

    def test_collector_routes_delegate_without_network_or_auth_bypass(self):
        status, config = self.request("GET", "/api/collection/status")
        self.assertEqual(status, 200)
        self.assertFalse(config["enabled"])
        self.assertEqual(self.collector.calls[-1][0], "config")

        status, items = self.request("POST", "/api/collection/run", {})
        self.assertEqual(status, 200)
        self.assertEqual(items, {"items": []})
        self.assertEqual(self.collector.calls[-1], ("run_once",))

        status, inbox = self.request("GET", "/api/collection/inbox?status=pending")
        self.assertEqual(status, 200)
        self.assertEqual(inbox, {"items": []})

        status, configured = self.request(
            "POST",
            "/api/collection/config",
            {"enabled": False, "authorized": False, "feeds": []},
        )
        self.assertEqual(status, 200)
        self.assertFalse(configured["enabled"])
        self.assertEqual(self.collector.calls[-1][0], "config")

    def test_collector_configuration_rejects_unauthorized_enabled_request(self):
        status, error = self.request(
            "POST",
            "/api/collection/config",
            {
                "enabled": True,
                "authorized": False,
                "feeds": [{"url": "https://news.example.test/feed", "authorized": True}],
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "validation_error"})
        self.assertFalse(any(call[0] == "run_once" for call in self.collector.calls))

    def test_collector_review_route_keeps_facts_unapproved(self):
        self.collector.items = [{"id": "item-1", "status": "pending", "facts_approved": False}]
        status, reviewed = self.request("POST", "/api/collection/review", {"id": "item-1", "status": "accepted"})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["item"]["status"], "accepted")
        self.assertFalse(reviewed["item"]["facts_approved"])

    def test_generic_errors_do_not_leak_api_keys_or_paths(self):
        secret = "sk-test-secret"
        with patch.dict(os.environ, {"TMT_LLM_API_KEY": secret}, clear=False):
            status, error = self.request("POST", "/api/ingest", {"filename": "bad.py", "content_base64": "a"})
        self.assertEqual(status, 400)
        serialized = json.dumps(error)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(str(Path(self.tmp.name)), serialized)


if __name__ == "__main__":
    unittest.main()
