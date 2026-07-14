#!/usr/bin/env python3
"""维护机会雷达 V2 当前视图与追加式观察历史。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from contracts import (
    SCHEMA_VERSION,
    ContractError,
    beijing_today,
    canonical_sha256,
    evidence_independent_sources,
    fingerprint_record as contract_fingerprint_record,
    make_run_id,
    stable_record_id,
    validate_record_id,
    validate_run_as_of,
)


DEFAULT_HOME = Path(os.environ.get("AI_OPPORTUNITY_RADAR_HOME", "~/Documents/AI-Opportunity-Radar")).expanduser()
KINDS = {"opportunity": "opportunities.jsonl", "signal": "signals.jsonl"}
OBSERVATION_FILES = {
    "opportunity": "opportunity-observations.jsonl",
    "signal": "signal-observations.jsonl",
}
SOURCE_STATUSES = {"ok", "no-results", "auth-required", "rate-limited", "blocked", "skipped-policy", "error"}

DEFAULT_PREFERENCES: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "platform_phase": "phase_1_existing_platforms",
    "platform_expansion_enabled": False,
    "timezone": "Asia/Shanghai",
    "markets": ["global", "china", "southeast_asia", "south_asia", "africa", "middle_east", "latin_america"],
    "audiences": ["small_business", "consumer"],
    "mvp_days_max": 30,
    "deep_opportunities_per_day": [3, 5],
    "watchlist_max": 20,
    "allowed_sensitive_domains": [
        "dating_relationship_companionship",
        "adult_content",
        "gaming_virtual_character_social_entertainment",
    ],
    "delivery_formats": [
        "web_saas",
        "mobile_app",
        "browser_extension",
        "messaging_bot",
        "paid_content",
        "developer_tool",
        "marketplace",
    ],
    "preserve_original_quote": True,
    "translate_to_chinese": True,
}


class StateError(ValueError):
    """状态文件损坏或输入不符合契约。"""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


@contextmanager
def _exclusive_lock(home: Path) -> Iterator[None]:
    """序列化当前视图、观察历史和来源状态更新。"""
    lock_path = home / "state" / ".radar.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def initialize_home(home: Path = DEFAULT_HOME) -> Path:
    """创建稳定目录、当前视图和追加式历史文件。"""
    home = home.expanduser().resolve()
    for relative in ("reports/daily", "reports/deep-dives", "state", "raw", "config"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    preferences = home / "config" / "preferences.json"
    if not preferences.exists():
        _atomic_write_text(preferences, json.dumps(DEFAULT_PREFERENCES, ensure_ascii=False, indent=2) + "\n")
    for filename in [*KINDS.values(), *OBSERVATION_FILES.values(), "source-health-events.jsonl"]:
        path = home / "state" / filename
        if not path.exists():
            _atomic_write_text(path, "")
    source_health = home / "state" / "source-health.json"
    if not source_health.exists():
        _atomic_write_text(
            source_health,
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "updated_at": None, "sources": {}},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    return home


def fingerprint_record(record: dict[str, Any]) -> str:
    try:
        return contract_fingerprint_record(record)
    except ContractError as exc:
        raise StateError(str(exc)) from exc


def _state_path(home: Path, kind: str) -> Path:
    if kind not in KINDS:
        raise StateError(f"未知记录类型：{kind}")
    return home / "state" / KINDS[kind]


def _observation_path(home: Path, kind: str) -> Path:
    if kind not in OBSERVATION_FILES:
        raise StateError(f"未知记录类型：{kind}")
    return home / "state" / OBSERVATION_FILES[kind]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StateError(f"状态文件 {path} 第 {line_number} 行损坏：{exc}") from exc
        if not isinstance(value, dict):
            raise StateError(f"状态文件 {path} 第 {line_number} 行不是对象")
        records.append(value)
    return records


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]], *, sort_current: bool = False) -> None:
    rows = list(records)
    if sort_current:
        rows.sort(key=lambda item: (item.get("first_seen", ""), item.get("id", "")))
    payload = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in rows)
    _atomic_write_text(path, payload)


def load_records(home: Path, kind: str) -> list[dict[str, Any]]:
    return _load_jsonl(_state_path(home, kind))


def load_observations(home: Path, kind: str) -> list[dict[str, Any]]:
    return _load_jsonl(_observation_path(home, kind))


def _merge_evidence(old: Any, new: Any) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for item in [*(old if isinstance(old, list) else []), *(new if isinstance(new, list) else [])]:
        if isinstance(item, dict):
            marker = str(item.get("url") or item.get("id") or canonical_sha256(item))
        else:
            marker = canonical_sha256(item)
        if marker not in seen:
            seen.add(marker)
            merged.append(item)
    return merged


def _expected_id(kind: str, record: dict[str, Any], observed_on: date, existing: dict[str, Any] | None) -> str:
    fingerprint = fingerprint_record(record)
    try:
        if existing is not None:
            expected = validate_record_id(existing.get("id"), kind=kind)
        else:
            expected = stable_record_id(kind, observed_on, fingerprint)
        supplied = record.get("id")
        if supplied is not None and validate_record_id(supplied, kind=kind) != expected:
            raise StateError(f"记录 ID {supplied} 与历史解析结果 {expected} 不一致")
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    return expected


def resolve_record_ids(home: Path, kind: str, records: list[dict[str, Any]], observed_on: date) -> list[dict[str, Any]]:
    """在写报告前根据历史状态分配或复用稳定 ID，不提交观察事件。"""
    if not isinstance(records, list) or not records:
        raise StateError("待解析记录必须是非空数组")
    home = initialize_home(home)
    with _exclusive_lock(home):
        current = load_records(home, kind)
        by_fingerprint = {item.get("fingerprint"): item for item in current if item.get("fingerprint")}
        prepared: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise StateError("每条待解析记录必须是对象")
            fingerprint = fingerprint_record(record)
            if fingerprint in seen_fingerprints:
                raise StateError(f"输入批次包含重复机会指纹：{fingerprint}")
            seen_fingerprints.add(fingerprint)
            existing = by_fingerprint.get(fingerprint)
            value = deepcopy(record)
            value["id"] = _expected_id(kind, value, observed_on, existing)
            value["fingerprint"] = fingerprint
            prepared.append(value)
        return prepared


def _observation_event(
    *,
    kind: str,
    run_id: str,
    observed_on: date,
    record: dict[str, Any],
) -> dict[str, Any]:
    evidence = record.get("evidence") if isinstance(record.get("evidence"), list) else []
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": hashlib.sha256(f"{run_id}|{kind}|{record['fingerprint']}".encode("utf-8")).hexdigest()[:24],
        "run_id": run_id,
        "kind": kind,
        "observed_on": observed_on.isoformat(),
        "id": record["id"],
        "fingerprint": record["fingerprint"],
        "title": record.get("title") or record.get("name"),
        "total_score": record.get("total_score"),
        "scoring_version": record.get("scoring_version"),
        "recommendation": record.get("recommendation"),
        "evidence_count": len(evidence),
        "independent_source_count": len(evidence_independent_sources(evidence)),
        "snapshot": deepcopy(record),
    }


def upsert_records(
    home: Path,
    kind: str,
    records: list[dict[str, Any]],
    observed_on: date,
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    """批量提交当前视图和观察历史；同一 run_id 重放不重复计数。"""
    try:
        validate_run_as_of(run_id, observed_on.isoformat())
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    home = initialize_home(home)
    with _exclusive_lock(home):
        current = load_records(home, kind)
        observations = load_observations(home, kind)
        event_ids = {item.get("event_id") for item in observations}
        by_fingerprint = {item.get("fingerprint"): item for item in current if item.get("fingerprint")}
        results: list[dict[str, Any]] = []
        seen_batch: set[str] = set()
        for input_record in records:
            if not isinstance(input_record, dict):
                raise StateError("记录必须是 JSON 对象")
            fingerprint = fingerprint_record(input_record)
            if fingerprint in seen_batch:
                raise StateError(f"输入批次包含重复机会指纹：{fingerprint}")
            seen_batch.add(fingerprint)
            existing = by_fingerprint.get(fingerprint)
            record_id = _expected_id(kind, input_record, observed_on, existing)
            base = deepcopy(existing) if existing is not None else {}
            merged = deepcopy(base)
            merged.update(deepcopy(input_record))
            merged.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": record_id,
                    "fingerprint": fingerprint,
                    "first_seen": base.get("first_seen", observed_on.isoformat()),
                    "last_seen": max(base.get("last_seen", observed_on.isoformat()), observed_on.isoformat()),
                    "last_run_id": run_id,
                }
            )
            merged["seen_dates"] = sorted(set([*base.get("seen_dates", []), observed_on.isoformat()]))
            merged["occurrences"] = len(merged["seen_dates"])
            merged["evidence"] = _merge_evidence(base.get("evidence"), input_record.get("evidence"))
            event = _observation_event(kind=kind, run_id=run_id, observed_on=observed_on, record=merged)
            if event["event_id"] in event_ids:
                results.append({"status": "replayed", "id": record_id, "fingerprint": fingerprint})
                continue
            event_ids.add(event["event_id"])
            observations.append(event)
            if existing is None:
                current.append(merged)
                status = "created"
            else:
                current[current.index(existing)] = merged
                status = "updated"
            by_fingerprint[fingerprint] = merged
            results.append({"status": status, "id": record_id, "fingerprint": fingerprint})
        _write_jsonl(_observation_path(home, kind), observations)
        _write_jsonl(_state_path(home, kind), current, sort_current=True)
        return results


def upsert_record(
    home: Path,
    kind: str,
    record: dict[str, Any],
    observed_on: date,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise StateError("记录必须是 JSON 对象")
    resolved_run_id = run_id or make_run_id(
        as_of=observed_on,
        mode="manual-record",
        focus=canonical_sha256(record),
    )
    return upsert_records(home, kind, [record], observed_on, run_id=resolved_run_id)[0]


def history(home: Path, kind: str, as_of: date, days: int) -> list[dict[str, Any]]:
    """返回窗口内当前记录及逐次评分和证据观察历史。"""
    if days <= 0:
        raise StateError("days 必须大于 0")
    home = initialize_home(home)
    threshold = as_of - timedelta(days=days - 1)
    with _exclusive_lock(home):
        current = load_records(home, kind)
        observations = load_observations(home, kind)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in observations:
        try:
            observed_on = datetime.strptime(event["observed_on"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError) as exc:
            raise StateError(f"观察事件 {event.get('event_id', '<unknown>')} 日期无效") from exc
        if threshold <= observed_on <= as_of:
            grouped.setdefault(str(event.get("id")), []).append(event)
    result: list[dict[str, Any]] = []
    for record in current:
        try:
            last_seen = datetime.strptime(record["last_seen"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError) as exc:
            raise StateError(f"记录 {record.get('id', '<unknown>')} 的 last_seen 无效") from exc
        if threshold <= last_seen <= as_of:
            value = deepcopy(record)
            value["observation_history"] = sorted(
                grouped.get(str(record.get("id")), []),
                key=lambda item: (item.get("observed_on", ""), item.get("run_id", "")),
            )
            result.append(value)
    return sorted(result, key=lambda item: (item["last_seen"], item.get("id", "")), reverse=True)


def find_record(home: Path, kind: str, record_id: str) -> dict[str, Any]:
    try:
        validate_record_id(record_id, kind=kind)
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    home = initialize_home(home)
    with _exclusive_lock(home):
        for record in load_records(home, kind):
            if record.get("id") == record_id:
                return record
    raise StateError(f"未找到记录：{record_id}")


def _redact_detail(detail: str) -> str:
    redacted = str(detail)
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", redacted)
    return redacted[:1000]


def update_source_health(
    home: Path,
    source: str,
    status: str,
    detail: str,
    observed_on: date,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """更新来源当前状态，同时按 run_id 保存观察事件。"""
    if status not in SOURCE_STATUSES:
        raise StateError(f"无效来源状态：{status}")
    home = initialize_home(home)
    resolved_run_id = run_id or make_run_id(as_of=observed_on, mode="source-health", focus=source)
    try:
        validate_run_as_of(resolved_run_id, observed_on.isoformat())
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    event_id = hashlib.sha256(f"{resolved_run_id}|source-health|{source}".encode("utf-8")).hexdigest()[:24]
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "run_id": resolved_run_id,
        "source": source,
        "status": status,
        "detail": _redact_detail(detail),
        "observed_on": observed_on.isoformat(),
    }
    with _exclusive_lock(home):
        path = home / "state" / "source-health.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = SCHEMA_VERSION
        data["updated_at"] = observed_on.isoformat()
        data.setdefault("sources", {})[source] = {key: value for key, value in event.items() if key != "event_id"}
        events_path = home / "state" / "source-health-events.jsonl"
        events = _load_jsonl(events_path)
        existing_index = next((index for index, item in enumerate(events) if item.get("event_id") == event_id), None)
        if existing_index is None:
            events.append(event)
        else:
            event = events[existing_index]
            data.setdefault("sources", {})[source] = {key: value for key, value in event.items() if key != "event_id"}
        _write_jsonl(events_path, events)
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return data["sources"][source]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"无法读取 {path}：{exc}") from exc


def _read_json_object(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise StateError(f"输入必须是 JSON 对象：{path}")
    return value


def _read_records(path: Path) -> list[dict[str, Any]]:
    value = _read_json(path)
    if isinstance(value, list):
        records = value
    elif isinstance(value, dict) and isinstance(value.get("candidates"), list):
        records = value["candidates"]
    elif isinstance(value, dict) and isinstance(value.get("records"), list):
        records = value["records"]
    elif isinstance(value, dict):
        records = [value]
    else:
        raise StateError("输入必须是记录对象、对象数组或包含 candidates/records 数组")
    if not all(isinstance(item, dict) for item in records):
        raise StateError("输入数组中的每条记录必须是对象")
    return records


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise StateError(f"无效日期：{value}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="管理 AI 创业机会雷达 V2 状态")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化目录和状态")
    init_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)

    prepare_parser = subparsers.add_parser("prepare", help="在写报告前解析稳定 ID，不提交历史")
    prepare_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    prepare_parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    prepare_parser.add_argument("--date", default=beijing_today().isoformat())
    prepare_parser.add_argument("--input", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path)

    for command in ("record", "record-batch"):
        record_parser = subparsers.add_parser(command, help="原子提交记录和观察历史")
        record_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
        record_parser.add_argument("--kind", choices=sorted(KINDS), required=True)
        record_parser.add_argument("--date", default=beijing_today().isoformat())
        record_parser.add_argument("--run-id", required=True)
        record_parser.add_argument("--input", type=Path, required=True)

    history_parser = subparsers.add_parser("history", help="读取当前记录及窗口内观察历史")
    history_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    history_parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    history_parser.add_argument("--date", default=beijing_today().isoformat())
    history_parser.add_argument("--days", type=int, default=30)

    get_parser = subparsers.add_parser("get", help="按稳定 ID 读取记录")
    get_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    get_parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    get_parser.add_argument("--id", required=True)

    source_parser = subparsers.add_parser("source-health", help="更新来源当前状态并追加观察事件")
    source_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    source_parser.add_argument("--source", required=True)
    source_parser.add_argument("--status", required=True)
    source_parser.add_argument("--detail", default="")
    source_parser.add_argument("--date", default=beijing_today().isoformat())
    source_parser.add_argument("--run-id")

    args = parser.parse_args()
    try:
        if args.command == "init":
            result: Any = {"status": "initialized", "home": str(initialize_home(args.home))}
        elif args.command == "prepare":
            result = resolve_record_ids(args.home, args.kind, _read_records(args.input), _parse_date(args.date))
            if args.output:
                _write_json(args.output, result)
        elif args.command in {"record", "record-batch"}:
            records = _read_records(args.input)
            batch_result = upsert_records(
                args.home,
                args.kind,
                records,
                _parse_date(args.date),
                run_id=args.run_id,
            )
            result = batch_result[0] if args.command == "record" and len(batch_result) == 1 else batch_result
        elif args.command == "history":
            result = history(args.home, args.kind, _parse_date(args.date), args.days)
        elif args.command == "get":
            result = find_record(args.home, args.kind, args.id)
        else:
            result = update_source_health(
                args.home,
                args.source,
                args.status,
                args.detail,
                _parse_date(args.date),
                run_id=args.run_id,
            )
    except (OSError, StateError, ContractError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
