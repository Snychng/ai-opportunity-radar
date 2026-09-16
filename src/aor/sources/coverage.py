"""按实际请求与证据生成行业覆盖，区分计划、采集、核验和研究结论。"""

from collections import Counter
from aor.evidence.identity import canonical_evidence_url
from aor.sources.industries import industry_ids


def _legacy_coverage(plan: dict, payloads: list[dict], tiered: dict | None = None) -> dict:
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


def _legacy_quality(coverage: dict, packet: dict, tiered: dict) -> dict:
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


BEHAVIOR_ROLES = {"workflow_pain", "usage_behavior", "creative_output", "learning_progress", "social_sharing", "outsourcing", "payment"}
SUCCESS = {"ok", "succeeded", "no-results", "empty_result"}
SKIPPED = {"not_requested", "skipped-policy", "needs_host_queries", "skipped", "host_verification_required", "not_scheduled"}
REVIEW_DIMENSIONS = ("recent_user_behavior", "commercial_benchmark", "alternative", "counter_evidence")


def _object_key(item):
    """同一原生对象在采集 payload 与证据库中保持一致，父帖与评论分开。"""
    from aor.evidence.identity import evidence_object_identity
    identity = evidence_object_identity(item)
    if identity:
        return identity
    if item.get("url_kind") == "parent_post" or item.get("evidence_kind") == "comment":
        identifier = item.get("source_item_id") or item.get("id")
        return (item.get("source"), "comment", identifier) if identifier else None
    return canonical_evidence_url(item.get("original_url") or item.get("url")) or item.get("evidence_id") or item.get("id")


def _origins(item):
    yield item
    for key in ("provenance", "query_metadata", "retrieval"):
        values = item.get(key)
        if isinstance(values, dict):
            yield values
        elif isinstance(values, list):
            yield from (v for v in values if isinstance(v, dict))


def _task_keys(item):
    keys = set()
    for origin in _origins(item):
        language = (origin.get("query_scope") or origin.get("locale") or {}).get("language") or origin.get("language")
        task_id = origin.get("task_id")
        if task_id:
            keys.add((task_id, language or "unknown"))
    known_tasks = {task_id for task_id, language in keys if language != "unknown"}
    return {key for key in keys if key[1] != "unknown" or key[0] not in known_tasks}


def _review_status(item, as_of):
    """语义判断必须绑定当前原文修订；网页访问确认不替代相关性审阅。"""
    from datetime import datetime, timezone
    if item.get("derivation_status") in {"superseded", "needs_review"}:
        return "not_reviewed"
    review = item.get("relevance_review") or {}
    if not isinstance(review, dict):
        return "not_reviewed"
    status, reviewer, stamp = review.get("status"), review.get("reviewer"), review.get("reviewed_at")
    if not isinstance(reviewer, str) or not reviewer.strip() or not stamp or status not in {"relevant", "unrelated"}:
        return "not_reviewed"
    try:
        instant = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if instant.tzinfo:
            instant = instant.astimezone(timezone.utc)
        if instant.date().isoformat() > as_of:
            return "not_reviewed"
    except (ValueError, TypeError):
        return "not_reviewed"
    identifier = item.get("library_evidence_id") or item.get("evidence_id") or item.get("id")
    revision_bound = (bool(item.get("revision_id")) and review.get("evidence_id") == identifier
                      and review.get("revision_id") == item.get("revision_id"))
    content_bound = bool(item.get("content_hash")) and review.get("content_sha256") == item["content_hash"]
    return status if revision_bound or content_bound else "not_reviewed"


def _window(item, plan):
    from aor.evidence.quality import window_status
    # 重新按当前研究日期计算，不能把旧运行的 in_window 原样当成今天的近期证据。
    if item.get("published_at"):
        return window_status(item["published_at"], as_of=plan["as_of"], window=plan.get("window"))
    interval = item.get("published_at_interval")
    if isinstance(interval, dict) and interval.get("earliest") and interval.get("latest"):
        states = {window_status(interval[k], as_of=plan["as_of"], window=plan.get("window")) for k in ("earliest", "latest")}
        return states.pop() if len(states) == 1 else "uncertain"
    return "unknown"


