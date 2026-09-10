#!/usr/bin/env python3
"""把 TikHub 一期搜索结果规范化为可追溯、可去重的证据集合。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from contracts import SCHEMA_VERSION, ContractError, canonical_sha256, validate_run_as_of, validate_run_id, validate_stage_envelope
from tikhub_query import TIKHUB_RESULT_STAGES


PHASE_ONE_SOURCES = (
    "tiktok",
    "instagram",
    "linkedin",
    "threads",
    "twitter",
    "youtube",
    "reddit",
    "douyin",
    "xiaohongshu",
    "bilibili",
    "zhihu",
    "wechat_search",
)
STATUS_PRIORITY = {
    "error": 1,
    "auth-required": 1,
    "rate-limited": 1,
    "skipped-policy": 1,
    "no-results": 2,
    "ok": 3,
}
HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _path(value: Any, *parts: str) -> Any:
    current = value
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _clean_text(value: Any, *, limit: int = 4000) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    text = html.unescape(str(value))
    text = HTML_TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:limit]


def _join_text(*values: Any, limit: int = 4000) -> str | None:
    parts: list[str] = []
    for value in values:
        text = _clean_text(value, limit=limit)
        if text and text not in parts:
            parts.append(text)
    return _clean_text("\n".join(parts), limit=limit)


def _url(value: Any) -> str | None:
    text = _clean_text(value, limit=2000)
    if text and text.startswith(("https://", "http://")):
        return text
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if not candidate:
            return None
        try:
            return float(candidate) if "." in candidate else int(candidate)
        except ValueError:
            return None
    return None


def _engagement(**fields: Any) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for name, value in fields.items():
        number = _number(value)
        if number is not None:
            result[name] = number
    return result


def _normalize_date(value: Any) -> tuple[str | None, str, Any]:
    if value is None or value == "":
        return None, "unknown", None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            rendered = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            return rendered, "high", value
        except (OSError, OverflowError, ValueError):
            return str(value), "low", value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "unknown", value
        if re.fullmatch(r"\d{10,13}(?:\.\d+)?", text):
            number = float(text)
            if number > 10_000_000_000:
                number /= 1000
            try:
                rendered = datetime.fromtimestamp(number, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                return rendered, "high", value
            except (OSError, OverflowError, ValueError):
                pass
        if re.search(r"\b(?:ago|前|刚刚|yesterday|today)\b", text, re.IGNORECASE):
            return text, "low", value
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat().replace("+00:00", "Z"), "high", value
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(text)
            return parsed.isoformat().replace("+00:00", "Z"), "high", value
        except (TypeError, ValueError, OverflowError):
            return text, "low", value
    return _clean_text(value), "low", value


def _language(text: str | None, explicit: Any = None) -> str:
    explicit_text = _clean_text(explicit, limit=20)
    if explicit_text:
        return explicit_text.lower().replace("_", "-")
    if text and re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if text and re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if text and CJK_RE.search(text):
        return "zh"
    return "unknown"


def _status_update(statuses: dict[str, str], source: str, new_status: str) -> None:
    current = statuses.get(source)
    if current is None or STATUS_PRIORITY[new_status] > STATUS_PRIORITY[current]:
        statuses[source] = new_status


def _extract_items(source: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    if source == "tiktok":
        return [item for row in _list(data.get("search_item_list")) if (item := _dict(_path(row, "aweme_info")))]
    if source == "instagram":
        return [_dict(item) for item in _list(_path(data, "data", "items")) if isinstance(item, dict)]
    if source == "linkedin":
        return [_dict(item) for item in _list(data.get("data")) if isinstance(item, dict)]
    if source == "threads":
        items: list[dict[str, Any]] = []
        for edge in _list(_path(data, "searchResults", "edges")):
            thread_items = _list(_path(edge, "thread_items"))
            thread_post = _path(thread_items[0], "post") if thread_items else None
            node = _dict(_first(_path(edge, "node"), thread_post, edge))
            if node and any(key in node for key in ("id", "pk", "text", "caption")):
                items.append(node)
        return items
    if source == "twitter":
        return [_dict(item) for item in _list(data.get("timeline")) if isinstance(item, dict)]
    if source == "youtube":
        return [_dict(item) for item in _list(data.get("videos")) if isinstance(item, dict)]
    if source == "reddit":
        items = []
        for edge in _list(_path(data, "search", "dynamic", "components", "main", "edges")):
            node = _dict(_first(_path(edge, "node"), _path(edge, "post"), _path(edge, "data")))
            if node and any(key in node for key in ("id", "name", "title", "selftext", "permalink")):
                items.append(node)
        return items
    if source == "douyin":
        return [item for row in _list(data.get("business_data")) if (item := _dict(_path(row, "data", "aweme_info")))]
    if source == "xiaohongshu":
        return [item for row in _list(_path(data, "data", "items")) if (item := _dict(_path(row, "note")))]
    if source == "bilibili":
        return [_dict(item) for item in _list(data.get("result")) if isinstance(item, dict)]
    if source == "zhihu":
        return [item for row in _list(data.get("data")) if (item := _dict(_path(row, "object")))]
    if source == "wechat_search":
        return [_dict(item) for item in _list(data.get("items")) if isinstance(item, dict)]
    return []


def _source_fields(source: str, item: dict[str, Any]) -> dict[str, Any]:
    if source in {"tiktok", "douyin"}:
        stats = _dict(item.get("statistics"))
        aweme_id = _clean_text(item.get("aweme_id"), limit=200)
        return {
            "source_item_id": aweme_id,
            "title": _first(item.get("item_title"), item.get("preview_title")),
            "text": _first(item.get("desc"), item.get("search_desc"), item.get("content_desc")),
            "author": _first(_path(item, "author", "unique_id"), _path(item, "author", "nickname")),
            "container": _first(item.get("region"), item.get("city")),
            "date": item.get("create_time"),
            "language": item.get("desc_language"),
            "url": _first(item.get("share_url"), _path(item, "share_info", "share_url")),
            "engagement": _engagement(
                likes=stats.get("digg_count"), comments=stats.get("comment_count"),
                shares=stats.get("share_count"), views=stats.get("play_count"),
                saves=stats.get("collect_count"), reposts=stats.get("repost_count"),
            ),
            "identifiers": {"aweme_id": aweme_id} if aweme_id else {},
        }
    if source == "instagram":
        code = _clean_text(item.get("code"), limit=200)
        caption = item.get("caption")
        caption_text = _path(caption, "text") if isinstance(caption, dict) else caption
        return {
            "source_item_id": _first(item.get("id"), code),
            "title": item.get("title"), "text": caption_text,
            "author": _first(_path(item, "user", "username"), _path(item, "user", "full_name")),
            "container": "Instagram", "date": _first(item.get("taken_at"), item.get("taken_at_date")),
            "language": _first(item.get("language"), item.get("text_language"), item.get("video_subtitles_locale")),
            "url": f"https://www.instagram.com/p/{code}/" if code else None,
            "engagement": _engagement(likes=item.get("like_count"), comments=item.get("comment_count"),
                                      shares=item.get("share_count"), views=_first(item.get("play_count"), item.get("view_count")),
                                      reposts=item.get("repost_count")),
            "identifiers": {"code_or_url": code} if code else {},
        }
    if source == "linkedin":
        activity = _dict(item.get("activity"))
        post_id = _clean_text(item.get("id"), limit=300)
        return {
            "source_item_id": post_id, "title": None, "text": item.get("title"),
            "author": _first(_path(item, "author", "name"), item.get("author")), "container": "LinkedIn",
            "date": item.get("created_at"), "language": item.get("language"), "url": item.get("url"),
            "engagement": _engagement(
                likes=_first(activity.get("numLikes"), activity.get("likes"), activity.get("like_count")),
                comments=_first(activity.get("numComments"), activity.get("comments"), activity.get("comment_count")),
                shares=_first(activity.get("numShares"), activity.get("shares"), activity.get("share_count")),
            ),
            "identifiers": {"post_id": post_id} if post_id else {},
        }
    if source == "threads":
        post_id = _clean_text(_first(item.get("id"), item.get("pk")), limit=200)
        username = _clean_text(_first(_path(item, "user", "username"), item.get("username")), limit=200)
        url = f"https://www.threads.net/@{username}/post/{post_id}" if username and post_id else None
        return {
            "source_item_id": post_id, "title": None,
            "text": _first(item.get("text"), _path(item, "caption", "text"), item.get("caption")),
            "author": username, "container": "Threads", "date": _first(item.get("taken_at"), item.get("created_at")),
            "language": item.get("text_language"), "url": _first(item.get("url"), item.get("permalink"), url),
            "engagement": _engagement(likes=_first(item.get("like_count"), item.get("likes")),
                                      comments=_first(item.get("reply_count"), item.get("comment_count")),
                                      reposts=item.get("repost_count"), quotes=item.get("quote_count")),
            "identifiers": {"post_id": post_id} if post_id else {},
        }
    if source == "twitter":
        tweet_id = _clean_text(item.get("tweet_id"), limit=200)
        username = _clean_text(item.get("screen_name"), limit=200)
        return {
            "source_item_id": tweet_id, "title": None, "text": item.get("text"),
            "author": username, "container": "X", "date": item.get("created_at"), "language": item.get("lang"),
            "url": f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else None,
            "engagement": _engagement(likes=item.get("favorites"), comments=item.get("replies"),
                                      reposts=item.get("retweets"), quotes=item.get("quotes"),
                                      views=item.get("views"), saves=item.get("bookmarks")),
            "identifiers": {"tweet_id": tweet_id} if tweet_id else {},
        }
    if source == "youtube":
        video_id = _clean_text(item.get("video_id"), limit=200)
        return {
            "source_item_id": video_id, "title": item.get("title"), "text": item.get("description"),
            "author": item.get("author"), "container": item.get("channel_id"), "date": item.get("published_time"),
            "language": item.get("language"),
            "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            "engagement": _engagement(views=item.get("number_of_views")),
            "identifiers": {"video_id": video_id} if video_id else {},
        }
    if source == "reddit":
        raw_id = _clean_text(_first(item.get("id"), item.get("name")), limit=200)
        post_id = raw_id[3:] if raw_id and raw_id.startswith("t3_") else raw_id
        permalink = _clean_text(item.get("permalink"), limit=2000)
        url = f"https://www.reddit.com{permalink}" if permalink and permalink.startswith("/") else permalink
        return {
            "source_item_id": post_id, "title": item.get("title"), "text": _first(item.get("selftext"), item.get("body")),
            "author": item.get("author"), "container": _first(item.get("subreddit_name_prefixed"), item.get("subreddit")),
            "date": _first(item.get("created_utc"), item.get("created")), "language": item.get("lang"), "url": url,
            "engagement": _engagement(likes=_first(item.get("score"), item.get("ups")), comments=item.get("num_comments"),
                                      shares=item.get("num_crossposts")),
            "identifiers": {"post_id": f"t3_{post_id}"} if post_id else {},
        }
    if source == "xiaohongshu":
        note_id = _clean_text(item.get("id"), limit=200)
        note_type = _clean_text(item.get("type"), limit=80)
        identifiers = {"note_id": note_id} if note_id else {}
        if note_type:
            identifiers["note_type"] = note_type
        return {
            "source_item_id": note_id, "title": item.get("title"), "text": item.get("desc"),
            "author": _first(_path(item, "user", "nickname"), _path(item, "user", "name"), _path(item, "user", "user_id")),
            "container": "小红书", "date": _first(item.get("timestamp"), item.get("update_time")),
            "language": item.get("language"),
            "url": f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else None,
            "engagement": _engagement(likes=item.get("liked_count"), comments=item.get("comments_count"),
                                      shares=item.get("shared_count"), saves=item.get("collected_count")),
            "identifiers": identifiers,
        }
    if source == "bilibili":
        bv_id = _clean_text(_first(item.get("bvid"), item.get("bv_id")), limit=200)
        return {
            "source_item_id": _first(bv_id, item.get("aid")), "title": item.get("title"),
            "text": _first(item.get("description"), item.get("desc")), "author": _first(item.get("author"), item.get("up_name")),
            "container": "Bilibili", "date": _first(item.get("pubdate"), item.get("created")),
            "language": item.get("language"), "url": _first(item.get("arcurl"), f"https://www.bilibili.com/video/{bv_id}" if bv_id else None),
            "engagement": _engagement(views=_first(item.get("play"), item.get("view")), likes=item.get("like"),
                                      comments=_first(item.get("review"), item.get("comment")),
                                      shares=item.get("share"), saves=item.get("favorites")),
            "identifiers": {"bv_id": bv_id} if bv_id else {},
        }
    if source == "zhihu":
        object_id = _clean_text(item.get("id"), limit=200)
        object_type = _clean_text(item.get("type"), limit=80)
        question_id = _clean_text(_path(item, "question", "id"), limit=200)
        if object_type == "answer" and question_id and object_id:
            url = f"https://www.zhihu.com/question/{question_id}/answer/{object_id}"
        elif object_type == "article" and object_id:
            url = f"https://zhuanlan.zhihu.com/p/{object_id}"
        else:
            url = item.get("url")
        identifiers: dict[str, str] = {}
        if object_type == "answer" and object_id:
            identifiers["answer_id"] = object_id
        elif object_id:
            identifiers[f"{object_type or 'object'}_id"] = object_id
        return {
            "source_item_id": object_id, "title": _first(item.get("title"), _path(item, "question", "title")),
            "text": _first(item.get("excerpt"), item.get("description"), item.get("content")),
            "author": _first(_path(item, "author", "name"), _path(item, "author", "headline")),
            "container": _path(item, "question", "title"), "date": _first(item.get("created_time"), item.get("updated_time")),
            "language": item.get("language"), "url": url,
            "engagement": _engagement(likes=item.get("voteup_count"), comments=item.get("comment_count"),
                                      saves=item.get("favorites_count"), views=item.get("visits_count")),
            "identifiers": identifiers,
        }
    if source == "wechat_search":
        document_id = _clean_text(_first(item.get("docID"), item.get("id")), limit=300)
        article_url = _first(item.get("doc_url"), item.get("url"))
        return {
            "source_item_id": document_id, "title": item.get("title"), "text": item.get("desc"),
            "author": item.get("source"), "container": item.get("source"), "date": _first(item.get("timestamp"), item.get("date")),
            "language": item.get("language"), "url": article_url, "engagement": {},
            "identifiers": {"url": article_url} if _url(article_url) else {},
        }
    return {}


def _normalize_item(
    source: str,
    item: dict[str, Any],
    *,
    query_id: str,
    query_group: str | None,
    query: str | None,
    observed_at: str | None,
    raw_file: str,
    raw_pointer: str,
    is_detail: bool = False,
) -> dict[str, Any] | None:
    fields = _source_fields(source, item)
    if is_detail:
        fields["text"] = _first(*(item.get(key) for key in ("full_text", "content", "body", "description", "text", "desc")), fields.get("text"))
        fields["date"] = _first(item.get("published_at"), fields.get("date"), item.get("created_at"))
        fields["url"] = _first(fields.get("url"), item.get("url"), item.get("web_url"))
    source_item_id = _clean_text(fields.get("source_item_id"), limit=300)
    title = _clean_text(fields.get("title"), limit=500)
    original_text = _join_text(title, fields.get("text"), limit=8 * 1024 * 1024 if is_detail else 4000)
    normalized_url = _url(fields.get("url"))
    if not any((source_item_id, original_text, normalized_url)):
        return None
    published_at, date_confidence, published_at_raw = _normalize_date(fields.get("date"))
    author = _clean_text(fields.get("author"), limit=300)
    container = _clean_text(fields.get("container"), limit=500)
    warnings: list[str] = []
    if not original_text:
        warnings.append("missing_text")
    if not normalized_url:
        warnings.append("missing_url")
    if date_confidence in {"low", "unknown"}:
        warnings.append("uncertain_date")
    return {
        "id": "",
        "source": source,
        "source_item_id": source_item_id,
        "query_id": query_id,
        "query_group": query_group,
        "query": query,
        "url": normalized_url,
        "author": author,
        "container": container,
        "title": title,
        "original_text": original_text,
        "zh_translation": None,
        "language": _language(original_text, _first(item.get("language"), fields.get("language"))),
        "source_region": _clean_text(_first(item.get("region"), item.get("country")), limit=100) or "unknown",
        "published_at": published_at,
        "published_at_raw": published_at_raw,
        "date_confidence": date_confidence,
        "observed_at": observed_at,
        "engagement": fields.get("engagement", {}),
        "access_method": "third-party-api",
        "signal_types": [],
        "identifiers": fields.get("identifiers", {}),
        "raw_file": raw_file,
        "raw_json_pointer": raw_pointer,
        "extraction_warnings": warnings,
    }


def _dedupe_key(item: dict[str, Any]) -> tuple[str, str]:
    source = item["source"]
    source_item_id = item.get("source_item_id")
    if source_item_id:
        return source, f"id:{source_item_id}"
    if item.get("url"):
        return source, f"url:{item['url']}"
    digest = hashlib.sha256((item.get("original_text") or "").encode("utf-8")).hexdigest()[:20]
    return source, f"text:{digest}"


def _comment_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    """把规范化搜索证据转换成评论计划可直接使用的候选。"""
    source = str(item.get("source") or "")
    identifiers = dict(item.get("identifiers") or {})
    content_type: str | None = None
    if source == "xiaohongshu":
        raw_type = str(identifiers.pop("note_type", "")).lower()
        if raw_type in {"normal", "image", "image_note", "note"}:
            content_type = "image_note"
        elif raw_type in {"video", "video_note"}:
            content_type = "video_note"
        else:
            return None
    elif source == "zhihu":
        if "answer_id" not in identifiers:
            return None
        content_type = "answer"
    elif source == "wechat_search":
        if "url" not in identifiers:
            return None
        content_type = "article"
    if not identifiers:
        return None
    candidate = {
        "source": source,
        "selected_item_id": item["id"],
        "parent_url": item.get("url"),
        "identifiers": identifiers,
    }
    if content_type:
        candidate["content_type"] = content_type
    return candidate


def _comment_text(row: dict[str, Any]) -> str | None:
    value = _first(*(row.get(key) for key in ("text", "content", "comment_text", "comment", "body", "message", "desc")))
    if isinstance(value, dict):
        value = _first(value.get("text"), value.get("content"), value.get("message"))
    return _clean_text(value, limit=8 * 1024 * 1024)


def _comment_id(row: dict[str, Any], parent: str | None = None, post: str | None = None) -> str:
    return _clean_text(_first(*(row.get(key) for key in ("comment_id", "cid", "reply_id", "id", "pk"))), limit=300) or canonical_sha256({"post": post, "parent": parent, "text": _comment_text(row)})[:20]


def _extract_comment_items(data: dict[str, Any], selected_item_id: str = "") -> list[dict[str, Any]]:
    """保留嵌套回复的父评论和原始 JSON 定位，禁止把作者对象当评论。"""
    result: list[dict[str, Any]] = []

    def visit(value: Any, parent: str | None, pointer: str, depth: int) -> None:
        if depth > 12:
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, parent, f"{pointer}/{index}", depth + 1)
        elif isinstance(value, dict):
            is_comment = _comment_text(value) and any(key in value for key in ("comment_id", "cid", "reply_id", "id", "pk", "author", "user", "create_time", "created_at"))
            next_parent = parent
            if is_comment:
                row = dict(value)
                explicit_parent = _clean_text(_first(value.get("parent_comment_id"), value.get("reply_to_comment_id"), value.get("reply_to_id"), value.get("parentId")), limit=300)
                if not explicit_parent and str(value.get("parent_id") or "").startswith("t1_"):
                    explicit_parent = str(value["parent_id"])[3:]
                row["_parent_comment_id"] = explicit_parent or parent
                row["_raw_pointer"] = pointer
                result.append(row)
                next_parent = _comment_id(row, row["_parent_comment_id"], selected_item_id)
            for key, child in value.items():
                if key not in {"author", "user", "owner", "reactions", "statistics", "content", "comment"}:
                    escaped_key = str(key).replace("~", "~0").replace("/", "~1")
                    visit(child, next_parent, f"{pointer}/{escaped_key}", depth + 1)

    visit(data, None, "", 0)
    return result


def _detail_items(source: str, data: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """沿固定响应容器提取详情，不把任意嵌套字段混成帖子证据。"""
    containers = ("data", "item", "items", "video", "video_info", "aweme_detail", "aweme_info", "itemInfo", "itemStruct", "post", "note", "note_card", "result", "article", "answer", "tweet", "legacy", "media")
    result: list[tuple[dict[str, Any], str]] = []

    def visit(value: Any, pointer: str, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}", depth + 1)
        elif isinstance(value, dict):
            fields = _source_fields(source, value)
            text = _first(fields.get("text"), fields.get("title"), *(value.get(key) for key in ("full_text", "content", "body", "description", "text", "desc", "title")))
            if _clean_text(text):
                result.append((value, pointer))
                return
            for key in containers:
                if key in value:
                    visit(value[key], f"{pointer}/{key}", depth + 1)

    visit(data, "", 0)
    return result


def _normalize_comment(
    source: str,
    row: dict[str, Any],
    *,
    selected_item_id: str,
    query_id: str,
    observed_at: str | None,
    parent_url: str | None = None,
    raw_file: str | None = None,
    raw_pointer: str | None = None,
) -> dict[str, Any] | None:
    text_value = _first(
        row.get("text"),
        row.get("content"),
        row.get("comment_text"),
        row.get("comment"),
        row.get("body"),
        row.get("message"),
        row.get("desc"),
    )
    if isinstance(text_value, dict):
        text_value = _first(text_value.get("text"), text_value.get("content"), text_value.get("message"))
    text = _clean_text(text_value, limit=8 * 1024 * 1024)
    if not text:
        return None
    comment_id = _clean_text(
        _first(row.get("comment_id"), row.get("cid"), row.get("reply_id"), row.get("id"), row.get("pk")),
        limit=300,
    )
    if not comment_id:
        comment_id = _comment_id(row, row.get("_parent_comment_id"), selected_item_id)
    author = _first(
        _path(row, "user", "username"),
        _path(row, "user", "nickname"),
        _path(row, "user", "name"),
        _path(row, "author", "name"),
        row.get("author"),
        row.get("username"),
    )
    published_at, date_confidence, published_at_raw = _normalize_date(
        _first(row.get("create_time"), row.get("created_at"), row.get("timestamp"), row.get("published_at"))
    )
    return {
        "id": f"{source}:comment:{comment_id}",
        "source": source,
        "source_item_id": comment_id,
        "parent_item_id": selected_item_id,
        "parent_comment_id": row.get("_parent_comment_id"),
        "origin_id": selected_item_id,
        "query_id": query_id,
        "url": _url(_first(row.get("url"), row.get("permalink"))) or parent_url,
        "url_kind": "comment" if _url(_first(row.get("url"), row.get("permalink"))) else ("parent_post" if parent_url else "unavailable"),
        "parent_url": parent_url,
        "raw_file": raw_file,
        "raw_json_pointer": raw_pointer,
        "author": _clean_text(author, limit=300),
        "container": selected_item_id,
        "title": None,
        "original_text": text,
        "zh_translation": None,
        "language": _language(text, row.get("language")),
        "published_at": published_at,
        "published_at_raw": published_at_raw,
        "date_confidence": date_confidence,
        "observed_at": observed_at,
        "engagement": _engagement(
            likes=_first(row.get("like_count"), row.get("digg_count"), row.get("likes")),
            replies=_first(row.get("reply_count"), row.get("sub_comment_count"), row.get("replies")),
        ),
        "access_method": "third-party-api",
        "signal_types": [],
    }


def _failure_status(result: dict[str, Any]) -> str:
    code = str(result.get("error_code") or "")
    if code == "auth_error":
        return "auth-required"
    if code == "rate_limited":
        return "rate-limited"
    if code.startswith("skipped_"):
        return "skipped-policy"
    return "error"


def normalize_documents(
    documents: Iterable[dict[str, Any]],
    *,
    source_files: Iterable[str] | None = None,
) -> dict[str, Any]:
    """合并 TikHub 搜索或评论结果，并输出白名单证据字段。"""
    documents_list = list(documents)
    if not documents_list:
        raise ValueError("至少需要一个 TikHub 结果文档")
    files = list(source_files or [f"input-{index}.json" for index in range(1, len(documents_list) + 1)])
    if len(files) != len(documents_list):
        raise ValueError("source_files 数量必须与 documents 一致")

    for document in documents_list:
        validate_stage_envelope(document)
        if document.get("provider") != "tikhub":
            raise ValueError("规范化输入 provider 必须为 tikhub")
    run_ids = {str(document.get("run_id") or "") for document in documents_list}
    if len(run_ids) != 1:
        raise ValueError("合并的 TikHub 文档必须使用同一个 run_id")
    try:
        run_id = validate_run_id(run_ids.pop())
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    as_of_values = {str(document.get("as_of") or "") for document in documents_list}
    if len(as_of_values) != 1:
        raise ValueError("合并的 TikHub 文档必须使用同一个 as_of")
    try:
        _, as_of = validate_run_as_of(run_id, as_of_values.pop())
    except ContractError as exc:
        raise ValueError(str(exc)) from exc
    stages = {str(document.get("stage") or "") for document in documents_list}
    if len(stages) != 1 or not stages <= TIKHUB_RESULT_STAGES:
        raise ValueError("只能合并同一阶段的 TikHub 搜索、补证或评论结果")
    source_stage = stages.pop()

    statuses: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    detail_results: list[dict[str, Any]] = []
    request_statuses: list[dict[str, Any]] = []
    duplicate_count = 0
    seen: set[tuple[str, str]] = set()
    seen_comments: set[tuple[str, str, str]] = set()
    request_count = 0

    for document_index, (document, raw_file) in enumerate(zip(documents_list, files, strict=True)):
        observed_at = _clean_text(document.get("generated_at"), limit=100)
        for result_index, result in enumerate(_list(document.get("results"))):
            if not isinstance(result, dict):
                continue
            request_count += 1
            source = _clean_text(result.get("source"), limit=100)
            if not source or source not in PHASE_ONE_SOURCES:
                continue
            if result.get("status") != "ok":
                failure_status = _failure_status(result)
                _status_update(statuses, source, failure_status)
                request_statuses.append(
                    {
                        "id": _clean_text(result.get("id"), limit=300),
                        "source": source,
                        "status": failure_status,
                        "error_code": _clean_text(result.get("error_code"), limit=100),
                    }
                )
                continue
            data = _dict(_path(result, "response", "data"))
            if source_stage == "comment_deep_dive":
                kind = str(result.get("kind") or "")
                selected_item_id = _clean_text(result.get("selected_item_id"), limit=300)
                if not selected_item_id:
                    raise ValueError("评论和详情结果必须保留 selected_item_id")
                query_id = _clean_text(result.get("id"), limit=300) or f"request-{document_index}-{result_index}"
                parent_url = _url(result.get("parent_url"))
                if kind == "detail":
                    detail_evidence_ids: list[str] = []
                    for row, pointer in _detail_items(source, data):
                        detail = _normalize_item(source, row, query_id=query_id, query_group=None, query=None,
                                                 observed_at=observed_at, raw_file=raw_file,
                                                 raw_pointer=f"/results/{result_index}/response/data{pointer}", is_detail=True)
                        if detail is None:
                            continue
                        detail["id"] = selected_item_id
                        detail["origin_id"] = selected_item_id
                        detail["parent_item_id"] = selected_item_id
                        detail["evidence_kind"] = "post_detail"
                        detail["url"] = detail.get("url") or parent_url
                        detail["comment_candidate"] = None
                        key = (source, selected_item_id)
                        if key in seen:
                            duplicate_count += 1
                            continue
                        seen.add(key)
                        evidence.append(detail)
                        detail_evidence_ids.append(detail["id"])
                    detail_status = "ok" if detail_evidence_ids else "no-results"
                    detail_results.append({"source": source, "selected_item_id": selected_item_id,
                                           "query_id": query_id, "status": detail_status, "evidence_ids": detail_evidence_ids})
                    _status_update(statuses, source, detail_status)
                    request_statuses.append({"id": query_id, "source": source, "status": detail_status, "error_code": None})
                    continue
                if kind != "top_level_comments":
                    raise ValueError(f"不支持的评论深挖 kind：{kind}")
                raw_comments = _extract_comment_items(data, selected_item_id)
                normalized_count = 0
                for raw_comment in raw_comments:
                    normalized_comment = _normalize_comment(
                        source,
                        raw_comment,
                        selected_item_id=selected_item_id,
                        query_id=query_id,
                        observed_at=observed_at,
                        parent_url=parent_url,
                        raw_file=raw_file,
                        raw_pointer=f"/results/{result_index}/response/data{raw_comment.get('_raw_pointer', '')}",
                    )
                    if normalized_comment is None:
                        continue
                    marker = (source, selected_item_id, normalized_comment["source_item_id"])
                    if marker in seen_comments:
                        duplicate_count += 1
                        continue
                    seen_comments.add(marker)
                    comments.append(normalized_comment)
                    normalized_count += 1
                _status_update(statuses, source, "ok" if normalized_count else "no-results")
                request_statuses.append(
                    {
                        "id": query_id,
                        "source": source,
                        "status": "ok" if normalized_count else "no-results",
                        "error_code": None,
                    }
                )
                continue
            raw_items = _extract_items(source, data)
            if not raw_items:
                _status_update(statuses, source, "no-results")
                request_statuses.append(
                    {
                        "id": _clean_text(result.get("id"), limit=300),
                        "source": source,
                        "status": "no-results",
                        "error_code": None,
                    }
                )
                continue
            normalized_for_result = 0
            params = _dict(result.get("params"))
            query = _clean_text(_first(params.get("keyword"), params.get("query"), params.get("searchTerms"), params.get("search_query")), limit=500)
            for item_index, raw_item in enumerate(raw_items):
                normalized = _normalize_item(
                    source,
                    raw_item,
                    query_id=_clean_text(result.get("id"), limit=300) or f"request-{document_index}-{result_index}",
                    query_group=_clean_text(result.get("query_group"), limit=200),
                    query=query,
                    observed_at=observed_at,
                    raw_file=raw_file,
                    raw_pointer=f"/results/{result_index}/response/data",
                )
                if normalized is None:
                    continue
                key = _dedupe_key(normalized)
                if key in seen:
                    duplicate_count += 1
                    continue
                seen.add(key)
                normalized["id"] = f"{source}:{key[1].split(':', 1)[1]}"
                normalized["origin_id"] = normalized["id"]
                normalized["evidence_kind"] = "post"
                normalized["query_language"] = _dict(result.get("query_scope")).get("language", "unknown")
                normalized["query_region"] = _dict(result.get("query_scope")).get("country", "unknown")
                if isinstance(result.get("evidence_gap"), dict):
                    normalized["evidence_gap"] = dict(result["evidence_gap"])
                normalized["comment_candidate"] = _comment_candidate(normalized)
                evidence.append(normalized)
                normalized_for_result += 1
            _status_update(statuses, source, "ok" if normalized_for_result else "no-results")
            request_statuses.append(
                {
                    "id": _clean_text(result.get("id"), limit=300),
                    "source": source,
                    "status": "ok" if normalized_for_result else "no-results",
                    "error_code": None,
                }
            )

    parent_urls = {item["id"]: item["url"] for item in evidence if item.get("url")}
    for comment in comments:
        parent_url = comment.get("parent_url") or parent_urls.get(comment["parent_item_id"])
        if parent_url:
            comment["parent_url"] = parent_url
            if not comment.get("url"):
                comment["url"] = parent_url
                comment["url_kind"] = "parent_post"
    by_source = Counter(item["source"] for item in [*evidence, *comments])
    comment_candidates = [item["comment_candidate"] for item in evidence if item.get("comment_candidate")]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "provider": "tikhub",
        "run_id": run_id,
        "as_of": as_of,
        "stage": {"search_discovery": "tikhub_normalized_search", "comment_deep_dive": "tikhub_normalized_comments", "evidence_gap_verification": "tikhub_normalized_gaps"}[source_stage],
        "source_stage": source_stage,
        "source_files": files,
        "stats": {
            "requests_seen": request_count,
            "valid_items": len(evidence),
            "valid_comments": len(comments),
            "comment_candidates": len(comment_candidates),
            "duplicates_removed": duplicate_count,
            "by_source": dict(sorted(by_source.items())),
            "source_status": {source: statuses[source] for source in PHASE_ONE_SOURCES if source in statuses},
        },
        "evidence": evidence,
        "comment_candidates": comment_candidates,
        "comments": comments,
        "detail_results": detail_results,
        "request_statuses": request_statuses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="规范化 TikHub 一期搜索或评论结果")
    parser.add_argument("--input", type=Path, action="append", required=True, help="可重复传入 TikHub 结果 JSON")
    parser.add_argument("--output", type=Path, required=True, help="规范化 JSON 输出路径")
    parser.add_argument("--selection-output", type=Path, help="可选导出评论候选；筛选后补 selection_reason")
    args = parser.parse_args()
    try:
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in args.input]
        payload = normalize_documents(documents, source_files=[str(path) for path in args.input])
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.selection_output:
        args.selection_output.parent.mkdir(parents=True, exist_ok=True)
        args.selection_output.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": payload["run_id"],
                    "candidates": payload["comment_candidates"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload["stats"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
