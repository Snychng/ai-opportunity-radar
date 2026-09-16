"""六方向覆盖、历史选题、价格替补与阅读队列的离线回归。"""

import sys
from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.sources.industries import build_industry_discovery, load_industries
from aor.sources.coverage import build_industry_coverage, research_quality
from aor.sources.discovery import prepare_discovery_plan
from aor.evidence.selection import compact_packet_metadata, evidence_index, build_industry_packets
from aor.evidence.claims import build_evidence_packet
from tikhub_query import RADAR_ENDPOINTS

DAY = date(2026, 9, 14)
RUN = "RUN-20260914-1234567890"


def prices(exclude=()):
    return [{"endpoint_uri": row["endpoint"], "endpoint_cost": 0.01, "allow_free_credit": False,
             "allow_discount": False, "platform": source} for source, row in RADAR_ENDPOINTS.items() if source not in exclude]


class IndustryResearchV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.discovery = self.build()
        self.plan = {"run_id": RUN, "as_of": DAY.isoformat(), "industry_catalog": self.discovery["catalog"],
                     "selected_industries": self.discovery["selected"], "retrieval_plans": self.discovery["retrieval_plans"]}

    def build(self, **kwargs):
        options = dict(as_of=DAY, run_id=RUN, home=self.home, preferences={}, planned_sources=list(RADAR_ENDPOINTS))
        options.update(kwargs)
        return build_industry_discovery(**options)

    def evidence(self, identifier="post-1", **kwargs):
        row = {"id": identifier, "evidence_id": "EV-" + identifier, "revision_id": "REV-1", "source": "reddit",
               "evidence_kind": "post", "url": "https://reddit.example/thread/1", "industry_ids": ["gaming"],
               "published_at": "2026-09-10", "observed_at": "2026-09-14", "original_text": "Working adults need fixed time gaming teammates.",
               "relevance_status": "relevant", "evidence_role": "usage_behavior"}
        row.update(deepcopy(kwargs))
        if row.get("relevance_review"):
            row["relevance_review"].update(evidence_id=row["evidence_id"], revision_id=row["revision_id"])
        return row

    def test_full_context_review_counts_include_historical_comments_without_changing_fresh_coverage(self):
        review = {"status": "relevant", "reviewer": "host", "reviewed_at": "2026-09-14"}
        fresh = self.evidence("post", source_item_id="post", library_evidence_id="EV-post", run_ids=[RUN],
                              industry_ids=["gaming", "personal_life"], relevance_review=review)
        old = [self.evidence(f"comment-{i}", source_item_id=f"comment-{i}", evidence_kind="comment",
                             url_kind="parent_post", run_ids=["RUN-20260913-1234567890"],
                             reused_for_run_id=RUN, relevance_review=review if i == 0 else None) for i in range(3)]
        context = [fresh, *old, deepcopy(fresh)]
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": context}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual((game["material_count"], game["historical_evidence_count"]), (1, 3))
        quality = research_quality(coverage, {"omitted": {"evidence_budget": 3}}, {}, evidence=context)
        self.assertEqual(quality["review_scope"], "active_context")
        self.assertEqual(quality["overall_evidence_count"], 4)
        self.assertEqual(quality["reviewed_evidence_count"], 2)
        self.assertEqual(quality["unreviewed_evidence_count"], 2)
        self.assertEqual(quality["current_evidence_count"], 1)
        self.assertEqual(quality["current_scope_unreviewed_evidence_count"], 0)
        self.assertEqual(quality["historical_reviewed_evidence_count"], 1)
        self.assertEqual(quality["historical_unreviewed_evidence_count"], 2)

    def test_full_context_never_inherits_review_from_old_or_superseded_revision(self):
        old = self.evidence("post", source_item_id="post", library_evidence_id="EV-post", run_ids=[RUN],
                            relevance_review={"status": "relevant", "reviewer": "host", "reviewed_at": "2026-09-14"})
        new = {**old, "revision_id": "REV-2", "original_text": "Corrected source text"}
        excluded = [self.evidence("superseded", source_item_id="superseded", derivation_status="superseded"),
                    self.evidence("retracted", source_item_id="retracted", retracted=True),
                    {**old, "historical_reference_only": True}]
        quality = research_quality({"version": "2.0", "run_id": RUN, "as_of": "2026-09-14", "industries": []},
                                   {}, {}, evidence=[old, new, *excluded])
        self.assertEqual(quality["overall_evidence_count"], 1)
        self.assertEqual(quality["reviewed_evidence_count"], 0)
        self.assertEqual(quality["current_unreviewed_evidence_count"], 1)
        self.assertTrue(quality["coverage_incomplete"])

    def test_review_scope_is_explicit_and_missing_full_context_keeps_legacy_quality_shape(self):
        coverage = {"version": "2.0", "industries": []}
        prior = research_quality(coverage, {}, {})
        self.assertNotIn("review_scope", prior)
        self.assertNotIn("overall_evidence_count", prior)
        current = research_quality(coverage, {}, {}, evidence=[])
        self.assertEqual(current["review_scope"], "active_context")
        self.assertEqual(current["unreviewed_evidence_count"], 0)
        self.assertEqual(current["overall_evidence_count"], 0)

    def test_six_directions_have_concrete_subtracks_and_unestablished_spend(self):
        catalog = load_industries()
        self.assertEqual(len(catalog), 6)
        self.assertFalse({"manufacturing", "agriculture"} & {r["id"] for r in catalog})
        self.assertEqual(sum(len(r["subtracks"]) for r in catalog), 36)
        for row in catalog:
            self.assertEqual(set(row["queries"]), {"zh", "en"})
            for task in row["subtracks"]:
                self.assertTrue(task["job_to_be_done"])
                self.assertTrue(task["existing_behavior_to_verify"])
                self.assertEqual(task["spend_status"], "research_question_not_established")
        self.assertEqual(len(self.discovery["retrieval_plans"]["tikhub"]["requests"]), 12)

    def test_missing_source_configuration_is_a_planning_gap_not_an_index_error(self):
        result = self.build(planned_sources=[])
        paid = result["retrieval_plans"]["tikhub"]
        self.assertFalse(paid["requests"])
        self.assertEqual(len(paid["skipped_requests"]), 12)
        self.assertEqual({r["reason"] for r in paid["skipped_requests"]}, {"no_configured_source_for_language"})

    def test_history_uses_actual_attempts_and_bounds_gap_followup(self):
        task = self.discovery["retrieval_plans"]["tikhub"]["research_tasks"][0]
        identifier, language = task["task_id"], task["language"]
        first = self.build(history={"tasks": {(identifier, language): {"attempts": 1, "review_gaps": ["commercial_benchmark"]}}})
        next_task = first["retrieval_plans"]["tikhub"]["research_tasks"][0]
        self.assertEqual(next_task["task_id"], identifier)
        self.assertEqual(next_task["selection_reason"], "unresolved_evidence_gap")
        second = self.build(history={"tasks": {(identifier, language): {"attempts": 2, "review_gaps": ["commercial_benchmark"]}}})
        self.assertNotEqual(second["retrieval_plans"]["tikhub"]["research_tasks"][0]["task_id"], identifier)
        directory = self.home / "runs/RUN-20260913-1234567890"
        directory.mkdir(parents=True)
        (directory / "industry-coverage.json").write_text(json.dumps({"version": "2.0", "as_of": "2026-09-13",
            "tasks": [{"task_id": identifier, "language": language, "request_count": 0, "planned_request_count": 2}]}))
        actual = self.build()["retrieval_plans"]["tikhub"]["research_tasks"][0]
        self.assertEqual(actual["prior_attempt_count"], 0)

    def test_unpriced_primary_uses_registered_same_query_fallback_under_budget(self):
        plan = deepcopy(self.discovery["retrieval_plans"]["tikhub"])
        primary = plan["requests"][0]
        self.assertTrue(primary["fallback_requests"])
        plan["requests"] = [primary]
        selected = prepare_discovery_plan(plan, prices(exclude=[primary["source"]]), max_cost_usd=0.02, prior_cost_usd="0.01")
        chosen = selected["requests"][0]
        self.assertNotEqual(chosen["source"], primary["source"])
        self.assertEqual(chosen["query_scope"], primary["query_scope"])
        self.assertEqual(chosen["task_id"], primary["task_id"])
        self.assertEqual(chosen["fallback_for_request_id"], primary["id"])
        self.assertEqual(selected["skipped_requests"][0]["replacement_request_id"], chosen["id"])
        self.assertLessEqual(selected["preflight_estimate"]["worst_case_cost_usd"] + 0.01, 0.02)
        with self.assertRaises(ValueError):
            prepare_discovery_plan(plan, prices(exclude=[primary["source"]]), max_cost_usd=0.01, prior_cost_usd="0.01")

    def test_fallback_cannot_silently_change_query_or_endpoint(self):
        plan = deepcopy(self.discovery["retrieval_plans"]["tikhub"])
        primary = plan["requests"][0]
        plan["requests"] = [primary]
        primary["fallback_requests"] = primary["fallback_requests"][:1]
        alternate = primary["fallback_requests"][0]
        alternate["params"][RADAR_ENDPOINTS[alternate["source"]]["keyword_param"]] = "unrelated new query"
        with self.assertRaises(ValueError):
            prepare_discovery_plan(plan, prices(exclude=[primary["source"]]), max_cost_usd=1)
        alternate["endpoint"] = "/api/not-registered"
        with self.assertRaises(ValueError):
            prepare_discovery_plan(plan, prices(exclude=[primary["source"]]), max_cost_usd=1)

    def test_unexecuted_requests_and_attested_pricing_do_not_complete_research(self):
        item = self.evidence(evidence_role="official_pricing", verification={"status": "host_attested"})
        payload = {"run_id": RUN, "evidence": [item], "requests": self.plan["retrieval_plans"]["tikhub"]["requests"]}
        coverage = build_industry_coverage(self.plan, [payload])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["request_count"], 0)
        self.assertEqual(game["verified_evidence_count"], 1)
        self.assertEqual(game["semantic_reviewed_count"], 0)
        self.assertEqual(game["recent_user_behavior_count"], 0)
        self.assertFalse(game["scheduled_research_complete"])
        self.assertTrue(research_quality(coverage, {}, {})["coverage_incomplete"])

    def test_post_and_comments_keep_separate_counts_and_review_is_explicit(self):
        post = self.evidence(relevance_review={"status": "relevant", "reviewer": "researcher", "reviewed_at": "2026-09-14"})
        comment = self.evidence("comment-1", evidence_kind="comment", url_kind="parent_post")
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": [post], "comments": [comment]}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["material_count"], 2)
        self.assertEqual(game["semantic_reviewed_count"], 1)
        self.assertEqual(game["recent_user_behavior_count"], 1)
        self.assertEqual(game["unreviewed_evidence_count"], 1)

    def test_past_and_uncertain_dates_cannot_count_as_recent_behavior(self):
        review = {"status": "relevant", "reviewer": "researcher", "reviewed_at": "2026-09-14"}
        old = self.evidence("old", published_at="2026-01-01", window_status="in_window", relevance_review=review)
        uncertain = self.evidence("uncertain", published_at=None, relevance_review=review,
            published_at_interval={"earliest": "2026-08-01", "latest": "2026-09-01"})
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": [old, uncertain]}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["recent_user_behavior_count"], 0)
        self.assertEqual(game["out_of_window_evidence_count"], 1)
        self.assertEqual(game["uncertain_date_evidence_count"], 1)

    def test_scope_and_old_coverage_version_are_explicit(self):
        outsider = self.evidence(industry_ids=["manufacturing"])
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": [outsider]}])
        self.assertEqual(coverage["out_of_scope_evidence_count"], 1)
        legacy = build_industry_coverage(self.plan, [], version="1.0")
        self.assertEqual(legacy["version"], "1.0")
        self.assertNotIn("version", research_quality(legacy, {}, {}))

    def test_full_index_and_review_queue_do_not_equate_selection_to_review(self):
        rows = [self.evidence(str(i), url=f"https://example.test/{i}") for i in range(8)]
        packet = build_evidence_packet(rows, as_of=DAY.isoformat(), run_id=RUN, max_items=2, max_chars=3000)
        index = evidence_index(rows, packet)
        self.assertEqual(len(index["items"]), 8)
        self.assertEqual(len(index["review_queue"]), 8)
        self.assertEqual(index["selected_unreviewed_count"], 2)
        by_industry = build_industry_packets(rows, as_of=DAY.isoformat(), run_id=RUN, selected_industries=["gaming", "learning"])
        self.assertEqual(len(by_industry["gaming"]["evidence"]), 8)
        self.assertFalse(by_industry["learning"]["evidence"])

    def test_review_revision_mismatch_and_row_level_reuse_are_not_fresh_reviewed_evidence(self):
        review = {"status": "relevant", "reviewer": "researcher", "reviewed_at": "2026-09-14"}
        stale = self.evidence(relevance_review=review)
        stale["relevance_review"]["revision_id"] = "REV-old"
        reused = self.evidence("reused", url="https://example.test/reused", relevance_review=review,
                               run_ids=["RUN-20260913-1234567890"], reused_for_run_id=RUN)
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": [stale, reused]}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["material_count"], 1)
        self.assertEqual(game["historical_evidence_count"], 1)
        self.assertEqual(game["semantic_reviewed_count"], 0)

    def test_raw_and_library_native_object_are_counted_once_after_review_overlay(self):
        raw = self.evidence("reddit:post-1", source_item_id="post-1")
        stored = deepcopy(raw)
        stored.update(id="EV-LIBRARY", evidence_id="EV-LIBRARY", object_identity={"source": "reddit", "kind": "post", "id": "post-1"})
        stored["relevance_review"] = {"status": "relevant", "reviewer": "researcher", "reviewed_at": "2026-09-14",
                                      "evidence_id": "EV-LIBRARY", "revision_id": "REV-1"}
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": [raw]}, {"run_id": RUN, "evidence": [stored]}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["material_count"], 1)
        self.assertEqual(game["semantic_reviewed_count"], 1)

    def test_historical_reference_appendix_cannot_replace_current_revision_review(self):
        current = self.evidence(source_item_id="post-1", object_identity={"source": "reddit", "kind": "post", "id": "post-1"})
        historic = deepcopy(current)
        historic.update(revision_id="REV-old", historical_reference_only=True, reused_for_run_id=RUN)
        historic["relevance_review"] = {"status": "relevant", "reviewer": "researcher", "reviewed_at": "2026-09-14",
                                        "evidence_id": historic["evidence_id"], "revision_id": "REV-old"}
        coverage = build_industry_coverage(self.plan, [{"run_id": RUN, "evidence": [current, historic]}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["material_count"], 1)
        self.assertEqual(game["semantic_reviewed_count"], 0)

    def test_all_review_dimensions_and_fallback_outcomes_remain_separate(self):
        paid = deepcopy(self.discovery["retrieval_plans"]["tikhub"])
        paid["requests"] = [next(r for r in paid["requests"] if r["industry_ids"] == ["gaming"])]
        primary = paid["requests"][0]
        ready = prepare_discovery_plan(paid, prices(exclude=[primary["source"]]), max_cost_usd=1)
        chosen = ready["requests"][0]
        plan = deepcopy(self.plan)
        plan["retrieval_plans"] = {"tikhub": {**paid, "skipped_requests": ready["skipped_requests"]}}
        review = {"status": "relevant", "reviewer": "researcher", "reviewed_at": "2026-09-14"}
        evidence = [self.evidence(role, url=f"https://example.test/{role}", evidence_role=role, relevance_review=review,
                                verification={"status": "host_attested"})
                    for role in ("usage_behavior", "official_pricing", "alternative", "counter_evidence")]
        results = [{**chosen, "status": "ok"}]
        coverage = build_industry_coverage(plan, [{"run_id": RUN, "evidence": evidence, "results": results}])
        game = next(row for row in coverage["industries"] if row["industry_id"] == "gaming")
        self.assertEqual(game["request_count"], 1)
        self.assertEqual(game["pending_request_count"], 0)
        self.assertGreater(game["skipped_request_count"], 0)
        self.assertNotIn("planned_queries", game["review_gaps"])
        self.assertEqual(game["recent_user_behavior_count"], 1)
        self.assertEqual(game["commercial_benchmark_count"], 1)
        self.assertTrue(game["scheduled_research_complete"])
        self.assertFalse(coverage["exhaustive_market_coverage"])

    def test_packet_metadata_removes_repetition_without_touching_quote_offsets(self):
        original = self.evidence(raw_refs=[{"path": "raw/a.json", "json_pointer": "/1"}] * 20,
            raw_file="raw/a.json", same_source_refs=[{"url": "https://example.test"}] * 20,
            aliases=["long-id"] * 30, canonical_url="https://reddit.example/thread/1",
            text_refs=[{"field": "original_text", "start": 1, "end": 4, "text": "ork"}])
        compact = compact_packet_metadata(original)
        self.assertEqual(compact["text_refs"], original["text_refs"])
        self.assertEqual(len(compact["raw_refs"]), 1)
        self.assertEqual(compact["additional_raw_reference_count"], 19)
        self.assertNotIn("aliases", compact)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(original)) // 2)
        self.assertEqual(len(original["raw_refs"]), 20)


if __name__ == "__main__":
    unittest.main()
