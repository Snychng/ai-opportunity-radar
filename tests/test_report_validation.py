from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from validate_report import validate_report  # noqa: E402
from tests.helpers import opportunity_block, valid_report  # noqa: E402


class ReportValidationTests(unittest.TestCase):
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
