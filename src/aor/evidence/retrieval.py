"""只融合检索排名；不计算机会分数或商业证据等级。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .identity import canonical_evidence_url, canonical_sha256


def _identity(item: dict[str, Any]) -> str:
    return (canonical_evidence_url(item.get("original_url") or item.get("url"))
            or str(item.get("evidence_id") or item.get("id") or canonical_sha256(item)))


def _richness(item: dict[str, Any]) -> int:
    return sum(len(str(item.get(field) or "")) for field in ("original_text", "text", "comments"))


def reciprocal_rank_fusion(
    streams: Iterable[Iterable[dict[str, Any]]], *, k: int = 60, limit: int | None = None,
) -> list[dict[str, Any]]:
    """按 URL 融合多个有序查询流，保留正文较丰富的同修订记录和评论。"""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k 必须是正整数")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit 必须是正整数")
    merged: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    matches: dict[str, list[dict[str, int]]] = {}
    for stream_index, stream in enumerate(streams):
        seen: set[str] = set()
        for rank, item in enumerate(stream, 1):
            key = _identity(item)
            previous = merged.get(key)
            if previous is not None:
                revisions = {value.get("revision_id") for value in (previous, item)}
                if len(revisions) > 1:
                    raise ValueError("检索融合必须先按 as_of 选择同一证据的当前修订")
                if item.get("revision_id") and any(previous.get(field) != item.get(field) for field in
                                                   ("original_text", "text", "comments", "fact", "quote", "supporting_fact")):
                    raise ValueError("同一显式修订的原文或评论冲突，不能融合已绑定的引用位置")
            if previous is None:
                merged[key] = deepcopy(item)
            else:
                richer, other = (item, previous) if _richness(item) > _richness(previous) else (previous, item)
                combined = deepcopy(richer)
                metadata_fields = ("source_labels", "same_source_refs", "raw_refs", "aliases")
                merge_fields = metadata_fields if item.get("revision_id") else ("comments", *metadata_fields)
                for field in merge_fields:
                    values = []
                    markers = set()
                    for value in [*(richer.get(field) or []), *(other.get(field) or [])]:
                        marker = canonical_sha256(value)
                        if marker not in markers:
                            markers.add(marker)
                            values.append(deepcopy(value))
                    if values:
                        combined[field] = values
                # 只有尚未绑定修订的发现结果可以丰富正文；已绑定修订的字段和评论位置保持原样。
                if not item.get("revision_id"):
                    for field in ("original_text", "text"):
                        if field in previous or field in item:
                            combined[field] = max((previous.get(field) or "", item.get(field) or ""), key=len)
                labels = set(combined.get("source_labels") or [])
                labels.update(str(value["source"]) for value in (previous, item) if value.get("source"))
                if labels:
                    combined["source_labels"] = sorted(labels)
                merged[key] = combined
            if key not in seen:
                seen.add(key)
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
                matches.setdefault(key, []).append({"stream": stream_index, "rank": rank})
    ordered = sorted(merged, key=lambda key: -scores[key])
    results = []
    for key in ordered[:limit]:
        item = merged[key]
        item["retrieval"] = {"method": "rrf", "score": scores[key], "matches": matches[key],
                             "independent_source_count_inferred": False}
        results.append(item)
    return results
