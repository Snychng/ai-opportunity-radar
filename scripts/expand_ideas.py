#!/usr/bin/env python3
"""从真实付费对标确定性扩展大量、可追溯的候选点子。"""

from __future__ import annotations

import argparse
import itertools
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from contracts import (ContractError, SCHEMA_VERSION, canonical_sha256, make_benchmark_id,
                       normalize_identity, validate_benchmark_id, validate_stage_envelope)


DEFAULT_LIMIT = 200
MAX_LIMIT = 500
MAX_COMBINATION_SCANS = 5000
DIMENSION_KEYS = ("segments", "triggers", "forms", "regions", "channels", "offers")


class ExpansionError(ValueError):
    """付费对标或扩展维度不符合契约。"""


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
    unique: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = canonical_sha256(value) if isinstance(value, dict) else normalize_identity(value)
        if marker not in seen:
            seen.add(marker)
            unique.append(value)
    return unique


def _text_variant(value: Any, field: str) -> str:
    if isinstance(value, dict):
        result = value.get(field) or value.get("name") or value.get("value")
    else:
        result = value
    if not _nonempty(result):
        raise ExpansionError(f"扩展维度缺少可用的 {field}")
    return str(result).strip()


def _region_variant(value: Any, benchmark: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"country": value}
    if not isinstance(value, dict):
        raise ExpansionError("regions 中每项必须是字符串或对象")
    country = value.get("country") or value.get("market") or value.get("region")
    if not _nonempty(country):
        raise ExpansionError("regions 对象缺少 country/market/region")
    return {
        "country": str(country).strip(),
        "region": str(value.get("region") or country).strip(),
        "language": value.get("language"),
        "localization_gap": value.get("localization_gap"),
        "transfer_reason": value.get("transfer_reason") or benchmark.get("transfer_reason"),
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


def _benchmark_variants(benchmark: dict[str, Any], dimensions: dict[str, Any]):
    """逐个产出变体，未经验证的扩展字段独立记录为假设。"""
    defaults = {
        "target_user": benchmark.get("target_user") or benchmark["payer"],
        "context": benchmark.get("buying_trigger") or benchmark.get("job"),
        "wedge": benchmark.get("new_form") or benchmark.get("wedge"),
        "target_region": benchmark["source_market"],
        "acquisition_channel": benchmark.get("acquisition_channel"),
    }
    axes = (
        _items(dimensions, "segments", defaults["target_user"]),
        _items(dimensions, "triggers", defaults["context"] or "待验证购买触发条件"),
        _items(dimensions, "forms", defaults["wedge"] or "待验证产品切入口"),
        _items(dimensions, "regions", defaults["target_region"]),
        _items(dimensions, "channels", defaults["acquisition_channel"]),
        _items(dimensions, "offers", benchmark.get("delivery_model")),
    )
    for segment, trigger, form, region_value, channel, offer in itertools.product(*axes):
        region = _region_variant(region_value, benchmark)
        target_user = _text_variant(segment, "target_user")
        buying_trigger = _text_variant(trigger, "buying_trigger")
        wedge = _text_variant(form, "wedge")
        acquisition_channel = _text_variant(channel, "acquisition_channel") if channel is not None else None
        delivery_model = _text_variant(offer, "delivery_model") if offer is not None else None
        identity = {
            "benchmark_id": benchmark["id"], "target_user": target_user, "buying_trigger": buying_trigger,
            "wedge": wedge, "country": region["country"], "channel": acquisition_channel,
            "delivery_model": delivery_model,
        }
        marker = canonical_sha256(identity)
        candidate = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": f"CAND-{marker[:10].upper()}",
            "variant_id": f"VAR-{marker[:10].upper()}",
            "title": f"面向{target_user}的{wedge}",
            "benchmark_ids": [benchmark["id"]], "benchmark_product": benchmark["product"],
            "target_user": target_user, "context": buying_trigger,
            "problem_or_desire": benchmark.get("problem_or_desire") or benchmark.get("job") or "待验证用户需求",
            "wedge": wedge, "payer": str(benchmark["payer"]).strip(), "buying_trigger": buying_trigger,
            "current_alternative": benchmark.get("current_alternative") or benchmark["product"],
            "current_spend": benchmark.get("current_spend") or benchmark.get("price"),
            "payment_signals": deepcopy(benchmark["payment_signals"]),
            "product_gap": benchmark.get("product_gap"),
            "acquisition_channel": acquisition_channel, "delivery_model": delivery_model,
            "mvp_days": benchmark.get("mvp_days"), "mvp_scope": benchmark.get("mvp_scope"),
            "evidence": deepcopy(benchmark.get("evidence") or []),
            "demand_signals": deepcopy(benchmark.get("demand_signals") or []),
            "source_region": str(benchmark["source_market"]).strip(), "target_region": region["country"],
            "localization_gap": region["localization_gap"], "transfer_reason": region["transfer_reason"],
            "market_scope": {"country": region["country"], "region": region["region"],
                             "language": region["language"], "primary_channel": acquisition_channel},
            "hypotheses": {},
        }
        for field, baseline in defaults.items():
            if normalize_identity(candidate[field]) != normalize_identity(baseline):
                candidate["hypotheses"][field] = {"value": candidate[field], "basis": "扩展假设，须取得候选范围内的新证据"}
        for field in ("product_gap", "acquisition_channel", "mvp_days", "mvp_scope"):
            if candidate[field] is None:
                candidate["hypotheses"][field] = {"value": None, "basis": "尚无事实或估算依据"}
        if benchmark.get("is_demo") is True:
            candidate["is_demo"] = True
        if isinstance(benchmark.get("candidate_verifications"), dict):
            candidate["candidate_verifications"] = deepcopy(benchmark["candidate_verifications"])
        yield candidate


