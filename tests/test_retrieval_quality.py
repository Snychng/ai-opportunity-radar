"""离线研究行为回归：质量语义、失败可见性、评论与默认边界。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from community_query import CommunityPlanError, build_community_plan, execute_plan, validate_plan
from evaluate_research import evaluate
from normalize_tikhub_results import normalize_documents
from tests.test_normalize_tikhub_results import _document, _result


class ResearchQualityTests(unittest.TestCase):
    def plan(self, **kwargs):
        return build_community_plan(as_of='2026-07-14', run_id='RUN-20260714-ABCDEF1234',
                                    focus_name='fixture', custom_focus='invoice', **kwargs)

    def test_versioned_behavior_eval(self):
        fixtures = json.loads((ROOT / 'evals/research-quality.json').read_text())
        result = evaluate(fixtures)
        self.assertTrue(result['passed'], [case for case in result['cases'] if not case['passed']])
        baseline = json.loads((ROOT / 'evals/research-quality-baseline.json').read_text())
        self.assertEqual(result, baseline)

    def test_zero_relevance_is_not_success(self):
        def transport(**kwargs):
            if 'algolia' in kwargs['endpoint']:
                return {'hits': [{'objectID': '1', 'title': 'Volcano photo contest', 'created_at': '2026-07-14'}]}
            return {'items': []}
        result = execute_plan(self.plan(), transport=transport)
        self.assertEqual(result['stats']['fetched'], 2)
        self.assertEqual(result['stats']['parsed'], 2)
        self.assertEqual(result['stats']['relevant'], 0)
        self.assertEqual(result['stats']['valid_items'], 0)
        self.assertEqual(result['evidence'], [])
        self.assertNotIn('ok', result['stats']['source_status'].values())

    def test_missing_host_queries_skips_without_network(self):
        plan = self.plan()
        plan.update(requests=[], plan_status='needs_host_queries', required_queries=['English query'])
        def transport(**kwargs):
            self.fail('等待宿主查询时不能访问网络')
        result = execute_plan(plan, transport=transport)
        self.assertEqual(result['status'], 'needs_host_queries')
        self.assertEqual(result['stats']['requests'], 0)
        self.assertEqual(set(result['stats']['source_status'].values()), {'skipped-policy'})
        plan['plan_status'] = 'not_requested'
        skipped = execute_plan(plan, transport=transport)
        self.assertEqual(skipped['status'], 'not_requested')
        self.assertEqual(skipped['stats']['requests'], 0)
        plan.pop('plan_status')
        with self.assertRaises(CommunityPlanError):
            validate_plan(plan)

    def test_comment_failure_remains_visible_and_default_does_not_deepen(self):
        calls = []
        def transport(**kwargs):
            endpoint = kwargs['endpoint']
            calls.append(endpoint)
            if '/items/' in endpoint:
                raise RuntimeError('network-error')
            if 'algolia' in endpoint:
                return {'hits': [{'objectID': '1', 'title': 'Invoice work', 'created_at': '2026-07-14'}]}
            return {'items': []}
        result = execute_plan(self.plan(), transport=transport, concurrency=2)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result['comments'], [])
        result = execute_plan(self.plan(), transport=transport, include_comments=True)
        self.assertEqual(result['stats']['valid_items'], 1)
        self.assertEqual(result['stats']['source_status']['hackernews'], 'partial')
        failed = result['requests'][-1]
        self.assertEqual(failed['kind'], 'comments')
        self.assertEqual(failed['status'], 'error')
        self.assertIn('started_at', failed)
        self.assertGreaterEqual(failed['duration_ms'], 0)

    def test_reposts_do_not_replace_original_author_with_popular_copy(self):
        def transport(**kwargs):
            url = 'https://github.com/synthetic/fixture/issues/1'
            if 'algolia' in kwargs['endpoint']:
                return {'hits': [{'objectID': '1', 'title': 'Invoice workflow', 'url': url,
                                  'author': 'original-author', 'created_at': '2026-07-14'}]}
            return {'items': [{'id': 2, 'title': 'Invoice workflow', 'html_url': url,
                               'user': {'login': 'different-author'}, 'comments': 9999,
                               'created_at': '2026-07-14'}], 'incomplete_results': True}
        result = execute_plan(self.plan(), transport=transport)
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual(result['evidence'][0]['author'], 'original-author')
        self.assertEqual(result['evidence'][0]['source'], 'hackernews')
        self.assertEqual(result['stats']['valid_items'], 1)
        self.assertEqual(result['stats']['source_status']['github'], 'partial')

    def test_recent_activity_requires_explicit_plan_option(self):
        plan = self.plan(include_recent_activity=True)
        validate_plan(plan)
        plan.pop('include_recent_activity')
        with self.assertRaises(CommunityPlanError):
            validate_plan(plan)

    def test_paid_deduplication_keeps_all_request_intents(self):
        first = _result('youtube', {'videos': [{'video_id': 'a', 'title': 'Invoice process'}]}, query_id='first')
        first.update(request_ids=['first'], intent_refs=[{'id': 'cost'}], query_metadata=[{'id': 'first'}])
        second = {**first, 'id': 'second', 'request_ids': ['second', 'third'],
                  'intent_refs': [{'id': 'pain'}], 'query_metadata': [{'id': 'second'}, {'id': 'third'}]}
        result = normalize_documents([_document([first, second])])
        self.assertEqual(len(result['evidence']), 1)
        item = result['evidence'][0]
        self.assertEqual(item['request_ids'], ['first', 'second', 'third'])
        self.assertEqual(item['intent_refs'], [{'id': 'cost'}, {'id': 'pain'}])
        self.assertEqual(item['query_ids'], ['first', 'second'])

    def test_paid_dates_and_mixed_status_preserve_legacy_counts(self):
        result = _result('youtube', {'videos': [
            {'video_id': 'a', 'title': 'Invoice process', 'published_time': '2020-01-01'},
            {'video_id': 'b', 'title': 'Invoice process', 'published_time': '2026-07-13'},
            {'video_id': 'c', 'title': 'Invoice process'},
        ]})
        result['params'] = {'keyword': 'invoice'}
        result.update(request_ids=['query-a', 'query-b'], intent_refs=[{'id': 'pain'}, {'id': 'cost'}],
                      query_metadata=[{'query_id': 'query-a', 'language': 'en'}, {'query_id': 'query-b', 'language': 'en'}])
        failure = {**result, 'id': 'failed', 'status': 'error', 'error_code': 'rate_limited'}
        normalized = normalize_documents([_document([result, failure])])
        self.assertEqual(normalized['stats']['valid_items'], 3)
        for item in normalized['evidence']:
            for key in ('request_ids', 'intent_refs', 'query_metadata'):
                self.assertEqual(item[key], result[key])
                self.assertIsNot(item[key], result[key])
        self.assertEqual([item['window_status'] for item in normalized['evidence']],
                         ['out_of_window', 'in_window', 'unknown'])
        self.assertEqual(normalized['stats']['recent_valid_items'], 1)
        self.assertEqual(normalized['stats']['source_status']['youtube'], 'partial')
        self.assertEqual(normalized['request_statuses'][-1]['status'], 'rate-limited')


if __name__ == '__main__':
    unittest.main()
