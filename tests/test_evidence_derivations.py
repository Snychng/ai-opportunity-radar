"""已付费响应派生集合替代视图；仅离线输入，不删除源日志。"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.evidence.derivations import build_derive_sets, select_active_derivations
from aor.evidence.identity import canonical_sha256, evidence_content
from aor.evidence.claims import build_evidence_packet
from aor.sources.coverage import build_industry_coverage
from aor.storage.evidence_library import EvidenceLibrary, EvidenceLibraryError
from aor.evidence.retrieval import EvidenceReferenceError
from normalize_tikhub_results import PARSER_VERSION, normalize_documents

A, B = "a" * 64, "b" * 64
RUN = "RUN-20260914-1234567890"


def record(identifier="old", *, sha=A, version="1.0.0"):
    return {"id": f"reddit:{identifier}", "source": "reddit", "source_item_id": identifier, "evidence_kind": "post",
            "url": f"https://reddit.com/{identifier}", "original_text": "一条应核验的用户行为材料", "industry_ids": ["gaming"],
            "observed_at": "2026-09-14", "published_at": "2026-09-13", "parser_version": version,
            "raw_file": "/raw/execution.json", "derivation_refs": [{"source_execution_sha256": sha,
                "source_file": "/raw/execution.json", "parser_version": version}]}


def payload(rows=(), *, sha=A, version="2.1.0", status="complete", path="/raw/execution.json"):
    return {"derive_sets": build_derive_sets(list(rows), [{"source_execution_sha256": sha,
             "source_file": path, "parser_version": version, "status": status}])}


class EvidenceDerivationsTests(unittest.TestCase):
    def test_complete_empty_new_parse_supersedes_only_same_execution(self):
        old, unrelated = record(), record("other", sha=B)
        original = deepcopy(old)
        active, superseded = select_active_derivations([old, unrelated], [payload()])
        self.assertEqual([r["id"] for r in active], [unrelated["id"]])
        self.assertEqual([r["id"] for r in superseded], [old["id"]])
        self.assertEqual(superseded[0]["derivation_status"], "superseded")
        self.assertNotIn("retracted", superseded[0])
        self.assertEqual(old, original)

    def test_revised_content_with_same_object_is_not_kept_as_current_old_revision(self):
        old = record()
        revised = record(version="2.1.0")
        revised["original_text"] = "修正后的完整正文"
        active, superseded = select_active_derivations([old, revised], [payload([revised])])
        self.assertEqual([r["original_text"] for r in active], [revised["original_text"]])
        self.assertEqual(len(superseded), 1)

    def test_same_content_survives_parser_metadata_upgrade(self):
        old, new = record(), record(version="2.1.0")
        active, superseded = select_active_derivations([old], [payload([new])])
        self.assertEqual(len(active), 1)
        self.assertFalse(superseded)
        self.assertEqual(canonical_sha256(evidence_content(old)), canonical_sha256(evidence_content(new)))
        first = EvidenceLibrary._event(old, as_of="2026-09-14", run_id=RUN, raw_ref=None)["record"]
        second = EvidenceLibrary._event(new, as_of="2026-09-14", run_id=RUN, raw_ref=None)["record"]
        self.assertEqual(first["revision_id"], second["revision_id"])

    def test_independent_raw_response_preserves_same_content(self):
        old = record()
        old["derivation_refs"].append({"source_execution_sha256": B, "source_file": "/raw/independent.json", "parser_version": "1.0.0"})
        active, superseded = select_active_derivations([old], [payload()])
        self.assertEqual(len(active), 1)
        self.assertFalse(superseded)

    def test_partial_or_unrecognized_parse_does_not_remove_previous_records(self):
        active, superseded = select_active_derivations([record()], [payload(status="partial")])
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")

    def test_older_parser_cannot_overwrite_newer_derivation_set(self):
        active, superseded = select_active_derivations([record(version="3.0.0")], [payload(version="2.0.0")])
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")

    def test_conflicting_same_version_sets_and_reused_path_are_quarantined_for_review(self):
        old, different = record(), record("new", version="2.1.0")
        active, superseded = select_active_derivations([old], [payload(), payload([different])])
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")
        legacy = record()
        legacy.pop("derivation_refs")
        active, superseded = select_active_derivations([legacy], [payload(), payload(sha=B)])
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")

    def test_legacy_exact_raw_path_match_and_unknown_provenance(self):
        legacy = record()
        legacy.pop("derivation_refs")
        active, superseded = select_active_derivations([legacy], [payload()])
        self.assertFalse(active)
        self.assertEqual(len(superseded), 1)
        legacy["raw_file"] = "/raw/unrelated.json"
        active, superseded = select_active_derivations([legacy], [payload()])
        self.assertEqual(len(active), 1)
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")
        legacy["raw_refs"] = [{"path": "/raw/execution.json"}]
        active, superseded = select_active_derivations([legacy], [payload()])
        self.assertEqual(len(active), 1)
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")

    def test_relocated_legacy_path_is_not_an_independent_source(self):
        legacy = record()
        legacy.pop("derivation_refs")
        legacy["raw_file"] = "/old-home/raw/execution.json"
        active, superseded = select_active_derivations([legacy], [payload(path="/new-home/raw/execution.json")])
        self.assertEqual(active[0]["derivation_status"], "needs_review")
        self.assertFalse(superseded)
        # 手工网页导入没有解析器主文件，普通导入产物定位不被误判为旧解析。
        web = {"url": "https://example.org", "source": "web", "original_text": "已打开网页原文",
               "raw_refs": [{"path": "/imports/web.json"}]}
        active, _ = select_active_derivations([web], [payload()])
        self.assertNotIn("derivation_status", active[0])

    def test_registry_survives_next_run_rebuild_and_identity_migration(self):
        with tempfile.TemporaryDirectory() as root:
            library = EvidenceLibrary(Path(root) / "library")
            library.ingest([record()], as_of="2026-09-14", run_id=RUN)
            before = library.search("", as_of="2026-09-14")[0]
            original = library.journal_path.read_bytes()
            registered = {**payload(), "as_of": "2026-09-14", "run_id": RUN}
            result = library.register_derivations([registered], known_on="2026-09-15",
                                                  run_id="RUN-20260915-1234567890")
            self.assertEqual(result["events_added"], 1)
            self.assertEqual(len(library.search("", as_of="2026-09-14")), 1)
            self.assertEqual(library.search("", as_of="2026-09-15"), [])
            # 新运行没有 payloads，也无需重新提交解析集合。
            self.assertEqual(EvidenceLibrary(library.root).search("", as_of="2026-09-16",
                             run_id="RUN-20260916-1234567890"), [])
            audit = library.search("", as_of="2026-09-16", include_superseded=True)
            self.assertEqual(audit[0]["derivation_status"], "superseded")
            exact = {k: before[k] for k in ("evidence_id", "revision_id")}
            self.assertEqual(library.resolve(exact, as_of="2026-09-16")["original_text"], record()["original_text"])
            with self.assertRaises(EvidenceReferenceError):
                library.resolve({"evidence_id": before["evidence_id"]}, as_of="2026-09-16")
            library.rebuild()
            self.assertEqual(library.search("", as_of="2026-09-16"), [])
            self.assertEqual(library.journal_path.read_bytes(), original)
            target = Path(root) / "migrated"
            result = library.migrate_identities(target)
            self.assertEqual(result["derivation_events"], 1)
            migrated = EvidenceLibrary(target)
            self.assertEqual(migrated.derivation_path.read_bytes(), library.derivation_path.read_bytes())
            self.assertEqual(migrated.search("", as_of="2026-09-16"), [])
            self.assertEqual(migrated.resolve(exact, as_of="2026-09-16")["original_text"], record()["original_text"])

    def test_registry_is_atomic_idempotent_locked_and_rejects_future_or_corrupt_sets(self):
        with tempfile.TemporaryDirectory() as root:
            library = EvidenceLibrary(root)
            p = payload()
            with ThreadPoolExecutor(max_workers=4) as workers:
                counts = list(workers.map(lambda _: library.register_derivations([p, p],
                                          known_on="2026-09-14", run_id=RUN)["events_added"], range(4)))
            self.assertEqual(sum(counts), 1)
            original = library.derivation_path.read_bytes()
            broken = deepcopy(p)
            broken["derive_sets"][0]["members"].append({"object_key": "ghost", "content_hash": B})
            for invalid in (broken, {**p, "as_of": "2026-09-15"}):
                with self.assertRaises(EvidenceLibraryError):
                    library.register_derivations([payload(sha=B), invalid], known_on="2026-09-14")
                self.assertEqual(library.derivation_path.read_bytes(), original)
            library.ingest([record()], as_of="2026-09-14")
            event = json.loads(original)
            event["derive_set"]["status"] = "partial"
            library.derivation_path.write_text(json.dumps(event) + "\n")
            with self.assertRaisesRegex(EvidenceLibraryError, "登记日志"):
                library.search("", as_of="2026-09-14")
            with self.assertRaises(EvidenceLibraryError):
                library.rebuild()

    def test_registered_partial_conflict_and_independent_sha_are_rechecked_across_runs(self):
        with tempfile.TemporaryDirectory() as root:
            library = EvidenceLibrary(root)
            row = record()
            row["derivation_refs"].append({"source_execution_sha256": B, "parser_version": "1.0.0"})
            library.ingest([row], as_of="2026-09-14")
            library.register_derivations([payload()], known_on="2026-09-14")
            self.assertNotIn("derivation_status", library.search("", as_of="2026-09-15")[0])
            library.register_derivations([payload(sha=B, status="partial")], known_on="2026-09-15")
            self.assertEqual(library.search("", as_of="2026-09-16")[0]["derivation_status"], "needs_review")
            library.register_derivations([payload(sha=B, version="3.0.0")], known_on="2026-09-16")
            self.assertEqual(library.search("", as_of="2026-09-17"), [])

    def test_registry_marks_legacy_unknown_path_for_review_without_losing_original(self):
        with tempfile.TemporaryDirectory() as root:
            library = EvidenceLibrary(root)
            row = record()
            row.pop("derivation_refs")
            row["raw_file"] = "/previous-home/execution.json"
            library.ingest([row], as_of="2026-09-14")
            original = library.journal_path.read_bytes()
            library.register_derivations([payload(path="/current-home/execution.json")], known_on="2026-09-14")
            current = library.search("", as_of="2026-09-15")[0]
            self.assertEqual(current["derivation_status"], "needs_review")
            self.assertEqual(current["original_text"], row["original_text"])
            self.assertEqual(library.journal_path.read_bytes(), original)

    def test_untraceable_legacy_parser_record_is_kept_for_review(self):
        unknown = record()
        unknown.pop("derivation_refs")
        unknown.pop("raw_file")
        active, superseded = select_active_derivations([unknown], [payload()])
        self.assertFalse(superseded)
        self.assertEqual(active[0]["derivation_status"], "needs_review")

    def test_superseded_material_is_omitted_from_packet_and_current_coverage(self):
        _, superseded = select_active_derivations([record()], [payload()])
        packet = build_evidence_packet(superseded, as_of="2026-09-14", run_id=RUN)
        self.assertFalse(packet["evidence"])
        self.assertEqual(packet["omitted"]["evidence_superseded"], 1)
        plan = {"run_id": RUN, "as_of": "2026-09-14", "selected_industries": ["gaming"],
                "industry_catalog": [{"id": "gaming", "name": "游戏", "audience": "玩家", "demand_model": "experience"}]}
        coverage = build_industry_coverage(plan, [{"run_id": RUN, "evidence": superseded}])
        self.assertEqual(coverage["industries"][0]["material_count"], 0)

    def test_normalizer_emits_empty_derive_set_and_binds_multi_document_duplicates(self):
        document = {"schema_version": "3.0", "provider": "tikhub", "run_id": RUN, "as_of": "2026-09-14",
                    "generated_at": "2026-09-14T00:00:00Z", "stage": "search_discovery", "results": []}
        empty = normalize_documents([document], source_files=["/raw/empty.json"])
        self.assertEqual(empty["derive_sets"][0]["members"], [])
        self.assertEqual(empty["derive_sets"][0]["status"], "complete")
        result = {"id": "reddit-query", "source": "reddit", "status": "ok", "params": {"query": "gaming teammates"},
                  "response": {"data": {"posts": [{"id": "same", "title": "gaming teammates", "selftext": "gaming teammates scheduling",
                                "url": "https://reddit.com/same", "created_utc": 1789257600}]}}}
        post = result["response"]["data"]["posts"][0]
        result["response"]["data"] = {"search": {"dynamic": {"components": {"main": {"edges": [{"node": post}]}}}}}
        one = {**document, "results": [result]}
        two = {**one, "generated_at": "2026-09-14T00:01:00Z"}
        merged = normalize_documents([one, two], source_files=["/raw/a.json", "/raw/b.json"])
        self.assertEqual(merged["parser_version"], PARSER_VERSION)
        self.assertEqual(len(merged["evidence"]), 1)
        self.assertEqual(len(merged["evidence"][0]["derivation_refs"]), 2)
        self.assertTrue(all(len(s["members"]) == 1 for s in merged["derive_sets"]))
        self.assertTrue(all(ref["derive_set_id"] for ref in merged["evidence"][0]["derivation_refs"]))


if __name__ == "__main__":
    unittest.main()
