"""跨运行发布回归：稳定更新、历史保留、显式失效与最新行业范围。"""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from tests import test_public_contract as fixtures
from aor.reporting.public import export_public
from aor.reporting.public_contract import validate_public_dataset
from aor.reporting.report import build_report
from aor.reporting.site import build_site_catalog, write_site_bundle
from build_query_plan import build_plan


class SiteCatalogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PublicContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home

    def report(self, *, day="2026-09-14", number=1, row=None, empty=False, approve=True,
               records=None, scope=None, generated_at=None):
        plan = build_plan(date.fromisoformat(day), self.home)
        run_id = "RUN-" + day.replace("-", "") + "-" + f"{number:010X}"
        plan.update(run_id=run_id, as_of=day)
        if scope is not None:
            plan["selected_industries"] = scope
        evidence = deepcopy([self.fixture.stored] if records is None else records)
        row = deepcopy(self.fixture.row if row is None else row)
        row.update(as_of=day, run_id=run_id)
        if approve:
            self.fixture.approve(row)
        tiered = {"schema_version": "3.0", "as_of": day, "run_id": run_id, "deep_candidates": [],
                  "validated_ideas": [], "regional_signals": [], "rejected": [], "research_leads": [] if empty else [row]}
        result = build_report(tiered, decision={"summary": "保留研究结论", "largest_unknown": "仍需验证用户行为",
            "next_action": "继续核验", "stop_condition": "没有新增有效证据时停止"}, claim_evidence=evidence,
            evidence=[{"run_id": run_id, "as_of": day, "evidence": evidence}], research_plan=plan)
        result["generated_at"] = generated_at or day + "T08:00:00Z"
        return result

    def test_revised_text_upserts_one_stable_item_and_input_order_is_irrelevant(self):
        first = self.report()
        row = deepcopy(self.fixture.row)
        row["problem_or_desire"] = "玩家想减少每周重复确认成员空闲时间的沟通"
        second = self.report(day="2026-09-15", row=row)
        catalog, withheld = build_site_catalog([second, first])
        reverse, _ = build_site_catalog([first, second, deepcopy(first)])
        self.assertEqual(catalog, reverse)
        self.assertFalse(withheld)
        self.assertEqual(catalog["snapshot_scope"], "site_catalog")
        self.assertEqual(catalog["dataset_id"], "SITE-DEFAULT")
        self.assertEqual(len(catalog["items"]), 1)
        self.assertEqual(catalog["items"][0]["summary"], row["problem_or_desire"])
        self.assertEqual(catalog["items"][0]["id"], self.fixture.row["lead_id"])
        self.assertEqual(catalog["items"][0]["source_run_ids"], [first["run_id"], second["run_id"]])
        self.assertEqual(catalog["site_manifest"]["published_count"], 1)
        self.assertEqual(catalog["site_manifest"]["source_run_count"], 2)

    def test_absent_old_item_is_retained_and_coverage_is_not_double_counted(self):
        first = self.report()
        later = self.report(day="2026-09-15", empty=True, records=[])
        catalog, _ = build_site_catalog([first, later])
        self.assertEqual(len(catalog["items"]), 1)
        self.assertEqual(catalog["items"][0]["publication_status"], "published")
        self.assertEqual(catalog["site_manifest"]["retained_from_history_count"], 1)
        self.assertEqual(catalog["coverage"], export_public(later)[0]["coverage"])
        self.assertEqual(catalog["site_manifest"]["coverage_basis"], "latest_research_run")

    def test_full_library_state_withdraws_item_without_loading_its_body(self):
        from aor.reporting.report import validate_structured_report

        first = self.report()
        later = self.report(day="2026-09-15", empty=True, records=[])
        stored = self.fixture.stored
        later["current_evidence_state"] = {stored["evidence_id"]: {
            "current_revision_id": stored["revision_id"], "retracted": False, "status": "superseded"}}
        self.assertTrue(validate_structured_report(later)["valid"])
        catalog, _ = build_site_catalog([first, later])
        self.assertEqual(catalog["items"][0]["publication_status"], "withdrawn")
        later["current_evidence_state"][stored["evidence_id"]]["current_revision_id"] = "other:invalid"
        self.assertFalse(validate_structured_report(later)["valid"])

    def test_later_withheld_item_retracts_old_publication_and_removes_evidence(self):
        first = self.report()
        later = self.report(day="2026-09-15", approve=False)
        catalog, withheld = build_site_catalog([first, later])
        self.assertEqual(len(withheld), 1)
        self.assertEqual(catalog["items"][0]["publication_status"], "withdrawn")
        self.assertEqual(catalog["items"][0]["evidence_refs"], [])
        self.assertEqual(catalog["evidence"], [])
        self.assertEqual(catalog["site_manifest"]["published_count"], 0)
        self.assertEqual(catalog["site_manifest"]["tombstones"][0]["reason"], "withheld")
        self.assertNotEqual(catalog["items"][0]["summary"], self.fixture.row["problem_or_desire"])

    def test_explicit_archive_is_a_traceable_tombstone(self):
        first = self.report()
        row = {**deepcopy(self.fixture.row), "research_status": "archived"}
        later = self.report(day="2026-09-15", row=row, approve=False)
        catalog, withheld = build_site_catalog([first, later])
        self.assertFalse(withheld)
        self.assertEqual(catalog["items"][0]["publication_status"], "withdrawn")
        self.assertEqual(catalog["site_manifest"]["tombstones"][0]["reason"], "explicit_withdrawal")

    def test_new_source_retraction_removes_old_item_even_if_not_repeated_in_new_run(self):
        first = self.report()
        withdrawn = {**deepcopy(self.fixture.stored), "retracted": True,
                     "revision_id": self.fixture.stored["evidence_id"] + ":retracted", "original_text": "原文已撤回"}
        later = self.report(day="2026-09-15", empty=True, records=[withdrawn])
        catalog, withheld = build_site_catalog([first, later])
        self.assertEqual(catalog["items"][0]["publication_status"], "withdrawn")
        self.assertEqual(catalog["evidence"], [])
        self.assertIn("当前已撤回", withheld[0]["reason"])

    def test_latest_scope_removes_excluded_items_and_exposes_delete_manifest(self):
        first = self.report()
        later = self.report(day="2026-09-15", empty=True, records=[], scope=["ecommerce"])
        catalog, _ = build_site_catalog([first, later])
        self.assertEqual(catalog["items"], [])
        self.assertEqual({row["id"] for row in catalog["industries"]}, {"ecommerce"})
        self.assertEqual(catalog["site_manifest"]["scope_removed_ids"], [self.fixture.row["lead_id"]])
        self.assertEqual(catalog["site_manifest"]["tombstones"][0]["reason"], "scope_excluded")

    def test_same_day_order_uses_timestamp_then_run_id_and_conflicting_ids_fail(self):
        first = self.report(number=1, generated_at="2026-09-14T09:00:00+08:00")
        second_row = {**deepcopy(self.fixture.row), "problem_or_desire": "更晚研究确认的玩家协调需求"}
        second = self.report(number=2, row=second_row, generated_at="2026-09-14T02:00:00Z")
        catalog, _ = build_site_catalog([second, first])
        self.assertEqual(catalog["items"][0]["summary"], second_row["problem_or_desire"])
        third = self.report(number=3, approve=False, generated_at="2026-09-14T02:00:00Z")
        catalog, _ = build_site_catalog([third, second, first])
        self.assertEqual(catalog["items"][0]["publication_status"], "withdrawn")
        conflicting = deepcopy(first)
        conflicting["generated_at"] = "2026-09-14T02:00:00Z"
        with self.assertRaisesRegex(ValueError, "冲突报告"):
            build_site_catalog([first, conflicting])

    def test_old_report_is_rejected_and_current_snapshot_cannot_masquerade_as_site(self):
        old = self.report()
        old["report_version"] = "1.0"
        with self.assertRaisesRegex(ValueError, "旧档案"):
            build_site_catalog([old])
        with self.assertRaises(ValueError):
            build_site_catalog([])
        public, _ = export_public(self.report())
        public["snapshot_scope"] = "site_catalog"
        self.assertFalse(validate_public_dataset(public)["valid"])

    def test_reapproved_item_returns_without_stale_tombstone(self):
        reports = [self.report(), self.report(day="2026-09-15", approve=False), self.report(day="2026-09-16")]
        catalog, _ = build_site_catalog(reports)
        self.assertEqual(catalog["items"][0]["publication_status"], "published")
        self.assertFalse(catalog["site_manifest"]["tombstones"])
        self.assertEqual(catalog["quality"]["withheld_item_count"], 0)
        self.assertEqual(len(catalog["items"][0]["source_run_ids"]), 3)

    def test_site_bundle_validates_three_views_and_replaces_old_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            first = self.report()
            write_site_bundle([first], output, site_id="SITE-test")
            later = self.report(day="2026-09-15", approve=False)
            result = write_site_bundle([first, later], output, site_id="SITE-test")
            self.assertEqual(result["published_count"], 0)
            self.assertEqual(result["withdrawn_count"], 1)
            for path in output.rglob("*.json"):
                public = json.loads(path.read_text())
                self.assertTrue(validate_public_dataset(public)["valid"], path)
                self.assertEqual(public["snapshot_scope"], "site_catalog")
                self.assertEqual(public["items"][0]["publication_status"], "withdrawn")

    def reviewed_record(self):
        record = deepcopy(self.fixture.stored)
        record["relevance_review"] = {"status": "relevant", "reviewer": "site-reviewer",
            "reviewed_at": "2026-09-14", "evidence_id": record["evidence_id"],
            "revision_id": record["revision_id"]}
        return record

    def test_retained_observed_need_rechecks_same_revision_review_and_role_in_all_views(self):
        for change in ("unrelated", "role_changed", "invalid_review_binding"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                record = self.reviewed_record()
                row = {**deepcopy(self.fixture.row), "research_status": "observed_need", "evidence": [record]}
                first = self.report(row=row, records=[record])
                changed = deepcopy(record)
                changed["last_observed_at"] = "2026-09-15"
                changed["relevance_review"]["reviewed_at"] = "2026-09-15"
                if change == "unrelated":
                    changed["relevance_review"]["status"] = "unrelated"
                elif change == "role_changed":
                    changed["evidence_role"] = "product_update"
                else:
                    changed["relevance_review"]["revision_id"] = "stale-review"
                later = self.report(day="2026-09-15", empty=True, records=[changed])
                direct, direct_withheld = export_public(self.report(day="2026-09-15", row=row, records=[changed]))
                self.assertEqual(direct["items"], [])
                self.assertIn("已核验行为", direct_withheld[0]["reason"])
                output = Path(directory) / "site"
                result = write_site_bundle([later, first], output)
                self.assertEqual(result["withdrawn_count"], 1)
                self.assertEqual(result["published_count"], 0)
                self.assertIn("已核验行为", result["withheld"][0]["reason"])
                documents = [json.loads(path.read_text()) for path in output.rglob("*.json")]
                self.assertEqual({doc["view"] for doc in documents}, {"full", "index", "detail"})
                for document in documents:
                    self.assertTrue(validate_public_dataset(document)["valid"])
                    item = document["items"][0]
                    self.assertEqual(item["publication_status"], "withdrawn")
                    self.assertEqual(item["freshness"]["status"], "unknown")
                    if document["view"] != "index":
                        self.assertEqual(item["evidence_refs"], [])
                        self.assertEqual(document["evidence"], [])

    def test_negative_review_does_not_refresh_retained_hypothesis_or_reopen_observed_need(self):
        record = self.reviewed_record()
        first = self.report(records=[record])
        changed = deepcopy(record)
        changed["last_observed_at"] = "2026-09-15"
        changed["relevance_review"].update(status="unrelated", reviewed_at="2026-09-15")
        later = self.report(day="2026-09-15", empty=True, records=[changed])
        catalog, withheld = build_site_catalog([first, later])
        self.assertFalse(withheld)  # 假设门槛不额外升级为 observed_need 门槛。
        self.assertEqual(catalog["items"][0]["publication_status"], "published")
        self.assertEqual(catalog["items"][0]["freshness"]["status"], "unknown")
        self.assertIsNone(catalog["items"][0]["freshness"]["semantic_reviewed_at"])
        row = {**deepcopy(self.fixture.row), "research_status": "observed_need", "evidence": [record]}
        observed = self.report(row=row, records=[record])
        restored = deepcopy(record)
        restored["last_observed_at"] = "2026-09-16"
        restored["relevance_review"]["reviewed_at"] = "2026-09-16"
        source_only = self.report(day="2026-09-16", empty=True, records=[restored])
        catalog, _ = build_site_catalog([observed, later, source_only])
        self.assertEqual(catalog["items"][0]["publication_status"], "withdrawn")
        reapproved = self.report(day="2026-09-17", row=row, records=[restored])
        catalog, _ = build_site_catalog([observed, later, source_only, reapproved])
        self.assertEqual(catalog["items"][0]["publication_status"], "published")
        self.assertEqual(catalog["items"][0]["freshness"]["last_verified_at"], "2026-09-16")

    def test_other_current_behavior_still_supports_need_and_historical_review_does_not_override_it(self):
        record = self.reviewed_record()
        other = fixtures.EvidenceLibrary._event({"source": "reddit", "source_item_id": "post-2",
            "evidence_kind": "post", "url": "https://www.reddit.com/r/gaming/comments/post2/",
            "title": "另一位玩家", "original_text": "我们固定每周五组队，总是有人临时改时间。",
            "observed_at": "2026-09-14", "published_at": "2026-09-14", "industry_ids": ["gaming"],
            "evidence_role": "usage_behavior", "window_status": "in_window"},
            as_of="2026-09-14", run_id=self.fixture.plan["run_id"], raw_ref=None)["record"]
        other["relevance_review"] = {**record["relevance_review"], "evidence_id": other["evidence_id"],
                                     "revision_id": other["revision_id"]}
        row = {**deepcopy(self.fixture.row), "research_status": "observed_need", "evidence": [record, other]}
        first = self.report(row=row, records=[record, other])
        changed = deepcopy(record)
        changed["evidence_role"] = "product_update"
        later = self.report(day="2026-09-15", empty=True, records=[changed])
        catalog, withheld = build_site_catalog([first, later])
        self.assertFalse(withheld)
        self.assertEqual(catalog["items"][0]["publication_status"], "published")
        public_record = next(r for r in catalog["evidence"] if r["id"] == record["evidence_id"])
        self.assertEqual(public_record["role"], "product_update")
        historical = {**deepcopy(other), "historical_reference_only": True, "evidence_role": "product_update"}
        historical["relevance_review"]["status"] = "unrelated"
        latest = self.report(day="2026-09-16", empty=True, records=[historical])
        catalog, withheld = build_site_catalog([first, later, latest])
        self.assertFalse(withheld)
        self.assertEqual(catalog["items"][0]["publication_status"], "published")


if __name__ == "__main__":
    unittest.main()
