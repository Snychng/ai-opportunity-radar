"""任务优先观察：跨产品身份、正向行为、假设边界及旧报告兼容。"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.opportunity.needs import build_user_discovery, public_discovery_counts, validate_user_discovery
from aor.reporting.public import export_public
from aor.reporting.report import build_report, render_report, validate_structured_report
from tests.test_evidence_claims import evidence

DAY = "2026-09-16"
RUN = "RUN-20260916-ABCDEF1234"


def observation(**changes):
    return {"target_user": "用户", "task": "导出报告", "need": "缩短导出耗时", "industry_ids": ["ecommerce"],
            "feedback_type": "usage", "sentiment": "negative", "evidence_refs": [
                {"evidence_id": "EVID-1", "revision_id": "REV-1", "quote": "Setup still takes hours."}], **changes}


def build(rows, records=None, **options):
    return build_user_discovery(rows, records or [evidence()], as_of=DAY, run_id=RUN, **options)


def legacy_fixture():
    """发布 4.3.1 的固定快照；不通过当前 builder 生成期望值。"""
    row = observation(product="TestProduct")
    row.update(observation_id="OBS-0F0456AEA6E9AF98", cluster_id="NEED-6BAFEFF4A176DE04",
               sources=["community"], is_demo=False, classification_status="host_classified",
               authenticity="not_independently_verified", market_validated=False)
    row["evidence_refs"][0].update(field="original_text", start=26, end=50)
    return {"version": "1.0", "run_id": RUN, "as_of": DAY, "observations": [row], "demand_clusters": [{
        "cluster_id": "NEED-6BAFEFF4A176DE04", "product": "TestProduct", "target_user": "用户", "task": "导出报告",
        "need": "缩短导出耗时", "industry_ids": ["ecommerce"], "observation_ids": ["OBS-0F0456AEA6E9AF98"],
        "evidence_ids": ["EVID-1"], "sources": ["community"], "status": "needs_verification",
        "market_validated": False, "independent_user_count": None}], "summary": {"observation_count": 1,
        "demand_cluster_count": 1, "unique_evidence_count": 1, "feedback_types": {"usage": 1}, "market_validated": False}}


class TaskObservationTests(unittest.TestCase):
    def test_productless_task_needs_no_payment_ai_or_mvp_and_keeps_unknown_language(self):
        result = build([observation(trigger="交付前", current_workaround="逐项手工核对", desired_outcome="按时交付",
                                    artifact="核对清单")])
        row = result["observations"][0]
        self.assertEqual(result["version"], "2.0")
        self.assertEqual(row["products"], [])
        self.assertEqual(row["language"], "unknown")
        self.assertEqual(row["artifact"], "核对清单")
        self.assertEqual(result["summary"]["artifact_count"], 1)
        self.assertEqual(result["summary"]["task_family_count"], 1)
        self.assertFalse(row["market_validated"])
        self.assertIsNone(result["demand_clusters"][0]["independent_user_count"])
        validate_user_discovery(result, [evidence()], as_of=DAY, run_id=RUN)

    def test_products_merge_without_creating_extra_observation_or_need(self):
        left, right = observation(product="Spreadsheet"), observation(products=["Notebook", "Spreadsheet"])
        result = build([left, right])
        self.assertEqual(result, build([right, left]))
        self.assertEqual(result["summary"]["observation_count"], 1)
        self.assertEqual(result["summary"]["demand_cluster_count"], 1)
        self.assertEqual(result["demand_clusters"][0]["products"], ["Notebook", "Spreadsheet"])
        self.assertEqual(build([left])["observations"][0]["observation_id"],
                         build([right])["observations"][0]["observation_id"])

    def test_different_users_constraints_and_tasks_do_not_collapse(self):
        result = build([observation(), observation(constraints=["完全离线"]),
                        observation(target_user="多人工作室"), observation(task="归档报告")])
        self.assertEqual(result["summary"]["demand_cluster_count"], 4)
        self.assertEqual(result["summary"]["task_family_count"], 4)
        first, second = observation(constraints=["Offline", "Local"]), observation(constraints=["Local", "Offline"])
        self.assertEqual(build([first]), build([second]))
        family = build([observation(), observation(need="保留修改记录")])
        self.assertEqual(family["summary"]["demand_cluster_count"], 2)
        self.assertEqual(family["summary"]["task_family_count"], 1)

    def test_positive_behaviors_and_delivery_hypotheses_stay_separate(self):
        proposals = [{"delivery_form": form, "statement": "辅助完成同一份报告", "status": "hypothesis"}
                     for form in ("one_off_delivery", "human_assisted_service", "plugin", "studio_tool", "subscription_software")]
        row = observation(feedback_type="positive_behavior", sentiment="positive", behavior_type="sharing",
                          artifact="家庭报告", solution_hypotheses=proposals)
        result = build([row])
        self.assertEqual(result["summary"]["positive_behavior_count"], 1)
        self.assertEqual(result["summary"]["delivery_hypothesis_count"], 5)
        self.assertEqual(result["summary"]["demand_cluster_count"], 1)
        self.assertEqual(result["observations"][0]["cluster_id"], build([observation()])["observations"][0]["cluster_id"])
        for status in ("supported", "verified", "market_validated"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                build([observation(solution_hypotheses=[{**proposals[0], "status": status}])])
        for kind in ("promotion", "official_response", "suspected_spam", "unknown"):
            excluded = build([{**row, "feedback_type": kind}])
            self.assertEqual(excluded["summary"]["task_family_count"], 0)
            self.assertEqual(excluded["summary"]["positive_behavior_count"], 0)
            self.assertEqual(excluded["summary"]["delivery_hypothesis_count"], 0)
        self.assertEqual(build([row], [evidence(is_demo=True)])["demand_clusters"], [])

    def test_bounded_context_and_explicit_reference_validation(self):
        for changes in ({"trigger": "x" * 501}, {"artifact": []}, {"constraints": ["x"] * 11},
                        {"language": "auto"}, {"query_terms": ["x" * 101]}, {"behavior_type": "paid"},
                        {"solution_hypotheses": [{"delivery_form": "saas", "statement": "x", "status": "hypothesis"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build([observation(**changes)])
        for change in ({"quote": "invented"}, {"revision_id": None}, {"evidence_id": None}):
            row = observation()
            row["evidence_refs"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                build([row])
        for changes in ({"status": "ambiguous"}, {"status": "needs_review"}, {"historical_reference_only": True},
                        {"retracted": True}, {"derivation_status": "superseded"}, {"observed_at": "2026-09-17"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build([observation()], [evidence(**changes)])

    def test_public_counts_use_whitelist_and_never_copy_context(self):
        result = build([observation(artifact="private artifact", product="private product", query_terms=["private query"],
                       solution_hypotheses=[{"delivery_form": "plugin", "statement": "private proposal", "status": "hypothesis"}])])
        result["summary"].update(private_text="private quote", internal_ids=["private identity"])
        result["summary"]["delivery_forms"]["private name"] = 1
        text = json.dumps(public_discovery_counts(result))
        self.assertNotIn("private", text)
        self.assertIn('"delivery_hypothesis_count": 1', text)
        self.assertIn('"task_family_count": 1', text)

    def test_legacy_snapshot_is_verified_without_rekeying(self):
        saved = legacy_fixture()
        before = deepcopy(saved)
        validate_user_discovery(saved, [evidence()], as_of=DAY, run_id=RUN)
        self.assertEqual(saved, before)
        self.assertEqual(build([observation(product="TestProduct")], version="1.0"), saved)
        self.assertNotEqual(build([observation(product="TestProduct")])["observations"][0]["cluster_id"],
                            saved["observations"][0]["cluster_id"])
        saved["demand_clusters"][0]["product"] = "forged"
        with self.assertRaises(ValueError):
            validate_user_discovery(saved, [evidence()], as_of=DAY, run_id=RUN)

    def test_task_context_report_and_public_aggregate(self):
        record = evidence(revision_id="EVID-1:REV-1")
        row = observation(trigger="交付前", current_workaround="逐项手工核对", desired_outcome="按时交付", artifact="私有清单",
                          solution_hypotheses=[{"delivery_form": "human_assisted_service", "statement": "私有代做方案", "status": "hypothesis"}])
        row["evidence_refs"][0]["revision_id"] = record["revision_id"]
        discovery = build([row], [record])
        tiered = {"schema_version": "3.0", "run_id": RUN, "as_of": DAY, "deep_candidates": [], "validated_ideas": [],
                  "regional_signals": [], "rejected": [], "user_discovery": discovery}
        report = build_report(tiered, decision={"summary": "发现任务", "largest_unknown": "是否反复发生",
            "next_action": "检查同类原文", "stop_condition": "现有工具满足"}, claim_evidence=[record])
        self.assertTrue(validate_structured_report(report)["valid"])
        markdown = render_report(report)
        for phrase in ("任务族：1", "触发事件：交付前", "现有做法：逐项手工核对", "具体产物：私有清单",
                       "解决方案假设", "人工辅助服务：私有代做方案", "观察/检索语言：unknown"):
            self.assertIn(phrase, markdown)
        public, _ = export_public(report)
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("私有", serialized)
        self.assertNotIn("Setup still takes hours", serialized)
        self.assertEqual(public["extensions"]["user_discovery"]["task_family_count"], 1)


if __name__ == "__main__":
    unittest.main()
