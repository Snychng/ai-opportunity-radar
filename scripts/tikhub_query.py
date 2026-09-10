#!/usr/bin/env python3
"""为机会雷达生成、估价并执行受预算保护的 TikHub 查询。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import error, parse, request

from contracts import SCHEMA_VERSION, ContractError, validate_run_as_of


PRICING_URL = "https://user.tikhub.io/api/pricing?page=1&page_size=2000"
DEFAULT_API_BASE = "https://api.tikhub.dev"
ALLOWED_API_BASES = {"https://api.tikhub.dev", "https://api.tikhub.io"}
ACCOUNT_INFO_ENDPOINT = "/api/v1/tikhub/user/get_user_info"
MAX_REQUESTS = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_BATCH_RESULT_BYTES = 32 * 1024 * 1024
SENSITIVE_KEY_PARTS = ("authorization", "cookie", "token", "api_key", "apikey", "secret", "password")
EVIDENCE_GAP_PROMOTIONS = {"rejected_to_b", "rejected_to_r", "r_to_b", "b_to_a", "confirm_rejection"}
# 仅校验已有地域参数；不据此扩展端点或宣称证据来自该地区。
COUNTRY_CODES = frozenset("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split())
TIKHUB_RESULT_STAGES = {"search_discovery", "evidence_gap_verification", "comment_deep_dive"}


def validate_query_locale(country: Any = None, language: Any = None) -> dict[str, str]:
    """显式代码可校验，未知地域与语言保留 unknown。"""
    country = "unknown" if country in (None, "", "unknown") else country
    language = "unknown" if language in (None, "", "unknown") else language
    if not isinstance(country, str) or (country != "unknown" and country not in COUNTRY_CODES):
        raise PlanError("country 必须为已知大写两字母地区代码或 unknown")
    if not isinstance(language, str) or (language != "unknown" and not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}", language)):
        raise PlanError("language 必须为语言代码，例如 ja、pt-BR，或 unknown")
    return {"country": country, "language": language}


def _localized_defaults(source: str, locale: dict[str, str]) -> dict[str, Any]:
    defaults = dict(RADAR_ENDPOINTS[source]["defaults"])
    if source == "tiktok" and locale["country"] != "unknown":
        defaults["region"] = locale["country"]
    if source == "youtube":
        if locale["country"] != "unknown":
            defaults["country_code"] = locale["country"].lower()
        if locale["language"] != "unknown":
            defaults["language_code"] = locale["language"].lower()
    return defaults



class PlanError(ValueError):
    """查询计划不安全、不完整或无法定价。"""


class BudgetExceeded(PlanError):
    """查询的最坏情况费用超过用户显式预算。"""


def _profile(
    endpoint: str,
    method: str,
    keyword_param: str,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "method": method,
        "keyword_param": keyword_param,
        "defaults": defaults,
        "allowed_params": {keyword_param, *defaults.keys()},
    }


# 仅允许与创业痛点发现直接相关的搜索端点。评论深挖将在选出高价值帖子后另建计划。
RADAR_ENDPOINTS: dict[str, dict[str, Any]] = {
    "tiktok": _profile(
        "/api/v1/tiktok/app/v3/fetch_video_search_result",
        "GET",
        "keyword",
        {"offset": 0, "count": 20, "sort_type": 0, "publish_time": 30, "region": "US"},
    ),
    "instagram": _profile(
        "/api/v1/instagram/v2/general_search",
        "GET",
        "keyword",
        {},
    ),
    "linkedin": _profile(
        "/api/v1/linkedin/web/search_posts",
        "GET",
        "keyword",
        {"page": 1},
    ),
    "threads": _profile(
        "/api/v1/threads/web/search_recent",
        "GET",
        "query",
        {},
    ),
    "twitter": _profile(
        "/api/v1/twitter/web/fetch_search_timeline",
        "GET",
        "keyword",
        {"search_type": "Latest"},
    ),
    "youtube": _profile(
        "/api/v1/youtube/web/search_video",
        "GET",
        "search_query",
        {"language_code": "en", "order_by": "this_month", "country_code": "us"},
    ),
    "douyin": _profile(
        "/api/v1/douyin/search/fetch_video_search_v2",
        "POST",
        "keyword",
        {
            "cursor": 0,
            "sort_type": "2",
            "publish_time": "7",
            "filter_duration": "0",
            "content_type": "0",
            "search_id": "",
            "backtrace": "",
        },
    ),
    "xiaohongshu": _profile(
        "/api/v1/xiaohongshu/app_v2/search_notes",
        "GET",
        "keyword",
        {
            "page": 1,
            "sort_type": "general",
            "note_type": "不限",
            "time_filter": "不限",
            "search_id": "",
            "search_session_id": "",
            "source": "explore_feed",
            "ai_mode": 0,
        },
    ),
    "bilibili": _profile(
        "/api/v1/bilibili/web/fetch_general_search",
        "GET",
        "keyword",
        {"order": "pubdate", "page": 1, "page_size": 20, "duration": 0},
    ),
    "zhihu": _profile(
        "/api/v1/zhihu/web/fetch_article_search_v3",
        "GET",
        "keyword",
        {"search_source": "Filter", "vertical": "answer", "offset": "0", "limit": "20"},
    ),
    "wechat_search": _profile(
        "/api/v1/wechat_search/v2/fetch_search",
        "POST",
        "keyword",
        {
            "business_type": "article",
            "sort": "latest",
            "publish_time": "week",
            "offset": 0,
            "cursor": None,
            "raw": False,
        },
    ),
    "reddit": _profile(
        "/api/v1/reddit/app/fetch_dynamic_search",
        "GET",
        "query",
        {
            "search_type": "post",
            "sort": "NEW",
            "time_range": "WEEK",
            "safe_search": "unset",
            "allow_nsfw": "0",
            "after": "",
            "need_format": True,
        },
    ),
}


def _deep_profile(
    *,
    source: str,
    kind: str,
    endpoint: str,
    method: str = "GET",
    required_all: tuple[str, ...] = (),
    required_any: tuple[tuple[str, ...], ...] = (),
    optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    allowed = set(required_all) | set(optional)
    for group in required_any:
        allowed.update(group)
    return {
        "source": source,
        "kind": kind,
        "endpoint": endpoint,
        "method": method,
        "required_all": required_all,
        "required_any": required_any,
        "allowed_params": allowed,
    }


# 每个候选帖子固定拆成详情与一级评论两个请求，翻页必须另建计划并重新估价。
COMMENT_PROFILES: dict[str, dict[str, Any]] = {
    profile["endpoint"]: profile
    for profile in (
        _deep_profile(source="tiktok", kind="detail", endpoint="/api/v1/tiktok/web/fetch_post_detail_v2", required_all=("itemId",)),
        _deep_profile(source="tiktok", kind="top_level_comments", endpoint="/api/v1/tiktok/web/fetch_post_comment", required_all=("aweme_id",)),
        _deep_profile(source="instagram", kind="detail", endpoint="/api/v1/instagram/v2/fetch_post_info", required_all=("code_or_url",)),
        _deep_profile(source="instagram", kind="top_level_comments", endpoint="/api/v1/instagram/v2/fetch_post_comments", required_all=("code_or_url",)),
        _deep_profile(source="linkedin", kind="detail", endpoint="/api/v1/linkedin/web/get_post_detail", required_all=("post_id",)),
        _deep_profile(source="linkedin", kind="top_level_comments", endpoint="/api/v1/linkedin/web/get_post_comments", required_all=("post_id",)),
        _deep_profile(source="threads", kind="detail", endpoint="/api/v1/threads/web/fetch_post_detail_v2", required_all=("post_id",)),
        _deep_profile(source="threads", kind="top_level_comments", endpoint="/api/v1/threads/web/fetch_post_comments", required_all=("post_id",)),
        _deep_profile(source="twitter", kind="detail", endpoint="/api/v1/twitter/web/fetch_tweet_detail", required_all=("tweet_id",)),
        _deep_profile(source="twitter", kind="top_level_comments", endpoint="/api/v1/twitter/web/fetch_post_comments", required_all=("tweet_id",)),
        _deep_profile(source="youtube", kind="detail", endpoint="/api/v1/youtube/web_v2/get_video_info_v2", required_all=("video_id",)),
        _deep_profile(source="youtube", kind="top_level_comments", endpoint="/api/v1/youtube/web_v2/get_video_comments", required_all=("video_id",)),
        _deep_profile(source="douyin", kind="detail", endpoint="/api/v1/douyin/web/fetch_one_video_v2", required_all=("aweme_id",)),
        _deep_profile(source="douyin", kind="top_level_comments", endpoint="/api/v1/douyin/web/fetch_video_comments", required_all=("aweme_id",)),
        _deep_profile(
            source="xiaohongshu",
            kind="detail",
            endpoint="/api/v1/xiaohongshu/app_v2/get_image_note_detail",
            required_any=(("note_id", "share_text"),),
        ),
        _deep_profile(
            source="xiaohongshu",
            kind="detail",
            endpoint="/api/v1/xiaohongshu/app_v2/get_video_note_detail",
            required_any=(("note_id", "share_text"),),
        ),
        _deep_profile(
            source="xiaohongshu",
            kind="top_level_comments",
            endpoint="/api/v1/xiaohongshu/app_v2/get_note_comments",
            required_any=(("note_id", "share_text"),),
        ),
        _deep_profile(source="bilibili", kind="detail", endpoint="/api/v1/bilibili/web/fetch_one_video", required_all=("bv_id",)),
        _deep_profile(source="bilibili", kind="top_level_comments", endpoint="/api/v1/bilibili/web/fetch_video_comments", required_all=("bv_id",)),
        _deep_profile(source="zhihu", kind="detail", endpoint="/api/v1/zhihu/web/fetch_answer_detail", required_all=("answer_id",)),
        _deep_profile(source="zhihu", kind="top_level_comments", endpoint="/api/v1/zhihu/web/fetch_comment_v5", required_all=("answer_id",)),
        _deep_profile(
            source="wechat_search",
            kind="detail",
            endpoint="/api/v1/wechat_mp/v2/fetch_article_detail",
            method="POST",
            required_all=("url",),
        ),
        _deep_profile(
            source="wechat_search",
            kind="top_level_comments",
            endpoint="/api/v1/wechat_mp/v2/fetch_article_comments",
            method="POST",
            required_all=("url",),
        ),
        _deep_profile(source="reddit", kind="detail", endpoint="/api/v1/reddit/app/fetch_post_details", required_all=("post_id",)),
        _deep_profile(source="reddit", kind="top_level_comments", endpoint="/api/v1/reddit/app/fetch_post_comments", required_all=("post_id",)),
    )
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")
    return slug[:50] or "query"


def build_search_plan(*, as_of: str, run_id: str, query_groups: list[dict[str, Any]]) -> dict[str, Any]:
    """把少量关键词组展开为可执行且不含凭证的 TikHub 请求。"""
    try:
        validate_run_as_of(run_id, as_of)
    except ContractError as exc:
        raise PlanError(str(exc)) from exc
    if not isinstance(query_groups, list) or not query_groups:
        raise PlanError("query_groups 必须是非空数组")
    if len(query_groups) > 20:
        raise PlanError("单个计划最多包含 20 个关键词组")

    requests: list[dict[str, Any]] = []
    for group_index, group in enumerate(query_groups, start=1):
        if not isinstance(group, dict):
            raise PlanError("每个关键词组必须是 JSON 对象")
        group_id = _slug(str(group.get("id", f"group-{group_index}")))
        keyword = str(group.get("keyword", "")).strip()
        if not 1 <= len(keyword) <= 100:
            raise PlanError(f"关键词组 {group_id} 必须包含 1 到 100 个字符")
        locale = validate_query_locale(group.get("country"), group.get("language"))
        sources = group.get("sources")
        if not isinstance(sources, list) or not sources:
            raise PlanError(f"关键词组 {group_id} 必须指定来源")
        for source_index, source_value in enumerate(sources, start=1):
            source = str(source_value).strip().lower()
            profile = RADAR_ENDPOINTS.get(source)
            if profile is None:
                raise PlanError(f"不支持的 TikHub 雷达来源：{source}")
            params = {profile["keyword_param"]: keyword, **_localized_defaults(source, locale)}
            requests.append(
                {
                    "id": f"{group_id}-{source}-{source_index}",
                    "query_group": group_id,
                    "query_scope": dict(locale),
                    "source": source,
                    "endpoint": profile["endpoint"],
                    "method": profile["method"],
                    "params": params,
                }
            )

    if len(requests) > MAX_REQUESTS:
        raise PlanError(f"单个计划最多包含 {MAX_REQUESTS} 个请求")
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "tikhub",
        "run_id": run_id,
        "as_of": as_of,
        "stage": "search_discovery",
        "scope": {"id": "phase_1_existing_platforms", "platform_expansion_enabled": False},
        "requests": requests,
        "cost_policy": {
            "price_source": "live_dashboard_before_run",
            "discount_policy": "conservative_list_price_unless_explicit_rate",
            "max_attempts": 1,
            "comments_are_separate_plan": True,
        },
    }


def build_evidence_gap_plan(
    *,
    as_of: str,
    run_id: str,
    gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """把已经识别的候选证据缺口转换为可审计的定向搜索计划。"""
    if not isinstance(gaps, list) or not 1 <= len(gaps) <= 10:
        raise PlanError("付费补证计划每次必须包含 1 到 10 个证据缺口")
    groups: list[dict[str, Any]] = []
    gap_metadata: dict[str, dict[str, str]] = {}
    for index, gap in enumerate(gaps, start=1):
        if not isinstance(gap, dict):
            raise PlanError(f"第 {index} 个证据缺口必须是 JSON 对象")
        candidate_id = str(gap.get("candidate_id") or "").strip()
        missing_gate = str(gap.get("missing_gate") or "").strip()
        target_region = str(gap.get("target_region") or "").strip()
        expected_promotion = str(gap.get("expected_promotion") or "").strip()
        keyword = str(gap.get("keyword") or "").strip()
        sources = gap.get("sources")
        if not re.fullmatch(r"(?:OPP|SIG|CAND)-[A-Za-z0-9._-]{3,64}", candidate_id):
            raise PlanError(f"第 {index} 个证据缺口 candidate_id 不合法")
        for label, value in (("missing_gate", missing_gate), ("target_region", target_region)):
            if not 1 <= len(value) <= 200:
                raise PlanError(f"第 {index} 个证据缺口 {label} 必须包含 1 到 200 个字符")
        if expected_promotion not in EVIDENCE_GAP_PROMOTIONS:
            raise PlanError(
                f"第 {index} 个证据缺口 expected_promotion 必须为：{', '.join(sorted(EVIDENCE_GAP_PROMOTIONS))}"
            )
        if not 1 <= len(keyword) <= 100:
            raise PlanError(f"第 {index} 个证据缺口 keyword 必须包含 1 到 100 个字符")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
            raise PlanError(f"第 {index} 个证据缺口 sources 必须包含 1 到 3 个来源")
        normalized_sources = [str(source).strip().lower() for source in sources]
        if any(not source for source in normalized_sources) or len(set(normalized_sources)) != len(normalized_sources):
            raise PlanError(f"第 {index} 个证据缺口 sources 不能包含空值或重复来源")
        group_id = _slug(f"gap-{candidate_id}-{index}")
        groups.append({"id": group_id, "keyword": keyword, "sources": normalized_sources,
                       "country": gap.get("country"), "language": gap.get("language")})
        gap_metadata[group_id] = {
            "candidate_id": candidate_id,
            "missing_gate": missing_gate,
            "target_region": target_region,
            "expected_promotion": expected_promotion,
        }

    plan = build_search_plan(as_of=as_of, run_id=run_id, query_groups=groups)
    per_source: dict[str, int] = defaultdict(int)
    for item in plan["requests"]:
        per_source[item["source"]] += 1
    over_limit = sorted(source for source, count in per_source.items() if count > 3)
    if over_limit:
        raise PlanError(f"单次补证计划每个来源最多 3 个请求；请先执行并评估产出：{', '.join(over_limit)}")
    plan["stage"] = "evidence_gap_verification"
    plan["evidence_gaps"] = list(gap_metadata.values())
    for item in plan["requests"]:
        item["evidence_gap"] = gap_metadata[item["query_group"]]
    plan["cost_policy"]["purpose"] = "candidate_gate_verification_only"
    plan["cost_policy"]["stop_after_requests_without_yield"] = 3
    return plan


def _required_identifier(identifiers: dict[str, Any], *names: str) -> str:
    for name in names:
        value = identifiers.get(name)
        if isinstance(value, str) and value.strip():
            if len(value.strip()) > 500:
                raise PlanError(f"标识符 {name} 最长 500 字符")
            return value.strip()
    raise PlanError(f"缺少标识符：{' 或 '.join(names)}")


def _comment_pair(source: str, identifiers: dict[str, Any], content_type: str | None) -> list[dict[str, Any]]:
    if source == "tiktok":
        aweme_id = _required_identifier(identifiers, "aweme_id", "itemId")
        return [
            {"kind": "detail", "endpoint": "/api/v1/tiktok/web/fetch_post_detail_v2", "method": "GET", "params": {"itemId": aweme_id}},
            {"kind": "top_level_comments", "endpoint": "/api/v1/tiktok/web/fetch_post_comment", "method": "GET", "params": {"aweme_id": aweme_id}},
        ]
    mappings = {
        "instagram": ("code_or_url", "/api/v1/instagram/v2/fetch_post_info", "/api/v1/instagram/v2/fetch_post_comments", "GET"),
        "linkedin": ("post_id", "/api/v1/linkedin/web/get_post_detail", "/api/v1/linkedin/web/get_post_comments", "GET"),
        "threads": ("post_id", "/api/v1/threads/web/fetch_post_detail_v2", "/api/v1/threads/web/fetch_post_comments", "GET"),
        "twitter": ("tweet_id", "/api/v1/twitter/web/fetch_tweet_detail", "/api/v1/twitter/web/fetch_post_comments", "GET"),
        "youtube": ("video_id", "/api/v1/youtube/web_v2/get_video_info_v2", "/api/v1/youtube/web_v2/get_video_comments", "GET"),
        "douyin": ("aweme_id", "/api/v1/douyin/web/fetch_one_video_v2", "/api/v1/douyin/web/fetch_video_comments", "GET"),
        "bilibili": ("bv_id", "/api/v1/bilibili/web/fetch_one_video", "/api/v1/bilibili/web/fetch_video_comments", "GET"),
        "zhihu": ("answer_id", "/api/v1/zhihu/web/fetch_answer_detail", "/api/v1/zhihu/web/fetch_comment_v5", "GET"),
        "wechat_search": ("url", "/api/v1/wechat_mp/v2/fetch_article_detail", "/api/v1/wechat_mp/v2/fetch_article_comments", "POST"),
        "reddit": ("post_id", "/api/v1/reddit/app/fetch_post_details", "/api/v1/reddit/app/fetch_post_comments", "GET"),
    }
    if source == "xiaohongshu":
        if content_type not in {"image_note", "video_note"}:
            raise PlanError("小红书评论深挖必须指定 content_type=image_note 或 video_note")
        key = "note_id" if identifiers.get("note_id") else "share_text"
        value = _required_identifier(identifiers, "note_id", "share_text")
        detail_endpoint = (
            "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
            if content_type == "image_note"
            else "/api/v1/xiaohongshu/app_v2/get_video_note_detail"
        )
        return [
            {"kind": "detail", "endpoint": detail_endpoint, "method": "GET", "params": {key: value}},
            {"kind": "top_level_comments", "endpoint": "/api/v1/xiaohongshu/app_v2/get_note_comments", "method": "GET", "params": {key: value}},
        ]
    mapping = mappings.get(source)
    if mapping is None:
        raise PlanError(f"不支持的 TikHub 评论深挖来源：{source}")
    param_name, detail_endpoint, comments_endpoint, method = mapping
    if source == "zhihu" and content_type != "answer":
        raise PlanError("知乎当前只允许 content_type=answer 的回答评论深挖")
    if source == "wechat_search" and content_type != "article":
        raise PlanError("微信搜一搜结果必须以 content_type=article 切换到公众号接口")
    value = _required_identifier(identifiers, param_name)
    if source == "reddit" and not value.startswith("t3_"):
        value = f"t3_{value}"
    if source == "wechat_search":
        parsed_url = parse.urlsplit(value)
        if parsed_url.scheme != "https" or parsed_url.hostname != "mp.weixin.qq.com":
            raise PlanError("微信文章 URL 仅允许 https://mp.weixin.qq.com/ 域名")
    params = {param_name: value}
    return [
        {"kind": "detail", "endpoint": detail_endpoint, "method": method, "params": params},
        {"kind": "top_level_comments", "endpoint": comments_endpoint, "method": method, "params": dict(params)},
    ]


def build_comment_plan(
    *,
    as_of: str,
    parent_search_run_id: str,
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    """为人工或模型选出的 1–5 个高价值帖子生成独立评论深挖计划。"""
    try:
        validate_run_as_of(parent_search_run_id, as_of)
    except ContractError as exc:
        raise PlanError(f"parent_search_run_id 不合法：{exc}") from exc
    if not isinstance(selections, list) or not 1 <= len(selections) <= 5:
        raise PlanError("评论深挖每次必须选择 1 到 5 个帖子")
    requests: list[dict[str, Any]] = []
    selected_items: list[dict[str, Any]] = []
    for index, selection in enumerate(selections, start=1):
        if not isinstance(selection, dict):
            raise PlanError("每个评论候选必须是 JSON 对象")
        source = str(selection.get("source", "")).strip().lower()
        selected_item_id = str(selection.get("selected_item_id", "")).strip()
        reason = str(selection.get("selection_reason", "")).strip()
        parent_url = selection.get("parent_url")
        if parent_url is not None and (not isinstance(parent_url, str) or len(parent_url) > 2000 or not parent_url.startswith(("https://", "http://"))):
            raise PlanError(f"第 {index} 个候选 parent_url 必须为公开网页 URL")
        if not selected_item_id or len(selected_item_id) > 200:
            raise PlanError(f"第 {index} 个候选缺少合法 selected_item_id")
        if not reason or len(reason) > 500:
            raise PlanError(f"第 {index} 个候选缺少 1 到 500 字符的 selection_reason")
        identifiers = selection.get("identifiers")
        if not isinstance(identifiers, dict):
            raise PlanError(f"第 {index} 个候选 identifiers 必须是对象")
        for key, value in identifiers.items():
            if _is_sensitive_key(key):
                raise PlanError(f"第 {index} 个候选禁止携带敏感标识符：{key}")
            _validate_scalar(value, location=f"第 {index} 个候选标识符 {key}")
        content_type_value = selection.get("content_type")
        content_type = str(content_type_value).strip() if content_type_value is not None else None
        pair = _comment_pair(source, identifiers, content_type)
        selection_key = f"selected-{index}-{_slug(source)}"
        for request_item in pair:
            requests.append(
                {
                    "id": f"{selection_key}-{request_item['kind']}",
                    "source": source,
                    "kind": request_item["kind"],
                    "selected_item_id": selected_item_id,
                    "parent_url": parent_url,
                    "selection_reason": reason,
                    "content_type": content_type,
                    "endpoint": request_item["endpoint"],
                    "method": request_item["method"],
                    "params": request_item["params"],
                }
            )
        selected_items.append(
            {
                "source": source,
                "selected_item_id": selected_item_id,
                "selection_reason": reason,
                "content_type": content_type,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "tikhub",
        "run_id": parent_search_run_id,
        "as_of": as_of,
        "stage": "comment_deep_dive",
        "parent_search_run_id": parent_search_run_id,
        "selected_items": selected_items,
        "scope": {"id": "phase_1_existing_platforms", "platform_expansion_enabled": False},
        "requests": requests,
        "cost_policy": {
            "price_source": "live_dashboard_before_run",
            "budget_scope": "separate_from_search",
            "max_attempts": 1,
            "one_page_only": True,
            "pagination_requires_new_plan": True,
        },
    }


def normalize_pricing_rows(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把控制台价格表转成按端点索引的安全结构。"""
    catalog: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        endpoint = str(raw.get("endpoint_uri", "")).strip()
        if not endpoint.startswith("/api/"):
            continue
        try:
            cost = Decimal(str(raw.get("endpoint_cost")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not cost.is_finite() or cost < 0:
            continue
        candidate = {
            "endpoint_uri": endpoint,
            "endpoint_cost": cost,
            # 价格目录字段必须是真正的 JSON boolean；字符串等异常类型按最保守的 False 处理。
            "allow_free_credit": raw.get("allow_free_credit") is True,
            "allow_discount": raw.get("allow_discount") is True,
            "platform": str(raw.get("platform", "unknown")),
        }
        previous = catalog.get(endpoint)
        if previous is None:
            catalog[endpoint] = candidate
        else:
            # 重复价格行按最高价与最保守资格合并，避免目录异常导致低估。
            catalog[endpoint] = {
                "endpoint_uri": endpoint,
                "endpoint_cost": max(previous["endpoint_cost"], candidate["endpoint_cost"]),
                "allow_free_credit": previous["allow_free_credit"] and candidate["allow_free_credit"],
                "allow_discount": previous["allow_discount"] and candidate["allow_discount"],
                "platform": previous["platform"],
            }
    return catalog


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _validate_scalar(value: Any, *, location: str) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, str) and len(value) <= 500:
        return
    raise PlanError(f"{location} 只允许长度不超过 500 的标量值")


def validate_plan(plan: dict[str, Any], pricing_rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """校验来源、端点、参数、请求量和实时价格。"""
    if not isinstance(plan, dict) or plan.get("provider") != "tikhub":
        raise PlanError("计划 provider 必须为 tikhub")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise PlanError(f"TikHub 计划 schema_version 必须为 {SCHEMA_VERSION}")
    try:
        validate_run_as_of(plan.get("run_id"), plan.get("as_of"))
    except ContractError as exc:
        raise PlanError(str(exc)) from exc
    scope = plan.get("scope")
    if not isinstance(scope, dict) or scope.get("id") != "phase_1_existing_platforms":
        raise PlanError("TikHub 计划缺少一期范围声明")
    if scope.get("platform_expansion_enabled") is not False:
        raise PlanError("TikHub 计划禁止启用平台自动扩展")
    requests = plan.get("requests")
    if not isinstance(requests, list) or not requests:
        raise PlanError("计划必须包含非空 requests")
    if len(requests) > MAX_REQUESTS:
        raise PlanError(f"单个计划最多包含 {MAX_REQUESTS} 个请求")
    stage = str(plan.get("stage") or "search_discovery")
    if stage not in TIKHUB_RESULT_STAGES:
        raise PlanError(f"不支持的 TikHub 计划阶段：{stage}")
    if stage == "comment_deep_dive":
        if plan.get("parent_search_run_id") != plan.get("run_id"):
            raise PlanError("评论深挖计划缺少合法 parent_search_run_id")
        if len(requests) > 10 or len(requests) % 2 != 0:
            raise PlanError("评论深挖计划应为 1 到 5 个帖子各两次请求")
    if stage == "evidence_gap_verification":
        cost_policy = plan.get("cost_policy")
        if not isinstance(cost_policy, dict) or cost_policy.get("purpose") != "candidate_gate_verification_only":
            raise PlanError("定向补证计划 cost_policy.purpose 不合法")
        if cost_policy.get("stop_after_requests_without_yield") != 3:
            raise PlanError("定向补证计划必须在连续 3 个请求无产出后停止")

    catalog = normalize_pricing_rows(pricing_rows)
    seen_ids: set[str] = set()
    source_request_counts: dict[str, int] = defaultdict(int)
    for index, item in enumerate(requests, start=1):
        if not isinstance(item, dict):
            raise PlanError(f"第 {index} 个请求必须是 JSON 对象")
        request_id = str(item.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", request_id):
            raise PlanError(f"第 {index} 个请求 id 不合法")
        if request_id in seen_ids:
            raise PlanError(f"请求 id 重复：{request_id}")
        seen_ids.add(request_id)

        endpoint = str(item.get("endpoint", "")).strip()
        source = str(item.get("source", "")).lower()
        source_request_counts[source] += 1
        if stage in {"search_discovery", "evidence_gap_verification"}:
            profile = RADAR_ENDPOINTS.get(source)
            if profile is None:
                raise PlanError(f"不支持的 TikHub 雷达来源：{source}")
            if endpoint != profile["endpoint"]:
                raise PlanError(f"来源 {source} 不允许端点 {endpoint}")
        else:
            profile = COMMENT_PROFILES.get(endpoint)
            if profile is None or profile["source"] != source:
                raise PlanError(f"来源 {source} 不允许评论深挖端点 {endpoint}")
            if item.get("kind") != profile["kind"]:
                raise PlanError(f"端点 {endpoint} 的 kind 应为 {profile['kind']}")
        if endpoint not in catalog:
            raise PlanError(f"TikHub 实时价格表缺少端点：{endpoint}")
        method = str(item.get("method", "")).upper()
        if method != profile["method"]:
            raise PlanError(f"端点 {endpoint} 的方法应为 {profile['method']}")

        params = item.get("params")
        if not isinstance(params, dict):
            raise PlanError(f"请求 {request_id} 的 params 必须是对象")
        for key, value in params.items():
            if _is_sensitive_key(key):
                raise PlanError(f"请求 {request_id} 禁止携带敏感参数：{key}")
            if key not in profile["allowed_params"]:
                raise PlanError(f"请求 {request_id} 包含未允许参数：{key}")
            _validate_scalar(value, location=f"请求 {request_id} 参数 {key}")
        if stage in {"search_discovery", "evidence_gap_verification"}:
            query_scope = item.get("query_scope", {})
            if not isinstance(query_scope, dict) or set(query_scope) - {"country", "language"}:
                raise PlanError(f"请求 {request_id} 的 query_scope 不合法")
            locale = validate_query_locale(query_scope.get("country"), query_scope.get("language"))
            for key, expected in _localized_defaults(source, locale).items():
                actual = params.get(key, object())
                if type(actual) is not type(expected) or actual != expected:
                    raise PlanError(f"请求 {request_id} 参数 {key} 必须固定为 {expected!r}")
            keyword = params.get(profile["keyword_param"])
            if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 100:
                raise PlanError(f"请求 {request_id} 缺少合法关键词")
            if stage == "evidence_gap_verification":
                gap = item.get("evidence_gap")
                if not isinstance(gap, dict):
                    raise PlanError(f"请求 {request_id} 缺少 evidence_gap")
                required_gap_fields = ("candidate_id", "missing_gate", "target_region", "expected_promotion")
                if any(not str(gap.get(field) or "").strip() for field in required_gap_fields):
                    raise PlanError(f"请求 {request_id} 的 evidence_gap 字段不完整")
                if not re.fullmatch(r"(?:OPP|SIG|CAND)-[A-Za-z0-9._-]{3,64}", str(gap["candidate_id"])):
                    raise PlanError(f"请求 {request_id} 的 evidence_gap.candidate_id 不合法")
                if str(gap["expected_promotion"]) not in EVIDENCE_GAP_PROMOTIONS:
                    raise PlanError(f"请求 {request_id} 的 evidence_gap.expected_promotion 不合法")
        else:
            for key in profile["required_all"]:
                value = params.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise PlanError(f"请求 {request_id} 缺少必填参数 {key}")
            for group in profile["required_any"]:
                if not any(
                    params.get(key) is not None
                    and (not isinstance(params.get(key), str) or bool(str(params.get(key)).strip()))
                    for key in group
                ):
                    raise PlanError(f"请求 {request_id} 至少需要参数：{' 或 '.join(group)}")
    if stage == "evidence_gap_verification":
        over_limit = sorted(source for source, count in source_request_counts.items() if count > 3)
        if over_limit:
            raise PlanError(f"定向补证计划每个来源最多 3 个请求：{', '.join(over_limit)}")
    return catalog


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001")))


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _catalog_sha256(catalog: dict[str, dict[str, Any]]) -> str:
    serializable = [
        {
            "endpoint_uri": endpoint,
            "endpoint_cost": format(row["endpoint_cost"], "f"),
            "allow_free_credit": row["allow_free_credit"],
            "allow_discount": row["allow_discount"],
            "platform": row["platform"],
        }
        for endpoint, row in sorted(catalog.items())
    ]
    return _canonical_sha256(serializable)


def estimate_plan(
    plan: dict[str, Any],
    pricing_rows: Iterable[dict[str, Any]],
    *,
    usd_to_cny: float = 7.2,
    discount_rate: float = 1.0,
    max_attempts: int = 1,
    price_source: str = "provided_pricing_catalog",
    price_observed_at: str | None = None,
    pricing_url: str | None = None,
) -> dict[str, Any]:
    """使用控制台实时标价生成保守费用估算。"""
    catalog = validate_plan(plan, pricing_rows)
    try:
        fx = Decimal(str(usd_to_cny))
        discount = Decimal(str(discount_rate))
    except InvalidOperation as exc:
        raise PlanError("汇率和折扣必须是数字") from exc
    if not fx.is_finite() or fx <= 0:
        raise PlanError("usd_to_cny 必须大于 0")
    if not discount.is_finite() or not Decimal("0") < discount <= Decimal("1"):
        raise PlanError("discount_rate 必须在 0 到 1 之间")
    if max_attempts not in {1, 2, 3}:
        raise PlanError("max_attempts 只允许 1、2 或 3")

    list_total = Decimal("0")
    estimated_total = Decimal("0")
    free_eligible_total = Decimal("0")
    paid_required_total = Decimal("0")
    free_eligible_list_total = Decimal("0")
    free_ineligible_list_total = Decimal("0")
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "list_price_usd": Decimal("0"),
            "estimated_cost_usd": Decimal("0"),
            "all_allow_free_credit": True,
        }
    )
    per_request: list[dict[str, Any]] = []

    for item in plan["requests"]:
        price = catalog[item["endpoint"]]
        list_cost = price["endpoint_cost"]
        effective_cost = list_cost * discount if price["allow_discount"] else list_cost
        list_total += list_cost
        estimated_total += effective_cost
        if price["allow_free_credit"]:
            free_eligible_total += effective_cost
            free_eligible_list_total += list_cost
        else:
            paid_required_total += effective_cost
            free_ineligible_list_total += list_cost
        source = item["source"]
        row = grouped[source]
        row["calls"] += 1
        row["list_price_usd"] += list_cost
        row["estimated_cost_usd"] += effective_cost
        row["all_allow_free_credit"] = row["all_allow_free_credit"] and price["allow_free_credit"]
        per_request.append(
            {
                "id": item["id"],
                "source": source,
                "endpoint": item["endpoint"],
                "unit_list_price_usd": _money(list_cost),
                "unit_estimated_price_usd": _money(effective_cost),
                "allow_free_credit": price["allow_free_credit"],
                "allow_discount": price["allow_discount"],
            }
        )

    by_source = []
    for source in sorted(grouped):
        row = grouped[source]
        by_source.append(
            {
                "source": source,
                "calls": row["calls"],
                "list_price_usd": _money(row["list_price_usd"]),
                "estimated_cost_usd": _money(row["estimated_cost_usd"]),
                "all_allow_free_credit": row["all_allow_free_credit"],
            }
        )

    return {
        "schema_version": "1.0",
        "provider": "tikhub",
        "price_source": price_source,
        "price_observed_at": price_observed_at or datetime.now(timezone.utc).isoformat(),
        "pricing_url": pricing_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": _canonical_sha256(plan),
        "catalog_sha256": _catalog_sha256(catalog),
        "request_count": len(plan["requests"]),
        "usd_to_cny": float(fx),
        "discount_rate": float(discount),
        "max_attempts": max_attempts,
        "list_price_usd": _money(list_total),
        "estimated_cost_usd": _money(estimated_total),
        "estimated_cost_cny": _money(estimated_total * fx),
        "worst_case_cost_usd": _money(list_total * max_attempts),
        "worst_case_cost_cny": _money(list_total * max_attempts * fx),
        "budget_guard_cost_usd": format(list_total * max_attempts, "f"),
        "budget_guard_free_credit_eligible_usd": format(free_eligible_list_total * max_attempts, "f"),
        "budget_guard_free_credit_ineligible_usd": format(free_ineligible_list_total * max_attempts, "f"),
        "free_credit_eligible_cost_usd": _money(free_eligible_total),
        "free_credit_ineligible_cost_usd": _money(paid_required_total),
        "all_requests_allow_free_credit": paid_required_total == 0,
        "by_source": by_source,
        "requests": per_request,
        "assumptions": [
            "按每个请求一页计算，评论深挖另行估价",
            "默认使用公开标价；只有显式 discount_rate 才计入折扣",
            "失败请求是否计费以 TikHub 账单为准，因此预算按目录原价乘最大尝试次数保护",
        ],
    }


def enforce_budget(estimate: dict[str, Any], max_cost_usd: float) -> None:
    try:
        budget = Decimal(str(max_cost_usd))
        worst = Decimal(str(estimate.get("budget_guard_cost_usd", estimate["worst_case_cost_usd"])))
    except (InvalidOperation, KeyError) as exc:
        raise PlanError("预算或估价格式无效") from exc
    if not budget.is_finite() or not worst.is_finite():
        raise PlanError("预算和最坏情况估价必须是有限数字")
    if budget < 0:
        raise PlanError("max_cost_usd 不能为负数")
    if worst > budget:
        raise BudgetExceeded(f"最坏情况预计费用 ${worst} 超过预算上限 ${budget}")


def _redact_text(value: str, secret: str) -> str:
    redacted = value.replace(secret, "[REDACTED]") if secret else value
    patterns = (
        (r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]"),
        (r"(?i)\b(authorization|api[_-]?key|token|cookie)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]"),
    )
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted[:2000]


def _sanitize(value: Any, secret: str) -> Any:
    """递归去掉第三方响应中的凭证字段和值。"""
    if isinstance(value, dict):
        return {key: _sanitize(item, secret) for key, item in value.items() if not _is_sensitive_key(key)}
    if isinstance(value, list):
        return [_sanitize(item, secret) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secret)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


class _SameOriginRedirectHandler(request.HTTPRedirectHandler):
    """只允许同协议、同主机重定向，避免 Authorization 被转发到第三方。"""

    def redirect_request(
        self,
        req: request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> request.Request | None:
        old = parse.urlsplit(req.full_url)
        new = parse.urlsplit(parse.urljoin(req.full_url, newurl))
        if old.scheme != new.scheme or old.netloc != new.netloc:
            raise error.HTTPError(req.full_url, code, "拒绝跨域或降级重定向", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_urlopen(req: request.Request, *, timeout: int) -> Any:
    opener = request.build_opener(_SameOriginRedirectHandler())
    return opener.open(req, timeout=timeout)


def _default_transport(
    *,
    method: str,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    if method == "GET":
        query = parse.urlencode({key: value for key, value in params.items() if value is not None})
        target = f"{url}?{query}" if query else url
        req = request.Request(target, headers=headers, method="GET")
    else:
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
    try:
        with _safe_urlopen(req, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        # 不持久化第三方错误正文，避免正文中的 Cookie、令牌或个人信息进入报告。
        raise RuntimeError(f"TikHub HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"TikHub 网络错误：{exc.reason}") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("TikHub 响应超过 8 MiB 安全上限")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("TikHub 返回了无法解析的 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("TikHub 响应必须是 JSON 对象")
    return data


def _classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "http 401" in message or "http 403" in message:
        return "auth_error"
    if "http 429" in message:
        return "rate_limited"
    if "http 408" in message or isinstance(exc, TimeoutError) or "timed out" in message or "超时" in message:
        return "timeout"
    status_match = re.search(r"http\s+(\d{3})", message)
    if status_match and 500 <= int(status_match.group(1)) <= 599:
        return "server_error"
    if "超过 8 mib" in message or "response_too_large" in message:
        return "response_too_large"
    if "无法解析" in message or "响应必须是 json" in message:
        return "invalid_response"
    if "网络错误" in message or "urlerror" in message:
        return "network_error"
    return "request_error"


def _assert_application_success(payload: Any) -> None:
    """把 HTTP 200 内的 TikHub 业务错误转换为显式失败。"""
    if not isinstance(payload, dict):
        raise RuntimeError("TikHub 响应必须是 JSON 对象")
    code = payload.get("code")
    if code is not None and code not in {0, 200, "0", "200"}:
        raise RuntimeError(f"TikHub API error code {str(code)[:40]}")
    if payload.get("success") is False:
        raise RuntimeError("TikHub API success=false")


def _is_retryable_error(exc: Exception) -> bool:
    return _classify_error(exc) in {"rate_limited", "timeout", "network_error", "server_error"}


def _nonnegative_decimal(value: Any, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PlanError(f"TikHub 账户字段 {field} 不是合法金额") from exc
    if not number.is_finite() or number < 0:
        raise PlanError(f"TikHub 账户字段 {field} 必须是非负有限金额")
    return number


def fetch_account_snapshot(
    *,
    token: str,
    api_base: str,
    timeout: int = 45,
    transport: Callable[..., dict[str, Any]] = _default_transport,
) -> dict[str, Any]:
    """通过零费用端点检查账户可用性，只保留余额白名单字段。"""
    secret = token.strip()
    if not secret:
        raise PlanError("缺少 TIKHUB_API_KEY；密钥只能通过环境变量提供")
    base = api_base.rstrip("/")
    if base not in ALLOWED_API_BASES:
        raise PlanError("api_base 仅允许 TikHub 官方域名")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {secret}",
        "User-Agent": "AI-Opportunity-Radar/3.0",
    }
    try:
        payload = transport(
            method="GET",
            url=f"{base}{ACCOUNT_INFO_ENDPOINT}",
            params={},
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:
        # 不传播第三方错误正文，避免其中的令牌或个人信息进入日志。
        raise PlanError(f"TikHub 账户预检失败（{_classify_error(exc)}）") from exc
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise PlanError("TikHub 账户预检返回格式无效")
    user_data = payload.get("user_data")
    api_key_data = payload.get("api_key_data")
    if not isinstance(user_data, dict) or not isinstance(api_key_data, dict):
        raise PlanError("TikHub 账户预检缺少账户或密钥状态")
    if user_data.get("account_disabled") is not False or user_data.get("is_active") is not True:
        raise PlanError("TikHub 账户当前不可用")
    if type(api_key_data.get("api_key_status")) is not int or api_key_data.get("api_key_status") != 1:
        raise PlanError("TikHub API Key 当前不可用")
    balance = _nonnegative_decimal(user_data.get("balance"), field="balance")
    free_credit = _nonnegative_decimal(user_data.get("free_credit"), field="free_credit")
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "balance_usd": _money(balance),
        "balance_usd_exact": format(balance, "f"),
        "free_credit_usd": _money(free_credit),
        "free_credit_usd_exact": format(free_credit, "f"),
    }


def _execute_plan_with_pricing(
    plan: dict[str, Any],
    pricing_rows: Iterable[dict[str, Any]],
    *,
    token: str,
    max_cost_usd: float,
    usd_to_cny: float = 7.2,
    discount_rate: float = 1.0,
    max_attempts: int = 1,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 45,
    transport: Callable[..., dict[str, Any]] = _default_transport,
    account_transport: Callable[..., dict[str, Any]] = _default_transport,
    price_source: str = "provided_pricing_catalog",
    price_observed_at: str | None = None,
    pricing_url: str | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """使用已验证价格执行计划；仅供生产入口和测试调用。"""
    secret = token.strip()
    if not secret:
        raise PlanError("缺少 TIKHUB_API_KEY；密钥只能通过环境变量提供")
    base = api_base.rstrip("/")
    if base not in ALLOWED_API_BASES:
        raise PlanError("api_base 仅允许 TikHub 官方域名")
    pricing_catalog_rows = list(pricing_rows)
    pricing_catalog = normalize_pricing_rows(pricing_catalog_rows)
    account_price = pricing_catalog.get(ACCOUNT_INFO_ENDPOINT)
    if account_price is None:
        raise PlanError(f"TikHub 实时价格表缺少账户预检端点：{ACCOUNT_INFO_ENDPOINT}")
    if account_price["endpoint_cost"] != 0:
        raise PlanError("TikHub 账户预检端点不再免费，已拒绝自动调用")
    estimate = estimate_plan(
        plan,
        pricing_catalog_rows,
        usd_to_cny=usd_to_cny,
        discount_rate=discount_rate,
        max_attempts=max_attempts,
        price_source=price_source,
        price_observed_at=price_observed_at,
        pricing_url=pricing_url,
    )
    enforce_budget(estimate, max_cost_usd)
    account_snapshot = fetch_account_snapshot(
        token=secret,
        api_base=base,
        timeout=timeout,
        transport=account_transport,
    )
    eligible_cost = Decimal(estimate["budget_guard_free_credit_eligible_usd"])
    ineligible_cost = Decimal(estimate["budget_guard_free_credit_ineligible_usd"])
    paid_balance = Decimal(account_snapshot["balance_usd_exact"])
    free_credit_balance = Decimal(account_snapshot["free_credit_usd_exact"])
    required_paid_balance = ineligible_cost + max(Decimal("0"), eligible_cost - free_credit_balance)
    account_snapshot.update(
        {
            "eligible_cost_worst_case_usd": _money(eligible_cost),
            "ineligible_cost_worst_case_usd": _money(ineligible_cost),
            "required_paid_balance_usd": _money(required_paid_balance),
            "sufficient_for_worst_case": paid_balance >= required_paid_balance,
        }
    )
    if paid_balance < required_paid_balance:
        raise PlanError(
            "TikHub 付费余额不足："
            f"最坏情况至少需要 ${format(required_paid_balance, 'f')}，"
            f"当前付费余额 ${format(paid_balance, 'f')}"
        )
    request_prices = {item["id"]: Decimal(str(item["unit_estimated_price_usd"])) for item in estimate["requests"]}
    headers = {"Accept": "application/json", "Authorization": f"Bearer {secret}", "User-Agent": "AI-Opportunity-Radar/3.0"}
    results: list[dict[str, Any]] = []
    attempted_cost = Decimal("0")
    ok_count = 0
    error_count = 0
    skipped_count = 0
    stored_result_bytes = 0
    stop_reason: str | None = None
    by_source: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "requests": 0,
            "ok": 0,
            "error": 0,
            "skipped": 0,
            "attempts": 0,
            "estimated_attempted_cost_usd": Decimal("0"),
        }
    )

    for item in plan["requests"]:
        if stop_reason:
            status = "skipped"
            attempts = 0
            result_item = {
                "id": item["id"],
                "query_group": item.get("query_group"),
                "source": item["source"],
                "endpoint": item["endpoint"],
                "method": item["method"],
                "params": _sanitize(item["params"], secret),
                "status": status,
                "attempts": attempts,
                "estimated_attempted_cost_usd": 0.0,
                "error": "批次结果已达到安全上限，未发起请求",
                "error_code": "skipped_batch_limit",
            }
            for field in ("selected_item_id", "parent_url", "kind", "content_type", "selection_reason", "evidence_gap", "query_scope"):
                if field in item:
                    result_item[field] = _sanitize(item[field], secret)
            results.append(result_item)
            skipped_count += 1
            source_summary = by_source[item["source"]]
            source_summary["requests"] += 1
            source_summary["skipped"] += 1
            continue

        status = "error"
        response_data: Any = None
        sanitized_response: Any = None
        error_message: str | None = None
        error_code: str | None = None
        attempts = 0
        for _ in range(max_attempts):
            attempts += 1
            attempted_cost += request_prices[item["id"]]
            try:
                response_data = transport(
                    method=item["method"],
                    url=f"{base}{item['endpoint']}",
                    params=dict(item["params"]),
                    headers=headers,
                    timeout=timeout,
                )
                _assert_application_success(response_data)
                status = "ok"
                error_message = None
                break
            except Exception as exc:  # 外部来源失败必须转成单来源状态
                error_message = str(_sanitize(str(exc), secret))[:500]
                error_code = _classify_error(exc)
                if attempts >= max_attempts or not _is_retryable_error(exc):
                    break
                sleep_func(min(2 ** (attempts - 1), 5))
        if status == "ok":
            try:
                sanitized_response = _sanitize(response_data, secret)
                response_bytes = len(
                    json.dumps(sanitized_response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
            except Exception:
                status = "error"
                error_code = "response_sanitization_error"
                error_message = "TikHub 响应清洗失败"
            else:
                if stored_result_bytes + response_bytes > MAX_BATCH_RESULT_BYTES:
                    status = "error"
                    error_code = "batch_result_limit"
                    error_message = "TikHub 批次结果超过 32 MiB 安全上限"
                    stop_reason = "batch_result_limit"
                else:
                    stored_result_bytes += response_bytes
                    ok_count += 1
        result_item = {
            "id": item["id"],
            "query_group": item.get("query_group"),
            "source": item["source"],
            "endpoint": item["endpoint"],
            "method": item["method"],
            "params": _sanitize(item["params"], secret),
            "status": status,
            "attempts": attempts,
            "estimated_attempted_cost_usd": _money(request_prices[item["id"]] * attempts),
        }
        for field in ("selected_item_id", "parent_url", "kind", "content_type", "selection_reason", "evidence_gap", "query_scope"):
            if field in item:
                result_item[field] = _sanitize(item[field], secret)
        if status == "ok":
            result_item["response"] = sanitized_response
        else:
            error_count += 1
            result_item["error"] = error_message or "TikHub 请求失败"
            result_item["error_code"] = error_code or "request_error"
        results.append(result_item)
        source_summary = by_source[item["source"]]
        source_summary["requests"] += 1
        source_summary[status] += 1
        source_summary["attempts"] += attempts
        source_summary["estimated_attempted_cost_usd"] += request_prices[item["id"]] * attempts

    source_rows = [
        {
            "source": source,
            "requests": row["requests"],
            "ok": row["ok"],
            "error": row["error"],
            "skipped": row["skipped"],
            "attempts": row["attempts"],
            "estimated_attempted_cost_usd": _money(row["estimated_attempted_cost_usd"]),
        }
        for source, row in sorted(by_source.items())
    ]

    pricing_snapshot = {
        key: estimate[key]
        for key in ("price_source", "price_observed_at", "pricing_url", "plan_sha256", "catalog_sha256")
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "tikhub",
        "run_id": plan["run_id"],
        "as_of": plan["as_of"],
        "stage": plan.get("stage", "search_discovery"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_base": base,
        "estimate": estimate,
        "pricing_snapshot": pricing_snapshot,
        "account_snapshot": account_snapshot,
        "summary": {
            "requests": len(results),
            "ok": ok_count,
            "error": error_count,
            "skipped": skipped_count,
            "stopped_early": stop_reason is not None,
            "stop_reason": stop_reason,
            "stored_result_bytes": stored_result_bytes,
            "estimated_attempted_cost_usd": _money(attempted_cost),
            "estimated_attempted_cost_cny": _money(attempted_cost * Decimal(str(usd_to_cny))),
            "by_source": source_rows,
            "billing_note": "实际扣费以 TikHub 使用日志与账单为准",
        },
        "results": results,
    }


def _read_json(path: Path) -> Any:
    try:
        def reject_constant(value: str) -> None:
            raise ValueError(f"不允许非有限数字 {value}")

        return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PlanError(f"无法读取 JSON 文件 {path}：{exc}") from exc


def _load_pricing_file(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise PlanError("价格文件必须是数组，或包含 data 数组")
    return rows


def fetch_live_pricing() -> list[dict[str, Any]]:
    req = request.Request(PRICING_URL, headers={"Accept": "application/json", "User-Agent": "AI-Opportunity-Radar/3.0"})
    try:
        with _safe_urlopen(req, timeout=20) as response:
            payload = json.loads(response.read(MAX_RESPONSE_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"无法读取 TikHub 实时价格：{exc}") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise PlanError("TikHub 实时价格响应缺少 data 数组")
    return rows


def execute_plan(
    plan: dict[str, Any],
    *,
    token: str,
    max_cost_usd: float,
    usd_to_cny: float = 7.2,
    discount_rate: float = 1.0,
    max_attempts: int = 1,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 45,
    transport: Callable[..., dict[str, Any]] = _default_transport,
    account_transport: Callable[..., dict[str, Any]] = _default_transport,
    sleep_func: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """生产执行入口：内部强制刷新实时价格后再做预算、余额和数据请求。"""
    secret = token.strip()
    if not secret:
        raise PlanError("缺少 TIKHUB_API_KEY；密钥只能通过环境变量提供")
    observed_at = datetime.now(timezone.utc).isoformat()
    pricing_rows = fetch_live_pricing()
    return _execute_plan_with_pricing(
        plan,
        pricing_rows,
        token=secret,
        max_cost_usd=max_cost_usd,
        usd_to_cny=usd_to_cny,
        discount_rate=discount_rate,
        max_attempts=max_attempts,
        api_base=api_base,
        timeout=timeout,
        transport=transport,
        account_transport=account_transport,
        price_source="dashboard_pricing_catalog",
        price_observed_at=observed_at,
        pricing_url=PRICING_URL,
        sleep_func=sleep_func,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _load_plan(path: Path) -> dict[str, Any]:
    plan = _read_json(path)
    if not isinstance(plan, dict):
        raise PlanError("计划必须是 JSON 对象")
    return plan


def _print_estimate(estimate: dict[str, Any]) -> None:
    print(
        "预计费用："
        f"${estimate['estimated_cost_usd']:.6f}（约 ¥{estimate['estimated_cost_cny']:.4f}）；"
        f"最坏情况 ${estimate['worst_case_cost_usd']:.6f}；"
        f"共 {estimate['request_count']} 次请求；"
        f"免费额度适用 ${estimate['free_credit_eligible_cost_usd']:.6f}；"
        f"免费额度不适用 ${estimate['free_credit_ineligible_cost_usd']:.6f}。"
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, required=True, help="TikHub 查询计划 JSON")
    parser.add_argument("--usd-to-cny", type=float, default=7.2, help="估算汇率，默认 7.2，仅用于展示")
    parser.add_argument("--discount-rate", type=float, default=1.0, help="明确已知的账户折扣；默认按原价保守估算")
    parser.add_argument("--max-attempts", type=int, choices=(1, 2, 3), default=1, help="每个请求最大尝试次数")
    parser.add_argument("--output", type=Path, help="输出 JSON 文件")


def main() -> int:  # pragma: no cover - CLI 由集成测试覆盖
    parser = argparse.ArgumentParser(description="TikHub 查询与运行前成本预估")
    subparsers = parser.add_subparsers(dest="command", required=True)
    comments_parser = subparsers.add_parser("build-comments", help="为 1–5 个已选帖子生成详情与一级评论计划")
    comments_parser.add_argument("--date", dest="as_of", required=True, help="计划日期 YYYY-MM-DD")
    comments_parser.add_argument("--parent-search-run-id", required=True, help="来源搜索运行 ID")
    comments_parser.add_argument("--input", type=Path, required=True, help="候选数组或包含 selections 数组的 JSON")
    comments_parser.add_argument("--output", type=Path, required=True, help="评论深挖计划 JSON")
    gaps_parser = subparsers.add_parser("build-gaps", help="从明确候选证据缺口生成定向付费补证计划")
    gaps_parser.add_argument("--date", dest="as_of", required=True, help="计划日期 YYYY-MM-DD")
    gaps_parser.add_argument("--run-id", required=True, help="本次雷达共享的运行 ID")
    gaps_parser.add_argument("--input", type=Path, required=True, help="缺口数组或包含 gaps 数组的 JSON")
    gaps_parser.add_argument("--output", type=Path, required=True, help="定向补证计划 JSON")
    estimate_parser = subparsers.add_parser("estimate", help="只读取实时价格并估算，不调用付费接口")
    _add_common_arguments(estimate_parser)
    estimate_parser.add_argument("--pricing-file", type=Path, help="仅供离线估价/测试；省略时读取控制台实时价格")
    run_parser = subparsers.add_parser("run", help="预算检查通过后执行 TikHub 查询")
    _add_common_arguments(run_parser)
    run_parser.add_argument("--max-cost-usd", type=float, required=True, help="显式费用上限；按最坏情况校验")
    run_parser.add_argument("--api-base", choices=sorted(ALLOWED_API_BASES), default=DEFAULT_API_BASE)
    args = parser.parse_args()

    try:
        if args.command == "build-comments":
            raw_selections = _read_json(args.input)
            selections = raw_selections.get("selections") if isinstance(raw_selections, dict) else raw_selections
            if not isinstance(selections, list):
                raise PlanError("评论候选输入必须是数组，或包含 selections 数组")
            payload = build_comment_plan(
                as_of=args.as_of,
                parent_search_run_id=args.parent_search_run_id,
                selections=selections,
            )
            _write_json(args.output, payload)
            print(f"已生成评论深挖计划：{len(payload['selected_items'])} 个帖子，{len(payload['requests'])} 次请求。")
            return 0
        if args.command == "build-gaps":
            raw_gaps = _read_json(args.input)
            gaps = raw_gaps.get("gaps") if isinstance(raw_gaps, dict) else raw_gaps
            if not isinstance(gaps, list):
                raise PlanError("证据缺口输入必须是数组，或包含 gaps 数组")
            payload = build_evidence_gap_plan(as_of=args.as_of, run_id=args.run_id, gaps=gaps)
            _write_json(args.output, payload)
            print(f"已生成定向补证计划：{len(gaps)} 个缺口，{len(payload['requests'])} 次请求。")
            return 0
        plan = _load_plan(args.plan)
        token = ""
        if args.command == "run":
            token = os.environ.get("TIKHUB_API_KEY") or os.environ.get("TIKHUB_API_TOKEN") or ""
            if not token.strip():
                raise PlanError("缺少 TIKHUB_API_KEY；密钥只能通过环境变量提供")
        if args.command == "estimate":
            observed_at = datetime.now(timezone.utc).isoformat()
            pricing_file = getattr(args, "pricing_file", None)
            if pricing_file:
                pricing = _load_pricing_file(pricing_file)
                price_source = "offline_pricing_file"
                pricing_url: str | None = None
            else:
                pricing = fetch_live_pricing()
                price_source = "dashboard_pricing_catalog"
                pricing_url = PRICING_URL
            estimate = estimate_plan(
                plan,
                pricing,
                usd_to_cny=args.usd_to_cny,
                discount_rate=args.discount_rate,
                max_attempts=args.max_attempts,
                price_source=price_source,
                price_observed_at=observed_at,
                pricing_url=pricing_url,
            )
            _print_estimate(estimate)
            payload = estimate
        else:
            payload = execute_plan(
                plan,
                token=token,
                max_cost_usd=args.max_cost_usd,
                usd_to_cny=args.usd_to_cny,
                discount_rate=args.discount_rate,
                max_attempts=args.max_attempts,
                api_base=args.api_base,
            )
            _print_estimate(payload["estimate"])
        if args.output:
            _write_json(args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (PlanError, BudgetExceeded) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
