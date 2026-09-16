"""有界评论分页与回复树计划，所有 HTTP 请求仍经过现有付费账本。"""

from copy import deepcopy

from contracts import validate_run_as_of
from aor.request_identity import request_fingerprint

SOURCES = {"xiaohongshu", "douyin", "twitter"}
DEFAULT_POLICY = {"max_pages": 3, "max_reply_pages": 2, "max_reply_threads": 5,
                  "max_comments_per_post": 100, "max_requests": 150}
POLICY_LIMITS = {"max_pages": 20, "max_reply_pages": 10, "max_reply_threads": 30,
                 "max_comments_per_post": 2000, "max_requests": 1000}


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or set(policy) - set(DEFAULT_POLICY):
        raise ValueError("评论采集 policy 含未知参数")
    merged = {**DEFAULT_POLICY, **policy}
    for key, upper in POLICY_LIMITS.items():
        if type(merged[key]) is not int or not 1 <= merged[key] <= upper:
            raise ValueError(f"{key} 必须为 1–{upper} 的整数")
    return merged


def _identify(item: dict) -> dict:
    item["id"] = "comments-" + request_fingerprint(item)[:24]
    return item


def collection_plan(run_id: str, as_of: str, requests: list, policy: dict) -> dict:
    return {"schema_version": "3.0", "provider": "tikhub", "run_id": run_id, "as_of": as_of,
            "stage": "comment_deep_dive", "parent_search_run_id": run_id, "collection_version": "2.0",
            "scope": {"id": "phase_1_existing_platforms", "platform_expansion_enabled": False},
            "collection_policy": validate_policy(policy), "requests": requests,
            "cost_policy": {"price_source": "live_dashboard_before_run", "max_attempts": 1,
                            "shared_run_budget": True, "one_page_only": False}}


def start_collection(payload: dict, *, run_id: str, as_of: str) -> dict:
    from tikhub_query import build_comment_plan
    validate_run_as_of(run_id, as_of)
    if not isinstance(payload, dict) or set(payload) - {"selections", "policy"}:
        raise ValueError("评论输入仅支持 selections 和 policy")
    selected = payload.get("selections")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 30:
        raise ValueError("评论采集需选择 1–30 个帖子")
    policy = validate_policy(payload.get("policy", {}))
    requests, seen = [], {}
    for selection in selected:
        if not isinstance(selection, dict) or selection.get("source") not in SOURCES:
            raise ValueError("批量评论采集仅支持小红书、抖音和 X")
        pair = build_comment_plan(as_of=as_of, parent_search_run_id=run_id, selections=[selection])["requests"]
        product = selection.get("product")
        if not isinstance(product, str) or not 1 <= len(product.strip()) <= 200:
            raise ValueError("评论选择需要 product，关联产品的实际语义由宿主核验")
        for item in pair:
            item["collection"] = {"product": product.strip(), "page": 1, "sort": "relevance", "parent_comment_id": None}
            item["industry_ids"] = deepcopy(selection.get("industry_ids", []))
            _identify(item)
            if item["id"] in seen and seen[item["id"]] != product.strip():
                raise ValueError("同一帖子关联多个产品，请拆分集合并分别核验产品关联")
            if item["id"] not in seen:
                requests.append(item)
                seen[item["id"]] = product.strip()
            if item["source"] == "twitter" and item["kind"] == "top_level_comments":
                latest = deepcopy(item)
                latest["endpoint"] = "/api/v1/twitter/web/fetch_latest_post_comments"
                latest["collection"]["sort"] = "latest"
                _identify(latest)
                if latest["id"] not in seen:
                    requests.append(latest)
                    seen[latest["id"]] = product.strip()
    if len(requests) > policy["max_requests"]:
        raise ValueError("首批评论请求超过 max_requests，请减少帖子数或明确提高容量")
    return {"version": "1.0", "run_id": run_id, "as_of": as_of, "policy": policy, "pending": requests,
            "processed": [], "seen_comments": {}, "reply_threads": {}, "pages": [], "exhaustive": False}


