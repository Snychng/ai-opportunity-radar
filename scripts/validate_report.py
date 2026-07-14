#!/usr/bin/env python3
"""校验本地 Markdown 日报是否满足 V1 结构契约。"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import Path


REQUIRED_REPORT_SECTIONS = (
    "## 今日结论",
    "## 数据源覆盖",
    "## 采集费用",
    "## 综合推荐排名",
    "## 分类视图",
    "## 深度机会",
    "## 早期观察池",
    "## 历史变化",
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
REQUIRED_OPPORTUNITY_SECTIONS = (
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
ALLOWED_SENSITIVE = {"无", "恋爱约会与情感陪伴", "成人内容", "游戏虚拟角色与社交娱乐", "恋爱", "成人", "游戏"}
ALLOWED_DATA_ACCESS = {"公开", "官方API", "官方 API", "授权登录", "人工验证", "混合", "未知"}
ALLOWED_ACCESS_METHODS = {"原生平台", "第三方API", "第三方 API", "搜索索引", "授权浏览器抽样", "人工核验"}
OPPORTUNITY_RE = re.compile(r"^### (OPP-\d{8}-[A-F0-9]{6})｜(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]
    opportunity_count: int

    def to_dict(self) -> dict:
        return asdict(self)


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


def _opportunity_blocks(content: str) -> list[tuple[str, str]]:
    matches = list(OPPORTUNITY_RE.finditer(content))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        blocks.append((match.group(1), content[match.start():end]))
    return blocks


def validate_report(content: str) -> ValidationResult:
    """结构校验，不宣称验证市场真实性或法律结论。"""
    errors: list[str] = []
    warnings: list[str] = []
    for section in REQUIRED_REPORT_SECTIONS:
        if section not in content:
            errors.append(f"缺少日报章节：{section}")

    cost_section = _report_section(content, "## 采集费用")
    if cost_section is None:
        if "## 采集费用" in content:
            errors.append("缺少精确日报章节：## 采集费用")
    else:
        cost_values = {field: _field(cost_section, field) for field in REQUIRED_TIKHUB_COST_FIELDS}
        for field, value in cost_values.items():
            if value is None:
                errors.append(f"采集费用缺少字段：{field}")

        request_count = cost_values["TikHub 请求次数"]
        if request_count is not None and (
            not re.fullmatch(r"\d+", request_count) or int(request_count) <= 0
        ):
            errors.append("TikHub 请求次数必须是正整数")

        parsed_amounts: dict[str, Decimal] = {}
        for field in TIKHUB_AMOUNT_FIELDS:
            raw_value = cost_values[field]
            if raw_value is None:
                continue
            try:
                amount = Decimal(raw_value)
            except InvalidOperation:
                amount = None
            if amount is None or not amount.is_finite() or amount < 0:
                errors.append(f"{field} 必须是非负有限数字")
            else:
                parsed_amounts[field] = amount

        usd_breakdown_fields = (
            "TikHub 预计费用 USD",
            "TikHub 免费额度适用成本 USD",
            "TikHub 免费额度不适用成本 USD",
        )
        if all(field in parsed_amounts for field in usd_breakdown_fields):
            expected = parsed_amounts["TikHub 预计费用 USD"]
            applicable = parsed_amounts["TikHub 免费额度适用成本 USD"]
            inapplicable = parsed_amounts["TikHub 免费额度不适用成本 USD"]
            try:
                difference = abs(applicable + inapplicable - expected)
            except DecimalException:
                errors.append("TikHub 费用拆分必须使用可计算的有限数字")
            else:
                if difference > TIKHUB_COST_TOLERANCE_USD:
                    errors.append(
                        "TikHub 费用拆分不一致：免费额度适用成本与不适用成本之和必须等于预计费用 USD"
                    )

    blocks = _opportunity_blocks(content)
    count = len(blocks)
    declared_match = re.search(r"^- 深度机会数量：(\d+)$", content, re.MULTILINE)
    if not declared_match:
        errors.append("缺少深度机会数量声明")
    elif int(declared_match.group(1)) != count:
        errors.append(f"深度机会数量声明与实际不一致：声明 {declared_match.group(1)}，实际 {count}")
    if count > 5:
        errors.append(f"深度机会最多 5 个，当前 {count} 个")
    if count < 3 and not re.search(r"^- 少于 3 个的原因：\S.+$", content, re.MULTILINE):
        errors.append("深度机会少于 3 个时必须说明少于 3 个的原因，不能凑数")

    for opportunity_id, block in blocks:
        for section in REQUIRED_OPPORTUNITY_SECTIONS:
            if section not in block:
                errors.append(f"{opportunity_id} 缺少章节：{section}")
        for field in (
            "类型", "综合分", "首笔收入潜力", "长期规模潜力", "个人影响力潜力", "证据置信度",
            "目标市场", "目标用户", "敏感领域", "数字化交付", "一个月 MVP", "平台型", "竞争强度", "数据获取",
        ):
            if _field(block, field) is None:
                errors.append(f"{opportunity_id} 缺少字段：{field}")

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

        if not re.search(r"^- 原文：\S.+$", block, re.MULTILINE):
            errors.append(f"{opportunity_id} 缺少原文证据")
        if not re.search(r"^- 中文翻译：\S.+$", block, re.MULTILINE):
            errors.append(f"{opportunity_id} 缺少中文翻译")
        source_urls = re.findall(r"^- 来源：(https?://\S+)$", block, re.MULTILINE)
        if not source_urls:
            errors.append(f"{opportunity_id} 缺少有效来源 URL")
        if not re.search(r"^- 发布时间：\S.+$", block, re.MULTILINE):
            errors.append(f"{opportunity_id} 缺少发布时间")
        if not re.search(r"^- 日期置信度：(高|中|低)$", block, re.MULTILINE):
            errors.append(f"{opportunity_id} 缺少日期置信度")
        if not re.search(r"^- 采集时间：\S.+$", block, re.MULTILINE):
            errors.append(f"{opportunity_id} 缺少采集时间")
        if not re.search(r"^- 语言：\S.+$", block, re.MULTILINE):
            errors.append(f"{opportunity_id} 缺少证据语言")
        access_methods = re.findall(r"^- 访问方式：(.+)$", block, re.MULTILINE)
        if not access_methods:
            errors.append(f"{opportunity_id} 缺少证据访问方式")
        for access_method in access_methods:
            if access_method.strip() not in ALLOWED_ACCESS_METHODS:
                errors.append(f"{opportunity_id} 使用了未知访问方式：{access_method.strip()}")

        confidence_value = _field(block, "证据置信度")
        try:
            confidence_number = float(confidence_value) if confidence_value is not None else None
        except ValueError:
            confidence_number = None
            errors.append(f"{opportunity_id} 的证据置信度必须是 0 到 10 的数字")
        if len(set(source_urls)) == 1 and confidence_number is not None and confidence_number > 4:
            errors.append(f"{opportunity_id} 只有单来源证据，证据置信度不得高于 4")

        if _field(block, "平台型") == "是" and "#### 单边切入口" not in block:
            errors.append(f"{opportunity_id} 是平台型机会，必须填写单边切入口")
        if _field(block, "竞争强度") == "高" and "#### 老产品的新形态" not in block:
            errors.append(f"{opportunity_id} 竞争强度高，必须说明老产品的新形态")
        if data_access == "未知":
            warnings.append(f"{opportunity_id} 的产品化数据获取仍需验证")

    if "### 已覆盖" not in content or "### 跳过或失败" not in content:
        errors.append("数据源覆盖必须同时列出“已覆盖”和“跳过或失败”")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings, opportunity_count=count)


def main() -> int:  # pragma: no cover - 由 tests/test_e2e.py 通过独立子进程覆盖
    parser = argparse.ArgumentParser(description="校验 AI 创业机会雷达 Markdown 日报")
    parser.add_argument("report", type=Path)
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = parser.parse_args()
    try:
        content = args.report.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(str(exc))
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
    raise SystemExit(main())
