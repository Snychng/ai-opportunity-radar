#!/usr/bin/env python3
"""生成不丢结论的完整机会清单与费用产出摘要。"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from contracts import SCHEMA_VERSION, ContractError, canonical_sha256, fingerprint_record, normalize_identity, validate_stage_envelope


class DigestError(ValueError):
    """完整结论清单输入不符合契约。"""


def _as_list(value: Any, *, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DigestError(f"{label} 必须是对象数组")
    return value


def _text(value: Any, default: str = "待验证") -> str:
    if isinstance(value, list):
        result = "、".join(_text(item, "") for item in value if _text(item, ""))
    elif isinstance(value, dict):
        result = str(value.get("name") or value.get("value") or "")
    else:
        result = str(value or "")
    result = re.sub(r"\s+", " ", result).strip()
    return result or default


def _cell(value: Any, default: str = "待验证") -> str:
    return _text(value, default).replace("|", "\\|")


def _is_demo(record: dict[str, Any]) -> bool:
    if record.get("is_demo") is True:
        return True
    return any(isinstance(item, dict) and _is_demo(item)
               for field in ("variants", "evidence")
               if isinstance(record.get(field), list) for item in record[field])


def _record_id(record: dict[str, Any], *, inherited_demo: bool = False) -> str:
    identifier = _cell(record.get("id") or record.get("candidate_id") or "未分配 ID")
    return identifier + ("（演示）" if inherited_demo or _is_demo(record) else "")


def _family_identity(record: dict[str, Any]) -> str:
    """展示构成业务身份的实际差异，避免不同场景或渠道呈现为重复点子。"""
    scope = record.get("market_scope") or {}
    scope = scope if isinstance(scope, dict) else {}
    fields = (
        ("人群", record.get("target_user")),
        ("触发", record.get("context") or record.get("buying_trigger")),
        ("任务", record.get("problem_or_desire")),
        ("切口", record.get("wedge")),
        ("国家", scope.get("country") or record.get("target_region")),
        ("区域", scope.get("region")),
        ("主渠道", scope.get("primary_channel") or record.get("acquisition_channel")),
    )
    return _cell("；".join(f"{label}：{_text(value)}" for label, value in fields if value))


def _benchmark(record: dict[str, Any]) -> str:
    ids = record.get("benchmark_ids") or []
    id_text = "、".join(str(item) for item in ids) if isinstance(ids, list) else str(ids)
    product = _text(record.get("benchmark_product"), "")
    current_spend = _text(record.get("current_spend"), "")
    parts = [part for part in (id_text, product, current_spend) if part]
    return _cell("｜".join(parts), "待补付费对标")


def _evidence_urls(record: dict[str, Any], *, limit: int = 3) -> list[str]:
    result: list[str] = []
    for item in record.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in result:
            continue
        result.append(url)
        if len(result) >= limit:
            break
    return result


def _evidence_links(record: dict[str, Any]) -> str:
    urls = _evidence_urls(record)
    return " ".join(f"[{index}]({url})" for index, url in enumerate(urls, start=1)) or "待补直达证据"


def _all_qualified(tiered: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    deep = _as_list(tiered.get("deep_candidates"), label="deep_candidates")
    quick = _as_list(tiered.get("validated_ideas"), label="validated_ideas")
    regional = _as_list(tiered.get("regional_signals"), label="regional_signals")
    overflow = tiered.get("overflow") or {}
    if not isinstance(overflow, dict):
        raise DigestError("overflow 必须是对象")
    quick = [*quick, *_as_list(overflow.get("validated_ideas"), label="overflow.validated_ideas")]
    regional = [*regional, *_as_list(overflow.get("regional_signals"), label="overflow.regional_signals")]
    return deep, quick, regional


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except InvalidOperation as exc:
        raise DigestError(f"无效费用数字：{value}") from exc
    if not result.is_finite() or result < 0:
        raise DigestError(f"费用必须是非负有限数字：{value}")
    return result


def _execution_metrics(executions: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    totals = {"requests": 0, "ok": 0, "error": 0, "estimated_cost_usd": Decimal("0")}
    sources: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"requests": 0, "ok": 0, "error": 0, "estimated_cost_usd": Decimal("0")}
    )
    seen: set[str] = set()
    for payload in executions:
        if not isinstance(payload, dict):
            raise DigestError("execution 输入必须是对象")
        marker = canonical_sha256(payload)
        if marker in seen:
            continue
        seen.add(marker)
        summary = payload.get("summary") or {}
        if not isinstance(summary, dict):
            raise DigestError("execution.summary 必须是对象")
        totals["requests"] += _count(summary.get("requests", 0))
        totals["ok"] += _count(summary.get("ok", 0))
        totals["error"] += _count(summary.get("error", 0))
        if _count(summary.get("ok", 0)) + _count(summary.get("error", 0)) > _count(summary.get("requests", 0)):
            raise DigestError("成功与错误请求之和不能超过总请求数")
        totals["estimated_cost_usd"] += _decimal(summary.get("estimated_attempted_cost_usd"))
        by_source = summary.get("by_source") or []
        if not isinstance(by_source, list):
            raise DigestError("execution.summary.by_source 必须是数组")
        for item in by_source:
            if not isinstance(item, dict):
                continue
            source = normalize_identity(item.get("source")) or "unknown"
            sources[source]["requests"] += _count(item.get("requests", 0))
            sources[source]["ok"] += _count(item.get("ok", 0))
            sources[source]["error"] += _count(item.get("error", 0))
            sources[source]["estimated_cost_usd"] += _decimal(item.get("estimated_attempted_cost_usd"))
    return totals, sources


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DigestError("请求计数必须是非负整数")
    return value


def _validate_inputs(tiered: dict[str, Any], executions: list[dict[str, Any]], evidence: list[dict[str, Any]], research: list[dict[str, Any]]) -> None:
    try:
        envelope = validate_stage_envelope(tiered)
        for kind, payloads in (("execution", executions), ("evidence", evidence), ("research", research)):
            for payload in payloads:
                other = validate_stage_envelope(payload)
                target_run = envelope.get("run_id")
                if not target_run:
                    continue
                if kind == "execution" and other.get("run_id") != target_run:
                    raise DigestError("执行费用必须属于本次 run_id；历史证据复用不能混入旧费用")
                if other.get("run_id") and other["run_id"] != target_run:
                    if kind == "execution" or payload.get("reused_for_run_id") != target_run:
                        raise DigestError(f"{kind} 跨运行复用必须声明 reused_for_run_id")
    except ContractError as exc:
        raise DigestError(str(exc)) from exc


def _family_key(record: dict[str, Any]) -> str:
    try:
        return fingerprint_record(record)
    except ContractError:
        return str(record.get("fingerprint") or record.get("id") or record.get("candidate_id") or canonical_sha256(record))


def _normalized_evidence(
    payloads: Iterable[dict[str, Any]],
) -> tuple[set[str], dict[str, set[str]]]:
    all_markers: set[str] = set()
    by_source: dict[str, set[str]] = defaultdict(set)
    for payload in payloads:
        if not isinstance(payload, dict):
            raise DigestError("evidence 输入必须是对象")
        evidence = _as_list(payload.get("evidence"), label="evidence")
        for item in evidence:
            url = str(item.get("url") or "").strip()
            marker = url or canonical_sha256(item)
            source = normalize_identity(item.get("source")) or "unknown"
            all_markers.add(marker)
            by_source[source].add(marker)
    return all_markers, by_source


def _cluster_count(payloads: Iterable[dict[str, Any]]) -> int:
    markers: set[str] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            raise DigestError("research 输入必须是对象")
        values = payload.get("ranked_candidates")
        if values is None:
            values = payload.get("clusters") or []
        if not isinstance(values, list):
            raise DigestError("research.ranked_candidates/clusters 必须是数组")
        for item in values:
            marker = canonical_sha256(item)
            markers.add(marker)
    return len(markers)


def _linked_by_source(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for record in records:
        seen: set[str] = set()
        for item in record.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            source = normalize_identity(item.get("source")) or "unknown"
            seen.add(source)
        for source in seen:
            result[source] += 1
    return result


def _used_evidence_markers(records: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for record in records:
        for item in record.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            result.add(url or canonical_sha256(item))
    return result


def _opportunity_table(records: list[dict[str, Any]], *, tier: str, inherited_demo: bool = False) -> str:
    lines = [
        "| ID | 点子 | 人群、场景与市场 | 付款者 | 付费对标/现有支出 | 当前替代 | 产品缺口 | 获客渠道 | 30 天 MVP | 证据 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not records:
        lines.append(f"| - | 没有达到 {tier} 级的候选 | - | - | - | - | - | - | - | - |")
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                (
                    _record_id(record, inherited_demo=inherited_demo),
                    _cell(record.get("title") or record.get("name")),
                    _family_identity(record),
                    _cell(record.get("payer")),
                    _benchmark(record),
                    _cell(record.get("current_alternative")),
                    _cell(record.get("product_gap")),
                    _cell(record.get("acquisition_channel")),
                    _cell(record.get("mvp_scope") or record.get("mvp_days")),
                    _evidence_links(record),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def _regional_table(records: list[dict[str, Any]], *, inherited_demo: bool = False) -> str:
    lines = [
        "| ID | 点子 | 人群、场景与市场 | 来源 → 目标地区 | 可能付款者 | 付费对标 | 本地差异 | 最小产品 | 缺失证据 | 证据 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not records:
        lines.append("| - | 没有达到 R 级的区域迁移候选 | - | - | - | - | - | - | - | - |")
    for record in records:
        route = f"{_text(record.get('source_region'))} → {_text(record.get('target_region'))}"
        lines.append(
            "| "
            + " | ".join(
                (
                    _record_id(record, inherited_demo=inherited_demo),
                    _cell(record.get("title") or record.get("name")),
                    _family_identity(record),
                    _cell(route),
                    _cell(record.get("payer")),
                    _benchmark(record),
                    _cell(record.get("localization_gap")),
                    _cell(record.get("mvp_scope")),
                    _cell(record.get("missing_proof")),
                    _evidence_links(record),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def _rejected_table(records: list[dict[str, Any]], *, inherited_demo: bool = False) -> str:
    lines = [
        "| ID | 点子 | 未通过门槛 | 已知付款者 | 已知对标 | 补证后可重审 |",
        "|---|---|---|---|---|---|",
    ]
    if not records:
        lines.append("| - | 没有接近合格的拒绝候选 | - | - | - | - |")
    for record in records:
        reasons = record.get("rejection_reasons") or []
        lines.append(
            "| "
            + " | ".join(
                (
                    _record_id(record, inherited_demo=inherited_demo),
                    _cell(record.get("title") or record.get("name")),
                    _cell(reasons),
                    _cell(record.get("payer")),
                    _benchmark(record),
                    _cell("、".join(f"补齐 {reason}" for reason in reasons), "重新建立付费与需求证据"),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def build_result_digest(
    tiered: dict[str, Any],
    *,
    executions: Iterable[dict[str, Any]] = (),
    evidence_payloads: Iterable[dict[str, Any]] = (),
    research_payloads: Iterable[dict[str, Any]] = (),
    rejected_limit: int = 20,
    run_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成指标和 Markdown；所有合格及 overflow 候选都必须展示。"""
    if not isinstance(tiered, dict):
        raise DigestError("tiered 输入必须是对象")
    if not 0 <= rejected_limit <= 100:
        raise DigestError("rejected_limit 必须在 0 到 100 之间")
    deep, quick, regional = _all_qualified(tiered)
    leads = _as_list(tiered.get("research_leads"), label="research_leads")
    rejected = _as_list(tiered.get("rejected"), label="rejected")
    rejected = sorted(
        rejected,
        key=lambda item: (len(item.get("rejection_reasons") or []), _record_id(item)),
    )[:rejected_limit]
    all_qualified = [*deep, *quick, *regional]
    execution_payloads = list(executions)
    evidence_payloads = list(evidence_payloads)
    research_payloads = list(research_payloads)
    _validate_inputs(tiered, execution_payloads, evidence_payloads, research_payloads)
    execution_totals, execution_sources = _execution_metrics(execution_payloads)
    if run_ledger is not None:
        if run_ledger.get("run_id") != tiered.get("run_id") or run_ledger.get("as_of") != tiered.get("as_of"):
            raise DigestError("请求账本必须属于本轮 run_id/as_of")
        states = run_ledger.get("attempt_states", {})
        execution_totals = {"requests": run_ledger["attempts"], "ok": states.get("succeeded", 0),
                            "error": sum(states.get(key, 0) for key in ("failed", "outcome_unknown", "started")),
                            "estimated_cost_usd": _decimal(run_ledger["estimated_attempted_cost_usd_exact"])}
        execution_sources = {row["source"]: {
            "requests": row["attempts"], "ok": row.get("succeeded", 0),
            "error": sum(row.get(key, 0) for key in ("failed", "outcome_unknown", "started")),
            "estimated_cost_usd": _decimal(row["estimated_attempted_cost_usd_exact"]),
        } for row in run_ledger.get("by_source", [])}
    cost_available = bool(execution_payloads) or run_ledger is not None
    evidence_markers, evidence_by_source = _normalized_evidence(evidence_payloads)
    cluster_count = _cluster_count(research_payloads)
    used_markers = _used_evidence_markers(all_qualified)
    used_normalized = used_markers & evidence_markers
    linked_by_source = _linked_by_source(all_qualified)
    qualified_count = len(all_qualified)
    family_count = len({_family_key(record) for record in all_qualified})
    validated_family_count = len({_family_key(record) for record in [*deep, *quick]})
    suggested_report_display_count = min(len(deep), 5) + min(len(quick), 40) + min(len(regional), 80)
    additional_conclusion_count = qualified_count - suggested_report_display_count
    cost = execution_totals["estimated_cost_usd"]
    cost_per_qualified = cost / qualified_count if cost_available and qualified_count else None
    utilization = (Decimal(len(used_normalized)) / len(evidence_markers) * 100) if evidence_markers else None
    benchmarks = _as_list(tiered.get("benchmarks"), label="benchmarks")

    sources = sorted(set(execution_sources) | set(evidence_by_source) | set(linked_by_source))
    source_rows: list[dict[str, Any]] = []
    for source in sources:
        execution = execution_sources.get(source) or {}
        source_rows.append(
            {
                "source": source,
                "requests": int(execution.get("requests") or 0),
                "ok": int(execution.get("ok") or 0),
                "error": int(execution.get("error") or 0),
                "estimated_cost_usd": str(execution.get("estimated_cost_usd") or Decimal("0")),
                "normalized_evidence": len(evidence_by_source.get(source) or set()),
                "linked_conclusions": linked_by_source.get(source, 0),
            }
        )

    summary = tiered.get("summary") or {}
    expansion_summary = tiered.get("expansion_summary") or {}
    warnings = tiered.get("warnings") or []
    if not isinstance(expansion_summary, dict):
        raise DigestError("expansion_summary 必须是对象")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise DigestError("warnings 必须是字符串数组")
    inherited_demo = tiered.get("is_demo") is True
    contains_demo = inherited_demo or any(
        _is_demo(record) for record in [*all_qualified, *leads, *benchmarks, *_as_list(tiered.get("rejected"), label="rejected")]
    )
    raw_count = int(summary.get("raw") or 0)
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "as_of": tiered.get("as_of"),
        "run_id": tiered.get("run_id"),
        "benchmark_count": len(benchmarks),
        "raw_candidate_count": raw_count,
        "contains_demo_data": contains_demo,
        "demo_family_count": len({_family_key(record) for record in all_qualified if inherited_demo or _is_demo(record)}),
        "expansion_truncated": expansion_summary.get("truncated"),
        "deep_candidate_count": len(deep),
        "quick_idea_count": len(quick),
        "regional_signal_count": len(regional),
        "suggested_report_display_count": suggested_report_display_count,
        "additional_conclusion_count": additional_conclusion_count,
        "qualified_conclusion_count": qualified_count,
        "qualified_family_count": family_count,
        "validated_opportunity_family_count": validated_family_count,
        "regional_hypothesis_family_count": len({_family_key(record) for record in regional}),
        "delivery_variant_count": sum(len(record.get("variants") or [record]) for record in all_qualified),
        "rejected_total": len(_as_list(tiered.get("rejected"), label="rejected")),
        "research_lead_count": len(leads),
        "lead_used_normalized_evidence_count": len(_used_evidence_markers(leads) & evidence_markers),
        "rejected_displayed": len(rejected),
        "normalized_evidence_count": len(evidence_markers),
        "used_normalized_evidence_count": len(used_normalized),
        "evidence_utilization_percent": None if utilization is None else float(utilization.quantize(Decimal("0.01"))),
        "cluster_candidate_count": cluster_count,
        "paid_request_count": execution_totals["requests"],
        "paid_request_ok": execution_totals["ok"],
        "paid_request_error": execution_totals["error"],
        "estimated_cost_usd": float(cost) if cost_available else None,
        "cost_per_qualified_conclusion_usd": None if cost_per_qualified is None else float(cost_per_qualified.quantize(Decimal("0.000001"))),
        "cost_per_validated_family_usd": None if not cost_available or not validated_family_count else float((cost / validated_family_count).quantize(Decimal("0.000001"))),
    }

    utilization_text = "未知" if utilization is None else f"{utilization.quantize(Decimal('0.01'))}%"
    cost_text = "未知" if not cost_available else f"{cost.quantize(Decimal('0.000001'))}"
    cost_per_text = "未知" if cost_per_qualified is None else f"{cost_per_qualified.quantize(Decimal('0.000001'))}"
    source_table = [
        "| 来源 | 付费请求 | 成功/错误 | 估算费用 USD | 规范化证据 | 关联结论 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    if not source_rows:
        source_table.append("| 无付费来源 | 0 | 0/0 | 0 | 0 | 0 |")
    for row in source_rows:
        source_table.append(
            f"| {_cell(row['source'])} | {row['requests']} | {row['ok']}/{row['error']} | "
            f"{_decimal(row['estimated_cost_usd']).quantize(Decimal('0.000001'))} | "
            f"{row['normalized_evidence']} | {row['linked_conclusions']} |"
        )

    notices = []
    if contains_demo:
        notices.append("> **包含演示数据**：标为“演示”的候选只用于验证流程；对标、分层和费用产出不能作为真实市场验证。")
    if expansion_summary.get("truncated") is True:
        notices.append("> **扩展可能受限**：候选数量或扫描达到配置上限，尚未确认是否仍有未生成组合；本清单仅覆盖实际生成的候选，不代表穷尽全部组合。")
    if warnings:
        notices.append("运行提示：\n\n" + "\n".join(f"- {_text(item)}" for item in warnings))

    lead_section = "## 待验证线索完整清单\n\n" + _leads_table(leads) if leads else ""
    markdown = f"""# AI 创业机会完整结论清单｜{_text(tiered.get('as_of'), '未注明日期')}

{chr(10).join(notices)}

> 本文件展示全部合格 A/B/R 候选，包括超出日报数量上限的 overflow；不会只保留 Top 5。

{lead_section}

## 结果总览

- 付费对标数量：{metrics['benchmark_count']}
- 原始候选数量：{metrics['raw_candidate_count']}
- A 级深度候选数量：{metrics['deep_candidate_count']}
- B 级快速点子数量：{metrics['quick_idea_count']}
- R 级区域迁移数量：{metrics['regional_signal_count']}
- 建议日报展示结论数量：{metrics['suggested_report_display_count']}
- 完整清单额外结论数量：{metrics['additional_conclusion_count']}
- 合格结论数量：{metrics['qualified_conclusion_count']}
- 独立机会家族数量：{metrics['qualified_family_count']}
- A/B 研究资格家族数量：{metrics['validated_opportunity_family_count']}
- 交付与报价变体数量：{metrics['delivery_variant_count']}
- 被拒绝候选总数：{metrics['rejected_total']}
- 待验证研究线索：{metrics['research_lead_count']}（不计入合格结论）
- 展示的接近合格候选数量：{metrics['rejected_displayed']}

## 费用产出

- 付费请求次数：{metrics['paid_request_count']}
- 付费请求成功/错误：{metrics['paid_request_ok']}/{metrics['paid_request_error']}
- 预计费用 USD：{cost_text}
- 规范化证据数量：{metrics['normalized_evidence_count']}
- 聚类候选数量：{metrics['cluster_candidate_count']}
- 已利用证据数量：{metrics['used_normalized_evidence_count']}
- 探索线索引用证据：{metrics['lead_used_normalized_evidence_count']}（单独统计，不增加合格结论数）
- 证据利用率：{utilization_text}
- 单个合格结论估算成本 USD：{cost_per_text}

{chr(10).join(source_table)}

## 一、全部 A 级深度候选

{_opportunity_table(deep, tier='A', inherited_demo=inherited_demo)}

## 二、全部 B 级快速点子

{_opportunity_table(quick, tier='B', inherited_demo=inherited_demo)}

## 三、全部 R 级区域迁移创意

{_regional_table(regional, inherited_demo=inherited_demo)}

## 四、最接近合格但被拒绝

{_rejected_table(rejected, inherited_demo=inherited_demo)}

## 五、说明

- A/B/R 表展示全部合格候选，包含 `overflow`，不会因日报上限而隐藏。
- 拒绝池按未通过门槛数量从少到多展示前 {rejected_limit} 个；完整拒绝池保留在 tiered JSON。
- 编排报告费用取整轮请求账本；手动链路取传入执行文件中的尝试估算，实际账单仍以服务商日志为准。
- 证据利用率只统计传入的规范化证据中，被合格候选直接引用的唯一证据。
- 合格结论包含 R 级假设；低单价不代表市场已验证。A/B 研究资格也不等于你的产品已获得付款。
"""
    return {"metrics": metrics, "source_yield": source_rows, "markdown": markdown}


def _leads_table(leads: list[dict]) -> str:
    lines = ["| 线索 | 用户与需求 | AI 增量价值假设 | 待核验 | 证据 |", "|---|---|---|---|---|"]
    for row in leads:
        lines.append(f"| {_cell(row.get('lead_id'))} · {_cell(row.get('title'))} | "
                     f"{_cell(row.get('target_user'))}：{_cell(row.get('problem_or_desire'))} | "
                     f"{_cell((row.get('ai_value') or {}).get('incremental_advantage'))} | "
                     f"{_cell(row.get('missing_requirements'))}；下一步：{_cell(row.get('next_question'))} | {_evidence_links(row)} |")
    if not leads:
        lines.append("| 暂无 | 本轮尚未保存可追溯线索 | — | 按覆盖缺口继续调查 | — |")
    return "\n".join(lines)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DigestError(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise DigestError(f"{path} 必须是 JSON 对象")
    return value


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="生成完整机会清单与费用产出摘要")
    parser.add_argument("--tiered", type=Path, required=True)
    parser.add_argument("--execution", type=Path, action="append", default=[])
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--research", type=Path, action="append", default=[])
    parser.add_argument("--rejected-limit", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--metrics-output", type=Path)
    args = parser.parse_args()
    try:
        result = build_result_digest(
            _read_object(args.tiered),
            executions=[_read_object(path) for path in _unique_paths(args.execution)],
            evidence_payloads=[_read_object(path) for path in _unique_paths(args.evidence)],
            research_payloads=[_read_object(path) for path in _unique_paths(args.research)],
            rejected_limit=args.rejected_limit,
        )
    except DigestError as exc:
        parser.error(str(exc))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result["markdown"], encoding="utf-8")
    else:
        print(result["markdown"], end="")
    if args.metrics_output:
        args.metrics_output.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_output.write_text(
            json.dumps(
                {"metrics": result["metrics"], "source_yield": result["source_yield"]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
