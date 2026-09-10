#!/usr/bin/env python3
"""运行脱敏的离线采集场景，输出可跨版本比较的确定性基线。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import aor_bootstrap  # noqa: F401
from aor.evidence.quality import QUALITY_VERSION, assess_quality
from community_query import build_community_plan, execute_plan

ROOT = Path(__file__).resolve().parents[1]


def evaluate(fixtures: dict[str, Any]) -> dict[str, Any]:
    cases = []

    def check(case_id: str, actual: dict, expected: dict) -> None:
        measured = {}
        for path in expected:
            value: Any = actual
            for key in path.split('.'):
                value = value[key]
            measured[path] = value
        cases.append({'id': case_id, 'passed': measured == expected, 'actual': measured, 'expected': expected})

    for case in fixtures['quality_cases']:
        check(case['id'], assess_quality(case['item'], query=case['query'], as_of=fixtures['as_of']), case['expected'])
    plan = build_community_plan(as_of=fixtures['as_of'], run_id='RUN-20260714-ABCDEF1234',
                                focus_name='fixture', custom_focus='invoice reconciliation')

    def partial_transport(**kwargs):
        if 'algolia' in kwargs['endpoint']:
            if 'paid' in kwargs['params'].get('query', ''):
                raise RuntimeError('rate-limited:429')
            return {'hits': fixtures['community']['hn_hits']}
        return {'items': []}

    result = execute_plan(plan, transport=partial_transport)
    check('community-partial-reposts-ranking', result, fixtures['community']['expected'])
    check('relevant-first', {'id': result['evidence'][0]['id']}, {'id': 'hackernews:101'})
    dive = fixtures['comment_dive']
    calls = []

    def comments_transport(**kwargs):
        calls.append(kwargs['endpoint'])
        if '/api/v1/items/' in kwargs['endpoint']:
            return dive['hn_thread']
        if kwargs['endpoint'].endswith('/comments'):
            return dive['github_comments']
        if 'algolia' in kwargs['endpoint']:
            return {'hits': dive['hn_hits']}
        return {'items': dive['github_items'] if 'updated:' in kwargs['params']['q'] else []}

    plan = build_community_plan(as_of=fixtures['as_of'], run_id='RUN-20260714-ABCDEF1234',
                                focus_name='fixture', custom_focus='invoice', include_recent_activity=True)
    result = execute_plan(plan, transport=comments_transport, include_comments=True)
    check('dedupe-before-comments-and-old-activity', result, dive['expected'])
    check('one-comment-request-per-thread', {'calls': len(set(calls)), 'comments': len(result['comments']),
          'authors': sorted(row['author'] for row in result['comments'])},
          {'calls': 4, 'comments': 3, 'authors': ['synthetic-buyer-a', 'synthetic-buyer-b', 'synthetic-buyer-c']})
    old_issue = next(row for row in result['evidence'] if row['source'] == 'github')
    check('activity-does-not-rewrite-published-date', old_issue,
          {'window_status': 'out_of_window', 'activity_window_status': 'in_window', 'recent_evidence_eligible': False})
    check('comment-parent-chain', result['comments'][1],
          {'parent_comment_id': '211', 'parent_item_id': 'hackernews:201', 'relevance_status': 'unknown'})
    return {'eval_version': '1.0', 'quality_version': QUALITY_VERSION, 'fixture_version': fixtures['fixture_version'],
            'fixture_sha256': hashlib.sha256(json.dumps(fixtures, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'network_called': False, 'passed': all(case['passed'] for case in cases),
            'cases_passed': sum(case['passed'] for case in cases), 'cases_total': len(cases), 'cases': cases}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=ROOT / 'evals/research-quality.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.fixtures.read_text(encoding='utf-8')))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered, end='')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
