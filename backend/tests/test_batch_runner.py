"""Offline batch persistence tests; the executor replaces only paid replay."""
import asyncio
import copy
import functools
import json
import uuid
from pathlib import Path

import pytest

from evaluation import batch_runner as batch
from evaluation.scenarios import build_fingerprints, load_dataset, DEFAULT_DATASET


def async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture
def dataset():
    data = load_dataset(DEFAULT_DATASET)
    groups = list(dict.fromkeys(case['scenario'] for case in data['cases']))[:3]
    data['cases'] = [next(case for case in data['cases'] if case['scenario'] == group)
                     for group in groups]
    return data


CONFIG = {'model': 'offline-model', 'judge_model': 'offline-judge', 'api_key': 'never-store-this-secret'}


def report(args, data, *, status='passed', interrupted=False):
    run_id = uuid.uuid4().hex
    config = dict(CONFIG, judge_enabled=args.judge)
    fingerprints = build_fingerprints(data, config)
    cases = [{'id': case['id'], 'scenario': case['scenario'], 'status': status,
              'budget_exceeded': interrupted, 'turns': [], 'assertions': []}
             for case in data['cases']]
    return {'schema_version': 1, 'run_id': run_id, 'namespace': 'eval_' + run_id,
            'mode': 'live', 'status': 'error' if interrupted else 'completed',
            'dataset': {'fingerprint': fingerprints['dataset']},
            'metadata': {'fingerprints': fingerprints}, 'cases': cases,
            'total': len(cases), 'passed': len(cases) if status == 'passed' else 0,
            'cleanup': {'status': 'completed'},
            'usage': {'status': 'observed', 'observed_turns': 1,
                      'totals': {'input_tokens': 10, 'output_tokens': 4, 'model_calls': 2}},
            'evaluation_overhead_usage': {'input_tokens': 3, 'output_tokens': 1, 'model_calls': 1},
            'budget': {'max_model_calls': args.max_model_calls, 'max_model_seconds': args.max_model_seconds,
                       'started_calls': 3, 'blocked_calls': int(interrupted),
                       'stop_reason': 'model_calls' if interrupted else None}}


async def passing(args, data):
    return report(args, data)


@async_test
async def test_pilot_keeps_full_denominator_and_resume_never_replays_failure(tmp_path, dataset):
    calls = []

    async def failed(args, data):
        calls.append(data['cases'][0]['id'])
        return report(args, data, status='hard_fail')

    path = tmp_path / 'batch'
    pilot = await batch.run_batch(dataset, CONFIG, batch_dir=path,
                                 scenario=[dataset['cases'][0]['scenario']], max_workers=2,
                                 judge=True, executor=failed)
    assert pilot['total'] == 3
    assert pilot['pending'] == 2
    assert pilot['counts']['hard_fail'] == 1
    assert pilot['pass_rate'] == 0
    assert pilot['batch_dir'] == str(path.resolve())

    complete = await batch.run_batch(dataset, CONFIG, resume=path, max_workers=2,
                                    judge=True, executor=failed)
    assert len(calls) == len(set(calls)) == 3
    assert complete['pending'] == 0
    assert complete['counts']['hard_fail'] == 3
    assert complete['usage']['combined']['input_tokens'] == 39
    assert complete['usage']['combined']['model_calls'] == 9
    assert len(complete['cleanup']) == 3
    assert complete['coverage']['reported_case_ids'] == [c['id'] for c in dataset['cases']]
    manifest = json.loads((path / 'manifest.json').read_text('utf-8'))
    assert all(g['status'] == 'completed' for g in manifest['groups'])
    assert all(not Path(g['report_path']).is_absolute() for g in manifest['groups'])
    assert 'never-store-this-secret' not in (path / 'manifest.json').read_text('utf-8')


@async_test
async def test_concurrency_is_bounded_at_two(tmp_path, dataset):
    active = high = 0

    async def executor(args, data):
        nonlocal active, high
        active += 1
        high = max(high, active)
        await asyncio.sleep(.01)
        active -= 1
        return report(args, data)

    result = await batch.run_batch(dataset, CONFIG, batch_dir=tmp_path / 'batch',
                                   max_workers=2, executor=executor)
    assert high == 2
    assert result['passed'] == 3


@async_test
@pytest.mark.parametrize('change', ['dataset', 'knowledge', 'model', 'judge', 'budget', 'workers',
                                   'prompt', 'retrieval', 'judge_model', 'skills'])
