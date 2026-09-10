"""追加式证据观察日志及可删除重建的 SQLite 检索索引。"""

from __future__ import annotations

import fcntl
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

from aor.evidence.identity import canonical_evidence_url, canonical_sha256, normalize_identity
from aor.evidence.retrieval import reciprocal_rank_fusion


TZ = ZoneInfo("Asia/Shanghai")
COLLECTION_FIELDS = {
    "id", "evidence_id", "revision_id", "version", "state_revision_id", "supersedes", "source", "source_labels", "run_id", "run_ids",
    "as_of", "observed_at", "first_observed_at", "last_observed_at", "recorded_on", "reused_for_run_id",
    "raw_file", "raw_ref", "raw_refs", "raw_json_pointer", "query", "query_id", "query_group", "engagement",
    "access_method", "extraction_warnings", "retrieval", "aliases", "same_source_refs", "independent_source_key",
    "schema_version", "canonical_url", "content_hash", "library_evidence_id",
}


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
        # 原始 URL 合并转载，发布主体只标注同源性，不把不同内容合成一条。
        library_id = "EVID-" + canonical_sha256(canonical_url)[:16].upper()
        aliases = sorted({str(value) for value in (item.get("id"), item.get("evidence_id"), library_id) if value})
        content = {key: value for key, value in item.items() if key not in COLLECTION_FIELDS}
        content["url"] = canonical_url
        if "original_url" in content:
            content["original_url"] = canonical_url
        content_hash = canonical_sha256(content)
        revision = item.get("revision_id") or f"{library_id}:{content_hash[:16]}"
        if not isinstance(revision, str) or not revision.strip():
            raise EvidenceLibraryError("revision_id 必须为非空字符串")
        item.update(evidence_id=library_id, library_evidence_id=library_id, revision_id=revision,
                    content_hash=content_hash, canonical_url=canonical_url, observed_at=observed, aliases=aliases,
                    independent_source_key=f"publisher:{publisher}" if publisher else f"host:{urlparse(canonical_url).hostname}")
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

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, as_of: str, run_id: str | None = None,
                  *, include_retracted: bool = False) -> list[dict[str, Any]]:
        cutoff = _timestamp(as_of, end_of_day=True)
        rows = connection.execute("""
            SELECT o.payload FROM observations o JOIN revisions r
            ON o.evidence_id=r.evidence_id AND o.revision_id=r.revision_id
            WHERE o.observed_ts<=? AND o.known_ts<=? AND (r.published_ts IS NULL OR r.published_ts<=?)
            ORDER BY o.known_ts, o.observed_ts, o.rowid
        """, (cutoff, cutoff, cutoff))
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            event = json.loads(row[0])
            grouped.setdefault(event["record"]["evidence_id"], []).append(event)
        records = []
        for events in grouped.values():
            latest = events[-1]
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
            item["aliases"] = sorted({alias for event in revision_events for alias in event["record"]["aliases"]})
            item.pop("reused_for_run_id", None)
            if run_id is not None and run_id not in item["run_ids"]:
                item["reused_for_run_id"] = run_id
            records.append(item)
        return records

    def search(self, query: str | Iterable[str], *, as_of: str | date, limit: int | None = 20,
               run_id: str | None = None, include_retracted: bool = False) -> list[dict[str, Any]]:
        """检索截止日前的当前修订；多个 query 用 RRF，中文子串补足 FTS 分词边界。"""
        day = _date_text(as_of)
        _run_id(run_id, day)
        queries = [query] if isinstance(query, str) else list(query)
        if not queries or any(not isinstance(item, str) for item in queries):
            raise EvidenceLibraryError("query 必须为字符串或非空字符串数组")
        with self._connection() as connection:
            snapshot = self._snapshot(connection, day, run_id, include_retracted=include_retracted)
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
