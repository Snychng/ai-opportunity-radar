#!/usr/bin/env python3
"""用六项硬门槛过滤候选，并分为 A/B/R 三个证据层级。"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from contracts import (ContractError, canonical_evidence_url, canonical_sha256, evidence_independent_sources,
                       fingerprint_record, normalize_identity, validate_benchmark_id, validate_stage_envelope)


DEMAND_SIGNAL_TYPES = {
    "complaint", "workaround", "manual", "manual_workaround", "hiring", "hire", "job_posting",
    "outsourcing", "outsource", "cancellation", "cancelled", "switching", "switched",
}
DIRECT_PAYMENT_TYPES = {
    "paid", "payment", "purchase", "customer_purchase", "transaction", "sale", "sales",
    "paid_subscription", "paid_invoice", "paid_contract", "paid_preorder",
}
MARKET_SIGNAL_TYPES = DIRECT_PAYMENT_TYPES | {"pricing", "price", "subscription", "invoice", "contract", "preorder"}
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
FACT_FIELDS = ("fact", "supporting_fact", "quote", "text", "supports", "original_text")


class FilterError(ValueError):
    """候选输入或过滤配置不符合契约。"""


def _present(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and not text.lower().startswith(("待验证", "待核实", "待确认", "未知", "unknown", "tbd", "todo"))


def _active(item: dict[str, Any]) -> bool:
    return item.get("retracted") is not True and item.get("status") != "retracted"


def _linked_fact(item: Any) -> bool:
    return (isinstance(item, dict) and _active(item) and bool(canonical_evidence_url(item.get("url")))
            and any(_present(item.get(field)) for field in FACT_FIELDS))


def qualifying_evidence(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """仅返回候选证据目录中有链接、支持文本且未撤回的非演示证据。"""
    values = candidate.get("evidence")
    if not isinstance(values, list):
        return []
    return [item for item in values if _linked_fact(item) and item.get("is_demo") is not True]


def resolve_candidate_evidence(candidate: dict[str, Any], reference: Any) -> dict[str, Any] | None:
    """将主张引用绑定到候选的有效证据；修订后的证据需要绑定当前版本。"""
    if not isinstance(reference, dict) or not _active(reference) or reference.get("is_demo") is True:
        return None
    if any(reference.get(field) is not None and not isinstance(reference[field], str) for field in FACT_FIELDS):
        return None
    evidence_id = reference.get("evidence_id") or reference.get("id")
    url = canonical_evidence_url(reference.get("url"))
    if not evidence_id and not url:
        return None
    if "url" in reference and reference["url"] is not None and not url:
        return None
    for item in qualifying_evidence(candidate):
        if evidence_id and evidence_id != (item.get("evidence_id") or item.get("id")):
            continue
        if url and url != canonical_evidence_url(item.get("url")):
            continue
        version = item.get("version", 1)
        revision = reference.get("evidence_revision_id")
        if (isinstance(version, int) and version > 1) or revision is not None:
            if not revision or revision != item.get("revision_id"):
                continue
            facts = {item[field].strip() for field in FACT_FIELDS if _present(item.get(field))}
            if any(reference[field].strip() not in facts for field in FACT_FIELDS if _present(reference.get(field))):
                continue
        return item
    return None


def _supported_signals(values: Any, candidate: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    if candidate is None:
        return [item for item in values if _linked_fact(item)]
    result = []
    for reference in values:
        evidence = resolve_candidate_evidence(candidate, reference)
        if evidence is not None:
            result.append({**evidence, **reference})
    return result


def _signal_types(values: Any, candidate: dict[str, Any] | None = None) -> set[str]:
    # 基础收费/需求信号允许自带链接事实；A 级另要求进入候选证据目录。
    signals = _supported_signals(values)
    if candidate is not None:
        signals += _supported_signals(values, candidate)
    return {normalize_identity(item.get("type")).replace(" ", "_") for item in signals}


def _local_payment(candidate: dict[str, Any]) -> bool:
    """目标地区、付款主体、交易行为和支持事实必须在同一条证据中。"""
    target = normalize_identity(candidate.get("target_region"))
    if not target or candidate.get("is_demo") is True:
        return False
    for item in _supported_signals(candidate.get("payment_signals"), candidate):
        signal_type = normalize_identity(item.get("type")).replace(" ", "_")
        region = normalize_identity(item.get("region") or item.get("market"))
        if (signal_type in DIRECT_PAYMENT_TYPES and region == target and _present(item.get("payer"))
                and item.get("is_demo") is not True):
            return True
    return False


def _unverified_hypotheses(candidate: dict[str, Any]) -> list[str]:
    hypotheses = candidate.get("hypotheses", {})
    if not isinstance(hypotheses, dict):
        return ["invalid_hypotheses"]
    checks = candidate.get("candidate_verifications", {})
    if not isinstance(checks, dict):
        checks = {}
    unverified = []
    for field in hypotheses:
        check = checks.get(field)
        if (not isinstance(check, dict) or candidate.get(field) is None
                or normalize_identity(check.get("value")) != normalize_identity(candidate.get(field))
                or not _supported_signals(check.get("evidence"), candidate)):
            unverified.append(field)
    return unverified


def evaluate_gates(candidate: dict[str, Any]) -> dict[str, bool]:
    benchmark_ids = candidate.get("benchmark_ids")
    valid_benchmarks = isinstance(benchmark_ids, list) and bool(benchmark_ids)
    if valid_benchmarks:
        try:
            for benchmark_id in benchmark_ids:
                validate_benchmark_id(benchmark_id)
        except ValueError:
            valid_benchmarks = False
    mvp_days = candidate.get("mvp_days")
    if isinstance(mvp_days, bool) or not isinstance(mvp_days, int):
        mvp_days = 0
    return {
        "paid_market": valid_benchmarks and bool(_signal_types(candidate.get("payment_signals"), candidate) & MARKET_SIGNAL_TYPES),
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

    evidence = qualifying_evidence(candidate)
    demand_types = _signal_types(candidate.get("demand_signals"))
    local_payment = _local_payment(candidate)
    source_region = normalize_identity(candidate.get("source_region"))
    target_region = normalize_identity(candidate.get("target_region"))
    is_transfer = bool(source_region and target_region and source_region != target_region)

    if (
        local_payment
        and not _unverified_hypotheses(candidate)
        and len(evidence_independent_sources(evidence)) >= 2
    ):
        return "A", [], gates
    if is_transfer and not local_payment and _present(candidate.get("transfer_reason")):
        return "R", [], gates
    if demand_types & DEMAND_SIGNAL_TYPES:
        return "B", [], gates
    return None, ["missing_demand_or_local_payment_proof"], gates


def filter_ideas(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        envelope = validate_stage_envelope(payload)
    except ContractError as exc:
        raise FilterError(str(exc)) from exc
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise FilterError("输入必须包含 candidates 数组")
    benchmarks = payload.get("benchmarks") or []
    if not isinstance(benchmarks, list) or not all(isinstance(item, dict) for item in benchmarks):
        raise FilterError("benchmarks 必须是对象数组")
    expansion_summary = payload.get("summary") or {}
    if not isinstance(expansion_summary, dict):
        raise FilterError("扩展 summary 必须是对象")

    families: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise FilterError("candidates 中每项必须是对象")
        try:
            candidate_envelope = validate_stage_envelope(candidate)
            for field in ("run_id", "as_of"):
                if field in envelope and field in candidate_envelope and envelope[field] != candidate_envelope[field]:
                    raise ContractError(f"候选 {field} 与阶段运行不一致")
            fingerprint = fingerprint_record(candidate)
        except ContractError as exc:
            raise FilterError(str(exc)) from exc
        families.setdefault(fingerprint, []).append(candidate)

    buckets: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "R": [], "rejected": []}
    for fingerprint, family_candidates in families.items():
        evaluated = [(candidate, *classify_candidate(candidate)) for candidate in family_candidates]
        # 以单个独立合格的变体为代表，禁止拼凑不同变体的门槛或付款事实。
        candidate, tier, reasons, gates = min(evaluated, key=lambda row: {"A": 0, "B": 1, "R": 2, None: 3}[row[1]])
        value = deepcopy(candidate)
        value["opportunity_family"] = fingerprint
        value["variants"] = []
        seen_variants = set()
        for variant, variant_tier, variant_reasons, variant_gates in evaluated:
            variant = deepcopy(variant)
            variant.pop("variants", None)
            variant_id = variant.get("variant_id") or variant.get("candidate_id") or f"VAR-{canonical_sha256(variant)[:10].upper()}"
            marker = canonical_sha256(variant)
            if marker in seen_variants:
                continue
            seen_variants.add(marker)
            variant.update(variant_id=variant_id, evidence_tier=variant_tier, hard_gates=variant_gates)
            if variant_reasons:
                variant["rejection_reasons"] = variant_reasons
            value["variants"].append(variant)
        value["benchmark_ids"] = list(dict.fromkeys(identifier for item in family_candidates
                                                     if isinstance(item.get("benchmark_ids"), list)
                                                     for identifier in item["benchmark_ids"] if isinstance(identifier, str)))
        value["hard_gates"] = gates
        value["unverified_hypotheses"] = _unverified_hypotheses(value)
        if tier is None:
            value.pop("evidence_tier", None)
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
        "families": len(families),
        "tier_a": len(buckets["A"]),
        "tier_b": len(emitted_b),
        "tier_r": len(emitted_r),
        "tier_b_qualified": len(buckets["B"]),
        "tier_r_qualified": len(buckets["R"]),
        "rejected": len(buckets["rejected"]),
    }
    warnings: list[str] = []
    if expansion_summary.get("truncated") is True:
        warnings.append("扩展已达到数量或扫描上限：本次仅过滤已生成的候选，尚未确认是否仍有未生成组合")
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
        **envelope,
        "benchmarks": deepcopy(benchmarks),
        "policy": {"hard_gates": list(HARD_GATES), "tiers": ["A", "B", "R"]},
        "summary": counts,
        "expansion_summary": deepcopy(expansion_summary),
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
