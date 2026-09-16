from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.evidence.claims import build_evidence_packet
from aor.evidence.selection import evidence_index
from aor.opportunity.exploration import lead_history, normalize_leads, record_leads
from aor.reporting.report import build_report, validate_structured_report, render_report
from aor.sources.coverage import build_industry_coverage
from aor.sources.discovery import prepare_discovery_plan
from aor.sources.industries import load_industries
from aor.workflow.research import resume_research, start_research
from build_query_plan import build_plan
from expand_ideas import expand_ideas
from filter_ideas import classify_candidate, filter_ideas
from tests.test_idea_funnel import benchmark
from tests.test_tikhub_query import pricing_rows


class CrossIndustryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.plan = build_plan(date(2026, 9, 14), self.home)

    def evidence(self, source="reddit", identifier="1", industry="gaming"):
        return {"id": f"{source}:{identifier}", "source": source, "url": f"https://{source}.example/{identifier}",
                "title": "玩家每周组织多人游戏", "original_text": "玩家每周组织游戏时反复遇到时区和空闲时间协调问题。",
                "observed_at": "2026-09-14", "published_at": "2026-09-14", "industry_ids": [industry],
                "relevance_status": "relevant"}

    def test_default_scope_is_user_selected_and_queries_cover_both_languages(self):
        expected = {"ecommerce", "gaming", "content_creation", "learning", "personal_life", "internet_products"}
        self.assertEqual(set(self.plan["selected_industries"]), expected)
        self.assertEqual({r["id"] for r in load_industries()}, expected)
        paid = self.plan["retrieval_plans"]["tikhub"]
        for industry in expected:
            self.assertEqual({r["query_scope"]["language"] for r in paid["requests"] if industry in r["industry_ids"]}, {"en", "zh"})
        self.assertEqual(len(self.plan["retrieval_plans"]["web_import"]["required_imports"]), 6)
        for row in self.plan["retrieval_plans"]["community"]["requests"]:
            self.assertNotIn(row["params"].get("query"), {"AI agent", "manual workflow"})
        self.assertNotIn("github", {r["source"] for r in self.plan["retrieval_plans"]["community"]["requests"]})

    def test_industry_extension_requires_no_collector_code_change(self):
        row = load_industries()[0]
        row.update(id="photography", name="摄影", audience="摄影爱好者")
        (self.home / "config").mkdir()
        (self.home / "config/industries.json").write_text(json.dumps([row]))
        (self.home / "config/preferences.json").write_text(json.dumps({"industries": ["photography"]}))
        plan = build_plan(date(2026, 9, 14), self.home)
        self.assertEqual(plan["selected_industries"], ["photography"])
        self.assertEqual(len(plan["retrieval_plans"]["tikhub"]["requests"]), 2)

    def test_budget_selection_skips_missing_prices_and_keeps_multiple_industries(self):
        plan = self.plan["retrieval_plans"]["tikhub"]
        prices = pricing_rows()
        from tikhub_query import RADAR_ENDPOINTS
        prices = [{"endpoint_uri": p["endpoint"], "endpoint_cost": 0.01, "allow_free_credit": False,
                   "allow_discount": False, "platform": source} for source, p in RADAR_ENDPOINTS.items() if source != "threads"]
        # 使用测试价格目录；有价格的来源继续执行，无价格的来源明确记录。
        selected = prepare_discovery_plan(plan, prices, max_cost_usd=1, max_requests=3)
        self.assertEqual(len(selected["requests"]), 3)
        self.assertEqual(len({r["industry_ids"][0] for r in selected["requests"]}), 3)
        self.assertTrue(selected["skipped_requests"])
        self.assertLessEqual(selected["preflight_estimate"]["worst_case_cost_usd"], 1)
        with self.assertRaises(ValueError):
            prepare_discovery_plan(plan, prices, max_cost_usd=0.001, prior_cost_usd="0.001")

    def test_packet_is_order_independent_and_full_index_preserves_omitted_sources(self):
        rows = [self.evidence("github", str(i), "internet_products") for i in range(18)]
        rows += [self.evidence("reddit", str(i), "gaming") for i in range(10)]
        rows += [self.evidence("web", str(i), "ecommerce") for i in range(8)]
        for row in rows:
            row["original_text"] *= 70
        options = {"as_of": "2026-09-14", "max_items": 6, "max_chars": 7000}
        first = build_evidence_packet(rows, **options)
        reverse = build_evidence_packet(reversed(rows), **options)
        self.assertEqual(first, reverse)
        self.assertEqual({r["source"] for r in first["evidence"]}, {"github", "reddit", "web"})
        self.assertLessEqual(first["serialized_chars"], 7000)
        index = evidence_index(rows, first)
        self.assertEqual(len(index["items"]), len(rows))
        self.assertEqual(sum(r["selected"] for r in index["items"]), len(first["evidence"]))

    def test_consumption_behavior_can_support_b_but_likes_cannot(self):
        base = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        base["payment_signals"] = [{"type": "pricing", "url": "https://vendor.example/pricing", "fact": "订阅价格为每月 9 元"}]
        for behavior in ("repeat_usage", "creative_output", "learning_progress", "organic_sharing"):
            base["demand_signals"] = [{"type": behavior, "url": "https://player.example/experience", "fact": "玩家记录了四周反复组织活动的真实使用过程"}]
            self.assertEqual(classify_candidate(base)[0], "B")
        base["demand_signals"][0]["type"] = "likes"
        self.assertIsNone(classify_candidate(base)[0])

    def test_lead_survives_unknown_distribution_without_becoming_formal_opportunity(self):
        base = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
        base.update(acquisition_channel=None, mvp_days=45, require_ai_value=True)
        result = filter_ideas({"candidates": [base], "preserve_research_leads": True})
        self.assertEqual(len(result["research_leads"]), 1)
        self.assertFalse(result["rejected"])
        self.assertFalse(result["deep_candidates"])
        self.assertIn("mvp_within_30_days", result["research_leads"][0]["missing_requirements"])
        base["ai_value"] = {"status": "hypothesis", "capability": "加聊天框"}
        rejected = filter_ideas({"candidates": [base], "preserve_research_leads": True})
        self.assertFalse(rejected["research_leads"])
        self.assertIn("clear_ai_value", rejected["rejected"][0]["rejection_reasons"])

    def test_leads_without_paid_benchmark_are_durable_and_idempotent(self):
        row = {"title": "游戏组队时间协调", "target_user": "跨时区游戏玩家", "problem_or_desire": "每周协调时间花费很多精力",
               "wedge": "根据成员可用时间提出组队方案", "industry_ids": ["gaming"],
               "ai_value": benchmark()["ai_value"], "evidence": [self.evidence()]}
        leads = normalize_leads([row], run_id=self.plan["run_id"], as_of="2026-09-14")
        record_leads(self.home, leads, run_id=self.plan["run_id"])
        record_leads(self.home, leads, run_id=self.plan["run_id"])
        self.assertEqual(len((self.home / "state/research-leads.jsonl").read_text().splitlines()), 1)
        changed = deepcopy(leads)
        changed[0]["next_question"] = "不同判断"
        with self.assertRaises(ValueError):
            record_leads(self.home, changed, run_id=self.plan["run_id"])
        self.assertEqual(lead_history(self.home, as_of="2026-09-14"), leads)
        self.assertEqual(lead_history(self.home, as_of="2026-09-13"), [])

    def test_reddit_uses_documented_lowercase_time_range(self):
        from tikhub_query import build_search_plan
        plan = build_search_plan(as_of=self.plan["as_of"], run_id=self.plan["run_id"],
                                 query_groups=[{"keyword": "gaming teammates", "sources": ["reddit"]}])
        self.assertEqual(plan["requests"][0]["params"]["time_range"], "month")

    def test_coverage_does_not_claim_planned_or_historical_sources_were_collected(self):
        empty = build_industry_coverage(self.plan, [])
        self.assertEqual(empty["status_counts"], {"not_collected": 6})
        payload = {"run_id": "RUN-20260913-ABCDEF1234", "reused_for_run_id": self.plan["run_id"], "evidence": [self.evidence()]}
        old = build_industry_coverage(self.plan, [payload])
        game = next(r for r in old["industries"] if r["industry_id"] == "gaming")
        self.assertEqual((game["material_count"], game["historical_evidence_count"]), (0, 1))
        payload = {"run_id": self.plan["run_id"], "evidence": [self.evidence()]}
        current = build_industry_coverage(self.plan, [payload])
        game = next(r for r in current["industries"] if r["industry_id"] == "gaming")
        self.assertEqual(game["status"], "related_material")
        self.assertEqual(game["verified_evidence_count"], 0)

    def test_report_detects_fabricated_coverage(self):
        tiered = {"schema_version": "3.0", "as_of": self.plan["as_of"], "run_id": self.plan["run_id"],
                  "deep_candidates": [], "validated_ideas": [], "regional_signals": [], "rejected": []}
        decision = {"summary": "本轮覆盖不足", "largest_unknown": "用户需求尚未核验", "next_action": "补查用户评论", "stop_condition": "无相关材料停止该查询"}
        report = build_report(tiered, decision=decision, research_plan=self.plan)
        self.assertIn("已规划，尚未采集", render_report(report))
        self.assertTrue(validate_structured_report(report)["valid"])
        report["industry_coverage"]["industries"][0]["verified_evidence_count"] = 100
        self.assertFalse(validate_structured_report(report)["valid"])

    def test_legacy_structured_report_without_new_coverage_fields_remains_valid(self):
        tiered = {"schema_version": "3.0", "as_of": self.plan["as_of"], "run_id": self.plan["run_id"],
                  "deep_candidates": [], "validated_ideas": [], "regional_signals": [], "rejected": []}
        decision = {"summary": "无合格候选", "largest_unknown": "用户需求", "next_action": "查看原文", "stop_condition": "无真实任务"}
        report = build_report(tiered, decision=decision)
        for key in ("coverage_plan", "industry_coverage", "evidence_selection", "research_quality"):
            report.pop(key)
        for key in ("research_lead_count", "lead_used_normalized_evidence_count"):
            report["metrics"].pop(key)
        self.assertTrue(validate_structured_report(report)["valid"])

    def test_offline_leads_only_research_completes_without_fabricating_benchmarks(self):
        with patch.dict("os.environ", {"AOR_OFFLINE": "1"}):
            run = start_research(self.home, as_of=date(2026, 9, 14), offline=True)
            candidate = expand_ideas({"benchmarks": [benchmark()]})["candidates"][0]
            path = self.home / "leads.json"
            path.write_text(json.dumps({"benchmarks": [], "leads": [candidate]}))
            run = resume_research(self.home, run["run_id"], benchmarks_file=path)
            path = self.home / "assessment.json"
            path.write_text(json.dumps({"decision": {"summary": "保留探索线索", "largest_unknown": "付费意愿待核验",
                                                      "next_action": "核验真实任务", "stop_condition": "无法找到目标用户则停止"}}))
            done = resume_research(self.home, run["run_id"], assessment_file=path)
            self.assertEqual(done["status"], "completed")
            report = json.loads(Path(done["artifacts"]["report"]["path"]).read_text())
            self.assertEqual(report["metrics"]["research_lead_count"], 1)
            self.assertEqual(report["metrics"]["qualified_conclusion_count"], 0)
            self.assertTrue((self.home / "state/research-leads.jsonl").exists())

    def test_auto_retained_lead_is_available_in_next_research_history(self):
        with patch.dict("os.environ", {"AOR_OFFLINE": "1"}):
            run = start_research(self.home, as_of=date(2026, 9, 14), offline=True)
            candidate = benchmark()
            candidate.update(mvp_days=45, acquisition_channel=None)
            path = self.home / "benchmarks.json"
            path.write_text(json.dumps({"benchmarks": [candidate]}))
            resume_research(self.home, run["run_id"], benchmarks_file=path)
            path = self.home / "assessment.json"
            path.write_text(json.dumps({"decision": {"summary": "保留线索", "largest_unknown": "交付成本",
                                                      "next_action": "核验最小任务", "stop_condition": "无法缩小范围"}}))
            resume_research(self.home, run["run_id"], assessment_file=path)
            rows = lead_history(self.home, as_of="2026-09-14")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], run["run_id"])


if __name__ == "__main__":
    unittest.main()
