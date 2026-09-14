"""宿主意图契约与实际请求去重，不进行模型翻译或网络访问。"""

from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from typing import Any

from aor.request_identity import request_fingerprint

from .registry import COMMUNITY_SOURCES, EVIDENCE_ROLES, source_catalog


def english_query_available(query: str) -> bool:
    """只排除明显非拉丁文字，不声称自动判断或翻译自然语言。"""
    return bool(re.search(r"[A-Za-z]", query)) and all(
        not char.isalpha() or "LATIN" in unicodedata.name(char, "") for char in query
    )


def text_field(value: Any, field: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or "\x00" in value:
        raise ValueError(f"{field} 必须是 1 到 {limit} 字符的文本")
    return value.strip()


def validate_intent_plan(plan: Any) -> dict[str, Any]:
    """验证 {intents: [...]}；每个意图只有一个 source 和一个检索查询。"""
    from tikhub_query import validate_query_locale

    if not isinstance(plan, dict) or set(plan) != {"intents"}:
        raise ValueError("intent_plan 必须只包含 intents 数组")
    if not isinstance(plan["intents"], list) or not 1 <= len(plan["intents"]) <= 20:
        raise ValueError("intents 必须包含 1 到 20 个结构化意图")
    fields = {"id", "question", "evidence_type", "search_query", "ranking_query", "source", "locale", "candidate_gaps"}
    sources = {row["source"] for row in source_catalog()}
    seen = set()
    result = []
    for raw in plan["intents"]:
        if not isinstance(raw, dict) or not fields <= set(raw) or set(raw) - fields - {"industry_ids"}:
            raise ValueError("单条意图必须包含：" + ", ".join(sorted(fields)))
        item = deepcopy(raw)
        if "industry_ids" in item:
            if not isinstance(item["industry_ids"], list) or len(item["industry_ids"]) > 10:
                raise ValueError("industry_ids 必须为最多 10 项的数组")
            item["industry_ids"] = [text_field(v, "industry_ids", 36) for v in item["industry_ids"]]
        for name in fields - {"locale", "candidate_gaps"}:
            item[name] = text_field(item[name], name, 100 if name == "search_query" else 1000)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", item["id"]) or item["id"].endswith("-") or item["id"] in seen:
            raise ValueError("意图 id 必须唯一，使用不超过 40 字符的小写字母数字、点、横线或下划线，不能以横线结尾")
        seen.add(item["id"])
        if item["source"] not in sources or item["evidence_type"] not in EVIDENCE_ROLES:
            raise ValueError("意图 source 或 evidence_type 不在来源目录中")
        locale = item["locale"]
        if not isinstance(locale, dict) or set(locale) != {"country", "language"}:
            raise ValueError("locale 必须包含 country 和 language")
        item["locale"] = validate_query_locale(**locale)
        if item["source"] in COMMUNITY_SOURCES and (
            locale["language"].split("-")[0] != "en" or not english_query_available(item["search_query"])
        ):
            raise ValueError("Hacker News/GitHub 需要宿主提供 locale.language=en 的英语 search_query；不会机械翻译")
        gaps = item["candidate_gaps"]
        if not isinstance(gaps, list) or len(gaps) > 20:
            raise ValueError("candidate_gaps 必须是不超过 20 项的数组，可为空")
        item["candidate_gaps"] = [text_field(gap, "candidate_gaps", 200) for gap in gaps]
        result.append(item)
    return {"intents": result}


def deduplicate_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留首个请求 ID，合并全部 provenance 和别名，可重复调用且不修改输入。"""
    result: dict[str, dict[str, Any]] = {}
    for original in requests:
        item = deepcopy(original)
        fingerprint = request_fingerprint(item)
        provenance = item.pop("provenance", None)
        if provenance is None:
            query = next((item["params"][key] for key in ("keyword", "query", "q", "searchTerms", "search_query") if key in item["params"]), None)
            provenance = [{"intent_id": item.get("query_group"), "search_query": query,
                           "ranking_query": item.get("ranking_query"), "locale": item.get("query_scope")}]
        aliases = item.pop("request_aliases", [item["id"]])
        if fingerprint not in result:
            item.update(request_fingerprint=fingerprint, provenance=[], request_aliases=[], intent_refs=[])
            result[fingerprint] = item
        kept = result[fingerprint]
        for key, values in (("provenance", provenance), ("request_aliases", aliases)):
            for value in values:
                if value not in kept[key]:
                    kept[key].append(value)
        for origin in provenance:
            intent_id = origin.get("id") or origin.get("intent_id")
            if intent_id and intent_id not in kept["intent_refs"]:
                kept["intent_refs"].append(intent_id)
    return list(result.values())


def finalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """就地补充请求去重与就绪状态；不把草稿标记为执行结果。"""
    count = len(plan["requests"])
    plan["requests"] = deduplicate_requests(plan["requests"])
    plan["deduplication"] = {"input_requests": count, "unique_requests": len(plan["requests"]),
                             "removed_duplicates": count - len(plan["requests"])}
    plan.setdefault("plan_status", "ready" if plan["requests"] else "not_requested")
    return plan


def route_community_focus(plan: dict[str, Any], focus: str | None, scope: dict[str, Any] | None) -> dict[str, Any]:
    """使用宿主英语查询或保留待办；不把非英语主题拼上英文当作翻译。"""
    english = [row["query"] for row in (scope or {}).get("queries", []) if row["language"].split("-")[0] == "en" and english_query_available(row["query"])]
    if english:
        requests = []
        for index, query in enumerate(english, start=1):
            for template in plan["requests"]:
                item = deepcopy(template)
                item["id"] += f"-scope-{index}"
                item["query_group"] += f"-scope-{index}"
                key = "query" if item["source"] == "hackernews" else "q"
                item["params"][key] = query
                item["relevance_query"] = query
                if key == "q":
                    item["params"][key] += f" is:issue created:{plan['window']['range_from']}..{plan['as_of']}"
                item["query_scope"] = {"country": "unknown", "language": "en"}
                requests.append(item)
        plan["requests"] = requests
        finalize_plan(plan)
        if len(plan["requests"]) > 12:
            raise ValueError("英语 scope 查询在 HN/GitHub 去重后超过 12 个请求，请拆分计划")
        return plan
    if not focus or english_query_available(focus) and (not scope or all(language.split("-")[0] == "en" for language in scope["languages"])):
        return finalize_plan(plan)
    plan["requests"] = []
    plan["plan_status"] = "needs_host_queries"
    plan["required_queries"] = [{"source": source, "language": "en", "topic": focus,
                                  "reason": "请宿主提供自然英语 search_query 与独立 ranking_query"} for source in COMMUNITY_SOURCES]
    return finalize_plan(plan)


def compile_intents(intent_plan: dict[str, Any], *, as_of: str, run_id: str) -> dict[str, Any]:
    """将宿主意图转换为既有白名单请求；人工来源只产生导入待办。"""
    from community_query import build_community_plan, validate_plan as validate_community
    from tikhub_query import build_search_plan
    from .registry import TIKHUB_SOURCES

    validated = validate_intent_plan(intent_plan)
    community = build_community_plan(as_of=as_of, run_id=run_id, focus_name="unknown")
    templates = {item["source"]: item for item in community["requests"]}
    community["requests"] = []
    paid_groups = []
    by_id = {}
    imports = []
    for intent in validated["intents"]:
        source = intent["source"]
        by_id[intent["id"]] = intent
        if source in COMMUNITY_SOURCES:
            item = deepcopy(templates[source])
            item.update(id=intent["id"], query_group=intent["id"], ranking_query=intent["ranking_query"], relevance_query=intent["search_query"],
                        query_scope=intent["locale"], provenance=[intent])
            key = "query" if source == "hackernews" else "q"
            item["params"][key] = intent["search_query"]
            if source == "github":
                item["params"][key] += f" is:issue created:{community['window']['range_from']}..{as_of}"
            community["requests"].append(item)
        elif source in TIKHUB_SOURCES:
            paid_groups.append({"id": intent["id"], "keyword": intent["search_query"], "sources": [source], **intent["locale"]})
        else:
            imports.append({**intent, "status": "host_verification_required", "automatic_fetch": False})
    # 宿主未请求付费来源时保留空草稿，不能送给估价/执行器当作有效请求。
    tikhub = build_search_plan(as_of=as_of, run_id=run_id, query_groups=paid_groups) if paid_groups else {
        "schema_version": community["schema_version"], "provider": "tikhub",
        "run_id": run_id, "as_of": as_of, "stage": "search_discovery",
        "scope": {"id": "phase_1_existing_platforms", "platform_expansion_enabled": False},
        "requests": [],
        "cost_policy": {"price_source": "live_dashboard_before_run",
                        "discount_policy": "conservative_list_price_unless_explicit_rate",
                        "max_attempts": 1, "comments_are_separate_plan": True},
    }
    for item in tikhub["requests"]:
        item["provenance"] = [by_id[item["query_group"]]]
        item["ranking_query"] = by_id[item["query_group"]]["ranking_query"]
    finalize_plan(community)
    finalize_plan(tikhub)
    if community["requests"]:
        validate_community(community)
    return {"community": community, "tikhub": tikhub,
            "web_import": {"provider": "host-verified-web", "run_id": run_id, "as_of": as_of,
                           "requests": [], "required_imports": imports, "automatic_fetch": False}}
