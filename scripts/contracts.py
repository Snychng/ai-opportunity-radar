#!/usr/bin/env python3
"""AI 创业机会雷达 V2 的共享数据契约。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "2.0"
QUERY_PLAN_VERSION = "2.0"
SCORING_VERSION = "2.0"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
RUN_ID_RE = re.compile(r"^RUN-\d{8}-[A-F0-9]{10}$")
RECORD_ID_RE = re.compile(r"^(OPP|SIG)-\d{8}-[A-F0-9]{6}$")


class ContractError(ValueError):
    """阶段输入或输出不符合共享契约。"""


def beijing_today() -> date:
    """返回北京时间日期，避免服务器时区改变日报归属。"""
    return datetime.now(tz=BEIJING_TZ).date()


def canonical_sha256(value: Any) -> str:
    """对 JSON 兼容对象生成稳定摘要。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_identity(value: Any) -> str:
    """规范化用于机会身份判断的文本。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def fingerprint_record(record: dict[str, Any]) -> str:
    """用用户、场景、需求和切入口生成稳定指纹。"""
    fields = ("target_user", "context", "problem_or_desire", "wedge")
    missing = [field for field in fields if not normalize_identity(record.get(field))]
    if missing:
        raise ContractError(f"生成指纹缺少字段：{', '.join(missing)}")
    normalized = "|".join(normalize_identity(record[field]) for field in fields)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


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


def make_run_id(*, as_of: date, mode: str, focus: str | None, version: str = QUERY_PLAN_VERSION) -> str:
    """生成可重跑、可审计的确定性运行 ID。"""
    payload = {
        "as_of": as_of.isoformat(),
        "mode": normalize_identity(mode),
        "focus": normalize_identity(focus),
        "version": version,
    }
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


def evidence_independent_sources(evidence: Any) -> set[str]:
    """按来源与容器粗略计算独立证据源。"""
    result: set[str] = set()
    if not isinstance(evidence, list):
        return result
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source = normalize_identity(item.get("source"))
        container = normalize_identity(item.get("container"))
        url = str(item.get("url") or "").strip()
        identity = "|".join(part for part in (source, container, url) if part)
        if identity:
            result.add(identity)
    return result
