"""Exercise purpose/domain understanding through shared service and direct routing."""
import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from agents.agent_orchestrator import (
    AgentOrchestrator, AgentType, BillingAgent, EscalationAgent, GeneralAgent,
    Request, ResponseComposer, TechnicalAgent,
)
from core.intent_recognizer import IntentCategory as C, UrgencyLevel as U
from tests.test_chat_service import Memory, Orchestrator, new_service
from tests.test_knowledge_requirement import provider_recognizer
from tests.test_required_knowledge import Client, Manager, setup


def meaning(purpose='policy_question', domain='general', confidence=.9):
    return {'version': 1, 'purpose': purpose, 'domain': domain, 'confidence': confidence}


def orchestrator(payload):
    recognizer, classifications = provider_recognizer(payload)
    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch._intent_recognizer = recognizer
    orch._pool = {}
    clients = []
    manager = Manager()
    for kind, cls in ((AgentType.GENERAL, GeneralAgent), (AgentType.TECHNICAL, TechnicalAgent),
                      (AgentType.BILLING, BillingAgent), (AgentType.ESCALATION, EscalationAgent)):
        agent, client, _ = setup(cls, manager)
        orch._pool[kind] = [agent]
        clients.append(client)
    orch._recent_tool_traces = deque(maxlen=10)
    orch._composer = ResponseComposer(Client(), 'offline-model')
    return orch, classifications, clients, manager


@pytest.mark.parametrize('raw,domain,want,agent', [
    ('human_handoff', 'general', 'query', 'general'),
    ('query', 'technical', 'technical', 'technical'),
    ('other', 'billing', 'billing', 'billing'),
])
@pytest.mark.parametrize('direct', [False, True])
def test_resolved_policy_intent_domain_and_real_retrieval_agree_across_entries(raw, domain, want, agent, direct):
    payload = {'intent': raw, 'confidence': .8, 'knowledge_required': True,
               'reasoning': 'private rationale', 'request_understanding': meaning(domain=domain)}
    orch, classifications, _, manager = orchestrator(payload)

    async def exercise():
        if direct:
            return await orch.run(Request('请说明服务条款的适用范围。', 'u', 'c'))
        service = new_service(orch, Memory())
        try:
            return await service.chat('请说明服务条款的适用范围。', 'u', 'c', wait_for_profile=True)
        finally:
            await service.close()

    result = asyncio.run(exercise())
    assert len(classifications) == 1
    assert len(manager.calls) == 1
    if direct:
        assert result.intent.value == want
        assert result.primary_agent.value == agent
        assert result.tool_traces[0]['trigger'] == 'policy_preflight'
        assert not result.escalated
    else:
        assert result['intent'] == want and result['primary_agent'] == agent
        assert result['voted_intent'] == raw
        assert result['request_understanding'] == meaning(domain=domain)
        assert result['intent_resolution'] == 'policy_domain'
        assert result['intent_decision_mechanism'] == 'weighted_vote'
        assert result['intent_source_categories']['llm'] == raw
        assert result['intent_group'] == ('query' if want == 'query' else want)
        assert not result['escalated']
        assert 'private rationale' not in str(result)


def test_accepted_policy_answer_mentions_handoff_without_marking_escalation():
    agent, client, _ = setup()

    async def answer(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type='text', text='政策说明不等于实际转人工。')])

    client.messages.create = answer
    req = Request('解释适用规则', 'u', 'c', intent=C.QUERY, urgency=U.LOW,
                  knowledge_required=True, request_understanding=meaning(),
                  voted_intent='human_handoff', intent_resolution='policy_domain')
    response = asyncio.run(agent.handle(req))
    assert response.success
    assert '转人工' in response.content
    assert not response.escalate


@pytest.mark.parametrize('resolution,contract', [
    ('legacy_vote', meaning()), ('policy_domain', {}),
    ('policy_domain', meaning(purpose='service_action')),
    ('policy_domain', {**meaning(), 'private': 'not valid'}),
])
def test_unaccepted_or_invalid_contract_cannot_suppress_existing_escalation(resolution, contract):
    agent, client, _ = setup()

    async def answer(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type='text', text='这个问题需要转人工继续核验。')])

    client.messages.create = answer
    req = Request('请协助解决问题', 'u', 'c', intent=C.QUERY,
                  request_understanding=contract, intent_resolution=resolution)
    assert asyncio.run(agent.handle(req)).escalate


@pytest.mark.parametrize('intent,knowledge', [(C.OTHER, False), (C.OTHER, True), (C.QUERY, True)])
def test_critical_is_never_stopped_by_generic_clarification_or_policy_signal(intent, knowledge):
    orch, _, clients, manager = orchestrator({})
    result = asyncio.run(orch.run(Request('紧急，需要协助。', 'u', 'c', intent=intent,
        urgency=U.CRITICAL, intent_confidence=.2, knowledge_required=knowledge,
        request_understanding=meaning(), intent_resolution='critical_guard')))
    assert result.primary_agent is AgentType.ESCALATION
    assert result.escalated
    assert not manager.calls and not any(c.calls for c in clients)


def test_actual_handoff_purpose_preserves_real_escalation_even_when_legacy_is_other():
    orch, _, clients, manager = orchestrator({'intent': 'other', 'confidence': .8,
        'knowledge_required': True, 'request_understanding': meaning('human_handoff', 'billing')})

    async def exercise():
        service = new_service(orch, Memory())
        try:
            return await service.chat('请说明规则并请人工继续核验。', 'u', 'c', wait_for_profile=True)
        finally:
            await service.close()

    result = asyncio.run(exercise())
    assert result['intent'] == 'human_handoff' and result['intent_group'] == 'escalation'
    assert result['voted_intent'] == 'other'
    assert result['intent_resolution'] == 'human_request'
    assert result['primary_agent'] == 'escalation' and result['escalated']
    assert not manager.calls and not any(c.calls for c in clients)


@pytest.mark.parametrize('legacy', [True, False])
def test_service_sanitizes_diagnostics_and_keeps_legacy_results_compatible(legacy):
    async def exercise():
        orch = Orchestrator()
        previous = orch.recognize_intent

        async def recognize(message, history=None):
            item = await previous(message, history)
            if not legacy:
                item.request_understanding = {**meaning(), 'reasoning': 'private data'}
                item.voted_intent = 'private category'
                item.intent_resolution = 'private resolution'
            return item

        orch.recognize_intent = recognize
        service = new_service(orch, Memory())
        try:
            result = await service.chat('请协助排查', 'u', 'c', wait_for_profile=True)
            req = orch.requests[0]
            assert req.request_understanding == {}
            assert req.voted_intent == '' and req.intent_resolution == 'legacy_vote'
            return result
        finally:
            await service.close()

    result = asyncio.run(exercise())
    assert result['request_understanding'] == {}
    assert result['voted_intent'] == '' and result['intent_resolution'] == 'legacy_vote'
    assert 'private' not in str(result)
