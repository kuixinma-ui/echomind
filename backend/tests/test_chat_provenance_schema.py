"""The HTTP response model retains safe additive intent provenance."""
from api.main import ChatResponse


def test_chat_response_keeps_intent_provenance_in_serialization():
    categories = {'llm': 'refund', 'embedding': 'billing', 'pattern': 'query'}
    response = ChatResponse(conv_id='c', response='核验退款进度。', intent='refund', agent_type='billing',
                            escalated=False, latency_ms=1,
                            intent_source_categories=categories, intent_decision_mechanism='weighted_vote')
    payload = response.model_dump()
    assert payload['intent_source_categories'] == categories
    assert payload['intent_decision_mechanism'] == 'weighted_vote'


def test_chat_response_defaults_preserve_legacy_service_replies():
    payload = ChatResponse(conv_id='c', response='您好。', intent='greeting', agent_type='general',
                           escalated=False, latency_ms=1).model_dump()
    assert payload['intent_source_categories'] == {}
    assert payload['intent_decision_mechanism'] == ''
    assert payload['knowledge_required'] is False


def test_chat_response_preserves_explicit_knowledge_requirement():
    payload = ChatResponse(conv_id='c', response='请参考已核验的服务规则。', intent='other',
                           agent_type='general', escalated=False, latency_ms=1,
                           knowledge_required=True).model_dump()
    assert payload['knowledge_required'] is True


def test_chat_response_retains_safe_understanding_and_raw_vote():
    contract = {'version': 1, 'purpose': 'policy_question', 'domain': 'billing', 'confidence': .9}
    payload = ChatResponse(conv_id='c', response='请核验政策条款。', intent='billing',
                           agent_type='billing', escalated=False, latency_ms=1,
                           request_understanding=contract, voted_intent='other',
                           intent_resolution='policy_domain').model_dump()
    assert payload['request_understanding'] == contract
    assert payload['voted_intent'] == 'other'
    assert payload['intent_resolution'] == 'policy_domain'


def test_chat_response_defaults_understanding_without_changing_legacy_fields():
    payload = ChatResponse(conv_id='c', response='您好。', intent='greeting',
                           agent_type='general', escalated=False, latency_ms=1).model_dump()
    assert payload['request_understanding'] == {}
    assert payload['voted_intent'] == '' and payload['intent_resolution'] == 'legacy_vote'
