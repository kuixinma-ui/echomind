"""Offline contracts for the independent knowledge requirement signal."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from core.intent_recognizer import IntentCategory, IntentRecognizer, IntentResult, UrgencyLevel
from tests.test_chat_service import Memory, Orchestrator, new_service


MISSING = object()


def provider_recognizer(payload=None, *, raw=None, error=None):
    """Replace only the external provider; preserve parsing, voting and caching."""
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        if error:
            raise error
        return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(
            type='text', text=raw if raw is not None else json.dumps(payload),
        )])

    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    recognizer.model = 'offline-model'
    recognizer.threshold = 0.5
    recognizer._embedding_enabled = False
    recognizer._cache = {}
    recognizer.cache_hits = recognizer.cache_misses = 0
    return recognizer, requests


def classification(value=MISSING, **extra):
    payload = {'intent': 'other', 'confidence': 0.2, 'reasoning': 'private classifier rationale'}
    if value is not MISSING:
        payload['knowledge_required'] = value
    return {**payload, **extra}


def test_legacy_intent_result_defaults_knowledge_requirement_off():
    result = IntentResult(IntentCategory.OTHER, .2, UrgencyLevel.LOW, 'other', {}, '', 0)
    assert result.knowledge_required is False


@pytest.mark.parametrize('value,want', [
    (True, True), (False, False), (MISSING, False), ('true', False),
    ('false', False), (1, False), (0, False), (None, False), ([], False), ({}, False),
])
def test_only_json_boolean_true_enables_requirement_without_changing_classification(value, want):
    recognizer, requests = provider_recognizer(classification(value))

    async def run():
        first = await recognizer.recognize('咨询服务适用范围')
        cached = await recognizer.recognize('咨询服务适用范围')
        assert cached is first
        return first

    result = asyncio.run(run())
    assert result.knowledge_required is want
    assert result.intent is IntentCategory.OTHER
    assert result.confidence == pytest.approx(.17)
    assert result.source_scores == {'llm': .2, 'embedding': 0., 'pattern': 0.}
    assert result.source_categories == {'llm': 'other', 'embedding': 'other', 'pattern': 'other'}
    assert result.decision_mechanism == 'below_threshold'
    assert len(requests) == 1
    assert recognizer.cache_hits == 1
    assert recognizer.cache_misses == 1


@pytest.mark.parametrize('kwargs', [
    {'raw': '{"intent":"other","knowledge_required":true'},
    {'raw': '[{"intent":"other","knowledge_required":true}]'},
    {'payload': {'confidence': .2, 'knowledge_required': True}},
    {'payload': classification(True, intent='unknown-category')},
    {'error': RuntimeError('provider unavailable')},
])
def test_failed_or_malformed_classification_disables_requirement(kwargs):
    recognizer, _ = provider_recognizer(**kwargs)
    result = asyncio.run(recognizer.recognize('咨询服务适用范围'))
    assert result.knowledge_required is False


def test_failed_legacy_classifier_cannot_leave_true_signal():
    recognizer, _ = provider_recognizer()

    async def failed(message, history=None):
        return {'intent': IntentCategory.OTHER, 'confidence': 0., 'failed': True,
                'knowledge_required': True}

    recognizer._llm_recognize = failed
    result = asyncio.run(recognizer.recognize('咨询服务适用范围'))
    assert result.knowledge_required is False
    assert result.decision_mechanism == 'llm_failed_no_signal'


def test_existing_provider_request_carries_semantic_requirement_and_handoff_distinction():
    recognizer, requests = provider_recognizer(classification(True))
    parsed = asyncio.run(recognizer._llm_recognize('咨询服务适用范围', None))
    assert parsed['knowledge_required'] is True
    assert len(requests) == 1
    prompt = requests[0]['messages'][0]['content']
    # Assert the outbound instruction contract, not source-file implementation text.
    assert 'knowledge_required' in prompt
    for concept in ('规则', '流程', '收费', '能力边界', 'true', 'false'):
        assert concept in prompt
    assert '了解转人工政策' in prompt
    assert '实际转人工' in prompt
    assert '细粒度' in prompt


@pytest.mark.parametrize('value,want', [
    (True, True), (False, False), (MISSING, False), ('true', False),
    (1, False), (None, False),
])
def test_service_passes_and_exports_only_safe_boolean_with_legacy_compatibility(value, want):
    async def run():
        orchestrator = Orchestrator()
        original = orchestrator.recognize_intent

        async def recognize(message, history=None):
            result = await original(message, history)
            if value is not MISSING:
                result.knowledge_required = value
            result.reasoning = 'private classifier rationale'
            return result

        orchestrator.recognize_intent = recognize
        service = new_service(orchestrator, Memory())
        try:
            response = await service.chat('咨询服务适用范围', 'offline-user', 'offline-conv',
                                          wait_for_profile=True)
            assert orchestrator.requests[0].knowledge_required is want
            return response
        finally:
            await service.close()

    response = asyncio.run(run())
    assert response['knowledge_required'] is want
    assert response['intent_source_scores'] == {'rule': .8}
    assert 'reasoning' not in response
    assert 'private classifier rationale' not in json.dumps(response)
