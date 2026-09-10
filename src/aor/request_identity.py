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
        row["request_ids"].append(item["id"])
        for intent in item.get("intent_refs", []):
            if intent not in row["intent_refs"]:
                row["intent_refs"].append(intent)
        row["query_metadata"].append({
            key: value for key, value in item.items() if key not in {"source", "endpoint", "method", "params"}
        })
    return list(grouped.values())
