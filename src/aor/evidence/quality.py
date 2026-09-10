"""采集质量标记，与付款资格及商业 A/B/R 完全分离。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aor.text import scripts, tokens

QUALITY_VERSION = '1.0'
FAILURE_STATUSES = {'error', 'auth-required', 'rate-limited', 'skipped-policy', 'partial'}


def research_window(as_of: str, lookback_days: int = 30) -> dict[str, Any]:
    end = date.fromisoformat(as_of)
    return {'lookback_days': lookback_days, 'range_from': (end - timedelta(days=lookback_days - 1)).isoformat(),
            'range_to': as_of, 'semantics': 'inclusive_calendar_days'}


def window_status(published_at: Any, *, as_of: str, window: dict | None = None) -> str:
    """按 UTC 自然日判断发布时间，观察/更新日期不能替代发布时间。"""
    window = window or research_window(as_of)
    try:
        published = datetime.fromisoformat(str(published_at).replace('Z', '+00:00'))
        if published.tzinfo:
            published = published.astimezone(timezone.utc)
        day = published.date()
    except (TypeError, ValueError):
        return 'unknown'
    return 'in_window' if date.fromisoformat(window['range_from']) <= day <= date.fromisoformat(window['range_to']) else 'out_of_window'


def assess_quality(item: dict[str, Any], *, query: str = '', as_of: str, window: dict | None = None) -> dict[str, Any]:
    """词面相关为线索；不同文字且无交集保留未知，供宿主核验。"""
    text = ' '.join(str(item.get(key) or '') for key in ('title', 'original_text'))
    query_tokens, text_tokens = tokens(query), tokens(text)
    matched = query_tokens & text_tokens
    score = round(len(matched) / len(query_tokens), 4) if query_tokens else 0.0
    if matched:
        relevance, reason = 'relevant', 'lexical_overlap'
    elif not query_tokens or not text_tokens:
        relevance, reason = 'unknown', 'missing_query_or_text'
    elif not scripts(query) & scripts(text):
        relevance, reason = 'unknown', 'cross_script_requires_review'
    else:
        relevance, reason = 'unrelated', 'no_lexical_overlap'
    state = window_status(item.get('published_at'), as_of=as_of, window=window)
    return {'quality_version': QUALITY_VERSION, 'local_relevance': score, 'relevance_status': relevance,
            'relevance_reason': reason, 'matched_terms': sorted(matched), 'window_status': state,
            'recent_evidence_eligible': relevance == 'relevant' and state == 'in_window'}


def aggregate_status(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row['source'], []).append(row['status'])
    result = {}
    for source, states in groups.items():
        failures = [state for state in states if state in FAILURE_STATUSES]
        if failures:
            result[source] = failures[0] if len(set(states)) == 1 else 'partial'
        else:
            result[source] = 'ok' if 'ok' in states else 'no-results'
    return result


def canonical_url(url: str) -> str:
    """只移除已知跟踪参数；保留 HN id 与其他内容身份参数。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', urlencode(sorted(query)), parts.fragment))


def mark_reposts(items: list[dict[str, Any]]) -> None:
    """同链接转载标记为同一证据；评论身份与作者独立保留。"""
    seen: dict[str, str] = {}
    for item in items:
        if not item.get('url') or item.get('evidence_kind') == 'comment' or item.get('parent_comment_id'):
            continue
        key = canonical_url(item['url'])
        if key in seen:
            item['duplicate_of'] = seen[key]
            item['recent_evidence_eligible'] = False
        else:
            seen[key] = item['id']
