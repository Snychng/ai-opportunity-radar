from __future__ import annotations

import json
from copy import deepcopy
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
    from tests.test_idea_funnel import benchmark

    expanded = expand_ideas({
        "run_id": "RUN-20260715-ABCDEF1234", "as_of": "2026-07-15",
        "benchmarks": [benchmark()], "dimensions": {"offers": ["订阅", "按次", "人工审核"]},
    })
    base = deepcopy(expanded["candidates"][0])
    for index in range(45):
        item = deepcopy(base)
        item.update(candidate_id=f"B-{index}", context=f"快速任务{index}")
        item["payment_signals"] = [{"type": "pricing", "region": "美国", "url": "https://vendor.example/pricing", "fact": "展示每月49美元"}]
        expanded["candidates"].append(item)
    for index in range(85):
        item = deepcopy(base)
        item.update(candidate_id=f"R-{index}", context=f"地区任务{index}", target_region="印度尼西亚", transfer_reason="同类任务但当地尚未验证")
        item["market_scope"] = {"country": "印度尼西亚", "primary_channel": "商家社群"}
        expanded["candidates"].append(item)
    return filter_ideas(expanded)


def execution() -> dict:
    return {
        "run_id": "RUN-20260715-ABCDEF1234", "as_of": "2026-07-15",
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
            {"source": "vendor", "url": "https://vendor.example/receipt"},
            {"source": "forum", "url": "https://forum.example/1"},
            {"source": "agency-pricing", "url": "https://agency.example/pricing"},
            {"source": "job-board", "url": "https://jobs.example/customer-support-contract"},
        ]
    }