async def test_resume_rejects_changed_frozen_controls_before_new_calls(tmp_path, dataset, change):
    path = tmp_path / 'batch'
    await batch.run_batch(dataset, CONFIG, batch_dir=path, scenario=[dataset['cases'][0]['scenario']],
                          executor=passing)
    changed, config, options = copy.deepcopy(dataset), dict(CONFIG), {}
    if change == 'dataset':
        changed['cases'][1]['description'] += ' changed'
    elif change == 'knowledge':
        changed['knowledge'][0]['content'] += ' changed'
    elif change == 'model':
        config['model'] = 'other-model'
    elif change == 'judge':
        options['judge'] = True
    elif change == 'budget':
        options['max_model_calls'] = 199
    elif change == 'workers':
        options['max_workers'] = 2
    elif change == 'prompt':
        config['skills_max_prompt_chars'] = 1234
    elif change == 'retrieval':
        config['retrieval_top_k'] = 1
    elif change == 'judge_model':
        config['judge_model'] = 'other-judge'
    else:
        config['skills_dir'] = str(tmp_path / 'other-skills')

    async def forbidden(*args):
        pytest.fail('Changed controls must fail before a paid call')

    with pytest.raises(ValueError, match='changed|fingerprint|controls'):
        await batch.run_batch(changed, config, resume=path, executor=forbidden, **options)


@async_test
async def test_budget_interruption_is_completed_and_separately_visible(tmp_path, dataset):
    async def interrupted(args, data):
        return report(args, data, status='error', interrupted=True)

    path = tmp_path / 'batch'
    result = await batch.run_batch(dataset, CONFIG, batch_dir=path, executor=interrupted)
    assert result['total'] == 3
    assert result['counts']['error'] == 3
    assert result['budget_interruptions']['groups'] == 3
    assert result['budget_interruptions']['cases'] == 3
    assert result['status'] == 'budget_interrupted'
    resumed = await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)
    assert resumed['counts']['error'] == 3
    assert resumed['passed'] == 0


@async_test
async def test_failed_calls_remain_in_sdk_usage_when_no_reply_usage_is_available(tmp_path, dataset):
    async def unavailable(args, subset):
        payload = report(args, subset, status='error', interrupted=True)
        payload['usage'] = {'status': 'unavailable', 'observed_turns': 0, 'totals': {}}
        payload['evaluation_overhead_usage'] = {}
        payload['budget'].update(started_calls=4, blocked_calls=2)
        return payload

    result = await batch.run_batch(dataset, CONFIG, batch_dir=tmp_path / 'batch', executor=unavailable)
    assert result['usage']['combined'] == {}
    assert result['usage']['sdk_create_attempts'] == 12
    assert result['usage']['blocked_create_attempts'] == 6


@async_test
async def test_executor_crash_blocks_group_and_resume_fails_closed(tmp_path, dataset):
    path = tmp_path / 'batch'

    async def crash(*args):
        raise RuntimeError('could have charged; private credential must not be stored')

    with pytest.raises(ValueError, match='blocked|interrupted'):
        await batch.run_batch(dataset, CONFIG, batch_dir=path, executor=crash)
    manifest = json.loads((path / 'manifest.json').read_text('utf-8'))
    assert manifest['groups'][0]['status'] == 'blocked'
    assert 'private credential' not in (path / 'manifest.json').read_text('utf-8')
    with pytest.raises(ValueError, match='blocked|interrupted'):
        await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)


@async_test
async def test_started_group_after_process_loss_is_not_replayed(tmp_path, dataset):
    path = tmp_path / 'batch'
    await batch.run_batch(dataset, CONFIG, batch_dir=path, scenario=[dataset['cases'][0]['scenario']],
                          executor=passing)
    manifest_path = path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    manifest['groups'][1]['status'] = 'started'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='interrupted|blocked'):
        await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)
    assert json.loads(manifest_path.read_text('utf-8'))['groups'][1]['status'] == 'blocked'


@async_test
async def test_exclusive_lock_prevents_duplicate_runs_and_never_deletes_stale_lock(tmp_path, dataset):
    path = tmp_path / 'batch'
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(args, data):
        entered.set()
        await release.wait()
        return report(args, data)

    task = asyncio.create_task(batch.run_batch(dataset, CONFIG, batch_dir=path, executor=waiting))
    await entered.wait()
    try:
        with pytest.raises(ValueError, match='lock'):
            await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)
    finally:
        release.set()
        await task
    (path / '.lock').write_text('dead process', encoding='utf-8')
    with pytest.raises(ValueError, match='lock'):
        await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)
    assert (path / '.lock').read_text('utf-8') == 'dead process'


