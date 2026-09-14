"""只融合检索排名；不计算机会分数或商业证据等级。"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable

from .identity import canonical_evidence_url, canonical_sha256, evidence_identity_key, evidence_object_identity


class EvidenceReferenceError(ValueError):
    """引用缺失、歧义或与原文冲突；不能通过换成另一条材料静默恢复。"""


def _text_matches(reference: dict[str, Any], record: dict[str, Any]) -> bool:
    for field in ("original_text", "text"):
        value = reference.get(field)
        if isinstance(value, str) and value.strip() and value != record.get(field):
            return False
    quote = reference.get("quote")
    if isinstance(quote, str) and quote.strip():
        # 带路径的引用由 claims._locate 校验字段和位置；这里也不允许路径越界回退。
        field = reference.get("field")
        if field:
            value: Any = record
            for part in re.sub(r"\[(\d+)\]", r".\1", str(field)).split("."):
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                    value = value[int(part)]
                else:
                    return False
            return isinstance(value, str) and quote in value
        return any(quote in str(record.get(key) or "") for key in ("original_text", "text"))
    return True


def resolve_evidence_reference(reference: dict[str, Any], records: Iterable[dict[str, Any]], *,
                               require_revision: bool = False) -> dict[str, Any]:
    """纯函数解析一个引用，返回副本；显式 ID／修订找不到时绝不降级为 URL。

    native identity 优先于 URL。迁移前旧 ID 只有精确旧 revision 可定位；URL
    多义时必须附准确原文或 quote 缩小到一条。此检查不判断摘要的商业语义。
    require_revision=True 保持 claims 的严格版本绑定，兼容双方都无修订的旧证据。
    """
    if not isinstance(reference, dict):
        raise EvidenceReferenceError("证据引用必须是对象")
    if (reference.get("library_evidence_id") and reference.get("evidence_id")
            and reference["library_evidence_id"] != reference["evidence_id"]):
        raise EvidenceReferenceError("引用的 evidence_id 与 library_evidence_id 冲突")
    catalog: dict[tuple[str, str | None], dict[str, Any]] = {}
    for item in records:
        marker = (str(item.get("evidence_id") or item.get("id") or evidence_identity_key(item)), item.get("revision_id"))
        if marker in catalog and any(catalog[marker].get(field) != item.get(field)
                                     for field in ("original_text", "text", "comments")):
            raise EvidenceReferenceError("同一证据修订存在冲突原文")
        if marker in catalog:
            combined = deepcopy(item)
            combined["aliases"] = sorted(set(item.get("aliases") or []) | set(catalog[marker].get("aliases") or []))
            combined["legacy_references"] = list({canonical_sha256(ref): ref for current in (catalog[marker], item)
                for ref in current.get("legacy_references") or []}.values())
            catalog[marker] = combined
        else:
            catalog[marker] = item
    candidates = list(catalog.values())
    identity = reference.get("library_evidence_id") or reference.get("evidence_id") or reference.get("id")
    native = evidence_object_identity(reference)
    revision = reference.get("revision_id")
    if identity:
        direct = [item for item in candidates if identity in {item.get("evidence_id"), item.get("library_evidence_id"),
                  item.get("id"), *(item.get("aliases") or [])}]
        legacy = [item for item in candidates if any(ref.get("evidence_id") == identity
                  and revision is not None and ref.get("revision_id") == revision
                  for ref in item.get("legacy_references") or [])]
        # 旧 URL 级 ID 必须带版本，不把混合身份的旧别名当作某条评论的唯一别名。
        candidates = list({(item.get("evidence_id") or item.get("id"), item.get("revision_id")): item
                           for item in [*direct, *legacy]}.values())
        if revision is not None or require_revision:
            candidates = [item for item in candidates if item.get("revision_id") == revision or any(
                old.get("evidence_id") == identity and old.get("revision_id") == revision
                for old in item.get("legacy_references") or [])]
    elif native:
        candidates = [item for item in candidates if evidence_object_identity(item) == native]
        if revision is not None or require_revision:
            candidates = [item for item in candidates if item.get("revision_id") == revision]
    elif revision is not None:
        candidates = [item for item in candidates if item.get("revision_id") == revision]
    else:
        url = canonical_evidence_url(reference.get("original_url") or reference.get("url"))
        if not url:
            raise EvidenceReferenceError("引用缺少可解析的证据身份或 URL")
        candidates = [item for item in candidates if url in {
            canonical_evidence_url(item.get("original_url") or item.get("url")),
            *(canonical_evidence_url(value) for value in item.get("same_source_refs") or [])}]
        if require_revision:
            candidates = [item for item in candidates if item.get("revision_id") is None]
    if native:
        candidates = [item for item in candidates if evidence_object_identity(item) == native]
    candidates = [item for item in candidates if _text_matches(reference, item)]
    if not candidates:
        raise EvidenceReferenceError("证据引用不存在、修订不一致或原文不匹配")
    if len(candidates) != 1:
        raise EvidenceReferenceError("证据引用不唯一；请提供原生对象 ID、精确修订或准确原文")
    return deepcopy(candidates[0])


def _identity(item: dict[str, Any]) -> str:
    return (evidence_identity_key(item)
            or str(item.get("evidence_id") or item.get("id") or canonical_sha256(item)))


def _richness(item: dict[str, Any]) -> int:
    return sum(len(str(item.get(field) or "")) for field in ("original_text", "text", "comments"))


def reciprocal_rank_fusion(
    streams: Iterable[Iterable[dict[str, Any]]], *, k: int = 60, limit: int | None = None,
) -> list[dict[str, Any]]:
    """按原生对象／网页身份融合查询流，保留独立评论和丰富的同修订正文。"""
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
