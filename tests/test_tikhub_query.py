from __future__ import annotations

import json
import io
import math
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tikhub_query as tq  # noqa: E402
from tikhub_query import (  # noqa: E402
    BudgetExceeded,
    PlanError,
    build_comment_plan,
    build_search_plan,
    enforce_budget,
    estimate_plan,
    validate_plan,
)

# 细粒度执行测试使用私有注入入口；生产入口必须自行刷新 TikHub 实时价格。
execute_plan = tq._execute_plan_with_pricing
RUN_ID = "RUN-20260714-ABCDEF1234"


def pricing_rows() -> list[dict[str, object]]:
    return [
        {
            "endpoint_uri": "/api/v1/tikhub/user/get_user_info",
            "endpoint_cost": 0,
            "allow_free_credit": True,
            "allow_discount": False,
            "platform": "tikhub",
        },
        {
            "endpoint_uri": "/api/v1/tiktok/app/v3/fetch_video_search_result",
            "endpoint_cost": 0.001,
            "allow_free_credit": True,
            "allow_discount": True,
            "platform": "tiktok",
        },
        {
            "endpoint_uri": "/api/v1/xiaohongshu/app_v2/search_notes",
            "endpoint_cost": 0.01,
            "allow_free_credit": False,
            "allow_discount": False,
            "platform": "xiaohongshu",
        },
        {
            "endpoint_uri": "/api/v1/wechat_search/v2/fetch_search",
            "endpoint_cost": 0.01,
            "allow_free_credit": False,
            "allow_discount": False,
            "platform": "wechat_search",
        },
    ]


def sample_plan() -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "provider": "tikhub",
        "run_id": RUN_ID,
        "as_of": "2026-07-14",
        "stage": "search_discovery",
        "scope": {"id": "phase_1_existing_platforms", "platform_expansion_enabled": False},
        "requests": [
            {
                "id": "global-tiktok-1",
                "source": "tiktok",
                "endpoint": "/api/v1/tiktok/app/v3/fetch_video_search_result",
                "method": "GET",
                "params": {
                    "keyword": "AI app complaint",
                    "offset": 0,
                    "count": 20,
                    "sort_type": 0,
                    "publish_time": 30,
                    "region": "US",
                },
            },
            {
                "id": "china-xhs-1",
                "source": "xiaohongshu",
                "endpoint": "/api/v1/xiaohongshu/app_v2/search_notes",
                "method": "GET",
                "params": {
                    "keyword": "AI 工具 难用",
                    "page": 1,
                    "sort_type": "general",
                    "note_type": "不限",
                    "time_filter": "不限",
                    "search_id": "",
                    "search_session_id": "",
                    "source": "explore_feed",
                    "ai_mode": 0,
                },
            },
        ],
    }


def account_payload(*, balance: float = 1.0, free_credit: float = 1.0, active: bool = True) -> dict[str, object]:
    return {
        "code": 200,
        "api_key_data": {"api_key_name": "test-key", "api_key_status": 1},
        "user_data": {
            "email": "must-not-be-persisted@example.com",
            "balance": balance,
            "free_credit": free_credit,
            "account_disabled": not active,
            "is_active": active,
        },
    }


def healthy_account_transport(**kwargs: object) -> dict[str, object]:
    return account_payload()


