"""跨模型计数保留未知，并要求原文核验绑定独立证据修订。"""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aor.evidence.search_evaluation import summarize_search_results
from aor.sources.search_candidates import merge_search_candidates, normalize_search_candidates


URL = "https://x.com/i/status/2100011781863170285"


def receipt(provider="grok", query="first", urls=None):
    return {"provider": provider, "query": query, "answer": "No verified match found.", "status": "completed",
            "citations": [{"url": url, "start_index": 0, "end_index": 0} for url in (urls or [URL])],
            "usage": {"server_side_tool_usage_details": {"x_search_calls": 2}}}


class SearchEvaluationTests(unittest.TestCase):
    def test_receipt_claims_do_not_turn_candidates_into_evidence_or_observations(self):
        raw = {**receipt(), "verified": True, "evidence": [{"original_text": "model summary"}], "observations": [1]}
        result = summarize_search_results([raw], normalize_search_candidates(raw))
        self.assertEqual(result["unique_candidate_count"], 1)
        self.assertEqual(result["safe_unlocated_citation_count"], 1)
        self.assertIsNone(result["host_verified_candidate_count"])
        self.assertIsNone(result["effective_observation_count"])
        self.assertIsNone(result["independent_author_count"])
        self.assertEqual(result["reported_x_search_calls"], 2)

    def test_provider_increment_counts_unique_posts_without_query_conflicts(self):
        receipts = [receipt(), receipt("host", "other", [URL, "https://x.com/user/status/12345"]),
                    receipt("grok", "third")]
        candidates = merge_search_candidates([row for raw in receipts for row in normalize_search_candidates(raw)])
        result = summarize_search_results(receipts, candidates)
        self.assertEqual(result["unique_candidate_count"], 2)
        self.assertEqual(result["providers"]["grok"]["exclusive_candidate_count"], 0)
        self.assertEqual(result["providers"]["host"]["exclusive_candidate_count"], 1)
        self.assertEqual(len(result["queries"]), 3)

    def test_unknown_usage_is_not_silently_reported_as_complete_zero(self):
        raw = {**receipt(), "usage": None}
        result = summarize_search_results([raw], normalize_search_candidates(raw))
        self.assertEqual(result["receipts_without_x_search_usage"], 1)
        self.assertEqual(result["reported_x_search_calls"], 0)

    def test_explicit_empty_verifications_distinguish_checked_zero_from_unknown(self):
        raw = receipt()
        result = summarize_search_results([raw], normalize_search_candidates(raw), evidence=[], verifications=[])
        self.assertEqual(result["host_verified_candidate_count"], 0)
        self.assertIsNone(result["effective_observation_count"])

    def test_verification_requires_matching_fixed_revision_and_original_quote(self):
        raw = receipt()
        candidates = normalize_search_candidates(raw)
        evidence = [{"source": "twitter", "url": URL, "evidence_id": "EVID-a", "revision_id": "REV-a",
                     "original_text": "I track orders in a spreadsheet.", "verification": {
                         "method": "opened_page", "status": "host_attested"}}]
        check = {"candidate_id": candidates[0]["candidate_id"], "evidence_ref": {
            "evidence_id": "EVID-a", "revision_id": "REV-a", "quote": "track orders"}}
        result = summarize_search_results([raw], candidates, evidence=evidence, verifications=[check, check])
        self.assertEqual(result["host_verified_candidate_count"], 1)
        self.assertEqual(result["providers"]["grok"]["exclusive_host_verified_candidate_count"], 1)
        for field, value in (("revision_id", "other"), ("quote", "model invented text")):
            changed = deepcopy(check)
            changed["evidence_ref"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                summarize_search_results([raw], candidates, evidence=evidence, verifications=[changed])
        for updates in ({"url": "https://x.com/i/status/12345"}, {"verification": {}}, {"retracted": True}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                summarize_search_results([raw], candidates, evidence=[{**evidence[0], **updates}], verifications=[check])


if __name__ == "__main__":
    unittest.main()
