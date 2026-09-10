"""校验原文引用并生成受容量约束的证据包；商业语义仍由宿主判断。"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

_LOCAL_TZ = timezone(timedelta(hours=8))
_STATUSES = {"supports", "partial", "conflicts", "unverified"}
_DATES = ("published_at", "observed_at", "first_observed_at", "last_observed_at", "date", "recorded_on", "as_of")
_EXPERIMENT_DATES = (*_DATES, "recorded_at", "performed_at", "completed_at")
_COLLECTION_FIELDS = {
    "id", "evidence_id", "revision_id", "version", "state_revision_id", "supersedes", "source", "source_labels",
    "run_id", "run_ids", "as_of", "observed_at", "first_observed_at", "last_observed_at",
    "recorded_on", "reused_for_run_id", "raw_file", "raw_ref", "raw_refs", "raw_json_pointer",
    "query", "query_id", "query_group", "engagement", "access_method", "extraction_warnings",
    "retrieval", "aliases", "same_source_refs", "independent_source_key", "schema_version",
    "canonical_url", "content_hash", "library_evidence_id",
}
_META = (
    "id", "evidence_id", "revision_id", "url", "canonical_url", "original_url", "source",
    "source_labels", "aliases", "title", "published_at", "observed_at", "date", "date_confidence",
    "first_observed_at", "last_observed_at", "recorded_on", "as_of", "run_id", "run_ids", "reused_for_run_id",
    "raw_file", "raw_ref", "local_ref", "raw_json_pointer", "raw_refs", "same_source_refs", "original_publisher",
    "subject_id", "origin_id", "parent_item_id", "parent_comment_id", "author", "language",
    "fact", "supporting_fact", "supports", "quote",
    "retracted", "status", "is_demo",
)


def _instant(value: str | date | datetime, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date) or (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
        day = value if isinstance(value, date) else date.fromisoformat(value)
        parsed = datetime.combine(day, time.max if end_of_day else time.min)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("日期必须是 ISO 日期或时间")
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_LOCAL_TZ)


def _visibility(record: Mapping[str, Any], cutoff: datetime, *, experiment: bool = False,
                require_observation: bool = True) -> str | None:
    keys = _EXPERIMENT_DATES if experiment else _DATES
    for key in keys:
        if record.get(key) is not None and _instant(record[key]) > cutoff:
            return "future"
    anchors = keys if experiment else ("observed_at", "first_observed_at")
    if require_observation and not any(record.get(key) is not None for key in anchors):
        return "missing_date" if experiment else "missing_observation"
    return None


def _key(record: Mapping[str, Any]) -> tuple[str, str | None]:
    identity = record.get("evidence_id") or record.get("id")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("证据必须有非空 evidence_id 或 id")
    revision = record.get("revision_id")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise ValueError("revision_id 必须是非空字符串或 null")
    return identity, revision


def _future_in_tree(value: Any, cutoff: datetime) -> bool:
    """嵌套评论和实验附件同样受截止日期约束。"""
    if isinstance(value, Mapping):
        if _visibility(value, cutoff, experiment=True, require_observation=False):
            return True
        return any(_future_in_tree(child, cutoff) for child in value.values() if isinstance(child, (Mapping, list)))
    if isinstance(value, list):
        return any(_future_in_tree(child, cutoff) for child in value)
    return False


def _catalog(evidence: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str | None], dict[str, Any]]:
    catalog: dict[tuple[str, str | None], dict[str, Any]] = {}
    for record in evidence:
        if not isinstance(record, Mapping):
            raise ValueError("证据必须是对象")
        marker = _key(record)
        if marker not in catalog:
            catalog[marker] = copy.deepcopy(dict(record))
            continue
        existing = catalog[marker]
        old_content = {key: value for key, value in existing.items() if key not in _COLLECTION_FIELDS}
        new_content = {key: value for key, value in record.items() if key not in _COLLECTION_FIELDS}
        if old_content != new_content:
            raise ValueError(f"同一证据修订有冲突原文或业务内容：{marker[0]}")
        for field, scalar in (("aliases", "id"), ("source_labels", "source")):
            existing[field] = sorted({str(value) for source in (existing, record)
                                      for value in [*(source.get(field) or []), source.get(scalar)] if value})
    return catalog


def _resolve(ref: Mapping[str, Any], catalog: Mapping[tuple[str, str | None], dict[str, Any]]) -> dict[str, Any]:
    identity = ref.get("evidence_id")
    if not isinstance(identity, str) or not identity:
        raise ValueError("引用缺少 evidence_id")
    candidates = [record for marker, record in catalog.items()
                  if identity == marker[0] or identity == record.get("id") or identity in (record.get("aliases") or [])]
    if not candidates:
        raise ValueError(f"未知证据引用：{identity}")
    matching = [record for record in candidates if record.get("revision_id") == ref.get("revision_id")]
    if len(matching) != 1:
        raise ValueError(f"证据修订不一致或引用不唯一：{identity}")
    return matching[0]


def _text_at(record: Mapping[str, Any], field: str) -> tuple[str, list[Mapping[str, Any]]]:
    parts = field.split(".")
    if parts[-1] not in {"original_text", "text"} or (len(parts) > 1 and parts[0] != "comments"):
        raise ValueError("引用 field 必须指向 original_text、text 或 comments 内的原文")
    value: Any = record
    parents = []
    for part in parts:
        if isinstance(value, Mapping):
            parents.append(value)
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f"原文字段不存在：{field}")
    if not isinstance(value, str):
        raise ValueError(f"原文字段不是文本：{field}")
    return value, parents


def _locate(ref: Mapping[str, Any], record: Mapping[str, Any], cutoff: datetime | None) -> dict[str, Any]:
    quote = ref.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("证据引用必须包含非空原文 quote")
    specified = ref.get("field")
    if specified is not None and not isinstance(specified, str):
        raise ValueError("引用 field 必须是字符串")
    fields = [re.sub(r"\[(\d+)\]", r".\1", specified)] if specified else ["original_text", "text"]
    for field in fields:
        if field not in record and field in {"original_text", "text"}:
            continue
        text, parents = _text_at(record, field)
        start = text.find(quote)
        if start < 0:
            continue
        if cutoff is not None:
            if _visibility(record, cutoff) or _future_in_tree(record, cutoff):
                raise ValueError("引用证据在 as_of 时尚不可用或缺少观察日期")
            if any(_visibility(parent, cutoff, require_observation=False) for parent in parents[1:]):
                raise ValueError("引用评论晚于 as_of")
        return {**dict(ref), "evidence_id": _key(record)[0], "revision_id": record.get("revision_id"),
                "field": field, "start": start, "end": start + len(quote)}
    raise ValueError(f"quote 无法定位于所引用修订的原文：{_key(record)[0]}")


def validate_claims(claims: Iterable[Mapping[str, Any]], evidence: Iterable[Mapping[str, Any]], *,
                    as_of: str | date | datetime | None = None) -> list[dict[str, Any]]:
    """返回带原文字段及字符位置的 claim 副本；无效引用抛出 ValueError。

    支持引用规范 evidence_id 或原始 id，revision_id 必须精确相等。历史
    原始证据没有修订字段时，双方省略 revision_id 才可兼容。field 支持
    original_text、text、comments.0.text 或 comments[0].original_text 等路径。
    日期型 as_of 包含北京时间当日全天。此函数不判断引文是否支持商业结论。
    """
    cutoff = _instant(as_of, end_of_day=True) if as_of is not None else None
    records = list(evidence)
    # 同一修订的新采集标签不可混进旧快照；时间过滤先于采集元数据合并。
    if cutoff is not None:
        records = [record for record in records if not _visibility(record, cutoff) and not _future_in_tree(record, cutoff)]
    catalog = _catalog(records)
    result = []
    seen = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise ValueError("claim 必须是对象")
        item = copy.deepcopy(dict(claim))
        for field in ("id", "statement"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"claim 缺少非空 {field}")
        if item["id"] in seen:
            raise ValueError(f"claim id 重复：{item['id']}")
        seen.add(item["id"])
        for field in ("kind", "payer", "market", "date"):
            item.setdefault(field, None)
        if item["date"] is not None:
            claim_date = _instant(item["date"])
            if cutoff is not None and claim_date > cutoff:
                raise ValueError("claim 日期晚于 as_of")
        status = item.setdefault("verification_status", "unverified")
        if status not in _STATUSES:
            raise ValueError("verification_status 必须是 supports/partial/conflicts/unverified")
        refs = item.setdefault("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, Mapping) for ref in refs):
            raise ValueError("evidence_refs 必须是引用对象数组")
        if not refs and status != "unverified":
            raise ValueError("无证据引用的 claim 只能标记 unverified")
        item["evidence_refs"] = [_locate(ref, _resolve(ref, catalog), cutoff) for ref in refs]
        item["reference_validation"] = "located" if refs else "no_evidence"
        item["semantic_validation"] = "not_performed"
        result.append(item)
    return result


def _original_texts(record: Any, prefix: str = "") -> dict[str, str]:
    """统计原文槽位，不把标题、翻译或摘要当成可引用原文。"""
    result = {}
    if isinstance(record, Mapping):
        for field, value in record.items():
            path = f"{prefix}.{field}" if prefix else field
            if field in {"original_text", "text"} and isinstance(value, str):
                result[path] = value
            elif field == "comments" or prefix:
                result.update(_original_texts(value, path))
    elif isinstance(record, list):
        for index, value in enumerate(record):
            result.update(_original_texts(value, f"{prefix}.{index}"))
    return result


def _compact(record: Mapping[str, Any], refs: list[dict[str, Any]], run_id: str | None) -> dict[str, Any]:
    item = {key: copy.deepcopy(record[key]) for key in _META if key in record}
    item["evidence_id"], item["revision_id"] = _key(record)
    origins = set(record.get("run_ids") or [])
    if record.get("run_id"):
        origins.add(record["run_id"])
    if run_id and origins and run_id not in origins:
        item["reused_for_run_id"] = run_id
    item["text_refs"] = []
    windows: dict[str, list[tuple[int, int]]] = {}
    for ref in refs:
        text, _ = _text_at(record, ref["field"])
        windows.setdefault(ref["field"], []).append((max(0, ref["start"] - 60), min(len(text), ref["end"] + 60)))
    if not windows:
        for field in ("original_text", "text"):
            if isinstance(record.get(field), str) and record[field]:
                windows[field] = [(0, min(len(record[field]), 500))]
                break
    original_texts = _original_texts(record)
    included_chars = 0
    for field, ranges in windows.items():
        text, _ = _text_at(record, field)
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in merged:
            item["text_refs"].append({"field": field, "start": start, "end": end, "text": text[start:end]})
        included_chars += sum(end - start for start, end in merged)
    item["omitted_text_chars"] = sum(map(len, original_texts.values())) - included_chars
    item["omitted_text_fields"] = len(original_texts.keys() - windows.keys())
    item["missing_fields"] = [key for key in ("url", "source", "published_at") if not record.get(key)]
    if not any(text.strip() for text in original_texts.values()):
        item["missing_fields"].append("original_text/text")
    return item


def _experiment_visible(item: Mapping[str, Any], cutoff: datetime) -> str | None:
    reason = _visibility(item, cutoff, experiment=True)
    if reason:
        return reason
    return "future" if _future_in_tree(item, cutoff) else None


def build_evidence_packet(evidence: Iterable[Mapping[str, Any]], *,
                          claims: Iterable[Mapping[str, Any]] | None = None,
                          as_of: str | date | datetime, run_id: str | None = None,
                          max_items: int = 20, max_chars: int = 12000,
                          experiments: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """构建含 evidence/claims/experiments/omitted 的可序列化紧凑包。

    max_items 约束证据与实验的合计条数；max_chars 约束 ensure_ascii=False、
    separators=(',', ':') 的完整 JSON 字符数。claim 连同其全部引用原文作为
    一组优先装入，放不下则整组省略，不截断仍保留的 quote。text_refs 保存
    原文路径、原文字符位置与片段，raw_refs/raw_file 指回完整事实来源。
    旧版 fact/supporting_fact/supports/quote 原样保留为陈述，不参与原文核验。
    缺观察日期的证据、未来证据与未来实验会明确计入 omitted。
    """
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0:
        raise ValueError("max_items 必须是非负整数")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars 必须是正整数")
    cutoff = _instant(as_of, end_of_day=True)
    records = list(evidence)
    catalog = _catalog(records)
    historical_records = []
    counts = {"evidence_future": 0, "evidence_missing_observation": 0, "claims_future": 0,
              "claims_unavailable_evidence": 0, "experiments_future": 0, "experiments_missing_date": 0}
    for record in records:
        reason = _visibility(record, cutoff) or ("future" if _future_in_tree(record, cutoff) else None)
        if reason:
            counts[f"evidence_{reason}"] += 1
        else:
            historical_records.append(record)
    available = _catalog(historical_records)
    historical_claims = []
    for claim in claims or []:
        if not isinstance(claim, Mapping):
            raise ValueError("claim 必须是对象")
        if claim.get("date") is not None and _instant(claim["date"]) > cutoff:
            counts["claims_future"] += 1
        else:
            historical_claims.append(claim)
    checked = validate_claims(historical_claims, catalog.values())
    eligible_claims = []
    for claim in checked:
        refs = claim["evidence_refs"]
        if any((ref["evidence_id"], ref["revision_id"]) not in available for ref in refs):
            counts["claims_unavailable_evidence"] += 1
            continue
        eligible_claims.append(claim)
    eligible_experiments = []
    for experiment in experiments or []:
        reason = _experiment_visible(experiment, cutoff)
        if reason:
            counts[f"experiments_{reason}"] += 1
        else:
            eligible_experiments.append(copy.deepcopy(dict(experiment)))

    def assemble(selected: dict, selected_claims: list, selected_experiments: list) -> dict[str, Any]:
        omitted = {**counts, "evidence_budget": len(available) - len(selected),
                   "claims_budget": len(eligible_claims) - len(selected_claims),
                   "experiments_budget": len(eligible_experiments) - len(selected_experiments)}
        packet = {"schema_version": "3.0", "as_of": as_of.isoformat() if isinstance(as_of, date) else as_of,
                  "run_id": run_id, "evidence": list(selected.values()), "claims": selected_claims,
                  "experiments": selected_experiments, "omitted": omitted,
                  "semantic_validation": "not_performed", "truncated": any(omitted.values()) or any(
                      record["omitted_text_chars"] for record in selected.values()),
                  "limits": {"max_items": max_items, "max_chars": max_chars}, "serialized_chars": 0}
        while True:
            length = len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
            if length == packet["serialized_chars"]:
                return packet
            packet["serialized_chars"] = length

    selected: dict[tuple[str, str | None], dict[str, Any]] = {}
    selected_claims: list[dict[str, Any]] = []
    selected_experiments: list[dict[str, Any]] = []
    if assemble(selected, selected_claims, selected_experiments)["serialized_chars"] > max_chars:
        raise ValueError("max_chars 不足以容纳证据包元数据")
    for claim in eligible_claims:
        candidate = dict(selected)
        candidate_claims = [*selected_claims, claim]
        refs_by_record: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        for chosen in candidate_claims:
            for ref in chosen["evidence_refs"]:
                refs_by_record.setdefault((ref["evidence_id"], ref["revision_id"]), []).append(ref)
        for marker, refs in refs_by_record.items():
            candidate[marker] = _compact(available[marker], refs, run_id)
        if len(candidate) <= max_items and assemble(candidate, candidate_claims, [])["serialized_chars"] <= max_chars:
            selected, selected_claims = candidate, candidate_claims
    for experiment in eligible_experiments:
        candidate_experiments = [*selected_experiments, experiment]
        if len(selected) + len(candidate_experiments) <= max_items and assemble(
                selected, selected_claims, candidate_experiments)["serialized_chars"] <= max_chars:
            selected_experiments = candidate_experiments
    for marker, record in available.items():
        if marker not in selected and len(selected) + len(selected_experiments) < max_items:
            candidate = {**selected, marker: _compact(record, [], run_id)}
            if assemble(candidate, selected_claims, selected_experiments)["serialized_chars"] <= max_chars:
                selected = candidate
    return assemble(selected, selected_claims, selected_experiments)
