#!/usr/bin/env python3
"""把 TikHub 一期搜索结果规范化为可追溯、可去重的证据集合。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from contracts import SCHEMA_VERSION, ContractError, canonical_sha256, validate_run_as_of, validate_run_id, validate_stage_envelope
from tikhub_query import TIKHUB_RESULT_STAGES
import aor_bootstrap  # noqa: F401
from aor.text import language as _language
from aor.evidence.quality import assess_quality, aggregate_status, mark_reposts, research_window


PARSER_VERSION = "2.2.0"

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
    "unrecognized_response": 1,
    "upstream_error": 1,
    "partial_parse": 1,
    "no-results": 2,
    "ok": 3,
}
HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
DETAIL_CONTAINERS = ("data", "item", "items", "video", "video_info", "aweme_detail", "aweme_info", "itemInfo",
                     "itemStruct", "post", "note", "note_list", "note_card", "result", "article", "answer", "tweet",
                     "legacy", "media")


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
            return None, "low", value
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
            return None, "low", value
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
            return None, "low", value
    return None, "low", value


def _publication_date_fields(value: Any, observed_at: str | None) -> dict[str, Any]:
    """相对日期只提供保守区间；采集时间必须来自原始响应，重解析不得改用当前时间。"""
    published_at, confidence, raw = _normalize_date(value)
    result = {"published_at": published_at, "published_at_raw": raw, "date_confidence": confidence,
              "date_basis": "absolute" if published_at else "unknown", "published_at_interval": None}
    if published_at or not isinstance(value, str) or not observed_at:
        return result
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return result
        observed = observed.astimezone(timezone.utc)
    except ValueError:
        return result
    text = value.strip().casefold()
    text = re.sub(r"^(?:streamed|premiered|updated|posted|published)\s+", "", text)
    match = re.fullmatch(r"(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?|months?|years?|秒|分钟|小时|天|周|个月|月|年)\s*(?:ago|前)", text)
    if match:
        count, unit = int(match[1]), match[2]
        # 月和年的天数因日历而异，宁可扩大区间也不伪造一个发布时间。
        units = {"second": (1, 1), "minute": (60, 60), "hour": (3600, 3600),
                 "day": (86400, 86400), "week": (604800, 604800),
                 "month": (28 * 86400, 31 * 86400), "year": (365 * 86400, 366 * 86400)}
        aliases = {"秒": "second", "分钟": "minute", "小时": "hour", "天": "day", "周": "week",
                   "个月": "month", "月": "month", "年": "year"}
        unit = aliases.get(unit, unit.rstrip('s'))
        shortest, longest = units[unit]
        try:
            earliest = observed - timedelta(seconds=(count + 1) * longest)
            latest = observed - timedelta(seconds=count * shortest)
        except (OverflowError, ValueError):
            return result
    elif text in {"today", "yesterday", "今天", "昨天", "刚刚", "just now"}:
        if text in {"刚刚", "just now"}:
            earliest, latest = observed - timedelta(minutes=1), observed
        else:
            day = observed.date() - timedelta(days=int(text in {"yesterday", "昨天"}))
            # 来源本地时区未知，允许 UTC-12 至 UTC+14 的自然日范围。
            midnight = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            earliest = midnight - timedelta(hours=14)
            latest = min(observed, midnight + timedelta(hours=36))
    else:
        return result
    result.update(date_confidence="estimated", date_basis="relative_to_observation",
                  published_at_interval={"earliest": earliest.date().isoformat(), "latest": latest.date().isoformat(),
                                         "semantics": "inclusive_calendar_days", "anchor_observed_at": observed_at})
    return result


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
            for child in _list(node.get("children")):
                post = _dict(_path(child, "post"))
                if post.get("id") and (post.get("postTitle") or post.get("title")):
                    items.append(post)
            if node and any(key in node for key in ("id", "name", "title", "selftext", "permalink")):
                items.append(node)
        return items
    if source == "douyin":
        return [item for row in _list(data.get("business_data")) if (item := _dict(_path(row, "data", "aweme_info")))]
    if source == "xiaohongshu":
        return [item for row in _list(_path(data, "data", "items")) if (item := _dict(_path(row, "note")))]
    if source == "bilibili":
        results = _first(data.get("result"), _path(data, "data", "result"))
        return [_dict(item) for item in _list(results) if isinstance(item, dict)]
    if source == "zhihu":
        return [item for row in _list(data.get("data")) if (item := _dict(_path(row, "object")))]
    if source == "wechat_search":
        return [_dict(item) for item in _list(data.get("items")) if isinstance(item, dict)]
    return []


def _search_shape(source: str, data: dict[str, Any]) -> tuple[bool, int | None, int]:
    """只把已知列表容器中的明确空列表视为零结果；未知非空结构必须显式失败。"""
    paths = {
        "tiktok": (("search_item_list",),), "instagram": (("data", "items"),),
        "linkedin": (("data",),), "threads": (("searchResults", "edges"),),
        "twitter": (("timeline",),), "youtube": (("videos",),),
        "reddit": (("search", "dynamic", "components", "main", "edges"),),
        "douyin": (("business_data",),), "xiaohongshu": (("data", "items"),),
        "bilibili": (("result",), ("data", "result")), "zhihu": (("data",),),
        "wechat_search": (("items",),),
    }
    rows = next((rows for path in paths.get(source, ()) if isinstance(rows := _path(data, *path), list)), None)
    if rows is None:
        return False, None, 0
    raw_count, skipped = 0, 0
    for row in rows:
        if source == "reddit" and isinstance(row, dict):
            if _path(row, "presentation", "type") == "empty_state":
                skipped += 1
                continue
            children = _path(row, "node", "children")
            if isinstance(children, list) and children:
                raw_count += len(children)
                continue
        if source == "zhihu" and isinstance(row, dict) and row.get("object") is None and "query_list" in row:
            skipped += 1
            continue
        if source == "xiaohongshu" and isinstance(row, dict) and row.get("model_type") == "ads" and "note" not in row:
            skipped += 1
            continue
        raw_count += 1
    return True, raw_count, skipped


def _comment_shape(data: dict[str, Any]) -> tuple[bool, int | None, int]:
    """统计评论容器中的原始条目，包括无法解析的行，避免部分响应伪装成完整成功。"""
    collections = {"comments", "comment_list", "root_comments", "replies", "sub_comments", "sub_comment_list"}
    found, count = False, 0

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal found, count
        if depth > 12:
            return
        if isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in collections and isinstance(child, list):
                    found = True
                    count += len(child)
                if key not in {"user", "author", "owner", "reactions", "statistics"}:
                    visit(child, depth + 1)

    visit(data)
    return found, count if found else None, 0


def _upstream_error(response: Any) -> str | None:
    """只读取响应信封状态，不将成功 HTTP 下的供应商错误当成空搜索。"""
    value = response
    for _ in range(4):
        if not isinstance(value, dict):
            return None
        if value.get("success") is False:
            return "upstream_success_false"
        for key in ("code", "status_code", "statusCode"):
            code = value.get(key)
            if isinstance(code, (int, float)) and not isinstance(code, bool) and code not in {0, 200}:
                return "upstream_non_success_code"
            if isinstance(code, str) and code.strip().isdigit() and int(code) not in {0, 200}:
                return "upstream_non_success_code"
        if value.get("error") or value.get("errors"):
            return "upstream_error_payload"
        value = value.get("data")
    return None


def _parse_record(result: dict, source: str, *, recognized: bool, raw_count: int | None,
                  parsed_count: int, skipped: int = 0) -> dict[str, Any]:
    if parsed_count and (raw_count is None or parsed_count >= raw_count):
        parse_status, status = "parsed", "ok"
    elif parsed_count:
        parse_status = status = "partial_parse"
    elif recognized and raw_count == 0:
        parse_status, status = "empty_result", "no-results"
    else:
        parse_status = status = "unrecognized_response"
    return {"id": _clean_text(result.get("id"), limit=300), "source": source, "status": status,
            "error_code": None if status in {"ok", "no-results"} else parse_status,
            "parse_status": parse_status, "raw_items": raw_count, "parsed_items": parsed_count,
            "skipped_noncontent_items": skipped, "parser_version": PARSER_VERSION,
            "input_response_sha256": canonical_sha256(result.get("response"))}


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
            "author": item.get("author"), "container": item.get("channel_id"),
            "date": _first(item.get("published_at"), item.get("published_time"), item.get("publishedTimeText"),
                           item.get("published_time_text"), item.get("publishedTime")),
            "language": item.get("language"),
            "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            "engagement": _engagement(views=item.get("number_of_views")),
            "identifiers": {"video_id": video_id} if video_id else {},
        }
    if source == "reddit":
        raw_id = _clean_text(_first(item.get("id"), item.get("name")), limit=200)
        post_id = raw_id[3:] if raw_id and raw_id.startswith("t3_") else raw_id
        permalink = _clean_text(item.get("permalink"), limit=2000)
        url = f"https://www.reddit.com{permalink}" if permalink and permalink.startswith("/") else permalink or item.get("url")
        return {
            "source_item_id": post_id, "title": _first(item.get("title"), item.get("postTitle")),
            "text": _first(item.get("selftext"), item.get("body"), _path(item, "content", "markdown")),
            "author": _first(_path(item, "author", "name"), item.get("author")),
            "container": _first(item.get("subreddit_name_prefixed"), _path(item, "subreddit", "name"), item.get("subreddit")),
            "date": _first(item.get("created_utc"), item.get("created"), item.get("createdAt")), "language": item.get("lang"), "url": url,
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
            "container": "小红书", "date": _first(item.get("timestamp"), item.get("time")),
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
            "container": _path(item, "question", "title"), "date": item.get("created_time"),
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
    date_fields = _publication_date_fields(fields.get("date"), observed_at)
    author = _clean_text(fields.get("author"), limit=300)
    container = _clean_text(fields.get("container"), limit=500)
    warnings: list[str] = []
    if not original_text:
        warnings.append("missing_text")
    if not normalized_url:
        warnings.append("missing_url")
    if date_fields["date_confidence"] != "high":
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
        **date_fields,
        "parser_version": PARSER_VERSION,
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
    value = _first(*(row.get(key) for key in ("text", "full_text", "content", "comment_text", "comment", "body", "message", "desc")))
    if isinstance(value, dict):
        value = _first(value.get("text"), value.get("content"), value.get("message"))
    return _clean_text(value, limit=8 * 1024 * 1024)


def _comment_id(row: dict[str, Any], parent: str | None = None, post: str | None = None) -> str:
    return _clean_text(_first(*(row.get(key) for key in ("comment_id", "cid", "reply_id", "id", "id_str", "rest_id", "pk"))), limit=300) or canonical_sha256({"post": post, "parent": parent, "text": _comment_text(row)})[:20]


def _extract_comment_items(data: dict[str, Any], selected_item_id: str = "") -> list[dict[str, Any]]:
    """保留嵌套回复的父评论和原始 JSON 定位，禁止把作者对象当评论。"""
    result: list[dict[str, Any]] = []

    def visit(value: Any, parent: str | None, pointer: str, depth: int) -> None:
        if depth > 24:
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, parent, f"{pointer}/{index}", depth + 1)
        elif isinstance(value, dict):
            is_comment = _comment_text(value) and any(key in value for key in ("comment_id", "cid", "reply_id", "id", "id_str", "rest_id", "pk", "author", "user", "create_time", "created_at"))
            next_parent = parent
            if is_comment:
                row = dict(value)
                explicit_parent = _clean_text(_first(value.get("parent_comment_id"), value.get("reply_to_comment_id"), value.get("reply_to_id"), value.get("parentId"), value.get("in_reply_to_status_id_str"), value.get("inReplyToId")), limit=300)
                if not explicit_parent and str(value.get("parent_id") or "").startswith("t1_"):
                    explicit_parent = str(value["parent_id"])[3:]
                row["_parent_comment_id"] = explicit_parent or parent
                row["_raw_pointer"] = pointer
                result.append(row)
                next_parent = _comment_id(row, row["_parent_comment_id"], selected_item_id)
            for key, child in value.items():
                if key not in {"author", "user", "owner", "reactions", "statistics", "quoted_status_result",
                               "retweeted_status_result", "quoted_status", "retweeted_status"}:
                    escaped_key = str(key).replace("~", "~0").replace("/", "~1")
                    visit(child, next_parent, f"{pointer}/{escaped_key}", depth + 1)

    visit(data, None, "", 0)
    return result


def _detail_items(source: str, data: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """沿固定响应容器提取详情，不把任意嵌套字段混成帖子证据。"""
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
            for key in DETAIL_CONTAINERS:
                if key in value:
                    visit(value[key], f"{pointer}/{key}", depth + 1)

    visit(data, "", 0)
    return result


def _detail_shape(source: str, data: dict[str, Any]) -> tuple[bool, int | None, int]:
    def visit(value: Any, depth: int = 0) -> tuple[bool, int | None]:
        if depth > 8:
            return False, None
        if isinstance(value, list):
            counts = [visit(child, depth + 1)[1] for child in value]
            return True, sum(count if count is not None else 1 for count in counts)
        if isinstance(value, dict):
            fields = _source_fields(source, value)
            text = _first(fields.get("text"), fields.get("title"),
                          *(value.get(key) for key in ("full_text", "content", "body", "description", "text", "desc", "title")))
            if _clean_text(text):
                return True, 1
            children = [visit(value[key], depth + 1) for key in DETAIL_CONTAINERS if key in value]
            if children:
                return any(recognized for recognized, _ in children), sum(count or 0 for _, count in children)
        return False, None

    recognized, count = visit(data)
    return recognized, count, 0


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
        row.get("full_text"),
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
    date_fields = _publication_date_fields(
        _first(row.get("create_time"), row.get("created_at"), row.get("timestamp"), row.get("published_at"),
               row.get("time") if source == "xiaohongshu" else None), observed_at
    )
    return {
        "id": f"{source}:comment:{comment_id}",
        "source": source,
        "source_item_id": comment_id,
        "parent_item_id": selected_item_id,
        "parent_comment_id": row.get("_parent_comment_id"),
        "origin_id": selected_item_id,
        "query_id": query_id,
        "url": _url(_first(row.get("url"), row.get("permalink"))) or (f"https://x.com/i/status/{comment_id}" if source == "twitter" and comment_id.isdigit() else parent_url),
        "url_kind": "comment" if _url(_first(row.get("url"), row.get("permalink"))) or source == "twitter" and comment_id.isdigit() else ("parent_post" if parent_url else "unavailable"),
        "parent_url": parent_url,
        "raw_file": raw_file,
        "raw_json_pointer": raw_pointer,
        "author": _clean_text(author, limit=300),
        "container": selected_item_id,
        "title": None,
        "original_text": text,
        "zh_translation": None,
        "language": _language(text, row.get("language")),
        **date_fields,
        "parser_version": PARSER_VERSION,
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


def _request_links(result: dict[str, Any]) -> dict[str, Any]:
    """同一个 HTTP 响应可服务多个意图，保留执行器已归并的关联。"""
    from aor.sources.industries import industry_ids
    return {**{key: deepcopy(result[key]) for key in ('request_ids', 'intent_refs', 'query_metadata', 'collection') if key in result},
            'industry_ids': industry_ids(result)}


def _merge_request_links(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """证据归并时合并请求关联，原文与作者仍属于首次保留的条目。"""
    for key in ('request_ids', 'intent_refs', 'query_metadata', 'industry_ids'):
        value = incoming.get(key)
        if key not in existing and value is not None:
            existing[key] = deepcopy(value)
        elif isinstance(value, list) and isinstance(existing.get(key), list):
            existing[key].extend(deepcopy(entry) for entry in value if entry not in existing[key])
    # 同对象的不同正文不能声明彼此派生来源完全等价；只合并同正文的输入归属。
    from aor.evidence.identity import evidence_content
    if canonical_sha256(evidence_content(existing)) == canonical_sha256(evidence_content(incoming)):
        refs = existing.setdefault("derivation_refs", [])
        refs.extend(deepcopy(ref) for ref in incoming.get("derivation_refs", []) if ref not in refs)
    query_ids = existing.setdefault('query_ids', [existing['query_id']])
    if incoming['query_id'] not in query_ids:
        query_ids.append(incoming['query_id'])


def normalize_documents(
    documents: Iterable[dict[str, Any]],
    *,
    source_files: Iterable[str] | None = None,
    window: dict[str, Any] | None = None,
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
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    seen_comments: dict[tuple[str, str, str], dict[str, Any]] = {}
    request_count = 0
    derive_inputs = []

    for document_index, (document, raw_file) in enumerate(zip(documents_list, files, strict=True)):
        observed_at = _clean_text(document.get("generated_at"), limit=100)
        execution_ref = {"source_execution_sha256": canonical_sha256(document), "source_file": raw_file,
                         "parser_version": PARSER_VERSION}
        status_start = len(request_statuses)
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
                        "parse_status": "upstream_error", "raw_items": None, "parsed_items": 0,
                        "skipped_noncontent_items": 0, "parser_version": PARSER_VERSION,
                    }
                )
                continue
            upstream_error = _upstream_error(result.get("response"))
            if upstream_error:
                request_statuses.append({"id": _clean_text(result.get("id"), limit=300), "source": source,
                                         "status": "upstream_error", "error_code": upstream_error,
                                         "parse_status": "upstream_error", "raw_items": None, "parsed_items": 0,
                                         "skipped_noncontent_items": 0, "parser_version": PARSER_VERSION})
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
                    detail_rows = _detail_items(source, data)
                    parsed_details = 0
                    for row, pointer in detail_rows:
                        detail = _normalize_item(source, row, query_id=query_id, query_group=None, query=None,
                                                 observed_at=observed_at, raw_file=raw_file,
                                                 raw_pointer=f"/results/{result_index}/response/data{pointer}", is_detail=True)
                        if detail is None:
                            continue
                        parsed_details += 1
                        detail.update(_request_links(result))
                        detail["derivation_refs"] = [dict(execution_ref)]
                        detail["id"] = selected_item_id
                        detail["origin_id"] = selected_item_id
                        detail["parent_item_id"] = selected_item_id
                        detail["evidence_kind"] = "post_detail"
                        detail["url"] = detail.get("url") or parent_url
                        detail["comment_candidate"] = None
                        detail_evidence_ids.append(detail["id"])
                        key = (source, selected_item_id)
                        if key in seen:
                            _merge_request_links(seen[key], detail)
                            duplicate_count += 1
                            continue
                        seen[key] = detail
                        evidence.append(detail)
                    recognized, raw_count, skipped = _detail_shape(source, data)
                    record = _parse_record(result, source, recognized=recognized,
                                           raw_count=raw_count, parsed_count=parsed_details, skipped=skipped)
                    detail_status = record["status"]
                    detail_results.append({"source": source, "selected_item_id": selected_item_id,
                                           "query_id": query_id, "status": detail_status, "evidence_ids": detail_evidence_ids})
                    _status_update(statuses, source, detail_status)
                    request_statuses.append(record)
                    continue
                if kind not in {"top_level_comments", "comment_replies"}:
                    raise ValueError(f"不支持的评论深挖 kind：{kind}")
                raw_comments = _extract_comment_items(data, selected_item_id)
                root_count = 0
                if source == "twitter":
                    filtered = [row for row in raw_comments if _comment_id(row) != selected_item_id.split(":")[-1]]
                    root_count = len(raw_comments) - len(filtered)
                    raw_comments = filtered
                normalized_count = 0
                for raw_comment in raw_comments:
                    if kind == "comment_replies" and not raw_comment.get("_parent_comment_id"):
                        raw_comment["_parent_comment_id"] = (result.get("collection") or {}).get("parent_comment_id") or (result.get("params") or {}).get("comment_id")
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
                    normalized_count += 1
                    normalized_comment.update(_request_links(result))
                    normalized_comment["derivation_refs"] = [dict(execution_ref)]
                    marker = (source, selected_item_id, normalized_comment["source_item_id"])
                    if marker in seen_comments:
                        _merge_request_links(seen_comments[marker], normalized_comment)
                        duplicate_count += 1
                        continue
                    seen_comments[marker] = normalized_comment
                    comments.append(normalized_comment)
                recognized, raw_count, skipped = _comment_shape(data)
                if source == "twitter" and recognized and raw_count is not None:
                    raw_count = max(0, raw_count - root_count)
                if source == "twitter" and not recognized and raw_comments:
                    recognized, raw_count = True, len(raw_comments)
                request_statuses.append(_parse_record(result, source, recognized=recognized,
                                                     raw_count=raw_count, parsed_count=normalized_count, skipped=skipped))
                continue
            raw_items = _extract_items(source, data)
            recognized, raw_count, skipped = _search_shape(source, data)
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
                normalized_for_result += 1
                normalized.update(_request_links(result))
                normalized["derivation_refs"] = [dict(execution_ref)]
                key = _dedupe_key(normalized)
                if key in seen:
                    _merge_request_links(seen[key], normalized)
                    duplicate_count += 1
                    continue
                seen[key] = normalized
                normalized["id"] = f"{source}:{key[1].split(':', 1)[1]}"
                normalized["origin_id"] = normalized["id"]
                normalized["evidence_kind"] = "post"
                normalized["query_language"] = _dict(result.get("query_scope")).get("language", "unknown")
                normalized["query_region"] = _dict(result.get("query_scope")).get("country", "unknown")
                if isinstance(result.get("evidence_gap"), dict):
                    normalized["evidence_gap"] = dict(result["evidence_gap"])
                normalized["comment_candidate"] = _comment_candidate(normalized)
                evidence.append(normalized)
            request_statuses.append(_parse_record(result, source, recognized=recognized, raw_count=raw_count,
                                                 parsed_count=normalized_for_result, skipped=skipped))

        parse_states = [row.get("parse_status") for row in request_statuses[status_start:]]
        complete = (bool(parse_states) and all(status in {"parsed", "empty_result"} for status in parse_states)) or not document.get("results")
        derive_inputs.append({**execution_ref, "status": "complete" if complete else "partial"})

    window = window or research_window(as_of)
    for item in [*evidence, *comments]:
        item.update(assess_quality(item, query=item.get('query') or '', as_of=as_of, window=window))
    mark_reposts(evidence)
    statuses = aggregate_status(request_statuses)
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
    from aor.evidence.derivations import build_derive_sets
    derive_sets = build_derive_sets([*evidence, *comments], derive_inputs)
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "derive_sets": derive_sets,
        "input_fingerprints": [{"source_file": file, "sha256": canonical_sha256(document)}
                               for file, document in zip(files, documents_list, strict=True)],
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "provider": "tikhub",
        "run_id": run_id,
        "as_of": as_of,
        "stage": {"search_discovery": "tikhub_normalized_search", "comment_deep_dive": "tikhub_normalized_comments", "evidence_gap_verification": "tikhub_normalized_gaps"}[source_stage],
        "source_stage": source_stage,
        "window": window,
        "source_files": files,
        "stats": {
            "requests_seen": request_count,
            "raw_items": sum(row["raw_items"] or 0 for row in request_statuses),
            "parsed_items": sum(row["parsed_items"] for row in request_statuses),
            "requests_with_unknown_raw_count": sum(row["raw_items"] is None for row in request_statuses),
            "skipped_noncontent_items": sum(row["skipped_noncontent_items"] for row in request_statuses),
            "parse_status": dict(Counter(row["parse_status"] for row in request_statuses)),
            "valid_items": len(evidence),
            "valid_comments": len(comments),
            "recent_valid_items": sum(item['recent_evidence_eligible'] for item in evidence),
            "recent_valid_comments": sum(item['recent_evidence_eligible'] for item in comments),
            "window_status": dict(Counter(item['window_status'] for item in [*evidence, *comments])),
            "relevance_status": dict(Counter(item['relevance_status'] for item in [*evidence, *comments])),
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
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
