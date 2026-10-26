import copy

import pytest

from evaluation.report_store import ReportStore


def report(run_id='one'):
    return {'run_id': run_id, 'metadata': {'fingerprints': {n: n for n in
            ('dataset', 'model', 'prompt', 'skills', 'knowledge', 'code', 'judge', 'retrieval')}},
            'total': 2, 'passed': 1, 'pass_rate': 0.5, 'invalid': 0, 'hard_fail': 0,
            'status': 'completed', 'cases': []}


def test_archive_unique_safe_paths_and_explicit_baseline(tmp_path):
    store = ReportStore(tmp_path)
    store.save(report())
    assert store.load('one')['run_id'] == 'one'
    assert store.list()[0]['run_id'] == 'one'
    with pytest.raises(FileExistsError): store.save(report())
    for bad in ('../bad', 'x/y', 'CON', 'NUL', '.hidden', 'baseline'):
        with pytest.raises(ValueError): store.load(bad)
    assert store.baseline() is None
    store.promote_baseline('one')
    store.save(report('two'))
    assert store.baseline()['run_id'] == 'one'


def test_comparison_requires_all_fingerprints_and_no_fabricated_deltas(tmp_path):
    store = ReportStore(tmp_path)
    left, right = report('left'), report('right')
    store.save(left); store.save(right)
    assert store.compare('left', 'right')['comparable'] is True
    changed = copy.deepcopy(right); changed['run_id'] = 'changed'
    changed['metadata']['fingerprints']['knowledge'] = 'changed'
    store.save(changed)
    compared = store.compare('left', 'changed')
    assert compared['comparable'] is False and compared['deltas'] == {}
    missing = report('missing'); del missing['metadata']['fingerprints']['judge']
    store.save(missing)
    assert store.compare('left', 'missing')['comparable'] is False


def test_comparison_explicit_variants_and_paired_cases(tmp_path):
    store = ReportStore(tmp_path)
    left, right = report('left'), report('right')
    right['metadata']['fingerprints']['model'] = 'new-model'
    left['cases'] = [{'id': 'task1', 'status': 'failed', 'turns': [{'latency_ms': 10}]}]
    right['cases'] = [{'id': 'task1', 'status': 'passed', 'turns': [{'latency_ms': 7}]}]
    store.save(left); store.save(right)
    compared = store.compare('left', 'right', vary=['model'])
    assert compared['comparable'] is True
    assert compared['changed_factors'] == ['model']
    assert compared['paired'][0]['id'] == 'task1'
    assert compared['paired'][0]['left_status'] == 'failed'
    assert compared['paired'][0]['right_status'] == 'passed'
    assert compared['paired'][0]['latency_delta_ms'] == -3
    with pytest.raises(ValueError):
        store.compare('left', 'right', vary=['knowledge'])


def test_missing_or_mismatched_case_ids_refuse_pairing(tmp_path):
    store = ReportStore(tmp_path)
    left, right = report('left'), report('right')
    left['cases'] = [{'id': 'one', 'status': 'passed'}]
    right['cases'] = [{'id': 'two', 'status': 'passed'}]
    store.save(left); store.save(right)
    assert store.compare('left', 'right')['comparable'] is False
