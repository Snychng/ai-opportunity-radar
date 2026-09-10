"""公开社区采集执行：质量统计、搜索归并和显式评论深挖。"""
from __future__ import annotations

import math
import re
import time as clock
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from aor.net import json_get
from aor.text import language as _language, clean_text
from aor.evidence.quality import assess_quality, aggregate_status, canonical_url, window_status

ALLOWED_SOURCES = {'hackernews', 'github'}
MAX_ITEMS_PER_REQUEST = 30


class CommunityPlanError(ValueError):
    """社区查询计划或响应不符合约束。"""


def _clean(value: Any, limit: int = 1000) -> str | None:
    return clean_text(value, limit) or None


def _classify_error(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("auth-required"):
        return "auth-required"
    if message.startswith("rate-limited"):
        return "rate-limited"
    if message.startswith(("network-error", "http-error", "invalid-", "response-too-large")):
        return "error"
    return "error"


def _query(item: dict[str, Any]) -> str:
    value = item.get('relevance_query') or item['params'].get('query') or item['params'].get('q') or ''
    return re.sub(r'\b(?:is|created|updated|sort|order):\S+', ' ', str(value))


def _rank(row: dict[str, Any]) -> tuple:
    return ({'relevant': 2, 'unknown': 1, 'unrelated': 0}[row['relevance_status']],
            row['local_relevance'], row['recent_evidence_eligible'], math.log1p(sum(max(0, value) for value in row.get('engagement', {}).values()
                                                 if isinstance(value, (int, float)) and math.isfinite(value))))


def _headers(source: str, github_token: str) -> dict[str, str]:
    headers = {'Accept': 'application/json', 'User-Agent': 'AI-Opportunity-Radar/3.0'}
    if source == 'github' and github_token.strip():
        headers['Authorization'] = f'Bearer {github_token.strip()}'
        headers['X-GitHub-Api-Version'] = '2022-11-28'
    return headers


def _comment_request(parent: dict[str, Any], limit: int) -> tuple[str, dict] | None:
    if parent['source'] == 'hackernews' and re.fullmatch(r'\d+', parent['source_item_id']):
        return f"https://hn.algolia.com/api/v1/items/{parent['source_item_id']}", {}
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)', parent['thread_url'])
    if parent['source'] == 'github' and match:
        owner, repo, number = match.groups()
        return f'https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments', {'per_page': limit}
    return None


def _normalize_comments(payload: Any, parent: dict[str, Any], observed_at: str, limit: int) -> tuple[int, list[dict]]:
    source = parent['source']
    if source == 'hackernews':
        if not isinstance(payload, dict) or not isinstance(payload.get('children'), list):
            raise RuntimeError('invalid-response-shape')
        raw_rows = []
        pending = [(row, None) for row in reversed(payload['children'])]
        # 有界遍历；HN items 的一次响应包含嵌套回复，不发递归请求。
        while pending:
            row, reply_to = pending.pop()
            if not isinstance(row, dict):
                continue
            if row.get('type') == 'comment':
                raw_rows.append((row, reply_to))
            pending.extend((child, row.get('id')) for child in reversed(row.get('children') or []))
    else:
        if not isinstance(payload, list):
            raise RuntimeError('invalid-response-shape')
        raw_rows = [(row, None) for row in payload]
    result = []
    for row, reply_to in raw_rows[:limit]:
        if not isinstance(row, dict):
            continue
        item_id = _clean(row.get('id'), 100)
        text = _clean(row.get('text') if source == 'hackernews' else row.get('body'), 4000)
        if not item_id or not text or row.get('deleted') or row.get('dead'):
            continue
        url = f'https://news.ycombinator.com/item?id={item_id}' if source == 'hackernews' else _clean(row.get('html_url'), 2000)
        author = row.get('author') if source == 'hackernews' else (row.get('user') or {}).get('login')
        result.append({'id': f'{source}:comment:{item_id}', 'source': source, 'source_item_id': item_id,
                       'evidence_kind': 'comment', 'parent_item_id': parent['id'], 'origin_id': parent['id'],
                       'parent_comment_id': str(reply_to) if reply_to else None, 'parent_url': parent['thread_url'],
                       'thread_url': parent['thread_url'], 'url': url or parent['thread_url'],
                       'url_kind': 'comment' if url else 'parent_post', 'author': _clean(author, 200),
                       'container': parent['container'], 'title': None, 'original_text': text,
                       'language': _language(text), 'zh_translation': None, 'published_at': _clean(row.get('created_at'), 100),
                       'observed_at': observed_at, 'engagement': {}, 'signal_types': [],
                       'query_id': parent['query_id'] + '-comments-' + parent['source_item_id'], 'access_method': 'native-platform'})
    return len(raw_rows), result


