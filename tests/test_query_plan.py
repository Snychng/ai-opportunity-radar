from __future__ import annotations

import sys
import json
import tempfile
import subprocess
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_query_plan import build_plan, parse_date, resolve_focus  # noqa: E402


class QueryPlanTests(unittest.TestCase):
    def test_host_intents_survive_plan_deduplication_and_executors(self):
        from aor.sources.planning import compile_intents
        from community_query import execute_plan
        from tikhub_query import estimate_plan
        from tests.test_tikhub_query import pricing_rows

        intents = [{"id": f"{source}-{kind}", "question": "确认发票工作流的付款与痛点", "evidence_type": kind,
                    "search_query": "invoice workflow", "ranking_query": f"保留 {kind} 原文", "source": source,
                    "locale": {"country": "US", "language": "en"}, "candidate_gaps": []}
                   for source in ("hackernews", "tiktok") for kind in ("payment", "workflow_pain")]
        plans = compile_intents({"intents": intents}, as_of="2026-09-10", run_id="RUN-20260910-ABCDEF1234")

        def transport(**kwargs):
            if '/api/v1/items/' in kwargs['endpoint']:
                return {'children': [{'id': 2, 'type': 'comment', 'text': 'Paid for invoice workflow', 'author': 'synthetic-buyer',
                                      'created_at': '2026-09-10'}]}
            return {'hits': [{'objectID': '1', 'title': 'Invoice workflow', 'num_comments': 1, 'created_at': '2026-09-10'}]}

        result = execute_plan(plans["community"], transport=transport, include_comments=True)
        expected = {'hackernews-payment', 'hackernews-workflow_pain'}
        self.assertEqual(set(result['evidence'][0]['intent_refs']), expected)
        self.assertEqual(set(result['comments'][0]['intent_refs']), expected)
        self.assertTrue(expected <= set(result['comments'][0]['request_ids']))
        estimate = estimate_plan(plans['tikhub'], pricing_rows())
        self.assertEqual((estimate['request_count'], estimate['logical_request_count']), (1, 2))
        self.assertEqual(estimate['requests'][0]['request_fingerprint'], plans['tikhub']['requests'][0]['request_fingerprint'])
        self.assertEqual(set(estimate['requests'][0]['request_ids']), {'tiktok-payment-tiktok-1', 'tiktok-workflow_pain-tiktok-1'})
        self.assertEqual(set(estimate['requests'][0]['intent_refs']), {'tiktok-payment', 'tiktok-workflow_pain'})

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
        self.assertIn("evidence_gap_plan", plan["stage_contract"])
        self.assertIn("tikhub_gap_results", plan["stage_contract"])
        self.assertIn("tiered_candidates", plan["stage_contract"])
        self.assertIn("full_result_digest", plan["stage_contract"])
        self.assertTrue(plan["output_contract"]["display_full_qualified_ledger"])
        self.assertEqual(plan["output_contract"]["near_miss_display_max"], 20)
        self.assertEqual(plan["paid_retrieval_policy"]["strategy"], "free_discovery_then_paid_gap_verification")
        self.assertEqual(
            plan["paid_retrieval_policy"]["stop_after_paid_requests_without_new_benchmark_or_qualified_idea"],
            3,
        )

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

    def test_long_focus_produces_bounded_queries_and_unknown_geography(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 9, 10), Path(tmp), focus="AI customer service for small online merchants " * 8)
        self.assertEqual(plan["focus_region"]["id"], "unknown")
        self.assertEqual(plan["languages"], ["unknown"])
        for item in plan["retrieval_plans"]["tikhub"]["requests"]:
            query = next(v for k, v in item["params"].items() if k in {"keyword", "query", "searchTerms", "search_query"})
            self.assertLessEqual(len(query), 100)
            self.assertIn("customer", query)

    def test_structured_scope_overrides_rotation_and_controls_existing_parameters(self) -> None:
        scope = {"countries": ["JP"], "languages": ["ja"], "industry": "電商", "payer": "店主", "task": "注文対応", "queries": [{"language": "ja", "query": "注文対応 高い 手作業"}]}
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 9, 10), Path(tmp), scope=scope)
        self.assertEqual(plan["focus_region"]["markets"], ["JP"])
        self.assertEqual(plan["languages"], ["ja"])
        self.assertEqual(plan["core_regions"], ["JP"])
        for item in plan["retrieval_plans"]["tikhub"]["requests"]:
            if item["source"] == "youtube":
                self.assertEqual(item["params"]["country_code"], "jp")
                self.assertEqual(item["params"]["language_code"], "ja")
        self.assertIn("注文対応", json.dumps(plan["retrieval_plans"]["community"], ensure_ascii=False))
        with self.assertRaises(ValueError):
            build_plan(date(2026, 9, 10), Path(tmp), scope={"countries": ["not-a-country"], "languages": ["ja"]})

    def test_scope_file_cli_and_run_identity_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scope = {"countries": ["JP"], "languages": ["ja"], "task": "注文対応"}
            scope_path = Path(tmp) / "scope.json"
            scope_path.write_text(json.dumps(scope))
            result = subprocess.run([sys.executable, str(SCRIPTS / "build_query_plan.py"), "--date", "2026-09-10", "--home", tmp, "--scope-file", str(scope_path)], text=True, capture_output=True, check=True)
            generated = json.loads(result.stdout)
            direct = build_plan(date(2026, 9, 10), Path(tmp), scope=scope)
            changed = build_plan(date(2026, 9, 10), Path(tmp), scope=dict(scope, countries=["ZA"]))
        self.assertEqual(generated["run_id"], direct["run_id"])
        self.assertNotEqual(direct["run_id"], changed["run_id"])

    def test_scope_query_does_not_multiply_identical_paid_requests(self) -> None:
        from tikhub_query import estimate_plan

        scope = {"countries": ["JP"], "languages": ["ja"], "task": "注文対応",
                 "queries": [{"language": "ja", "query": "注文対応 高い 手作業"}] * 2}
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 9, 10), Path(tmp), scope=scope)
        paid = plan["retrieval_plans"]["tikhub"]
        self.assertEqual(len(paid["requests"]), len(plan["coverage_schedule"]["planned_sources"]))
        self.assertEqual(len({row["request_fingerprint"] for row in paid["requests"]}), len(paid["requests"]))
        for row in paid["requests"]:
            self.assertEqual(row["intent_refs"], ["scope-query-1", "scope-query-2"])
        estimate = estimate_plan(paid, [{"endpoint_uri": row["endpoint"], "endpoint_cost": 0.01,
                                        "platform": row["source"]} for row in paid["requests"]])
        self.assertEqual(estimate["request_count"], len(paid["requests"]))
        community = plan["retrieval_plans"]["community"]
        self.assertEqual(community["requests"], [])
        self.assertEqual(community["plan_status"], "needs_host_queries")

    def test_japanese_merchant_scan_routes_chinese_platforms_only_when_requested(self) -> None:
        from aor.sources.registry import CHINESE_SOURCES

        scope = {"countries": ["JP"], "languages": ["ja"], "task": "注文対応",
                 "queries": [{"language": "ja", "query": "注文対応 高い 手作業"}]}
        with tempfile.TemporaryDirectory() as tmp:
            local = build_plan(date(2026, 9, 10), Path(tmp), scope=scope)
            bilingual = build_plan(date(2026, 9, 10), Path(tmp), scope={**scope, "languages": ["ja", "zh-CN"],
                                   "queries": [*scope["queries"], {"language": "zh-CN", "query": "日本商家 客服 人工成本"}]})
            explicit = build_plan(date(2026, 9, 10), Path(tmp), intent_plan={"intents": [{
                "id": "japan-review", "question": "日本商家有哪些订单处理困难？", "evidence_type": "workflow_pain",
                "search_query": "注文対応 高い 手作業", "ranking_query": "真实人工成本", "source": "xiaohongshu",
                "locale": {"country": "JP", "language": "ja"}, "candidate_gaps": [],
            }]})
        local_requests = local["retrieval_plans"]["tikhub"]["requests"]
        self.assertTrue(local_requests)
        self.assertFalse(CHINESE_SOURCES & {row["source"] for row in local_requests})
        self.assertFalse(CHINESE_SOURCES & set(local["coverage_schedule"]["planned_sources"]))
        self.assertEqual(local["retrieval_plans"]["community"]["plan_status"], "needs_host_queries")
        chinese_requests = [row for row in bilingual["retrieval_plans"]["tikhub"]["requests"] if row["source"] in CHINESE_SOURCES]
        self.assertTrue(chinese_requests)
        self.assertTrue(all(row["query_scope"]["language"] == "zh-CN" for row in chinese_requests))
        self.assertEqual(explicit["retrieval_plans"]["tikhub"]["requests"][0]["source"], "xiaohongshu")

    def test_host_intents_preserve_questions_and_merge_only_actual_requests(self) -> None:
        from community_query import validate_plan
        from aor.sources.planning import deduplicate_requests

        base = {"id": "pain", "question": "客服系统的替代成本是什么？", "evidence_type": "workflow_pain",
                "search_query": "customer support manual workflow", "ranking_query": "优先有实际支出和明确缺口的叙述",
                "source": "hackernews", "locale": {"country": "JP", "language": "en"},
                "candidate_gaps": ["CAND-1:missing-spend"]}
        intent_plan = {"intents": [base, dict(base, id="alternative", evidence_type="alternative"),
                                    dict(base, id="pricing", source="web", evidence_type="official_pricing"),
                                    dict(base, id="paid", source="youtube")]}
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 9, 10), Path(tmp), intent_plan=intent_plan)
        community = plan["retrieval_plans"]["community"]
        validate_plan(community)
        self.assertEqual(len(community["requests"]), 1)
        row = community["requests"][0]
        self.assertEqual(row["intent_refs"], ["pain", "alternative"])
        self.assertEqual(row["provenance"][1]["evidence_type"], "alternative")
        self.assertEqual(row["params"]["query"], base["search_query"])
        self.assertEqual(row["ranking_query"], base["ranking_query"])
        self.assertEqual(deduplicate_requests(community["requests"]), community["requests"])
        self.assertEqual(plan["retrieval_plans"]["tikhub"]["requests"][0]["params"]["country_code"], "jp")
        self.assertEqual(plan["retrieval_plans"]["web_import"]["required_imports"][0]["id"], "pricing")

    def test_english_query_must_be_supplied_for_chinese_hn_intent(self) -> None:
        from aor.sources.planning import validate_intent_plan

        intent = {"id": "pain", "question": "实际成本？", "evidence_type": "workflow_pain",
                  "search_query": "客服 manual", "ranking_query": "客服成本", "source": "hackernews",
                  "locale": {"country": "CN", "language": "en"}, "candidate_gaps": []}
        with self.assertRaisesRegex(ValueError, "宿主提供"):
            validate_intent_plan({"intents": [intent]})

    def test_host_web_only_cli_produces_no_automatic_requests(self) -> None:
        intent = {"id": "pricing", "question": "官网套餐多少钱？", "evidence_type": "official_pricing",
                  "search_query": "customer support pricing", "ranking_query": "官方定价和产品版本",
                  "source": "web", "locale": {"country": "JP", "language": "en"}, "candidate_gaps": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.json"
            path.write_text(json.dumps({"intents": [intent]}))
            result = subprocess.run([sys.executable, str(SCRIPTS / "build_query_plan.py"), "--date", "2026-09-10",
                                     "--home", tmp, "--intent-plan-file", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        for provider in ("community", "tikhub"):
            self.assertEqual(plan["retrieval_plans"][provider]["requests"], [])
            self.assertEqual(plan["retrieval_plans"][provider]["plan_status"], "not_requested")
        self.assertEqual(plan["retrieval_plans"]["web_import"]["required_imports"][0]["source"], "web")

    def test_scope_english_query_routes_hn_github_without_chinese_search(self) -> None:
        scope = {"countries": ["JP"], "languages": ["ja", "en"], "task": "注文対応",
                 "queries": [{"language": "ja", "query": "注文対応 手作業"},
                             {"language": "en", "query": "order support manual workflow"}]}
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_plan(date(2026, 9, 10), Path(tmp), scope=scope)
        requests = plan["retrieval_plans"]["community"]["requests"]
        self.assertEqual(len(requests), 2)
        for item in requests:
            query = item["params"].get("query", item["params"].get("q"))
            self.assertIn("order support", query)
            self.assertNotIn("注文対応", query)

    def test_request_deduplication_keeps_different_methods_and_params(self) -> None:
        from aor.sources.planning import deduplicate_requests

        row = {"id": "one", "source": "reddit", "endpoint": "/search", "method": "GET", "params": {"q": "paid"}}
        requests = [row, dict(row, id="two"), dict(row, id="post", method="POST"),
                    dict(row, id="other", params={"q": "manual"})]
        result = deduplicate_requests(requests)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["request_aliases"], ["one", "two"])
        self.assertNotIn("provenance", row)

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
