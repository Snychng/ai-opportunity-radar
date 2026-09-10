"""以实际 HTTP 请求识别重复查询，同时保留各研究意图。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_fingerprint(item: dict[str, Any]) -> str:
    """GET 与 urlencode 一致地忽略 None、转成字符串；POST 保留 JSON 类型。"""
    method = item["method"].upper()
    params = item["params"]
    if method == "GET":
        params = {key: str(value) for key, value in params.items() if value is not None}
    return canonical_sha256({
        "source": item["source"].lower(), "endpoint": item["endpoint"], "method": method, "params": params,
    })


def deduplicate_requests(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """输出一行实际请求；query_metadata 保留每条输入的查询与缺口字段。"""
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        fingerprint = request_fingerprint(item)
        if fingerprint not in grouped:
            grouped[fingerprint] = {
                **item, "source": item["source"].lower(), "method": item["method"].upper(),
                "request_fingerprint": fingerprint, "request_ids": [], "intent_refs": [], "query_metadata": [],
            }
        row = grouped[fingerprint]
        for request_id in [item["id"], *item.get("request_ids", []), *item.get("request_aliases", [])]:
            if request_id not in row["request_ids"]:
                row["request_ids"].append(request_id)
        for intent in item.get("intent_refs", []):
            if intent not in row["intent_refs"]:
                row["intent_refs"].append(intent)
        metadata = item.get("query_metadata") or [{
            key: value for key, value in item.items()
            if key not in {"source", "endpoint", "method", "params", "query_metadata"}
        }]
        for value in metadata:
            if value not in row["query_metadata"]:
                row["query_metadata"].append(value)
    return list(grouped.values())
