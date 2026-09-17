"""评论优先研究：原文绑定、跨平台分页、预算恢复与公开边界。"""
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import aor_bootstrap  # noqa: F401
import tikhub_query as tq
from normalize_tikhub_results import normalize_documents
from aor.opportunity.needs import build_user_discovery, validate_user_discovery
from aor.sources.comments import start_collection, advance_collection, collection_plan, page_metadata, collection_coverage
from aor.sources.products import product_intents
from aor.sources.industries import load_industries
from aor.reporting.public import export_public
from aor.reporting.report import validate_structured_report
from aor.workflow.research import start_research, resume_research, run_comment_collection
from tests.test_evidence_claims import evidence
from tests.test_research_workflow import write
from tests.test_tikhub_query import healthy_account_transport

RUN = 'RUN-20260916-ABCDEF1234'
DAY = '2026-09-16'
INDUSTRY = load_industries()[0]['id']


def selection(source='douyin', identity='100'):
    identifiers = {'douyin': {'aweme_id': identity}, 'xiaohongshu': {'note_id': identity},
                   'twitter': {'tweet_id': identity}}[source]
    return {'source': source, 'selected_item_id': source + ':' + identity,
            'parent_url': 'https://example.com/post/' + identity, 'selection_reason': '产品实际使用体验讨论',
            'content_type': 'image_note' if source == 'xiaohongshu' else None,
            'product': 'TestProduct', 'industry_ids': [INDUSTRY], 'identifiers': identifiers}


def prices():
    return [{'endpoint_uri': endpoint, 'endpoint_cost': 0.001, 'allow_free_credit': False,
             'allow_discount': False, 'platform': p['source']} for endpoint, p in tq.COMMENT_PROFILES.items()] + [
                {'endpoint_uri': tq.ACCOUNT_INFO_ENDPOINT, 'endpoint_cost': 0, 'allow_free_credit': True, 'allow_discount': False}]


def execution(state, data=None):
    return {'schema_version': '3.0', 'provider': 'tikhub', 'run_id': RUN, 'as_of': DAY,
            'executed_at': DAY + 'T08:00:00+08:00', 'stage': 'comment_deep_dive',
            'results': [{**r, 'status': 'ok', 'response': {'code': 200, 'data':
                ({'aweme_detail': {'aweme_id': '100', 'desc': '产品测评'}} if r['kind'] == 'detail' else
                 data or {'comments': [{'cid': 'c1', 'text': '一直在用，导出太慢', 'reply_count': 2}], 'cursor': 20, 'has_more': 1})}}
                for r in state['pending'][:100]]}


def observation(**changes):
    return {'product': 'TestProduct', 'target_user': '用户', 'task': '导出报告', 'need': '缩短导出耗时',
            'industry_ids': [INDUSTRY], 'feedback_type': 'usage', 'sentiment': 'negative',
            'evidence_refs': [{'evidence_id': 'EVID-1', 'revision_id': 'REV-1', 'quote': 'Setup still takes hours.'}], **changes}


