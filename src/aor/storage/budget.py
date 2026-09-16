"""DATA_HOME 内共享的保守费用账本；记录估算占用，不冒充供应商账单。

付费日志把本库 ATTACH 到磁盘 SQLite 主库，两者在 DELETE/FULL 模式下
同一事务提交。金额使用 Decimal 字符串；日/月按真实执行的北京时间计算。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class BudgetError(ValueError):
    """全局预算、身份或恢复边界不满足，不能发送请求。"""


def _amount(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise BudgetError("费用必须是非负有限十进制金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetError("费用必须是非负有限十进制金额") from exc
    if not result.is_finite() or result < 0:
        raise BudgetError("费用必须是非负有限十进制金额")
    return result


def _time(value: str | datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if isinstance(result, str):
        try:
            result = datetime.fromisoformat(result.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BudgetError("费用时间必须是带时区的 ISO 时间") from exc
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise BudgetError("费用时间必须是带时区的 ISO 时间")
    return result.astimezone(ZoneInfo("Asia/Shanghai"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def budget_path(home: str | Path) -> Path:
    return Path(home).expanduser().resolve() / "state" / "budget.sqlite3"


def attempt_key(run_id: str, fingerprint: str, attempt_number: int) -> str:
    return "attempt:" + hashlib.sha256(_json([run_id, fingerprint, attempt_number]).encode()).hexdigest()


class BudgetStore:
    """独立配置/查询，或绑定 RequestJournal 的同一个 SQLite 连接。"""

    def __init__(self, home: str | Path):
        self.path = budget_path(home)
        self.db: sqlite3.Connection | None = None
        self.schema = "main"
        self.owns_connection = True

    def __enter__(self) -> BudgetStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            self.db.close()
            raise BudgetError("全局预算库必须使用 DELETE journal_mode，不能使用 WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self._initialize()
        return self

    def __exit__(self, *args: Any) -> None:
        if self.db is not None and self.owns_connection:
            self.db.close()

    @classmethod
    def attach(cls, db: sqlite3.Connection, home: str | Path) -> BudgetStore:
        # 主库必须落盘；内存主库或 WAL 无法保证跨库崩溃原子性。
        main_path = db.execute("PRAGMA database_list").fetchone()[2]
        if not main_path or db.execute("PRAGMA main.journal_mode").fetchone()[0] != "delete":
            raise BudgetError("跨运行预算要求磁盘请求日志及 DELETE journal_mode")
        if Path(main_path).resolve() == budget_path(home):
            raise BudgetError("请求日志不能与全局预算库使用同一文件")
        with cls(home):
            pass
        store = cls(home)
        db.execute("ATTACH DATABASE ? AS aor_budget", (str(store.path),))
        if db.execute("PRAGMA aor_budget.journal_mode").fetchone()[0] != "delete":
            raise BudgetError("全局预算库必须使用 DELETE journal_mode")
        db.execute("PRAGMA main.synchronous=FULL")
        db.execute("PRAGMA aor_budget.synchronous=FULL")
        store.db, store.schema, store.owns_connection = db, "aor_budget", False
        return store

    def _initialize(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS budget_policy (
                id INTEGER PRIMARY KEY CHECK(id=1), limits_json TEXT NOT NULL,
                recurring_enabled INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budget_entries (
                reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                attempt_number INTEGER NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
                list_cost_usd TEXT NOT NULL, estimated_cost_usd TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, settled_at TEXT, recurring INTEGER NOT NULL,
                origin TEXT NOT NULL, source_details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS budget_entry_period ON budget_entries(day, month);
            CREATE INDEX IF NOT EXISTS budget_entry_run ON budget_entries(run_id);
        """)

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _table(self, name: str) -> str:
        return f"{self.schema}.{name}"

    def policy(self) -> dict[str, Any]:
        row = self.db.execute(f"SELECT * FROM {self._table('budget_policy')} WHERE id=1").fetchone()
        if row is None:
            return {"configured": False, "limits": dict.fromkeys(("daily", "monthly", "total")),
                    "recurring_enabled": False, "paused": False, "pause_reason": None}
        return {"configured": True, "limits": json.loads(row["limits_json"]),
                "recurring_enabled": bool(row["recurring_enabled"]), "paused": bool(row["paused"]),
                "pause_reason": row["pause_reason"]}

    def configure(self, *, daily_limit_usd: Any = None, monthly_limit_usd: Any = None,
                  total_limit_usd: Any = None, recurring_enabled: bool = False) -> dict[str, Any]:
        limits = {key: None if value is None else str(_amount(value)) for key, value in
                  (("daily", daily_limit_usd), ("monthly", monthly_limit_usd), ("total", total_limit_usd))}
        if type(recurring_enabled) is not bool or (recurring_enabled and any(v is None for v in limits.values())):
            raise BudgetError("持续预算必须显式提供有限日、月、累计上限")
        with self.transaction():
            # 调整额度不会顺便解除失败暂停。
            self.db.execute(f"""INSERT INTO {self._table('budget_policy')}
                (id,limits_json,recurring_enabled,updated_at) VALUES (1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET limits_json=excluded.limits_json,
                recurring_enabled=excluded.recurring_enabled,updated_at=excluded.updated_at""",
                            (_json(limits), int(recurring_enabled), _time().isoformat()))
        return self.snapshot()

    def _pending_attempts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            f"SELECT run_id,fingerprint,attempt_number,list_cost_usd FROM {self._table('budget_entries')} "
            "WHERE state='reserved' ORDER BY created_at,reservation_id"
        ).fetchall()]

    def require_authorization(self, *, recurring: bool, recovery_run_id: str | None = None) -> None:
        """恢复预检可延后本 RUN 的未决检查；真实预留不得提供 recovery_run_id。"""
        policy = self.policy()
        pending = self._pending_attempts()
        recovering = recovery_run_id is not None and any(row["run_id"] == recovery_run_id for row in pending)
        if policy["paused"] and not recovering:
            raise BudgetError("共享预算已暂停；核对失败/未知请求后显式 budget-resume，不会自动重试")
        if recurring and (not policy["recurring_enabled"] or any(v is None for v in policy["limits"].values())):
            raise BudgetError("尚未配置并启用持续预算；一次性 max-cost-usd 不授权持续支出")
        if recurring and pending and not recovering:
            raise BudgetError("共享预算有未结算请求；等待执行结束，或使用原 journal 恢复为未知后核对，不能开启新的持续请求")

    def pause(self, reason: str) -> None:
        with self.transaction():
            if not self.policy()["configured"]:
                self.configure()
            self.db.execute(f"UPDATE {self._table('budget_policy')} SET paused=1,pause_reason=?,updated_at=? WHERE id=1",
                            (reason, _time().isoformat()))

    def resume(self, *, acknowledge_pause: bool) -> dict[str, Any]:
        if acknowledge_pause is not True:
            raise BudgetError("恢复预算需要显式确认已核对暂停原因")
        with self.transaction():
            if self._pending_attempts():
                raise BudgetError("仍有未结算请求；先等待执行结束或恢复原 journal，budget-resume 不会释放费用或改写在途请求")
            self.db.execute(f"UPDATE {self._table('budget_policy')} SET paused=0,pause_reason=NULL,updated_at=? WHERE id=1",
                            (_time().isoformat(),))
        return self.snapshot()

    def snapshot(self, *, now: str | datetime | None = None) -> dict[str, Any]:
        observed = _time(now)
        day, month = observed.date().isoformat(), observed.strftime("%Y-%m")
        rows = self.db.execute(f"SELECT * FROM {self._table('budget_entries')}").fetchall()
        totals = {"daily": Decimal(0), "monthly": Decimal(0), "total": Decimal(0)}
        estimated, states, by_run = Decimal(0), {}, {}
        for row in rows:
            amount = Decimal(row["list_cost_usd"])
            totals["total"] += amount
            if row["day"] == day:
                totals["daily"] += amount
            if row["month"] == month:
                totals["monthly"] += amount
            estimated += Decimal(row["estimated_cost_usd"])
            states[row["state"]] = states.get(row["state"], 0) + 1
            by_run[row["run_id"]] = by_run.get(row["run_id"], Decimal(0)) + amount
        policy = self.policy()
        pending = self._pending_attempts()
        return {"schema_version": "1.0", "scope": "data_home", "configured": policy["configured"], "timezone": "Asia/Shanghai",
                "day": day, "month": month, "policy": policy, "entry_count": len(rows), "states": states,
                "occupied_usd": {k: str(v) for k, v in totals.items()},
                "remaining_usd": {k: None if policy["limits"][k] is None else
                                  str(max(Decimal(0), Decimal(policy["limits"][k]) - value)) for k, value in totals.items()},
                "estimated_total_usd": str(estimated), "by_run_usd": {k: str(v) for k, v in by_run.items()},
                "pending_attempts": pending,
                "recurring_block_reason": "unsettled_attempts" if pending else "paused" if policy["paused"] else None,
                "authorization_scope": "configured_recurring" if policy["recurring_enabled"] else "explicit_one_shot_only",
                "history_scope": "仅已登记尝试和显式导入的历史估算；未导入历史不包含在内",
                "billing_note": "保守费用占用，包含失败和未知；非供应商账单"}

    def reserve(self, *, run_id: str, fingerprint: str, attempt_number: int, list_cost_usd: Any,
                estimated_cost_usd: Any, recurring: bool = False, now: str | datetime | None = None,
                historical: bool = False) -> bool:
        if not run_id or not fingerprint or type(attempt_number) is not int or attempt_number < 1:
            raise BudgetError("预留需要运行、请求指纹和正整数尝试序号")
        amount, estimated = _amount(list_cost_usd), _amount(estimated_cost_usd)
        observed = _time(now)
        key = attempt_key(run_id, fingerprint, attempt_number)
        with self.transaction():
            existing = self.db.execute(f"SELECT * FROM {self._table('budget_entries')} WHERE reservation_id=?", (key,)).fetchone()
            if existing:
                if Decimal(existing["list_cost_usd"]) != amount or Decimal(existing["estimated_cost_usd"]) != estimated:
                    raise BudgetError("同一尝试的费用与全局记录冲突")
                return False
            if not historical:
                self.require_authorization(recurring=recurring)
                report = self.snapshot(now=observed)
                for period, limit in report["policy"]["limits"].items():
                    if limit is not None and Decimal(report["occupied_usd"][period]) + amount > Decimal(limit):
                        raise BudgetError(f"共享 {period} 预算不足，已在发送前停止")
            self.db.execute(f"INSERT INTO {self._table('budget_entries')} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key, run_id, fingerprint, attempt_number, observed.date().isoformat(), observed.strftime("%Y-%m"),
                             str(amount), str(estimated), "reserved", observed.isoformat(), None, int(recurring),
                             "journal_import" if historical else "live_attempt", "{}"))
        return True

    def settle(self, *, run_id: str, fingerprint: str, attempt_number: int, state: str) -> None:
        if state not in {"succeeded", "failed", "outcome_unknown"}:
            raise BudgetError("未知/失败也必须保留原价占用，不允许自动退款")
        with self.transaction():
            key = attempt_key(run_id, fingerprint, attempt_number)
            row = self.db.execute(f"SELECT state,recurring FROM {self._table('budget_entries')} WHERE reservation_id=?", (key,)).fetchone()
            if row is None:
                raise BudgetError("不能结算没有预留的请求")
            if row["state"] == state:
                return
            if row["state"] != "reserved" and row["state"] != state:
                raise BudgetError("已结算尝试不能改写结果")
            self.db.execute(f"UPDATE {self._table('budget_entries')} SET state=?,settled_at=? WHERE reservation_id=?",
                            (state, _time().isoformat(), key))
            if row["recurring"] and state in {"failed", "outcome_unknown"}:
                self.pause("paid_request_" + state)

    def has_history(self, run_id: str) -> bool:
        return self.history_coverage(run_id) is not None

    def history_coverage(self, run_id: str) -> Decimal | None:
        row = self.db.execute(f"SELECT list_cost_usd FROM {self._table('budget_entries')} WHERE reservation_id=?",
                              ("history:" + run_id,)).fetchone()
        return Decimal(row[0]) if row is not None else None

    def has_attempt(self, run_id: str, fingerprint: str, attempt_number: int) -> bool:
        return self.db.execute(f"SELECT 1 FROM {self._table('budget_entries')} WHERE reservation_id=?",
                               (attempt_key(run_id, fingerprint, attempt_number),)).fetchone() is not None

    def import_history(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """显式导入已发生的整轮估算；来源哈希/内容不一致或逐次记录重叠时拒绝。"""
        if not isinstance(entries, list) or not entries:
            raise BudgetError("历史导入需要非空 entries 数组")
        with self.transaction():
            for item in entries:
                if not isinstance(item, dict):
                    raise BudgetError("历史费用条目必须为对象")
                from contracts import validate_run_id

                run_id = validate_run_id(item.get("run_id"))
                if not isinstance(item.get("occurred_at"), str) or not item["occurred_at"].strip():
                    raise BudgetError("历史估算必须有实际发生时间 occurred_at")
                amount, observed = _amount(item.get("amount_usd")), _time(item["occurred_at"])
                if observed > _time():
                    raise BudgetError("不能导入尚未发生的历史费用")
                digest, refs = item.get("source_sha256"), item.get("source_refs")
                if (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                        or not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs)):
                    raise BudgetError("历史估算必须附来源 SHA256 与非空 source_refs")
                detail = _json({"source_sha256": digest, "source_refs": refs, "billing_status": "estimated_history_non_invoice"})
                key = "history:" + run_id
                old = self.db.execute(f"SELECT * FROM {self._table('budget_entries')} WHERE reservation_id=?", (key,)).fetchone()
                if old:
                    if (Decimal(old["list_cost_usd"]) != amount or old["source_details"] != detail
                            or old["created_at"] != observed.isoformat()):
                        raise BudgetError("历史估算与已导入版本冲突")
                    continue
                if self.db.execute(f"SELECT 1 FROM {self._table('budget_entries')} WHERE run_id=?", (run_id,)).fetchone():
                    raise BudgetError("该 RUN 已登记逐次费用，不能再叠加整轮历史估算")
                self.db.execute(f"INSERT INTO {self._table('budget_entries')} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (key, run_id, "", 0, observed.date().isoformat(), observed.strftime("%Y-%m"),
                                 str(amount), str(amount), "historical_estimate", observed.isoformat(), observed.isoformat(),
                                 0, "explicit_history_import", detail))
        return self.snapshot()


def budget_report(home: str | Path) -> dict[str, Any]:
    """不存在时只返回未配置状态；报告命令不会初始化真实目录。"""
    path = budget_path(home)
    if not path.exists():
        store = BudgetStore(home)
        store.db = sqlite3.connect(":memory:")
        store.db.row_factory = sqlite3.Row
        try:
            store._initialize()
            return {**store.snapshot(), "history_scope": "尚无共享账本，历史支出未知"}
        finally:
            store.db.close()
    store = BudgetStore(home)
    store.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    store.db.row_factory = sqlite3.Row
    try:
        return store.snapshot()
    finally:
        store.db.close()
