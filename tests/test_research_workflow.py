from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.reporting.report import commit_report, validate_structured_report
from aor.workflow.research import postmortem, resume_research, run_paid_batch, start_research
from tests.test_idea_funnel import benchmark


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


class ResearchWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.environment = patch.dict(os.environ, {"AOR_OFFLINE": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assessment(self, run: dict) -> Path:
        tiered = json.loads(Path(run["artifacts"]["tiered"]["path"]).read_text())
        scores = [{"id": row["id"], "track": "needle",
                   "scores": dict.fromkeys(("demand", "new_form", "distribution", "regional_gap",
                                            "monetization", "mvp_feasibility", "evidence"), 8),
                   "auxiliary_scores": dict.fromkeys(("first_revenue", "scale", "personal_influence", "confidence"), 7)}
                  for row in tiered["deep_candidates"]]
        return write(self.home / "assessment-input.json", {"scores": scores,
            "decision": {"summary": "优先核验真实任务中的交付成本", "primary_id": None,
                         "largest_unknown": "尚无个人获客验证", "next_action": "取得一份真实任务样本",
                         "stop_condition": "无法获得真实任务则暂停"}})

    def test_agent_handoff_report_commit_and_resume_after_interruption(self):
        run = start_research(self.home, as_of=date(2026, 9, 10), offline=True)
        self.assertEqual(run["status"], "awaiting_benchmarks")
        bench = write(self.home / "bench-input.json", {"benchmarks": [benchmark()], "dimensions": {"offers": ["订阅", "按次"]}})
        run = resume_research(self.home, run["run_id"], benchmarks_file=bench)
        self.assertEqual(run["status"], "awaiting_assessment")
        self.assertEqual((self.home / "state/opportunities.jsonl").read_text(), "")
        assessment = self.assessment(run)
        with patch("aor.workflow.research.render_report", side_effect=OSError("模拟报告 JSON 保存后渲染中断")):
            with self.assertRaises(OSError):
                resume_research(self.home, run["run_id"], assessment_file=assessment)
        self.assertEqual((self.home / "state/opportunities.jsonl").read_text(), "")

        def interrupted(home, report):
            commit_report(home, report)
            raise OSError("模拟提交已完成但回执保存前退出")

        with patch("aor.workflow.research.commit_report", side_effect=interrupted):
            with self.assertRaises(OSError):
                resume_research(self.home, run["run_id"])
        with patch("aor.workflow.research._refresh_library", side_effect=AssertionError("提交重放不能刷新证据")):
            finished = resume_research(self.home, run["run_id"])
        self.assertEqual(finished["status"], "completed")
        records = [json.loads(line) for line in (self.home / "state/opportunities.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["variants"]), 2)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertTrue(validate_structured_report(report)["valid"])
        self.assertEqual(records[0]["report_sha256"], json.loads(Path(finished["artifacts"]["receipt"]["path"]).read_text())["report_sha256"])
        self.assertIn("收费对标支持的候选", Path(finished["report_path"]).read_text())
        Path(finished["report_path"]).unlink()
        self.assertEqual(resume_research(self.home, run["run_id"])["status"], "completed")
        self.assertTrue(Path(finished["report_path"]).exists())
        with self.assertRaisesRegex(ValueError, "不可变"):
            resume_research(self.home, run["run_id"], assessment_file=assessment)
        report["metrics"]["deep_candidate_count"] = 99
        self.assertFalse(validate_structured_report(report)["valid"])

    def test_empty_research_is_valid_and_same_day_new_runs_are_distinct(self):
        first = start_research(self.home, as_of=date(2026, 9, 10), offline=True)
        second = start_research(self.home, as_of=date(2026, 9, 10), offline=True, parent_run_id=first["run_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        bench = write(self.home / "empty.json", {"benchmarks": [], "empty_reason": "现有证据无法确认收费对标"})
        run = resume_research(self.home, first["run_id"], benchmarks_file=bench)
        finished = resume_research(self.home, first["run_id"], assessment_file=self.assessment(run))
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertEqual(report["metrics"]["qualified_conclusion_count"], 0)
        self.assertEqual((self.home / "state/opportunities.jsonl").read_text(), "")

    def test_offline_cli_rejects_paid_path_before_execution(self):
        import research

        for flag in ("--offline", "--no-collect"):
            with self.subTest(flag=flag), patch("research.run_paid_batch") as execute, redirect_stderr(io.StringIO()):
                result = research.main(["resume", "RUN-20260910-ABCDEF1234", "--paid-plan", "unused.json",
                                        "--max-cost-usd", "1", flag])
                self.assertEqual(result, 2)
                execute.assert_not_called()

    def test_history_does_not_report_current_source_health(self):
        history = write(self.home / "history.json", {"schema_version": "3.0", "run_id": "RUN-20260909-ABCDEF1234",
            "as_of": "2026-09-09", "evidence": [], "stats": {"source_status": {"github": "ok"}}})
        current = write(self.home / "current.json", {"evidence": [], "stats": {"source_status": {"github": "rate-limited"}}})
        run = start_research(self.home, as_of=date(2026, 9, 10), offline=True, evidence_files=[history, current])
        result = postmortem(self.home, run["run_id"])
        self.assertEqual(result["source_outcomes"], {"github": "rate-limited"})
        self.assertEqual(len(result["reused_evidence"]), 1)
        self.assertIn("history-context", run["artifacts"])

    def test_interrupted_paid_attempts_and_parser_recovery_keep_full_run_cost(self):
        import tikhub_query as tq
        from tests.test_tikhub_query import sample_plan, pricing_rows, healthy_account_transport

        run = start_research(self.home, as_of=date(2026, 9, 10))  # 环境离线，manifest 可用于之后的显式补证。
        plan = sample_plan()
        plan.update(run_id=run["run_id"], as_of="2026-09-10", stage="evidence_gap_verification")
        plan["cost_policy"] = {"purpose": "candidate_gate_verification_only", "stop_after_requests_without_yield": 3}
        for request in plan["requests"]:
            request["evidence_gap"] = {"candidate_id": "CAND-test-paid", "missing_gate": "target_payment",
                                       "target_region": "美国", "expected_promotion": "b_to_a"}
        plan_file = write(self.home / "paid-plan.json", plan)
        replies = [{"data": []}, KeyboardInterrupt()]
        transport_calls = []

        def transport(**kwargs):
            transport_calls.append(kwargs["url"])
            response = replies.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        def offline_execute(plan, **options):
            return tq._execute_plan_with_pricing(plan, pricing_rows(), **{**options, "token": "synthetic-test-token"},
                                                account_transport=healthy_account_transport, transport=transport)

        with patch("tikhub_query.execute_plan", side_effect=offline_execute), patch.dict(os.environ, {"AOR_OFFLINE": "0"}):
            with self.assertRaises(KeyboardInterrupt):
                run_paid_batch(self.home, run["run_id"], plan_file, max_cost_usd=0.02, batch_id="gap")
            with patch("normalize_tikhub_results.normalize_documents", side_effect=ValueError("模拟解析中断")):
                with self.assertRaisesRegex(ValueError, "解析中断"):
                    run_paid_batch(self.home, run["run_id"], plan_file, max_cost_usd=0.02, batch_id="gap", resume=True)
        self.assertEqual(len(transport_calls), 2)
        manifest = json.loads((Path(run["run_path"]) / "run.json").read_text())
        self.assertEqual(len(manifest["execution_artifacts"]), 1)
        bench = write(self.home / "empty.json", {"benchmarks": [], "empty_reason": "本轮证据不足"})
        run = resume_research(self.home, run["run_id"], benchmarks_file=bench, collect=False)
        finished = resume_research(self.home, run["run_id"], assessment_file=self.assessment(run), collect=False)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertEqual(report["metrics"]["estimated_cost_usd"], 0.011)
        self.assertEqual(report["metrics"]["paid_request_count"], 2)
        self.assertEqual(report["run_ledger"]["attempt_states"]["outcome_unknown"], 1)

    def test_claim_and_score_basis_share_original_revision_after_benchmark_summary(self):
        source = {"id": "receipt-raw", "url": "https://vendor.example/receipt", "source": "vendor",
                  "original_text": "合成测试原文：商家已支付49美元购买客服服务", "observed_at": "2026-09-09"}
        material = write(self.home / "material.json", {"evidence": [source]})
        run = start_research(self.home, as_of=date(2026, 9, 10), focus="中文客服自动化", offline=True, evidence_files=[material])
        bench = write(self.home / "benchmark.json", {"benchmarks": [benchmark()], "dimensions": {}})
        run = resume_research(self.home, run["run_id"], benchmarks_file=bench)
        context = json.loads(Path(run["artifacts"]["evidence-context"]["path"]).read_text())["evidence"]
        original = next(row for row in context if row["url"] == source["url"])
        self.assertEqual(original["original_text"], source["original_text"])
        ref = {"evidence_id": original["evidence_id"], "revision_id": original["revision_id"], "quote": "商家已支付49美元"}
        assessment_path = self.assessment(run)
        assessment = json.loads(assessment_path.read_text())
        assessment["claims"] = [{"id": "payment", "statement": "演示付款事实", "verification_status": "supports", "evidence_refs": [ref]}]
        for score in assessment["scores"]:
            score["score_basis"] = {"monetization": {"rationale": "存在付款事实，尚未证明本产品购买意愿", "evidence_refs": [ref]}}
        write(assessment_path, assessment)
        finished = resume_research(self.home, run["run_id"], assessment_file=assessment_path)
        report = json.loads(Path(finished["artifacts"]["report"]["path"]).read_text())
        self.assertEqual(report["claims"][0]["evidence_refs"][0]["field"], "original_text")
        self.assertEqual(report["metrics"]["deep_candidate_count"], 1)
        self.assertIn("尚未证明本产品购买意愿", Path(finished["report_path"]).read_text())
        # 同页演示原文的标记不能在对标摘要省略字段时丢失并升级为 A。
        write(material, {"evidence": [{**source, "is_demo": True}]})
        revised = start_research(self.home, as_of=date(2026, 9, 10), offline=True, evidence_files=[material])
        revised = resume_research(self.home, revised["run_id"], benchmarks_file=bench)
        tiered = json.loads(Path(revised["artifacts"]["tiered"]["path"]).read_text())
        self.assertEqual(tiered["deep_candidates"], [])


if __name__ == "__main__":
    unittest.main()
