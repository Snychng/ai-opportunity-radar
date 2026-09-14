"""研究档案到网站数据的单向白名单导出；不把审核通过解释为市场已验证。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import fcntl
import ipaddress
import os
import re
from pathlib import Path
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from contracts import canonical_sha256
from aor.reporting.public_contract import validate_public_dataset

PUBLIC_VERSION = "1.0.0"


def public_url(value: str) -> str:
    parts = urlsplit(str(value or ""))
    host = parts.hostname or ""
    if parts.scheme not in {"https", "http"} or not host or parts.username or parts.password:
        raise ValueError("引用缺少公开网页地址")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("本机地址不可公开")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("私有地址不可公开")
    except ValueError as exc:
        if "私有" in str(exc):
            raise
    query = [(k, v) for k, v in parse_qsl(parts.query) if k in {"id", "v", "p", "story_fbid", "fbid"}]
    if re.search(r"(?:token|secret|signature|cache)/", parts.path, re.I):
        raise ValueError("临时响应地址不可作为公开引用")
    return urlunsplit(("https", parts.netloc, parts.path, urlencode(query), ""))


def public_text(value, *, limit: int = 4000) -> str:
    """去除链接凭证、联系方式和本机路径；纯文本供网站按 textContent 渲染。"""
    text = str(value or "")
    text = re.sub(r"(?:/Users/|/home/|/tmp/|/private/|/var/folders/|[A-Za-z]:\\Users\\)[^\s\]）)<>]+", "[本地路径已移除]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[联系方式已移除]", text)
    text = re.sub(r"(?<![A-Za-z0-9])(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)", "[联系方式已移除]", text)
    text = re.sub(r"(?i)(?:微信|wechat|whatsapp|电话|手机|联系电话|vx|v信)\s*[:：号]?\s*[A-Za-z0-9_+ -]{5,}", "[联系方式已移除]", text)
    text = re.sub(r"(?i)(?:phone|mobile|tel(?:ephone)?)\s*[:：]?\s*\+?[\d() -]{7,}", "[联系方式已移除]", text)
    def link(match):
        try:
            return public_url(match.group())
        except ValueError:
            return "[链接已移除]"
    text = re.sub(r"https?://[^\s<>\]）)]+", link, text)
    return text[:limit].strip()


def review_content_hash(row: dict) -> str:
    ignored = {"publication_review", "run_id", "as_of", "revision_id", "revision", "lead_version", "previous_revision_id"}
    return canonical_sha256({k: v for k, v in row.items() if k not in ignored})


def _reviewed(row: dict, as_of: str) -> bool:
    review = row.get("publication_review") or {}
    try:
        return (review.get("status") == "approved" and bool(review.get("reviewer"))
                and bool(review.get("rationale")) and date.fromisoformat(review["reviewed_at"][:10]) <= date.fromisoformat(as_of)
                and review.get("content_sha256") == review_content_hash(row))
    except (ValueError, KeyError, TypeError):
        return False


def _review_date_valid(review: dict, as_of: str) -> bool:
    try:
        return bool(isinstance(review, dict) and str(review.get("reviewer") or "").strip()
                    and date.fromisoformat(str(review["reviewed_at"])[:10]) <= date.fromisoformat(as_of))
    except (ValueError, KeyError, TypeError):
        return False


def _observed_behavior(stored: dict, as_of: str) -> bool:
    from aor.sources.coverage import BEHAVIOR_ROLES, _review_status
    return stored.get("evidence_role") in BEHAVIOR_ROLES and _review_status(stored, as_of) == "relevant"


def evidence_publication_issue(evidence_id: str, revision_id: str, report: dict) -> str | None:
    """历史引文可供审计；网站发布还必须确认该对象的当前状态与正文版本。"""
    from aor.opportunity.exploration import current_reference_issue
    from aor.reporting.report import evidence_current_state
    states = report.get("current_evidence_state")
    if not isinstance(states, dict):
        states = evidence_current_state(report.get("claim_evidence", []))
    return current_reference_issue(evidence_id, revision_id, states)


def _public_day(value, as_of: str) -> str | None:
    try:
        day = date.fromisoformat(str(value)[:10]).isoformat()
        return day if day <= as_of else None
    except (ValueError, TypeError):
        return None


def public_evidence_dates(record: dict, as_of: str) -> dict:
    """仅接受真实来源观察与绑定当前修订的语义复核，发布批准不充当页面重抓。"""
    from aor.sources.coverage import _review_status
    review = record.get("relevance_review") or {}
    semantic = review.get("reviewed_at") if _review_status(record, as_of) in {"relevant", "unrelated"} else None
    return {"source_observed_at": _public_day(record.get("last_observed_at") or record.get("observed_at"), as_of),
            "semantic_reviewed_at": _public_day(semantic, as_of)}


def apply_freshness(dataset: dict, *, as_of: str | None = None, review_period_days: int = 30,
                    due_soon_days: int = 7) -> dict:
    """在明确截止日计算复核期限，保留过期历史项及其业务身份。"""
    if (type(review_period_days) is not int or not 1 <= review_period_days <= 3650
            or type(due_soon_days) is not int or not 0 <= due_soon_days < review_period_days):
        raise ValueError("复核周期须为 1–3650 天，到期提醒天数须非负且小于周期")
    day = date.fromisoformat(as_of or dataset["as_of"]).isoformat()
    if day < dataset["as_of"]:
        raise ValueError("复核截止日不能早于输入研究报告")
    result = deepcopy(dataset)
    result["as_of"] = day
    result["freshness_policy"] = {"review_period_days": review_period_days, "due_soon_days": due_soon_days}
    catalog = {(row["id"], row["revision_id"]): row for row in result.get("evidence", [])}
    for row in result["items"]:
        refs = [*row["evidence_refs"], *row["ai_value"]["evidence_refs"]]
        records = [catalog.get((ref["id"], ref["revision_id"]), {}) for ref in refs]
        def oldest(field):
            values = [_public_day(record.get(field), day) for record in records]
            return min(values) if values and all(values) else None
        source, semantic = oldest("source_observed_at"), oldest("semantic_reviewed_at")
        verified = min(source, semantic) if source and semantic else None
        due = (date.fromisoformat(verified) + timedelta(days=review_period_days)).isoformat() if verified else None
        status = ("unknown" if due is None else "overdue" if day > due else
                  "due" if day >= (date.fromisoformat(due) - timedelta(days=due_soon_days)).isoformat() else "fresh")
        row["freshness"] = {"as_of": day, "status": status, "last_verified_at": verified, "review_due_at": due,
                            "source_observed_at": source, "semantic_reviewed_at": semantic,
                            "publication_checked_at": _public_day((row.get("freshness") or {}).get("publication_checked_at"), day)}
        # 网站按条目修订更新缓存，日期状态变化也必须有新公开修订；业务 ID 仍稳定。
        row["revision_id"] = row["id"] + ":public:" + canonical_sha256({k: v for k, v in row.items() if k != "revision_id"})[:16]
    result["revision"] = ""
    result["revision"] = canonical_sha256(result)[:16]
    return result


def build_refresh_tasks(dataset: dict) -> list[dict]:
    """返回私有执行清单；只给公开来源 URL 与固定修订，不自行请求网络或恢复归档。"""
    catalog = {(row["id"], row["revision_id"]): row for row in dataset.get("evidence", [])}
    tasks = []
    for row in dataset["items"]:
        freshness = row.get("freshness") or {}
        if row["publication_status"] != "published" or freshness.get("status", "unknown") == "fresh":
            continue
        refs = {(ref["id"], ref["revision_id"]): ref for ref in [*row["evidence_refs"], *row["ai_value"]["evidence_refs"]]}
        tasks.append({"task_id": "REFRESH-" + canonical_sha256([row["id"], dataset["as_of"], sorted(refs)])[:16],
                      "item_id": row["id"], "action": "refresh_source_and_semantic_review", "execution_status": "not_started",
                      "reason": freshness.get("status", "unknown"), "as_of": dataset["as_of"],
                      "review_due_at": freshness.get("review_due_at"),
                      "evidence_refs": [{"evidence_id": key[0], "revision_id": key[1], "url": catalog[key]["url"]}
                                        for key in sorted(refs) if key in catalog],
                      "steps": ["重新访问原始来源并保存观察时间、原文与修订", "对当前修订填写 evidence_reviews",
                                "如引用或结论变化，在新研究运行修订条目并重新完成 publication_reviews"],
                      "expected_outputs": ["normalized_evidence", "evidence_reviews", "revised_report_if_changed"]})
    return tasks


def export_public(report: dict, *, as_of: str | None = None, review_period_days: int = 30,
                  due_soon_days: int = 7) -> tuple[dict, list[dict]]:
    """返回可公开数据及仅供内部使用的隔离原因；不自动批准任何线索。"""
    from aor.evidence.retrieval import resolve_evidence_reference
    from aor.opportunity.exploration import meaningful_ai_value, validate_lead_fields
    from aor.reporting.report import report_records, validate_structured_report
    from aor.sources.industries import load_industries

    validation = validate_structured_report(report)
    if not validation["valid"]:
        raise ValueError("无法导出无效报告：" + "；".join(validation["errors"]))
    if report.get("report_version") != "1.1":
        raise ValueError("旧报告须在新研究运行中修订引用后导出；不得只改版本字段")
    plan = report.get("coverage_plan") or {}
    catalog = plan.get("industry_catalog") or load_industries()
    if isinstance(catalog, dict):
        catalog = catalog.get("industries", [])
    selected = set(plan.get("selected_industries") or [r["id"] for r in catalog])
    industries = [{"id": r["id"], "name": public_text(r["name"], limit=200),
                   "subtracks": [{"id": s["id"], "name": public_text(s.get("name") or s["id"], limit=200)}
                                 for s in r.get("subtracks", [])]}
                  for r in catalog if r["id"] in selected]
    allowed = {r["id"] for r in industries}
    evidence_catalog = report.get("claim_evidence") or []
    items, evidence, withheld = [], {}, []
    rows = [*report_records(report["tiered"]), *report["tiered"].get("research_leads", [])]
    origins = {row["promoted_to"]: row["lead_id"] for row in report["tiered"].get("research_leads", [])
               if row.get("promoted_to") and row.get("lead_id")}
    for row in rows:
        identifier = row.get("lead_id") or row.get("id")
        try:
            validate_lead_fields(row, allowed_industries=allowed)
            withdrawn = row.get("research_status") in {"archived", "disproven"} or row.get("retracted") is True
            if not withdrawn and not _reviewed(row, report["as_of"]):
                raise ValueError("尚未完成针对当前内容的发布复核")
            ai = row.get("ai_value") or {}
            if not withdrawn and not meaningful_ai_value(ai):
                raise ValueError("AI 增量假设或支持证据不完整")
            refs, ai_refs, source_records, bound_records = [], [], [], []
            if not withdrawn:
                citations = [(ref, refs) for ref in row.get("evidence", [])]
                if ai.get("status") == "supported":
                    if not _review_date_valid(ai.get("review") or {}, report["as_of"]):
                        raise ValueError("AI 支持结论缺少有效的复核日期与人员")
                    citations += [(ref, ai_refs) for ref in ai.get("evidence_refs", [])]
                for ref, destination_refs in citations:
                    if not ref.get("evidence_id") or not ref.get("revision_id"):
                        raise ValueError("发布引用必须固定证据与修订 ID")
                    stored = resolve_evidence_reference(ref, evidence_catalog, require_revision=True)
                    current_issue = evidence_publication_issue(stored["evidence_id"], stored["revision_id"], report)
                    if current_issue:
                        raise ValueError(current_issue)
                    if stored.get("retracted") or stored.get("status") in {"retracted", "withdrawn"} or stored.get("is_demo"):
                        raise ValueError("引用已撤回或为演示数据")
                    original = stored.get("original_text") or stored.get("text") or ""
                    quote = ref.get("quote") or ref.get("original_text") or ref.get("text") or ""
                    if not isinstance(quote, str) or not quote.strip() or quote.strip() not in original:
                        raise ValueError("引用摘录与原文不一致")
                    quote = public_text(quote.strip(), limit=600)
                    if not quote or quote == "[联系方式已移除]":
                        raise ValueError("引用没有可公开的有效摘录")
                    key = (stored["evidence_id"], stored["revision_id"])
                    destination_refs.append({"id": key[0], "revision_id": key[1], "quote": quote})
                    if destination_refs is refs:
                        bound_records.append(stored)
                    source_records.append((key, {"id": key[0], "revision_id": key[1], "source": public_text(stored.get("source") or "web"),
                        "url": public_url(stored.get("original_url") or stored["url"]),
                        "title": public_text(stored.get("title"), limit=300), "excerpt": quote,
                        "role": public_text(stored.get("evidence_role") or "unclassified"),
                        "observed_at": str(stored.get("observed_at") or report["as_of"]),
                        "published_at": stored.get("published_at"), "date_confidence": str(stored.get("date_confidence") or "unknown"),
                        **public_evidence_dates(stored, report["as_of"])}))
                if not refs:
                    raise ValueError("发布内容没有有效引用")
            tier = row.get("evidence_tier")
            stage = ("regional_hypothesis" if tier == "R" else "candidate" if tier else
                     "observed_need" if row.get("research_status") == "observed_need" else "hypothesis")
            if not withdrawn and stage == "observed_need" and not any(_observed_behavior(e, report["as_of"]) for e in bound_records):
                raise ValueError("观察到需求的状态缺少已核验行为证据")
            item = {"id": identifier, "revision_id": str(identifier) + ":" + review_content_hash(row)[:16],
                    "title": public_text(row["title"], limit=300), "summary": public_text(row["problem_or_desire"]),
                    "industry_ids": sorted(set(row["industry_ids"])), "subtrack_ids": sorted(set(row.get("subtrack_ids", []))),
                    "target_user": public_text(row["target_user"]), "job_to_be_done": public_text(row["problem_or_desire"]),
                    "wedge": public_text(row["wedge"]), "stage": stage, "evidence_tier": tier,
                    "ai_value": {**{k: public_text(ai.get(k)) for k in ("status", "baseline", "capability", "user_benefit", "incremental_advantage")},
                                 "evidence_refs": ai_refs},
                    "evidence_refs": refs, "missing_evidence": [public_text(v) for v in row.get("missing_requirements", [])],
                    "next_action": public_text(row.get("next_question") or "补齐引用和关键市场证据，再判断是否值得验证"),
                    "publication_status": "withdrawn" if withdrawn else "published", "updated_at": report["as_of"],
                    "freshness": {"publication_checked_at": _public_day((row.get("publication_review") or {}).get("reviewed_at"), report["as_of"])},
                    "source_run_ids": [report["run_id"]], "last_source_run_id": report["run_id"],
                    "origin_lead_id": row.get("origin_lead_id") or origins.get(identifier), "promoted_to": row.get("promoted_to")}
            if withdrawn:
                item["ai_value"]["status"] = "disproven" if row.get("research_status") == "disproven" else "unknown"
            items.append(item)
            evidence.update(source_records)
        except (ValueError, KeyError, TypeError) as exc:
            withheld.append({"id": identifier, "reason": str(exc)})
    coverage = []
    for row in (report.get("industry_coverage") or {}).get("industries", []):
        if row["industry_id"] not in allowed:
            continue
        coverage.append({"industry_id": row["industry_id"], "status": row["status"],
                         "material_count": row["material_count"], "reviewed_count": row.get("semantic_reviewed_count", 0),
                         "historical_material_count": row.get("historical_evidence_count", 0),
                         "recent_demand_count": row.get("recent_user_behavior_count", 0),
                         "commercial_evidence_count": row.get("commercial_benchmark_count", 0),
                         "counterevidence_count": row.get("counter_evidence_count", 0),
                         "unreviewed_count": row.get("unreviewed_evidence_count", row["material_count"]),
                         "missing_dimensions": [public_text(v) for v in row.get("review_gaps", [])], "exhaustive": False})
    dataset = {"contract_version": PUBLIC_VERSION, "view": "full", "dataset_id": report["run_id"], "revision": "",
               "snapshot_scope": "research_run", "source_runs": [{"run_id": report["run_id"], "as_of": report["as_of"],
                  "generated_at": report["generated_at"], "report_sha256": canonical_sha256(report)}], "site_manifest": None,
               "as_of": report["as_of"], "generated_at": report["generated_at"], "taxonomy_version": str(plan.get("catalog_version") or "1.0"),
               "industries": industries, "items": items, "evidence": list(evidence.values()), "coverage": coverage,
               "quality": {"coverage_incomplete": bool((report.get("research_quality") or {}).get("coverage_incomplete", True)),
                           "omitted_evidence_count": (report.get("research_quality") or {}).get("omitted_evidence_count", 0),
                           "withheld_item_count": len(withheld), "market_validated": False}, "extensions": {}}
    quality = report.get("research_quality") or {}
    if quality.get("review_scope") == "active_context":
        dataset["quality"]["review_summary"] = {"source_run_id": report["run_id"],
            "evidence_count": quality["overall_evidence_count"], "reviewed_count": quality["reviewed_evidence_count"],
            "unreviewed_count": quality["unreviewed_evidence_count"]}
    dataset = apply_freshness(dataset, as_of=as_of, review_period_days=review_period_days, due_soon_days=due_soon_days)
    result = validate_public_dataset(dataset)
    if not result["valid"]:
        raise ValueError("公开契约验证失败：" + "；".join(result["errors"]))
    return dataset, withheld


def write_public_bundle(report: dict, output: Path, *, as_of: str | None = None, review_period_days: int = 30,
                        due_soon_days: int = 7) -> dict:
    """单个研究运行公开导出；全站累积使用 site.write_site_bundle。"""
    dataset, withheld = export_public(report, as_of=as_of, review_period_days=review_period_days, due_soon_days=due_soon_days)
    return write_public_dataset(dataset, output, withheld=withheld)


def write_public_dataset(dataset: dict, output: Path, *, withheld: list[dict] | None = None) -> dict:
    """完整生成并校验后原子切换公开目录链接，当前目录不残留上次详情。

    站点仅可托管 output 指向的目录。相邻隐藏目录保存本地旧代用于回滚，不能
    将它或 output 的父目录发布。已有非空普通目录拒绝覆盖，保护无关内容。
    """
    from aor_runtime import atomic_json
    validation = validate_public_dataset(dataset)
    if not validation["valid"] or dataset.get("view") != "full":
        raise ValueError("发布输入必须是已通过公开契约校验的 full 视图")
    dataset = deepcopy(dataset)
    withheld = deepcopy(withheld or [])
    output = Path(os.path.abspath(output))
    listing = deepcopy(dataset)
    listing.pop("evidence")
    listing["items"] = [{k: row[k] for k in ("id", "revision_id", "title", "summary", "industry_ids", "stage", "evidence_tier", "publication_status", "source_run_ids", "last_source_run_id", "freshness") if k in row}
                        for row in dataset["items"]]
    listing["view"] = "index"
    bundles = {"public.v1.json": dataset, "index.json": listing}
    for item in dataset["items"]:
        keys = {(r["id"], r["revision_id"]) for r in [*item["evidence_refs"], *item["ai_value"]["evidence_refs"]]}
        detail = {**dataset, "view": "detail", "items": [item],
                  "evidence": [r for r in dataset["evidence"] if (r["id"], r["revision_id"]) in keys]}
        bundles[f"items/{item['id']}.json"] = detail
    for document in bundles.values():
        validation = validate_public_dataset(document)
        if not validation["valid"]:
            raise ValueError("公开视图校验失败：" + "；".join(validation["errors"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    releases = output.parent / f".{output.name}-releases"
    releases.mkdir(exist_ok=True)
    with (releases / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if output.is_symlink():
            if output.resolve().parent != releases.resolve():
                raise ValueError("输出路径不是本导出器管理的发布链接，拒绝覆盖")
        elif output.exists():
            if not output.is_dir() or any(output.iterdir()):
                raise ValueError("输出目录已存在且非空，请选择新的专用公开目录")
        stage = Path(tempfile.mkdtemp(prefix=f"{dataset['revision']}-", dir=releases))
        for relative, document in bundles.items():
            (stage / relative).parent.mkdir(parents=True, exist_ok=True)
            atomic_json(stage / relative, document)
        temporary_link = releases / f"publish-{stage.name}"
        try:
            temporary_link.symlink_to(stage, target_is_directory=True)
            if output.exists() and not output.is_symlink():
                output.rmdir()  # 仅移除上面已确认的空目录。
            os.replace(temporary_link, output)
        finally:
            temporary_link.unlink(missing_ok=True)
    return {"path": str(output / "public.v1.json"), "index": str(output / "index.json"),
            "published_count": sum(item["publication_status"] == "published" for item in dataset["items"]),
            "withdrawn_count": sum(item["publication_status"] == "withdrawn" for item in dataset["items"]),
            "withheld": withheld, "refresh_tasks": build_refresh_tasks(dataset),
            "contract_version": PUBLIC_VERSION, "snapshot_scope": dataset["snapshot_scope"]}
