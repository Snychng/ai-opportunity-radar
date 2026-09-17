"""真实 X 搜索返回形状和核验边界的离线回归。"""

from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from contracts import make_run_id
from aor.evidence.identity import evidence_content, evidence_object_identity
from aor.sources.importing import import_web_evidence
from aor.sources.search_candidates import merge_search_candidates, normalize_search_candidates, public_candidate_url, x_post_id
from aor.storage.evidence_library import EvidenceLibrary


POST_ID = "2100011781863170285"
URL = f"https://x.com/user/status/{POST_ID}"


def receipt(**updates):
    return {"run_id": "run", "task_id": "task", "provider": "grok", "model": "grok-test",
            "query": "how sellers track orders", "answer": "No verified match found.",
            "status": "completed", "citations": [{"url": URL, "start_index": 0, "end_index": 0}], **updates}


def imported_item(**updates):
    return {"source": "twitter", "source_object_id": POST_ID, "object_kind": "post", "url": URL,
            "title": "核验原帖", "original_text": "I track orders in a spreadsheet.",
            "supporting_quote": "I track orders in a spreadsheet.", "evidence_role": "usage_behavior",
            "observed_at": "2026-09-16T09:00:00Z", "verification": {
                "verified_by": "host", "verified_at": "2026-09-16T09:00:00Z", "method": "opened_page"}, **updates}


def imported(items):
    return import_web_evidence({"items": items}, run_id=make_run_id(as_of=date(2026, 9, 16), mode="targeted_scan", focus=None),
                               as_of="2026-09-16")["evidence"]


class SearchCandidateTests(unittest.TestCase):
    def test_eighteen_zero_length_annotations_remain_candidates_without_fabricated_text(self):
        raw = receipt(citations=[{"url": f"https://x.com/i/status/{2100011781863170285 + index}",
                                  "start_index": 0, "end_index": 0} for index in range(18)],
                      verified=True, original_text="model self attested")
        candidates = normalize_search_candidates(raw)
        self.assertEqual(len(candidates), 18)
        for candidate in candidates:
            self.assertEqual(candidate["status"], "pending")
            self.assertFalse(candidate["original_text_available"])
            self.assertNotIn("original_text", candidate)
            self.assertNotIn("author", candidate)
            self.assertNotIn("published_at", candidate)
            self.assertEqual(candidate["discoveries"][0]["citation"]["range_status"], "zero_length")

    def test_nonzero_annotation_does_not_prove_original_and_model_answer_remains_labeled(self):
        candidates = normalize_search_candidates(receipt(citations=[{"url": URL, "start_index": 1, "end_index": 10}]))
        self.assertEqual(candidates[0]["verification"]["status"], "pending")
        self.assertEqual(candidates[0]["model_summaries"][0]["kind"], "model_answer_preview")
        self.assertEqual(candidates[0]["object_kind"], "status")
        self.assertEqual(candidates[0]["discoveries"][0]["citation"]["range_status"], "answer_span")

    def test_aliases_and_providers_merge_without_losing_query_provenance(self):
        first = normalize_search_candidates(receipt())
        second = normalize_search_candidates(receipt(provider="host", query="another query", task_id="task-2",
            citations=[{"url": f"https://twitter.com/other/status/{POST_ID}?utm_source=test"}]))
        original = deepcopy(first)
        combined = merge_search_candidates(first + second)
        self.assertEqual(first, original)
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["source"], "twitter")
        self.assertEqual(len(combined[0]["discoveries"]), 2)
        self.assertEqual({row["provider"] for row in combined[0]["discoveries"]}, {"host", "grok"})
        self.assertEqual(merge_search_candidates(combined + combined), combined)

    def test_different_posts_are_not_merged_by_similar_text_or_model(self):
        result = normalize_search_candidates(receipt(citations=[{"url": URL}, {"url": "https://x.com/user/status/12345"}]))
        self.assertEqual(len(result), 2)

    def test_unsafe_urls_and_nonpost_x_links_never_enter_verification_queue(self):
        invalid = ["http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/",
                   "http://[::1]/", "http://2130706433/", "http://0x7f000001/", "https://user:secret@x.com/i/status/12",
                   "https://x.com:8443/i/status/12", "https://x.com/intent/post", "https://x.com/user/status/not-id",
                   "https://x.com/user/status/18446744073709551616", "file:///tmp/post", "https://x.com\\@127.0.0.1/",
                   "https://host.local/post", "https://x.com/\nuser/status/12", "https://x.com/user"]
        result = normalize_search_candidates(receipt(citations=[{"url": url} for url in invalid]))
        self.assertEqual(result, [])
        self.assertIsNone(x_post_id("https://x.com.evil.example/i/status/12"))
        self.assertEqual(x_post_id(f"https://x.com./i/status/{POST_ID}"), POST_ID)
        with self.assertRaises(ValueError):
            public_candidate_url("https://example.com:999999/path")

    def test_regular_web_candidates_are_allowed_and_do_not_claim_x_identity(self):
        row = normalize_search_candidates(receipt(citations=[{"url": "https://example.com/product?utm_source=grok"}]))[0]
        self.assertEqual(row["source"], "web")
        self.assertIsNone(row["source_object_id"])
        self.assertEqual(row["url"], "https://example.com/product")
        ipv6 = normalize_search_candidates(receipt(citations=[{"url": "https://[2606:4700:4700::1111]/public"}]))[0]
        self.assertEqual(ipv6["url"], "https://[2606:4700:4700::1111]/public")

    def test_no_citations_means_no_candidates_and_bounds_are_explicit(self):
        self.assertEqual(normalize_search_candidates(receipt(citations=[])), [])
        with self.assertRaises(ValueError):
            normalize_search_candidates(receipt(citations=[{"url": URL}] * 501))


