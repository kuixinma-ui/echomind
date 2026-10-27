"""One-shot Judge diagnostics on immutable archived replies; validation is offline.

This module never creates a conversation runtime. Existing output directories
are refused, including interrupted runs: investigate before starting a new run.
The resulting counts describe Judge assertions, not an end-to-end pass rate.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import json
import math
import uuid
from pathlib import Path

from evaluation.batch_runner import _atomic_json, _inside, _now, _read, aggregate_batch
from evaluation.scenarios import build_fingerprints, load_dataset, stable_hash


SCORE_FIELDS = ('relevance', 'accuracy', 'completeness', 'helpfulness')
STATUSES = ('passed', 'failed', 'invalid', 'source_unavailable')
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / 'data' / 'eval' / 'calibrations'


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_calibration(source_batch, case_ids=()):
    """Verify the entire source batch, then freeze selected semantic inputs.

Missing reports, actual replies, and original assertions remain in the item
denominator. Integrity failures reject the source instead of silently skipping.
"""
    root = Path(source_batch).resolve()
    aggregate = aggregate_batch(root)  # Full coverage and every completed report SHA.
    manifest = _read(root / 'manifest.json')
    dataset = load_dataset(root / 'dataset.json')
    selected = [case_ids] if isinstance(case_ids, str) else list(case_ids)
    if len(set(selected)) != len(selected):
        raise ValueError('Duplicate selected case ID')
    known = {case['id'] for case in dataset['cases']}
    if set(selected) - known:
        raise ValueError('Unknown selected case ID')
    reports = {}
    report_sources = []
    for group in manifest['groups']:
        if group['status'] != 'completed':
            continue
        path = _inside(root, group['report_path'])
        report = _read(path)
        identity = {'report_path': group['report_path'], 'report_sha256': _sha(path),
                    'run_id': report['run_id'], 'fingerprints': report['metadata']['fingerprints']}
        report_sources.append(identity)
        reports.update({case['id']: (case, identity) for case in report['cases']})
    items = []
    for case in dataset['cases']:
        if selected and case['id'] not in selected:
            continue
        recorded, identity = reports.get(case['id'], ({}, {}))
        turns = recorded.get('turns', [])
        for rule in case['assertions']:
            if rule['type'] != 'semantic':
                continue
            old = next((a for a in recorded.get('assertions', [])
                        if a.get('id') == rule['id'] and a.get('type') == 'semantic'), None)
            indices = [rule['turn']] if 'turn' in rule else list(range(len(case['turns'])))
            available = old is not None and all(
                i < len(turns) and turns[i].get('message') == case['turns'][i]['message']
                and turns[i].get('status') not in {'error', 'timeout'}
                and isinstance(turns[i].get('actual'), dict)
                and isinstance(turns[i]['actual'].get('response'), str)
                and bool(turns[i]['actual']['response'].strip()) for i in indices)
            packet = {'question': '\n'.join(case['turns'][i]['message'] for i in indices),
                      'response': '\n'.join(turns[i]['actual']['response'] for i in indices) if available else None,
                      'context': '\n'.join(f"{m.get('session', 'current')}/{m['role']}: {m['content']}"
                                           for m in case['initial_state'].get('messages', [])) or None,
                      'expected': rule['expected'],
                      'evidence': [copy.deepcopy(d) for d in dataset['knowledge'] if d['id'] in rule['evidence']]}
            items.append({'case_id': case['id'], 'assertion_id': rule['id'],
                          **{k: rule[k] for k in ('turn', 'scope') if k in rule},
                          'source': copy.deepcopy(identity),
                          'source_status': 'available' if available else 'source_unavailable',
                          'source_reason': None if available else 'Required source output or semantic assertion unavailable',
                          'threshold': rule.get('threshold', .75),
                          'judge_timeout_s': case['budgets'].get('judge_timeout_s', 60),
                          'required': rule.get('required', True), 'hard': rule.get('hard', False),
                          'old_scores': copy.deepcopy(old.get('scores', {})) if old else {},
                          'old_status': old.get('status') if old else None,
                          'input': packet, 'input_sha256': stable_hash(packet)})
    if not 1 <= len(items) <= 12:
        raise ValueError('Select between 1 and 12 semantic assertions for one calibration')
    prepared = {'schema_version': 1, 'kind': 'frozen_response_judge_diagnostic',
                'source_batch': str(root), 'source_batch_id': aggregate['batch_id'],
                'selected_case_ids': selected,
                'source_artifacts': {'manifest_sha256': _sha(root / 'manifest.json'),
                                     'dataset_sha256': _sha(root / 'dataset.json'), 'reports': report_sources},
                'source_fingerprints': copy.deepcopy(manifest['fingerprints']),
                'dataset': copy.deepcopy(dataset), 'items': items}
    prepared['snapshot_sha256'] = stable_hash(prepared)
    return prepared


def _verify(prepared):
    frozen = copy.deepcopy(prepared)
    digest = frozen.pop('snapshot_sha256', None)
    if stable_hash(frozen) != digest:
        raise ValueError('Prepared input snapshot hash changed')
    current = prepare_calibration(prepared['source_batch'], prepared['selected_case_ids'])
    if current != prepared:
        raise ValueError('Source artifacts or input snapshot changed')


def _controls(max_model_calls, max_model_seconds, max_workers):
    if type(max_model_calls) is not int or not 1 <= max_model_calls <= 12:
        raise ValueError('max_model_calls must be between 1 and 12')
    if type(max_model_seconds) not in (int, float) or not math.isfinite(max_model_seconds) or not 0 < max_model_seconds <= 600:
        raise ValueError('max_model_seconds must be positive and at most 600')
    if type(max_workers) is not int or max_workers not in (1, 2):
        raise ValueError('max_workers must be 1 or 2')
    return {'max_model_calls': max_model_calls, 'max_model_seconds': max_model_seconds,
            'max_workers': max_workers}


async def run_calibration(prepared, judge, *, output_dir=None, config=None,
                          max_model_calls=12, max_model_seconds=600, max_workers=2):
    """Judge each available frozen item once, using a caller-owned proxy client.

