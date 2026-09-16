"""用户原话与需求簇；不要求收费对标、AI 方案或个人 MVP 判断。"""

from collections import Counter
from copy import deepcopy

from contracts import canonical_sha256, normalize_identity
from aor.evidence.claims import validate_claims
from aor.evidence.retrieval import resolve_evidence_reference
from aor.sources.planning import text_field
from aor.sources.industries import load_industries

LEGACY_FEEDBACK_TYPES = {"usage", "purchase_claim", "refund_claim", "recommendation_request",
                         "promotion", "official_response", "suspected_spam", "unknown"}
FEEDBACK_TYPES = LEGACY_FEEDBACK_TYPES | {"positive_behavior"}
SENTIMENTS = {"positive", "negative", "mixed", "neutral", "unknown"}
LEGACY_DIRECT_TYPES = {"usage", "purchase_claim", "refund_claim", "recommendation_request"}
DIRECT_TYPES = LEGACY_DIRECT_TYPES | {"positive_behavior"}
BEHAVIOR_TYPES = {"problem_solving", "collecting", "commemorating", "sharing", "creating",
                  "completing", "mastery", "unknown"}
POSITIVE_BEHAVIORS = BEHAVIOR_TYPES - {"unknown", "problem_solving"}
DELIVERY_FORMS = {"one_off_delivery", "human_assisted_service", "plugin", "studio_tool",
                  "subscription_software", "other"}
TASK_CONTEXT_FIELDS = ("trigger", "current_workaround", "desired_outcome", "artifact")


