"""由已绑定原文的用户观察提出有限补查；纯规划，不执行搜索或付费。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import re

from contracts import canonical_sha256, normalize_identity
from .planning import english_query_available, validate_intent_plan

MAX_FAMILIES = 8
MAX_TASKS = MAX_FAMILIES * 3
MAX_REFERENCES = 20
CHECKS = ["opened_user_original", "existing_alternative", "non_adoption_reason", "counter_evidence"]
DIRECT_FEEDBACK = {"usage", "purchase_claim", "refund_claim", "recommendation_request", "positive_behavior"}


def _text(value: object) -> str:
    """字段仅作为搜索数据，去掉控制字符；不解析链接、命令或宿主指令。"""
    return re.sub(r"\s+", " ", "".join(c for c in value if c.isprintable())).strip()[:500] if isinstance(value, str) else ""


def _query(parts: list[str], suffix: str = "") -> str:
    # 给每个实际字段留下空间，避免长 task 吞掉后面的产物/绕行办法。
    parts = list(dict.fromkeys(_text(value) for value in parts if _text(value)))[:3]
    if not parts:
        return ""
    room = 100 - len(suffix) - (1 if suffix else 0) - len(parts) + 1
    width = max(1, room // len(parts))
    fragments = []
    for part in parts:
        fragment = part[:width]
        if len(part) > width and " " in fragment:
            fragment = fragment.rsplit(" ", 1)[0]
        fragments.append(fragment)
    return " ".join([*fragments, *([suffix] if suffix else [])]).strip()


def _language(query: str, declared: str) -> str:
    # 不能把中文观察加英文尾词后标成英语；也不把所有拉丁文字猜成英语。
    if declared == "zh" and re.search(r"[\u3400-\u9fff]", query) and not re.search(r"[\u3040-\u30ff]", query):
        return "zh"
    if declared == "en" and english_query_available(query):
        return "en"
    return "unknown"


def _references(observation: dict) -> list[dict]:
    values = observation.get("evidence_refs")
    if not isinstance(values, list) or not values:
        return []
    result = []
    for value in values:
        if not isinstance(value, dict) or any(not _text(value.get(k)) for k in ("evidence_id", "revision_id", "quote")):
            return []
        # 不凭字段宣称验证完成；调用方必须先用 build/validate_user_discovery 核验原文。
        result.append({key: value[key] for key in ("evidence_id", "revision_id", "quote")})
    return result


def _proposals(row: dict) -> list[tuple[str, str]]:
    task = _text(row.get("task"))
    terms = row.get("query_terms") or []
    anchor = " ".join(_text(term) for term in terms[:3]) if isinstance(terms, list) else ""
    anchor = anchor or task
    language = _language(anchor, row.get("language", "unknown"))
    artifact = _text(row.get("artifact"))
    workaround = _text(row.get("current_workaround"))
    trigger = _text(row.get("trigger"))
    outcome = _text(row.get("desired_outcome")) or _text(row.get("need"))
    suffixes = ({"alternative": "现成方法 替代 服务", "non_adoption": "试过 放弃 没有购买 原因"} if language == "zh"
                else {"alternative": "alternatives services", "non_adoption": "tried stopped using why"} if language == "en"
                else {"alternative": "", "non_adoption": ""})
    return [("workflow_artifact", _query([anchor, artifact or trigger, workaround])),
            ("alternative", _query([anchor, outcome], suffixes["alternative"])),
            ("non_adoption", _query([anchor, workaround or trigger], suffixes["non_adoption"]))]


def build_task_followups(user_discovery: dict, *, as_of: str, run_id: str) -> dict:
    """消费 build_user_discovery 的产物；每任务族至多三条、每轮至多八族。

    查询 key 不含产品、方案、日期或 run_id，同一观察重复规划不会产生新任务。
    可选 completed_followup_query_keys 仅用于调用方传入已经做过的查询 key；
    它不改变 user-discovery 的存储契约。此函数不会拿自己输出递归生成观察。
    """
    date.fromisoformat(as_of)
    if not isinstance(user_discovery, dict) or not isinstance(user_discovery.get("observations"), list):
        raise ValueError("task followup 需要已核验的 user_discovery.observations")
    observations = user_discovery["observations"]
    if len(observations) > 2000:
        raise ValueError("task followup 最多接收 2000 条观察")
    completed = user_discovery.get("completed_followup_query_keys", [])
    if not isinstance(completed, list) or len(completed) > 10000 or any(not isinstance(v, str) for v in completed):
        raise ValueError("completed_followup_query_keys 必须是最多 10000 项的查询 key 数组")
    completed = set(completed)
    families, skipped = {}, []
    for row in observations:
        if not isinstance(row, dict):
            raise ValueError("task followup 观察必须是对象")
        identifier = _text(row.get("observation_id"))
        refs = _references(row)
        reason = ("demo_observation" if row.get("is_demo") else
                  "not_direct_user_behavior" if row.get("feedback_type") not in DIRECT_FEEDBACK else
                  "missing_bound_observation" if not identifier.startswith("OBS-") or not refs or not _text(row.get("task")) else None)
        if reason:
            skipped.append({"observation_id": identifier or None, "reason": reason})
            continue
        family = row.get("task_family_id") or "TASK-" + canonical_sha256({
            key: normalize_identity(_text(row.get(key))) for key in ("target_user", "task")})[:16].upper()
        families.setdefault(family, []).append(row)
    tasks, seen = [], set()
    # 轮换超过预算的任务族，身份和查询 key 仍保持稳定；不会永久偏向 ID 字典序。
    ordered = sorted(families)
    if len(ordered) > MAX_FAMILIES:
        offset = date.fromisoformat(as_of).toordinal() % len(ordered)
        ordered = ordered[offset:] + ordered[:offset]
        remaining, selected, industries_seen, languages_seen = list(ordered), [], set(), set()
        while remaining and len(selected) < MAX_FAMILIES:
            def breadth(family):
                industry_ids = {value for row in families[family] for value in row.get("industry_ids", [])}
                languages = {row.get("language", "unknown") for row in families[family]} - {"unknown"}
                return (-(2 * len(industry_ids - industries_seen) + len(languages - languages_seen)), ordered.index(family))
            family = min(remaining, key=breadth)
            selected.append(family)
            remaining.remove(family)
            industries_seen.update(value for row in families[family] for value in row.get("industry_ids", []))
            languages_seen.update(row.get("language", "unknown") for row in families[family])
        ordered = selected + remaining
    for family_index, family in enumerate(ordered):
        rows = sorted(families[family], key=lambda r: r["observation_id"])
        if family_index >= MAX_FAMILIES:
            skipped.append({"task_family_id": family, "reason": "family_budget", "observation_count": len(rows)})
            continue
        # 实际操作字段更完整的原文优先；产品和交付形态不参与选择或身份。
        row = min(rows, key=lambda r: (-sum(bool(r.get(k)) for k in
                      ("trigger", "current_workaround", "desired_outcome", "artifact", "query_terms")), r["observation_id"]))
        refs = sorted({canonical_sha256(ref): ref for r in rows for ref in _references(r)}.values(),
                      key=lambda ref: (ref["evidence_id"], ref["revision_id"], ref["quote"]))
        observation_ids = sorted(r["observation_id"] for r in rows)
        for purpose, query in _proposals(row):
            language = _language(query, row.get("language", "unknown"))
            query_key = canonical_sha256({"query": normalize_identity(query), "language": language})[:24]
            if not query or query_key in seen or query_key in completed:
                skipped.append({"task_family_id": family, "purpose": purpose,
                                "reason": "already_completed" if query_key in completed else "duplicate_or_empty_query"})
                continue
            seen.add(query_key)
            tasks.append({"id": "followup-" + query_key, "query_key": query_key,
                          "task_id": family, "task_family_id": family, "purpose": purpose,
                          "query": query, "language": language, "task": row["task"],
                          "industry_ids": sorted({i for r in rows for i in r.get("industry_ids", [])}),
                          "source_observation_ids": observation_ids[:MAX_REFERENCES],
                          "source_evidence_refs": deepcopy(refs[:MAX_REFERENCES]),
                          "omitted_source_reference_count": max(0, len(refs) - MAX_REFERENCES),
                          "required_checks": list(CHECKS), "status": "host_verification_required",
                          "hypothesis_status": "query_not_market_evidence", "automatic_fetch": False})
    result = {"version": "1.0", "run_id": run_id, "as_of": as_of, "tasks": tasks,
              "skipped": sorted(skipped, key=canonical_sha256), "automatic_fetch": False,
              "limits": {"max_task_families": MAX_FAMILIES, "max_queries": MAX_TASKS},
              "summary": {"eligible_task_family_count": len(families), "followup_count": len(tasks),
                          "planned_task_family_count": len({task["task_family_id"] for task in tasks}),
                          "market_validated": False}}
    result["intent_plan"] = task_followup_intents(result)
    return result


def task_followup_intents(plan: dict) -> dict | None:
    """导出可直接交给 resume --intent-plan-file 的计划；没有新查询时不生成空意图文件。"""
    intents = []
    for task in plan["tasks"]:
        intents.append({"id": task["id"], "question": f"核验任务：{task['task']}；补查目的：{task['purpose']}",
            "evidence_type": "alternative" if task["purpose"] == "alternative" else "usage_behavior",
            "search_query": task["query"], "ranking_query": task["task"], "source": "web",
            "locale": {"country": "unknown", "language": task["language"]},
            "industry_ids": task["industry_ids"], "candidate_gaps": task["required_checks"],
            "task_family_id": task["task_family_id"], "source_observation_ids": task["source_observation_ids"],
            "source_evidence_refs": [{key: ref[key] for key in ("evidence_id", "revision_id")}
                                     for ref in task["source_evidence_refs"]],
            "purpose": task["purpose"], "query_key": task["query_key"], "required_checks": task["required_checks"]})
    return validate_intent_plan({"intents": intents}) if intents else None
