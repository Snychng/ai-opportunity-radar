from __future__ import annotations

import sys
from copy import deepcopy
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from expand_ideas import ExpansionError, expand_ideas  # noqa: E402
from filter_ideas import (FilterError, classify_candidate, filter_ideas, qualifying_evidence,
                         resolve_candidate_evidence)
from contracts import fingerprint_record  # noqa: E402


def benchmark() -> dict:
    return {
        "product": "现有客服 SaaS",
        "source_market": "美国",
        "payer": "独立站商家",
        "price": "每月 49 美元",
        "job": "重复回复售前问题",
        "wedge": "自动生成 FAQ 回复",
        "delivery_model": "每月订阅",
        "current_alternative": "人工客服与现有 SaaS",
        "product_gap": "价格高且不支持本地语言",
        "acquisition_channel": "Shopify 商家社区",
        "mvp_days": 21,
        "mvp_scope": "导入 FAQ 并自动生成一次回复",
        "payment_signals": [{"type": "purchase", "region": "美国", "payer": "独立站商家", "url": "https://vendor.example/receipt", "fact": "商家已支付49美元购买客服服务"}],
        "demand_signals": [{"type": "complaint", "url": "https://forum.example/1", "fact": "商家抱怨每天耗费两小时重复回复问题"}],
        "evidence": [
            {"source": "vendor", "url": "https://vendor.example/receipt", "fact": "商家已支付49美元购买客服服务"},
            {"source": "forum", "url": "https://forum.example/1", "fact": "商家抱怨每天耗费两小时重复回复问题"},
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
        tier_b["context"] = "夜间客服任务"
        tier_b["payment_signals"] = [{"type": "pricing", "region": "美国", "url": "https://vendor.example/pricing", "fact": "定价页面展示每月49美元"}]
        tier_b["source_region"] = tier_b["target_region"] = "美国"
        tier_r = dict(base)
        tier_r["candidate_id"] = "R"
        tier_r["transfer_reason"] = "目标地区存在同类任务，需验证当地付款"
        tier_r["target_region"] = "印度尼西亚"
        tier_r["market_scope"] = {"country": "印度尼西亚", "region": "东南亚", "primary_channel": "WhatsApp"}
        tier_r["payment_signals"] = [{"type": "purchase", "region": "美国", "payer": "独立站商家", "url": "https://vendor.example/receipt", "fact": "商家已支付49美元购买服务"}]
        rejected = dict(base)
        rejected["candidate_id"] = "X"
        rejected["context"] = "无付款者的任务"
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

    def test_unknown_fields_remain_unknown_and_fail_hard_gates(self):
        value = benchmark()
        for field in ("product_gap", "acquisition_channel", "mvp_days", "mvp_scope"):
            value.pop(field)
        expanded = expand_ideas({"benchmarks": [value]})
        item = expanded["candidates"][0]
        for field in ("product_gap", "acquisition_channel", "mvp_days", "mvp_scope"):
            self.assertIsNone(item[field])
        self.assertFalse(filter_ideas(expanded)["deep_candidates"])

    def test_foreign_purchase_and_local_pricing_do_not_prove_local_payment(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item["target_region"] = "印度尼西亚"
        item["transfer_reason"] = "目标地区存在类似任务"
        item["payment_signals"].append({"type": "pricing", "region": "印度尼西亚",
                                        "url": "https://vendor.example/id", "fact": "当地展示定价"})
        self.assertEqual(classify_candidate(item)[0], "R")

    def test_local_flags_and_region_fallback_cannot_replace_transaction_fact(self):
        for signal in ({"type": "purchase", "local": True},
                       {"type": "purchase", "region": "美国", "payer": "商家", "url": "https://vendor.example/missing"}):
            item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
            item["payment_signals"] = [signal]
            self.assertIsNone(classify_candidate(item)[0])

    def test_duplicate_dimensions_are_collapsed_before_scanning(self):
        dimensions = {key: [value] * 10000 for key, value in {
            "segments": "商家", "triggers": "夜间咨询", "forms": "FAQ回复", "regions": "美国",
            "channels": "商家社区", "offers": "订阅"}.items()}
        result = expand_ideas({"benchmarks": [benchmark()], "dimensions": dimensions})
        self.assertEqual(result["summary"]["candidate_count"], 1)
        self.assertEqual(result["summary"]["combinations_scanned"], 1)
        self.assertFalse(result["summary"]["truncated"])

    def test_expansion_round_robins_benchmarks(self):
        values = []
        for index in range(10):
            item = benchmark()
            item["product"] = f"对标{index}"
            values.append(item)
        result = expand_ideas({"benchmarks": values, "dimensions": {"offers": [f"报价{i}" for i in range(20)]}}, limit=20)
        self.assertEqual(len({item["benchmark_ids"][0] for item in result["candidates"]}), 10)
        self.assertLessEqual(result["summary"]["combinations_scanned"], 20)

    def test_offer_variants_form_one_family_and_preserve_details(self):
        expanded = expand_ideas({"benchmarks": [benchmark()], "dimensions": {"offers": ["订阅", "按次", "人工审核"]}})
        result = filter_ideas(expanded)
        self.assertEqual(len(result["deep_candidates"]), 1)
        family = result["deep_candidates"][0]
        self.assertEqual(len(family["variants"]), 3)
        self.assertEqual({item["delivery_model"] for item in family["variants"]}, {"订阅", "按次", "人工审核"})
        self.assertTrue(all(item["variant_id"] and item["evidence"] for item in family["variants"]))
        self.assertEqual(family["opportunity_family"], fingerprint_record(family))
        self.assertEqual(result["summary"]["raw"], 3)
        self.assertEqual(result["summary"]["families"], 1)

    def test_new_segment_and_form_need_specific_verification_to_be_a(self):
        result = expand_ideas({"benchmarks": [benchmark()], "dimensions": {
            "segments": ["律师"], "forms": ["庭审语音转录"]}})
        item = result["candidates"][0]
        self.assertIn("target_user", item["hypotheses"])
        self.assertIn("wedge", item["hypotheses"])
        self.assertNotEqual(classify_candidate(item)[0], "A")
        item["candidate_verifications"] = {field: {"value": item[field], "evidence": [
            {"url": "https://lawyers.example/interview", "fact": "律师确认购买庭审语音转录并使用此流程"}]}
            for field in item["hypotheses"]}
        item["evidence"].append({"url": "https://lawyers.example/interview", "fact": "律师确认购买庭审语音转录并使用此流程"})
        self.assertEqual(classify_candidate(item)[0], "A")

    def test_expansion_and_filter_reject_invalid_envelopes(self):
        for envelope in ({"schema_version": "2.0"}, {"run_id": "bad", "as_of": "yesterday"}, {"run_id": None}):
            with self.subTest(envelope=envelope):
                with self.assertRaises(ExpansionError):
                    expand_ideas({"benchmarks": [benchmark()], **envelope})
                with self.assertRaises(FilterError):
                    filter_ideas({"candidates": [], **envelope})

    def test_placeholder_and_invalid_mvp_values_cannot_pass(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        for field, value in (("product_gap", "待验证"), ("acquisition_channel", "unknown"),
                             ("mvp_days", True), ("mvp_days", 2.8), ("mvp_days", float("inf"))):
            invalid = deepcopy(item)
            invalid[field] = value
            self.assertIsNone(classify_candidate(invalid)[0])

    def test_scan_budget_stops_duplicate_benchmark_expansion(self):
        from unittest.mock import patch
        with patch("expand_ideas.MAX_COMBINATION_SCANS", 7):
            result = expand_ideas({"benchmarks": [benchmark()] * 10}, limit=20)
        self.assertEqual(result["summary"]["combinations_scanned"], 7)
        self.assertEqual(result["summary"]["candidate_count"], 1)
        self.assertTrue(result["summary"]["truncated"])

    def test_retracted_payment_and_demonstration_never_qualify_a(self):
        for mutation in ({"retracted": True}, {"status": "retracted"}, {"is_demo": True}):
            item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
            item["payment_signals"][0].update(mutation)
            self.assertNotEqual(classify_candidate(item)[0], "A")

    def test_invalid_benchmark_ids_are_rejected_without_aggregation_crash(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item["benchmark_ids"] = None
        result = filter_ideas({"candidates": [item]})
        self.assertIn("paid_market", result["rejected"][0]["rejection_reasons"])

    def test_candidate_run_must_match_parent_envelope(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item.update(run_id="RUN-20260909-ABCDEF1234", as_of="2026-09-09")
        with self.assertRaises(FilterError):
            filter_ideas({"run_id": "RUN-20260910-ABCDEF1234", "as_of": "2026-09-10", "candidates": [item]})

    def test_truthy_objects_cannot_stand_in_for_factual_fields(self):
        for field in ("payer", "current_alternative", "product_gap", "acquisition_channel", "mvp_scope"):
            item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
            item[field] = {"unverified": "placeholder"}
            with self.subTest(field=field):
                self.assertIsNone(classify_candidate(item)[0])
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item["payment_signals"][0]["fact"] = {"unverified": True}
        self.assertIsNone(classify_candidate(item)[0])

    def test_partial_verification_does_not_promote_new_segments(self):
        item = expand_ideas({"benchmarks": [benchmark()], "dimensions": {"segments": ["律师"]}})["candidates"][0]
        item["candidate_verifications"] = {"target_user": {"value": "商家", "evidence": [
            {"url": "https://lawyers.example/1", "fact": "商家已购买"}]}}
        self.assertNotEqual(classify_candidate(item)[0], "A")
        item["candidate_verifications"]["target_user"]["value"] = "律师"
        item["candidate_verifications"]["target_user"]["evidence"][0]["status"] = "retracted"
        self.assertNotEqual(classify_candidate(item)[0], "A")

    def test_family_batch_prepares_stable_ids_without_offer_collisions(self):
        import tempfile
        from datetime import date
        from manage_state import resolve_record_ids
        tiered = filter_ideas(expand_ideas({"benchmarks": [benchmark()], "dimensions": {
            "offers": ["每月订阅", "按次付费", "工具加人工审核"]}}))
        with tempfile.TemporaryDirectory() as tmp:
            prepared = resolve_record_ids(Path(tmp), "opportunity", tiered["deep_candidates"], date(2026, 9, 10))
            repeated = resolve_record_ids(Path(tmp), "opportunity", tiered["deep_candidates"], date(2026, 9, 10))
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["id"], repeated[0]["id"])
        self.assertEqual(len(prepared[0]["variants"]), 3)

    def test_expansion_rejects_nontext_identity_and_malformed_dimensions(self):
        for field in ("product", "payer", "source_market"):
            value = benchmark()
            value[field] = {"label": "未核实"}
            with self.subTest(field=field), self.assertRaises(ExpansionError):
                expand_ideas({"benchmarks": [value]})
        for dimensions in ({"segments": [42]}, {"regions": [False]}, {"forms": []}, []):
            with self.subTest(dimensions=dimensions), self.assertRaises(ExpansionError):
                expand_ideas({"benchmarks": [benchmark()], "dimensions": dimensions})

    def test_unrelated_links_and_detached_receipt_cannot_supply_a(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item["evidence"] = [{"url": "https://one.example/post"}, {"url": "https://two.example/post"}]
        self.assertNotEqual(classify_candidate(item)[0], "A")
        item["evidence"] = [{"url": "https://one.example/post", "fact": "一条支持事实"},
                            {"url": "https://two.example/post", "original_text": "另一条支持事实"}]
        self.assertNotEqual(classify_candidate(item)[0], "A")

    def test_reference_only_payment_resolves_standard_original_text(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item["evidence"] = [
            {"evidence_id": "receipt", "url": "https://vendor.example/receipt", "original_text": "商家已支付49美元"},
            {"evidence_id": "review", "url": "https://forum.example/1", "original_text": "商家抱怨配置繁琐"},
        ]
        item["payment_signals"] = [{"type": "purchase", "region": "美国", "payer": "独立站商家", "evidence_id": "receipt"}]
        self.assertEqual(classify_candidate(item)[0], "A")
        item["evidence"][0]["status"] = "retracted"
        self.assertNotEqual(classify_candidate(item)[0], "A")

    def test_verification_must_reference_active_candidate_evidence(self):
        item = expand_ideas({"benchmarks": [benchmark()], "dimensions": {"segments": ["律师"]}})["candidates"][0]
        proof = {"evidence_id": "interview", "url": "https://lawyers.example/interview", "original_text": "律师确认购买需求"}
        item["candidate_verifications"] = {"target_user": {"value": "律师", "evidence": [proof]}}
        self.assertNotEqual(classify_candidate(item)[0], "A")
        item["evidence"].append(deepcopy(proof))
        self.assertEqual(classify_candidate(item)[0], "A")
        item["evidence"][-1]["retracted"] = True
        self.assertNotEqual(classify_candidate(item)[0], "A")

    def test_receipt_reference_requires_consistent_id_url_and_revision(self):
        item = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        item["evidence"][0] = {"evidence_id": "receipt", "url": "https://vendor.example/receipt",
                               "original_text": "商家已支付49美元", "version": 2, "revision_id": "receipt:v2"}
        signal = item["payment_signals"][0]
        for updates in ({"evidence_id": "missing"}, {"evidence_id": "receipt", "evidence_revision_id": "receipt:v1"},
                        {"evidence_id": "receipt", "evidence_revision_id": None}):
            signal.update(updates)
            self.assertNotEqual(classify_candidate(item)[0], "A")
        signal.update(evidence_id="receipt", evidence_revision_id="receipt:v2", fact="商家已支付49美元")
        self.assertEqual(classify_candidate(item)[0], "A")

    def test_evidence_resolver_handles_reference_boundaries(self):
        candidate = {"evidence": [
            {"evidence_id": "receipt", "url": "https://vendor.example/receipt", "original_text": "用户已支付"},
            {"url": "https://bare.example/1"},
            {"url": "https://demo.example/1", "fact": "演示", "is_demo": True},
            {"url": "https://old.example/1", "fact": "旧证据", "status": "retracted"},
        ]}
        self.assertEqual(len(qualifying_evidence(candidate)), 1)
        self.assertEqual(qualifying_evidence({"evidence": None}), [])
        resolved = resolve_candidate_evidence(candidate, {"url": "http://www.vendor.example/receipt/?utm_source=test#reply"})
        self.assertEqual(resolved["evidence_id"], "receipt")
        for reference in (None, {}, {"evidence_id": "missing"}, {"url": "not-a-url"},
                          {"evidence_id": "receipt", "url": "https://other.example/receipt"},
                          {"evidence_id": "receipt", "url": "broken"},
                          {"evidence_id": "receipt", "fact": {"unknown": True}},
                          {"evidence_id": "receipt", "status": "retracted"},
                          {"evidence_id": "receipt", "is_demo": True},
                          {"evidence_id": "receipt", "evidence_revision_id": "receipt:v2"}):
            with self.subTest(reference=reference):
                self.assertIsNone(resolve_candidate_evidence(candidate, reference))

    def test_revision_binding_does_not_accept_old_positive_text(self):
        candidate = {"evidence": [{"evidence_id": "receipt", "url": "https://vendor.example/receipt",
                                  "original_text": "客户并未付款", "version": 2, "revision_id": "receipt:v2"}]}
        reference = {"evidence_id": "receipt", "evidence_revision_id": "receipt:v2", "fact": "客户已付款"}
        self.assertIsNone(resolve_candidate_evidence(candidate, reference))
        reference["fact"] = "客户并未付款"
        self.assertEqual(resolve_candidate_evidence(candidate, reference)["original_text"], "客户并未付款")

    def test_caps_report_buckets_without_losing_overflow(self) -> None:
        base = expand_ideas({"benchmarks": [benchmark()]}, limit=1)["candidates"][0]
        quick: list[dict] = []
        regional: list[dict] = []
        for index in range(45):
            value = dict(base)
            value["candidate_id"] = f"B-{index}"
            value["context"] = f"任务{index}"
            value["payment_signals"] = [{"type": "pricing", "region": "美国", "url": "https://vendor.example/pricing", "fact": "定价页面展示每月49美元"}]
            quick.append(value)
        for index in range(85):
            value = dict(base)
            value["candidate_id"] = f"R-{index}"
            value["context"] = f"地区任务{index}"
            value["market_scope"] = {"country": "印度尼西亚"}
            value["transfer_reason"] = "目标地区存在同类任务，需验证当地付款"
            value["target_region"] = "印度尼西亚"
            value["payment_signals"] = [{"type": "purchase", "region": "美国", "payer": "独立站商家", "url": "https://vendor.example/receipt", "fact": "商家已支付49美元购买服务"}]
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
