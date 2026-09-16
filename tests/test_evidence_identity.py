"""真实故障形状的离线回归：父帖链接、评论对象、旧修订和派生归类。"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aor.evidence.claims import validate_claims
from aor.evidence.identity import canonical_evidence_url, canonical_sha256, evidence_object_identity
from aor.evidence.retrieval import EvidenceReferenceError, reciprocal_rank_fusion, resolve_evidence_reference
from aor.storage.evidence_library import EvidenceLibrary, EvidenceLibraryError


URL = "https://www.xiaohongshu.com/explore/post-1"


def post(**updates):
    return {"id": "xiaohongshu:post-1", "source": "xiaohongshu", "source_item_id": "post-1",
            "evidence_kind": "post", "url": URL, "original_text": "每晚下班只有两小时，希望找固定队友。",
            "observed_at": "2026-09-01", "published_at": "2026-09-01", **updates}


def comment(number=1, **updates):
    return {"id": f"xiaohongshu:comment:comment-{number}", "source": "xiaohongshu",
            "source_item_id": f"comment-{number}", "parent_item_id": "xiaohongshu:post-1",
            "parent_url": URL, "url": URL, "url_kind": "parent_post", "original_text": f"评论 {number}：可以可以！",
            "observed_at": "2026-09-01", "published_at": "2026-09-01", **updates}


def legacy_event(record, revision):
    item = deepcopy(record)
    old_id = "EVID-" + canonical_sha256(canonical_evidence_url(item["url"]))[:16].upper()
    item.update(evidence_id=old_id, library_evidence_id=old_id, revision_id=revision,
                aliases=[old_id, item["id"]], content_hash=canonical_sha256(record))
    event = {"schema_version": "3.0", "as_of": item["observed_at"], "observed_at": item["observed_at"],
             "run_id": None, "raw_ref": "raw/fixture.json", "record": item}
    event["event_id"] = canonical_sha256(event)
    return event


class EvidenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = EvidenceLibrary(Path(self.temp.name) / "library")

    def test_parent_post_and_multiple_comments_survive_every_ingest_order_and_rebuild(self):
        rows = [post(), comment(), comment(2)]
        for index, batch in enumerate((rows, list(reversed(rows)), [rows[1], rows[0], rows[2]])):
            library = EvidenceLibrary(Path(self.temp.name) / f"case-{index}")
            library.ingest(batch, as_of="2026-09-01")
            results = library.search("", as_of="2026-09-01", limit=None)
            self.assertEqual(len(results), 3)
            self.assertEqual({item["original_text"] for item in results}, {item["original_text"] for item in rows})
            self.assertEqual(len({item["evidence_id"] for item in results}), 3)
            self.assertEqual(len(reciprocal_rank_fusion([results, list(reversed(results))])), 3)
            self.assertEqual(library.rebuild()["revisions"], 3)
            self.assertEqual(results, library.search("", as_of="2026-09-01", limit=None))

    def test_comment_native_permalink_and_detail_metadata_do_not_create_other_objects(self):
        self.library.ingest([post(), comment()], as_of="2026-09-01")
        first = self.library.search("", as_of="2026-09-01", limit=None)
        result = self.library.ingest([
            post(evidence_kind="post_detail", source_item_id=None, query="another task", industry_ids=["gaming"]),
            comment(url="https://www.xiaohongshu.com/comment/comment-1", url_kind="comment", query_ids=["other"],
                    relevance_status="unrelated", window_status="out_of_window", quality_version="3.0",
                    local_relevance=0.0, matched_terms=[], published_at_interval={"earliest": "2026-08-01"}),
        ], as_of="2026-09-01")
        self.assertEqual(result["revisions_added"], 0)
        self.assertEqual({item["revision_id"] for item in first},
                         {item["revision_id"] for item in self.library.search("", as_of="2026-09-01", limit=None)})

    def test_missing_comment_native_identity_cannot_merge_with_parent_url(self):
        with self.assertRaisesRegex(EvidenceLibraryError, "缺少原生"):
            self.library.ingest([{"source": "forum", "url": URL, "url_kind": "parent_post", "original_text": "评论"}],
                                as_of="2026-09-01")

    def test_native_reply_kinds_and_namespaces_stay_distinct(self):
        self.assertEqual(evidence_object_identity(comment()), ("xiaohongshu", "comment", "comment-1"))
        self.library.ingest([post(source_item_id="same"), comment(source_item_id="same"),
                             post(source="other-platform", source_item_id="same")], as_of="2026-09-01")
        self.assertEqual(len(self.library.search("", as_of="2026-09-01")), 3)

    def test_url_resolution_refuses_ambiguity_and_explicit_bad_id_never_falls_back(self):
        self.library.ingest([post(), comment(), comment(2)], as_of="2026-09-01")
        records = self.library.search("", as_of="2026-09-01", limit=None)
        with self.assertRaisesRegex(EvidenceReferenceError, "不唯一"):
            resolve_evidence_reference({"url": URL}, records)
        bound = resolve_evidence_reference({"url": URL, "original_text": post()["original_text"]}, records)
        self.assertEqual(bound["id"], post()["id"])
        self.assertEqual(resolve_evidence_reference({"source": "xiaohongshu", "source_item_id": "comment-1",
                         "evidence_kind": "comment"}, records)["id"], comment()["id"])
        for reference in ({"url": URL, "evidence_id": "missing"},
                          {"evidence_id": bound["evidence_id"], "library_evidence_id": "EVID-CONFLICT"},
                          {"evidence_id": bound["evidence_id"], "revision_id": "missing", "quote": "两小时"},
                          {"evidence_id": bound["evidence_id"], "original_text": comment()["original_text"]}):
            with self.subTest(reference=reference), self.assertRaises(EvidenceReferenceError):
                resolve_evidence_reference(reference, records)

    def test_historical_revision_resolves_without_turning_comment_into_original_post(self):
        self.library.ingest([post(), comment()], as_of="2026-09-01")
        original = self.library.resolve({"id": post()["id"]}, as_of="2026-09-01")
        self.library.ingest([post(original_text="更正：已经找到队友。", observed_at="2026-09-03")], as_of="2026-09-03")
        historical = self.library.resolve({"evidence_id": original["evidence_id"], "revision_id": original["revision_id"]},
                                          as_of="2026-09-04")
        self.assertEqual(historical["original_text"], post()["original_text"])
        self.assertEqual(self.library.resolve({"id": post()["id"]}, as_of="2026-09-04")["original_text"], "更正：已经找到队友。")

    def _write_legacy(self, events):
        self.library.root.mkdir()
        self.library.journal_path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
        return self.library.journal_path.read_bytes()

    def test_explicit_migration_recovers_all_objects_and_preserves_legacy_revision_references(self):
        events = [legacy_event(post(), "old:post"), legacy_event(comment(), "old:comment1"),
                  legacy_event(comment(2), "old:comment2"),
                  legacy_event(post(original_text="更正：已经找到队友。", observed_at="2026-09-03"), "old:post2")]
        before = self._write_legacy(events)
        with self.assertRaisesRegex(EvidenceLibraryError, "先 migrate_identities"):
            self.library.search("", as_of="2026-09-04")
        with self.assertRaisesRegex(EvidenceLibraryError, "先 migrate_identities"):
            self.library.ingest([post()], as_of="2026-09-04")
        target = Path(self.temp.name) / "migrated"
        result = self.library.migrate_identities(target)
        self.assertEqual(self.library.journal_path.read_bytes(), before)
        self.assertEqual(result["identities"], 3)
        self.assertEqual(len(result["ambiguous_legacy_ids"]), 1)
        migrated = EvidenceLibrary(target)
        self.assertEqual(len(migrated.search("", as_of="2026-09-04", limit=None)), 3)
        old_id = events[0]["record"]["evidence_id"]
        with self.assertRaises(EvidenceReferenceError):
            migrated.resolve({"evidence_id": old_id}, as_of="2026-09-04")
        for event in events:
            original = event["record"]
            resolved = migrated.resolve({"evidence_id": old_id, "revision_id": original["revision_id"]}, as_of="2026-09-04")
            self.assertEqual(resolved["original_text"], original["original_text"])
        original = migrated.resolve({"evidence_id": old_id, "revision_id": "old:post"}, as_of="2026-09-04")
        claims = [{"id": "C-1", "statement": "下班后寻找固定队友", "verification_status": "supports", "evidence_refs": [
            {"evidence_id": old_id, "revision_id": "old:post", "quote": "每晚下班只有两小时"}]}]
        validated = validate_claims(claims, [original], as_of="2026-09-04")
        self.assertEqual(validated[0]["evidence_refs"][0]["evidence_id"], original["evidence_id"])
        with self.assertRaisesRegex(EvidenceLibraryError, "尚不存在"):
            self.library.migrate_identities(target)

    def test_migration_unifies_only_metadata_revisions_without_losing_old_reference_alias(self):
        events = [legacy_event(post(query="first"), "old:first"), legacy_event(post(query="second"), "old:second")]
        self._write_legacy(events)
        target = Path(self.temp.name) / "migrated"
        self.library.migrate_identities(target)
        migrated = EvidenceLibrary(target)
        old_id = events[0]["record"]["evidence_id"]
        refs = [migrated.resolve({"evidence_id": old_id, "revision_id": revision}, as_of="2026-09-01")
                for revision in ("old:first", "old:second")]
        self.assertEqual(refs[0]["revision_id"], refs[1]["revision_id"])
        self.assertEqual(len(migrated.search("", as_of="2026-09-01")[0]["legacy_references"]), 2)

    def test_corrupt_migration_never_publishes_destination(self):
        events = [legacy_event(post(), "old:first")]
        self._write_legacy(events)
        with self.library.journal_path.open("a") as handle:
            handle.write("{broken")
        target = Path(self.temp.name) / "migrated"
        with self.assertRaisesRegex(EvidenceLibraryError, "第 2 行"):
            self.library.migrate_identities(target)
        self.assertFalse(target.exists())

    def test_cli_migration_writes_new_library_without_overwriting_source(self):
        before = self._write_legacy([legacy_event(post(), "old:post"), legacy_event(comment(), "old:comment")])
        target = Path(self.temp.name) / "migrated-cli"
        script = Path(__file__).resolve().parents[1] / "scripts/evidence_library.py"
        result = subprocess.run([sys.executable, str(script), "--root", str(self.library.root),
                                 "migrate-identities", "--destination", str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["identities"], 2)
        self.assertEqual(self.library.journal_path.read_bytes(), before)
        self.assertTrue((target / "identity-migration.json").exists())

    def test_reimported_historical_cache_cannot_restore_retracted_or_corrected_source(self):
        original = post()
        self.library.ingest([original], as_of="2026-09-01")
        first = self.library.resolve({"id": original["id"]}, as_of="2026-09-01")
        self.library.ingest([post(original_text="更正后内容", retracted=True, observed_at="2026-09-03")], as_of="2026-09-03")
        # 导入时间更新甚至与更正同日，来源实际仍是旧缓存。
        for day in ("2026-09-03", "2026-09-04"):
            self.library.ingest([{**original, "historical_import": True}], as_of=day)
            self.assertEqual(self.library.search("", as_of=day), [])
            current = self.library.search("", as_of=day, include_retracted=True)[0]
            self.assertTrue(current["retracted"])
            self.assertEqual(current["original_text"], "更正后内容")
            historical = self.library.resolve({"evidence_id": first["evidence_id"], "revision_id": first["revision_id"]}, as_of=day)
            self.assertEqual(historical["original_text"], original["original_text"])

    def test_history_only_objects_remain_usable_then_yield_to_an_actual_observation(self):
        self.library.ingest([post(historical_import=True)], as_of="2026-09-03", run_id="RUN-20260903-0000000001")
        reused = self.library.search("", as_of="2026-09-04", run_id="RUN-20260904-0000000001")[0]
        self.assertTrue(reused["historical_import"])
        self.assertEqual(reused["reused_for_run_id"], "RUN-20260904-0000000001")
        self.library.ingest([post(original_text="实际重新采集后的新正文", observed_at="2026-09-04")], as_of="2026-09-04")
        self.library.ingest([post(historical_import=True)], as_of="2026-09-05")
        current = self.library.search("", as_of="2026-09-05")[0]
        self.assertEqual(current["original_text"], "实际重新采集后的新正文")
        self.assertFalse(current.get("historical_import", False))

    def test_same_revision_preserves_independent_derivation_refs(self):
        first = {"raw_sha256": "a" * 64, "parser_version": "2.0", "derivation_id": "A"}
        second = {"raw_sha256": "b" * 64, "parser_version": "2.0", "derivation_id": "B"}
        self.library.ingest([post(derivation_refs=[first])], as_of="2026-09-01")
        result = self.library.ingest([post(derivation_refs=[second])], as_of="2026-09-02")
        self.assertEqual(result["revisions_added"], 0)
        row = self.library.search("", as_of="2026-09-03")[0]
        self.assertEqual(row["derivation_refs"], [first, second])

    def test_same_day_later_date_only_observation_wins_over_old_precise_timestamp(self):
        for same_body in (False, True):
            with self.subTest(same_body=same_body):
                library = EvidenceLibrary(Path(self.temp.name) / f"same-day-{same_body}")
                old = post(observed_at="2026-09-14T07:44:59Z", query="旧材料")
                new = post(observed_at="2026-09-14", query="重新核验",
                           original_text=old["original_text"] if same_body else "重新核验后的正文")
                library.ingest([old], as_of="2026-09-14")
                original = library.search("", as_of="2026-09-14")[0]
                library.ingest([new], as_of="2026-09-14")
                current = library.search("", as_of="2026-09-14")[0]
                self.assertEqual(current["original_text"], new["original_text"])
                self.assertEqual(current["query"], "重新核验")
                self.assertEqual(current["observed_at"], "2026-09-14")
                resolved = library.resolve({"evidence_id": current["evidence_id"],
                                            "revision_id": current["revision_id"]}, as_of="2026-09-14")
                self.assertEqual(resolved["query"], "重新核验")
                self.assertEqual(library.search("", as_of="2026-09-13"), [])
                if not same_body:
                    historical = library.resolve({"evidence_id": original["evidence_id"],
                                                  "revision_id": original["revision_id"]}, as_of="2026-09-14")
                    self.assertEqual(historical["original_text"], old["original_text"])

    def test_same_day_repeated_ingest_does_not_reorder_journal_or_restore_old_revision(self):
        old = post(observed_at="2026-09-14T07:44:59Z")
        new = post(observed_at="2026-09-14", original_text="重新核验后的正文")
        self.library.ingest([old], as_of="2026-09-14", raw_ref="raw/old.json")
        self.library.ingest([new], as_of="2026-09-14", raw_ref="raw/new.json")
        before = self.library.journal_path.read_bytes()
        for _ in range(2):
            for record, raw_ref in ((old, "raw/old.json"), (new, "raw/new.json")):
                result = self.library.ingest([record], as_of="2026-09-14", raw_ref=raw_ref)
                self.assertEqual(result["observations_added"], 0)
                self.assertEqual(self.library.search("", as_of="2026-09-14")[0]["original_text"],
                                 new["original_text"])
        self.assertEqual(self.library.journal_path.read_bytes(), before)

    def test_same_day_revision_choice_survives_rebuild_without_changing_journal(self):
        self.library.ingest([post(observed_at="2026-09-14T07:44:59Z")], as_of="2026-09-14")
        self.library.ingest([post(observed_at="2026-09-14", original_text="重新核验后的正文")],
                            as_of="2026-09-14")
        before = self.library.journal_path.read_bytes()
        expected = self.library.search("", as_of="2026-09-14")
        self.assertEqual(expected[0]["original_text"], "重新核验后的正文")
        self.library.rebuild()
        self.assertEqual(self.library.search("", as_of="2026-09-14"), expected)
        self.assertEqual(self.library.journal_path.read_bytes(), before)

    def test_same_day_historical_snapshot_import_cannot_replace_later_verified_revision(self):
        old = post(observed_at="2026-09-14T07:44:59Z")
        self.library.ingest([old], as_of="2026-09-14")
        self.library.ingest([post(observed_at="2026-09-14", original_text="重新核验后的正文")],
                            as_of="2026-09-14")
        self.library.ingest([{**old, "historical_import": True}], as_of="2026-09-14",
                            raw_ref="raw/historical-snapshot.json")
        current = self.library.search("", as_of="2026-09-14")[0]
        self.assertEqual(current["original_text"], "重新核验后的正文")
        self.assertFalse(current.get("historical_import", False))


if __name__ == "__main__":
    unittest.main()
