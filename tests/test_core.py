import tempfile
import unittest

import server


class TestAtlasCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.AtlasStore()
        self.original_local_sources_path = server.LOCAL_SOURCES_PATH
        server.LOCAL_SOURCES_PATH = server.Path(self.tmp.name) / "local_sources.json"

    def tearDown(self):
        server.LOCAL_SOURCES_PATH = self.original_local_sources_path
        self.tmp.cleanup()

    def test_seed_has_cross_market_tmt_companies(self):
        self.assertGreaterEqual(len(self.store.companies), 9)
        self.assertIn("AI基础设施/半导体", self.store.sectors())
        self.assertIn("云/SaaS", self.store.sectors())
        self.assertIn("平台/电商/广告", self.store.sectors())

    def test_snapshot_keeps_official_provenance_and_missing_boundary(self):
        snapshot = self.store.snapshot("nvidia")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["company"]["name"], "NVIDIA")
        self.assertEqual(snapshot["data_mode"], "official_historical")
        self.assertTrue(snapshot["data_notice"])
        self.assertTrue(all(metric["status"] in {"direct", "derived", "missing"} for metric in snapshot["metrics"]))
        self.assertTrue(any(metric["status"] == "missing" for metric in snapshot["metrics"]))
        self.assertTrue(any(metric["source_url"] for metric in snapshot["metrics"] if metric["status"] in {"direct", "derived"}))

    def test_sector_scan_does_not_turn_missing_into_zero(self):
        result = self.store.sector_scan("云/SaaS")
        self.assertGreaterEqual(len(result["rows"]), 3)
        self.assertIn("revenue_growth", result["columns"])
        self.assertIn("可比", result["method"])
        self.assertNotIn(None, [row["revenue_growth"]["display_value"] for row in result["rows"]])

    def test_event_analysis_separates_facts_inferences_and_questions(self):
        analysis = self.store.event_analysis("evt-001")
        self.assertIsNotNone(analysis)
        self.assertTrue(analysis["direct_facts"])
        self.assertTrue(analysis["inferences"])
        self.assertTrue(analysis["questions"])
        self.assertEqual(analysis["evidence_coverage"], 0)

    def test_search_returns_companies_events_and_sources(self):
        result = self.store.search("百度")
        self.assertTrue(result["companies"])
        self.assertTrue(result["events"])
        self.assertTrue(result["sources"])

    def test_memo_is_exportable_without_llm(self):
        memo = self.store.memo("microsoft", "云/SaaS", "evt-004", "重点关注 AI 投入是否能转化为云业务增长。")
        self.assertIn("Microsoft", memo["markdown"])
        self.assertIn("来源", memo["markdown"])
        self.assertIn("<!doctype html>", memo["html"])
        self.assertGreater(memo["evidence_count"], 0)

    def test_source_addition_is_local_and_deduplicated(self):
        payload = {"title": "Local test source", "url": "https://example.com/test", "notes": "test"}
        first = self.store.add_source(payload)
        second = self.store.add_source(payload)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len([s for s in self.store.local_sources if s["id"] == first["id"]]), 1)


if __name__ == "__main__":
    unittest.main()
