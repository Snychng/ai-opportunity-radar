#!/usr/bin/env python3
"""按机会轨道分别计算 V3 A 级候选分数，并执行证据置信度约束。"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from contracts import SCORING_VERSION, ContractError, evidence_independent_sources, validate_record_id, validate_stage_envelope
from filter_ideas import classify_candidate, qualifying_evidence

WEIGHTS_BY_TRACK: dict[str, dict[str, float]] = {
    "needle": {
        "demand": 2.5, "new_form": 0.5, "distribution": 1.5, "regional_gap": 0.5,
        "monetization": 1.5, "mvp_feasibility": 2.0, "evidence": 1.5,
    },
    "new_form": {
        "demand": 2.0, "new_form": 2.5, "distribution": 1.5, "regional_gap": 0.5,
        "monetization": 1.0, "mvp_feasibility": 1.5, "evidence": 1.0,
    },
    "regional_gap": {
        "demand": 2.0, "new_form": 0.5, "distribution": 1.5, "regional_gap": 2.5,
        "monetization": 1.5, "mvp_feasibility": 1.0, "evidence": 1.0,
    },
}
SCORE_FIELDS = tuple(next(iter(WEIGHTS_BY_TRACK.values())))
AUXILIARY_FIELDS = ("first_revenue", "scale", "personal_influence", "confidence")
IDENTITY_FIELDS = ("target_user", "context", "problem_or_desire", "wedge")


class ScoreValidationError(ValueError):
    """候选评分不符合契约。"""


def _validate_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreValidationError(f"{field} 必须是 0 到 10 的数字")
    number = float(value)
    if not 0 <= number <= 10:
        raise ScoreValidationError(f"{field} 超出 0 到 10：{value}")
    return number


def score_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """验证并计算单个候选，不改变输入对象。"""
    if not isinstance(candidate, dict):
        raise ScoreValidationError("候选必须是 JSON 对象")
    try:
        validate_stage_envelope(candidate)
        validate_record_id(candidate.get("id"), kind="opportunity")
    except ContractError as exc:
        raise ScoreValidationError(f"候选必须先通过 manage_state.py prepare 分配 ID：{exc}") from exc
    track = candidate.get("track")
    if track not in WEIGHTS_BY_TRACK:
        raise ScoreValidationError(f"track 必须是：{', '.join(WEIGHTS_BY_TRACK)}")
    missing_identity = [field for field in IDENTITY_FIELDS if not str(candidate.get(field) or "").strip()]
    if missing_identity:
        raise ScoreValidationError(f"候选缺少身份字段：{', '.join(missing_identity)}")
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ScoreValidationError("候选必须包含非空 evidence 数组")
    tier, reasons, gates = classify_candidate(candidate)
    if tier != "A" or candidate.get("evidence_tier", "A") != "A":
        raise ScoreValidationError(f"只有重新核验为 A 级的候选可以评分：{', '.join(reasons) or tier or '证据不足'}")
    weights = WEIGHTS_BY_TRACK[track]
    scores = candidate.get("scores")
    if not isinstance(scores, dict):
        raise ScoreValidationError("候选缺少 scores 对象")
    missing = [field for field in SCORE_FIELDS if field not in scores]
    if missing:
        raise ScoreValidationError(f"缺少主评分字段：{', '.join(missing)}")
    unknown = sorted(set(scores) - set(SCORE_FIELDS))
    if unknown:
        raise ScoreValidationError(f"未知主评分字段：{', '.join(unknown)}")

    normalized_scores = {field: _validate_number(scores[field], field) for field in SCORE_FIELDS}
    auxiliary = candidate.get("auxiliary_scores", {})
    if not isinstance(auxiliary, dict):
        raise ScoreValidationError("auxiliary_scores 必须是对象")
    missing_aux = [field for field in AUXILIARY_FIELDS if field not in auxiliary]
    if missing_aux:
        raise ScoreValidationError(f"缺少辅助评分字段：{', '.join(missing_aux)}")
    normalized_aux = {field: _validate_number(auxiliary[field], field) for field in AUXILIARY_FIELDS}
    independent_source_count = len(evidence_independent_sources(qualifying_evidence(candidate)))

    total = round(sum(normalized_scores[field] * weights[field] for field in SCORE_FIELDS), 1)
    if total >= 75:
        recommendation = "推荐"
    elif total >= 60:
        recommendation = "重点观察"
    else:
        recommendation = "早期信号"

    result = deepcopy(candidate)
    result["evidence_tier"] = "A"
    result["hard_gates"] = gates
    result["scores"] = normalized_scores
    result["auxiliary_scores"] = normalized_aux
    result["total_score"] = total
    result["recommendation"] = recommendation
    result["scoring_version"] = SCORING_VERSION
    result["scoring_track"] = track
    result["scoring_weights"] = deepcopy(weights)
    result["independent_source_count"] = independent_source_count
    result["confidence_capped"] = False  # 保留输出兼容字段；单来源已在 A 级资格校验中拒绝。
    return result


def rank_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """稳定排序；同分时保留原始顺序。"""
    scored = [score_candidate(candidate) for candidate in candidates]
    ranked = sorted(scored, key=lambda item: -item["total_score"])
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def _read_candidates(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        candidates: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScoreValidationError(f"JSONL 第 {line_number} 行损坏：{exc}") from exc
            if not isinstance(value, dict):
                raise ScoreValidationError(f"JSONL 第 {line_number} 行必须是对象")
            candidates.append(value)
        return candidates

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("candidates"), list):
        try:
            envelope = validate_stage_envelope(data)
        except ContractError as exc:
            raise ScoreValidationError(str(exc)) from exc
        candidates = []
        for candidate in data["candidates"]:
            if not isinstance(candidate, dict):
                raise ScoreValidationError("candidates 中每项必须是对象")
            try:
                item_envelope = validate_stage_envelope(candidate)
            except ContractError as exc:
                raise ScoreValidationError(str(exc)) from exc
            for field in ("run_id", "as_of"):
                if field in envelope and field in item_envelope and envelope[field] != item_envelope[field]:
                    raise ScoreValidationError(f"候选 {field} 与包装运行不一致")
            candidates.append({**envelope, **candidate})
        return candidates
    if isinstance(data, dict):
        return [data]
    raise ScoreValidationError("输入必须是候选对象、对象数组或 JSONL")


def main() -> int:  # pragma: no cover - 由 tests/test_e2e.py 通过独立子进程覆盖
    parser = argparse.ArgumentParser(description="计算并排序 AI 创业机会候选")
    parser.add_argument("--input", type=Path, required=True, help="JSON 或 JSONL 候选文件")
    parser.add_argument("--output", type=Path, help="可选输出文件")
    args = parser.parse_args()
    try:
        ranked = rank_candidates(_read_candidates(args.input))
    except (OSError, ScoreValidationError) as exc:
        parser.error(str(exc))
    payload = json.dumps(ranked, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
