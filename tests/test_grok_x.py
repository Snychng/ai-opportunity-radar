"""只用合成 OAuth 与注入传输验证 Grok X 搜索边界。"""

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import aor_bootstrap  # noqa: F401
from aor.sources import grok_x


def fake_jwt(expiry):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((encode({"alg": "test-only"}), encode({"exp": expiry}), "syntheticSignature"))


def search_response():
    return {"status": "completed", "model": "grok-4.6", "output": [
        {"type": "reasoning", "summary": [{"text": "PRIVATE REASONING MUST NOT BE RETURNED"}]},
        {"type": "custom_tool_call", "name": "x_keyword_search", "status": "completed",
         "id": "call_1", "input": '{"query":"workflow since:2026-09-01","limit":"2"}'},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "A public workflow example.", "annotations": [
                {"type": "url_citation", "url": "https://x.com/person/status/123456789",
                 "start_index": 0, "end_index": 24, "title": "Example"}]}]}],
        "usage": {"input_tokens": 20, "total_tokens": 40, "num_sources_used": 0,
                  "cost_in_usd_ticks": 1234, "server_side_tool_usage_details": {"x_search_calls": 1}},
        "max_tool_calls": None}


class GrokXTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_file = Path(self.temp.name) / "auth.json"
        self.token = fake_jwt(time.time() + 7200)
        self.auth_file.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {
            "access_token": self.token, "refresh_token": "DO-NOT-READ-OR-RETURN"}}}}))
        self.config = {"enabled": True, "auth_file": str(self.auth_file)}
        self.requests = []

    def transport(self, response=None, status=200, content_type="application/json"):
        response = search_response() if response is None else response

        def send(request, **kwargs):
            self.requests.append((request, kwargs))
            body = response if isinstance(response, bytes) else json.dumps(response).encode()
            return status, content_type, body
        return send

    def search(self, **kwargs):
        return grok_x.search_x("Find a concrete workflow with source links", config=self.config,
                              from_date="2026-09-01", to_date="2026-09-16", **kwargs)

    def test_diagnose_is_offline_and_does_not_mutate_auth(self):
        before = self.auth_file.read_bytes(), self.auth_file.stat().st_mtime_ns
        with patch.object(grok_x, "_transport", side_effect=AssertionError("no network")):
            result = grok_x.diagnose_grok(self.config)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["live_health"], "not_checked")
        self.assertEqual(result["network_requests"], 0)
        self.assertNotIn(self.token, json.dumps(result))
        self.assertEqual(before, (self.auth_file.read_bytes(), self.auth_file.stat().st_mtime_ns))

    def test_disabled_and_missing_auth_do_not_read_env_or_send(self):
        with patch.dict("os.environ", {"XAI_API_KEY": "never-use-me"}), \
                patch.object(grok_x, "_transport", side_effect=AssertionError("no network")):
            self.assertEqual(grok_x.diagnose_grok({})["status"], "disabled")
            self.assertEqual(grok_x.diagnose_grok({"enabled": True})["status"], "missing_auth")
            self.config = {"enabled": False, "auth_file": str(self.auth_file)}
            self.assertEqual(self.search()["status"], "disabled")
            self.config = {"enabled": True}
            result = self.search()
        self.assertEqual(result["status"], "auth_denied")
        self.assertEqual(result["error_code"], "missing_auth")
        self.assertEqual(result["network_attempts"], 0)

    def test_expired_and_near_expiry_tokens_never_refresh(self):
        for remaining in (-1, 100, 600):
            self.auth_file.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {
                "access_token": fake_jwt(time.time() + remaining), "refresh_token": "do-not-use"}}}}))
            with self.subTest(remaining=remaining):
                self.assertEqual(grok_x.diagnose_grok(self.config)["status"], "auth_expired")
                result = self.search(transport=lambda *a, **k: self.fail("No refresh or search"))
                self.assertEqual(result["status"], "auth_expired")
                self.assertFalse(result["auth_refresh_attempted"])
                self.assertEqual(result["network_attempts"], 0)

    def test_explicit_absolute_expiry_supports_opaque_token(self):
        stamp = datetime.fromtimestamp(time.time() + 7200, timezone.utc).isoformat()
        self.auth_file.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {
            "access_token": "syntheticOpaqueToken", "expires_at": stamp}}}}))
        self.assertEqual(grok_x.diagnose_grok(self.config)["status"], "ready")

    def test_malformed_auth_is_rejected_without_secrets(self):
        rows = [[], {}, {"providers": []}, {"providers": {"xai-oauth": {"tokens": {
            "access_token": "opaque-without-expiry"}}}},
            {"providers": {"xai-oauth": {"tokens": {"access_token": "a.b.c"}}}}]
        for row in rows:
            self.auth_file.write_text(json.dumps(row))
            with self.subTest(row=row):
                self.assertEqual(grok_x.diagnose_grok(self.config)["status"], "auth_invalid")
        self.auth_file.write_bytes(b"{" * (grok_x.MAX_AUTH_BYTES + 1))
        self.assertEqual(grok_x.diagnose_grok(self.config)["status"], "auth_invalid")

    def test_shared_auth_symlink_is_read_only(self):
        link = Path(self.temp.name) / "auth-link.json"
        link.symlink_to(self.auth_file)
        self.config["auth_file"] = str(link)
        self.assertEqual(grok_x.diagnose_grok(self.config)["status"], "ready")
        self.assertTrue(link.is_symlink())

    def test_search_request_and_actual_tool_execution(self):
        result = self.search(transport=self.transport())
        request, limits = self.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, grok_x.ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
        self.assertEqual(request.get_header("User-agent"), "AOR-Grok-X-Search")
        self.assertEqual(payload["input"][1], {"role": "user", "content": "Find a concrete workflow with source links"})
        self.assertEqual(payload["input"][0]["role"], "system")
        self.assertIn("using the x_search tool", payload["input"][0]["content"])
        self.assertEqual(payload["tools"], [{"type": "x_search", "from_date": "2026-09-01", "to_date": "2026-09-16"}])
        self.assertFalse(payload["store"])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertEqual(limits, {"timeout": 90, "max_bytes": grok_x.MAX_RESPONSE_BYTES})
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["requested_max_tool_calls"], 2)
        self.assertNotIn("max_tool_calls", result)
        self.assertEqual(result["usage"]["num_sources_used"], 0)
        self.assertEqual(result["cost_basis"], "subscription_unknown")
        self.assertNotIn("PRIVATE REASONING", json.dumps(result))

    def test_zero_length_citations_preserved_as_unverified_candidates(self):
        response = search_response()
        annotation = response["output"][2]["content"][0]["annotations"][0]
        annotation["end_index"] = 0
        response["output"][2]["content"][0]["text"] = "No verified match found."
        result = self.search(transport=self.transport(response))
        self.assertEqual(result["citations"][0]["end_index"], 0)
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("evidence", result)

    def test_top_level_citations_are_preserved_and_deduplicated(self):
        response = search_response()
        citation = deepcopy(response["output"][2]["content"][0]["annotations"][0])
        response["citations"] = [citation, "https://x.com/i/status/987654321", "https://u:password@x.com/private"]
        result = self.search(transport=self.transport(response))
        self.assertEqual(len(result["citations"]), 2)
        self.assertIsNone(result["citations"][1]["start_index"])

    def test_answer_and_citation_without_search_trace_are_not_success(self):
        response = search_response()
        response["output"].pop(1)
        response["usage"] = {"num_sources_used": 50, "num_server_side_tools_used": 2}
        result = self.search(transport=self.transport(response))
        self.assertEqual(result["status"], "tool_not_invoked")
        self.assertFalse(result["search_executed"])

    def test_citation_limit_matches_import_contract_and_reports_overflow(self):
        response = search_response()
        response["output"] = response["output"][:2]
        response["citations"] = [{"url": f"https://x.com/i/status/{number}", "start_index": 0, "end_index": 0}
                                 for number in range(100000, 100502)]
        response["citations"].append(response["citations"][0])
        result = self.search(transport=self.transport(response))
        self.assertEqual(len(result["citations"]), 500)
        self.assertEqual(result["citation_truncated_count"], 2)
        self.assertEqual(result["status"], "succeeded")

    def test_x_usage_without_trace_and_trace_without_usage_both_confirm_execution(self):
        for keep_trace in (True, False):
            response = search_response()
            if keep_trace:
                response["usage"] = {}
            else:
                response["output"].pop(1)
            with self.subTest(keep_trace=keep_trace):
                self.assertTrue(self.search(transport=self.transport(response))["search_executed"])

    def test_in_progress_trace_alone_does_not_confirm_execution(self):
        response = search_response()
        response["output"][1]["status"] = "in_progress"
        response["usage"] = {}
        self.assertEqual(self.search(transport=self.transport(response))["status"], "tool_not_invoked")

    def test_executed_without_citations_is_empty(self):
        response = search_response()
        response["output"][2]["content"][0]["annotations"] = []
        result = self.search(transport=self.transport(response))
        self.assertEqual(result["status"], "empty")
        self.assertTrue(result["search_executed"])

    def test_sse_terminal_response_parsing_discards_reasoning(self):
        events = [{"type": "response.reasoning_summary_text.delta", "delta": "SENSITIVE_REASONING"},
                  {"type": "response.completed", "response": search_response()}]
        body = "\r\n\r\n".join("event: " + row["type"] + "\r\ndata: " + json.dumps(row) for row in events)
        result = self.search(transport=self.transport((body + "\r\n\r\ndata: [DONE]\r\n\r\n").encode(),
                                                      content_type="text/event-stream"))
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("REASONING", json.dumps(result))

    def test_truncated_sse_returns_unknown_without_retry(self):
        event = {"type": "response.output_item.done", "item": search_response()["output"][1]}
        body = ("data: " + json.dumps(event) + "\n\n").encode()
        result = self.search(transport=self.transport(body, content_type="text/event-stream"))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["tool_calls"][0]["name"], "x_keyword_search")

    def test_http_errors_and_parse_errors_are_not_retried(self):
        for status, expected in ((401, "auth_denied"), (403, "auth_denied"), (400, "failed"),
                                 (302, "failed"), (429, "outcome_unknown"), (500, "outcome_unknown")):
            with self.subTest(status=status):
                before = len(self.requests)
                result = self.search(transport=self.transport(b"secret error " + self.token.encode(), status=status))
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(self.requests), before + 1)
                self.assertNotIn(self.token, json.dumps(result))
        result = self.search(transport=self.transport(b"invalid json " + self.token.encode()))
        self.assertEqual(result["status"], "outcome_unknown")

    def test_timeout_does_not_leak_exception_or_retry(self):
        calls = []
        def timeout(request, **kwargs):
            calls.append(request)
            raise TimeoutError("Authorization: Bearer " + self.token)
        result = self.search(transport=timeout)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(len(calls), 1)
        self.assertNotIn(self.token, json.dumps(result))

    def test_successful_response_is_also_scrubbed(self):
        response = search_response()
        response["output"][2]["content"][0]["text"] += " reflected " + self.token
        response["output"][1]["input"] = self.token
        response["usage"]["user"] = "private-id"
        result = self.search(transport=self.transport(response))
        self.assertNotIn(self.token, json.dumps(result))
        self.assertNotIn("private-id", json.dumps(result))

    def test_invalid_search_input_never_sends(self):
        for start, end in (("2026-09-17", "2026-09-16"), ("2026-02-30", "2026-09-16"), ("20260901", "2026-09-16")):
            with self.subTest(start=start):
                result = grok_x.search_x("query", config=self.config, from_date=start, to_date=end,
                                        transport=lambda *a, **k: self.fail("must not send"))
                self.assertEqual(result["network_attempts"], 0)
                self.assertEqual(result["error_code"], "invalid_search_input")

    def test_config_limits_and_workflow_request_cap(self):
        self.config["max_responses_requests"] = 6
        self.assertEqual(grok_x.diagnose_grok(self.config)["status"], "ready")
        for key, value in (("timeout_seconds", 200), ("max_tool_calls", 0), ("model", "https://evil.invalid"),
                           ("enabled", "true"), ("max_output_tokens", True)):
            with self.subTest(key=key):
                self.assertEqual(grok_x.diagnose_grok({**self.config, key: value})["status"], "config_invalid")

    def test_default_transport_disables_proxy_and_redirect(self):
        class Response:
            headers = {"Content-Type": "application/json"}
            status = 200
            def __enter__(self):
                self.parts = [b"{}", b""]
                return self
            def __exit__(self, *args):
                return None
            def read1(self, size):
                return self.parts.pop(0)
        with patch.object(grok_x, "build_opener") as build:
            build.return_value.open.return_value = Response()
            result = self.search()
        self.assertEqual(result["status"], "outcome_unknown")
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        self.assertEqual(build.return_value.open.call_count, 1)

    def test_default_transport_does_not_read_error_body(self):
        class SecretBody:
            def read(self, *args):
                self.fail("must not read error body")
            def close(self):
                pass
        error = HTTPError(grok_x.ENDPOINT, 401, "token in message", {}, SecretBody())
        with patch.object(grok_x, "build_opener") as build:
            build.return_value.open.side_effect = error
            result = self.search()
        self.assertEqual(result["status"], "auth_denied")
        self.assertNotIn("token in message", json.dumps(result))

    def test_response_byte_limit_is_enforced_for_injected_transport(self):
        result = self.search(transport=self.transport(b"x" * (grok_x.MAX_RESPONSE_BYTES + 1)))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["error_code"], "response_size_invalid")


if __name__ == "__main__":
    unittest.main()
