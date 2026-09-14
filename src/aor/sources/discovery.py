"""基于实时价格生成有界跨行业发现批次，不绕过现有执行账本。"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal

from aor.sources.industries import industry_ids


def prepare_discovery_plan(plan: dict, pricing: list[dict], *, max_cost_usd: float,
                           prior_cost_usd: str = "0", max_requests: int = 12) -> dict:
    from tikhub_query import estimate_plan, normalize_pricing_rows, enforce_budget

    if (plan.get("stage") != "search_discovery" or
            (plan.get("cost_policy") or {}).get("purpose") != "cross_industry_discovery"):
        raise ValueError("需要带行业目标的跨行业发现计划")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or not 1 <= max_requests <= 100:
        raise ValueError("发现批次最多 1–100 个请求")
    enforce_budget({"budget_guard_cost_usd": prior_cost_usd, "worst_case_cost_usd": prior_cost_usd}, max_cost_usd)
    catalog = normalize_pricing_rows(pricing)
    result = deepcopy(plan)
    result["requests"], skipped = [], []
    counts = Counter()
    remaining = list(plan["requests"])
    while remaining:
        item = min(remaining, key=lambda r: (min(counts[k] for k in industry_ids(r) or ["unknown"]), r["id"]))
        remaining.remove(item)
        reason = None
        if item["endpoint"] not in catalog:
            reason = "live_price_unavailable"
        elif len(result["requests"]) >= max_requests:
            reason = "batch_limit"
        else:
            probe = {**result, "requests": [*result["requests"], item]}
            estimate = estimate_plan(probe, pricing)
            total = Decimal(prior_cost_usd) + Decimal(str(estimate["worst_case_cost_usd"]))
            if total > Decimal(str(max_cost_usd)):
                reason = "budget_limit"
        if reason:
            skipped.append({"id": item["id"], "source": item["source"], "industry_ids": industry_ids(item), "reason": reason})
        else:
            result["requests"].append(item)
            counts.update(industry_ids(item) or ["unknown"])
    result["skipped_requests"] = skipped
    result["budget_selection"] = {"run_limit_usd": max_cost_usd, "prior_cost_usd": prior_cost_usd,
                                  "max_requests": max_requests, "selected_requests": len(result["requests"])}
    if not result["requests"]:
        raise ValueError("当前价格、余额预算或批次范围下没有可执行的发现请求；未调用付费端点")
    result["preflight_estimate"] = estimate_plan(result, pricing)
    return result
