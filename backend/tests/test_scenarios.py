import asyncio
import copy
import json
import re
from functools import wraps
from types import SimpleNamespace

import pytest

from evaluation.scenarios import build_fingerprints, load_dataset, validate_dataset
from evaluation.scenario_runner import ScenarioRunner, main, _latency_summary


def run_async(function):
    @wraps(function)
    def execute(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return execute


def dataset(assertions=None, turns=None):
    return {'id': 'test', 'version': '1', 'status': 'synthetic_pending_review',
            'knowledge': [{'id': 'policy', 'title': '演示政策', 'content': '仅演示，不是实际承诺。', 'version': '1'}],
            'cases': [{'id': 'case', 'scenario': 'routing', 'description': '独立路由',
                       'turns': turns or [{'message': '你好', 'expected_intent': 'greeting'}],
                       'initial_state': {}, 'budgets': {'turn_timeout_s': 0.05},
                       'assertions': assertions or [{'id': 'intent', 'type': 'intent', 'turn': 0, 'expected': 'greeting'}]}]}


def reply(**values):
    return {'response': '您好，请描述问题。', 'intent': 'greeting', 'entities': {},
            'primary_agent': 'general', 'agent_types': ['general'], 'tools_used': [],
            'tool_traces': [], 'stage_latency_ms': {'memory': 1}, 'latency_ms': 4,
            'profile_status': 'completed', **values}


class Runtime:
    def __init__(self, actions=None):
        self.calls, self.seeded, self.closed = [], [], False
        self.actions = list(actions or [])
        self.service = SimpleNamespace(chat=self.chat)
        self.memory = SimpleNamespace(add_message=self.add_message, archive_session=self.archive_session)
        self.orchestrator = object()

    async def chat(self, message, user_id, conv_id, *, wait_for_profile):
        self.calls.append((message, user_id, conv_id, wait_for_profile))
        action = self.actions.pop(0) if self.actions else reply()
        if isinstance(action, Exception):
            raise action
        if action == 'timeout':
            await asyncio.sleep(0.2)
        return action

    async def add_message(self, *args):
        self.seeded.append(args)

    async def archive_session(self, user_id, conv_id):
        return {'evidence_id': 'mem_fixture', 'source_conv': conv_id}

    async def close(self, cleanup=False):
        self.closed = cleanup


@run_async
async def test_initialization_rollback_status_is_reported_instead_of_not_created():
    class BuildFailure(RuntimeError):
        cleanup_status = 'rolled_back'
        cleanup_errors = ()
        error_type = 'ValueError'
    async def unavailable(**kwargs):
        raise BuildFailure('Runtime initialization failed')
    report = await ScenarioRunner(unavailable).run(dataset())
    assert report['total'] == 1 and report['error'] == 1 and report['passed'] == 0
    assert report['cleanup'] == {'status': 'rolled_back', 'error_type': 'ValueError', 'errors': []}


def test_builtin_dataset_has_reviewable_diverse_coverage():
    data = load_dataset()
    summary = validate_dataset(data)
    assert summary['case_count'] == 120
    assert summary['scenario_count'] == 8
    assert len(summary['intents']) == 19
    assert data['status'] == 'synthetic_pending_review'
    assert len({c['description'] for c in data['cases']}) == 120
    assert len({c['turns'][0]['message'] for c in data['cases']}) == 120
    assert all(c['assertions'] for c in data['cases'])
    assert all('演示' in d['content'] and '实际承诺' in d['content'] for d in data['knowledge'])


@pytest.mark.parametrize('mutation', [
    lambda d: d['cases'][0]['assertions'][0].update(type='pretend_success'),
    lambda d: d['cases'][0]['assertions'][0].update(turn=9),
    lambda d: d['cases'][0]['turns'][0].update(response='gold answer'),
    lambda d: d['cases'][0]['budgets'].update(turn_timeout_s=float('nan')),
    lambda d: d['cases'][0]['assertions'][0].update(unrecognised=True),
    lambda d: d.update(cases=[]),
])
def test_invalid_schema_rejected(mutation):
    data = dataset()
    mutation(data)
    with pytest.raises(ValueError):
        validate_dataset(data)


@run_async
async def test_runtime_and_users_isolated_seed_history_and_finally_cleanup():
    runtimes, kwargs = [], []
    async def factory(**kw):
        kwargs.append(kw)
        rt = Runtime()
        runtimes.append(rt)
        return rt
    data = dataset()
    data['cases'][0]['initial_state'] = {'messages': [{'session': 'prior', 'role': 'user', 'content': '上次问过退货'}]}
    second = copy.deepcopy(data['cases'][0]); second['id'] = 'case2'
    data['cases'].append(second)
    runner = ScenarioRunner(factory)
    first = await runner.run(data)
    last = await runner.run(data)
    assert first['total'] == first['passed'] == 2
    assert all(rt.closed for rt in runtimes)
    assert len(runtimes) == 2
    assert all(re.fullmatch(r'eval_[0-9a-f]{32}', k['namespace']) for k in kwargs)
    assert kwargs[0]['namespace'] != kwargs[1]['namespace']
    assert kwargs[0]['knowledge_documents'] == data['knowledge']
    assert len({call[1] for rt in runtimes for call in rt.calls}) == 4
    assert all(call[3] for rt in runtimes for call in rt.calls)
    assert runtimes[0].seeded[0][0] == runtimes[0].calls[0][1]
    assert runtimes[0].seeded[0][1] != runtimes[0].calls[0][2]
    assert first['run_id'] != last['run_id']
    json.dumps(first, allow_nan=False)


@run_async
async def test_timeout_errors_and_invalid_remain_in_denominator_and_continue():
    rt = Runtime(['timeout', RuntimeError('broken'), reply()])
    data = dataset(turns=[{'message': s, 'expected_intent': 'greeting'} for s in ('a', 'b', 'c')])
    data['cases'][0]['assertions'] = [{'id': 'semantic', 'type': 'semantic', 'turn': 2,
                                     'expected': '尊重用户', 'evidence': ['policy']}]
    report = await ScenarioRunner(lambda **_: rt).run(data)
    assert report['passed'] == 0
    assert report['summary']['turns']['attempted'] == 3
    assert report['summary']['turns']['timeout'] == 1
    assert report['summary']['turns']['error'] == 1
    assert report['invalid'] == 1
    assert rt.closed and len(rt.calls) == 3


@run_async
async def test_task_cannot_average_away_a_hard_failure():
    rt = Runtime([reply(intent='refund'), reply()])
    data = dataset(turns=[{'message': 'a', 'expected_intent': 'refund'}, {'message': 'b', 'expected_intent': 'greeting'}],
                   assertions=[{'id': 'hard', 'type': 'tool_absent', 'turn': 0, 'tool': 'refund_execute', 'hard': True},
                               {'id': 'last', 'type': 'intent', 'turn': 1, 'expected': 'greeting'}])
    rt.actions[0]['tool_traces'] = [{'tool_name': 'refund_execute', 'success': True, 'input': {}, 'data': {}}]
    report = await ScenarioRunner(lambda **_: rt).run(data)
    assert report['passed'] == 0 and report['hard_fail'] == 1
    assert report['cases'][0]['status'] == 'hard_fail'
    assert report['cases'][0]['assertions'][1]['status'] == 'passed'


@run_async
async def test_structured_tool_parameters_results_and_evidence():
    trace = {'tool_name': 'knowledge_search', 'success': True, 'input': {'query': '退款'},
             'data': [{'evidence_id': 'policy', 'source': 'synthetic', 'eligible': False}]}
    data = dataset(assertions=[
        {'id': 'tool', 'type': 'tool_called', 'turn': 0, 'tool': 'knowledge_search'},
        {'id': 'arg', 'type': 'tool_argument', 'turn': 0, 'tool': 'knowledge_search', 'path': 'query', 'expected': '退款'},
        {'id': 'result', 'type': 'tool_result', 'turn': 0, 'tool': 'knowledge_search', 'path': '0.eligible', 'expected': False},
        {'id': 'source', 'type': 'evidence', 'turn': 0, 'expected': ['policy']},
        {'id': 'task', 'type': 'field_equals', 'scope': 'task', 'path': 'turns.0.intent', 'expected': 'greeting'},
    ])
    rt = Runtime([reply(tool_traces=[trace], tools_used=['knowledge_search'])])
    report = await ScenarioRunner(lambda **_: rt).run(data)
    assert report['passed'] == 1


@run_async
async def test_semantics_require_evidence_and_strict_judge_scores():
    class Judge:
        async def judge(self, question, response, **kwargs):
            assert kwargs['expected'] == '遵守演示政策'
            assert kwargs['evidence'][0]['id'] == 'policy'
            return {'relevance': 1, 'accuracy': float('nan'), 'completeness': 1, 'helpfulness': 1}
    data = dataset(assertions=[{'id': 'policy', 'type': 'semantic', 'turn': 0,
                                'expected': '遵守演示政策', 'evidence': ['policy']}])
    report = await ScenarioRunner(lambda **_: Runtime(), judge=Judge()).run(data)
    assert report['invalid'] == 1 and report['passed'] == 0
    assert report['cases'][0]['assertions'][0]['scores'] == {}


@run_async
async def test_factory_and_cleanup_errors_are_visible():
    def fail(**_): raise RuntimeError('construction failed')
    report = await ScenarioRunner(fail).run(dataset())
    assert report['error'] == 1 and report['total'] == 1 and report['passed'] == 0
    rt = Runtime()
    async def fail_close(cleanup=False): raise RuntimeError('cleanup failed')
    rt.close = fail_close
    report = await ScenarioRunner(lambda **_: rt).run(dataset())
    assert report['cleanup']['status'] == 'error'
    assert report['status'] == 'error'


def test_validate_cli_reports_validation_and_keeps_dataset_status(capsys):
    assert main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['mode'] == 'validate' and output['status'] == 'validated'
    assert output['dataset_status'] == 'synthetic_pending_review'
    assert output['case_count'] == 120


def test_fingerprints_are_safe_and_capture_real_variant_configuration(monkeypatch):
    config = {'model': 'demo', 'api_key': 'secret-key', 'base_url': 'https://secret.example',
              'retrieval_timeout_s': 30, 'adaptive_rules_version': '1'}
    first = build_fingerprints(dataset(), config)
    assert 'secret' not in json.dumps(first)
    assert set(first) == {'dataset', 'model', 'prompt', 'skills', 'knowledge', 'code', 'judge', 'retrieval'}
    config['retrieval_timeout_s'] = 2
    assert build_fingerprints(dataset(), config)['retrieval'] != first['retrieval']
    monkeypatch.setenv('ECHOMIND_TECHNICAL_MODEL', 'alternate')
    assert build_fingerprints(dataset(), config)['model'] != first['model']


@run_async
async def test_document_level_evidence_and_tools_used_cannot_hide_forbidden_call():
    data = dataset(assertions=[{'id': 'doc', 'type': 'evidence', 'turn': 0, 'expected': ['policy']},
                               {'id': 'forbidden', 'type': 'tool_absent', 'turn': 0, 'tool': 'refund_execute', 'hard': True}])
    trace = {'tool_name': 'search_knowledge_base', 'success': True, 'document_ids': ['policy'],
             'evidence_ids': ['ctx_versioned_chunk']}
    rt = Runtime([reply(tool_traces=[trace], tools_used=['refund_execute'])])
    result = await ScenarioRunner(lambda **_: rt).run(data)
    assert result['cases'][0]['assertions'][0]['status'] == 'passed'
    assert result['hard_fail'] == 1


def test_live_cli_limits_selected_cases_and_validate_always_checks_all(monkeypatch, capsys):
    captured = []
    async def fake_live(args, selected):
        captured.append(selected)
        return {'case_count': len(selected['cases'])}
    monkeypatch.setattr('evaluation.scenario_runner._live', fake_live)
    main(['--mode', 'live', '--scenario', 'billing_refund'])
    assert len(captured[0]['cases']) == 5
    assert {case['scenario'] for case in captured[0]['cases']} == {'billing_refund'}
    capsys.readouterr()
    main(['--mode', 'validate', '--limit', '1'])
    assert json.loads(capsys.readouterr().out)['case_count'] == 120


@run_async
async def test_live_without_key_never_constructs_runtime(monkeypatch):
    import sys
    from evaluation.scenario_runner import _live
    def forbidden(*args, **kwargs):
        raise AssertionError('runtime should not be constructed')
    fake = SimpleNamespace(create_runtime=forbidden, runtime_config_from_env=lambda: {'api_key': ''})
    monkeypatch.setitem(sys.modules, 'services.runtime', fake)
    with pytest.raises(ValueError, match='ANTHROPIC_API_KEY'):
        await _live(SimpleNamespace(judge=False), dataset())


def test_latency_percentiles_include_sample_count_and_empty_values():
    empty = _latency_summary([])
    assert empty['p50_ms'] is None and empty['p95_ms'] is None and empty['sample_count'] == 0
    observed = _latency_summary([10, 20, 30, 40])
    assert observed['p50_ms'] == 25 and observed['p95_ms'] == 38.5
    assert observed['sample_count'] == 4 and observed['small_sample_estimate'] is True


@run_async
async def test_evidence_recall_and_semantic_hard_failure_are_not_averaged_away():
    data = dataset(assertions=[{'id': 'doc', 'type': 'evidence', 'turn': 0, 'expected': ['policy'],
                               'evidence_ids': ['ctx_expected_version']},
                              {'id': 'policy', 'type': 'semantic', 'turn': 0, 'hard': True,
                               'expected': '不得承诺已退款', 'evidence': ['policy']}])
    trace = {'tool_name': 'search_knowledge_base', 'success': True, 'document_ids': ['policy'],
             'evidence_ids': ['ctx_wrong_version']}
    class Judge:
        async def judge(self, *args, **kwargs):
            return {'relevance': 1, 'accuracy': 0, 'completeness': 1, 'helpfulness': 1}
    result = await ScenarioRunner(lambda **_: Runtime([reply(tool_traces=[trace])]), judge=Judge()).run(data)
    assert result['hard_fail'] == 1
    assert result['cases'][0]['assertions'][0]['scores']['recall'] == .5
    assert result['evidence_recall']['mean'] == .5 and result['evidence_recall']['valid_count'] == 1


@run_async
async def test_live_uses_fixed_judge_model_instead_of_chat_variant(monkeypatch, tmp_path):
    import sys
    from evaluation.scenario_runner import _live
    selected_models = []
    class Client:
        def __init__(self, **kwargs):
            self.messages = SimpleNamespace(create=self.create)
        async def create(self, **kwargs):
            raise AssertionError('This fixture must not call a provider')
        async def close(self): pass
    def make_judge(client, model):
        selected_models.append(model)
        return SimpleNamespace()
    config = {'api_key': 'configured-key', 'model': 'chat-variant', 'judge_model': 'fixed-judge'}
    monkeypatch.setitem(sys.modules, 'services.runtime', SimpleNamespace(
        create_runtime=lambda *a, **kw: Runtime(), runtime_config_from_env=lambda: config))
    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(AsyncAnthropic=Client))
    monkeypatch.setitem(sys.modules, 'evaluation.evaluator', SimpleNamespace(LLMJudge=make_judge))
    await _live(SimpleNamespace(judge=True, report_dir=tmp_path), dataset())
    assert selected_models == ['fixed-judge']
