"""串行付费请求恢复；HTTP 与供应商校验由兼容入口注入。"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable, Iterable

from aor.storage.request_journal import JournalError, RequestJournal


def select_attempts(
    items: list[dict[str, Any]], journal: RequestJournal, *, max_attempts: int,
    resolve_unknown: Iterable[str], retry_failed: Iterable[str],
) -> dict[str, int]:
    """授权仅匹配当前批具体请求，次数从同 run 的历史累计值扣除。"""
    if isinstance(resolve_unknown, str) or isinstance(retry_failed, str):
        raise JournalError("请求级授权必须为请求 id/指纹数组")
    unknown = set(resolve_unknown)
    failed = set(retry_failed)
    selector_targets: dict[str, set[str]] = defaultdict(set)
    for item in items:
        fingerprint = item["request_fingerprint"]
        for selector in {fingerprint, *item["request_ids"]}:
            selector_targets[selector].add(fingerprint)
    if any(len(selector_targets[selector]) > 1 for selector in unknown | failed):
        raise JournalError("请求 id 与其他请求指纹冲突；请使用不含歧义的请求 id 或指纹授权")
    matched: set[str] = set()
    allowed: dict[str, int] = {}
    for item in items:
        fingerprint = item["request_fingerprint"]
        previous = journal.get_request(fingerprint)
        selectors = {fingerprint, *item["request_ids"]}
        selected_unknown, selected_failed = selectors & unknown, selectors & failed
        matched.update(selected_unknown | selected_failed)
        if selected_unknown and previous["state"] != "outcome_unknown":
            raise JournalError("resolve_unknown 只能授权结果未知的请求")
        if selected_failed and previous["state"] != "failed":
            raise JournalError("retry_failed 只能授权已失败的请求")
        remaining = max(0, max_attempts - previous["attempts"])
        if (selected_unknown or selected_failed) and remaining == 0:
            raise JournalError("已达到累计 max_attempts，不能再次请求")
        allowed[fingerprint] = remaining if previous["state"] == "planned" or selected_unknown or selected_failed else 0
    if (unknown | failed) - matched:
        raise JournalError("请求级授权包含当前批次不存在的请求")
    return allowed


def execute_requests(
    items: list[dict[str, Any]], *, journal: RequestJournal, batch_id: str,
    allowances: dict[str, int], prices: dict[str, tuple[Decimal, Decimal]],
    max_attempts: int, max_cost_usd: Decimal, usd_to_cny: Decimal,
    pricing_snapshot: dict[str, Any], send: Callable[[dict[str, Any]], Any],
    sanitize: Callable[[Any], Any], classify_error: Callable[[Exception], str],
    is_retryable: Callable[[Exception], bool], sleep_func: Callable[[float], None],
    max_result_bytes: int,
) -> dict[str, Any]:
    """每次发送前记录 started，每次完成立即保存脱敏结果。"""
    results: list[dict[str, Any]] = []
    estimated_cost = Decimal(0)
    list_cost = Decimal(0)
    stored_bytes = 0
    stop_reason = None
    by_source: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "requests": 0, "ok": 0, "error": 0, "skipped": 0, "outcome_unknown": 0,
        "reused": 0, "attempts": 0, "estimated_attempted_cost_usd": Decimal(0),
    })

    for item in items:
        fingerprint = item["request_fingerprint"]
        previous = journal.get_request(fingerprint)
        result = {**sanitize(item), "attempts": 0, "estimated_attempted_cost_usd": 0.0,
                  "estimated_attempted_cost_usd_exact": "0", "list_attempted_cost_usd_exact": "0",
                  "reused": False, "cache": "none", "lifetime_attempts": previous["attempts"]}
        request_cost = Decimal(0)
        request_list_cost = Decimal(0)
        if not allowances[fingerprint]:
            saved = previous["result"] or {}
            for field in ("status", "response", "error", "error_code"):
                if field in saved:
                    result[field] = sanitize(saved[field])
            result["status"] = {"succeeded": "ok", "failed": "error", "outcome_unknown": "outcome_unknown"}[previous["state"]]
            if result["status"] == "outcome_unknown":
                result["error_code"] = "outcome_unknown"
                result["error"] = "前次请求可能已计费；需明确授权 resolve_unknown 才能重试"
            result.update(reused=True, cache="journal")
            if result["status"] == "ok":
                stored_bytes += len(json.dumps(result["response"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if stored_bytes > max_result_bytes:
                    result.pop("response")
                    result.update(status="error", error_code="batch_result_limit", error="复用结果超过批次安全上限")
                    stop_reason = "batch_result_limit"
        elif stop_reason:
            result.update(status="skipped", error_code="skipped_batch_limit", error="批次结果已达到安全上限，未发起请求")
        else:
            unit_list, unit_estimated = prices[fingerprint]
            for index in range(allowances[fingerprint]):
                attempt_id = journal.start_attempt(
                    fingerprint, batch_id=batch_id, list_cost_usd=unit_list, estimated_cost_usd=unit_estimated,
                    pricing_snapshot=pricing_snapshot, max_cost_usd=max_cost_usd, max_attempts=max_attempts,
                )
                result["attempts"] += 1
                result["lifetime_attempts"] += 1
                request_cost += unit_estimated
                request_list_cost += unit_list
                estimated_cost += unit_estimated
                list_cost += unit_list
                result.update(
                    estimated_attempted_cost_usd=float(round(request_cost, 6)),
                    estimated_attempted_cost_usd_exact=str(request_cost),
                    list_attempted_cost_usd_exact=str(request_list_cost),
                )
                retry = False
                try:
                    response = send(item)
                except Exception as exc:
                    error_code = classify_error(exc)
                    # 未收到可确认的完整响应不能证明请求未执行，普通恢复也不自动再次计费。
                    uncertain = error_code in {"timeout", "network_error", "invalid_response", "response_too_large"}
                    state = "outcome_unknown" if uncertain else "failed"
                    result.update(status=state if uncertain else "error", error_code=error_code,
                                  error=str(sanitize(str(exc)))[:500])
                    retry = not uncertain and is_retryable(exc) and index + 1 < allowances[fingerprint]
                else:
                    try:
                        cleaned = sanitize(response)
                        size = len(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                    except Exception:
                        state = "failed"
                        result.update(status="error", error_code="response_sanitization_error", error="TikHub 响应清洗失败")
                    else:
                        if stored_bytes + size > max_result_bytes:
                            state = "failed"
                            stop_reason = "batch_result_limit"
                            result.update(status="error", error_code=stop_reason, error="TikHub 批次结果超过 32 MiB 安全上限")
                        else:
                            state = "succeeded"
                            stored_bytes += size
                            result.update(status="ok", response=cleaned)
                            result.pop("error", None)
                            result.pop("error_code", None)
                journal.finish_attempt(attempt_id, state=state, result=result)
                if not retry:
                    break
                sleep_func(min(2 ** index, 5))
        results.append(result)
        source = by_source[item["source"]]
        source["requests"] += 1
        source[result["status"]] += 1
        source["reused"] += int(result["reused"])
        source["attempts"] += result["attempts"]
        source["estimated_attempted_cost_usd"] += request_cost

    source_rows = [{**row, "source": source,
                    "estimated_attempted_cost_usd": float(round(row["estimated_attempted_cost_usd"], 6))}
                   for source, row in sorted(by_source.items())]
    summary = {key: sum(row[key] for row in source_rows)
               for key in ("requests", "ok", "error", "skipped", "outcome_unknown", "reused", "attempts")}
    summary.update({
        "logical_requests": sum(len(item["request_ids"]) for item in items),
        "stopped_early": stop_reason is not None, "stop_reason": stop_reason, "stored_result_bytes": stored_bytes,
        "estimated_attempted_cost_usd": float(round(estimated_cost, 6)),
        "estimated_attempted_cost_usd_exact": str(estimated_cost),
        "list_attempted_cost_usd_exact": str(list_cost),
        "estimated_attempted_cost_cny": float(round(estimated_cost * usd_to_cny, 6)),
        "by_source": source_rows, "billing_note": "实际扣费以 TikHub 使用日志与账单为准",
    })
    return {"results": results, "summary": summary, "run_ledger": journal.snapshot()}
