import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_orchestrator import AgentOrchestrator, OrchestratorError


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class FakeCollector:
    def __init__(self, result=None, error=None, scoped_error=None):
        self.result = [] if result is None else result
        self.error = error
        self.scoped_error = scoped_error
        self.calls = 0
        self.once_calls = 0
        self.scoped_calls = []

    def run_once(self):
        self.calls += 1
        self.once_calls += 1
        if self.error:
            raise self.error
        return self.result

    def run_scoped(self, source_scope):
        self.calls += 1
        self.scoped_calls.append(source_scope)
        error = self.scoped_error if self.scoped_error is not None else self.error
        if error:
            raise error
        return self.result


class OnceOnlyCollector:
    """A collector that only offers the global run_once entry point."""

    def __init__(self, result=None):
        self.result = [] if result is None else result
        self.calls = 0

    def run_once(self):
        self.calls += 1
        return self.result


def collection_payload(**overrides):
    payload = {
        "kind": "collection_cycle",
        "name": "authorized feed cycle",
        "schedule": {"interval_minutes": 15},
        "source_scope": ["https://example.test/feed"],
        "authorization": True,
    }
    payload.update(overrides)
    return payload


def research_payload(kind="company_snapshot_refresh", **overrides):
    payload = {
        "kind": kind,
        "name": "research job",
        "schedule": {"interval_minutes": 30},
        "company_id": "acme",
    }
    payload.update(overrides)
    return payload


