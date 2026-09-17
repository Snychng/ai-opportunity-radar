from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.workflow.research import _run_lock, start_research
from aor.workflow.search import (_paid_receipts, collect_search, import_search, plan_search, read_config, search_status)
from contracts import canonical_sha256
from tikhub_query import validate_plan


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class SearchWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        with patch.dict(os.environ, {"AOR_OFFLINE": "1"}):
            self.run = start_research(self.home, as_of=date(2026, 9, 16), focus="custom seller orders")
        self.run_id = self.run["run_id"]
        self.directory = Path(self.run["run_path"])
        self.environment = patch.dict(os.environ, {"AOR_OFFLINE": "0", "AOR_NO_UPDATE_CHECK": "1", "TIKHUB_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def setup_grok(self, limit=6):
        auth = write(self.home / "test-auth.json", {"providers": {"xai-oauth": {"tokens": {
            "access_token": "test-fixture-not-a-real-token", "expires_at": time.time() + 3600}}}})
        config = read_config(write(self.home / "search-config.json", {"grok": {"enabled": True,
            "auth_file": str(auth), "max_responses_requests": limit}, "max_tasks": 8}))
        return config, plan_search(self.home, self.run_id, config=config)

    @staticmethod
    def transport(request, **kwargs):
        body = {"status": "completed", "model": "grok-4.6", "output": [
            {"type": "custom_tool_call", "name": "x_keyword_search", "status": "completed", "input": "{}"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One result", "annotations": [
                {"type": "url_citation", "url": "https://x.com/test/status/2100011781863170285", "start_index": 0, "end_index": 0}]}]}],
                "usage": {"server_side_tool_usage_details": {"x_search_calls": 1}}}
        return 200, "application/json", json.dumps(body).encode()

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/radar.py"), "search", *args,
                                 "--home", str(self.home), "--run-id", self.run_id],
                                capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_real_cli_no_grok_host_x_handoff_and_import_idempotence(self):
        plan = self.cli("plan")
        task = next(row for row in plan["tasks"] if row["lane"] == "x")
        self.assertEqual(task["provider"], "host-x")
        output = self.home / "receipt.json"
        before = (self.directory / "run.json").read_bytes()
        receipt = self.cli("collect", "--task-id", task["task_id"], "--output", str(output))
        self.assertEqual(receipt["fallback"]["provider"], "host-x")
        self.assertFalse(receipt["execution"]["searched"])
        self.assertEqual(before, (self.directory / "run.json").read_bytes())
        self.cli("import", "--input", str(output))
        first = self.cli("status")
        self.cli("import", "--input", str(output))
        second = self.cli("status")
        self.assertEqual(first, second)
        self.assertEqual(second["execution"]["searched"], 0)

    def test_grok_success_reused_and_zero_span_is_only_candidate(self):
        config, plan = self.setup_grok()
        task = plan["tasks"][0]
        called = []
        def transport(request, **kwargs):
            called.append(True)
            attempt = self.directory / "search/attempts" / (task["task_id"] + ".json")
            self.assertEqual(json.loads(attempt.read_text())["status"], "sent")
            # 网络发生在研究锁外。
            with _run_lock(self.directory):
                pass
            return self.transport(request, **kwargs)
        path = self.home / "receipt.json"
        receipt = collect_search(self.home, self.run_id, task["task_id"], config=config, output=path, transport=transport)
        self.assertEqual(receipt["status"], "succeeded")
        again = collect_search(self.home, self.run_id, task["task_id"], config=config, transport=transport)
        self.assertEqual(receipt, again)
        self.assertEqual(len(called), 1)
        status = import_search(self.home, self.run_id, [path])
        self.assertEqual(status["execution"]["searched"], 1)
        self.assertEqual(status["discovery"]["unique_candidates"], 1)
        self.assertEqual(status["discovery"]["evidence_count"], 0)
        self.assertIsNone(status["verification"]["verified"])

    def test_unknown_does_not_repeat_or_create_paid_fallback_host_still_available(self):
        config, plan = self.setup_grok()
        def transport(request, **kwargs):
            raise TimeoutError("fixture")
        with patch.dict(os.environ, {"TIKHUB_API_KEY": "fixture-not-a-real-key"}):
            result = collect_search(self.home, self.run_id, plan["tasks"][0]["task_id"], config=config,
                                    max_cost_usd=1, transport=transport)
            self.assertEqual(result["status"], "outcome_unknown")
            self.assertEqual(result["fallback"]["provider"], "host-x")
            other = next(row for row in plan["tasks"] if row["lane"] == "web")
            web = collect_search(self.home, self.run_id, other["task_id"], config=config, transport=transport)
            self.assertEqual(web["fallback"]["provider"], "host-web")
            other_x = [row for row in plan["tasks"] if row["lane"] == "x"][1]
            with patch("aor.sources.grok_x.search_x", side_effect=AssertionError("must not send")):
                blocked = collect_search(self.home, self.run_id, other_x["task_id"], config=config, max_cost_usd=1)
            self.assertEqual(blocked["status"], "outcome_unknown")
            self.assertNotIn("paid_plan_path", blocked["fallback"])

    def test_explicit_budget_without_grok_builds_valid_paid_plan(self):
        config = read_config()
        plan = plan_search(self.home, self.run_id, config=config)
        task = plan["tasks"][0]
        with patch.dict(os.environ, {"TIKHUB_API_KEY": "fixture-not-a-real-key"}):
            no_budget = collect_search(self.home, self.run_id, task["task_id"], config=config)
            self.assertEqual(no_budget["fallback"]["provider"], "host-x")
            result = collect_search(self.home, self.run_id, task["task_id"], config=config, max_cost_usd=1)
        fallback = result["fallback"]
        self.assertEqual(fallback["provider"], "tikhub")
        self.assertIn("--paid-plan", fallback["argv"])
        self.assertEqual(fallback["argv"][-2:], ["--max-cost-usd", "1"])
        paid = json.loads(Path(fallback["paid_plan_path"]).read_text())
        endpoint = paid["requests"][0]["endpoint"]
        validate_plan(paid, [{"endpoint_uri": endpoint, "endpoint_cost": 0.01}])
        self.assertFalse((self.directory / "paid-journal.sqlite3").exists())
        self.assertFalse(result["execution"]["searched"])

    def test_config_default_and_request_limit_bound_to_plan(self):
        config, plan = self.setup_grok(limit=1)
        self.assertEqual(read_config(home=self.home), config)
        collect_search(self.home, self.run_id, plan["tasks"][0]["task_id"], config=config, transport=self.transport)
        task = [row for row in plan["tasks"] if row["lane"] == "x"][1]
        result = collect_search(self.home, self.run_id, task["task_id"], config=config, transport=self.transport)
        self.assertEqual(result["status"], "request_limit")
        config["grok"]["max_responses_requests"] = 2
        with self.assertRaisesRegex(ValueError, "配置"):
            collect_search(self.home, self.run_id, task["task_id"], config=config)

    def test_offline_and_completed_prohibit_calls_or_inputs(self):
        config, plan = self.setup_grok()
        with patch.dict(os.environ, {"AOR_OFFLINE": "1"}), patch("aor.sources.grok_x.search_x") as execute:
            result = collect_search(self.home, self.run_id, plan["tasks"][0]["task_id"], config=config)
        self.assertEqual(result["status"], "offline")
        execute.assert_not_called()
        manifest = json.loads((self.directory / "run.json").read_text())
        manifest["status"] = "completed"
        write(self.directory / "run.json", manifest)
        with self.assertRaisesRegex(ValueError, "完成"):
            import_search(self.home, self.run_id, [write(self.home / "receipt.json", result)])

    def test_tampered_task_and_receipt_rejected_atomically(self):
        plan = plan_search(self.home, self.run_id)
        template = self.directory / "search/host-templates" / (plan["tasks"][0]["task_id"] + ".json")
        value = json.loads(template.read_text())
        value["input_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "input_sha256"):
            import_search(self.home, self.run_id, [write(self.home / "bad.json", value)])
        self.assertNotIn("search-executions", json.loads((self.directory / "run.json").read_text())["artifacts"])
        value = json.loads(template.read_text())
        path = write(self.home / "host.json", value)
        import_search(self.home, self.run_id, [path])
        value["answer"] = "changed"
        with self.assertRaisesRegex(ValueError, "receipt_id"):
            import_search(self.home, self.run_id, [write(path, value)])
        plan["tasks"][0]["query"] = "tampered"
        write(self.directory / "search-plan.json", plan)
        with self.assertRaisesRegex(ValueError, "已被修改"):
            search_status(self.home, self.run_id)

    def test_parallel_collect_only_one_request_without_run_lock(self):
        config, plan = self.setup_grok()
        entered, release = threading.Event(), threading.Event()
        def transport(request, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.transport(request, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(collect_search, self.home, self.run_id, plan["tasks"][0]["task_id"], config=config, transport=transport)
            try:
                self.assertTrue(entered.wait(5))
                with _run_lock(self.directory):
                    pass
                with self.assertRaisesRegex(ValueError, "并发上限"):
                    collect_search(self.home, self.run_id, plan["tasks"][0]["task_id"], config=config)
            finally:
                release.set()
            self.assertEqual(future.result()["status"], "succeeded")

    def test_host_search_receipt_import_is_data_not_evidence(self):
        plan = plan_search(self.home, self.run_id)
        task = plan["tasks"][0]
        template = json.loads((self.directory / "search/host-templates" / (task["task_id"] + ".json")).read_text())
        template.update(status="succeeded", citations=[{"url": "https://x.com/i/status/2100011781863170285"}],
                        answer="model summary", original_text="model original", verified=True)
        template["execution"].update(searched=True, queries=[task["query"]])
        status = import_search(self.home, self.run_id, [write(self.home / "host.json", template)])
        self.assertEqual(status["execution"]["searched"], 1)
        self.assertIsNone(status["verification"]["verified"])
        manifest = json.loads((self.directory / "run.json").read_text())
        self.assertNotIn("search-candidates", manifest["evidence_artifacts"])
        self.assertEqual(canonical_sha256(template), canonical_sha256(json.loads((self.home / "host.json").read_text())))

    def test_missing_login_can_recover_without_replanning(self):
        config, plan = self.setup_grok()
        auth_path = Path(config["grok"]["auth_file"])
        auth_data = json.loads(auth_path.read_text())
        auth_path.unlink()
        task = plan["tasks"][0]
        path = self.home / "first.json"
        missing = collect_search(self.home, self.run_id, task["task_id"], config=config, output=path)
        self.assertEqual(missing["status"], "missing_auth")
        import_search(self.home, self.run_id, [path])
        write(auth_path, auth_data)
        recovered = collect_search(self.home, self.run_id, task["task_id"], config=config,
                                   output=self.home / "second.json", transport=self.transport)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertNotEqual(recovered["receipt_id"], missing["receipt_id"])
        status = import_search(self.home, self.run_id, [self.home / "second.json"])
        self.assertEqual(status["execution"]["searched"], 1)

    def test_output_cannot_overwrite_config_auth_symlink_or_run_state(self):
        config, plan = self.setup_grok()
        auth = Path(config["grok"]["auth_file"])
        alias = self.home / "auth-alias.json"
        alias.symlink_to(auth)
        for path in (auth, alias, self.home / "search-config.json", self.directory / "run.json",
                     self.directory / "search-plan.json", self.directory / "search/receipts/internal.json"):
            before = path.read_bytes() if path.exists() else None
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "output"):
                collect_search(self.home, self.run_id, plan["tasks"][0]["task_id"], config=config, output=path)
            self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_spoofed_grok_receipt_wrong_lane_and_unknown_config_rejected(self):
        plan = plan_search(self.home, self.run_id)
        template_path = self.directory / "search/host-templates" / (plan["tasks"][0]["task_id"] + ".json")
        value = json.loads(template_path.read_text())
        value.update(provider="grok-x", status="succeeded")
        value["execution"].update(searched=True, queries=["query"])
        with self.assertRaisesRegex(ValueError, "Grok 回执"):
            import_search(self.home, self.run_id, [write(self.home / "spoof.json", value)])
        value["provider"] = "host-web"
        with self.assertRaisesRegex(ValueError, "lane"):
            import_search(self.home, self.run_id, [write(self.home / "spoof.json", value)])
        with self.assertRaisesRegex(ValueError, "未知字段"):
            read_config(write(self.home / "invalid-config.json", {"grok": {"secrets": {"token": "not-real"}}}))

    def test_crash_after_attempt_completed_without_receipt_stays_unknown(self):
        config, plan = self.setup_grok()
        task = plan["tasks"][0]
        collect_search(self.home, self.run_id, task["task_id"], config=config, transport=self.transport)
        (self.directory / "search/receipts" / (task["task_id"] + ".json")).unlink()
        with patch("aor.sources.grok_x.search_x", side_effect=AssertionError("must not repeat")):
            result = collect_search(self.home, self.run_id, task["task_id"], config=config, max_cost_usd=1)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["fallback"]["provider"], "host-x")

    def test_paid_fallback_existing_executor_closes_status_loop(self):
        import tikhub_query as tq
        from aor.workflow.research import run_paid_batch
        from tests.test_tikhub_query import healthy_account_transport, pricing_rows
        plan = plan_search(self.home, self.run_id)
        task = plan["tasks"][0]
        with patch.dict(os.environ, {"TIKHUB_API_KEY": "fixture-not-real"}):
            receipt = collect_search(self.home, self.run_id, task["task_id"], max_cost_usd=1)
            paid_file = Path(receipt["fallback"]["paid_plan_path"])
            endpoint = json.loads(paid_file.read_text())["requests"][0]["endpoint"]
            rows = pricing_rows() + [{"endpoint_uri": endpoint, "endpoint_cost": 0.01}]
            real_execute = tq.execute_plan
            def execute(plan, **options):
                return real_execute(plan, **options, transport=lambda **kwargs: {"data": []},
                                    account_transport=healthy_account_transport)
            with patch("tikhub_query.fetch_live_pricing", return_value=rows), patch("tikhub_query.execute_plan", side_effect=execute):
                run_paid_batch(self.home, self.run_id, paid_file, max_cost_usd=1, batch_id="search-" + task["task_id"])
        status = search_status(self.home, self.run_id)
        row = next(row for row in status["tasks"] if row["task_id"] == task["task_id"])
        self.assertEqual(row["execution"], "searched")
        self.assertTrue((self.directory / "paid-journal.sqlite3").exists())
        self.assertEqual(search_status(self.home, self.run_id), status)
        manifest = json.loads((self.directory / "run.json").read_text())
        paid_receipt = _paid_receipts(manifest, plan)[0]
        imported = import_search(self.home, self.run_id, [write(self.home / "paid-receipt.json", paid_receipt)])
        self.assertEqual(imported["evaluation"]["receipt_count"], status["evaluation"]["receipt_count"])

    def test_original_page_import_closes_verification_loop(self):
        from aor.sources.importing import import_web_evidence
        from aor.workflow.research import resume_research
        config, plan = self.setup_grok()
        task = plan["tasks"][0]
        path = self.home / "receipt.json"
        collect_search(self.home, self.run_id, task["task_id"], config=config, output=path, transport=self.transport)
        import_search(self.home, self.run_id, [path])
        evidence = import_web_evidence({"items": [{"source": "twitter", "source_object_id": "2100011781863170285", "object_kind": "post",
            "url": "https://x.com/test/status/2100011781863170285", "title": "Seller process",
            "original_text": "I use a spreadsheet for custom orders.", "supporting_quote": "spreadsheet for custom orders",
            "evidence_role": "usage_behavior", "observed_at": "2026-09-16T10:00:00Z",
            "verification": {"method": "opened_page", "verified_by": "fixture-browser", "verified_at": "2026-09-16T10:00:00Z"}}]},
            run_id=self.run_id, as_of="2026-09-16")
        resume_research(self.home, self.run_id, evidence_files=[write(self.home / "evidence.json", evidence)], collect=False)
        status = search_status(self.home, self.run_id)
        self.assertEqual(status["verification"]["verified"], 1)
        self.assertEqual(status["verification"]["pending"], 0)
        from aor.workflow.research import _artifact, _load_artifact
        manifest = json.loads((self.directory / "run.json").read_text())
        context = _load_artifact(manifest, "evidence-context")
        for row in context["evidence"]:
            row["historical_reference_only"] = True
            row["derivation_status"] = "superseded"
        _artifact(self.directory, manifest, "evidence-context", context)
        self.assertIsNone(search_status(self.home, self.run_id)["verification"]["verified"])

    def test_plan_preserves_followup_provenance_and_calendar_window(self):
        from aor.workflow.research import _artifact
        manifest = json.loads((self.directory / "run.json").read_text())
        followup = {"tasks": [{"query": "order spreadsheet", "id": "followup-a", "language": "en",
            "task_family_id": "TASK-orders", "source_observation_ids": ["OBS-a"],
            "source_evidence_refs": [{"evidence_id": "twitter:123", "revision_id": "revision", "quote": "text"}]}]}
        _artifact(self.directory, manifest, "task-followup-plan", followup)
        plan = plan_search(self.home, self.run_id)
        task = plan["tasks"][0]
        self.assertEqual(task["provenance"]["source_observation_ids"], ["OBS-a"])
        self.assertEqual(task["provenance"]["task_family_id"], "TASK-orders")
        self.assertEqual(task["window"], {"from_date": "2026-08-18", "to_date": "2026-09-16"})

    def test_real_cli_offline_suppresses_network_with_configured_grok(self):
        config, plan = self.setup_grok()
        result = self.cli("collect", "--task-id", plan["tasks"][0]["task_id"], "--offline")
        self.assertEqual(result["status"], "offline")
        self.assertEqual(list((self.directory / "search/attempts").glob("*.json")), [])

    def test_long_query_preserved_and_tikhub_falls_back_without_truncation(self):
        from aor.workflow.research import _artifact
        manifest = json.loads((self.directory / "run.json").read_text())
        query = "seller order workflow " * 10
        _artifact(self.directory, manifest, "task-followup-plan", {"tasks": [{"query": query, "language": "en"}]})
        plan = plan_search(self.home, self.run_id)
        self.assertEqual(plan["tasks"][0]["query"], query.strip())
        with patch.dict(os.environ, {"TIKHUB_API_KEY": "fixture-not-real"}):
            result = collect_search(self.home, self.run_id, plan["tasks"][0]["task_id"], max_cost_usd=1)
        self.assertEqual(result["fallback"]["provider"], "host-x")
        self.assertEqual(result["fallback"]["reason"], "tikhub_query_too_long")

    def _stale_fallback_crash(self, after_completed_attempt):
        from aor_runtime import atomic_json as real_atomic_json
        config, plan = self.setup_grok()
        task_id = plan["tasks"][0]["task_id"]
        auth_path = Path(config["grok"]["auth_file"])
        auth_data = json.loads(auth_path.read_text())
        auth_path.unlink()
        receipt_path = self.directory / "search/receipts" / (task_id + ".json")
        attempt_path = self.directory / "search/attempts" / (task_id + ".json")
        with patch.dict(os.environ, {"TIKHUB_API_KEY": "fixture-not-real"}):
            old = collect_search(self.home, self.run_id, task_id, config=config, max_cost_usd=1)
            self.assertEqual(old["status"], "missing_auth")
            self.assertEqual(old["fallback"]["provider"], "tikhub")
            write(auth_path, auth_data)
            if after_completed_attempt:
                def interrupt_receipt(path, payload):
                    if path == receipt_path and payload.get("status") == "succeeded":
                        raise KeyboardInterrupt("fixture: completed attempt persisted before replacing old receipt")
                    real_atomic_json(path, payload)
                with patch("aor.workflow.search.atomic_json", side_effect=interrupt_receipt), self.assertRaises(KeyboardInterrupt):
                    collect_search(self.home, self.run_id, task_id, config=config, max_cost_usd=1, transport=self.transport)
            else:
                def interrupt_transport(request, **kwargs):
                    raise KeyboardInterrupt("fixture: interrupted after sent marker")
                with self.assertRaises(KeyboardInterrupt):
                    collect_search(self.home, self.run_id, task_id, config=config, max_cost_usd=1, transport=interrupt_transport)
            self.assertEqual(json.loads(receipt_path.read_text()), old)
            attempt = json.loads(attempt_path.read_text())
            self.assertEqual(attempt["status"], "succeeded" if after_completed_attempt else "sent")
            self.assertTrue(attempt["attempt_id"])
            with patch("aor.sources.grok_x.search_x", side_effect=AssertionError("must not send again")) as search:
                recovered = collect_search(self.home, self.run_id, task_id, config=config, max_cost_usd=1)
                repeated = collect_search(self.home, self.run_id, task_id, config=config, max_cost_usd=1)
            search.assert_not_called()
        for result in (recovered, repeated):
            self.assertEqual(result["status"], "outcome_unknown")
            self.assertEqual(result["fallback"]["provider"], "host-x")
            self.assertNotIn("paid_plan_path", result["fallback"])
            self.assertNotIn("--paid-plan", result["fallback"]["argv"])

    def test_old_paid_fallback_receipt_never_masks_new_sent_attempt(self):
        self._stale_fallback_crash(after_completed_attempt=False)

    def test_old_paid_fallback_receipt_never_masks_completed_attempt_without_new_receipt(self):
        self._stale_fallback_crash(after_completed_attempt=True)

    def test_attempt_digest_checked_before_cached_receipt_reuse(self):
        config, plan = self.setup_grok()
        task_id = plan["tasks"][0]["task_id"]
        collect_search(self.home, self.run_id, task_id, config=config, transport=self.transport)
        path = self.directory / "search/attempts" / (task_id + ".json")
        attempt = json.loads(path.read_text())
        attempt["status"] = "sent"
        write(path, attempt)
        with self.assertRaisesRegex(ValueError, "attempt 摘要"):
            collect_search(self.home, self.run_id, task_id, config=config)


if __name__ == "__main__":
    unittest.main()
