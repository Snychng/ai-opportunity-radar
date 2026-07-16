from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_result_digest import DigestError, build_result_digest  # noqa: E402
from expand_ideas import expand_ideas  # noqa: E402
from filter_ideas import filter_ideas  # noqa: E402


def example_tiered() -> dict:
    payload = json.loads((ROOT / "examples" / "benchmarks-and-dimensions.json").read_text(encoding="utf-8"))
    return filter_ideas(expand_ideas(payload, limit=200))


def execution() -> dict:
    return {
        "summary": {
            "requests": 3,
            "ok": 2,
            "error": 1,
            "estimated_attempted_cost_usd": 0.053,
            "by_source": [
                {
                    "source": "vendor-pricing",
                    "requests": 2,
                    "ok": 2,
                    "error": 0,
                    "estimated_attempted_cost_usd": 0.05,
                },
                {
                    "source": "merchant-community",
                    "requests": 1,
                    "ok": 0,
                    "error": 1,
                    "estimated_attempted_cost_usd": 0.003,
                },
            ],
        }
    }


def evidence_payload() -> dict:
    return {
        "evidence": [
            {"source": "vendor-pricing", "url": "https://vendor.example/pricing"},
            {"source": "merchant-community", "url": "https://community.example/customer-support-cost"},
            {"source": "agency-pricing", "url": "https://agency.example/pricing"},
            {"source": "job-board", "url": "https://jobs.example/customer-support-contract"},
        ]
    }


class ResultDigestTests(unittest.TestCase):
    def test_displays_all_qualified_and_overflow_candidates(self) -> None:
        tiered = example_tiered()
        result = build_result_digest(
            tiered,
            executions=[execution()],
            evidence_payloads=[evidence_payload()],
            research_payloads=[{"ranked_candidates": [{"id": 1}, {"id": 2}]}],
        )

        metrics = result["metrics"]
        self.assertEqual(metrics["benchmark_count"], 2)
        self.assertEqual(metrics["raw_candidate_count"], 200)
        self.assertEqual(metrics["deep_candidate_count"], 72)
        self.assertEqual(metrics["quick_idea_count"], 30)
        self.assertEqual(metrics["regional_signal_count"], 98)
        self.assertEqual(metrics["suggested_report_display_count"], 115)
        self.assertEqual(metrics["additional_conclusion_count"], 85)
        self.assertEqual(metrics["qualified_conclusion_count"], 200)
        self.assertEqual(metrics["paid_request_count"], 3)
        self.assertEqual(metrics["estimated_cost_usd"], 0.053)
        self.assertEqual(metrics["cost_per_qualified_conclusion_usd"], 0.000265)
        self.assertEqual(metrics["normalized_evidence_count"], 4)
        self.assertEqual(metrics["evidence_utilization_percent"], 100.0)
        self.assertIn(tiered["overflow"]["regional_signals"][-1]["candidate_id"], result["markdown"])
        self.assertIn("不会只保留 Top 5", result["markdown"])

    def test_prioritizes_near_miss_rejections_and_limits_display(self) -> None:
        tiered = example_tiered()
        tiered["rejected"] = [
            {"candidate_id": "CAND-MANY", "title": "失败较多", "rejection_reasons": ["a", "b", "c"]},
            {"candidate_id": "CAND-NEAR", "title": "只差一项", "rejection_reasons": ["clear_payer"]},
        ]
        result = build_result_digest(tiered, rejected_limit=1)

        self.assertEqual(result["metrics"]["rejected_total"], 2)
        self.assertEqual(result["metrics"]["rejected_displayed"], 1)
        self.assertIsNone(result["metrics"]["estimated_cost_usd"])
        self.assertIsNone(result["metrics"]["cost_per_qualified_conclusion_usd"])
        self.assertIn("预计费用 USD：未知", result["markdown"])
        self.assertIn("CAND-NEAR", result["markdown"])
        self.assertNotIn("CAND-MANY", result["markdown"])

    def test_rejects_invalid_shapes_and_money(self) -> None:
        with self.assertRaisesRegex(DigestError, "deep_candidates"):
            build_result_digest({"deep_candidates": "bad"})
        with self.assertRaisesRegex(DigestError, "非负有限"):
            build_result_digest(example_tiered(), executions=[{"summary": {"estimated_attempted_cost_usd": -1}}])
        with self.assertRaisesRegex(DigestError, "rejected_limit"):
            build_result_digest(example_tiered(), rejected_limit=101)

    def test_cli_writes_markdown_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiered_path = root / "tiered.json"
            execution_path = root / "execution.json"
            evidence_path = root / "evidence.json"
            research_path = root / "research.json"
            output_path = root / "digest.md"
            metrics_path = root / "metrics.json"
            tiered_path.write_text(json.dumps(example_tiered(), ensure_ascii=False), encoding="utf-8")
            execution_path.write_text(json.dumps(execution()), encoding="utf-8")
            evidence_path.write_text(json.dumps(evidence_payload()), encoding="utf-8")
            research_path.write_text(json.dumps({"clusters": [{"id": 1}]}), encoding="utf-8")

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_result_digest.py"),
                    "--tiered",
                    str(tiered_path),
                    "--execution",
                    str(execution_path),
                    "--execution",
                    str(execution_path),
                    "--evidence",
                    str(evidence_path),
                    "--research",
                    str(research_path),
                    "--output",
                    str(output_path),
                    "--metrics-output",
                    str(metrics_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))["metrics"]
            self.assertEqual(metrics["paid_request_count"], 3)
            self.assertIn("全部 R 级区域迁移创意", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
