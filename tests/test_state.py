from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from manage_state import (  # noqa: E402
    StateError,
    _parse_date,
    _read_json_object,
    _read_records,
    find_record,
    fingerprint_record,
    history,
    initialize_home,
    load_observations,
    load_records,
    promote_signal,
    resolve_record_ids,
    update_source_health,
    upsert_record,
    upsert_records,
)
import manage_state  # noqa: E402


def sample_record(title: str = "AI 主动匹配工具") -> dict:
    return {
        "title": title,
        "target_user": "寻找开发者的小微企业",
        "context": "需求尚未结构化时",
        "problem_or_desire": "难以找到合适的 AI 开发者",
        "wedge": "先生成需求范围再进行匹配",
        "total_score": 78,
        "evidence": [{"url": "https://example.com/1"}],
    }


def qualified_record() -> dict:
    value = sample_record("已验证机会")
    value.update({
        "benchmark_ids": ["BENCH-A1B2C3D4"], "payer": "小微企业", "current_alternative": "人工",
        "product_gap": "需求结构化", "acquisition_channel": "企业社区", "mvp_days": 21,
        "mvp_scope": "完成一次需求匹配", "source_region": "美国", "target_region": "美国",
        "payment_signals": [{"type": "purchase", "region": "美国", "payer": "小微企业",
                             "url": "https://vendor.example/receipt", "fact": "用户已支付49美元购买服务"}],
        "evidence": [{"source": "vendor", "url": "https://vendor.example/receipt", "fact": "用户已支付49美元购买服务"},
                     {"source": "community", "url": "https://community.example/paid-user", "fact": "另一位用户报告购买后的交付体验"}],
    })
    return value


