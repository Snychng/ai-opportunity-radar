"""语义复核独立持久化：不伪造采集、不能让旧正文复活。"""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from tests.test_evidence_identity import post
from aor.evidence.retrieval import EvidenceReferenceError
from aor.storage.evidence_library import EvidenceLibrary, EvidenceLibraryError


def review(row, **updates):
    return {"evidence_id": row["evidence_id"], "revision_id": row["revision_id"], "status": "relevant",
            "reviewer": "测试审阅者", "reviewed_at": "2026-09-02", "rationale": "原文描述了反复出现的任务",
            "evidence_role": "usage_behavior", **updates}


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.library = EvidenceLibrary(self.home / "library")
        self.library.ingest([post()], as_of="2026-09-01", run_id="RUN-20260901-0000000001")
        self.original = self.library.search("", as_of="2026-09-01")[0]

    def test_review_reuse_preserves_observation_dates_runs_and_source_journal(self):
        original = self.library.journal_path.read_bytes()
        self.library.register_reviews([review(self.original)], known_on="2026-09-02",
                                      run_id="RUN-20260902-0000000001")
        current = self.library.search("", as_of="2026-09-03", run_id="RUN-20260903-0000000001")[0]
        self.assertEqual(current["semantic_relevance_status"], "relevant")
        self.assertEqual(current["evidence_role"], "usage_behavior")
        for key in ("first_observed_at", "last_observed_at", "observed_at", "run_ids", "revision_id", "content_hash"):
            self.assertEqual(current[key], self.original[key], key)
        self.assertEqual(self.library.journal_path.read_bytes(), original)
        self.assertNotIn("relevance_review", self.library.search("", as_of="2026-09-01")[0])
        # 同正文新采集仍能复用复核，只有这次真正采集才增加观察和来源 run。
        self.library.ingest([post(observed_at="2026-09-04")], as_of="2026-09-04",
                            run_id="RUN-20260904-0000000001")
        current = self.library.search("", as_of="2026-09-04")[0]
        self.assertEqual(current["revision_id"], self.original["revision_id"])
        self.assertEqual(current["semantic_relevance_status"], "relevant")
        self.assertEqual(current["run_ids"], ["RUN-20260901-0000000001", "RUN-20260904-0000000001"])

    def test_review_of_historical_revision_never_restores_old_body_or_leaks_to_new_body(self):
        self.library.ingest([post(original_text="更正后的正文", observed_at="2026-09-03", retracted=True)],
                            as_of="2026-09-03")
        source = self.library.journal_path.read_bytes()
        self.library.register_reviews([review(self.original, reviewed_at="2026-09-04")], known_on="2026-09-04")
        self.assertEqual(self.library.search("", as_of="2026-09-05"), [])
        current = self.library.search("", as_of="2026-09-05", include_retracted=True)[0]
        self.assertEqual(current["original_text"], "更正后的正文")
        self.assertNotIn("relevance_review", current)
        old = self.library.resolve({k: self.original[k] for k in ("evidence_id", "revision_id")}, as_of="2026-09-05")
        self.assertEqual(old["relevance_review"]["status"], "relevant")
        self.assertEqual(self.library.journal_path.read_bytes(), source)

    def test_last_review_wins_and_reimporting_older_review_does_not_restore_relevant(self):
        first = review(self.original)
        second = review(self.original, status="unrelated", rationale="复核发现并非目标任务")
        self.library.register_reviews([first], known_on="2026-09-02")
        self.library.register_reviews([second], known_on="2026-09-02")
        self.assertEqual(self.library.search("", as_of="2026-09-03")[0]["semantic_relevance_status"], "unrelated")
        repeated = self.library.register_reviews([first], known_on="2026-09-03",
                                                 run_id="RUN-20260903-0000000001")
        self.assertEqual(repeated["events_added"], 0)
        earlier = review(self.original, reviewed_at="2026-09-01", rationale="更早的历史复核")
        self.library.register_reviews([earlier], known_on="2026-09-04")
        self.assertEqual(self.library.search("", as_of="2026-09-04")[0]["semantic_relevance_status"], "unrelated")

    def test_review_registry_rebuild_migration_and_cutoff_preserve_semantics(self):
        self.library.register_reviews([review(self.original)], known_on="2026-09-03")
        self.assertNotIn("relevance_review", self.library.search("", as_of="2026-09-02")[0])
        before = self.library.review_path.read_bytes()
        self.library.rebuild()
        self.assertEqual(self.library.search("", as_of="2026-09-03")[0]["semantic_relevance_status"], "relevant")
        migrated = EvidenceLibrary(self.home / "migrated")
        result = self.library.migrate_identities(migrated.root)
        self.assertEqual(result["review_events"], 1)
        self.assertEqual(migrated.review_path.read_bytes(), before)
        current = migrated.search("", as_of="2026-09-04")[0]
        self.assertEqual(current["relevance_review"]["revision_id"], current["revision_id"])
        self.assertEqual(current["relevance_review"]["status"], "relevant")

    def test_invalid_batch_is_atomic_and_corrupt_registry_cannot_be_ignored(self):
        valid = review(self.original)
        for invalid in ({**valid, "revision_id": "missing"}, {**valid, "reviewed_at": "2026-09-04"},
                        {**valid, "reviewer": " "}, {k: v for k, v in valid.items() if k != "revision_id"}):
            with self.assertRaises((EvidenceLibraryError, EvidenceReferenceError)):
                self.library.register_reviews([valid, invalid], known_on="2026-09-02")
            self.assertFalse(self.library.review_path.exists())
        self.library.register_reviews([valid], known_on="2026-09-02")
        value = json.loads(self.library.review_path.read_text())
        value["review"]["status"] = "unrelated"
        self.library.review_path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(EvidenceLibraryError, "复核日志"):
            self.library.search("", as_of="2026-09-03")
        with self.assertRaises(EvidenceLibraryError):
            self.library.rebuild()

    def test_workflow_persists_review_and_next_run_reuses_without_new_collection(self):
        from aor.workflow.research import start_research, resume_research
        from tests.test_research_integrity import DECISION, write

        home = self.home / "workflow"
        home.mkdir()
        evidence = write(home / "source.json", {"evidence": [post(industry_ids=["gaming"])]})
        benchmarks = write(home / "input.json", {"benchmarks": [], "leads": [], "empty_reason": "仅验证证据复核复用"})
        first = start_research(home, as_of=date(2026, 9, 2), offline=True, focus="两小时",
                               evidence_files=[evidence], benchmarks_file=benchmarks)
        context = json.loads(Path(first["artifacts"]["evidence-context"]["path"]).read_text())["evidence"]
        assessment = write(home / "review.json", {"scores": [], "claims": [], "decision": deepcopy(DECISION),
                            "evidence_reviews": [review(context[0])]})
        resume_research(home, first["run_id"], assessment_file=assessment, collect=False)
        library = EvidenceLibrary(home / "evidence-library")
        source = library.journal_path.read_bytes()
        second = start_research(home, as_of=date(2026, 9, 3), offline=True, focus="两小时",
                                benchmarks_file=benchmarks)
        reused = json.loads(Path(second["artifacts"]["evidence-context"]["path"]).read_text())["evidence"]
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["relevance_review"]["status"], "relevant")
        self.assertNotIn(second["run_id"], reused[0]["run_ids"])
        self.assertEqual(library.journal_path.read_bytes(), source)


if __name__ == "__main__":
    unittest.main()
