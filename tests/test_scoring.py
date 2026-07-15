from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from score_candidates import ScoreValidationError, _read_candidates, rank_candidates, score_candidate  # noqa: E402


def candidate(name: str, value: float) -> dict:
    return {
        "id": "OPP-20260714-A1B2C3",
        "name": name,
        "track": "needle",
        "target_user": "独立开发者",
        "context": "处理重复工作时",
        "problem_or_desire": "现有工具流程过于复杂",
        "wedge": "先自动完成一个高频步骤",
        "evidence": [{"source": "hackernews", "url": "https://news.ycombinator.com/item?id=1"}],
        "scores": {
            "demand": value,
            "new_form": value,
            "distribution": value,
            "regional_gap": value,
            "monetization": value,
            "mvp_feasibility": value,
            "evidence": value,
        },
        "auxiliary_scores": {
            "first_revenue": 8,
            "scale": 7,
            "personal_influence": 5,
            "confidence": 6,
        },
    }


class ScoringTests(unittest.TestCase):
    def test_weighted_score_reaches_one_hundred(self) -> None:
        scored = score_candidate(candidate("满分机会", 10))
        self.assertEqual(scored["total_score"], 100.0)
        self.assertEqual(scored["recommendation"], "推荐")

    def test_weighted_score_matches_specification(self) -> None:
        data = candidate("混合机会", 0)
        data["scores"].update(
            demand=10,
            new_form=8,
            distribution=6,
            regional_gap=4,
            monetization=7,
            mvp_feasibility=9,
            evidence=5,
        )
        scored = score_candidate(data)
        self.assertEqual(scored["total_score"], 76.0)
        self.assertEqual(scored["recommendation"], "推荐")

    def test_track_weights_change_ranking_and_single_source_caps_confidence(self) -> None:
        data = candidate("区域机会", 5)
        data["track"] = "regional_gap"
        data["scores"]["regional_gap"] = 10
        scored = score_candidate(data)

        self.assertEqual(scored["scoring_version"], "3.0")
        self.assertEqual(scored["scoring_weights"]["regional_gap"], 2.5)
        self.assertTrue(scored["confidence_capped"])
        self.assertEqual(scored["auxiliary_scores"]["confidence"], 4.0)

    def test_rejects_missing_or_out_of_range_score(self) -> None:
        missing = candidate("缺字段", 5)
        del missing["scores"]["evidence"]
        with self.assertRaises(ScoreValidationError):
            score_candidate(missing)

        invalid = candidate("越界", 11)
        with self.assertRaises(ScoreValidationError):
            score_candidate(invalid)

    def test_rank_is_stable_for_equal_scores(self) -> None:
        ranked = rank_candidates([candidate("先出现", 7), candidate("后出现", 7), candidate("最高", 9)])
        self.assertEqual([item["name"] for item in ranked], ["最高", "先出现", "后出现"])
        self.assertEqual([item["rank"] for item in ranked], [1, 2, 3])

    def test_rejects_invalid_candidate_shapes(self) -> None:
        with self.assertRaises(ScoreValidationError):
            score_candidate([])  # type: ignore[arg-type]
        with self.assertRaises(ScoreValidationError):
            score_candidate({"scores": []})

        missing_id = candidate("缺 ID", 5)
        del missing_id["id"]
        with self.assertRaises(ScoreValidationError):
            score_candidate(missing_id)

        missing_evidence = candidate("缺证据", 5)
        missing_evidence["evidence"] = []
        with self.assertRaises(ScoreValidationError):
            score_candidate(missing_evidence)

        unknown = candidate("未知字段", 5)
        unknown["scores"]["mystery"] = 5
        with self.assertRaises(ScoreValidationError):
            score_candidate(unknown)

        invalid_aux = candidate("辅助类型", 5)
        invalid_aux["auxiliary_scores"] = []
        with self.assertRaises(ScoreValidationError):
            score_candidate(invalid_aux)

        missing_aux = candidate("辅助缺失", 5)
        del missing_aux["auxiliary_scores"]["confidence"]
        with self.assertRaises(ScoreValidationError):
            score_candidate(missing_aux)

        boolean_score = candidate("布尔分数", 5)
        boolean_score["scores"]["demand"] = True
        with self.assertRaises(ScoreValidationError):
            score_candidate(boolean_score)

    def test_low_score_is_retained_as_early_signal(self) -> None:
        scored = score_candidate(candidate("早期信号", 3))
        self.assertEqual(scored["recommendation"], "早期信号")

    def test_reads_object_array_wrapper_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            single = root / "single.json"
            single.write_text(json.dumps(candidate("单个", 5)))
            self.assertEqual(len(_read_candidates(single)), 1)

            wrapped = root / "wrapped.json"
            wrapped.write_text(json.dumps({"candidates": [candidate("包装", 5)]}))
            self.assertEqual(_read_candidates(wrapped)[0]["name"], "包装")

            array = root / "array.json"
            array.write_text(json.dumps([candidate("数组", 5)]))
            self.assertEqual(_read_candidates(array)[0]["name"], "数组")

            jsonl = root / "items.jsonl"
            jsonl.write_text("\n" + json.dumps(candidate("一", 5)) + "\n" + json.dumps(candidate("二", 6)) + "\n")
            self.assertEqual(len(_read_candidates(jsonl)), 2)

    def test_rejects_broken_or_non_object_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "broken.jsonl"
            broken.write_text("{broken\n")
            with self.assertRaises(ScoreValidationError):
                _read_candidates(broken)

            non_object_line = root / "line.jsonl"
            non_object_line.write_text("1\n2\n")
            with self.assertRaises(ScoreValidationError):
                _read_candidates(non_object_line)

            scalar = root / "scalar.json"
            scalar.write_text("123")
            with self.assertRaises(ScoreValidationError):
                _read_candidates(scalar)


if __name__ == "__main__":
    unittest.main()
