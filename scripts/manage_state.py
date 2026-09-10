#!/usr/bin/env python3
"""维护机会雷达 V3 当前视图、观察历史与 SIG→OPP 升级关系。"""

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
    canonical_evidence_url,
    canonical_sha256,
    evidence_independent_sources,
    fingerprint_record as contract_fingerprint_record,
    make_run_id,
    stable_record_id,
    validate_record_id,
    validate_run_as_of,
    validate_stage_envelope,
)
from filter_ideas import classify_candidate


DEFAULT_HOME = Path(os.environ.get("AI_OPPORTUNITY_RADAR_HOME", "~/Documents/AI-Opportunity-Radar")).expanduser()
KINDS = {"opportunity": "opportunities.jsonl", "signal": "signals.jsonl"}
OBSERVATION_FILES = {
    "opportunity": "opportunity-observations.jsonl",
    "signal": "signal-observations.jsonl",
}
SOURCE_STATUSES = {"ok", "no-results", "auth-required", "rate-limited", "blocked", "skipped-policy", "error"}
JOURNAL_FILENAME = "pending-transaction.json"
STATE_FILES = {*KINDS.values(), *OBSERVATION_FILES.values(), "source-health.json", "source-health-events.jsonl"}

DEFAULT_PREFERENCES: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "platform_phase": "phase_1_existing_platforms",
    "platform_expansion_enabled": False,
    "timezone": "Asia/Shanghai",
    "markets": ["global", "china", "southeast_asia", "south_asia", "africa", "middle_east", "latin_america"],
    "audiences": ["small_business", "consumer"],
    "mvp_days_max": 30,
    "raw_candidates_per_day": [100, 200],
    "validated_quick_ideas_per_day": [20, 40],
    "regional_migration_signals_per_day": [30, 80],
    "deep_opportunities_per_day": [3, 5],
    "display_full_qualified_ledger": True,
    "near_miss_display_max": 20,
    "paid_discovery_budget_share_max": 0.2,
    "paid_no_yield_stop_requests": 3,
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
    try:
        os.replace(temp_path, path)
        _sync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_lock(home: Path) -> Iterator[None]:
    """序列化当前视图、观察历史和来源状态更新。"""
    lock_path = home / "state" / ".radar.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            _recover_transaction(home)
            _repair_legacy_views(home)
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


def _load_records_unlocked(home: Path, kind: str) -> list[dict[str, Any]]:
    return _load_jsonl(_state_path(home, kind))


def _load_observations_unlocked(home: Path, kind: str) -> list[dict[str, Any]]:
    return _load_jsonl(_observation_path(home, kind))


def load_records(home: Path, kind: str) -> list[dict[str, Any]]:
    with _exclusive_lock(home):
        return _load_records_unlocked(home, kind)


def load_observations(home: Path, kind: str) -> list[dict[str, Any]]:
    with _exclusive_lock(home):
        return _load_observations_unlocked(home, kind)


def _validate_transaction(files: Any) -> dict[str, Any]:
    if not isinstance(files, dict) or not files:
        raise StateError("事务 files 必须是非空对象")
    for filename, payload in files.items():
        if filename not in STATE_FILES:
            raise StateError(f"事务包含未允许的状态文件：{filename}")
        if filename.endswith(".jsonl"):
            if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
                raise StateError(f"事务 {filename} 必须是对象数组")
        elif not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
            raise StateError("来源健康事务必须包含 sources 对象")
    return files


def _recover_transaction(home: Path) -> None:
    """先完成已持久化的事务，再允许读取；多文件中断可安全重放。"""
    journal = home / "state" / JOURNAL_FILENAME
    if not journal.exists():
        return
    try:
        value = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"无法读取待恢复事务：{exc}") from exc
    if not isinstance(value, dict):
        raise StateError("待恢复事务必须是对象")
    files = _validate_transaction(value.get("files"))
    for filename, payload in files.items():
        path = home / "state" / filename
        if filename.endswith(".jsonl"):
            _write_jsonl(path, payload, sort_current=filename in KINDS.values())
        else:
            _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    journal.unlink()
    _sync_directory(journal.parent)


