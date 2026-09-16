"""网站公开链路回归：真正报告导出、引用门槛、兼容视图和原子撤回。"""

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
from aor.opportunity.exploration import normalize_leads
from aor.reporting.public import evidence_publication_issue, export_public, public_text, public_url, review_content_hash, write_public_bundle
from aor.reporting.public_contract import typescript, validate_public_dataset
from aor.reporting.report import build_report
from aor.storage.evidence_library import EvidenceLibrary
from build_query_plan import build_plan


class PublicContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.plan = build_plan(date(2026, 9, 14), self.home)
        raw = {"id": "reddit:post-1", "source": "reddit", "source_item_id": "post-1", "evidence_kind": "post",
               "url": "https://www.reddit.com/r/gaming/comments/post1/", "title": "固定队友协调",
               "original_text": "我们每周花两小时协调队友时间。报名表仍然需要来回确认。",
               "observed_at": "2026-09-14", "published_at": "2026-09-13", "industry_ids": ["gaming"],
               "evidence_role": "usage_behavior", "relevance_status": "relevant", "window_status": "in_window",
               "raw_file": "/Users/private/raw.json", "raw_refs": [{"path": "/tmp/private/cache.json"}]}
        self.stored = EvidenceLibrary._event(raw, as_of="2026-09-14", run_id=self.plan["run_id"], raw_ref=None)["record"]
        row = {"title": "游戏组队协调", "target_user": "只有晚上有空的游戏玩家", "problem_or_desire": "玩家每周来回协调队友时间",
               "wedge": "收集日程并解释冲突，提出替补组队方案", "industry_ids": ["gaming"], "subtrack_ids": ["teammate_matching"],
               "evidence": [deepcopy(self.stored)], "ai_value": {"status": "hypothesis",
                 "baseline": "手动填写表格再逐个私聊确认时间", "capability": "从群聊解析成员约束并解释日程冲突",
                 "user_benefit": "减少玩家协调日程时的重复沟通", "incremental_advantage": "相比固定表格可处理聊天里的临时变更"}}
        self.row = normalize_leads([row], run_id=self.plan["run_id"], as_of="2026-09-14")[0]

    def approve(self, row):
        row["publication_review"] = {"status": "approved", "reviewer": "test-reviewer", "rationale": "核对示例引用与明确假设",
                                     "reviewed_at": "2026-09-14", "content_sha256": review_content_hash(row)}
        return row

    def report(self, row=None, records=None, *, approve=True):
        row = deepcopy(self.row if row is None else row)
        records = deepcopy([self.stored] if records is None else records)
        if approve:
            self.approve(row)
        tiered = {"schema_version": "3.0", "run_id": self.plan["run_id"], "as_of": "2026-09-14",
                  "deep_candidates": [], "validated_ideas": [], "regional_signals": [], "rejected": [], "research_leads": [row]}
        return build_report(tiered, decision={"summary": "保留一条待验证线索", "largest_unknown": "是否愿意持续使用",
            "next_action": "访谈有真实组队经历的玩家", "stop_condition": "没有重复需求时归档"}, claim_evidence=records,
            evidence=[{"run_id": self.plan["run_id"], "as_of": "2026-09-14", "evidence": records}], research_plan=self.plan)

    def test_export_real_report_is_allowlisted_and_all_three_views_validate(self):
        output = self.home / "public"
        result = write_public_bundle(self.report(), output)
        self.assertTrue(output.is_symlink())
        self.assertEqual(result["published_count"], 1)
        documents = [json.loads(path.read_text()) for path in output.rglob("*.json")]
        self.assertEqual({document["view"] for document in documents}, {"full", "index", "detail"})
        for document in documents:
            validation = validate_public_dataset(document)
            self.assertTrue(validation["valid"], validation)
            text = json.dumps(document, ensure_ascii=False)
            self.assertNotIn("raw_refs", text)
            self.assertNotIn("/Users/", text)
            self.assertNotIn("/tmp/", text)
            self.assertNotIn("publication_review", text)

    def test_unreviewed_and_changed_content_are_withheld_without_silently_approving(self):
        self.assertEqual(export_public(self.report(approve=False))[0]["items"], [])
        row = self.approve(deepcopy(self.row))
        row["wedge"] += "新增一项不同服务"
        public, withheld = export_public(self.report(row, approve=False))
        self.assertEqual(public["items"], [])
        self.assertIn("当前内容", withheld[0]["reason"])

    def test_supported_ai_value_requires_real_refs_current_quotes_and_review_date(self):
        row = deepcopy(self.row)
        row["ai_value"].update(status="supported", evidence_refs=[{
            "evidence_id": self.stored["evidence_id"], "revision_id": self.stored["revision_id"], "quote": "报名表仍然需要来回确认"}],
            review={"reviewer": "test-reviewer", "reviewed_at": "2026-09-14", "rationale": "仅用于验证引用链路的离线样例"})
        public, withheld = export_public(self.report(row))
        self.assertFalse(withheld)
        self.assertTrue(public["items"][0]["ai_value"]["evidence_refs"])
        row["ai_value"]["review"]["reviewed_at"] = "2026-09-15"
        self.assertEqual(export_public(self.report(row))[0]["items"], [])
        row["ai_value"]["review"]["reviewed_at"] = "2026-09-14"
        row["ai_value"]["evidence_refs"][0]["evidence_id"] = "EVID-MISSING"
        with self.assertRaises(ValueError):
            export_public(self.report(row))

    def test_observed_need_requires_semantic_review_bound_to_actual_evidence(self):
        row = deepcopy(self.row)
        row["research_status"] = "observed_need"
        row["evidence"][0]["semantic_review"] = {"status": "relevant"}
        with self.assertRaisesRegex(ValueError, "观察到需求"):
            self.report(row)
        stored = deepcopy(self.stored)
        stored["relevance_review"] = {"status": "relevant", "reviewer": "test-reviewer", "reviewed_at": "2026-09-14",
                                      "evidence_id": stored["evidence_id"], "revision_id": stored["revision_id"]}
        row["evidence"] = [deepcopy(stored)]
        public, withheld = export_public(self.report(row, [stored]))
        self.assertFalse(withheld)
        self.assertEqual(public["items"][0]["stage"], "observed_need")
        coverage = next(r for r in public["coverage"] if r["industry_id"] == "gaming")
        self.assertEqual(coverage["reviewed_count"], 1)
        self.assertEqual(coverage["recent_demand_count"], 1)
        stored["relevance_review"]["revision_id"] = "stale"
        # 行中仍带旧批准，真实证据库的复核已失效；公开导出必须检查已解析的 stored。
        self.assertEqual(export_public(self.report(row, [stored]))[0]["items"], [])

    def test_current_public_directory_removes_withheld_details_and_failure_preserves_current_generation(self):
        output = self.home / "public"
        write_public_bundle(self.report(), output)
        old_detail = output / "items" / f"{self.row['lead_id']}.json"
        self.assertTrue(old_detail.exists())
        old_generation = output.resolve()
        with patch("aor_runtime.atomic_json", side_effect=OSError("模拟写入中断")):
            with self.assertRaises(OSError):
                write_public_bundle(self.report(approve=False), output)
        self.assertEqual(output.resolve(), old_generation)
        self.assertTrue(old_detail.exists())
        write_public_bundle(self.report(approve=False), output)
        self.assertFalse(old_detail.exists())
        self.assertNotEqual(output.resolve(), old_generation)
        self.assertTrue(old_generation.exists())
        self.assertEqual(json.loads((output / "index.json").read_text())["items"], [])

    def test_existing_unmanaged_directory_is_not_overwritten(self):
        output = self.home / "unmanaged"
        output.mkdir()
        (output / "important.txt").write_text("保留无关内容")
        with self.assertRaisesRegex(ValueError, "专用公开目录"):
            write_public_bundle(self.report(), output)
        self.assertEqual((output / "important.txt").read_text(), "保留无关内容")

    def test_consumer_allows_minor_additive_fields_but_known_types_and_major_version_remain_checked(self):
        public, _ = export_public(self.report())
        public["contract_version"] = "1.2.0"
        public["new_display_hint"] = "future"
        public["items"][0]["new_annotation"] = "future"
        self.assertFalse(validate_public_dataset(public)["valid"])
        self.assertTrue(validate_public_dataset(public, consumer=True)["valid"])
        public["contract_version"] = "2.0.0"
        self.assertFalse(validate_public_dataset(public, consumer=True)["valid"])
        public["contract_version"] = "1.2.0"
        public["items"][0]["title"] = []
        self.assertFalse(validate_public_dataset(public, consumer=True)["valid"])

    def test_schema_checks_subtracks_dates_coverage_and_stage_relationships(self):
        original, _ = export_public(self.report())
        changes = [lambda p: p["items"][0].update(subtrack_ids=["unknown_track"]),
                   lambda p: p["items"][0].update(stage="candidate", evidence_tier=None),
                   lambda p: p["items"][0].update(promoted_to="/Users/private/file"),
                   lambda p: p["items"][0].update(updated_at="next week"),
                   lambda p: p["evidence"][0].update(observed_at="/Users/private/file"),
                   lambda p: p["coverage"][0].update(industry_id="missing"),
                   lambda p: p["coverage"].append(deepcopy(p["coverage"][0])),
                   lambda p: p["quality"].update(market_validated=0)]
        for mutation in changes:
            public = deepcopy(original)
            mutation(public)
            self.assertFalse(validate_public_dataset(public)["valid"])

    def test_text_redacts_contacts_adjacent_to_chinese_and_local_paths(self):
        for text in ("手机号是13800138000", "我住在/Users/private/a.txt", "缓存/private/tmp/test.json",
                     "phone: +1 (415) 555-0100", "email@example.com"):
            self.assertNotEqual(public_text(text), text)
        for url in ("http://localhost/private", "https://127.0.0.1/file", "https://192.168.1.1/file",
                    "https://example.com/cache/signed-token"):
            with self.assertRaises(ValueError):
                public_url(url)
        self.assertEqual(public_url("https://example.com/page?id=123&token=private&utm_source=ad"), "https://example.com/page?id=123")

    def test_typescript_is_generated_from_the_committed_schema(self):
        generated = Path(__file__).resolve().parents[1] / "types/public-v1.ts"
        self.assertEqual(generated.read_text(), typescript())
        for name in ("PublicDataset", "PublicIndex", "PublicDetail", "PublicDocument"):
            self.assertIn(f"export type {name} =", generated.read_text())

    def test_historical_quote_cannot_bypass_current_retraction_or_correction(self):
        old = deepcopy(self.stored)
        old["historical_reference_only"] = True
        new = {**deepcopy(self.stored), "revision_id": self.stored["evidence_id"] + ":current",
               "original_text": "更正：此前描述的需求已经消失。", "retracted": True}
        public, withheld = export_public(self.report(records=[new, old]))
        self.assertFalse(public["items"])
        self.assertIn("当前已撤回", withheld[0]["reason"])
        new["retracted"] = False
        public, withheld = export_public(self.report(records=[old, new]))
        self.assertFalse(public["items"])
        self.assertIn("当前正文", withheld[0]["reason"])
        state = {"current_evidence_state": {self.stored["evidence_id"]: {
            "current_revision_id": self.stored["revision_id"], "retracted": False, "status": "ambiguous"}}}
        self.assertIsNotNone(evidence_publication_issue(self.stored["evidence_id"], self.stored["revision_id"], state))


if __name__ == "__main__":
    unittest.main()