def page_metadata(data: dict) -> dict:
    """只读容器层的分页元数据，避免误用某条评论内部的回复游标。"""
    containers, current = [], data
    for _ in range(5):
        if not isinstance(current, dict):
            break
        containers.append(current)
        child = current.get("data")
        if not isinstance(child, dict):
            child = current.get("result")
        current = child
    meta = {"next_cursor": None, "has_more": None, "reported_total": None}
    for box in containers:
        for key in ("has_more", "hasMore", "has_next_page"):
            if key in box and type(box[key]) in (bool, int) and box[key] in (0, 1):
                meta["has_more"] = bool(box[key])
        for key in ("cursor", "max_cursor", "next_cursor", "nextCursor"):
            if key in box and isinstance(box[key], (str, int)) and not isinstance(box[key], bool):
                meta["next_cursor"] = box[key]
        for key in ("total", "total_count", "comment_count"):
            if type(box.get(key)) is int and box[key] >= 0:
                meta["reported_total"] = box[key]
    # X 原始时间线的 Bottom 游标只用于当前响应，Thread 游标不混作分页。
    def visit(value, depth=0):
        if depth > 24:
            return
        if isinstance(value, dict):
            if value.get("cursorType") == "Bottom" and isinstance(value.get("value"), str):
                meta["next_cursor"] = value["value"]
            for key, child in value.items():
                if key not in {"user", "author", "legacy", "comments", "replies", "sub_comments"}:
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
    visit(data)
    return meta


def _reply_request(item: dict, comment_id: str) -> dict | None:
    source, params = item["source"], item["params"]
    reply = deepcopy(item)
    if source == "xiaohongshu":
        reply["endpoint"] = "/api/v1/xiaohongshu/app_v2/get_note_sub_comments"
        reply["params"] = {k: v for k, v in params.items() if k in {"note_id", "share_text"}}
        reply["params"]["comment_id"] = comment_id
    elif source == "douyin":
        reply["endpoint"] = "/api/v1/douyin/web/fetch_video_comment_replies"
        reply["params"] = {"item_id": params["aweme_id"], "comment_id": comment_id}
    else:
        return None
    reply["kind"] = "comment_replies"
    reply["collection"].update(page=1, parent_comment_id=comment_id)
    return _identify(reply)