def _build_legacy_discovery(observations: list, evidence: list, *, as_of: str, run_id: str) -> dict:
    """核验原文绑定并按宿主提供的规范需求归组，不用词面猜真实性。"""
    if not isinstance(observations, list) or len(observations) > 2000:
        raise ValueError("observations 必须是最多 2000 条的数组；更多材料分批研究")
    known = {r["id"] for r in load_industries()}
    normalized, clusters = {}, {}
    for raw in observations:
        if not isinstance(raw, dict):
            raise ValueError("用户观察必须为对象")
        item = {k: text_field(raw.get(k), k, 500) for k in ("product", "target_user", "task", "need")}
        industries = raw.get("industry_ids")
        if not isinstance(industries, list) or not industries or not all(isinstance(v, str) and v in known for v in industries):
            raise ValueError("用户观察需要有效 industry_ids")
        feedback, sentiment = raw.get("feedback_type", "unknown"), raw.get("sentiment", "unknown")
        if not isinstance(feedback, str) or not isinstance(sentiment, str) or feedback not in LEGACY_FEEDBACK_TYPES or sentiment not in SENTIMENTS:
            raise ValueError("用户观察 feedback_type 或 sentiment 不合法")
        refs = raw.get("evidence_refs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 20:
            raise ValueError("用户观察需要 1–20 条固定修订的原文引用")
        checked = validate_claims([{"id": "observation", "statement": item["need"],
                    "verification_status": "unverified", "evidence_refs": refs}], evidence, as_of=as_of)[0]
        source_rows = [resolve_evidence_reference(ref, evidence, require_revision=True) for ref in checked["evidence_refs"]]
        if any(r.get("retracted") or r.get("status") in {"withdrawn", "superseded", "not_current", "removed", "deleted"}
               or r.get("derivation_status") in {"superseded", "needs_review"} for r in source_rows):
            raise ValueError("用户观察不能引用已撤回或失效的原文")
        group = {k: normalize_identity(item[k]) for k in ("product", "target_user", "task", "need")}
        cluster_id = "NEED-" + canonical_sha256(group)[:16].upper()
        canonical_refs = sorted(checked["evidence_refs"], key=lambda r: (r["evidence_id"], r["revision_id"], r["quote"]))
        identifier = "OBS-" + canonical_sha256({"cluster": cluster_id, "refs": canonical_refs})[:16].upper()
        item.update(observation_id=identifier, cluster_id=cluster_id, industry_ids=sorted(set(industries)),
                    feedback_type=feedback, sentiment=sentiment, evidence_refs=canonical_refs,
                    sources=sorted({str(r.get("source") or "unknown") for r in source_rows}),
                    is_demo=any(r.get("is_demo") is True for r in source_rows),
                    classification_status="host_classified", authenticity="not_independently_verified",
                    market_validated=False)
        if identifier in normalized:
            if normalized[identifier] != item:
                raise ValueError("相同原话与需求存在冲突分类，请合并后提交")
            continue
        normalized[identifier] = item
        # 推广、官方回复和未知材料保留在原话层，不冒充直接用户需求。
        if feedback not in LEGACY_DIRECT_TYPES or item["is_demo"]:
            continue
        cluster = clusters.setdefault(cluster_id, {"cluster_id": cluster_id, **{k: item[k] for k in group},
                    "industry_ids": [], "observation_ids": [], "evidence_ids": [], "sources": [],
                    "status": "needs_verification", "market_validated": False, "independent_user_count": None})
        cluster["observation_ids"].append(identifier)
        for key, values in (("industry_ids", item["industry_ids"]), ("evidence_ids", [r["evidence_id"] for r in canonical_refs]),
                            ("sources", item["sources"])):
            cluster[key] = sorted(set(cluster[key]) | set(values))
    rows = sorted(normalized.values(), key=lambda r: r["observation_id"])
    groups = sorted(clusters.values(), key=lambda r: r["cluster_id"])
    for group in groups:
        group["observation_ids"].sort()
    return {"version": "1.0", "run_id": run_id, "as_of": as_of, "observations": rows, "demand_clusters": groups,
            "summary": {"observation_count": len(rows), "demand_cluster_count": len(groups),
                        "unique_evidence_count": len({ref["evidence_id"] for r in rows for ref in r["evidence_refs"]}),
                        "feedback_types": dict(sorted(Counter(r["feedback_type"] for r in rows).items())),
                        "market_validated": False}}


def _text_list(value, field: str, *, count: int, limit: int) -> list:
    if not isinstance(value, list) or len(value) > count:
        raise ValueError(f"{field} 必须是最多 {count} 项的文本数组")
    return sorted({text_field(item, field, limit) for item in value})


def _solution_hypotheses(value) -> list:
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("solution_hypotheses 必须是最多 10 项的数组")
    proposals = {}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"delivery_form", "statement", "status"}:
            raise ValueError("解决方案假设只包含 delivery_form、statement 和 status")
        form = raw["delivery_form"]
        if not isinstance(form, str) or form not in DELIVERY_FORMS or raw["status"] != "hypothesis":
            raise ValueError("交付形态必须有效，解决方案始终保留 hypothesis 状态")
        proposal = {"delivery_form": form, "statement": text_field(raw["statement"], "statement", 500),
                    "status": "hypothesis"}
        proposals[canonical_sha256(proposal)] = proposal
    return [proposals[key] for key in sorted(proposals)]


