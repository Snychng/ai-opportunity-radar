from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from normalize_tikhub_results import normalize_documents  # noqa: E402
from tikhub_query import build_comment_plan  # noqa: E402


RUN_ID = "RUN-20260714-ABCDEF1234"


def _result(source: str, data: dict, *, query_id: str | None = None) -> dict:
    return {
        "id": query_id or f"query-{source}",
        "query_group": "global-pain",
        "source": source,
        "params": {"keyword": "need a tool"},
        "status": "ok",
        "response": {"data": data},
    }


def _document(
    results: list[dict],
    *,
    generated_at: str = "2026-07-14T16:00:00+08:00",
    stage: str = "search_discovery",
) -> dict:
    return {
        "schema_version": "3.0",
        "run_id": RUN_ID,
        "as_of": "2026-07-14",
        "stage": stage,
        "generated_at": generated_at,
        "provider": "tikhub",
        "results": results,
    }


class NormalizeTikHubResultsTests(unittest.TestCase):
    def test_normalizes_all_phase_one_source_shapes(self) -> None:
        results = [
            _result("tiktok", {"search_item_list": [{"aweme_info": {
                "aweme_id": "tt1", "desc": "I need a better AI companion",
                "create_time": 1784000000, "share_url": "https://www.tiktok.com/t/tt1",
                "author": {"unique_id": "creator"},
                "statistics": {"digg_count": 12, "comment_count": 3, "share_count": 2, "play_count": 100},
            }}]}),
            _result("instagram", {"data": {"items": [{
                "id": "ig1", "code": "ABC", "caption": {"text": "Manual workaround"},
                "taken_at": 1784000000, "user": {"username": "maker"},
                "like_count": 9, "comment_count": 2, "play_count": 80,
            }]}}),
            _result("linkedin", {"data": [{
                "id": "li1", "title": "We still copy invoices by hand", "author": "Owner",
                "created_at": "2026-07-13T10:00:00Z", "url": "https://www.linkedin.com/feed/update/li1",
                "activity": {"numLikes": 4, "numComments": 2},
            }]}),
            _result("threads", {"searchResults": {"edges": [{"node": {
                "id": "th1", "text": "Wish this social app remembered context",
                "taken_at": 1784000000, "user": {"username": "person"},
                "like_count": 5, "reply_count": 1,
            }}]}}),
            _result("twitter", {"timeline": [{
                "tweet_id": "tw1", "text": "I would pay for this integration", "screen_name": "buyer",
                "created_at": "Mon Jul 13 10:00:00 +0000 2026", "favorites": 8,
                "replies": 2, "retweets": 1, "views": 50, "lang": "en",
            }]}),
            _result("youtube", {"videos": [{
                "video_id": "yt1", "title": "AI tools that still fail", "description": "A concrete workflow",
                "author": "Channel", "channel_id": "channel1", "published_time": "2 days ago", "number_of_views": 300,
            }]}),
            _result("reddit", {"search": {"dynamic": {"components": {"main": {"edges": [
                {"presentation": {"type": "empty_state"}},
                {"node": {"id": "t3_rd1", "title": "Need a niche app", "selftext": "Spreadsheet workaround",
                          "author": "user", "subreddit": "smallbusiness", "created_utc": 1784000000,
                          "score": 6, "num_comments": 4, "permalink": "/r/smallbusiness/comments/rd1/test/"}},
            ]}}}}}),
            _result("douyin", {"business_data": [{"data": {"aweme_info": {
                "aweme_id": "dy1", "desc": "有没有工具自动处理", "create_time": 1784000000,
                "share_url": "https://www.douyin.com/video/dy1", "author": {"nickname": "创作者"},
                "statistics": {"digg_count": 7, "comment_count": 2, "share_count": 1, "play_count": 90},
            }}}]}),
            _result("xiaohongshu", {"data": {"items": [{"note": {
                "id": "xhs1", "title": "求推荐", "desc": "一直用表格凑合", "type": "normal",
                "timestamp": 1784000000, "user": {"nickname": "用户"},
                "liked_count": 11, "comments_count": 5, "shared_count": 2, "collected_count": 4,
            }}]}}),
            _result("bilibili", {"result": [{
                "bvid": "BV1x", "title": "<em class=\"keyword\">AI</em> 工作流吐槽", "description": "还要手工复制",
                "author": "UP主", "pubdate": 1784000000, "play": 200, "like": 10, "review": 3,
            }]}),
            _result("zhihu", {"data": [
                {"object": None, "query_list": ["suggestion"]},
                {"object": {"id": "zh1", "type": "answer", "title": "如何解决？",
                            "excerpt": "<b>仍然</b>需要手工处理", "created_time": 1784000000,
                            "author": {"name": "回答者"}, "question": {"id": "q1", "title": "问题"},
                            "voteup_count": 13, "comment_count": 4, "favorites_count": 2}},
            ]}),
            _result("wechat_search", {"items": [{
                "docID": "wx1", "title": "小店自动化痛点", "desc": "每天复制订单",
                "source": "公众号", "timestamp": 1784000000, "doc_url": "https://mp.weixin.qq.com/s/wx1",
            }]}),
        ]

        normalized = normalize_documents([_document(results)], source_files=["round-1.json"])
        evidence = normalized["evidence"]

        self.assertEqual(len(evidence), 12)
        self.assertEqual(normalized["stats"]["valid_items"], 12)
        self.assertEqual(set(normalized["stats"]["by_source"]), {
            "tiktok", "instagram", "linkedin", "threads", "twitter", "youtube",
            "reddit", "douyin", "xiaohongshu", "bilibili", "zhihu", "wechat_search",
        })

        by_source = {item["source"]: item for item in evidence}
        self.assertEqual(by_source["instagram"]["url"], "https://www.instagram.com/p/ABC/")
        self.assertEqual(by_source["twitter"]["url"], "https://x.com/buyer/status/tw1")
        self.assertEqual(by_source["youtube"]["url"], "https://www.youtube.com/watch?v=yt1")
        self.assertEqual(by_source["reddit"]["source_item_id"], "rd1")
        self.assertEqual(by_source["xiaohongshu"]["identifiers"]["note_id"], "xhs1")
        self.assertEqual(by_source["zhihu"]["identifiers"]["answer_id"], "zh1")
        self.assertEqual(by_source["wechat_search"]["engagement"], {})
        self.assertNotIn("<", by_source["zhihu"]["original_text"])
        self.assertNotIn("<", by_source["bilibili"]["title"])
        self.assertEqual(by_source["youtube"]["date_confidence"], "low")
        self.assertEqual(by_source["douyin"]["language"], "zh")
        candidates = {item["source"]: item for item in normalized["comment_candidates"]}
        self.assertEqual(candidates["xiaohongshu"]["content_type"], "image_note")
        self.assertEqual(candidates["zhihu"]["content_type"], "answer")
        self.assertEqual(candidates["wechat_search"]["identifiers"]["url"], "https://mp.weixin.qq.com/s/wx1")
        selections = []
        for source in ("xiaohongshu", "zhihu", "wechat_search"):
            selection = dict(candidates[source])
            selection["selection_reason"] = "集成测试：标识完整且包含具体需求"
            selections.append(selection)
        comment_plan = build_comment_plan(
            as_of="2026-07-14",
            parent_search_run_id=RUN_ID,
            selections=selections,
        )
        self.assertEqual(len(comment_plan["requests"]), 6)
        self.assertTrue(any("wechat_mp" in item["endpoint"] for item in comment_plan["requests"]))
        self.assertTrue(any("get_image_note_detail" in item["endpoint"] for item in comment_plan["requests"]))
        self.assertTrue(any("fetch_answer_detail" in item["endpoint"] for item in comment_plan["requests"]))

    def test_merges_documents_deduplicates_items_and_tracks_empty_sources(self) -> None:
        tiktok = _result("tiktok", {"search_item_list": [{"aweme_info": {
            "aweme_id": "same", "desc": "same item", "share_url": "https://www.tiktok.com/t/same",
        }}]})
        empty = _result("threads", {"searchResults": {"edges": []}})
        failed = {"id": "bad", "source": "bilibili", "status": "error", "error_code": "request_error"}

        normalized = normalize_documents(
            [_document([tiktok, empty, failed]), _document([tiktok], generated_at="2026-07-14T16:05:00+08:00")],
            source_files=["a.json", "b.json"],
        )

        self.assertEqual(len(normalized["evidence"]), 1)
        self.assertEqual(normalized["stats"]["duplicates_removed"], 1)
        self.assertEqual(normalized["stats"]["source_status"]["threads"], "no-results")
        self.assertEqual(normalized["stats"]["source_status"]["bilibili"], "error")
        self.assertIn(
            {"id": "bad", "source": "bilibili", "status": "error", "error_code": "request_error"},
            normalized["request_statuses"],
        )
        self.assertEqual(normalized["source_files"], ["a.json", "b.json"])

    def test_gap_stage_preserves_candidate_context(self) -> None:
        result = _result("youtube", {"videos": [{"video_id": "yt1", "title": "Local paid invoice tool"}]})
        result["evidence_gap"] = {"candidate_id": "CAND-example", "missing_gate": "local_payment", "target_region": "JP", "expected_promotion": "b_to_a"}
        result["query_scope"] = {"country": "JP", "language": "ja"}
        normalized = normalize_documents([_document([result], stage="evidence_gap_verification")])
        self.assertEqual(normalized["stage"], "tikhub_normalized_gaps")
        self.assertEqual(normalized["evidence"][0]["evidence_gap"], result["evidence_gap"])
        self.assertEqual(normalized["evidence"][0]["query_language"], "ja")
        self.assertEqual(normalized["evidence"][0]["source_region"], "unknown")

    def test_details_keep_full_text_and_nested_comment_context(self) -> None:
        detail = {"id": "detail-1", "source": "youtube", "kind": "detail", "selected_item_id": "youtube:yt1", "parent_url": "https://www.youtube.com/watch?v=yt1", "status": "ok", "response": {"data": {"video_id": "yt1", "title": "Invoice task", "description": "完整详情 " + "manual work " * 500, "published_at": "2026-07-13T10:00:00Z"}}}
        comments = {"id": "comments-1", "source": "youtube", "kind": "top_level_comments", "selected_item_id": "youtube:yt1", "parent_url": detail["parent_url"], "status": "ok", "response": {"data": {"comments": [{"comment_id": "c1", "text": "paid customer", "replies": [{"comment_id": "c2", "text": "Correction: trial only"}]}]}}}
        normalized = normalize_documents([_document([detail, comments], stage="comment_deep_dive")])
        self.assertEqual(len(normalized["evidence"]), 1)
        evidence = normalized["evidence"][0]
        self.assertIn("manual work " * 400, evidence["original_text"])
        self.assertEqual(evidence["published_at"], "2026-07-13T10:00:00Z")
        self.assertEqual(evidence["url"], detail["parent_url"])
        reply = next(row for row in normalized["comments"] if row["source_item_id"] == "c2")
        self.assertEqual(reply["parent_comment_id"], "c1")
        self.assertEqual(reply["url"], detail["parent_url"])
        self.assertEqual(reply["url_kind"], "parent_post")
        self.assertEqual(reply["origin_id"], evidence["origin_id"])
        self.assertTrue(reply["raw_json_pointer"].endswith("/comments/0/replies/0"))

    def test_missing_comment_ids_stay_scoped_to_post_and_reply_parent(self) -> None:
        results = []
        for post in ("one", "two"):
            results.append({"id": post, "source": "youtube", "kind": "top_level_comments", "selected_item_id": "youtube:" + post, "status": "ok", "response": {"data": {"comments": [{"text": "same root text", "author": "person", "replies": [{"text": "same reply", "author": "person"}]}]}}})
        normalized = normalize_documents([_document(results, stage="comment_deep_dive")])
        self.assertEqual(len({row["id"] for row in normalized["comments"]}), 4)
        for post in ("one", "two"):
            rows = [row for row in normalized["comments"] if row["parent_item_id"] == "youtube:" + post]
            self.assertEqual(rows[1]["parent_comment_id"], rows[0]["source_item_id"])

    def test_comment_candidate_url_survives_plan_and_detail_order(self) -> None:
        search = normalize_documents([_document([_result("youtube", {"videos": [{"video_id": "yt1", "title": "Invoice task"}]})])])
        candidate = dict(search["comment_candidates"][0], selection_reason="具体任务需要补证")
        plan = build_comment_plan(as_of="2026-07-14", parent_search_run_id=RUN_ID, selections=[candidate])
        self.assertTrue(all(row["parent_url"] == candidate["parent_url"] for row in plan["requests"]))
        results = []
        for request_item in reversed(plan["requests"]):
            data = {"comments": [{"comment_id": "c1", "text": "manual"}]} if request_item["kind"] == "top_level_comments" else {"video_info": {"video_id": "yt1", "description": "full text"}}
            item = dict(request_item, status="ok", response={"data": data})
            item.pop("parent_url")
            results.append(item)
        normalized = normalize_documents([_document(results, stage="comment_deep_dive")])
        self.assertEqual(normalized["comments"][0]["url"], candidate["parent_url"])

    def test_unknown_latin_languages_are_not_assumed_english(self) -> None:
        examples = ["herramienta demasiado cara", "ferramenta muito cara", "outil trop cher", "công cụ đắt", "日本語の注文対応"]
        results = [_result("youtube", {"videos": [{"video_id": str(index), "title": value}]}, query_id=str(index)) for index, value in enumerate(examples)]
        results.append(_result("bilibili", {"result": [{"bvid": "explicit", "title": "outil cher", "language": "fr"}]}))
        normalized = normalize_documents([_document(results)])
        for item in normalized["evidence"][:-1]:
            self.assertNotEqual(item["language"], "en")
        self.assertEqual(normalized["evidence"][-1]["language"], "fr")

    def test_rejects_unsupported_schema(self) -> None:
        document = _document([])
        document["schema_version"] = "2.0"
        with self.assertRaises(ValueError):
            normalize_documents([document])

    def test_cli_writes_normalized_json(self) -> None:
        doc = _document([_result("youtube", {"videos": [{
            "video_id": "yt1", "title": "Niche pain", "description": "Details", "author": "Channel",
        }]})])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            output = root / "normalized.json"
            source.write_text(json.dumps(doc), encoding="utf-8")

            subprocess.run(
                [sys.executable, str(SCRIPTS / "normalize_tikhub_results.py"), "--input", str(source), "--output", str(output)],
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["stats"]["valid_items"], 1)
            self.assertEqual(payload["evidence"][0]["source"], "youtube")

    def test_normalizes_top_level_comments_with_parent_identity(self) -> None:
        comment_result = {
            "id": "comment-youtube-1",
            "source": "youtube",
            "kind": "top_level_comments",
            "selected_item_id": "youtube:yt1",
            "status": "ok",
            "response": {"data": {"comments": [{
                "comment_id": "c1",
                "text": "I would pay if this handled invoices.",
                "like_count": 7,
                "author": {"name": "buyer"},
            }]}},
        }
        normalized = normalize_documents([_document([comment_result], stage="comment_deep_dive")])

        self.assertEqual(normalized["stage"], "tikhub_normalized_comments")
        self.assertEqual(normalized["stats"]["valid_comments"], 1)
        self.assertEqual(normalized["comments"][0]["parent_item_id"], "youtube:yt1")


if __name__ == "__main__":
    unittest.main()
