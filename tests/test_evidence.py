"""Local temporary-workspace tests, including optional real PDF/OCR coverage."""
import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import evidence_engine as engine
from workspace_db import FACT_REQUIRED_FIELDS, MAX_SOURCE_BYTES, WorkspaceDB


def valid_row(**changes):
    row = {
        "company_id": "demo-cloud", "metric_key": "revenue", "value": "130.5",
        "unit": "million", "currency": "USD", "period_start": "2025-01-01",
        "period_end": "2025-12-31", "period_type": "FY", "accounting_basis": "DEMO_ONLY",
        "scope": "illustrative-demo",
    }
    row.update(changes)
    return row


def csv_bytes(rows, columns=engine.FACT_COLUMNS):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class TestWorkspaceDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fact_payload(self, **changes):
        imported = self.db.import_bytes("filing.txt", b"DEMO ONLY: revenue = 130.5 million USD")
        return valid_row(evidence_id=imported["evidence"][0]["id"], **changes)

    def test_text_import_locators_and_sha_storage(self):
        content = "Revenue grew\nfrom 10 to 12.".encode()
        with mock.patch("socket.create_connection", side_effect=AssertionError("no network")):
            result = self.db.import_bytes("../../secret.txt", content, company_id="demo-cloud")
        self.assertEqual(set(result), {"source", "evidence", "evidence_count", "pending_fact_count"})
        self.assertEqual(result["evidence_count"], 1)
        source = result["source"]
        self.assertEqual(source["filename"], "secret.txt")
        storage = Path(source["storage_path"])
        self.assertEqual(storage.parent, Path(self.tmp.name).resolve() / "sources")
        self.assertEqual(storage.stem, engine.sha256_bytes(content))
        self.assertEqual(storage.read_bytes(), content)
        self.assertEqual(result["evidence"][0]["locator_value"], "L1-L2")
        self.assertEqual(result["evidence"][0]["quote"], content.decode())
        self.assertEqual(source["company_id"], "demo-cloud")

    def test_dedup_does_not_duplicate_evidence_facts_or_reset_reviews(self):
        data = csv_bytes([valid_row()])
        first = self.db.import_bytes("facts.csv", data)
        fact = self.db.facts()[0]
        self.db.review_fact(fact["id"], "accepted", "manual check")
        before = self.db.stats()
        second = self.db.import_bytes(r"C:\private\renamed.csv", data, company_id="other")
        self.assertEqual(first["source"], second["source"])
        self.assertEqual(before, self.db.stats())
        self.assertEqual(second["pending_fact_count"], 0)
        self.assertEqual(self.db.facts()[0]["status"], "accepted")

    def test_csv_proposes_pending_and_links_actual_physical_row(self):
        data = csv_bytes([valid_row(scope="line one\nline two"), valid_row(metric_key="capex")])
        result = self.db.import_bytes("facts.csv", data)
        self.assertEqual(result["pending_fact_count"], 2)
        self.assertEqual([e["locator_value"] for e in result["evidence"]], ["row 2-3", "row 4"])
        links = {e["id"] for e in result["evidence"]}
        for fact in self.db.facts():
            self.assertEqual(fact["status"], "pending")
            self.assertIn(fact["evidence_id"], links)
        self.assertEqual(len(self.db.audit()), 5)  # source, 2 evidence, 2 proposals

    def test_csv_cannot_choose_approval_id_or_evidence(self):
        extras = ("status", "id", "evidence_id")
        row = valid_row(status="accepted", id="injected", evidence_id="forged-link")
        self.db.import_bytes("facts.csv", csv_bytes([row], (*engine.FACT_COLUMNS, *extras)))
        fact = self.db.facts()[0]
        self.assertEqual(fact["status"], "pending")
        self.assertNotEqual(fact["id"], "injected")
        self.assertNotEqual(fact["evidence_id"], "forged-link")

    def test_bad_csv_values_preserve_evidence_and_warn(self):
        rows = [valid_row(value="NaN"), valid_row(scope=""), valid_row(company_id=""), valid_row()]
        result = self.db.import_bytes("facts.csv", csv_bytes(rows), company_id="do-not-fill-in")
        self.assertEqual(result["evidence_count"], 4)
        self.assertEqual(result["pending_fact_count"], 1)
        self.assertEqual(len(result["source"]["warnings"]), 3)
        self.assertIn("row 2", result["source"]["warnings"][0])
        imported = next(a for a in self.db.audit() if a["entity_type"] == "source")
        self.assertEqual(imported["after"]["warnings"], result["source"]["warnings"])

    def test_unmatched_csv_is_evidence_only(self):
        result = self.db.import_bytes("generic.csv", b"name,amount\nCloud,100\n")
        self.assertEqual(result["pending_fact_count"], 0)
        self.assertEqual(result["evidence_count"], 1)
        self.assertTrue(result["source"]["warnings"])

    def test_malformed_inputs_leave_no_rows_files_or_audit(self):
        bad = [
            ("bad.json", b'{"value": NaN}'), ("bad.json", b'{"value": Infinity}'),
            ("bad.json", b'{"value": 1e9999}'), ("bad.json", b'{'),
            ("bad.csv", b'a,b\n1,2,3\n'), ("bad.csv", b'a,b\n1\n'),
            ("bad.csv", b'a,a\n1,2\n'), ("bad.csv", b'a,b\n"unterminated'),
            ("bad.csv", b''), ("bad.pdf", b'not a pdf'),
            ("bad.txt", b'\xff'), ("bad.txt", b'abc\x00xyz'),
            ("script.py", b'print(1)'), ("empty-name", b'hello'),
        ]
        for name, content in bad:
            with self.subTest(name=name, content=content):
                with self.assertRaises(ValueError):
                    self.db.import_bytes(name, content)
                self.assertEqual(self.db.stats()["sources"], 0)
                self.assertEqual(self.db.stats()["audit"], 0)
                self.assertEqual(list(self.db.storage_dir.iterdir()), [])

    def test_size_limit_exact_boundary_and_type_validation(self):
        with self.assertRaises(ValueError):
            self.db.import_bytes("too-big.txt", b"x" * (MAX_SOURCE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.db.import_bytes("not-bytes.txt", "/etc/passwd")
        with self.assertRaises(ValueError):
            self.db.import_bytes("not-bool.txt", b"hello", ocr="false")
        # Mock extraction to test the exact size boundary without adding a
        # multi-megabyte quote to SQLite/audit during a unit test.
        with mock.patch.object(engine, "extract_bytes", return_value=("", [], [])):
            result = self.db.import_bytes("exact.txt", b"x" * MAX_SOURCE_BYTES)
        self.assertEqual(result["source"]["size_bytes"], MAX_SOURCE_BYTES)

    def test_every_required_fact_field_is_enforced(self):
        base = self.fact_payload()
        for field in FACT_REQUIRED_FIELDS:
            for invalid in (None, "", [], False):
                payload = {**base, field: invalid}
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    self.db.add_fact(payload)
            with self.subTest(missing=field), self.assertRaises(ValueError):
                self.db.add_fact({key: value for key, value in base.items() if key != field})
        self.assertEqual(self.db.stats()["facts"], 0)

    def test_finite_values_periods_evidence_and_states(self):
        base = self.fact_payload()
        for value in (float("nan"), float("inf"), -float("inf"), "NaN", "1e9999", 10 ** 1000, True):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                self.db.add_fact({**base, "value": value})
        for changes in ({"evidence_id": "missing"}, {"period_start": "yesterday"},
                        {"period_start": "2025-02-29"}, {"period_end": "2024-01-01"},
                        {"status": "approved"}, {"status": "accepted"}, {"status": "rejected"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.db.add_fact({**base, **changes})
        zero = self.db.add_fact({**base, "value": 0})
        self.assertEqual(zero["value"], 0)
        self.assertEqual(zero["status"], "pending")

    def test_review_states_reason_and_audit_before_after(self):
        fact = self.db.add_fact(self.fact_payload())
        for status, reason in (("approved", "test"), ("accepted", ""), ("accepted", None)):
            with self.assertRaises(ValueError):
                self.db.review_fact(fact["id"], status, reason)
        with self.assertRaises(ValueError):
            self.db.review_fact("missing", "accepted", "test")
        previous = "pending"
        for status in ("accepted", "rejected", "pending"):
            result = self.db.review_fact(fact["id"], status, "核对官方年报")
            self.assertEqual(result["status"], status)
            event = [a for a in self.db.audit() if a["action"] == "review"][-1]
            self.assertEqual(event["before"]["status"], previous)
            self.assertEqual(event["after"]["status"], status)
            self.assertEqual(event["reason"], "核对官方年报")
            previous = status
        # Public audit contains parsed snapshots only, not inconsistent JSON
        # fields for records whose before was null.
        self.assertTrue(all("before_json" not in a and "after_json" not in a for a in self.db.audit()))

    def test_conflicts_detect_pending_and_basis_changes_then_clear_on_reject(self):
        base = self.fact_payload()
        first = self.db.add_fact({**base, "value": 100})
        same = self.db.add_fact({**base, "value": 100})
        self.assertFalse(same["conflict"])
        other = self.db.add_fact({**base, "value": 101, "accounting_basis": "IFRS"})
        self.assertTrue(other["conflict"])
        self.assertEqual(set(other["conflicts_with"]), {first["id"], same["id"]})
        self.assertTrue(all(f["conflict"] for f in self.db.facts()))
        accepted = self.db.review_fact(other["id"], "accepted", "keep conflicting filing for review")
        self.assertTrue(accepted["conflict"])
        self.assertEqual(self.db.stats()["conflicting_facts"], 3)
        self.db.review_fact(other["id"], "rejected", "wrong basis")
        self.assertTrue(all(not f["conflict"] for f in self.db.facts()))
        self.assertEqual(self.db.stats()["facts"], 3)
        self.assertTrue(any(a["action"] == "conflict_alert" for a in self.db.audit()))

    def test_company_period_scope_separate_conflicts(self):
        base = self.fact_payload()
        self.db.add_fact(base)
        changes = ({"company_id": "another"}, {"scope": "segment-cloud"},
                   {"period_start": "2026-01-01", "period_end": "2026-12-31"},
                   {"metric_key": "capex"})
        for change in changes:
            self.assertFalse(self.db.add_fact({**base, **change, "value": 999})["conflict"])
        self.assertEqual(len(self.db.facts("another")), 1)

    def test_search_fts_and_no_fts_cjk_literal_wildcard(self):
        first = self.db.import_bytes("one.md", "云计算收入增长 Cloud revenue grows 20%".encode())
        self.db.import_bytes("two.txt", b"revenue decline elsewhere")
        for enabled in (self.db._fts_available, False):
            self.db._fts_available = enabled
            self.assertEqual(len(self.db.evidence("云计算")), 1)
            self.assertEqual(len(self.db.evidence("CLOUD")), 1)
            self.assertEqual(len(self.db.evidence("%")), 1)
            self.assertEqual(len(self.db.evidence("revenue", first["source"]["id"])), 1)
            self.assertEqual(len(self.db.evidence(source_id="missing")), 0)
            self.db.evidence('" OR revenue:*')  # plain terms, no FTS syntax errors

    def test_reopen_persists_facts_evidence_and_audit(self):
        fact = self.db.add_fact(self.fact_payload())
        self.db.review_fact(fact["id"], "accepted", "checked")
        before = self.db.stats()
        with WorkspaceDB(self.tmp.name) as reopened:
            self.assertEqual(reopened.stats(), before)
            self.assertEqual(reopened.facts()[0]["status"], "accepted")
            self.assertEqual(len(reopened.evidence("revenue")), 1)

    def test_simultaneous_imports_dedup_across_db_instances(self):
        with WorkspaceDB(self.tmp.name) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(db.import_bytes, "facts.csv", csv_bytes([valid_row()]))
                           for db in (self.db, second)]
                results = [f.result(timeout=20) for f in futures]
        self.assertEqual(results[0]["source"]["id"], results[1]["source"]["id"])
        self.assertEqual(self.db.stats()["sources"], 1)
        self.assertEqual(self.db.stats()["facts"], 1)

    def test_failed_audit_rolls_back_import_and_generated_file(self):
        with mock.patch.object(self.db, "_audit", side_effect=RuntimeError("simulated disk error")):
            with self.assertRaises(RuntimeError):
                self.db.import_bytes("test.txt", b"failure simulation")
        self.assertEqual(self.db.stats()["sources"], 0)
        self.assertEqual(list(self.db.storage_dir.iterdir()), [])
        self.db.import_bytes("test.txt", b"failure simulation")
        self.assertEqual(self.db.stats()["sources"], 1)

    def test_sql_constraints_reject_invalid_fact_status_and_orphan(self):
        fact = self.db.add_fact(self.fact_payload())
        with self.assertRaises(sqlite3.IntegrityError), self.db._conn:
            self.db._conn.execute("UPDATE facts SET status='approved' WHERE id=?", (fact["id"],))
        with self.assertRaises(sqlite3.IntegrityError), self.db._conn:
            self.db._conn.execute("UPDATE facts SET evidence_id='missing' WHERE id=?", (fact["id"],))

    def test_example_csv_is_explicitly_demo_and_importable(self):
        path = Path(__file__).resolve().parents[1] / "data" / "example-facts.csv"
        result = self.db.import_bytes(path.name, path.read_bytes())
        self.assertGreater(result["pending_fact_count"], 0)
        self.assertTrue(all(f["status"] == "pending" and f["accounting_basis"] == "DEMO_ONLY"
                            for f in self.db.facts()))

    def test_standalone_ingest_compatibility(self):
        path = Path(self.tmp.name) / "source.txt"
        path.write_text("first\nsecond", encoding="utf-8")
        result = engine.ingest_file(str(path))
        self.assertEqual(result["evidence_count"], 1)
        self.assertEqual(engine.search_evidence(result["evidence"], "second"), result["evidence"])


class TestPDFSafety(unittest.TestCase):
    def test_missing_optional_dependencies_returns_warning(self):
        with mock.patch.object(engine, "_run_pdf_operation", side_effect=ImportError("optional dependencies missing")):
            text, evidence, warnings = engine.extract_pdf_bytes(b"%PDF-1.4", "test", ocr=True)
        self.assertEqual((text, evidence), ("", []))
        self.assertIn("missing", warnings[0])

    def test_language_selection_checks_actual_installed_packs(self):
        cases = [(["eng", "chi_sim"], "eng+chi_sim", False), (["eng"], "eng", True),
                 (["chi_sim"], "chi_sim", True), (["osd"], None, True)]
        for available, expected, warn in cases:
            with self.subTest(available=available), mock.patch.object(engine, "_run_pdf_operation", return_value=available):
                language, warnings = engine._available_ocr_language()
                self.assertEqual(language, expected)
                self.assertEqual(bool(warnings), warn)

    def test_page_cap_cannot_be_raised_by_caller(self):
        info = {"count": 51, "text_available": True, "warnings": []}
        with mock.patch.object(engine, "_run_pdf_operation", return_value=info) as job:
            with self.assertRaisesRegex(ValueError, "50"):
                engine.extract_pdf_bytes(b"%PDF-1.4", "test", max_pages=999)
            self.assertEqual(job.call_count, 1)

    def test_ocr_uses_blank_pages_only_and_page_timeouts_warn(self):
        def worker(data, operation, **options):
            if operation == "count":
                return {"count": 3, "text_available": True, "warnings": []}
            if operation == "languages":
                return ["eng"]
            if operation == "text":
                return "existing text" if options["index"] == 0 else ""
            if options["index"] == 1:
                return "scanned revenue 123"
            raise TimeoutError("OCR timeout")
        with mock.patch.object(engine, "_run_pdf_operation", side_effect=worker) as job:
            text, evidence, warnings = engine.extract_pdf_bytes(b"%PDF-1.4", "test", ocr=True)
        self.assertIn("existing text", text)
        self.assertIn("scanned revenue", text)
        self.assertEqual([e["page_number"] for e in evidence], [1, 2])
        self.assertTrue(any("chi_sim" in warning for warning in warnings))
        self.assertTrue(any("第 3 页" in warning and "timeout" in warning for warning in warnings))
        ocr_calls = [c for c in job.call_args_list if c.args[1] == "ocr"]
        self.assertEqual([c.kwargs["index"] for c in ocr_calls], [1, 2])

    def test_hard_timeout_kills_worker_process(self):
        # A tiny real deadline exercises process cleanup without an unbounded
        # sleeping thread. No PDF dependencies are needed to reach the timer.
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            engine._run_pdf_operation(b"%PDF-1.4", "count", page_timeout=0.000001)
        self.assertLess(time.monotonic() - start, 3)

    def test_resources_close_on_ocr_failure_and_timeout_is_passed(self):
        image, bitmap, page, document = [mock.Mock() for _ in range(4)]
        page.get_size.return_value = (612, 792)
        page.render.return_value = bitmap
        bitmap.to_pil.return_value = image
        pdfium = types.SimpleNamespace(PdfDocument=mock.Mock(return_value=document))
        # Special methods must live on a MagicMock to support indexing.
        document = mock.MagicMock()
        document.__getitem__.return_value = page
        pdfium.PdfDocument.return_value = document
        tesseract = types.SimpleNamespace(image_to_string=mock.Mock(side_effect=RuntimeError("OCR failure")))
        with mock.patch.dict(sys.modules, {"pypdfium2": pdfium, "pytesseract": tesseract}):
            with self.assertRaisesRegex(RuntimeError, "OCR failure"):
                engine._pdf_operation(b"%PDF-1.4", {"operation": "ocr", "index": 0,
                                                    "language": "eng", "timeout": 3})
        for resource in (image, bitmap, page, document):
            resource.close.assert_called_once()
        self.assertEqual(tesseract.image_to_string.call_args.kwargs["timeout"], 3)

    def test_real_text_pdf_and_51_page_rejection_optional(self):
        try:
            import pdfplumber
            from reportlab.pdfgen import canvas
        except ImportError:
            self.skipTest("optional PDF dependencies unavailable")
        with tempfile.TemporaryDirectory() as state, WorkspaceDB(state) as db:
            output = io.BytesIO()
            pdf = canvas.Canvas(output)
            pdf.drawString(72, 720, "Revenue 130.5 million USD")
            pdf.save()
            result = db.import_bytes("one.pdf", output.getvalue())
            self.assertIn("130.5", result["evidence"][0]["quote"])
            self.assertEqual(result["evidence"][0]["page_number"], 1)
            too_many = io.BytesIO()
            pdf = canvas.Canvas(too_many)
            for _ in range(51):
                pdf.showPage()
            pdf.save()
            with self.assertRaisesRegex(ValueError, "50"):
                db.import_bytes("51.pdf", too_many.getvalue(), ocr=True)
            self.assertEqual(db.stats()["sources"], 1)
            with self.assertRaises(ValueError):
                db.import_bytes("corrupt.pdf", b"%PDF-1.4\ncorrupt structure")

    def test_real_synthetic_single_page_ocr_optional(self):
        try:
            import pypdfium2
            import pytesseract
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.skipTest("optional OCR packages unavailable")
        if not shutil.which("tesseract"):
            self.skipTest("local Tesseract unavailable")
        languages = pytesseract.get_languages(config="")
        if "eng" not in languages:
            self.skipTest("eng language pack unavailable")
        font_paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "/System/Library/Fonts/Supplemental/Arial.ttf"]
        font_path = next((p for p in font_paths if Path(p).exists()), None)
        if not font_path:
            self.skipTest("suitable local test font unavailable")
        output = io.BytesIO()
        with Image.new("RGB", (1400, 400), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.text((60, 100), "REVENUE 123 MILLION USD", fill="black",
                      font=ImageFont.truetype(font_path, 64))
            image.save(output, format="PDF", resolution=144)
        with tempfile.TemporaryDirectory() as state, WorkspaceDB(state) as db:
            result = db.import_bytes("synthetic-scan.pdf", output.getvalue(), ocr=True)
            text = " ".join(e["quote"] for e in result["evidence"])
            self.assertIn("REVENUE", text.upper())
            self.assertIn("123", text)
            self.assertEqual(result["pending_fact_count"], 0)
            self.assertEqual(result["evidence"][0]["page_number"], 1)
            self.assertTrue(any("OCR" in w for w in result["source"]["warnings"]))
            if "chi_sim" not in languages:
                self.assertTrue(any("chi_sim" in w and "eng" in w for w in result["source"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
