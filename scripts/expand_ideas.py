#!/usr/bin/env python3
"""从真实付费对标确定性扩展大量、可追溯的候选点子。"""

from __future__ import annotations

import argparse
import itertools
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from contracts import ContractError, SCHEMA_VERSION, canonical_sha256, make_benchmark_id, validate_benchmark_id


DEFAULT_LIMIT = 200
MAX_LIMIT = 500
DIMENSION_KEYS = ("segments", "triggers", "forms", "regions", "channels", "offers")


class ExpansionError(ValueError):
    """付费对标或扩展维度不符合契约。"""


def _nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _require(record: dict[str, Any], fields: Iterable[str], label: str) -> None:
    missing = [field for field in fields if not _nonempty(record.get(field))]
    if missing:
        raise ExpansionError(f"{label}缺少字段：{', '.join(missing)}")


def _items(dimensions: dict[str, Any], key: str, fallback: Any) -> list[Any]:
    values = dimensions.get(key)
    if values is None:
        return [fallback]
    if not isinstance(values, list) or not values:
        raise ExpansionError(f"扩展维度 {key} 必须是非空数组")
    return values


def _text_variant(value: Any, field: str) -> str:
    if isinstance(value, dict):
        result = value.get(field) or value.get("name") or value.get("value")
    else:
        result = value
    if not _nonempty(result):
        raise ExpansionError(f"扩展维度缺少可用的 {field}")
    return str(result).strip()


def _region_variant(value: Any, benchmark: dict[str, Any]) -> dict[str, str]:
    if isinstance(value, str):
        return {
            "country": value.strip(),
            "region": value.strip(),
            "language": "待验证",
            "localization_gap": "待验证本地语言、支付、渠道或工作流差异",
            "transfer_reason": "同类任务在来源市场已有付费，本地供给仍需验证",
        }
    if not isinstance(value, dict):
        raise ExpansionError("regions 中每项必须是字符串或对象")
    country = value.get("country") or value.get("market") or value.get("region")
    if not _nonempty(country):
        raise ExpansionError("regions 对象缺少 country/market/region")
    return {
        "country": str(country).strip(),
        "region": str(value.get("region") or country).strip(),
        "language": str(value.get("language") or "待验证").strip(),
        "localization_gap": str(
            value.get("localization_gap") or "待验证本地语言、支付、渠道或工作流差异"
        ).strip(),
        "transfer_reason": str(
            value.get("transfer_reason") or f"{benchmark['source_market']} 已存在付费对标"
        ).strip(),
    }