def comment_pricing_rows() -> list[dict[str, object]]:
    rows: list[tuple[str, float, bool, str]] = [
        ("/api/v1/tiktok/web/fetch_post_detail_v2", 0.001, True, "tiktok"),
        ("/api/v1/tiktok/web/fetch_post_comment", 0.001, True, "tiktok"),
        ("/api/v1/instagram/v2/fetch_post_info", 0.002, False, "instagram"),
        ("/api/v1/instagram/v2/fetch_post_comments", 0.002, False, "instagram"),
        ("/api/v1/linkedin/web/get_post_detail", 0.004, False, "linkedin"),
        ("/api/v1/linkedin/web/get_post_comments", 0.004, False, "linkedin"),
        ("/api/v1/threads/web/fetch_post_detail_v2", 0.002, False, "threads"),
        ("/api/v1/threads/web/fetch_post_comments", 0.002, False, "threads"),
        ("/api/v1/twitter/web/fetch_tweet_detail", 0.001, False, "twitter"),
        ("/api/v1/twitter/web/fetch_post_comments", 0.001, False, "twitter"),
        ("/api/v1/youtube/web_v2/get_video_info_v2", 0.001, False, "youtube"),
        ("/api/v1/youtube/web_v2/get_video_comments", 0.001, False, "youtube"),
        ("/api/v1/douyin/web/fetch_one_video_v2", 0.001, True, "douyin"),
        ("/api/v1/douyin/web/fetch_video_comments", 0.001, True, "douyin"),
        ("/api/v1/xiaohongshu/app_v2/get_image_note_detail", 0.01, False, "xiaohongshu"),
        ("/api/v1/xiaohongshu/app_v2/get_video_note_detail", 0.01, False, "xiaohongshu"),
        ("/api/v1/xiaohongshu/app_v2/get_note_comments", 0.01, False, "xiaohongshu"),
        ("/api/v1/bilibili/web/fetch_one_video", 0.001, True, "bilibili"),
        ("/api/v1/bilibili/web/fetch_video_comments", 0.001, True, "bilibili"),
        ("/api/v1/zhihu/web/fetch_answer_detail", 0.001, False, "zhihu"),
        ("/api/v1/zhihu/web/fetch_comment_v5", 0.001, True, "zhihu"),
        ("/api/v1/wechat_mp/v2/fetch_article_detail", 0.01, False, "wechat_mp"),
        ("/api/v1/wechat_mp/v2/fetch_article_comments", 0.01, False, "wechat_mp"),
        ("/api/v1/reddit/app/fetch_post_details", 0.001, False, "reddit"),
        ("/api/v1/reddit/app/fetch_post_comments", 0.001, False, "reddit"),
    ]
    return [
        {
            "endpoint_uri": endpoint,
            "endpoint_cost": cost,
            "allow_free_credit": free,
            "allow_discount": False,
            "platform": platform,
        }
        for endpoint, cost, free, platform in rows
    ]


class TikHubPlanTests(unittest.TestCase):
    def test_tiktok_search_uses_app_v3_after_live_web_endpoint_failure(self) -> None:
        plan = build_search_plan(
            as_of="2026-07-14",
            run_id=RUN_ID,
            query_groups=[{"id": "tiktok", "keyword": "AI app complaint", "sources": ["tiktok"]}],
        )
        request_item = plan["requests"][0]
        self.assertEqual(request_item["endpoint"], "/api/v1/tiktok/app/v3/fetch_video_search_result")
        self.assertEqual(request_item["method"], "GET")
        self.assertEqual(
            request_item["params"],
            {
                "keyword": "AI app complaint",
                "offset": 0,
                "count": 20,
                "sort_type": 0,
                "publish_time": 30,
                "region": "US",
            },
        )

    def test_builds_allowlisted_search_requests_without_secrets(self) -> None:
        plan = build_search_plan(
            as_of="2026-07-14",
            run_id=RUN_ID,
            query_groups=[
                {"id": "global", "keyword": "AI app complaint", "sources": ["tiktok"]},
                {"id": "china", "keyword": "AI 工具 难用", "sources": ["xiaohongshu", "wechat_search"]},
            ],
        )

        self.assertEqual(plan["provider"], "tikhub")
        self.assertEqual(len(plan["requests"]), 3)
        self.assertEqual(plan["requests"][0]["params"]["keyword"], "AI app complaint")
        self.assertNotIn("cookie", json.dumps(plan, ensure_ascii=False).lower())
        self.assertNotIn("token", json.dumps(plan, ensure_ascii=False).lower())

        zhihu = build_search_plan(
            as_of="2026-07-14",
            run_id=RUN_ID,
            query_groups=[{"id": "zhihu", "keyword": "AI 工具 难用", "sources": ["zhihu"]}],
        )["requests"][0]
        self.assertEqual(zhihu["params"]["vertical"], "answer")

    def test_rejects_unknown_source_and_sensitive_parameters(self) -> None:
        with self.assertRaises(PlanError):
            build_search_plan(
                as_of="2026-07-14",
                run_id=RUN_ID,
                query_groups=[{"id": "bad", "keyword": "test", "sources": ["unknown"]}],
            )

        plan = sample_plan()
        plan["requests"][0]["params"]["cookie"] = "secret"
        with self.assertRaises(PlanError):
            validate_plan(plan, pricing_rows())

        with self.assertRaisesRegex(PlanError, "日期与 as_of 不一致"):
            build_search_plan(
                as_of="2026-07-15",
                run_id=RUN_ID,
                query_groups=[{"id": "bad-date", "keyword": "test", "sources": ["tiktok"]}],
            )

    def test_rejects_modified_or_missing_fixed_search_parameters(self) -> None:
        plan = sample_plan()
        plan["requests"][0]["params"]["count"] = -1
        with self.assertRaisesRegex(PlanError, "固定为"):
            validate_plan(plan, pricing_rows())

        plan = sample_plan()
        del plan["requests"][0]["params"]["count"]
        with self.assertRaisesRegex(PlanError, "固定为"):
            validate_plan(plan, pricing_rows())


