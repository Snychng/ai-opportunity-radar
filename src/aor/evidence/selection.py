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


def task_family_ids(item):
    """只使用明确登记的任务元数据；缺失时不从标题或原文伪造任务。"""
    from aor.sources.coverage import _origins
    families, tasks = set(), set()
    for origin in _origins(item):
        values = origin.get("task_family_ids") or []
        if isinstance(values, str):
            values = [values]
        families.update(value for value in values if isinstance(value, str) and value.strip())
        if isinstance(origin.get("task_family_id"), str) and origin["task_family_id"].strip():
            families.add(origin["task_family_id"])
        if isinstance(origin.get("task_id"), str) and origin["task_id"].strip():
            tasks.add(origin["task_id"])
    return sorted(families or tasks)


def diversified_evidence(records):
    """按明确任务族、行业、来源及角色轮询；输入顺序不改变阅读顺序。"""
    from aor.sources.coverage import BEHAVIOR_ROLES, _review_status
    grouped = defaultdict(list)
    for row in records:
        sectors = tuple(sorted(set(industry_ids(row)))) or ("unclassified",)
        families = tuple(task_family_ids(row)) or ("unknown",)
        source, role = row.get("source") or "unknown", row.get("evidence_role") or "discovery"
        role_group = ("behavior" if role in BEHAVIOR_ROLES else "commercial" if role == "official_pricing"
                      else "challenge" if role in {"alternative", "counter_evidence"} else "unknown")
        semantic = _review_status(row, str(row.get("as_of") or row.get("observed_at") or "9999-12-31")[:10])
        quality = (3 if row.get("retracted") or semantic == "unrelated" else
                   2 if row.get("relevance_status") == "unrelated" else 0 if semantic == "relevant" else 1)
        identity = str(row.get("evidence_id") or row.get("id") or row.get("url"))
        grouped[(sectors, families, source, role, role_group, quality >= 2)].append(
            (quality, identity, json.dumps(row, ensure_ascii=False, sort_keys=True, default=str), row))
    queues = {group: deque(sorted(items, key=lambda item: item[:3])) for group, items in grouped.items()}
    industries, tasks, sources, roles, role_groups = (Counter() for _ in range(5))
    while queues:
        def rank(group):
            sectors, families, source, role, role_group, unrelated = group
            quality, identity, serialized, _ = queues[group][0]
            # 供应商材料即使登记多个任务，也不能用数量淹没用户行为；真实性不靠关键词推断。
            return (unrelated, min(industries[i] for i in sectors), role_groups[role_group],
                    min(tasks[task] for task in families), sources[source], roles[role], quality, identity, serialized)
        chosen = min(queues, key=rank)
        _, _, _, row = queues[chosen].popleft()
        if not queues[chosen]:
            del queues[chosen]
        sectors, families, source, role, role_group, _ = chosen
        industries.update(sectors)
        tasks.update(families)
        sources[source] += 1
        roles[role] += 1
        role_groups[role_group] += 1
        yield row


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
               "task_id": record.get("task_id"), "task_family_ids": task_family_ids(record),
               "evidence_role": record.get("evidence_role"),
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
                          "task_family_ids": row["task_family_ids"],
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


def build_review_packets(context, *, primary_packet, as_of, run_id, max_items=15, max_chars=12000):
    """为主包之外的完整证据索引生成有界续读批次；生成文件不登记语义审阅。"""
    from aor.evidence.claims import _catalog, _future_in_tree, _instant, _visibility, build_evidence_packet
    from aor.sources.coverage import _review_status
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
        raise ValueError("续读包 max_items 必须为正整数")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("续读包 max_chars 必须为正整数")
    # 即使没有待分配材料，过小的字符预算也不能默默通过。
    build_evidence_packet([], as_of=as_of, run_id=run_id, max_items=max_items, max_chars=max_chars)
    cutoff = _instant(as_of, end_of_day=True)
    day = cutoff.date().isoformat()
    catalog = _catalog(diversified_evidence(context))
    primary = {(r["evidence_id"], r.get("revision_id")) for r in primary_packet.get("evidence", [])}
    def ref(marker):
        return {"evidence_id": marker[0], "revision_id": marker[1]}
    pending, unassigned = [], []
    for marker, record in catalog.items():
        if marker in primary:
            continue
        reason = ("historical_reference_only" if record.get("historical_reference_only") else
                  "demo" if record.get("is_demo") else "retracted" if record.get("retracted") or record.get("status") in {"retracted", "withdrawn", "deleted", "removed"} else
                  "superseded" if record.get("derivation_status") == "superseded" or record.get("status") == "superseded" else
                  _visibility(record, cutoff) or ("future" if _future_in_tree(record, cutoff) else None))
        if reason:
            unassigned.append({**ref(marker), "reason": reason})
        else:
            pending.append(record)
    packets, batches = [], []
    # 全量只排序一次；字符容量拆分仅在一个 max_items 块内进行，避免逐批重排整个上下文。
    for offset in range(0, len(pending), max_items):
        chunk = pending[offset:offset + max_items]
        while chunk:
            packet = build_evidence_packet(chunk, as_of=as_of, run_id=run_id, max_items=max_items, max_chars=max_chars)
            selected = {(r["evidence_id"], r.get("revision_id")) for r in packet["evidence"]}
            if not selected:
                unassigned.extend({**ref((r.get("evidence_id") or r["id"], r.get("revision_id"))),
                                   "reason": "single_record_exceeds_packet_budget"} for r in chunk)
                break
            batch_id = f"READ-{len(packets) + 1:04d}"
            refs = [ref((r["evidence_id"], r.get("revision_id"))) for r in packet["evidence"]]
            unreviewed = sum(_review_status(catalog[(r["evidence_id"], r.get("revision_id"))], day)
                             not in {"relevant", "unrelated"} for r in refs)
            batches.append({"batch_id": batch_id, "packet_index": len(packets), "evidence_refs": refs,
                            "unreviewed_count": unreviewed, "reason": "outside_primary_packet"})
            packets.append(packet)
            chunk = [r for r in chunk if (r.get("evidence_id") or r["id"], r.get("revision_id")) not in selected]
    manifest = {"primary_evidence_refs": [ref(marker) for marker in sorted(primary, key=lambda x: (x[0], x[1] or ""))],
                "batches": batches, "outside_primary_count": len(set(catalog) - primary),
                "unassigned_refs": unassigned,
                "next_batch_id": next((r["batch_id"] for r in batches if r["unreviewed_count"]), None),
                "packet_generation_is_review": False,
                "selection_eligibility": "as_of 可见且有观察日期的非示例、非撤回、非 superseded、非历史引用附录记录；不推断真实性或购买行为。",
                "fulltext_reference": "按 evidence_id/revision_id 从 evidence-context.json 读取完整原文；摘录仍可能截断。"}
    return {"version": "1.0", "as_of": day, "run_id": run_id, "packets": packets, "manifest": manifest}
