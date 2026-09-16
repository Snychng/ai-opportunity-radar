"""逐请求 SQLite 日志，也是同一研究 run 的跨批费用账本。

每个 started 先提交费用占用；成功/失败立即提交。旁路 SQLite 锁覆盖整个
执行会话，进程退出自动释放，避免另一执行器把仍在运行的请求误判为中断。
传入的 metadata/result 必须已脱敏；金额始终保存 Decimal 字符串。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from aor.storage.budget import BudgetError, BudgetStore


class JournalError(ValueError):
    """日志身份冲突、并行执行或不合法状态转换。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class RequestJournal:
    """一个路径绑定一个 run；path=None 时保留相同的内存执行语义。"""

    def __init__(self, path: str | Path | None, *, run_id: str, as_of: str,
                 budget_home: str | Path | None = None, recurring: bool = False):
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self.run_id = run_id
        self.as_of = as_of
        self.connection: sqlite3.Connection | None = None
        self.lock: sqlite3.Connection | None = None
        self.budget_home = budget_home
        self.recurring = recurring
        self.budget: BudgetStore | None = None

    def __enter__(self) -> RequestJournal:
        try:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.lock = sqlite3.connect(str(self.path) + ".lock", timeout=0)
                self.lock.execute("BEGIN EXCLUSIVE")
            self.connection = sqlite3.connect(str(self.path) if self.path else ":memory:")
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS run (run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    fingerprint TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_requests (
                    batch_id TEXT NOT NULL, fingerprint TEXT NOT NULL, metadata TEXT NOT NULL,
                    PRIMARY KEY (batch_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, batch_id TEXT NOT NULL,
                    state TEXT NOT NULL, list_cost_usd TEXT NOT NULL, estimated_cost_usd TEXT NOT NULL,
                    pricing_snapshot TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT
                );
            """)
            existing = self.connection.execute("SELECT * FROM run").fetchone()
            if existing and (existing["run_id"], existing["as_of"]) != (self.run_id, self.as_of):
                raise JournalError("journal 已绑定其他 run_id/as_of")
            with self.connection:
                self.connection.execute("INSERT OR IGNORE INTO run VALUES (?, ?)", (self.run_id, self.as_of))
            if self.budget_home is not None:
                self.budget = BudgetStore.attach(self.connection, self.budget_home)
                with self._transaction():
                    self._synchronize_budget()
            return self
        except BaseException as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, sqlite3.Error):
                raise JournalError("无法打开 journal；文件无效或已有执行器占用") from exc
            raise

    def __exit__(self, *args: Any) -> None:
        if (args and args[0] is not None and self.budget is not None and self.recurring
                and self.connection.execute("SELECT 1 FROM attempts WHERE state='started'").fetchone()):
            # 正常异常退出可记录暂停；进程被强杀时 reserved 仍保守占用。
            try:
                self.budget.pause("interrupted_paid_execution")
            except (sqlite3.Error, BudgetError):
                pass
        if self.connection is not None:
            self.connection.close()
        if self.lock is not None:
            self.lock.close()

    @contextmanager
    def _transaction(self):
        # BEGIN IMMEDIATE 同时锁住已附加的全局库。事务涵盖费用与本地尝试。
        with self.budget.transaction() if self.budget is not None else self.connection:
            yield

    def _synchronize_budget(self) -> None:
        if self.budget is None:
            return
        history_coverage = self.budget.history_coverage(self.run_id)
        ordinals: dict[str, int] = {}
        pending_sync: list[tuple[Any, int]] = []
        uncovered = Decimal(0)
        for row in self.connection.execute("SELECT * FROM attempts ORDER BY id").fetchall():
            fingerprint = row["fingerprint"]
            ordinals[fingerprint] = ordinals.get(fingerprint, 0) + 1
            ordinal = ordinals[fingerprint]
            if history_coverage is not None and not self.budget.has_attempt(self.run_id, fingerprint, ordinal):
                uncovered += Decimal(row["list_cost_usd"])
            else:
                pending_sync.append((row, ordinal))
        if history_coverage is not None and uncovered > history_coverage:
            raise BudgetError("本地 journal 已尝试费用超过已导入历史覆盖额；请核对并补账，不能忽略新增费用或自动叠加整轮历史")
        for row, ordinal in pending_sync:
            fingerprint = row["fingerprint"]
            self.budget.reserve(run_id=self.run_id, fingerprint=fingerprint, attempt_number=ordinal,
                                list_cost_usd=row["list_cost_usd"], estimated_cost_usd=row["estimated_cost_usd"],
                                now=row["started_at"], recurring=self.recurring, historical=True)
            if row["state"] != "started":
                self.budget.settle(run_id=self.run_id, fingerprint=fingerprint,
                                   attempt_number=ordinal, state=row["state"])

    def register_batch(
        self, batch_id: str, plan_sha256: str, requests: list[dict[str, Any]], *, resume: bool = False,
    ) -> None:
        """新批显式命名；恢复要求批次与完整计划摘要同时相同。"""
        if not isinstance(batch_id, str) or not batch_id.strip() or len(batch_id) > 120:
            raise JournalError("batch_id 必须为 1 到 120 字符")
        db = self.connection
        existing = db.execute("SELECT plan_sha256 FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if resume and not existing:
            raise JournalError("resume 找不到已登记批次")
        if existing and not resume:
            raise JournalError("批次已登记；恢复请使用 resume，新补证批请指定新的 batch_id")
        if existing and existing[0] != plan_sha256:
            raise JournalError("恢复计划内容与 journal 不符；请显式建立新批次")
        with self._transaction():
            db.execute("INSERT OR IGNORE INTO batches VALUES (?, ?, ?)", (batch_id, plan_sha256, _now()))
            for item in requests:
                fingerprint = item["request_fingerprint"]
                db.execute("INSERT OR IGNORE INTO requests VALUES (?, 'planned', NULL, ?)", (fingerprint, _now()))
                db.execute("INSERT OR IGNORE INTO batch_requests VALUES (?, ?, ?)", (batch_id, fingerprint, _json(item)))
            # 会话锁已取得，因此上次遗留 started 必定不再有本地执行者。
            db.execute("UPDATE attempts SET state='outcome_unknown', finished_at=? WHERE state='started'", (_now(),))
            db.execute("UPDATE requests SET state='outcome_unknown', updated_at=? WHERE state='started'", (_now(),))
            self._synchronize_budget()

    def get_request(self, fingerprint: str) -> dict[str, Any]:
        row = dict(self.connection.execute("SELECT * FROM requests WHERE fingerprint=?", (fingerprint,)).fetchone())
        row["result"] = json.loads(row["result"]) if row["result"] else None
        row["attempts"] = self.connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE fingerprint=?", (fingerprint,),
        ).fetchone()[0]
        return row

    def snapshot(self) -> dict[str, Any]:
        """金额包含成功、失败、未知以及尚在执行的所有尝试，不能当作已对账账单。"""
        # 来源取尝试所属批次的请求元数据，不能从可能尚未写出的结果反推。
        # 同时关联批次和指纹，防止跨批复用将一条历史尝试重复计入。
        rows = self.connection.execute("""
            SELECT attempts.*, batch_requests.metadata
            FROM attempts LEFT JOIN batch_requests
                ON attempts.batch_id = batch_requests.batch_id
                AND attempts.fingerprint = batch_requests.fingerprint
        """).fetchall()
        list_cost = sum((Decimal(row["list_cost_usd"]) for row in rows), Decimal(0))
        estimated_cost = sum((Decimal(row["estimated_cost_usd"]) for row in rows), Decimal(0))
        states = dict(self.connection.execute("SELECT state, COUNT(*) FROM requests GROUP BY state").fetchall())
        attempt_states = dict.fromkeys(("succeeded", "failed", "outcome_unknown", "started"), 0)
        by_source: dict[str, dict[str, Any]] = {}
        for row in rows:
            source = json.loads(row["metadata"])["source"] if row["metadata"] else "unknown"
            if source not in by_source:
                by_source[source] = {
                    "source": source, "attempts": 0, **dict.fromkeys(attempt_states, 0),
                    "list_attempted_cost_usd_exact": Decimal(0),
                    "estimated_attempted_cost_usd_exact": Decimal(0),
                }
            attempt_states[row["state"]] += 1
            source_row = by_source[source]
            source_row["attempts"] += 1
            source_row[row["state"]] += 1
            source_row["list_attempted_cost_usd_exact"] += Decimal(row["list_cost_usd"])
            source_row["estimated_attempted_cost_usd_exact"] += Decimal(row["estimated_cost_usd"])
        for source_row in by_source.values():
            for field in ("list_attempted_cost_usd_exact", "estimated_attempted_cost_usd_exact"):
                source_row[field] = format(source_row[field], "f")
        return {
            "run_id": self.run_id, "as_of": self.as_of,
            "attempts": len(rows), "request_states": states,
            "attempt_states": attempt_states,
            "by_source": [by_source[source] for source in sorted(by_source)],
            "batch_count": self.connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
            "list_attempted_cost_usd_exact": format(list_cost, "f"),
            "estimated_attempted_cost_usd_exact": format(estimated_cost, "f"),
        }

    def start_attempt(
        self, fingerprint: str, *, batch_id: str, list_cost_usd: Decimal,
        estimated_cost_usd: Decimal, pricing_snapshot: dict[str, Any],
        max_cost_usd: Decimal, max_attempts: int,
    ) -> int:
        """先持久化 started 与预算占用，返回本次 attempt id。"""
        with self._transaction():
            current = self.get_request(fingerprint)
            if current["state"] in {"started", "succeeded"} or current["attempts"] >= max_attempts:
                raise JournalError("请求不可再次开始或已达到累计 max_attempts")
            occupied = Decimal(self.snapshot()["list_attempted_cost_usd_exact"])
            if occupied + list_cost_usd > max_cost_usd:
                raise JournalError("本次尝试超过 run 累计预算")
            if self.budget is not None and not self.budget.reserve(
                run_id=self.run_id, fingerprint=fingerprint, attempt_number=current["attempts"] + 1,
                list_cost_usd=list_cost_usd, estimated_cost_usd=estimated_cost_usd, recurring=self.recurring,
            ):
                raise BudgetError("全局账本已登记此尝试，当前本地日志不匹配；不能再次发送")
            cursor = self.connection.execute(
                "INSERT INTO attempts (fingerprint,batch_id,state,list_cost_usd,estimated_cost_usd,"
                "pricing_snapshot,started_at) VALUES (?,?,'started',?,?,?,?)",
                (fingerprint, batch_id, str(list_cost_usd), str(estimated_cost_usd), _json(pricing_snapshot), _now()),
            )
            self.connection.execute(
                "UPDATE requests SET state='started', result=NULL, updated_at=? WHERE fingerprint=?",
                (_now(), fingerprint),
            )
            return cursor.lastrowid

    def finish_attempt(self, attempt_id: int, *, state: str, result: dict[str, Any]) -> None:
        """result 为脱敏产物，保存失败会保留 started 供恢复时按未知处理。"""
        if state not in {"succeeded", "failed", "outcome_unknown"}:
            raise JournalError("不合法的请求完成状态")
        with self._transaction():
            row = self.connection.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["state"] != "started":
                raise JournalError("只能完成正在执行的尝试")
            if self.budget is not None:
                ordinal = self.connection.execute("SELECT COUNT(*) FROM attempts WHERE fingerprint=? AND id<=?",
                                                  (row["fingerprint"], attempt_id)).fetchone()[0]
                self.budget.settle(run_id=self.run_id, fingerprint=row["fingerprint"], attempt_number=ordinal, state=state)
            self.connection.execute("UPDATE attempts SET state=?, finished_at=? WHERE id=?", (state, _now(), attempt_id))
            self.connection.execute(
                "UPDATE requests SET state=?, result=?, updated_at=? WHERE fingerprint=?",
                (state, _json(result), _now(), row["fingerprint"]),
            )


def batch_registered(path: str | Path, batch_id: str) -> bool:
    """计划落盘不代表执行器已登记批次；预检失败后据真实 journal 决定恢复方式。"""
    path = Path(path).expanduser().resolve()
    if not path.exists():
        return False
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone() is not None


def read_run_ledger(path: str | Path) -> dict[str, Any]:
    """只读账本，供 runner 读取跨批累计金额；不恢复状态，也不发起网络请求。"""
    path = Path(path).expanduser().resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        run = db.execute("SELECT run_id, as_of FROM run").fetchone()
        journal = RequestJournal(None, run_id=run[0], as_of=run[1])
        journal.connection = db
        db.row_factory = sqlite3.Row
        return journal.snapshot()