def _sets():
    return {key: set() for key in ("attempts", "failures", "materials", "related", "verified", "reviewed", "semantic_related",
                                  "recent", "dated_historical", "unknown_date", "uncertain_date", "historical", "sources",
                                  "recent_user_behavior", "commercial_benchmark", "alternative", "counter_evidence")}


def _counts(row):
    return {"request_count": len(row["attempts"]), "failed_request_count": len(row["failures"]),
            "material_count": len(row["materials"]), "related_evidence_count": len(row["related"]),
            "verified_evidence_count": len(row["verified"]), "historical_evidence_count": len(row["historical"] - row["materials"]),
            "semantic_reviewed_count": len(row["reviewed"]), "semantic_related_count": len(row["semantic_related"]),
            "unreviewed_evidence_count": len(row["materials"] - row["reviewed"]),
            "recent_evidence_count": len(row["recent"]), "out_of_window_evidence_count": len(row["dated_historical"]),
            "unknown_date_evidence_count": len(row["unknown_date"]), "uncertain_date_evidence_count": len(row["uncertain_date"]),
            **{dimension + "_count": len(row[dimension]) for dimension in REVIEW_DIMENSIONS},
            "sources": sorted(row["sources"])}


def build_industry_coverage(plan: dict, payloads: list[dict], tiered: dict | None = None, *, version: str = "2.1") -> dict:
    if version == "1.0":
        return _legacy_coverage(plan, payloads, tiered)
    if version not in {"2.0", "2.1"}:
        raise ValueError("未知行业覆盖版本")
    selected = set(plan.get("selected_industries", []))
    catalog = plan.get("industry_catalog", [])
    rows = {r["id"]: {"industry_id": r["id"], "name": r["name"], "audience": r["audience"],
                      "demand_model": r["demand_model"], "scheduled": r["id"] in selected,
                      "catalog_subtrack_count": len(r.get("subtracks", [])), "qualified_count": 0, "lead_count": 0,
                      **_sets()} for r in catalog}
    tasks, requests, planned, skipped = {}, {}, {}, {}
    scheduled_task_keys = set()

    def task_row(key, ids=()):
        if key not in tasks:
            tasks[key] = {"task_id": key[0], "language": key[1], "industry_ids": sorted(ids),
                          "planned_requests": set(), "skipped_requests": set(), **_sets()}
        return tasks[key]

    def register(item, *, planned_request=True):
        identifier = item.get("request_id") or item.get("id")
        for alias in (identifier, *item.get("request_aliases", [])):
            if alias:
                requests[alias] = item
        ids = industry_ids(item)
        for key in _task_keys(item):
            target = task_row(key, ids)
            if planned_request:
                target["planned_requests"].add(identifier)
                scheduled_task_keys.add(key)
        if planned_request:
            planned[(item.get("source"), identifier)] = item
        for alternative in item.get("fallback_requests", []):
            register(alternative, planned_request=False)

    for child in plan.get("retrieval_plans", {}).values():
        for request in child.get("requests", []):
            register(request)
        for task in child.get("research_tasks", []):
            for key in _task_keys(task):
                task_row(key, industry_ids(task))
                scheduled_task_keys.add(key)
        for request in child.get("required_imports", []):
            register(request, planned_request=False)
            if version == "2.1":
                scheduled_task_keys.update(_task_keys(request))
        for request in child.get("skipped_requests", []):
            skipped[(request.get("source"), request.get("id"))] = request
    if version == "2.1":
        # 自由 focus 可显式定义无行业标签的研究任务；采到材料本身不能反向伪造研究范围。
        for task in plan.get("research_tasks", []):
            for key in _task_keys(task):
                task_row(key, industry_ids(task))
                scheduled_task_keys.add(key)
    # 最新库快照的修订与审阅覆盖同对象的采集副本，旧审阅不能在集合并集里残留。
    authoritative = {}
    for payload in payloads:
        for item in [*payload.get("evidence", []), *payload.get("comments", [])]:
            key = _object_key(item)
            if key and not item.get("historical_reference_only") and item.get("revision_id") and (item.get("object_identity") or item.get("library_evidence_id")):
                authoritative[key] = item
    unmapped, out_of_scope = set(), set()
    observed_attempts = set()
    for payload in payloads:
        fresh = payload.get("run_id") == plan.get("run_id") and not payload.get("reused_for_run_id")
        if fresh:
            for skipped_item in payload.get("skipped_requests", []):
                origin = {**requests.get(skipped_item.get("id"), {}), **skipped_item}
                skipped[(origin.get("source"), origin.get("id"))] = origin
            for result in [*payload.get("requests", []), *payload.get("results", [])]:
                request_id = result.get("request_id") or result.get("id")
                origin = {**requests.get(request_id, {}), **result}
                ids = industry_ids(origin)
                status = result.get("status")
                # 无 status 的请求对象是计划，不能算作已尝试。
                if not status or status in SKIPPED:
                    if status in SKIPPED:
                        skipped[(origin.get("source"), request_id)] = {**origin, "reason": result.get("reason") or status}
                    continue
                key = (origin.get("source"), request_id)
                observed_attempts.add(key)
                if origin.get("fallback_for_request_id"):
                    for planned_key in planned:
                        if planned_key[1] == origin["fallback_for_request_id"]:
                            observed_attempts.add(planned_key)
                targets = [rows[identifier] for identifier in ids if identifier in rows]
                targets += [task_row(k, ids) for k in _task_keys(origin)]
                for target in targets:
                    target["attempts"].add(key)
                    if status not in SUCCESS or result.get("parse_status") in {"unrecognized_response", "upstream_error", "partial_parse"}:
                        target["failures"].add(key)
        for item in [*payload.get("evidence", []), *payload.get("comments", [])]:
            if item.get("historical_reference_only") or item.get("derivation_status") == "superseded" or item.get("status") == "superseded":
                continue
            key = _object_key(item)
            if key in authoritative:
                item = {**item, **authoritative[key]}
            if not key or item.get("retracted") or item.get("status") in {"retracted", "withdrawn"} or item.get("is_demo"):
                continue
            origins = [item]
            refs = [*item.get("intent_refs", []), *item.get("request_ids", []), item.get("query_id"), item.get("query_group")]
            origins.extend(requests[ref] for ref in refs if ref in requests)
            ids = set(identifier for origin in origins for identifier in industry_ids(origin))
            task_keys = {task_key for origin in origins for task_key in _task_keys(origin)}
            known = ids & rows.keys()
            if not known:
                unmapped.add(key)
            if ids - selected:
                out_of_scope.add(key)
            targets = [rows[identifier] for identifier in known] + [task_row(k, ids) for k in task_keys]
            semantic = _review_status(item, str(plan.get("as_of") or "9999-12-31"))
            verified = (item.get("verification") or {}).get("status") == "host_attested"
            window = _window(item, plan) if plan.get("as_of") else "unknown"
            role = item.get("evidence_role")
            for target in targets:
                row_fresh = fresh and not item.get("reused_for_run_id")
                if item.get("run_ids") and plan.get("run_id") not in item["run_ids"]:
                    row_fresh = False
                if not row_fresh:
                    target["historical"].add(key)
                    continue
                target["materials"].add(key)
                target["sources"].add(item.get("source") or "unknown")
                if item.get("relevance_status") == "relevant" or semantic == "relevant":
                    target["related"].add(key)
                if verified:
                    target["verified"].add(key)
                if semantic in {"relevant", "unrelated"}:
                    target["reviewed"].add(key)
                target[{"in_window": "recent", "out_of_window": "dated_historical", "uncertain": "uncertain_date"}.get(window, "unknown_date")].add(key)
                if semantic != "relevant":
                    continue
                target["semantic_related"].add(key)
                if role in BEHAVIOR_ROLES and window == "in_window":
                    target["recent_user_behavior"].add(key)
                if role == "official_pricing" and verified:
                    target["commercial_benchmark"].add(key)
                if role in {"alternative", "counter_evidence"}:
                    target[role].add(key)
    if tiered:
        for bucket in ("deep_candidates", "validated_ideas", "regional_signals", "research_leads"):
            for candidate in [*tiered.get(bucket, []), *(tiered.get("overflow") or {}).get(bucket, [])]:
                for identifier in industry_ids(candidate):
                    if identifier in rows and not candidate.get("is_demo"):
                        rows[identifier]["lead_count" if bucket == "research_leads" else "qualified_count"] += 1
    task_output = []
    for key, row in sorted(tasks.items()):
        for source_id, item in skipped.items():
            if key in _task_keys(item):
                row["skipped_requests"].add(source_id)
        gaps = [dimension for dimension in REVIEW_DIMENSIONS if not row[dimension]]
        task_result = {"task_id": row["task_id"], "language": row["language"], "industry_ids": row["industry_ids"],
                            **_counts(row), "planned_request_count": len(row["planned_requests"]),
                            "skipped_request_count": len(row["skipped_requests"]), "review_gaps": gaps}
        if version == "2.1":
            if row["materials"] - row["reviewed"]:
                gaps.append("semantic_review")
            if key in scheduled_task_keys and not row["attempts"] and not row["materials"]:
                gaps.append("collection")
            own_planned = {pair for pair, request in planned.items() if key in _task_keys(request)}
            unresolved = [request for request in skipped.values() if key in _task_keys(request)
                          and (request.get("replacement_source"), request.get("replacement_request_id")) not in observed_attempts]
            if own_planned - observed_attempts or unresolved:
                gaps.append("planned_queries")
            if row["failures"]:
                gaps.append("collection_failures")
            task_result.update(scheduled=key in scheduled_task_keys,
                               scheduled_research_complete=key in scheduled_task_keys and not gaps)
        task_output.append(task_result)
    output = []
    for row in rows.values():
        identifier = row["industry_id"]
        related_tasks = [task for task in task_output if identifier in task["industry_ids"]]
        own_planned = {key for key, r in planned.items() if identifier in industry_ids(r)}
        own_skipped = [{"id": r.get("id"), "source": r.get("source"), "reason": r.get("reason") or r.get("status"),
                        "task_id": r.get("task_id")} for r in skipped.values() if identifier in industry_ids(r)]
        gaps = [dimension for dimension in REVIEW_DIMENSIONS if not row[dimension]]
        if row["materials"] - row["reviewed"]:
            gaps.append("semantic_review")
        if not row["attempts"] and row["scheduled"] and (version == "2.0" or not row["materials"]):
            gaps.append("collection")
        unresolved_skips = [r for r in skipped.values() if identifier in industry_ids(r)
                            and (r.get("replacement_source"), r.get("replacement_request_id")) not in observed_attempts]
        if unresolved_skips or own_planned - observed_attempts:
            gaps.append("planned_queries")
        if row["failures"]:
            gaps.append("collection_failures")
        status = ("reviewed_evidence" if row["reviewed"] else "related_material" if row["related"] else
                  "needs_relevance_review" if row["materials"] else "collection_failed" if row["failures"] else
                  "no_results" if row["attempts"] else "not_collected" if row["scheduled"] else "not_scheduled")
        if row["failures"] and row["materials"]:
            status = "partial"
        languages = sorted({t["language"] for t in related_tasks if t["request_count"]})
        scheduled_rows = [t for t in related_tasks if (t.get("scheduled") if version == "2.1" else t["planned_request_count"])]
        planned_languages = sorted({t["language"] for t in scheduled_rows})
        scheduled_tasks = {t["task_id"] for t in scheduled_rows}
        attempted_tasks = {t["task_id"] for t in related_tasks if t["request_count"]}
        catalog_counts = {"unplanned_subtrack_count": max(0, row["catalog_subtrack_count"] - len(scheduled_tasks))}
        if version == "2.1":
            # 只有对应目录身份的计划才能减少目录缺口；独立 TASK-* 不是已覆盖的子赛道。
            definition = next(item for item in catalog if item["id"] == identifier)
            subtracks = {f"{identifier}.{item['id']}" for item in definition.get("subtracks", [])}
            entries = {f"{identifier}.{item['id']}" for item in definition.get("discovery_entries", [])}
            catalog_counts = {"catalog_discovery_entry_count": len(entries),
                              "catalog_task_count": len(subtracks | entries),
                              "unplanned_subtrack_count": len(subtracks - scheduled_tasks),
                              "unplanned_discovery_entry_count": len(entries - scheduled_tasks),
                              "unplanned_task_count": len((subtracks | entries) - scheduled_tasks)}
        source_names = sorted({key[0] for key in own_planned} | {key[0] for key in row["attempts"]} | row["sources"])
        source_coverage = [{"source": source, "planned_request_count": sum(key[0] == source for key in own_planned),
                            "request_count": sum(key[0] == source for key in row["attempts"]),
                            "failed_request_count": sum(key[0] == source for key in row["failures"]),
                            "skipped_request_count": sum(item["source"] == source for item in own_skipped)}
                           for source in source_names]
        output.append({**{k: v for k, v in row.items() if not isinstance(v, set)}, **_counts(row), "status": status,
                       "planned_request_count": len(own_planned), "pending_request_count": len(own_planned - observed_attempts),
                       "skipped_request_count": len(own_skipped), "skipped_requests": own_skipped,
                       "planned_languages": planned_languages, "attempted_languages": languages,
                       "pending_languages": sorted(set(planned_languages) - set(languages)), "source_coverage": source_coverage,
                       "scheduled_task_count": len(scheduled_tasks), "attempted_task_count": len(attempted_tasks),
                       **catalog_counts,
                       "review_gaps": gaps, "scheduled_research_complete": row["scheduled"] and not gaps})
    coverage = {"version": version, "as_of": plan.get("as_of"), "run_id": plan.get("run_id"),
            "industries": output, "tasks": task_output, "unclassified_evidence_count": len(unmapped),
            "out_of_scope_evidence_count": len(out_of_scope),
            "status_counts": dict(Counter(row["status"] for row in output)), "exhaustive_market_coverage": False,
            "note": "行业目录与查询只是研究范围。页面已打开、词面相关、近期材料、语义审阅与商业证据分别计数；空结果不代表没有机会。"}
    if version == "2.1":
        known_scope = selected & rows.keys()
        scope_defined = bool(known_scope or scheduled_task_keys)
        scope_gaps = ([] if scope_defined else ["research_scope_undefined"])
        if selected - rows.keys():
            scope_gaps.append("selected_industries_missing_from_catalog")
        if not scheduled_task_keys:
            scope_gaps.append("research_tasks_undefined")
        coverage.update(scope_defined=scope_defined, scope_gaps=scope_gaps,
                        scheduled_research_task_count=len(scheduled_task_keys))
    return coverage


