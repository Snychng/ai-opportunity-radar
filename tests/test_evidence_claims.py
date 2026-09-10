from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aor.evidence.claims import build_evidence_packet, validate_claims


def evidence(**changes):
    return {
        "id": "post-1", "evidence_id": "EVID-1", "revision_id": "REV-1",
        "url": "https://example.com/1", "source": "community",
        "original_text": "I paid $29 for this tool. Setup still takes hours.",
        "published_at": "2026-09-01", "observed_at": "2026-09-02T09:00:00+08:00",
        "run_id": "RUN-OLD", "raw_file": "raw/2026-09-02/posts.json",
        **changes,
    }


def claim(**changes):
    return {
        "id": "CLAIM-1", "statement": "存在明确的工具付款描述。",
        "kind": "payment", "payer": "小型工作室", "market": "US",
        "date": "2026-09-02", "verification_status": "supports",
        "evidence_refs": [{"evidence_id": "EVID-1", "revision_id": "REV-1", "quote": "I paid $29"}],
        **changes,
    }


class ClaimValidationTests(unittest.TestCase):
    def test_quote_locations_aliases_and_host_judgment_are_preserved(self):
        item = evidence(comments=[{"original_text": "I canceled my subscription."}])
        claims = [claim(), claim(id="CLAIM-2", verification_status="conflicts", evidence_refs=[{
            "evidence_id": "post-1", "revision_id": "REV-1",
            "quote": "canceled", "field": "comments[0].original_text",
        }])]
        before = copy.deepcopy(claims)
        result = validate_claims(claims, [item], as_of="2026-09-10")
        self.assertEqual(claims, before)
        self.assertEqual(result[0]["verification_status"], "supports")
        self.assertEqual(result[1]["verification_status"], "conflicts")
        self.assertEqual(result[0]["semantic_validation"], "not_performed")
        ref = result[1]["evidence_refs"][0]
        self.assertEqual(ref["evidence_id"], "EVID-1")
        self.assertEqual(ref["field"], "comments.0.original_text")
        self.assertEqual(item["comments"][0]["original_text"][ref["start"]:ref["end"]], "canceled")

    def test_invalid_references_are_rejected(self):
        ref = claim()["evidence_refs"][0]
        for changes in ({"evidence_id": "missing"}, {"revision_id": "REV-2"},
                        {"revision_id": None}, {"quote": "Nobody paid"}, {"quote": ""},
                        {"field": "zh_translation"}, {"field": "comments.9.text"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_claims([claim(evidence_refs=[{**ref, **changes}])], [evidence()])

    def test_cutoff_checks_publication_observation_and_comment_date(self):
        for changes in ({"published_at": "2026-09-11"}, {"observed_at": "2026-09-11T00:00:00+08:00"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_claims([claim()], [evidence(**changes)], as_of="2026-09-10")
        late_comment = evidence(comments=[{"text": "paid later", "published_at": "2026-09-11"}])
        comment_claim = claim(evidence_refs=[{"evidence_id": "EVID-1", "revision_id": "REV-1",
                                             "quote": "paid later", "field": "comments.0.text"}])
        with self.assertRaises(ValueError):
            validate_claims([comment_claim], [late_comment], as_of="2026-09-10")
        self.assertEqual(len(validate_claims([claim()], [evidence(observed_at="2026-09-10T15:59:59Z")],
                                           as_of="2026-09-10")), 1)
        with self.assertRaises(ValueError):
            validate_claims([claim()], [evidence(observed_at="2026-09-10T16:00:00Z")], as_of="2026-09-10")

    def test_unverified_claim_can_express_missing_evidence(self):
        result = validate_claims([{"id": "C", "statement": "付款者尚待确认", "evidence_refs": []}], [])
        self.assertEqual(result[0]["verification_status"], "unverified")
        self.assertIsNone(result[0]["payer"])
        with self.assertRaises(ValueError):
            validate_claims([claim(evidence_refs=[])], [])

    def test_library_alias_and_duplicate_collection_label_keep_one_revision(self):
        records = [evidence(evidence_id="EVID-LIBRARY", aliases=["EVID-1"]),
                   evidence(evidence_id="EVID-LIBRARY", source="search", aliases=["EVID-1"])]
        checked = validate_claims([claim()], records)
        self.assertEqual(checked[0]["evidence_refs"][0]["evidence_id"], "EVID-LIBRARY")
        packet = build_evidence_packet(records, claims=[claim()], as_of="2026-09-10")
        self.assertEqual(len(packet["evidence"]), 1)
        self.assertEqual(packet["evidence"][0]["source_labels"], ["community", "search"])
        with self.assertRaises(ValueError):
            validate_claims([claim()], [evidence(), evidence(original_text="A different revision.")])
        with self.assertRaises(ValueError):
            validate_claims([claim()], [evidence(payer="工作室"), evidence(payer="学校")])

    def test_later_collection_label_cannot_appear_in_historical_snapshot(self):
        earlier = evidence()
        later = evidence(source="later-search", observed_at="2026-09-11")
        for records in ([earlier, later], [later, earlier]):
            with self.subTest(order=records[0]["source"]):
                packet = build_evidence_packet(records, claims=[claim()], as_of="2026-09-10")
                self.assertNotIn("later-search", json.dumps(packet, ensure_ascii=False))
                self.assertEqual(packet["omitted"]["evidence_future"], 1)


class EvidencePacketTests(unittest.TestCase):
    def test_historical_packet_omits_future_material_and_marks_reuse(self):
        items = [evidence(), evidence(id="post-2", evidence_id="EVID-2", observed_at="2026-09-11"),
                 evidence(id="post-3", evidence_id="EVID-3", observed_at=None)]
        future_claim = claim(id="CLAIM-2", evidence_refs=[{
            "evidence_id": "EVID-2", "revision_id": "REV-1", "quote": "I paid $29",
        }])
        experiments = [
            {"experiment_id": "EXP-1", "as_of": "2026-09-09", "status": "completed", "decision": "继续访谈"},
            {"experiment_id": "EXP-2", "as_of": "2026-09-11", "status": "completed"},
            {"experiment_id": "EXP-3", "as_of": "2026-09-09", "evidence": [{"observed_at": "2026-09-11"}]},
        ]
        packet = build_evidence_packet(items, claims=[claim(), future_claim], as_of="2026-09-10",
                                       run_id="RUN-NEW", experiments=experiments)
        self.assertEqual([e["evidence_id"] for e in packet["evidence"]], ["EVID-1"])
        self.assertEqual(packet["evidence"][0]["reused_for_run_id"], "RUN-NEW")
        self.assertEqual([c["id"] for c in packet["claims"]], ["CLAIM-1"])
        self.assertEqual([e["experiment_id"] for e in packet["experiments"]], ["EXP-1"])
        self.assertEqual(packet["omitted"]["evidence_future"], 1)
        self.assertEqual(packet["omitted"]["evidence_missing_observation"], 1)
        self.assertEqual(packet["omitted"]["claims_unavailable_evidence"], 1)
        self.assertEqual(packet["omitted"]["experiments_future"], 2)

    def test_long_text_keeps_cited_tail_and_raw_provenance_within_budget(self):
        item = evidence(original_text="无关上下文" * 5000 + "I paid $29" + "末尾" * 5000,
                        raw_refs=[{"path": "raw/posts.json", "json_pointer": "/items/0"}])
        packet = build_evidence_packet([item], claims=[claim()], as_of="2026-09-10", max_chars=2200)
        self.assertEqual(len(packet["claims"]), 1)
        excerpt = packet["evidence"][0]["text_refs"][0]
        self.assertEqual(item["original_text"][excerpt["start"]:excerpt["end"]], excerpt["text"])
        self.assertIn("I paid $29", excerpt["text"])
        self.assertEqual(packet["evidence"][0]["raw_refs"], item["raw_refs"])
        serialized = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(packet["serialized_chars"], len(serialized))
        self.assertLessEqual(len(serialized), 2200)

    def test_claim_bundle_is_atomic_when_item_limit_cannot_fit_all_refs(self):
        second = evidence(id="post-2", evidence_id="EVID-2")
        both = claim(evidence_refs=[*claim()["evidence_refs"], {
            "evidence_id": "EVID-2", "revision_id": "REV-1", "quote": "I paid $29",
        }])
        packet = build_evidence_packet([evidence(), second], claims=[both], as_of="2026-09-10", max_items=1)
        self.assertEqual(len(packet["evidence"]), 1)
        self.assertEqual(packet["claims"], [])
        self.assertEqual(packet["omitted"]["claims_budget"], 1)
        self.assertEqual(packet["omitted"]["evidence_budget"], 1)

    def test_quote_too_large_is_omitted_as_a_claim_instead_of_truncated(self):
        text = "完整引文" * 1000
        huge = claim(evidence_refs=[{"evidence_id": "EVID-1", "revision_id": "REV-1", "quote": text}])
        packet = build_evidence_packet([evidence(original_text=text)], claims=[huge], as_of="2026-09-10", max_chars=1600)
        self.assertEqual(packet["claims"], [])
        self.assertEqual(packet["omitted"]["claims_budget"], 1)
        self.assertLessEqual(packet["serialized_chars"], 1600)

    def test_bad_reference_is_not_silently_removed_from_packet(self):
        with self.assertRaises(ValueError):
            build_evidence_packet([evidence()], claims=[claim(evidence_refs=[{
                "evidence_id": "EVID-1", "revision_id": "REV-WRONG", "quote": "I paid $29",
            }])], as_of="2026-09-10")

    def test_experiment_context_gets_room_before_uncited_search_results(self):
        packet = build_evidence_packet([evidence()], as_of="2026-09-10", max_items=1,
                                       experiments=[{"experiment_id": "EXP-1", "as_of": "2026-09-09"}])
        self.assertEqual(len(packet["experiments"]), 1)
        self.assertEqual(packet["omitted"]["evidence_budget"], 1)

    def test_omitted_comments_are_counted_without_copying_their_text(self):
        item = evidence(comments=[{"text": "完整评论"}, {"original_text": "第二条评论"}])
        packet = build_evidence_packet([item], as_of="2026-09-10")
        compact = packet["evidence"][0]
        self.assertEqual(compact["omitted_text_fields"], 2)
        self.assertEqual(compact["omitted_text_chars"], len("完整评论第二条评论"))
        self.assertNotIn("第二条评论", json.dumps(packet, ensure_ascii=False))

    def test_nested_future_comments_and_experiment_results_are_excluded(self):
        item = evidence(comments=[{"text": "当时的评论", "replies": [{"text": "后续回复", "observed_at": "2026-09-11"}]}])
        experiments = [{"experiment_id": "EXP-1", "as_of": "2026-09-09",
                        "result": {"completed_at": "2026-09-11", "decision": "后续付款"}}]
        packet = build_evidence_packet([item], claims=[claim()], as_of="2026-09-10", experiments=experiments)
        self.assertEqual(packet["evidence"], [])
        self.assertEqual(packet["claims"], [])
        self.assertEqual(packet["experiments"], [])
        self.assertEqual(packet["omitted"]["evidence_future"], 1)
        self.assertEqual(packet["omitted"]["experiments_future"], 1)

    def test_late_recording_cannot_rewrite_an_earlier_cutoff(self):
        late = evidence(recorded_on="2026-09-10")
        packet = build_evidence_packet([late], claims=[claim()], as_of="2026-09-02")
        self.assertEqual(packet["evidence"], [])
        self.assertEqual(packet["claims"], [])
        self.assertEqual(packet["omitted"]["evidence_future"], 1)
        with self.assertRaises(ValueError):
            validate_claims([claim()], [late], as_of="2026-09-02")

    def test_legacy_fact_fields_survive_without_becoming_verified_original_quotes(self):
        legacy_fields = {"fact": "用户表示已付款", "supporting_fact": "订单支持描述",
                         "supports": "付款者有购买意向", "quote": "I paid $29"}
        item = evidence(**legacy_fields)
        del item["original_text"]
        packet = build_evidence_packet([item], as_of="2026-09-10")
        compact = packet["evidence"][0]
        for field, value in legacy_fields.items():
            self.assertEqual(compact[field], value)
        self.assertEqual(compact["text_refs"], [])
        self.assertIn("original_text/text", compact["missing_fields"])
        self.assertEqual(packet["semantic_validation"], "not_performed")
        with self.assertRaises(ValueError):
            validate_claims([claim()], [item])
        for field, value in legacy_fields.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_claims([claim(evidence_refs=[{
                    "evidence_id": "EVID-1", "revision_id": "REV-1", "quote": value, "field": field,
                }])], [item])
        for empty_text in (None, "", " \n"):
            with self.subTest(original_text=empty_text):
                packet = build_evidence_packet([evidence(original_text=empty_text, **legacy_fields)], as_of="2026-09-10")
                self.assertIn("original_text/text", packet["evidence"][0]["missing_fields"])


if __name__ == "__main__":
    unittest.main()