def _commit_state(home: Path, files: dict[str, Any]) -> None:
    _validate_transaction(files)
    journal = {"schema_version": SCHEMA_VERSION, "files": files}
    _atomic_write_text(home / "state" / JOURNAL_FILENAME, json.dumps(journal, ensure_ascii=False) + "\n")
    _recover_transaction(home)


def _repair_legacy_views(home: Path) -> None:
    """从旧版已提交观察恢复缺失或落后的派生视图，保留无历史的旧记录。"""
    repairs: dict[str, Any] = {}
    for kind, filename in KINDS.items():
        current = _load_records_unlocked(home, kind)
        by_id = {row.get("id"): row for row in current}
        for event in _load_observations_unlocked(home, kind):
            snapshot = event.get("snapshot")
            if not isinstance(snapshot, dict) or not snapshot.get("id"):
                continue
            record_id = snapshot["id"]
            previous = by_id.get(record_id)
            if previous is None or str(snapshot.get("last_seen", "")) >= str(previous.get("last_seen", "")):
                by_id[record_id] = deepcopy(snapshot)
        repaired = list(by_id.values())
        changed = canonical_sha256(sorted(current, key=lambda row: str(row.get("id")))) != canonical_sha256(
            sorted(repaired, key=lambda row: str(row.get("id")))
        )
        if changed:
            repairs[filename] = repaired
    if repairs:
        _commit_state(home, repairs)


EVIDENCE_METADATA = {"evidence_id", "version", "revision_id", "supersedes", "recorded_on"}


