from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import valid_report
from tests.test_idea_funnel import benchmark

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "radar.py"


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([sys.executable, str(ENTRY), *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(f"CLI 失败：{args}\n{result.stderr}")
    return result


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class CliJourneyTests(unittest.TestCase):
    def test_exports_plans_through_platform_independent_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_cli("plan", "--date", "2026-09-10", "--home", str(base / "data"),
                    "--export-community-plan", str(base / "community.json"),
                    "--export-tikhub-plan", str(base / "paid.json"), cwd=base)
            community = json.loads((base / "community.json").read_text())
            paid = json.loads((base / "paid.json").read_text())
            self.assertEqual(community["run_id"], paid["run_id"])
            self.assertFalse(paid["scope"]["platform_expansion_enabled"])

    def test_funnel_prepare_score_validate_then_commit_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "data"
            run_cli("state", "init", "--home", str(home), cwd=base)
            run_cli("plan", "--date", "2026-09-10", "--home", str(home), "--output", str(base / "plan.json"), cwd=base)
            plan = json.loads((base / "plan.json").read_text())
            envelope = {key: plan[key] for key in ("schema_version", "run_id", "as_of")}
            write_json(base / "bench.json", {**envelope, "benchmarks": [benchmark()],
                       "dimensions": {"offers": ["订阅", "按次", "人工辅助"]}})
            run_cli("expand", "--input", str(base / "bench.json"), "--output", str(base / "expanded.json"), cwd=base)
            run_cli("filter", "--input", str(base / "expanded.json"), "--output", str(base / "tiered.json"), cwd=base)
            tiered = json.loads((base / "tiered.json").read_text())
            self.assertEqual(len(tiered["deep_candidates"]), 1)
            item = tiered["deep_candidates"][0]
            self.assertEqual(len(item["variants"]), 3)
            item["track"] = "needle"
            item["scores"] = dict.fromkeys(("demand", "new_form", "distribution", "regional_gap", "monetization", "mvp_feasibility", "evidence"), 8)
            item["auxiliary_scores"] = dict.fromkeys(("first_revenue", "scale", "personal_influence", "confidence"), 7)
            write_json(base / "deep.json", {**envelope, "candidates": [item]})
            run_cli("state", "prepare", "--home", str(home), "--kind", "opportunity", "--date", "2026-09-10",
                    "--input", str(base / "deep.json"), "--output", str(base / "prepared.json"), cwd=base)
            run_cli("score", "--input", str(base / "prepared.json"), "--output", str(base / "scored.json"), cwd=base)
            scored = json.loads((base / "scored.json").read_text())
            self.assertEqual(scored[0]["total_score"], 80)
            record_id = scored[0]["id"]
            report = valid_report(1, low_count_reason=True)
            report = re.sub(r"(## 二、已验证快速点子\n).*?(?=\n## 三、)", r"\1\n", report, flags=re.S)
            report = re.sub(r"(## 三、区域迁移创意池\n).*?(?=\n## 四、)", r"\1\n", report, flags=re.S)
            report = report.replace("OPP-20260714-A1B201", record_id)
            report = report.replace("已验证快速点子数量：1", "已验证快速点子数量：0").replace("区域迁移创意数量：1", "区域迁移创意数量：0")
            report = report.replace("日报展示结论数量：3", "日报展示结论数量：1").replace("合格结论数量：3", "合格结论数量：1")
            report = report.replace("单个合格结论估算成本 USD：0.017667", "单个合格结论估算成本 USD：0.053000")
            (base / "report.md").write_text(report, encoding="utf-8")
            self.assertEqual((home / "state" / "opportunities.jsonl").read_text(), "")
            validation = run_cli("report", str(base / "report.md"), "--json", cwd=base)
            self.assertTrue(json.loads(validation.stdout)["valid"])
            args = ("state", "record-batch", "--home", str(home), "--kind", "opportunity", "--date", "2026-09-10",
                    "--run-id", plan["run_id"], "--input", str(base / "scored.json"))
            first = json.loads(run_cli(*args, cwd=base).stdout)
            second = json.loads(run_cli(*args, cwd=base).stdout)
            self.assertIn("created", json.dumps(first))
            self.assertIn("replayed", json.dumps(second))
            records = [json.loads(line) for line in (home / "state" / "opportunities.jsonl").read_text().splitlines()]
            self.assertEqual([record["id"] for record in records], [record_id])
            run_cli("digest", "--tiered", str(base / "tiered.json"), "--output", str(base / "digest.md"),
                    "--metrics-output", str(base / "metrics.json"), cwd=base)
            self.assertEqual(json.loads((base / "metrics.json").read_text())["metrics"]["qualified_family_count"], 1)

    def test_demo_cannot_become_a_and_families_prepare_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_cli("expand", "--input", str(ROOT / "examples" / "benchmarks-and-dimensions.json"),
                    "--output", str(base / "expanded.json"), cwd=base)
            run_cli("filter", "--input", str(base / "expanded.json"), "--output", str(base / "tiered.json"), cwd=base)
            tiered = json.loads((base / "tiered.json").read_text())
            self.assertEqual(tiered["deep_candidates"], [])
            self.assertLess(tiered["summary"]["families"], tiered["summary"]["raw"])
            for kind, key in (("opportunity", "validated_ideas"), ("signal", "regional_signals")):
                rows = [*tiered[key], *tiered["overflow"].get(key, [])]
                if not rows:
                    continue
                path = base / f"{kind}.json"
                write_json(path, rows)
                result = run_cli("state", "prepare", "--home", str(base / "data"), "--kind", kind,
                                 "--date", tiered["as_of"], "--input", str(path), cwd=base)
                prepared = json.loads(result.stdout)
                self.assertEqual(len({row["id"] for row in prepared}), len(rows))


if __name__ == "__main__":
    unittest.main()
