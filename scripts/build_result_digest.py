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

from contracts import SCHEMA_VERSION, canonical_sha256, normalize_identity


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


def _record_id(record: dict[str, Any]) -> str:
    return _cell(record.get("id") or record.get("candidate_id") or "未分配 ID")


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
    for payload in executions:
        if not isinstance(payload, dict):
            raise DigestError("execution 输入必须是对象")
        summary = payload.get("summary") or {}
        if not isinstance(summary, dict):
            raise DigestError("execution.summary 必须是对象")
        totals["requests"] += int(summary.get("requests") or 0)
        totals["ok"] += int(summary.get("ok") or 0)
        totals["error"] += int(summary.get("error") or 0)
        totals["estimated_cost_usd"] += _decimal(summary.get("estimated_attempted_cost_usd"))
        by_source = summary.get("by_source") or []
        if not isinstance(by_source, list):
            raise DigestError("execution.summary.by_source 必须是数组")
        for item in by_source:
            if not isinstance(item, dict):
                continue
            source = normalize_identity(item.get("source")) or "unknown"
            sources[source]["requests"] += int(item.get("requests") or 0)
            sources[source]["ok"] += int(item.get("ok") or 0)
            sources[source]["error"] += int(item.get("error") or 0)
            sources[source]["estimated_cost_usd"] += _decimal(item.get("estimated_attempted_cost_usd"))
    return totals, sources


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


