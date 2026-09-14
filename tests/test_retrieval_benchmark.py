from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.evidence.benchmark import evaluate_retrieval


class RetrievalBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"evidence_id": "E1", "revision_id": "R1", "source": "forum",
                      "industry_ids": ["gaming"], "query": "找固定队友",
                      "original_text": "下班后想找固定队友一起玩。", "observed_at": "2026-09-14"},
                     {"evidence_id": "E2", "revision_id": "R2", "source": "forum",
                      "industry_ids": ["gaming"], "query": "找固定队友",
                      "original_text": "下载我的组队神器。", "observed_at": "2026-09-14"}]
        self.labels = {"schema_version": "retrieval-labels-1", "labels": [
            {"evidence_id": "E1", "revision_id": "R1", "actor": "user", "query_match": "direct",
             "signal": "task", "quote": "下班后想找固定队友", "rationale": "明确个人任务",
             "reviewer": "fixture", "reviewed_at": "2026-09-14"},
            {"evidence_id": "E2", "revision_id": "R2", "actor": "supplier", "query_match": "direct",
             "signal": "promotion", "quote": "下载我的组队神器", "rationale": "工具推广",
             "reviewer": "fixture", "reviewed_at": "2026-09-14"}]}

    def evaluate(self, rows=None, labels=None, **kwargs):
        return evaluate_retrieval(rows or self.rows, labels or self.labels, as_of="2026-09-14", **kwargs)

    def test_vendor_is_not_direct_user_signal(self):
        result = self.evaluate(estimated_cost_usd=0.02)
        self.assertEqual(result["summary"]["direct_user_signal_count"], 1)
        self.assertEqual(result["summary"]["supplier_count"], 1)
        self.assertEqual(result["cost"]["estimated_usd_per_direct_user_signal"], 0.02)

    def test_unlabeled_denominator_is_explicit(self):
        labels = deepcopy(self.labels)
        labels["labels"].pop()
        summary = self.evaluate(labels=labels)["summary"]
        self.assertEqual(summary["label_coverage"], 0.5)
        self.assertEqual(summary["unlabeled_count"], 1)
        self.assertEqual(summary["direct_user_share_of_labeled"], 1.0)

    def test_duplicate_input_is_not_extra_signal(self):
        result = self.evaluate(rows=[*self.rows, self.rows[0]])
        self.assertEqual(result["duplicate_revision_count"], 1)
        self.assertEqual(result["summary"]["direct_user_signal_count"], 1)

    def test_old_and_unknown_publication_are_not_recent(self):
        for published in (None, "2014-01-01"):
            rows = deepcopy(self.rows)
            rows[0]["published_at"] = published
            result = self.evaluate(rows=rows)["summary"]
            self.assertEqual(result["direct_user_signal_count"], 1)
            self.assertEqual(result["recent_direct_user_signal_count"], 0)
        rows[0]["published_at"] = "2026-09-02"
        self.assertEqual(self.evaluate(rows=rows)["summary"]["recent_direct_user_signal_count"], 1)

    def test_revision_quote_and_review_time_are_bound(self):
        for field, value in [("revision_id", "changed"), ("quote", "没有这句"),
                             ("reviewed_at", "2026-09-15"), ("reviewed_at", "2026-09-13"),
                             ("actor", "customer"), ("reviewer", "")]:
            with self.subTest(field=field):
                labels = deepcopy(self.labels)
                labels["labels"][0][field] = value
                with self.assertRaises(ValueError):
                    self.evaluate(labels=labels)

    def test_missing_query_requires_unknown_match(self):
        rows = deepcopy(self.rows)
        rows[0].pop("query")
        with self.assertRaises(ValueError):
            self.evaluate(rows=rows)
        labels = deepcopy(self.labels)
        labels["labels"][0]["query_match"] = "unknown"
        self.assertEqual(self.evaluate(rows=rows, labels=labels)["summary"]["direct_user_signal_count"], 0)

    def test_parent_cannot_borrow_comment_quote(self):
        rows = deepcopy(self.rows)
        rows[0].update(original_text="下载我的工具", comments=[{"original_text": "下班后想找固定队友"}])
        with self.assertRaises(ValueError):
            self.evaluate(rows=rows)

    def test_statistical_metadata_conflict_is_not_order_dependent(self):
        for field, value in [("query", "其他任务"), ("published_at", "2014-01-01")]:
            changed = dict(self.rows[0], **{field: value})
            for rows in ([*self.rows, changed], [changed, *self.rows]):
                with self.assertRaises(ValueError):
                    self.evaluate(rows=rows)

    def test_invalid_shape_missing_query_and_future_utc_observation(self):
        for change in ({"industry_ids": "gaming"}, {"query": []},
                       {"observed_at": "2026-09-14T23:30:00-07:00"}):
            rows = [dict(self.rows[0], **change), self.rows[1]]
            with self.assertRaises(ValueError):
                self.evaluate(rows=rows)
        with self.assertRaises(ValueError):
            self.evaluate(labels={"schema_version": "retrieval-labels-1", "labels": ["not-an-object"]})
        for status in ("adjacent", "unrelated"):
            labels = deepcopy(self.labels)
            labels["labels"][0]["query_match"] = status
            with self.assertRaises(ValueError):
                self.evaluate(rows=[dict(self.rows[0], query=""), self.rows[1]], labels=labels)

    def test_ambiguous_revisions_and_duplicate_labels_rejected(self):
        row = dict(self.rows[0], revision_id="new")
        with self.assertRaises(ValueError):
            self.evaluate(rows=[*self.rows, row])
        labels = deepcopy(self.labels)
        labels["labels"].append(labels["labels"][0])
        with self.assertRaises(ValueError):
            self.evaluate(labels=labels)

    def test_zero_signal_and_cost_are_not_division_by_zero(self):
        result = self.evaluate(rows=self.rows[1:], labels={"schema_version": "retrieval-labels-1",
                              "labels": self.labels["labels"][1:]}, estimated_cost_usd=0)
        self.assertIsNone(result["cost"]["estimated_usd_per_direct_user_signal"])
        for cost in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.evaluate(estimated_cost_usd=cost)


if __name__ == "__main__":
    unittest.main()
