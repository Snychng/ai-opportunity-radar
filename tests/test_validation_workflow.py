from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from manage_validation import ValidationError, assess_candidates, record_experiment, list_experiments  # noqa: E402


def opportunity() -> dict:
    return {
        "id": "OPP-20260910-ABCDEF",
        "evidence_tier": "A",
        "title": "订单答复",
        "acquisition_channel": "商家社群",
        "mvp_days": 14,
        "validation_plan": {
            "required_skills": ["Python"], "required_languages": ["中文"],
            "hours": 5, "budget_usd": 20,
            "hypothesis": "商家愿意支付一次人工辅助答复费用",
            "success_criteria": "真实任务交付后接受报价", "stop_criteria": "无法获得真实任务",
        },
    }


def profile() -> dict:
    return {
        "schema_version": "3.0", "skills": ["Python"], "languages": ["中文"],
        "reachable_channels": ["商家社群"], "weekly_hours": 8,
        "validation_budget_usd": 50, "max_mvp_days": 30,
    }


def experiment() -> dict:
    return {
        "experiment_id": "EXP-20260910-001", "record_id": "OPP-20260910-ABCDEF",
        "run_id": "RUN-20260910-ABCDEF1234", "as_of": "2026-09-10",
        "status": "completed", "hypothesis": "愿意为回复工作付钱",
        "offer": "人工辅助交付一次回复", "success_criteria": "接受报价", "stop_criteria": "没有真实任务",
        "counts": {"contacted": 5, "interviewed": 3, "real_tasks": 2, "accepted_quotes": 1, "paid_trials": 0},
        "cost_usd": 2.5, "minutes_spent": 60,
        "outcome": "有一个客户接受报价，尚未付款", "decision": "continue",
        "next_action": "验证交付质量", "evidence": [{"url": "https://example.org/task/1", "original_text": "测试用报价反馈"}],
    }


class ValidationWorkflowTests(unittest.TestCase):
    def test_unknown_profile_does_not_become_positive_fit(self) -> None:
        result = assess_candidates([opportunity()], {})
        self.assertEqual(result["candidates"][0]["action"], "clarify")
        self.assertTrue(result["candidates"][0]["unknowns"])

    def test_fit_is_separate_from_market_tier_and_not_launch_approval(self) -> None:
        result = assess_candidates([opportunity()], profile())
        item = result["candidates"][0]
        self.assertEqual(item["action"], "validate")
        self.assertEqual(item["evidence_tier"], "A")
        self.assertFalse(item["market_validated"])

    def test_resource_conflicts_and_invalid_profile(self) -> None:
        values = profile()
        values["weekly_hours"] = 1
        self.assertEqual(assess_candidates([opportunity()], values)["candidates"][0]["action"], "park")
        values["weekly_hours"] = -1
        with self.assertRaises(ValidationError):
            assess_candidates([opportunity()], values)

    def test_experiments_are_idempotent_and_conflicts_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            item = experiment()
            self.assertEqual(record_experiment(home, item)["status"], "created")
            self.assertEqual(record_experiment(home, item)["status"], "replayed")
            item["counts"]["paid_trials"] = 1
            with self.assertRaises(ValidationError):
                record_experiment(home, item)
            rows = list_experiments(home)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["counts"]["paid_trials"], 0)

    def test_finished_experiment_needs_result_decision_and_finite_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for field, value in (("outcome", ""), ("decision", ""), ("cost_usd", float("nan")), ("minutes_spent", -1)):
                with self.subTest(field=field), self.assertRaises(ValidationError):
                    item = experiment()
                    item[field] = value
                    record_experiment(Path(tmp), item)

    def test_empty_evidence_cannot_support_customer_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            item = experiment()
            item["counts"]["paid_trials"] = 100
            item["evidence"] = [{}]
            with self.assertRaises(ValidationError):
                record_experiment(Path(tmp), item)
            item["evidence"] = [{"local_ref": "private/validated-order.txt", "fact": "测试：客户订单已人工核验"}]
            self.assertEqual(record_experiment(Path(tmp), item)["status"], "created")

    def test_history_keeps_different_runs_without_promoting_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = experiment()
            record_experiment(home, first)
            second = experiment()
            second.update(run_id="RUN-20260911-ABCDEF1234", as_of="2026-09-11")
            second["counts"]["paid_trials"] = 1
            record_experiment(home, second)
            self.assertEqual(len(list_experiments(home)), 2)
            self.assertEqual(len(list_experiments(home, as_of="2026-09-10")), 1)
            self.assertFalse((home / "state" / "opportunities.jsonl").exists())

    def test_universal_cli_assessment_works_with_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "profile.json").write_text(json.dumps(profile()))
            (base / "candidates.json").write_text(json.dumps([opportunity()]))
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "radar.py"), "validation", "assess",
                 "--profile", str(base / "profile.json"), "--input", str(base / "candidates.json")],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(result.stdout)["candidates"][0]["action"], "validate")


if __name__ == "__main__":
    unittest.main()
