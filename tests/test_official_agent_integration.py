import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

import server
from agent_orchestrator import AgentOrchestrator, OrchestratorError


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class FakeCollector:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.calls = 0
        self.scopes = []

    def run_once(self):
        self.calls += 1
        return list(self.items)

    def run_scoped(self, source_scope):
        self.calls += 1
        self.scopes.append(source_scope)
        return list(self.items)


class OfficialDataIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = server.AtlasStore()
        cls.company_ids = {company["id"] for company in cls.store.companies}

    def test_official_loader_accepts_mapping_and_verified_non_gaap(self):
        facts = server.load_official_facts()
        self.assertEqual(len(facts), 60)
        self.assertEqual({fact["company_id"] for fact in facts}, self.company_ids)
        self.assertIn("verified_non_gaap", {fact["status"] for fact in facts})
        self.assertTrue(all(fact["source"].get("url") for fact in facts))
        self.assertTrue(all(fact["source"].get("locator") for fact in facts))

    def test_all_nine_snapshots_are_official_and_never_demo(self):
        self.assertEqual(len(self.company_ids), 9)
        allowed = {"direct", "derived", "missing"}
        for company_id in self.company_ids:
            snapshot = self.store.snapshot(company_id)
            self.assertEqual(snapshot["data_mode"], "official_historical")
            self.assertGreater(snapshot["official_fact_count"], 0)
            self.assertTrue(snapshot["metrics"])
            for metric in snapshot["metrics"]:
                self.assertNotEqual(metric.get("status"), "demo")
                self.assertIn(metric.get("status"), allowed)
                self.assertNotIn("demo", str(metric.get("quality", "")).lower())

    def test_metric_statuses_have_direct_derived_and_missing_examples(self):
        nvidia = {metric["key"]: metric for metric in self.store.snapshot("nvidia")["metrics"]}
        self.assertEqual(nvidia["revenue"]["status"], "direct")
        self.assertEqual(nvidia["operating_margin"]["status"], "derived")
        self.assertEqual(nvidia["rd_ratio"]["status"], "missing")
        self.assertIsNone(nvidia["rd_ratio"]["value"])
        self.assertEqual(nvidia["rd_ratio"]["missing_reason"], "requires_research_and_development_and_revenue")

    def test_derived_operating_margin_is_auditable(self):
        metrics = {metric["key"]: metric for metric in self.store.snapshot("microsoft")["metrics"]}
        margin = metrics["operating_margin"]
        self.assertEqual(margin["status"], "derived")
        self.assertAlmostEqual(margin["value"], 128528 / 281724)
        self.assertEqual(margin["formula"], "operating_income / revenue")
        self.assertEqual(margin["calculation"]["inputs"][0]["key"], "operating_income")
        self.assertIn("Operating income", margin["source_locator"])
        self.assertIn("Revenue", margin["source_locator"])

    def test_missing_cloud_fields_do_not_fall_back_to_seed_demo_values(self):
        for company_id in ("microsoft", "alibaba", "baidu"):
            metrics = {metric["key"]: metric for metric in self.store.snapshot(company_id)["metrics"]}
            self.assertIn("cloud_share", metrics)
            self.assertEqual(metrics["cloud_share"]["status"], "missing")
            self.assertIsNone(metrics["cloud_share"]["value"])
            self.assertNotIn("USD bn", metrics["cloud_share"].get("display_value", ""))

    def test_missing_capex_rd_and_cloud_render_as_undisclosed_not_demo(self):
        # Alibaba now has an official capital-expenditures disclosure; this is
        # no longer a missing field. The UI must still avoid the legacy demo USD
        # value and preserve the official RMB source unit.
        alibaba = {metric["key"]: metric for metric in self.store.snapshot("alibaba")["metrics"]}
        self.assertEqual(alibaba["capex"]["status"], "direct")
        self.assertEqual(alibaba["capex"]["unit"], "RMB million")
        self.assertEqual(alibaba["capex"]["value"], 85972)
        self.assertNotIn("USD bn", alibaba["capex"].get("display_value", ""))

        # Amazon reports no R&D expense fact, so the ratio stays missing instead
        # of borrowing the demo research-intensity value.
        amazon = {metric["key"]: metric for metric in self.store.snapshot("amazon")["metrics"]}
        self.assertEqual(amazon["rd_ratio"]["status"], "missing")
        self.assertIsNone(amazon["rd_ratio"]["value"])
        self.assertEqual(amazon["rd_ratio"]["display_value"], "未披露")
        self.assertEqual(amazon["rd_ratio"]["missing_reason"], "requires_research_and_development_and_revenue")

        for company_id in ("microsoft", "alibaba", "baidu"):
            metrics = {metric["key"]: metric for metric in self.store.snapshot(company_id)["metrics"]}
            cloud = metrics["cloud_share"]
            self.assertEqual(cloud["status"], "missing")
            self.assertIsNone(cloud["value"])
            self.assertEqual(cloud["display_value"], "未披露")

    def test_snapshot_has_no_duplicate_data_mode_and_provenance_trace(self):
        snapshot = self.store.snapshot("microsoft")
        self.assertEqual(list(snapshot).count("data_mode"), 1)
        for metric in snapshot["metrics"]:
            self.assertIn("trace", metric)
            self.assertEqual(metric["trace"]["status"], metric["status"])
            if metric["status"] in {"direct", "derived"}:
                self.assertTrue(metric["source_locator"])
                self.assertTrue(metric["source_url"])

    def test_sector_scan_separates_missing_and_not_comparable_without_false_rank(self):
        scan = self.store.sector_scan("云/SaaS")
        self.assertEqual(set(scan), {
            "sector", "rows", "eligible_rows", "not_comparable_rows", "missing_rows",
            "rank_groups", "columns", "method", "as_of", "data_mode", "official_fact_count",
        })
        self.assertEqual(scan["data_mode"], "official_historical")
        self.assertTrue(scan["rows"])
        self.assertTrue(scan["not_comparable_rows"])
        self.assertEqual(scan["eligible_rows"], [])
        self.assertEqual(scan["rank_groups"], [])
        for row in scan["rows"]:
            growth = row["revenue_growth"]
            if growth["status"] == "missing":
                self.assertNotIn("growth_rank", row)
            else:
                self.assertNotIn("growth_rank", row)
                self.assertIn(row, scan["not_comparable_rows"])

    def test_sector_scan_never_mixes_currency_or_reporting_scope_in_rank_groups(self):
        for sector in self.store.sectors():
            scan = self.store.sector_scan(sector)
            for group in scan["rank_groups"]:
                rows = [row for row in scan["eligible_rows"] if row.get("rank_group", {}).get("id") == group["id"]]
                self.assertGreaterEqual(len(rows), 2)
                self.assertEqual({row["revenue_growth"].get("currency") for row in rows}, {group["currency"]})
                self.assertEqual({row["revenue_growth"].get("scope") for row in rows}, {group["scope"]})
                self.assertEqual({row["revenue_growth"].get("period") for row in rows}, {group["period"]})

    def test_sector_scan_ranks_only_identical_dimensions_and_keeps_buckets_disjoint(self):
        """Exercise the eligible ranking path with a temp synthetic corpus.

        The curated corpus uses company-specific scope labels, so no real sector
        ever produces a rank group; this fixture makes two USD and two EUR
        companies fully comparable, one company comparable with nothing, and one
        company without a prior-period revenue fact.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def fact(company_id, *, currency="USD", scope="Shared scope", value=120, prior=100):
            record = {
                "company_id": company_id,
                "metric_key": "revenue",
                "label": "Revenue",
                "value": value,
                "unit": f"{currency} million",
                "currency": currency,
                "period": "FY2025",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "period_type": "calendar_year",
                "accounting_basis": "US GAAP",
                "scope": scope,
                "status": "verified",
                "source_url": f"https://example.test/{company_id}",
                "source_title": f"{company_id} annual results",
                "published_at": "2026-02-01",
                "locator": "Income statement; Revenue",
            }
            if prior is not None:
                record["prior_value"] = prior
            return record

        companies = [
            {
                "id": company_id,
                "name": company_id,
                "name_zh": company_id,
                "ticker": company_id.upper(),
                "country": "US",
                "listing": "TEST",
                "sector": "合成赛道",
                "description": "synthetic official fact fixture",
                "source_ids": [],
                "metrics": [{"key": "revenue", "label": "总收入"}],
            }
            for company_id in ("alpha", "delta", "euro1", "euro2", "beta", "omega")
        ]
        facts = [
            fact("alpha", value=120, prior=100),
            fact("delta", value=150, prior=100),
            fact("euro1", currency="EUR", value=110, prior=100),
            fact("euro2", currency="EUR", value=105, prior=100),
            fact("beta", scope="Solo scope", value=130, prior=100),
            fact("omega", value=90, prior=None),
        ]
        (root / "seed.json").write_text(
            json.dumps({"dataset": {"as_of": "2026-10-09"}, "companies": companies, "events": [], "research_prompts": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        (root / "facts.json").write_text(json.dumps({"facts": facts}, ensure_ascii=False), encoding="utf-8")

        original_seed = server.SEED_PATH
        original_facts = server.OFFICIAL_FACTS_PATH
        server.SEED_PATH = root / "seed.json"
        server.OFFICIAL_FACTS_PATH = root / "facts.json"
        try:
            store = server.AtlasStore()
            scan = store.sector_scan("合成赛道")
        finally:
            server.SEED_PATH = original_seed
            server.OFFICIAL_FACTS_PATH = original_facts

        self.assertEqual(scan["data_mode"], "official_historical")
        self.assertEqual(scan["official_fact_count"], 6)
        self.assertEqual({row["company_id"] for row in scan["eligible_rows"]}, {"alpha", "delta", "euro1", "euro2"})
        self.assertEqual({row["company_id"] for row in scan["not_comparable_rows"]}, {"beta"})
        self.assertEqual({row["company_id"] for row in scan["missing_rows"]}, {"omega"})
        self.assertEqual(
            {row["company_id"] for row in scan["rows"]},
            {"alpha", "delta", "euro1", "euro2", "beta", "omega"},
        )

        growth_values = {row["company_id"]: row["revenue_growth"]["value"] for row in scan["eligible_rows"]}
        self.assertAlmostEqual(growth_values["alpha"], 0.2)
        self.assertAlmostEqual(growth_values["delta"], 0.5)

        groups = {group["currency"]: group for group in scan["rank_groups"]}
        self.assertEqual(set(groups), {"USD", "EUR"})
        for group in scan["rank_groups"]:
            members = [row for row in scan["eligible_rows"] if row["rank_group"]["id"] == group["id"]]
            self.assertGreaterEqual(len(members), 2)
            self.assertEqual(len(members), group["size"])
            # A rank group must never mix currencies or contain unranked rows.
            self.assertEqual({row["revenue_growth"]["currency"] for row in members}, {group["currency"]})
            self.assertEqual(sorted(row["growth_rank"] for row in members), list(range(1, len(members) + 1)))
            ordered = sorted(members, key=lambda row: row["growth_rank"])
            values = [row["revenue_growth"]["value"] for row in ordered]
            self.assertEqual(values, sorted(values, reverse=True))
            for row in members:
                self.assertEqual(row["comparability"], "eligible")
                self.assertEqual(row["comparability_reason"], "matching_currency_period_basis_scope")

        usd_order = [
            row["company_id"]
            for row in sorted(
                (row for row in scan["eligible_rows"] if row["revenue_growth"]["currency"] == "USD"),
                key=lambda row: row["growth_rank"],
            )
        ]
        self.assertEqual(usd_order, ["delta", "alpha"])

        beta = scan["not_comparable_rows"][0]
        self.assertEqual(beta["comparability"], "not_comparable")
        self.assertEqual(beta["comparability_reason"], "fewer_than_two_matching_facts")
        omega = scan["missing_rows"][0]
        self.assertEqual(omega["comparability"], "missing")
        self.assertEqual(omega["revenue_growth"]["status"], "missing")
        self.assertEqual(omega["revenue_growth"]["missing_reason"], "requires_revenue_prior_value")
        for row in scan["missing_rows"] + scan["not_comparable_rows"]:
            self.assertNotIn("growth_rank", row)
            self.assertNotIn(row["company_id"], {r["company_id"] for r in scan["eligible_rows"]})

    def test_loader_supports_bare_list_and_preserves_non_gaap(self):
        original = server.load_json
        try:
            server.load_json = lambda _path, _fallback: [
                {"company_id": "x", "metric_key": "fcf", "value": 1, "status": "verified_non_gaap", "source_url": "https://example.test/x", "locator": "p1"},
                {"company_id": "x", "metric_key": "bad", "value": 2, "status": "pending", "source_url": "https://example.test/bad"},
            ]
            facts = server.load_official_facts()
        finally:
            server.load_json = original
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["status"], "verified_non_gaap")


class AgentIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, collector=None, handlers=None):
        return AgentOrchestrator(
            self.tmp.name,
            collector=collector,
            research_handlers=handlers,
            clock=self.clock,
        )

    @staticmethod
    def research_payload(kind="company_snapshot_refresh", **overrides):
        payload = {
            "kind": kind,
            "name": "official research job",
            "schedule": {"interval_minutes": 30},
            "company_id": "microsoft",
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def collection_payload(**overrides):
        payload = {
            "kind": "collection_cycle",
            "name": "authorized source cycle",
            "schedule": {"interval_minutes": 15},
            "source_scope": ["https://example.test/feed"],
            "authorization": True,
        }
        payload.update(overrides)
        return payload

    def test_research_plan_starts_as_draft_and_collection_requires_scope_and_auth(self):
        orchestrator = self.make()
        plan = orchestrator.create_plan(self.research_payload(sector="云/SaaS"))
        self.assertEqual(plan["status"], "draft")
        self.assertEqual(plan["source_scope"], None)
        with self.assertRaisesRegex(OrchestratorError, "source_scope_required"):
            orchestrator.create_plan({"kind": "collection_cycle", "schedule": {"interval_minutes": 15}, "authorization": True})
        with self.assertRaisesRegex(OrchestratorError, "authorization_required"):
            orchestrator.create_plan({"kind": "collection_cycle", "schedule": {"interval_minutes": 15}, "source_scope": ["x"]})

    def test_official_snapshot_handler_runs_and_stores_unapproved_draft_artifact(self):
        orchestrator = self.make(handlers={"company_snapshot_refresh": server._agent_company_snapshot})
        plan = orchestrator.create_plan(self.research_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "succeeded")
        state = json.loads(orchestrator.state_path.read_text(encoding="utf-8"))
        artifact = state["artifacts"][0]
        self.assertEqual(artifact["status"], "draft")
        self.assertFalse(artifact["payload"]["facts_approved"])
        self.assertEqual(artifact["payload"]["data_mode"], "official_historical")
        self.assertEqual(artifact["payload"]["result"]["company"]["id"], "microsoft")

    def test_memo_draft_handler_builds_reviewable_official_memo(self):
        orchestrator = self.make(handlers={"memo_draft": server._agent_memo_draft})
        plan = orchestrator.create_plan(self.research_payload(
            "memo_draft", sector="云/SaaS", notes="重点核对云业务口径。"
        ))
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "succeeded")
        state = json.loads(orchestrator.state_path.read_text(encoding="utf-8"))
        artifact = state["artifacts"][0]
        self.assertEqual(artifact["kind"], "memo_draft")
        self.assertEqual(artifact["status"], "draft")
        self.assertFalse(artifact["payload"]["facts_approved"])
        self.assertTrue(artifact["payload"]["review_required"])
        self.assertEqual(artifact["payload"]["data_mode"], "official_historical")
        memo = artifact["payload"]["result"]
        self.assertEqual(memo["title"], "微软公司快照｜云/SaaS赛道扫描")
        self.assertGreater(memo["evidence_count"], 0)
        self.assertIn("微软（Microsoft）", memo["markdown"])
        self.assertIn("研究边界", memo["markdown"])
        self.assertIn("来源", memo["markdown"])
        self.assertIn("<!doctype html>", memo["html"])

    def test_collection_run_creates_review_gate_and_never_approves_facts(self):
        collector = FakeCollector([{"id": "feed-item", "title": "Official draft", "url": "https://example.test/a", "facts_approved": True}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(self.collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "waiting_review")
        self.assertEqual(collector.scopes, [["https://example.test/feed"]])
        item = orchestrator.retrieval_items()[0]
        gate = orchestrator.review_gates()[0]
        self.assertFalse(item["facts_approved"])
        self.assertEqual(gate["item_id"], item["id"])
        orchestrator.decide_gate(gate["id"], "accepted", "checked source scope")
        self.assertFalse(orchestrator.retrieval_items()[0]["facts_approved"])

    def test_scheduler_tick_and_manual_run_are_idempotent(self):
        collector = FakeCollector([])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(self.collection_payload())
        orchestrator.approve_plan(plan["id"])
        first = orchestrator.tick()
        self.assertEqual(len(first), 1)
        self.assertEqual(orchestrator.tick(), [])
        self.assertEqual(collector.calls, 1)
        same = orchestrator.run_plan(plan["id"], scheduled_for=first[0]["scheduled_for"])
        self.assertEqual(same["id"], first[0]["id"])
        self.clock.advance(minutes=16)
        self.assertEqual(len(orchestrator.tick()), 1)
        self.assertEqual(collector.calls, 2)

    def test_handler_errors_are_safe_and_do_not_leak_details(self):
        def failing(_kind, _payload):
            raise RuntimeError("secret provider token")

        orchestrator = self.make(handlers={"memo_draft": failing})
        plan = orchestrator.create_plan(self.research_payload("memo_draft"))
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("secret provider token", json.dumps(orchestrator.runs()))


class AgentHTTPIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clock = MutableClock(datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc))
        cls.orchestrator = AgentOrchestrator(
            cls.tmp.name,
            collector=FakeCollector([{"id": "http-item", "title": "Feed draft"}]),
            research_handlers={"company_snapshot_refresh": server._agent_company_snapshot},
            clock=cls.clock,
        )
        cls.original_orchestrator = server.AGENT_ORCHESTRATOR
        server.AGENT_ORCHESTRATOR = cls.orchestrator
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server.AGENT_ORCHESTRATOR = cls.original_orchestrator
        cls.tmp.cleanup()

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

    def test_agent_plan_collection_routes_and_authorization(self):
        status, response = self.request("POST", "/api/agent/plans", {
            "kind": "collection_cycle",
            "schedule": {"interval_minutes": 15},
            "source_scope": ["https://example.test/feed"],
            "authorization": True,
        })
        self.assertEqual(status, 201)
        plan_id = response["plan"]["id"]
        self.assertEqual(response["plan"]["status"], "draft")
        status, listing = self.request("GET", "/api/agent/plans")
        self.assertEqual(status, 200)
        self.assertTrue(any(plan["id"] == plan_id for plan in listing["plans"]))
        status, detail = self.request("GET", f"/api/agent/plans/{plan_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["plan"]["id"], plan_id)

    def test_agent_approve_run_and_task_routes(self):
        status, created = self.request("POST", "/api/agent/plans", {
            "kind": "company_snapshot_refresh",
            "schedule": {"interval_minutes": 30},
            "company_id": "microsoft",
        })
        self.assertEqual(status, 201)
        plan_id = created["plan"]["id"]
        status, approved = self.request("POST", f"/api/agent/plans/{plan_id}/approve", {})
        self.assertEqual(status, 200)
        self.assertEqual(approved["plan"]["status"], "approved")
        status, run_response = self.request("POST", f"/api/agent/plans/{plan_id}/run", {})
        self.assertEqual(status, 200)
        run_id = run_response["run"]["id"]
        self.assertEqual(run_response["run"]["status"], "succeeded")
        status, runs = self.request("GET", "/api/agent/runs")
        self.assertEqual(status, 200)
        self.assertTrue(any(run["id"] == run_id for run in runs["runs"]))
        status, tasks = self.request("GET", f"/api/agent/runs/{run_id}/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(tasks["tasks"][0]["status"], "succeeded")

    def test_agent_collection_review_gate_and_retrieval_routes(self):
        status, created = self.request("POST", "/api/agent/plans", {
            "kind": "collection_cycle",
            "schedule": {"interval_minutes": 15},
            "source_scope": ["https://example.test/feed"],
            "authorization": True,
        })
        self.assertEqual(status, 201)
        plan_id = created["plan"]["id"]
        self.request("POST", f"/api/agent/plans/{plan_id}/approve", {})
        status, run_response = self.request("POST", f"/api/agent/plans/{plan_id}/run", {})
        self.assertEqual(status, 200)
        status, gates = self.request("GET", "/api/agent/review-gates?status=pending")
        self.assertEqual(status, 200)
        gate_id = gates["review_gates"][-1]["id"]
        item_id = gates["review_gates"][-1]["item_id"]
        status, items = self.request("GET", "/api/agent/retrieval-items?status=pending")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == item_id for item in items["retrieval_items"]))
        status, reviewed = self.request("POST", f"/api/agent/review-gates/{gate_id}/decision", {"decision": "accepted", "reason": "verified"})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["review_gate"]["status"], "accepted")
        status, item = self.request("POST", f"/api/agent/retrieval-items/{item_id}/review", {"status": "accepted"})
        self.assertEqual(status, 200)
        self.assertFalse(item["retrieval_item"]["facts_approved"])

    def test_agent_api_errors_are_generic_and_statused_as_bad_request(self):
        status, error = self.request("GET", "/api/agent/plans/not-a-plan")
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "plan_not_found"})
        status, error = self.request("POST", "/api/agent/plans", {"kind": "unknown", "schedule": {"interval_minutes": 15}})
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "plan_kind_invalid"})
        self.assertNotIn("Traceback", json.dumps(error))

    def test_memo_route_posts_official_memo_and_rejects_get(self):
        status, memo = self.request(
            "POST", "/api/memos", {"company_id": "microsoft", "notes": "重点核对云业务口径。"}
        )
        self.assertEqual(status, 200)
        self.assertIn("微软", memo["title"])
        self.assertGreater(memo["evidence_count"], 0)
        self.assertIn("微软（Microsoft）", memo["markdown"])
        self.assertIn("来源", memo["markdown"])
        self.assertIn("<!doctype html>", memo["html"])

        status, error = self.request("GET", "/api/memos/anything")
        self.assertEqual(status, 405)
        self.assertEqual(error, {"error": "memos are generated with POST /api/memos"})

    def test_unknown_routes_and_missing_entities_use_generic_errors(self):
        status, error = self.request("POST", "/api/does-not-exist", {"kind": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(error, {"error": "not_found"})

        status, error = self.request("GET", "/api/agent/runs/not-a-run/tasks")
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "run_not_found"})

        status, error = self.request(
            "POST",
            "/api/agent/review-gates/not-a-gate/decision",
            {"decision": "accepted", "reason": "checked the source"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "gate_not_found"})

        status, error = self.request("GET", "/api/agent/plans/not-a-plan")
        self.assertEqual(status, 400)
        self.assertEqual(set(error), {"error"})
        self.assertNotIn(str(Path(self.tmp.name)), json.dumps(error))


class SchedulerLoopTest(unittest.TestCase):
    def test_scheduler_loop_stops_on_event_without_import_time_thread(self):
        stop_event = threading.Event()
        calls = []
        original = server.AGENT_ORCHESTRATOR

        class FakeOrchestrator:
            def tick(self):
                calls.append("tick")
                stop_event.set()

        server.AGENT_ORCHESTRATOR = FakeOrchestrator()
        try:
            thread = threading.Thread(target=server._agent_scheduler_loop, args=(stop_event, 0.001), daemon=True)
            thread.start()
            thread.join(timeout=2)
        finally:
            server.AGENT_ORCHESTRATOR = original
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ["tick"])


if __name__ == "__main__":
    unittest.main()
