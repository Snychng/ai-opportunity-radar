"""按固定截止日生成可解释的复核期限，不能用发布批准刷新来源日期。"""

from copy import deepcopy
import json
import unittest

from tests import test_public_contract as fixtures
from tests import test_site_catalog as site_fixtures
from aor.reporting.public import build_refresh_tasks, export_public, write_public_bundle
from aor.reporting.public_contract import validate_public_dataset
from aor.reporting.site import build_site_catalog


class PublicFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PublicContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def reviewed_record(self, day="2026-09-14"):
        row = deepcopy(self.fixture.stored)
        row["relevance_review"] = {"status": "relevant", "reviewer": "测试复核", "reviewed_at": day,
                                    "evidence_id": row["evidence_id"], "revision_id": row["revision_id"]}
        return row

    def report(self):
        return self.fixture.report(records=[self.reviewed_record()])

    def test_explicit_date_controls_due_status_and_does_not_withdraw_history(self):
        previous = None
        for day, status in (("2026-09-14", "fresh"), ("2026-10-07", "due"),
                            ("2026-10-14", "due"), ("2026-10-15", "overdue")):
            public, withheld = export_public(self.report(), as_of=day)
            item = public["items"][0]
            self.assertEqual(item["freshness"]["status"], status)
            self.assertEqual(item["freshness"]["review_due_at"], "2026-10-14")
            self.assertEqual(item["publication_status"], "published")
            self.assertFalse(withheld)
            self.assertEqual(public["source_runs"][0]["as_of"], "2026-09-14")
            self.assertTrue(validate_public_dataset(public)["valid"])
            if previous:
                self.assertEqual(previous["id"], item["id"])
                self.assertNotEqual(previous["revision_id"], item["revision_id"])
            previous = item

    def test_publication_approval_without_semantic_review_is_unknown(self):
        public, _ = export_public(self.fixture.report())
        freshness = public["items"][0]["freshness"]
        self.assertEqual(freshness["publication_checked_at"], "2026-09-14")
        self.assertEqual(freshness["source_observed_at"], "2026-09-14")
        self.assertIsNone(freshness["semantic_reviewed_at"])
        self.assertIsNone(freshness["last_verified_at"])
        self.assertEqual(freshness["status"], "unknown")

    def test_oldest_needed_source_or_semantic_review_limits_freshness(self):
        record = self.reviewed_record()
        record["observed_at"] = "2026-09-01"
        public, _ = export_public(self.fixture.report(records=[record]))
        freshness = public["items"][0]["freshness"]
        self.assertEqual(freshness["semantic_reviewed_at"], "2026-09-14")
        self.assertEqual(freshness["last_verified_at"], "2026-09-01")
        self.assertEqual(freshness["review_due_at"], "2026-10-01")
        record["relevance_review"]["revision_id"] = "stale-review"
        public, _ = export_public(self.fixture.report(records=[record]))
        self.assertEqual(public["items"][0]["freshness"]["status"], "unknown")

    def test_configuration_and_all_views_are_valid_and_tasks_remain_private(self):
        output = self.fixture.home / "site"
        result = write_public_bundle(self.report(), output, as_of="2026-09-25", review_period_days=10, due_soon_days=2)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(len(result["refresh_tasks"]), 1)
        task = result["refresh_tasks"][0]
        self.assertEqual(task["reason"], "overdue")
        self.assertEqual(task["execution_status"], "not_started")
        self.assertTrue(task["evidence_refs"][0]["url"].startswith("https://"))
        self.assertTrue(task["evidence_refs"][0]["revision_id"])
        for path in output.rglob("*.json"):
            value = json.loads(path.read_text())
            self.assertTrue(validate_public_dataset(value)["valid"])
            self.assertEqual(value["items"][0]["freshness"]["status"], "overdue")
            self.assertNotIn("refresh_tasks", value)
            self.assertNotIn("/Users/", path.read_text())

    def test_old_public_without_optional_fields_stays_compatible_and_false_freshness_fails(self):
        public, _ = export_public(self.report())
        old = deepcopy(public)
        old.pop("freshness_policy")
        for item in old["items"]:
            item.pop("freshness")
        for record in old["evidence"]:
            record.pop("source_observed_at")
            record.pop("semantic_reviewed_at")
        self.assertTrue(validate_public_dataset(old)["valid"])
        false = deepcopy(public)
        false["items"][0]["freshness"]["review_due_at"] = "2026-12-31"
        self.assertFalse(validate_public_dataset(false)["valid"])
        false = deepcopy(public)
        false["evidence"][0]["semantic_reviewed_at"] = None
        self.assertFalse(validate_public_dataset(false)["valid"])
        for kwargs in ({"as_of": "2026-09-13"}, {"review_period_days": 0}, {"review_period_days": 10, "due_soon_days": 10}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                export_public(self.report(), **kwargs)

    def test_absent_item_ages_and_later_source_review_does_not_change_publication_date(self):
        case = site_fixtures.SiteCatalogTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        first_record = deepcopy(case.fixture.stored)
        first_record["relevance_review"] = {"status": "relevant", "reviewer": "测试复核", "reviewed_at": "2026-09-14",
            "evidence_id": first_record["evidence_id"], "revision_id": first_record["revision_id"]}
        first = case.report(records=[first_record])
        empty = case.report(day="2026-10-15", empty=True, records=[])
        aged, _ = build_site_catalog([empty, first], as_of="2026-10-16")
        item = aged["items"][0]
        self.assertEqual(item["freshness"]["status"], "overdue")
        self.assertEqual(item["publication_status"], "published")
        self.assertEqual(aged["site_manifest"]["retained_from_history_count"], 1)
        refreshed = deepcopy(first_record)
        refreshed["observed_at"] = "2026-10-15"
        # 新近语义复核但旧来源，或新来源但旧复核，均不能宣称完整新鲜。
        source_only = case.report(day="2026-10-15", empty=True, records=[refreshed])
        catalog, _ = build_site_catalog([first, source_only])
        self.assertEqual(catalog["items"][0]["freshness"]["last_verified_at"], "2026-09-14")
        refreshed["relevance_review"]["reviewed_at"] = "2026-10-15"
        current = case.report(day="2026-10-15", empty=True, records=[refreshed])
        catalog, _ = build_site_catalog([first, current], as_of="2026-10-16")
        item = catalog["items"][0]
        self.assertEqual(item["freshness"]["status"], "fresh")
        self.assertEqual(item["freshness"]["last_verified_at"], "2026-10-15")
        self.assertEqual(item["freshness"]["publication_checked_at"], "2026-09-14")
        self.assertEqual(build_refresh_tasks(catalog), [])

    def test_archived_items_are_not_automatically_reopened_or_scheduled(self):
        row = {**deepcopy(self.fixture.row), "research_status": "archived"}
        public, _ = export_public(self.fixture.report(row), as_of="2026-12-01")
        self.assertEqual(public["items"][0]["publication_status"], "withdrawn")
        self.assertEqual(build_refresh_tasks(public), [])


if __name__ == "__main__":
    unittest.main()
