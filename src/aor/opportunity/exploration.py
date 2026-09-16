"""保留可追溯的探索线索，独立于 A/B/R 资格及个人立项结论。"""

from copy import deepcopy
from datetime import date
import fcntl
import json
import os
import re
from pathlib import Path
import tempfile

from contracts import canonical_sha256, normalize_identity

LEAD_VERSION = "2.0"
LEAD_ID = re.compile(r"LEAD-[A-F0-9]{12,32}")
LEAD_STATES = {"needs_verification", "observed_need", "archived", "disproven", "promoted", "needs_review"}


def validate_lead_fields(candidate: dict, *, allowed_industries: set[str] | None = None) -> None:
    """校验展示必需字段和明确范围；不推断行业或补写商业事实。"""
    from aor.sources.industries import load_industries

    for field in ("title", "target_user", "problem_or_desire", "wedge"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise ValueError(f"探索线索缺少 {field}")
    ids = candidate.get("industry_ids")
    known = allowed_industries if allowed_industries is not None else {r["id"] for r in load_industries()}
    if not isinstance(ids, list) or not ids or any(not isinstance(v, str) for v in ids) or not set(ids) <= known:
        raise ValueError("探索线索行业不在当前允许目录中")
    if len(ids) != len(set(ids)):
        raise ValueError("探索线索行业不能重复")
    if candidate.get("lead_id") and not LEAD_ID.fullmatch(str(candidate["lead_id"])):
        raise ValueError("探索线索 lead_id 格式错误")


def lead_revision(row: dict) -> str:
    ignored = {"run_id", "as_of", "revision", "lead_version", "revision_id", "previous_revision_id", "publication_review",
               "reference_status", "reference_issues", "stored_research_status", "review_reason"}
    return canonical_sha256({k: v for k, v in row.items() if k not in ignored})[:16]


def meaningful_ai_value(value) -> bool:
    """检查明确对比描述，不声称代码能证明 AI 优势或实际效果。"""
    if not isinstance(value, dict) or value.get("status") not in {"hypothesis", "supported"}:
        return False
    fields = ("baseline", "capability", "user_benefit", "incremental_advantage")
    placeholders = {"", "未知", "待验证", "ai", "ai赋能", "加聊天框", "提升效率", "unknown", "tbd", "这是需要验证的事情"}
    descriptions = [normalize_identity(value.get(k)) for k in fields]
    if (len(set(descriptions)) < len(fields) or not all(isinstance(value.get(k), str) and len(value[k].strip()) >= 6
            and descriptions[i] not in placeholders for i, k in enumerate(fields))):
        return False
    if value["status"] == "supported":
        review = value.get("review") or {}
        refs = value.get("evidence_refs")
        return bool(review.get("reviewer") and review.get("reviewed_at") and review.get("rationale")
                    and isinstance(refs, list) and refs and all(isinstance(r, dict) and r.get("evidence_id")
                    and r.get("revision_id") and r.get("quote") for r in refs))
    return True


def exploration_lead(candidate: dict, reasons: list[str], *, require_ai: bool = False,
                     strict: bool = False, allowed_industries: set[str] | None = None) -> dict | None:
    from filter_ideas import _linked_fact, _present

    evidence = [deepcopy(row) for row in candidate.get("evidence", []) if _linked_fact(row)]
    if not evidence or not _present(candidate.get("target_user")) or not _present(candidate.get("problem_or_desire")):
        return None
    ai_value = candidate.get("ai_value")
    if isinstance(ai_value, dict) and ai_value.get("status") == "disproven":
        return None
    if require_ai and not meaningful_ai_value(candidate.get("ai_value")):
        return None
    if strict:
        validate_lead_fields(candidate, allowed_industries=allowed_industries)
    identity = {key: normalize_identity(candidate.get(key)) for key in ("target_user", "problem_or_desire", "wedge", "target_region")}
    identifier = candidate.get("lead_id") or "LEAD-" + canonical_sha256(identity)[:12].upper()
    if not LEAD_ID.fullmatch(str(identifier)):
        raise ValueError("探索线索 lead_id 格式错误")
    row = deepcopy(candidate)
    row.pop("id", None)
    row.pop("evidence_tier", None)
    state = candidate.get("research_status", "needs_verification")
    if state not in LEAD_STATES:
        raise ValueError("未知的探索线索状态")
    if strict and state == "observed_need":
        from aor.sources.coverage import BEHAVIOR_ROLES, _review_status
        if not any(_review_status(e, str(candidate.get("as_of") or "0001-01-01")) == "relevant"
                   and e.get("evidence_role") in BEHAVIOR_ROLES for e in evidence):
            raise ValueError("观察到需求必须引用已复核的用户行为")
    if strict and state == "promoted" and not re.fullmatch(r"(?:OPP|SIG)-[A-Za-z0-9-]+", str(candidate.get("promoted_to", ""))):
        raise ValueError("已升级线索缺少正式记录关联")
    row.update(lead_id=identifier, lead_version=LEAD_VERSION, evidence=evidence,
               research_status=state, missing_requirements=list(reasons), market_validated=False,
               next_question=candidate.get("next_question") or "核验缺失条件：" + "、".join(reasons),
               promotion_condition="补齐正式候选门槛与对应证据后重新过滤；不凭热度自动升级")
    row["revision_id"] = identifier + ":" + lead_revision(row)
    return row


def normalize_leads(values, *, run_id: str, as_of: str, allowed_industries: set[str] | None = None) -> list[dict]:
    if not isinstance(values, list) or len(values) > 200:
        raise ValueError("leads 必须是最多 200 条的探索线索数组")
    result = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("探索线索必须是对象")
        value = {**value, "run_id": run_id, "as_of": as_of}
        row = exploration_lead(value, value.get("missing_requirements") or ["待核验收费市场与用户行为"],
                               require_ai=True, strict=True, allowed_industries=allowed_industries)
        if row is None:
            raise ValueError("探索线索需要目标用户、具体需求、链接原文事实以及明确的 AI 增量价值假设")
        row.update(run_id=run_id, as_of=as_of)
        if row["lead_id"] in result and result[row["lead_id"]] != row:
            raise ValueError("同一批探索线索 ID 对应不同内容")
        result[row["lead_id"]] = row
    return list(result.values())


def current_reference_issue(evidence_id: str, revision_id: str, states: dict) -> str | None:
    """线索及网站共用当前状态判别；精确历史引用的可解析性不代表它仍可支持当前结论。"""
    state = states.get(evidence_id)
    if not state:
        return "引用缺少当前证据对象状态"
    if state.get("retracted") or state.get("status") in {"retracted", "withdrawn"}:
        return "引用对象当前已撤回，不能使用旧修订继续发布"
    if state.get("status") in {"ambiguous", "superseded", "derivation_superseded", "needs_review", "deleted", "removed"}:
        return "引用对象的当前状态不可确定，需要重新复核"
    if state.get("current_revision_id") != revision_id:
        return "引用对象的当前正文已变化或版本不唯一，需要重新复核"
    return None


def current_lead_evidence(home: Path, *, as_of: str) -> list[dict]:
    from aor.storage.evidence_library import EvidenceLibrary
    as_of = date.fromisoformat(as_of).isoformat()
    library = EvidenceLibrary(Path(home) / "evidence-library")
    return library.search("", as_of=as_of, limit=None, include_retracted=True, include_superseded=True) if library.journal_path.exists() else []


def lead_history(home: Path, *, as_of: str, evidence: list[dict] | None = None) -> list[dict]:
    """恢复截止研究日的最新观察；历史线索不冒充本轮发现或已验证市场。"""
    as_of = date.fromisoformat(as_of).isoformat()
    path = Path(home) / "state/research-leads.jsonl"
    latest = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        if not line.strip():
            continue
        row = json.loads(line)
        if not row.get("as_of") or row["as_of"] > as_of:
            continue
        previous = latest.get(row["lead_id"])
        if previous is None or row["as_of"] >= previous["as_of"]:
            latest[row["lead_id"]] = row
    rows = list(latest.values())
    if evidence is None and (Path(home) / "evidence-library/evidence.jsonl").exists():
        evidence = current_lead_evidence(home, as_of=as_of)
    if evidence is not None:
        from aor.evidence.retrieval import resolve_evidence_reference
        from aor.reporting.report import evidence_current_state
        states = evidence_current_state(evidence)
        for row in rows:
            issues, bound = [], []
            for ref in [*row.get("evidence", []), *(row.get("ai_value") or {}).get("evidence_refs", [])]:
                try:
                    stored = resolve_evidence_reference(ref, evidence, require_revision=True)
                    issue = current_reference_issue(stored["evidence_id"], stored["revision_id"], states)
                    if issue:
                        raise ValueError(issue)
                    if ref in row.get("evidence", []):
                        bound.append(stored)
                except ValueError as exc:
                    issues.append({"evidence_id": ref.get("evidence_id"), "revision_id": ref.get("revision_id"), "reason": str(exc)})
            if row.get("research_status") == "observed_need" and not issues:
                from aor.sources.coverage import BEHAVIOR_ROLES, _review_status
                if not any(_review_status(record, as_of) == "relevant" and record.get("evidence_role") in BEHAVIOR_ROLES
                           for record in bound):
                    issues.append({"reason": "观察到需求的状态缺少当前已复核的用户行为"})
            if issues:
                row["reference_status"], row["reference_issues"] = "needs_review", issues
                row["review_reason"] = "原始引用已变化、撤回或无法唯一解析；历史观察保留，需重新核验"
                if row.get("research_status") not in {"archived", "disproven", "promoted"}:
                    row["stored_research_status"] = row.get("research_status")
                    row["research_status"] = "needs_review"
    return sorted(rows, key=lambda row: (row["as_of"], row["lead_id"]), reverse=True)


def transition_lead(home: Path, lead_id: str, *, status: str, reason: str, run_id: str,
                    as_of: str, promoted_record: dict | None = None) -> dict:
    """追加生命周期修订；升级要求已存在且仍合格的正式记录。"""
    from contracts import validate_run_as_of
    validate_run_as_of(run_id, as_of)
    if status not in LEAD_STATES or not isinstance(reason, str) or not reason.strip():
        raise ValueError("线索状态变更需要有效状态与理由")
    evidence = current_lead_evidence(home, as_of=as_of)
    row = next((r for r in lead_history(home, as_of=as_of, evidence=evidence) if r["lead_id"] == lead_id), None)
    if row is None:
        raise ValueError("找不到要修订的探索线索")
    if status in {"needs_verification", "observed_need", "promoted"} and row.get("reference_status") == "needs_review":
        raise ValueError("引用状态已失效，请先修订并重新核验引用；不能仅变更线索状态恢复")
    if status == "promoted":
        from filter_ideas import classify_candidate
        if not promoted_record or not promoted_record.get("id") or classify_candidate(promoted_record)[0] not in {"A", "B", "R"}:
            raise ValueError("升级必须关联已通过正式门槛的记录")
        row["promoted_to"] = promoted_record["id"]
    elif status == "observed_need":
        from aor.sources.coverage import BEHAVIOR_ROLES, _review_status
        from aor.evidence.retrieval import resolve_evidence_reference
        if not any(_review_status(e, as_of) == "relevant"
                   and e.get("evidence_role") in BEHAVIOR_ROLES
                   for e in [resolve_evidence_reference(ref, evidence, require_revision=True) for ref in row.get("evidence", [])]):
            raise ValueError("观察到需求需要已复核的用户行为原文")
    for key in ("reference_status", "reference_issues", "stored_research_status", "review_reason"):
        row.pop(key, None)
    row.update(research_status=status, transition_reason=reason, as_of=as_of, run_id=run_id,
               previous_revision_id=row.get("revision_id"))
    row["revision_id"] = lead_id + ":" + lead_revision(row)
    record_leads(home, [row], run_id=run_id)
    return row


def record_leads(home: Path, leads: list[dict], *, run_id: str) -> None:
    """按运行与稳定线索 ID 幂等追加观察；原子替换避免半行记录。"""
    path = Path(home) / "state/research-leads.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        known = {(r["run_id"], r["lead_id"]): r for r in rows}
        for lead in leads:
            row = {**lead, "run_id": run_id}
            key = (run_id, row["lead_id"])
            if key in known and canonical_sha256(known[key]) != canonical_sha256(row):
                raise ValueError("同一运行的探索线索已存在不同内容，请使用新运行修订")
            if key not in known:
                rows.append(row)
                known[key] = row
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
