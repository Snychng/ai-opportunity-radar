from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class CliJourneyTests(unittest.TestCase):
    def test_exports_tikhub_plan_for_separate_estimation_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tikhub_plan = root / "tikhub-plan.json"
            community_plan = root / "community-plan.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_query_plan.py"),
                    "--date",
                    "2026-07-14",
                    "--home",
                    str(root / "radar"),
                    "--export-tikhub-plan",
                    str(tikhub_plan),
                    "--export-community-plan",
                    str(community_plan),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            exported = json.loads(tikhub_plan.read_text(encoding="utf-8"))
            self.assertEqual(exported["provider"], "tikhub")
            self.assertEqual(exported["stage"], "search_discovery")
            self.assertGreater(len(exported["requests"]), 0)
            self.assertEqual(exported["scope"]["id"], "phase_1_existing_platforms")
            self.assertFalse(exported["scope"]["platform_expansion_enabled"])
            community = json.loads(community_plan.read_text(encoding="utf-8"))
            self.assertEqual(community["provider"], "community-public")
            self.assertEqual(community["run_id"], exported["run_id"])

    def test_daily_preparation_scoring_state_and_validation_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "radar"
            init_result = subprocess.run(
                [sys.executable, str(SCRIPTS / "manage_state.py"), "init", "--home", str(home)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("initialized", init_result.stdout)

            plan_result = subprocess.run(
                [sys.executable, str(SCRIPTS / "build_query_plan.py"), "--date", "2026-07-14", "--home", str(home)],
                check=True,
                capture_output=True,
                text=True,
            )
            plan = json.loads(plan_result.stdout)
            self.assertEqual(plan["as_of"], "2026-07-14")

            candidate_path = home / "candidate.json"
            candidate_path.write_text(json.dumps({
                "name": "区域化 AI 社交产品",
                "track": "regional_gap",
                "target_user": "东南亚消费者",
                "context": "线上娱乐",
                "problem_or_desire": "缺少本地语言体验",
                "wedge": "本地语言虚拟角色",
                "evidence": [
                    {"source": "github", "url": "https://github.com/example/repo/issues/1"},
                    {"source": "hackernews", "url": "https://news.ycombinator.com/item?id=1"}
                ],
                "scores": {
                    "demand": 8,
                    "new_form": 8,
                    "distribution": 7,
                    "regional_gap": 9,
                    "monetization": 7,
                    "mvp_feasibility": 8,
                    "evidence": 6
                },
                "auxiliary_scores": {
                    "first_revenue": 7,
                    "scale": 8,
                    "personal_influence": 6,
                    "confidence": 6
                }
            }, ensure_ascii=False))
            prepared_path = home / "prepared-candidate.json"
            subprocess.run(
                [
                    sys.executable, str(SCRIPTS / "manage_state.py"), "prepare",
                    "--home", str(home), "--kind", "opportunity", "--date", "2026-07-14",
                    "--input", str(candidate_path), "--output", str(prepared_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            score_result = subprocess.run(
                [sys.executable, str(SCRIPTS / "score_candidates.py"), "--input", str(prepared_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            scored = json.loads(score_result.stdout)[0]
            self.assertGreater(scored["total_score"], 70)

            state_input = home / "state-candidate.json"
            state_input.write_text(json.dumps(scored, ensure_ascii=False))
            record_result = subprocess.run(
                [
                    sys.executable, str(SCRIPTS / "manage_state.py"), "record", "--home", str(home),
                    "--kind", "opportunity", "--date", "2026-07-14", "--run-id", plan["run_id"],
                    "--input", str(state_input),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(record_result.stdout)["status"], "created")

            sys.path.insert(0, str(ROOT))
            from tests.helpers import valid_report

            report = home / "reports" / "daily" / "2026-07-14.md"
            report.write_text(valid_report())
            validation = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate_report.py"), str(report), "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(validation.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
