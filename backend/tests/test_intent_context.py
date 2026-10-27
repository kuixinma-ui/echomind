"""Typed archived user context is bounded data, never reconstructed from prose."""
import copy
import json

import pytest

from context.memory_evidence import memory_evidence_id


def record(content='我想看订单状态，但还未提交订单号。', **changes):
    return {'role': 'user', 'content': content, 'source_conv': 'prior',
            'evidence_id': memory_evidence_id('archive document fixture'), **changes}


def normalize(value):
    from core.intent_context import normalize_archived_user_messages
    return normalize_archived_user_messages(value)


def test_normalizer_copies_whitelisted_fields_without_mutating_source():
    original = [record(extra='not a classifier instruction')]
    before = copy.deepcopy(original)
    actual = normalize(original)
    assert actual == [record()]
    actual[0]['content'] = 'mutated'
    assert original == before


@pytest.mark.parametrize('value', [None, {}, 'user: topic', 1, [None], ['user: topic'],
    [record(role='assistant')], [record(role='system')], [record(content={})],
    [record(source_conv='')], [record(source_conv=' ' * 2)], [record(source_conv='x' * 257)],
    [record(evidence_id='mem_forged')], [record(evidence_id='mem_' + 'A' * 64)],
    [record(content='  ')], [{'role': 'user', 'content': 'no provenance'}]])
def test_untyped_or_invalid_archive_is_not_classifier_context(value):
    assert normalize(value) == []


def test_normalizer_bounds_work_count_content_and_serialized_json():
    values = [record('主题' * 1000, source_conv='prior-' + str(i)) for i in range(100)]
    result = normalize(values)
    assert result and len(result) <= 5
    assert all(len(item['content']) <= 500 for item in result)
    assert len(json.dumps(result, ensure_ascii=False)) <= 3000
    assert normalize([None] * 30 + [record()]) == []


def test_normalizer_removes_invalid_surrogates_and_duplicates():
    result = normalize([record('旧主题\ud800'), record('旧主题\ud800')])
    assert len(result) == 1
    assert result[0]['content'] == '旧主题'
    json.dumps(result, ensure_ascii=False).encode('utf-8')


def test_normalizer_handles_escaped_content_within_json_limit():
    values = [record('\n\t"\\' * 300, source_conv='prior-' + str(i)) for i in range(30)]
    result = normalize(values)
    assert len(json.dumps(result, ensure_ascii=False)) <= 3000
    assert len(result) <= 5