class TikHubCommentPlanTests(unittest.TestCase):
    def test_builds_two_request_deep_dive_for_all_twelve_supported_sources(self) -> None:
        cases = [
            ("tiktok", {"aweme_id": "100"}, None),
            ("instagram", {"code_or_url": "ABC"}, None),
            ("linkedin", {"post_id": "urn:li:activity:1"}, None),
            ("threads", {"post_id": "12345"}, None),
            ("twitter", {"tweet_id": "200"}, None),
            ("youtube", {"video_id": "yt1"}, None),
            ("douyin", {"aweme_id": "300"}, None),
            ("xiaohongshu", {"note_id": "xhs1"}, "image_note"),
            ("bilibili", {"bv_id": "BV1xx"}, None),
            ("zhihu", {"answer_id": "400"}, "answer"),
            ("wechat_search", {"url": "https://mp.weixin.qq.com/s/example"}, "article"),
            ("reddit", {"post_id": "abc"}, None),
        ]
        for source, identifiers, content_type in cases:
            with self.subTest(source=source):
                selection = {
                    "source": source,
                    "selected_item_id": f"{source}-1",
                    "selection_reason": "高互动且包含具体痛点",
                    "identifiers": identifiers,
                }
                if content_type:
                    selection["content_type"] = content_type
                plan = build_comment_plan(
                    as_of="2026-07-14",
                    parent_search_run_id=RUN_ID,
                    selections=[selection],
                )
                self.assertEqual(plan["stage"], "comment_deep_dive")
                self.assertEqual(len(plan["requests"]), 2)
                self.assertEqual({item["kind"] for item in plan["requests"]}, {"detail", "top_level_comments"})
                validate_plan(plan, comment_pricing_rows())

    def test_routes_xiaohongshu_type_wechat_body_and_reddit_prefix(self) -> None:
        video = build_comment_plan(
            as_of="2026-07-14",
            parent_search_run_id=RUN_ID,
            selections=[{
                "source": "xiaohongshu",
                "selected_item_id": "xhs-video",
                "selection_reason": "具体抱怨",
                "content_type": "video_note",
                "identifiers": {"share_text": "https://xhslink.com/example"},
            }],
        )
        self.assertIn("get_video_note_detail", video["requests"][0]["endpoint"])

        wechat = build_comment_plan(
            as_of="2026-07-14",
            parent_search_run_id=RUN_ID,
            selections=[{
                "source": "wechat_search",
                "selected_item_id": "wx-1",
                "selection_reason": "需求明确",
                "content_type": "article",
                "identifiers": {"url": "https://mp.weixin.qq.com/s/example"},
            }],
        )
        self.assertEqual({item["method"] for item in wechat["requests"]}, {"POST"})

        reddit = build_comment_plan(
            as_of="2026-07-14",
            parent_search_run_id=RUN_ID,
            selections=[{
                "source": "reddit",
                "selected_item_id": "reddit-1",
                "selection_reason": "付费意愿",
                "identifiers": {"post_id": "abc123"},
            }],
        )
        self.assertEqual(reddit["requests"][0]["params"]["post_id"], "t3_abc123")

    def test_rejects_missing_live_price_and_invalid_type_or_identifier(self) -> None:
        base = {
            "selected_item_id": "item-1",
            "selection_reason": "痛点明确",
            "identifiers": {"post_id": "1"},
        }
        threads = build_comment_plan(
            as_of="2026-07-14",
            parent_search_run_id=RUN_ID,
            selections=[{"source": "threads", **base}],
        )
        rows_without_comment_price = [
            row for row in comment_pricing_rows()
            if row["endpoint_uri"] != "/api/v1/threads/web/fetch_post_comments"
        ]
        with self.assertRaisesRegex(PlanError, "实时价格表缺少"):
            validate_plan(threads, rows_without_comment_price)
        with self.assertRaises(PlanError):
            build_comment_plan(
                as_of="2026-07-14",
                parent_search_run_id=RUN_ID,
                selections=[{"source": "zhihu", "content_type": "article", **base}],
            )
        with self.assertRaises(PlanError):
            build_comment_plan(
                as_of="2026-07-14",
                parent_search_run_id=RUN_ID,
                selections=[{
                    "source": "xiaohongshu",
                    "selected_item_id": "xhs",
                    "selection_reason": "痛点明确",
                    "content_type": "image_note",
                    "identifiers": {},
                }],
            )

    def test_comment_plan_has_separate_budget_and_known_per_item_cost(self) -> None:
        plan = build_comment_plan(
            as_of="2026-07-14",
            parent_search_run_id=RUN_ID,
            selections=[{
                "source": "linkedin",
                "selected_item_id": "li-1",
                "selection_reason": "小企业明确付费",
                "identifiers": {"post_id": "urn:li:activity:1"},
            }],
        )
        estimate = estimate_plan(plan, comment_pricing_rows())
        self.assertAlmostEqual(estimate["estimated_cost_usd"], 0.008)
        self.assertEqual(plan["cost_policy"]["budget_scope"], "separate_from_search")

    def test_rejects_unknown_endpoint_and_oversized_plan(self) -> None:
        plan = sample_plan()
        plan["requests"][0]["endpoint"] = "/api/v1/unknown"
        with self.assertRaises(PlanError):
            validate_plan(plan, pricing_rows())

        plan = sample_plan()
        plan["requests"] = plan["requests"] * 101
        with self.assertRaises(PlanError):
            validate_plan(plan, pricing_rows())


