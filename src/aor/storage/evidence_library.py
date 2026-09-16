"""追加式证据观察日志及可删除重建的 SQLite 检索索引。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aor.evidence.identity import (
    EVIDENCE_METADATA_FIELDS, canonical_evidence_url, canonical_sha256, evidence_content,
    evidence_identity_key, evidence_object_identity, normalize_identity,
)
from aor.evidence.retrieval import reciprocal_rank_fusion, resolve_evidence_reference
from aor.evidence.derivations import select_active_derivations, validate_derive_set


TZ = ZoneInfo("Asia/Shanghai")
COLLECTION_FIELDS = EVIDENCE_METADATA_FIELDS


class EvidenceLibraryError(ValueError):
    """证据内容、时间或修订引用不符合本地存储契约。"""


def _timestamp(value: str | date, *, end_of_day: bool = False) -> float:
    try:
        if isinstance(value, date) and not isinstance(value, datetime):
            parsed = datetime.combine(value, time.max if end_of_day else time.min, TZ)
        else:
            text = str(value)
            if len(text) == 10:
                parsed = datetime.combine(date.fromisoformat(text), time.max if end_of_day else time.min, TZ)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=TZ)
        return parsed.timestamp()
    except (ValueError, TypeError) as exc:
        raise EvidenceLibraryError(f"无效证据日期：{value}") from exc


def _date_text(value: str | date) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise EvidenceLibraryError("as_of 必须为 YYYY-MM-DD 日期") from exc


def _run_id(value: str | None, as_of: str) -> None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"RUN-\d{8}-[A-F0-9]{10}", value)
                              or value[4:12] != as_of.replace("-", "")):
        raise EvidenceLibraryError("run_id 格式或日期与 as_of 不一致")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _search_text(record: dict[str, Any]) -> str:
    return " ".join(str(record.get(field) or "") for field in
                    ("title", "original_text", "text", "fact", "supporting_fact", "quote", "comments", "payer", "market"))


def _validate_dates(value: Any, cutoff: float) -> None:
    """正文附带的评论也必须处于观察窗口，不能借父帖子日期绕过截止。"""
    if isinstance(value, dict):
        for field in ("published_at", "observed_at", "date", "first_observed_at", "last_observed_at",
                      "recorded_on", "as_of", "recorded_at", "performed_at", "completed_at"):
            if value.get(field) and _timestamp(value[field]) > cutoff:
                raise EvidenceLibraryError(f"未来 {field} 不能进入 as_of")
        for child in value.values():
            if isinstance(child, (dict, list)):
                _validate_dates(child, cutoff)
    elif isinstance(value, list):
        for child in value:
            _validate_dates(child, cutoff)


class EvidenceLibrary:
    """root 下的 evidence.jsonl 是事实来源，evidence.sqlite3 仅为派生缓存。

    本地写入使用 macOS/Linux 文件锁串行化；实例不持有长连接。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.journal_path = self.root / "evidence.jsonl"
        self.derivation_path = self.root / "derivations.jsonl"
        self.review_path = self.root / "evidence-reviews.jsonl"
        self.index_path = self.root / "evidence.sqlite3"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".evidence.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _stamp(self) -> str:
        if not self.journal_path.exists():
            return "0:0"
        stat = self.journal_path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def _derivation_events(self) -> list[dict[str, Any]]:
        """调用者持锁；独立事实日志不依赖可删除的 SQLite 索引。"""
        if not self.derivation_path.exists():
            return []
        events = []
        for number, line in enumerate(self.derivation_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get("event_id") != canonical_sha256({k: v for k, v in event.items() if k != "event_id"}):
                    raise ValueError("登记日志摘要不匹配")
                if event.get("schema_version") != "derivations-1":
                    raise ValueError("不支持的登记日志版本")
                known_on, as_of = _date_text(event["known_on"]), _date_text(event["as_of"])
                if as_of > known_on:
                    raise ValueError("原始解析日期晚于获知日期")
                _run_id(event.get("run_id"), known_on)
                validate_derive_set(event["derive_set"])
                events.append(event)
            except (ValueError, KeyError, TypeError) as exc:
                raise EvidenceLibraryError(f"解析登记日志第 {number} 行无效：{exc}") from exc
        return events

    def _derivation_payloads(self, as_of: str) -> list[dict[str, Any]]:
        return [{"derive_sets": [event["derive_set"]]} for event in self._derivation_events()
                if event["known_on"] <= as_of and event["as_of"] <= as_of]

    def derivation_payloads(self, *, as_of: str | date) -> list[dict[str, Any]]:
        """按实际获知日加载所有历史登记；新 run 无解析输入也不能恢复已替代成员。"""
        day = _date_text(as_of)
        with self._locked():
            return self._derivation_payloads(day)

    def register_derivations(self, payloads: Iterable[dict[str, Any]], *, known_on: str | date,
                             run_id: str | None = None, source: str | None = None) -> dict[str, Any]:
        """登记已核验的离线解析集合；批次先验证、锁内幂等追加，不更改证据原日志。"""
        day = _date_text(known_on)
        _run_id(run_id, day)
        events = []
        for payload in payloads:
            if not isinstance(payload, dict) or not isinstance(payload.get("derive_sets", []), list):
                raise EvidenceLibraryError("解析登记输入必须为含 derive_sets 数组的对象")
            for item in payload.get("derive_sets", []):
                try:
                    checked = validate_derive_set(item)
                    as_of = _date_text(payload.get("as_of") or day)
                    if as_of > day:
                        raise ValueError("未来解析不能进入当前获知日")
                except (ValueError, TypeError) as exc:
                    raise EvidenceLibraryError(str(exc)) from exc
                event = {"schema_version": "derivations-1", "known_on": day, "as_of": as_of,
                         "run_id": run_id, "source_run_id": payload.get("run_id"), "source": source,
                         "derive_set": checked}
                event["event_id"] = canonical_sha256(event)
                events.append(event)
        with self._locked():
            known = {event["event_id"] for event in self._derivation_events()}
            pending = []
            for event in events:
                if event["event_id"] not in known:
                    pending.append(event)
                    known.add(event["event_id"])
            if pending:
                with self.derivation_path.open("a", encoding="utf-8") as journal:
                    journal.write("".join(_json(event) + "\n" for event in pending))
                    journal.flush()
                    os.fsync(journal.fileno())
        return {"received": len(events), "events_added": len(pending), "known_on": day,
                "run_id": run_id, "journal_path": str(self.derivation_path)}

    @staticmethod
    def _checked_review(review: dict[str, Any], known_on: str) -> dict[str, Any]:
        required = ("evidence_id", "revision_id", "status", "reviewer", "reviewed_at", "rationale")
        if not isinstance(review, dict) or any(not isinstance(review.get(k), str) or not review[k].strip()
                                               for k in required):
            raise EvidenceLibraryError("语义复核必须固定证据 ID、修订、审阅者、日期、结论及依据")
        if review["status"] not in {"relevant", "unrelated"}:
            raise EvidenceLibraryError("语义复核结论必须为 relevant 或 unrelated")
        if _timestamp(review["reviewed_at"]) > _timestamp(known_on, end_of_day=True):
            raise EvidenceLibraryError("未来复核不能进入当前获知日")
        checked = {key: review[key] for key in required}
        if review.get("evidence_role") is not None:
            if not isinstance(review["evidence_role"], str) or not review["evidence_role"].strip():
                raise EvidenceLibraryError("复核证据角色必须为非空字符串")
            checked["evidence_role"] = review["evidence_role"]
        return checked

    def _review_events(self) -> list[dict[str, Any]]:
        """调用者持锁；复核日志不属于采集观察，也不参与当前正文选择。"""
        if not self.review_path.exists():
            return []
        events = []
        for number, line in enumerate(self.review_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get("event_id") != canonical_sha256({k: v for k, v in event.items() if k != "event_id"}):
                    raise ValueError("复核日志摘要不匹配")
                if event.get("schema_version") != "evidence-reviews-1":
                    raise ValueError("不支持的复核日志版本")
                day = _date_text(event["known_on"])
                _run_id(event.get("run_id"), day)
                checked = self._checked_review(event["review"], day)
                if checked != event["review"] or event.get("review_id") != "REVIEW-" + canonical_sha256(checked):
                    raise ValueError("复核内容摘要不匹配")
                events.append(event)
            except (ValueError, KeyError, TypeError) as exc:
                raise EvidenceLibraryError(f"复核日志第 {number} 行无效：{exc}") from exc
        return events

    def _apply_reviews(self, records: list[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
        latest = {}
        for index, event in enumerate(self._review_events()):
            if event["known_on"] > as_of:
                continue
            review = event["review"]
            key = (review["evidence_id"], review["revision_id"])
            order = (_timestamp(review["reviewed_at"]), event["known_on"], index)
            if key not in latest or order > latest[key][0]:
                latest[key] = (order, review)
        for row in records:
            key = (row.get("evidence_id"), row.get("revision_id"))
            keys = [key, *[(ref.get("evidence_id"), ref.get("revision_id"))
                          for ref in row.get("legacy_references", [])]]
            candidates = [latest[k] for k in keys if k in latest]
            if candidates:
                review = deepcopy(max(candidates, key=lambda entry: entry[0])[1])
                review.update(evidence_id=key[0], revision_id=key[1])
                row["relevance_review"] = review
                row["semantic_relevance_status"] = review["status"]
                if review.get("evidence_role"):
                    row["evidence_role"] = review["evidence_role"]
            else:
                embedded = row.get("relevance_review")
                if isinstance(embedded, dict) and (embedded.get("evidence_id"), embedded.get("revision_id")) != key:
                    row.pop("relevance_review", None)
                    row.pop("semantic_relevance_status", None)
        return records

    def apply_reviews(self, records: list[dict[str, Any]], *, as_of: str | date) -> list[dict[str, Any]]:
        """给已选择的正文视图附加有效复核；不改变观察事件及调用方对象。"""
        day = _date_text(as_of)
        with self._locked():
            return self._apply_reviews(deepcopy(records), day)

    def register_reviews(self, reviews: Iterable[dict[str, Any]], *, known_on: str | date,
                         run_id: str | None = None, source: str | None = None) -> dict[str, Any]:
        """精确历史修订可被复核，但登记不会让它重新成为当前正文或一次新采集。"""
        day = _date_text(known_on)
        _run_id(run_id, day)
        events = []
        for review in reviews:
            checked = self._checked_review(review, day)
            stored = self.resolve({k: checked[k] for k in ("evidence_id", "revision_id")}, as_of=day)
            checked.update(evidence_id=stored["evidence_id"], revision_id=stored["revision_id"])
            event = {"schema_version": "evidence-reviews-1", "known_on": day, "run_id": run_id,
                     "source": source, "review_id": "REVIEW-" + canonical_sha256(checked), "review": checked}
            event["event_id"] = canonical_sha256(event)
            events.append(event)
        with self._locked():
            # 跨 run 重复提交同一旧复核不能覆盖后来的一次否定复核。
            known = {event["review_id"] for event in self._review_events()}
            pending = []
            for event in events:
                if event["review_id"] not in known:
                    pending.append(event)
                    known.add(event["review_id"])
            if pending:
                with self.review_path.open("a", encoding="utf-8") as journal:
                    journal.write("".join(_json(event) + "\n" for event in pending))
                    journal.flush()
                    os.fsync(journal.fileno())
        return {"received": len(events), "events_added": len(pending), "known_on": day,
                "run_id": run_id, "journal_path": str(self.review_path)}

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> bool:
        connection.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE revisions (
                evidence_id TEXT NOT NULL, revision_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                published_ts REAL, search_text TEXT NOT NULL,
                PRIMARY KEY (evidence_id, revision_id));
            CREATE TABLE observations (
                event_id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                observed_ts REAL NOT NULL, known_ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX observation_cutoff ON observations(observed_ts, evidence_id);
        """)
        try:
            connection.execute("CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, revision_id UNINDEXED, body)")
            fts = True
        except sqlite3.OperationalError as exc:
            if "no such module" not in str(exc).lower():
                raise
            fts = False
        connection.execute("INSERT INTO meta VALUES ('fts', ?)", (str(int(fts)),))
        return fts

    @staticmethod
    def _insert(connection: sqlite3.Connection, event: dict[str, Any], *, fts: bool) -> tuple[int, int]:
        if event.get("event_id") != canonical_sha256({key: value for key, value in event.items() if key != "event_id"}):
            raise EvidenceLibraryError("证据日志摘要不匹配，不能从已损坏事实来源重建")
        record = event["record"]
        evidence_id, revision_id = record["evidence_id"], record["revision_id"]
        existing = connection.execute("SELECT content_hash FROM revisions WHERE evidence_id=? AND revision_id=?",
                                      (evidence_id, revision_id)).fetchone()
        if existing and existing[0] != record["content_hash"]:
            raise EvidenceLibraryError(f"同一修订对应不同正文：{revision_id}")
        added_revision = 0
        if existing is None:
            published = record.get("published_at") or record.get("date")
            body = _search_text(record)
            connection.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?)",
                               (evidence_id, revision_id, record["content_hash"],
                                _timestamp(published) if published else None, body))
            if fts:
                connection.execute("INSERT INTO evidence_fts VALUES (?, ?, ?)", (evidence_id, revision_id, body))
            added_revision = 1
        cursor = connection.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?, ?)",
                                    (event["event_id"], evidence_id, revision_id, _timestamp(event["observed_at"]),
                                     _timestamp(event["as_of"]), _json(event)))
        return cursor.rowcount, added_revision

    def _rebuild(self) -> dict[str, Any]:
        self._derivation_events()  # 不能以重建绕过已损坏的持久派生状态。
        self._review_events()
        descriptor, name = tempfile.mkstemp(prefix="evidence-index-", suffix=".sqlite3", dir=self.root)
        os.close(descriptor)
        temporary = Path(name)
        connection = sqlite3.connect(temporary)
        observations = revisions = 0
        try:
            fts = self._schema(connection)
            if self.journal_path.exists():
                with self.journal_path.open(encoding="utf-8") as source:
                    for line_number, line in enumerate(source, 1):
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                            count, new_revision = self._insert(connection, event, fts=fts)
                        except (ValueError, KeyError, TypeError) as exc:
                            raise EvidenceLibraryError(f"证据日志第 {line_number} 行不可重建：{exc}") from exc
                        observations += count
                        revisions += new_revision
            connection.execute("INSERT INTO meta VALUES ('journal_stamp', ?)", (self._stamp(),))
            connection.commit()
            connection.close()
            os.replace(temporary, self.index_path)
        finally:
            connection.close()
            temporary.unlink(missing_ok=True)
        return {"observations": observations, "revisions": revisions, "fts5": fts,
                "journal_path": str(self.journal_path), "index_path": str(self.index_path)}

    def rebuild(self) -> dict[str, Any]:
        """完整重放 JSONL；损坏或修订冲突时拒绝替换已有索引。"""
        with self._locked():
            return self._rebuild()

    def migrate_identities(self, destination: str | Path) -> dict[str, Any]:
        """将每条历史观察派生到新库；不修改源日志，不静默重定向旧含糊 ID。

        目标必须不存在。先完整校验源、生成映射并重建临时库，再原子发布目标目录。
        历史旧 ID + revision 通过 legacy_references 保留；只有旧 ID 的引用需要人工
        选择原生对象或原文。无法辨认的父帖评论拒绝迁移，保留源数据等待修复。
        """
        target = Path(destination).resolve()
        if target == self.root.resolve() or target.exists():
            raise EvidenceLibraryError("迁移目标必须是不同且尚不存在的目录")
        events = []
        with self._locked():
            if not self.journal_path.exists():
                raise EvidenceLibraryError("找不到待迁移的证据日志")
            original_bytes = self.journal_path.read_bytes()
            derivation_events = self._derivation_events()
            derivation_bytes = self.derivation_path.read_bytes() if self.derivation_path.exists() else None
            review_events = self._review_events()
            review_bytes = self.review_path.read_bytes() if self.review_path.exists() else None
            for number, line in enumerate(original_bytes.decode("utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if event.get("event_id") != canonical_sha256({key: value for key, value in event.items() if key != "event_id"}):
                        raise ValueError("证据日志摘要不匹配")
                    events.append(event)
                except (ValueError, TypeError, KeyError) as exc:
                    raise EvidenceLibraryError(f"证据日志第 {number} 行不可迁移：{exc}") from exc
        migrated = []
        mappings: dict[tuple[str, str], set[tuple[str, str]]] = {}
        old_id_targets: dict[str, set[str]] = {}
        for event in events:
            record = deepcopy(event["record"])
            old = {"evidence_id": record["evidence_id"], "revision_id": record["revision_id"]}
            inherited = record.get("legacy_references") or []
            for field in ("evidence_id", "library_evidence_id", "revision_id", "content_hash", "identity_version"):
                record.pop(field, None)
            record["aliases"] = [alias for alias in record.get("aliases") or [] if not str(alias).startswith("EVID-")]
            record["legacy_references"] = [*inherited, old]
            new = self._event(record, as_of=event["as_of"], run_id=event.get("run_id"), raw_ref=event.get("raw_ref"))
            migrated.append(new)
            new_record = new["record"]
            mappings.setdefault((old["evidence_id"], old["revision_id"]), set()).add(
                (new_record["evidence_id"], new_record["revision_id"]))
            old_id_targets.setdefault(old["evidence_id"], set()).add(new_record["evidence_id"])
        for event in review_events:
            reference = event["review"]
            targets = mappings.get((reference["evidence_id"], reference["revision_id"]), set())
            if len(targets) != 1:
                raise EvidenceLibraryError("复核引用在身份迁移中不唯一，保留原库等待核验")
        manifest = {
            "migration_version": "evidence-identity-v2", "source_journal": str(self.journal_path.resolve()),
            "source_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "observations": len(migrated), "identities": len({item["record"]["evidence_id"] for item in migrated}),
            "derivation_events": len(derivation_events),
            "review_events": len(review_events),
            "source_reviews_sha256": hashlib.sha256(review_bytes).hexdigest() if review_bytes is not None else None,
            "source_derivations_sha256": hashlib.sha256(derivation_bytes).hexdigest() if derivation_bytes is not None else None,
            "ambiguous_legacy_ids": sorted(key for key, values in old_id_targets.items() if len(values) > 1),
            "references": [{"old": {"evidence_id": key[0], "revision_id": key[1]},
                            "targets": [{"evidence_id": eid, "revision_id": rev} for eid, rev in sorted(values)],
                            "status": "unique" if len(values) == 1 else "ambiguous"}
                           for key, values in sorted(mappings.items())],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="evidence-migration-", dir=target.parent) as temporary:
            stage = Path(temporary) / "library"
            stage.mkdir()
            staged = EvidenceLibrary(stage)
            staged.journal_path.write_text("".join(_json(event) + "\n" for event in migrated), encoding="utf-8")
            if derivation_bytes is not None:
                staged.derivation_path.write_bytes(derivation_bytes)
            if review_bytes is not None:
                staged.review_path.write_bytes(review_bytes)
            staged.rebuild()
            (stage / "identity-migration.json").write_text(_json(manifest) + "\n", encoding="utf-8")
            # 仍拒绝覆盖可能在迁移期间出现的目标；目录发布不会触碰原库。
            if target.exists():
                raise EvidenceLibraryError("迁移目标已存在，未覆盖")
            os.rename(stage, target)
        return {**manifest, "destination": str(target)}

    @staticmethod
    def _assert_identity(record: dict[str, Any]) -> None:
        expected = "EVID-" + canonical_sha256(evidence_identity_key(record))[:16].upper()
        if record.get("evidence_id") != expected:
            raise EvidenceLibraryError("证据库含旧的 URL 合并身份；请先 migrate_identities 到新目录，保留旧库和引用映射")

    @contextmanager
    def _connection(self):
        with self._locked():
            connection = None
            try:
                if self.index_path.exists():
                    connection = sqlite3.connect(self.index_path)
                    stamp = connection.execute("SELECT value FROM meta WHERE key='journal_stamp'").fetchone()
                    if not stamp or stamp[0] != self._stamp():
                        connection.close()
                        connection = None
                if connection is None:
                    self._rebuild()
                    connection = sqlite3.connect(self.index_path)
            except sqlite3.DatabaseError:
                if connection is not None:
                    connection.close()
                self._rebuild()
                connection = sqlite3.connect(self.index_path)
            try:
                yield connection
            finally:
                connection.close()

    @staticmethod
    def _event(record: dict[str, Any], *, as_of: str, run_id: str | None, raw_ref: str | None) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise EvidenceLibraryError("证据必须是对象")
        item = deepcopy(record)
        canonical_url = canonical_evidence_url(item.get("original_url") or item.get("url"))
        publisher = normalize_identity(item.get("original_publisher") or item.get("original_author") or item.get("publisher_id"))
        if not canonical_url:
            raise EvidenceLibraryError("证据必须提供可追溯的 HTTP(S) URL")
        observed = str(item.get("observed_at") or item.get("recorded_on") or as_of)
        cutoff = _timestamp(as_of, end_of_day=True)
        if _timestamp(observed) > cutoff:
            raise EvidenceLibraryError("未来观察不能进入 as_of")
        _validate_dates(item, cutoff)
        # 原生对象优先：父帖地址只是关联，不能令评论覆盖父帖正文。
        try:
            identity = evidence_identity_key(item)
        except ValueError as exc:
            raise EvidenceLibraryError(str(exc)) from exc
        library_id = "EVID-" + canonical_sha256(identity)[:16].upper()
        native = evidence_object_identity(item)
        if any(value and str(value).startswith("EVID-") and value != library_id
               for value in (item.get("library_evidence_id"), item.get("evidence_id"))):
            raise EvidenceLibraryError("旧证据身份或对象身份冲突；请先显式迁移，不能静默覆盖旧引用")
        aliases = sorted({str(value) for value in [*(item.get("aliases") or []), item.get("id"),
                         item.get("evidence_id"), library_id] if value})
        content = evidence_content(item)
        content_hash = canonical_sha256(content)
        revision = item.get("revision_id") or f"{library_id}:{content_hash[:16]}"
        if not isinstance(revision, str) or not revision.strip():
            raise EvidenceLibraryError("revision_id 必须为非空字符串")
        item.update(evidence_id=library_id, library_evidence_id=library_id, revision_id=revision, identity_version="2",
                    content_hash=content_hash, canonical_url=canonical_url, observed_at=observed, aliases=aliases,
                    independent_source_key=f"publisher:{publisher}" if publisher else f"host:{urlparse(canonical_url).hostname}")
        if native:
            item["object_identity"] = dict(zip(("source", "kind", "id"), native))
        event = {"schema_version": "3.0", "observed_at": observed, "run_id": run_id or item.get("run_id"),
                 "as_of": as_of, "raw_ref": raw_ref or item.get("raw_file") or item.get("raw_ref"), "record": item}
        event["event_id"] = canonical_sha256(event)
        return event

    def ingest(self, records: Iterable[dict[str, Any]], *, as_of: str | date,
               run_id: str | None = None, raw_ref: str | None = None) -> dict[str, Any]:
        """入库一个离线批次；原文及原始文件定位随观察日志永久保留。"""
        day = _date_text(as_of)
        _run_id(run_id, day)
        events = [self._event(record, as_of=day, run_id=run_id, raw_ref=raw_ref) for record in records]
        added = revisions = 0
        with self._connection() as connection:
            for (payload,) in connection.execute("SELECT payload FROM observations"):
                self._assert_identity(json.loads(payload)["record"])
            fts = connection.execute("SELECT value FROM meta WHERE key='fts'").fetchone()[0] == "1"
            pending = []
            # 先在事务内检验整个批次；引用冲突不会留下半批事实日志。
            with connection:
                for event in events:
                    count, new_revision = self._insert(connection, event, fts=fts)
                    if count:
                        pending.append(event)
                    added += count
                    revisions += new_revision
                if pending:
                    with self.journal_path.open("a", encoding="utf-8") as journal:
                        journal.write("".join(_json(event) + "\n" for event in pending))
                        journal.flush()
                        os.fsync(journal.fileno())
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('journal_stamp', ?)", (self._stamp(),))
        return {"schema_version": "3.0", "as_of": day, "run_id": run_id, "received": len(events),
                "observations_added": added, "revisions_added": revisions,
                "journal_path": str(self.journal_path), "index_path": str(self.index_path)}

    def _snapshot(self, connection: sqlite3.Connection, as_of: str, run_id: str | None = None,
                  *, include_retracted: bool = False, include_superseded: bool = False) -> list[dict[str, Any]]:
        cutoff = _timestamp(as_of, end_of_day=True)
        # 同一获知日按不可变日志的先后选修订；仅日期的观察不能因精度较低而输给旧时间戳。
        rows = connection.execute("""
            SELECT o.payload FROM observations o JOIN revisions r
            ON o.evidence_id=r.evidence_id AND o.revision_id=r.revision_id
            WHERE o.observed_ts<=? AND o.known_ts<=? AND (r.published_ts IS NULL OR r.published_ts<=?)
            ORDER BY o.known_ts, o.rowid
        """, (cutoff, cutoff, cutoff))
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            event = json.loads(row[0])
            EvidenceLibrary._assert_identity(event["record"])
            grouped.setdefault(event["record"]["evidence_id"], []).append(event)
        records = []
        for events in grouped.values():
            # 旧运行的缓存导入保留为历史观察，但不能反向覆盖后来实际观察到的更正／撤回。
            # 对象只有历史材料时仍可检索，其 historical_import 标记明确表示复用。
            current_events = [event for event in events if not event["record"].get("historical_import")]
            latest = (current_events or events)[-1]
            item = deepcopy(latest["record"])
            if not include_retracted and (item.get("retracted") is True or item.get("status") == "retracted"):
                continue
            revision_events = [event for event in events if event["record"]["revision_id"] == item["revision_id"]]
            item["first_observed_at"] = min((event["observed_at"] for event in events), key=_timestamp)
            item["last_observed_at"] = max((event["observed_at"] for event in events), key=_timestamp)
            item["recorded_on"] = latest["as_of"]
            item["run_ids"] = sorted({event["run_id"] for event in revision_events if event.get("run_id")})
            item["source_labels"] = sorted({str(event["record"]["source"]) for event in revision_events if event["record"].get("source")})
            item["same_source_refs"] = sorted({str(event["record"]["url"]) for event in revision_events if event["record"].get("url")})
            refs = {}
            for event in revision_events:
                raw_refs = list(event["record"].get("raw_refs") or [])
                if event.get("raw_ref"):
                    raw_refs.append({"path": event["raw_ref"], "json_pointer": event["record"].get("raw_json_pointer")})
                for ref in raw_refs:
                    refs[canonical_sha256(ref)] = ref
            item["raw_refs"] = list(refs.values())
            derivations = {canonical_sha256(ref): ref for event in revision_events
                           for ref in event["record"].get("derivation_refs") or []}
            if derivations:
                item["derivation_refs"] = list(derivations.values())
            item["aliases"] = sorted({alias for event in revision_events for alias in event["record"]["aliases"]})
            legacy = {canonical_sha256(ref): ref for event in revision_events
                      for ref in event["record"].get("legacy_references") or []}
            if legacy:
                item["legacy_references"] = list(legacy.values())
            item.pop("reused_for_run_id", None)
            if run_id is not None and run_id not in item["run_ids"]:
                item["reused_for_run_id"] = run_id
            records.append(item)
        records = self._apply_reviews(records, as_of)
        active, superseded = select_active_derivations(records, self._derivation_payloads(as_of))
        return [*active, *superseded] if include_superseded else active

    def resolve(self, reference: dict[str, Any], *, as_of: str | date) -> dict[str, Any]:
        """按截止日解析精确历史修订；无版本引用只在当前快照中解析。"""
        day = _date_text(as_of)
        with self._connection() as connection:
            if reference.get("revision_id") is None:
                records = self._snapshot(connection, day, include_retracted=True)
            else:
                cutoff = _timestamp(day, end_of_day=True)
                records = []
                for (payload,) in connection.execute("""
                    SELECT o.payload FROM observations o JOIN revisions r
                    ON o.evidence_id=r.evidence_id AND o.revision_id=r.revision_id
                    WHERE o.observed_ts<=? AND o.known_ts<=? AND (r.published_ts IS NULL OR r.published_ts<=?)
                    ORDER BY o.known_ts, o.rowid
                """, (cutoff, cutoff, cutoff)):
                    record = json.loads(payload)["record"]
                    self._assert_identity(record)
                    records.append(record)
            resolved = resolve_evidence_reference(reference, records)
            return self._apply_reviews([resolved], day)[0]

    def search(self, query: str | Iterable[str], *, as_of: str | date, limit: int | None = 20,
               run_id: str | None = None, include_retracted: bool = False,
               include_superseded: bool = False) -> list[dict[str, Any]]:
        """检索截止日前的当前修订；多个 query 用 RRF，中文子串补足 FTS 分词边界。"""
        day = _date_text(as_of)
        _run_id(run_id, day)
        queries = [query] if isinstance(query, str) else list(query)
        if not queries or any(not isinstance(item, str) for item in queries):
            raise EvidenceLibraryError("query 必须为字符串或非空字符串数组")
        with self._connection() as connection:
            snapshot = self._snapshot(connection, day, run_id, include_retracted=include_retracted,
                                      include_superseded=include_superseded)
            records = {(item["evidence_id"], item["revision_id"]): item for item in snapshot}
            use_fts = connection.execute("SELECT value FROM meta WHERE key='fts'").fetchone()[0] == "1"
            streams = []
            for text in queries:
                tokens = re.findall(r"\w+", text.casefold())
                ranked = []
                if tokens and use_fts:
                    expression = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
                    rows = connection.execute("SELECT evidence_id, revision_id FROM evidence_fts WHERE evidence_fts MATCH ? ORDER BY bm25(evidence_fts)",
                                              (expression,))
                    ranked = [records[key] for row in rows if (key := (row[0], row[1])) in records]
                found = {item["evidence_id"] for item in ranked}
                fallback = [item for item in snapshot if item["evidence_id"] not in found
                            and (not tokens or all(token in _search_text(item).casefold() for token in tokens))]
                fallback.sort(key=lambda item: (-_timestamp(item["last_observed_at"]), item["evidence_id"]))
                streams.append(ranked + fallback)
        return reciprocal_rank_fusion(streams, limit=limit)

    def context(self, query: str | Iterable[str] = "", *, as_of: str | date, run_id: str | None = None,
                claims: list[dict[str, Any]] | None = None, experiments: list[dict[str, Any]] | None = None,
                max_items: int = 20, max_chars: int = 12000) -> dict[str, Any]:
        """构建供新研究复用的紧凑证据、商业主张和个人实验上下文。"""
        from aor.evidence.claims import build_evidence_packet

        # 交由证据包统一计数限额，避免先截断检索结果后隐去 omitted 数量。
        records = self.search(query, as_of=as_of, run_id=run_id, limit=None)
        return build_evidence_packet(records, claims=claims, as_of=_date_text(as_of), run_id=run_id,
                                     max_items=max_items, max_chars=max_chars, experiments=experiments)

    def delta(self, *, since: str | date, as_of: str | date, run_id: str | None = None) -> dict[str, Any]:
        """比较两个日末的事实快照；没有新结果不会被解释成需求下降。"""
        before_day, day = _date_text(since), _date_text(as_of)
        if before_day > day:
            raise EvidenceLibraryError("since 不能晚于 as_of")
        _run_id(run_id, day)
        with self._connection() as connection:
            before = {item["evidence_id"]: item for item in self._snapshot(connection, before_day, include_retracted=True)}
            after = {item["evidence_id"]: item for item in self._snapshot(connection, day, run_id, include_retracted=True)}
        added, revised, reobserved = [], [], []
        for key, item in after.items():
            previous = before.get(key)
            if previous is None:
                added.append(item)
            elif previous["revision_id"] != item["revision_id"]:
                revised.append({"evidence_id": key, "before_revision_id": previous["revision_id"],
                                "after_revision_id": item["revision_id"], "evidence": item})
            elif previous["last_observed_at"] != item["last_observed_at"]:
                reobserved.append(item)
        return {"schema_version": "3.0", "since": before_day, "as_of": day, "run_id": run_id,
                "added": added, "revised": revised, "reobserved": reobserved,
                "interpretation": "仅表示已记录证据变化，不自动推断需求或商业等级变化"}
