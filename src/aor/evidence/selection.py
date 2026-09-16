"""按行业、来源与证据角色分配阅读容量；索引和审阅队列保留未读材料。"""

from collections import Counter, defaultdict, deque
from copy import deepcopy
import json

from aor.sources.industries import industry_ids


def compact_packet_metadata(item):
    """保留引用文本与位置；完整追溯信息通过 ID/修订从完整证据上下文读取。"""
    result = deepcopy(item)
    for field in ("raw_file", "raw_ref", "local_ref", "raw_json_pointer", "same_source_refs", "aliases", "legacy_references",
                  "run_ids", "query_metadata", "provenance", "retrieval", "author", "original_publisher"):
        result.pop(field, None)
    if isinstance(result.get("title"), str):
        result["title"] = result["title"][:200]
    refs = result.get("raw_refs")
    if isinstance(refs, list):
        result["raw_refs"] = [{key: ref[key] for key in ("path", "json_pointer") if key in ref}
                              for ref in refs[:1] if isinstance(ref, dict)]
        if len(refs) > 1:
            result["additional_raw_reference_count"] = len(refs) - 1
    for field in ("canonical_url", "original_url"):
        if result.get(field) == result.get("url"):
            result.pop(field, None)
    for field in ("first_observed_at", "last_observed_at"):
        if result.get(field) == result.get("observed_at"):
            result.pop(field, None)
    if result.get("id") == result.get("evidence_id"):
        result.pop("id", None)
    return result


def diversified_evidence(records):
    """确定性轮询；优先语义已审相关材料，但不按单个行业或热度抢占全包。"""
    grouped = defaultdict(list)
    for row in records:
        industry = (industry_ids(row) or ["unclassified"])[0]
        source, role = row.get("source", "unknown"), row.get("evidence_role") or "discovery"
        from aor.sources.coverage import _review_status
        semantic = _review_status(row, str(row.get("as_of") or row.get("observed_at") or "9999-12-31")[:10])
        quality = (3 if row.get("retracted") or semantic == "unrelated" else
                   2 if row.get("relevance_status") == "unrelated" else 0 if semantic == "relevant" else 1)
        key = (quality, 0 if role in {"payment", "official_pricing", "counter_evidence", "alternative"} else 1,
               str(row.get("evidence_id") or row.get("id") or row.get("url")),
               json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
        grouped[(industry, source, role)].append((key, row))
    queues = {group: deque(sorted(items, key=lambda pair: pair[0])) for group, items in grouped.items()}
    industries, sources, roles = Counter(), Counter(), Counter()
    while queues:
        def rank(group):
            industry, source, role = group
            priority = queues[group][0][0]
            # 负相关和撤回记录始终靠后；在其余材料中优先保证行业分配，再考虑审阅状态。
            return (priority[0] >= 2, industries[industry], sources[source], roles[role], *priority[:3])
        group = min(queues, key=rank)
        _, chosen = queues[group].popleft()
        if not queues[group]:
            del queues[group]
        industries[group[0]] += 1
        sources[group[1]] += 1
        roles[group[2]] += 1
        yield chosen


def evidence_index(context, packet):
    from aor.sources.coverage import _review_status
    selected = {(r["evidence_id"], r.get("revision_id")) for r in packet["evidence"]}
    items, queue = [], []
    for record in diversified_evidence(context):
        identifier = record.get("evidence_id") or record.get("id")
        revision = record.get("revision_id")
        semantic = _review_status(record, str(packet["as_of"])[:10])
        included = (identifier, revision) in selected
        row = {"evidence_id": identifier, "revision_id": revision, "title": str(record.get("title") or "")[:200],
               "url": record.get("url"), "source": record.get("source"), "industry_ids": industry_ids(record),
               "task_id": record.get("task_id"), "evidence_role": record.get("evidence_role"),
               "published_at": record.get("published_at"), "window_status": record.get("window_status", "unknown"),
               "semantic_relevance_status": semantic, "selected": included,
               "reviewed": semantic in {"relevant", "unrelated"}, "retracted": bool(record.get("retracted"))}
        items.append(row)
        if not row["reviewed"] and not row["retracted"] and not record.get("is_demo") and record.get("derivation_status") != "superseded" and record.get("status") != "superseded":
            questions = ["是否对应目标用户的具体任务；引用是否支持所述事实"]
            if row["window_status"] in {"unknown", "uncertain"}:
                questions.append("核验发布时间；暂不当作近期需求")
            if row["evidence_role"] in {"official_pricing", "product_review"}:
                questions.append("补用户行为与反证；产品页面不证明实际购买或使用")
            queue.append({"evidence_id": identifier, "revision_id": revision, "industry_ids": row["industry_ids"],
                          "source": row["source"], "evidence_role": row["evidence_role"],
                          "reason": "selected_not_reviewed" if included else "outside_primary_packet",
                          "questions": questions})
    return {"version": "2.0", "as_of": packet["as_of"], "run_id": packet["run_id"],
            "note": "selected 只表示已装入阅读包，不表示已审阅；按 ID 与修订从 evidence-context.json 读取完整原文。",
            "items": items, "review_queue": queue, "unreviewed_count": len(queue),
            "selected_unreviewed_count": sum(item["reason"] == "selected_not_reviewed" for item in queue)}


def build_industry_packets(context, *, as_of, run_id, selected_industries, max_items=10, max_chars=9000):
    """生成可按缺口继续阅读的行业包；不把生成文件算作已经审阅。"""
    from aor.evidence.claims import build_evidence_packet
    records = list(context)
    return {identifier: build_evidence_packet([r for r in records if identifier in industry_ids(r)],
                    as_of=as_of, run_id=run_id, max_items=max_items, max_chars=max_chars)
            for identifier in selected_industries}