def research_quality(coverage: dict, packet: dict, tiered: dict, *, evidence: list[dict] | None = None) -> dict:
    if coverage.get("version") == "1.0":
        return _legacy_quality(coverage, packet, tiered)
    rows = coverage.get("industries", [])
    gaps = [r["industry_id"] for r in rows if r["scheduled"] and not r.get("scheduled_research_complete")]
    omitted = packet.get("omitted", {}).get("evidence_budget", 0)
    unreviewed = sum(r.get("unreviewed_evidence_count", 0) for r in rows if r["scheduled"])
    review_counts = {}
    if evidence is not None:
        from aor.evidence.identity import evidence_identity_key
        current = {}
        authoritative = set()
        for item in evidence:
            if item.get("historical_reference_only"):
                continue
            key = evidence_identity_key(item) or item.get("evidence_id") or item.get("id")
            if not key:
                continue
            snapshot = item.get("revision_id") and (item.get("library_evidence_id") or item.get("object_identity"))
            if snapshot or key not in authoritative:
                current[key] = item
            if snapshot:
                authoritative.add(key)
        counts = Counter()
        scope_ids = {r["industry_id"] for r in rows if r["scheduled"]}
        run_id = coverage.get("run_id") or tiered.get("run_id")
        as_of = str(coverage.get("as_of") or tiered.get("as_of") or packet.get("as_of") or "0001-01-01")[:10]
        for item in current.values():
            if (item.get("historical_reference_only") or item.get("is_demo") or item.get("retracted")
                    or item.get("status") in {"superseded", "retracted", "withdrawn"}
                    or item.get("derivation_status") == "superseded"):
                continue
            historical = (bool(item.get("reused_for_run_id")) or bool(item.get("historical_import"))
                          or bool(item.get("run_ids") and run_id not in item["run_ids"])
                          or bool(not item.get("run_ids") and item.get("run_id") and item["run_id"] != run_id))
            group = "historical" if historical else "current"
            reviewed = _review_status(item, as_of) in {"relevant", "unrelated"}
            counts[group + "_evidence_count"] += 1
            counts[group + ("_reviewed_evidence_count" if reviewed else "_unreviewed_evidence_count")] += 1
            if not historical and not reviewed and set(industry_ids(item)) & scope_ids:
                counts["current_scope_unreviewed_evidence_count"] += 1
        review_counts = {key: counts[key] for key in ("current_evidence_count", "current_reviewed_evidence_count",
                        "current_unreviewed_evidence_count", "historical_evidence_count", "historical_reviewed_evidence_count",
                        "historical_unreviewed_evidence_count", "current_scope_unreviewed_evidence_count")}
        unreviewed = counts["current_unreviewed_evidence_count"] + counts["historical_unreviewed_evidence_count"]
        review_counts.update(review_scope="active_context",
                             overall_evidence_count=counts["current_evidence_count"] + counts["historical_evidence_count"],
                             reviewed_evidence_count=counts["current_reviewed_evidence_count"] + counts["historical_reviewed_evidence_count"])
    qualified = sum(len(tiered.get(k, [])) + len((tiered.get("overflow") or {}).get(k, []))
                    for k in ("deep_candidates", "validated_ideas", "regional_signals"))
    if coverage.get("version") == "2.1":
        scope_gaps = list(coverage.get("scope_gaps") or [])
        if not coverage.get("scope_defined") and "research_scope_undefined" not in scope_gaps:
            scope_gaps.append("research_scope_undefined")
        tasks = [row for row in coverage.get("tasks", []) if row.get("scheduled")]
        if not tasks and "research_tasks_undefined" not in scope_gaps:
            scope_gaps.append("research_tasks_undefined")
        task_gaps = [{"task_id": row["task_id"], "language": row["language"], "review_gaps": row["review_gaps"]}
                     for row in tasks if row.get("review_gaps")]
        incomplete = bool(scope_gaps or gaps or task_gaps or unreviewed)
        return {"version": "2.1", **review_counts, "scope_defined": bool(coverage.get("scope_defined")),
                "scope_gaps": scope_gaps, "task_gaps": task_gaps, "coverage_incomplete": incomplete,
                "industries_needing_review": gaps,
                "industry_gaps": {r["industry_id"]: r["review_gaps"] for r in rows if r["scheduled"] and r["review_gaps"]},
                "omitted_evidence_count": omitted, "unreviewed_evidence_count": unreviewed,
                "market_absence_established": False,
                "zero_result_reason": (None if qualified else "coverage_or_evidence_incomplete" if incomplete
                                       else "no_qualified_candidates_in_reviewed_material"),
                "required_followup": (["明确研究范围及具体研究任务；已审材料不等于覆盖市场"] if scope_gaps else []) +
                                     (["按任务补查近期用户行为、收费对标、现有替代和反证"] if gaps or task_gaps else []) +
                                     (["按 review_queue 与续读批次读取未审原文并记录语义复核"] if unreviewed else [])}
    return {"version": "2.0", **review_counts, "coverage_incomplete": bool(gaps or unreviewed), "industries_needing_review": gaps,
            "industry_gaps": {r["industry_id"]: r["review_gaps"] for r in rows if r["scheduled"] and r["review_gaps"]},
            "omitted_evidence_count": omitted, "unreviewed_evidence_count": unreviewed,
            "market_absence_established": False,
            "zero_result_reason": (None if qualified else "coverage_or_evidence_incomplete" if gaps or unreviewed or omitted else "no_qualified_candidates_in_reviewed_material"),
            "required_followup": (["按子赛道补查近期用户行为、收费对标、现有替代和反证"] if gaps else []) +
                                 (["按 review_queue 读取未审阅原文并记录语义复核"] if unreviewed or omitted else [])}
