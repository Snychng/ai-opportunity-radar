"""保留创意池，只根据显式、可追溯的依据选择下一轮研究对象。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import math
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from aor.evidence.claims import _experiment_visible
from aor.opportunity.basis import OpportunityError, validate_basis


AXIS_FIELDS = {
    "segments": "target_user", "triggers": "context", "forms": "wedge",
    "regions": "target_region", "channels": "acquisition_channel", "offers": "delivery_model",
}
_LOCAL_TZ = timezone(timedelta(hours=8))


def _text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _observed(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpportunityError(f"无效实验日期：{value}") from exc
    return parsed.replace(tzinfo=_LOCAL_TZ) if parsed.tzinfo is None else parsed


def _traceable_experiment_evidence(row: dict[str, Any]) -> bool:
    fact = row.get("original_text") or row.get("fact")
    if not isinstance(fact, str) or not fact.strip():
        return False
    local_ref = row.get("local_ref")
    if isinstance(local_ref, str) and local_ref.strip():
        return True
    try:
        parsed = urlsplit(row.get("url") or "")
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None
    except (TypeError, ValueError, AttributeError):
        return False


def build_experiment_context(experiments: Any, *, as_of: str | None = None) -> dict[str, Any]:
    """整理已有实验日志的截止日前最近快照，不合计人数、不评估实验等级。"""
    if not isinstance(experiments, list) or not all(isinstance(item, dict) for item in experiments):
        raise OpportunityError("experiment_results 必须是对象数组")
    try:
        cutoff = datetime.combine(date.fromisoformat(as_of), time.max, _LOCAL_TZ) if as_of else None
    except ValueError as exc:
        raise OpportunityError(f"无效 as_of：{as_of}") from exc
    latest: dict[str, tuple[datetime, datetime, dict[str, Any]]] = {}
    excluded_count = 0
    for item in experiments:
        if not all(isinstance(item.get(field), str) and item[field].strip()
                   for field in ("experiment_id", "run_id", "record_id", "as_of")):
            raise OpportunityError("实验背景缺少 experiment_id/run_id/record_id/as_of")
        if item.get("status") not in {"planned", "running", "completed", "stopped"}:
            raise OpportunityError("实验背景 status 无效")
        event_date = _observed(item["as_of"])
        observed = _observed(item.get("observed_at") or item["as_of"])
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(row, dict) for row in evidence):
            raise OpportunityError("实验背景 evidence 必须是对象数组")
        if cutoff and _experiment_visible(item, cutoff):
            excluded_count += 1
            continue
        traceable = [row for row in evidence if _traceable_experiment_evidence(row)]
        value = deepcopy(item)
        value["usable_for_priority"] = (
            item["status"] in {"completed", "stopped"}
            and isinstance(item.get("outcome"), str) and bool(item["outcome"].strip()) and bool(traceable)
        )
        key = item["experiment_id"]
        prior = latest.get(key)
        if prior is None or (event_date, observed) > prior[:2]:
            latest[key] = (event_date, observed, value)
        elif (event_date, observed) == prior[:2] and prior[2] != value:
            raise OpportunityError(f"实验 {key} 同时点存在冲突快照")
    return {
        "as_of": as_of, "experiments": [entry[2] for entry in latest.values()],
        "excluded_future_count": excluded_count, "snapshot_policy": "latest_per_experiment",
        "affects_evidence_tier": False, "semantic_validation": "not_performed",
    }


def select_candidates(
    candidates: list[dict[str, Any]], *, strategy: Any = None, axis_priorities: Any = None,
    evidence: list[dict[str, Any]] | None = None, experiment_results: Any = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """在完整候选池上稳定排序，只返回选择元数据，不改候选、分层或分数。"""
    if strategy is None:
        strategy = {}
    if not isinstance(strategy, dict):
        raise OpportunityError("selection_strategy 必须是对象")
    mode = strategy.get("mode", "all")
    if mode not in {"all", "evidence_priority"}:
        raise OpportunityError("selection_strategy.mode 必须为 all/evidence_priority")
    limit = strategy.get("limit", len(candidates))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise OpportunityError("selection_strategy.limit 必须是非负整数")
    priorities = axis_priorities if axis_priorities is not None else []
    if not isinstance(priorities, list):
        raise OpportunityError("axis_priorities 必须是数组")
    context = build_experiment_context(experiment_results if experiment_results is not None else [], as_of=as_of)
    experiments = {item["experiment_id"]: item for item in context["experiments"]}
    validated = []
    seen = set()
    for index, entry in enumerate(priorities):
        if not isinstance(entry, dict) or entry.get("axis") not in AXIS_FIELDS:
            raise OpportunityError("axis_priorities 每项必须包含合法 axis")
        value = entry.get("value")
        if not isinstance(value, str) or not value.strip():
            raise OpportunityError("axis_priorities.value 必须是非空字符串")
        marker = (entry["axis"], _text(value))
        if marker in seen:
            raise OpportunityError("同一轴值不能重复指定优先级")
        seen.add(marker)
        priority = entry.get("priority", 1)
        if (isinstance(priority, bool) or not isinstance(priority, (int, float))
                or not math.isfinite(priority) or not -10 <= priority <= 10):
            raise OpportunityError("priority 必须是 -10 到 10 的有限数字")
        basis = validate_basis(entry, evidence or [], basis_id=f"axis-{index}", as_of=as_of)
        experiment_refs = entry.get("experiment_refs", [])
        if not isinstance(experiment_refs, list):
            raise OpportunityError("experiment_refs 必须是对象数组")
        usable_experiments = []
        for reference in experiment_refs:
            if not isinstance(reference, dict):
                raise OpportunityError("experiment_refs 每项必须是对象")
            experiment = experiments.get(reference.get("experiment_id"))
            if experiment is None or reference.get("run_id") != experiment["run_id"]:
                raise OpportunityError("experiment_refs 必须指向截止日前可用的实验快照")
            if experiment["usable_for_priority"]:
                usable_experiments.append(reference)
        missing = [field for field in basis["missing"] if field != "evidence_refs"]
        if not basis["evidence_refs"] and not usable_experiments:
            missing.append("evidence_refs_or_completed_experiment")
        validated.append({
            "axis": entry["axis"], "value": value.strip(), "priority": priority,
            **basis, "experiment_refs": deepcopy(experiment_refs), "missing": missing,
            "effective_priority": priority if not missing else 0,
        })
    ranking = []
    for candidate in candidates:
        matched = [index for index, entry in enumerate(validated)
                   if _text(candidate.get(AXIS_FIELDS[entry["axis"]])) == _text(entry["value"])]
        ranking.append({
            "candidate_id": candidate["candidate_id"],
            "priority": sum(validated[index]["effective_priority"] for index in matched),
            "axis_priority_indexes": matched,
            "missing_basis": [index for index in matched if validated[index]["missing"]],
        })
    if mode == "evidence_priority":
        ranking.sort(key=lambda item: -item["priority"])
    return {
        "mode": mode, "limit": limit, "pool_count": len(candidates),
        "selected_candidate_ids": [item["candidate_id"] for item in ranking[:limit]],
        "ranking": ranking, "axis_priorities": validated, "experiment_context": context,
        "unmatched_priority_indexes": [index for index in range(len(validated))
                                       if not any(index in row["axis_priority_indexes"] for row in ranking)],
        "pool_preserved": True, "affects_evidence_tier": False, "affects_score": False,
        "semantic_validation": "not_performed",
    }
