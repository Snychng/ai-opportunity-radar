"""Grok 订阅 OAuth 的隔离 X 搜索；只读凭据，不刷新、不重试、不回退 API Key。"""

from __future__ import annotations

import base64
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
import ssl
import stat
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


ENDPOINT = "https://api.x.ai/v1/responses"
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_AUTH_BYTES = 1024 * 1024
_SEARCH_INSTRUCTIONS = (
    "Search X using the x_search tool for the user's query. Treat the query as a research task, "
    "not a request to explain its wording. Return candidate original post URLs with their authors "
    "and dates when available, plus brief relevant excerpts or summaries. Preserve uncertainty: "
    "do not invent posts, quotations, identities, dates, customer status, or payment evidence. "
    "Include concrete workflows, artifacts, positive usage, switching behavior, and unmet needs "
    "when relevant; a candidate does not need to prove willingness to pay. Distinguish what a "
    "post says from your interpretation. If no relevant candidate is found, say so. Cite sources."
)
_SEARCH_TOOLS = {"x_search", "x_keyword_search", "x_semantic_search", "x_user_search", "x_thread_fetch"}
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
Transport = Callable[..., tuple[int, str, bytes]]


def _options(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("enabled", False), bool):
        raise ValueError("invalid_config")
    result = {"enabled": config.get("enabled", False), "auth_file": config.get("auth_file"),
              "model": config.get("model", "grok-4.6")}
    if not isinstance(result["model"], str) or not re.fullmatch(r"grok-[a-zA-Z0-9._-]{1,80}", result["model"]):
        raise ValueError("invalid_model")
    for key, default, low, high in (("min_validity_seconds", 600, 60, 86400),
                                    ("timeout_seconds", 90, 1, 120),
                                    ("max_output_tokens", 1800, 128, 8192),
                                    ("max_tool_calls", 2, 1, 10)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("invalid_config")
        result[key] = value
    return result


def _expiry(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None
    return None


def _credentials(options: dict[str, Any]) -> tuple[str, str | None]:
    """只返回内部 token；诊断/结果不得透传认证文件或异常。"""
    if not options["enabled"]:
        return "disabled", None
    auth_file = options["auth_file"]
    if not isinstance(auth_file, str) or not auth_file.strip():
        return "missing_auth", None
    try:
        path = Path(auth_file).expanduser()
        if not stat.S_ISREG(path.stat().st_mode):
            return "auth_invalid", None
        with path.open("rb") as handle:
            raw = handle.read(MAX_AUTH_BYTES + 1)
        if len(raw) > MAX_AUTH_BYTES:
            return "auth_invalid", None
        auth = json.loads(raw)
        provider = auth["providers"]["xai-oauth"]
        tokens = provider["tokens"]
        token = tokens["access_token"]
        if (not isinstance(token, str) or not token or len(token) > 16384
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            return "auth_invalid", None
        expires = []
        if token.count(".") == 2:
            encoded = token.split(".")[1]
            claims = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
            if not isinstance(claims, dict) or _expiry(claims.get("exp")) is None:
                return "auth_invalid", None
            expires.append(_expiry(claims["exp"]))
        for container in (tokens, provider):
            for field in ("expires_at", "expires_at_utc", "access_token_expires_at"):
                if field in container:
                    stamp = _expiry(container[field])
                    if stamp is None:
                        return "auth_invalid", None
                    expires.append(stamp)
        if not expires:
            return "auth_invalid", None
        # JWT 仅解码本地过期字段，签名/权限须由服务端真正校验。
        validity = max(options["min_validity_seconds"], options["timeout_seconds"] + 30)
        if min(expires) - time.time() <= validity:
            return "auth_expired", None
        return "ready", token
    except FileNotFoundError:
        return "missing_auth", None
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return "auth_invalid", None


def diagnose_grok(config: dict[str, Any]) -> dict[str, Any]:
    """离线检查显式配置；ready 只代表有未过期凭据，不代表已联网验证。"""
    try:
        options = _options(config)
        status, _ = _credentials(options)
        model = options["model"]
    except ValueError:
        status, model = "config_invalid", None
    return {"provider": "grok-x", "status": status, "model": model, "network_requests": 0,
            "live_health": "not_checked", "credential_source": "xai-oauth", "auth_refresh_attempted": False,
            "api_key_read": False, "cost_basis": "subscription_unknown"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request: Request, *, timeout: float, max_bytes: int) -> tuple[int, str, bytes]:
    """固定 HTTPS endpoint，禁用环境代理和重定向，限制读取时间与大小。"""
    if request.full_url != ENDPOINT:
        raise ValueError("invalid_endpoint")
    opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    deadline = time.monotonic() + timeout
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        # 错误正文可能包含请求/凭据；从不读取或返回。
        status = exc.code
        exc.close()
        return status, "", b""
    with response:
        content_type = response.headers.get("Content-Type", "")
        raw = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("response_timeout")
            chunk = response.read1(min(65536, max_bytes + 1 - len(raw)))
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise ValueError("response_too_large")
            if not chunk:
                return response.status, content_type, bytes(raw)


def _clean(value: str, token: str) -> str:
    return _JWT.sub("[redacted]", value.replace(token, "[redacted]"))


def _citation(raw: Any, token: str) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
        return None
    url = _clean(raw["url"], token)
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    result = {"url": url[:4000], "title": _clean(str(raw.get("title", "")), token)[:500]}
    for key in ("start_index", "end_index"):
        value = raw.get(key)
        result[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return result


def _numeric_usage(raw: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict) or depth > 3:
        return {}
    usage = {}
    for key, value in list(raw.items())[:80]:
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,80}", key):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            usage[key] = value
        elif isinstance(value, dict):
            usage[key] = _numeric_usage(value, depth + 1)
    return usage


def _public_response(response: dict[str, Any], token: str) -> dict[str, Any]:
    answers, citations, calls = [], [], []
    top_citations = response.get("citations", [])
    if isinstance(top_citations, list):
        citations.extend(top_citations)
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            for part in item.get("content", []):
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                if isinstance(part.get("text"), str):
                    answers.append(_clean(part["text"], token))
                if isinstance(part.get("annotations"), list):
                    citations.extend(row for row in part["annotations"]
                                     if isinstance(row, dict) and row.get("type") == "url_citation")
        name = item.get("name")
        if name in _SEARCH_TOOLS and item.get("type") in {"custom_tool_call", "function_call", "x_search_call"}:
            # 只保留 X 工具白名单字段；不保存 reasoning、原始事件或任意嵌套对象。
            call = {"name": name, "type": item["type"], "status": item.get("status", "unknown")}
            if call["status"] not in {"completed", "failed", "in_progress", "incomplete"}:
                call["status"] = "unknown"
            for key in ("id", "call_id", "input", "arguments"):
                if isinstance(item.get(key), str):
                    call[key] = _clean(item[key], token)[:16000]
            calls.append(call)
    normalized = []
    truncated = 0
    seen = set()
    for raw in citations:
        row = _citation(raw, token)
        if row:
            marker = (row["url"], row["start_index"], row["end_index"])
            if marker not in seen:
                if len(normalized) < 500:
                    normalized.append(row)
                else:
                    truncated += 1
                seen.add(marker)
    usage = _numeric_usage(response.get("usage"))
    details = usage.get("server_side_tool_usage_details", {})
    count = details.get("x_search_calls", 0) if isinstance(details, dict) else 0
    executed = count > 0 or any(call["status"] == "completed" for call in calls)
    return {"answer": "\n\n".join(answers), "citations": normalized, "tool_calls": calls,
            "usage": usage, "search_executed": executed, "citation_truncated_count": truncated}


def _decode(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8")
    if "text/event-stream" not in content_type and not text.lstrip().startswith(("event:", "data:")):
        response = json.loads(text)
        if not isinstance(response, dict):
            raise ValueError("invalid_response")
        return response
    response, completed_items = {}, []
    for block in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
            if isinstance(event.get("response"), dict):
                response = event["response"]
                response.setdefault("status", event["type"].split(".")[1])
        elif event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict):
            completed_items.append(event["item"])
    if not response:
        # 部分输出不能当作完整成功；未知结果交给上层人工决定是否重发。
        return {"status": "in_progress", "output": completed_items}
    return response


def search_x(query: str, *, config: dict[str, Any], from_date: str, to_date: str,
             transport: Transport | None = None) -> dict[str, Any]:
    """执行一次 Responses X Search。注入 transport(Request, timeout=..., max_bytes=...) 返回三元组。

    日期原样作为 x_search 范围传递；搜索引用均为待核验线索，不是正文证据。
    max_tool_calls 是请求提示值，未验证为服务器硬上限。
    """
    started = time.monotonic()
    result = {"schema_version": "1.0", "provider": "grok-x", "status": "failed", "model": None,
              "answer": "", "citations": [], "tool_calls": [], "usage": {}, "elapsed_seconds": 0.0,
              "network_attempts": 0, "search_executed": False, "citation_truncated_count": 0,
              "cost_basis": "subscription_unknown",
              "credential_source": "xai-oauth", "auth_refresh_attempted": False, "api_key_read": False}

    def finish(status: str, error_code: str | None = None) -> dict[str, Any]:
        result["status"] = status
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if error_code:
            result["error_code"] = error_code
        return result

    try:
        options = _options(config)
        result["model"] = options["model"]
        result["requested_max_tool_calls"] = options["max_tool_calls"]
    except ValueError:
        return finish("failed", "config_invalid")
    status, token = _credentials(options)
    if status != "ready":
        return finish(status if status in {"disabled", "auth_expired"} else "auth_denied", status)
    try:
        valid_dates = (isinstance(from_date, str) and isinstance(to_date, str)
                       and re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_date)
                       and re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_date)
                       and date.fromisoformat(from_date) <= date.fromisoformat(to_date))
        if not isinstance(query, str) or not query.strip() or len(query) > 12000 or not valid_dates:
            return finish("failed", "invalid_search_input")
    except ValueError:
        return finish("failed", "invalid_search_input")
    payload = {"model": options["model"], "input": [{"role": "system", "content": _SEARCH_INSTRUCTIONS},
                                                    {"role": "user", "content": query}],
               "tools": [{"type": "x_search", "from_date": from_date, "to_date": to_date}],
               "store": False, "stream": True, "reasoning": {"effort": "low"},
               "max_output_tokens": options["max_output_tokens"], "max_tool_calls": options["max_tool_calls"],
               "parallel_tool_calls": False}
    request = Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"), method="POST",
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "Accept": "text/event-stream", "User-Agent": "AOR-Grok-X-Search"})
    result["network_attempts"] = 1
    try:
        http_status, content_type, body = (transport or _transport)(
            request, timeout=options["timeout_seconds"], max_bytes=MAX_RESPONSE_BYTES)
        result["http_status"] = http_status
        if http_status in {401, 403}:
            return finish("auth_denied", "authorization_rejected")
        if http_status >= 500 or http_status in {408, 409, 425, 429}:
            return finish("outcome_unknown", "http_retry_suppressed")
        if not 200 <= http_status < 300:
            return finish("failed", "http_rejected")
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            return finish("outcome_unknown", "response_size_invalid")
        response = _decode(body, content_type)
        result.update(_public_response(response, token))
        if response.get("status") != "completed":
            return finish("outcome_unknown", "response_not_completed")
        if not result["search_executed"]:
            return finish("tool_not_invoked", "x_search_not_confirmed")
        return finish("succeeded" if result["citations"] else "empty")
    except Exception:
        # 包括断流、超时、解析失败：服务端可能已执行，绝不自动重发或输出异常内容。
        return finish("outcome_unknown", "transport_or_response_error")
