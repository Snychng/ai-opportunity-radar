#!/usr/bin/env python3
"""独立采集 Hacker News 与 GitHub 的公开社区证据。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import request  # noqa: F401 旧调用者使用脚本级 request

from contracts import SCHEMA_VERSION, ContractError, canonical_sha256, validate_run_as_of
import aor_bootstrap  # noqa: F401
from aor.net import SameOriginRedirectHandler as _SameOriginRedirectHandler  # noqa: F401
from aor.net import json_get
from aor.text import language as _language, tokens as _tokens, clean_text
from aor.community import ALLOWED_SOURCES, MAX_ITEMS_PER_REQUEST, CommunityPlanError, collect_community


HN_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"
GITHUB_ENDPOINT = "https://api.github.com/search/issues"
MAX_REQUESTS = 12
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _slug(value: str) -> str:
    rendered = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")
    return rendered[:50] or "query"



def short_topic(value: str | None, limit: int = 60) -> str:
    """提取字面主题，去掉检索操作符，并限制单条查询长度。"""
    words = re.findall(r"[^\W_]+", str(value or ""), re.UNICODE)
    return " ".join(dict.fromkeys(words[:8]))[:limit].strip()


def build_community_plan(
    *,
    as_of: str,
    run_id: str,
    focus_name: str,
    custom_focus: str | None = None,
    include_recent_activity: bool = False,
) -> dict[str, Any]:
    """生成只包含 HN 与 GitHub 的社区查询计划。"""
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError as exc:
        raise CommunityPlanError(f"无效日期：{as_of}") from exc
    try:
        validate_run_as_of(run_id, as_of)
    except ContractError as exc:
        raise CommunityPlanError(str(exc)) from exc
    # 主题进入实际查询；只保留短词组，避免长串意图造成几乎无结果。
    regional = focus_name.strip()[:40]
    groups = [
        {
            "id": "workflow-pain",
            "source": "hackernews",
            "query": "manual workflow",
            "ranking_query": "哪些具体数字任务仍依赖手工流程、拼接工具或昂贵替代方案？",
            "weight": 1.0,
        },
        {
            "id": "new-product-form",
            "source": "hackernews",
            "query": "AI agent",
            "ranking_query": "哪些成熟需求正在因代理、语音、长期记忆或动态生成重新成立？",
            "weight": 0.9,
        },
        {
            "id": "developer-friction",
            "source": "github",
            "query": f'"manual" "workflow" AI is:issue created:{as_of_date - timedelta(days=29)}..{as_of_date}',
            "ranking_query": "开发者在哪些 AI 工作流中持续报告缺失能力、集成失败或手工替代？",
            "weight": 1.0,
        },
        {
            "id": "regional-integration-gap",
            "source": "github",
            "query": f'"localization" AI is:issue created:{as_of_date - timedelta(days=29)}..{as_of_date}',
            "ranking_query": f"{regional}相关的语言、本地渠道和集成缺口中有哪些可产品化信号？",
            "weight": 0.8,
        },
    ]
    topic = short_topic(custom_focus) if custom_focus else None
    if topic:
        intents = ("manual", "paid", "workflow", "localization")
        for group, intent in zip(groups, intents, strict=True):
            group["query"] = f"{topic} {intent}"
            if group["source"] == "github":
                group["query"] += f" is:issue created:{as_of_date - timedelta(days=29)}..{as_of_date}"
            group["ranking_query"] = topic + "：" + group["ranking_query"]
    if include_recent_activity:
        for group in list(groups):
            if group['source'] == 'github':
                groups.append({**group, 'id': group['id'] + '-recent-activity',
                               'query': group['query'].replace('created:', 'updated:')})
    requests: list[dict[str, Any]] = []
    threshold = int(datetime.combine(as_of_date - timedelta(days=29), time.min, tzinfo=timezone.utc).timestamp())
    upper_threshold = int(datetime.combine(as_of_date + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp()) - 1
    for index, group in enumerate(groups, start=1):
        source = group["source"]
        if source == "hackernews":
            endpoint = HN_ENDPOINT
            params = {
                "query": group["query"],
                "tags": "story",
                "numericFilters": f"created_at_i>={threshold},created_at_i<={upper_threshold}",
                "hitsPerPage": 20,
            }
        else:
            endpoint = GITHUB_ENDPOINT
            params = {"q": group["query"], "sort": "comments", "order": "desc", "per_page": 20}
        requests.append(
            {
                "id": f"{index}-{_slug(group['id'])}",
                "query_group": group["id"],
                "source": source,
                "endpoint": endpoint,
                "method": "GET",
                "params": params,
                "ranking_query": group["ranking_query"],
                "relevance_query": topic or re.sub(r"\b(?:is|created|updated):\S+", " ", group["query"]),
                "weight": group["weight"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "community-public",
        "run_id": run_id,
        "as_of": as_of,
        "stage": "community_discovery",
        "include_recent_activity": include_recent_activity,
        "window": {
            "lookback_days": 30,
            "range_from": (as_of_date - timedelta(days=29)).isoformat(),
            "range_to": as_of,
            "semantics": "inclusive_calendar_days",
        },
        "scope": {"id": "phase_1_existing_platforms", "sources": sorted(ALLOWED_SOURCES)},
        "requests": requests,
    }


def validate_plan(plan: dict[str, Any]) -> None:
    """验证公开社区计划没有越界来源或任意端点。"""
    if not isinstance(plan, dict) or plan.get("provider") != "community-public":
        raise CommunityPlanError("计划 provider 必须为 community-public")
    try:
        validate_run_as_of(plan.get("run_id"), plan.get("as_of"))
    except ContractError as exc:
        raise CommunityPlanError(str(exc)) from exc
    as_of_date = date.fromisoformat(str(plan["as_of"]))
    range_from = as_of_date - timedelta(days=29)
    expected_window = {
        "lookback_days": 30,
        "range_from": range_from.isoformat(),
        "range_to": as_of_date.isoformat(),
        "semantics": "inclusive_calendar_days",
    }
    if plan.get("window") != expected_window:
        raise CommunityPlanError("社区计划窗口必须是截止 as_of 的 30 个自然日")
    lower_timestamp = int(datetime.combine(range_from, time.min, tzinfo=timezone.utc).timestamp())
    upper_timestamp = int(datetime.combine(as_of_date + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp()) - 1
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("stage") != "community_discovery":
        raise CommunityPlanError("社区计划必须使用 V3 discovery 契约")
    scope = plan.get("scope")
    if not isinstance(scope, dict) or set(scope.get("sources") or []) != ALLOWED_SOURCES:
        raise CommunityPlanError("社区计划来源必须严格为 Hacker News 与 GitHub")
    requests = plan.get("requests")
    if requests == [] and plan.get('plan_status') in {'needs_host_queries', 'not_requested'}:
        return
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
        raise CommunityPlanError(f"社区计划请求数必须为 1 到 {MAX_REQUESTS}")
    seen: set[str] = set()
    allowed_endpoints = {"hackernews": HN_ENDPOINT, "github": GITHUB_ENDPOINT}
    for item in requests:
        if not isinstance(item, dict):
            raise CommunityPlanError("每个社区请求必须是对象")
        request_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", request_id) or request_id in seen:
            raise CommunityPlanError(f"社区请求 ID 无效或重复：{request_id}")
        seen.add(request_id)
        source = str(item.get("source") or "")
        if source not in ALLOWED_SOURCES or item.get("endpoint") != allowed_endpoints[source]:
            raise CommunityPlanError(f"社区来源或端点越界：{source}")
        if item.get("method") != "GET" or not isinstance(item.get("params"), dict):
            raise CommunityPlanError(f"社区请求 {request_id} 必须是带参数的 GET")
        params = item["params"]
        allowed_params = {
            "hackernews": {"query", "tags", "numericFilters", "hitsPerPage"},
            "github": {"q", "sort", "order", "per_page"},
        }[source]
        if set(params) != allowed_params:
            raise CommunityPlanError(f"社区请求 {request_id} 的参数字段不符合固定契约")
        query = params.get("query") if source == "hackernews" else params.get("q")
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise CommunityPlanError(f"社区请求 {request_id} 的查询词无效")
        page_size = params.get("hitsPerPage") if source == "hackernews" else params.get("per_page")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_ITEMS_PER_REQUEST:
            raise CommunityPlanError(f"社区请求 {request_id} 的每页数量无效")
        if source == "hackernews" and params.get("tags") != "story":
            raise CommunityPlanError(f"社区请求 {request_id} 仅允许 Hacker News story")
        if source == "hackernews" and params.get("numericFilters") != (
            f"created_at_i>={lower_timestamp},created_at_i<={upper_timestamp}"
        ):
            raise CommunityPlanError(f"社区请求 {request_id} 的 Hacker News 日期窗口无效")
        if source == "github" and (params.get("sort") != "comments" or params.get("order") != "desc"):
            raise CommunityPlanError(f"社区请求 {request_id} 必须固定按评论数降序")
        if source == 'github':
            qualifiers = re.findall(r'\b(created|updated):([^\s]+)', query)
            allowed = {'created', 'updated'} if plan.get('include_recent_activity') is True else {'created'}
            if len(qualifiers) != 1 or qualifiers[0][0] not in allowed or qualifiers[0][1] != f'{range_from}..{as_of_date}':
                raise CommunityPlanError(f'社区请求 {request_id} 的 GitHub 日期窗口无效')
        if not str(item.get("ranking_query") or "").strip():
            raise CommunityPlanError(f"社区请求 {request_id} 缺少 ranking_query")


# 旧测试及调用者仍可使用这些脚本级名称。
_default_transport = json_get


def _clean(value: Any, limit: int = 1000) -> str | None:
    return clean_text(value, limit) or None


def _local_relevance(item: dict[str, Any], ranking_query: str) -> float:
    query_tokens = _tokens(ranking_query)
    text_tokens = _tokens(" ".join(str(item.get(key) or "") for key in ("title", "original_text", "container")))
    if not query_tokens:
        return 0.0
    return round(len(query_tokens & text_tokens) / len(query_tokens), 4)


def _normalize_hn(payload: dict[str, Any], request_item: dict[str, Any], observed_at: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in payload.get("hits") or []:
        if not isinstance(row, dict):
            continue
        object_id = _clean(row.get("objectID"), 100)
        title = _clean(row.get("title"), 500)
        if not object_id or not title:
            continue
        url = _clean(row.get("url"), 2000) or f"https://news.ycombinator.com/item?id={object_id}"
        evidence = {
            "id": f"hackernews:{object_id}",
            "source": "hackernews",
            "source_item_id": object_id,
            "query_id": request_item["id"],
            "query_group": request_item["query_group"],
            "url": url,
            "evidence_kind": "post",
            "author": _clean(row.get("author"), 200),
            "container": "Hacker News",
            "thread_url": f"https://news.ycombinator.com/item?id={object_id}",
            "title": title,
            "original_text": _clean(row.get("story_text"), 1500) or title,
            "zh_translation": None,
            "language": _language(title + " " + str(row.get("story_text") or row.get("body") or ""), row.get("language")),
            "published_at": _clean(row.get("created_at"), 100),
            "date_confidence": "high" if row.get("created_at") else "unknown",
            "observed_at": observed_at,
            "engagement": {
                key: value
                for key, value in {"points": row.get("points"), "comments": row.get("num_comments")}.items()
                if isinstance(value, (int, float))
            },
            "access_method": "native-platform",
            "signal_types": [],
        }
        evidence["local_relevance"] = _local_relevance(evidence, str(request_item["params"]["query"]))
        result.append(evidence)
    return result


def _normalize_github(payload: dict[str, Any], request_item: dict[str, Any], observed_at: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in payload.get("items") or []:
        if not isinstance(row, dict):
            continue
        url = _clean(row.get("html_url"), 2000)
        title = _clean(row.get("title"), 500)
        item_id = _clean(row.get("id"), 100)
        if not url or not title or not item_id:
            continue
        repository_url = str(row.get("repository_url") or "").rstrip("/")
        repository = repository_url.rsplit("/", 2)[-2:] if repository_url else []
        container = "/".join(repository) if len(repository) == 2 else "GitHub"
        evidence = {
            "id": f"github:{item_id}",
            "source": "github",
            "source_item_id": item_id,
            "query_id": request_item["id"],
            "query_group": request_item["query_group"],
            "url": url,
            "evidence_kind": "post",
            "author": _clean((row.get("user") or {}).get("login") if isinstance(row.get("user"), dict) else None, 200),
            "container": container,
            "thread_url": url,
            "updated_at": _clean(row.get("updated_at"), 100),
            "title": title,
            "original_text": _clean(row.get("body"), 1500) or title,
            "zh_translation": None,
            "language": _language(title + " " + str(row.get("story_text") or row.get("body") or ""), row.get("language")),
            "published_at": _clean(row.get("created_at"), 100),
            "date_confidence": "high" if row.get("created_at") else "unknown",
            "observed_at": observed_at,
            "engagement": {
                key: value
                for key, value in {"comments": row.get("comments"), "reactions": row.get("reactions", {}).get("total_count") if isinstance(row.get("reactions"), dict) else None}.items()
                if isinstance(value, (int, float))
            },
            "access_method": "native-platform",
            "signal_types": [],
        }
        search_query = re.sub(r"\b(?:is|created|updated|sort|order):\S+", " ", str(request_item["params"]["q"]))
        evidence["local_relevance"] = _local_relevance(evidence, search_query)
        result.append(evidence)
    return result


def execute_plan(
    plan: dict[str, Any], *, github_token: str = '', timeout: int = 20,
    max_items_per_request: int = 20, transport: Callable[..., Any] = _default_transport,
    include_comments: bool = False, max_comment_threads: int = 4, max_comments_per_thread: int = 10,
    concurrency: int = 1,
) -> dict[str, Any]:
    """兼容入口：验证旧计划，使用统一采集引擎执行。"""
    validate_plan(plan)
    return collect_community(
        plan, normalize_hn=_normalize_hn, normalize_github=_normalize_github, plan_sha256=canonical_sha256(plan),
        github_token=github_token, timeout=timeout, max_items_per_request=max_items_per_request, transport=transport,
        include_comments=include_comments, max_comment_threads=max_comment_threads,
        max_comments_per_thread=max_comments_per_thread, concurrency=concurrency,
    )


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommunityPlanError(f"无法读取社区计划 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise CommunityPlanError("社区计划必须是 JSON 对象")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="采集 Hacker News 与 GitHub 公开社区证据")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="输出本 Skill 的固定社区能力，不访问网络")
    doctor_parser.add_argument("--json", action="store_true")
    run_parser = subparsers.add_parser("run", help="执行社区查询计划")
    run_parser.add_argument("--plan", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--max-items-per-request", type=int, default=20)
    run_parser.add_argument('--include-comments', action='store_true', help='归并搜索后补充公开评论')
    run_parser.add_argument('--max-comment-threads', type=int, default=4)
    run_parser.add_argument('--max-comments-per-thread', type=int, default=10)
    run_parser.add_argument('--timeout', type=int, default=20)
    run_parser.add_argument('--concurrency', type=int, default=1)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            payload = {
                "schema_version": SCHEMA_VERSION,
                "provider": "community-public",
                "sources": sorted(ALLOWED_SOURCES),
                "network_called": False,
                "github_token_configured": bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "Hacker News 与 GitHub 适配器已就绪")
            return 0
        result = execute_plan(
            _read_plan(args.plan),
            github_token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "",
            max_items_per_request=args.max_items_per_request,
            include_comments=args.include_comments, max_comment_threads=args.max_comment_threads,
            max_comments_per_thread=args.max_comments_per_thread, timeout=args.timeout, concurrency=args.concurrency,
        )
        _write_json(args.output, result)
        print(json.dumps(result["stats"], ensure_ascii=False))
        return 0
    except CommunityPlanError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