def _evidence_content(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    content = {key: value for key, value in item.items() if key not in EVIDENCE_METADATA}
    if canonical_evidence_url(item.get("url")):
        content["url"] = canonical_evidence_url(item["url"])
    return content


def _evidence_key(item: Any) -> str:
    if isinstance(item, dict):
        return str(canonical_evidence_url(item.get("url")) or item.get("evidence_id")
                   or item.get("id") or canonical_sha256(item))
    return canonical_sha256(item)


def _evidence_references(item: dict[str, Any]) -> set[str]:
    references = {str(item[key]) for key in ("evidence_id", "id") if item.get(key)}
    url = canonical_evidence_url(item.get("url"))
    if url:
        references.add(url)
    return references


def _register_evidence_aliases(aliases: dict[str, str], value: dict[str, Any], key: str) -> None:
    for alias in _evidence_references(value) | {key}:
        if alias in aliases and aliases[alias] != key:
            raise StateError("证据身份冲突：同一 URL 或 ID 指向多个证据，请先明确纠错关系")
        aliases[alias] = key


def _has_current_support(item: Any, revised: list[dict[str, Any]]) -> bool:
    """修订来源的旧事实不得再次复用；需绑定有效版本及该版本的原文事实。"""
    if not isinstance(item, dict):
        return True
    references = _evidence_references(item)
    for evidence in revised:
        if not references.intersection(_evidence_references(evidence)):
            continue
        if evidence.get("retracted") is True or evidence.get("status") == "retracted":
            return False
        if item.get("evidence_revision_id") != evidence.get("revision_id"):
            return False
        fact_fields = ("fact", "supporting_fact", "quote", "text", "supports", "original_text")
        facts = {str(evidence[field]).strip() for field in fact_fields if evidence.get(field)}
        cited_facts = {str(item[field]).strip() for field in fact_fields if item.get(field)}
        if not cited_facts or not cited_facts.issubset(facts):
            return False
    return True


def _apply_evidence(merged: dict[str, Any], base: dict[str, Any], incoming: dict[str, Any], observed_on: date) -> None:
    archive = deepcopy(base.get("evidence_history") or base.get("evidence") or [])
    if not isinstance(archive, list) or not isinstance(incoming.get("evidence", []), list):
        raise StateError("evidence 和 evidence_history 必须是数组")
    latest: dict[str, Any] = {}
    aliases: dict[str, str] = {}
    normalized: list[Any] = []
    for item in archive:
        value = deepcopy(item)
        key = _evidence_key(value)
        if isinstance(value, dict):
            value.setdefault("evidence_id", "EVID-" + canonical_sha256(key)[:16].upper())
            value.setdefault("version", 1)
            value.setdefault("revision_id", f"{value['evidence_id']}:v{value['version']}")
            key = value["evidence_id"]
            _register_evidence_aliases(aliases, value, key)
        latest[key] = value
        normalized.append(value)
    for item in incoming.get("evidence", []):
        value = deepcopy(item)
        raw_key = _evidence_key(value)
        key = aliases.get(raw_key, raw_key)
        if isinstance(value, dict):
            known_keys = {aliases[alias] for alias in _evidence_references(value) if alias in aliases}
            if len(known_keys) > 1:
                raise StateError("证据身份冲突：输入 URL 与 ID 对应不同证据")
            if known_keys:
                key = next(iter(known_keys))
        previous = latest.get(key)
        if previous is not None and _evidence_content(previous) == _evidence_content(value):
            continue
        if isinstance(value, dict):
            evidence_id = previous.get("evidence_id") if isinstance(previous, dict) else value.get("evidence_id")
            evidence_id = evidence_id or "EVID-" + canonical_sha256(raw_key)[:16].upper()
            version = previous.get("version", 1) + 1 if isinstance(previous, dict) else 1
            value.update({"evidence_id": evidence_id, "version": version,
                          "revision_id": f"{evidence_id}:v{version}", "recorded_on": observed_on.isoformat()})
            if isinstance(previous, dict):
                value["supersedes"] = previous["revision_id"]
            key = evidence_id
            _register_evidence_aliases(aliases, value, key)
        latest[key] = value
        normalized.append(value)
    retracted = [item for item in latest.values() if isinstance(item, dict)
                 and (item.get("retracted") is True or item.get("status") == "retracted")]
    merged["evidence"] = [item for item in latest.values() if item not in retracted]
    merged["evidence_history"] = normalized
    revised = [item for item in latest.values() if isinstance(item, dict)
               and (item.get("version", 1) > 1 or item in retracted)]
    for field in ("payment_signals", "demand_signals"):
        if isinstance(merged.get(field), list):
            merged[field] = [item for item in merged[field] if _has_current_support(item, revised)]
    checks = merged.get("candidate_verifications")
    if isinstance(checks, dict):
        for check in checks.values():
            if isinstance(check, dict) and isinstance(check.get("evidence"), list):
                check["evidence"] = [item for item in check["evidence"] if _has_current_support(item, revised)]


def _validate_record_kind(kind: str, record: dict[str, Any]) -> None:
    tier = record.get("evidence_tier")
    if tier is None:
        return
    allowed = {"opportunity": {"A", "B"}, "signal": {"R"}}
    if tier not in allowed.get(kind, set()):
        raise StateError(f"证据层级 {tier} 不能写入 {kind}")
    actual, _, _ = classify_candidate(record)
    if actual != tier:
        raise StateError(f"证据层级 {tier} 与重新核验结果 {actual or '未合格'} 不一致")


def _validate_record_envelope(record: dict[str, Any], observed_on: date | None = None,
                              run_id: str | None = None) -> None:
    try:
        envelope = validate_stage_envelope(record)
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    if observed_on is not None and envelope.get("as_of", observed_on.isoformat()) != observed_on.isoformat():
        raise StateError("输入 as_of 与状态提交日期不一致")
    if run_id is not None and envelope.get("run_id", run_id) != run_id:
        raise StateError("输入 run_id 与状态提交运行不一致")


def _input_digest(record: dict[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in record.items() if key not in {"id", "fingerprint"}})


def _validate_replay(event: dict[str, Any], record: dict[str, Any]) -> None:
    if event.get("input_sha256"):
        identical = event["input_sha256"] == _input_digest(record)
    else:
        snapshot = event.get("snapshot") or {}
        identical = all(snapshot.get(key) == value for key, value in record.items() if key not in {"id", "fingerprint"})
    if not identical:
        raise StateError("同一 run_id 的输入内容不同；纠错请使用新的 run_id")


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
        current = _load_records_unlocked(home, kind)
        by_fingerprint = {item.get("fingerprint"): item for item in current if item.get("fingerprint")}
        prepared: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise StateError("每条待解析记录必须是对象")
            _validate_record_envelope(record, observed_on)
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
    if not isinstance(records, list) or not records:
        raise StateError("待提交记录必须是非空数组")
    home = initialize_home(home)
    with _exclusive_lock(home):
        current = _load_records_unlocked(home, kind)
        observations = _load_observations_unlocked(home, kind)
        events_by_id = {item.get("event_id"): item for item in observations}
        by_fingerprint = {item.get("fingerprint"): item for item in current if item.get("fingerprint")}
        results: list[dict[str, Any]] = []
        seen_batch: set[str] = set()
        for input_record in records:
            if not isinstance(input_record, dict):
                raise StateError("记录必须是 JSON 对象")
            _validate_record_envelope(input_record, observed_on, run_id)
            fingerprint = fingerprint_record(input_record)
            if fingerprint in seen_batch:
                raise StateError(f"输入批次包含重复机会指纹：{fingerprint}")
            seen_batch.add(fingerprint)
            existing = by_fingerprint.get(fingerprint)
            record_id = _expected_id(kind, input_record, observed_on, existing)
            event_id = hashlib.sha256(f"{run_id}|{kind}|{fingerprint}".encode("utf-8")).hexdigest()[:24]
            if event_id in events_by_id:
                _validate_replay(events_by_id[event_id], input_record)
                results.append({"status": "replayed", "id": record_id, "fingerprint": fingerprint})
                continue
            if existing is not None and observed_on.isoformat() < existing.get("last_seen", ""):
                raise StateError("不能在较新观察之后倒填历史；请按日期顺序导入，避免未来信息进入过去快照")
            base = deepcopy(existing) if existing is not None else {}
            merged = deepcopy(base)
            merged.update(deepcopy(input_record))
            merged.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "as_of": observed_on.isoformat(),
                    "id": record_id,
                    "fingerprint": fingerprint,
                    "first_seen": base.get("first_seen", observed_on.isoformat()),
                    "last_seen": max(base.get("last_seen", observed_on.isoformat()), observed_on.isoformat()),
                    "last_run_id": run_id,
                }
            )
            merged["seen_dates"] = sorted(set([*base.get("seen_dates", []), observed_on.isoformat()]))
            merged["occurrences"] = len(merged["seen_dates"])
            _apply_evidence(merged, base, input_record, observed_on)
            _validate_record_kind(kind, merged)
            event = _observation_event(kind=kind, run_id=run_id, observed_on=observed_on, record=merged)
            event["input_sha256"] = _input_digest(input_record)
            events_by_id[event["event_id"]] = event
            observations.append(event)
            if existing is None:
                current.append(merged)
                status = "created"
            else:
                current[current.index(existing)] = merged
                status = "updated"
            by_fingerprint[fingerprint] = merged
            results.append({"status": status, "id": record_id, "fingerprint": fingerprint})
        _commit_state(home, {OBSERVATION_FILES[kind]: observations, KINDS[kind]: current})
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


