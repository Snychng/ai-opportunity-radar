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
                   parent_run_id: str | None = None, evidence_files: list[Path] | None = None,
                   benchmarks_file: Path | None = None, assessment_file: Path | None = None,
                   profile_file: Path | None = None) -> dict:
    home = initialize_home(Path(home).expanduser().resolve())
    if parent_run_id:
        validate_run_id(parent_run_id)
    run_id = make_run_id(as_of=as_of, mode="research", focus=f"{focus or ''}|{uuid.uuid4().hex}")
    directory = _run_dir(home, run_id)
    directory.mkdir(parents=True)
    options = {"scope": scope}
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
            for downstream in ("report", "receipt"):
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
                   resolve_unknown: tuple[str, ...] = ()) -> dict:
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
        if plan.get("stage") == "search_discovery":
            raise ValueError("研究编排只执行明确缺口或详情评论补证；先建立对标与缺口计划")
        batch_name = "paid-" + canonical_sha256(batch_id)[:12]
        plan_name = batch_name + "-plan"
        if plan_name in manifest["artifacts"] and _load_artifact(manifest, plan_name) != plan:
            raise ValueError("同一批次计划已改变；新计划使用新的 batch-id")
        _artifact(directory, manifest, plan_name, plan)
        payload = execute_plan(plan, token=os.environ.get("TIKHUB_API_KEY", ""), max_cost_usd=max_cost_usd,
                               max_attempts=max_attempts, journal_path=directory / "paid-journal.sqlite3",
                               resume=resume, batch_id=batch_id, resolve_unknown=resolve_unknown)
        # 每次调用只记录本调用费用，恢复不会把之前的调用结果覆盖或重复累计。
        invocation = batch_name + "-" + uuid.uuid4().hex[:10]
        result_path = _artifact(directory, manifest, invocation, payload)
        manifest["execution_artifacts"].append(invocation)
        normalized = normalize_documents([payload], source_files=[str(result_path)])
        evidence_name = invocation + "-evidence"
        _artifact(directory, manifest, evidence_name, normalized)
        manifest["evidence_artifacts"].append(evidence_name)
        for name in ("report", "receipt"):
            manifest["artifacts"].pop(name, None)
        return _handoff(directory, manifest, "awaiting_benchmarks",
                        "本批补证已保存，请根据新增事实修订对标与主张，再用 resume --benchmarks FILE 提交。")


def _collect(directory: Path, manifest: dict, *, collect: bool) -> None:
    if "community" in manifest["stages"] or not collect:
        return
    if manifest["offline"] or os.environ.get("AOR_OFFLINE", "").lower() in {"1", "true", "yes"}:
        return
    from community_query import execute_plan

    started = time.monotonic()
    plan = _load_artifact(manifest, "community-plan")
    payload = execute_plan(plan, github_token=os.environ.get("GITHUB_TOKEN", ""))
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


def _prepare_tiered(directory: Path, manifest: dict) -> dict:
    benchmarks = _load_artifact(manifest, "benchmarks")
    if not benchmarks.get("benchmarks"):
        if not str(benchmarks.get("empty_reason") or "").strip():
            raise ValueError("没有合格对标时请填写 empty_reason；无需编造候选")
        expanded = {**_metadata(manifest), "benchmarks": [], "candidates": [], "summary": {"raw": 0},
                    "warnings": [benchmarks["empty_reason"]]}
    else:
        expanded = expand_ideas(benchmarks)
    _artifact(directory, manifest, "expanded", expanded)
    tiered = filter_ideas(expanded)
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
            allowed = ("track", "scores", "auxiliary_scores", "score_reasons", "scoring_evidence", "validation_plan")
            scored.append(score_candidate({**row, **{key: judgment[key] for key in allowed if key in judgment}}))
        if "deep_candidates" in bucket:
            bucket["deep_candidates"] = sorted(scored, key=lambda row: -row["total_score"])
    return result


