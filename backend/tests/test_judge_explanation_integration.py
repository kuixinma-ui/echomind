"""Strict optional explanation integration without changing score thresholds."""
import asyncio

import pytest

from evaluation.scenario_runner import ScenarioRunner
from evaluation.scenarios import build_fingerprints, load_dataset


def explained(value=.9):
    names = ('relevance', 'accuracy', 'completeness', 'helpfulness')
    return {**{name: value for name in names}, 'judge_mode': 'explained_v1',
            'explanations': {name: {'criterion': '按给定政策和预期判断', 'reason': '回答说明了能力边界和下一步',
                                    'evidence_ids': ['policy'], 'missing': []} for name in names}}


def evaluate(value):
    async def judge(*args, **kwargs):
        return value
    rule = {'id': 'policy', 'type': 'semantic', 'scope': 'task', 'expected': '说明实际能力边界',
            'evidence': ['policy'], 'threshold': .75}
    return asyncio.run(ScenarioRunner(None, judge=judge)._assert(rule,
        {'response': '只能提供建议，不能代为操作。'}, {'initial_state': {}, 'budgets': {}},
        [{'id': 'policy', 'content': '演示系统'}], [{'message': '如何处理？'}]))


def test_explanations_survive_assertion_results_and_scores_remain_separate():
    value = explained()
    outcome = evaluate(value)
    assert outcome['status'] == 'passed'
    assert outcome['explanations'] == value['explanations']
    assert outcome['judge_mode'] == 'explained_v1'
    assert set(outcome['scores']) == {'relevance', 'accuracy', 'completeness', 'helpfulness', 'overall'}
    value['explanations']['accuracy']['reason'] = 'changed'
    assert outcome['explanations']['accuracy']['reason'] != 'changed'


def test_low_dimension_still_fails_with_explanations_and_high_overall():
    value = explained()
    value['completeness'] = .7
    value['explanations']['completeness']['missing'] = ['具体下一步未说明']
    outcome = evaluate(value)
    assert outcome['status'] == 'failed' and outcome['scores']['overall'] > .8
    assert outcome['explanations']['completeness']['missing'] == ['具体下一步未说明']


@pytest.mark.parametrize('damage', ['missing', 'unknown_evidence', 'bad_type'])
def test_injected_explained_judge_cannot_pass_with_invalid_explanations(damage):
    value = explained(1)
    if damage == 'missing':
        del value['explanations']
    elif damage == 'unknown_evidence':
        value['explanations']['accuracy']['evidence_ids'] = ['unprovided-policy']
    else:
        value['explanations']['helpfulness']['reason'] = {'private': 'raw-payload'}
    outcome = evaluate(value)
    assert outcome['status'] == 'invalid' and outcome['scores'] == {}
    assert 'raw-payload' not in str(outcome)


def test_score_only_injected_judge_stays_compatible():
    outcome = evaluate({name: .8 for name in ('relevance', 'accuracy', 'completeness', 'helpfulness')})
    assert outcome['status'] == 'passed'
    assert 'explanations' not in outcome


def test_only_judge_fingerprint_changes_when_explicit_mode_changes():
    data = load_dataset()
    plain = build_fingerprints(data, {'judge_enabled': True, 'judge_mode': 'scores'})
    rich = build_fingerprints(data, {'judge_enabled': True, 'judge_mode': 'explained_v1'})
    assert {key for key in plain if plain[key] != rich[key]} == {'judge'}


def test_unknown_judge_mode_cannot_get_a_valid_fingerprint():
    with pytest.raises(ValueError, match='[Jj]udge'):
        build_fingerprints(load_dataset(), {'judge_mode': 'unknown'})


def test_runtime_config_defaults_score_only_and_accepts_explicit_mode(monkeypatch):
    from services.runtime import runtime_config_from_env
    monkeypatch.delenv('ECHOMIND_JUDGE_MODE', raising=False)
    assert runtime_config_from_env()['judge_mode'] == 'scores'
    monkeypatch.setenv('ECHOMIND_JUDGE_MODE', 'explained_v1')
    assert runtime_config_from_env()['judge_mode'] == 'explained_v1'


def test_live_runner_forwards_explicit_mode_and_archives_explanations(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import anthropic
    import evaluation.evaluator as evaluator
    from evaluation.scenario_runner import _live
    from services import runtime
    from tests.test_scenarios import Runtime, dataset
    seen = []

    class Client:
        messages = SimpleNamespace(create=None)
        async def close(self):
            pass

    class Judge:
        def __init__(self, client, model, *, mode):
            seen.append((model, mode))
        async def judge(self, *args, **kwargs):
            return explained()

    monkeypatch.setattr(anthropic, 'AsyncAnthropic', lambda **kwargs: Client())
    monkeypatch.setattr(evaluator, 'LLMJudge', Judge)
    monkeypatch.setattr(runtime, 'runtime_config_from_env', lambda: {
        'api_key': 'offline-configured-key', 'model': 'fixed', 'judge_model': 'fixed', 'judge_mode': 'scores'})
    monkeypatch.setattr(runtime, 'create_runtime', lambda config, **kwargs: Runtime())
    data = dataset(assertions=[{'id': 'policy', 'type': 'semantic', 'scope': 'task',
                              'expected': '说明能力边界', 'evidence': ['policy']}])
    args = SimpleNamespace(judge=True, judge_mode='explained_v1', report_dir=str(tmp_path))
    report = asyncio.run(_live(args, data))
    assert seen == [('fixed', 'explained_v1')]
    assert report['passed'] == 1
    assert report['cases'][0]['assertions'][0]['explanations'] == explained()['explanations']
    assert (tmp_path / (report['run_id'] + '.json')).is_file()