def promote_signal(
    home: Path,
    signal_id: str,
    opportunity: dict[str, Any],
    observed_on: date,
    *,
    run_id: str,
) -> dict[str, Any]:
    """在同一把锁内把区域 SIG 升级为 OPP，并双向保留审计链接。"""
    try:
        validate_record_id(signal_id, kind="signal")
        validate_run_as_of(run_id, observed_on.isoformat())
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    if not isinstance(opportunity, dict):
        raise StateError("升级后的机会必须是 JSON 对象")
    _validate_record_envelope(opportunity, observed_on, run_id)

    home = initialize_home(home)
    with _exclusive_lock(home):
        signals = _load_records_unlocked(home, "signal")
        signal = next((item for item in signals if item.get("id") == signal_id), None)
        if signal is None:
            raise StateError(f"未找到待升级信号：{signal_id}")
        if signal.get("promoted_to"):
            promotion_events = _load_observations_unlocked(home, "opportunity")
            prior = next((event for event in promotion_events
                          if event.get("run_id") == run_id and event.get("id") == signal["promoted_to"]
                          and (event.get("event_type") == "promotion"
                               or event.get("snapshot", {}).get("promoted_from") == signal_id)), None)
            if prior is not None and prior.get("input_sha256"):
                _validate_replay(prior, opportunity)
            return {
                "status": "replayed",
                "signal_id": signal_id,
                "opportunity_id": signal["promoted_to"],
            }
        if observed_on.isoformat() < signal.get("last_seen", ""):
            raise StateError("不能在较新观察之前升级信号；请按日期顺序导入")

        opportunities = _load_records_unlocked(home, "opportunity")
        opportunity_observations = _load_observations_unlocked(home, "opportunity")
        signal_observations = _load_observations_unlocked(home, "signal")
        value = deepcopy(opportunity)
        value["promoted_from"] = signal_id
        tier, reasons, gates = classify_candidate(value)
        if tier != "A":
            failed = reasons or [gate for gate, passed in gates.items() if not passed]
            raise StateError(f"SIG 只有达到 A 级证据才能升级：{', '.join(failed)}")
        value["evidence_tier"] = "A"
        fingerprint = fingerprint_record(value)
        if fingerprint != signal.get("fingerprint"):
            raise StateError("升级后的 OPP 必须与原 SIG 保持相同业务身份；用户、场景、需求、切入口和市场范围不能改变")
        existing = next((item for item in opportunities if item.get("fingerprint") == fingerprint), None)
        if existing is not None and observed_on.isoformat() < existing.get("last_seen", ""):
            raise StateError("不能在较新观察之前升级已有机会；请按日期顺序导入")
        opportunity_id = _expected_id("opportunity", value, observed_on, existing)
        base = deepcopy(existing) if existing is not None else {}
        merged = deepcopy(base)
        merged.update(value)
        merged.update(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "as_of": observed_on.isoformat(),
                "id": opportunity_id,
                "fingerprint": fingerprint,
                "first_seen": base.get("first_seen", observed_on.isoformat()),
                "last_seen": max(base.get("last_seen", observed_on.isoformat()), observed_on.isoformat()),
                "last_run_id": run_id,
            }
        )
        merged["seen_dates"] = sorted(set([*base.get("seen_dates", []), observed_on.isoformat()]))
        merged["occurrences"] = len(merged["seen_dates"])
        _apply_evidence(merged, base, value, observed_on)
        _validate_record_kind("opportunity", merged)
        opportunity_event = _observation_event(
            kind="opportunity", run_id=run_id, observed_on=observed_on, record=merged
        )
        opportunity_event["event_id"] = hashlib.sha256(
            f"{run_id}|opportunity-promotion|{fingerprint}".encode("utf-8")
        ).hexdigest()[:24]
        opportunity_event["event_type"] = "promotion"
        opportunity_event["input_sha256"] = _input_digest(opportunity)
        if opportunity_event["event_id"] not in {item.get("event_id") for item in opportunity_observations}:
            opportunity_observations.append(opportunity_event)
        if existing is None:
            opportunities.append(merged)
            status = "created"
        else:
            opportunities[opportunities.index(existing)] = merged
            status = "updated"

        signal_index = signals.index(signal)
        promoted_signal = deepcopy(signal)
        promoted_signal.update(
            {
                "promoted_to": opportunity_id,
                "run_id": run_id,
                "as_of": observed_on.isoformat(),
                "promotion_status": "promoted",
                "promoted_on": observed_on.isoformat(),
                "last_seen": max(signal.get("last_seen", observed_on.isoformat()), observed_on.isoformat()),
                "last_run_id": run_id,
            }
        )
        promoted_signal["seen_dates"] = sorted(
            set([*signal.get("seen_dates", []), observed_on.isoformat()])
        )
        promoted_signal["occurrences"] = len(promoted_signal["seen_dates"])
        signals[signal_index] = promoted_signal
        signal_event = _observation_event(
            kind="signal", run_id=run_id, observed_on=observed_on, record=promoted_signal
        )
        signal_event["event_id"] = hashlib.sha256(
            f"{run_id}|signal-promotion|{promoted_signal['fingerprint']}".encode("utf-8")
        ).hexdigest()[:24]
        signal_event["event_type"] = "promotion"
        if signal_event["event_id"] not in {item.get("event_id") for item in signal_observations}:
            signal_observations.append(signal_event)

        _commit_state(home, {KINDS["opportunity"]: opportunities,
                             OBSERVATION_FILES["opportunity"]: opportunity_observations,
                             KINDS["signal"]: signals,
                             OBSERVATION_FILES["signal"]: signal_observations})
        return {"status": status, "signal_id": signal_id, "opportunity_id": opportunity_id}


