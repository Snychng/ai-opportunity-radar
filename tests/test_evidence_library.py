from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aor.evidence.retrieval import reciprocal_rank_fusion
from aor.storage.evidence_library import EvidenceLibrary, EvidenceLibraryError


def evidence(**updates):
    return {"id": "forum-1", "url": "https://forum.example/posts/1", "source": "forum",
            "original_text": "小商家已付款购买客服工具，每月 49 美元。", "published_at": "2026-09-01",
            "observed_at": "2026-09-02", "raw_json_pointer": "/data/items/0", **updates}


class EvidenceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.library = EvidenceLibrary(self.directory.name)

    def test_revision_cutoff_delta_and_rebuild_keep_authoritative_history(self):
        first = evidence()
        self.library.ingest([first], as_of="2026-09-02", run_id="RUN-20260902-ABCDEF1234", raw_ref="raw/first.json")
        second = evidence(original_text="更正：并未付款，只看过每月 49 美元的报价。", observed_at="2026-09-08")
        self.library.ingest([second], as_of="2026-09-08", run_id="RUN-20260908-ABCDEF1234")
        old = self.library.search("客服", as_of="2026-09-05")
        new = self.library.search("更正", as_of="2026-09-09")
        self.assertEqual(old[0]["original_text"], first["original_text"])
        self.assertEqual(new[0]["original_text"], second["original_text"])
        self.assertNotEqual(old[0]["revision_id"], new[0]["revision_id"])
        self.assertEqual(old[0]["last_observed_at"], "2026-09-02")
        self.assertEqual(self.library.search("客服", as_of="2026-09-09"), [])
        delta = self.library.delta(since="2026-09-05", as_of="2026-09-09")
        self.assertEqual(len(delta["revised"]), 1)
        self.assertEqual(delta["revised"][0]["before_revision_id"], old[0]["revision_id"])
        self.library.index_path.unlink()
        self.assertEqual(self.library.search("更正", as_of="2026-09-09"), new)
        self.assertEqual(self.library.rebuild()["revisions"], 2)

    def test_future_publication_observation_and_timezone_are_excluded(self):
        for values in ({"published_at": "2026-09-11"}, {"observed_at": "2026-09-11"},
                       {"observed_at": "2026-09-10T16:00:00Z"}, {"recorded_on": "2026-09-11"},
                       {"as_of": "2026-09-11"}):
            with self.subTest(values=values), self.assertRaises(EvidenceLibraryError):
                self.library.ingest([evidence(**values)], as_of="2026-09-10")
        self.library.ingest([evidence(observed_at="2026-09-10T15:59:59Z")], as_of="2026-09-10")
        self.assertEqual(self.library.search("", as_of="2026-09-09"), [])
        self.assertEqual(len(self.library.search("", as_of="2026-09-10")), 1)

    def test_empty_ingest_and_legacy_fact_only_evidence_remain_usable(self):
        self.assertEqual(self.library.ingest([], as_of="2026-09-10")["observations_added"], 0)
        legacy = {"url": "https://legacy.example/receipt", "source": "vendor", "fact": "商家已付49美元"}
        self.library.ingest([legacy], as_of="2026-09-10")
        self.assertEqual(self.library.search("已付", as_of="2026-09-10")[0]["fact"], legacy["fact"])
        self.assertEqual(len(self.library.context("", as_of="2026-09-10")["evidence"]), 1)

    def test_normalized_url_relabels_and_retries_preserve_provenance(self):
        records = [evidence(url="http://www.forum.example/posts/1/?utm_source=a#comment"),
                   evidence(source="mirror-label", id="another-id")]
        first = self.library.ingest(records, as_of="2026-09-02", raw_ref="raw/input.json")
        self.assertEqual(first["revisions_added"], 1)
        self.assertEqual(self.library.ingest(records, as_of="2026-09-02", raw_ref="raw/input.json")["observations_added"], 0)
        results = self.library.search("付款", as_of="2026-09-10", run_id="RUN-20260910-ABCDEF1234")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_labels"], ["forum", "mirror-label"])
        self.assertEqual(results[0]["reused_for_run_id"], "RUN-20260910-ABCDEF1234")
        self.assertIn("another-id", results[0]["aliases"])
        self.assertEqual(len(results[0]["same_source_refs"]), 2)
        self.assertEqual(results[0]["raw_refs"][0]["json_pointer"], "/data/items/0")

    def test_same_publisher_has_one_source_key_without_dropping_distinct_posts(self):
        self.library.ingest([evidence(original_publisher="同一作者"),
                             evidence(original_publisher="同一作者", url="https://another.example/post", id="second")],
                            as_of="2026-09-02")
        results = self.library.search("", as_of="2026-09-02")
        self.assertEqual(len(results), 2)
        self.assertEqual(len({item["independent_source_key"] for item in results}), 1)

    def test_nested_future_comment_is_rejected_and_original_raw_refs_survive(self):
        with self.assertRaisesRegex(EvidenceLibraryError, "未来"):
            self.library.ingest([evidence(comments=[{"original_text": "未来付款", "published_at": "2026-09-11"}])],
                                as_of="2026-09-10")
        refs = [{"path": "raw/posts.json", "json_pointer": "/items/0"}]
        self.library.ingest([evidence(raw_refs=refs)], as_of="2026-09-02")
        self.assertEqual(self.library.search("", as_of="2026-09-02")[0]["raw_refs"], refs)

    def test_revision_collision_rejects_whole_batch_before_append(self):
        self.library.ingest([evidence(revision_id="legacy:v1")], as_of="2026-09-02")
        before = self.library.journal_path.read_bytes()
        with self.assertRaisesRegex(EvidenceLibraryError, "修订"):
            self.library.ingest([evidence(url="https://extra.example/new"),
                                 evidence(revision_id="legacy:v1", original_text="原文已经改变")], as_of="2026-09-02")
        self.assertEqual(self.library.journal_path.read_bytes(), before)
        self.assertEqual(len(self.library.search("", as_of="2026-09-02")), 1)

    def test_newer_retraction_removes_current_search_without_erasing_history(self):
        self.library.ingest([evidence()], as_of="2026-09-02")
        self.library.ingest([evidence(retracted=True, observed_at="2026-09-08")], as_of="2026-09-08")
        self.assertEqual(len(self.library.search("", as_of="2026-09-05")), 1)
        self.assertEqual(self.library.search("", as_of="2026-09-09"), [])
        self.assertTrue(self.library.delta(since="2026-09-05", as_of="2026-09-09")["revised"][0]["evidence"]["retracted"])

    def test_reobservation_and_append_log_recovery(self):
        self.library.ingest([evidence()], as_of="2026-09-02")
        saved_index = self.library.index_path.read_bytes()
        self.library.ingest([evidence(observed_at="2026-09-08")], as_of="2026-09-08")
        # 模拟事实日志落盘后，派生索引提交前中断。
        self.library.index_path.write_bytes(saved_index)
        results = self.library.search("", as_of="2026-09-09")
        self.assertEqual(results[0]["first_observed_at"], "2026-09-02")
        self.assertEqual(results[0]["last_observed_at"], "2026-09-08")
        self.assertEqual(len(self.library.delta(since="2026-09-05", as_of="2026-09-09")["reobserved"]), 1)

    def test_later_correction_with_old_source_date_cannot_rewrite_past(self):
        self.library.ingest([evidence()], as_of="2026-09-02")
        original = self.library.search("", as_of="2026-09-03")
        self.library.ingest([evidence(original_text="九月十日才核实的更正")], as_of="2026-09-10",
                            run_id="RUN-20260910-ABCDEF1234")
        self.assertEqual(self.library.search("", as_of="2026-09-03"), original)
        current = self.library.search("", as_of="2026-09-10")
        self.assertEqual(current[0]["original_text"], "九月十日才核实的更正")
        self.assertEqual(current[0]["recorded_on"], "2026-09-10")

    def test_no_fts_and_damaged_cache_are_recoverable(self):
        self.library.ingest([evidence()], as_of="2026-09-02")
        with sqlite3.connect(self.library.index_path) as connection:
            connection.execute("UPDATE meta SET value='0' WHERE key='fts'")
            connection.execute("DROP TABLE IF EXISTS evidence_fts")
        self.assertEqual(len(self.library.search("客服", as_of="2026-09-02")), 1)
        self.library.index_path.write_bytes(b"corrupt cache")
        self.assertEqual(len(self.library.search("客服", as_of="2026-09-02")), 1)

    def test_corrupt_source_refuses_rebuild_and_does_not_rewrite_source(self):
        self.library.ingest([evidence()], as_of="2026-09-02")
        with self.library.journal_path.open("a") as source:
            source.write('{"broken":')
        before = self.library.journal_path.read_bytes()
        with self.assertRaisesRegex(EvidenceLibraryError, "第 2 行"):
            self.library.rebuild()
        self.assertEqual(self.library.journal_path.read_bytes(), before)

    def test_cli_ingest_search_rebuild_offline(self):
        source = Path(self.directory.name) / "fixture.json"
        source.write_text(json.dumps({"evidence": [evidence()]}), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "evidence_library.py"
        command = [sys.executable, str(script), "--root", str(Path(self.directory.name) / "cli")]
        result = subprocess.run(command + ["ingest", "--input", str(source), "--as-of", "2026-09-02"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ["search", "--query", "客服", "--as-of", "2026-09-02"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["evidence"]), 1)

    def test_context_reuses_old_alias_quote_and_discloses_budget_omissions(self):
        from aor.evidence.claims import validate_claims

        self.library.ingest([evidence(), evidence(url="https://extra.example/post", id="second")], as_of="2026-09-02")
        self.library.ingest([evidence(id="new-alias", source="collector")], as_of="2026-09-03")
        latest = self.library.search("", as_of="2026-09-10")
        original = next(item for item in latest if "forum-1" in item["aliases"])
        claim = {"id": "C-1", "statement": "有人购买客服工具", "verification_status": "supports",
                 "evidence_refs": [{"evidence_id": "forum-1", "revision_id": original["revision_id"],
                                    "quote": "已付款购买客服工具"}]}
        self.assertEqual(validate_claims([claim], latest, as_of="2026-09-10")[0]["reference_validation"], "located")
        packet = self.library.context(as_of="2026-09-10", run_id="RUN-20260910-ABCDEF1234", claims=[claim], max_items=1)
        self.assertEqual(packet["omitted"]["evidence_budget"], 1)
        self.assertEqual(packet["claims"][0]["semantic_validation"], "not_performed")
        self.assertEqual(packet["evidence"][0]["reused_for_run_id"], "RUN-20260910-ABCDEF1234")
        self.assertIn("已付款购买客服工具", packet["evidence"][0]["text_refs"][0]["text"])

    def test_multi_query_results_can_be_reingested_without_changing_revision(self):
        self.library.ingest([evidence(comments=[{"text": "相同评论"}, {"text": "相同评论"}, {"text": "第三条"}])],
                            as_of="2026-09-02")
        result = self.library.search(["付款", "客服"], as_of="2026-09-02")
        self.assertNotIn("text", result[0])
        self.assertEqual(result[0]["comments"][2]["text"], "第三条")
        self.assertEqual(self.library.ingest(result, as_of="2026-09-02")["revisions_added"], 0)


class RetrievalFusionTests(unittest.TestCase):
    def test_multiple_queries_preserve_rich_body_and_comments_but_one_url(self):
        short = evidence(original_text="客服", comments=[{"text": "评论一"}])
        full = evidence(original_text="完整原文：小商家付款购买客服", source="other", comments=[{"text": "评论二"}])
        original = deepcopy(short)
        other = evidence(url="https://other.example/post", id="other-post")
        results = reciprocal_rank_fusion([[short, short, other], [full, other]])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["original_text"], full["original_text"])
        self.assertEqual(len(results[0]["comments"]), 2)
        self.assertEqual(len(results[0]["retrieval"]["matches"]), 2)
        self.assertNotIn("total_score", results[0])
        self.assertEqual(short, original)

    def test_conflicting_revisions_require_explicit_snapshot_before_fusion(self):
        for second in (evidence(revision_id="v2"), evidence(original_text="未绑定版本的新正文"),
                       evidence(revision_id="v1", original_text="同一个版本标记但冲突正文"),
                       evidence(revision_id="v1", comments=[{"text": "附加的评论使索引变更"}])):
            with self.subTest(second=second), self.assertRaises(ValueError):
                reciprocal_rank_fusion([[evidence(revision_id="v1")], [second]])


if __name__ == "__main__":
    unittest.main()
