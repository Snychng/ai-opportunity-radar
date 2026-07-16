#!/usr/bin/env python3
"""用六项硬门槛过滤候选，并分为 A/B/R 三个证据层级。"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from contracts import SCHEMA_VERSION, evidence_independent_sources, normalize_identity, validate_benchmark_id


DEMAND_SIGNAL_TYPES = {
    "complaint", "workaround", "manual", "manual_workaround", "hiring", "hire", "job_posting",
    "outsourcing", "outsource", "cancellation", "cancelled", "switching", "switched",
}
DIRECT_PAYMENT_TYPES = {
    "paid", "payment", "purchase", "customer_purchase", "transaction", "revenue", "sale", "sales",
    "subscription", "paid_subscription", "invoice", "contract", "paid_contract", "preorder",
}
HARD_GATES = (
    "paid_market",
    "clear_payer",
    "current_alternative",
    "product_gap",
    "acquisition_channel",
    "mvp_within_30_days",
)
QUICK_IDEA_MAX = 40
REGIONAL_SIGNAL_MAX = 80


class FilterError(ValueError):
    """候选输入或过滤配置不符合契约。"""


def _present(value: Any) -> bool:
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(str(value or "").strip())


def _signal_types(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for item in values:
        raw = item.get("type") if isinstance(item, dict) else item
        normalized = normalize_identity(raw).replace(" ", "_")
        if normalized:
            result.add(normalized)
    return result


def _local_payment(candidate: dict[str, Any]) -> bool:
    target = normalize_identity(candidate.get("target_region"))
    source = normalize_identity(candidate.get("source_region"))
    for item in candidate.get("payment_signals") or []:
        if not isinstance(item, dict):
            continue
        if item.get("local") is True:
            return True
        region = normalize_identity(item.get("region") or item.get("market"))
        if target and region == target:
            return True
    return bool(target and source and target == source)


def evaluate_gates(candidate: dict[str, Any]) -> dict[str, bool]:
    benchmark_ids = candidate.get("benchmark_ids")
    valid_benchmarks = isinstance(benchmark_ids, list) and bool(benchmark_ids)
    if valid_benchmarks:
        try:
            for benchmark_id in benchmark_ids:
                validate_benchmark_id(benchmark_id)
        except ValueError:
            valid_benchmarks = False
    try:
        mvp_days = int(candidate.get("mvp_days"))
    except (TypeError, ValueError):
        mvp_days = 0
    return {
        "paid_market": valid_benchmarks and _present(candidate.get("payment_signals")),
        "clear_payer": _present(candidate.get("payer")),
        "current_alternative": _present(candidate.get("current_alternative")),
        "product_gap": _present(candidate.get("product_gap")),
        "acquisition_channel": _present(candidate.get("acquisition_channel")),
        "mvp_within_30_days": 1 <= mvp_days <= 30 and _present(candidate.get("mvp_scope")),
    }


def classify_candidate(candidate: dict[str, Any]) -> tuple[str | None, list[str], dict[str, bool]]:
    gates = evaluate_gates(candidate)
    failed = [gate for gate in HARD_GATES if not gates[gate]]
    if failed:
        return None, failed, gates

    evidence = candidate.get("evidence") or []
    payment_types = _signal_types(candidate.get("payment_signals"))
    demand_types = _signal_types(candidate.get("demand_signals"))
    local_payment = _local_payment(candidate)
    source_region = normalize_identity(candidate.get("source_region"))
    target_region = normalize_identity(candidate.get("target_region"))
    is_transfer = bool(source_region and target_region and source_region != target_region)

    if (
        local_payment
        and bool(payment_types & DIRECT_PAYMENT_TYPES)
        and len(evidence_independent_sources(evidence)) >= 2
    ):
        return "A", [], gates
    if is_transfer and not local_payment and _present(candidate.get("transfer_reason")):
        return "R", [], gates
    if demand_types & DEMAND_SIGNAL_TYPES:
        return "B", [], gates
    return None, ["missing_demand_or_local_payment_proof"], gates


def filter_ideas(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FilterError("输入必须是 JSON 对象")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise FilterError("输入必须包含 candidates 数组")
    benchmarks = payload.get("benchmarks") or []
    if not isinstance(benchmarks, list) or not all(isinstance(item, dict) for item in benchmarks):
        raise FilterError("benchmarks 必须是对象数组")

    buckets: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "R": [], "rejected": []}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise FilterError("candidates 中每项必须是对象")
        tier, reasons, gates = classify_candidate(candidate)
        value = deepcopy(candidate)
        value["hard_gates"] = gates
        if tier is None:
            value["rejection_reasons"] = reasons
            buckets["rejected"].append(value)
            continue
        value["evidence_tier"] = tier
        value["analysis_depth"] = "deep_candidate" if tier == "A" else "quick" if tier == "B" else "regional_hypothesis"
        value["record_kind"] = "signal" if tier == "R" else "opportunity"
        if tier == "R":
            value.setdefault("missing_proof", "目标地区本地付款、投诉或替代行为证据")
            value.setdefault("promotion_triggers", ["发现目标地区直接付款证据", "补齐至少两个独立本地证据源"])
        buckets[tier].append(value)

    emitted_b = buckets["B"][:QUICK_IDEA_MAX]
    emitted_r = buckets["R"][:REGIONAL_SIGNAL_MAX]
    overflow_b = buckets["B"][QUICK_IDEA_MAX:]
    overflow_r = buckets["R"][REGIONAL_SIGNAL_MAX:]
    counts = {
        "benchmark_count": len(benchmarks),
        "raw": len(candidates),
        "tier_a": len(buckets["A"]),
        "tier_b": len(emitted_b),
        "tier_r": len(emitted_r),
        "tier_b_qualified": len(buckets["B"]),
        "tier_r_qualified": len(buckets["R"]),
        "rejected": len(buckets["rejected"]),
    }
    warnings: list[str] = []
    if counts["raw"] < 100:
        warnings.append("原始候选少于 100：允许输出，但应说明付费对标或扩展维度不足")
    if counts["tier_b"] < 20:
        warnings.append("B 级快速点子少于 20：不得用弱证据补齐")
    if counts["tier_r"] < 30:
        warnings.append("R 级区域迁移点子少于 30：不得冒充已验证机会")
    if overflow_b:
        warnings.append(f"B 级合格候选超过 {QUICK_IDEA_MAX}：日报只保留前 {QUICK_IDEA_MAX} 个，其余进入 overflow")
    if overflow_r:
        warnings.append(f"R 级合格候选超过 {REGIONAL_SIGNAL_MAX}：日报只保留前 {REGIONAL_SIGNAL_MAX} 个，其余进入 overflow")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": payload.get("run_id"),
        "as_of": payload.get("as_of"),
        "benchmarks": deepcopy(benchmarks),
        "policy": {"hard_gates": list(HARD_GATES), "tiers": ["A", "B", "R"]},
        "summary": counts,
        "warnings": warnings,
        "deep_candidates": buckets["A"],
        "validated_ideas": emitted_b,
        "regional_signals": emitted_r,
        "overflow": {"validated_ideas": overflow_b, "regional_signals": overflow_r},
        "rejected": buckets["rejected"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="过滤并分层 AI 产品候选点子")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = filter_ideas(payload)
    except (OSError, json.JSONDecodeError, FilterError) as exc:
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
