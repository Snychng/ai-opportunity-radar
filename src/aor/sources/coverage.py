"""按实际请求与证据生成行业覆盖，区分计划、采集、核验和研究结论。"""

from collections import Counter
from aor.evidence.identity import canonical_evidence_url
from aor.sources.industries import industry_ids


def build_industry_coverage(plan: dict, payloads: list[dict], tiered: dict | None = None) -> dict:
    catalog = plan.get("industry_catalog", [])
    selected = set(plan.get("selected_industries", []))
    run_id = plan.get("run_id")
    requests = {}
    for child in plan.get("retrieval_plans", {}).values():
        for row in [*child.get("requests", []), *child.get("required_imports", [])]:
            for key in [row.get("id"), *row.get("request_aliases", [])]:
                requests[key] = row
    rows = {r["id"]: {"industry_id": r["id"], "name": r["name"], "audience": r["audience"],
                      "demand_model": r["demand_model"], "scheduled": r["id"] in selected,
                      "attempts": set(), "failures": set(), "materials": set(), "related": set(),
                      "verified": set(), "historical": set(), "sources": set(), "qualified_count": 0,
                      "lead_count": 0} for r in catalog}
    unmapped = set()
    for payload in payloads:
        fresh = payload.get("run_id") == run_id and not payload.get("reused_for_run_id")
        if fresh:
            for result in [*payload.get("requests", []), *payload.get("results", [])]:
                request_id = result.get("request_id") or result.get("id")
                ids = industry_ids(result) or industry_ids(requests.get(request_id, {}))
                if result.get("status") in {"not_requested", "skipped-policy", "needs_host_queries", "skipped"}:
                    continue
                for identifier in ids:
                    if identifier in rows:
                        key = (result.get("source"), request_id)
                        rows[identifier]["attempts"].add(key)
                        if result.get("status") not in {"ok", "succeeded", "no-results"}:
                            rows[identifier]["failures"].add(key)
        for item in [*payload.get("evidence", []), *payload.get("comments", [])]:
            key = canonical_evidence_url(item.get("original_url") or item.get("url")) or item.get("id")
            if not key or item.get("retracted") or item.get("status") in {"retracted", "withdrawn"}:
                continue
            ids = industry_ids(item)
            if not ids:
                for ref in [*item.get("intent_refs", []), *item.get("request_ids", []), item.get("query_id")]:
                    ids.extend(industry_ids(requests.get(ref, {})))
            known = set(ids) & rows.keys()
            if not known:
                unmapped.add(key)
            verified = (item.get("verification") or {}).get("status") == "host_attested"
            related = verified or item.get("relevance_status") == "relevant"
            if item.get("is_demo"):
                related = verified = False
            for identifier in known:
                row = rows[identifier]
                if not fresh:
                    row["historical"].add(key)
                    continue
                row["materials"].add(key)
                row["sources"].add(item.get("source") or "unknown")
                if related:
                    row["related"].add(key)
                if verified:
                    row["verified"].add(key)
    if tiered:
        for bucket in ("deep_candidates", "validated_ideas", "regional_signals", "research_leads"):
            candidates = [*tiered.get(bucket, []), *(tiered.get("overflow") or {}).get(bucket, [])]
            for candidate in candidates:
                for identifier in industry_ids(candidate):
                    if identifier in rows and not candidate.get("is_demo"):
                        rows[identifier]["lead_count" if bucket == "research_leads" else "qualified_count"] += 1
    output = []
    for row in rows.values():
        status = ("reviewed_evidence" if row["verified"] else "related_material" if row["related"] else
                  "needs_relevance_review" if row["materials"] else "collection_failed" if row["failures"] else
                  "no_results" if row["attempts"] else "not_collected" if row["scheduled"] else "not_scheduled")
        if row["failures"] and row["materials"]:
            status = "partial"
        output.append({**{k: v for k, v in row.items() if not isinstance(v, set)}, "status": status,
                       "request_count": len(row["attempts"]), "failed_request_count": len(row["failures"]),
                       "material_count": len(row["materials"]), "related_evidence_count": len(row["related"]),
                       "verified_evidence_count": len(row["verified"]), "historical_evidence_count": len(row["historical"]),
                       "sources": sorted(row["sources"])})
    return {"version": "1.0", "industries": output, "unclassified_evidence_count": len(unmapped),
            "status_counts": dict(Counter(row["status"] for row in output)), "exhaustive_market_coverage": False,
            "note": "行业标签来自检索意图；相关材料仍需原文核验。未采集、历史复用及搜索无结果均不代表行业没有机会。"}


def research_quality(coverage: dict, packet: dict, tiered: dict) -> dict:
    rows = coverage.get("industries", [])
    gaps = [r["industry_id"] for r in rows if r["scheduled"] and not r["verified_evidence_count"]]
    omitted = packet.get("omitted", {}).get("evidence_budget", 0)
    qualified = sum(len(tiered.get(k, [])) + len((tiered.get("overflow") or {}).get(k, []))
                    for k in ("deep_candidates", "validated_ideas", "regional_signals"))
    return {"coverage_incomplete": bool(gaps), "industries_needing_review": gaps,
            "omitted_evidence_count": omitted, "market_absence_established": False,
            "zero_result_reason": (None if qualified else "coverage_or_evidence_incomplete" if gaps or omitted else "no_qualified_candidates_in_reviewed_material"),
            "required_followup": (["按行业补查用户原文、收费对标与反证"] if gaps else []) +
                                 (["从 evidence-index.json 选择遗漏材料核验"] if omitted else [])}
