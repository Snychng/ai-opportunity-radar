from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from validate_report import validate_report  # noqa: E402
from tests.helpers import opportunity_block, valid_report  # noqa: E402


class ReportValidationTests(unittest.TestCase):
    def test_rejects_nonfinite_or_out_of_range_scores(self) -> None:
        for value in ("-1", "99", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                result = validate_report(valid_report().replace("证据置信度：7", f"证据置信度：{value}"))
                self.assertFalse(result.valid)

    def test_invalid_usd_is_reported_without_crashing(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity", "bad"):
            with self.subTest(value=value):
                result = validate_report(valid_report().replace("TikHub 预计费用 USD：0.053000", f"TikHub 预计费用 USD：{value}"))
                self.assertFalse(result.valid)

    def test_same_origin_urls_do_not_prove_independence(self) -> None:
        result = validate_report(valid_report().replace("https://community.example.org/thread/", "https://example.com/thread/"))
        self.assertFalse(result.valid)
        self.assertTrue(any("独立来源" in error for error in result.errors))

    def test_free_only_report_accepts_zero_paid_requests(self) -> None:
        report = valid_report().replace("TikHub 请求次数：17", "TikHub 请求次数：0")
        report = report.replace("0.053000", "0.000000").replace("0.3816", "0").replace("0.004000", "0.000000").replace("0.049000", "0.000000")
        report = report.replace("单个合格结论估算成本 USD：0.010600", "单个合格结论估算成本 USD：0.000000")
        self.assertTrue(validate_report(report).valid)

    def test_accepts_complete_report(self) -> None:
        result = validate_report(valid_report())
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.opportunity_count, 3)

    def test_requires_reason_when_fewer_than_three(self) -> None:
        invalid = validate_report(valid_report(2))
        valid = validate_report(valid_report(2, low_count_reason=True))
        self.assertFalse(invalid.valid)
        self.assertTrue(any("少于 3 个" in error for error in invalid.errors))
        self.assertTrue(valid.valid, valid.errors)

    def test_rejects_more_than_five_deep_opportunities(self) -> None:
        result = validate_report(valid_report(6))
        self.assertFalse(result.valid)
        self.assertTrue(any("最多 5 个" in error for error in result.errors))

    def test_requires_original_translation_and_source_url(self) -> None:
        content = valid_report().replace("- 来源：https://example.com/post/1", "- 来源：无").replace(
            "- 来源：https://community.example.org/thread/1", "- 来源：无"
        )
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertTrue(any("来源 URL" in error for error in result.errors))

    def test_single_source_requires_low_confidence(self) -> None:
        block = opportunity_block(1, include_second_source=False, confidence=7)
        content = valid_report(1, low_count_reason=True).replace(opportunity_block(1), block)
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertTrue(any("单来源" in error for error in result.errors))

        low_block = opportunity_block(1, include_second_source=False, confidence=4)
        low_content = valid_report(1, low_count_reason=True).replace(opportunity_block(1), low_block)
        low_result = validate_report(low_content)
        self.assertFalse(low_result.valid)
        self.assertTrue(any("至少提供两个独立来源" in error for error in low_result.errors))

    def test_rejects_disallowed_sensitive_domain(self) -> None:
        content = valid_report().replace("- 敏感领域：无", "- 敏感领域：医疗", 1)
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertTrue(any("敏感领域" in error for error in result.errors))

    def test_platform_requires_single_side_wedge(self) -> None:
        block = opportunity_block(1, platform="是").replace("#### 单边切入口", "#### 被删除的部分")
        content = valid_report(1, low_count_reason=True).replace(opportunity_block(1), block)
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertTrue(any("单边切入口" in error for error in result.errors))

    def test_high_competition_requires_new_form(self) -> None:
        block = opportunity_block(1, competition="高").replace("#### 老产品的新形态", "#### 被删除的新形态")
        content = valid_report(1, low_count_reason=True).replace(opportunity_block(1), block)
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertTrue(any("老产品的新形态" in error for error in result.errors))

    def test_reports_multiple_structural_errors_and_warning(self) -> None:
        content = valid_report().replace("## 四、今日升级与降级", "## 被删除的升级与降级", 1)
        content = content.replace("- 深度机会数量：3", "- 深度机会数量：2", 1)
        content = content.replace("- 数字化交付：是", "- 数字化交付：否", 1)
        content = content.replace("- 一个月 MVP：是", "- 一个月 MVP：否", 1)
        content = content.replace("- 数据获取：公开", "- 数据获取：未知", 1)
        content = content.replace("- 访问方式：搜索索引", "- 访问方式：神秘方式", 1)
        content = content.replace("- 证据置信度：7", "- 证据置信度：高", 1)
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertGreaterEqual(len(result.errors), 5)
        self.assertTrue(result.warnings)
        self.assertFalse(result.to_dict()["valid"])

    def test_requires_coverage_sections_and_declared_count(self) -> None:
        content = valid_report().replace("### 已覆盖", "### 覆盖被删除").replace("- 深度机会数量：3", "")
        result = validate_report(content)
        self.assertFalse(result.valid)
        self.assertTrue(any("数量声明" in error for error in result.errors))
        self.assertTrue(any("数据源覆盖" in error for error in result.errors))

    def test_requires_complete_result_link_and_cost_yield_section(self) -> None:
        missing_section = validate_report(valid_report().replace("## 六、费用产出", "## 被删除的费用产出", 1))
        self.assertFalse(missing_section.valid)
        self.assertTrue(any("费用产出" in error for error in missing_section.errors))

        missing_link = validate_report(
            valid_report().replace("- 完整结论清单：reports/daily/2026-07-14-full-results.md", "")
        )
        self.assertFalse(missing_link.valid)
        self.assertTrue(any("完整结论清单" in error for error in missing_link.errors))

    def test_validates_cost_yield_consistency(self) -> None:
        wrong_count = validate_report(valid_report().replace("- 合格结论数量：5", "- 合格结论数量：99", 1))
        self.assertFalse(wrong_count.valid)
        self.assertTrue(any("数量之和" in error for error in wrong_count.errors))

        wrong_displayed = validate_report(
            valid_report().replace("- 日报展示结论数量：5", "- 日报展示结论数量：99", 1)
        )
        self.assertFalse(wrong_displayed.valid)
        self.assertTrue(any("三层实际总数" in error for error in wrong_displayed.errors))

        with_overflow = valid_report().replace(
            "- 完整清单额外结论数量：0\n- 合格结论数量：5",
            "- 完整清单额外结论数量：5\n- 合格结论数量：10",
            1,
        ).replace("- 单个合格结论估算成本 USD：0.010600", "- 单个合格结论估算成本 USD：0.005300", 1)
        self.assertTrue(validate_report(with_overflow).valid)

        wrong_utilization = validate_report(valid_report().replace("- 证据利用率：50.00%", "- 证据利用率：90%", 1))
        self.assertFalse(wrong_utilization.valid)
        self.assertTrue(any("利用率" in error for error in wrong_utilization.errors))

        wrong_cost = validate_report(
            valid_report().replace("- 单个合格结论估算成本 USD：0.010600", "- 单个合格结论估算成本 USD：1", 1)
        )
        self.assertFalse(wrong_cost.valid)
        self.assertTrue(any("单个合格结论" in error for error in wrong_cost.errors))

        used_too_many = validate_report(valid_report().replace("- 已利用证据数量：5", "- 已利用证据数量：11", 1))
        self.assertFalse(used_too_many.valid)
        self.assertTrue(any("不能大于" in error for error in used_too_many.errors))

    def test_requires_quick_and_regional_evidence_contracts(self) -> None:
        quick = validate_report(valid_report().replace("- 付款者：小微企业主", "- 快速付款者被删除：小微企业主", 1))
        self.assertFalse(quick.valid)
        self.assertTrue(any("付款者" in error for error in quick.errors))

        regional = validate_report(valid_report().replace("- 缺失证据：当地直接付款与重复投诉", "- 区域缺口被删除：无", 1))
        self.assertFalse(regional.valid)
        self.assertTrue(any("缺失证据" in error for error in regional.errors))

    def test_quick_section_accepts_non_top_a_level_candidate(self) -> None:
        content = valid_report().replace("- 证据等级：B", "- 证据等级：A", 1)
        result = validate_report(content)
        self.assertTrue(result.valid, result.errors)

    def test_requires_tikhub_cost_section_and_all_fields(self) -> None:
        missing = validate_report(valid_report().replace("## 采集费用", "## 被删除的费用", 1))
        self.assertFalse(missing.valid)
        self.assertTrue(any("采集费用" in error for error in missing.errors))

        renamed = validate_report(valid_report().replace("## 采集费用", "## 采集费用说明", 1))
        self.assertFalse(renamed.valid)
        self.assertTrue(any("采集费用" in error for error in renamed.errors))

        fields = (
            ("TikHub 请求次数", "17"),
            ("TikHub 预计费用 USD", "0.053000"),
            ("TikHub 预计费用 RMB", "0.3816"),
            ("TikHub 免费额度适用成本 USD", "0.004000"),
            ("TikHub 免费额度不适用成本 USD", "0.049000"),
            ("TikHub 实际账单", "未执行；执行后以 TikHub 使用日志为准"),
        )
        for field, value in fields:
            with self.subTest(field=field):
                content = valid_report().replace(f"- {field}：{value}", f"- 被删除的字段：{value}", 1)
                result = validate_report(content)
                self.assertFalse(result.valid)
                self.assertTrue(any(field in error for error in result.errors), result.errors)

    def test_requires_positive_integer_tikhub_request_count(self) -> None:
        for invalid_value in ("0", "-1", "1.5", "不是数字"):
            with self.subTest(value=invalid_value):
                content = valid_report().replace(
                    "- TikHub 请求次数：17", f"- TikHub 请求次数：{invalid_value}", 1
                )
                result = validate_report(content)
                self.assertFalse(result.valid)
                self.assertTrue(any("TikHub 请求次数" in error for error in result.errors), result.errors)

    def test_requires_non_negative_finite_tikhub_amounts(self) -> None:
        cases = (
            ("TikHub 预计费用 USD", "0.053000", "-0.001"),
            ("TikHub 预计费用 RMB", "0.3816", "NaN"),
            ("TikHub 免费额度适用成本 USD", "0.004000", "Infinity"),
            ("TikHub 免费额度不适用成本 USD", "0.049000", "不是数字"),
        )
        for field, valid_value, invalid_value in cases:
            with self.subTest(field=field, value=invalid_value):
                content = valid_report().replace(f"- {field}：{valid_value}", f"- {field}：{invalid_value}", 1)
                result = validate_report(content)
                self.assertFalse(result.valid)
                self.assertTrue(any(field in error for error in result.errors), result.errors)

    def test_validates_tikhub_usd_breakdown_with_one_microdollar_tolerance(self) -> None:
        within_tolerance = valid_report().replace(
            "- TikHub 免费额度不适用成本 USD：0.049000",
            "- TikHub 免费额度不适用成本 USD：0.0490005",
            1,
        )
        self.assertTrue(validate_report(within_tolerance).valid)

        outside_tolerance = valid_report().replace(
            "- TikHub 免费额度不适用成本 USD：0.049000",
            "- TikHub 免费额度不适用成本 USD：0.049002",
            1,
        )
        result = validate_report(outside_tolerance)
        self.assertFalse(result.valid)
        self.assertTrue(any("费用拆分" in error for error in result.errors), result.errors)


if __name__ == "__main__":
    unittest.main()