class ObservationTests(unittest.TestCase):
    def test_early_needs_without_benchmark_ai_or_mvp_and_dedup(self):
        result = build_user_discovery([observation(), observation()], [evidence()], as_of=DAY, run_id=RUN)
        self.assertEqual(result['summary']['observation_count'], 1)
        self.assertEqual(result['summary']['demand_cluster_count'], 1)
        self.assertIsNone(result['demand_clusters'][0]['independent_user_count'])
        self.assertFalse(result['summary']['market_validated'])
        validate_user_discovery(result, [evidence()], as_of=DAY, run_id=RUN)

    def test_promotion_unknown_and_demo_not_direct_demand(self):
        for kind in ('promotion', 'official_response', 'suspected_spam', 'unknown'):
            result = build_user_discovery([observation(feedback_type=kind)], [evidence()], as_of=DAY, run_id=RUN)
            self.assertEqual(result['summary']['observation_count'], 1)
            self.assertEqual(result['demand_clusters'], [])
        result = build_user_discovery([observation()], [evidence(is_demo=True)], as_of=DAY, run_id=RUN)
        self.assertEqual(result['demand_clusters'], [])

    def test_reject_forged_retracted_future_and_tampered_counts(self):
        for change in ({'quote': 'invented quote'}, {'revision_id': 'missing'}, {'revision_id': None}):
            obs = observation()
            obs['evidence_refs'][0].update(change)
            with self.assertRaises(ValueError):
                build_user_discovery([obs], [evidence()], as_of=DAY, run_id=RUN)
        for record in (evidence(retracted=True), evidence(observed_at='2026-09-17')):
            with self.assertRaises(ValueError):
                build_user_discovery([observation()], [record], as_of=DAY, run_id=RUN)
        result = build_user_discovery([observation()], [evidence()], as_of=DAY, run_id=RUN)
        result['summary']['observation_count'] = 999
        with self.assertRaises(ValueError):
            validate_user_discovery(result, [evidence()], as_of=DAY, run_id=RUN)

    def test_two_quotes_same_need_canonical_order_and_conflicting_labels(self):
        first, second = observation(), observation()
        second['evidence_refs'][0]['quote'] = 'I paid $29'
        second['feedback_type'] = 'purchase_claim'
        result = build_user_discovery([first, second], [evidence()], as_of=DAY, run_id=RUN)
        validate_user_discovery(result, [evidence()], as_of=DAY, run_id=RUN)
        self.assertEqual(result, build_user_discovery([second, first], [evidence()], as_of=DAY, run_id=RUN))
        self.assertEqual(result['summary']['demand_cluster_count'], 1)
        with self.assertRaises(ValueError):
            build_user_discovery([first, observation(feedback_type='promotion')], [evidence()], as_of=DAY, run_id=RUN)

    def test_observation_only_full_workflow_and_public_counts(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'AOR_OFFLINE': '1'}):
            home = Path(temp)
            original = evidence()
            for key in ('evidence_id', 'revision_id', 'run_id'):
                original.pop(key)
            material = write(home / 'material.json', {'evidence': [original]})
            run = start_research(home, as_of=date.fromisoformat(DAY), offline=True, evidence_files=[material])
            rows = json.loads(Path(run['artifacts']['evidence-context']['path']).read_text())['evidence']
            obs = observation(evidence_refs=[{'evidence_id': rows[0]['evidence_id'], 'revision_id': rows[0]['revision_id'],
                                             'quote': 'Setup still takes hours.'}])
            bench = write(home / 'bench.json', {'observations': [obs]})
            run = resume_research(home, run['run_id'], benchmarks_file=bench)
            assessment = write(home / 'assessment.json', {'scores': [], 'decision': {'summary': '发现一项待验证需求',
                'primary_id': None, 'largest_unknown': '用户实际任务', 'next_action': '访谈用户', 'stop_condition': '需求无法复现'}})
            run = resume_research(home, run['run_id'], assessment_file=assessment)
            self.assertEqual(run['status'], 'completed')
            report = json.loads(Path(run['artifacts']['report']['path']).read_text())
            self.assertTrue(validate_structured_report(report)['valid'])
            self.assertEqual(report['metrics']['qualified_conclusion_count'], 0)
            self.assertEqual(report['tiered']['user_discovery']['summary']['demand_cluster_count'], 1)
            public = export_public(report)
            serialized = json.dumps(public)
            self.assertNotIn('Setup still takes hours.', serialized)
            self.assertNotIn('TestProduct', serialized)
            self.assertIn('observation_count', serialized)
            stored = home / 'state/user-discovery' / (run['run_id'] + '.json')
            before = stored.read_bytes()
            resume_research(home, run['run_id'])
            self.assertEqual(stored.read_bytes(), before)
            report['tiered']['user_discovery']['summary']['observation_count'] = 8
            self.assertFalse(validate_structured_report(report)['valid'])