def collect_community(
    plan: dict[str, Any], *, normalize_hn: Callable, normalize_github: Callable, plan_sha256: str, github_token: str = '', timeout: int = 20,
    max_items_per_request: int = 20, transport: Callable[..., Any] = json_get,
    include_comments: bool = False, max_comment_threads: int = 4, max_comments_per_thread: int = 10,
    concurrency: int = 1,
) -> dict[str, Any]:
    """先归并搜索证据，再按显式开关补充有限评论；保留每次请求质量。"""
    if not 1 <= max_items_per_request <= MAX_ITEMS_PER_REQUEST:
        raise CommunityPlanError(f'max_items_per_request 必须为 1 到 {MAX_ITEMS_PER_REQUEST}')
    if not 1 <= timeout <= 60 or not 1 <= concurrency <= 4:
        raise CommunityPlanError('timeout 必须为 1 到 60 秒，concurrency 必须为 1 到 4')
    if not 1 <= max_comment_threads <= 6 or not 1 <= max_comments_per_thread <= 30:
        raise CommunityPlanError('评论深挖最多 6 个主题，每主题最多 30 条')
    observed_at = datetime.now(tz=timezone.utc).isoformat()
    evidence: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    request_results: list[dict[str, Any]] = []

    def fetch(item: dict, parent: dict | None = None) -> tuple[dict, list[dict]]:
        started = datetime.now(tz=timezone.utc).isoformat()
        tick = clock.monotonic()
        counts = dict.fromkeys(('fetched', 'parsed', 'relevant', 'retained', 'valid_items'), 0)
        rows = []
        status = 'error'
        try:
            payload = transport(endpoint=item['endpoint'], params=dict(item['params']),
                                headers=_headers(item['source'], github_token), timeout=timeout)
            if parent:
                counts['fetched'], rows = _normalize_comments(payload, parent, started, max_comments_per_thread)
            else:
                key = 'hits' if item['source'] == 'hackernews' else 'items'
                if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                    raise RuntimeError('invalid-response-shape')
                counts['fetched'] = len(payload[key])
                rows = (normalize_hn if item['source'] == 'hackernews' else normalize_github)(payload, item, started)
            counts['parsed'] = len(rows)
            for row in rows:
                origin = parent or item
                row['request_ids'] = list(dict.fromkeys([item['id'], *origin.get('request_ids', []), *origin.get('request_aliases', [])]))
                row['intent_refs'] = list(origin.get('intent_refs', []))
                row['query_metadata'] = list(origin.get('query_metadata') or origin.get('provenance') or [])
                row['query'] = _query(item)
                row.update(assess_quality(row, query=row['query'], as_of=plan['as_of'], window=plan['window']))
                if parent and row['relevance_status'] == 'unrelated':
                    row.update(relevance_status='unknown', relevance_reason='parent_context_requires_review')
                row['date_confidence'] = 'high' if row['window_status'] != 'unknown' else 'unknown'
                if row.get('updated_at'):
                    row['activity_window_status'] = window_status(row['updated_at'], as_of=plan['as_of'], window=plan['window'])
            counts['relevant'] = sum(row['relevance_status'] == 'relevant' for row in rows)
            rows = sorted((row for row in rows if row['relevance_status'] != 'unrelated'), key=_rank, reverse=True)
            rows = rows[:max_comments_per_thread if parent else max_items_per_request]
            counts['retained'] = len(rows)
            counts['valid_items'] = sum(row['recent_evidence_eligible'] for row in rows)
            status = 'partial' if isinstance(payload, dict) and payload.get('incomplete_results') else ('ok' if counts['valid_items'] else 'no-results')
        except Exception as exc:
            status = _classify_error(exc)
            rows = []
        record = {'id': item['id'], 'source': item['source'], 'kind': 'comments' if parent else 'search',
                  'status': status, **counts, 'items': len(rows), 'started_at': started,
                  'finished_at': datetime.now(tz=timezone.utc).isoformat(),
                  'duration_ms': round((clock.monotonic() - tick) * 1000, 3)}
        if parent:
            record['parent_item_id'] = parent['id']
        return record, rows

    # map 保持计划顺序，归并与计数不受请求完成顺序影响。
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        fetched = list(pool.map(fetch, plan['requests']))
    seen: dict[str, dict] = {}
    duplicates = 0
    for record, rows in fetched:
        retained = []
        for row in rows:
            marker = canonical_url(row['url'])
            if marker in seen:
                duplicates += 1
                existing = seen[marker]
                existing['query_ids'] = list(dict.fromkeys([*existing['query_ids'], row['query_id']]))
                for key in ('request_ids', 'intent_refs', 'query_metadata'):
                    for value in row[key]:
                        if value not in existing[key]:
                            existing[key].append(value)
                quality = assess_quality(existing, query=row['query'], as_of=plan['as_of'], window=plan['window'])
                if _rank({**existing, **quality}) > _rank(existing):
                    existing.update(quality, query=row['query'])
                continue
            row['query_ids'] = [row['query_id']]
            seen[marker] = row
            evidence.append(row)
            retained.append(row)
        # 请求 ok 表达请求有近期相关结果；去重只影响独立保留数量。
        record['retained'] = record['items'] = len(retained)
        request_results.append(record)
    evidence.sort(key=_rank, reverse=True)
    if include_comments:
        parents = [row for row in sorted(evidence, key=_rank, reverse=True) if _comment_request(row, max_comments_per_thread)]
        for parent in parents[:max_comment_threads]:
            endpoint, params = _comment_request(parent, max_comments_per_thread)
            if parent['source'] == 'github':
                params['since'] = plan['window']['range_from'] + 'T00:00:00Z'
            item = {'id': parent['query_id'] + '-comments-' + parent['source_item_id'], 'source': parent['source'],
                    'endpoint': endpoint, 'params': params, 'relevance_query': parent['query']}
            record, rows = fetch(item, parent)
            unique_rows = {row['id']: row for row in rows}
            comments.extend(unique_rows.values())
            record['retained'] = record['items'] = len(unique_rows)
            record['valid_items'] = sum(row['recent_evidence_eligible'] for row in unique_rows.values())
            request_results.append(record)
    status = plan['plan_status'] if not plan['requests'] and plan.get('plan_status') in {'needs_host_queries', 'not_requested'} else 'completed'
    all_rows = [*evidence, *comments]
    stats = {key: sum(row[key] for row in request_results) for key in ('fetched', 'parsed', 'relevant', 'retained')}
    stats.update({'requests': len(request_results), 'valid_items': sum(row['recent_evidence_eligible'] for row in evidence),
                  'valid_comments': sum(row['recent_evidence_eligible'] for row in comments), 'duplicates_removed': duplicates,
                  'window_status': dict(Counter(row['window_status'] for row in all_rows)),
                  'relevance_status': dict(Counter(row['relevance_status'] for row in all_rows)),
                  'by_source': dict(sorted(Counter(row['source'] for row in all_rows).items())),
                  'source_status': aggregate_status(request_results) if status == 'completed' else
                                   {source: 'skipped-policy' for source in sorted(ALLOWED_SOURCES)}})
    return {'schema_version': plan['schema_version'], 'provider': 'community-public', 'run_id': plan['run_id'],
            'as_of': plan['as_of'], 'stage': 'community_normalized', 'generated_at': observed_at,
            'plan_sha256': plan_sha256, 'window': plan['window'], 'status': status,
            'required_queries': plan.get('required_queries', []), 'stats': stats,
            'requests': request_results, 'evidence': evidence, 'comments': comments}

