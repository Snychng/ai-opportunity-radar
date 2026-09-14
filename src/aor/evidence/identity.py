"""证据与业务实体共享的稳定标识基础函数。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# 这些字段描述采集、归类或引用，不属于发布者原文。重新查询和归类不能制造正文修订。
EVIDENCE_METADATA_FIELDS = {
    "derivation_refs", "source_execution_sha256", "derive_set_id", "derivation_status", "derivation_reason",
    "historical_reference_only", "superseded_by_derive_sets",
    "id", "evidence_id", "revision_id", "version", "state_revision_id", "supersedes", "source", "source_labels",
    "run_id", "run_ids", "as_of", "observed_at", "first_observed_at", "last_observed_at", "recorded_on",
    "reused_for_run_id", "raw_file", "raw_ref", "raw_refs", "raw_json_pointer", "query", "query_id", "query_ids",
    "query_group", "query_groups", "query_language", "query_region", "query_scope", "engagement", "access_method",
    "extraction_warnings", "retrieval", "aliases", "same_source_refs", "independent_source_key", "schema_version",
    "canonical_url", "content_hash", "library_evidence_id", "identity_version", "object_identity", "legacy_references",
    "industry_ids", "industry_id", "evidence_role", "relevance_status", "relevance_reason", "relevance_matches",
    "window_status", "date_confidence", "date_interval", "date_precision", "signal_types", "evidence_gap",
    "comment_candidate", "host_attested", "verification", "verification_status", "review_status", "reviewed_at",
    "normalized_at", "parser_version", "zh_translation", "source_item_id", "source_object_id", "object_kind",
    "object_type", "evidence_kind", "platform", "origin_id", "parent_item_id", "parent_comment_id", "container",
    "identifiers", "request_ids", "request_links", "queries", "query_languages", "query_regions",
    "quality_version", "local_relevance", "matched_terms", "task_anchor_terms", "lexical_status", "relevance_basis",
    "semantic_relevance_status", "requires_semantic_review", "recent_lexical_match", "recent_semantically_verified",
    "recent_evidence_eligible", "relevance_review", "duplicate_of", "published_at_interval", "published_at_raw",
    "query_metadata", "intent_refs", "subtrack_ids", "task_ids", "task_id", "semantic_review", "reviewed_by",
    "historical_reference_only", "current_revision_id",
    "historical_import",
}


def evidence_object_identity(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """返回平台、对象类型、原生 ID；普通网页和不透明采集 ID 不冒充平台对象。

    评论即使只有父帖 URL 也以评论 ID 定位。规范化器已有 source_item_id；旧
    记录可从 source:comment:id 恢复。转载显式 original_url 指向原出处时继续按
    原网址去重，除非提供 original_object_identity 指明原始平台对象。
    """
    original = record.get("original_object_identity")
    if isinstance(original, dict) and all(original.get(key) for key in ("source", "kind", "id")):
        return str(original["source"]).casefold(), str(original["kind"]).casefold(), str(original["id"])
    if record.get("original_url") and canonical_evidence_url(record["original_url"]) != canonical_evidence_url(record.get("url")):
        return None
    stored = record.get("object_identity")
    if isinstance(stored, dict) and all(stored.get(key) for key in ("source", "kind", "id")):
        return str(stored["source"]).casefold(), str(stored["kind"]).casefold(), str(stored["id"])
    source = str(record.get("platform") or record.get("source") or "").strip().casefold()
    native_id = record.get("source_object_id") or record.get("source_item_id")
    kind = str(record.get("object_kind") or record.get("object_type") or record.get("evidence_kind") or "").casefold()
    raw_id = str(record.get("id") or "")
    comment_alias = re.fullmatch(r"([^:]+):comment:(.+)", raw_id)
    if comment_alias:
        source, kind = source or comment_alias[1].casefold(), "comment"
        native_id = native_id or comment_alias[2]
    if record.get("url_kind") in {"comment", "parent_post"} or record.get("parent_comment_id"):
        kind = "comment"
    if not native_id:
        # 已有规范化帖子使用 source:id；web 的任意 id 仍只按 URL 合并。
        if source and raw_id.startswith(source + ":") and kind:
            native_id = raw_id[len(source) + 1:]
        else:
            return None
    if not source:
        return None
    kind = "comment" if kind in {"comment", "reply"} else ("post" if kind in {"", "post_detail"} else kind)
    return source, kind, str(native_id)


def evidence_identity_key(record: dict[str, Any]) -> str:
    """稳定对象键。URL 只是没有原生身份时的网页回退，不用作评论父帖身份。"""
    native = evidence_object_identity(record)
    if native:
        return "object:" + json.dumps(native, ensure_ascii=False, separators=(",", ":"))
    url = canonical_evidence_url(record.get("original_url") or record.get("url"))
    if record.get("url_kind") == "parent_post":
        raise ValueError("评论只有父帖 URL 且缺少原生对象 ID，不能建立证据身份")
    return url


def evidence_content(record: dict[str, Any]) -> dict[str, Any]:
    """规范原文修订内容，保持事实／正文变化可见并排除派生采集元数据。"""
    content = {key: value for key, value in record.items() if key not in EVIDENCE_METADATA_FIELDS}
    canonical_url = canonical_evidence_url(record.get("original_url") or record.get("url"))
    if evidence_object_identity(record):
        for key in ("url", "original_url", "parent_url", "url_kind", "original_object_identity"):
            content.pop(key, None)
    else:
        content["url"] = canonical_url
        if "original_url" in content:
            content["original_url"] = canonical_url
    return content


def canonical_sha256(value: Any) -> str:
    """对 JSON 兼容对象生成稳定摘要。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_identity(value: Any) -> str:
    """规范化用于机会身份判断的文本。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def canonical_evidence_url(value: Any) -> str:
    """规范可追溯网页地址，移除片段和常见追踪参数而保留内容参数。"""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.lower().removeprefix("www.")
        port = parsed.port
        netloc = hostname if port in (None, 80, 443) else f"{hostname}:{port}"
        query = urlencode(sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                                 if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}))
        return urlunparse(("https", netloc, parsed.path.rstrip("/"), "", query, ""))
    except ValueError:
        return ""
