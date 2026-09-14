"""保留可追溯的探索线索，独立于 A/B/R 资格及个人立项结论。"""

from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile

from contracts import canonical_sha256, normalize_identity


def meaningful_ai_value(value) -> bool:
    """检查明确对比描述，不声称代码能证明 AI 优势或实际效果。"""
    if not isinstance(value, dict) or value.get("status") not in {"hypothesis", "supported"}:
        return False
    fields = ("baseline", "capability", "user_benefit", "incremental_advantage")
    placeholders = {"", "未知", "待验证", "ai", "ai赋能", "加聊天框", "提升效率", "unknown", "tbd"}
    return all(isinstance(value.get(k), str) and len(value[k].strip()) >= 6
               and normalize_identity(value[k]) not in placeholders for k in fields)


def exploration_lead(candidate: dict, reasons: list[str], *, require_ai: bool = False) -> dict | None:
    from filter_ideas import _linked_fact, _present

    evidence = [deepcopy(row) for row in candidate.get("evidence", []) if _linked_fact(row)]
    if not evidence or not _present(candidate.get("target_user")) or not _present(candidate.get("problem_or_desire")):
        return None
    ai_value = candidate.get("ai_value")
    if isinstance(ai_value, dict) and ai_value.get("status") == "disproven":
        return None
    if require_ai and not meaningful_ai_value(candidate.get("ai_value")):
        return None
    identity = {key: candidate.get(key) for key in ("target_user", "problem_or_desire", "wedge", "target_region")}
    row = deepcopy(candidate)
    row.pop("id", None)
    row.pop("evidence_tier", None)
    row.update(lead_id="LEAD-" + canonical_sha256(identity)[:12].upper(), evidence=evidence,
               research_status="needs_verification", missing_requirements=list(reasons), market_validated=False,
               next_question=candidate.get("next_question") or "核验缺失条件：" + "、".join(reasons),
               promotion_condition="补齐正式候选门槛与对应证据后重新过滤；不凭热度自动升级")
    return row


def normalize_leads(values, *, run_id: str, as_of: str) -> list[dict]:
    if not isinstance(values, list) or len(values) > 200:
        raise ValueError("leads 必须是最多 200 条的探索线索数组")
    result = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("探索线索必须是对象")
        row = exploration_lead(value, value.get("missing_requirements") or ["待核验收费市场与用户行为"], require_ai=True)
        if row is None:
            raise ValueError("探索线索需要目标用户、具体需求、链接原文事实以及明确的 AI 增量价值假设")
        row.update(run_id=run_id, as_of=as_of)
        result[row["lead_id"]] = row
    return list(result.values())


def lead_history(home: Path, *, as_of: str) -> list[dict]:
    """恢复截止研究日的最新观察；历史线索不冒充本轮发现或已验证市场。"""
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
    return sorted(latest.values(), key=lambda row: (row["as_of"], row["lead_id"]), reverse=True)


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
