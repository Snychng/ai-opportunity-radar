from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.reporting.report import commit_report, validate_structured_report
from aor.workflow.research import resume_research, start_research
from tests.test_idea_funnel import benchmark


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


class ResearchWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.environment = patch.dict(os.environ, {"AOR_OFFLINE": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assessment(self, run: dict) -> Path:
        tiered = json.loads(Path(run["artifacts"]["tiered"]["path"]).read_text())
        scores = [{"id": row["id"], "track": "needle",
                   "scores": dict.fromkeys(("demand", "new_form", "distribution", "regional_gap",
                                            "monetization", "mvp_feasibility", "evidence"), 8),
                   "auxiliary_scores": dict.fromkeys(("first_revenue", "scale", "personal_influence", "confidence"), 7)}
                  for row in tiered["deep_candidates"]]
        return write(self.home / "assessment-input.json", {"scores": scores,
            "decision": {"summary": "优先核验真实任务中的交付成本", "primary_id": None,
                         "largest_unknown": "尚无个人获客验证", "next_action": "取得一份真实任务样本",
                         "stop_condition": "无法获得真实任务则暂停"}})

    def test_agent_handoff_report_commit_and_resume_after_interruption(self):
        run = start_research(self.home, as_of=date(2026, 9, 10), offline=True)
        self.assertEqual(run["status"], "awaiting_benchmarks")
        bench = write(self.home / "bench-input.json", {"benchmarks": [benchmark()], "dimensions": {"offers": ["订阅", "按次"]}})
        run = resume_research(self.home, run["run_id"], benchmarks_file=bench)
        self.assertEqual(run["status"], "awaiting_assessment")
        self.assertEqual((self.home / "state/opportunities.jsonl").read_text(), "")
        assessment = self.assessment(run)

        def interrupted(home, report):
            commit_report(home, report)
            raise OSError("模拟提交已完成但回执保存前退出")

        with patch("aor.workflow.research.commit_report", side_effect=interrupted):
            with self.assertRaises(OSError):
                resume_research(self.home, run["run_id"], assessment_file=assessment)
        finished = resume_research(self.home, run["run_id"])
        self.assertEqual(finished["status"], "completed")
        records = [json.loads(line) for line in (self.home / "state/opportunities.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["variants"]), 2)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertTrue(validate_structured_report(report)["valid"])
        self.assertEqual(records[0]["report_sha256"], json.loads(Path(finished["artifacts"]["receipt"]["path"]).read_text())["report_sha256"])
        self.assertIn("收费对标支持的候选", Path(finished["report_path"]).read_text())
        self.assertEqual(resume_research(self.home, run["run_id"])["status"], "completed")
        with self.assertRaisesRegex(ValueError, "不可变"):
            resume_research(self.home, run["run_id"], assessment_file=assessment)
        report["metrics"]["deep_candidate_count"] = 99
        self.assertFalse(validate_structured_report(report)["valid"])

    def test_empty_research_is_valid_and_same_day_new_runs_are_distinct(self):
        first = start_research(self.home, as_of=date(2026, 9, 10), offline=True)
        second = start_research(self.home, as_of=date(2026, 9, 10), offline=True, parent_run_id=first["run_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        bench = write(self.home / "empty.json", {"benchmarks": [], "empty_reason": "现有证据无法确认收费对标"})
        run = resume_research(self.home, first["run_id"], benchmarks_file=bench)
        finished = resume_research(self.home, first["run_id"], assessment_file=self.assessment(run))
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertEqual(report["metrics"]["qualified_conclusion_count"], 0)
        self.assertEqual((self.home / "state/opportunities.jsonl").read_text(), "")


if __name__ == "__main__":
    unittest.main()