def advance_collection(state: dict, execution: dict) -> dict:
    """消费一批已经保存的执行结果，产生可恢复的下一批，不发送网络。"""
    from normalize_tikhub_results import _extract_comment_items, _comment_id, _is_twitter_comment, _upstream_error
    result = deepcopy(state)
    pending = {r["id"]: r for r in result["pending"][:100]}
    next_requests = deepcopy(result["pending"][100:])
    seen_requests = set(result["processed"]) | {request_fingerprint(v) for v in result["pending"]}
    policy = result["policy"]
    responses = {r["id"]: r for r in execution.get("results", [])}
    for identifier, item in pending.items():
        fingerprint = request_fingerprint(item)
        response = responses.get(identifier, {})
        result["processed"].append(fingerprint)
        record = {"request_id": identifier, "source": item["source"], "post_id": item["selected_item_id"],
                  "kind": item["kind"], **item["collection"], "unique_comments_added": 0,
                  "stop_reason": None, "next_cursor": None, "has_more": None, "reported_total": None}
        result["pages"].append(record)
        if response.get("status") != "ok" or _upstream_error(response.get("response")):
            record["stop_reason"] = response.get("status") if response.get("status") != "ok" else "upstream_error"
            record["stop_reason"] = record["stop_reason"] or "missing_result"
            continue
        if item["kind"] == "detail":
            record["stop_reason"] = "detail_saved"
            continue
        data = (response.get("response") or {}).get("data")
        if not isinstance(data, dict):
            record["stop_reason"] = "unrecognized_response"
            continue
        rows = _extract_comment_items(data, item["selected_item_id"])
        if item["source"] == "twitter":
            rows = [r for r in rows if _is_twitter_comment(r, item["selected_item_id"])]
        record.update(page_metadata(data))
        post_key = item["source"] + ":" + item["selected_item_id"]
        known = set(result["seen_comments"].get(post_key, []))
        ids = {_comment_id(r, r.get("_parent_comment_id"), item["selected_item_id"]) for r in rows}
        record["unique_comments_added"] = len(ids - known)
        known.update(ids)
        result["seen_comments"][post_key] = sorted(known)
        page_limit = policy["max_reply_pages"] if item["kind"] == "comment_replies" else policy["max_pages"]
        cursor = record["next_cursor"]
        if not rows:
            record["stop_reason"] = "empty_page" if record["has_more"] is False else "empty_or_unrecognized_page"
        elif len(known) >= policy["max_comments_per_post"]:
            record["stop_reason"] = "comment_limit"
        elif record["has_more"] is False:
            record["stop_reason"] = "provider_end"
        elif item["collection"]["page"] >= page_limit:
            record["stop_reason"] = "page_limit"
        elif cursor in (None, ""):
            record["stop_reason"] = "pagination_unknown"
        else:
            next_item = deepcopy(item)
            next_item["params"]["cursor"] = cursor
            next_item["collection"]["page"] += 1
            _identify(next_item)
            if request_fingerprint(next_item) in seen_requests or str(cursor) == str(item["params"].get("cursor")):
                record["stop_reason"] = "repeated_cursor"
            else:
                next_requests.append(next_item)
                seen_requests.add(request_fingerprint(next_item))
                record["stop_reason"] = "next_page_planned"
        if item["kind"] == "top_level_comments" and len(known) < policy["max_comments_per_post"]:
            threads = result["reply_threads"].setdefault(post_key, [])
            for row in rows:
                count = row.get("sub_comment_count", row.get("reply_count", 0))
                if row.get("_parent_comment_id") or not str(count).isdigit() or int(count) < 1:
                    continue
                cid = _comment_id(row, None, item["selected_item_id"])
                if cid in threads or len(threads) >= policy["max_reply_threads"]:
                    continue
                reply = _reply_request(item, cid)
                if reply and request_fingerprint(reply) not in seen_requests:
                    threads.append(cid)
                    next_requests.append(reply)
                    seen_requests.add(request_fingerprint(reply))
    capacity = max(0, policy["max_requests"] - len(result["processed"]))
    result["pending"] = next_requests[:capacity]
    result["deferred_request_count"] = result.get("deferred_request_count", 0) + max(0, len(next_requests) - capacity)
    result["status"] = "awaiting_next_page" if result["pending"] else "completed"
    result["summary"] = {"requests": len(result["processed"]),
                         "unique_comments": sum(len(v) for v in result["seen_comments"].values()),
                         "deferred_request_count": result["deferred_request_count"], "exhaustive": False}
    return result


def collection_coverage(executions: list) -> dict:
    """从实际保存的响应重建采集计数；计划或历史复用不增加本轮覆盖。"""
    from normalize_tikhub_results import normalize_documents
    results, seen = [], set()
    for execution in executions:
        selected = []
        for item in execution.get("results", []):
            if not item.get("collection") or item.get("status") == "skipped":
                continue
            identity = request_fingerprint(item)
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(item)
        if selected:
            results.append({**execution, "results": selected})
    if not results:
        return {"requests": 0, "unique_comments": 0, "sources": {}, "exhaustive": False}
    parsed = normalize_documents(results)
    sources = {}
    for execution in results:
        for item in execution["results"]:
            row = sources.setdefault(item["source"], {"requests": 0, "comment_pages": 0, "unique_comments": 0})
            row["requests"] += 1
            row["comment_pages"] += int(item.get("kind") != "detail" and item.get("status") == "ok")
    for comment in parsed["comments"]:
        sources[comment["source"]]["unique_comments"] += 1
    return {"requests": len(seen), "unique_comments": len(parsed["comments"]), "sources": sources, "exhaustive": False}
