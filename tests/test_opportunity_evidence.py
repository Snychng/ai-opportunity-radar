from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from aor_bootstrap import SOURCE_ROOT  # noqa: F401
from aor.opportunity.selection import build_experiment_context
from expand_ideas import ExpansionError, expand_ideas
from filter_ideas import classify_candidate
from score_candidates import AUXILIARY_FIELDS, SCORE_FIELDS, ScoreValidationError, score_candidate
from tests.test_idea_funnel import benchmark
from tests.test_scoring import candidate


def source() -> dict:
    return {
        "id": "EV-1", "revision_id": "REV-1", "url": "https://forum.example/workflow",
        "source": "forum", "original_text": "商家希望在浏览器侧边栏完成回复，不想来回切换页面。",
        "published_at": "2026-09-08", "observed_at": "2026-09-09T09:00:00+08:00",
    }


def reference() -> dict:
    return {"evidence_id": "EV-1", "revision_id": "REV-1", "quote": "在浏览器侧边栏完成回复"}


def experiment() -> dict:
    return {
        "experiment_id": "EXP-sidebar", "record_id": "OPP-20260908-ABCDEF",
        "run_id": "RUN-20260909-ABCDEF1234", "as_of": "2026-09-09", "status": "completed",
        "hypothesis": "商家愿意使用侧边栏回复", "offer": "一次回复工具",
        "success_criteria": "愿意提供真实任务", "stop_criteria": "无人提供真实任务",
        "counts": {"real_tasks": 1}, "outcome": "一名商家提交了真实任务",
        "decision": "continue", "next_action": "验证报价接受度",
        "evidence": [{"local_ref": "private/experiment-notes.md", "fact": "一名商家提交了真实客服任务"}],
    }