class ProductSearchTests(unittest.TestCase):
    def test_multiplatform_positive_negative_alias_queries_over_twenty(self):
        plan = product_intents({'products': [{'name': 'TestProduct', 'aliases': ['测试产品', 'TP'],
                                               'task': '报告导出', 'industry_ids': [INDUSTRY]}]})
        self.assertEqual(len(plan['intents']), 27)
        from build_query_plan import build_plan
        with tempfile.TemporaryDirectory() as temp:
            prepared = build_plan(date.fromisoformat(DAY), Path(temp), intent_plan=plan)
        self.assertEqual(len(prepared['retrieval_plans']['tikhub']['requests']), 27)
        paid = prepared['retrieval_plans']['tikhub']
        self.assertEqual(paid['cost_policy']['purpose'], 'cross_industry_discovery')
        self.assertIn('TestProduct', paid['discovery_objective'])
        from aor.sources.discovery import prepare_discovery_plan
        pricing = [{'endpoint_uri': r['endpoint'], 'endpoint_cost': 0.001,
                    'allow_free_credit': False, 'allow_discount': False, 'platform': r['source']}
                   for r in {r['endpoint']: r for r in paid['requests']}.values()]
        bounded = prepare_discovery_plan(paid, pricing, max_cost_usd=0.003, max_requests=100)
        self.assertEqual(len(bounded['requests']), 3)
        self.assertEqual(bounded['discovery_objective'], paid['discovery_objective'])
        self.assertEqual({r['source'] for r in plan['intents']}, {'xiaohongshu', 'douyin', 'twitter'})
        self.assertTrue(any('worth it' in r['search_query'] for r in plan['intents']))
        self.assertTrue(any('退款' in r['search_query'] for r in plan['intents']))

    def test_invalid_products_and_query_capacity(self):
        for value in ({'products': [None]}, {'products': []}, {'products': [{'name': 'p', 'task': 't', 'industry_ids': ['missing']}]},
                      {'products': [{'name': str(i), 'aliases': ['a' + str(i), 'b' + str(i)], 'task': 't',
                                     'industry_ids': [INDUSTRY]} for i in range(10)]}):
            with self.assertRaises(ValueError):
                product_intents(value)