class ResultDigestTests(unittest.TestCase):
    def test_demo_and_truncation_remain_visible_after_full_pipeline(self) -> None:
        payload = json.loads((ROOT / "examples" / "benchmarks-and-dimensions.json").read_text())
        expanded = expand_ideas(payload, limit=200)
        tiered = filter_ideas(expanded)
        result = build_result_digest(tiered)

        self.assertTrue(result["metrics"]["contains_demo_data"])
        self.assertTrue(result["metrics"]["expansion_truncated"])
        self.assertEqual(tiered["expansion_summary"], expanded["summary"])
        self.assertIn("包含演示数据", result["markdown"].split("## 结果总览")[0])
        self.assertIn("不代表穷尽全部组合", result["markdown"])
        self.assertIn("扩展已达到数量或扫描上限", result["markdown"])

        complete = build_result_digest(filter_ideas(expand_ideas(payload, limit=500)))
        self.assertFalse(complete["metrics"]["expansion_truncated"])
        self.assertNotIn("扩展可能受限", complete["markdown"])

    def test_inherits_demo_marker_from_document_and_nested_variant_evidence(self) -> None:
        from tests.test_idea_funnel import benchmark

        tiered = filter_ideas(expand_ideas({"benchmarks": [benchmark()]}))
        identifier = tiered["deep_candidates"][0]["candidate_id"]
        nested = deepcopy(tiered)
        nested["deep_candidates"][0]["variants"][0]["evidence"][0]["is_demo"] = True
        inherited = deepcopy(tiered)
        inherited["is_demo"] = True
        for label, payload in (("nested", nested), ("inherited", inherited)):
            with self.subTest(marker=label):
                result = build_result_digest(payload)
                self.assertTrue(result["metrics"]["contains_demo_data"])
                self.assertEqual(result["metrics"]["demo_family_count"], 1)
                self.assertIn(f"{identifier}（演示）", result["markdown"])
        self.assertFalse(build_result_digest(tiered)["metrics"]["contains_demo_data"])

    def test_exact_limit_does_not_claim_proven_missing_combinations(self) -> None:
        from tests.test_idea_funnel import benchmark

        tiered = filter_ideas(expand_ideas({"benchmarks": [benchmark()]}, limit=1))
        result = build_result_digest(tiered)
        self.assertIn("扩展可能受限", result["markdown"])
        self.assertIn("尚未确认是否仍有未生成组合", result["markdown"])
        self.assertNotIn("未穷尽全部组合", result["markdown"])

    def test_demo_family_is_labeled_in_a_mixed_result(self) -> None:
        from tests.test_idea_funnel import benchmark

        real = benchmark()
        demo = benchmark()
        demo.update(is_demo=True, job="演示任务")
        tiered = filter_ideas(expand_ideas({"benchmarks": [real, demo]}))
        result = build_result_digest(tiered)
        self.assertEqual(result["metrics"]["demo_family_count"], 1)
        demo_id = tiered["validated_ideas"][0]["candidate_id"]
        real_id = tiered["deep_candidates"][0]["candidate_id"]
        self.assertIn(f"{demo_id}（演示）", result["markdown"])
        self.assertNotIn(f"{real_id}（演示）", result["markdown"])

    def test_family_differences_are_visible_without_reading_json(self) -> None:
        payload = json.loads((ROOT / "examples" / "benchmarks-and-dimensions.json").read_text())
        tiered = filter_ideas(expand_ideas(payload, limit=500))
        result = build_result_digest(tiered)
        records = [*tiered["deep_candidates"], *tiered["validated_ideas"], *tiered["regional_signals"],
                   *tiered["overflow"]["validated_ideas"], *tiered["overflow"]["regional_signals"]]
        rows = []
        for record in records:
            row = next(line for line in result["markdown"].splitlines() if line.startswith(f"| {record['candidate_id']}"))
            self.assertIn(record["context"], row)
            self.assertIn(record["market_scope"]["primary_channel"], row)
            rows.append(row.split(" | ", 1)[1])
        self.assertEqual(len(rows), len(set(rows)))

    def test_rejects_execution_cost_from_another_run(self) -> None:
        tiered = {"schema_version": "3.0", "run_id": "RUN-20260910-ABCDEF1234", "as_of": "2026-09-10"}
        paid = execution()
        paid.update(run_id="RUN-20260909-ABCDEF1234", as_of="2026-09-09")
        with self.assertRaises(DigestError):
            build_result_digest(tiered, executions=[paid])

    def test_execution_copies_are_not_double_counted(self) -> None:
        result = build_result_digest({}, executions=[execution(), execution()])
        self.assertEqual(result["metrics"]["paid_request_count"], 3)
        self.assertEqual(result["metrics"]["estimated_cost_usd"], 0.053)

    def test_negative_or_boolean_request_counts_are_rejected(self) -> None:
        for count in (-1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(DigestError):
                build_result_digest({}, executions=[{"summary": {"requests": count}}])

    def test_displays_all_qualified_and_overflow_candidates(self) -> None:
        tiered = example_tiered()
        result = build_result_digest(
            tiered,
            executions=[execution()],
            evidence_payloads=[evidence_payload()],
            research_payloads=[{"ranked_candidates": [{"id": 1}, {"id": 2}]}],
        )

        metrics = result["metrics"]
        self.assertEqual(metrics["benchmark_count"], 1)
        self.assertEqual(metrics["raw_candidate_count"], 133)
        self.assertEqual(metrics["deep_candidate_count"], 1)
        self.assertEqual(metrics["quick_idea_count"], 45)
        self.assertEqual(metrics["regional_signal_count"], 85)
        self.assertEqual(metrics["suggested_report_display_count"], 121)
        self.assertEqual(metrics["additional_conclusion_count"], 10)
        self.assertEqual(metrics["qualified_conclusion_count"], 131)
        self.assertEqual(metrics["paid_request_count"], 3)
        self.assertEqual(metrics["estimated_cost_usd"], 0.053)
        self.assertEqual(metrics["cost_per_qualified_conclusion_usd"], 0.000405)
        self.assertEqual(metrics["normalized_evidence_count"], 4)
        self.assertEqual(metrics["evidence_utilization_percent"], 50.0)
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
            build_result_digest({}, executions=[{"summary": {"estimated_attempted_cost_usd": -1}}])
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
