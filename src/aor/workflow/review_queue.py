"""本地批量语义审阅队列：精确修订、行业均衡、租约和可恢复提交。"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from zoneinfo import ZoneInfo

from aor.evidence.identity import canonical_sha256
from aor.sources.coverage import _review_status
from aor.sources.industries import industry_ids
from aor.sources.registry import EVIDENCE_ROLES
from aor.storage.evidence_library import EvidenceLibrary


class ReviewQueueError(ValueError):
    """队列输入、领取权限或修订状态不符。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(name).unlink(missing_ok=True)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ReviewQueueError(f"{label} 必须为非空文本")
    return value.strip()


def _pair(row):
    return _text(row.get("evidence_id"), "evidence_id"), _text(row.get("revision_id"), "revision_id")


def _inactive(row):
    return (row.get("historical_reference_only") or row.get("is_demo") or row.get("retracted")
            or row.get("status") in {"retracted", "withdrawn", "superseded", "deleted", "removed"}
            or row.get("derivation_status") == "superseded")


class ReviewQueue:
    """每个运行一份独立队列；文件锁覆盖领取与提交，lease 使用真实 UTC 时间。

    审阅任务只绑定 evidence_id/revision_id。原文、运行 manifest 和采集日志不被
    改写；语义结果只通过 EvidenceLibrary.register_reviews 登记。
    """

    def __init__(self, home: str | Path, run_id: str, *, library_root: str | Path | None = None, clock=None):
        if not re.fullmatch(r"RUN-\d{8}-[A-F0-9]{10}", run_id):
            raise ReviewQueueError("run_id 格式无效")
        self.home = Path(home).expanduser().resolve()
        self.run_id = run_id
        self.root = self.home / "review-queues" / run_id
        self.path = self.root / "queue.json"
        self.library = EvidenceLibrary(Path(library_root).expanduser().resolve() if library_root else self.home / "evidence-library")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise ReviewQueueError("租约时钟必须包含时区")
        return value.astimezone(timezone.utc)

    def _day(self):
        return self._now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self):
        if not self.path.exists():
            raise ReviewQueueError("队列尚未创建；先运行 plan")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if (state.get("version") != "1.0" or state.get("run_id") != self.run_id
                or state.get("library_root") != str(self.library.root)):
            raise ReviewQueueError("队列版本、运行或证据库路径不一致")
        return state

    def _save(self, state):
        state["updated_at"] = self._now().isoformat()
        _write(self.path, state)

    def _source(self, input_path):
        manifest_path = self.home / "runs" / self.run_id / "run.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        as_of = manifest.get("as_of") or datetime.strptime(self.run_id[4:12], "%Y%m%d").date().isoformat()
        if as_of > self._day():
            raise ReviewQueueError("不能提前规划未来研究日的语义审阅")
        reference = None
        if input_path is None:
            for name in ("evidence-index", "research-followup"):
                if name in manifest.get("artifacts", {}):
                    reference = manifest["artifacts"][name]
                    input_path = reference["path"]
                    break
        if input_path is None:
            rows = self.library.search("", as_of=as_of, limit=None, include_retracted=True, include_superseded=True)
            return rows, as_of, {"kind": "library_snapshot", "as_of": as_of}
        path = Path(input_path).expanduser().resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if reference and canonical_sha256(payload) != reference["sha256"]:
            raise ReviewQueueError("manifest 中的审阅索引已被修改；先通过研究工作流恢复正确产物")
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            if payload.get("run_id") and payload["run_id"] != self.run_id and payload.get("reused_for_run_id") != self.run_id:
                raise ReviewQueueError("审阅索引属于另一运行")
            if payload.get("as_of") and payload["as_of"] > as_of:
                raise ReviewQueueError("审阅索引晚于研究截止日")
            rows = next((payload[key] for key in ("items", "review_queue", "evidence") if isinstance(payload.get(key), list)), None)
        else:
            rows = None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ReviewQueueError("审阅输入必须是引用数组或含 items/review_queue/evidence 的对象")
        return rows, as_of, {"kind": "reference_file", "path": str(path), "sha256": canonical_sha256(payload)}

    def _record(self, row, hint):
        body = row.get("original_text") or row.get("text") or ""
        if not isinstance(body, str):
            raise ReviewQueueError("待审原文必须是文本")
        query = row.get("query") or row.get("query_scope") or hint.get("query") or ""
        if not isinstance(query, str):
            query = _json(query)
        questions = hint.get("questions") or ["原文是否描述目标人群的具体任务；证据角色和主张是否与原文相符"]
        if not isinstance(questions, list) or any(not isinstance(value, str) for value in questions):
            raise ReviewQueueError("questions 必须为文本数组")
        return {"evidence_id": row["evidence_id"], "revision_id": row["revision_id"],
                "source": row.get("source"), "url": row.get("original_url") or row.get("url"),
                "title": str(row.get("title") or "")[:200], "original_text": body,
                "query": query[:800], "industry_ids": industry_ids(hint) or industry_ids(row),
                "published_at": row.get("published_at"), "observed_at": row.get("observed_at"),
                "window_status": row.get("window_status", "unknown"), "evidence_role": row.get("evidence_role"),
                "evidence_kind": row.get("evidence_kind"), "language": row.get("language"),
                "questions": [value[:400] for value in questions[:6]]}

    def _sync(self, state):
        """以当前库识别已审、换版和撤回；不由 selected 或队列结果自证已审。"""
        current = {row["evidence_id"]: row for row in self.library.search(
            "", as_of=self._day(), limit=None, include_retracted=True, include_superseded=True)}
        for task in state["tasks"].values():
            row = current.get(task["evidence_id"])
            if not row or row["revision_id"] != task["revision_id"]:
                task.update(status="stale", reason="原文当前修订已变化或不可解析", lease_id=None)
            elif _inactive(row):
                task.update(status="excluded", reason="当前材料已撤回、被解析替代或属于演示", lease_id=None)
            elif _review_status(row, self._day()) in {"relevant", "unrelated"}:
                task.update(status="reviewed", reason=None, review_status=_review_status(row, self._day()), lease_id=None)
            elif task["status"] == "reviewed":
                task.update(status="pending", reason="此前审阅现已失效，需要重新核验", lease_id=None)
        for lease in state["leases"].values():
            if lease["status"] != "active":
                continue
            remaining = [task for task in state["tasks"].values() if task.get("lease_id") == lease["lease_id"]]
            if not remaining:
                lease["status"] = "completed"
            elif datetime.fromisoformat(lease["expires_at"]) <= self._now():
                lease["status"] = "expired"
                for task in remaining:
                    task.update(status="failed", reason="领取租约已过期，可由其他审阅者重新领取", lease_id=None)

    def _finish_submission(self, state, submission):
        receipt = self.library.register_reviews(submission["reviews"], known_on=submission["known_on"],
                    run_id=self.run_id if self.run_id[4:12] == submission["known_on"].replace("-", "") else None,
                    source=f"review-queue:{self.run_id}:{submission['submission_id']}")
        for result in submission["results"]:
            task = state["tasks"][result["task_id"]]
            task.update(status="reviewed" if result["status"] in {"relevant", "unrelated"} else result["status"],
                        reason=result["rationale"], result=deepcopy(result), lease_id=None)
        submission.update(status="committed", receipt={"submission_id": submission["submission_id"],
                          "submitted_count": len(submission["results"]), "reviewed_count": len(submission["reviews"]),
                          "deferred_count": len(submission["results"]) - len(submission["reviews"]),
                          "review_registry": receipt})
        self._save(state)

    def _recover(self, state):
        # 意图先于外部库写入落盘；register_reviews 按内容幂等，可补完中断的提交。
        for submission in state["submissions"].values():
            if submission["status"] == "prepared":
                self._finish_submission(state, submission)
        self._sync(state)

    def _summary(self, state):
        counts = Counter(task["status"] for task in state["tasks"].values())
        return {"version": "1.0", "run_id": self.run_id, "queue_path": str(self.path),
                "source": state["source"], "total": len(state["tasks"]),
                "counts": {key: counts[key] for key in ("pending", "leased", "reviewed", "unknown", "needs_fulltext", "failed", "stale", "excluded")},
                "unreviewed_count": sum(counts[key] for key in ("pending", "leased", "unknown", "needs_fulltext", "failed")),
                "batch_count": len(state["batches"]), "active_leases": sum(x["status"] == "active" for x in state["leases"].values()),
                "note": "领取和入包不算审阅；unknown、需补全文和失败均保持未审。"}

    def _item(self, task, text_limit=None):
        item = {"task_id": task["task_id"], **deepcopy(task["record"])}
        body = item["original_text"]
        if text_limit is not None:
            item["original_text"] = body[:text_limit]
        previous = task.get("result")
        item.update(text_truncated=len(item["original_text"]) < len(body), fulltext_chars=len(body),
                    fulltext_path=str(self.root / "records" / f"{task['task_id']}.json"),
                    fulltext_sha256=task["record_sha256"],
                    previous_outcome={"status": previous["status"], "rationale": previous["rationale"][:600]} if previous else None)
        return item

    def _packet(self, state, lease, tasks):
        packet = {"version": "1.0", "run_id": self.run_id, "batch_id": lease["batch_id"],
                  "lease_id": lease["lease_id"], "worker": lease["worker"], "expires_at": lease["expires_at"],
                  "instructions": "逐条核验原文。只能提交真实判断；不确定用 unknown，材料不足用 needs_fulltext。截断材料须读取完整文件并回传 fulltext_sha256。",
                  "items": [self._item(task) for task in tasks]}
        maximum = state["config"]["max_chars"]
        while len(_json(packet)) > maximum:
            item = max(packet["items"], key=lambda row: len(row["original_text"]))
            if not item["original_text"]:
                raise ReviewQueueError("当前字符预算不足以容纳最小引用元数据；请提高 max_chars")
            excess = len(_json(packet)) - maximum
            item["original_text"] = item["original_text"][:max(0, len(item["original_text"]) - max(excess, 100))]
            item["text_truncated"] = True
        return packet

    def plan(self, *, input_path: str | Path | None = None, max_items: int = 16, max_chars: int = 18000):
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 100:
            raise ReviewQueueError("max_items 必须为 1–100")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 2000 <= max_chars <= 200000:
            raise ReviewQueueError("max_chars 必须为 2000–200000")
        with self._locked():
            refs, as_of, source = self._source(input_path)
            state = self._load() if self.path.exists() else {"version": "1.0", "run_id": self.run_id, "as_of": as_of,
                    "library_root": str(self.library.root), "tasks": {}, "batches": {}, "leases": {}, "submissions": {}}
            if state["tasks"]:
                self._recover(state)
            if any(lease["status"] == "active" for lease in state["leases"].values()):
                raise ReviewQueueError("仍有有效领取租约；完成或释放后再重新规划")
            state.update(config={"max_items": max_items, "max_chars": max_chars}, source=source)
            current = {row["evidence_id"]: row for row in self.library.search(
                "", as_of=as_of, limit=None, include_retracted=True, include_superseded=True)}
            for ref in refs:
                identifier, revision = _pair(ref)
                task_id = "TASK-" + canonical_sha256([identifier, revision])[:20]
                if task_id in state["tasks"]:
                    continue
                row = self.library.resolve({"evidence_id": identifier, "revision_id": revision}, as_of=as_of)
                if (row["evidence_id"], row["revision_id"]) != (identifier, revision):
                    raise ReviewQueueError("审阅输入须使用迁移后准确的证据 ID 和修订，不自动改写旧引用")
                record = self._record(row, ref)
                state["tasks"][task_id] = {"task_id": task_id, "evidence_id": identifier, "revision_id": revision,
                    "record": record, "record_sha256": canonical_sha256(record), "attempts": 0, "lease_id": None,
                    "status": "pending", "reason": ref.get("reason"), "result": None}
                if not record["original_text"].strip():
                    state["tasks"][task_id].update(status="needs_fulltext", reason="库中缺少可读原文，需补充完整正文")
                _write(self.root / "records" / f"{task_id}.json", record)
                if current.get(identifier, {}).get("revision_id") != revision:
                    state["tasks"][task_id].update(status="stale", reason="输入引用不是当前修订")
            self._sync(state)
            # 先轮询行业，再按来源分散；每个精确对象只进入一个批次。
            remaining = [task for task in state["tasks"].values() if task["status"] in {"pending", "failed", "unknown", "needs_fulltext"}]
            industries, sources = Counter(), Counter()
            ordered = []
            while remaining:
                def rank(task):
                    row = task["record"]
                    sector = (row["industry_ids"] or ["unclassified"])[0]
                    return industries[sector], sources[row["source"]], task["task_id"]
                task = min(remaining, key=rank)
                remaining.remove(task)
                ordered.append(task)
                industries[(task["record"]["industry_ids"] or ["unclassified"])[0]] += 1
                sources[task["record"]["source"]] += 1
            batches, batch, size = {}, [], 1000
            for task in ordered:
                chars = len(_json(self._item(task)))
                if batch and (len(batch) >= max_items or size + chars > max_chars):
                    key = "BATCH-" + canonical_sha256(batch)[:16]
                    batches[key] = {"batch_id": key, "task_ids": batch}
                    batch, size = [], 1000
                batch.append(task["task_id"])
                size += chars
            if batch:
                key = "BATCH-" + canonical_sha256(batch)[:16]
                batches[key] = {"batch_id": key, "task_ids": batch}
            state["batches"] = batches
            self._save(state)
            return {**self._summary(state), "batches": list(batches.values())}

    def claim(self, worker: str, *, lease_seconds: int = 1800, batch_id: str | None = None,
              industries: list[str] | None = None, retry_deferred: bool = False):
        worker = _text(worker, "worker")
        if len(worker) > 120 or isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86400:
            raise ReviewQueueError("worker 最多 120 字符；lease_seconds 必须为 1–86400")
        with self._locked():
            state = self._load()
            self._recover(state)
            eligible = {"pending", "failed"} | ({"unknown", "needs_fulltext"} if retry_deferred else set())
            choices = []
            if batch_id and batch_id not in state["batches"]:
                raise ReviewQueueError("找不到指定批次")
            for batch in state["batches"].values():
                if batch_id and batch["batch_id"] != batch_id:
                    continue
                tasks = [state["tasks"][key] for key in batch["task_ids"] if state["tasks"][key]["status"] in eligible
                         and (not industries or set(industries) & set(state["tasks"][key]["record"]["industry_ids"]))]
                if tasks:
                    choices.append((min(task["attempts"] for task in tasks), batch["batch_id"], tasks))
            if not choices:
                self._save(state)
                return {**self._summary(state), "claimed": False, "reason": "没有符合行业与状态的可领取任务"}
            _, selected_batch, tasks = min(choices, key=lambda item: item[:2])
            lease_id = "LEASE-" + uuid.uuid4().hex
            lease = {"lease_id": lease_id, "batch_id": selected_batch, "worker": worker, "status": "active",
                     "created_at": self._now().isoformat(), "expires_at": (self._now() + timedelta(seconds=lease_seconds)).isoformat(),
                     "task_ids": [task["task_id"] for task in tasks]}
            packet = self._packet(state, lease, tasks)
            lease["truncated_tasks"] = [row["task_id"] for row in packet["items"] if row["text_truncated"]]
            for task in tasks:
                task.update(status="leased", lease_id=lease_id, attempts=task["attempts"] + 1)
            state["leases"][lease_id] = lease
            packet_path = self.root / "claims" / f"{lease_id}.json"
            _write(packet_path, packet)
            self._save(state)
            return {**self._summary(state), "claimed": True, "lease_id": lease_id, "batch_id": selected_batch,
                    "worker": worker, "expires_at": lease["expires_at"], "item_count": len(tasks),
                    "packet_chars": len(_json(packet)), "packet_path": str(packet_path)}

    def submit(self, lease_id: str, worker: str, submission_id: str, results: list[dict]):
        worker, lease_id = _text(worker, "worker"), _text(lease_id, "lease_id")
        if not isinstance(submission_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", submission_id):
            raise ReviewQueueError("submission_id 使用 1–100 位字母、数字、下划线、点或横线")
        if not isinstance(results, list) or not results or any(not isinstance(item, dict) for item in results):
            raise ReviewQueueError("results 必须是非空结果数组")
        digest = canonical_sha256({"lease_id": lease_id, "worker": worker, "results": results})
        with self._locked():
            state = self._load()
            existing = state["submissions"].get(submission_id)
            if existing and existing["sha256"] != digest:
                raise ReviewQueueError("同一 submission_id 不能改换内容、领取者或租约")
            self._recover(state)
            if existing:
                self._save(state)
                return {**deepcopy(existing["receipt"]), "idempotent": True, "progress": self._summary(state)}
            lease = state["leases"].get(lease_id)
            if not lease or lease["worker"] != worker or lease["status"] != "active":
                raise ReviewQueueError("租约不存在、已过期或不属于该审阅者，请重新领取")
            normalized, reviews, seen = [], [], set()
            for result in results:
                pair = _pair(result)
                task_id = "TASK-" + canonical_sha256(list(pair))[:20]
                task = state["tasks"].get(task_id)
                if task_id in seen or not task or task.get("lease_id") != lease_id:
                    raise ReviewQueueError("提交包含重复任务、未领取任务或错误的原文修订")
                seen.add(task_id)
                status = result.get("status")
                if status not in {"relevant", "unrelated", "unknown", "needs_fulltext", "failed"}:
                    raise ReviewQueueError("结果只允许 relevant/unrelated/unknown/needs_fulltext/failed")
                rationale = _text(result.get("rationale") or result.get("reason"), "rationale/reason")
                checked = {"task_id": task_id, "evidence_id": pair[0], "revision_id": pair[1], "status": status, "rationale": rationale}
                if status in {"relevant", "unrelated"}:
                    if not task["record"]["original_text"].strip():
                        raise ReviewQueueError("库中缺少原文，不能将缺失材料登记为已审")
                    if task_id in lease["truncated_tasks"] and result.get("fulltext_sha256") != task["record_sha256"]:
                        raise ReviewQueueError("原文已截断；读完整文件后回传 fulltext_sha256，或提交 needs_fulltext")
                    if task_id in lease["truncated_tasks"]:
                        fulltext = json.loads((self.root / "records" / f"{task_id}.json").read_text(encoding="utf-8"))
                        if canonical_sha256(fulltext) != task["record_sha256"]:
                            raise ReviewQueueError("完整文本文件已被修改，不能据此登记审阅")
                    if result.get("reviewer", worker) != worker:
                        raise ReviewQueueError("审阅者须与领取 worker 一致")
                    review = {key: checked[key] for key in ("evidence_id", "revision_id", "status", "rationale")}
                    review.update(reviewer=worker, reviewed_at=_text(result.get("reviewed_at"), "reviewed_at"))
                    if result.get("evidence_role") is not None:
                        if result["evidence_role"] not in EVIDENCE_ROLES:
                            raise ReviewQueueError("证据角色不在已登记目录中")
                        review["evidence_role"] = result["evidence_role"]
                    self.library._checked_review(review, self._day())
                    current = self.library.resolve({"evidence_id": pair[0]}, as_of=self._day())
                    if current["revision_id"] != pair[1] or _inactive(current) or current.get("derivation_status") == "needs_review":
                        raise ReviewQueueError("当前原文已变化、失效或解析来源待核验，不能登记已审")
                    reviews.append(review)
                normalized.append(checked)
            submission = {"submission_id": submission_id, "sha256": digest, "lease_id": lease_id,
                          "worker": worker, "status": "prepared", "known_on": self._day(), "results": normalized, "reviews": reviews}
            state["submissions"][submission_id] = submission
            self._save(state)
            self._finish_submission(state, submission)
            self._sync(state)
            self._save(state)
            return {**deepcopy(submission["receipt"]), "idempotent": False, "progress": self._summary(state)}

    def release(self, lease_id: str, worker: str, *, reason: str):
        worker, reason, lease_id = _text(worker, "worker"), _text(reason, "reason"), _text(lease_id, "lease_id")
        with self._locked():
            state = self._load()
            self._recover(state)
            lease = state["leases"].get(lease_id)
            if not lease or lease["worker"] != worker:
                raise ReviewQueueError("租约不存在或不属于该审阅者")
            for task in state["tasks"].values():
                if task.get("lease_id") == lease_id:
                    task.update(status="failed", reason=reason, lease_id=None)
            if lease["status"] == "active":
                lease.update(status="released", release_reason=reason)
            self._save(state)
            return self._summary(state)

    def status(self):
        with self._locked():
            state = self._load()
            self._recover(state)
            self._save(state)
            return self._summary(state)