class OpportunityEvidenceTests(unittest.TestCase):
    def test_selection_preserves_pool_and_only_supported_priority_contributes(self):
        payload = {
            "as_of": "2026-09-10", "run_id": "RUN-20260910-ABCDEF1234",
            "benchmarks": [benchmark()], "dimensions": {"forms": ["自动生成 FAQ 回复", "侧边栏回复", "万能助手"]},
        }
        original = expand_ideas(payload)
        selected = expand_ideas({
            **payload, "evidence": [source()],
            "selection_strategy": {"mode": "evidence_priority", "limit": 1},
            "axis_priorities": [
                {"axis": "forms", "value": "万能助手", "priority": 10, "rationale": "只有想法"},
                {"axis": "forms", "value": "侧边栏回复", "priority": 2,
                 "rationale": "用户已有侧边栏工作流需求", "evidence_refs": [reference()]},
            ],
        })
        self.assertEqual(selected["candidates"], original["candidates"])
        picked = next(item for item in selected["candidates"] if item["wedge"] == "侧边栏回复")
        self.assertEqual(selected["selection"]["selected_candidate_ids"], [picked["candidate_id"]])
        self.assertEqual(selected["selection"]["axis_priorities"][0]["effective_priority"], 0)
        self.assertTrue(selected["selection"]["axis_priorities"][0]["missing"])
        self.assertNotEqual(classify_candidate(picked)[0], "A")

    def test_selection_rejects_wrong_quote_and_revision(self):
        for mutation in ({"quote": "用户已付款"}, {"revision_id": "REV-0"}, {"evidence_id": "EV-missing"}):
            with self.subTest(mutation=mutation), self.assertRaises(ExpansionError):
                expand_ideas({
                    "benchmarks": [benchmark()], "evidence": [source()],
                    "axis_priorities": [{"axis": "forms", "value": "侧边栏回复", "rationale": "待研究",
                                         "evidence_refs": [{**reference(), **mutation}]}],
                })

    def test_missing_rationale_cannot_contribute_even_with_located_quote(self):
        result = expand_ideas({
            "benchmarks": [benchmark()], "evidence": [source()],
            "axis_priorities": [{"axis": "forms", "value": "自动生成 FAQ 回复", "priority": 9,
                                 "evidence_refs": [reference()]}],
        })
        self.assertEqual(result["selection"]["ranking"][0]["priority"], 0)

    def test_experiment_context_uses_both_cutoffs_and_does_not_sum_snapshots(self):
        old = experiment()
        old.update(as_of="2026-09-08", run_id="RUN-20260908-ABCDEF1234", counts={"real_tasks": 1})
        latest = {**experiment(), "counts": {"real_tasks": 3}}
        future = {**experiment(), "experiment_id": "EXP-future", "as_of": "2026-09-11"}
        observed_later = {**experiment(), "experiment_id": "EXP-late", "observed_at": "2026-09-11T00:00:00+08:00"}
        result = build_experiment_context([latest, old, future, observed_later], as_of="2026-09-10")
        self.assertEqual(len(result["experiments"]), 1)
        self.assertEqual(result["experiments"][0]["counts"]["real_tasks"], 3)
        self.assertEqual(result["excluded_future_count"], 2)
        self.assertFalse(result["affects_evidence_tier"])

    def test_experiment_result_can_guide_selection_without_changing_pool(self):
        payload = {"benchmarks": [benchmark()], "dimensions": {"forms": ["自动生成 FAQ 回复", "侧边栏回复"]}}
        selected = expand_ideas({
            **payload, "selection_strategy": {"mode": "evidence_priority", "limit": 1},
            "experiment_results": [experiment()], "axis_priorities": [{
                "axis": "forms", "value": "侧边栏回复", "priority": 1, "rationale": "已经拿到一项真实任务，继续验证报价",
                "experiment_refs": [{"experiment_id": "EXP-sidebar", "run_id": "RUN-20260909-ABCDEF1234"}],
            }],
        })
        self.assertEqual(selected["candidates"], expand_ideas(payload)["candidates"])
        self.assertEqual(selected["selection"]["selected_candidate_ids"], [selected["candidates"][1]["candidate_id"]])
        self.assertNotEqual(classify_candidate(selected["candidates"][1])[0], "A")
        planned = deepcopy(experiment())
        planned["status"] = "planned"
        selected = expand_ideas({**payload, "experiment_results": [planned],
                                "axis_priorities": selected["selection"]["axis_priorities"]})
        self.assertEqual(selected["selection"]["ranking"][1]["priority"], 0)

    def test_future_execution_or_evidence_excludes_experiment_context(self):
        for field in ("published_at", "observed_at", "performed_at", "completed_at"):
            for nested in (False, True):
                item = deepcopy(experiment())
                target = item["evidence"][0] if nested else item
                target[field] = "2026-09-11T00:00:00+08:00"
                with self.subTest(field=field, nested=nested):
                    context = build_experiment_context([item], as_of="2026-09-10")
                    self.assertEqual(context["experiments"], [])
                    self.assertEqual(context["excluded_future_count"], 1)

    def test_score_basis_is_transparent_without_changing_legacy_scores(self):
        value = candidate("评分依据", 8)
        previous = score_candidate(value)
        self.assertEqual(previous["total_score"], 80)
        self.assertEqual(previous["scoring_basis_summary"]["missing_dimensions"], list(SCORE_FIELDS + AUXILIARY_FIELDS))
        value["evidence"].append(source())
        value["score_basis"] = {"demand": {"rationale": "有明确工作流诉求", "evidence_refs": [reference()]}}
        scored = score_candidate(value)
        self.assertEqual(scored["total_score"], previous["total_score"])
        self.assertNotIn("demand", scored["scoring_basis_summary"]["missing_dimensions"])
        self.assertEqual(scored["score_basis"]["demand"]["reference_validation"], "located")
        self.assertEqual(scored["score_basis"]["demand"]["semantic_validation"], "not_performed")
        self.assertNotIn("start", value["score_basis"]["demand"]["evidence_refs"][0])
        self.assertEqual(score_candidate(scored)["score_basis"], scored["score_basis"])

    def test_score_basis_wrong_revision_is_rejected(self):
        value = candidate("错误依据", 8)
        value["evidence"].append(source())
        value["score_basis"] = {"demand": {"rationale": "工作流需求", "evidence_refs": [{**reference(), "revision_id": "REV-0"}]}}
        with self.assertRaises(ScoreValidationError):
            score_candidate(value)

    def test_scoring_passes_cutoff_to_basis_validation(self):
        value = candidate("截止日评分", 8)
        value.update(as_of="2026-09-10", run_id="RUN-20260910-ABCDEF1234")
        value["evidence"].append({**source(), "observed_at": "2026-09-11T01:00:00+08:00"})
        value["score_basis"] = {"demand": {"rationale": "晚于本轮的观察", "evidence_refs": [reference()]}}
        with self.assertRaises(ScoreValidationError):
            score_candidate(value)

    def test_source_collection_labels_never_multiply_priority(self):
        base = {"benchmarks": [benchmark()], "axis_priorities": [{
            "axis": "forms", "value": "自动生成 FAQ 回复", "priority": 1, "rationale": "用户描述此流程",
            "evidence_refs": [reference()],
        }]}
        once = expand_ideas({**base, "evidence": [source()]})
        duplicated = expand_ideas({**base, "evidence": [source(), {**source(), "source": "aggregator"}]})
        self.assertEqual(once["selection"]["ranking"], duplicated["selection"]["ranking"])


if __name__ == "__main__":
    unittest.main()
