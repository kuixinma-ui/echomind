"""Durable, explicitly live, resumable scenario groups.

``run_batch`` freezes the entire validated dataset and control fingerprints;
``executor(args, subset)`` is an async injection boundary matching ``_live``.
Completed reports, including assertion failures and budget stops, never replay.
An interrupted/blocked group requires investigation and a new batch. A leftover
``.lock`` is never removed automatically: establish that its process is dead
before manual recovery. No baseline is promoted by this module.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from evaluation.scenarios import (DEFAULT_DATASET, FINGERPRINT_FIELDS,
                                  build_fingerprints, load_dataset, stable_hash,
                                  validate_dataset)

DEFAULT_BATCH_DIR = Path(__file__).resolve().parents[1] / 'data' / 'eval' / 'batches'
CASE_STATUSES = ('passed', 'failed', 'hard_fail', 'invalid', 'timeout', 'error')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Missing or invalid batch artifact: ' + path.name) from exc


def _inside(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Batch artifact path must be relative')
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Batch artifact path escapes batch directory')
    return path


@contextmanager
def _lock(root):
    path = root / '.lock'
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError('Batch lock exists; no stale lock is removed automatically') from exc
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(json.dumps({'pid': os.getpid(), 'created_at': _now()}))
            output.flush()
            os.fsync(output.fileno())
        yield
    finally:
        path.unlink()


def _controls(judge, max_workers, max_model_calls, max_model_seconds):
    if type(judge) is not bool:
        raise ValueError('judge must be boolean')
    if type(max_workers) is not int or max_workers not in (1, 2):
        raise ValueError('max_workers must be 1 or 2')
    if type(max_model_calls) is not int or not 1 <= max_model_calls <= 200:
        raise ValueError('max_model_calls must be between 1 and 200')
    if type(max_model_seconds) not in (int, float) or not math.isfinite(max_model_seconds) or not 0 < max_model_seconds <= 1200:
        raise ValueError('max_model_seconds must be positive and at most 1200')
    return {'judge': judge, 'max_workers': max_workers, 'max_model_calls': max_model_calls,
            'max_model_seconds': max_model_seconds}


def _groups(dataset):
    names = dict.fromkeys(case['scenario'] for case in dataset['cases'])
    return [{'scenario': name,
             'case_ids': [case['id'] for case in dataset['cases'] if case['scenario'] == name],
             'status': 'pending'} for name in names]


def _subset(dataset, group):
    selected = copy.deepcopy(dataset)
    selected['cases'] = [case for case in selected['cases'] if case['scenario'] == group['scenario']]
    return selected


def _expected_fingerprints(manifest, subset):
    return dict(manifest['fingerprints'], dataset=stable_hash(subset))


def _validate_manifest(root, manifest, dataset):
    if manifest.get('schema_version') != 1:
        raise ValueError('Unsupported batch manifest schema')
    fingerprints = manifest.get('fingerprints', {})
    if set(fingerprints) != set(FINGERPRINT_FIELDS) or any(not isinstance(value, str) or not value for value in fingerprints.values()):
        raise ValueError('Missing frozen batch fingerprints')
    if fingerprints['dataset'] != stable_hash(dataset) or fingerprints['knowledge'] != stable_hash(dataset['knowledge']):
        raise ValueError('Frozen dataset copy changed')
    controls = manifest.get('controls', {})
    if controls != _controls(**controls):
        raise ValueError('Invalid frozen controls')
    groups = manifest.get('groups', [])
    if [(g.get('scenario'), g.get('case_ids')) for g in groups] != [(g['scenario'], g['case_ids']) for g in _groups(dataset)]:
        raise ValueError('Frozen batch group coverage changed')
    if any(g.get('status') not in {'pending', 'started', 'completed', 'blocked'} for g in groups):
        raise ValueError('Invalid batch group state')


def _validate_report(report, manifest, group, subset):
    from evaluation.report_store import ReportStore
    run_id = ReportStore.validate_run_id(report.get('run_id'))
    if report.get('namespace') != 'eval_' + run_id or report.get('mode') != 'live':
        raise ValueError('Report execution mode or namespace differs')
    expected = _expected_fingerprints(manifest, subset)
    if report.get('metadata', {}).get('fingerprints') != expected or report.get('dataset', {}).get('fingerprint') != expected['dataset']:
        raise ValueError('Report subset fingerprints differ from frozen batch')
    cases = report.get('cases', [])
    if [case.get('id') for case in cases] != group['case_ids'] or report.get('total') != len(cases):
        raise ValueError('Report case IDs or total differ from frozen group')
    if any(case.get('scenario') != group['scenario'] or case.get('status') not in CASE_STATUSES for case in cases):
        raise ValueError('Report case scenario or status is invalid')
    budget = report.get('budget', {})
    if any(budget.get(key) != manifest['controls'][key] for key in ('max_model_calls', 'max_model_seconds')):
        raise ValueError('Report budget differs from frozen controls')
    if type(budget.get('started_calls')) is not int or not 0 <= budget['started_calls'] <= manifest['controls']['max_model_calls']:
        raise ValueError('Invalid reported budget usage')
    if budget.get('stop_reason') not in (None, 'model_calls', 'deadline'):
        raise ValueError('Unknown reported budget interruption')
    if not isinstance(report.get('cleanup'), dict) or not report['cleanup'].get('status'):
        raise ValueError('Report lacks cleanup evidence')
    json.dumps(report, allow_nan=False)


def _cached_reports(root, manifest, dataset):
    reports = []
    for group in manifest['groups']:
        if group['status'] != 'completed':
            continue
        path = _inside(root, group.get('report_path'))
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError('Cached report is missing') from exc
        if digest != group.get('report_sha256'):
            raise ValueError('Cached report integrity hash differs')
        report = _read(path)
        _validate_report(report, manifest, group, _subset(dataset, group))
        reports.append((group, report))
    return reports


def _add_usage(target, values):
    for key, value in values.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid report usage total')
        target[key] = target.get(key, 0) + value


def aggregate_batch(batch_dir):
    """Read-only, offline aggregation with report hash/coverage checks.

    Pending and uncertain groups remain in the full dataset denominator. This
    verifies archived controls against reports, without claiming the current
    checkout/environment still matches them (``run_batch`` checks that on resume).
    """
    root = Path(batch_dir).resolve()
    manifest = _read(root / 'manifest.json')
    dataset = load_dataset(_inside(root, 'dataset.json'))
    _validate_manifest(root, manifest, dataset)
    reports = _cached_reports(root, manifest, dataset)
    by_id, cleanup, interruptions = {}, [], []
    chat, judge = {}, {}
    for group, report in reports:
        by_id.update({case['id']: case for case in report['cases']})
        _add_usage(chat, report.get('usage', {}).get('totals', {}))
        _add_usage(judge, report.get('evaluation_overhead_usage', {}))
        cleanup.append({'scenario': group['scenario'], 'run_id': report['run_id'],
                        'namespace': report['namespace'], **report['cleanup']})
        if report['budget'].get('stop_reason'):
            interruptions.append({'scenario': group['scenario'], **report['budget']})
    combined = dict(chat)
    _add_usage(combined, judge)
    cases = []
    state_by_group = {group['scenario']: group['status'] for group in manifest['groups']}
    for case in dataset['cases']:
        cases.append(copy.deepcopy(by_id.get(case['id'], {
            'id': case['id'], 'scenario': case['scenario'],
            'status': 'blocked' if state_by_group[case['scenario']] in {'started', 'blocked'} else 'pending'})))
    counts = {status: sum(case['status'] == status for case in cases)
              for status in (*CASE_STATUSES, 'pending', 'blocked')}
    pending_ids = [case['id'] for case in cases if case['status'] == 'pending']
    blocked_ids = [case['id'] for case in cases if case['status'] == 'blocked']
    reported_ids = [case['id'] for case in cases if case['id'] in by_id]
    status = ('blocked' if blocked_ids else 'incomplete' if pending_ids else
              'budget_interrupted' if interruptions else 'completed')
    return {'schema_version': 1, 'batch_id': manifest['batch_id'], 'batch_dir': str(root),
            'status': status, 'dataset_status': dataset['status'],
            'total': len(cases), 'reported': len(reported_ids), 'pending': counts['pending'],
            'blocked': counts['blocked'], 'passed': counts['passed'],
            'pass_rate': round(counts['passed'] / len(cases), 4), 'counts': counts,
            'coverage': {'expected_case_ids': [case['id'] for case in dataset['cases']],
                         'reported_case_ids': reported_ids, 'pending_case_ids': pending_ids,
                         'blocked_case_ids': blocked_ids},
            'groups': copy.deepcopy(manifest['groups']), 'cases': cases,
            'usage': {'chat': chat, 'judge_and_evaluation': judge, 'combined': combined,
                      'sdk_create_attempts': sum(report['budget']['started_calls'] for _, report in reports),
                      'blocked_create_attempts': sum(report['budget'].get('blocked_calls', 0) for _, report in reports)},
            'budget_interruptions': {'groups': len(interruptions),
                                     'cases': sum(bool(case.get('budget_exceeded')) for case in cases),
                                     'details': interruptions},
            'cleanup': cleanup, 'fingerprints': manifest['fingerprints'],
            'controls': manifest['controls']}


async def run_batch(dataset, config, *, batch_dir=None, resume=None, scenario=(),
                    max_workers=1, judge=False, executor=None,
                    max_model_calls=200, max_model_seconds=1200):
    """Run pending selected scenario groups; ``resume`` accepts the batch folder.

    ``config`` is used only for fingerprints and is never serialized. The default
    executor additionally verifies actual environment config before any model
    calls. An injected executor must return a complete, bounded live report.
    """
    dataset = copy.deepcopy(dataset)
    validation = validate_dataset(dataset)
    controls = _controls(judge, max_workers, max_model_calls, max_model_seconds)
    config = dict(config, judge_enabled=judge)
    fingerprints = build_fingerprints(dataset, config)
    selected = {scenario} if isinstance(scenario, str) else set(scenario)
    if selected - set(validation['scenarios']):
        raise ValueError('Unknown scenario group')
    if resume is not None and batch_dir is not None:
        raise ValueError('Choose batch_dir or resume, not both')
    root = Path(resume or batch_dir or (DEFAULT_BATCH_DIR / uuid.uuid4().hex)).resolve()
    if resume is None:
        root.mkdir(parents=True, exist_ok=False)
    elif not root.is_dir():
        raise ValueError('Resume batch directory does not exist')

    def check_current():
        if build_fingerprints(dataset, config) != fingerprints:
            raise ValueError('Current fingerprints changed; create a new batch')
        if executor is None:
            from services.runtime import runtime_config_from_env
            actual = dict(runtime_config_from_env(), judge_enabled=judge)
            if build_fingerprints(dataset, actual) != fingerprints:
                raise ValueError('Actual runtime fingerprints differ from supplied config; create a new batch')

    with _lock(root):
        if resume is None:
            manifest = {'schema_version': 1, 'batch_id': uuid.uuid4().hex,
                        'created_at': _now(), 'fingerprints': fingerprints,
                        'controls': controls, 'groups': _groups(dataset)}
            _atomic_json(root / 'dataset.json', dataset)
            _atomic_json(root / 'manifest.json', manifest)
        else:
            manifest = _read(root / 'manifest.json')
            frozen = load_dataset(_inside(root, 'dataset.json'))
            _validate_manifest(root, manifest, frozen)
            if frozen != dataset or manifest['fingerprints'] != fingerprints or manifest['controls'] != controls:
                raise ValueError('Dataset, fingerprints or controls changed; create a new batch')
            _cached_reports(root, manifest, dataset)
        interrupted = [group for group in manifest['groups'] if group['status'] in {'started', 'blocked'}]
        if interrupted:
            for group in interrupted:
                group.update(status='blocked', interruption='uncertain_previous_execution')
            _atomic_json(root / 'manifest.json', manifest)
            raise ValueError('Batch has interrupted/blocked groups; investigate and create a new batch')
        check_current()
        semaphore = asyncio.Semaphore(max_workers)
        failure = False

        async def run_group(index, group):
            nonlocal failure
            async with semaphore:
                if failure:
                    return
                check_current()
                group.update(status='started', started_at=_now())
                _atomic_json(root / 'manifest.json', manifest)
                try:
                    subset = _subset(dataset, group)
                    report_dir = _inside(root, f'reports/{index:02d}')
                    args = SimpleNamespace(report_dir=str(report_dir), judge=judge,
                                           max_model_calls=max_model_calls,
                                           max_model_seconds=max_model_seconds)
                    if executor is None:
                        from evaluation.scenario_runner import _live
                        report = await _live(args, subset)
                    else:
                        report = await executor(args, subset)
                    _validate_report(report, manifest, group, subset)
                    from evaluation.report_store import ReportStore
                    report_path = _inside(root, f'reports/{index:02d}/{report["run_id"]}.json')
                    if report_path.exists():
                        if _read(report_path) != report:
                            raise ValueError('Stored live report differs from returned report')
                    else:
                        ReportStore(report_dir).save(report)
                    group.update(status='completed', completed_at=_now(),
                                 report_path=report_path.relative_to(root).as_posix(),
                                 report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
                    _atomic_json(root / 'manifest.json', manifest)
                except BaseException as exc:
                    failure = True
                    group.update(status='blocked', interruption='uncertain_execution', error_type=type(exc).__name__)
                    _atomic_json(root / 'manifest.json', manifest)
                    if not isinstance(exc, Exception):
                        raise

        tasks = [asyncio.create_task(run_group(index, group)) for index, group in enumerate(manifest['groups'])
                 if group['status'] == 'pending' and (not selected or group['scenario'] in selected)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if failure:
            raise ValueError('Batch group blocked after interrupted or invalid execution; no automatic replay')
        result = aggregate_batch(root)
        _atomic_json(root / 'aggregate.json', result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('validate', 'live'), default='validate')
    parser.add_argument('--dataset', default=str(DEFAULT_DATASET))
    parser.add_argument('--batch-dir')
    parser.add_argument('--resume', help='Existing batch directory; unchanged controls required')
    parser.add_argument('--aggregate', help='Verify and summarize a batch offline')
    parser.add_argument('--scenario', action='append', default=[])
    parser.add_argument('--max-workers', type=int, choices=(1, 2), default=1)
    parser.add_argument('--judge', action='store_true')
    parser.add_argument('--max-model-calls', type=int, default=200)
    parser.add_argument('--max-model-seconds', type=float, default=1200)
    args = parser.parse_args(argv)
    if args.aggregate:
        output = aggregate_batch(args.aggregate)
    else:
        dataset = load_dataset(args.dataset)
        validation = validate_dataset(dataset)
        if args.mode == 'validate':
            output = {**validation, 'mode': 'validate', 'status': 'validated',
                      'dataset_status': validation['status']}
        else:
            from services.runtime import runtime_config_from_env
            from observability.telemetry import get_telemetry
            try:
                output = asyncio.run(run_batch(dataset, runtime_config_from_env(),
                    batch_dir=args.batch_dir, resume=args.resume, scenario=args.scenario,
                    max_workers=args.max_workers, judge=args.judge,
                    max_model_calls=args.max_model_calls, max_model_seconds=args.max_model_seconds))
            finally:
                telemetry = get_telemetry()
                try:
                    telemetry.flush()
                finally:
                    telemetry.shutdown()
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