class VerifiedSearchImportTests(unittest.TestCase):
    def test_new_verified_x_aliases_share_native_identity_with_tikhub(self):
        first = imported([imported_item()])[0]
        second = imported([imported_item(url=f"https://twitter.com/i/status/{POST_ID}")])[0]
        tikhub = {"source": "twitter", "source_item_id": POST_ID, "evidence_kind": "post", "url": URL}
        self.assertEqual(evidence_object_identity(first), ("twitter", "post", POST_ID))
        self.assertEqual(evidence_object_identity(first), evidence_object_identity(tikhub))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(evidence_content(first), evidence_content(second))
        self.assertIn(URL, first["same_source_refs"])

    def test_imported_replies_keep_existing_comment_namespace(self):
        row = imported([imported_item(source_object_id=POST_ID, object_kind="reply")])[0]
        self.assertEqual(evidence_object_identity(row), ("twitter", "comment", POST_ID))
        self.assertEqual(row["id"], f"twitter:comment:{POST_ID}")
        for updates in ({"source_object_id": "123", "object_kind": "post"},
                        {"source_object_id": POST_ID, "object_kind": None},
                        {"source_object_id": POST_ID, "object_kind": "post", "source": "web"},
                        {"source_object_id": POST_ID, "object_kind": "post", "original_url": "https://x.com/user/status/123"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                imported([imported_item(**updates)])

    def test_old_url_only_identity_is_not_reinterpreted(self):
        old = {"id": "web:legacy-id", "source": "twitter", "url": URL, "original_text": "unchanged"}
        self.assertIsNone(evidence_object_identity(old))
        with tempfile.TemporaryDirectory() as tmp:
            library = EvidenceLibrary(Path(tmp))
            library.ingest([old], as_of="2026-09-16")
            old_id = library.search("", as_of="2026-09-16")[0]["evidence_id"]
            library.ingest(imported([imported_item()]), as_of="2026-09-16")
            library.rebuild()
            self.assertEqual(library.resolve({"evidence_id": old_id}, as_of="2026-09-16")["original_text"], "unchanged")

    def test_new_import_without_explicit_object_kind_retains_url_identity(self):
        item = imported_item()
        del item["source_object_id"], item["object_kind"]
        self.assertIsNone(evidence_object_identity(imported([item])[0]))

    def test_model_answer_preview_is_bounded_and_complete_answer_is_only_in_receipt(self):
        raw = receipt(answer="a" * 200000)
        row = normalize_search_candidates(raw)[0]
        self.assertEqual(len(row["model_summaries"][0]["text"]), 1000)
        self.assertTrue(row["model_summaries"][0]["truncated"])
        self.assertEqual(len(row["model_summaries"][0]["receipt_sha256"]), 64)

    def test_queries_change_metadata_without_fabricating_content_revisions(self):
        first = imported([imported_item(query="first", query_id="q1", retrieval={"rank": 2})])[0]
        second = imported([imported_item(query="second", query_id="q2", retrieval={"rank": 5})])[0]
        with tempfile.TemporaryDirectory() as tmp:
            library = EvidenceLibrary(Path(tmp))
            library.ingest([first], as_of="2026-09-16")
            self.assertEqual(library.ingest([second], as_of="2026-09-16")["revisions_added"], 0)
        for updates in ({"retrieval": {"provider": "grok"}}, {"retrieval": {"rank": True}}, {"model": "grok"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                imported([imported_item(**updates)])


if __name__ == "__main__":
    unittest.main()
