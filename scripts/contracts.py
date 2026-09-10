#!/usr/bin/env python3
"""AI 创业机会雷达 V3 的共享数据契约。"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aor_bootstrap  # noqa: F401
from aor.evidence.identity import canonical_evidence_url, canonical_sha256, normalize_identity


SCHEMA_VERSION = "3.0"
QUERY_PLAN_VERSION = "3.0"
SCORING_VERSION = "3.0"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
RUN_ID_RE = re.compile(r"^RUN-\d{8}-[A-F0-9]{10}$")
RECORD_ID_RE = re.compile(r"^(OPP|SIG)-\d{8}-[A-F0-9]{6}$")
BENCHMARK_ID_RE = re.compile(r"^BENCH-[A-F0-9]{8}$")


class ContractError(ValueError):
    """阶段输入或输出不符合共享契约。"""


def beijing_today() -> date:
    """返回北京时间日期，避免服务器时区改变日报归属。"""
    return datetime.now(tz=BEIJING_TZ).date()


def fingerprint_record(record: dict[str, Any]) -> str:
    """用业务身份生成稳定指纹，并在存在时区分国家和主渠道。"""
    fields = ("target_user", "context", "problem_or_desire", "wedge")
    missing = [field for field in fields if not normalize_identity(record.get(field))]
    if missing:
        raise ContractError(f"生成指纹缺少字段：{', '.join(missing)}")
    identity_parts = [normalize_identity(record[field]) for field in fields]
    market_scope = record.get("market_scope")
    if isinstance(market_scope, dict):
        for field in ("country", "region", "primary_channel"):
            value = normalize_identity(market_scope.get(field))
            if value:
                identity_parts.append(f"{field}:{value}")
    normalized = "|".join(identity_parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def make_benchmark_id(benchmark: dict[str, Any]) -> str:
    """保留显式 BENCH ID，否则按产品、市场和付款者生成身份；价格属于观察。"""
    if not isinstance(benchmark, dict):
        raise ContractError("付费对标必须是对象")
    if benchmark.get("id"):
        return validate_benchmark_id(benchmark["id"])
    identity = {
        "product": normalize_identity(benchmark.get("product") or benchmark.get("title")),
        "source_market": normalize_identity(benchmark.get("source_market")),
        "payer": normalize_identity(benchmark.get("payer")),
    }
    missing = [field for field, value in identity.items() if not value]
    if missing:
        raise ContractError(f"生成 BENCH ID 缺少字段：{', '.join(missing)}")
    return f"BENCH-{canonical_sha256(identity)[:8].upper()}"


def validate_benchmark_id(benchmark_id: Any) -> str:
    """校验付费对标 ID。"""
    value = str(benchmark_id or "").strip()
    if not BENCHMARK_ID_RE.fullmatch(value):
        raise ContractError(f"无效 BENCH ID：{value or '<empty>'}")
    return value


def stable_record_id(kind: str, observed_on: date, fingerprint: str) -> str:
    """根据首次观察日期与指纹生成统一稳定 ID。"""
    prefixes = {"opportunity": "OPP", "signal": "SIG"}
    try:
        prefix = prefixes[kind]
    except KeyError as exc:
        raise ContractError(f"未知记录类型：{kind}") from exc
    if not re.fullmatch(r"[a-f0-9]{20}", fingerprint):
        raise ContractError("记录指纹必须是 20 位小写十六进制")
    return f"{prefix}-{observed_on:%Y%m%d}-{fingerprint[:6].upper()}"


def validate_record_id(record_id: Any, *, kind: str | None = None) -> str:
    """校验机会或信号 ID。"""
    value = str(record_id or "").strip()
    match = RECORD_ID_RE.fullmatch(value)
    if not match:
        raise ContractError(f"无效记录 ID：{value or '<empty>'}")
    if kind is not None:
        expected = "OPP" if kind == "opportunity" else "SIG" if kind == "signal" else None
        if expected is None:
            raise ContractError(f"未知记录类型：{kind}")
        if match.group(1) != expected:
            raise ContractError(f"记录 ID {value} 与类型 {kind} 不一致")
    return value


def make_run_id(*, as_of: date, mode: str, focus: str | None, version: str = QUERY_PLAN_VERSION,
                nonce: str | None = None, parent_run_id: str | None = None) -> str:
    """生成确定性运行 ID；新研究可传非空 nonce，恢复时复用相同参数或原 ID。"""
    payload = {
        "as_of": as_of.isoformat(),
        "mode": normalize_identity(mode),
        "focus": normalize_identity(focus),
        "version": version,
    }
    if nonce is not None:
        if not isinstance(nonce, str) or not nonce.strip():
            raise ContractError("nonce 必须是非空字符串")
        payload["nonce"] = nonce
    if parent_run_id is not None:
        payload["parent_run_id"] = validate_run_id(parent_run_id)
    digest = canonical_sha256(payload)[:10].upper()
    return f"RUN-{as_of:%Y%m%d}-{digest}"


def validate_run_id(run_id: Any) -> str:
    """校验阶段共享运行 ID。"""
    value = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(value):
        raise ContractError(f"无效 run_id：{value or '<empty>'}")
    return value


def validate_run_as_of(run_id: Any, as_of: Any) -> tuple[str, str]:
    """校验运行 ID 中的北京时间日期与阶段截止日期一致。"""
    validated_run_id = validate_run_id(run_id)
    value = str(as_of or "").strip()
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"无效 as_of：{value or '<empty>'}") from exc
    if validated_run_id[4:12] != parsed.strftime("%Y%m%d"):
        raise ContractError(f"run_id 日期与 as_of 不一致：{validated_run_id} / {value}")
    return validated_run_id, value


def validate_stage_envelope(payload: Any) -> dict[str, str]:
    """校验阶段版本和成对的运行元数据；无元数据的低层输入仍可使用。"""
    if not isinstance(payload, dict):
        raise ContractError("阶段输入必须是对象")
    if "schema_version" in payload and payload["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"不支持 schema_version：{payload['schema_version']}；需要 {SCHEMA_VERSION}")
    envelope = {"schema_version": SCHEMA_VERSION}
    if "run_id" in payload or "as_of" in payload:
        run_id, as_of = validate_run_as_of(payload.get("run_id"), payload.get("as_of"))
        envelope.update(run_id=run_id, as_of=as_of)
    return envelope


def evidence_independent_sources(evidence: Any) -> set[str]:
    """按原始内容和发布主体合并来源；采集标签不代表独立证据。"""
    if not isinstance(evidence, list):
        return set()
    parents: dict[str, str] = {}

    def root(value: str) -> str:
        parents.setdefault(value, value)
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    for item in evidence:
        if not isinstance(item, dict) or item.get("retracted") is True or item.get("status") == "retracted":
            continue
        url = canonical_evidence_url(item.get("url"))
        if not url:
            continue
        original_url = canonical_evidence_url(item.get("original_url")) or url
        publisher = normalize_identity(item.get("original_publisher") or item.get("original_author")
                                       or item.get("publisher_id"))
        identity = f"publisher:{publisher}" if publisher else f"host:{urlparse(original_url).hostname}"
        keys = [f"url:{url}", f"url:{original_url}", identity]
        anchor = min(root(key) for key in keys)
        for key in keys:
            parents[root(key)] = anchor
    return {root(key) for key in parents}
