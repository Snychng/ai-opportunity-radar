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
                 claim_evidence: list[dict] | None = None, run_ledger: dict | None = None,
                 research_plan: dict | None = None, evidence_packet: dict | None = None) -> dict:
    """只组织已研究的数据，不代填市场事实或评分。"""
    inventory = [{**{key: payload[key] for key in ("schema_version", "run_id", "as_of", "reused_for_run_id") if key in payload},
                  "evidence": [{key: row[key] for key in ("id", "url", "original_url", "source", "industry_ids", "query_metadata",
                               "intent_refs", "request_ids", "query_id", "relevance_status", "verification", "is_demo", "retracted", "status") if key in row}
                               for row in [*payload.get("evidence", []), *payload.get("comments", [])]],
                  "requests": deepcopy(payload.get("requests", []))} for payload in evidence or []]
    from aor.sources.coverage import build_industry_coverage, research_quality
    coverage_plan = deepcopy(research_plan or {})
    coverage = build_industry_coverage(coverage_plan, [*inventory, *(executions or [])], tiered)
    selection = {"omitted": deepcopy((evidence_packet or {}).get("omitted", {}))}
    digest = build_result_digest(tiered, executions=executions or [], evidence_payloads=inventory, run_ledger=run_ledger)
    report = {
        **validate_stage_envelope(tiered), "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tiered": deepcopy(tiered), "decision": deepcopy(decision),
        "metrics": digest["metrics"], "source_yield": digest["source_yield"],
        "source_coverage": source_coverage or {}, "claims": claims or [], "claim_evidence": claim_evidence or [],
        "profile_assessment": profile_assessment, "market_validated": False,
        "execution_results": executions or [], "evidence_inventory": inventory, "run_ledger": run_ledger,
        "coverage_plan": coverage_plan, "industry_coverage": coverage, "evidence_selection": selection,
        "research_quality": research_quality(coverage, selection, tiered),
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
        if "industry_coverage" in report:
            from aor.sources.coverage import build_industry_coverage, research_quality
            actual_coverage = build_industry_coverage(report.get("coverage_plan") or {},
                    [*(report.get("evidence_inventory") or []), *(report.get("execution_results") or [])], tiered)
            if report["industry_coverage"] != actual_coverage:
                errors.append("行业覆盖与实际请求及证据不一致")
            if report.get("research_quality") != research_quality(actual_coverage, report.get("evidence_selection") or {}, tiered):
                errors.append("研究覆盖结论与结构化输入不一致")
        from aor.opportunity.exploration import exploration_lead
        lead_ids = set()
        for lead in tiered.get("research_leads", []):
            checked = exploration_lead(lead, lead.get("missing_requirements", []), require_ai=True)
            if checked is None or checked["lead_id"] != lead.get("lead_id") or lead.get("market_validated") is not False:
                errors.append("探索线索缺少原文依据、AI 价值假设或合法身份")
            if lead.get("lead_id") in lead_ids:
                errors.append("探索线索重复")
            lead_ids.add(lead.get("lead_id"))
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
        expected_digest = build_result_digest(tiered, executions=report.get("execution_results") or [],
                                              evidence_payloads=report.get("evidence_inventory") or [],
                                              run_ledger=report.get("run_ledger"))
        # 旧 1.0 报告没有探索线索字段；仅允许缺省的零值新增指标，保留原报告哈希。
        if "research_leads" not in tiered and "industry_coverage" not in report:
            for field in ("research_lead_count", "lead_used_normalized_evidence_count"):
                if field not in metrics and expected_digest["metrics"][field] == 0:
                    expected_digest["metrics"].pop(field)
        if metrics != expected_digest["metrics"] or report.get("source_yield") != expected_digest["source_yield"]:
            errors.append("报告统计或费用与原始结构化输入不一致")
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
    lines.extend([f"待验证线索：{metrics.get('research_lead_count', 0)} 条；与正式候选分开展示。", ""])
    quality = report.get("research_quality") or {}
    if quality.get("coverage_incomplete"):
        lines.extend([f"覆盖缺口：{len(quality['industries_needing_review'])} 个方向尚需核验用户原文；不能据此判断没有机会。", ""])
    if report.get("profile_assessment") is None:
        lines.extend(["个人适配：尚未完成时间、技能、渠道与预算的综合评估，以上研究排序不能直接视为立项建议。", ""])
    if full_path:
        lines.extend([f"[完整清单]({full_path})", ""])
    return "\n".join(lines)


def render_report(report: dict) -> str:
    """结构化对象是校验来源，Markdown 仅负责阅读展示。"""
    lines = [render_summary(report), render_industries(report), "## 来源覆盖", "",
             "| 来源 | 本轮结果 |", "|---|---|"]
    for source, outcome in sorted(report.get("source_coverage", {}).items()):
        lines.append(f"| {source} | {str(outcome).replace('|', '/')} |")
    if not report.get("source_coverage"):
        lines.append("| 已有材料 | 本轮未执行实时采集 |")
    digest = build_result_digest(report["tiered"], executions=report.get("execution_results") or [],
                                 evidence_payloads=report.get("evidence_inventory") or [], run_ledger=report.get("run_ledger"))
    lines.extend(["", digest["markdown"].replace("已验证快速点子", "收费对标支持的候选")
                  .replace("B 级快速点子", "B 级收费对标支持的候选"), ""])
    if report.get("run_ledger"):
        ledger = report["run_ledger"]
        lines.extend(["## 本轮付费账本", "", f"实际发起 {ledger['attempts']} 次 HTTP 尝试；"
                      f"原价占用 {ledger['list_attempted_cost_usd_exact']} USD，"
                      f"估计费用 {ledger['estimated_attempted_cost_usd_exact']} USD。中断与未知结果仍占用预算，金额尚未经供应商账单对账。", ""])
    lines.extend(["## 商业主张与原文依据", "", "下列支持程度由宿主判断；程序只核验引用及版本，不自动证明商业语义。", ""])
    for claim in report.get("claims", []):
        lines.extend([f"- **{claim['id']}** · {claim['verification_status']}：{claim['statement']}"])
        for ref in claim.get("evidence_refs", []):
            lines.append(f"  - {ref['evidence_id']} / {ref.get('revision_id') or '旧版'}：{ref['quote']}")
    if not report.get("claims"):
        lines.append("尚未提供商业主张清单。")
    for lead in report["tiered"].get("research_leads", []):
        value = lead["ai_value"]
        lines.extend(["", f"## {lead['lead_id']} · AI 增量对比", "",
                      f"- 原有办法：{_safe_text(value['baseline'])}",
                      f"- AI 能力：{_safe_text(value['capability'])}",
                      f"- 用户收益：{_safe_text(value['user_benefit'])}",
                      f"- 增量判断：{_safe_text(value['incremental_advantage'])}",
                      f"- 状态：{'待验证假设' if value['status'] == 'hypothesis' else '宿主标记为有证据支持，仍需核验'}"])
        for item in lead.get("evidence", []):
            if item.get("fact"):
                lines.append(f"- 材料含义：{_safe_text(item['fact'])}")
    for candidate in report_records(report["tiered"]):
        if candidate["evidence_tier"] != "A":
            continue
        lines.extend(["", f"## {candidate['id']} · {candidate['total_score']} 分", "",
                      "| 维度 | 分数 | 判断依据 |", "|---|---:|---|"])
        for field, score in {**candidate["scores"], **candidate["auxiliary_scores"]}.items():
            basis = candidate.get("score_basis", {}).get(field, {})
            rationale = str(basis.get("rationale") or "待补判断依据").replace("|", "/").replace("\n", " ")
            refs = ", ".join(ref["evidence_id"] for ref in basis.get("evidence_refs", []))
            lines.append(f"| {field} | {score} | {rationale}{'；' + refs if refs else '；未绑定证据引用'} |")
    lines.append("")
    return "\n".join(lines)


def _safe_text(value: str) -> str:
    return str(value).replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def render_industries(report: dict) -> str:
    labels = {"reviewed_evidence": "已核验材料", "related_material": "已取得相关材料，待核验",
              "needs_relevance_review": "已有材料，相关性待核验", "collection_failed": "采集失败",
              "no_results": "本次检索无结果", "not_collected": "已规划，尚未采集", "not_scheduled": "本轮未安排", "partial": "部分采集失败"}
    lines = ["## 行业全景", "", "| 方向 | 本轮调查 | 请求 | 相关/已核验材料 | 正式候选 | 待验证线索 |", "|---|---|---:|---:|---:|---:|"]
    for row in (report.get("industry_coverage") or {}).get("industries", []):
        name = row["name"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {name} | {labels[row['status']]} | {row['request_count']} | "
                     f"{row['related_evidence_count']}/{row['verified_evidence_count']} | {row['qualified_count']} | {row['lead_count']} |")
    lines.extend(["", "相关材料不等于需求成立；未调查和无结果不等于没有市场机会。", ""])
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
    from aor.opportunity.exploration import record_leads
    if report["tiered"].get("research_leads"):
        record_leads(home, report["tiered"]["research_leads"], run_id=report["run_id"])
    results = {}
    for kind, prefix in (("opportunity", "OPP-"), ("signal", "SIG-")):
        rows = [{**row, "report_sha256": report_hash} for row in records if row["id"].startswith(prefix)]
        if rows:
            results[kind] = upsert_records(home, kind, rows, date.fromisoformat(report["as_of"]),
                                           run_id=report["run_id"])
    return {"run_id": report["run_id"], "report_sha256": report_hash,
            "records_sha256": canonical_sha256(records), "results": results, "status": "committed"}