def _normalize_benchmark(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExpansionError("benchmarks 中每项必须是对象")
    benchmark = deepcopy(value)
    _require(benchmark, ("source_market", "payer"), "付费对标")
    if not _nonempty(benchmark.get("product") or benchmark.get("title")):
        raise ExpansionError("付费对标缺少 product/title")
    payment_signals = benchmark.get("payment_signals")
    if not isinstance(payment_signals, list) or not payment_signals:
        raise ExpansionError("付费对标必须包含非空 payment_signals")
    supplied_id = benchmark.get("id")
    try:
        benchmark["id"] = validate_benchmark_id(supplied_id) if supplied_id else make_benchmark_id(benchmark)
    except ContractError as exc:
        raise ExpansionError(str(exc)) from exc
    benchmark["product"] = str(benchmark.get("product") or benchmark.get("title")).strip()
    return benchmark


def expand_ideas(payload: dict[str, Any], *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """做有限笛卡尔扩展；相同输入始终产生相同顺序与候选 ID。"""
    if not isinstance(payload, dict):
        raise ExpansionError("输入必须是 JSON 对象")
    if not 1 <= limit <= MAX_LIMIT:
        raise ExpansionError(f"limit 必须在 1 到 {MAX_LIMIT} 之间")
    raw_benchmarks = payload.get("benchmarks")
    if not isinstance(raw_benchmarks, list) or not raw_benchmarks:
        raise ExpansionError("输入必须包含非空 benchmarks 数组")
    dimensions = payload.get("dimensions") or {}
    if not isinstance(dimensions, dict):
        raise ExpansionError("dimensions 必须是对象")

    benchmarks = [_normalize_benchmark(item) for item in raw_benchmarks]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    truncated = False
    for benchmark in benchmarks:
        segment_fallback = benchmark.get("target_user") or benchmark["payer"]
        trigger_fallback = benchmark.get("buying_trigger") or benchmark.get("job") or "出现高频重复任务时"
        form_fallback = benchmark.get("new_form") or benchmark.get("wedge") or "用 AI 缩短一次高成本操作"
        channel_fallback = benchmark.get("acquisition_channel") or "付费对标用户所在社区"
        offer_fallback = benchmark.get("delivery_model") or "订阅制数字产品"
        axes = (
            _items(dimensions, "segments", segment_fallback),
            _items(dimensions, "triggers", trigger_fallback),
            _items(dimensions, "forms", form_fallback),
            _items(dimensions, "regions", benchmark["source_market"]),
            _items(dimensions, "channels", channel_fallback),
            _items(dimensions, "offers", offer_fallback),
        )
        for segment, trigger, form, region_value, channel, offer in itertools.product(*axes):
            region = _region_variant(region_value, benchmark)
            target_user = _text_variant(segment, "target_user")
            buying_trigger = _text_variant(trigger, "buying_trigger")
            wedge = _text_variant(form, "wedge")
            acquisition_channel = _text_variant(channel, "acquisition_channel")
            delivery_model = _text_variant(offer, "delivery_model")
            identity = {
                "benchmark_id": benchmark["id"],
                "target_user": target_user,
                "buying_trigger": buying_trigger,
                "wedge": wedge,
                "country": region["country"],
                "channel": acquisition_channel,
                "delivery_model": delivery_model,
            }
            marker = canonical_sha256(identity)
            if marker in seen:
                continue
            seen.add(marker)
            source_market = str(benchmark["source_market"]).strip()
            candidate = {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": f"CAND-{marker[:10].upper()}",
                "title": f"面向{target_user}的{wedge}",
                "benchmark_ids": [benchmark["id"]],
                "benchmark_product": benchmark["product"],
                "target_user": target_user,
                "context": buying_trigger,
                "problem_or_desire": str(
                    benchmark.get("problem_or_desire") or benchmark.get("job") or f"更低成本完成 {benchmark['product']} 对应任务"
                ).strip(),
                "wedge": wedge,
                "payer": str(benchmark["payer"]).strip(),
                "buying_trigger": buying_trigger,
                "current_alternative": str(
                    benchmark.get("current_alternative") or benchmark["product"]
                ).strip(),
                "current_spend": str(benchmark.get("current_spend") or benchmark.get("price") or "金额待核实").strip(),
                "payment_signals": deepcopy(benchmark["payment_signals"]),
                "product_gap": str(
                    benchmark.get("product_gap") or region["localization_gap"]
                ).strip(),
                "acquisition_channel": acquisition_channel,
                "delivery_model": delivery_model,
                "mvp_days": int(benchmark.get("mvp_days", 30)),
                "mvp_scope": str(benchmark.get("mvp_scope") or f"只完成“{wedge}”单任务闭环").strip(),
                "evidence": deepcopy(benchmark.get("evidence") or []),
                "demand_signals": deepcopy(benchmark.get("demand_signals") or []),
                "source_region": source_market,
                "target_region": region["country"],
                "localization_gap": region["localization_gap"],
                "transfer_reason": region["transfer_reason"],
                "market_scope": {
                    "country": region["country"],
                    "region": region["region"],
                    "language": region["language"],
                    "primary_channel": acquisition_channel,
                },
            }
            candidates.append(candidate)
            if len(candidates) >= limit:
                truncated = True
                break
        if truncated:
            break
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": payload.get("run_id"),
        "as_of": payload.get("as_of"),
        "benchmarks": benchmarks,
        "summary": {
            "benchmark_count": len(benchmarks),
            "candidate_count": len(candidates),
            "limit": limit,
            "truncated": truncated,
            "expansion_axes": list(DIMENSION_KEYS),
        },
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="从付费对标批量扩展 AI 产品候选点子")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = expand_ideas(payload, limit=args.limit)
    except (OSError, json.JSONDecodeError, ExpansionError, ValueError) as exc:
        parser.error(str(exc))
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
