"""可扩展行业目录与跨行业发现计划；查询标签不等于市场证据。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import re
from pathlib import Path

from .registry import TIKHUB_SOURCES, source_catalog

DISCOVERY_LENSES = ("event", "workflow", "artifact", "positive", "capability_change")
DISCOVERY_CHECKS = ["recent_user_behavior", "existing_alternative", "non_adoption_reason", "counter_evidence"]


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
    subtracks = row.get("subtracks", [])
    if not isinstance(subtracks, list) or len(subtracks) > 50:
        raise ValueError("subtracks 必须是最多 50 项的数组")
    entries = row.get("discovery_entries", [])
    if not isinstance(entries, list) or len(entries) > 50:
        raise ValueError("discovery_entries 必须是最多 50 项的数组")
    seen = set()
    for task in [*subtracks, *entries]:
        identifier = task.get("id") if isinstance(task, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,35}", identifier) or identifier in seen:
            raise ValueError("子赛道 id 必须合法且在行业内唯一")
        seen.add(identifier)
        for field in ("name", "audience", "job_to_be_done", "existing_behavior_to_verify"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise ValueError(f"子赛道缺少 {field}")
        for language in ("en", "zh"):
            queries = task.get("queries", {}).get(language)
            if not isinstance(queries, list) or not queries or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 100 for q in queries):
                raise ValueError("子赛道必须包含中英文查询")
        if task in entries and task.get("lens") not in DISCOVERY_LENSES:
            raise ValueError("discovery_entries.lens 必须是已定义的任务发现入口")
        known_sources = {r["source"] for r in source_catalog()}
        if not set(task.get("manual_sources", [])) <= known_sources:
            raise ValueError("子赛道人工来源必须来自已有来源目录")
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


def _history(home: Path, run_id: str) -> dict:
    """只读取已落盘覆盖快照；规划意图不算采集历史，损坏文件不会猜作已完成。"""
    tasks, consulted, ignored = {}, 0, 0
    paths = sorted((Path(home) / "runs").glob("*/industry-coverage.json"), reverse=True)[:60]
    for path in paths:
        if path.parent.name == run_id:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                ignored += 1
                continue
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if snapshot.get("version") not in {"2.0", "2.1"}:
                continue
            consulted += 1
            for row in snapshot.get("tasks", []):
                attempts = row.get("request_count", 0)
                if not isinstance(attempts, int) or attempts < 1:
                    continue
                key = (row.get("task_id"), row.get("language"))
                entry = tasks.setdefault(key, {"attempts": 0, "last_attempt": "", "review_gaps": []})
                entry["attempts"] += attempts
                observed = str(snapshot.get("as_of") or "")
                if observed >= entry["last_attempt"]:
                    entry.update(last_attempt=observed, review_gaps=row.get("review_gaps", []))
        except (OSError, ValueError, TypeError, AttributeError):
            ignored += 1
    return {"tasks": tasks, "snapshots_consulted": consulted, "ignored_snapshots": ignored}


def _task_for(row: dict, language: str, as_of: date, history: dict, *, offset: int = 0) -> tuple[dict, dict]:
    tasks = row.get("discovery_entries") or row.get("subtracks") or [{"id": "general", "name": row["name"], "audience": row["audience"],
        "queries": row["queries"], "job_to_be_done": row["demand_model"],
        "existing_behavior_to_verify": "核验已有使用与支出行为", "manual_sources": ["web"]}]
    rotation = (as_of.toordinal() + offset) % len(tasks)
    def rank(pair):
        index, task = pair
        state = history.get("tasks", {}).get((f"{row['id']}.{task['id']}", language), {})
        attempts = state.get("attempts", 0)
        # 一次明确缺口补查优先，随后继续轮转，防止同一困难主题一直占满预算。
        followup = attempts == 1 and bool(state.get("review_gaps"))
        return (not followup, attempts, state.get("last_attempt", ""), (index - rotation) % len(tasks))
    _, task = min(enumerate(tasks), key=rank)
    state = history.get("tasks", {}).get((f"{row['id']}.{task['id']}", language), {})
    return task, state


def build_industry_discovery(*, as_of: date, run_id: str, home: Path,
                             preferences: dict, planned_sources: list[str], history: dict | None = None) -> dict:
    from community_query import build_community_plan
    from tikhub_query import build_search_plan
    from .planning import finalize_plan
    from .registry import default_sources_for_language

    catalog = load_industries(home)
    selected = preferences.get("industries", [row["id"] for row in catalog])
    if (not isinstance(selected, list) or not selected or any(not isinstance(v, str) for v in selected)
            or len(selected) != len(set(selected))
            or not set(selected) <= {row["id"] for row in catalog}):
        raise ValueError("preferences.industries 必须是目录中不重复的行业 ID 数组")
    active = [row for row in catalog if row["id"] in selected]
    history = _history(home, run_id) if history is None else history
    requests, imports, tasks, unavailable = [], [], [], []
    paid = None
    for index, row in enumerate(active):
        chosen = {}
        for language in ("en", "zh"):
            task, state = _task_for(row, language, as_of, history, offset=index)
            chosen[language] = task
            query = task["queries"][language][as_of.toordinal() % len(task["queries"][language])]
            choices = [s for s in row["sources"][language] if s in planned_sources]
            if not choices:
                choices = default_sources_for_language([s for s in planned_sources if s in TIKHUB_SOURCES], language)
            identifier, task_id = f"{row['id']}-{language}", f"{row['id']}.{task['id']}"
            locale = {"country": "CN" if language == "zh" else "unknown", "language": language}
            priority = 0 if state.get("attempts") == 1 and state.get("review_gaps") else 1
            origin = {"id": identifier, "industry_ids": [row["id"]], "subtrack_ids": [task["id"]],
                      "task_id": task_id, "search_query": query, "question": task["job_to_be_done"],
                      "candidate_gaps": list(state.get("review_gaps", [])),
                      "discovery_lens": task.get("lens", "legacy_seed"),
                      "artifact_to_verify": task.get("artifact"), "required_checks": DISCOVERY_CHECKS,
                      "hypothesis_status": "retrieval_seed_not_observed_demand",
                      "evidence_type": "workflow_pain" if row["demand_model"] == "efficiency" else "usage_behavior",
                      "locale": locale}
            tasks.append({**origin, "language": language, "status": "planned" if choices else "not_scheduled",
                          "prior_attempt_count": state.get("attempts", 0), "research_priority": priority,
                          "selection_reason": "unresolved_evidence_gap" if priority == 0 else "least_attempted_task_rotation"})
            if not choices:
                unavailable.append({"id": identifier, **origin, "source": None, "reason": "no_configured_source_for_language"})
                continue
            offset = (as_of.toordinal() + index) % len(choices)
            choices = choices[offset:] + choices[:offset]
            child = build_search_plan(as_of=as_of.isoformat(), run_id=run_id, query_groups=[
                {"id": identifier, "keyword": query, "sources": choices, **locale}])
            paid = paid or child
            alternatives = []
            for item in child["requests"]:
                item.update(provenance=[origin], industry_ids=[row["id"]], subtrack_ids=[task["id"]],
                            task_id=task_id, discovery_lens=origin["discovery_lens"],
                            research_priority=priority, ranking_query=task["job_to_be_done"])
                alternatives.append(item)
            primary = alternatives[0]
            for alternate in alternatives[1:]:
                alternate["fallback_for_request_id"] = primary["id"]
            primary["fallback_requests"] = alternatives[1:]
            requests.append(primary)
        manual_language = "zh" if (as_of.toordinal() + index) % 2 else "en"
        task = chosen[manual_language]
        imports.append({"id": row["id"] + "-web", "industry_ids": [row["id"]], "subtrack_ids": [task["id"]],
                        "task_id": f"{row['id']}.{task['id']}", "source": "web",
                        "question": f"核验{task['name']}的用户操作、产物、现成替代和未采用原因",
                        "search_query": task["queries"][manual_language][0], "evidence_type": "usage_behavior",
                        "locale": {"country": "CN" if manual_language == "zh" else "unknown", "language": manual_language},
                        "discovery_lens": task.get("lens", "legacy_seed"),
                        "suggested_sources": task.get("manual_sources", ["web"]),
                        "required_checks": DISCOVERY_CHECKS,
                        "status": "host_verification_required", "automatic_fetch": False})
    if paid is None:
        paid = {"schema_version": "3.0", "provider": "tikhub", "run_id": run_id, "as_of": as_of.isoformat(),
                "stage": "search_discovery", "cost_policy": {"max_attempts": 1}}
    paid["requests"] = requests
    paid["skipped_requests"] = unavailable
    paid["catalog_version"] = "3.0"
    paid["research_tasks"] = tasks
    paid["selection_history"] = {k: v for k, v in history.items() if k != "tasks"}
    paid["discovery_objective"] = "从事件、操作、用户产物、正向行为与能力变化发现任务；核验替代与未采用原因，后续再判断产品和付款"
    paid["cost_policy"].update(purpose="cross_industry_discovery", explicit_budget_required=True)
    community = build_community_plan(as_of=as_of.isoformat(), run_id=run_id, focus_name="全球与中国")
    templates = {r["source"]: r for r in community["requests"]}
    community["requests"] = []
    for i in range(min(3, len(active))):
        row = active[(as_of.toordinal() * 3 + i) % len(active)]
        task, _ = _task_for(row, "en", as_of, history, offset=active.index(row))
        query = task["queries"]["en"][0]
        origin = {"id": f"hn-{row['id']}", "industry_ids": [row["id"]], "subtrack_ids": [task["id"]],
                  "task_id": f"{row['id']}.{task['id']}", "search_query": query,
                  "locale": {"country": "unknown", "language": "en"}}
        item = deepcopy(templates["hackernews"])
        item.update(id=origin["id"], query_group=origin["id"], relevance_query=query,
                    ranking_query=task["job_to_be_done"], industry_ids=[row["id"]],
                    task_id=origin["task_id"], subtrack_ids=[task["id"]], provenance=[origin],
                    query_scope=origin["locale"], source_role="auxiliary")
        item["params"]["query"] = query
        community["requests"].append(item)
    return {"catalog": catalog, "catalog_version": "3.0", "selected": selected,
            "retrieval_plans": {"community": finalize_plan(community), "tikhub": finalize_plan(paid),
                "web_import": {"provider": "host-verified-web", "run_id": run_id, "as_of": as_of.isoformat(),
                               "requests": [], "required_imports": imports, "automatic_fetch": False}}}
