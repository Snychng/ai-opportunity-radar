"""搜索候选 sidecar：引用只提供核验入口，模型回答不冒充发布者原文。"""

from __future__ import annotations

from copy import deepcopy
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aor.evidence.identity import canonical_evidence_url, canonical_sha256


X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
MAX_CITATIONS = 500
MAX_CANDIDATES = 5000


def public_candidate_url(value: Any) -> str:
    """只接受公开 HTTP(S) URL；不解析 DNS，不承诺后续重定向也是公网。"""
    if not isinstance(value, str) or not 1 <= len(value) <= 4000 or any(ord(c) < 33 for c in value):
        raise ValueError("候选 url 必须为不含空白和控制字符的公开 HTTP(S) 地址")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 80, 443} or "\\" in value):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        address = None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
                    or re.fullmatch(r"[0-9.]+", host) or host.startswith("0x")
                    or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                               for part in host.split("."))):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        # IPv6 需要括号，使用输入地址经既有规范函数统一追踪参数。
        normalized = canonical_evidence_url(value)
        if not normalized:
            raise ValueError
        # 既有 helper 保留路径／内容参数；这里同时规整 IDNA、末尾点和 IPv6 方括号。
        parts = urlsplit(normalized)
        authority = f"[{address.compressed}]" if isinstance(address, ipaddress.IPv6Address) else host.removeprefix("www.")
        return urlunsplit(("https", authority, parts.path, parts.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("候选 url 必须为不含凭证、内网或非常用端口的公开 HTTP(S) 地址") from exc


def x_post_id(value: Any) -> str | None:
    """仅从白名单 X 状态帖 URL 取得原生 ID，不推断作者、日期或正文。"""
    try:
        url = public_candidate_url(value)
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.hostname not in X_HOSTS:
        return None
    match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i)/status/([1-9][0-9]{0,19})(?:/(?:photo|video)/[1-9][0-9]*)?", parsed.path)
    if not match or int(match[1]) > 2**64 - 1:
        return None
    return match[1]


def _identity(value: Any) -> dict[str, Any]:
    url = public_candidate_url(value)
    native_id = x_post_id(url)
    if urlsplit(url).hostname in X_HOSTS and not native_id:
        raise ValueError("X 候选必须是可定位的状态帖 URL")
    canonical = f"https://x.com/i/status/{native_id}" if native_id else url
    key = f"twitter:status:{native_id}" if native_id else canonical
    return {"candidate_id": "SEARCH-" + canonical_sha256(key)[:20].upper(),
            "source": "twitter" if native_id else "web", "url": canonical,
            "source_object_id": native_id, "object_kind": "status" if native_id else "web_page"}


def _bounded_text(value: Any, field: str, limit: int = 1000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field} 必须是不超过 {limit} 字符的文本")
    return value


def normalize_search_candidates(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """从回执生成待核验候选；无效／不安全 URL 丢弃，可从回执总数对照统计。

    非零正文注解也不是原文核验。任何 receipt.verified、original_text、作者或日期
    都不传入候选，更不产生 evidence 或 OBS。
    """
    if not isinstance(receipt, dict):
        raise ValueError("搜索回执必须为对象")
    citations = receipt.get("citations", [])
    if not isinstance(citations, list) or len(citations) > MAX_CITATIONS:
        raise ValueError(f"citations 必须是不超过 {MAX_CITATIONS} 项的数组")
    context = {key: _bounded_text(receipt.get(key), key, 2000 if key == "query" else 1000)
               for key in ("run_id", "task_id", "query", "query_key", "provider", "model", "status",
                           "plan_sha256", "input_sha256", "created_at") if receipt.get(key) is not None}
    answer = _bounded_text(receipt.get("answer", ""), "answer", 200000) or ""
    summary = {"text": answer[:1000], "kind": "model_answer_preview", "truncated": len(answer) > 1000,
               "answer_sha256": canonical_sha256(answer), "receipt_sha256": canonical_sha256(receipt)}
    result = []
    for citation in citations:
        if isinstance(citation, str):
            citation = {"url": citation}
        if not isinstance(citation, dict):
            continue
        try:
            identity = _identity(citation.get("url"))
            alias = public_candidate_url(citation["url"])
        except ValueError:
            continue
        start, end = citation.get("start_index"), citation.get("end_index")
        integer_range = all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end))
        range_status = ("zero_length" if integer_range and start == end else
                        "answer_span" if integer_range and 0 <= start < end <= len(answer) else "unlocated")
        annotation = {"url": alias, "range_status": range_status}
        if integer_range:
            annotation.update(start_index=start, end_index=end)
        title = _bounded_text(citation.get("title"), "citation.title", 4000)
        if title:
            annotation["model_title"] = title
        result.append({**identity, "aliases": sorted({alias, identity["url"]}), "status": "pending",
                       "verification": {"status": "pending", "reason": "original_not_opened"},
                       "model_summaries": [{**context, **summary}] if answer else [],
                       "discoveries": [{**context, "citation": annotation}],
                       "original_text_available": False})
    return merge_search_candidates(result)


def _unique(values: list[Any]) -> list[Any]:
    return list({canonical_sha256(value): deepcopy(value) for value in values}.values())


def merge_search_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同帖不同路径和检索途径只占一候选；不合并不同帖子或不同用户。"""
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"候选必须是不超过 {MAX_CANDIDATES} 项的数组")
    merged: dict[str, dict[str, Any]] = {}
    for raw in candidates:
        if not isinstance(raw, dict):
            raise ValueError("候选必须为对象")
        identity = _identity(raw.get("url"))
        aliases = [identity["url"]]
        for alias in raw.get("aliases", []):
            try:
                if _identity(alias)["candidate_id"] == identity["candidate_id"]:
                    aliases.append(public_candidate_url(alias))
            except ValueError:
                continue
        row = merged.setdefault(identity["candidate_id"], {**identity, "aliases": [], "status": "pending",
            "verification": {"status": "pending", "reason": "original_not_opened"},
            "model_summaries": [], "discoveries": [], "original_text_available": False})
        row["aliases"] = sorted(set([*row["aliases"], *aliases]))
        for field in ("model_summaries", "discoveries"):
            values = raw.get(field, [])
            if not isinstance(values, list) or len(values) > MAX_CANDIDATES:
                raise ValueError(f"{field} 必须是不超过 {MAX_CANDIDATES} 项的数组")
            row[field] = _unique([*row[field], *values])
        # 失败的原文读取声明可以保留，但任何自报 verified 都不能升级候选。
        if raw.get("status") == "blocked":
            row["status"] = "blocked"
            row["verification"] = {"status": "blocked", "reason": "host_reported_access_blocked"}
    return list(merged.values())
