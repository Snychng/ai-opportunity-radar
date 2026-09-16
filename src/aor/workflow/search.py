"""多渠道检索文件交接：网络收集与单一研究提交者分离。"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
from aor.evidence.identity import evidence_identity_key
import fcntl
import math
import os
import uuid
from pathlib import Path

from aor_runtime import atomic_json
from contracts import canonical_sha256
from aor.workflow.research import _artifact, _load_artifact, _now, _read, _run_dir, _run_lock

SCHEMA_VERSION = "1.0"
SUCCESS = {"succeeded", "empty"}
UNKNOWN = {"outcome_unknown", "unknown", "sent"}
SAFE_FALLBACK = {"disabled", "missing_auth", "auth_expired", "auth_invalid", "auth_denied", "unavailable", "tool_not_invoked"}


def read_config(path: Path | None = None, *, home: Path | None = None) -> dict:
    if path is None and home is not None:
        default = Path(home).expanduser() / "search-config.json"
        path = default if default.exists() else None
    config = _read(path) if path else {}
    if set(config) - {"schema_version", "grok", "max_tasks"} or config.get("schema_version", "1.0") != "1.0":
        raise ValueError("search 配置仅接受 schema_version=1.0、grok、max_tasks")
    grok = deepcopy(config.get("grok", {}))
    if not isinstance(grok, dict):
        raise ValueError("grok 配置必须为对象")
    allowed_grok = {"enabled", "auth_file", "model", "max_responses_requests", "min_validity_seconds",
                    "timeout_seconds", "max_output_tokens", "max_tool_calls"}
    if set(grok) - allowed_grok:
        raise ValueError("grok 配置包含未知字段；凭据仅允许通过 auth_file 读取")
    grok.setdefault("enabled", False)
    if type(grok["enabled"]) is not bool:
        raise ValueError("grok.enabled 必须为布尔值")
    grok.setdefault("max_responses_requests", 6)
    if type(grok["max_responses_requests"]) is not int or not 1 <= grok["max_responses_requests"] <= 100:
        raise ValueError("max_responses_requests 必须为 1–100")
    maximum = config.get("max_tasks", 24)
    if type(maximum) is not int or not 2 <= maximum <= 100:
        raise ValueError("max_tasks 必须为 2–100")
    # 凭据只允许由适配器从文件读取，配置不会容纳内联 token 或 API Key。
    if any(key in grok for key in ("token", "access_token", "api_key", "refresh_token")):
        raise ValueError("search 配置禁止内联凭据；使用 auth_file")
    return {"schema_version": SCHEMA_VERSION, "grok": grok, "max_tasks": maximum,
            "_config_path": str(path.expanduser().resolve()) if path else None}


def _active(manifest: dict) -> None:
    if manifest["status"] in {"completed", "committing"}:
        raise ValueError("已开始提交或完成的研究不可新增检索输入；请创建后续运行")


def _offline(manifest: dict) -> bool:
    return manifest.get("offline", False) or os.environ.get("AOR_OFFLINE", "").lower() in {"1", "true", "yes"}


def _hash(value: dict, field: str) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != field})


def _load_plan(manifest: dict) -> dict:
    if "search-plan" not in manifest["artifacts"]:
        raise ValueError("先运行 aor search plan")
    plan = _load_artifact(manifest, "search-plan")
    if plan.get("plan_sha256") != _hash(plan, "plan_sha256"):
        raise ValueError("search plan 摘要不一致")
    return plan


def _template(plan: dict, task: dict, provider: str | None = None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "run_id": plan["run_id"], "plan_sha256": plan["plan_sha256"],
            "task_id": task["task_id"], "input_sha256": task["input_sha256"],
            "receipt_id": task["task_id"] + "-host", "provider": provider or "host-" + task["lane"],
            "query": task["query"], "status": "pending", "answer": "", "citations": [], "tool_calls": [],
            "usage": {}, "execution": {"searched": False, "method": "host_search", "queries": []},
            "verification": {"status": "pending", "note": "仅为线索；原文核验后使用 sources import"}}


def _seed_tasks(manifest: dict) -> list[dict]:
    plan = _load_artifact(manifest, "plan")
    seeds = []
    if "task-followup-plan" in manifest["artifacts"]:
        for row in _load_artifact(manifest, "task-followup-plan").get("tasks", []):
            seeds.append({**row, "query": row["query"], "origin": "task-followup-plan"})
    for row in (plan.get("intent_plan") or {}).get("intents", []):
        seeds.append({**row, "query": row["search_query"], "language": row["locale"]["language"],
                      "origin": "intent_plan"})
    if not seeds:
        for lane, child in plan.get("retrieval_plans", {}).items():
            if lane == "community":
                continue
            for row in child.get("requests", []) + child.get("required_imports", []):
                params = row.get("params", {})
                query = row.get("search_query") or row.get("query") or next(
                    (params[key] for key in ("keyword", "query", "q") if key in params), "")
                if query:
                    seeds.append({**row, "query": query, "language": row.get("query_scope", row.get("locale", {})).get("language", "unknown"),
                                  "origin": lane})
    if not seeds and manifest.get("focus"):
        seeds.append({"query": manifest["focus"], "language": "unknown", "origin": "focus"})
    unique = {}
    for row in seeds:
        query = str(row["query"]).strip()
        if query:
            marker = (query, row.get("language", "unknown"))
            if marker not in unique:
                unique[marker] = {**row, "query": query, "provenance": [deepcopy(row)]}
            else:
                unique[marker]["provenance"].append(deepcopy(row))
                for key in ("industry_ids", "source_observation_ids", "source_evidence_refs", "intent_refs"):
                    merged = unique[marker].setdefault(key, [])
                    for value in row.get(key, []):
                        if value not in merged:
                            merged.append(deepcopy(value))
    return list(unique.values())


def plan_search(home: Path, run_id: str, *, config: dict | None = None) -> dict:
    config = config or read_config(home=home)
    directory = _run_dir(home, run_id)
    with _run_lock(directory):
        manifest = _read(directory / "run.json")
        _active(manifest)
        if "search-plan" in manifest["artifacts"]:
            existing = _load_plan(manifest)
            if existing["config_sha256"] != canonical_sha256(config):
                raise ValueError("本轮检索配置已固定；新配置请创建后续研究")
            return existing
        seeds = _seed_tasks(manifest)
        tasks = []
        original_window = _load_artifact(manifest, "plan").get("windows", {}).get("30d", {})
        window = {"from_date": original_window.get("from") or (date.fromisoformat(manifest["as_of"]) - timedelta(days=29)).isoformat(),
                  "to_date": original_window.get("to") or manifest["as_of"]}
        for row in seeds:
            for lane in ("x", "web"):
                task = {"query": row["query"], "lane": lane, "language": row.get("language", "unknown"),
                        "industry_ids": row.get("industry_ids", []), "origin": row["origin"], "window": window,
                        "query_key": row.get("query_key") or canonical_sha256({"query": row["query"], "language": row.get("language")})[:24],
                        "purpose": row.get("purpose") or ("user_workflow" if lane == "x" else "alternatives_and_counter_evidence"),
                        "instructions": ("寻找实际用户的操作、产物、正向使用、切换与未采用行为，保留原帖链接；不预先要求付款或AI需求。" if lane == "x"
                                         else "检索官网、定价、公开社区和评论，核验现成替代、商业事实与反证；保留原始页面链接。"),
                        "provider": "grok-x" if lane == "x" and config["grok"]["enabled"] else "host-" + lane,
                        "fallback_chain": ["grok-x", "tikhub-with-explicit-budget", "host-x"] if lane == "x" else ["host-web"]}
                task["provenance"] = {key: deepcopy(row[key]) for key in
                    ("id", "task_family_id", "source_observation_ids", "source_evidence_refs", "intent_refs", "provenance", "required_checks") if key in row}
                task["task_id"] = "SEARCH-" + canonical_sha256(task)[:16].upper()
                task["input_sha256"] = canonical_sha256(task)
                tasks.append(task)
                if len(tasks) >= config["max_tasks"]:
                    break
            if len(tasks) >= config["max_tasks"]:
                break
        plan = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "as_of": manifest["as_of"], "created_at": _now(),
                "config_sha256": canonical_sha256(config), "limits": {"max_responses_requests": config["grok"]["max_responses_requests"],
                "grok_concurrency": 1, "max_attempts": 1}, "tasks": tasks,
                "deferred_seed_count": max(0, len(seeds) - (len(tasks) + 1) // 2), "automatic_fetch": False}
        plan["plan_sha256"] = _hash(plan, "plan_sha256")
        _artifact(directory, manifest, "search-plan", plan)
        template_dir = directory / "search" / "host-templates"
        template_dir.mkdir(parents=True, exist_ok=True)
        for task in tasks:
            atomic_json(template_dir / (task["task_id"] + ".json"), _template(plan, task))
        return plan


@contextmanager
def _collector_lock(directory: Path):
    path = directory / "search"
    path.mkdir(exist_ok=True)
    with (path / ".grok.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("本轮 Grok 收集器正在运行；并发上限为1") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fallback(directory: Path, manifest: dict, plan: dict, task: dict, *, reason: str,
              max_cost_usd: float | None, allow_paid: bool = True) -> dict:
    template_path = directory / "search" / "host-templates" / (task["task_id"] + ".json")
    result = {"status": "pending", "provider": "host-" + task["lane"], "reason": reason,
              "template_path": str(template_path), "next_action": "宿主使用可用联网搜索工具执行该任务，填写回执后由协调者 search import。",
              "argv": ["aor", "search", "import", "--home", manifest["home"], "--run-id", manifest["run_id"],
                       "--input", str(template_path)]}
    if (task["lane"] == "x" and allow_paid and not _offline(manifest) and max_cost_usd is not None
            and os.environ.get("TIKHUB_API_KEY", "").strip()):
        if len(task["query"]) > 100:
            result.update(reason="tikhub_query_too_long", next_action="完整查询超过 TikHub 的100字符限制；宿主按原查询继续搜索，或在后续研究显式提供较短查询。")
            return result
        from tikhub_query import build_search_plan
        paid = build_search_plan(as_of=manifest["as_of"], run_id=manifest["run_id"], query_groups=[{
            "id": task["task_id"].lower(), "keyword": task["query"], "sources": ["twitter"],
            "country": "unknown", "language": task["language"]}])
        paid["cost_policy"]["purpose"] = "cross_industry_discovery"
        paid["discovery_objective"] = task["instructions"]
        path = directory / "search" / (task["task_id"] + "-tikhub-plan.json")
        atomic_json(path, paid)
        result.update(provider="tikhub", paid_plan_path=str(path), max_cost_usd=max_cost_usd,
                      next_action="由单一协调者执行 argv；现有执行器会查询实时原价并检查本轮累计 paid-journal 预算。尚未调用TikHub。",
                      argv=["aor", "resume", manifest["run_id"], "--home", manifest["home"], "--paid-plan", str(path),
                            "--batch-id", "search-" + task["task_id"], "--max-cost-usd", str(max_cost_usd)],
                      after_execution_argv=["aor", "search", "status", "--home", manifest["home"], "--run-id", manifest["run_id"]])
    return result


def _validate_budget(value: float | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
        raise ValueError("max-cost-usd 必须是显式指定的正数")



def _validate_output(output: Path | None, directory: Path, config: dict) -> None:
    if output is None:
        return
    target = Path(output).expanduser().resolve()
    protected = [directory / "run.json", directory / "search-plan.json", directory.parent.parent / "search-config.json"]
    protected.extend(Path(value["path"]) for value in _read(directory / "run.json").get("artifacts", {}).values())
    protected.extend(Path(value).expanduser() for value in (config.get("_config_path"), config["grok"].get("auth_file")) if value)
    if target in {path.resolve() for path in protected}:
        raise ValueError("output 不得覆盖研究产物、配置或认证文件")
    if directory.resolve() in target.parents:
        output_root = (directory / "search" / "outputs").resolve()
        if output_root not in target.parents:
            raise ValueError("运行目录内 output 只能写到 search/outputs/，不能覆盖内部状态或模板")

def collect_search(home: Path, run_id: str, task_id: str, *, config: dict | None = None,
                   output: Path | None = None, max_cost_usd: float | None = None, offline: bool = False, transport=None) -> dict:
    from aor.sources.grok_x import diagnose_grok, search_x
    config = config or read_config(home=home)
    _validate_budget(max_cost_usd)
    directory = _run_dir(home, run_id)
    _validate_output(output, directory, config)
    with _collector_lock(directory):
        with _run_lock(directory):
            manifest = _read(directory / "run.json")
            _active(manifest)
            plan = _load_plan(manifest)
            if canonical_sha256(config) != plan["config_sha256"]:
                raise ValueError("collect 配置必须与 search plan 一致")
            task = next((row for row in plan["tasks"] if row["task_id"] == task_id), None)
            if not task:
                raise ValueError("task-id 不属于本轮检索计划")
        receipts = directory / "search" / "receipts"
        attempts = directory / "search" / "attempts"
        receipts.mkdir(exist_ok=True)
        attempts.mkdir(exist_ok=True)
        receipt_path = receipts / (task_id + ".json")
        attempt_path = attempts / (task_id + ".json")
        previous = [_read(path) for path in attempts.glob("*.json")]
        for previous_attempt in previous:
            if previous_attempt.get("attempt_sha256") != _hash(previous_attempt, "attempt_sha256") or previous_attempt.get("run_id") != run_id or previous_attempt.get("plan_sha256") != plan["plan_sha256"]:
                raise ValueError("已保存检索 attempt 摘要或运行不一致")
        current_attempt = _read(attempt_path) if attempt_path.exists() else None
        if receipt_path.exists() and current_attempt is not None:
            saved = _read(receipt_path)
            if saved.get("receipt_sha256") != _hash(saved, "receipt_sha256"):
                raise ValueError("已保存检索回执摘要不一致")
            # 先前未发送的 fallback 回执可以比新 attempt 更旧。只有明确绑定同次完成
            # 请求的回执才允许复用；sent 或写入两文件之间崩溃一律保持 unknown。
            matches_attempt = (current_attempt.get("status") != "sent"
                               and current_attempt.get("attempt_id")
                               and saved.get("attempt_id") == current_attempt["attempt_id"]
                               and saved.get("status") == current_attempt.get("status")
                               and all(saved.get(key) == current_attempt.get(key) for key in
                                       ("run_id", "plan_sha256", "task_id", "input_sha256")))
            if matches_attempt:
                if output:
                    atomic_json(output, saved)
                return saved
        paid_receipts = [row for row in _paid_receipts(manifest, plan) if row["task_id"] == task_id]
        completed_paid = next((row for row in paid_receipts if row["status"] in SUCCESS), None)
        if completed_paid is not None and current_attempt is None:
            if output:
                atomic_json(output, completed_paid)
            return completed_paid
        receipt = _template(plan, task)
        receipt.update(receipt_id=task_id + "-collect", created_at=_now(), provider=task["provider"])
        if task["lane"] == "x" and (current_attempt is not None or any(row["status"] in UNKNOWN for row in previous + paid_receipts)):
            receipt.update(status="outcome_unknown", reason="本轮存在已发送但结果未知或回执未与请求匹配的记录；不得自动重发或购买替代请求")
            receipt["fallback"] = _fallback(directory, manifest, plan, task, reason="outcome_unknown", max_cost_usd=None)
        elif offline or _offline(manifest):
            receipt.update(status="offline", reason="离线研究禁止真实请求")
            receipt["fallback"] = _fallback(directory, manifest, plan, task, reason="offline", max_cost_usd=None)
        elif task["lane"] == "web":
            receipt["fallback"] = _fallback(directory, manifest, plan, task, reason="host_execution_required", max_cost_usd=None)
        else:
            diagnostic = diagnose_grok(config["grok"])
            if not diagnostic.get("ready", diagnostic.get("status") == "ready"):
                reason = diagnostic.get("status", "unavailable")
                receipt.update(status=reason, reason=diagnostic.get("reason", reason))
                receipt["fallback"] = _fallback(directory, manifest, plan, task, reason=reason, max_cost_usd=max_cost_usd)
            elif len(previous) >= plan["limits"]["max_responses_requests"]:
                receipt.update(status="request_limit", reason="本轮 Responses 请求上限已达到")
                receipt["fallback"] = _fallback(directory, manifest, plan, task, reason="request_limit", max_cost_usd=None)
            else:
                attempt = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "task_id": task_id,
                           "plan_sha256": plan["plan_sha256"], "input_sha256": task["input_sha256"],
                           "status": "sent", "started_at": _now(), "attempt_id": uuid.uuid4().hex}
                attempt["attempt_sha256"] = _hash(attempt, "attempt_sha256")
                atomic_json(attempt_path, attempt)
                try:
                    result = search_x(task["query"], config=config["grok"], **task["window"], transport=transport)
                except Exception:
                    # 未知异常可能发生在已发送之后；不输出包含凭据的底层异常文字。
                    result = {"status": "outcome_unknown", "reason": "适配器异常；可能已发送，请人工核对后创建后续运行"}
                receipt.update(result)
                receipt["attempt_id"] = attempt["attempt_id"]
                receipt.update({key: _template(plan, task)[key] for key in ("schema_version", "run_id", "plan_sha256", "task_id", "input_sha256")})
                receipt["execution"] = {"searched": receipt.get("search_executed", False), "method": "grok_x_search",
                                        "queries": [task["query"]] if receipt.get("search_executed") else []}
                attempt.update(status=receipt["status"], finished_at=_now())
                attempt["attempt_sha256"] = _hash(attempt, "attempt_sha256")
                atomic_json(attempt_path, attempt)
                if receipt["status"] in UNKNOWN:
                    receipt["fallback"] = _fallback(directory, manifest, plan, task, reason=receipt["status"], max_cost_usd=None)
                elif receipt["status"] == "failed":
                    receipt["fallback"] = _fallback(directory, manifest, plan, task, reason="failed", max_cost_usd=None)
                elif receipt["status"] in SAFE_FALLBACK:
                    receipt["fallback"] = _fallback(directory, manifest, plan, task, reason=receipt["status"], max_cost_usd=max_cost_usd)
        receipt["receipt_id"] = task_id + "-" + canonical_sha256(receipt)[:12]
        receipt["receipt_sha256"] = _hash(receipt, "receipt_sha256")
        atomic_json(receipt_path, receipt)
        if output:
            atomic_json(output, receipt)
        return receipt



def _paid_receipts(manifest: dict, plan: dict) -> list[dict]:
    """从已保护的本轮付费执行产物派生回执；不接受宿主布尔值代替调用证据。"""
    receipts = []
    for task in plan["tasks"]:
        if task["lane"] != "x":
            continue
        prefix = "paid-" + canonical_sha256("search-" + task["task_id"])[:12] + "-"
        for name in manifest.get("execution_artifacts", []):
            if not name.startswith(prefix):
                continue
            payload = _load_artifact(manifest, name)
            results = payload.get("results", [])
            # 只有预定查询、来源与已登记 batch 相符的结果才能归属于该任务。
            if any(row.get("source") != "twitter" or row.get("params", {}).get("keyword") != task["query"] for row in results):
                continue
            evidence_name = name + "-evidence"
            normalized = _load_artifact(manifest, evidence_name) if evidence_name in manifest["artifacts"] else {}
            statuses = [row.get("status") for row in normalized.get("request_statuses", [])]
            succeeded = bool(statuses) and all(value in {"ok", "no-results"} for value in statuses)
            searched = any(row.get("status") == "ok" for row in results)
            citations = [{"url": row.get("original_url") or row.get("url"), "title": row.get("title", "")}
                         for row in normalized.get("evidence", []) if row.get("original_url") or row.get("url")][:500]
            receipt = _template(plan, task, "tikhub")
            receipt.update(receipt_id=name, status=("succeeded" if citations else "empty") if succeeded else
                           "outcome_unknown" if any(row.get("status") == "outcome_unknown" for row in results) else "failed",
                           citations=citations, execution_artifact=name,
                           execution={"searched": searched, "method": "paid_journal", "queries": [task["query"]] if searched else []},
                           usage={"paid_summary": payload.get("summary", {})})
            receipts.append(receipt)
    return receipts


def _candidate_verifications(manifest: dict, candidates: list[dict]) -> tuple[list[dict], list[dict] | None]:
    from aor.sources.search_candidates import x_post_id
    evidence = _load_artifact(manifest, "evidence-context").get("evidence", []) if "evidence-context" in manifest["artifacts"] else []
    opened = [row for row in evidence if isinstance(row.get("verification"), dict)
              and row["verification"].get("method") in {"opened_page", "authorized_browser"}
              and row["verification"].get("status") == "host_attested" and not row.get("retracted")
              and not row.get("historical_reference_only") and row.get("status") != "superseded" and row.get("derivation_status") not in {"superseded", "retracted", "invalid"}]
    if not opened:
        return evidence, None
    verifications = []
    for candidate in candidates:
        for row in opened:
            matches = (row.get("source") == "twitter" and x_post_id(row.get("original_url") or row.get("url")) == candidate.get("source_object_id")) if candidate["source"] == "twitter" else candidate["url"] == evidence_identity_key(row)
            quote = row.get("supporting_quote")
            original = row.get("original_text") or row.get("text") or ""
            if matches and row.get("evidence_id") and row.get("revision_id") and isinstance(quote, str) and quote and quote in original:
                verifications.append({"candidate_id": candidate["candidate_id"], "evidence_ref": {
                    "evidence_id": row["evidence_id"], "revision_id": row["revision_id"], "quote": quote}})
    return evidence, verifications

def import_search(home: Path, run_id: str, inputs: list[Path]) -> dict:
    from aor.sources.search_candidates import merge_search_candidates, normalize_search_candidates
    if not inputs:
        raise ValueError("search import 需要至少一个 --input")
    directory = _run_dir(home, run_id)
    with _run_lock(directory):
        manifest = _read(directory / "run.json")
        _active(manifest)
        plan = _load_plan(manifest)
        tasks = {row["task_id"]: row for row in plan["tasks"]}
        existing = _load_artifact(manifest, "search-executions") if "search-executions" in manifest["artifacts"] else {"schema_version": SCHEMA_VERSION, "run_id": run_id, "receipts": []}
        records = {row["receipt_id"]: row for row in existing["receipts"]}
        for path in inputs:
            value = _read(path)
            task = tasks.get(value.get("task_id"))
            if (not task or value.get("schema_version") != SCHEMA_VERSION or value.get("run_id") != run_id
                    or value.get("plan_sha256") != plan["plan_sha256"] or value.get("input_sha256") != task["input_sha256"]):
                raise ValueError("检索回执的 run_id / plan_sha256 / task_id / input_sha256 不匹配")
            if not isinstance(value.get("receipt_id"), str) or not 1 <= len(value["receipt_id"]) <= 120:
                raise ValueError("检索回执需要 receipt_id")
            if not isinstance(value.get("citations", []), list) or len(value.get("citations", [])) > 500:
                raise ValueError("检索回执 citations 必须为最多500项的数组")
            if value.get("receipt_sha256") and value["receipt_sha256"] != _hash(value, "receipt_sha256"):
                raise ValueError("检索回执内容摘要不一致")
            execution = value.get("execution", {})
            if not isinstance(execution, dict) or type(execution.get("searched")) is not bool:
                raise ValueError("检索回执需要 execution.searched 布尔值")
            if value.get("status") in SUCCESS and not execution["searched"]:
                raise ValueError("未实际搜索的任务不能标记 succeeded/empty")
            if execution["searched"] and (not isinstance(execution.get("queries"), list) or not execution["queries"]
                    or any(not isinstance(query, str) or not query.strip() for query in execution["queries"])):
                raise ValueError("已搜索回执需要真实 queries 数组")
            allowed_statuses = SUCCESS | UNKNOWN | SAFE_FALLBACK | {"pending", "offline", "request_limit", "failed", "config_invalid", "skipped"}
            if value.get("status") not in allowed_statuses:
                raise ValueError("不支持的检索 status")
            provider = value.get("provider")
            allowed_providers = {"host-" + task["lane"]} | ({"grok-x", "tikhub"} if task["lane"] == "x" else set())
            if provider not in allowed_providers:
                raise ValueError("provider 与任务 lane 不匹配")
            local_path = directory / "search" / "receipts" / (task["task_id"] + ".json")
            local = _read(local_path) if local_path.exists() else None
            if provider == "grok-x" and local != value:
                raise ValueError("Grok 回执必须与本轮本地收集结果一致，不能由外部 JSON 自报")
            if provider == "tikhub" and value not in _paid_receipts(manifest, plan):
                raise ValueError("TikHub 回执必须来自本轮已登记的 paid execution")
            identifier = value["receipt_id"]
            if identifier in records and records[identifier] != value:
                raise ValueError("同一 receipt_id 内容已改变；新结果请使用新的 receipt_id")
            records[identifier] = value
        existing["receipts"] = list(records.values())
        candidates = merge_search_candidates([candidate for row in existing["receipts"] for candidate in normalize_search_candidates(row)])
        _artifact(directory, manifest, "search-executions", existing)
        _artifact(directory, manifest, "search-candidates", {"schema_version": SCHEMA_VERSION, "run_id": run_id, "candidates": candidates})
    return search_status(home, run_id)


def search_status(home: Path, run_id: str) -> dict:
    from aor.evidence.search_evaluation import summarize_search_results
    from aor.sources.search_candidates import merge_search_candidates, normalize_search_candidates
    directory = _run_dir(home, run_id)
    manifest = _read(directory / "run.json")
    plan = _load_plan(manifest)
    receipts = _load_artifact(manifest, "search-executions").get("receipts", []) if "search-executions" in manifest["artifacts"] else []
    unique_receipts = {row["receipt_id"]: row for row in receipts}
    for row in _paid_receipts(manifest, plan):
        if row["receipt_id"] in unique_receipts and unique_receipts[row["receipt_id"]] != row:
            raise ValueError("paid 回执与已导入的同一 receipt_id 内容冲突")
        unique_receipts[row["receipt_id"]] = row
    receipts = list(unique_receipts.values())
    candidates = merge_search_candidates([candidate for receipt in receipts for candidate in normalize_search_candidates(receipt)])
    evidence, verifications = _candidate_verifications(manifest, candidates)
    evaluation = summarize_search_results(receipts, candidates, evidence=evidence, verifications=verifications)
    tasks = []
    for task in plan["tasks"]:
        rows = [row for row in receipts if row["task_id"] == task["task_id"]]
        searched = any(row.get("execution", {}).get("searched") for row in rows)
        successful = any(row["status"] in SUCCESS for row in rows)
        tasks.append({"task_id": task["task_id"], "lane": task["lane"], "provider": task["provider"], "query": task["query"],
                      "execution": "searched" if searched else "pending", "discovery": "completed" if successful else "incomplete",
                      "statuses": [row["status"] for row in rows],
                      "attestations": ["host_attested" if row["provider"].startswith("host-") else "local_execution" for row in rows], "fallbacks": [row["fallback"] for row in rows if "fallback" in row],
                      "template_path": str(directory / "search" / "host-templates" / (task["task_id"] + ".json"))})
    attempts = [_read(path) for path in (directory / "search" / "attempts").glob("*.json")]
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "plan_path": str(directory / "search-plan.json"),
            "execution": {"planned": len(tasks), "searched": sum(row["execution"] == "searched" for row in tasks),
                          "grok_requests_started": len(attempts), "attempt_statuses": dict(Counter(row["status"] for row in attempts))},
            "discovery": {"unique_candidates": len(candidates), "evidence_count": 0},
            "verification": {"pending": len(candidates) - (evaluation["host_verified_candidate_count"] or 0),
                             "verified": evaluation["host_verified_candidate_count"],
                             "note": "核验数绑定已导入 evidence-context 固定修订；没有宿主原文核验记录时保持未知"},
            "evaluation": evaluation,
            "coverage_gaps": [row["task_id"] for row in tasks if row["discovery"] != "completed"],
            "tasks": tasks, "next_action": "完成未执行宿主任务并 import 回执；核验候选原文后沿用 sources import / resume --evidence。"}
