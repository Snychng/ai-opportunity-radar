"""从不可变研究报告构建全站机会目录；未出现、待复核和撤回具有不同语义。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

from contracts import canonical_sha256
from aor.reporting.public import (
    _observed_behavior, apply_freshness, evidence_publication_issue, export_public,
    public_evidence_dates, public_text, write_public_dataset,
)
from aor.reporting.public_contract import validate_public_dataset
from aor.sources.coverage import _review_status


def _run_order(report: dict) -> tuple[str, float, str]:
    return report["as_of"], datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00")).timestamp(), report["run_id"]


def _withdraw(previous: dict, run: dict) -> dict:
    """清除旧论据与论断，保留稳定 ID 和标题，让收藏与站点缓存可以明确撤回。"""
    row = deepcopy(previous)
    row.update(publication_status="withdrawn", updated_at=run["as_of"], last_source_run_id=run["run_id"],
               source_run_ids=list(dict.fromkeys([*row["source_run_ids"], run["run_id"]])),
               summary="该条内容的引用或发布复核已失效，等待重新核验。", job_to_be_done="待重新核验",
               wedge="待重新核验后再判断是否恢复", evidence_refs=[],
               missing_evidence=["需要重新完成引用核验与发布复核"], next_action="完成重新核验后再恢复发布")
    row["ai_value"] = {"status": "unknown", "baseline": "", "capability": "", "user_benefit": "",
                       "incremental_advantage": "", "evidence_refs": []}
    row["revision_id"] = row["id"] + ":withdrawn:" + canonical_sha256({k: v for k, v in row.items() if k != "revision_id"})[:16]
    return row


def build_site_catalog(reports: list[dict], *, site_id: str = "SITE-DEFAULT", as_of: str | None = None,
                       review_period_days: int = 30, due_soon_days: int = 7) -> tuple[dict, list[dict]]:
    """严格校验每次运行后，按时间与稳定实体 ID 累积公开目录。

    新运行完全没提及的旧项保留；同 ID 被隔离则撤回历史发布，不能继续展示旧
    原文。最新运行定义当前行业范围。覆盖数字取最新运行，不跨运行重复加总；
    site_manifest 单独统计全站内容。输入顺序不影响输出，冲突的同 run_id 拒绝。
    """
    if not isinstance(reports, list) or not reports:
        raise ValueError("全站目录至少需要一份已修订的结构化报告")
    if not re.fullmatch(r"SITE-[A-Za-z0-9][A-Za-z0-9_-]{0,79}", site_id):
        raise ValueError("site_id 必须使用稳定的 SITE- 标识")
    unique = {}
    exported = {}
    for report in reports:
        if not isinstance(report, dict) or report.get("report_version") != "1.1":
            raise ValueError("全站目录只接受 report_version=1.1 的已修订报告，旧档案不能混作公开数据")
        document, withheld = export_public(report, review_period_days=review_period_days, due_soon_days=due_soon_days)
        run_id = report["run_id"]
        if run_id in unique and canonical_sha256(unique[run_id]) != canonical_sha256(report):
            raise ValueError("同一 run_id 存在冲突报告，不能确定全站内容顺序")
        unique[run_id] = report
        exported[run_id] = document, withheld
    ordered = sorted(unique.values(), key=_run_order)
    latest = ordered[-1]
    latest_public = exported[latest["run_id"]][0]
    items, source_evidence, tombstones, blocked = {}, {}, {}, {}
    current_records = {}
    withheld_log = []
    runs = []
    def merge_dates(target, dates):
        # 同一正文修订的旧缓存不能回滚已知观察/复核日期，缺失字段也不抹去已有核验。
        for field in ("source_observed_at", "semantic_reviewed_at"):
            known = [value for value in (target.get(field), dates.get(field)) if value]
            target[field] = max(known) if known else None
    for report in ordered:
        public, withheld = exported[report["run_id"]]
        run = public["source_runs"][0]
        runs.append(run)
        for record in public["evidence"]:
            key = (record["id"], record["revision_id"])
            previous = source_evidence.get(key, {})
            source_evidence[key] = deepcopy(record)
            merge_dates(source_evidence[key], previous)
        # 后续运行可只复查来源，不必重复提交旧条目；新观察和语义复核各自更新，发布日保持原样。
        for record in report.get("claim_evidence", []):
            if record.get("historical_reference_only"):
                continue
            key = (record.get("evidence_id"), record.get("revision_id"))
            current_records[key] = record
            if key in source_evidence:
                merge_dates(source_evidence[key], public_evidence_dates(record, report["as_of"]))
                source_evidence[key]["role"] = public_text(record.get("evidence_role") or "unclassified")
                # 反向审阅不是对现有结论的再次确认，不能给仍保留的假设延长核验期限。
                if _review_status(record, report["as_of"]) == "unrelated":
                    source_evidence[key]["semantic_reviewed_at"] = None
        for new in public["items"]:
            row = deepcopy(new)
            prior = items.get(row["id"])
            row["source_run_ids"] = list(dict.fromkeys([*(prior or {}).get("source_run_ids", []), run["run_id"]]))
            items[row["id"]] = row
            blocked.pop(row["id"], None)
            if row["publication_status"] == "withdrawn":
                tombstones[row["id"]] = {"id": row["id"], "run_id": run["run_id"], "as_of": run["as_of"],
                                          "reason": "explicit_withdrawal"}
            else:
                tombstones.pop(row["id"], None)
        for omitted in withheld:
            record = {**omitted, "run_id": run["run_id"], "as_of": run["as_of"]}
            withheld_log.append(record)
            identifier = omitted.get("id")
            blocked[identifier] = record
            if identifier in items:
                items[identifier] = _withdraw(items[identifier], run)
                tombstones[identifier] = {"id": identifier, "run_id": run["run_id"], "as_of": run["as_of"], "reason": "withheld"}
        # 新运行即使没有再次提交旧机会，只要带来其来源的新正文／撤回状态，也必须失效旧发布。
        known_objects = set(report.get("current_evidence_state") or {}) | {
            row.get("evidence_id") for row in report.get("claim_evidence", []) if not row.get("historical_reference_only")}
        for identifier, item in list(items.items()):
            if item["publication_status"] != "published":
                continue
            issues = [evidence_publication_issue(ref["id"], ref["revision_id"], report)
                      for ref in [*item["evidence_refs"], *item["ai_value"]["evidence_refs"]] if ref["id"] in known_objects]
            reason = next((issue for issue in issues if issue), None)
            if not reason and item["stage"] == "observed_need" and not any(
                _observed_behavior(current_records.get((ref["id"], ref["revision_id"]), {}), report["as_of"])
                for ref in item["evidence_refs"]
            ):
                reason = "观察到需求的状态缺少已核验行为证据"
            if reason:
                record = {"id": identifier, "reason": reason, "run_id": run["run_id"], "as_of": run["as_of"]}
                withheld_log.append(record)
                blocked[identifier] = record
                items[identifier] = _withdraw(item, run)
                tombstones[identifier] = {"id": identifier, "run_id": run["run_id"], "as_of": run["as_of"], "reason": "withheld"}

    allowed = {row["id"]: {track["id"] for track in row["subtracks"]} for row in latest_public["industries"]}
    scope_removed = []
    for identifier, item in list(items.items()):
        industries = sorted(set(item["industry_ids"]) & allowed.keys())
        if not industries:
            scope_removed.append(identifier)
            tombstones[identifier] = {"id": identifier, "run_id": latest["run_id"], "as_of": latest["as_of"], "reason": "scope_excluded"}
            del items[identifier]
            continue
        tracks = set().union(*(allowed[key] for key in industries))
        subtracks = sorted(set(item["subtrack_ids"]) & tracks)
        if item["industry_ids"] != industries or item["subtrack_ids"] != subtracks:
            item.update(industry_ids=industries, subtrack_ids=subtracks)
            item["revision_id"] = identifier + ":scope:" + canonical_sha256({k: v for k, v in item.items() if k != "revision_id"})[:16]
    # 不从不存在的行业推断机会，也不从前一次运行的孤立引用恢复已撤回内容。
    current_items = sorted(items.values(), key=lambda row: (row["updated_at"], row["id"]), reverse=True)
    needed = {(ref["id"], ref["revision_id"]) for row in current_items
              for ref in [*row["evidence_refs"], *row["ai_value"]["evidence_refs"]]}
    try:
        evidence = [deepcopy(source_evidence[key]) for key in sorted(needed)]
    except KeyError as exc:
        raise ValueError("全站内容引用的证据修订不存在") from exc
    manifest = {"source_run_count": len(runs), "latest_run_id": latest["run_id"], "coverage_basis": "latest_research_run",
                "published_count": sum(row["publication_status"] == "published" for row in current_items),
                "withdrawn_count": sum(row["publication_status"] == "withdrawn" for row in current_items),
                "retained_from_history_count": sum(row["publication_status"] == "published" and row["last_source_run_id"] != latest["run_id"]
                                                   for row in current_items),
                "scope_removed_ids": sorted(scope_removed), "tombstones": [tombstones[key] for key in sorted(tombstones)]}
    dataset = {**deepcopy(latest_public), "snapshot_scope": "site_catalog", "dataset_id": site_id, "revision": "",
               "source_runs": runs, "site_manifest": manifest, "items": current_items, "evidence": evidence,
               "generated_at": max(runs, key=lambda row: datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00")))["generated_at"]}
    dataset["quality"]["withheld_item_count"] = len(blocked)
    dataset = apply_freshness(dataset, as_of=as_of, review_period_days=review_period_days, due_soon_days=due_soon_days)
    validation = validate_public_dataset(dataset)
    if not validation["valid"]:
        raise ValueError("全站目录契约无效：" + "；".join(validation["errors"]))
    return dataset, withheld_log


def write_site_bundle(reports: list[dict], output: Path, *, site_id: str = "SITE-DEFAULT", as_of: str | None = None,
                      review_period_days: int = 30, due_soon_days: int = 7) -> dict:
    dataset, withheld = build_site_catalog(reports, site_id=site_id, as_of=as_of,
                                          review_period_days=review_period_days, due_soon_days=due_soon_days)
    result = write_public_dataset(dataset, output, withheld=withheld)
    return {**result, "dataset_id": dataset["dataset_id"], "site_manifest": dataset["site_manifest"]}
