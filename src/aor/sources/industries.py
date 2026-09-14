"""可扩展行业目录与跨行业发现计划；查询标签不等于市场证据。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import re
from pathlib import Path

from .registry import TIKHUB_SOURCES


def load_industries(home: Path | None = None) -> list[dict]:
    """内置目录可由 DATA_HOME/config/industries.json 同 ID 覆盖或新增。"""
    rows = json.loads(Path(__file__).with_name("industries.json").read_text(encoding="utf-8"))
    custom = Path(home) / "config/industries.json" if home else None
    if custom and custom.exists():
        additions = json.loads(custom.read_text(encoding="utf-8"))
        if not isinstance(additions, list):
            raise ValueError("industries.json 必须是行业对象数组")
        merged = {row["id"]: row for row in rows}
        for row in additions:
            validate_industry(row)
            merged[row["id"]] = row
        rows = list(merged.values())
    if not 1 <= len(rows) <= 50:
        raise ValueError("行业目录应包含 1–50 项；更大目录请按研究主题拆分")
    seen = set()
    for row in rows:
        validate_industry(row)
        if row["id"] in seen:
            raise ValueError("行业 ID 重复")
        seen.add(row["id"])
    return deepcopy(rows)


def validate_industry(row: dict) -> None:
    if not isinstance(row, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{1,35}", str(row.get("id", ""))):
        raise ValueError("行业 id 必须是 2–36 位小写字母、数字或下划线")
    for field in ("name", "audience", "demand_model"):
        if not isinstance(row.get(field), str) or not 1 <= len(row[field].strip()) <= 200:
            raise ValueError(f"行业缺少 {field}")
    for language in ("en", "zh"):
        queries = row.get("queries", {}).get(language)
        if not isinstance(queries, list) or not queries or any(
            not isinstance(q, str) or not 1 <= len(q.strip()) <= 80 or "\x00" in q for q in queries
        ):
            raise ValueError(f"行业 {row['id']} 缺少自然语言 {language} 查询")
        sources = row.get("sources", {}).get(language)
        if not isinstance(sources, list) or not sources or any(s not in TIKHUB_SOURCES for s in sources):
            raise ValueError(f"行业 {row['id']} 来源必须使用已有适配器")


def industry_ids(row: dict) -> list[str]:
    """沿查询引用读取标签；不按关键词猜测行业，不把标签当相关性结论。"""
    result = set()
    def visit(value):
        if isinstance(value, dict):
            ids = value.get("industry_ids", [])
            if isinstance(ids, list):
                result.update(item for item in ids if isinstance(item, str))
            for key in ("provenance", "query_metadata", "retrieval"):
                visit(value.get(key))
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(row)
    return sorted(result)


def build_industry_discovery(*, as_of: date, run_id: str, home: Path,
                             preferences: dict, planned_sources: list[str]) -> dict:
    from community_query import build_community_plan
    from tikhub_query import build_search_plan
    from .planning import finalize_plan

    catalog = load_industries(home)
    selected = preferences.get("industries", [row["id"] for row in catalog])
    if (not isinstance(selected, list) or not selected or any(not isinstance(v, str) for v in selected)
            or len(selected) != len(set(selected))
            or not set(selected) <= {row["id"] for row in catalog}):
        raise ValueError("preferences.industries 必须是目录中不重复的行业 ID 数组")
    active = [row for row in catalog if row["id"] in selected]
    groups, origins, imports = [], {}, []
    for index, row in enumerate(active):
        for language in ("en", "zh"):
            query = row["queries"][language][as_of.toordinal() % len(row["queries"][language])]
            choices = [s for s in row["sources"][language] if s in planned_sources]
            if not choices:
                from .registry import default_sources_for_language
                choices = default_sources_for_language(planned_sources, language)
            source = choices[(as_of.toordinal() + index) % len(choices)]
            identifier = f"{row['id']}-{language}"
            origin = {"id": identifier, "industry_ids": [row["id"]], "search_query": query,
                      "evidence_type": "workflow_pain" if row["demand_model"] == "efficiency" else "usage_behavior",
                      "locale": {"country": "CN" if language == "zh" else "unknown", "language": language}}
            groups.append({"id": identifier, "keyword": query, "sources": [source], **origin["locale"]})
            origins[identifier] = origin
        language = "zh" if (as_of.toordinal() + index) % 2 else "en"
        query = row["queries"][language][0]
        imports.append({"id": row["id"] + "-web", "industry_ids": [row["id"]], "source": "web",
                        "question": f"核验{row['name']}的收费对标、真实用户行为和免费替代", "search_query": query,
                        "evidence_type": "product_review", "locale": {"country": "CN" if language == "zh" else "unknown", "language": language},
                        "status": "host_verification_required", "automatic_fetch": False})
    paid = None
    for offset in range(0, len(groups), 20):
        child = build_search_plan(as_of=as_of.isoformat(), run_id=run_id, query_groups=groups[offset:offset + 20])
        if paid is None:
            paid = child
        else:
            paid["requests"].extend(child["requests"])
    for item in paid["requests"]:
        item["provenance"] = [origins[item["query_group"]]]
        item["industry_ids"] = origins[item["query_group"]]["industry_ids"]
    paid["discovery_objective"] = "跨行业寻找真实任务、持续使用、收费对标及反证；不要求先有开发者候选"
    paid["cost_policy"].update(purpose="cross_industry_discovery", explicit_budget_required=True)
    community = build_community_plan(as_of=as_of.isoformat(), run_id=run_id, focus_name="全球与中国")
    templates = {r["source"]: r for r in community["requests"]}
    community["requests"] = []
    nondev = [r for r in active if r["id"] != "developer_tools"] or active
    for i in range(min(3, len(nondev))):
        row = nondev[(as_of.toordinal() * 3 + i) % len(nondev)]
        query = row["queries"]["en"][as_of.toordinal() % len(row["queries"]["en"])]
        item = deepcopy(templates["hackernews"])
        item.update(id=f"hn-{row['id']}", query_group=f"hn-{row['id']}", relevance_query=query,
                    ranking_query=f"核验{row['name']}的具体任务与用户需求", industry_ids=[row["id"]],
                    provenance=[{"id": f"hn-{row['id']}", "industry_ids": [row["id"]], "search_query": query}])
        item["params"]["query"] = query
        community["requests"].append(item)
    if "developer_tools" in selected:
        item = deepcopy(templates["github"])
        query = '"missing integration"'
        item.update(id="github-developer-tools", query_group="developer-tools", relevance_query=query,
                    ranking_query="开发工具的具体集成缺口，不将协作日志当作买家需求",
                    industry_ids=["developer_tools"], provenance=[{"id": "developer-tools", "industry_ids": ["developer_tools"], "search_query": query}])
        item["params"]["q"] = f'{query} is:issue created:{community["window"]["range_from"]}..{as_of}'
        community["requests"].append(item)
    return {"catalog": catalog, "selected": selected,
            "retrieval_plans": {"community": finalize_plan(community), "tikhub": finalize_plan(paid),
                "web_import": {"provider": "host-verified-web", "run_id": run_id, "as_of": as_of.isoformat(),
                               "requests": [], "required_imports": imports, "automatic_fetch": False}}}
