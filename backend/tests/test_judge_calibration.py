"""Frozen-response calibration never regenerates source conversations."""
import asyncio
import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from evaluation import batch_runner
from evaluation.scenarios import build_fingerprints, stable_hash


CONFIG = {'model': 'offline-model', 'judge_model': 'offline-judge', 'api_key': 'never-archive-key'}
FIELDS = ('relevance', 'accuracy', 'completeness', 'helpfulness')


def dataset():
    return {'id': 'calibration-fixture', 'version': '1', 'status': 'candidate',
            'knowledge': [{'id': 'policy', 'title': 'Policy', 'content': 'Fixed evidence', 'version': '1'}],
            'cases': [{'id': f'case_{i}', 'scenario': 'group', 'description': 'fixture',
                       'initial_state': {'messages': [{'role': 'user', 'content': 'seed', 'session': 'prior'}]},
                       'turns': [{'message': 'first', 'expected_intent': 'query'},
                                 {'message': 'second', 'expected_intent': 'query'}],
                       'budgets': {'judge_timeout_s': 1},
                       'assertions': [{'id': 'quality', 'type': 'semantic', 'expected': 'help',
                                       'evidence': ['policy'], **({'turn': 1} if i == 0 else {'scope': 'task'})}]}
                      for i in range(2)]}


def source(tmp_path, *, missing=False, count=2):
    data = dataset()
    if count != 2:
        data['cases'] = [dict(copy.deepcopy(data['cases'][0]), id=f'case_{i}') for i in range(count)]

    async def executor(args, subset):
        run_id = uuid.uuid4().hex
        fingerprints = build_fingerprints(subset, dict(CONFIG, judge_enabled=True))
        cases = []
        for case in subset['cases']:
            turns = [{'index': i, 'message': t['message'], 'status': 'passed',
                      'actual': {'response': f'answer {i}'}} for i, t in enumerate(case['turns'])]
            if missing and case['id'] == 'case_0':
                turns[1].pop('actual')
                turns[1]['status'] = 'error'
            cases.append({'id': case['id'], 'scenario': case['scenario'], 'status': 'failed',
                          'turns': turns, 'assertions': [{'id': 'quality', 'type': 'semantic',
                          'status': 'failed', 'scores': {name: .5 for name in FIELDS}}]})
        return {'run_id': run_id, 'namespace': 'eval_' + run_id, 'mode': 'live',
                'metadata': {'fingerprints': fingerprints}, 'dataset': {'fingerprint': fingerprints['dataset']},
                'cases': cases, 'total': len(cases), 'cleanup': {'status': 'completed'},
                'budget': {'max_model_calls': args.max_model_calls,
                           'max_model_seconds': args.max_model_seconds, 'started_calls': 0}}

    root = tmp_path / 'source'
    asyncio.run(batch_runner.run_batch(data, CONFIG, batch_dir=root, judge=True, executor=executor))
    return root


def explained(score=.8):
    return {**{name: score for name in FIELDS}, 'judge_mode': 'explained_v1',
            'explanations': {name: {'criterion': 'Rule', 'reason': 'Evidence agrees',
                                   'evidence_ids': ['policy'], 'missing': []} for name in FIELDS}}


def test_prepare_reconstructs_original_turn_and_task_packets(tmp_path):
    from evaluation.judge_calibration import prepare_calibration
    prepared = prepare_calibration(source(tmp_path))
    assert len(prepared['items']) == 2
    turn, task = prepared['items']
    assert turn['input'] == {'question': 'second', 'response': 'answer 1', 'context': 'prior/user: seed',
                             'expected': 'help', 'evidence': dataset()['knowledge']}
    assert task['input']['question'] == 'first\nsecond'
    assert task['input']['response'] == 'answer 0\nanswer 1'
    assert turn['input_sha256'] == stable_hash(turn['input'])
    assert turn['source']['report_sha256'] and turn['source']['run_id']
    assert turn['old_scores']['relevance'] == .5


