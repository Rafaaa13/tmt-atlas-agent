import inspect
import json
import os
import unittest
from unittest.mock import patch

from llm_research import MAX_CONTEXT_CHARS, MAX_CONTEXT_ITEMS, enhance


KINDS = (
    "collection_cycle",
    "company_snapshot_refresh",
    "sector_scan",
    "event_triage",
    "memo_draft",
)


def envelope(body, usage=None):
    response = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": json.dumps(body, ensure_ascii=False),
            },
            "finish_reason": "stop",
        }],
    }
    if usage is not None:
        response["usage"] = usage
    return response


class TestLLMResearch(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "allow_external_llm": True,
            "base_url": "https://api.example.test/v1",
            "model": "test-model",
        }
        self.context = [{
            "id": "e1",
            "text": "Revenue was 100 in 2025. The period ended on 2025-12-31.",
        }, {
            "id": "e2",
            "text": "Management described the change as temporary.",
        }]
        self.env = {"TMT_LLM_API_KEY": "test-key"}

    def call(self, body, *, context=None, settings=None, usage=None, **kwargs):
        calls = []

        def transport(**request):
            calls.append(request)
            return envelope(body, usage=usage)

        with patch.dict(os.environ, self.env, clear=False):
            result = enhance(
                "sector_scan",
                self.context if context is None else context,
                self.settings if settings is None else settings,
                consent=True,
                transport=transport,
                **kwargs,
            )
        return result, calls

    def test_public_api_signature_is_stable(self):
        parameters = inspect.signature(enhance).parameters
        self.assertEqual(
            list(parameters),
            ["kind", "context", "settings", "consent", "transport"],
        )
        self.assertTrue(parameters["consent"].kind is inspect.Parameter.KEYWORD_ONLY)
        self.assertTrue(parameters["transport"].kind is inspect.Parameter.KEYWORD_ONLY)
        self.assertFalse(parameters["consent"].default)
        self.assertIsNone(parameters["transport"].default)

    def test_opt_in_requires_setting_and_explicit_consent(self):
        called = []

        def transport(**_request):
            called.append(True)
            return envelope({"claims": [], "questions": []})

        with patch.dict(os.environ, self.env, clear=False):
            disabled = enhance(
                "memo_draft", self.context,
                {**self.settings, "allow_external_llm": False},
                consent=True, transport=transport,
            )
            no_consent = enhance(
                "memo_draft", self.context, self.settings,
                consent=False, transport=transport,
            )
        self.assertEqual(disabled["status"], "skipped")
        self.assertEqual(disabled["error"], "disabled")
        self.assertEqual(no_consent["status"], "skipped")
        self.assertEqual(no_consent["error"], "consent_required")
        self.assertEqual(called, [])

    def test_missing_config_is_skipped_even_with_transport_hook(self):
        called = []

        def transport(**_request):
            called.append(True)
            return envelope({"claims": [], "questions": []})

        with patch.dict(os.environ, {}, clear=True):
            result = enhance(
                "event_triage", self.context, self.settings,
                consent=True, transport=transport,
            )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["error"], "missing_config")
        self.assertEqual(called, [])

    def test_all_five_research_kinds_use_same_citation_checked_draft_api(self):
        calls = []

        def transport(**request):
            calls.append(request)
            return envelope({
                "claims": [{
                    "text": "Revenue was 100 in 2025.",
                    "refs": ["e1"],
                    "kind": "summary",
                    "provider_extra": "discard me",
                }],
                "questions": ["What explains the change?"],
                "top_level_extra": {"discard": True},
            }, usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})

        with patch.dict(os.environ, self.env, clear=False):
            results = [enhance(kind, self.context, self.settings, consent=True, transport=transport)
                       for kind in KINDS]

        self.assertEqual([result["status"] for result in results], ["draft"] * 5)
        self.assertEqual([result["model"] for result in results], ["test-model"] * 5)
        for result in results:
            self.assertEqual(result["claims"], [{
                "text": "Revenue was 100 in 2025.",
                "refs": ["e1"],
                "kind": "summary",
            }])
            self.assertEqual(result["questions"], ["What explains the change?"])
            self.assertEqual(result["usage"], {
                "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
            })
            self.assertIsNone(result["error"])
            self.assertEqual(result["warnings"], [
                "Unverified LLM draft; citations and numbers do not prove truth. Human review required; not financial advice."
            ])
        self.assertEqual([call["payload"]["max_tokens"] for call in calls], [800] * 5)
        self.assertEqual([call["payload"]["temperature"] for call in calls], [0] * 5)
        self.assertTrue(all(call["payload"]["response_format"] == {"type": "json_object"} for call in calls))
        self.assertTrue(all(call["timeout"] == 20 for call in calls))
        self.assertTrue(all(call["max_response_bytes"] == 1024 * 1024 for call in calls))
        self.assertTrue(all(call["payload"]["messages"][0]["role"] == "system" for call in calls))
        system_prompt = calls[0]["payload"]["messages"][0]["content"].lower()
        self.assertIn("untrusted", system_prompt)
        self.assertIn("not instructions", system_prompt)
        self.assertIn("no tools", system_prompt)
        self.assertIn("financial advice", system_prompt)

    def test_transport_receives_only_whitelisted_context_and_env_key(self):
        received = []

        def transport(**request):
            received.append(request)
            return envelope({"claims": [], "questions": []})

        context = [{"id": "e1", "text": "Visible evidence", "secret_path": "/tmp/no"}]
        settings = {
            "allow_external_llm": True,
            "base_url": "https://api.example.test/v1",
            "model": "configured-model",
            "api_key": "must-not-be-used",
        }
        with patch.dict(os.environ, self.env, clear=True):
            result = enhance("company_snapshot_refresh", context, settings,
                             consent=True, transport=transport)
        self.assertEqual(result["status"], "draft")
        request = received[0]
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-key")
        user_payload = json.loads(request["payload"]["messages"][1]["content"])
        self.assertEqual(user_payload["context"], [{"id": "e1", "text": "Visible evidence"}])
        self.assertNotIn("must-not-be-used", json.dumps(request))
        self.assertEqual(request["payload"]["model"], "configured-model")

    def test_environment_supplies_base_url_and_model_but_not_api_key(self):
        received = []

        def transport(**request):
            received.append(request)
            return envelope({"claims": [], "questions": []})

        with patch.dict(os.environ, {
            "TMT_LLM_API_KEY": "environment-key",
            "TMT_LLM_BASE_URL": "https://env.example.test/api",
            "TMT_LLM_MODEL": "environment-model",
        }, clear=True):
            result = enhance(
                "event_triage", self.context,
                {"allow_external_llm": True}, consent=True, transport=transport,
            )
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["model"], "environment-model")
        self.assertEqual(received[0]["url"], "https://env.example.test/api/chat/completions")
        self.assertEqual(received[0]["headers"]["Authorization"], "Bearer environment-key")

    def test_extra_fields_are_stripped_but_shape_and_citations_are_strict(self):
        result, _calls = self.call({
            "claims": [{
                "text": "Management described the change as temporary.",
                "refs": ["e2"],
                "kind": "inference",
                "unexpected": ["removed"],
            }],
            "questions": [],
            "ignored": "removed",
        })
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["claims"], [{
            "text": "Management described the change as temporary.",
            "refs": ["e2"],
            "kind": "inference",
        }])

        no_refs, _ = self.call({
            "claims": [{"text": "Management described the change as temporary.", "refs": [], "kind": "summary"}],
            "questions": [],
        })
        self.assertEqual(no_refs["status"], "failed")
        self.assertEqual(no_refs["error"], "invalid_json")

    def test_unknown_reference_is_rejected_without_returning_claims(self):
        result, _calls = self.call({
            "claims": [{"text": "Revenue was 100.", "refs": ["missing"], "kind": "summary"}],
            "questions": [],
        })
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "unknown_ref")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["questions"], [])

    def test_unbacked_numbers_including_dates_are_rejected(self):
        novel_number, _ = self.call({
            "claims": [{"text": "Revenue was 101 in 2025.", "refs": ["e1"], "kind": "summary"}],
            "questions": [],
        })
        self.assertEqual(novel_number["status"], "failed")
        self.assertEqual(novel_number["error"], "unbacked_number")

        novel_date, _ = self.call({
            "claims": [{"text": "The period ended on 2026-12-31.", "refs": ["e1"], "kind": "summary"}],
            "questions": [],
        })
        self.assertEqual(novel_date["status"], "failed")
        self.assertEqual(novel_date["error"], "unbacked_number")

    def test_non_json_model_content_is_rejected_generically(self):
        result, _ = self.call({"claims": [], "questions": []})
        # Replace the normal JSON content with a natural-language response.
        def transport(**_request):
            return {"choices": [{"message": {"content": "not JSON"}, "finish_reason": "stop"}]}

        with patch.dict(os.environ, self.env, clear=False):
            result = enhance("memo_draft", self.context, self.settings,
                             consent=True, transport=transport)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "invalid_json")
        self.assertNotIn("not JSON", json.dumps(result))

    def test_usage_is_finite_bounded_and_bad_provider_usage_is_omitted(self):
        result, _ = self.call(
            {"claims": [], "questions": []},
            # completion_tokens is deliberately above the configured cap;
            # provider-supplied usage cannot make the local result unbounded.
            usage={
                "prompt_tokens": 7,
                "completion_tokens": 801,
                "total_tokens": 2_000_000,
                "bad": "secret provider detail",
            },
        )
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["usage"], {"prompt_tokens": 7})
        self.assertTrue(any("usage" in warning.lower() for warning in result["warnings"]))

    def test_context_limits_fail_closed_without_transport(self):
        too_many = [{"id": f"e{i}", "text": "x"} for i in range(MAX_CONTEXT_ITEMS + 1)]
        result, calls = self.call({"claims": [], "questions": []}, context=too_many)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "invalid_context")
        self.assertEqual(calls, [])

        too_long = [{"id": "e1", "text": "x" * (MAX_CONTEXT_CHARS + 1)}]
        result, calls = self.call({"claims": [], "questions": []}, context=too_long)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "invalid_context")
        self.assertEqual(calls, [])

    def test_local_ollama_requires_explicit_local_setting_and_allows_http(self):
        called = []

        def transport(**request):
            called.append(request)
            return envelope({"claims": [], "questions": []})

        local_settings = {
            "allow_external_llm": True,
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "ollama-model",
        }
        with patch.dict(os.environ, self.env, clear=False):
            blocked = enhance("memo_draft", self.context, local_settings,
                              consent=True, transport=transport)
            allowed = enhance("memo_draft", self.context,
                              {**local_settings, "allow_local_llm": True},
                              consent=True, transport=transport)
        self.assertEqual(blocked["status"], "failed")
        self.assertEqual(blocked["error"], "unsafe_endpoint")
        self.assertEqual(allowed["status"], "draft")
        self.assertEqual(len(called), 1)

    def test_transport_timeout_and_provider_failures_are_generic(self):
        def timeout(**_request):
            raise TimeoutError("provider secret and key")

        with patch.dict(os.environ, self.env, clear=False):
            timed = enhance("sector_scan", self.context, self.settings,
                            consent=True, transport=timeout)
        self.assertEqual(timed["status"], "failed")
        self.assertEqual(timed["error"], "timeout")
        self.assertNotIn("provider secret", json.dumps(timed))
        self.assertNotIn("test-key", json.dumps(timed))

        def unavailable(**_request):
            raise RuntimeError("authorization token")

        with patch.dict(os.environ, self.env, clear=False):
            failed = enhance("sector_scan", self.context, self.settings,
                             consent=True, transport=unavailable)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "unavailable")
        self.assertNotIn("authorization token", json.dumps(failed))

    def test_response_size_limit_is_applied_to_transport_hook_too(self):
        def oversized(**_request):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({"claims": [], "questions": [], "x": "a" * (1024 * 1024)}),
                    },
                    "finish_reason": "stop",
                }],
            }

        with patch.dict(os.environ, self.env, clear=False):
            result = enhance("event_triage", self.context, self.settings,
                             consent=True, transport=oversized)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
