"""Offline request and provenance contracts; no provider client is constructed."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from core.intent_recognizer import IntentCategory as C, IntentRecognizer, _TEMPLATES
from tests.test_chat_service import Memory, Orchestrator, new_service


def recognizer_for(llm=None, embedding=None, pattern=None, *, enabled=True):
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer.model = 'offline-model'
    recognizer.threshold = 0.5
    recognizer._embedding_enabled = enabled
    recognizer._cache = {}
    recognizer.cache_hits = recognizer.cache_misses = 0

    async def llm_result(message, history=None):
        return llm

    async def embedding_result(message):
        return embedding

    if llm is not None:
        recognizer._llm_recognize = llm_result
    recognizer._embedding_recognize = embedding_result
    if pattern is not None:
        recognizer._pattern_recognize = lambda message: pattern
    return recognizer


def signal(intent, confidence, **extra):
    return {'intent': intent, 'confidence': confidence, **extra}


def test_sent_prompt_contains_existing_labeled_examples_current_message_and_recent_history():
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps({
            'intent': 'request', 'confidence': 0.9, 'reasoning': 'fixture',
        }))])

    recognizer = recognizer_for()
    recognizer.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    result = asyncio.run(recognizer._llm_recognize('现在请取消这笔订单', [
        {'role': 'user', 'content': '旧的第四条不应送出'},
        {'role': 'user', 'content': '订单还没发货'},
        {'role': 'assistant', 'content': '您希望继续办理吗'},
        {'role': 'user', 'content': '我已确认取消'},
    ]))

    assert result['intent'] is C.REQUEST
    assert len(requests) == 1
    sent = requests[0]['messages'][0]['content']
    for category, templates in _TEMPLATES.items():
        assert f'消息: "{templates[0]}" → 意图: {category.value}' in sent
    assert '用户消息: "现在请取消这笔订单"' in sent
    assert 'user: 订单还没发货' in sent
    assert 'assistant: 您希望继续办理吗' in sent
    assert 'user: 我已确认取消' in sent
    assert '旧的第四条不应送出' not in sent


@pytest.mark.parametrize('llm,embedding,pattern,enabled,want,score,mechanism', [
    (signal(C.LOGISTICS, .9), signal(C.REQUEST, .4), signal(C.QUERY, .75), True,
     C.LOGISTICS, .63, 'weighted_vote'),
    (signal(C.OTHER, 1), signal(C.REQUEST, 0), signal(C.QUERY, 0), True,
     C.OTHER, .7, 'weighted_vote'),
    (signal(C.QUERY, 1), signal(C.OTHER, 0), signal(C.REFUND, .5), True,
     C.REFUND, .7, 'pattern_refinement'),
    (signal(C.QUERY, 1), signal(C.QUERY, .5), signal(C.REFUND, .5), True,
     C.REFUND, .7999999999999999, 'pattern_refinement'),
    (signal(C.QUERY, .9), signal(C.QUERY, .85), signal(C.REFUND, .5), True,
     C.QUERY, .8, 'weighted_vote'),
    (signal(C.QUERY, .5), signal(C.OTHER, 0), signal(C.REFUND, .499), True,
     C.OTHER, .35, 'below_threshold'),
    (signal(C.REQUEST, .6), signal(C.REQUEST, .4), signal(C.OTHER, 0), True,
     C.REQUEST, .5, 'weighted_vote'),
    (signal(C.REQUEST, .5), signal(C.REQUEST, .5), signal(C.REQUEST, .5), True,
     C.OTHER, .49999999999999994, 'below_threshold'),
    (signal(C.REQUEST, .499), signal(C.REQUEST, .499), signal(C.REQUEST, .499), True,
     C.OTHER, .499, 'below_threshold'),
    (signal(C.QUERY, 1), signal(C.REFUND, 1), signal(C.REFUND, .5), False,
     C.QUERY, .85, 'weighted_vote'),
    (signal(C.OTHER, 0, failed=True), signal(C.ACCOUNT, .01), signal(C.REFUND, 1), True,
     C.ACCOUNT, .01, 'llm_failed_embedding'),
    (signal(C.OTHER, 0, failed=True), signal(C.OTHER, 1), signal(C.REFUND, .01), True,
     C.REFUND, .01, 'llm_failed_pattern'),
    (signal(C.OTHER, 0, failed=True), signal(C.ACCOUNT, 0), signal(C.REFUND, 0), True,
     C.OTHER, 0, 'llm_failed_no_signal'),
])
def test_provenance_records_actual_categories_and_branch_without_changing_votes(
    llm, embedding, pattern, enabled, want, score, mechanism,
):
    llm['reasoning'] = 'private reasoning / secret-token / user narrative'
    recognizer = recognizer_for(llm, embedding, pattern, enabled=enabled)

    async def run():
        first = await recognizer.recognize('private user narrative')
        cached = await recognizer.recognize('private user narrative')
        assert cached is first
        return first

    result = asyncio.run(run())
    assert result.intent is want
    assert result.confidence == pytest.approx(score)
    assert result.source_categories == {
        'llm': llm['intent'].value,
        'embedding': embedding['intent'].value if enabled else 'other',
        'pattern': pattern['intent'].value,
    }
    assert result.decision_mechanism == mechanism
    assert all(type(value) is float for value in result.source_scores.values())
    diagnostics = json.dumps([result.source_categories, result.decision_mechanism])
    assert 'private' not in diagnostics and 'secret-token' not in diagnostics
    actual_embedding = embedding if enabled else signal(C.OTHER, 0)
    legacy = recognizer._vote(llm, actual_embedding, pattern)
    assert len(legacy) == 3
    assert legacy[0] is want
    assert legacy[1] == pytest.approx(score)
    assert legacy[2] == result.source_scores


@pytest.mark.parametrize('with_diagnostics', [False, True])
def test_chat_exports_optional_provenance_and_supports_legacy_results(with_diagnostics):
    async def run():
        orch = Orchestrator()
        original = orch.recognize_intent

        async def recognize(message, history=None):
            result = await original(message, history)
            if with_diagnostics:
                result.source_categories = {'llm': 'technical_login', 'embedding': 'account', 'pattern': 'technical_login'}
                result.decision_mechanism = 'weighted_vote'
            return result

        orch.recognize_intent = recognize
        service = new_service(orch, Memory())
        try:
            return await service.chat('登录401', 'offline-user', 'offline-conv', wait_for_profile=True)
        finally:
            await service.close()

    response = asyncio.run(run())
    assert response['intent_source_scores'] == {'rule': .8}
    assert response['intent_source_categories'] == ({
        'llm': 'technical_login', 'embedding': 'account', 'pattern': 'technical_login',
    } if with_diagnostics else {})
    assert response['intent_decision_mechanism'] == ('weighted_vote' if with_diagnostics else 'unreported')