def resume_research(home: Path, run_id: str, *, evidence_files: list[Path] | None = None,
                    benchmarks_file: Path | None = None, assessment_file: Path | None = None,
                    profile_file: Path | None = None, collect: bool = True) -> dict:
    directory = _run_dir(home, run_id)
    if not (directory / "run.json").exists():
        raise ValueError(f"找不到研究运行：{run_id}")
    with _run_lock(directory):
        manifest = _read(directory / "run.json")
        validate_run_as_of(manifest["run_id"], manifest["as_of"])
        if manifest["status"] == "committing" and (evidence_files or benchmarks_file or assessment_file or profile_file):
            raise ValueError("报告正在恢复提交，先用原输入恢复完成；修订请另建研究运行")
        if manifest["status"] == "completed":
            if evidence_files or benchmarks_file or assessment_file or profile_file:
                raise ValueError("已提交研究保持不可变；使用 research --parent-run-id 创建补证运行")
            return inspect_run(home, run_id)
        _accept_inputs(directory, manifest, evidence_files=evidence_files or [], benchmarks_file=benchmarks_file,
                       assessment_file=assessment_file, profile_file=profile_file)
        _collect(directory, manifest, collect=collect)
        evidence = _evidence_payloads(manifest)
        # evidence-packet 固定落盘，供宿主逐条引用；后续知识库适配在此统一接入。
        packet = {**_metadata(manifest), "evidence": [row for payload in evidence
                  for row in [*payload.get("evidence", []), *payload.get("comments", [])]],
                  "instruction": "核验原文后建立 BENCH 和主张，定价、愿付费和真实付款分别记录。"}
        _artifact(directory, manifest, "evidence-packet", packet)
        if "benchmarks" not in manifest["artifacts"]:
            return _handoff(directory, manifest, "awaiting_benchmarks",
                            "阅读 evidence-packet.json，补充官网定价、付款和反证；用 resume --benchmarks FILE 提交。",
                            template={**_metadata(manifest), "benchmarks": [], "dimensions": {}, "empty_reason": None})
        tiered = _load_artifact(manifest, "tiered") if "tiered" in manifest["artifacts"] else _prepare_tiered(directory, manifest)
        if "assessment" not in manifest["artifacts"]:
            return _handoff(directory, manifest, "awaiting_assessment",
                            "阅读 tiered.json；为 A 级填写评分依据，为研究填写最大未知项和停止条件；resume --assessment FILE。",
                            template={**_metadata(manifest), "scores": [{"id": row["id"], "track": None,
                                      "scores": {}, "auxiliary_scores": {}, "score_reasons": {}}
                                      for row in report_records(tiered) if row["evidence_tier"] == "A"],
                                      "claims": [], "decision": {"summary": None, "primary_id": None,
                                      "largest_unknown": None, "next_action": None, "stop_condition": None}})
        if "report" not in manifest["artifacts"]:
            assessment = _load_artifact(manifest, "assessment")
            tiered = _apply_assessment(tiered, assessment)
            _artifact(directory, manifest, "tiered", tiered)
            profile_assessment = None
            if "profile" in manifest["artifacts"]:
                from manage_validation import assess_candidates

                profile_assessment = assess_candidates(report_records(tiered), _load_artifact(manifest, "profile"))
            coverage = {}
            for payload in evidence:
                coverage.update(payload.get("stats", {}).get("source_status", {}))
            report = build_report(tiered, decision=assessment.get("decision") or {}, evidence=evidence,
                                  executions=[_load_artifact(manifest, name) for name in manifest["execution_artifacts"]],
                                  profile_assessment=profile_assessment, source_coverage=coverage,
                                  claims=assessment.get("claims") or [])
            _artifact(directory, manifest, "report", report)
            (directory / "report.md").write_text(render_report(report), encoding="utf-8")
            (directory / "summary.md").write_text(render_summary(report, full_path=str(directory / "report.md")), encoding="utf-8")
        report = _load_artifact(manifest, "report")
        manifest["status"] = "committing"
        _save(directory, manifest)
        receipt = commit_report(Path(manifest["home"]), report)
        _artifact(directory, manifest, "receipt", receipt)
        manifest["stages"]["commit"] = {"finished_at": _now(), "report_sha256": receipt["report_sha256"]}
        return _handoff(directory, manifest, "completed", "阅读 summary.md 和完整报告；下一步开展验证实验或创建补证运行。")


def inspect_run(home: Path, run_id: str) -> dict:
    directory = _run_dir(home, run_id)
    manifest = _read(directory / "run.json")
    return {**_metadata(manifest), "status": manifest["status"], "next_action": manifest.get("next_action"),
            "run_path": str(directory), "input_template": manifest.get("input_template"),
            "artifacts": manifest["artifacts"], "stages": manifest["stages"],
            "summary_path": str(directory / "summary.md") if (directory / "summary.md").exists() else None,
            "report_path": str(directory / "report.md") if (directory / "report.md").exists() else None}


def postmortem(home: Path, run_id: str) -> dict:
    """读取本轮真实采集结果；不根据凭证或历史成功推断当前覆盖。"""
    directory = _run_dir(home, run_id)
    manifest = _read(directory / "run.json")
    requests = []
    sources: dict[str, list[str]] = {}
    for payload in _evidence_payloads(manifest):
        for source, status in payload.get("stats", {}).get("source_status", {}).items():
            sources.setdefault(source, []).append(status)
        requests.extend(payload.get("request_statuses", payload.get("requests", [])))
    return {"run_id": run_id, "status": manifest["status"], "next_action": manifest.get("next_action"),
            "stages": manifest["stages"], "source_outcomes": sources, "requests": requests,
            "paid_journal": str(directory / "paid-journal.sqlite3") if (directory / "paid-journal.sqlite3").exists() else None,
            "note": "仅表示本轮尝试与实际结果；旧证据复用不代表本轮平台实时可用。"}
