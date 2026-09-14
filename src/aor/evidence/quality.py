"""采集质量标记，与付款资格及商业 A/B/R 完全分离。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aor.text import scripts, tokens

QUALITY_VERSION = '2.0'
FAILURE_STATUSES = {'error', 'auth-required', 'rate-limited', 'skipped-policy', 'partial',
                    'unrecognized_response', 'upstream_error', 'partial_parse'}
# 研究意图词不是任务锚点；不能因为 again / help / tool 等词相同就认定内容相关。
QUERY_STOP_WORDS = frozenset('again still just also some any how what why when where who would could should '
                            'can does do did have has had was were be been am my your our their them me us '
                            'not no all really very more most less much many get got want looking find '
                            'finding help please anyone someone something about into than then now today '
                            'use using used need needs tools app apps ai new good best better'.split())
BROAD_TERMS = frozenset('gaming game games teammates team teams internet online digital social product '
                       'products content work workflow community people software service services'.split())


def _query_terms(query: str) -> set[str]:
    return {term for term in tokens(query) - QUERY_STOP_WORDS if not term.isdigit()}


def _semantic_status(review: Any, as_of: str, *, item: dict) -> str:
    if not isinstance(review, dict) or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        return 'not_reviewed'
    try:
        reviewed = datetime.fromisoformat(str(review.get('reviewed_at')).replace('Z', '+00:00'))
        if reviewed.tzinfo:
            reviewed = reviewed.astimezone(timezone.utc)
        if reviewed.date() > date.fromisoformat(as_of):
            return 'not_reviewed'
    except (TypeError, ValueError):
        return 'not_reviewed'
    identifier = item.get('library_evidence_id') or item.get('evidence_id') or item.get('id')
    revision_bound = (bool(item.get('revision_id')) and review.get('evidence_id') == identifier
                      and review.get('revision_id') == item['revision_id'])
    content_bound = bool(item.get('content_hash')) and review.get('content_sha256') == item['content_hash']
    if not revision_bound and not content_bound:
        return 'not_reviewed'
    return review['status'] if review.get('status') in {'relevant', 'unrelated', 'unknown'} else 'not_reviewed'


def research_window(as_of: str, lookback_days: int = 30) -> dict[str, Any]:
    end = date.fromisoformat(as_of)
    return {'lookback_days': lookback_days, 'range_from': (end - timedelta(days=lookback_days - 1)).isoformat(),
            'range_to': as_of, 'semantics': 'inclusive_calendar_days'}


def window_status(published_at: Any, *, as_of: str, window: dict | None = None,
                  interval: dict | None = None) -> str:
    """按 UTC 自然日判断发布时间，观察/更新日期不能替代发布时间。"""
    window = window or research_window(as_of)
    if not published_at and isinstance(interval, dict):
        try:
            start = date.fromisoformat(str(interval['earliest'])[:10])
            end = date.fromisoformat(str(interval['latest'])[:10])
            lower, upper = date.fromisoformat(window['range_from']), date.fromisoformat(window['range_to'])
            if start > end:
                return 'unknown'
            if lower <= start <= end <= upper:
                return 'in_window'
            if end < lower or start > upper:
                return 'out_of_window'
            return 'uncertain'
        except (KeyError, TypeError, ValueError):
            return 'unknown'
    try:
        published = datetime.fromisoformat(str(published_at).replace('Z', '+00:00'))
        if published.tzinfo:
            published = published.astimezone(timezone.utc)
        day = published.date()
    except (TypeError, ValueError):
        return 'unknown'
    return 'in_window' if date.fromisoformat(window['range_from']) <= day <= date.fromisoformat(window['range_to']) else 'out_of_window'


def assess_quality(item: dict[str, Any], *, query: str = '', as_of: str, window: dict | None = None) -> dict[str, Any]:
    """词面筛选不等于语义核验；弱命中和跨语种内容保留待复核。"""
    text = ' '.join(str(item.get(key) or '') for key in ('title', 'original_text'))
    query_tokens, text_tokens = _query_terms(query), tokens(text)
    matched = query_tokens & text_tokens
    score = round(len(matched) / len(query_tokens), 4) if query_tokens else 0.0
    anchors = query_tokens - BROAD_TERMS
    strong_match = bool(anchors & matched) and (
        (len(query_tokens) == 1 and len(matched) == 1) or (len(matched) >= 2 and score >= 0.35))
    if not query_tokens or not text_tokens:
        relevance, reason, lexical = 'unknown', 'missing_task_query_or_text', 'unassessable'
    elif strong_match:
        relevance, reason, lexical = 'relevant', 'multiple_task_terms_or_specific_query', 'matched'
    elif matched:
        relevance, reason, lexical = 'unknown', 'weak_overlap_requires_review', 'weak_match'
    elif not scripts(query) & scripts(text):
        relevance, reason, lexical = 'unknown', 'cross_script_requires_review', 'unassessable'
    else:
        relevance, reason, lexical = 'unrelated', 'no_task_term_overlap', 'no_match'
    semantic = _semantic_status(item.get('relevance_review'), as_of, item=item)
    if semantic != 'not_reviewed':
        relevance, reason = semantic, 'explicit_semantic_review'
    state = window_status(item.get('published_at'), as_of=as_of, window=window,
                          interval=item.get('published_at_interval'))
    return {'quality_version': QUALITY_VERSION, 'local_relevance': score, 'relevance_status': relevance,
            'relevance_reason': reason, 'matched_terms': sorted(matched), 'window_status': state,
            'task_anchor_terms': sorted(anchors & matched), 'lexical_status': lexical,
            'relevance_basis': 'semantic_review' if semantic != 'not_reviewed' else 'lexical_screening',
            'semantic_relevance_status': semantic,
            'requires_semantic_review': semantic in {'not_reviewed', 'unknown'},
            'recent_lexical_match': lexical == 'matched' and state == 'in_window',
            'recent_semantically_verified': semantic == 'relevant' and state == 'in_window',
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
