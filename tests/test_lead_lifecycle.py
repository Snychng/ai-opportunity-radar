"""网站收藏身份、修订、范围和证据失效的回归。"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.opportunity.exploration import lead_history, meaningful_ai_value, normalize_leads, record_leads, transition_lead
from expand_ideas import expand_ideas
from tests.test_idea_funnel import benchmark

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


if __name__ == "__main__":
    unittest.main()
