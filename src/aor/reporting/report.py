"""以结构化报告统一完整清单、聊天摘要与正式状态提交。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_result_digest import build_result_digest
from contracts import canonical_sha256, validate_record_id, validate_stage_envelope
from filter_ideas import classify_candidate

REPORT_VERSION = "1.0"
BUCKETS = (("deep_candidates", "A"), ("validated_ideas", "B"), ("regional_signals", "R"))


def report_records(tiered: dict) -> list[dict]:
    """包含 overflow，保留每个家族的全部报价变体。"""
    overflow = tiered.get("overflow") or {}
    return [row for key, _ in BUCKETS for row in [*tiered.get(key, []), *overflow.get(key, [])]]


def build_report(tiered: dict, *, decision: dict, executions: list[dict] | None = None,
                 evidence: list[dict] | None = None, profile_assessment: dict | None = None,
                 source_coverage: dict | None = None, claims: list[dict] | None = None,
                 claim_evidence: list[dict] | None = None) -> dict:
    """只组织已研究的数据，不代填市场事实或评分。"""
    digest = build_result_digest(tiered, executions=executions or [], evidence_payloads=evidence or [])
    report = {
        **validate_stage_envelope(tiered), "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tiered": deepcopy(tiered), "decision": deepcopy(decision),
        "metrics": digest["metrics"], "source_yield": digest["source_yield"],
        "source_coverage": source_coverage or {}, "claims": claims or [], "claim_evidence": claim_evidence or [],
        "profile_assessment": profile_assessment, "market_validated": False,
        "execution_results": executions or [], "digest_markdown": digest["markdown"],
    }
    validation = validate_structured_report(report)
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    return report


def validate_structured_report(report: Any) -> dict:
    """核验程序可确认的引用、等级、计数和交付字段。"""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        envelope = validate_stage_envelope(report)
        if not envelope.get("run_id") or report.get("report_version") != REPORT_VERSION:
            raise ValueError("结构化报告必须包含运行元数据及 report_version=1.0")
        tiered = report["tiered"]
        if validate_stage_envelope(tiered) != envelope:
            raise ValueError("报告与候选的运行元数据不一致")
        seen: set[str] = set()
        counts = {}
        for key, tier in BUCKETS:
            rows = [*tiered.get(key, []), *(tiered.get("overflow") or {}).get(key, [])]
            counts[tier] = len(rows)
            for row in rows:
                identifier = validate_record_id(row.get("id"), kind="signal" if tier == "R" else "opportunity")
                if identifier in seen:
                    errors.append(f"报告存在重复记录：{identifier}")
                seen.add(identifier)
                actual, _, _ = classify_candidate(row)
                if actual != tier or row.get("evidence_tier") != tier:
                    errors.append(f"{identifier} 的证据不能支持 {tier} 级")
                if tier == "A":
                    from score_candidates import score_candidate

                    scored = score_candidate(row)
                    if scored["total_score"] != row.get("total_score"):
                        errors.append(f"{identifier} 的评分与原始分项不一致")
        metrics = report.get("metrics") or {}
        for field, expected in (("deep_candidate_count", counts["A"]), ("quick_idea_count", counts["B"]),
                                ("regional_signal_count", counts["R"]), ("qualified_conclusion_count", len(seen))):
            if metrics.get(field) != expected:
                errors.append(f"报告统计不一致：{field}")
        decision = report.get("decision") or {}
        for field in ("summary", "largest_unknown", "next_action", "stop_condition"):
            if not isinstance(decision.get(field), str) or not decision[field].strip():
                errors.append(f"决策说明缺少 {field}")
        if decision.get("primary_id") and decision["primary_id"] not in seen:
            errors.append("主验证项目不在报告候选中")
        if report.get("market_validated") is not False:
            errors.append("研究报告不能自动声明自己的产品已被客户验证")
        if metrics.get("contains_demo_data"):
            warnings.append("包含演示数据，不构成真实市场结论")
        if report.get("profile_assessment") is None:
            warnings.append("尚未结合个人约束评估可执行性")
        if report.get("claims"):
            from aor.evidence.claims import validate_claims

            validate_claims(report["claims"], report.get("claim_evidence") or [], as_of=report["as_of"])
        elif seen:
            warnings.append("尚未提供独立商业主张清单，现有证据资格不代表原文语义已经自动核验")
    except (ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def render_summary(report: dict, *, full_path: str | None = None) -> str:
    decision = report["decision"]
    metrics = report["metrics"]
    lines = [f"# 机会雷达 · {report['as_of']}", "", decision["summary"], ""]
    if metrics.get("contains_demo_data"):
        lines.extend(["**演示结果：不能作为真实市场结论。**", ""])
    primary = decision.get("primary_id")
    if primary:
        lines.extend([f"主验证项目：{primary}", ""])
    lines.extend([f"最大未知项：{decision['largest_unknown']}", "",
                  f"下一步：{decision['next_action']}", "", f"停止条件：{decision['stop_condition']}", "",
                  f"研究分层：A {metrics['deep_candidate_count']} / B {metrics['quick_idea_count']} / "
                  f"R {metrics['regional_signal_count']}。A/B/R 不代表客户验证已完成。", ""])
    if report.get("profile_assessment") is None:
        lines.extend(["个人适配：尚未提供个人约束，以上研究排序不能直接视为个人立项建议。", ""])
    if full_path:
        lines.extend([f"[完整清单]({full_path})", ""])
    return "\n".join(lines)


def render_report(report: dict) -> str:
    """结构化对象是校验来源，Markdown 仅负责阅读展示。"""
    lines = [render_summary(report), "## 来源覆盖", "",
             "| 来源 | 本轮结果 |", "|---|---|"]
    for source, outcome in sorted(report.get("source_coverage", {}).items()):
        lines.append(f"| {source} | {str(outcome).replace('|', '/')} |")
    if not report.get("source_coverage"):
        lines.append("| 已有材料 | 本轮未执行实时采集 |")
    lines.extend(["", report["digest_markdown"].replace("已验证快速点子", "收费对标支持的候选")
                  .replace("B 级快速点子", "B 级收费对标支持的候选"), ""])
    return "\n".join(lines)


def commit_report(home: Path, report: dict) -> dict:
    """先核验整份报告，再幂等提交 OPP/SIG；中断后可用原报告重放。"""
    from datetime import date
    from manage_state import upsert_records

    validation = validate_structured_report(report)
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    report_hash = canonical_sha256(report)
    records = report_records(report["tiered"])
    results = {}
    for kind, prefix in (("opportunity", "OPP-"), ("signal", "SIG-")):
        rows = [{**row, "report_sha256": report_hash} for row in records if row["id"].startswith(prefix)]
        if rows:
            results[kind] = upsert_records(home, kind, rows, date.fromisoformat(report["as_of"]),
                                           run_id=report["run_id"])
    return {"run_id": report["run_id"], "report_sha256": report_hash,
            "records_sha256": canonical_sha256(records), "results": results, "status": "committed"}
