"""基于实时价格生成有界跨行业发现批次；替补也来自已有端点和同一预算账本。"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal

from aor.sources.industries import industry_ids


def prepare_discovery_plan(plan: dict, pricing: list[dict], *, max_cost_usd: float,
                           prior_cost_usd: str = "0", max_requests: int = 12) -> dict:
    from tikhub_query import estimate_plan, normalize_pricing_rows, enforce_budget, RADAR_ENDPOINTS
    from aor.sources.planning import deduplicate_requests

    if (plan.get("stage") != "search_discovery" or
            (plan.get("cost_policy") or {}).get("purpose") != "cross_industry_discovery"):
        raise ValueError("需要带行业目标的跨行业发现计划")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or not 1 <= max_requests <= 100:
        raise ValueError("发现批次最多 1–100 个请求")
    enforce_budget({"budget_guard_cost_usd": prior_cost_usd, "worst_case_cost_usd": prior_cost_usd}, max_cost_usd)
    catalog = normalize_pricing_rows(pricing)
    result = deepcopy(plan)
    result["requests"], skipped, substitutions = [], deepcopy(plan.get("skipped_requests", [])), []
    counts = Counter()
    remaining = list(plan["requests"])
    while remaining:
        item = min(remaining, key=lambda r: (min(counts[k] for k in industry_ids(r) or ["unknown"]),
                                            r.get("research_priority", 1), r["id"]))
        remaining.remove(item)
        alternatives = [item, *item.get("fallback_requests", [])]
        selected, declined = None, []
        for alternative in alternatives:
            source = alternative.get("source")
            expected = RADAR_ENDPOINTS.get(source, {})
            reason = None
            if (not expected or alternative.get("endpoint") != expected.get("endpoint") or
                    alternative.get("method") != expected.get("method")):
                reason = "unregistered_search_capability"
            elif alternative.get("query_scope") != item.get("query_scope") or alternative.get("query_group") != item.get("query_group"):
                reason = "fallback_scope_mismatch"
            elif alternative["params"].get(expected["keyword_param"]) != item["params"].get(RADAR_ENDPOINTS[item["source"]]["keyword_param"]):
                reason = "fallback_query_mismatch"
            elif alternative["endpoint"] not in catalog:
                reason = "live_price_unavailable"
            elif len(result["requests"]) >= max_requests:
                reason = "batch_limit"
            else:
                probe = {**result, "requests": [*result["requests"], alternative]}
                estimate = estimate_plan(probe, pricing)
                total = Decimal(prior_cost_usd) + Decimal(str(estimate["worst_case_cost_usd"]))
                if total > Decimal(str(max_cost_usd)):
                    reason = "budget_limit"
            if reason:
                declined.append({"id": alternative["id"], "source": source, "industry_ids": industry_ids(item),
                                 "task_id": item.get("task_id"), "query_scope": item.get("query_scope"),
                                 "reason": reason})
                if reason == "batch_limit":
                    break
            else:
                selected = deepcopy(alternative)
                selected.pop("fallback_requests", None)
                if selected["id"] != item["id"]:
                    selected["fallback_for_request_id"] = item["id"]
                    selected["request_aliases"] = list(dict.fromkeys([selected["id"], *selected.get("request_aliases", []), item["id"]]))
                    substitutions.append({"planned_request_id": item["id"], "planned_source": item["source"],
                                          "selected_request_id": selected["id"], "selected_source": selected["source"],
                                          "task_id": item.get("task_id"), "industry_ids": industry_ids(item),
                                          "reason": declined[0]["reason"] if declined else "same_task_registered_fallback"})
                break
        for rejected in declined:
            if selected:
                rejected["replacement_request_id"] = selected["id"]
                rejected["replacement_source"] = selected["source"]
            skipped.append(rejected)
        if selected:
            result["requests"].append(selected)
            counts.update(industry_ids(item) or ["unknown"])
    result["requests"] = deduplicate_requests(result["requests"])
    result["skipped_requests"] = skipped
    result["substitutions"] = substitutions
    result["budget_selection"] = {"version": "2.0", "run_limit_usd": max_cost_usd, "prior_cost_usd": prior_cost_usd,
                                  "max_requests": max_requests, "planned_requests": len(plan["requests"]),
                                  "selected_requests": len(result["requests"]), "substitution_count": len(substitutions)}
    if not result["requests"]:
        raise ValueError("当前价格、余额预算或批次范围下没有可执行的发现请求；未调用付费端点")
    result["preflight_estimate"] = estimate_plan(result, pricing)
    return result
