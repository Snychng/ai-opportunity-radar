"""证据与业务实体共享的稳定标识基础函数。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def canonical_sha256(value: Any) -> str:
    """对 JSON 兼容对象生成稳定摘要。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_identity(value: Any) -> str:
    """规范化用于机会身份判断的文本。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def canonical_evidence_url(value: Any) -> str:
    """规范可追溯网页地址，移除片段和常见追踪参数而保留内容参数。"""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.lower().removeprefix("www.")
        port = parsed.port
        netloc = hostname if port in (None, 80, 443) else f"{hostname}:{port}"
        query = urlencode(sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                                 if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}))
        return urlunparse(("https", netloc, parsed.path.rstrip("/"), "", query, ""))
    except ValueError:
        return ""
