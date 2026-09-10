from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from tests.test_idea_funnel import benchmark
from expand_ideas import expand_ideas
from score_candidates import ScoreValidationError, _read_candidates, rank_candidates, score_candidate  # noqa: E402


def candidate(name: str, value: float) -> dict:
    return {
        **expand_ideas({"benchmarks": [benchmark()]})["candidates"][0],
        "id": "OPP-20260714-A1B2C3",
        "name": name,
        "track": "needle",
        "target_user": "独立开发者",
        "context": "处理重复工作时",
        "problem_or_desire": "现有工具流程过于复杂",
        "wedge": "先自动完成一个高频步骤",
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

    def test_track_weights_change_ranking(self) -> None:
        data = candidate("区域机会", 5)
        data["track"] = "regional_gap"
        data["scores"]["regional_gap"] = 10
        scored = score_candidate(data)

        self.assertEqual(scored["scoring_version"], "3.0")
        self.assertEqual(scored["scoring_weights"]["regional_gap"], 2.5)
        self.assertFalse(scored["confidence_capped"])
        self.assertEqual(scored["auxiliary_scores"]["confidence"], 6.0)

    def test_rechecks_a_eligibility_before_scoring(self):
        for field, value in (("payer", ""), ("payment_signals", []), ("mvp_days", 100),
                             ("evidence_tier", "R"), ("evidence", [{"url": "https://one.example/1"}])):
            item = candidate("不合格满分", 10)
            item[field] = value
            with self.subTest(field=field), self.assertRaises(ScoreValidationError):
                score_candidate(item)

    def test_rejects_invalid_scoring_envelope(self):
        item = candidate("错误运行", 10)
        item["schema_version"] = "2.0"
        with self.assertRaises(ScoreValidationError):
            score_candidate(item)

    def test_score_counts_only_active_sources_with_support_text(self):
        item = candidate("有效证据", 8)
        item["evidence"].extend([{"url": "https://bare.example/1"},
                                 {"url": "https://old.example/1", "text": "已经撤回", "retracted": True}])
        self.assertEqual(score_candidate(item)["independent_source_count"], 2)

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

    def test_wrapper_run_is_inherited_and_conflicts_are_rejected(self):
        envelope = {"schema_version": "3.0", "run_id": "RUN-20260910-ABCDEF1234", "as_of": "2026-09-10"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrapped.json"
            path.write_text(json.dumps({**envelope, "candidates": [candidate("匹配", 8)]}))
            self.assertEqual(_read_candidates(path)[0]["run_id"], envelope["run_id"])
            other = candidate("不同运行", 8)
            other.update(run_id="RUN-20260909-ABCDEF1234", as_of="2026-09-09")
            for payload in ({**envelope, "candidates": [other]},
                            {"schema_version": "2.0", "candidates": []},
                            {**envelope, "candidates": [42]}):
                path.write_text(json.dumps(payload))
                with self.subTest(payload=payload), self.assertRaises(ScoreValidationError):
                    _read_candidates(path)

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
