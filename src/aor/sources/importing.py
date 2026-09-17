"""导入宿主已打开核验的网页摘录，保留原文、核验声明和内容摘要。"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from .planning import text_field
from .registry import EVIDENCE_ROLES, source_catalog
from .search_candidates import x_post_id


def _timestamp(value: Any, field: str) -> str:
    value = text_field(value, field, 80)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 必须为 ISO 8601 时间戳") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return value


def _public_url(value: Any) -> str:
    value = text_field(value, "url", 4000)
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError as exc:
        raise ValueError("url 必须为不含登录凭证的 HTTP(S) 页面地址") from exc
    if not valid:
        raise ValueError("url 必须为不含登录凭证的 HTTP(S) 页面地址")
    return value


def import_web_evidence(payload: Any, *, run_id: str, as_of: str) -> dict[str, Any]:
    """消费 {items:[...]} 并返回标准 evidence 列表；不抓取网页、不推断已付款。"""
    from contracts import SCHEMA_VERSION, canonical_sha256, validate_run_as_of
    from tikhub_query import validate_query_locale

    validate_run_as_of(run_id, as_of)
    if not isinstance(payload, dict) or set(payload) != {"items"}:
        raise ValueError("导入文件必须只包含 items 数组")
    if not isinstance(payload["items"], list) or not 1 <= len(payload["items"]) <= 100:
        raise ValueError("单次导入必须包含 1 到 100 条网页摘录")
    required = {"source", "url", "title", "original_text", "supporting_quote", "evidence_role", "observed_at", "verification"}
    optional = {"published_at", "language", "country", "original_url", "original_publisher", "intent_refs", "candidate_gaps", "is_demo", "industry_ids",
                "source_object_id", "object_kind", "query", "query_id", "query_language", "query_region", "retrieval"}
    sources = {row["source"] for row in source_catalog()}
    evidence = []
    seen = set()
    for raw in payload["items"]:
        if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
            raise ValueError("网页摘录字段必须符合导入契约，未知证据字段不会直接透传")
        source = text_field(raw["source"], "source", 80)
        if source not in sources:
            raise ValueError("未知 source；普通商业网页请使用 web")
        role = text_field(raw["evidence_role"], "evidence_role", 80)
        if role not in EVIDENCE_ROLES:
            raise ValueError("不支持的 evidence_role")
        url = _public_url(raw["url"])
        original = text_field(raw["original_text"], "original_text", 20000)
        quote = text_field(raw["supporting_quote"], "supporting_quote", 4000)
        if quote not in original:
            raise ValueError("supporting_quote 必须是 original_text 中可定位的原文摘录")
        verification = raw["verification"]
        if not isinstance(verification, dict) or set(verification) != {"verified_by", "verified_at", "method"}:
            raise ValueError("verification 必须包含 verified_by、verified_at、method")
        if verification["method"] not in {"opened_page", "authorized_browser"}:
            raise ValueError("宿主必须打开页面核验；搜索摘要不能作为已核验网页导入")
        verification = {
            "verified_by": text_field(verification["verified_by"], "verified_by", 200),
            "verified_at": _timestamp(verification["verified_at"], "verified_at"),
            "method": verification["method"],
            "status": "host_attested", "independently_checked_by_importer": False,
        }
        observed = _timestamp(raw["observed_at"], "observed_at")
        published = _timestamp(raw["published_at"], "published_at") if raw.get("published_at") is not None else None
        locale = validate_query_locale(raw.get("country"), raw.get("language"))
        if not isinstance(raw.get("is_demo", False), bool):
            raise ValueError("is_demo 必须为布尔值")
        refs = {}
        for field in ("intent_refs", "candidate_gaps", "industry_ids"):
            values = raw.get(field, [])
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError(f"{field} 必须是不超过 20 项的数组")
            refs[field] = [text_field(value, field, 200) for value in values]
        original_url = _public_url(raw["original_url"]) if raw.get("original_url") else url
        native = {}
        supplied_identity = "source_object_id" in raw or "object_kind" in raw
        post_id, original_post_id = x_post_id(url), x_post_id(original_url)
        if supplied_identity:
            if source != "twitter" or not post_id or post_id != original_post_id:
                raise ValueError("原生对象身份仅用于 URL 与 original_url 指向同一 X 状态帖的 twitter 摘录")
            supplied_id = text_field(raw.get("source_object_id"), "source_object_id", 20)
            kind = raw.get("object_kind")
            if supplied_id != post_id or kind not in {"post", "comment", "reply"}:
                raise ValueError("source_object_id 必须匹配 X URL；object_kind 必须为 post、comment 或 reply")
            native = {"source_object_id": post_id, "object_kind": "comment" if kind == "reply" else kind}
        if native:
            # 仅显式声明对象类型的新宿主核验导入绑定原生身份；URL 不区分原帖和回复。
            # 不改旧日志或全局 URL 身份推导。
            # 作者路径是别名，不属于身份；保留原访问 URL，但原始 URL 用统一 permalink。
            # 两者指向同帖，需同时规范化以满足既有 original_url 转载判定。
            native["same_source_refs"] = sorted({url, original_url})
            url = original_url = f"https://x.com/i/status/{post_id}"
        query_metadata = {}
        for field in ("query", "query_id", "query_language", "query_region"):
            if field in raw:
                query_metadata[field] = text_field(raw[field], field, 2000 if field == "query" else 200)
        if "retrieval" in raw:
            retrieval = raw["retrieval"]
            allowed_retrieval = {"query", "query_id", "rank", "searched_at"}
            if not isinstance(retrieval, dict) or retrieval.keys() - allowed_retrieval:
                raise ValueError("retrieval 只接受 query、query_id、rank、searched_at；模型发现信息请保留在搜索回执")
            normalized_retrieval = {}
            for field in ("query", "query_id"):
                if field in retrieval:
                    normalized_retrieval[field] = text_field(retrieval[field], field, 2000)
            if "rank" in retrieval:
                rank = retrieval["rank"]
                if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 100000:
                    raise ValueError("retrieval.rank 必须为 1 到 100000 的整数")
                normalized_retrieval["rank"] = rank
            if "searched_at" in retrieval:
                normalized_retrieval["searched_at"] = _timestamp(retrieval["searched_at"], "retrieval.searched_at")
            query_metadata["retrieval"] = normalized_retrieval
        # 同一 URL 的证据身份稳定；文本修订用独立摘要表达，导入途径不增加来源数。
        evidence_id = (f"twitter:{'comment:' if native.get('object_kind') == 'comment' else ''}{post_id}" if native else
                       "web:" + hashlib.sha256(original_url.encode("utf-8")).hexdigest()[:24])
        content_sha = canonical_sha256({"original_text": original, "supporting_quote": quote, "evidence_role": role})
        marker = (evidence_id, content_sha)
        if marker in seen:
            raise ValueError("同批次包含重复网页摘录，请合并 intent_refs 后导入")
        seen.add(marker)
        evidence.append({
            "id": evidence_id, "source": source, "provider": "host-verified-web",
            "url": url, "original_url": original_url,
            "original_publisher": text_field(raw["original_publisher"], "original_publisher", 300) if raw.get("original_publisher") else None,
            "title": text_field(raw["title"], "title", 500), "original_text": original,
            "supporting_quote": quote, "evidence_role": role,
            "observed_at": observed, "published_at": published,
            "date_confidence": "host_attested" if published else "unknown",
            "language": locale["language"], "country": locale["country"],
            "access_method": "authorized-browser" if verification["method"] == "authorized_browser" else "manual-verification",
            "verification": verification, "content_sha256": content_sha,
            "signal_types": ["pricing"] if role == "official_pricing" else [],
            "payment_status": "not_established", "is_demo": raw.get("is_demo", False),
            **refs, **native, **query_metadata,
        })
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "as_of": as_of,
            "provider": "host-verified-web", "stage": "web_evidence_import",
            "input_sha256": canonical_sha256(payload), "evidence": evidence,
            "summary": {"evidence_count": len(evidence), "network_requests": 0, "cost_usd": 0}}


def read_json_file(path: Any, max_bytes: int = 4 * 1024 * 1024) -> Any:
    """按字节限制读取宿主文件。"""
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"JSON 文件不能超过 {max_bytes} 字节")
    return json.loads(raw.decode("utf-8"))
