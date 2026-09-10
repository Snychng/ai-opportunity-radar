from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import tikhub_query as tq  # noqa: E402
from aor.storage.request_journal import read_run_ledger  # noqa: E402
from test_tikhub_query import (  # noqa: E402
    account_payload,
    healthy_account_transport,
    pricing_rows,
    sample_plan,
)


class PaidRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / "requests.sqlite3"

    def execute(self, plan: dict[str, Any], **options: Any) -> dict[str, Any]:
        rows = options.pop("rows", pricing_rows())
        defaults = {
            "token": "offline-test-token",
            "max_cost_usd": 0.04,
            "journal_path": self.journal,
            "account_transport": healthy_account_transport,
            "sleep_func": lambda seconds: None,
        }
        defaults.update(options)
        return tq._execute_plan_with_pricing(plan, rows, **defaults)

    @staticmethod
    def one_request_plan() -> dict[str, Any]:
        plan = sample_plan()
        plan["requests"] = plan["requests"][:1]
        return plan

    def test_interrupted_resume_reuses_success_and_does_not_buy_unknown_again(self) -> None:
        plan = sample_plan()
        during_second_request = []

        def interrupt_second(**kwargs: object) -> dict[str, Any]:
            if first_transport.call_count == 1:
                raise RuntimeError("TikHub HTTP 429")
            if first_transport.call_count == 2:
                return {"data": ["saved"]}
            during_second_request.append(read_run_ledger(self.journal))
            raise KeyboardInterrupt()

        first_transport = mock.Mock(side_effect=interrupt_second)
        with self.assertRaises(KeyboardInterrupt):
            self.execute(plan, transport=first_transport, max_attempts=2)
        self.assertEqual(first_transport.call_count, 3)
        self.assertEqual(during_second_request[0]["request_states"], {"succeeded": 1, "started": 1})
        self.assertEqual(Decimal(during_second_request[0]["list_attempted_cost_usd_exact"]), Decimal("0.012"))
        self.assertEqual(during_second_request[0]["attempt_states"],
                         {"succeeded": 1, "failed": 1, "started": 1, "outcome_unknown": 0})
        self.assertEqual(during_second_request[0]["by_source"], [
            {"source": "tiktok", "attempts": 2, "succeeded": 1, "failed": 1, "started": 0, "outcome_unknown": 0,
             "list_attempted_cost_usd_exact": "0.002", "estimated_attempted_cost_usd_exact": "0.0020"},
            {"source": "xiaohongshu", "attempts": 1, "succeeded": 0, "failed": 0, "started": 1, "outcome_unknown": 0,
             "list_attempted_cost_usd_exact": "0.01", "estimated_attempted_cost_usd_exact": "0.01"},
        ])

        resume_transport = mock.Mock()
        result = self.execute(plan, transport=resume_transport, resume=True)

        resume_transport.assert_not_called()
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][0]["response"], {"data": ["saved"]})
        self.assertTrue(result["results"][0]["reused"])
        self.assertEqual(result["results"][0]["cache"], "journal")
        self.assertEqual(result["results"][1]["status"], "outcome_unknown")
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0)
        self.assertEqual(Decimal(result["run_ledger"]["estimated_attempted_cost_usd_exact"]), Decimal("0.012"))
        self.assertEqual(result["run_ledger"]["attempt_states"],
                         {"succeeded": 1, "failed": 1, "started": 0, "outcome_unknown": 1})
        # 在另一批复用同一指纹，来源 JOIN 不应把旧尝试按批次数重复累计。
        self.execute(plan, transport=resume_transport, batch_id="reused-batch")
        resume_transport.assert_not_called()
        ledger = read_run_ledger(self.journal)
        self.assertEqual(ledger["attempts"], 3)
        self.assertEqual(ledger["attempt_states"], result["run_ledger"]["attempt_states"])
        self.assertEqual(ledger["by_source"], result["run_ledger"]["by_source"])
        self.assertEqual(ledger["by_source"][1]["outcome_unknown"], 1)
        self.assertEqual(ledger["by_source"][1]["started"], 0)
        self.assertEqual(sum(Decimal(row["list_attempted_cost_usd_exact"]) for row in ledger["by_source"]),
                         Decimal(ledger["list_attempted_cost_usd_exact"]))

    def test_resume_continues_unstarted_requests_after_an_interrupted_request(self) -> None:
        plan = sample_plan()
        third = copy.deepcopy(plan["requests"][0])
        third["id"] = "global-tiktok-followup"
        third["params"]["keyword"] = "AI workflow complaint"
        plan["requests"].append(third)
        with self.assertRaises(KeyboardInterrupt):
            self.execute(plan, transport=mock.Mock(side_effect=[{"data": [1]}, KeyboardInterrupt()]))

        transport = mock.Mock(return_value={"data": [3]})
        result = self.execute(plan, transport=transport, resume=True)

        transport.assert_called_once()
        self.assertEqual(transport.call_args.kwargs["params"]["keyword"], "AI workflow complaint")
        self.assertEqual([row["status"] for row in result["results"]], ["ok", "outcome_unknown", "ok"])
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0.001)
        self.assertEqual(result["run_ledger"]["list_attempted_cost_usd_exact"], "0.012")

    def test_duplicate_http_requests_keep_both_intents_but_only_buy_once(self) -> None:
        plan = self.one_request_plan()
        plan["requests"][0].update(intent_refs=["intent-demand"], query_group="demand")
        duplicate = copy.deepcopy(plan["requests"][0])
        duplicate.update(id="global-tiktok-pricing", intent_refs=["intent-payment"], query_group="payment")
        duplicate["params"] = dict(reversed(list(duplicate["params"].items())))
        plan["requests"].append(duplicate)
        transport = mock.Mock(return_value={"data": []})

        result = self.execute(plan, transport=transport, max_cost_usd=0.001)

        transport.assert_called_once()
        self.assertEqual(result["estimate"]["request_count"], 1)
        self.assertEqual(result["estimate"]["logical_request_count"], 2)
        self.assertEqual(len(result["results"]), 1)
        row = result["results"][0]
        self.assertEqual(set(row["request_ids"]), {"global-tiktok-1", "global-tiktok-pricing"})
        self.assertEqual(set(row["intent_refs"]), {"intent-demand", "intent-payment"})
        self.assertEqual(
            {entry["id"]: (entry["query_group"], entry["intent_refs"]) for entry in row["query_metadata"]},
            {"global-tiktok-1": ("demand", ["intent-demand"]),
             "global-tiktok-pricing": ("payment", ["intent-payment"])},
        )
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0.001)

    def test_reuse_preserves_response_at_the_same_byte_limit(self) -> None:
        plan = self.one_request_plan()
        transport = mock.Mock(return_value={"data": []})
        with mock.patch.object(tq, "MAX_BATCH_RESULT_BYTES", 11):
            self.execute(plan, transport=transport)
            resumed = self.execute(plan, transport=transport, resume=True)
        transport.assert_called_once()
        self.assertEqual(resumed["results"][0]["status"], "ok")
        self.assertEqual(resumed["results"][0]["response"], {"data": []})
        self.assertEqual(resumed["summary"]["stored_result_bytes"], 11)

    def test_cli_cannot_overwrite_journal_with_result_output(self) -> None:
        plan = self.one_request_plan()
        self.execute(plan, transport=mock.Mock(return_value={"data": []}))
        before = self.journal.read_bytes()
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        argv = ["tikhub_query.py", "run", "--plan", str(plan_path), "--journal", str(self.journal),
                "--resume", "--output", str(self.journal), "--max-cost-usd", "0.04"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(tq, "execute_plan") as execute, redirect_stderr(io.StringIO()):
            self.assertEqual(tq.main(), 2)
        execute.assert_not_called()
        self.assertEqual(before, self.journal.read_bytes())

    def test_http_404_is_confirmed_failure_not_unknown_delivery(self) -> None:
        transport = mock.Mock(side_effect=HTTPError("https://api.tikhub.dev", 404, "not found", {}, None))
        result = self.execute(self.one_request_plan(), transport=transport, max_attempts=3)
        transport.assert_called_once()
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertEqual(result["results"][0]["error_code"], "request_error")

    def test_timeout_is_unknown_and_never_automatically_retried(self) -> None:
        plan = self.one_request_plan()
        transport = mock.Mock(side_effect=TimeoutError("响应超时，无法确定是否扣费"))
        result = self.execute(plan, transport=transport, max_attempts=3)
        transport.assert_called_once()
        self.assertEqual(result["results"][0]["status"], "outcome_unknown")

        resumed = self.execute(plan, transport=transport, max_attempts=3, resume=True)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(resumed["results"][0]["status"], "outcome_unknown")
        self.assertEqual(resumed["summary"]["estimated_attempted_cost_usd"], 0)
        self.assertEqual(resumed["run_ledger"]["list_attempted_cost_usd_exact"], "0.001")

    def test_connection_loss_is_unknown_and_resume_never_repeats_possible_paid_request(self) -> None:
        failures = [
            ConnectionResetError("连接已重置"),
            IncompleteRead(b"partial response", 100),
            RemoteDisconnected("远端在返回结果前断开连接"),
        ]
        for index, failure in enumerate(failures):
            with self.subTest(error=type(failure).__name__):
                journal = self.journal.with_name(f"network-{index}.sqlite3")
                transport = mock.Mock(side_effect=failure)
                plan = self.one_request_plan()

                result = self.execute(plan, transport=transport, max_attempts=3, journal_path=journal)

                transport.assert_called_once()
                self.assertEqual(result["results"][0]["status"], "outcome_unknown")
                self.assertEqual(result["results"][0]["error_code"], "network_error")
                resumed = self.execute(plan, transport=transport, max_attempts=3,
                                       journal_path=journal, resume=True)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(resumed["results"][0]["status"], "outcome_unknown")
                self.assertEqual(resumed["summary"]["estimated_attempted_cost_usd"], 0)
                self.assertEqual(Decimal(resumed["run_ledger"]["list_attempted_cost_usd_exact"]), Decimal("0.001"))

    def test_explicit_unknown_retry_still_obeys_run_budget_and_lifetime_attempt_limit(self) -> None:
        plan = self.one_request_plan()
        transport = mock.Mock(side_effect=TimeoutError("未收到响应"))
        self.execute(plan, transport=transport, max_attempts=2, max_cost_usd=0.002)
        retry = {"resume": True, "max_attempts": 2, "resolve_unknown": [plan["requests"][0]["id"]]}

        with self.assertRaises(tq.BudgetExceeded):
            self.execute(plan, transport=transport, max_cost_usd=0.0019, **retry)
        self.assertEqual(transport.call_count, 1)

        result = self.execute(plan, transport=transport, max_cost_usd=0.002, **retry)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result["results"][0]["status"], "outcome_unknown")
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0.001)
        self.assertEqual(result["run_ledger"]["list_attempted_cost_usd_exact"], "0.002")
        with self.assertRaises(tq.PlanError):
            self.execute(plan, transport=transport, max_cost_usd=0.01, **retry)
        self.assertEqual(transport.call_count, 2)

    def test_one_retry_selector_cannot_authorize_two_requests_when_id_matches_another_fingerprint(self) -> None:
        for field, failure in (("resolve_unknown", TimeoutError("无响应")),
                               ("retry_failed", RuntimeError("TikHub HTTP 404"))):
            with self.subTest(authorization=field):
                plan = self.one_request_plan()
                second = copy.deepcopy(plan["requests"][0])
                second["id"] = "second-query"
                second["params"]["keyword"] = "another query"
                plan["requests"].append(second)
                plan["requests"][0]["id"] = tq.request_fingerprint(second)
                journal = self.journal.with_name(field + ".sqlite3")
                transport = mock.Mock(side_effect=failure)
                self.execute(plan, transport=transport, journal_path=journal, max_attempts=2)
                self.assertEqual(transport.call_count, 2)
                with self.assertRaisesRegex(tq.PlanError, "冲突"):
                    self.execute(plan, transport=transport, journal_path=journal, resume=True, max_attempts=2,
                                 **{field: [plan["requests"][0]["id"]]})
                self.assertEqual(transport.call_count, 2)

    def test_resume_rejects_changed_plan_before_using_saved_results(self) -> None:
        plan = self.one_request_plan()
        transport = mock.Mock(return_value={"data": ["first query"]})
        self.execute(plan, transport=transport)
        changed = copy.deepcopy(plan)
        changed["requests"][0]["params"]["keyword"] = "different paid query"

        with self.assertRaises(tq.PlanError):
            self.execute(changed, transport=transport, resume=True)
        self.assertEqual(transport.call_count, 1)

    def test_shared_run_budget_counts_prior_batches_and_reuses_same_http_request(self) -> None:
        plan = sample_plan()
        transport = mock.Mock(return_value={"data": []})
        self.execute(plan, transport=transport, max_cost_usd=0.012)

        repeated = self.one_request_plan()
        repeated["requests"][0]["id"] = "repeat-for-payment-intent"
        reused = self.execute(repeated, transport=transport, batch_id="repeat", max_cost_usd=0.012)
        self.assertEqual(transport.call_count, 2)
        self.assertTrue(reused["results"][0]["reused"])
        self.assertEqual(reused["summary"]["estimated_attempted_cost_usd"], 0)

        additional = self.one_request_plan()
        additional["requests"][0]["params"]["keyword"] = "second evidence batch"
        with self.assertRaises(tq.BudgetExceeded):
            self.execute(additional, transport=transport, batch_id="evidence", max_cost_usd=0.0115)
        self.assertEqual(transport.call_count, 2)
        result = self.execute(additional, transport=transport, batch_id="funded-evidence", max_cost_usd=0.012)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0.001)
        self.assertEqual(result["run_ledger"]["list_attempted_cost_usd_exact"], "0.012")

    def test_new_batch_checks_current_account_balance_before_new_paid_request(self) -> None:
        transport = mock.Mock(return_value={"data": []})
        self.execute(self.one_request_plan(), transport=transport)
        second = sample_plan()
        second["requests"] = second["requests"][1:]
        account = mock.Mock(return_value=account_payload(balance=0.009, free_credit=1.0))

        with self.assertRaisesRegex(tq.PlanError, "付费余额不足"):
            self.execute(second, transport=transport, account_transport=account, batch_id="second")
        account.assert_called_once()
        self.assertEqual(transport.call_count, 1)

    def test_price_changes_preserve_old_exact_cost_and_budget_new_cost_without_rounding(self) -> None:
        plan = self.one_request_plan()
        rows = pricing_rows()
        rows[1]["endpoint_cost"] = 0.0000004
        transport = mock.Mock(return_value={"data": []})
        self.execute(plan, rows=rows, transport=transport, discount_rate=0.8, max_cost_usd=0.0000011)

        rows[1]["endpoint_cost"] = 0.0000007
        second = copy.deepcopy(plan)
        second["requests"][0]["params"]["keyword"] = "new query at changed price"
        with self.assertRaises(tq.BudgetExceeded):
            self.execute(second, rows=rows, transport=transport, discount_rate=0.8,
                         batch_id="too-small", max_cost_usd=0.0000010)
        self.assertEqual(transport.call_count, 1)
        result = self.execute(second, rows=rows, transport=transport, discount_rate=0.8,
                              batch_id="funded", max_cost_usd=0.0000011)
        ledger = result["run_ledger"]
        self.assertIsInstance(ledger["list_attempted_cost_usd_exact"], str)
        self.assertIsInstance(ledger["estimated_attempted_cost_usd_exact"], str)
        self.assertEqual(Decimal(ledger["list_attempted_cost_usd_exact"]), Decimal("0.0000011"))
        self.assertEqual(Decimal(ledger["estimated_attempted_cost_usd_exact"]), Decimal("0.00000088"))

    def test_journal_and_reused_results_never_persist_credentials(self) -> None:
        token = "paid-recovery-private-fixture-token"
        response_secret = "private-response-credential-fixture"
        error_secret = "private-error-credential-fixture"
        transport = mock.Mock(side_effect=[
            {"data": {"title": "可以保存", "token": response_secret, "note": f"Bearer {token}"}},
            RuntimeError(f"TikHub HTTP 401: api_key={error_secret}"),
        ])
        self.execute(sample_plan(), transport=transport, token=token)
        resume_transport = mock.Mock()
        result = self.execute(sample_plan(), transport=resume_transport, token=token, resume=True)
        resume_transport.assert_not_called()

        self.assertTrue(self.journal.is_file())
        self.assertGreater(self.journal.stat().st_size, 0)
        persisted = b"".join(path.read_bytes() for path in Path(self.temp.name).glob("requests.sqlite3*"))
        self.assertTrue("可以保存".encode() in persisted or b"\\u53ef\\u4ee5\\u4fdd\\u5b58" in persisted)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("可以保存", serialized)
        for secret in (token, response_secret, error_secret, "must-not-be-persisted@example.com"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, serialized)
                self.assertNotIn(secret.encode(), persisted)

    def test_known_failed_request_needs_explicit_retry_authorization_after_resume(self) -> None:
        plan = self.one_request_plan()
        transport = mock.Mock(side_effect=[RuntimeError("TikHub HTTP 401: denied"), {"data": ["retried"]}])
        self.execute(plan, transport=transport, max_attempts=2, max_cost_usd=0.002)
        resumed = self.execute(plan, transport=transport, resume=True, max_attempts=2, max_cost_usd=0.002)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(resumed["results"][0]["status"], "error")

        result = self.execute(plan, transport=transport, resume=True, max_attempts=2,
                              retry_failed=[plan["requests"][0]["id"]], max_cost_usd=0.002)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0.001)
        self.assertEqual(result["run_ledger"]["list_attempted_cost_usd_exact"], "0.002")

    def test_known_retryable_http_failure_can_retry_within_authorized_attempts(self) -> None:
        transport = mock.Mock(side_effect=[RuntimeError("TikHub HTTP 429: retry later"), {"data": ["ok"]}])
        result = self.execute(self.one_request_plan(), transport=transport, max_attempts=2, max_cost_usd=0.002)

        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["summary"]["estimated_attempted_cost_usd"], 0.002)
        self.assertEqual(result["run_ledger"]["list_attempted_cost_usd_exact"], "0.002")

    def test_active_execution_blocks_second_session_before_any_nested_paid_request(self) -> None:
        plan = self.one_request_plan()
        nested_transport = mock.Mock(return_value={"data": ["must not be requested"]})
        blocked = []

        def transport(**kwargs: object) -> dict[str, Any]:
            try:
                self.execute(plan, transport=nested_transport, resume=True)
            except tq.PlanError as exc:
                blocked.append(exc)
            return {"data": ["outer request completed"]}

        result = self.execute(plan, transport=transport)

        self.assertEqual(len(blocked), 1)
        nested_transport.assert_not_called()
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["run_ledger"]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
