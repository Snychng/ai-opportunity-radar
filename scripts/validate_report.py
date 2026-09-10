#!/usr/bin/env python3
"""校验 AI 创业机会雷达 V3 三层 Markdown 日报结构。"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import Path

from contracts import evidence_independent_sources


REQUIRED_REPORT_SECTIONS = (
    "## 今日摘要",
    "## 数据源覆盖",
    "## 采集费用",
    "## 一、深度机会",
    "## 二、已验证快速点子",
    "## 三、区域迁移创意池",
    "## 四、今日升级与降级",
    "## 五、接近合格但被拒绝",
    "## 六、费用产出",
    "## 方法与局限",
)
REQUIRED_TIKHUB_COST_FIELDS = (
    "TikHub 请求次数",
    "TikHub 预计费用 USD",
    "TikHub 预计费用 RMB",
    "TikHub 免费额度适用成本 USD",
    "TikHub 免费额度不适用成本 USD",
    "TikHub 实际账单",
)
TIKHUB_AMOUNT_FIELDS = (
    "TikHub 预计费用 USD",
    "TikHub 预计费用 RMB",
    "TikHub 免费额度适用成本 USD",
    "TikHub 免费额度不适用成本 USD",
)
TIKHUB_COST_TOLERANCE_USD = Decimal("0.000001")
REQUIRED_YIELD_FIELDS = (
    "规范化证据数量",
    "聚类候选数量",
    "付费对标数量",
    "原始候选数量",
    "日报展示结论数量",
    "完整清单额外结论数量",
    "合格结论数量",
    "被拒绝候选数量",
    "已利用证据数量",
    "证据利用率",
    "单个合格结论估算成本 USD",
    "完整结论清单",
)
REQUIRED_DEEP_SECTIONS = (
    "#### 一句话产品",
    "#### 用户场景与具体触发时刻",
    "#### 原始证据",
    "#### 当前替代方案",
    "#### 需求规模与升温原因",
    "#### 现有竞品和集中差评",
    "#### 一个月 MVP",
    "#### 首笔收入路径",
    "#### 长期规模化路径",
    "#### 获客与自然传播机制",
    "#### 数据来源及产品化可持续性",
    "#### 最大反对理由",
    "#### 72 小时验证实验",
    "#### AI 判断",
)
REQUIRED_DEEP_FIELDS = (
    "证据等级", "付款者", "购买触发", "付费对标", "获客渠道",
    "类型", "综合分", "首笔收入潜力", "长期规模潜力", "个人影响力潜力", "证据置信度",
    "目标市场", "目标用户", "敏感领域", "数字化交付", "一个月 MVP", "平台型", "竞争强度", "数据获取",
)
REQUIRED_QUICK_FIELDS = (
    "证据等级", "付款者", "付费对标", "需求证据", "当前替代方案", "产品缺口", "获客渠道", "30 天 MVP",
)
REQUIRED_REGIONAL_FIELDS = (
    "证据等级", "来源市场", "付费对标", "目标地区", "可能付款者", "本地差异", "最小产品", "迁移理由", "缺失证据", "升级条件",
)
ALLOWED_SENSITIVE = {"无", "恋爱约会与情感陪伴", "成人内容", "游戏虚拟角色与社交娱乐", "恋爱", "成人", "游戏"}
ALLOWED_DATA_ACCESS = {"公开", "官方API", "官方 API", "授权登录", "人工验证", "混合", "未知"}
ALLOWED_ACCESS_METHODS = {"原生平台", "第三方API", "第三方 API", "搜索索引", "授权浏览器抽样", "人工核验"}
OPPORTUNITY_RE = re.compile(r"^### (OPP-\d{8}-[A-F0-9]{6})｜(.+)$", re.MULTILINE)
SIGNAL_RE = re.compile(r"^### (SIG-\d{8}-[A-F0-9]{6})｜(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]
    deep_opportunity_count: int
    quick_idea_count: int
    regional_signal_count: int

    @property
    def opportunity_count(self) -> int:
        """兼容 V2 调用方，返回深度机会数量。"""
        return self.deep_opportunity_count

    def to_dict(self) -> dict:
        value = asdict(self)
        value["opportunity_count"] = self.opportunity_count
        return value


def _field(block: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}：(.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def _report_section(content: str, heading: str) -> str | None:
    match = re.search(rf"^{re.escape(heading)}[ \t]*\r?$", content, re.MULTILINE)
    if not match:
        return None
    remainder = content[match.end():]
    next_heading = re.search(r"^## [^\r\n]+", remainder, re.MULTILINE)
    return remainder[:next_heading.start()] if next_heading else remainder


def _blocks(section: str | None, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    if section is None:
        return []
    matches = list(pattern.finditer(section))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        result.append((match.group(1), section[match.start():end]))
    return result


def _validate_costs(content: str, errors: list[str]) -> None:
    cost_section = _report_section(content, "## 采集费用")
    if cost_section is None:
        return
    cost_values = {field: _field(cost_section, field) for field in REQUIRED_TIKHUB_COST_FIELDS}
    for field, value in cost_values.items():
        if value is None:
            errors.append(f"采集费用缺少字段：{field}")

    request_count = cost_values["TikHub 请求次数"]
    if request_count is not None and not re.fullmatch(r"\d+", request_count):
        errors.append("TikHub 请求次数必须是非负整数")

    parsed: dict[str, Decimal] = {}
    for field in TIKHUB_AMOUNT_FIELDS:
        raw = cost_values[field]
        if raw is None:
            continue
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            amount = None
        if amount is None or not amount.is_finite() or amount < 0:
            errors.append(f"{field} 必须是非负有限数字")
        else:
            parsed[field] = amount
    if request_count == "0" and any(amount > 0 for amount in parsed.values()):
        errors.append("TikHub 请求次数为 0 时，采集费用必须为 0")
    breakdown = (
        "TikHub 预计费用 USD",
        "TikHub 免费额度适用成本 USD",
        "TikHub 免费额度不适用成本 USD",
    )
    if all(field in parsed for field in breakdown):
        try:
            difference = abs(parsed[breakdown[1]] + parsed[breakdown[2]] - parsed[breakdown[0]])
        except DecimalException:
            errors.append("TikHub 费用拆分必须使用可计算的有限数字")
        else:
            if difference > TIKHUB_COST_TOLERANCE_USD:
                errors.append("TikHub 费用拆分不一致：免费额度适用成本与不适用成本之和必须等于预计费用 USD")


def _validate_declared_count(
    content: str,
    *,
    label: str,
    actual: int,
    maximum: int,
    minimum_target: int,
    shortage_label: str,
    errors: list[str],
) -> None:
    match = re.search(rf"^- {re.escape(label)}：(\d+)$", content, re.MULTILINE)
    if not match:
        errors.append(f"缺少{label}声明")
    elif int(match.group(1)) != actual:
        errors.append(f"{label}声明与实际不一致：声明 {match.group(1)}，实际 {actual}")
    if actual > maximum:
        errors.append(f"{label}最多 {maximum} 个，当前 {actual} 个")
    if actual < minimum_target and not re.search(rf"^- {re.escape(shortage_label)}：\S.+$", content, re.MULTILINE):
        errors.append(f"{label}少于 {minimum_target} 个时必须填写“{shortage_label}”且不得用弱证据凑数")


def _nonnegative_int(section: str, field: str, errors: list[str]) -> int | None:
    value = _field(section, field)
    if value is None:
        return None
    if not re.fullmatch(r"\d+", value):
        errors.append(f"费用产出字段“{field}”必须是非负整数")
        return None
    return int(value)


def _validate_yield(
    content: str,
    *,
    qualified_count: int,
    expected_cost: Decimal | None,
    errors: list[str],
) -> None:
    section = _report_section(content, "## 六、费用产出")
    if section is None:
        return
    for field in REQUIRED_YIELD_FIELDS:
        if _field(section, field) is None:
            errors.append(f"费用产出缺少字段：{field}")
    integers = {
        field: _nonnegative_int(section, field, errors)
        for field in (
            "规范化证据数量",
            "聚类候选数量",
            "付费对标数量",
            "原始候选数量",
            "日报展示结论数量",
            "完整清单额外结论数量",
            "合格结论数量",
            "被拒绝候选数量",
            "已利用证据数量",
        )
    }
    displayed_qualified = integers["日报展示结论数量"]
    additional_qualified = integers["完整清单额外结论数量"]
    declared_qualified = integers["合格结论数量"]
    if displayed_qualified is not None and displayed_qualified != qualified_count:
        errors.append(f"日报展示结论数量与三层实际总数不一致：声明 {displayed_qualified}，实际 {qualified_count}")
    if (
        declared_qualified is not None
        and displayed_qualified is not None
        and additional_qualified is not None
        and declared_qualified != displayed_qualified + additional_qualified
    ):
        errors.append("合格结论数量必须等于日报展示结论数量与完整清单额外结论数量之和")
    normalized = integers["规范化证据数量"]
    used = integers["已利用证据数量"]
    if normalized is not None and used is not None and used > normalized:
        errors.append("已利用证据数量不能大于规范化证据数量")

    utilization_value = _field(section, "证据利用率")
    if utilization_value is not None:
        if utilization_value == "未知":
            if normalized not in {None, 0}:
                errors.append("存在规范化证据时，证据利用率不能写“未知”")
        else:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)%", utilization_value)
            if not match or Decimal(match.group(1)) > 100:
                errors.append("证据利用率必须是 0% 到 100% 的百分比或“未知”")
            elif normalized and used is not None:
                expected_rate = Decimal(used) / Decimal(normalized) * 100
                if abs(Decimal(match.group(1)) - expected_rate) > Decimal("0.01"):
                    errors.append("证据利用率与已利用/规范化证据数量不一致")

    cost_value = _field(section, "单个合格结论估算成本 USD")
    if cost_value is not None and cost_value != "未知":
        try:
            cost_per = Decimal(cost_value)
        except InvalidOperation:
            cost_per = None
        if cost_per is None or not cost_per.is_finite() or cost_per < 0:
            errors.append("单个合格结论估算成本 USD 必须是非负有限数字或“未知”")
        elif expected_cost is not None and declared_qualified is not None and declared_qualified > 0:
            expected_per = expected_cost / declared_qualified
            if abs(cost_per - expected_per) > TIKHUB_COST_TOLERANCE_USD:
                errors.append("单个合格结论估算成本与预计费用/合格结论数量不一致")


def _validate_deep(blocks: list[tuple[str, str]], errors: list[str], warnings: list[str]) -> None:
    for opportunity_id, block in blocks:
        for section in REQUIRED_DEEP_SECTIONS:
            if section not in block:
                errors.append(f"{opportunity_id} 缺少章节：{section}")
        for field in REQUIRED_DEEP_FIELDS:
            if _field(block, field) is None:
                errors.append(f"{opportunity_id} 缺少字段：{field}")
        if _field(block, "证据等级") != "A":
            errors.append(f"{opportunity_id} 深度机会必须是 A 级证据")
        sensitive = _field(block, "敏感领域")
        if sensitive is not None and sensitive not in ALLOWED_SENSITIVE:
            errors.append(f"{opportunity_id} 使用了不允许的敏感领域：{sensitive}")
        if _field(block, "数字化交付") != "是":
            errors.append(f"{opportunity_id} 必须确认数字化交付为“是”")
        if _field(block, "一个月 MVP") != "是":
            errors.append(f"{opportunity_id} 必须确认一个月 MVP 为“是”")
        data_access = _field(block, "数据获取")
        if data_access is not None and data_access not in ALLOWED_DATA_ACCESS:
            errors.append(f"{opportunity_id} 数据获取类型不受支持：{data_access}")

        for label, pattern in (
            ("原文证据", r"^- 原文：\S.+$"),
            ("中文翻译", r"^- 中文翻译：\S.+$"),
            ("发布时间", r"^- 发布时间：\S.+$"),
            ("日期置信度", r"^- 日期置信度：(高|中|低)$"),
            ("采集时间", r"^- 采集时间：\S.+$"),
            ("证据语言", r"^- 语言：\S.+$"),
        ):
            if not re.search(pattern, block, re.MULTILINE):
                errors.append(f"{opportunity_id} 缺少{label}")
        source_urls = re.findall(r"^- 来源：(https?://\S+)$", block, re.MULTILINE)
        if not source_urls:
            errors.append(f"{opportunity_id} 缺少有效来源 URL")
        elif len(evidence_independent_sources([{"url": url} for url in source_urls])) < 2:
            errors.append(f"{opportunity_id} 是 A 级深度机会，必须至少提供两个独立来源")
        access_methods = re.findall(r"^- 访问方式：(.+)$", block, re.MULTILINE)
        if not access_methods:
            errors.append(f"{opportunity_id} 缺少证据访问方式")
        for method in access_methods:
            if method.strip() not in ALLOWED_ACCESS_METHODS:
                errors.append(f"{opportunity_id} 使用了未知访问方式：{method.strip()}")
        values: dict[str, float] = {}
        for field, maximum in (("综合分", 100), ("首笔收入潜力", 10), ("长期规模潜力", 10), ("个人影响力潜力", 10), ("证据置信度", 10)):
            raw = _field(block, field)
            try:
                value = float(raw) if raw is not None else None
            except ValueError:
                value = None
            if value is None or not math.isfinite(value) or not 0 <= value <= maximum:
                errors.append(f"{opportunity_id} 的{field}必须是 0 到 {maximum} 的有限数字")
            else:
                values[field] = value
        confidence = values.get("证据置信度")
        if len(evidence_independent_sources([{"url": url} for url in source_urls])) == 1 and confidence is not None and confidence > 4:
            errors.append(f"{opportunity_id} 只有单来源证据，证据置信度不得高于 4")
        if _field(block, "平台型") == "是" and "#### 单边切入口" not in block:
            errors.append(f"{opportunity_id} 是平台型机会，必须填写单边切入口")
        if _field(block, "竞争强度") == "高" and "#### 老产品的新形态" not in block:
            errors.append(f"{opportunity_id} 竞争强度高，必须说明老产品的新形态")
        if data_access == "未知":
            warnings.append(f"{opportunity_id} 的产品化数据获取仍需验证")


def _validate_cards(
    blocks: list[tuple[str, str]],
    *,
    required_fields: tuple[str, ...],
    expected_tiers: set[str],
    errors: list[str],
) -> None:
    for record_id, block in blocks:
        for field in required_fields:
            if _field(block, field) is None:
                errors.append(f"{record_id} 缺少字段：{field}")
        if _field(block, "证据等级") not in expected_tiers:
            allowed = "/".join(sorted(expected_tiers))
            errors.append(f"{record_id} 必须标记为 {allowed} 级证据")


def validate_report(content: str) -> ValidationResult:
    """校验报告可审计结构，不替代市场真实性与法律核验。"""
    errors: list[str] = []
    warnings: list[str] = []
    for section in REQUIRED_REPORT_SECTIONS:
        if _report_section(content, section) is None:
            errors.append(f"缺少日报章节：{section}")
    _validate_costs(content, errors)

    deep = _blocks(_report_section(content, "## 一、深度机会"), OPPORTUNITY_RE)
    quick = _blocks(_report_section(content, "## 二、已验证快速点子"), OPPORTUNITY_RE)
    regional = _blocks(_report_section(content, "## 三、区域迁移创意池"), SIGNAL_RE)
    _validate_declared_count(
        content, label="深度机会数量", actual=len(deep), maximum=5, minimum_target=3,
        shortage_label="深度机会不足 3 个的原因", errors=errors,
    )
    _validate_declared_count(
        content, label="已验证快速点子数量", actual=len(quick), maximum=40, minimum_target=20,
        shortage_label="快速点子不足 20 个的原因", errors=errors,
    )
    _validate_declared_count(
        content, label="区域迁移创意数量", actual=len(regional), maximum=80, minimum_target=30,
        shortage_label="区域创意不足 30 个的原因", errors=errors,
    )
    _validate_deep(deep, errors, warnings)
    _validate_cards(quick, required_fields=REQUIRED_QUICK_FIELDS, expected_tiers={"A", "B"}, errors=errors)
    _validate_cards(regional, required_fields=REQUIRED_REGIONAL_FIELDS, expected_tiers={"R"}, errors=errors)
    cost_section = _report_section(content, "## 采集费用")
    expected_cost: Decimal | None = None
    if cost_section is not None:
        raw_cost = _field(cost_section, "TikHub 预计费用 USD")
        try:
            expected_cost = Decimal(raw_cost) if raw_cost is not None else None
        except InvalidOperation:
            expected_cost = None
        if expected_cost is not None and (not expected_cost.is_finite() or expected_cost < 0):
            expected_cost = None
    _validate_yield(
        content,
        qualified_count=len(deep) + len(quick) + len(regional),
        expected_cost=expected_cost,
        errors=errors,
    )

    if "### 已覆盖" not in content or "### 跳过或失败" not in content:
        errors.append("数据源覆盖必须同时列出“已覆盖”和“跳过或失败”")
    ids = [record_id for record_id, _ in [*deep, *quick, *regional]]
    if len(ids) != len(set(ids)):
        errors.append("日报中存在重复 OPP/SIG ID")
    return ValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        deep_opportunity_count=len(deep),
        quick_idea_count=len(quick),
        regional_signal_count=len(regional),
    )


def main() -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="校验 AOR 结构化报告或兼容 Markdown 日报")
    parser.add_argument("report", type=Path)
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--commit", action="store_true", help="校验结构化报告后提交本地研究状态")
    parser.add_argument("--home", type=Path, help="研究状态数据目录")
    args = parser.parse_args()
    try:
        content = args.report.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(str(exc))
    if args.report.suffix.lower() == ".json":
        import aor_bootstrap  # noqa: F401
        from aor.reporting.report import commit_report, validate_structured_report
        from manage_state import DEFAULT_HOME

        try:
            report = json.loads(content)
            result_data = validate_structured_report(report)
            if args.commit and result_data["valid"]:
                result_data["commit"] = commit_report(args.home or DEFAULT_HOME, report)
            print(json.dumps(result_data, ensure_ascii=False, indent=2))
            return 0 if result_data["valid"] else 1
        except (ValueError, OSError) as exc:
            print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
            return 1
    if args.commit:
        parser.error("正式提交使用结构化 report.json；Markdown 仅校验兼容格式")
    result = validate_report(content)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("VALID" if result.valid else "INVALID")
        for error in result.errors:
            print(f"ERROR: {error}")
        for warning in result.warnings:
            print(f"WARN: {warning}")
    return 0 if result.valid else 1


if __name__ == "__main__":  # pragma: no cover
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