def _opportunity_table(records: list[dict[str, Any]], *, tier: str) -> str:
    lines = [
        "| ID | 点子 | 付款者 | 付费对标/现有支出 | 当前替代 | 产品缺口 | 获客渠道 | 30 天 MVP | 证据 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    if not records:
        lines.append(f"| - | 没有达到 {tier} 级的候选 | - | - | - | - | - | - | - |")
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                (
                    _record_id(record),
                    _cell(record.get("title") or record.get("name")),
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


def _regional_table(records: list[dict[str, Any]]) -> str:
    lines = [
        "| ID | 点子 | 来源 → 目标地区 | 可能付款者 | 付费对标 | 本地差异 | 最小产品 | 缺失证据 | 证据 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    if not records:
        lines.append("| - | 没有达到 R 级的区域迁移候选 | - | - | - | - | - | - | - |")
    for record in records:
        route = f"{_text(record.get('source_region'))} → {_text(record.get('target_region'))}"
        lines.append(
            "| "
            + " | ".join(
                (
                    _record_id(record),
                    _cell(record.get("title") or record.get("name")),
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


def _rejected_table(records: list[dict[str, Any]]) -> str:
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
                    _record_id(record),
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
) -> dict[str, Any]:
    """生成指标和 Markdown；所有合格及 overflow 候选都必须展示。"""
    if not isinstance(tiered, dict):
        raise DigestError("tiered 输入必须是对象")
    if not 0 <= rejected_limit <= 100:
        raise DigestError("rejected_limit 必须在 0 到 100 之间")
    deep, quick, regional = _all_qualified(tiered)
    rejected = _as_list(tiered.get("rejected"), label="rejected")
    rejected = sorted(
        rejected,
        key=lambda item: (len(item.get("rejection_reasons") or []), _record_id(item)),
    )[:rejected_limit]
    all_qualified = [*deep, *quick, *regional]
    execution_payloads = list(executions)
    execution_totals, execution_sources = _execution_metrics(execution_payloads)
    evidence_markers, evidence_by_source = _normalized_evidence(evidence_payloads)
    cluster_count = _cluster_count(research_payloads)
    used_markers = _used_evidence_markers(all_qualified)
    used_normalized = used_markers & evidence_markers
    linked_by_source = _linked_by_source(all_qualified)
    qualified_count = len(all_qualified)
    suggested_report_display_count = min(len(deep), 5) + min(len(quick), 40) + min(len(regional), 80)
    additional_conclusion_count = qualified_count - suggested_report_display_count
    cost = execution_totals["estimated_cost_usd"]
    cost_per_qualified = cost / qualified_count if execution_payloads and qualified_count else None
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
    raw_count = int(summary.get("raw") or 0)
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "as_of": tiered.get("as_of"),
        "run_id": tiered.get("run_id"),
        "benchmark_count": len(benchmarks),
        "raw_candidate_count": raw_count,
        "deep_candidate_count": len(deep),
        "quick_idea_count": len(quick),
        "regional_signal_count": len(regional),
        "suggested_report_display_count": suggested_report_display_count,
        "additional_conclusion_count": additional_conclusion_count,
        "qualified_conclusion_count": qualified_count,
        "rejected_total": len(_as_list(tiered.get("rejected"), label="rejected")),
        "rejected_displayed": len(rejected),
        "normalized_evidence_count": len(evidence_markers),
        "used_normalized_evidence_count": len(used_normalized),
        "evidence_utilization_percent": None if utilization is None else float(utilization.quantize(Decimal("0.01"))),
        "cluster_candidate_count": cluster_count,
        "paid_request_count": execution_totals["requests"],
        "paid_request_ok": execution_totals["ok"],
        "paid_request_error": execution_totals["error"],
        "estimated_cost_usd": float(cost) if execution_payloads else None,
        "cost_per_qualified_conclusion_usd": None if cost_per_qualified is None else float(cost_per_qualified.quantize(Decimal("0.000001"))),
    }

    utilization_text = "未知" if utilization is None else f"{utilization.quantize(Decimal('0.01'))}%"
    cost_text = "未知" if not execution_payloads else f"{cost.quantize(Decimal('0.000001'))}"
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

    markdown = f"""# AI 创业机会完整结论清单｜{_text(tiered.get('as_of'), '未注明日期')}

> 本文件展示全部合格 A/B/R 候选，包括超出日报数量上限的 overflow；不会只保留 Top 5。

## 结果总览

- 付费对标数量：{metrics['benchmark_count']}
- 原始候选数量：{metrics['raw_candidate_count']}
- A 级深度候选数量：{metrics['deep_candidate_count']}
- B 级快速点子数量：{metrics['quick_idea_count']}
- R 级区域迁移数量：{metrics['regional_signal_count']}
- 建议日报展示结论数量：{metrics['suggested_report_display_count']}
- 完整清单额外结论数量：{metrics['additional_conclusion_count']}
- 合格结论数量：{metrics['qualified_conclusion_count']}
- 被拒绝候选总数：{metrics['rejected_total']}
- 展示的接近合格候选数量：{metrics['rejected_displayed']}

## 费用产出

- 付费请求次数：{metrics['paid_request_count']}
- 付费请求成功/错误：{metrics['paid_request_ok']}/{metrics['paid_request_error']}
- 预计费用 USD：{cost_text}
- 规范化证据数量：{metrics['normalized_evidence_count']}
- 聚类候选数量：{metrics['cluster_candidate_count']}
- 已利用证据数量：{metrics['used_normalized_evidence_count']}
- 证据利用率：{utilization_text}
- 单个合格结论估算成本 USD：{cost_per_text}

{chr(10).join(source_table)}

## 一、全部 A 级深度候选

{_opportunity_table(deep, tier='A')}

## 二、全部 B 级快速点子

{_opportunity_table(quick, tier='B')}

## 三、全部 R 级区域迁移创意

{_regional_table(regional)}

## 四、最接近合格但被拒绝

{_rejected_table(rejected)}

## 五、说明

- A/B/R 表展示全部合格候选，包含 `overflow`，不会因日报上限而隐藏。
- 拒绝池按未通过门槛数量从少到多展示前 {rejected_limit} 个；完整拒绝池保留在 tiered JSON。
- 费用为执行文件中的尝试成本估算，实际账单仍以服务商日志为准。
- 证据利用率只统计传入的规范化证据中，被合格候选直接引用的唯一证据。
"""
    return {"metrics": metrics, "source_yield": source_rows, "markdown": markdown}


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
    raise SystemExit(main())