def expand_ideas(payload: dict[str, Any], *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """维度先去重，再按对标轮询扩展；扫描预算独立于候选数量。"""
    try:
        envelope = validate_stage_envelope(payload)
    except ContractError as exc:
        raise ExpansionError(str(exc)) from exc
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ExpansionError(f"limit 必须在 1 到 {MAX_LIMIT} 之间")
    raw_benchmarks = payload.get("benchmarks")
    if not isinstance(raw_benchmarks, list) or not raw_benchmarks:
        raise ExpansionError("输入必须包含非空 benchmarks 数组")
    dimensions = payload.get("dimensions", {})
    if not isinstance(dimensions, dict):
        raise ExpansionError("dimensions 必须是对象")
    benchmarks = [_normalize_benchmark(item) for item in raw_benchmarks]
    # 共享维度只去重一次，避免每个对标重复处理大数组。
    dimensions = {key: _items(dimensions, key, None) for key in DIMENSION_KEYS if key in dimensions}
    iterators = [iter(_benchmark_variants(item, dimensions)) for item in benchmarks]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    scanned = 0
    active = list(iterators)
    while active and len(candidates) < limit and scanned < MAX_COMBINATION_SCANS:
        next_active = []
        for iterator in active:
            if len(candidates) >= limit or scanned >= MAX_COMBINATION_SCANS:
                next_active.append(iterator)
                continue
            candidate = next(iterator, None)
            if candidate is None:
                continue
            next_active.append(iterator)
            scanned += 1
            if candidate["candidate_id"] not in seen:
                seen.add(candidate["candidate_id"])
                candidates.append(candidate)
        active = next_active
    # 达到输出上限时保守标注截断；自然耗尽的输入不会被误报。
    truncated = bool(active)
    return {
        **envelope, "benchmarks": benchmarks,
        "summary": {
            "benchmark_count": len(benchmarks), "candidate_count": len(candidates), "limit": limit,
            "truncated": truncated, "expansion_axes": list(DIMENSION_KEYS),
            "combinations_scanned": scanned, "scan_budget": MAX_COMBINATION_SCANS,
            "benchmark_coverage": len({item["benchmark_ids"][0] for item in candidates}),
            "target_user_coverage": len({item["target_user"] for item in candidates}),
            "target_region_coverage": len({item["target_region"] for item in candidates}),
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
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
