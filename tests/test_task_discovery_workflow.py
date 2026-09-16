"""任务观察先于产品方案：独立输入、后续检索、补读和完成状态的集成验收。"""

from contextlib import redirect_stdout, redirect_stderr
from datetime import date
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
import research
from aor.reporting.report import validate_structured_report
from aor.sources.planning import validate_intent_plan
from aor.workflow.research import start_research, resume_research
from tests.test_research_workflow import write


DAY = "2026-09-16"


class TaskDiscoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"AOR_OFFLINE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        material = write(self.home / "evidence.json", {"evidence": [{
            "id": "source-order", "source": "web", "url": "https://example.org/order-notes",
            "title": "Custom order production notes", "original_text": "I keep a spreadsheet for each custom order.",
            "observed_at": DAY + "T09:00:00+08:00", "published_at": DAY,
            "industry_ids": ["ecommerce"], "language": "en", "evidence_role": "workflow_pain",
        }]})
        self.run = start_research(self.home, as_of=date.fromisoformat(DAY), focus="custom orders",
                                  evidence_files=[material], offline=True)
        row = self.artifact("evidence-context")["evidence"][0]
        self.observation = {
            "target_user": "custom order makers", "task": "prepare custom order production cards",
            "need": "keep personalization requirements together", "industry_ids": ["ecommerce"],
            "feedback_type": "usage", "sentiment": "mixed", "language": "en",
            "trigger": "starting a new custom order", "current_workaround": "one spreadsheet per order",
            "desired_outcome": "a complete production card", "artifact": "order spreadsheet",
            "query_terms": ["custom order", "production card"],
            "evidence_refs": [{"evidence_id": row["evidence_id"], "revision_id": row["revision_id"],
                               "quote": "I keep a spreadsheet for each custom order."}],
            "solution_hypotheses": [{"delivery_form": "one_off_delivery", "statement": "Prepare a checked production card.",
                                     "status": "hypothesis"}],
        }

    def artifact(self, name):
        return json.loads(Path(self.run["artifacts"][name]["path"]).read_text())

    def observe(self, rows):
        path = write(self.home / "observations.json", {"run_id": self.run["run_id"], "as_of": DAY, "observations": rows})
        self.run = resume_research(self.home, self.run["run_id"], observations_file=path, collect=False)
        return path

    def test_observe_without_product_or_benchmark_then_start_followup_child(self):
        self.observe([self.observation])
        self.assertEqual(self.run["status"], "awaiting_benchmarks")
        self.assertNotIn("benchmarks", self.run["artifacts"])
        discovery = self.artifact("user-discovery")
        self.assertEqual(discovery["summary"]["demand_cluster_count"], 1)
        self.assertFalse(discovery["summary"]["market_validated"])
        intents = self.artifact("task-followup-intents")
        validate_intent_plan(intents)
        self.assertTrue(intents["intents"])
        self.assertTrue(all(i["source"] == "web" for i in intents["intents"]))
        self.assertIn("review-packets", self.run["artifacts"])
        child = start_research(self.home, as_of=date.fromisoformat(DAY), intent_plan=intents,
                               parent_run_id=self.run["run_id"], offline=True)
        self.assertNotEqual(child["run_id"], self.run["run_id"])
        self.assertEqual(child["status"], "awaiting_benchmarks")
        self.assertTrue(self.artifact("evidence-context")["evidence"][0]["task_family_ids"])

    def test_observation_snapshot_replacement_removes_stale_followup(self):
        self.observe([self.observation])
        followup = Path(self.run["artifacts"]["task-followup-intents"]["path"])
        self.observe([])
        self.assertEqual(self.artifact("user-discovery")["summary"]["observation_count"], 0)
        self.assertNotIn("task-followup-intents", self.run["artifacts"])
        self.assertFalse(followup.exists())

    def test_legacy_and_independent_observations_deduplicate_and_finish_without_false_coverage(self):
        path = self.observe([self.observation])
        benchmarks = write(self.home / "benchmarks.json", {"observations": [self.observation]})
        self.run = resume_research(self.home, self.run["run_id"], benchmarks_file=benchmarks, collect=False)
        self.assertEqual(self.artifact("tiered")["user_discovery"]["summary"]["observation_count"], 1)
        assessment = write(self.home / "assessment.json", {"scores": [], "decision": {
            "summary": "已保留具体任务，商业与时间覆盖未完成", "primary_id": None,
            "largest_unknown": "现有工具是否已经解决", "next_action": "检查制作单样本与替代工具", "stop_condition": "替代工具已充分解决",
        }})
        self.run = resume_research(self.home, self.run["run_id"], assessment_file=assessment, collect=False)
        self.assertEqual(self.run["status"], "completed")
        self.assertFalse(self.run["research_completion"]["market_research_complete"])
        self.assertTrue(validate_structured_report(self.artifact("report"))["valid"])
        with self.assertRaisesRegex(ValueError, "不可变"):
            resume_research(self.home, self.run["run_id"], observations_file=path, collect=False)

    def test_cli_passes_observation_input_and_rejects_silently_ignored_combinations(self):
        with patch("research.resume_research", return_value={"status": "awaiting_benchmarks"}) as resume:
            with redirect_stdout(io.StringIO()):
                code = research.main(["resume", self.run["run_id"], "--observations-file", "input.json", "--no-collect"])
            self.assertEqual(code, 0)
            self.assertEqual(resume.call_args.kwargs["observations_file"], Path("input.json"))
        for flags in (["inspect"], ["resume", "--reparse"], ["resume", "--discover", "--max-cost-usd", "1"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()):
                self.assertEqual(research.main([flags[0], self.run["run_id"], *flags[1:], "--observations-file", "input.json"]), 2)


if __name__ == "__main__":
    unittest.main()
