"""公开 JSON 请求与付费调用可共用的同源重定向边界。"""
from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SameOriginRedirectHandler(request.HTTPRedirectHandler):
    """只允许同协议、同主机重定向，避免转发 Authorization。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = parse.urlsplit(req.full_url)
        new = parse.urlsplit(parse.urljoin(req.full_url, newurl))
        if old.scheme != new.scheme or old.netloc != new.netloc:
            raise error.HTTPError(req.full_url, code, '拒绝跨域或降级重定向', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SameOriginRedirectHandler = SameOriginRedirectHandler


def json_get(*, endpoint: str, params: dict[str, Any], headers: dict[str, str], timeout: int) -> dict | list:
    """有界 GET；端点白名单由调用者按具体产品协议验证。"""
    target = endpoint + ('?' + parse.urlencode(params) if params else '')
    req = request.Request(target, headers=headers, method='GET')
    try:
        with request.build_opener(SameOriginRedirectHandler()).open(req, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        label = 'auth-required' if exc.code in {401, 403} else 'rate-limited' if exc.code == 429 else 'http-error'
        raise RuntimeError(f'{label}:{exc.code}') from exc
    except (error.URLError, TimeoutError) as exc:
        raise RuntimeError('network-error') from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError('response-too-large')
    try:
        decoded = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError('invalid-json') from exc
    if not isinstance(decoded, (dict, list)):
        raise RuntimeError('invalid-response-shape')
    return decoded