``judge`` may be an LLMJudge or an injected async callable. Actual SDK attempts
and tokens require AnthropicClientProxy; fake Judges still obey the item bound.
No client lifecycle or telemetry lifecycle is owned by this reusable function.
"""
    from observability.budgets import EvaluationBudget, bind_budget
    from observability.instrumentation import bind_usage
    from observability.telemetry import bind_telemetry, get_telemetry
    from evaluation.evaluator import validated_explanations

    controls = _controls(max_model_calls, max_model_seconds, max_workers)
    _verify(prepared)
    prepared = copy.deepcopy(prepared)
    supplied = dict(config or {})
    actual_model = getattr(judge, '_model', None)
    if actual_model is not None:
        if config is None:
            supplied.update(model=actual_model, judge_model=actual_model)
        elif supplied.get('judge_model', supplied.get('model')) != actual_model:
            raise ValueError('Judge model differs from fingerprint configuration')
    if getattr(judge, '_mode', 'explained_v1') != 'explained_v1':
        raise ValueError('Calibration requires explained_v1 Judge mode')
    config = dict(supplied, judge_enabled=True, judge_mode='explained_v1')
    fingerprints = build_fingerprints(prepared['dataset'], config)
    run_id = uuid.uuid4().hex
    root = Path(output_dir or (DEFAULT_OUTPUT_DIR / run_id)).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / 'items').mkdir()
    manifest = {'schema_version': 1, 'kind': prepared['kind'], 'run_id': run_id,
                'created_at': _now(), 'status': 'started', 'judge_mode': 'explained_v1',
                'snapshot_sha256': prepared['snapshot_sha256'],
                'source_batch_id': prepared['source_batch_id'],
                'source_fingerprints': prepared['source_fingerprints'],
                'fingerprints': fingerprints, 'controls': controls,
                'items': [{'index': i, 'case_id': item['case_id'], 'assertion_id': item['assertion_id'],
                           'input_sha256': item['input_sha256'], 'status': 'pending'}
                          for i, item in enumerate(prepared['items'])]}
    _atomic_json(root / 'inputs.json', prepared)
    _atomic_json(root / 'manifest.json', manifest)
    budget = EvaluationBudget(max_model_calls=max_model_calls, max_model_seconds=max_model_seconds)
    semaphore = asyncio.Semaphore(max_workers)
    results = [None] * len(prepared['items'])
    started_items = 0

    async def one(index, item):
        nonlocal started_items
        async with semaphore:
            interrupted = False
            record = copy.deepcopy(item)
            record.update(index=index, status='source_unavailable', scores={}, explanations={}, usage={})
            if item['source_status'] == 'available':
                # Durable started state precedes any possible provider invocation.
                manifest['items'][index].update(status='started', started_at=_now())
                _atomic_json(root / 'manifest.json', manifest)
                record.update(status='invalid', reason='Judge unavailable')
                with bind_usage() as usage:
                    try:
                        budget.check()
                        if started_items >= max_model_calls:
                            raise budget.stop('model_calls')
                        started_items += 1
                        callback = getattr(judge, 'judge', judge)
                        value = callback(**copy.deepcopy(item['input']))
                        timeout = min(item['judge_timeout_s'], max(0, budget.remaining_seconds()))
                        value = await asyncio.wait_for(value, timeout) if inspect.isawaitable(value) else value
                        values = value if isinstance(value, dict) else vars(value)
                        if values.get('judge_failed') or values.get('judge_mode') != 'explained_v1':
                            raise ValueError('Judge failed or wrong mode')
                        scores = {}
                        for name in SCORE_FIELDS:
                            score = values.get(name)
                            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                                raise ValueError('Invalid Judge score')
                            scores[name] = float(score)
                        explanations = validated_explanations(values.get('explanations'), item['input']['evidence'])
                        if budget.stop_reason:
                            raise budget.stop(budget.stop_reason)
                        record.update(status='passed' if all(s >= item['threshold'] for s in scores.values()) else 'failed',
                                      scores=scores, explanations=explanations, judge_mode='explained_v1',
                                      reason='All four original thresholds applied')
                    except asyncio.CancelledError:
                        interrupted = True
                        record.update(status='invalid', reason='Interrupted execution requires investigation',
                                      execution_uncertain=True, scores={}, explanations={})
                    except Exception as exc:
                        # Keep safe error type, never provider/config payloads.
                        record.update(status='invalid', reason='Judge failed validation or execution',
                                      error_type=type(exc).__name__, scores={}, explanations={})
                        if isinstance(exc, asyncio.TimeoutError) and budget.remaining_seconds() <= 0:
                            budget.stop('deadline')
                        if budget.stop_reason:
                            record['budget_reason'] = budget.stop_reason
                    finally:
                        record['usage'] = usage.snapshot()
            else:
                record['reason'] = item['source_reason']
            record['completed_at'] = _now()
            _atomic_json(root / 'items' / f'{index:02d}.json', record)
            results[index] = record
            manifest['items'][index].update(status=record['status'], result_path=f'items/{index:02d}.json',
                                             result_sha256=_sha(root / 'items' / f'{index:02d}.json'))
            manifest['budget'] = budget.snapshot()
            _atomic_json(root / 'manifest.json', manifest)
            if interrupted:
                raise asyncio.CancelledError

    telemetry = get_telemetry()

    async def observed(index, item):
        with telemetry.span('judge_calibration.item', kind='evaluator', metadata={
                'calibration_run_id': run_id, 'case_id': item['case_id'],
                'assertion_id': item['assertion_id']}) as observation:
            await one(index, item)
            if observation is not None:
                observation.score('calibration_pass', float(results[index]['status'] == 'passed'))
                observation.score('calibration_invalid', float(results[index]['status'] == 'invalid'))
                observation.update(metadata={'status': results[index]['status']})

    with bind_budget(budget), bind_telemetry(telemetry):
        tasks = [asyncio.create_task(observed(i, item)) for i, item in enumerate(prepared['items'])]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            manifest.update(status='interrupted', budget=budget.snapshot())
            _atomic_json(root / 'manifest.json', manifest)
            raise
    usage = {}
    for item in results:
        for name, value in item['usage'].items():
            usage[name] = usage.get(name, 0) + value
    result = {**{k: manifest[k] for k in ('schema_version', 'kind', 'run_id', 'judge_mode',
               'snapshot_sha256', 'source_batch_id', 'source_fingerprints', 'fingerprints', 'controls')},
              'status': 'completed', 'output_dir': str(root), 'total': len(results),
              'counts': {s: sum(item['status'] == s for item in results) for s in STATUSES},
              'usage': usage, 'budget': budget.snapshot(), 'items': results,
              'interpretation': 'Fixed-response Judge diagnostics only; business review remains pending'}
    _atomic_json(root / 'result.json', result)
    manifest.update(status='completed', completed_at=_now(), budget=budget.snapshot())
    _atomic_json(root / 'manifest.json', manifest)
    return result


async def _live(args, prepared):
    # Only this explicit live boundary loads environment configuration and SDKs.
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=True)
    from services.runtime import runtime_config_from_env
    from anthropic import AsyncAnthropic
    from evaluation.evaluator import LLMJudge
    from observability.instrumentation import AnthropicClientProxy
    from observability.telemetry import get_telemetry
    config = runtime_config_from_env()
    if not config.get('api_key') or config['api_key'].strip().lower() in {'test', 'dummy', 'changeme', 'your-api-key'}:
        raise ValueError('Configured API key required for explicit live calibration')
    client = AnthropicClientProxy(AsyncAnthropic(api_key=config['api_key'],
        **({'base_url': config['base_url']} if config.get('base_url') else {})))
    try:
        judge = LLMJudge(client, config.get('judge_model', config['model']), mode='explained_v1')
        return await run_calibration(prepared, judge, output_dir=args.output_dir, config=config,
            max_model_calls=args.max_model_calls, max_model_seconds=args.max_model_seconds,
            max_workers=args.max_workers)
    finally:
        try:
            await client.close()
        finally:
            telemetry = get_telemetry()
            try:
                telemetry.flush()
            finally:
                telemetry.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-batch', required=True)
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--mode', choices=('validate', 'live'), default='validate')
    parser.add_argument('--output-dir')
    parser.add_argument('--env-file', help='Load process configuration only in explicit live mode')
    parser.add_argument('--max-model-calls', type=int, default=12)
    parser.add_argument('--max-model-seconds', type=float, default=600)
    parser.add_argument('--max-workers', type=int, choices=(1, 2), default=2)
    args = parser.parse_args(argv)
    _controls(args.max_model_calls, args.max_model_seconds, args.max_workers)
    prepared = prepare_calibration(args.source_batch, args.case)
    if args.mode == 'live':
        output = asyncio.run(_live(args, prepared))
    else:
        output = {k: v for k, v in prepared.items() if k != 'dataset'}
        output.update(mode='validate', status='validated', total=len(prepared['items']), model_calls=0)
        if args.output_dir:
            root = Path(args.output_dir).resolve()
            root.mkdir(parents=True, exist_ok=False)
            _atomic_json(root / 'inputs.json', prepared)
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
