"""跨模块研究交付回归：原文身份、离线修订、公开发布与质量边界。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.evidence.quality import assess_quality
from aor.opportunity.exploration import normalize_leads
from aor.reporting.public import export_public, review_content_hash
from aor.reporting.report import build_report, validate_structured_report
from aor.sources.coverage import build_industry_coverage, research_quality
from aor.storage.evidence_library import EvidenceLibrary
from aor.storage.request_journal import RequestJournal, read_run_ledger
from aor.workflow.research import _apply_assessment, _artifact, _save, reparse_run, resume_research, start_research
from build_query_plan import build_plan
from filter_ideas import filter_ideas
from tests.test_evidence_identity import post, comment
from tests.test_idea_funnel import benchmark


DAY = "2026-09-14"
RUN = "RUN-20260914-AABBCCDDEE"
DECISION = {"summary": "保留组队协调假设，继续验证真实行为", "primary_id": None,
            "largest_unknown": "尚未验证持续使用及付费意愿", "next_action": "取得一周真实协调记录",
            "stop_condition": "没有反复协调需求则停止"}


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def lead(reference: dict) -> dict:
    return {"title": "下班后固定游戏队友协调", "industry_ids": ["gaming"], "target_user": "空闲时间有限的上班族玩家",
            "problem_or_desire": "每晚只有两小时，难以找到时间一致的固定队友",
            "wedge": "从成员的自然语言时间安排中整理兼容时段", "ai_value": deepcopy(benchmark()["ai_value"]),
            "evidence": [{**deepcopy(reference), "fact": "玩家需要固定时段的队友"}]}


class ResearchIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        offline = patch.dict(os.environ, {"AOR_OFFLINE": "1"})
        offline.start()
        self.addCleanup(offline.stop)

    def pure_report(self):
        library = EvidenceLibrary(self.home / "library")
        library.ingest([post(industry_ids=["gaming"], evidence_role="usage_behavior")], as_of=DAY, run_id=RUN)
        context = library.search("", as_of=DAY, limit=None)
        rows = normalize_leads([lead(context[0])], run_id=RUN, as_of=DAY)
        tiered = filter_ideas({"schema_version": "3.0", "run_id": RUN, "as_of": DAY, "candidates": []})
        tiered["research_leads"] = rows
        plan = build_plan(date.fromisoformat(DAY), self.home)
        plan["run_id"] = RUN
        return build_report(tiered, decision=DECISION, evidence=[{"schema_version": "3.0", "run_id": RUN, "as_of": DAY,
                                                               "evidence": context}],
                            claim_evidence=context, research_plan=plan)

    def test_post_comments_remain_distinct_through_prepare_and_report(self):
        raw_post = post(industry_ids=["gaming"], evidence_role="usage_behavior")
        rows = [comment(2, industry_ids=["gaming"]), raw_post, comment(1, industry_ids=["gaming"])]
        material = write(self.home / "material.json", {"evidence": rows})
        benchmarks = write(self.home / "benchmarks.json", {"benchmarks": [], "leads": [lead(raw_post)]})
        run = start_research(self.home, as_of=date.fromisoformat(DAY), offline=True,
                             evidence_files=[material], benchmarks_file=benchmarks)
        self.assertEqual(run["status"], "awaiting_assessment")
        context = json.loads(Path(run["artifacts"]["evidence-context"]["path"]).read_text())["evidence"]
        self.assertEqual(len(context), 3)
        self.assertEqual(len({r["evidence_id"] for r in context}), 3)
        tiered = json.loads(Path(run["artifacts"]["tiered"]["path"]).read_text())
        ref = tiered["research_leads"][0]["evidence"][0]
        original = next(r for r in context if r["id"] == raw_post["id"])
        self.assertEqual(ref["original_text"], raw_post["original_text"])
        self.assertEqual(ref["evidence_id"], original["evidence_id"])
        self.assertEqual(ref["revision_id"], original["revision_id"])
        assessment = write(self.home / "assessment.json", {"scores": [], "claims": [], "decision": DECISION})
        finished = resume_research(self.home, run["run_id"], assessment_file=assessment, collect=False)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertTrue(validate_structured_report(report)["valid"])
        published_ref = report["tiered"]["research_leads"][0]["evidence"][0]
        self.assertEqual(published_ref["revision_id"], original["revision_id"])
        self.assertEqual(published_ref["original_text"], raw_post["original_text"])
        self.assertEqual(report["metrics"]["paid_request_count"], 0)
        self.assertEqual(report["metrics"]["normalized_evidence_count"], 3)
        self.assertEqual(report["research_quality"]["review_scope"], "active_context")
        self.assertEqual(report["research_quality"]["unreviewed_evidence_count"], 3)

    def test_prior_report_metrics_and_review_scope_remain_auditable(self):
        from build_result_digest import build_result_digest
        from aor.sources.coverage import research_quality

        report = self.pure_report()
        digest = build_result_digest(report["tiered"], evidence_payloads=report["evidence_inventory"], metrics_version="1.0")
        report["metrics"], report["source_yield"] = digest["metrics"], digest["source_yield"]
        report["research_quality"] = research_quality(report["industry_coverage"], report["evidence_selection"], report["tiered"])
        original = deepcopy(report)
        validation = validate_structured_report(report)
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertEqual(report, original)

    def test_uncertain_derivation_cannot_support_current_publication(self):
        from aor.reporting.report import evidence_current_state
        from aor.reporting.public import evidence_publication_issue

        stored = {"evidence_id": "EVID-ABC", "revision_id": "EVID-ABC:123",
                  "status": "active", "derivation_status": "needs_review"}
        state = evidence_current_state([stored])
        self.assertEqual(state["EVID-ABC"]["status"], "needs_review")
        self.assertIsNotNone(evidence_publication_issue("EVID-ABC", "EVID-ABC:123", {"current_evidence_state": state}))

    def test_legacy_report_remains_auditable_but_not_public(self):
        report = self.pure_report()
        report["report_version"] = "1.0"
        report["tiered"]["research_leads"][0].pop("title")
        original = deepcopy(report)
        validation = validate_structured_report(report)
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertTrue(any("历史审计" in message for message in validation["warnings"]))
        with self.assertRaisesRegex(ValueError, "旧报告"):
            export_public(report)
        self.assertEqual(report, original)

    def test_pinned_historical_revision_survives_new_material_in_same_run(self):
        original_post = post(industry_ids=["gaming"], evidence_role="usage_behavior")
        material = write(self.home / "original.json", {"evidence": [original_post]})
        run = start_research(self.home, as_of=date.fromisoformat(DAY), offline=True, evidence_files=[material])
        context = json.loads(Path(run["artifacts"]["evidence-context"]["path"]).read_text())["evidence"]
        old = context[0]
        revised = write(self.home / "revised.json", {"evidence": [post(industry_ids=["gaming"],
                            observed_at=DAY, original_text="补充：已找到队友，仍需要协调每晚的空闲时间。") ]})
        benchmarks = write(self.home / "pinned-benchmarks.json", {"benchmarks": [], "leads": [lead(old)]})
        run = resume_research(self.home, run["run_id"], evidence_files=[revised], benchmarks_file=benchmarks, collect=False)
        assessment = write(self.home / "assessment.json", {"scores": [], "claims": [], "decision": DECISION})
        finished = resume_research(self.home, run["run_id"], assessment_file=assessment, collect=False)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertTrue(validate_structured_report(report)["valid"])
        ref = report["tiered"]["research_leads"][0]["evidence"][0]
        self.assertEqual(ref["revision_id"], old["revision_id"])
        self.assertEqual(ref["original_text"], original_post["original_text"])
        self.assertTrue(any(r["revision_id"] == old["revision_id"] for r in report["claim_evidence"]))

    def test_manual_review_reaches_coverage_and_observed_need_publication(self):
        original_post = post(industry_ids=["gaming"], evidence_role="usage_behavior")
        material = write(self.home / "material.json", {"evidence": [original_post]})
        candidate = lead(original_post)
        benchmarks = write(self.home / "benchmarks.json", {"benchmarks": [], "leads": [candidate]})
        run = start_research(self.home, as_of=date.fromisoformat(DAY), offline=True,
                             evidence_files=[material], benchmarks_file=benchmarks)
        context = json.loads(Path(run["artifacts"]["evidence-context"]["path"]).read_text())["evidence"]
        original = context[0]
        review = {"evidence_id": original["evidence_id"], "revision_id": original["revision_id"],
                  "status": "relevant", "reviewer": "fixture-reviewer", "reviewed_at": DAY,
                  "rationale": "原帖描述了上班族固定时段找队友的实际任务", "evidence_role": "usage_behavior"}
        assessment = write(self.home / "assessment.json", {"scores": [], "claims": [], "evidence_reviews": [review],
                                                            "decision": DECISION})
        finished = resume_research(self.home, run["run_id"], assessment_file=assessment, collect=False)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        game = next(r for r in report["industry_coverage"]["industries"] if r["industry_id"] == "gaming")
        self.assertEqual(game["recent_user_behavior_count"], 1)
        candidate = report["tiered"]["research_leads"][0]
        self.assertEqual(candidate["evidence"][0]["relevance_review"]["revision_id"], original["revision_id"])
        candidate["research_status"] = "observed_need"
        candidate["publication_review"] = {"status": "approved", "reviewer": "fixture-reviewer", "reviewed_at": DAY,
                                           "rationale": "用户任务事实可公开，保留商业验证缺口",
                                           "content_sha256": review_content_hash(candidate)}
        dataset, withheld = export_public(report)
        self.assertEqual(withheld, [])
        self.assertEqual(dataset["items"][0]["stage"], "observed_need")
        public_game = next(r for r in dataset["coverage"] if r["industry_id"] == "gaming")
        self.assertEqual(public_game["recent_demand_count"], 1)
        self.assertEqual(dataset["quality"]["review_summary"]["reviewed_count"], 1)
        self.assertEqual(dataset["quality"]["review_summary"]["unreviewed_count"], 0)
        from aor.reporting.public_contract import validate_public_dataset
        dataset["quality"]["review_summary"]["unreviewed_count"] = 1
        self.assertFalse(validate_public_dataset(dataset)["valid"])

    def test_report_11_rejects_missing_fields_and_out_of_scope_industry(self):
        baseline = self.pure_report()
        for field in ("title", "industry_ids", "wedge"):
            with self.subTest(field=field):
                changed = deepcopy(baseline)
                changed["tiered"]["research_leads"][0].pop(field)
                self.assertFalse(validate_structured_report(changed)["valid"])
        changed = deepcopy(baseline)
        changed["tiered"]["research_leads"][0]["industry_ids"] = ["manufacturing"]
        self.assertFalse(validate_structured_report(changed)["valid"])

    def test_publication_ref_cannot_change_original_even_with_new_review_hash(self):
        report = self.pure_report()
        row = report["tiered"]["research_leads"][0]
        row["publication_review"] = {"status": "approved", "reviewer": "fixture-reviewer", "reviewed_at": DAY,
                                     "rationale": "原始用户任务支持线索，商业价值仍待验证",
                                     "content_sha256": review_content_hash(row)}
        dataset, withheld = export_public(report)
        self.assertEqual(withheld, [])
        self.assertEqual(len(dataset["items"]), 1)
        row["evidence"][0]["original_text"] = "与原始任务无关的另一条评论"
        row["publication_review"]["content_sha256"] = review_content_hash(row)
        self.assertFalse(validate_structured_report(report)["valid"])
        with self.assertRaisesRegex(ValueError, "无法导出无效报告"):
            export_public(report)

    def test_publication_review_binds_final_a_candidate_after_scoring(self):
        benchmarks = write(self.home / "formal-benchmarks.json", {"benchmarks": [benchmark()]})
        run = start_research(self.home, as_of=date.fromisoformat(DAY), offline=True, benchmarks_file=benchmarks)
        tiered = json.loads(Path(run["artifacts"]["tiered"]["path"]).read_text())
        self.assertEqual(len(tiered["deep_candidates"]), 1)
        identifier = tiered["deep_candidates"][0]["id"]
        assessment = {"scores": [{"id": identifier, "track": "needle",
            "scores": dict.fromkeys(("demand", "new_form", "distribution", "regional_gap", "monetization",
                                     "mvp_feasibility", "evidence"), 8),
            "auxiliary_scores": dict.fromkeys(("first_revenue", "scale", "personal_influence", "confidence"), 7)}]}
        scored = _apply_assessment(tiered, assessment)
        final_row = scored["deep_candidates"][0]
        assessment["publication_reviews"] = {identifier: {"status": "approved", "reviewer": "fixture-reviewer",
            "reviewed_at": DAY, "rationale": "审阅最终评分、证据引用及候选文本", "content_sha256": review_content_hash(final_row)}}
        reviewed = _apply_assessment(tiered, assessment)
        row = reviewed["deep_candidates"][0]
        self.assertEqual(row["publication_review"]["content_sha256"], review_content_hash(row))

    def test_weak_keyword_and_one_official_page_do_not_complete_industry(self):
        plan = build_plan(date.fromisoformat(DAY), self.home)
        plan["run_id"] = RUN
        weak = {"id": "reddit:fixture", "source": "reddit", "url": "https://www.reddit.com/r/example/comments/fixture",
                "industry_ids": ["gaming"], "evidence_kind": "post", "evidence_role": "usage_behavior",
                "original_text": "My baseball teammates won yesterday", "published_at": DAY}
        weak.update(assess_quality(weak, query="gaming teammates reliable evening schedule", as_of=DAY))
        official = {"id": "web:pricing", "source": "web", "url": "https://example.org/pricing",
                    "industry_ids": ["gaming"], "evidence_role": "official_pricing", "original_text": "Monthly price $9",
                    "verification": {"status": "host_attested"}}
        coverage = build_industry_coverage(plan, [{"run_id": RUN, "evidence": [weak, official]}])
        game = next(r for r in coverage["industries"] if r["industry_id"] == "gaming")
        self.assertEqual(weak["relevance_status"], "unknown")
        self.assertEqual(game["verified_evidence_count"], 1)
        self.assertEqual(game["recent_user_behavior_count"], 0)
        self.assertFalse(game["scheduled_research_complete"])
        self.assertTrue(research_quality(coverage, {}, {})["coverage_incomplete"])

    def test_reparse_creates_child_without_network_parent_mutation_or_new_cost(self):
        run = start_research(self.home, as_of=date.fromisoformat(DAY), focus="游戏组队时间协调", offline=True)
        directory = Path(run["run_path"])
        manifest = json.loads((directory / "run.json").read_text())
        raw = {"schema_version": "3.0", "run_id": run["run_id"], "as_of": DAY,
               "stage": "search_discovery", "provider": "tikhub", "generated_at": DAY + "T10:00:00Z",
               "results": [{"id": "fixture-paid", "source": "youtube", "status": "ok",
                            "params": {"keyword": "gaming schedule"}, "industry_ids": ["gaming"],
                            "response": {"data": {"videos": [{"video_id": "fixture-v1", "title": "Gaming schedule",
                                                                 "published_time": "2 days ago"}]}}}],
               "summary": {"requests": 1, "ok": 1, "error": 0, "estimated_attempted_cost_usd": "0.01"}}
        _artifact(directory, manifest, "paid-fixture", raw)
        manifest["execution_artifacts"].append("paid-fixture")
        _save(directory, manifest)
        journal_path = directory / "paid-journal.sqlite3"
        with RequestJournal(journal_path, run_id=run["run_id"], as_of=DAY) as journal:
            journal.register_batch("fixture", "plan-hash", [{"request_fingerprint": "request-hash", "source": "youtube"}])
            attempt = journal.start_attempt("request-hash", batch_id="fixture", list_cost_usd=Decimal("0.01"),
                                            estimated_cost_usd=Decimal("0.01"), pricing_snapshot={},
                                            max_cost_usd=Decimal("1"), max_attempts=1)
            journal.finish_attempt(attempt, state="succeeded", result={"status": "ok"})
        benchmarks = write(self.home / "empty-benchmarks.json", {"benchmarks": [], "empty_reason": "没有经核验的收费对标"})
        assessment = write(self.home / "empty-assessment.json", {"scores": [], "claims": [], "decision": DECISION})
        finished = resume_research(self.home, run["run_id"], benchmarks_file=benchmarks,
                                   assessment_file=assessment, collect=False)
        parent_report_path = Path(finished["artifacts"]["report"]["path"])
        parent_report = parent_report_path.read_bytes()
        parent_manifest = (directory / "run.json").read_bytes()
        parent_ledger = read_run_ledger(journal_path)
        self.assertEqual(json.loads(parent_report)["metrics"]["estimated_cost_usd"], 0.01)
        with (patch("tikhub_query.execute_plan", side_effect=AssertionError("重解析不能付费调用")) as paid,
              patch("community_query.execute_plan", side_effect=AssertionError("重解析不能免费联网")) as free):
            child = reparse_run(self.home, run["run_id"])
            paid.assert_not_called()
            free.assert_not_called()
        child_dir = Path(child["run_path"])
        child_manifest = json.loads((child_dir / "run.json").read_text())
        self.assertNotEqual(child["run_id"], run["run_id"])
        self.assertEqual(child_manifest["parent_run_id"], run["run_id"])
        self.assertEqual(child_manifest["focus"], "游戏组队时间协调")
        self.assertEqual(child_manifest["execution_artifacts"], [])
        self.assertEqual(parent_report_path.read_bytes(), parent_report)
        self.assertEqual((directory / "run.json").read_bytes(), parent_manifest)
        self.assertEqual(read_run_ledger(journal_path), parent_ledger)
        self.assertFalse((child_dir / "paid-journal.sqlite3").exists())
        child_finished = resume_research(self.home, child["run_id"], benchmarks_file=benchmarks,
                                        assessment_file=assessment, collect=False)
        child_report = json.loads(Path(child_finished["artifacts"]["report"]["path"]).read_text())
        self.assertEqual(child_report["metrics"]["paid_request_count"], 0)
        # 未创建新账本时费用字段为未知；不能继承父运行的历史收费。
        self.assertIn(child_report["metrics"]["estimated_cost_usd"], (None, 0))
        provenance = json.loads((child_dir / "reparse-provenance.json").read_text())
        self.assertEqual(provenance["new_paid_requests"], 0)
        self.assertEqual(parent_report_path.read_bytes(), parent_report)


if __name__ == "__main__":
    unittest.main()
