from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from manage_state import (  # noqa: E402
    StateError,
    _parse_date,
    _read_json_object,
    find_record,
    fingerprint_record,
    history,
    initialize_home,
    load_observations,
    load_records,
    resolve_record_ids,
    update_source_health,
    upsert_record,
    upsert_records,
)


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


class StateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
