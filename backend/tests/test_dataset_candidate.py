"""Candidate oracles may clarify intent, but must preserve the released fixture."""
import hashlib
from pathlib import Path

import pytest

from evaluation.scenarios import load_dataset, validate_dataset


DATASETS = Path(__file__).resolve().parents[1] / 'evaluation' / 'datasets'
SOURCE = DATASETS / 'customer_service_v1.json'
CANDIDATE = DATASETS / 'customer_service_v1_1_candidate.json'
# Ignore Git's platform checkout line endings, not source content.
SOURCE_SHA256 = 'da7bc63086e2168911445b8205c43a34930ade76aa53c3f0a20540a3a3a939f6'
BOUNDARY_CASES = {'basic_routing_05', 'basic_routing_06', 'basic_routing_09', 'basic_routing_13'}
MODIFIED_CASES = BOUNDARY_CASES | {'knowledge_boundaries_11', 'multi_turn_12'}


def proposed_dataset():
    assert CANDIDATE.is_file(), 'The separately versioned candidate fixture is missing'
    return load_dataset(CANDIDATE)


def indexed(data):
    return {case['id']: case for case in data['cases']}


def test_released_v1_snapshot_is_not_rewritten():
    assert hashlib.sha256(SOURCE.read_bytes().replace(b'\r\n', b'\n')).hexdigest() == SOURCE_SHA256


def test_candidate_is_unapproved_and_keeps_coverage_and_knowledge():
    candidate = proposed_dataset()
    source = load_dataset(SOURCE)
    summary = validate_dataset(candidate)
    assert (candidate['id'], candidate['version'], candidate['status']) == (
        'customer_service_v1_1_candidate', '1.1.0', 'synthetic_candidate_pending_business_review')
    assert (summary['case_count'], summary['turn_count'], summary['assertion_count']) == (120, 135, 706)
    assert summary['scenarios'] == validate_dataset(source)['scenarios']
    assert candidate['knowledge'] == source['knowledge']
    assert [case['id'] for case in candidate['cases']] == [case['id'] for case in source['cases']]


def test_all_existing_assertions_and_hard_boundaries_survive():
    candidate = proposed_dataset()
    proposed = indexed(candidate)
    for case in load_dataset(SOURCE)['cases']:
        rules = {rule['id']: rule for rule in proposed[case['id']]['assertions']}
        for rule in case['assertions']:
            if case['id'] == 'knowledge_boundaries_11' and rule['id'] in {'intent_0', 'routing'}:
                continue
            assert rules[rule['id']] == rule, (case['id'], rule['id'])


def test_only_declared_cases_change_beyond_review_tags():
    candidate = proposed_dataset()
    source = indexed(load_dataset(SOURCE))
    changed = set()
    for case in candidate['cases']:
        old = {key: value for key, value in source[case['id']].items() if key != 'tags'}
        new = {key: value for key, value in case.items() if key != 'tags'}
        if old != new:
            changed.add(case['id'])
        assert {'synthetic', 'pending_business_review', 'candidate_v1_1'} <= set(case['tags'])
        assert ('ai_modified' in case['tags']) == (case['id'] in MODIFIED_CASES)
    assert changed == MODIFIED_CASES


def test_handoff_policy_question_uses_query_with_real_retrieval():
    candidate = proposed_dataset()
    original = indexed(load_dataset(SOURCE))['knowledge_boundaries_11']
    case = indexed(candidate)['knowledge_boundaries_11']
    rules = {rule['id']: rule for rule in case['assertions']}
    assert case['turns'][0]['message'] == original['turns'][0]['message']
    assert case['turns'][0]['expected_intent'] == rules['intent_0']['expected'] == 'query'
    assert rules['routing']['expected'] == 'general'
    assert rules['retrieve']['tool'] == 'search_knowledge_base'
    assert rules['source']['expected'] == ['handoff']
    assert rules['policy']['hard'] is True


def test_complaint_turn_has_explicit_unresolved_and_disappointed_context():
    candidate = proposed_dataset()
    original = indexed(load_dataset(SOURCE))['multi_turn_12']
    case = indexed(candidate)['multi_turn_12']
    text = case['turns'][0]['message']
    assert '尝试' in text and '仍未解决' in text and '失望' in text
    assert case['turns'][0]['expected_intent'] == 'complaint'
    assert case['turns'][1:] == original['turns'][1:]
    assert case['assertions'] == original['assertions']


@pytest.mark.parametrize('case_id', sorted(BOUNDARY_CASES))
def test_basic_business_behavior_is_a_required_hard_semantic_rule(case_id):
    candidate = proposed_dataset()
    case = indexed(candidate)[case_id]
    original_ids = {rule['id'] for rule in indexed(load_dataset(SOURCE))[case_id]['assertions']}
    added = [rule for rule in case['assertions'] if rule['id'] not in original_ids]
    assert len(added) == 1
    rule = added[0]
    assert rule['type'] == 'semantic' and rule['turn'] == 0
    assert rule['required'] is True and rule['hard'] is True
    assert 'demo_scope' in rule['evidence']


def test_account_boundary_examples_are_not_relabelled_to_fit_the_rule():
    candidate = proposed_dataset()
    source = indexed(load_dataset(SOURCE))
    proposed = indexed(candidate)
    for case_id in ('technical_security_12', 'cross_session_memory_07'):
        assert proposed[case_id]['turns'] == source[case_id]['turns']
        assert proposed[case_id]['turns'][0]['expected_intent'] == 'account'
        assert proposed[case_id]['assertions'] == source[case_id]['assertions']
