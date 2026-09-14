"""以文件交接宿主 Agent 的研究流程，无额外模型服务。"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from aor_runtime import atomic_json
from build_query_plan import build_plan
from contracts import canonical_sha256, make_run_id, validate_run_as_of, validate_run_id
from expand_ideas import expand_ideas
from filter_ideas import filter_ideas
from manage_state import initialize_home, resolve_record_ids, update_source_health
from aor.reporting.report import BUCKETS, build_report, commit_report, render_report, render_summary, report_records


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"输入应为 JSON 对象：{path}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home).expanduser().resolve() / "runs" / validate_run_id(run_id)


@contextmanager
def _run_lock(directory: Path):
    with (directory / ".lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("本轮研究正在执行，请完成后再恢复") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save(directory: Path, manifest: dict) -> None:
    manifest["updated_at"] = _now()
    atomic_json(directory / "run.json", manifest)


def _artifact(directory: Path, manifest: dict, name: str, value: dict) -> Path:
    path = directory / f"{name}.json"
    atomic_json(path, value)
    manifest["artifacts"][name] = {"path": str(path), "sha256": canonical_sha256(value)}
    _save(directory, manifest)
    return path


def _load_artifact(manifest: dict, name: str) -> dict:
    record = manifest["artifacts"][name]
    value = _read(Path(record["path"]))
    if canonical_sha256(value) != record["sha256"]:
        raise ValueError(f"阶段产物 {name} 已被修改；用 resume 的输入参数提交修订")
    return value


def _handoff(directory: Path, manifest: dict, status: str, action: str, *, template: dict | None = None) -> dict:
    manifest["status"] = status
    manifest["next_action"] = action
    if template is not None:
        path = directory / f"{status}-template.json"
        atomic_json(path, template)
        manifest["input_template"] = str(path)
    else:
        manifest.pop("input_template", None)
    _save(directory, manifest)
    return inspect_run(Path(manifest["home"]), manifest["run_id"])


def _metadata(manifest: dict) -> dict:
    return {key: manifest[key] for key in ("schema_version", "run_id", "as_of")}


def _input_envelope(value: dict, manifest: dict, *, reused: bool = False) -> dict:
    result = deepcopy(value)
    previous_run = result.get("run_id")
    previous_date = result.get("as_of")
    if previous_date and previous_date > manifest["as_of"]:
        raise ValueError("不能将未来资料导入过去的研究")
    if previous_run and previous_run != manifest["run_id"]:
        if not reused:
            raise ValueError("输入属于其他运行，请使用本轮模板或通过 evidence 导入历史证据")
        result["reused_for_run_id"] = manifest["run_id"]
    else:
        result.update(_metadata(manifest))
    return result


def start_research(home: Path, *, as_of: date, focus: str | None = None, scope: dict | None = None,
                   intent_plan: dict | None = None, offline: bool = False,
                   include_comments: bool = False, include_recent_activity: bool = False, concurrency: int = 3,
                   parent_run_id: str | None = None, evidence_files: list[Path] | None = None,
                   benchmarks_file: Path | None = None, assessment_file: Path | None = None,
                   profile_file: Path | None = None) -> dict:
    home = initialize_home(Path(home).expanduser().resolve())
    if parent_run_id:
        validate_run_id(parent_run_id)
    run_id = make_run_id(as_of=as_of, mode="research", focus=focus, nonce=uuid.uuid4().hex, parent_run_id=parent_run_id)
    directory = _run_dir(home, run_id)
    directory.mkdir(parents=True)
    options = {"scope": scope, "include_recent_activity": include_recent_activity}
    if intent_plan is not None:
        options["intent_plan"] = intent_plan
    plan = build_plan(as_of, home, focus, **options)
    plan["run_id"] = run_id
    for child in plan["retrieval_plans"].values():
        child["run_id"] = run_id
    manifest = {
        "schema_version": "3.0", "workflow_version": "1.0", "run_id": run_id,
        "as_of": as_of.isoformat(), "parent_run_id": parent_run_id, "home": str(home),
        "focus": focus, "offline": offline, "created_at": _now(), "status": "planned",
        "collection_options": {"include_comments": include_comments, "concurrency": concurrency},
        "artifacts": {}, "stages": {}, "evidence_artifacts": [], "execution_artifacts": [],
    }
    _artifact(directory, manifest, "plan", plan)
    for kind, child in plan["retrieval_plans"].items():
        _artifact(directory, manifest, f"{kind}-plan", child)
    return resume_research(home, run_id, evidence_files=evidence_files, benchmarks_file=benchmarks_file,
                           assessment_file=assessment_file, profile_file=profile_file)


def _accept_inputs(directory: Path, manifest: dict, *, evidence_files: list[Path],
                   benchmarks_file: Path | None, assessment_file: Path | None,
                   profile_file: Path | None) -> None:
    for path in evidence_files:
        payload = _input_envelope(_read(path), manifest, reused=True)
        digest = canonical_sha256(payload)
        name = f"evidence-{digest[:12]}"
        if name not in manifest["evidence_artifacts"]:
            _artifact(directory, manifest, name, payload)
            manifest["evidence_artifacts"].append(name)
            for downstream in ("expanded", "tiered", "report", "receipt"):
                manifest["artifacts"].pop(downstream, None)
    for name, path in (("benchmarks", benchmarks_file), ("assessment", assessment_file), ("profile", profile_file)):
        if path is not None:
            payload = _read(path)
            if name != "profile":
                payload = _input_envelope(payload, manifest)
            _artifact(directory, manifest, name, payload)
            # 更换研究输入时重算其后纯计算产物，正式提交后的修订必须是新运行。
            for downstream in ("expanded", "tiered", "report", "receipt"):
                manifest["artifacts"].pop(downstream, None)
    _save(directory, manifest)


def run_paid_batch(home: Path, run_id: str, plan_file: Path, *, max_cost_usd: float,
                   batch_id: str, resume: bool = False, max_attempts: int = 1,
                   resolve_unknown: tuple[str, ...] = (), retry_failed: tuple[str, ...] = ()) -> dict:
    """显式预算入口，整轮补证共用请求日志；不自动决定购买哪些证据。"""
    from normalize_tikhub_results import normalize_documents
    from tikhub_query import execute_plan

    directory = _run_dir(home, run_id)
    with _run_lock(directory):
        manifest = _read(directory / "run.json")
        if manifest["status"] in {"completed", "committing"}:
            raise ValueError("已开始提交报告；新增付费补证请创建新研究运行")
        if manifest["offline"] or os.environ.get("AOR_OFFLINE", "").lower() in {"1", "true", "yes"}:
            raise ValueError("离线研究不会执行付费请求")
        plan = _input_envelope(_read(plan_file), manifest)
        if plan.get("stage") == "search_discovery" and (
            (plan.get("cost_policy") or {}).get("purpose") != "cross_industry_discovery"
            or not str(plan.get("discovery_objective") or "").strip()
        ):
            raise ValueError("付费发现需要明确跨行业目标；请使用 resume --discover --max-cost-usd")
        batch_name = "paid-" + canonical_sha256(batch_id)[:12]
        plan_name = batch_name + "-plan"
        if plan_name in manifest["artifacts"] and _load_artifact(manifest, plan_name) != plan:
            raise ValueError("同一批次计划已改变；新计划使用新的 batch-id")
        _artifact(directory, manifest, plan_name, plan)
        payload = execute_plan(plan, token=os.environ.get("TIKHUB_API_KEY", ""), max_cost_usd=max_cost_usd,
                               max_attempts=max_attempts, journal_path=directory / "paid-journal.sqlite3",
                               resume=resume, batch_id=batch_id, resolve_unknown=resolve_unknown, retry_failed=retry_failed)
        # 每次调用只记录本调用费用，恢复不会把之前的调用结果覆盖或重复累计。
        invocation = batch_name + "-" + uuid.uuid4().hex[:10]
        result_path = _artifact(directory, manifest, invocation, payload)
        manifest["execution_artifacts"].append(invocation)
        _save(directory, manifest)
        normalized = normalize_documents([payload], source_files=[str(result_path)])
        evidence_name = invocation + "-evidence"
        _artifact(directory, manifest, evidence_name, normalized)
        manifest["evidence_artifacts"].append(evidence_name)
        manifest.setdefault("normalized_executions", []).append(invocation)
        for name in ("expanded", "tiered", "report", "receipt"):
            manifest["artifacts"].pop(name, None)
        _refresh_library(directory, manifest)
        return _handoff(directory, manifest, "awaiting_benchmarks",
                        "本批采集与完整索引已保存；逐行业核验相关性、用户行为和 AI 增量价值。提交 benchmarks 或 leads，保留无产出来源与缺口。")


def run_discovery(home: Path, run_id: str, *, max_cost_usd: float, batch_id: str = "discovery",
                  max_requests: int = 12) -> dict:
    """显式一次性预算发现；缺价格来源记录跳过，不阻断其他行业。"""
    from aor.sources.discovery import prepare_discovery_plan
    from tikhub_query import fetch_live_pricing

    directory = _run_dir(home, run_id)
    with _run_lock(directory):
        manifest = _read(directory / "run.json")
        if manifest["offline"] or os.environ.get("AOR_OFFLINE", "").lower() in {"1", "true", "yes"}:
            raise ValueError("离线研究不会执行付费发现")
        if manifest["status"] in {"completed", "committing"}:
            raise ValueError("已提交研究不能增加付费采集；请另建研究运行")
        ledger = _paid_ledger(directory) or {}
        plan = prepare_discovery_plan(_load_artifact(manifest, "tikhub-plan"), fetch_live_pricing(),
                                      max_cost_usd=max_cost_usd, prior_cost_usd=ledger.get("list_attempted_cost_usd_exact", "0"),
                                      max_requests=max_requests)
        path = _artifact(directory, manifest, "discovery-ready-" + canonical_sha256(batch_id)[:12], plan)
        _save(directory, manifest)
    return run_paid_batch(home, run_id, path, max_cost_usd=max_cost_usd, batch_id=batch_id)


def _normalize_pending(directory: Path, manifest: dict) -> None:
    """已产生费用的执行先登记，解析失败不影响记账；恢复只重跑解析。"""
    from normalize_tikhub_results import normalize_documents

    for name in manifest["execution_artifacts"]:
        if name in manifest.get("normalized_executions", []):
            continue
        payload = _load_artifact(manifest, name)
        path = manifest["artifacts"][name]["path"]
        normalized = normalize_documents([payload], source_files=[path])
        evidence_name = name + "-evidence"
        _artifact(directory, manifest, evidence_name, normalized)
        if evidence_name not in manifest["evidence_artifacts"]:
            manifest["evidence_artifacts"].append(evidence_name)
        manifest.setdefault("normalized_executions", []).append(name)
        for downstream in ("expanded", "tiered", "report", "receipt"):
            manifest["artifacts"].pop(downstream, None)
        _save(directory, manifest)


def _coverage(manifest: dict, evidence: list[dict]) -> dict:
    from aor.evidence.quality import aggregate_status

    outcomes = []
    for payload in evidence:
        if payload.get("run_id") != manifest["run_id"] or payload.get("reused_for_run_id"):
            continue
        for source, status in payload.get("stats", {}).get("source_status", {}).items():
            outcomes.append({"source": source, "status": status})
    return aggregate_status(outcomes)


def _render_deliverables(directory: Path, report: dict) -> None:
    (directory / "report.md").write_text(render_report(report), encoding="utf-8")
    (directory / "summary.md").write_text(render_summary(report, full_path=str(directory / "report.md")), encoding="utf-8")


def _finish_report(directory: Path, manifest: dict) -> dict:
    """提交重放只读取已经固定的报告，不再刷新证据或触发采集。"""
    report = _load_artifact(manifest, "report")
    _render_deliverables(directory, report)
    manifest["status"] = "committing"
    _save(directory, manifest)
    receipt = commit_report(Path(manifest["home"]), report)
    _artifact(directory, manifest, "receipt", receipt)
    manifest["stages"]["commit"] = {"finished_at": _now(), "report_sha256": receipt["report_sha256"]}
    return _handoff(directory, manifest, "completed", "阅读 summary.md 和完整报告；下一步开展验证实验或创建补证运行。")


def _collect(directory: Path, manifest: dict, *, collect: bool) -> None:
    if "community" in manifest["stages"] or not collect:
        return
    if manifest["offline"] or os.environ.get("AOR_OFFLINE", "").lower() in {"1", "true", "yes"}:
        return
    from community_query import execute_plan

    started = time.monotonic()
    plan = _load_artifact(manifest, "community-plan")
    payload = execute_plan(plan, github_token=os.environ.get("GITHUB_TOKEN", ""), **manifest.get("collection_options", {}))
    name = "community-results"
    _artifact(directory, manifest, name, payload)
    if name not in manifest["evidence_artifacts"]:
        manifest["evidence_artifacts"].append(name)
    manifest["stages"]["community"] = {"finished_at": _now(), "elapsed_seconds": round(time.monotonic() - started, 3)}
    for source, status in payload.get("stats", {}).get("source_status", {}).items():
        update_source_health(Path(manifest["home"]), source, status, "本轮社区采集", date.fromisoformat(manifest["as_of"]),
                             run_id=manifest["run_id"])
    _save(directory, manifest)


def _evidence_payloads(manifest: dict) -> list[dict]:
    return [_load_artifact(manifest, name) for name in manifest["evidence_artifacts"]]


def _refresh_library(directory: Path, manifest: dict) -> tuple[list[dict], dict]:
    """原始材料入可重建索引，再将历史相关证据交给宿主。"""
    from aor.evidence.claims import build_evidence_packet
    from aor.evidence.identity import canonical_evidence_url
    from aor.storage.evidence_library import EvidenceLibrary

    library = EvidenceLibrary(Path(manifest["home"]) / "evidence-library")
    for name in manifest["evidence_artifacts"]:
        payload = _load_artifact(manifest, name)
        rows = [*payload.get("evidence", []), *payload.get("comments", [])]
        historical = payload.get("run_id") and payload["run_id"] != manifest["run_id"]
        if historical:
            rows = [{"observed_at": payload.get("as_of"), "run_id": payload["run_id"], **row} for row in rows]
        library.ingest(rows, as_of=manifest["as_of"], run_id=None if historical else manifest["run_id"],
                       raw_ref=manifest["artifacts"][name]["path"])
    if "benchmarks" in manifest["artifacts"]:
        benchmarks = _load_artifact(manifest, "benchmarks")
        rows = [item for benchmark in [*benchmarks.get("benchmarks", []), *benchmarks.get("leads", [])]
                for item in benchmark.get("evidence", [])]
        known_urls = {canonical_evidence_url(item.get("url")) for item in
                      library.search("", as_of=manifest["as_of"], limit=None, include_retracted=True)}
        # 对标中的事实摘要是研究判断，不能覆盖已导入的同页原文修订。
        additions = [item for item in rows if not canonical_evidence_url(item.get("url"))
                     or canonical_evidence_url(item.get("url")) not in known_urls]
        library.ingest(additions, as_of=manifest["as_of"], run_id=manifest["run_id"],
                       raw_ref=manifest["artifacts"]["benchmarks"]["path"])
    plan = _load_artifact(manifest, "plan")
    scope = plan.get("research_scope") or {}
    query = manifest.get("focus") or scope.get("task") or ""
    context = library.search(query, as_of=manifest["as_of"], run_id=manifest["run_id"], limit=100)
    assessment = _load_artifact(manifest, "assessment") if "assessment" in manifest["artifacts"] else {}
    claims = assessment.get("claims") or []
    referenced_ids = {ref.get("evidence_id") for claim in claims for ref in claim.get("evidence_refs", [])}
    for judgment in assessment.get("scores", []):
        for basis in (judgment.get("score_basis") or {}).values():
            referenced_ids.update(ref.get("evidence_id") for ref in basis.get("evidence_refs", []))
    benchmark_urls = {canonical_evidence_url(item.get("url")) for benchmark in
                      (_load_artifact(manifest, "benchmarks").get("benchmarks", []) if "benchmarks" in manifest["artifacts"] else [])
                      for item in benchmark.get("evidence", [])}
    seen = {row["evidence_id"] for row in context}
    for row in library.search("", as_of=manifest["as_of"], run_id=manifest["run_id"], limit=None):
        identities = {row.get("id"), row["evidence_id"], *row.get("aliases", [])}
        if row["evidence_id"] not in seen and (manifest["run_id"] in row.get("run_ids", [])
                or identities & referenced_ids or canonical_evidence_url(row.get("url")) in benchmark_urls):
            context.append(row)
            seen.add(row["evidence_id"])
    experiment_path = Path(manifest["home"]) / "state/experiment-events.jsonl"
    experiments = [json.loads(line) for line in experiment_path.read_text().splitlines() if line.strip()] if experiment_path.exists() else []
    from aor.evidence.selection import evidence_index
    from aor.sources.industries import industry_ids
    # 行业来自查询链路，历史原文及版本保持原样。
    for row in context:
        row["industry_ids"] = industry_ids(row)
    packet = build_evidence_packet(context, claims=claims, as_of=manifest["as_of"], run_id=manifest["run_id"],
                                   experiments=experiments, max_items=30, max_chars=18000)
    _artifact(directory, manifest, "evidence-context", {**_metadata(manifest), "evidence": context})
    _artifact(directory, manifest, "evidence-packet", packet)
    _artifact(directory, manifest, "evidence-index", evidence_index(context, packet))
    from aor.opportunity.exploration import lead_history
    _artifact(directory, manifest, "research-lead-history", {**_metadata(manifest),
              "leads": lead_history(Path(manifest["home"]), as_of=manifest["as_of"])})
    from aor.sources.coverage import build_industry_coverage
    _artifact(directory, manifest, "industry-coverage", build_industry_coverage(plan, [*_evidence_payloads(manifest),
              *[_load_artifact(manifest, name) for name in manifest["execution_artifacts"]]]))
    return context, packet


def _prepare_tiered(directory: Path, manifest: dict) -> dict:
    from aor.evidence.identity import canonical_evidence_url
    from aor.storage.evidence_library import EvidenceLibrary

    benchmarks = _load_artifact(manifest, "benchmarks")
    context = EvidenceLibrary(Path(manifest["home"]) / "evidence-library").search(
        "", as_of=manifest["as_of"], limit=None, include_retracted=True)
    by_url = {canonical_evidence_url(item.get("url")): item for item in context if item.get("url")}
    for benchmark in [*benchmarks.get("benchmarks", []), *benchmarks.get("leads", [])]:
        for item in benchmark.get("evidence", []):
            stored = by_url.get(canonical_evidence_url(item.get("url")))
            if stored:
                # 商业事实和分层输入保留；引用使用证据库实际原文及版本。
                for key in ("evidence_id", "library_evidence_id", "revision_id", "original_text", "text", "comments", "observed_at", "recorded_on", "aliases",
                            "original_url", "original_publisher", "original_author", "publisher_id", "is_demo", "retracted", "status"):
                    if key in stored:
                        item[key] = deepcopy(stored[key])
        supports = {canonical_evidence_url(item.get("url")): item for item in benchmark.get("evidence", [])}
        for field in ("payment_signals", "demand_signals"):
            for signal in benchmark.get(field, []):
                source = supports.get(canonical_evidence_url(signal.get("url"))) or {}
                if source.get("revision_id"):
                    signal["evidence_revision_id"] = source["revision_id"]
                for key in ("retracted", "status", "is_demo"):
                    if key in source:
                        signal[key] = source[key]
    if not benchmarks.get("benchmarks"):
        if not str(benchmarks.get("empty_reason") or "").strip() and not benchmarks.get("leads"):
            raise ValueError("没有合格对标时请填写 empty_reason；无需编造候选")
        expanded = {**_metadata(manifest), "benchmarks": [], "candidates": [], "summary": {"raw": 0},
                    "warnings": [benchmarks.get("empty_reason") or "本轮只保留待验证线索，尚未建立收费对标"]}
    else:
        expanded = expand_ideas(benchmarks)
    plan = _load_artifact(manifest, "plan")
    expanded["preserve_research_leads"] = True
    for candidate in expanded.get("candidates", []):
        candidate["require_ai_value"] = plan.get("require_ai_value", False)
    _artifact(directory, manifest, "expanded", expanded)
    tiered = filter_ideas(expanded)
    from aor.opportunity.exploration import normalize_leads
    explicit_leads = normalize_leads(benchmarks.get("leads", []), run_id=manifest["run_id"], as_of=manifest["as_of"])
    merged_leads = {r["lead_id"]: r for r in [*tiered.get("research_leads", []), *explicit_leads]}
    tiered["research_leads"] = list(merged_leads.values())
    for lead in tiered["research_leads"]:
        lead.update(run_id=manifest["run_id"], as_of=manifest["as_of"])
    tiered["summary"]["research_leads"] = len(merged_leads)
    for key, tier in BUCKETS:
        for bucket in (tiered, tiered.get("overflow") or {}):
            rows = bucket.get(key) or []
            if rows:
                bucket[key] = resolve_record_ids(Path(manifest["home"]), "signal" if tier == "R" else "opportunity",
                                                 rows, date.fromisoformat(manifest["as_of"]))
    _artifact(directory, manifest, "tiered", tiered)
    return tiered


def _apply_assessment(tiered: dict, assessment: dict) -> dict:
    from score_candidates import score_candidate

    result = deepcopy(tiered)
    judgments = {row["id"]: row for row in assessment.get("scores", [])}
    for bucket in (result, result.get("overflow") or {}):
        scored = []
        for row in bucket.get("deep_candidates", []):
            judgment = judgments.get(row["id"])
            if judgment is None:
                raise ValueError(f"A 级候选缺少评分依据：{row['id']}")
            allowed = ("track", "scores", "auxiliary_scores", "score_basis", "validation_plan")
            scored.append(score_candidate({**row, **{key: judgment[key] for key in allowed if key in judgment}}))
        if "deep_candidates" in bucket:
            bucket["deep_candidates"] = sorted(scored, key=lambda row: -row["total_score"])
    return result


def resume_research(home: Path, run_id: str, *, evidence_files: list[Path] | None = None,
                    benchmarks_file: Path | None = None, assessment_file: Path | None = None,
                    profile_file: Path | None = None, collect: bool = True, intent_plan: dict | None = None) -> dict:
    directory = _run_dir(home, run_id)
    if not (directory / "run.json").exists():
        raise ValueError(f"找不到研究运行：{run_id}")
    with _run_lock(directory):
        manifest = _read(directory / "run.json")
        validate_run_as_of(manifest["run_id"], manifest["as_of"])
        if manifest["status"] == "committing" and (evidence_files or benchmarks_file or assessment_file or profile_file or intent_plan):
            raise ValueError("报告正在恢复提交，先用原输入恢复完成；修订请另建研究运行")
        if manifest["status"] == "completed":
            if evidence_files or benchmarks_file or assessment_file or profile_file or intent_plan:
                raise ValueError("已提交研究保持不可变；使用 research --parent-run-id 创建补证运行")
            _render_deliverables(directory, _load_artifact(manifest, "report"))
            return inspect_run(home, run_id)
        if manifest["status"] == "committing":
            return _finish_report(directory, manifest)
        if intent_plan is not None:
            previous = _load_artifact(manifest, "plan")
            if "community" in manifest["stages"] and _load_artifact(manifest, "community-plan")["requests"]:
                raise ValueError("本轮已有检索执行；新的研究意图请另建运行")
            plan = build_plan(date.fromisoformat(manifest["as_of"]), Path(manifest["home"]), manifest.get("focus"),
                              scope=previous.get("research_scope"), intent_plan=intent_plan,
                              include_recent_activity=previous["retrieval_plans"]["community"].get("include_recent_activity", False))
            plan["run_id"] = run_id
            for child in plan["retrieval_plans"].values():
                child["run_id"] = run_id
            _artifact(directory, manifest, "plan", plan)
            for kind, child in plan["retrieval_plans"].items():
                _artifact(directory, manifest, f"{kind}-plan", child)
            manifest["stages"].pop("community", None)
            manifest["evidence_artifacts"] = [name for name in manifest["evidence_artifacts"] if name != "community-results"]
            for name in ("report", "receipt", "community-results"):
                manifest["artifacts"].pop(name, None)
        _accept_inputs(directory, manifest, evidence_files=evidence_files or [], benchmarks_file=benchmarks_file,
                       assessment_file=assessment_file, profile_file=profile_file)
        if "history-context" not in manifest["artifacts"]:
            _, historical_packet = _refresh_library(directory, manifest)
            _artifact(directory, manifest, "history-context", historical_packet)
        _collect(directory, manifest, collect=collect)
        _normalize_pending(directory, manifest)
        evidence = _evidence_payloads(manifest)
        context, packet = _refresh_library(directory, manifest)
        if "benchmarks" not in manifest["artifacts"]:
            community_plan = _load_artifact(manifest, "community-plan")
            query_notice = ("社区检索缺少英语查询，可用 resume --intent-plan-file FILE 补充；也可继续导入已核验材料。"
                            if community_plan.get("plan_status") == "needs_host_queries" else "")
            return _handoff(directory, manifest, "awaiting_benchmarks",
                            query_notice + "按 industry-coverage.json 检查未覆盖方向，执行 web_import-plan.json 的网页核验；"
                            "阅读 evidence-packet.json 与完整 evidence-index.json。B 补需求行为，R 补迁移理由，A 补直接付款。"
                            "每个方向写清 AI 相比原方案的增量价值；收费对标不足但有真实线索时填写 leads。付费发现用 resume --discover --max-cost-usd。",
                            template={**_metadata(manifest), "benchmarks": [], "leads": [], "dimensions": {}, "empty_reason": None})
        tiered = _load_artifact(manifest, "tiered") if "tiered" in manifest["artifacts"] else _prepare_tiered(directory, manifest)
        if "assessment" not in manifest["artifacts"]:
            return _handoff(directory, manifest, "awaiting_assessment",
                            "阅读 tiered.json；为 A 级填写评分依据，为研究填写最大未知项和停止条件；resume --assessment FILE。",
                            template={**_metadata(manifest), "scores": [{"id": row["id"], "track": None,
                                      "scores": {}, "auxiliary_scores": {}, "score_basis": {}}
                                      for row in report_records(tiered) if row["evidence_tier"] == "A"],
                                      "claims": [], "decision": {"summary": None, "primary_id": None,
                                      "largest_unknown": None, "next_action": None, "stop_condition": None}})
        if "report" not in manifest["artifacts"]:
            from aor.evidence.claims import validate_claims

            assessment = _load_artifact(manifest, "assessment")
            claims = validate_claims(assessment.get("claims") or [], context, as_of=manifest["as_of"])
            tiered = _apply_assessment(tiered, assessment)
            _artifact(directory, manifest, "tiered", tiered)
            profile_assessment = None
            if "profile" in manifest["artifacts"]:
                from manage_validation import assess_candidates

                profile_assessment = assess_candidates(report_records(tiered), _load_artifact(manifest, "profile"))
            report = build_report(tiered, decision=assessment.get("decision") or {}, evidence=evidence,
                                  executions=[_load_artifact(manifest, name) for name in manifest["execution_artifacts"]],
                                  profile_assessment=profile_assessment, source_coverage=_coverage(manifest, evidence),
                                  claims=claims, claim_evidence=context, run_ledger=_paid_ledger(directory),
                                  research_plan=_load_artifact(manifest, "plan"), evidence_packet=packet)
            _artifact(directory, manifest, "report", report)
        return _finish_report(directory, manifest)


def inspect_run(home: Path, run_id: str) -> dict:
    directory = _run_dir(home, run_id)
    manifest = _read(directory / "run.json")
    return {**_metadata(manifest), "status": manifest["status"], "next_action": manifest.get("next_action"),
            "run_path": str(directory), "input_template": manifest.get("input_template"),
            "artifacts": manifest["artifacts"], "stages": manifest["stages"],
            "summary_path": str(directory / "summary.md") if (directory / "summary.md").exists() else None,
            "report_path": str(directory / "report.md") if (directory / "report.md").exists() else None}


def _paid_ledger(directory: Path) -> dict | None:
    from aor.storage.request_journal import read_run_ledger

    path = directory / "paid-journal.sqlite3"
    return read_run_ledger(path) if path.exists() else None


def postmortem(home: Path, run_id: str) -> dict:
    """读取本轮真实采集结果；不根据凭证或历史成功推断当前覆盖。"""
    directory = _run_dir(home, run_id)
    manifest = _read(directory / "run.json")
    requests = []
    evidence = _evidence_payloads(manifest)
    reused = []
    for payload in evidence:
        if payload.get("run_id") != manifest["run_id"] or payload.get("reused_for_run_id"):
            reused.append({"run_id": payload.get("run_id"), "as_of": payload.get("as_of")})
            continue
        requests.extend(payload.get("request_statuses", payload.get("requests", [])))
    return {"run_id": run_id, "status": manifest["status"], "next_action": manifest.get("next_action"),
            "stages": manifest["stages"], "source_outcomes": _coverage(manifest, evidence), "requests": requests,
            "reused_evidence": reused,
            "run_ledger": _paid_ledger(directory),
            "paid_journal": str(directory / "paid-journal.sqlite3") if (directory / "paid-journal.sqlite3").exists() else None,
            "note": "仅表示本轮尝试与实际结果；旧证据复用不代表本轮平台实时可用。"}