class TikHubPricingTests(unittest.TestCase):
    def test_non_boolean_catalog_flags_fail_closed(self) -> None:
        rows = pricing_rows()
        tiktok = next(
            row for row in rows
            if row["endpoint_uri"] == "/api/v1/tiktok/app/v3/fetch_video_search_result"
        )
        tiktok["allow_free_credit"] = "false"
        tiktok["allow_discount"] = "true"
        estimate = estimate_plan(sample_plan(), rows, discount_rate=0.5)
        tiktok_estimate = next(row for row in estimate["requests"] if row["source"] == "tiktok")
        self.assertFalse(tiktok_estimate["allow_free_credit"])
        self.assertFalse(tiktok_estimate["allow_discount"])
        self.assertEqual(estimate["free_credit_eligible_cost_usd"], 0.0)

    def test_duplicate_price_rows_use_highest_price_and_conservative_flags(self) -> None:
        rows = pricing_rows()
        rows.append({
            "endpoint_uri": "/api/v1/tiktok/app/v3/fetch_video_search_result",
            "endpoint_cost": 0.009,
            "allow_free_credit": False,
            "allow_discount": False,
            "platform": "tiktok",
        })
        rows.append({
            "endpoint_uri": "/api/v1/tiktok/app/v3/fetch_video_search_result",
            "endpoint_cost": 0.0001,
            "allow_free_credit": True,
            "allow_discount": True,
            "platform": "tiktok",
        })
        estimate = estimate_plan(sample_plan(), rows)
        self.assertAlmostEqual(estimate["list_price_usd"], 0.019)
        tiktok = next(row for row in estimate["requests"] if row["source"] == "tiktok")
        self.assertFalse(tiktok["allow_free_credit"])
        self.assertFalse(tiktok["allow_discount"])

    def test_estimates_list_price_cny_and_worst_case(self) -> None:
        estimate = estimate_plan(sample_plan(), pricing_rows(), usd_to_cny=7.2, max_attempts=2)

        self.assertEqual(estimate["request_count"], 2)
        self.assertAlmostEqual(estimate["list_price_usd"], 0.011)
        self.assertAlmostEqual(estimate["estimated_cost_usd"], 0.011)
        self.assertAlmostEqual(estimate["estimated_cost_cny"], 0.0792)
        self.assertAlmostEqual(estimate["worst_case_cost_usd"], 0.022)
        self.assertAlmostEqual(estimate["free_credit_eligible_cost_usd"], 0.001)
        self.assertAlmostEqual(estimate["free_credit_ineligible_cost_usd"], 0.01)
        self.assertNotIn("paid_balance_required_cost_usd", estimate)
        self.assertFalse(estimate["all_requests_allow_free_credit"])
        self.assertEqual({row["source"] for row in estimate["by_source"]}, {"tiktok", "xiaohongshu"})
        self.assertRegex(estimate["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(estimate["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(estimate["price_observed_at"])

    def test_applies_discount_only_to_eligible_endpoints(self) -> None:
        estimate = estimate_plan(sample_plan(), pricing_rows(), discount_rate=0.8, max_attempts=2)
        self.assertAlmostEqual(estimate["estimated_cost_usd"], 0.0108)
        self.assertAlmostEqual(estimate["worst_case_cost_usd"], 0.022)

    def test_rejects_non_finite_money_inputs(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(PlanError):
                    estimate_plan(sample_plan(), pricing_rows(), usd_to_cny=value)
                with self.assertRaises(PlanError):
                    estimate_plan(sample_plan(), pricing_rows(), discount_rate=value)

        estimate = estimate_plan(sample_plan(), pricing_rows())
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(budget=value), self.assertRaises(PlanError):
                enforce_budget(estimate, value)

    def test_enforces_budget_against_worst_case_cost(self) -> None:
        estimate = estimate_plan(sample_plan(), pricing_rows(), max_attempts=2)
        with self.assertRaises(BudgetExceeded):
            enforce_budget(estimate, 0.02)
        enforce_budget(estimate, 0.022)

    def test_budget_guard_uses_unrounded_exact_cost(self) -> None:
        rows = pricing_rows()
        next(
            row for row in rows
            if row["endpoint_uri"] == "/api/v1/tiktok/app/v3/fetch_video_search_result"
        )["endpoint_cost"] = 0.0000004
        next(
            row for row in rows
            if row["endpoint_uri"] == "/api/v1/xiaohongshu/app_v2/search_notes"
        )["endpoint_cost"] = 0
        estimate = estimate_plan(sample_plan(), rows)
        self.assertEqual(estimate["worst_case_cost_usd"], 0.0)
        self.assertEqual(estimate["budget_guard_cost_usd"], "0.0000004")
        with self.assertRaises(BudgetExceeded):
            enforce_budget(estimate, 0)


class TikHubExecutionTests(unittest.TestCase):
    def test_production_entry_always_fetches_live_pricing_internally(self) -> None:
        with mock.patch.object(tq, "fetch_live_pricing", return_value=pricing_rows()) as fetch:
            result = tq.execute_plan(
                sample_plan(),
                token="secret",
                max_cost_usd=0.02,
                transport=lambda **kwargs: {"data": []},
                account_transport=healthy_account_transport,
            )
        fetch.assert_called_once_with()
        self.assertEqual(result["pricing_snapshot"]["price_source"], "dashboard_pricing_catalog")
        with self.assertRaises(TypeError):
            tq.execute_plan(  # type: ignore[misc]
                sample_plan(),
                pricing_rows(),
                token="secret",
                max_cost_usd=0.02,
            )

    def test_account_preflight_endpoint_must_exist_and_remain_free(self) -> None:
        account_calls: list[object] = []
        data_calls: list[object] = []

        def account_transport(**kwargs: object) -> dict[str, object]:
            account_calls.append(kwargs)
            return account_payload()

        def data_transport(**kwargs: object) -> dict[str, object]:
            data_calls.append(kwargs)
            return {"data": []}

        rows_without_account = [
            row for row in pricing_rows()
            if row["endpoint_uri"] != "/api/v1/tikhub/user/get_user_info"
        ]
        with self.assertRaisesRegex(PlanError, "缺少账户预检端点"):
            execute_plan(
                sample_plan(),
                rows_without_account,
                token="secret",
                max_cost_usd=0.02,
                transport=data_transport,
                account_transport=account_transport,
            )

        nonfree_rows = pricing_rows()
        next(
            row for row in nonfree_rows
            if row["endpoint_uri"] == "/api/v1/tikhub/user/get_user_info"
        )["endpoint_cost"] = 0.001
        with self.assertRaisesRegex(PlanError, "不再免费"):
            execute_plan(
                sample_plan(),
                nonfree_rows,
                token="secret",
                max_cost_usd=0.02,
                transport=data_transport,
                account_transport=account_transport,
            )
        self.assertEqual(account_calls, [])
        self.assertEqual(data_calls, [])

    def test_account_snapshot_uses_official_zero_cost_endpoint_and_whitelists_fields(self) -> None:
        captured: list[dict[str, object]] = []

        def account_transport(**kwargs: object) -> dict[str, object]:
            captured.append(kwargs)
            return account_payload(balance=0.5, free_credit=0.25)

        snapshot = tq.fetch_account_snapshot(
            token="secret",
            api_base="https://api.tikhub.dev/",
            transport=account_transport,
        )
        self.assertEqual(captured[0]["url"], "https://api.tikhub.dev/api/v1/tikhub/user/get_user_info")
        self.assertEqual(captured[0]["method"], "GET")
        self.assertEqual(captured[0]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(snapshot["balance_usd"], 0.5)
        self.assertEqual(snapshot["free_credit_usd"], 0.25)
        serialized = json.dumps(snapshot)
        self.assertNotIn("email", serialized)
        self.assertNotIn("test-key", serialized)

    def test_requires_token_before_any_request(self) -> None:
        calls: list[object] = []

        def transport(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"ok": True}

        with self.assertRaises(PlanError):
            execute_plan(
                sample_plan(),
                pricing_rows(),
                token="",
                max_cost_usd=0.02,
                transport=transport,
                account_transport=healthy_account_transport,
            )
        self.assertEqual(calls, [])

    def test_executes_with_bearer_but_never_persists_secret(self) -> None:
        seen_headers: list[dict[str, str]] = []

        def transport(**kwargs: object) -> dict[str, object]:
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            seen_headers.append(headers)
            return {"data": {"items": [{"title": "test"}]}, "authorization": "should-be-removed"}

        result = execute_plan(
            sample_plan(),
            pricing_rows(),
            token="super-secret-token",
            max_cost_usd=0.02,
            transport=transport,
            account_transport=healthy_account_transport,
        )

        self.assertEqual(len(seen_headers), 2)
        self.assertEqual(seen_headers[0]["Authorization"], "Bearer super-secret-token")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("super-secret-token", serialized)
        self.assertNotIn("should-be-removed", serialized)
        self.assertEqual(result["summary"]["ok"], 2)
        self.assertEqual(result["as_of"], "2026-07-14")
        self.assertAlmostEqual(result["summary"]["estimated_attempted_cost_usd"], 0.011)
        self.assertEqual({row["source"] for row in result["summary"]["by_source"]}, {"tiktok", "xiaohongshu"})
        self.assertEqual(result["estimate"]["plan_sha256"], result["pricing_snapshot"]["plan_sha256"])
        self.assertEqual(result["account_snapshot"]["balance_usd"], 1.0)
        self.assertNotIn("email", json.dumps(result["account_snapshot"]))

    def test_redacts_secret_from_transport_errors_and_continues(self) -> None:
        attempts = 0

        def transport(**kwargs: object) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Bearer super-secret-token failed")
            return {"data": []}

        result = execute_plan(
            sample_plan(),
            pricing_rows(),
            token="super-secret-token",
            max_cost_usd=0.02,
            transport=transport,
            account_transport=healthy_account_transport,
        )

        self.assertEqual(result["summary"]["error"], 1)
        self.assertEqual(result["summary"]["ok"], 1)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("super-secret-token", serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertEqual(result["results"][0]["error_code"], "request_error")

    def test_redacts_generic_credential_patterns_from_errors(self) -> None:
        def transport(**kwargs: object) -> dict[str, object]:
            raise RuntimeError("token=abc123 api_key: xyz789 Cookie=session-secret Bearer bearer-secret")

        result = execute_plan(
            sample_plan(),
            pricing_rows(),
            token="different-secret",
            max_cost_usd=0.02,
            transport=transport,
            account_transport=healthy_account_transport,
        )
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in ("abc123", "xyz789", "session-secret", "bearer-secret", "different-secret"):
            self.assertNotIn(secret, serialized)

    def test_http_200_application_error_is_not_counted_as_success(self) -> None:
        result = execute_plan(
            sample_plan(),
            pricing_rows(),
            token="secret",
            max_cost_usd=0.02,
            transport=lambda **kwargs: {"code": 500, "message": "upstream failed"},
            account_transport=healthy_account_transport,
        )

        self.assertEqual(result["summary"]["ok"], 0)
        self.assertEqual(result["summary"]["error"], 2)
        self.assertTrue(all(item["error_code"] == "request_error" for item in result["results"]))

    def test_classifies_common_transport_failures(self) -> None:
        self.assertEqual(tq._classify_error(RuntimeError("TikHub HTTP 401: denied")), "auth_error")
        self.assertEqual(tq._classify_error(RuntimeError("TikHub HTTP 429: slow down")), "rate_limited")
        self.assertEqual(tq._classify_error(TimeoutError("timed out")), "timeout")
        self.assertEqual(tq._classify_error(RuntimeError("TikHub 响应超过 8 MiB 安全上限")), "response_too_large")

    def test_does_not_retry_nonrecoverable_auth_errors(self) -> None:
        calls = 0

        def transport(**kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise RuntimeError("TikHub HTTP 401")

        result = execute_plan(
            sample_plan(),
            pricing_rows(),
            token="secret",
            max_cost_usd=0.04,
            max_attempts=3,
            transport=transport,
            account_transport=healthy_account_transport,
        )
        self.assertEqual(calls, 2)
        self.assertEqual([item["attempts"] for item in result["results"]], [1, 1])

    def test_stops_before_more_paid_calls_when_batch_result_limit_is_hit(self) -> None:
        calls = 0

        def transport(**kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"data": "x" * 500}

        with mock.patch.object(tq, "MAX_BATCH_RESULT_BYTES", 100):
            result = execute_plan(
                sample_plan(),
                pricing_rows(),
                token="secret",
                max_cost_usd=0.02,
                transport=transport,
                account_transport=healthy_account_transport,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(result["results"][0]["error_code"], "batch_result_limit")
        self.assertEqual(result["results"][1]["status"], "skipped")
        self.assertEqual(result["summary"]["skipped"], 1)
        self.assertTrue(result["summary"]["stopped_early"])

    def test_account_preflight_blocks_insufficient_paid_balance_before_data_calls(self) -> None:
        data_calls: list[object] = []

        def transport(**kwargs: object) -> dict[str, object]:
            data_calls.append(kwargs)
            return {"data": []}

        def low_balance(**kwargs: object) -> dict[str, object]:
            return account_payload(balance=0.009, free_credit=0.05)

        with self.assertRaisesRegex(PlanError, "付费余额不足"):
            execute_plan(
                sample_plan(),
                pricing_rows(),
                token="secret",
                max_cost_usd=0.02,
                transport=transport,
                account_transport=low_balance,
            )
        self.assertEqual(data_calls, [])

    def test_account_preflight_uses_paid_balance_when_free_credit_is_insufficient(self) -> None:
        def no_free_credit(**kwargs: object) -> dict[str, object]:
            return account_payload(balance=0.011, free_credit=0)

        result = execute_plan(
            sample_plan(),
            pricing_rows(),
            token="secret",
            max_cost_usd=0.02,
            transport=lambda **kwargs: {"data": []},
            account_transport=no_free_credit,
        )
        self.assertEqual(result["account_snapshot"]["required_paid_balance_usd"], 0.011)
        self.assertTrue(result["account_snapshot"]["sufficient_for_worst_case"])

    def test_account_preflight_rejects_disabled_or_malformed_account(self) -> None:
        with self.assertRaises(PlanError):
            execute_plan(
                sample_plan(),
                pricing_rows(),
                token="secret",
                max_cost_usd=0.02,
                transport=lambda **kwargs: {"data": []},
                account_transport=lambda **kwargs: account_payload(active=False),
            )
        with self.assertRaises(PlanError):
            execute_plan(
                sample_plan(),
                pricing_rows(),
                token="secret",
                max_cost_usd=0.02,
                transport=lambda **kwargs: {"data": []},
                account_transport=lambda **kwargs: {"user_data": {"balance": "NaN"}},
            )


class TikHubCliTests(unittest.TestCase):
    def test_build_comments_cli_writes_separate_plan_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selections_path = root / "selections.json"
            output_path = root / "comment-plan.json"
            selections_path.write_text(json.dumps([{
                "source": "threads",
                "selected_item_id": "threads-1",
                "selection_reason": "多人重复抱怨同一工作流",
                "identifiers": {"post_id": "12345"},
            }], ensure_ascii=False), encoding="utf-8")
            argv = [
                "tikhub_query.py",
                "build-comments",
                "--date",
                "2026-07-14",
                "--parent-search-run-id",
                RUN_ID,
                "--input",
                str(selections_path),
                "--output",
                str(output_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(tq, "fetch_live_pricing") as fetch,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(tq.main(), 0)
            fetch.assert_not_called()
            plan = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["stage"], "comment_deep_dive")
            self.assertEqual(len(plan["requests"]), 2)

    def test_estimate_cli_works_offline_with_pricing_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            pricing_path = root / "pricing.json"
            output_path = root / "estimate.json"
            plan_path.write_text(json.dumps(sample_plan(), ensure_ascii=False), encoding="utf-8")
            pricing_path.write_text(json.dumps({"data": pricing_rows()}), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "tikhub_query.py"),
                    "estimate",
                    "--plan",
                    str(plan_path),
                    "--pricing-file",
                    str(pricing_path),
                    "--output",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            estimate = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(estimate["estimated_cost_usd"], 0.011)
            self.assertIn("预计费用", completed.stdout)

    def test_main_estimate_and_run_branches_write_atomic_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            pricing_path = root / "pricing.json"
            estimate_path = root / "estimate.json"
            result_path = root / "result.json"
            plan_path.write_text(json.dumps(sample_plan(), ensure_ascii=False), encoding="utf-8")
            pricing_path.write_text(json.dumps({"data": pricing_rows()}), encoding="utf-8")

            estimate_argv = [
                "tikhub_query.py",
                "estimate",
                "--plan",
                str(plan_path),
                "--pricing-file",
                str(pricing_path),
                "--output",
                str(estimate_path),
            ]
            with mock.patch.object(sys, "argv", estimate_argv), redirect_stdout(io.StringIO()):
                self.assertEqual(tq.main(), 0)
            self.assertEqual(json.loads(estimate_path.read_text())["provider"], "tikhub")

            run_argv = [
                "tikhub_query.py",
                "run",
                "--plan",
                str(plan_path),
                "--max-cost-usd",
                "0.02",
                "--output",
                str(result_path),
            ]
            with (
                mock.patch.object(sys, "argv", run_argv),
                mock.patch.dict("os.environ", {"TIKHUB_API_KEY": "test-token"}, clear=False),
                mock.patch.object(
                    tq,
                    "execute_plan",
                    return_value={
                        "provider": "tikhub",
                        "estimate": estimate_plan(sample_plan(), pricing_rows()),
                        "summary": {"ok": 2},
                    },
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(tq.main(), 0)
            self.assertEqual(json.loads(result_path.read_text())["summary"]["ok"], 2)

    def test_run_rejects_offline_pricing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps(sample_plan(), ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "tikhub_query.py"),
                    "run",
                    "--plan",
                    str(plan_path),
                    "--pricing-file",
                    str(Path(tmp) / "pricing.json"),
                    "--max-cost-usd",
                    "0.02",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("unrecognized arguments", completed.stderr)

    def test_run_checks_token_before_fetching_live_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps(sample_plan(), ensure_ascii=False), encoding="utf-8")
            argv = ["tikhub_query.py", "run", "--plan", str(plan_path), "--max-cost-usd", "0.02"]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch.object(tq, "fetch_live_pricing") as fetch,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(tq.main(), 2)
            fetch.assert_not_called()

    def test_main_returns_two_for_invalid_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            pricing_path = root / "pricing.json"
            plan_path.write_text(json.dumps({"provider": "wrong", "requests": []}), encoding="utf-8")
            pricing_path.write_text(json.dumps({"data": pricing_rows()}), encoding="utf-8")
            argv = [
                "tikhub_query.py",
                "estimate",
                "--plan",
                str(plan_path),
                "--pricing-file",
                str(pricing_path),
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()):
                self.assertEqual(tq.main(), 2)

    def test_read_json_rejects_non_finite_constants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"value": NaN}', encoding="utf-8")
            with self.assertRaises(PlanError):
                tq._read_json(path)


class TikHubHttpTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload: object):
            self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def __enter__(self) -> "TikHubHttpTests.FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return self.payload

    def test_default_transport_builds_get_and_post_without_cookie(self) -> None:
        captured: list[object] = []

        def fake_urlopen(req: object, timeout: int) -> "TikHubHttpTests.FakeResponse":
            captured.append((req, timeout))
            return self.FakeResponse({"data": []})

        headers = {"Authorization": "Bearer test", "Accept": "application/json"}
        with mock.patch.object(tq, "_safe_urlopen", side_effect=fake_urlopen):
            get_result = tq._default_transport(
                method="GET",
                url="https://api.tikhub.dev/api/v1/test",
                params={"keyword": "AI 痛点", "cursor": None},
                headers=headers,
                timeout=45,
            )
            post_result = tq._default_transport(
                method="POST",
                url="https://api.tikhub.dev/api/v1/test",
                params={"keyword": "AI 痛点"},
                headers=headers,
                timeout=45,
            )

        self.assertEqual(get_result, {"data": []})
        self.assertEqual(post_result, {"data": []})
        get_request = captured[0][0]
        post_request = captured[1][0]
        self.assertEqual(get_request.get_method(), "GET")
        self.assertIn("keyword=AI+%E7%97%9B%E7%82%B9", get_request.full_url)
        self.assertNotIn("cursor", get_request.full_url)
        self.assertEqual(post_request.get_method(), "POST")
        self.assertEqual(json.loads(post_request.data.decode("utf-8"))["keyword"], "AI 痛点")
        self.assertIsNone(post_request.get_header("Cookie"))

    def test_redirect_handler_rejects_cross_origin_and_https_downgrade(self) -> None:
        handler = tq._SameOriginRedirectHandler()
        req = tq.request.Request(
            "https://api.tikhub.dev/api/v1/test",
            headers={"Authorization": "Bearer secret"},
        )
        for target in ("https://evil.example/steal", "http://api.tikhub.dev/steal"):
            with self.subTest(target=target), self.assertRaises(tq.error.HTTPError):
                handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_live_pricing_parser_uses_public_dashboard_response(self) -> None:
        payload = {"status": "success", "data": pricing_rows()}
        with mock.patch.object(tq, "_safe_urlopen", return_value=self.FakeResponse(payload)):
            rows = tq.fetch_live_pricing()
        self.assertEqual(len(rows), len(pricing_rows()))
        self.assertEqual(
            next(row for row in rows if row["endpoint_uri"] == "/api/v1/tiktok/app/v3/fetch_video_search_result")["platform"],
            "tiktok",
        )


if __name__ == "__main__":
    unittest.main()
