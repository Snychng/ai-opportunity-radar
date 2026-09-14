"""在容量内平衡行业、来源与证据角色；排序不替代商业语义核验。"""

from collections import Counter, defaultdict, deque
import json
import re

from aor.sources.industries import industry_ids


def diversified_evidence(records):
    """确定性贪心轮询，输入倒序不改变结果；不按采集毫秒或热度抢占容量。"""
    grouped = defaultdict(list)
    for row in records:
        industry = (industry_ids(row) or ["unclassified"])[0]
        source, role = row.get("source", "unknown"), row.get("evidence_role") or "discovery"
        text = str(row.get("title") or "") + " " + str(row.get("original_text") or "")[:1200]
        noise = bool(re.search(r"(?i)\b(?:control bus|coordination inbox|agent coordination ledger|master control|chat relay)\b", text))
        quality = 2 if row.get("relevance_status") == "unrelated" or row.get("retracted") else int(noise)
        key = (quality, 0 if role in {"payment", "official_pricing", "counter_evidence", "product_review"} else 1,
               str(row.get("evidence_id") or row.get("id") or row.get("url")),
               json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
        grouped[(industry, source, role)].append((key, row))
    queues = {group: deque(sorted(items, key=lambda pair: pair[0])) for group, items in grouped.items()}
    industries, sources, roles = Counter(), Counter(), Counter()
    while queues:
        def rank(group):
            industry, source, role = group
            priority = queues[group][0][0]
            return (priority[0], industries[industry], sources[source], roles[role], *priority[1:])
        group = min(queues, key=rank)
        _, chosen = queues[group].popleft()
        if not queues[group]:
            del queues[group]
        industries[group[0]] += 1
        sources[group[1]] += 1
        roles[group[2]] += 1
        yield chosen


def evidence_index(context, packet):
    selected = {(r["evidence_id"], r.get("revision_id")) for r in packet["evidence"]}
    return {"as_of": packet["as_of"], "run_id": packet["run_id"],
            "note": "完整索引不受阅读包字符上限裁剪；可按 evidence_id 从 evidence-context.json 读取遗漏原文。",
            "items": [{"evidence_id": r.get("evidence_id") or r.get("id"), "revision_id": r.get("revision_id"),
                       "title": str(r.get("title") or "")[:200], "url": r.get("url"), "source": r.get("source"),
                       "industry_ids": industry_ids(r), "evidence_role": r.get("evidence_role"),
                       "selected": (r.get("evidence_id") or r.get("id"), r.get("revision_id")) in selected}
                      for r in diversified_evidence(context)]}
