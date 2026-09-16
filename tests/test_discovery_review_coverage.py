"""任务多样性、完整续读和研究覆盖不能被摘要容量或已读数量替代。"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.evidence.claims import build_evidence_packet
from aor.evidence.selection import build_review_packets, diversified_evidence, evidence_index, task_family_ids
from aor.sources.coverage import build_industry_coverage, research_quality

DAY = "2026-09-16"
RUN = "RUN-20260916-1234567890"


def evidence(index, *, family="same-task", role="usage_behavior", reviewed=False, **changes):
    row = {"id": f"EV-{index:03}", "evidence_id": f"EV-{index:03}", "revision_id": "REV-1",
           "source": "reddit", "url": f"https://example.test/posts/{index}", "industry_ids": ["personal_life"],
           "observed_at": DAY, "published_at": DAY, "run_id": RUN, "task_family_ids": [family],
           "original_text": "I keep my project changes in a handwritten notebook.", "evidence_role": role}
    row.update(changes)
    if reviewed:
        row["relevance_review"] = {"status": "relevant", "reviewer": "host", "reviewed_at": DAY,
                                   "evidence_id": row["evidence_id"], "revision_id": row["revision_id"]}
    return row


def plan(*, with_tasks=True, selected=True):
    return {"run_id": RUN, "as_of": DAY, "window": {"range_from": "2026-08-17", "range_to": DAY},
            "industry_catalog": [{"id": "personal_life", "name": "生活", "audience": "个人", "demand_model": "consumer"}],
            "selected_industries": ["personal_life"] if selected else [],
            "retrieval_plans": {"web": {"research_tasks": [{"task_id": "family-book", "language": "zh",
                                    "industry_ids": ["personal_life"]}] if with_tasks else []}}}


class DiscoveryReviewCoverageTests(unittest.TestCase):
    def test_primary_fifteen_and_remainder_nine_are_bounded_and_do_not_mark_reviewed(self):
        rows = [evidence(i, family=f"task-{i % 8}") for i in range(24)]
        primary = build_evidence_packet(rows, as_of=DAY, run_id=RUN, max_items=15, max_chars=18000)
        supplement = build_review_packets(rows, primary_packet=primary, as_of=DAY, run_id=RUN,
                                          max_items=4, max_chars=4000)
        self.assertEqual(len(primary["evidence"]), 15)
        self.assertEqual(supplement["manifest"]["outside_primary_count"], 9)
        self.assertFalse(supplement["manifest"]["packet_generation_is_review"])
        self.assertEqual(supplement["manifest"]["next_batch_id"], "READ-0001")
        all_refs = [r["evidence_id"] for r in primary["evidence"]]
        for packet in supplement["packets"]:
            self.assertLessEqual(len(packet["evidence"]), 4)
            self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))), 4000)
            all_refs.extend(r["evidence_id"] for r in packet["evidence"])
        self.assertEqual(len(all_refs), len(set(all_refs)))
        self.assertEqual(set(all_refs), {r["evidence_id"] for r in rows})
        self.assertEqual(evidence_index(rows, primary)["unreviewed_count"], 24)
        self.assertEqual(supplement, build_review_packets(reversed(rows), primary_packet=primary,
                         as_of=DAY, run_id=RUN, max_items=4, max_chars=4000))

    def test_single_task_majority_cannot_hide_new_tasks(self):
        rows = [evidence(i) for i in range(40)]
        rows.extend(evidence(50 + i, family=family) for i, family in enumerate(["wedding", "recipes", "crochet"]))
        first = list(diversified_evidence(rows))[:4]
        self.assertEqual({r["task_family_ids"][0] for r in first}, {"same-task", "wedding", "recipes", "crochet"})
        self.assertEqual(first, list(diversified_evidence(reversed(rows)))[:4])

    def test_supplier_families_cannot_drown_behavior_and_unknown_is_not_invented(self):
        rows = [evidence(i, family=f"supplier-{i}", role="official_pricing", source="web") for i in range(30)]
        rows.extend(evidence(40 + i) for i in range(10))
        first = list(diversified_evidence(rows))[:10]
        self.assertEqual(sum(r["evidence_role"] == "usage_behavior" for r in first), 5)
        unknown = evidence(100, task_family_ids=[], original_text="An entirely unique wedding recipe business")
        self.assertEqual(task_family_ids(unknown), [])
        unknown["provenance"] = [{"task_id": "explicit-task"}]
        self.assertEqual(task_family_ids(unknown), ["explicit-task"])

    def test_ineligible_and_oversize_records_keep_explicit_references(self):
        rows = [evidence(0), evidence(1, published_at="2026-09-17"), evidence(2, is_demo=True),
                evidence(3, retracted=True), evidence(4, task_family_ids=["x" * 10000])]
        primary = build_evidence_packet(rows[:1], as_of=DAY, run_id=RUN)
        supplement = build_review_packets(rows, primary_packet=primary, as_of=DAY, run_id=RUN, max_chars=2000)
        reasons = {r["evidence_id"]: r["reason"] for r in supplement["manifest"]["unassigned_refs"]}
        self.assertEqual(reasons, {"EV-001": "future", "EV-002": "demo", "EV-003": "retracted",
                                   "EV-004": "single_record_exceeds_packet_budget"})
        self.assertIsNone(supplement["manifest"]["next_batch_id"])

    def test_invalid_packet_budgets_are_rejected_even_without_remaining_rows(self):
        for key, values in (("max_items", [0, -1, True, 2.5]), ("max_chars", [0, -1, True, 2.5, 1])):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    build_review_packets([], primary_packet={"evidence": []}, as_of=DAY, run_id=RUN, **{key: value})

    def test_fully_reviewed_empty_focus_does_not_complete_market_research(self):
        rows = [evidence(i, reviewed=True) for i in range(24)]
        coverage = build_industry_coverage(plan(with_tasks=False, selected=False), [{"run_id": RUN, "evidence": rows}])
        quality = research_quality(coverage, {"omitted": {"evidence_budget": 9}}, {}, evidence=rows)
        self.assertFalse(quality["scope_defined"])
        self.assertEqual(quality["unreviewed_evidence_count"], 0)
        self.assertTrue(quality["coverage_incomplete"])
        self.assertIn("research_scope_undefined", quality["scope_gaps"])
        self.assertIn("research_tasks_undefined", quality["scope_gaps"])

    def test_reading_every_batch_still_requires_fresh_behavior_and_commercial_evidence(self):
        rows = [evidence(i, reviewed=True, published_at="2024-01-01", task_id="family-book", language="zh")
                for i in range(24)]
        payload = {"run_id": RUN, "evidence": rows, "results": [{"id": "web-search", "source": "web", "status": "ok",
                   "task_id": "family-book", "language": "zh", "industry_ids": ["personal_life"]}]}
        coverage = build_industry_coverage(plan(), [payload])
        quality = research_quality(coverage, {"omitted": {"evidence_budget": 9}}, {}, evidence=rows)
        self.assertEqual(quality["reviewed_evidence_count"], 24)
        self.assertTrue(quality["coverage_incomplete"])
        self.assertIn("recent_user_behavior", quality["task_gaps"][0]["review_gaps"])
        self.assertIn("commercial_benchmark", quality["task_gaps"][0]["review_gaps"])
        self.assertFalse(any("未审" in item for item in quality["required_followup"]))

    def test_complete_research_does_not_depend_on_primary_packet_capacity(self):
        roles = ["usage_behavior", "official_pricing", "alternative", "counter_evidence"]
        rows = [evidence(i, reviewed=True, role=role, task_id="family-book", language="zh",
                         verification={"status": "host_attested"}) for i, role in enumerate(roles)]
        payload = {"run_id": RUN, "evidence": rows, "results": [{"id": "web-search", "source": "web", "status": "ok",
                   "task_id": "family-book", "language": "zh", "industry_ids": ["personal_life"]}]}
        coverage = build_industry_coverage(plan(), [payload])
        quality = research_quality(coverage, {"omitted": {"evidence_budget": 3}}, {}, evidence=rows)
        self.assertFalse(quality["coverage_incomplete"])
        self.assertFalse(quality["market_absence_established"])
        self.assertEqual(quality["required_followup"], [])

    def test_explicit_focus_task_has_scope_but_imported_task_alone_does_not_define_it(self):
        custom = plan(with_tasks=False, selected=False)
        row = evidence(1, reviewed=True, task_id="custom", language="zh")
        payload = {"run_id": RUN, "evidence": [row]}
        self.assertFalse(build_industry_coverage(custom, [payload])["scope_defined"])
        custom["research_tasks"] = [{"task_id": "custom", "language": "zh"}]
        coverage = build_industry_coverage(custom, [payload])
        self.assertTrue(coverage["scope_defined"])
        self.assertTrue(research_quality(coverage, {}, {}, evidence=[row])["coverage_incomplete"])

    def test_planned_web_imports_define_scope_and_collected_material_needs_no_fake_request(self):
        custom = plan(with_tasks=False, selected=False)
        custom["retrieval_plans"] = {"web_import": {"required_imports": [
            {"id": "manual-1", "source": "web", "task_id": "custom", "locale": {"language": "zh"}}]}}
        rows = [evidence(i, reviewed=True, role=role, task_id="custom", language="zh",
                         verification={"status": "host_attested"}) for i, role in enumerate(
                         ["usage_behavior", "official_pricing", "alternative", "counter_evidence"])]
        coverage = build_industry_coverage(custom, [{"run_id": RUN, "evidence": rows}])
        self.assertTrue(coverage["scope_defined"])
        task = coverage["tasks"][0]
        self.assertTrue(task["scheduled"])
        self.assertEqual(task["request_count"], 0)
        self.assertTrue(task["scheduled_research_complete"])
        self.assertFalse(research_quality(coverage, {}, {}, evidence=rows)["coverage_incomplete"])

    def test_old_coverage_and_quality_version_remain_reproducible(self):
        old = build_industry_coverage(plan(with_tasks=False, selected=False), [], version="2.0")
        self.assertNotIn("scope_defined", old)
        self.assertNotIn("scope_gaps", old)
        quality = research_quality(old, {}, {}, evidence=[])
        self.assertEqual(quality["version"], "2.0")
        self.assertFalse(quality["coverage_incomplete"])
        self.assertEqual(old, build_industry_coverage(plan(with_tasks=False, selected=False), [], version="2.0"))
        new = deepcopy(old)
        new["version"] = "2.1"
        self.assertTrue(research_quality(new, {}, {}, evidence=[])["coverage_incomplete"])


if __name__ == "__main__":
    unittest.main()