def test_prepare_keeps_missing_outputs_and_rejects_changed_reports(tmp_path):
    from evaluation.judge_calibration import prepare_calibration
    root = source(tmp_path, missing=True)
    prepared = prepare_calibration(root)
    assert len(prepared['items']) == 2
    assert prepared['items'][0]['source_status'] == 'source_unavailable'
    report = next((root / 'reports').rglob('*.json'))
    report.write_text(report.read_text('utf-8') + '\n', 'utf-8')
    with pytest.raises(ValueError, match='integrity'):
        prepare_calibration(root)


def test_run_one_shot_preserves_denominator_and_never_archives_config(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path, missing=True))
    calls = []

    async def judge(**packet):
        calls.append(packet)
        return explained()

    root = tmp_path / 'calibration'
    result = asyncio.run(run_calibration(prepared, judge, output_dir=root, config=CONFIG))
    assert result['total'] == 2
    assert result['counts'] == {'passed': 1, 'failed': 0, 'invalid': 0, 'source_unavailable': 1}
    assert len(calls) == 1
    assert 'pass_rate' not in result
    assert result['source_fingerprints'] == prepared['source_fingerprints']
    assert result['fingerprints'] == build_fingerprints(prepared['dataset'], dict(CONFIG, judge_enabled=True, judge_mode='explained_v1'))
    assert all('never-archive-key' not in p.read_text('utf-8') for p in root.rglob('*.json'))
    with pytest.raises((ValueError, FileExistsError)):
        asyncio.run(run_calibration(prepared, judge, output_dir=root, config=CONFIG))
    assert len(calls) == 1


@pytest.mark.parametrize('invalid', [dict(explained(), judge_failed=True),
                                   dict(explained(), explanations={}),
                                   dict(explained(), judge_mode='scores'),
                                   dict(explained(), completeness=True)])
def test_invalid_judges_never_pass_or_retry(tmp_path, invalid):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path), ['case_0'])
    calls = []

    async def judge(**packet):
        calls.append(packet)
        return invalid

    result = asyncio.run(run_calibration(prepared, judge, output_dir=tmp_path / 'out'))
    assert result['counts']['invalid'] == 1
    assert len(calls) == 1
    assert result['items'][0]['scores'] == {}


def test_mutated_prepared_input_fails_before_judge(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path))
    prepared['items'][0]['input']['response'] = 'changed'
    with pytest.raises(ValueError, match='changed|hash|snapshot'):
        asyncio.run(run_calibration(prepared, None, output_dir=tmp_path / 'out'))


def test_all_four_scores_must_meet_original_threshold(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path), ['case_0'])

    async def judge(**packet):
        return dict(explained(1), completeness=.74)

    result = asyncio.run(run_calibration(prepared, judge, output_dir=tmp_path / 'out'))
    assert result['counts']['failed'] == 1


def test_actual_proxy_calls_and_invalid_parser_usage_are_bounded(tmp_path):
    import httpx
    from anthropic import AsyncAnthropic
    from evaluation.evaluator import LLMJudge
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    from observability.instrumentation import AnthropicClientProxy

    prepared = prepare_calibration(source(tmp_path, count=12))
    active = high = calls = 0

    async def handler(request):
        nonlocal active, high, calls
        calls += 1
        number = calls
        active += 1
        high = max(high, active)
        await asyncio.sleep(.005)
        active -= 1
        values = explained()
        values.pop('judge_mode')
        return httpx.Response(200, json={'id': f'msg_{number}', 'type': 'message', 'role': 'assistant',
            'model': 'offline-judge', 'content': [{'type': 'text', 'text': 'invalid' if number == 1 else json.dumps(values)}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 7, 'output_tokens': 3}})

    async def exercise():
        async with AsyncAnthropic(api_key='offline', max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            judge = LLMJudge(AnthropicClientProxy(client), 'offline-judge', mode='explained_v1')
            return await run_calibration(prepared, judge, config=CONFIG, output_dir=tmp_path / 'out')

    result = asyncio.run(exercise())
    assert calls == result['budget']['started_calls'] == result['usage']['model_calls'] == 12
    assert high == 2
    assert result['counts']['invalid'] == 1
    assert result['usage']['input_tokens'] == 84
    assert result['items'][0]['usage']['output_tokens'] == 3


def test_reduced_budget_keeps_unstarted_items_invalid(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path))
    calls = []

    async def judge(**packet):
        calls.append(packet)
        return explained()

    result = asyncio.run(run_calibration(prepared, judge, max_model_calls=1, max_workers=1,
                                        output_dir=tmp_path / 'out'))
    assert len(calls) == 1
    assert result['counts']['invalid'] == 1
    assert result['total'] == 2