def build_user_discovery(observations: list, evidence: list, *, as_of: str, run_id: str,
                         version: str = "2.0") -> dict:
    """任务事实先于产品假设；固定旧版身份，跨产品归组新版需求。"""
    if version == "1.0":
        return _build_legacy_discovery(observations, evidence, as_of=as_of, run_id=run_id)
    if version != "2.0":
        raise ValueError("不支持的 user_discovery version")
    if not isinstance(observations, list) or len(observations) > 2000:
        raise ValueError("observations 必须是最多 2000 条的数组；更多材料分批研究")
    known = {row["id"] for row in load_industries()}
    normalized = {}
    for raw in observations:
        if not isinstance(raw, dict):
            raise ValueError("用户观察必须为对象")
        item = {key: text_field(raw.get(key), key, 500) for key in ("target_user", "task", "need")}
        for key in TASK_CONTEXT_FIELDS:
            if raw.get(key) is not None:
                item[key] = text_field(raw[key], key, 500)
        products = _text_list(raw.get("products", []), "products", count=20, limit=500)
        if raw.get("product") is not None:
            products.append(text_field(raw["product"], "product", 500))
        item["products"] = _text_list(products, "products", count=21, limit=500)
        if len(item["products"]) > 20:
            raise ValueError("products 最多 20 项")
        item["constraints"] = _text_list(raw.get("constraints", []), "constraints", count=10, limit=200)
        item["query_terms"] = _text_list(raw.get("query_terms", []), "query_terms", count=10, limit=100)
        item["solution_hypotheses"] = _solution_hypotheses(raw.get("solution_hypotheses", []))
        for key, choices, default in (("language", {"en", "zh", "unknown"}, "unknown"),
                                      ("behavior_type", BEHAVIOR_TYPES, "unknown"),
                                      ("feedback_type", FEEDBACK_TYPES, "unknown"),
                                      ("sentiment", SENTIMENTS, "unknown")):
            value = raw.get(key, default)
            if not isinstance(value, str) or value not in choices:
                raise ValueError(f"用户观察 {key} 不合法")
            item[key] = value
        industries = raw.get("industry_ids")
        if (not isinstance(industries, list) or not 1 <= len(industries) <= 10
                or not all(isinstance(value, str) and value in known for value in industries)):
            raise ValueError("用户观察需要 1–10 项有效 industry_ids")
        refs = raw.get("evidence_refs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 20:
            raise ValueError("用户观察需要 1–20 条固定修订的原文引用")
        if any(not isinstance(ref, dict) or any(not isinstance(ref.get(key), str) or not ref[key].strip()
                for key in ("evidence_id", "revision_id", "quote")) for ref in refs):
            raise ValueError("任务观察必须显式绑定 evidence_id、revision_id 和 quote")
        checked = validate_claims([{"id": "observation", "statement": item["need"],
                                  "verification_status": "unverified", "evidence_refs": refs}],
                                  evidence, as_of=as_of)[0]
        source_rows = [resolve_evidence_reference(ref, evidence, require_revision=True)
                       for ref in checked["evidence_refs"]]
        if any(row.get("retracted") or row.get("historical_reference_only") or row.get("status") in
               {"retracted", "withdrawn", "superseded", "not_current", "removed", "deleted", "ambiguous", "needs_review"}
               or row.get("derivation_status") in {"superseded", "needs_review"} for row in source_rows):
            raise ValueError("用户观察不能引用已撤回或失效的原文")
        family = {key: normalize_identity(item[key]) for key in ("target_user", "task")}
        family["constraints"] = sorted({normalize_identity(value) for value in item["constraints"]})
        family_id = "TASK-" + canonical_sha256(family)[:16].upper()
        cluster_id = "NEED-" + canonical_sha256({**family, "need": normalize_identity(item["need"])})[:16].upper()
        canonical_refs = sorted(checked["evidence_refs"],
                                key=lambda ref: (ref["evidence_id"], ref["revision_id"], ref["quote"]))
        identifier = "OBS-" + canonical_sha256({"cluster": cluster_id, "refs": canonical_refs})[:16].upper()
        item.update(observation_id=identifier, cluster_id=cluster_id, task_family_id=family_id,
                    industry_ids=sorted(set(industries)), evidence_refs=canonical_refs,
                    sources=sorted({str(row.get("source") or "unknown") for row in source_rows}),
                    is_demo=any(row.get("is_demo") is True for row in source_rows),
                    classification_status="host_classified", authenticity="not_independently_verified",
                    market_validated=False)
        if identifier in normalized:
            previous = normalized[identifier]
            merge_keys = {"products", "industry_ids", "query_terms", "solution_hypotheses"}
            if {key: value for key, value in previous.items() if key not in merge_keys} != {
                    key: value for key, value in item.items() if key not in merge_keys}:
                raise ValueError("相同原话与需求存在冲突分类，请合并后提交")
            for key in merge_keys - {"solution_hypotheses"}:
                item[key] = sorted(set(previous[key]) | set(item[key]))
            item["solution_hypotheses"] = _solution_hypotheses(
                _unique_hypotheses(previous["solution_hypotheses"] + item["solution_hypotheses"]))
            if len(item["products"]) > 20 or len(item["query_terms"]) > 10 or len(item["industry_ids"]) > 10:
                raise ValueError("合并后的产品、关键词或行业超出观察上限")
        normalized[identifier] = item
    rows = sorted(normalized.values(), key=lambda row: row["observation_id"])
    clusters = {}
    direct = [row for row in rows if row["feedback_type"] in DIRECT_TYPES and not row["is_demo"]]
    for item in direct:
        cluster = clusters.setdefault(item["cluster_id"], {
            "cluster_id": item["cluster_id"], "task_family_id": item["task_family_id"],
            **{key: item[key] for key in ("target_user", "task", "need", "constraints")},
            "products": [], "industry_ids": [], "observation_ids": [], "evidence_ids": [], "sources": [],
            "status": "needs_verification", "market_validated": False, "independent_user_count": None})
        for key, values in (("products", item["products"]), ("industry_ids", item["industry_ids"]),
                            ("observation_ids", [item["observation_id"]]), ("sources", item["sources"]),
                            ("evidence_ids", [ref["evidence_id"] for ref in item["evidence_refs"]])):
            cluster[key] = sorted(set(cluster[key]) | set(values))
    groups = sorted(clusters.values(), key=lambda row: row["cluster_id"])
    hypotheses = _unique_hypotheses([proposal for row in direct for proposal in row["solution_hypotheses"]])
    return {"version": "2.0", "run_id": run_id, "as_of": as_of, "observations": rows,
            "demand_clusters": groups, "summary": {
                "observation_count": len(rows), "demand_cluster_count": len(groups),
                "task_family_count": len({row["task_family_id"] for row in groups}),
                "unique_evidence_count": len({ref["evidence_id"] for row in rows for ref in row["evidence_refs"]}),
                "feedback_types": dict(sorted(Counter(row["feedback_type"] for row in rows).items())),
                "positive_behavior_count": sum(row["feedback_type"] == "positive_behavior"
                    or row["behavior_type"] in POSITIVE_BEHAVIORS for row in direct),
                "behavior_types": dict(sorted(Counter(row["behavior_type"] for row in direct).items())),
                "artifact_observation_count": sum("artifact" in row for row in direct),
                "artifact_count": len({normalize_identity(row["artifact"]) for row in direct if "artifact" in row}),
                "delivery_hypothesis_count": len(hypotheses),
                "delivery_forms": dict(sorted(Counter(row["delivery_form"] for row in hypotheses).items())),
                "market_validated": False}}


def _unique_hypotheses(values: list) -> list:
    by_hash = {canonical_sha256(value): value for value in values}
    return [by_hash[key] for key in sorted(by_hash)]


def validate_user_discovery(value: dict, evidence: list, *, as_of: str, run_id: str) -> None:
    if not isinstance(value, dict) or value != build_user_discovery(value.get("observations"), evidence,
            as_of=as_of, run_id=run_id, version=value.get("version")):
        raise ValueError("用户观察、需求簇或统计与原文绑定不一致")


def public_discovery_counts(value: dict) -> dict:
    """公开计数，不把尚未发布复核的原始评论和用户信息送入网站。"""
    summary = value["summary"]
    counts = {key: count for key, count in summary.items() if key in {
        "observation_count", "demand_cluster_count", "task_family_count", "unique_evidence_count",
        "positive_behavior_count", "artifact_count", "artifact_observation_count", "delivery_hypothesis_count"}
        and isinstance(count, int) and not isinstance(count, bool) and count >= 0}
    for key, allowed in (("feedback_types", FEEDBACK_TYPES), ("behavior_types", BEHAVIOR_TYPES),
                          ("delivery_forms", DELIVERY_FORMS)):
        if isinstance(summary.get(key), dict):
            counts[key] = {name: count for name, count in summary[key].items() if name in allowed
                           and isinstance(count, int) and not isinstance(count, bool) and count >= 0}
    counts["market_validated"] = False
    return deepcopy(counts)
