from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from community_query import (  # noqa: E402
    CommunityPlanError,
    build_community_plan,
    execute_plan,
    validate_plan,
)


RUN_ID = "RUN-20260714-ABCDEF1234"


class CommunityQueryTests(unittest.TestCase):
    def test_builds_bounded_thirty_day_plan(self) -> None:
        plan = build_community_plan(
            as_of="2026-07-14",
            run_id=RUN_ID,
            focus_name="东南亚",
            custom_focus="本地语言社交产品",
        )

        self.assertEqual(plan["schema_version"], "2.0")
        self.assertEqual(plan["run_id"], RUN_ID)
        self.assertEqual(plan["window"]["range_from"], "2026-06-15")
        self.assertEqual(plan["window"]["lookback_days"], 30)
        self.assertEqual({item["source"] for item in plan["requests"]}, {"hackernews", "github"})
        self.assertEqual(len(plan["requests"]), 4)
        hn_params = next(item["params"] for item in plan["requests"] if item["source"] == "hackernews")
        github_params = next(item["params"] for item in plan["requests"] if item["source"] == "github")
        self.assertIn("created_at_i<=", hn_params["numericFilters"])
        self.assertIn("created:2026-06-15..2026-07-14", github_params["q"])
        validate_plan(plan)

    def test_rejects_endpoint_and_parameter_expansion(self) -> None:
        plan = build_community_plan(as_of="2026-07-14", run_id=RUN_ID, focus_name="东南亚")
        plan["requests"][0]["endpoint"] = "https://example.com/collect"
        with self.assertRaises(CommunityPlanError):
            validate_plan(plan)

        with self.assertRaisesRegex(CommunityPlanError, "日期与 as_of 不一致"):
            build_community_plan(as_of="2026-07-15", run_id=RUN_ID, focus_name="东南亚")

        plan = build_community_plan(as_of="2026-07-14", run_id=RUN_ID, focus_name="东南亚")
        plan["requests"][0]["params"]["token"] = "secret"
        with self.assertRaises(CommunityPlanError):
            validate_plan(plan)

    def test_normalizes_deduplicates_and_never_persists_github_token(self) -> None:
        plan = build_community_plan(as_of="2026-07-14", run_id=RUN_ID, focus_name="东南亚")
        seen_headers: list[dict[str, str]] = []

        def transport(**kwargs: object) -> dict:
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            seen_headers.append(headers)
            endpoint = str(kwargs["endpoint"])
            if "algolia" in endpoint:
                return {"hits": [{
                    "objectID": "1",
                    "title": "Manual AI workflow needs a better tool",
                    "story_text": "Still copying data manually",
                    "created_at": "2026-07-13T00:00:00Z",
                    "points": 10,
                    "num_comments": 4,
                }]}
            return {"items": [{
                "id": 2,
                "html_url": "https://github.com/acme/tool/issues/2",
                "repository_url": "https://api.github.com/repos/acme/tool",
                "title": "Manual workflow missing AI localization",
                "body": "We still need a manual workaround",
                "created_at": "2026-07-12T00:00:00Z",
                "comments": 6,
                "user": {"login": "buyer"},
            }]}

        result = execute_plan(plan, github_token="top-secret-token", transport=transport)

        self.assertEqual(result["stats"]["valid_items"], 2)
        self.assertEqual(result["stats"]["source_status"], {"hackernews": "ok", "github": "ok"})
        self.assertGreater(result["evidence"][0]["local_relevance"], 0)
        self.assertTrue(any(headers.get("Authorization") == "Bearer top-secret-token" for headers in seen_headers))
        self.assertNotIn("top-secret-token", json.dumps(result))

    def test_records_partial_source_failure_without_aborting_other_sources(self) -> None:
        plan = build_community_plan(as_of="2026-07-14", run_id=RUN_ID, focus_name="东南亚")

        def transport(**kwargs: object) -> dict:
            if "algolia" in str(kwargs["endpoint"]):
                raise RuntimeError("rate-limited:429")
            return {"items": []}

        result = execute_plan(plan, transport=transport)

        self.assertEqual(result["stats"]["source_status"]["hackernews"], "rate-limited")
        self.assertEqual(result["stats"]["source_status"]["github"], "no-results")


if __name__ == "__main__":
    unittest.main()