def test_offline_cli_never_imports_runtime_sdk_or_telemetry(tmp_path, monkeypatch, capsys):
    import builtins
    from evaluation.judge_calibration import main
    root = source(tmp_path)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in {'services.runtime', 'evaluation.evaluator', 'anthropic',
                            'observability.telemetry', 'observability.instrumentation'}
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    assert main(['--source-batch', str(root)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['model_calls'] == 0
    assert output['total'] == 2
    assert output['source_artifacts']['manifest_sha256']


def test_more_than_twelve_items_is_rejected(tmp_path):
    from evaluation.judge_calibration import prepare_calibration
    with pytest.raises(ValueError, match='12'):
        prepare_calibration(source(tmp_path, count=13))


def test_item_observations_share_sdk_parent_and_include_safe_ids(tmp_path):
    from contextlib import contextmanager
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    from observability.telemetry import bind_telemetry
    captured = []

    class Observation:
        def score(self, name, value):
            captured.append(('score', name, value))

        def update(self, **kwargs):
            pass

    class Telemetry:
        @contextmanager
        def span(self, name, **kwargs):
            captured.append((name, kwargs))
            yield Observation()

    async def judge(**packet):
        return explained()

    prepared = prepare_calibration(source(tmp_path))
    with bind_telemetry(Telemetry()):
        result = asyncio.run(run_calibration(prepared, judge, output_dir=tmp_path / 'out'))
    spans = [value for name, *rest in captured if name == 'judge_calibration.item' for value in rest]
    assert len(spans) == 2
    assert spans[0]['metadata'] == {'calibration_run_id': result['run_id'],
                                    'case_id': 'case_0', 'assertion_id': 'quality'}
    assert ('score', 'calibration_pass', 1.0) in captured


def test_model_deadline_marks_remaining_items_invalid_without_replay(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path))
    calls = []

    async def judge(**packet):
        calls.append(packet)
        await asyncio.sleep(1)
        return explained()

    result = asyncio.run(run_calibration(prepared, judge, output_dir=tmp_path / 'out',
                                        max_model_seconds=.2, max_workers=1))
    assert len(calls) <= 1
    assert result['counts']['invalid'] == 2
    assert result['budget']['stop_reason'] == 'deadline'


def test_real_judge_model_must_match_recorded_config_before_calls(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    prepared = prepare_calibration(source(tmp_path))

    class WrongJudge:
        _model = 'another-model'
        _mode = 'explained_v1'

        async def judge(self, **packet):
            pytest.fail('Configuration mismatch must reject before Judge')

    with pytest.raises(ValueError, match='model'):
        asyncio.run(run_calibration(prepared, WrongJudge(), output_dir=tmp_path / 'out', config=CONFIG))


def test_cancelled_item_keeps_usage_and_uncertain_execution_ledger(tmp_path):
    from evaluation.judge_calibration import prepare_calibration, run_calibration
    from observability.instrumentation import current_usage
    prepared = prepare_calibration(source(tmp_path), ['case_0'])
    root = tmp_path / 'out'
    entered = asyncio.Event()

    async def judge(**packet):
        usage = current_usage()
        usage.model_calls = 1
        usage.input_tokens = 7
        entered.set()
        await asyncio.sleep(100)

    async def exercise():
        task = asyncio.create_task(run_calibration(prepared, judge, output_dir=root))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
    assert manifest['status'] == 'interrupted'
    item = json.loads((root / 'items' / '00.json').read_text('utf-8'))
    assert item['status'] == 'invalid'
    assert item['usage']['input_tokens'] == 7
    assert item['execution_uncertain'] is True
