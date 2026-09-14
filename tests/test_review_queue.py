"""批量语义审阅的离线回归：不将领取、未知或过期任务当作已审。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.evidence.identity import canonical_sha256
from aor.storage.evidence_library import EvidenceLibrary
from aor.workflow.review_queue import ReviewQueue, ReviewQueueError


RUN = "RUN-20260914-1234567890"
SECTORS = ["ecommerce", "gaming", "content_creation", "learning", "personal_life", "internet_products"]


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
        self.library = EvidenceLibrary(self.home / "evidence-library")
        self.queue = ReviewQueue(self.home, RUN, clock=lambda: self.now)

    def seed(self, count=6, *, body=None):
        rows = [{"id": f"reddit:post-{i}", "source": "reddit", "source_item_id": f"post-{i}",
                 "evidence_kind": "post", "url": f"https://reddit.example/posts/{i}", "title": f"材料 {i}",
                 "original_text": body if body is not None else f"用户 {i} 反复整理资料，原文尚待判断。",
                 "industry_ids": [SECTORS[i % len(SECTORS)]], "query": "目标用户重复任务",
                 "published_at": "2026-09-10", "observed_at": "2026-09-14"} for i in range(count)]
        self.library.ingest(rows, as_of="2026-09-14", run_id=RUN)
        stored = sorted(self.library.search("", as_of="2026-09-14", limit=None), key=lambda row: row["source_item_id"])
        payload = {"run_id": RUN, "as_of": "2026-09-14", "items": [
            {"evidence_id": row["evidence_id"], "revision_id": row["revision_id"], "industry_ids": row["industry_ids"],
             "selected": True, "reviewed": True} for row in stored]}
        directory = self.home / "runs" / RUN
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "evidence-index.json"
        path.write_text(json.dumps(payload))
        (directory / "run.json").write_text(json.dumps({"run_id": RUN, "as_of": "2026-09-14", "artifacts": {
            "evidence-index": {"path": str(path), "sha256": canonical_sha256(payload)}}}))
        return stored

    def packet(self, claimed):
        return json.loads(Path(claimed["packet_path"]).read_text())

    def outcome(self, row, status="relevant", **updates):
        return {"evidence_id": row["evidence_id"], "revision_id": row["revision_id"], "status": status,
                "rationale": "测试原文描述了目标用户的重复任务" if status == "relevant" else "测试保留此判断的明确依据",
                "reviewed_at": "2026-09-14", "evidence_role": "usage_behavior", **updates}

    def test_manifest_scope_and_existing_reviews_override_untrusted_selected_flags(self):
        rows = self.seed()
        self.library.ingest([{"source": "web", "url": "https://outside.example/", "original_text": "不在本轮索引中"}],
                            as_of="2026-09-14")
        self.library.register_reviews([{**self.outcome(rows[0]), "reviewer": "earlier-reviewer"}], known_on="2026-09-14")
        source = self.library.journal_path.read_bytes()
        result = self.queue.plan()
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["counts"]["reviewed"], 1)
        self.assertEqual(result["counts"]["pending"], 5)
        self.assertEqual(self.library.journal_path.read_bytes(), source)

    def test_planned_batches_balance_industries_and_respect_item_limits(self):
        self.seed(18)
        planned = self.queue.plan(max_items=6)
        state = json.loads(self.queue.path.read_text())
        self.assertEqual(planned["batch_count"], 3)
        for batch in state["batches"].values():
            sectors = {state["tasks"][key]["record"]["industry_ids"][0] for key in batch["task_ids"]}
            self.assertEqual(sectors, set(SECTORS))
        self.assertEqual(self.queue.plan(max_items=6)["total"], 18)

    def test_industry_claims_and_concurrent_agents_never_overlap(self):
        self.seed(18)
        self.queue.plan(max_items=6)
        def claim(worker):
            return ReviewQueue(self.home, RUN, clock=lambda: self.now).claim(worker)
        with ThreadPoolExecutor(max_workers=3) as pool:
            claims = list(pool.map(claim, ["worker-a", "worker-b", "worker-c"]))
        keys = [row["task_id"] for claim in claims for row in self.packet(claim)["items"]]
        self.assertEqual(len(keys), 18)
        self.assertEqual(len(set(keys)), 18)
        self.assertFalse(self.queue.claim("worker-d")["claimed"])

    def test_industry_filter_claims_only_owned_sectors(self):
        self.seed()
        self.queue.plan()
        claim = self.queue.claim("worker", industries=["ecommerce", "gaming"])
        self.assertEqual(claim["item_count"], 2)
        self.assertEqual({row["industry_ids"][0] for row in self.packet(claim)["items"]}, {"ecommerce", "gaming"})
        self.assertFalse(self.queue.claim("other", industries=["ecommerce", "gaming"])["claimed"])

    def test_long_text_is_explicitly_truncated_and_requires_fulltext_confirmation(self):
        self.seed(1, body="完整原文，不能省略核验。" * 1000)
        self.queue.plan(max_chars=2000)
        claim = self.queue.claim("worker")
        packet = self.packet(claim)
        row = packet["items"][0]
        self.assertTrue(row["text_truncated"])
        self.assertLessEqual(claim["packet_chars"], 2000)
        self.assertLessEqual(len(Path(claim["packet_path"]).read_text().rstrip("\n")), 2000)
        with self.assertRaisesRegex(ReviewQueueError, "截断"):
            self.queue.submit(claim["lease_id"], "worker", "s1", [self.outcome(row)])
        fulltext = json.loads(Path(row["fulltext_path"]).read_text())
        self.assertEqual(canonical_sha256(fulltext), row["fulltext_sha256"])
        result = self.queue.submit(claim["lease_id"], "worker", "s1", [self.outcome(row, fulltext_sha256=row["fulltext_sha256"])])
        self.assertEqual(result["progress"]["counts"]["reviewed"], 1)

    def test_unknown_fulltext_and_failed_results_remain_unreviewed_and_reclaimable(self):
        self.seed(4)
        self.queue.plan()
        claim = self.queue.claim("worker")
        rows = self.packet(claim)["items"]
        result = self.queue.submit(claim["lease_id"], "worker", "outcomes", [
            self.outcome(row, status) for row, status in zip(rows, ["unrelated", "unknown", "needs_fulltext", "failed"])])
        counts = result["progress"]["counts"]
        self.assertEqual([counts[key] for key in ("reviewed", "unknown", "needs_fulltext", "failed")], [1, 1, 1, 1])
        self.assertEqual(result["progress"]["unreviewed_count"], 3)
        self.assertEqual(len(self.library.review_path.read_text().splitlines()), 1)
        retry = self.queue.claim("retry")
        self.assertEqual(retry["item_count"], 1)
        deferred = self.queue.claim("fulltext-reader", retry_deferred=True)
        self.assertEqual(deferred["item_count"], 2)

    def test_exact_submission_is_idempotent_and_changed_content_rejected(self):
        self.seed(1)
        self.queue.plan()
        claim = self.queue.claim("worker")
        outcomes = [self.outcome(self.packet(claim)["items"][0])]
        self.queue.submit(claim["lease_id"], "worker", "same-id", outcomes)
        before = self.library.review_path.read_bytes()
        repeated = self.queue.submit(claim["lease_id"], "worker", "same-id", outcomes)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(self.library.review_path.read_bytes(), before)
        with self.assertRaisesRegex(ReviewQueueError, "不能改换"):
            self.queue.submit(claim["lease_id"], "worker", "same-id", [{**outcomes[0], "status": "unrelated"}])

    def test_crash_after_registry_append_recovers_without_duplicate_review(self):
        self.seed(1)
        self.queue.plan()
        claim = self.queue.claim("worker")
        outcomes = [self.outcome(self.packet(claim)["items"][0])]
        save = self.queue._save
        def interrupt(state):
            if any(row["status"] == "committed" for row in state["submissions"].values()):
                raise OSError("模拟登记后、队列落盘前中断")
            save(state)
        with patch.object(self.queue, "_save", side_effect=interrupt), self.assertRaises(OSError):
            self.queue.submit(claim["lease_id"], "worker", "interrupted", outcomes)
        self.assertEqual(len(self.library.review_path.read_text().splitlines()), 1)
        restored = ReviewQueue(self.home, RUN, clock=lambda: self.now)
        self.assertEqual(restored.status()["counts"]["reviewed"], 1)
        self.assertTrue(restored.submit(claim["lease_id"], "worker", "interrupted", outcomes)["idempotent"])
        self.assertEqual(len(self.library.review_path.read_text().splitlines()), 1)

    def test_expired_lease_can_be_reclaimed_but_old_worker_cannot_submit(self):
        self.seed(1)
        self.queue.plan()
        original = self.queue.claim("old", lease_seconds=1)
        self.now += timedelta(seconds=2)
        replacement = self.queue.claim("new")
        self.assertEqual(self.packet(original)["items"][0]["task_id"], self.packet(replacement)["items"][0]["task_id"])
        with self.assertRaisesRegex(ReviewQueueError, "租约"):
            self.queue.submit(original["lease_id"], "old", "late", [self.outcome(self.packet(original)["items"][0])])
        self.queue.release(original["lease_id"], "old", reason="过期后释放")
        self.assertEqual(self.queue.status()["counts"]["leased"], 1)

    def test_failed_release_is_reclaimable_and_wrong_owner_rejected(self):
        self.seed(1)
        self.queue.plan()
        claim = self.queue.claim("worker")
        with self.assertRaisesRegex(ReviewQueueError, "不属于"):
            self.queue.release(claim["lease_id"], "other", reason="不能释放别人任务")
        self.assertEqual(self.queue.release(claim["lease_id"], "worker", reason="本次无法完成")["counts"]["failed"], 1)
        self.assertTrue(self.queue.claim("replacement")["claimed"])

    def test_wrong_revision_or_invalid_review_batch_never_partially_registers(self):
        self.seed(2)
        self.queue.plan()
        claim = self.queue.claim("worker")
        first, second = self.packet(claim)["items"]
        for invalid in (self.outcome(second, revision_id="wrong"), self.outcome(second, reviewed_at="2026-09-15"),
                        self.outcome(second, rationale=" "), self.outcome(second, reviewer="somebody-else")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.queue.submit(claim["lease_id"], "worker", "invalid", [self.outcome(first), invalid])
            self.assertFalse(self.library.review_path.exists())

    def test_new_revision_invalidates_old_tasks_and_prior_leases(self):
        rows = self.seed(1)
        self.queue.plan()
        claim = self.queue.claim("worker")
        updated = deepcopy(rows[0])
        for field in ("evidence_id", "library_evidence_id", "revision_id", "content_hash"):
            updated.pop(field, None)
        updated["original_text"] = "同一来源现在已经更正"
        self.library.ingest([updated], as_of="2026-09-14")
        self.assertEqual(self.queue.status()["counts"]["stale"], 1)
        with self.assertRaises(ReviewQueueError):
            self.queue.submit(claim["lease_id"], "worker", "stale", [self.outcome(self.packet(claim)["items"][0])])
        self.assertFalse(self.library.review_path.exists())

    def test_missing_body_is_fulltext_gap_not_automatically_reviewable(self):
        self.seed(1, body="")
        self.assertEqual(self.queue.plan()["counts"]["needs_fulltext"], 1)
        self.assertFalse(self.queue.claim("worker")["claimed"])
        claim = self.queue.claim("worker", retry_deferred=True)
        with self.assertRaisesRegex(ReviewQueueError, "缺少原文"):
            self.queue.submit(claim["lease_id"], "worker", "empty", [self.outcome(self.packet(claim)["items"][0])])

    def test_manifest_hash_is_checked_and_active_leases_prevent_replanning(self):
        self.seed(1)
        self.queue.plan()
        self.queue.claim("worker")
        with self.assertRaisesRegex(ReviewQueueError, "有效领取"):
            self.queue.plan()
        (self.home / "runs" / RUN / "evidence-index.json").write_text('{"items":[]}')
        with self.assertRaisesRegex(ReviewQueueError, "已被修改"):
            self.queue.plan()

    def test_cli_plan_claim_and_status_need_no_network(self):
        self.seed(1)
        script = Path(__file__).resolve().parents[1] / "scripts/review_queue.py"
        for action in ("plan", "claim", "status"):
            args = [sys.executable, str(script), action, "--home", str(self.home), "--run-id", RUN]
            if action == "claim":
                args += ["--worker", "cli-worker"]
            result = subprocess.run(args, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["total"], 1)


if __name__ == "__main__":
    unittest.main()