class CommentPaginationTests(unittest.TestCase):
    def test_three_platform_plans_and_allowlist(self):
        state = start_collection({'selections': [selection(s) for s in ('douyin', 'xiaohongshu', 'twitter')]}, run_id=RUN, as_of=DAY)
        self.assertEqual(len(state['pending']), 7)
        tq.validate_plan(collection_plan(RUN, DAY, state['pending'], state['policy']), prices())
        advanced = advance_collection(state, execution(state))
        self.assertTrue(any(r['kind'] == 'comment_replies' for r in advanced['pending']))
        tq.validate_plan(collection_plan(RUN, DAY, advanced['pending'], state['policy']), prices())
        self.assertEqual(len(advanced['seen_comments']), 3)

    def test_cursor_loop_dedup_reply_parent_and_limits(self):
        state = start_collection({'selections': [selection()]}, run_id=RUN, as_of=DAY)
        state = advance_collection(state, execution(state))
        self.assertEqual(len(state['pending']), 2)
        response = execution(state, {'comments': [{'cid': 'c1', 'text': '一直在用，导出太慢'},
                                                 {'cid': 'c2', 'text': '同样的问题'}], 'has_more': 1, 'cursor': 20})
        parsed = normalize_documents([response])
        child = next(r for r in parsed['comments'] if r['source_item_id'] == 'c2')
        self.assertEqual(child['original_text'], '同样的问题')
        state = advance_collection(state, response)
        self.assertEqual(state['summary']['unique_comments'], 2)
        self.assertIn('repeated_cursor', {p['stop_reason'] for p in state['pages']})
        state = advance_collection(state, execution(state, {'comments': [], 'has_more': 0}))
        self.assertEqual(state['status'], 'completed')
        self.assertFalse(state['summary']['exhaustive'])

    def test_reply_normalization_inherits_requested_parent(self):
        state = start_collection({'selections': [selection('xiaohongshu')]}, run_id=RUN, as_of=DAY)
        state = advance_collection(state, execution(state))
        state['pending'] = [r for r in state['pending'] if r['kind'] == 'comment_replies']
        response = execution(state, {'comments': [{'id': 'reply2', 'content': '退款等了两周'}], 'has_more': False})
        parsed = normalize_documents([response])
        self.assertEqual(parsed['comments'][0]['parent_comment_id'], 'c1')
        self.assertEqual(parsed['comments'][0]['collection']['product'], 'TestProduct')

    def test_unknown_end_error_and_page_capacity_are_distinct(self):
        for payload, reason in (({'comments': []}, 'empty_or_unrecognized_page'),
                                ({'comments': [], 'has_more': 0}, 'empty_page'),
                                ({'comments': [{'cid': 'a', 'text': 'hello'}]}, 'pagination_unknown'),
                                ({'comments': [{'cid': 'a', 'text': 'hello'}], 'has_more': 0}, 'provider_end')):
            state = start_collection({'selections': [selection()]}, run_id=RUN, as_of=DAY)
            state = advance_collection(state, execution(state, payload))
            self.assertEqual(state['pages'][-1]['stop_reason'], reason)
            self.assertEqual(state['pending'], [])
        for policy, reason in (({'max_pages': 1}, 'page_limit'), ({'max_comments_per_post': 1}, 'comment_limit')):
            state = start_collection({'selections': [selection()], 'policy': policy}, run_id=RUN, as_of=DAY)
            state = advance_collection(state, execution(state))
            self.assertEqual(state['pages'][1]['stop_reason'], reason)
        state = start_collection({'selections': [selection()], 'policy': {'max_requests': 2}}, run_id=RUN, as_of=DAY)
        state = advance_collection(state, execution(state))
        self.assertEqual(state['pending'], [])
        self.assertGreater(state['summary']['deferred_request_count'], 0)
        state = start_collection({'selections': [selection()]}, run_id=RUN, as_of=DAY)
        response = execution(state)
        response['results'][1]['status'] = 'outcome_unknown'
        state = advance_collection(state, response)
        self.assertEqual(state['pending'], [])
        self.assertEqual(state['pages'][1]['stop_reason'], 'outcome_unknown')

    def test_x_graphql_native_ids_root_exclusion_and_cursor(self):
        state = start_collection({'selections': [selection('twitter')]}, run_id=RUN, as_of=DAY)
        data = {'instructions': [{'entries': [
            {'content': {'itemContent': {'tweet_results': {'result': {'legacy': {'id_str': '100', 'full_text': '原帖'}}}}}},
            {'content': {'itemContent': {'tweet_results': {'result': {'legacy': {'id_str': '101', 'full_text': 'I paid and still use it',
                                                                         'in_reply_to_status_id_str': '100'}}}}}},
            {'content': {'cursorType': 'Bottom', 'value': 'next-page'}}]}]}
        response = execution(state, data)
        parsed = normalize_documents([response])
        self.assertEqual(len(parsed['comments']), 1)
        self.assertEqual(parsed['comments'][0]['source_item_id'], '101')
        self.assertEqual(parsed['comments'][0]['url'], 'https://x.com/i/status/101')
        state = advance_collection(state, response)
        self.assertEqual(state['summary']['unique_comments'], 1)
        self.assertEqual(len(state['pending']), 2)
        self.assertEqual(collection_coverage([response, response])['unique_comments'], 1)
        self.assertEqual(page_metadata({'comments': [{'cursor': 'wrong', 'has_more': True}], 'has_more': False})['next_cursor'], None)

    def test_x_mixed_comment_endpoints_preserve_native_ids_and_exclude_root(self):
        state = start_collection({'selections': [selection('twitter')]}, run_id=RUN, as_of=DAY)
        data = {'thread': [{'id': '101', 'text': 'I paid but Pro did not activate'}],
                'timeline': [{'tweet_id': '100', 'text': 'Official product announcement'},
                             {'tweet_id': '101', 'text': 'I paid but Pro did not activate',
                              'in_reply_to_status_id_str': '100'},
                             {'tweet_id': '102', 'text': 'Same here', 'in_reply_to_status_id_str': '101'},
                             {'tweet_id': '999', 'conversation_id': '999', 'text': 'A separate quote post'}]}
        response = execution(state, data)
        parsed = normalize_documents([response])
        self.assertEqual({c['source_item_id'] for c in parsed['comments']}, {'101', '102'})
        child = next(c for c in parsed['comments'] if c['source_item_id'] == '102')
        self.assertEqual(child['url'], 'https://x.com/i/status/102')
        self.assertEqual(child['parent_comment_id'], '101')
        node = response
        for part in child['raw_json_pointer'].strip('/').split('/'):
            node = node[int(part)] if isinstance(node, list) else node[part]
        self.assertEqual(node['tweet_id'], child['source_item_id'])
        advanced = advance_collection(state, response)
        self.assertEqual(advanced['summary']['unique_comments'], 2)
        self.assertEqual(collection_coverage([response])['unique_comments'], 2)

    @patch('tikhub_query.datetime')
    def test_budget_stop_resume_and_recovery_do_not_rebuy_saved_pages(self, execution_clock):
        # 本例验证同轮分页恢复；采集时间与固定 as_of 一致，避免真实时钟跨日后倒写历史。
        executed_at = datetime.fromisoformat(DAY + 'T08:00:00+00:00')
        execution_clock.now.return_value = executed_at
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'AOR_OFFLINE': '1'}):
            home = Path(temp)
            run = start_research(home, as_of=date.fromisoformat(DAY))
            inputs = write(home / 'comments.json', {'selections': [selection()]})
            calls = []

            def transport(**kwargs):
                calls.append(kwargs['url'])
                if 'fetch_one_video' in kwargs['url']:
                    return {'code': 200, 'data': {'aweme_detail': {'aweme_id': '100', 'desc': '测试产品'}}}
                if str(kwargs['params'].get('cursor')) == '20':
                    return {'code': 200, 'data': {'comments': [{'cid': 'c2', 'text': '还有导出问题'}], 'has_more': 0}}
                return {'code': 200, 'data': {'comments': [{'cid': 'c1', 'text': '导出很慢'}], 'has_more': 1, 'cursor': 20}}

            def execute(plan, **options):
                return tq._execute_plan_with_pricing(plan, prices(), **{**options, 'token': 'synthetic'},
                    account_transport=healthy_account_transport, transport=transport)

            with patch('tikhub_query.execute_plan', side_effect=execute), patch.dict(os.environ, {'AOR_OFFLINE': '0'}):
                with patch('tikhub_query.execute_plan', side_effect=tq.PlanError('预检失败，批次尚未登记')):
                    with self.assertRaises(tq.PlanError):
                        run_comment_collection(home, run['run_id'], inputs, max_cost_usd=0.002)
                self.assertEqual(calls, [])
                with self.assertRaises(tq.BudgetExceeded):
                    run_comment_collection(home, run['run_id'], inputs, max_cost_usd=0.002, resume=True)
                self.assertEqual(len(calls), 2)
                with patch('aor.sources.comments.advance_collection', side_effect=OSError('中断保存分页状态')):
                    with self.assertRaises(OSError):
                        run_comment_collection(home, run['run_id'], inputs, max_cost_usd=0.01, resume=True)
                self.assertEqual(len(calls), 3)
                result = run_comment_collection(home, run['run_id'], inputs, max_cost_usd=0.01, resume=True)
                self.assertEqual(len(calls), 3)
                self.assertEqual(result['comment_collection']['unique_comments'], 2)
                manifest = json.loads((Path(result['run_path']) / 'run.json').read_text())
                self.assertTrue(manifest['execution_artifacts'])
                for name in manifest['execution_artifacts']:
                    payload = json.loads(Path(manifest['artifacts'][name]['path']).read_text())
                    self.assertEqual(payload['generated_at'], executed_at.isoformat())
                run_comment_collection(home, run['run_id'], inputs, max_cost_usd=0.01, resume=True)
                self.assertEqual(len(calls), 3)
                with self.assertRaises(ValueError):
                    run_comment_collection(home, run['run_id'], inputs, max_cost_usd=0.01)

    def test_more_than_one_hundred_pending_requests_are_not_lost(self):
        state = start_collection({'selections': [selection(identity=str(i)) for i in range(30)],
                                  'policy': {'max_requests': 400}}, run_id=RUN, as_of=DAY)
        rows = [{'cid': str(i), 'text': '产品实际体验', 'reply_count': 2} for i in range(5)]
        state = advance_collection(state, execution(state, {'comments': rows, 'has_more': 1, 'cursor': 20}))
        self.assertEqual(len(state['pending']), 180)
        state = advance_collection(state, execution(state, {'comments': [], 'has_more': 0}))
        self.assertEqual(len(state['pending']), 80)
        state = advance_collection(state, execution(state, {'comments': [], 'has_more': 0}))
        self.assertEqual(len(state['processed']), 240)
        self.assertEqual(state['pending'], [])

    def test_invalid_policy_parent_binding_and_duplicate_product_rejected(self):
        for policy in ({'max_pages': True}, {'max_pages': 0}, {'max_requests': 1001}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                start_collection({'selections': [selection()], 'policy': policy}, run_id=RUN, as_of=DAY)
        with self.assertRaises(ValueError):
            start_collection({'selections': [selection(), {**selection(), 'product': 'AnotherProduct'}]}, run_id=RUN, as_of=DAY)
        state = start_collection({'selections': [selection()]}, run_id=RUN, as_of=DAY)
        state = advance_collection(state, execution(state))
        plan = collection_plan(RUN, DAY, state['pending'], state['policy'])
        reply = next(r for r in plan['requests'] if r['kind'] == 'comment_replies')
        reply['collection']['parent_comment_id'] = 'unrelated'
        with self.assertRaises(tq.PlanError):
            tq.validate_plan(plan, prices())

    def test_cli_rejects_paid_comments_without_budget_and_offline(self):
        import research
        from contextlib import redirect_stderr
        import io
        for extra in ([], ['--offline', '--max-cost-usd', '1'], ['--no-collect', '--max-cost-usd', '1']):
            with patch('research.run_comment_collection') as execute, redirect_stderr(io.StringIO()):
                result = research.main(['resume', RUN, '--comments-file', 'unused.json', *extra])
                self.assertEqual(result, 2)
                execute.assert_not_called()

    def test_long_provider_cursor_is_bounded_without_relaxing_identifiers(self):
        state = start_collection({'selections': [selection('twitter')]}, run_id=RUN, as_of=DAY)
        state['pending'] = [r for r in state['pending'] if r['kind'] == 'top_level_comments']
        plan = collection_plan(RUN, DAY, state['pending'], state['policy'])
        for row in plan['requests']:
            row['params']['cursor'] = 'A' * 623
        tq.validate_plan(plan, prices())
        plan['requests'][0]['params']['cursor'] = 'A' * 4097
        with self.assertRaises(tq.PlanError):
            tq.validate_plan(plan, prices())
        plan['requests'][0]['params']['cursor'] = 'A' * 623
        plan['requests'][0]['params']['tweet_id'] = '9' * 501
        with self.assertRaises(tq.PlanError):
            tq.validate_plan(plan, prices())