class TestAgentOrchestrator(unittest.TestCase):
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

    def test_new_state_has_all_persistent_schema_collections(self):
        orchestrator = self.make()
        state_path = Path(self.tmp.name, AgentOrchestrator.STATE_FILENAME)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 1)
        for key in ("plans", "runs", "tasks", "review_gates", "artifacts", "retrieval_items", "audit"):
            self.assertIsInstance(state[key], list)
        self.assertTrue(Path(self.tmp.name, AgentOrchestrator.LOCK_FILENAME).exists())
        self.assertEqual(orchestrator.plans(), [])

    def test_create_plan_is_draft_and_keeps_structured_payload(self):
        orchestrator = self.make()
        plan = orchestrator.create_plan(research_payload(sector="Cloud/SaaS"))
        self.assertEqual(plan["status"], "draft")
        self.assertEqual(plan["kind"], "company_snapshot_refresh")
        self.assertEqual(plan["schedule"]["interval_minutes"], 30)
        self.assertEqual(plan["payload"]["sector"], "Cloud/SaaS")
        self.assertEqual(len(orchestrator.plans()), 1)

    def test_plan_creation_rejects_unknown_kind_and_short_schedule(self):
        orchestrator = self.make()
        with self.assertRaisesRegex(OrchestratorError, "plan_kind_invalid"):
            orchestrator.create_plan({"kind": "free_browse", "schedule": {"interval_minutes": 15}})
        with self.assertRaisesRegex(OrchestratorError, "schedule_interval_too_short"):
            orchestrator.create_plan(research_payload(schedule={"interval_minutes": 14}))
        with self.assertRaisesRegex(OrchestratorError, "schedule_interval_required"):
            orchestrator.create_plan({"kind": "memo_draft"})

    def test_collection_plan_requires_explicit_source_scope_and_authorization(self):
        orchestrator = self.make()
        missing_scope = collection_payload()
        del missing_scope["source_scope"]
        with self.assertRaisesRegex(OrchestratorError, "source_scope_required"):
            orchestrator.create_plan(missing_scope)

        missing_auth = collection_payload()
        del missing_auth["authorization"]
        with self.assertRaisesRegex(OrchestratorError, "authorization_required"):
            orchestrator.create_plan(missing_auth)

        invalid_auth = collection_payload(authorization="yes")
        with self.assertRaisesRegex(OrchestratorError, "authorization_must_be_boolean"):
            orchestrator.create_plan(invalid_auth)

    def test_approve_pause_and_resume_status_transitions_are_audited(self):
        orchestrator = self.make()
        plan = orchestrator.create_plan(research_payload())
        approved = orchestrator.approve_plan(plan["id"])
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["next_run"], "2026-10-09T09:00:00+00:00")
        paused = orchestrator.pause_plan(plan["id"])
        self.assertEqual(paused["status"], "paused")
        resumed = orchestrator.resume_plan(plan["id"])
        self.assertEqual(resumed["status"], "active")
        state = json.loads(Path(self.tmp.name, AgentOrchestrator.STATE_FILENAME).read_text())
        actions = [event["action"] for event in state["audit"]]
        self.assertIn("plan_created", actions)
        self.assertIn("plan_approved", actions)
        self.assertIn("plan_paused", actions)
        self.assertIn("plan_resumed", actions)

    def test_unapproved_plan_cannot_run_and_unknown_ids_are_safe(self):
        orchestrator = self.make()
        plan = orchestrator.create_plan(research_payload())
        with self.assertRaisesRegex(OrchestratorError, "plan_not_approved"):
            orchestrator.run_plan(plan["id"])
        with self.assertRaisesRegex(OrchestratorError, "plan_not_found"):
            orchestrator.get_plan("missing")
        with self.assertRaisesRegex(OrchestratorError, "run_not_found"):
            orchestrator.get_run("missing")

    def test_manual_run_is_idempotent_for_same_plan_and_scheduled_time(self):
        collector = FakeCollector()
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        first = orchestrator.run_plan(plan["id"])
        second = orchestrator.run_plan(plan["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(orchestrator.runs()), 1)
        self.assertEqual(collector.calls, 1)
        self.assertEqual(len(orchestrator.tasks(first["id"])), 1)

    def test_collection_fake_stores_pending_items_and_review_gates_without_fact_approval(self):
        collector = FakeCollector(
            [
                {
                    "id": "item-1",
                    "title": "Acme announcement",
                    "url": "https://example.test/a",
                    "facts_approved": True,
                }
            ]
        )
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "waiting_review")
        items = orchestrator.retrieval_items()
        gates = orchestrator.review_gates()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "item-1")
        self.assertEqual(items[0]["status"], "pending")
        self.assertFalse(items[0]["facts_approved"])
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["status"], "pending")
        self.assertEqual(gates[0]["item_id"], items[0]["id"])
        self.assertEqual(orchestrator.tasks(run["id"])[0]["status"], "waiting_review")

    def test_unauthorized_collection_never_calls_collector_and_is_blocked(self):
        collector = FakeCollector([{"id": "should-not-run"}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload(authorization=False))
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["error"], "authorization_required")
        self.assertEqual(collector.calls, 0)
        self.assertEqual(orchestrator.retrieval_items(), [])

    def test_collection_without_collector_is_blocked_without_network_fallback(self):
        orchestrator = self.make()
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["error"], "collector_unavailable")
        self.assertEqual(orchestrator.tasks(run["id"])[0]["status"], "blocked")

    def test_scoped_collection_run_uses_run_scoped_with_the_plan_scope(self):
        collector = FakeCollector([{"id": "scoped-item", "title": "In scope"}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(
            collection_payload(source_scope=["https://example.test/feed", "https://other.example.test/rss"])
        )
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "waiting_review")
        self.assertEqual(
            collector.scoped_calls,
            [["https://example.test/feed", "https://other.example.test/rss"]],
        )
        self.assertEqual(collector.once_calls, 0)
        self.assertEqual(len(orchestrator.retrieval_items()), 1)

    def test_scoped_collection_never_falls_back_to_global_run_once(self):
        collector = OnceOnlyCollector([{"id": "global-item", "title": "Out of scope"}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["error"], "collector_unavailable")
        self.assertEqual(collector.calls, 0)
        self.assertEqual(orchestrator.retrieval_items(), [])

    def test_scoped_collector_failure_is_generic_and_does_not_leak_details(self):
        collector = FakeCollector(scoped_error=RuntimeError("scoped feed token leaked"))
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "collection_failed")
        self.assertEqual(collector.once_calls, 0)
        self.assertNotIn("scoped feed token leaked", json.dumps(orchestrator.runs()))

    def test_empty_collection_is_a_successful_noop(self):
        collector = FakeCollector([])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "succeeded_noop")
        self.assertEqual(orchestrator.retrieval_items(), [])
        self.assertEqual(orchestrator.tasks(run["id"])[0]["status"], "succeeded")

    def test_gate_decision_updates_item_and_never_allows_approved_fact_state(self):
        collector = FakeCollector([{"id": "item-1", "title": "Draft"}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        gate = orchestrator.review_gates()[0]
        decided = orchestrator.decide_gate(gate["id"], "accepted", "Source is in scope")
        item = orchestrator.retrieval_items()[0]
        self.assertEqual(decided["status"], "accepted")
        self.assertEqual(item["status"], "accepted")
        self.assertFalse(item["facts_approved"])
        self.assertEqual(orchestrator.get_run(run["id"])["status"], "succeeded")
        with self.assertRaisesRegex(OrchestratorError, "decision_invalid"):
            orchestrator.decide_gate(gate["id"], "approved", "not a fact approval")

    def test_review_retrieval_rejects_item_and_completes_gate(self):
        collector = FakeCollector([{"id": "item-1", "title": "Draft"}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        item = orchestrator.review_retrieval("item-1", "rejected")
        self.assertEqual(item["status"], "rejected")
        self.assertFalse(item["facts_approved"])
        self.assertEqual(orchestrator.review_gates()[0]["status"], "rejected")
        self.assertEqual(orchestrator.get_run(run["id"])["review_outcome"], "rejected_items")

    def test_review_decisions_require_reason_and_are_idempotent_only_for_same_decision(self):
        collector = FakeCollector([{"id": "item-1"}])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        orchestrator.run_plan(plan["id"])
        gate = orchestrator.review_gates()[0]
        with self.assertRaisesRegex(OrchestratorError, "reason_required"):
            orchestrator.decide_gate(gate["id"], "accepted", "")
        first = orchestrator.decide_gate(gate["id"], "accepted", "Checked")
        second = orchestrator.decide_gate(gate["id"], "accepted", "Different text")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(OrchestratorError, "gate_already_decided"):
            orchestrator.decide_gate(gate["id"], "rejected", "Conflict")

    def test_research_handler_receives_kind_and_payload_and_stores_json_artifact(self):
        calls = []

        def handler(kind, payload):
            calls.append((kind, payload))
            return {"summary": "draft only", "numbers": [1, 2]}

        orchestrator = self.make(handlers={"sector_scan": handler})
        plan = orchestrator.create_plan(research_payload("sector_scan", sector="Cloud/SaaS"))
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(calls[0][0], "sector_scan")
        self.assertEqual(calls[0][1]["sector"], "Cloud/SaaS")
        state = json.loads(Path(self.tmp.name, AgentOrchestrator.STATE_FILENAME).read_text())
        self.assertEqual(len(state["artifacts"]), 1)
        artifact = state["artifacts"][0]
        self.assertEqual(artifact["status"], "draft")
        self.assertEqual(artifact["payload"]["summary"], "draft only")

    def test_one_argument_research_handler_is_supported_for_simple_test_doubles(self):
        received = []

        def handler(payload):
            received.append(payload["company_id"])
            return {"ok": True}

        orchestrator = self.make(handlers={"memo_draft": handler})
        plan = orchestrator.create_plan(research_payload("memo_draft"))
        orchestrator.approve_plan(plan["id"])
        self.assertEqual(orchestrator.run_plan(plan["id"])["status"], "succeeded")
        self.assertEqual(received, ["acme"])

    def test_missing_research_handler_creates_blocked_task(self):
        orchestrator = self.make()
        plan = orchestrator.create_plan(research_payload("event_triage"))
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "blocked")
        task = orchestrator.tasks(run["id"])[0]
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["error"], "research_handler_unavailable")
        self.assertEqual(orchestrator.retrieval_items(), [])

    def test_handler_exception_is_generic_and_audited(self):
        def handler(_kind, _payload):
            raise RuntimeError("secret provider details")

        orchestrator = self.make(handlers={"memo_draft": handler})
        plan = orchestrator.create_plan(research_payload("memo_draft"))
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "research_failed")
        self.assertNotIn("secret provider details", json.dumps(orchestrator.runs()))

    def test_tick_runs_due_approved_plan_once_and_does_not_replay_backlog(self):
        collector = FakeCollector([])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        first = orchestrator.tick()
        self.assertEqual(len(first), 1)
        self.assertEqual(collector.calls, 1)
        self.assertEqual(orchestrator.tick(), [])
        self.assertEqual(collector.calls, 1)
        self.clock.advance(days=4)
        second = orchestrator.tick()
        self.assertEqual(len(second), 1)
        self.assertEqual(collector.calls, 2)
        saved = orchestrator.get_plan(plan["id"])
        next_run = datetime.fromisoformat(saved["next_run"])
        self.assertGreater(next_run, self.clock.value)
        self.assertEqual(len(orchestrator.runs()), 2)

    def test_tick_skips_paused_plan_and_resume_makes_it_due(self):
        collector = FakeCollector([])
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        orchestrator.pause_plan(plan["id"])
        self.assertEqual(orchestrator.tick(), [])
        self.assertEqual(collector.calls, 0)
        orchestrator.resume_plan(plan["id"])
        self.assertEqual(len(orchestrator.tick()), 1)
        self.assertEqual(collector.calls, 1)

    def test_restart_persists_plans_runs_items_and_audit(self):
        collector = FakeCollector([{"id": "persisted-item", "title": "Saved"}])
        first = self.make(collector=collector)
        plan = first.create_plan(collection_payload())
        first.approve_plan(plan["id"])
        run = first.run_plan(plan["id"])
        self.assertEqual(len(first.retrieval_items()), 1)

        second = self.make(collector=FakeCollector([]))
        self.assertEqual(second.get_plan(plan["id"])["id"], plan["id"])
        self.assertEqual(second.get_run(run["id"])["id"], run["id"])
        self.assertEqual(second.retrieval_items()[0]["id"], "persisted-item")
        state = json.loads(Path(self.tmp.name, AgentOrchestrator.STATE_FILENAME).read_text())
        self.assertGreaterEqual(len(state["audit"]), 8)

    def test_corrupt_state_fails_with_generic_safe_error(self):
        state_path = Path(self.tmp.name, AgentOrchestrator.STATE_FILENAME)
        state_path.write_text("not-json", encoding="utf-8")
        with self.assertRaisesRegex(OrchestratorError, "state_invalid"):
            self.make()

    def test_collector_failure_does_not_expose_exception_details(self):
        collector = FakeCollector(error=RuntimeError("authorization token leaked"))
        orchestrator = self.make(collector=collector)
        plan = orchestrator.create_plan(collection_payload())
        orchestrator.approve_plan(plan["id"])
        run = orchestrator.run_plan(plan["id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "collection_failed")
        self.assertNotIn("authorization token leaked", json.dumps(orchestrator.runs()))


if __name__ == "__main__":
    unittest.main()
