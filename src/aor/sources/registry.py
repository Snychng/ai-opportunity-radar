"""一期来源能力目录；能力、配置和实时健康分别表达。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TIKHUB_SOURCES = (
    "tiktok", "instagram", "linkedin", "threads", "twitter", "youtube",
    "reddit", "douyin", "xiaohongshu", "bilibili", "zhihu", "wechat_search",
)
COMMUNITY_SOURCES = ("hackernews", "github")
CHINESE_SOURCES = frozenset(("douyin", "xiaohongshu", "bilibili", "zhihu", "wechat_search"))
EVIDENCE_ROLES = (
    "official_pricing", "product_update", "product_review", "hiring",
    "outsourcing", "payment", "workflow_pain", "alternative", "regional_gap",
    "counter_evidence",
)
MANUAL_PLATFORMS = (
    "web", "producthunt", "indiehackers", "appstore", "googleplay",
    "chrome_web_store", "g2", "capterra", "trustpilot", "v2ex",
)


def source_catalog() -> list[dict[str, Any]]:
    """返回独立副本；preferred_languages 是路由偏好，不是覆盖声明。"""
    rows = []
    for platform in (*TIKHUB_SOURCES, *COMMUNITY_SOURCES, *MANUAL_PLATFORMS):
        manual = platform in MANUAL_PLATFORMS
        community = platform in COMMUNITY_SOURCES
        rows.append({
            "source": platform,
            "platform": platform,
            "provider": "host-verified-web" if manual else ("community-public" if community else "tikhub"),
            "capabilities": ["import"] if manual else ["search", "detail", "top_level_comments"],
            "cost": "no_network_import" if manual else ("free_public_api" if community else "paid_live_quote_required"),
            "preferred_languages": ["en"] if community else (["zh"] if platform in CHINESE_SOURCES else ["multilingual"]),
            "regions": ["global"],
            "region_filter": platform in ("tiktok", "youtube"),
            "default_enabled": not manual,
            "manual_import_only": manual,
            "credential_env": [] if manual or platform == "hackernews" else (["GITHUB_TOKEN"] if community else ["TIKHUB_API_KEY"]),
            "credential_required": not manual and not community,
            "evidence_roles": list(EVIDENCE_ROLES),
            "live_health": "not_checked",
        })
    return rows


def default_sources_for_language(sources: list[str], language: str) -> list[str]:
    """默认定向路由按查询语言筛选；显式宿主意图不使用此过滤器。"""
    preferences = {row["source"]: row["preferred_languages"] for row in source_catalog()}
    return [source for source in sources
            if "multilingual" in preferences[source] or language.split("-")[0] in preferences[source]]


def diagnose_sources(configured: Mapping[str, bool] | None = None) -> dict[str, Any]:
    """仅消费凭证是否配置的布尔标记，不读取文件、凭证值或网络。"""
    configured = configured or {}
    rows = []
    for item in source_catalog():
        present = all(configured.get(name) is True for name in item["credential_env"])
        status = "manual_import_only" if item["manual_import_only"] else (
            "missing_required_config" if item["credential_required"] and not present else "configuration_ready"
        )
        rows.append({
            "source": item["source"], "provider": item["provider"],
            "configuration_status": status,
            "credentials": {name: configured.get(name) is True for name in item["credential_env"]},
            "live_health": "not_checked", "network_checked": False,
        })
    return {"sources": rows, "network_requests": 0, "cost_usd": 0,
            "note": "仅诊断配置；凭证存在不证明有效、联网健康或本次已覆盖。"}
