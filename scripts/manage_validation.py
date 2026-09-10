#!/usr/bin/env python3
"""记录个人适配与验证实验；市场资格和实际客户验证分别管理。"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import tempfile
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contracts import ContractError, normalize_identity, validate_record_id, validate_run_as_of

DEFAULT_HOME = Path(os.environ.get("AI_OPPORTUNITY_RADAR_HOME", "~/Documents/AI-Opportunity-Radar")).expanduser()
COUNT_FIELDS = ("contacted", "interviewed", "real_tasks", "accepted_quotes", "paid_trials")


class ValidationError(ValueError):
    """个人约束或实验记录不符合契约。"""


def _number(value: Any, field: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是非负有限数字")
    if not math.isfinite(value) or value < 0 or (integer and not isinstance(value, int)):
        raise ValidationError(f"{field} 必须是非负{'整数' if integer else '有限数字'}")
    return value


def _strings(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{field} 必须是非空字符串组成的数组")
    return value


def validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValidationError("个人约束必须是 JSON 对象")
    if profile.get("schema_version", "3.0") != "3.0":
        raise ValidationError("个人约束仅支持 schema_version=3.0")
    for field in ("skills", "languages", "reachable_channels"):
        _strings(profile.get(field), field)
    for field in ("weekly_hours", "validation_budget_usd", "max_mvp_days"):
        if profile.get(field) is not None:
            _number(profile[field], field)
    return deepcopy(profile)


def assess_candidates(candidates: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    """仅判断下一步验证的资源适配，不替用户立项或宣称市场成立。"""
    profile = validate_profile(profile)
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise ValidationError("候选必须是对象数组")
    results = []
    for candidate in candidates:
        record_id = candidate.get("id") or candidate.get("candidate_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValidationError("候选缺少 id/candidate_id")
        plan = candidate.get("validation_plan") or {}
        if not isinstance(plan, dict):
            raise ValidationError("validation_plan 必须是对象")
        checks = []
        for requirement, available, label in (
            ("required_skills", "skills", "技能"),
            ("required_languages", "languages", "语言"),
        ):
            needed = _strings(plan.get(requirement), requirement)
            owned = _strings(profile.get(available), available)
            if needed == []:
                status, reason = "pass", "已明确没有额外要求"
            elif needed is None or owned is None:
                status, reason = "unknown", "尚未填写项目要求或个人能力"
            else:
                missing = {normalize_identity(item) for item in needed} - {normalize_identity(item) for item in owned}
                status = "conflict" if missing else "pass"
                reason = "缺少：" + "、".join(sorted(missing)) if missing else "符合已填写要求"
            checks.append({"check": label, "status": status, "reason": reason})
        channel = candidate.get("acquisition_channel")
        reachable = profile.get("reachable_channels")
        if not isinstance(channel, str) or not channel.strip() or reachable is None:
            channel_status, channel_reason = "unknown", "尚未确认具体获客入口"
        elif normalize_identity(channel) in {normalize_identity(item) for item in reachable}:
            channel_status, channel_reason = "pass", "入口在个人已确认可触达的渠道中"
        else:
            channel_status, channel_reason = "conflict", "该渠道尚不在个人可触达入口中"
        checks.append({"check": "渠道", "status": channel_status, "reason": channel_reason})
        for required, maximum, label, origin in (
            ("hours", "weekly_hours", "本周验证时间", plan),
            ("budget_usd", "validation_budget_usd", "验证预算 USD", plan),
            ("mvp_days", "max_mvp_days", "MVP 工期", candidate),
        ):
            value, cap = origin.get(required), profile.get(maximum)
            if value is not None:
                _number(value, required)
            if value is None or cap is None:
                status, reason = "unknown", "缺少估算或个人上限"
            else:
                status = "pass" if value <= cap else "conflict"
                reason = f"估算 {value}；上限 {cap}"
            checks.append({"check": label, "status": status, "reason": reason})
        for field in ("hypothesis", "success_criteria", "stop_criteria"):
            value = plan.get(field)
            checks.append({"check": field, "status": "pass" if isinstance(value, str) and value.strip() else "unknown",
                           "reason": "已填写" if isinstance(value, str) and value.strip() else "验证前需补齐"})
        conflicts = [item["check"] for item in checks if item["status"] == "conflict"]
        unknowns = [item["check"] for item in checks if item["status"] == "unknown"]
        action = "park" if conflicts else "clarify" if unknowns else "validate"
        results.append({
            "id": record_id, "title": candidate.get("title") or candidate.get("name"),
            "evidence_tier": candidate.get("evidence_tier"),
            "market_validated": False, "action": action,
            "checks": checks, "conflicts": conflicts, "unknowns": unknowns,
            "next_action": "先解决资源或渠道冲突" if conflicts else "补齐未知项后再安排实验" if unknowns else "执行小规模验证并记录真实行为",
        })
    ready = [item["id"] for item in results if item["action"] == "validate"]
    return {
        "schema_version": "3.0", "candidates": results,
        "primary_validation": ready[0] if ready else None, "alternatives": ready[1:3],
        "note": "适配只依据已填写约束；A/B/R 是研究层级，实验记录不自动改变它，也不等于立项批准。",
    }


def _validate_experiment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("实验必须是对象")
    item = deepcopy(value)
    if item.get("schema_version", "3.0") != "3.0":
        raise ValidationError("实验仅支持 schema_version=3.0")
    experiment_id = item.get("experiment_id")
    if not isinstance(experiment_id, str) or not re.fullmatch(r"EXP-[A-Za-z0-9-]{1,80}", experiment_id):
        raise ValidationError("experiment_id 必须以 EXP- 开头并使用字母、数字或连字符")
    try:
        validate_record_id(item.get("record_id"))
        validate_run_as_of(item.get("run_id"), item.get("as_of"))
    except ContractError as exc:
        raise ValidationError(str(exc)) from exc
    if item.get("status") not in {"planned", "running", "completed", "stopped"}:
        raise ValidationError("实验 status 必须为 planned/running/completed/stopped")
    for field in ("hypothesis", "offer", "success_criteria", "stop_criteria"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValidationError(f"实验缺少 {field}")
    counts = item.get("counts", {})
    if not isinstance(counts, dict) or set(counts) - set(COUNT_FIELDS):
        raise ValidationError("counts 包含未知指标或不是对象")
    for field, count in counts.items():
        _number(count, field, integer=True)
    for field in ("cost_usd", "minutes_spent"):
        if item.get(field) is not None:
            _number(item[field], field)
    evidence = item.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(row, dict) for row in evidence):
        raise ValidationError("evidence 必须是对象数组")
    for row in evidence:
        url = row.get("url")
        valid_url = False
        if isinstance(url, str):
            try:
                parsed = urlsplit(url)
                valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None
            except ValueError:
                pass
        local_ref = row.get("local_ref")
        traceable = valid_url or (isinstance(local_ref, str) and bool(local_ref.strip()))
        fact = row.get("original_text") or row.get("fact")
        if not traceable or not isinstance(fact, str) or not fact.strip():
            raise ValidationError("实验 evidence 必须有可追溯的 url/local_ref 和 original_text/fact")
    if item["status"] in {"completed", "stopped"}:
        for field in ("outcome", "next_action"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValidationError(f"结束实验必须填写 {field}")
        if item.get("decision") not in {"continue", "pause", "abandon"}:
            raise ValidationError("结束实验必须明确 continue/pause/abandon 决策")
        if any(counts.values()) and not evidence:
            raise ValidationError("有客户行为的结束实验必须保留 evidence")
    item["schema_version"] = "3.0"
    return item


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return [_validate_experiment(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValidationError(f"实验历史无法读取：{exc}") from exc


def _atomic_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def record_experiment(home: Path, value: dict[str, Any]) -> dict[str, str]:
    """同实验同运行严格幂等；修改实验必须使用新的运行标识。"""
    item = _validate_experiment(value)
    state = home.expanduser().resolve() / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "experiment-events.jsonl"
    with (state / ".experiments.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        events = _read_events(path)
        key = (item["experiment_id"], item["run_id"])
        existing = next((event for event in events if (event["experiment_id"], event["run_id"]) == key), None)
        if existing is not None:
            if existing != item:
                raise ValidationError("同实验同 run_id 的内容冲突，请使用新运行记录变更")
            status = "replayed"
        else:
            _atomic_jsonl(path, [*events, item])
            status = "created"
    return {"status": status, "experiment_id": item["experiment_id"], "record_id": item["record_id"]}


def list_experiments(home: Path, *, record_id: str | None = None, as_of: str | None = None) -> list[dict[str, Any]]:
    if as_of is not None:
        try:
            date.fromisoformat(as_of)
        except ValueError as exc:
            raise ValidationError("as_of 必须是有效日期") from exc
    if record_id is not None:
        try:
            validate_record_id(record_id)
        except ContractError as exc:
            raise ValidationError(str(exc)) from exc
    events = _read_events(home.expanduser().resolve() / "state" / "experiment-events.jsonl")
    return [event for event in events if (record_id is None or event["record_id"] == record_id)
            and (as_of is None or event["as_of"] <= as_of)]


def _read_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValidationError(f"JSON 不允许非有限数字：{value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _candidates(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValidationError("候选输入必须是对象或数组")
    if isinstance(payload.get("candidates"), list):
        return payload["candidates"]
    keys = ("deep_candidates", "validated_ideas", "regional_signals")
    if any(key in payload for key in keys):
        rows = []
        overflow = payload.get("overflow") or {}
        if not isinstance(overflow, dict):
            raise ValidationError("overflow 必须是对象")
        for key in keys:
            for container in (payload, overflow):
                values = container.get(key, [])
                if not isinstance(values, list):
                    raise ValidationError(f"{key} 必须是数组")
                rows.extend(values)
        return rows
    return [payload]


def main() -> int:
    parser = argparse.ArgumentParser(description="个人适配和真实验证实验记录")
    sub = parser.add_subparsers(dest="command", required=True)
    assess = sub.add_parser("assess", help="比较个人约束和候选的验证资源要求")
    assess.add_argument("--profile", type=Path, required=True)
    assess.add_argument("--input", type=Path, required=True)
    record = sub.add_parser("record-experiment", help="记录已执行或计划中的实验；不会联系客户")
    record.add_argument("--home", type=Path, default=DEFAULT_HOME)
    record.add_argument("--input", type=Path, required=True)
    listing = sub.add_parser("experiments", help="查看逐次实验记录，不合计累计快照")
    listing.add_argument("--home", type=Path, default=DEFAULT_HOME)
    listing.add_argument("--record-id")
    listing.add_argument("--as-of")
    for command in (assess, record, listing):
        command.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "assess":
            result = assess_candidates(_candidates(_read_json(args.input)), _read_json(args.profile))
        elif args.command == "record-experiment":
            result = record_experiment(args.home, _read_json(args.input))
        else:
            result = {"events": list_experiments(args.home, record_id=args.record_id, as_of=args.as_of)}
        output = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
