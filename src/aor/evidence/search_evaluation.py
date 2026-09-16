"""协作搜索的描述性统计；未核验和未标注始终保留未知。"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from aor.evidence.identity import canonical_sha256, evidence_identity_key
from aor.evidence.retrieval import resolve_evidence_reference
from aor.sources.search_candidates import merge_search_candidates, normalize_search_candidates, x_post_id


def summarize_search_results(receipts: list[dict[str, Any]], candidates: list[dict[str, Any]], *,
                             evidence: list[dict[str, Any]] | None = None,
                             verifications: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """回执不提供核验权威；可选的核验引用必须解析到独立给定的固定证据修订。

    verifications 每项为 candidate_id、evidence_ref；后者必须含 evidence_id、
    revision_id、quote。即使解析成功，统计也只表示宿主提供了可核对原文，
    不能当作独立作者、有效任务观察、付款或商业机会数量。
    """
    if not isinstance(receipts, list) or len(receipts) > 5000 or any(not isinstance(row, dict) for row in receipts):
        raise ValueError("receipts 必须是不超过 5000 项的对象数组")
    rows = merge_search_candidates(candidates)
    catalog = {row["candidate_id"]: row for row in rows}
    statuses = Counter(str(row.get("status") or "unknown") for row in receipts)
    by_provider: dict[str, set[str]] = defaultdict(set)
    by_query: dict[tuple[str, str], set[str]] = defaultdict(set)
    citation_count = 0
    unlocated_count = 0
    safe_citation_count = 0
    for receipt in receipts:
        citations = receipt.get("citations", [])
        normalized = normalize_search_candidates(receipt)
        citation_count += len(citations)
        for row in normalized:
            safe_citation_count += len(row["discoveries"])
            unlocated_count += sum(item["citation"]["range_status"] != "answer_span" for item in row["discoveries"])
        provider = str(receipt.get("provider") or "unknown")
        query = str(receipt.get("query_key") or receipt.get("query") or "unrecorded")
        ids = {row["candidate_id"] for row in normalized}
        by_provider[provider].update(ids)
        by_query[provider, query].update(ids)
    verified: dict[str, set[str]] | None = None
    if verifications is not None:
        if evidence is None or not isinstance(evidence, list):
            raise ValueError("核验统计需要独立 evidence 数组，不读取回执中的自报原文")
        if not isinstance(verifications, list) or len(verifications) > 5000:
            raise ValueError("verifications 必须是不超过 5000 项的数组")
        verified = defaultdict(set)
        for check in verifications:
            if not isinstance(check, dict) or set(check) != {"candidate_id", "evidence_ref"}:
                raise ValueError("核验项只接受 candidate_id 和 evidence_ref")
            candidate = catalog.get(check["candidate_id"])
            if candidate is None:
                raise ValueError("核验候选不在本次候选全集")
            ref = check["evidence_ref"]
            if not isinstance(ref, dict) or not all(isinstance(ref.get(field), str) and ref[field].strip()
                                                   for field in ("evidence_id", "revision_id", "quote")):
                raise ValueError("核验必须绑定 evidence_id、revision_id 和原文 quote")
            record = resolve_evidence_reference(ref, evidence, require_revision=True)
            if record.get("retracted") or not any(isinstance(record.get(key), str) and record[key].strip()
                                                  for key in ("original_text", "text")):
                raise ValueError("核验引用需要未撤回的发布者正文")
            verification = record.get("verification") or {}
            if (not isinstance(verification, dict) or verification.get("method") not in {"opened_page", "authorized_browser"}
                    or verification.get("status") != "host_attested"):
                raise ValueError("原文核验统计需要 sources import 生成的宿主页面核验记录")
            if candidate["source"] == "twitter":
                # 同帖仍可能有 post/comment 两种已存在的适配器对象类型；候选不决定回复类型。
                matched = record.get("source") == "twitter" and candidate["source_object_id"] == x_post_id(
                    record.get("original_url") or record.get("url"))
            else:
                matched = candidate["url"] == evidence_identity_key(record)
            if not matched:
                raise ValueError("核验原文不属于所引用的候选对象")
            verified[candidate["candidate_id"]].add(canonical_sha256([record["evidence_id"], record["revision_id"]]))
    providers = {}
    for provider, ids in sorted(by_provider.items()):
        others = set().union(*(values for name, values in by_provider.items() if name != provider))
        providers[provider] = {"unique_candidate_count": len(ids),
            "exclusive_candidate_count": len(ids - others),
            "overlap_candidate_count": len(ids & others),
            "host_verified_candidate_count": len(ids & set(verified)) if verified is not None else None,
            "exclusive_host_verified_candidate_count": len((ids - others) & set(verified)) if verified is not None else None}
    calls = []
    for receipt in receipts:
        usage = receipt.get("usage")
        details = usage.get("server_side_tool_usage_details") if isinstance(usage, dict) else None
        count = details.get("x_search_calls") if isinstance(details, dict) else None
        calls.append(count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None)
    return {"schema_version": "search-evaluation-1", "receipt_count": len(receipts),
        "status_counts": dict(sorted(statuses.items())), "citation_count": citation_count,
        "safe_located_citation_count": safe_citation_count - unlocated_count,
        "safe_unlocated_citation_count": unlocated_count,
        "unusable_or_duplicate_citation_count": citation_count - safe_citation_count,
        "unique_candidate_count": len(rows), "host_verified_candidate_count": len(verified) if verified is not None else None,
        "effective_observation_count": None, "independent_author_count": None,
        "reported_x_search_calls": sum(count for count in calls if count is not None),
        "receipts_without_x_search_usage": sum(count is None for count in calls),
        "providers": providers,
        "queries": [{"provider": provider, "query_key_or_text": query, "unique_candidate_count": len(ids)}
                    for (provider, query), ids in sorted(by_query.items())],
        "limitations": ["检索独有候选不等于独有有效需求；多个模型找到同帖不增加来源数。",
                        "原文核验数来自独立导入证据的固定修订引用，宿主核验仍是声明。",
                        "未提供核验引用时保持未知；有效观察、作者独立性和商业成立需后续语义审阅。",
                        "服务端调用数仅累加已报告 usage，缺失 usage 不视为零调用或零费用。"]}