@async_test
@pytest.mark.parametrize('damage', ['bytes', 'path', 'ids', 'fingerprint', 'copy'])
async def test_cached_report_or_dataset_corruption_is_rejected_before_resume(tmp_path, dataset, damage):
    path = tmp_path / 'batch'
    await batch.run_batch(dataset, CONFIG, batch_dir=path, scenario=[dataset['cases'][0]['scenario']],
                          executor=passing)
    manifest_path = path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    group = manifest['groups'][0]
    report_path = path / group['report_path']
    if damage == 'path':
        group['report_path'] = '../outside.json'
    elif damage == 'copy':
        (path / 'dataset.json').write_text('{}', encoding='utf-8')
    else:
        payload = json.loads(report_path.read_text('utf-8'))
        if damage == 'ids':
            payload['cases'][0]['id'] = 'wrong-case'
        elif damage == 'fingerprint':
            payload['metadata']['fingerprints']['code'] = 'wrong-code'
        else:
            payload['passed'] = 99
        report_path.write_text(json.dumps(payload), encoding='utf-8')
        if damage != 'bytes':
            import hashlib
            group['report_sha256'] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)


@async_test
async def test_wrong_subset_fingerprints_block_new_report(tmp_path, dataset):
    async def wrong(args, data):
        payload = report(args, data)
        payload['metadata']['fingerprints']['dataset'] = build_fingerprints(dataset, CONFIG)['dataset']
        return payload

    with pytest.raises(ValueError, match='blocked|interrupted'):
        await batch.run_batch(dataset, CONFIG, batch_dir=tmp_path / 'batch', executor=wrong)


def test_cli_defaults_to_offline_validation_without_runtime_or_telemetry(tmp_path, capsys, monkeypatch):
    import builtins
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(('services.runtime', 'evaluation.scenario_runner', 'observability.telemetry')):
            pytest.fail('Offline CLI constructed runtime or telemetry')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    assert batch.main(['--dataset', str(DEFAULT_DATASET)]) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'validate'


@async_test
async def test_default_executor_rejects_actual_runtime_config_mismatch_before_calls(tmp_path, dataset, monkeypatch):
    from services import runtime
    from evaluation import scenario_runner

    async def forbidden(*args):
        pytest.fail('Actual runtime config mismatch reached the paid runner')

    monkeypatch.setattr(runtime, 'runtime_config_from_env', lambda: dict(CONFIG, model='different-model'))
    monkeypatch.setattr(scenario_runner, '_live', forbidden)
    with pytest.raises(ValueError, match='Actual runtime fingerprints'):
        await batch.run_batch(dataset, CONFIG, batch_dir=tmp_path / 'batch')


@async_test
async def test_offline_aggregate_checks_hashes_and_preserves_all_case_statuses(tmp_path, dataset):
    data = copy.deepcopy(dataset)
    data['cases'] = []
    statuses = ['passed', 'failed', 'hard_fail', 'invalid', 'timeout', 'error']
    for index, status in enumerate(statuses):
        case = copy.deepcopy(dataset['cases'][0])
        case.update(id=f'case_{index}', scenario=status)
        data['cases'].append(case)

    async def executor(args, subset):
        return report(args, subset, status=subset['cases'][0]['scenario'])

    path = tmp_path / 'batch'
    await batch.run_batch(data, CONFIG, batch_dir=path, executor=executor)
    summary = batch.aggregate_batch(path)
    assert summary['total'] == 6
    assert summary['pass_rate'] == .1667
    assert all(summary['counts'][status] == 1 for status in statuses)
    group = summary['groups'][0]
    (path / group['report_path']).write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        batch.aggregate_batch(path)


@async_test
async def test_cancellation_persists_blocked_group_before_lock_release(tmp_path, dataset):
    entered = asyncio.Event()

    async def waiting(*args):
        entered.set()
        await asyncio.Event().wait()

    path = tmp_path / 'batch'
    task = asyncio.create_task(batch.run_batch(dataset, CONFIG, batch_dir=path, executor=waiting))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (path / '.lock').exists()
    assert batch.aggregate_batch(path)['blocked'] == 1
    with pytest.raises(ValueError, match='blocked'):
        await batch.run_batch(dataset, CONFIG, resume=path, executor=passing)


@async_test
@pytest.mark.parametrize('workers', [0, 3, True])
async def test_unsupported_concurrency_rejected_before_creating_batch(tmp_path, dataset, workers):
    path = tmp_path / 'batch'
    with pytest.raises(ValueError, match='max_workers'):
        await batch.run_batch(dataset, CONFIG, batch_dir=path, max_workers=workers, executor=passing)
    assert not path.exists()
