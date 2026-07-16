#!/usr/bin/env python3
"""生成 AI 创业机会雷达 V3 的确定性查询计划。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from community_query import build_community_plan
from contracts import QUERY_PLAN_VERSION, SCHEMA_VERSION, beijing_today, make_run_id
from tikhub_query import build_search_plan


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
) -> dict[str, Any]:
    day_index = as_of.toordinal()
    custom_suffix = f" {custom_focus[:35]}" if custom_focus else ""
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
    groups = [group for group in groups if group["sources"]]
    plan = build_search_plan(as_of=as_of.isoformat(), run_id=run_id, query_groups=groups)
    plan["scope"] = {
        "id": PHASE_ONE_ID,
        "platform_expansion_enabled": False,
        "available_sources": list(PHASE_ONE_TIKHUB_SOURCES),
        "planned_sources": planned_sources,
    }
    plan["localized_query"] = localized
    return plan


def build_plan(as_of: date, home: Path = DEFAULT_HOME, focus: str | None = None) -> dict[str, Any]:
    """构建独立、可审计且跨阶段共享 run_id 的 V3 计划。"""
    home = home.expanduser().resolve()
    preferences = _load_preferences(home)
    platform_phase = preferences.get("platform_phase", PHASE_ONE_ID)
    expansion_enabled = preferences.get("platform_expansion_enabled", False)
    if platform_phase != PHASE_ONE_ID or expansion_enabled is not False:
        raise ValueError("一期只允许 phase_1_existing_platforms，且 platform_expansion_enabled 必须为 false")
    mode = "targeted_scan" if focus else "daily_radar"
    run_id = make_run_id(as_of=as_of, mode=mode, focus=focus)
    focus_region = FOCUS_REGIONS[as_of.toordinal() % len(FOCUS_REGIONS)]
    planned_sources, coverage_schedule = _daily_sources(as_of)
    community = build_community_plan(
        as_of=as_of.isoformat(),
        run_id=run_id,
        focus_name=focus_region["name"],
        custom_focus=focus,
    )
    tikhub = _tikhub_plan(as_of, run_id, focus_region, focus, planned_sources)
    return {
        "schema_version": SCHEMA_VERSION,
        "query_plan_version": QUERY_PLAN_VERSION,
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "timezone": preferences.get("timezone", "Asia/Shanghai"),
        "home": str(home),
        "mode": mode,
        "custom_focus": focus,
        "phase_scope": {
            "id": PHASE_ONE_ID,
            "platform_expansion_enabled": False,
            "tikhub_available_sources": list(PHASE_ONE_TIKHUB_SOURCES),
            "auxiliary_internal_sources": list(PHASE_ONE_AUXILIARY_SOURCES),
        },
        "coverage_schedule": coverage_schedule,
        "windows": {name: _window(as_of, days) for name, days in (("7d", 7), ("30d", 30), ("90d", 90), ("365d", 365))},
        "core_regions": ["全球英语市场", "中国"],
        "focus_region": focus_region,
        "languages": list(dict.fromkeys(["英语", "中文", *focus_region["languages"]])),
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
        "retrieval_plans": {"community": community, "tikhub": tikhub},
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
    parser.add_argument("--output", type=Path, help="可选总计划输出文件；默认打印到标准输出")
    parser.add_argument("--export-community-plan", type=Path, help="导出本 Skill 自带的 HN/GitHub 查询计划")
    parser.add_argument("--export-tikhub-plan", type=Path, help="导出可独立估价和执行的 TikHub 查询计划")
    args = parser.parse_args()
    try:
        plan = build_plan(parse_date(args.date), args.home, resolve_focus(args.focus, args.focus_file))
    except ValueError as exc:
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
    raise SystemExit(main())