class StateTests(unittest.TestCase):
    def test_canonical_url_corrections_replace_original_evidence_despite_new_ids(self) -> None:
        variants = ({}, {"id": "changed-source-id"}, {"evidence_id": "changed-evidence-id"})
        for extra in variants:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                home = initialize_home(Path(tmp))
                original = qualified_record()
                created = upsert_record(home, "opportunity", original, date(2026, 7, 14))
                original_evidence_id = find_record(home, "opportunity", created["id"])["evidence"][0]["evidence_id"]
                corrected = deepcopy(original)
                corrected["evidence"] = [{"url": "https://www.vendor.example/receipt/?utm_source=correction#top",
                                           "fact": "重新核实后并未付款", **extra}]
                upsert_record(home, "opportunity", corrected, date(2026, 7, 15))
                current = find_record(home, "opportunity", created["id"])
                self.assertEqual(len(current["evidence"]), 2)
                self.assertEqual(current["payment_signals"], [])
                revision = next(item for item in current["evidence"] if item["evidence_id"] == original_evidence_id)
                self.assertEqual(revision["version"], 2)
                self.assertEqual(revision["fact"], "重新核实后并未付款")

    def test_evidence_identity_preserves_content_query_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = sample_record()
            original["evidence"] = [{"url": "https://example.com/receipt?id=1", "fact": "第一笔付款"}]
            created = upsert_record(home, "opportunity", original, date(2026, 7, 14))
            other = sample_record()
            other["evidence"] = [{"url": "https://www.example.com/receipt/?id=2&utm_source=test", "fact": "第二笔付款"}]
            upsert_record(home, "opportunity", other, date(2026, 7, 15))
            self.assertEqual(len(find_record(home, "opportunity", created["id"])["evidence"]), 2)

    def test_evidence_alias_does_not_create_revision_without_fact_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = qualified_record()
            created = upsert_record(home, "opportunity", original, date(2026, 7, 14))
            repeated = deepcopy(original)
            repeated["evidence"][0]["url"] = "https://www.vendor.example/receipt/?utm_source=new#top"
            upsert_record(home, "opportunity", repeated, date(2026, 7, 15))
            current = find_record(home, "opportunity", created["id"])
            self.assertEqual(current["evidence"][0]["version"], 1)
            self.assertEqual(len(current["evidence_history"]), 2)

    def test_conflicting_evidence_url_and_existing_id_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = qualified_record()
            created = upsert_record(home, "opportunity", original, date(2026, 7, 14))
            current = find_record(home, "opportunity", created["id"])
            conflict = sample_record()
            conflict["evidence"] = [{"url": current["evidence"][0]["url"],
                                      "evidence_id": current["evidence"][1]["evidence_id"], "fact": "身份混合"}]
            with self.assertRaisesRegex(StateError, "证据身份冲突"):
                upsert_record(home, "opportunity", conflict, date(2026, 7, 15))

    def test_state_rejects_conflicting_declared_envelopes(self) -> None:
        cases = ({"schema_version": "2.0"}, {"run_id": "RUN-20260713-ABCDEF1234", "as_of": "2026-07-13"},
                 {"run_id": "RUN-20260714-0000000000", "as_of": "2026-07-14"}, {"as_of": "2026-07-14"})
        for fields in cases:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                value = sample_record()
                value.update(fields)
                with self.assertRaises(StateError):
                    upsert_records(initialize_home(Path(tmp)), "opportunity", [value], date(2026, 7, 14),
                                   run_id="RUN-20260714-ABCDEF1234")

    def test_cli_record_wrapper_keeps_envelope_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(json.dumps({"schema_version": "3.0", "run_id": "RUN-20260713-ABCDEF1234",
                                        "as_of": "2026-07-13", "records": [sample_record()]}))
            with self.assertRaises(StateError):
                _read_records(path, observed_on=date(2026, 7, 14), run_id="RUN-20260714-ABCDEF1234")

    def test_changed_payment_evidence_invalidates_stale_signals_and_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = qualified_record()
            original["evidence"][0]["fact"] = original["payment_signals"][0]["fact"]
            original["evidence_tier"] = "A"
            upsert_record(home, "opportunity", original, date(2026, 7, 14))
            corrected = deepcopy(original)
            corrected["evidence"] = [{"url": original["evidence"][0]["url"], "fact": "重新核实后并未付款"}]
            with self.assertRaisesRegex(StateError, "重新核验"):
                upsert_record(home, "opportunity", corrected, date(2026, 7, 15))

    def test_corrected_source_requires_current_revision_and_fact_for_signal_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = qualified_record()
            original["evidence"][0]["fact"] = original["payment_signals"][0]["fact"]
            created = upsert_record(home, "opportunity", original, date(2026, 7, 14))
            current = find_record(home, "opportunity", created["id"])
            corrected = deepcopy(original)
            corrected["evidence"] = [{"url": original["evidence"][0]["url"], "fact": "重新核实后并未付款"}]
            upsert_record(home, "opportunity", corrected, date(2026, 7, 15))
            self.assertEqual(find_record(home, "opportunity", created["id"])["payment_signals"], [])
            upsert_record(home, "opportunity", corrected, date(2026, 7, 16))
            self.assertEqual(find_record(home, "opportunity", created["id"])["payment_signals"], [])
            renewed = deepcopy(original)
            renewed["evidence"] = [{"url": original["evidence"][0]["url"], "fact": "用户此次已支付99美元购买服务"}]
            renewed["payment_signals"][0].update({
                "fact": "用户此次已支付99美元购买服务",
                "evidence_revision_id": current["evidence"][0]["evidence_id"] + ":v3",
            })
            renewed["evidence_tier"] = "A"
            upsert_record(home, "opportunity", renewed, date(2026, 7, 17))
            self.assertEqual(len(find_record(home, "opportunity", created["id"])["payment_signals"]), 1)

    def test_revision_binding_does_not_rescue_old_facts_or_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = qualified_record()
            original["evidence"][0]["fact"] = original["payment_signals"][0]["fact"]
            original["candidate_verifications"] = {"product_gap": {"value": original["product_gap"],
                                                                   "evidence": deepcopy(original["payment_signals"])}}
            created = upsert_record(home, "opportunity", original, date(2026, 7, 14))
            current = find_record(home, "opportunity", created["id"])
            corrected = deepcopy(original)
            corrected["evidence"] = [{"url": original["evidence"][0]["url"], "fact": "核实后未付款"}]
            corrected["payment_signals"][0].update({
                "evidence_revision_id": current["evidence"][0]["evidence_id"] + ":v2",
                "quote": "核实后未付款",
            })
            upsert_record(home, "opportunity", corrected, date(2026, 7, 15))
            current = find_record(home, "opportunity", created["id"])
            self.assertEqual(current["payment_signals"], [])
            self.assertEqual(current["candidate_verifications"]["product_gap"]["evidence"], [])

    def test_promotion_after_same_run_upsert_keeps_bidirectional_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            signal_id = upsert_record(home, "signal", sample_record(), date(2026, 7, 14))["id"]
            run_id = "RUN-20260715-ABCDEF1234"
            created = upsert_record(home, "opportunity", qualified_record(), date(2026, 7, 15), run_id=run_id)
            promoted = qualified_record()
            promoted["title"] = "升级后的标题"
            result = promote_signal(home, signal_id, promoted, date(2026, 7, 15), run_id=run_id)
            current = find_record(home, "opportunity", created["id"])
            self.assertEqual(current["promoted_from"], signal_id)
            self.assertEqual(current["title"], "升级后的标题")
            self.assertEqual(find_record(home, "signal", signal_id)["promoted_to"], result["opportunity_id"])
            self.assertEqual(promote_signal(home, signal_id, promoted, date(2026, 7, 15), run_id=run_id)["status"], "replayed")

    def test_promotion_rejects_existing_opportunity_from_a_later_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            signal_id = upsert_record(home, "signal", sample_record(), date(2026, 7, 14))["id"]
            upsert_record(home, "opportunity", qualified_record(), date(2026, 7, 20))
            with self.assertRaisesRegex(StateError, "较新.*机会"):
                promote_signal(home, signal_id, qualified_record(), date(2026, 7, 15), run_id="RUN-20260715-ABCDEF1234")

    def test_rejects_backdated_write_that_would_contaminate_past_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            upsert_record(home, "opportunity", sample_record(), date(2026, 7, 20))
            with self.assertRaisesRegex(StateError, "倒填历史"):
                upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            self.assertEqual(history(home, "opportunity", date(2026, 7, 14), 1), [])

    def test_promotion_recovers_at_every_partial_write(self) -> None:
        for filename in ("opportunities.jsonl", "opportunity-observations.jsonl",
                         "signals.jsonl", "signal-observations.jsonl"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                home = initialize_home(Path(tmp))
                signal_id = upsert_record(home, "signal", sample_record(), date(2026, 7, 14))["id"]
                original = manage_state._atomic_write_text

                def interrupted(path, content):
                    if path.name == filename:
                        raise OSError("模拟升级写入中断")
                    return original(path, content)

                with patch.object(manage_state, "_atomic_write_text", side_effect=interrupted):
                    with self.assertRaises(OSError):
                        promote_signal(home, signal_id, qualified_record(), date(2026, 7, 15),
                                       run_id="RUN-20260715-ABCDEF1234")
                result = promote_signal(home, signal_id, qualified_record(), date(2026, 7, 15),
                                        run_id="RUN-20260715-ABCDEF1234")
                self.assertEqual(result["status"], "replayed")
                self.assertEqual(find_record(home, "signal", signal_id)["promoted_to"], result["opportunity_id"])
                self.assertEqual(find_record(home, "opportunity", result["opportunity_id"])["promoted_from"], signal_id)
                self.assertEqual(len(load_observations(home, "signal")), 2)
                self.assertEqual(len(load_observations(home, "opportunity")), 1)

    def test_source_health_recovers_after_current_view_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            original = manage_state._atomic_write_text

            def interrupted(path, content):
                if path.name == "source-health.json":
                    raise OSError("模拟来源状态写入中断")
                return original(path, content)

            with patch.object(manage_state, "_atomic_write_text", side_effect=interrupted):
                with self.assertRaises(OSError):
                    update_source_health(home, "Example", "ok", "首轮完成", date(2026, 7, 14))
            result = update_source_health(home, "Example", "ok", "首轮完成", date(2026, 7, 14))
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len((home / "state" / "source-health-events.jsonl").read_text().splitlines()), 1)

    def test_legacy_observation_repairs_lagging_current_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            result = upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            old_content = (home / "state" / "opportunities.jsonl").read_text()
            updated = sample_record()
            updated["total_score"] = 55
            upsert_record(home, "opportunity", updated, date(2026, 7, 15))
            (home / "state" / "opportunities.jsonl").write_text(old_content)
            self.assertEqual(find_record(home, "opportunity", result["id"])["total_score"], 55)

    def test_interrupted_upsert_recovers_before_same_run_replay(self) -> None:
        for filename in ("opportunity-observations.jsonl", "opportunities.jsonl"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                home = initialize_home(Path(tmp))
                original = manage_state._atomic_write_text

                def interrupted(path, content):
                    if path.name == filename:
                        raise OSError("模拟写入中断")
                    return original(path, content)

                with patch.object(manage_state, "_atomic_write_text", side_effect=interrupted):
                    with self.assertRaises(OSError):
                        upsert_records(home, "opportunity", [sample_record()], date(2026, 7, 14),
                                       run_id="RUN-20260714-ABCDEF1234")
                replay = upsert_records(home, "opportunity", [sample_record()], date(2026, 7, 14),
                                        run_id="RUN-20260714-ABCDEF1234")
                self.assertEqual(replay[0]["status"], "replayed")
                self.assertEqual(len(load_records(home, "opportunity")), 1)
                self.assertEqual(len(load_observations(home, "opportunity")), 1)
                self.assertFalse((home / "state" / "pending-transaction.json").exists())

    def test_legacy_observation_repairs_missing_current_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            result = upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            (home / "state" / "opportunities.jsonl").write_text("")
            self.assertEqual(find_record(home, "opportunity", result["id"])["title"], sample_record()["title"])

    def test_same_run_changed_input_is_explicit_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            run_id = "RUN-20260714-ABCDEF1234"
            upsert_records(home, "opportunity", [sample_record()], date(2026, 7, 14), run_id=run_id)
            revised = sample_record()
            revised["total_score"] = 20
            with self.assertRaisesRegex(StateError, "同一 run_id.*不同"):
                upsert_records(home, "opportunity", [revised], date(2026, 7, 14), run_id=run_id)

    def test_evidence_corrections_and_retractions_preserve_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            initial = sample_record()
            initial["evidence"] = [{"url": "https://example.com/1", "text": "误判为已购买"}]
            created = upsert_record(home, "opportunity", initial, date(2026, 7, 14))
            corrected = sample_record()
            corrected["evidence"] = [{"url": "https://example.com/1", "text": "仅查看定价页", "correction_reason": "重新核验"}]
            upsert_record(home, "opportunity", corrected, date(2026, 7, 15))
            current = find_record(home, "opportunity", created["id"])
            self.assertEqual(current["evidence"][0]["text"], "仅查看定价页")
            self.assertEqual(current["evidence"][0]["version"], 2)
            self.assertEqual(len(current["evidence_history"]), 2)
            self.assertEqual(load_observations(home, "opportunity")[0]["snapshot"]["evidence"][0]["text"], "误判为已购买")
            retracted = sample_record()
            retracted["evidence"] = [{"url": "https://example.com/1", "retracted": True, "correction_reason": "来源已撤回"}]
            upsert_record(home, "opportunity", retracted, date(2026, 7, 16))
            current = find_record(home, "opportunity", created["id"])
            self.assertEqual(current["evidence"], [])
            self.assertEqual(len(current["evidence_history"]), 3)
            self.assertTrue(current["evidence_history"][-1]["retracted"])

    def test_history_as_of_returns_past_snapshot_without_future_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            future = sample_record()
            future.update({"total_score": 95, "future_only": "未来信息"})
            upsert_record(home, "opportunity", future, date(2026, 7, 20))
            past = history(home, "opportunity", date(2026, 7, 14), 1)
            self.assertEqual(len(past), 1)
            self.assertEqual(past[0]["total_score"], 78)
            self.assertNotIn("future_only", past[0])
            self.assertEqual(len(past[0]["observation_history"]), 1)

    def test_journal_rejects_untrusted_paths_and_payloads(self) -> None:
        for files in ({"../outside.json": []}, {"opportunities.jsonl": "not-records"},
                      {"opportunities.jsonl": ["not-an-object"]}):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as tmp:
                home = initialize_home(Path(tmp))
                (home / "state" / "pending-transaction.json").write_text(json.dumps({"files": files}))
                with self.assertRaises(StateError):
                    load_records(home, "opportunity")

    def test_rejects_regional_tier_in_opportunity_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            value = sample_record()
            value["evidence_tier"] = "R"
            with self.assertRaisesRegex(StateError, "证据层级"):
                upsert_record(initialize_home(Path(tmp)), "opportunity", value, date(2026, 7, 14))

    def test_initialize_creates_expected_layout_and_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            self.assertTrue((home / "reports" / "daily").is_dir())
            self.assertTrue((home / "reports" / "deep-dives").is_dir())
            self.assertTrue((home / "raw").is_dir())
            preferences = json.loads((home / "config" / "preferences.json").read_text())
            self.assertEqual(preferences["deep_opportunities_per_day"], [3, 5])
            self.assertIn("adult_content", preferences["allowed_sensitive_domains"])
            self.assertEqual(preferences["platform_phase"], "phase_1_existing_platforms")
            self.assertFalse(preferences["platform_expansion_enabled"])

    def test_upsert_is_idempotent_and_tracks_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            created = upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            updated = upsert_record(home, "opportunity", sample_record(), date(2026, 7, 15))
            records = load_records(home, "opportunity")

        self.assertEqual(created["status"], "created")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(created["id"], updated["id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["occurrences"], 2)
        self.assertEqual(records[0]["first_seen"], "2026-07-14")
        self.assertEqual(records[0]["last_seen"], "2026-07-15")

    def test_distinct_wedge_creates_distinct_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            other = sample_record()
            other["wedge"] = "先提供开发者能力评测"
            upsert_record(home, "opportunity", other, date(2026, 7, 14))
            records = load_records(home, "opportunity")

        self.assertEqual(len(records), 2)

    def test_title_translation_does_not_change_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            first = upsert_record(home, "opportunity", sample_record("中文标题"), date(2026, 7, 14))
            second = upsert_record(home, "opportunity", sample_record("English title"), date(2026, 7, 15))

        self.assertEqual(first["id"], second["id"])

    def test_market_scope_prevents_cross_country_or_channel_collision(self) -> None:
        generic = sample_record()
        indonesia = sample_record()
        indonesia["market_scope"] = {"country": "印度尼西亚", "primary_channel": "WhatsApp"}
        vietnam = sample_record()
        vietnam["market_scope"] = {"country": "越南", "primary_channel": "Zalo"}

        self.assertNotEqual(fingerprint_record(generic), fingerprint_record(indonesia))
        self.assertNotEqual(fingerprint_record(indonesia), fingerprint_record(vietnam))

    def test_find_record_returns_deep_dive_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            created = upsert_record(home, "opportunity", sample_record(), date(2026, 7, 14))
            found = find_record(home, "opportunity", created["id"])

        self.assertEqual(found["id"], created["id"])
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            with self.assertRaises(StateError):
                find_record(home, "opportunity", "OPP-20260714-FFFFFF")

    def test_prepare_assigns_id_and_same_day_replays_do_not_inflate_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            prepared = resolve_record_ids(home, "opportunity", [sample_record()], date(2026, 7, 14))
            run_id = "RUN-20260714-ABCDEF1234"
            first = upsert_records(home, "opportunity", prepared, date(2026, 7, 14), run_id=run_id)
            replay = upsert_records(home, "opportunity", prepared, date(2026, 7, 14), run_id=run_id)
            records = load_records(home, "opportunity")
            observations = load_observations(home, "opportunity")

        self.assertRegex(prepared[0]["id"], r"^OPP-20260714-[A-F0-9]{6}$")
        self.assertEqual(first[0]["status"], "created")
        self.assertEqual(replay[0]["status"], "replayed")
        self.assertEqual(records[0]["occurrences"], 1)
        self.assertEqual(len(observations), 1)

    def test_score_history_is_append_only_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            first = sample_record()
            first["total_score"] = 70
            upsert_records(
                home, "opportunity", [first], date(2026, 7, 14),
                run_id="RUN-20260714-ABCDEF1234",
            )
            second = sample_record()
            second["total_score"] = 82
            upsert_records(
                home, "opportunity", [second], date(2026, 7, 15),
                run_id="RUN-20260715-ABCDEF1234",
            )
            recent = history(home, "opportunity", date(2026, 7, 15), 30)

        self.assertEqual([item["total_score"] for item in recent[0]["observation_history"]], [70, 82])
        self.assertEqual(recent[0]["total_score"], 82)

    def test_corrupted_jsonl_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            (home / "state" / "opportunities.jsonl").write_text("{broken\n")
            with self.assertRaises(StateError):
                load_records(home, "opportunity")

    def test_rejects_invalid_state_inputs(self) -> None:
        with self.assertRaises(StateError):
            fingerprint_record({"target_user": "用户"})
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            with self.assertRaises(StateError):
                load_records(home, "unknown")
            with self.assertRaises(StateError):
                upsert_record(home, "opportunity", [], date(2026, 7, 14))  # type: ignore[arg-type]
            with self.assertRaises(StateError):
                history(home, "opportunity", date(2026, 7, 14), 0)
            with self.assertRaises(StateError):
                update_source_health(home, "Example", "unknown", "", date(2026, 7, 14))
            with self.assertRaisesRegex(StateError, "日期与 as_of 不一致"):
                upsert_records(
                    home,
                    "opportunity",
                    [sample_record()],
                    date(2026, 7, 15),
                    run_id="RUN-20260714-ABCDEF1234",
                )

            non_object = home / "non-object.json"
            non_object.write_text("[]")
            with self.assertRaises(StateError):
                _read_json_object(non_object)
            broken = home / "broken.json"
            broken.write_text("{broken")
            with self.assertRaises(StateError):
                _read_json_object(broken)
        with self.assertRaises(StateError):
            _parse_date("bad-date")

    def test_invalid_history_date_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            path = home / "state" / "opportunities.jsonl"
            path.write_text(json.dumps({"id": "OPP-X", "last_seen": "invalid"}) + "\n")
            with self.assertRaises(StateError):
                history(home, "opportunity", date(2026, 7, 14), 30)

    def test_source_health_redacts_secret_like_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            result = update_source_health(
                home,
                "Example",
                "auth-required",
                "Authorization: Bearer super-secret-token api_key=abc123 token=xyz789",
                date(2026, 7, 14),
            )
            stored = (home / "state" / "source-health.json").read_text()

        self.assertNotIn("super-secret-token", stored)
        self.assertNotIn("abc123", stored)
        self.assertNotIn("xyz789", stored)
        self.assertIn("[REDACTED]", result["detail"])

    def test_source_health_replay_does_not_overwrite_append_only_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            run_id = "RUN-20260714-ABCDEF1234"
            update_source_health(home, "GitHub", "ok", "first", date(2026, 7, 14), run_id=run_id)
            replay = update_source_health(home, "GitHub", "error", "second", date(2026, 7, 14), run_id=run_id)
            events = (home / "state" / "source-health-events.jsonl").read_text().splitlines()

        self.assertEqual(len(events), 1)
        self.assertEqual(replay["status"], "ok")
        self.assertEqual(replay["detail"], "first")

    def test_history_filters_by_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            old = sample_record("旧机会")
            old["problem_or_desire"] = "旧的低频问题"
            new = sample_record("新机会")
            new["problem_or_desire"] = "新的高频问题"
            upsert_record(home, "opportunity", old, date(2026, 5, 1))
            upsert_record(home, "opportunity", new, date(2026, 7, 10))
            recent = history(home, "opportunity", date(2026, 7, 14), 30)

        self.assertEqual([item["title"] for item in recent], ["新机会"])

    def test_promotes_signal_to_opportunity_with_bidirectional_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            signal = sample_record("印尼语客服迁移假设")
            signal["market_scope"] = {"country": "印度尼西亚", "primary_channel": "WhatsApp"}
            signal_id = upsert_record(home, "signal", signal, date(2026, 7, 14))["id"]
            opportunity = sample_record("印尼语客服已验证机会")
            opportunity["market_scope"] = signal["market_scope"]
            opportunity.update({
                "benchmark_ids": ["BENCH-A1B2C3D4"],
                "payer": "印尼独立站商家",
                "current_alternative": "人工客服",
                "product_gap": "缺少印尼语和 WhatsApp 集成",
                "acquisition_channel": "印尼 Shopify 商家社区",
                "mvp_days": 21,
                "mvp_scope": "WhatsApp 内自动回复一个 FAQ",
                "source_region": "美国",
                "target_region": "印度尼西亚",
                "payment_signals": [{"type": "paid_subscription", "region": "印度尼西亚", "local": True,
                                     "payer": "印尼独立站商家", "url": "https://community.example/paid-user",
                                     "fact": "印尼商家已付费订阅客服服务"}],
                "demand_signals": [{"type": "complaint"}],
                "evidence": [
                    {"source": "vendor", "url": "https://vendor.example/pricing", "fact": "产品公开提供付费客服套餐"},
                    {"source": "community", "url": "https://community.example/paid-user", "fact": "印尼商家已付费订阅客服服务"},
                ],
            })
            result = promote_signal(
                home,
                signal_id,
                opportunity,
                date(2026, 7, 15),
                run_id="RUN-20260715-ABCDEF1234",
            )
            promoted = find_record(home, "opportunity", result["opportunity_id"])
            source = find_record(home, "signal", signal_id)
            replay = promote_signal(
                home,
                signal_id,
                opportunity,
                date(2026, 7, 15),
                run_id="RUN-20260715-ABCDEF1234",
            )

        self.assertEqual(promoted["promoted_from"], signal_id)
        self.assertEqual(source["promoted_to"], result["opportunity_id"])
        self.assertEqual(source["promotion_status"], "promoted")
        self.assertEqual(source["occurrences"], 2)
        self.assertEqual(replay["status"], "replayed")

    def test_rejects_promotion_to_an_unrelated_business_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = initialize_home(Path(tmp))
            signal = sample_record("区域假设")
            signal["market_scope"] = {"country": "印度尼西亚", "primary_channel": "WhatsApp"}
            signal_id = upsert_record(home, "signal", signal, date(2026, 7, 14))["id"]
            unrelated = sample_record("不相关机会")
            unrelated.update({
                "wedge": "完全不同的产品切入口",
                "market_scope": signal["market_scope"],
                "benchmark_ids": ["BENCH-A1B2C3D4"],
                "payer": "印尼商家",
                "current_alternative": "人工",
                "product_gap": "本地语言",
                "acquisition_channel": "商家社区",
                "mvp_days": 20,
                "mvp_scope": "单任务闭环",
                "source_region": "美国",
                "target_region": "印度尼西亚",
                "payment_signals": [{"type": "payment", "region": "印度尼西亚", "local": True,
                                     "payer": "印尼商家", "url": "https://one.example/receipt",
                                     "fact": "印尼商家已支付客服服务费用"}],
                "evidence": [
                    {"source": "one", "url": "https://one.example/receipt", "fact": "印尼商家已支付客服服务费用"},
                    {"source": "two", "url": "https://two.example", "fact": "独立社区记录同类客服采购需求"},
                ],
            })

            with self.assertRaisesRegex(StateError, "相同业务身份"):
                promote_signal(
                    home,
                    signal_id,
                    unrelated,
                    date(2026, 7, 15),
                    run_id="RUN-20260715-ABCDEF1234",
                )


if __name__ == "__main__":
    unittest.main()
