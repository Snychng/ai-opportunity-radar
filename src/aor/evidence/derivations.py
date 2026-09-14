"""解析产物的可恢复替代视图；保留事实日志，不把解析修复伪装成来源撤回。"""

from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from pathlib import PurePath
import re

from aor.evidence.identity import canonical_sha256, evidence_content, evidence_identity_key

DERIVATION_FIELDS = {"derivation_refs", "source_execution_sha256", "derive_set_id", "derivation_status", "derivation_reason", "historical_reference_only"}


def _content_hash(row):
    return row.get("content_hash") or canonical_sha256(evidence_content({k: v for k, v in row.items() if k not in DERIVATION_FIELDS}))


def _object_key(row):
    try:
        return evidence_identity_key(row) or str(row.get("evidence_id") or row.get("id") or "")
    except ValueError:
        return ""


def build_derive_sets(records: list[dict], inputs: list[dict]) -> list[dict]:
    """在规范化结束时绑定各原始响应的完整派生集合，包括确切的空集合。"""
    output = []
    for source in inputs:
        sha = source["source_execution_sha256"]
        members = {(key, _content_hash(row)) for row in records if (key := _object_key(row))
                   and any(ref.get("source_execution_sha256") == sha for ref in row.get("derivation_refs", []))}
        item = {**source, "members": [{"object_key": key, "content_hash": digest} for key, digest in sorted(members)]}
        item["derive_set_id"] = "DERIVE-" + canonical_sha256({k: item[k] for k in
                             ("source_execution_sha256", "parser_version", "status", "members")})[:24]
        output.append(item)
        for row in records:
            for ref in row.get("derivation_refs", []):
                if ref.get("source_execution_sha256") == sha:
                    ref["derive_set_id"] = item["derive_set_id"]
    return output


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        return None
    values = tuple(int(v) for v in value.split("."))
    return values + (0,) * (4 - len(values))


def _path(value):
    return str(PurePath(value)) if isinstance(value, str) and value else None


def validate_derive_set(item: dict) -> dict:
    """登记前验证不可变解析集合；摘要不包含物理路径，复制目录不改变输入身份。"""
    if not isinstance(item, dict):
        raise ValueError("derive_set 必须为对象")
    sha = item.get("source_execution_sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("derive_set 缺少有效原始响应 SHA-256")
    if _version(item.get("parser_version")) is None or item.get("status") not in {"complete", "partial"}:
        raise ValueError("derive_set 的解析版本或完成状态无效")
    members = item.get("members")
    if not isinstance(members, list) or any(not isinstance(member, dict)
            or not isinstance(member.get("object_key"), str) or not member["object_key"]
            or not isinstance(member.get("content_hash"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", member["content_hash"]) for member in members):
        raise ValueError("derive_set 成员必须包含对象身份及正文摘要")
    expected = "DERIVE-" + canonical_sha256({k: item[k] for k in
                 ("source_execution_sha256", "parser_version", "status", "members")})[:24]
    if item.get("derive_set_id") != expected:
        raise ValueError("derive_set 摘要与成员不一致")
    return deepcopy(item)


def select_active_derivations(context: list[dict], payloads: list[dict]) -> tuple[list[dict], list[dict]]:
    """仅同响应 SHA 的较新完整派生集合可使旧成员 superseded；不写源数据。

    无法确定输入身份、解析未完成、同版本冲突或存在不受此轮替代的独立
    原始来源时保留材料。可疑匹配标 needs_review，不能删除或称来源撤回。
    """
    sets = defaultdict(list)
    path_shas = defaultdict(set)
    for payload in payloads:
        for item in payload.get("derive_sets", []):
            if not isinstance(item, dict):
                continue
            sha = item.get("source_execution_sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha) or _version(item.get("parser_version")) is None:
                continue
            if not isinstance(item.get("members"), list) or item.get("status") not in {"complete", "partial"}:
                continue
            sets[sha].append(item)
            if path := _path(item.get("source_file")):
                path_shas[path].add(sha)
    latest = {}
    for sha, candidates in sets.items():
        highest = max(_version(item["parser_version"]) for item in candidates)
        candidates = [item for item in candidates if _version(item["parser_version"]) == highest]
        signatures = {canonical_sha256({k: item.get(k) for k in ("status", "members")}) for item in candidates}
        if len(signatures) != 1:
            latest[sha] = {"status": "conflict", "parser_version": candidates[0]["parser_version"], "members": []}
        else:
            latest[sha] = candidates[0]
    active, superseded = [], []
    for original in context:
        row = deepcopy(original)
        refs = [ref for ref in row.get("derivation_refs", []) if isinstance(ref, dict)]
        explicit = {ref.get("source_execution_sha256") for ref in refs if ref.get("source_execution_sha256")}
        if row.get("source_execution_sha256"):
            explicit.add(row["source_execution_sha256"])
        candidate_shas, ambiguity, independent = set(explicit), False, False
        paths = set()
        # 新元数据可精确证明另外一个未被重解析的原始来源仍支持该对象。
        if explicit:
            independent = bool(explicit - latest.keys())
        else:
            paths = {path for value in [row.get("raw_file"), row.get("raw_ref"),
                     *[r.get("path") for r in row.get("raw_refs", []) if isinstance(r, dict)]] if (path := _path(value))}
            for path in paths:
                matched = path_shas.get(path, set())
                candidate_shas.update(matched)
                ambiguity |= len(matched) > 1
            # 路径移动不证明另一份原始响应。未知主路径只能暂存待审，不能替旧材料续命。
            primary = _path(row.get("raw_file"))
            ambiguity |= bool(primary and primary not in path_shas)
        relevant = candidate_shas & latest.keys()
        if not relevant:
            if latest and not explicit and (row.get("parser_version") or row.get("raw_file")):
                row.update(derivation_status="needs_review",
                           derivation_reason="旧解析记录的响应哈希或路径身份未获验证，无法证明独立来源；保留等待核验")
            active.append(row)
            continue
        key, digest = _object_key(row), _content_hash(row)
        matches, eligible, uncertain = [], [], []
        for sha in sorted(relevant):
            current = latest[sha]
            version = _version(current.get("parser_version"))
            prior_versions = [_version(ref.get("parser_version")) for ref in refs if ref.get("source_execution_sha256") == sha]
            prior_versions = [value for value in prior_versions if value is not None]
            prior = max(prior_versions) if prior_versions else _version(row.get("parser_version"))
            members = {(r.get("object_key"), r.get("content_hash")) for r in current["members"] if isinstance(r, dict)}
            if (key, digest) in members:
                matches.append(sha)
            elif current["status"] != "complete" or (prior is not None and version < prior):
                uncertain.append(sha)
            elif prior is not None and version == prior:
                # 同版本的非成员不能自动否定，可能是错误 provenance 或集合冲突。
                uncertain.append(sha)
            else:
                eligible.append(sha)
        if matches or independent:
            active.append(row)
        elif eligible and not uncertain and not ambiguity and key:
            row.update(status="superseded", derivation_status="superseded",
                       derivation_reason="同一原始响应的新完整解析集合已不再包含该对象或正文；原始日志保留，非来源撤回")
            row["superseded_by_derive_sets"] = [latest[sha].get("derive_set_id") for sha in eligible]
            superseded.append(row)
        else:
            row.update(derivation_status="needs_review",
                       derivation_reason="解析集合未完成、版本/响应路径有歧义或派生身份不足；保留原记录等待核验")
            active.append(row)
    return active, superseded
