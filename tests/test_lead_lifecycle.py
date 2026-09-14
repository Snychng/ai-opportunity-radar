"""网站收藏身份、修订、范围和证据失效的回归。"""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.opportunity.exploration import lead_history, meaningful_ai_value, normalize_leads, record_leads, transition_lead
from expand_ideas import expand_ideas
from tests.test_idea_funnel import benchmark
from tests.test_evidence_identity import post
from aor.storage.evidence_library import EvidenceLibrary

RUN = "RUN-20260914-AAAAAAAAAA"
LATER = "RUN-20260914-BBBBBBBBBB"


class LeadLifecycleTests(unittest.TestCase):
    def candidate(self):
        return expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]

    def normalize(self, row, run=RUN):
        return normalize_leads([row], run_id=run, as_of="2026-09-14")[0]

    def test_punctuation_and_later_editorial_changes_preserve_saved_identity(self):
        raw = self.candidate()
        first = self.normalize(raw)
        raw["problem_or_desire"] += "。"
        self.assertEqual(self.normalize(raw)["lead_id"], first["lead_id"])
        revised = deepcopy(first)
        revised["problem_or_desire"] = "经过访谈修订后的具体用户问题"
        second = self.normalize(revised, LATER)
        self.assertEqual(second["lead_id"], first["lead_id"])
        self.assertNotEqual(second["revision_id"], first["revision_id"])

    def test_required_fields_and_excluded_industry_fail_before_saving(self):
        for key in ("title", "industry_ids", "wedge"):
            row = self.candidate()
            row.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.normalize(row)
        row = self.candidate()
        row["industry_ids"] = ["manufacturing"]
        with self.assertRaises(ValueError):
            self.normalize(row)

    def test_repeated_placeholder_and_unreferenced_supported_ai_are_not_evidence(self):
        ai = self.candidate()["ai_value"]
        for field in ("baseline", "capability", "user_benefit", "incremental_advantage"):
            ai[field] = "这是需要验证的事情"
        self.assertFalse(meaningful_ai_value(ai))
        ai = self.candidate()["ai_value"]
        ai["status"] = "supported"
        self.assertFalse(meaningful_ai_value(ai))

    def test_archiving_keeps_history_and_changed_evidence_requires_review(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = self.normalize(self.candidate())
            record_leads(home, [first], run_id=RUN)
            stale = lead_history(home, as_of="2026-09-14", evidence=[])
            self.assertEqual(stale[0]["research_status"], "needs_review")
            self.assertEqual(lead_history(home, as_of="2026-09-14")[0]["research_status"], "needs_verification")
            archived = transition_lead(home, first["lead_id"], status="archived", reason="已找到足够好的现有替代品",
                                       run_id=LATER, as_of="2026-09-14")
            self.assertEqual(archived["lead_id"], first["lead_id"])
            self.assertEqual(lead_history(home, as_of="2026-09-14")[0]["research_status"], "archived")
            self.assertEqual(len((home / "state/research-leads.jsonl").read_text().splitlines()), 2)

    def tracked_lead(self, home):
        library = EvidenceLibrary(home / "evidence-library")
        library.ingest([post()], as_of="2026-09-14", run_id=RUN)
        stored = library.search("", as_of="2026-09-14")[0]
        row = self.candidate()
        row["evidence"] = [stored]
        lead = self.normalize(row)
        record_leads(home, [lead], run_id=RUN)
        return library, stored, lead

    def test_current_revision_change_is_seen_by_list_and_cannot_be_bypassed_by_transition(self):
        import manage_leads
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            library, stored, lead = self.tracked_lead(home)
            library.ingest([post(original_text="新的来源正文", observed_at="2026-09-15")], as_of="2026-09-15")
            self.assertEqual(lead_history(home, as_of="2026-09-14")[0]["research_status"], "needs_verification")
            current = lead_history(home, as_of="2026-09-15")[0]
            self.assertEqual(current["research_status"], "needs_review")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(manage_leads.main(["list", "--home", str(home), "--date", "2026-09-15"]), 0)
            self.assertEqual(json.loads(output.getvalue())["leads"][0]["reference_status"], "needs_review")
            for status in ("needs_verification", "observed_need", "promoted"):
                with self.subTest(status=status), self.assertRaisesRegex(ValueError, "引用状态已失效"):
                    transition_lead(home, lead["lead_id"], status=status, reason="直接恢复",
                                    run_id="RUN-20260915-CCCCCCCCCC", as_of="2026-09-15")

    def test_archived_lead_stays_archived_when_current_evidence_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            library, stored, lead = self.tracked_lead(home)
            transition_lead(home, lead["lead_id"], status="archived", reason="已有成熟替代",
                            run_id=LATER, as_of="2026-09-14")
            before = (home / "state/research-leads.jsonl").read_bytes()
            for update in ({"retracted": True}, {"derivation_status": "superseded"}, {"derivation_status": "needs_review"}):
                result = lead_history(home, as_of="2026-09-14", evidence=[{**stored, **update}])[0]
                self.assertEqual(result["research_status"], "archived")
                self.assertEqual(result["reference_status"], "needs_review")
            self.assertEqual((home / "state/research-leads.jsonl").read_bytes(), before)

    def test_observed_need_uses_persisted_current_review_instead_of_stale_lead_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            library, stored, lead = self.tracked_lead(home)
            review = {"evidence_id": stored["evidence_id"], "revision_id": stored["revision_id"], "status": "relevant",
                      "reviewer": "测试复核", "reviewed_at": "2026-09-14", "rationale": "原文描述玩家真实任务",
                      "evidence_role": "usage_behavior"}
            library.register_reviews([review], known_on="2026-09-14")
            changed = transition_lead(home, lead["lead_id"], status="observed_need", reason="已核验玩家原文",
                                      run_id=LATER, as_of="2026-09-14")
            self.assertEqual(changed["research_status"], "observed_need")
            library.register_reviews([{**review, "status": "unrelated", "reviewed_at": "2026-09-15"}], known_on="2026-09-15")
            self.assertEqual(lead_history(home, as_of="2026-09-15")[0]["research_status"], "needs_review")
            with self.assertRaisesRegex(ValueError, "引用状态已失效"):
                transition_lead(home, lead["lead_id"], status="observed_need", reason="再次设定状态",
                                run_id="RUN-20260915-CCCCCCCCCC", as_of="2026-09-15")


if __name__ == "__main__":
    unittest.main()
