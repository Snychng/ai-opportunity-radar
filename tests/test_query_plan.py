from __future__ import annotations

import sys
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_query_plan import build_plan, parse_date, resolve_focus  # noqa: E402


class QueryPlanTests(unittest.TestCase):
    def test_phase_one_is_locked_to_existing_platform_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp))

        expected_tikhub_sources = {
            "tiktok", "instagram", "linkedin", "threads", "twitter", "youtube", "reddit",
            "douyin", "xiaohongshu", "bilibili", "zhihu", "wechat_search",
        }
        self.assertEqual(plan["phase_scope"]["id"], "phase_1_existing_platforms")
        self.assertFalse(plan["phase_scope"]["platform_expansion_enabled"])
        self.assertEqual(set(plan["phase_scope"]["tikhub_available_sources"]), expected_tikhub_sources)
        planned = {item["source"] for item in plan["retrieval_plans"]["tikhub"]["requests"]}
        self.assertEqual(planned, set(plan["coverage_schedule"]["planned_sources"]))
        self.assertLess(len(planned), len(expected_tikhub_sources))
        three_day_union: set[str] = set()
        for offset in range(3):
            daily = build_plan(date(2026, 7, 14 + offset), Path(tmp))
            three_day_union.update(daily["coverage_schedule"]["planned_sources"])
        self.assertEqual(three_day_union, expected_tikhub_sources)
        serialized_groups = json.dumps(plan["coverage_schedule"], ensure_ascii=False)
        for deferred in ("Product Hunt", "Indie Hackers", "App Store", "即刻", "脉脉", "Telegram", "微博", "快手"):
            self.assertNotIn(deferred, serialized_groups)

    def test_builds_all_time_windows_and_core_regions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp))

        self.assertEqual(plan["as_of"], "2026-07-14")
        self.assertEqual(plan["windows"]["7d"]["from"], "2026-07-08")
        self.assertEqual(plan["windows"]["30d"]["from"], "2026-06-15")
        self.assertEqual(plan["windows"]["90d"]["from"], "2026-04-16")
        self.assertEqual(plan["windows"]["365d"]["from"], "2025-07-15")
        self.assertEqual(plan["windows"]["30d"]["lookback_days"], 30)
        self.assertIn("全球英语市场", plan["core_regions"])
        self.assertIn("中国", plan["core_regions"])

    def test_rotates_an_emerging_market_focus_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = build_plan(date(2026, 7, 14), Path(tmp))
            second = build_plan(date(2026, 7, 15), Path(tmp))
            repeated = build_plan(date(2026, 7, 14), Path(tmp))

        self.assertNotEqual(first["focus_region"]["id"], second["focus_region"]["id"])
        self.assertEqual(first["focus_region"], repeated["focus_region"])

    def test_sensitive_scope_and_tracks_are_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp))

        self.assertEqual(plan["allowed_sensitive_domains"], ["恋爱约会与情感陪伴", "成人内容", "游戏虚拟角色与社交娱乐"])
        self.assertEqual({track["id"] for track in plan["opportunity_tracks"]}, {"needle", "new_form", "regional_gap"})
        self.assertEqual(plan["output_contract"]["raw_candidates"], [100, 200])
        self.assertEqual(plan["output_contract"]["validated_quick_ideas"], [20, 40])
        self.assertEqual(plan["output_contract"]["regional_migration_signals"], [30, 80])
        self.assertIn("paid_benchmarks", plan["stage_contract"])
        self.assertIn("tiered_candidates", plan["stage_contract"])

    def test_exports_internal_community_plan_without_skill_chaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp))

        community = plan["retrieval_plans"]["community"]
        self.assertEqual(community["provider"], "community-public")
        self.assertEqual(community["run_id"], plan["run_id"])
        self.assertEqual({item["source"] for item in community["requests"]}, {"hackernews", "github"})
        self.assertEqual(len(community["requests"]), 4)
        self.assertNotIn("last30days", json.dumps(plan, ensure_ascii=False).lower())

    def test_includes_executable_tikhub_search_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp))

        tikhub = plan["retrieval_plans"]["tikhub"]
        self.assertEqual(tikhub["provider"], "tikhub")
        self.assertGreater(len(tikhub["requests"]), 0)
        sources = {request["source"] for request in tikhub["requests"]}
        self.assertEqual(sources, set(plan["coverage_schedule"]["planned_sources"]))
        self.assertEqual(tikhub["scope"]["id"], "phase_1_existing_platforms")
        self.assertEqual(tikhub["cost_policy"]["price_source"], "live_dashboard_before_run")
        self.assertEqual(tikhub["cost_policy"]["max_attempts"], 1)

    def test_custom_focus_overrides_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp), focus="东南亚恋爱应用")

        self.assertEqual(plan["custom_focus"], "东南亚恋爱应用")
        serialized_requests = json.dumps(plan["retrieval_plans"], ensure_ascii=False)
        self.assertIn("东南亚恋爱应用", serialized_requests)

    def test_uses_local_language_query_and_shared_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 7, 14), Path(tmp))

        localized = plan["retrieval_plans"]["tikhub"]["localized_query"]
        self.assertNotEqual(localized["language"], "中文")
        self.assertTrue(localized["query"])
        self.assertEqual(plan["retrieval_plans"]["tikhub"]["run_id"], plan["run_id"])
        self.assertEqual(plan["retrieval_plans"]["community"]["run_id"], plan["run_id"])

    def test_parse_date_rejects_invalid_value(self) -> None:
        with self.assertRaises(ValueError):
            parse_date("2026-02-30")

    def test_loads_preferences_and_rejects_broken_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config").mkdir()
            (home / "config" / "preferences.json").write_text(json.dumps({"timezone": "UTC", "validated_quick_ideas_per_day": [10, 25]}))
            plan = build_plan(date(2026, 7, 14), home)
            self.assertEqual(plan["timezone"], "UTC")
            self.assertEqual(plan["output_contract"]["validated_quick_ideas"], [10, 25])

            (home / "config" / "preferences.json").write_text(json.dumps({"platform_expansion_enabled": True}))
            with self.assertRaisesRegex(ValueError, "一期只允许"):
                build_plan(date(2026, 7, 14), home)

            (home / "config" / "preferences.json").write_text(json.dumps({"platform_phase": "phase_2"}))
            with self.assertRaisesRegex(ValueError, "一期只允许"):
                build_plan(date(2026, 7, 14), home)

            (home / "config" / "preferences.json").write_text("{broken")
            with self.assertRaises(ValueError):
                build_plan(date(2026, 7, 14), home)

            (home / "config" / "preferences.json").write_text("[]")
            with self.assertRaises(ValueError):
                build_plan(date(2026, 7, 14), home)

    def test_reads_untrusted_focus_from_file_as_literal_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            focus_path = Path(tmp) / "focus.txt"
            focus_path.write_text("东南亚 $(touch /tmp/should-not-run) 恋爱应用")
            focus = resolve_focus(None, focus_path)

        self.assertEqual(focus, "东南亚 $(touch /tmp/should-not-run) 恋爱应用")

    def test_focus_input_has_size_and_shape_limits(self) -> None:
        with self.assertRaises(ValueError):
            resolve_focus("直接值", Path("focus.txt"))
        with self.assertRaises(ValueError):
            resolve_focus("包含\x00空字节", None)
        with self.assertRaises(ValueError):
            resolve_focus("x" * 501, None)

        with tempfile.TemporaryDirectory() as tmp:
            oversized = Path(tmp) / "focus.txt"
            oversized.write_text("x" * 5000)
            with self.assertRaises(ValueError):
                resolve_focus(None, oversized)


if __name__ == "__main__":
    unittest.main()
