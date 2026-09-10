#!/usr/bin/env python3
"""生成 AI 创业机会雷达 V3 的确定性查询计划。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aor_bootstrap  # noqa: F401
from aor.sources.importing import read_json_file
from aor.sources.planning import compile_intents, finalize_plan, route_community_focus, validate_intent_plan
from aor.sources.registry import default_sources_for_language
from community_query import build_community_plan, short_topic
from contracts import QUERY_PLAN_VERSION, SCHEMA_VERSION, beijing_today, make_run_id
from tikhub_query import build_search_plan, validate_query_locale


DEFAULT_HOME = Path(os.environ.get("AI_OPPORTUNITY_RADAR_HOME", "~/Documents/AI-Opportunity-Radar")).expanduser()
PHASE_ONE_ID = "phase_1_existing_platforms"
PHASE_ONE_TIKHUB_SOURCES: tuple[str, ...] = (
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
PHASE_ONE_AUXILIARY_SOURCES: tuple[str, ...] = ("hackernews", "github")
DAILY_CORE_SOURCES: tuple[str, ...] = ("reddit", "twitter", "youtube", "xiaohongshu", "zhihu")
ROLLING_SOURCE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("tiktok", "instagram", "douyin"),
    ("linkedin", "threads", "bilibili"),
    ("tiktok", "instagram", "wechat_search"),
)

FOCUS_REGIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "southeast_asia",
        "name": "东南亚",
        "markets": ["印度尼西亚", "越南", "泰国", "菲律宾", "马来西亚", "新加坡"],
        "languages": ["英语", "印尼语", "越南语", "泰语", "菲律宾语", "马来语"],
        "localized_queries": [
            {"language": "印尼语", "query": "aplikasi AI terlalu mahal masih dikerjakan manual butuh alat"},
            {"language": "越南语", "query": "công cụ AI quá đắt vẫn phải làm thủ công cần ứng dụng"},
            {"language": "泰语", "query": "เครื่องมือ AI แพงเกินไป ยังต้องทำเอง อยากได้แอป"},
            {"language": "菲律宾语", "query": "AI app sobrang mahal mano-mano pa rin kailangan ng tool"},
        ],
    },
    {
        "id": "africa",
        "name": "非洲",
        "markets": ["南非", "尼日利亚", "肯尼亚", "加纳", "埃及", "摩洛哥"],
        "languages": ["英语", "法语", "葡萄牙语", "南非荷兰语", "斯瓦希里语", "阿拉伯语"],
        "localized_queries": [
            {"language": "英语", "query": "small business AI tool too expensive manual workaround Africa"},
            {"language": "法语", "query": "outil IA trop cher petite entreprise travail manuel Afrique"},
            {"language": "斯瓦希里语", "query": "zana ya AI ghali biashara ndogo bado tunafanya kwa mkono"},
            {"language": "阿拉伯语", "query": "أداة ذكاء اصطناعي غالية عمل يدوي مشروع صغير"},
        ],
    },
    {
        "id": "south_asia",
        "name": "南亚",
        "markets": ["印度", "巴基斯坦", "孟加拉国", "斯里兰卡"],
        "languages": ["英语", "印地语", "乌尔都语", "孟加拉语"],
        "localized_queries": [
            {"language": "印地语", "query": "AI टूल बहुत महंगा अभी भी हाथ से काम ऐप चाहिए"},
            {"language": "乌尔都语", "query": "AI ٹول بہت مہنگا ابھی بھی ہاتھ سے کام ایپ چاہیے"},
            {"language": "孟加拉语", "query": "AI টুল খুব দামি এখনও হাতে কাজ করতে হয় অ্যাপ দরকার"},
            {"language": "英语", "query": "local language AI app too expensive manual work South Asia"},
        ],
    },
    {
        "id": "middle_east",
        "name": "中东",
        "markets": ["阿联酋", "沙特阿拉伯", "土耳其", "埃及"],
        "languages": ["阿拉伯语", "英语", "土耳其语"],
        "localized_queries": [
            {"language": "阿拉伯语", "query": "أداة ذكاء اصطناعي غالية لا تدعم العربية عمل يدوي"},
            {"language": "土耳其语", "query": "AI aracı çok pahalı Türkçe desteklemiyor manuel iş"},
            {"language": "英语", "query": "AI app Arabic support expensive manual workaround Middle East"},
        ],
    },
    {
        "id": "latin_america",
        "name": "拉美",
        "markets": ["巴西", "墨西哥", "阿根廷", "哥伦比亚", "智利"],
        "languages": ["西班牙语", "葡萄牙语"],
        "localized_queries": [
            {"language": "西班牙语", "query": "herramienta IA demasiado cara todavía trabajo manual necesito app"},
            {"language": "葡萄牙语", "query": "ferramenta IA cara demais ainda faço manual preciso aplicativo"},
        ],
    },
)

GLOBAL_CONSUMER_QUERIES = (
    "paid subscription pricing cancelled switched workaround AI app",
    "hired freelancer paying monthly wish AI app missing feature",
    "revenue purchase paid creator tool complaint too expensive",
)
GLOBAL_BUSINESS_QUERIES = (
    "small business paying software manual spreadsheet missing integration",
    "hiring outsourcing repetitive task paid tool too expensive switched",
    "AI SaaS pricing subscription customer complaint workflow",
)
CHINA_QUERIES = (
    "付费 订阅 续费 取消 太贵 替代 AI 工具",
    "外包 招聘 每月花费 手工流程 AI 产品",
    "小店 付费软件 表格凑合 复制粘贴 更换工具",
)


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"无效日期：{value}，应使用 YYYY-MM-DD") from exc


def resolve_focus(focus: str | None, focus_file: Path | None) -> str | None:
    """通过 UTF-8 文件安全读取用户提供的定向范围。"""
    if focus is not None and focus_file is not None:
        raise ValueError("--focus 与 --focus-file 不能同时使用")
    if focus_file is not None:
        try:
            with focus_file.open("rb") as handle:
                raw_focus = handle.read(2001)
            if len(raw_focus) > 2000:
                raise ValueError("定向范围文件最多读取 2000 字节")
            focus = raw_focus.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"无法读取定向范围文件 {focus_file}：{exc}") from exc
    if focus is None:
        return None
    value = focus.strip()
    if not value:
        return None
    if "\x00" in value:
        raise ValueError("定向范围不能包含空字节")
    if len(value) > 500:
        raise ValueError("定向范围最多 500 个字符")
    return value



def resolve_scope(scope: Any) -> dict[str, Any] | None:
    """结构化范围优先于轮换；自由文本不自动猜测国家或翻译。"""
    if scope is None:
        return None
    if not isinstance(scope, dict) or not scope or set(scope) - {"countries", "languages", "industry", "payer", "task", "queries"}:
        raise ValueError("scope 必须是包含 countries、languages、industry、payer、task 或 queries 的对象")
    result: dict[str, Any] = {}
    for key, code_key in (("countries", "country"), ("languages", "language")):
        values = scope.get(key, ["unknown"])
        if not isinstance(values, list) or not 1 <= len(values) <= 10:
            raise ValueError(f"scope.{key} 必须包含 1 到 10 个代码")
        result[key] = list(dict.fromkeys(validate_query_locale(**{code_key: value})[code_key] for value in values))
    for key in ("industry", "payer", "task"):
        value = scope.get(key)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or "\x00" in value):
            raise ValueError(f"scope.{key} 必须为 1 到 200 字符的文本")
        result[key] = value.strip() if value else None
    queries = scope.get("queries", [])
    if not isinstance(queries, list) or len(queries) > 10:
        raise ValueError("scope.queries 最多包含 10 条人工提供的本地语言查询")
    result["queries"] = []
    for item in queries:
        if not isinstance(item, dict) or set(item) != {"language", "query"}:
            raise ValueError("scope.queries 每条必须包含 language 和 query")
        language = validate_query_locale(language=item["language"])["language"]
        query = item["query"]
        if language not in result["languages"] or not isinstance(query, str) or not 1 <= len(query.strip()) <= 100 or "\x00" in query:
            raise ValueError("本地查询语言必须属于 scope.languages，query 为 1 到 100 字符")
        result["queries"].append({"language": language, "query": query.strip()})
    return result


def read_scope(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(16001)
        if len(data) > 16000:
            raise ValueError("结构化范围文件最多 16000 字节")
        return resolve_scope(json.loads(data.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取结构化范围文件：{exc}") from exc

def _window(as_of: date, days: int) -> dict[str, str | int]:
    return {
        "lookback_days": days,
        "from": (as_of - timedelta(days=days - 1)).isoformat(),
        "to": as_of.isoformat(),
        "semantics": "inclusive_calendar_days",
    }


def _load_preferences(home: Path) -> dict[str, Any]:
    path = home / "config" / "preferences.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取偏好配置 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"偏好配置必须是 JSON 对象：{path}")
    return data


def _daily_sources(as_of: date) -> tuple[list[str], dict[str, Any]]:
    rotation_index = as_of.toordinal() % len(ROLLING_SOURCE_GROUPS)
    rolling = ROLLING_SOURCE_GROUPS[rotation_index]
    planned = list(dict.fromkeys([*DAILY_CORE_SOURCES, *rolling]))
    return planned, {
        "strategy": "daily_core_plus_three_day_rotation",
        "rotation_index": rotation_index,
        "core_sources": list(DAILY_CORE_SOURCES),
        "rolling_sources": list(rolling),
        "planned_sources": planned,
        "all_phase_one_sources": list(PHASE_ONE_TIKHUB_SOURCES),
    }


def _tikhub_plan(
    as_of: date,
    run_id: str,
    focus_region: dict[str, Any],
    custom_focus: str | None,
    planned_sources: list[str],
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    day_index = as_of.toordinal()
    custom_suffix = ""
    localized = focus_region["localized_queries"][day_index % len(focus_region["localized_queries"])]
    planned = set(planned_sources)
    groups = [
        {
            "id": "global-consumer",
            "keyword": f"{GLOBAL_CONSUMER_QUERIES[day_index % len(GLOBAL_CONSUMER_QUERIES)]}{custom_suffix}",
            "sources": [source for source in ("tiktok", "instagram", "threads", "twitter") if source in planned],
        },
        {
            "id": "global-business",
            "keyword": f"{GLOBAL_BUSINESS_QUERIES[day_index % len(GLOBAL_BUSINESS_QUERIES)]}{custom_suffix}",
            "sources": [source for source in ("linkedin", "reddit", "youtube") if source in planned],
        },
        {
            "id": "china-pain",
            "keyword": f"{CHINA_QUERIES[day_index % len(CHINA_QUERIES)]}{custom_suffix}",
            "sources": [
                source
                for source in ("douyin", "xiaohongshu", "bilibili", "zhihu", "wechat_search")
                if source in planned
            ],
        },
        {
            "id": f"regional-{localized['language']}",
            "keyword": f"{localized['query']}{custom_suffix}",
            "sources": [source for source in ("tiktok", "instagram", "linkedin", "reddit", "youtube") if source in planned],
        },
    ]
    if custom_focus:
        topic = short_topic(custom_focus)
        for index, group in enumerate(groups):
            # 先给目标主题分配长度，再附加单个行为意图。
            intent = ("paid", "manual workflow", "付费 手工", "local payment")[index]
            group["keyword"] = (f"{topic} {intent}")[:100]
    if scope and scope["queries"]:
        # 每条宿主本地查询是一个独立意图，不再冒充四个不同的默认意图。
        groups = [
            {"id": f"scope-query-{index}", "keyword": item["query"],
             "sources": list(planned_sources), "language": item["language"]}
            for index, item in enumerate(scope["queries"], start=1)
        ]
    country = scope["countries"][day_index % len(scope["countries"])] if scope else "unknown"
    language = localized["language"] if scope and scope["queries"] else (scope["languages"][0] if scope else "unknown")
    for group in groups:
        group["country"] = country
        group.setdefault("language", language)
        if custom_focus or scope:
            group["sources"] = default_sources_for_language(group["sources"], group["language"])
    groups = [group for group in groups if group["sources"]]
    actual_sources = [source for source in planned_sources if any(source in group["sources"] for group in groups)]
    plan = build_search_plan(as_of=as_of.isoformat(), run_id=run_id, query_groups=groups)
    plan["scope"] = {
        "id": PHASE_ONE_ID,
        "platform_expansion_enabled": False,
        "available_sources": list(PHASE_ONE_TIKHUB_SOURCES),
        "planned_sources": actual_sources,
        "excluded_sources": [source for source in planned_sources if source not in actual_sources],
    }
    plan["localized_query"] = localized
    plan["coverage_note"] = "检索参数仅表示查询意图；未支持地域筛选的平台及默认参数不证明目标市场覆盖。"
    return finalize_plan(plan)


def build_plan(as_of: date, home: Path = DEFAULT_HOME, focus: str | None = None, *, scope: dict[str, Any] | None = None, intent_plan: dict[str, Any] | None = None, include_recent_activity: bool = False) -> dict[str, Any]:
    """构建独立、可审计且跨阶段共享 run_id 的 V3 计划。"""
    focus = resolve_focus(focus, None)
    scope = resolve_scope(scope)
    intent_plan = validate_intent_plan(intent_plan) if intent_plan is not None else None
    home = home.expanduser().resolve()
    preferences = _load_preferences(home)
    platform_phase = preferences.get("platform_phase", PHASE_ONE_ID)
    expansion_enabled = preferences.get("platform_expansion_enabled", False)
    if platform_phase != PHASE_ONE_ID or expansion_enabled is not False:
        raise ValueError("一期只允许 phase_1_existing_platforms，且 platform_expansion_enabled 必须为 false")
    mode = "targeted_scan" if focus or scope or intent_plan else "daily_radar"
    run_focus = json.dumps({"focus": focus, "scope": scope}, sort_keys=True, ensure_ascii=False) if scope else focus
    if intent_plan is not None:
        run_focus = json.dumps({"focus": focus, "scope": scope, "intent_plan": intent_plan}, sort_keys=True, ensure_ascii=False)
    run_id = make_run_id(as_of=as_of, mode=mode, focus=run_focus)
    query_focus = focus or (" ".join(scope[key] for key in ("task", "industry", "payer") if scope[key]) if scope else None)
    if scope and not query_focus:
        query_focus = " ".join(scope["countries"] + scope["languages"])
    focus_region = FOCUS_REGIONS[as_of.toordinal() % len(FOCUS_REGIONS)]
    if focus or scope or intent_plan:
        focus_region = {
            "id": "explicit_scope" if scope else "unknown",
            "name": ", ".join(scope["countries"]) if scope else "unknown",
            "markets": scope["countries"] if scope else ["unknown"],
            "languages": scope["languages"] if scope else ["unknown"],
            "localized_queries": (scope["queries"] if scope else []) or [{"language": "unknown", "query": short_topic(query_focus)}],
        }
    planned_sources, coverage_schedule = _daily_sources(as_of)
    if intent_plan is not None:
        retrieval_plans = compile_intents(intent_plan, as_of=as_of.isoformat(), run_id=run_id)
        coverage_schedule = {
            "strategy": "host_structured_intents",
            "planned_sources": list(dict.fromkeys(item["source"] for item in intent_plan["intents"])),
            "all_phase_one_sources": list(PHASE_ONE_TIKHUB_SOURCES),
        }
    else:
        community = build_community_plan(
            as_of=as_of.isoformat(), run_id=run_id,
            focus_name=focus_region["name"], custom_focus=query_focus,
        )
        retrieval_plans = {
            "community": route_community_focus(community, query_focus, scope),
            "tikhub": _tikhub_plan(as_of, run_id, focus_region, query_focus, planned_sources, scope),
        }
        if focus or scope:
            coverage_schedule["planned_sources"] = retrieval_plans["tikhub"]["scope"]["planned_sources"]
            coverage_schedule["excluded_sources"] = retrieval_plans["tikhub"]["scope"]["excluded_sources"]
    if include_recent_activity:
        from copy import deepcopy
        from aor.sources.planning import finalize_plan

        community = retrieval_plans["community"]
        for item in list(community["requests"]):
            if item["source"] == "github":
                activity = deepcopy(item)
                activity["id"] += "-recent-activity"
                activity["params"]["q"] = activity["params"]["q"].replace("created:", "updated:")
                community["requests"].append(activity)
        community["include_recent_activity"] = True
        finalize_plan(community)
        if len(community["requests"]) > 12:
            raise ValueError("开启近期活动后社区请求超过 12 个，请缩小查询范围")
    return {
        "schema_version": SCHEMA_VERSION,
        "query_plan_version": QUERY_PLAN_VERSION,
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "timezone": preferences.get("timezone", "Asia/Shanghai"),
        "home": str(home),
        "mode": mode,
        "custom_focus": focus,
        "research_scope": scope,
        "intent_plan": intent_plan,
        "phase_scope": {
            "id": PHASE_ONE_ID,
            "platform_expansion_enabled": False,
            "tikhub_available_sources": list(PHASE_ONE_TIKHUB_SOURCES),
            "auxiliary_internal_sources": list(PHASE_ONE_AUXILIARY_SOURCES),
        },
        "coverage_schedule": coverage_schedule,
        "windows": {name: _window(as_of, days) for name, days in (("7d", 7), ("30d", 30), ("90d", 90), ("365d", 365))},
        "core_regions": focus_region["markets"] if mode == "targeted_scan" else ["全球英语市场", "中国"],
        "focus_region": focus_region,
        "languages": focus_region["languages"] if mode == "targeted_scan" else list(dict.fromkeys(["英语", "中文", *focus_region["languages"]])),
        "opportunity_tracks": [
            {"id": "needle", "name": "针尖型机会", "target": 2},
            {"id": "new_form", "name": "老产品新形态", "target": 2},
            {"id": "regional_gap", "name": "区域错配型机会", "target": 1},
        ],
        "query_priority": [
            "付款、营收、订阅、定价、招聘、外包",
            "取消、切换、投诉、手工表格与替代方案",
            "目标地区的语言、支付、渠道与工作流差异",
            "泛讨论仅作补充，不单独进入正式机会",
        ],
        "idea_expansion_axes": ["细分人群", "购买触发", "AI 新形态", "地区与语言", "渠道嵌入", "价格与交付"],
        "allowed_sensitive_domains": ["恋爱约会与情感陪伴", "成人内容", "游戏虚拟角色与社交娱乐"],
        "forbidden_sensitive_domains": ["医疗诊断治疗", "金融投资建议", "法律意见", "儿童敏感产品"],
        "retrieval_plans": retrieval_plans,
        "output_contract": {
            "raw_candidates": preferences.get("raw_candidates_per_day", [100, 200]),
            "validated_quick_ideas": preferences.get("validated_quick_ideas_per_day", [20, 40]),
            "regional_migration_signals": preferences.get("regional_migration_signals_per_day", [30, 80]),
            "deep_opportunities": preferences.get("deep_opportunities_per_day", [3, 5]),
            "stable_id_format": "OPP-YYYYMMDD-XXXXXX",
            "signal_id_format": "SIG-YYYYMMDD-XXXXXX",
            "benchmark_id_format": "BENCH-XXXXXXXX",
            "formal_opportunity_hard_gates": [
                "existing_paid_market",
                "clear_payer",
                "current_alternative",
                "concrete_product_gap",
                "clear_acquisition_channel",
                "mvp_within_30_days",
            ],
            "preserve_original_quote": True,
            "translate_to_chinese": True,
            "report_source_health": True,
            "display_full_qualified_ledger": preferences.get("display_full_qualified_ledger", True),
            "near_miss_display_max": preferences.get("near_miss_display_max", 20),
        },
        "paid_retrieval_policy": {
            "strategy": "free_discovery_then_paid_gap_verification",
            "broad_discovery_budget_share_max": preferences.get("paid_discovery_budget_share_max", 0.2),
            "stop_after_paid_requests_without_new_benchmark_or_qualified_idea": preferences.get(
                "paid_no_yield_stop_requests", 3
            ),
            "require_target_evidence_gap_before_paid_run": True,
            "require_post_run_cost_yield_report": True,
        },
        "stage_contract": [
            "query_plan",
            "community_normalized",
            "evidence_gap_plan",
            "tikhub_gap_results",
            "tikhub_normalized",
            "paid_benchmarks",
            "expanded_candidates",
            "tiered_candidates",
            "full_result_digest",
            "candidates_with_ids",
            "scored_candidates",
            "validated_report",
            "state_observations",
        ],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AI 创业机会雷达 V3 查询计划")
    parser.add_argument("--date", default=beijing_today().isoformat(), help="北京时间截止日期，YYYY-MM-DD")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="报告与状态根目录")
    focus_group = parser.add_mutually_exclusive_group()
    focus_group.add_argument("--focus", help="仅用于可信固定值；用户输入优先使用 --focus-file")
    focus_group.add_argument("--focus-file", type=Path, help="从 UTF-8 文件安全读取定向扫描主题")
    parser.add_argument("--scope-file", type=Path, help="包含国家、语言、人群、任务及本地查询的结构化 JSON 范围")
    parser.add_argument("--intent-plan-file", type=Path, help="宿主提供的问题、商业证据类型、独立搜索与排序查询、来源、locale 及候选缺口")
    parser.add_argument("--include-recent-activity", action="store_true", help="额外检索近期有活动的旧 GitHub Issue")
    parser.add_argument("--output", type=Path, help="可选总计划输出文件；默认打印到标准输出")
    parser.add_argument("--export-community-plan", type=Path, help="导出本 Skill 自带的 HN/GitHub 查询计划")
    parser.add_argument("--export-tikhub-plan", type=Path, help="导出可独立估价和执行的 TikHub 查询计划")
    args = parser.parse_args()
    try:
        plan = build_plan(parse_date(args.date), args.home, resolve_focus(args.focus, args.focus_file), scope=read_scope(args.scope_file), intent_plan=read_json_file(args.intent_plan_file, 64000) if args.intent_plan_file else None, include_recent_activity=args.include_recent_activity)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.export_community_plan:
        _write_json(args.export_community_plan, plan["retrieval_plans"]["community"])
    if args.export_tikhub_plan:
        _write_json(args.export_tikhub_plan, plan["retrieval_plans"]["tikhub"])
    payload = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