def history(home: Path, kind: str, as_of: date, days: int) -> list[dict[str, Any]]:
    """按观察时间返回截止日期的快照，避免当前视图泄漏未来信息。"""
    if days <= 0:
        raise StateError("days 必须大于 0")
    home = initialize_home(home)
    threshold = as_of - timedelta(days=days - 1)
    with _exclusive_lock(home):
        current = _load_records_unlocked(home, kind)
        observations = _load_observations_unlocked(home, kind)
    grouped: dict[str, list[dict[str, Any]]] = {}
    observed_ids: set[str] = set()
    for event in observations:
        try:
            observed_on = datetime.strptime(event["observed_on"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError) as exc:
            raise StateError(f"观察事件 {event.get('event_id', '<unknown>')} 日期无效") from exc
        observed_ids.add(str(event.get("id")))
        if threshold <= observed_on <= as_of:
            grouped.setdefault(str(event.get("id")), []).append(event)
    result: list[dict[str, Any]] = []
    for events in grouped.values():
        events = sorted(events, key=lambda item: item["observed_on"])
        snapshot = events[-1].get("snapshot")
        if not isinstance(snapshot, dict):
            raise StateError("观察事件缺少有效 snapshot，无法安全回顾历史")
        value = deepcopy(snapshot)
        value["observation_history"] = deepcopy(events)
        result.append(value)
    for record in current:
        try:
            last_seen = datetime.strptime(record["last_seen"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError) as exc:
            raise StateError(f"记录 {record.get('id', '<unknown>')} 的 last_seen 无效") from exc
        if str(record.get("id")) not in observed_ids and threshold <= last_seen <= as_of:
            value = deepcopy(record)
            value["observation_history"] = []
            result.append(value)
    return sorted(result, key=lambda item: (item["last_seen"], item.get("id", "")), reverse=True)


def find_record(home: Path, kind: str, record_id: str) -> dict[str, Any]:
    try:
        validate_record_id(record_id, kind=kind)
    except ContractError as exc:
        raise StateError(str(exc)) from exc
    home = initialize_home(home)
    with _exclusive_lock(home):
        for record in _load_records_unlocked(home, kind):
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
        _commit_state(home, {"source-health-events.jsonl": events, "source-health.json": data})
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


def _read_records(path: Path, *, observed_on: date | None = None,
                  run_id: str | None = None) -> list[dict[str, Any]]:
    value = _read_json(path)
    if isinstance(value, dict):
        _validate_record_envelope(value, observed_on, run_id)
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
    parser = argparse.ArgumentParser(description="管理 AI 创业机会雷达 V3 状态")
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

    promote_parser = subparsers.add_parser("promote", help="将已补齐本地证据的 SIG 升级为 OPP")
    promote_parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    promote_parser.add_argument("--signal-id", required=True)
    promote_parser.add_argument("--date", default=beijing_today().isoformat())
    promote_parser.add_argument("--run-id", required=True)
    promote_parser.add_argument("--input", type=Path, required=True, help="升级后的机会 JSON 对象")

    args = parser.parse_args()
    try:
        if args.command == "init":
            result: Any = {"status": "initialized", "home": str(initialize_home(args.home))}
        elif args.command == "prepare":
            observed_on = _parse_date(args.date)
            result = resolve_record_ids(args.home, args.kind,
                                        _read_records(args.input, observed_on=observed_on), observed_on)
            if args.output:
                _write_json(args.output, result)
        elif args.command in {"record", "record-batch"}:
            observed_on = _parse_date(args.date)
            records = _read_records(args.input, observed_on=observed_on, run_id=args.run_id)
            batch_result = upsert_records(
                args.home,
                args.kind,
                records,
                observed_on,
                run_id=args.run_id,
            )
            result = batch_result[0] if args.command == "record" and len(batch_result) == 1 else batch_result
        elif args.command == "history":
            result = history(args.home, args.kind, _parse_date(args.date), args.days)
        elif args.command == "get":
            result = find_record(args.home, args.kind, args.id)
        elif args.command == "source-health":
            result = update_source_health(
                args.home,
                args.source,
                args.status,
                args.detail,
                _parse_date(args.date),
                run_id=args.run_id,
            )
        else:
            result = promote_signal(
                args.home,
                args.signal_id,
                _read_json_object(args.input),
                _parse_date(args.date),
                run_id=args.run_id,
            )
    except (OSError, StateError, ContractError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
