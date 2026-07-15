from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from expand_ideas import ExpansionError, expand_ideas  # noqa: E402
from filter_ideas import filter_ideas  # noqa: E402


def benchmark() -> dict:
    return {
        "product": "现有客服 SaaS",
        "source_market": "美国",
        "payer": "独立站商家",
        "price": "每月 49 美元",
        "job": "重复回复售前问题",
        "current_alternative": "人工客服与现有 SaaS",
        "product_gap": "价格高且不支持本地语言",
        "acquisition_channel": "Shopify 商家社区",
        "mvp_days": 21,
        "mvp_scope": "导入 FAQ 并自动生成一次回复",
        "payment_signals": [{"type": "subscription", "region": "美国", "url": "https://vendor.example/pricing"}],
        "demand_signals": [{"type": "complaint", "url": "https://forum.example/1"}],
        "evidence": [
            {"source": "vendor", "url": "https://vendor.example/pricing"},
            {"source": "forum", "url": "https://forum.example/1"},
        ],
    }


class IdeaFunnelTests(unittest.TestCase):
    def test_expands_across_six_axes_deterministically(self) -> None:
        payload = {
            "run_id": "RUN-20260715-ABCDEF1234",
            "as_of": "2026-07-15",
            "benchmarks": [benchmark()],
            "dimensions": {
                "segments": ["Shopify 独立站商家", "跨境电商客服团队"],
                "triggers": ["大促前 FAQ 激增", "夜间无人值守"],
                "forms": ["WhatsApp 内自动回复", "浏览器侧边栏回复"],
                "regions": [
                    {"country": "美国", "language": "英语"},
                    {
                        "country": "印度尼西亚",
                        "language": "印尼语",
                        "localization_gap": "印尼语和 WhatsApp 工作流",
                        "transfer_reason": "当地独立站增长但本地化客服供给不足",
                    },
                ],
                "channels": ["Shopify 商家社区", "WhatsApp 服务商渠道"],
                "offers": ["每月订阅", "按回复量计费"],
            },
        }
        first = expand_ideas(payload, limit=100)
        second = expand_ideas(payload, limit=100)

        self.assertEqual(first, second)
        self.assertEqual(first["summary"]["candidate_count"], 64)
        self.assertRegex(first["benchmarks"][0]["id"], r"^BENCH-[A-F0-9]{8}$")
        self.assertEqual(len({item["candidate_id"] for item in first["candidates"]}), 64)

    def test_expansion_requires_real_payment_signal(self) -> None:
        value = benchmark()
        value["payment_signals"] = []
        with self.assertRaisesRegex(ExpansionError, "payment_signals"):
            expand_ideas({"benchmarks": [value]})

    def test_filter_separates_a_b_r_and_rejected(self) -> None:
        base = expand_ideas({"benchmarks": [benchmark()]}, limit=1)["candidates"][0]
        tier_a = dict(base)
        tier_a["candidate_id"] = "A"
        tier_b = dict(base)
        tier_b["candidate_id"] = "B"
        tier_b["payment_signals"] = [{"type": "pricing", "region": "美国"}]
        tier_b["source_region"] = tier_b["target_region"] = "美国"
        tier_r = dict(base)
        tier_r["candidate_id"] = "R"
        tier_r["target_region"] = "印度尼西亚"
        tier_r["market_scope"] = {"country": "印度尼西亚", "region": "东南亚", "primary_channel": "WhatsApp"}
        tier_r["payment_signals"] = [{"type": "subscription", "region": "美国"}]
        rejected = dict(base)
        rejected["candidate_id"] = "X"
        rejected["payer"] = ""

        result = filter_ideas({"candidates": [tier_a, tier_b, tier_r, rejected]})

        self.assertEqual([item["candidate_id"] for item in result["deep_candidates"]], ["A"])
        self.assertEqual([item["candidate_id"] for item in result["validated_ideas"]], ["B"])
        self.assertEqual([item["candidate_id"] for item in result["regional_signals"]], ["R"])
        self.assertEqual(result["rejected"][0]["rejection_reasons"], ["clear_payer"])
        self.assertEqual(result["regional_signals"][0]["record_kind"], "signal")

    def test_six_hard_gates_block_formal_opportunity(self) -> None:
        candidate = expand_ideas({"benchmarks": [benchmark()]}, limit=1)["candidates"][0]
        for field, gate in (
            ("payment_signals", "paid_market"),
            ("payer", "clear_payer"),
            ("current_alternative", "current_alternative"),
            ("product_gap", "product_gap"),
            ("acquisition_channel", "acquisition_channel"),
            ("mvp_scope", "mvp_within_30_days"),
        ):
            with self.subTest(field=field):
                invalid = dict(candidate)
                invalid[field] = [] if field == "payment_signals" else ""
                result = filter_ideas({"candidates": [invalid]})
                self.assertIn(gate, result["rejected"][0]["rejection_reasons"])

    def test_caps_report_buckets_without_losing_overflow(self) -> None:
        base = expand_ideas({"benchmarks": [benchmark()]}, limit=1)["candidates"][0]
        quick: list[dict] = []
        regional: list[dict] = []
        for index in range(45):
            value = dict(base)
            value["candidate_id"] = f"B-{index}"
            value["payment_signals"] = [{"type": "pricing", "region": "美国"}]
            quick.append(value)
        for index in range(85):
            value = dict(base)
            value["candidate_id"] = f"R-{index}"
            value["target_region"] = "印度尼西亚"
            value["payment_signals"] = [{"type": "subscription", "region": "美国"}]
            regional.append(value)

        result = filter_ideas({"candidates": [*quick, *regional]})

        self.assertEqual(len(result["validated_ideas"]), 40)
        self.assertEqual(len(result["regional_signals"]), 80)
        self.assertEqual(len(result["overflow"]["validated_ideas"]), 5)
        self.assertEqual(len(result["overflow"]["regional_signals"]), 5)
        self.assertEqual(result["summary"]["tier_b_qualified"], 45)
        self.assertEqual(result["summary"]["tier_r_qualified"], 85)


if __name__ == "__main__":
    unittest.main()
