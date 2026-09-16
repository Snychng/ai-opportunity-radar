from __future__ import annotations

import copy
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import tikhub_query as tq
from aor.storage.budget import BudgetError, BudgetStore, budget_report
from aor.storage.request_journal import RequestJournal, read_run_ledger
from test_tikhub_query import healthy_account_transport, pricing_rows, sample_plan


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def plan(self, index=1, count=1):
        plan = copy.deepcopy(sample_plan())
        plan["run_id"] = f"RUN-20260714-{index:010X}"
        plan["requests"] = plan["requests"][:count]
        return plan

    def execute(self, index=1, *, count=1, **options):
        plan = self.plan(index, count)
        defaults = {"token": "synthetic-budget-test", "budget_home": self.home, "max_cost_usd": 1,
                    "account_transport": healthy_account_transport, "transport": lambda **_: {"data": []}}
        defaults.update(options)
        return tq._execute_plan_with_pricing(plan, pricing_rows(), **defaults)

    def configure(self, daily="1", monthly="2", total="3", recurring=False):
        with BudgetStore(self.home) as budget:
            return budget.configure(daily_limit_usd=daily, monthly_limit_usd=monthly,
                                    total_limit_usd=total, recurring_enabled=recurring)

    def test_report_is_read_only_and_unconfigured_is_not_recurring_authority(self):
        missing = self.home / "missing"
        self.assertEqual(budget_report(missing)["authorization_scope"], "explicit_one_shot_only")
        self.assertFalse(missing.exists())
        with mock.patch.object(tq, "fetch_live_pricing") as fetch, self.assertRaisesRegex(tq.PlanError, "尚未配置"):
            tq.execute_plan(self.plan(), token="synthetic", max_cost_usd=1, budget_home=self.home, recurring=True)
        fetch.assert_not_called()
        self.assertEqual(budget_report(self.home)["entry_count"], 0)
        result = self.execute()
        self.assertEqual(result["summary"]["attempts"], 1)
        self.assertEqual(result["global_budget"]["authorization_scope"], "explicit_one_shot_only")

    def test_same_home_cross_run_limit_and_distinct_home_isolation(self):
        self.configure(daily="0.001")
        self.execute(1)
        send = mock.Mock()
        with self.assertRaisesRegex(tq.PlanError, "共享 daily"):
            self.execute(2, transport=send)
        send.assert_not_called()
        self.assertEqual(budget_report(self.home)["occupied_usd"]["total"], "0.001")
        self.execute(3, budget_home=self.home / "independent")

    def test_decimal_precision_zero_cap_and_invalid_limits(self):
        with BudgetStore(self.home) as budget:
            for value in (True, -1, "nan", "Infinity", "oops"):
                with self.subTest(value=value), self.assertRaises(BudgetError):
                    budget.configure(daily_limit_usd=value)
            with self.assertRaises(BudgetError):
                budget.configure(daily_limit_usd=1, recurring_enabled=True)
            budget.configure(total_limit_usd="0")
            with self.assertRaisesRegex(BudgetError, "共享 total"):
                budget.reserve(run_id="r", fingerprint="f", attempt_number=1,
                               list_cost_usd="0.0000004", estimated_cost_usd="0")
            self.assertEqual(budget.snapshot()["entry_count"], 0)

    def test_period_boundaries_use_beijing_execution_time_and_all_caps(self):
        with BudgetStore(self.home) as budget:
            budget.configure(daily_limit_usd="0.6", monthly_limit_usd="1", total_limit_usd="1.5")
            budget.reserve(run_id="first", fingerprint="f", attempt_number=1, list_cost_usd="0.6",
                           estimated_cost_usd="0.6", now="2026-09-01T15:59:59Z")
            with self.assertRaisesRegex(BudgetError, "共享 daily"):
                budget.reserve(run_id="second", fingerprint="f", attempt_number=1, list_cost_usd="0.1",
                               estimated_cost_usd="0.1", now="2026-09-01T15:59:59Z")
            budget.reserve(run_id="second", fingerprint="f", attempt_number=1, list_cost_usd="0.4",
                           estimated_cost_usd="0.4", now="2026-09-01T16:00:00Z")
            with self.assertRaisesRegex(BudgetError, "共享 monthly"):
                budget.reserve(run_id="third", fingerprint="f", attempt_number=1, list_cost_usd="0.1",
                               estimated_cost_usd="0.1", now="2026-09-02T16:00:00Z")
            budget.reserve(run_id="third", fingerprint="f", attempt_number=1, list_cost_usd="0.5",
                           estimated_cost_usd="0.5", now="2026-09-30T16:00:00Z")
            with self.assertRaisesRegex(BudgetError, "共享 total"):
                budget.reserve(run_id="fourth", fingerprint="f", attempt_number=1, list_cost_usd="0.1",
                               estimated_cost_usd="0.1", now="2026-10-01T16:00:00Z")
            report = budget.snapshot(now="2026-09-01T16:00:00Z")
            self.assertEqual(report["day"], "2026-09-02")
            self.assertEqual(report["occupied_usd"], {"daily": "0.4", "monthly": "1.0", "total": "1.5"})
        # 老 as_of 不影响真正执行的计费日。
        result = self.execute(8, budget_home=self.home / "execution-clock")
        self.assertNotEqual(result["global_budget"]["day"], result["as_of"])

    def test_reserve_and_settle_are_idempotent_but_conflicts_fail(self):
        args = dict(run_id="r", fingerprint="f", attempt_number=1, list_cost_usd="0.4", estimated_cost_usd="0.3")
        with BudgetStore(self.home) as budget:
            self.assertTrue(budget.reserve(**args))
            self.assertFalse(budget.reserve(**args))
            with self.assertRaises(BudgetError):
                budget.reserve(**{**args, "list_cost_usd": "0.5"})
            budget.settle(run_id="r", fingerprint="f", attempt_number=1, state="outcome_unknown")
            budget.settle(run_id="r", fingerprint="f", attempt_number=1, state="outcome_unknown")
            with self.assertRaises(BudgetError):
                budget.settle(run_id="r", fingerprint="f", attempt_number=1, state="succeeded")
            report = budget.snapshot()
            self.assertEqual(report["occupied_usd"]["total"], "0.4")
            self.assertEqual(report["estimated_total_usd"], "0.3")

    def test_atomic_reserve_and_settle_roll_back_both_databases(self):
        with RequestJournal(self.home / "journal.sqlite3", run_id="run", as_of="2026-09-14", budget_home=self.home) as journal:
            journal.register_batch("b", "p", [{"request_fingerprint": "f", "source": "reddit"}])
            journal.connection.execute("CREATE TRIGGER reject_attempt BEFORE INSERT ON attempts BEGIN SELECT RAISE(ABORT,'synthetic'); END")
            start = dict(batch_id="b", list_cost_usd=Decimal("0.1"), estimated_cost_usd=Decimal("0.1"),
                         pricing_snapshot={}, max_cost_usd=Decimal(1), max_attempts=1)
            with self.assertRaises(sqlite3.IntegrityError):
                journal.start_attempt("f", **start)
            self.assertEqual(journal.snapshot()["attempts"], 0)
            self.assertEqual(budget_report(self.home)["entry_count"], 0)
            journal.connection.execute("DROP TRIGGER reject_attempt")
            attempt = journal.start_attempt("f", **start)
            journal.connection.execute("CREATE TRIGGER aor_budget.reject_settle BEFORE UPDATE ON budget_entries BEGIN SELECT RAISE(ABORT,'synthetic'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                journal.finish_attempt(attempt, state="succeeded", result={"status": "ok"})
            self.assertEqual(journal.snapshot()["request_states"], {"started": 1})
            self.assertEqual(budget_report(self.home)["states"], {"reserved": 1})
            journal.connection.execute("DROP TRIGGER aor_budget.reject_settle")
            journal.finish_attempt(attempt, state="succeeded", result={"status": "ok"})
            self.assertEqual(budget_report(self.home)["states"], {"succeeded": 1})

    def test_concurrent_runs_cannot_both_reserve_last_budget(self):
        self.configure(daily="0.001")
        barrier = threading.Barrier(2)

        def worker(index):
            with RequestJournal(self.home / f"journal-{index}.sqlite3", run_id=f"run-{index}",
                                as_of="2026-09-14", budget_home=self.home) as journal:
                journal.register_batch("b", "p", [{"request_fingerprint": "f", "source": "reddit"}])
                barrier.wait(timeout=10)
                try:
                    journal.start_attempt("f", batch_id="b", list_cost_usd=Decimal("0.001"),
                                          estimated_cost_usd=Decimal("0.001"), pricing_snapshot={},
                                          max_cost_usd=Decimal(1), max_attempts=1)
                except BudgetError:
                    return "blocked"
                return "reserved"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, (1, 2)))
        self.assertCountEqual(results, ["reserved", "blocked"])
        self.assertEqual(budget_report(self.home)["occupied_usd"]["total"], "0.001")

    def test_interruption_resume_preserves_unknown_charge_and_never_rebuys(self):
        with self.assertRaises(KeyboardInterrupt):
            self.execute(transport=mock.Mock(side_effect=KeyboardInterrupt()))
        self.assertEqual(budget_report(self.home)["states"], {"reserved": 1})
        send = mock.Mock()
        result = self.execute(resume=True, transport=send)
        send.assert_not_called()
        self.assertEqual(result["summary"]["attempts"], 0)
        self.assertEqual(result["global_budget"]["states"], {"outcome_unknown": 1})
        self.assertEqual(result["global_budget"]["occupied_usd"]["total"], "0.001")

    def test_recurring_first_failure_pauses_skips_rest_and_needs_explicit_resume(self):
        self.configure(recurring=True)
        send = mock.Mock(side_effect=RuntimeError("TikHub HTTP 429"))
        result = self.execute(count=2, transport=send, recurring=True, max_attempts=3)
        send.assert_called_once()
        self.assertEqual([r["status"] for r in result["results"]], ["error", "skipped"])
        self.assertEqual(result["global_budget"]["states"], {"failed": 1})
        self.assertTrue(result["global_budget"]["policy"]["paused"])
        with self.assertRaisesRegex(tq.PlanError, "暂停"):
            self.execute(2, recurring=True)
        with BudgetStore(self.home) as budget:
            with self.assertRaises(BudgetError):
                budget.resume(acknowledge_pause=False)
            budget.resume(acknowledge_pause=True)
        # 恢复预算不授权重买失败请求，只继续从未发送的请求。
        after = mock.Mock(return_value={"data": []})
        resumed = self.execute(count=2, resume=True, recurring=True, transport=after)
        after.assert_called_once()
        self.assertEqual(resumed["summary"]["attempts"], 1)
        self.assertEqual(resumed["global_budget"]["occupied_usd"]["total"], "0.011")

    def test_recurring_interruption_pauses_and_unknown_remains_charged(self):
        self.configure(recurring=True)
        with self.assertRaises(KeyboardInterrupt):
            self.execute(recurring=True, transport=mock.Mock(side_effect=KeyboardInterrupt()))
        report = budget_report(self.home)
        self.assertTrue(report["policy"]["paused"])
        self.assertEqual(report["occupied_usd"]["total"], "0.001")

    def test_hard_exit_blocks_other_runs_and_resume_preserves_unsettled_cost(self):
        self.configure(recurring=True)
        program = "\n".join([
            "import os, sys", "from pathlib import Path",
            f"sys.path[:0] = {[str(ROOT / 'src'), str(ROOT / 'scripts'), str(ROOT / 'tests')]!r}",
            "from test_budget import BudgetTests", "case = BudgetTests()", "case.home = Path(sys.argv[1])",
            "case.execute(recurring=True, transport=lambda **kwargs: os._exit(0))",
        ])
        subprocess.run([sys.executable, "-B", "-c", program, str(self.home)], check=True, timeout=10)
        report = budget_report(self.home)
        self.assertEqual(report["states"], {"reserved": 1})
        self.assertEqual(report["recurring_block_reason"], "unsettled_attempts")
        send = mock.Mock()
        with self.assertRaisesRegex(tq.PlanError, "未结算"):
            self.execute(2, recurring=True, transport=send)
        send.assert_not_called()
        with BudgetStore(self.home) as budget, self.assertRaisesRegex(BudgetError, "未结算"):
            budget.resume(acknowledge_pause=True)
        # 生产入口允许原 journal 先恢复未知状态，但不会重新发送同一请求。
        with mock.patch.object(tq, "fetch_live_pricing", return_value=pricing_rows()):
            recovered = tq.execute_plan(self.plan(), token="synthetic", max_cost_usd=1,
                                        budget_home=self.home, recurring=True, resume=True,
                                        account_transport=healthy_account_transport, transport=send)
        send.assert_not_called()
        self.assertEqual(recovered["global_budget"]["states"], {"outcome_unknown": 1})
        self.assertTrue(recovered["global_budget"]["policy"]["paused"])
        self.assertEqual(recovered["global_budget"]["occupied_usd"]["total"], "0.001")
        with BudgetStore(self.home) as budget:
            budget.resume(acknowledge_pause=True)
        self.execute(2, recurring=True, resume=True)
        self.assertEqual(budget_report(self.home)["occupied_usd"]["total"], "0.002")

    def test_running_request_is_not_released_or_misclassified_by_another_run(self):
        self.configure(recurring=True)
        entered, release = threading.Event(), threading.Event()

        def send(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("测试执行器未释放")
            return {"data": []}

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.execute, recurring=True, transport=send)
            try:
                self.assertTrue(entered.wait(5))
                other_send = mock.Mock()
                with self.assertRaisesRegex(tq.PlanError, "未结算"):
                    self.execute(2, recurring=True, transport=other_send)
                other_send.assert_not_called()
                with BudgetStore(self.home) as budget, self.assertRaisesRegex(BudgetError, "未结算"):
                    budget.resume(acknowledge_pause=True)
                report = budget_report(self.home)
                self.assertEqual(report["states"], {"reserved": 1})
                self.assertFalse(report["policy"]["paused"])
            finally:
                release.set()
            result = running.result(timeout=5)
        self.assertEqual(result["global_budget"]["states"], {"succeeded": 1})
        self.assertIsNone(result["global_budget"]["recurring_block_reason"])
        self.execute(2, recurring=True, resume=True)

    def test_memory_and_wal_journals_fail_closed(self):
        with self.assertRaisesRegex(BudgetError, "磁盘"):
            with RequestJournal(None, run_id="r", as_of="2026-09-14", budget_home=self.home):
                pass
        path = self.home / "wal.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self.assertRaisesRegex(BudgetError, "DELETE"):
            with RequestJournal(path, run_id="r", as_of="2026-09-14", budget_home=self.home):
                pass

    def history(self, index=1, cost="0.111"):
        return {"run_id": self.plan(index)["run_id"], "amount_usd": cost,
                "occurred_at": "2026-07-14T12:00:00+08:00", "source_sha256": str(index) * 64,
                "source_refs": [f"audit/estimated-cost-{index}.json"]}

    def test_explicit_history_is_idempotent_conflicts_atomic_and_not_invoice(self):
        with BudgetStore(self.home) as budget:
            entries = [self.history(1), self.history(2, "0.003")]
            budget.import_history(entries)
            report = budget.import_history(entries)
            self.assertEqual(report["occupied_usd"]["total"], "0.114")
            self.assertEqual(report["states"], {"historical_estimate": 2})
            self.assertIn("非供应商账单", report["billing_note"])
            with self.assertRaises(BudgetError):
                budget.import_history([self.history(3, "0.01"), self.history(1, "0.2")])
            self.assertEqual(budget.snapshot()["entry_count"], 2)
            missing = self.history(3)
            missing.pop("occurred_at")
            with self.assertRaises(BudgetError):
                budget.import_history([missing])

    def test_existing_journal_import_and_aggregate_history_never_double_count(self):
        plan = self.plan()
        journal_path = self.home / "legacy.sqlite3"
        self.execute(budget_home=None, journal_path=journal_path)
        with BudgetStore(self.home) as budget:
            budget.import_history([self.history(1, "0.001")])
        send = mock.Mock()
        result = self.execute(journal_path=journal_path, resume=True, transport=send)
        send.assert_not_called()
        self.assertEqual(result["global_budget"]["occupied_usd"]["total"], "0.001")
        # 没有汇总历史的旧日志自动纳入当前 RUN 的既有尝试，且重复恢复不叠加。
        other = self.home / "other"
        result = self.execute(journal_path=journal_path, budget_home=other, resume=True, transport=send)
        self.assertEqual(result["global_budget"]["occupied_usd"]["total"], "0.001")
        self.execute(journal_path=journal_path, budget_home=other, resume=True, transport=send)
        self.assertEqual(budget_report(other)["entry_count"], 1)
        with BudgetStore(other) as budget, self.assertRaisesRegex(BudgetError, "逐次费用"):
            budget.import_history([self.history(1, "0.001")])
        self.assertEqual(read_run_ledger(journal_path)["run_id"], plan["run_id"])

    def test_history_cannot_hide_later_attempts_added_by_legacy_execution(self):
        journal = self.home / "legacy.sqlite3"
        self.execute(budget_home=None, journal_path=journal)
        with BudgetStore(self.home) as budget:
            budget.import_history([self.history(1, "0.001")])
        self.execute(count=2, budget_home=None, journal_path=journal, batch_id="supplement")
        self.assertEqual(read_run_ledger(journal)["list_attempted_cost_usd_exact"], "0.011")
        send = mock.Mock()
        with self.assertRaisesRegex(tq.PlanError, "历史覆盖额"):
            self.execute(count=2, journal_path=journal, batch_id="supplement", resume=True, transport=send)
        send.assert_not_called()
        # 未猜测历史与逐次费用的边界，也没有将整轮费用重复累加。
        self.assertEqual(budget_report(self.home)["occupied_usd"]["total"], "0.001")

    def test_history_does_not_prevent_new_attempt_unknown_settlement(self):
        journal = self.home / "legacy.sqlite3"
        self.execute(budget_home=None, journal_path=journal)
        with BudgetStore(self.home) as budget:
            budget.import_history([self.history(1, "0.001")])
        self.configure(recurring=True)
        with self.assertRaises(KeyboardInterrupt):
            self.execute(count=2, journal_path=journal, batch_id="supplement", recurring=True,
                         transport=mock.Mock(side_effect=KeyboardInterrupt()))
        send = mock.Mock()
        with mock.patch.object(tq, "fetch_live_pricing", return_value=pricing_rows()):
            recovered = tq.execute_plan(self.plan(count=2), token="synthetic", max_cost_usd=1,
                                        journal_path=journal, budget_home=self.home, recurring=True,
                                        batch_id="supplement", resume=True,
                                        account_transport=healthy_account_transport, transport=send)
        send.assert_not_called()
        self.assertEqual(recovered["global_budget"]["states"], {"historical_estimate": 1, "outcome_unknown": 1})
        self.assertEqual(recovered["global_budget"]["occupied_usd"]["total"], "0.011")
        self.assertTrue(recovered["global_budget"]["policy"]["paused"])
        with BudgetStore(self.home) as budget:
            budget.resume(acknowledge_pause=True)

    def test_same_attempt_in_a_new_local_journal_cannot_be_sent_twice(self):
        self.execute()
        send = mock.Mock()
        with self.assertRaisesRegex(tq.PlanError, "本地日志不匹配"):
            self.execute(journal_path=self.home / "copy.sqlite3", transport=send)
        send.assert_not_called()
        self.assertEqual(budget_report(self.home)["entry_count"], 1)

    def test_budget_cli_configure_report_import_and_resume(self):
        history_file = self.home / "history.json"
        history_file.write_text(json.dumps({"entries": [self.history()]}))
        commands = [
            ["budget-configure", "--daily-limit-usd", "1", "--monthly-limit-usd", "2", "--total-limit-usd", "3"],
            ["budget-import", "--input", str(history_file)], ["budget-report"],
            ["budget-resume", "--acknowledge-pause"],
        ]
        for args in commands:
            with self.subTest(command=args[0]), mock.patch.object(sys, "argv", ["tikhub_query.py", *args, "--home", str(self.home)]):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(tq.main(), 0)
                self.assertEqual(json.loads(output.getvalue())["scope"], "data_home")


if __name__ == "__main__":
    unittest.main()
