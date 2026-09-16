"""用户原话与需求簇；不要求收费对标、AI 方案或个人 MVP 判断。"""

from collections import Counter
from copy import deepcopy

from contracts import canonical_sha256, normalize_identity
from aor.evidence.claims import validate_claims
from aor.evidence.retrieval import resolve_evidence_reference
from aor.sources.planning import text_field
from aor.sources.industries import load_industries

FEEDBACK_TYPES = {"usage", "purchase_claim", "refund_claim", "recommendation_request",
                  "promotion", "official_response", "suspected_spam", "unknown"}
SENTIMENTS = {"positive", "negative", "mixed", "neutral", "unknown"}
DIRECT_TYPES = {"usage", "purchase_claim", "refund_claim", "recommendation_request"}


def build_user_discovery(observations: list, evidence: list, *, as_of: str, run_id: str) -> dict:
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
        if not isinstance(feedback, str) or not isinstance(sentiment, str) or feedback not in FEEDBACK_TYPES or sentiment not in SENTIMENTS:
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
        if feedback not in DIRECT_TYPES or item["is_demo"]:
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


def validate_user_discovery(value: dict, evidence: list, *, as_of: str, run_id: str) -> None:
    if not isinstance(value, dict) or value != build_user_discovery(value.get("observations"), evidence, as_of=as_of, run_id=run_id):
        raise ValueError("用户观察、需求簇或统计与原文绑定不一致")


def public_discovery_counts(value: dict) -> dict:
    """公开计数，不把尚未发布复核的原始评论和用户信息送入网站。"""
    return deepcopy(value["summary"])
