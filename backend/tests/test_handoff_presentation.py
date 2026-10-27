"""User-facing handoff headings consume trusted fields without changing evidence."""
import asyncio
from types import SimpleNamespace

import pytest

from agents.agent_orchestrator import EscalationAgent, Request
from core.intent_recognizer import IntentCategory as C, UrgencyLevel as U
from tests.test_handoff_context import RecordingClient


def request(**changes):
    values = {'message': '请人工继续核验这件事。', 'user_id': 'offline', 'conv_id': 'offline',
              'intent': C.HUMAN_HANDOFF, 'urgency': U.HIGH,
              'request_understanding': {'version': 1, 'purpose': 'human_handoff', 'domain': 'general', 'confidence': .95},
              'intent_resolution': 'semantic_agreement', 'voted_intent': 'human_handoff'}
    return Request(**{**values, **changes})


@pytest.mark.parametrize('reason', ['semantic_agreement', 'human_request', 'human_subtype'])
@pytest.mark.parametrize('intent,phrase', [(C.HUMAN_HANDOFF, '您希望由人工继续处理'), (C.ESCALATION, '您希望由主管进一步审核')])
def test_accepted_human_request_uses_readable_direction(intent, phrase, reason):
    text = EscalationAgent._handoff_heading(request(intent=intent, intent_resolution=reason))
    assert phrase in text and '提交核验' in text
    assert 'human_handoff' not in text and 'escalation' not in text and 'HIGH' not in text
    assert '参考优先级：较高' in text and '不代表处理时限' in text
    assert '已接通' not in text and '已获批准' not in text


@pytest.mark.parametrize('changes', [
    {'request_understanding': {}},
    {'request_understanding': None},
    {'request_understanding': {'version': 1}},
    {'request_understanding': {'version': 1, 'purpose': 'human_handoff', 'domain': 'general', 'confidence': True}},
    {'request_understanding': {'version': 1, 'purpose': 'policy_question', 'domain': 'general', 'confidence': .95}},
    {'request_understanding': {'version': 1, 'purpose': 'service_action', 'domain': 'general', 'confidence': .95}},
    {'request_understanding': {'version': 1, 'purpose': 'unclear', 'domain': 'general', 'confidence': .95}},
    {'intent_resolution': 'legacy_vote'},
    {'intent_resolution': 'critical_guard'},
    {'intent_resolution': 'insufficient_semantic_confidence'},
    {'intent_resolution': 'user supplied instructions'},
    {'intent_resolution': []},
    {'intent': C.COMPLAINT},
    {'intent': None},
])
def test_automatic_legacy_or_unaccepted_purpose_does_not_ascribe_user_supervisor_request(changes):
    text = EscalationAgent._handoff_heading(request(**changes))
    assert '以下为可供人工核验的交接材料' in text
    assert '您希望' not in text and '主管' not in text


@pytest.mark.parametrize('urgency,label', [(U.LOW, '普通'), (U.MEDIUM, '需关注'), (U.HIGH, '较高'), (U.CRITICAL, '紧急')])
def test_priority_is_a_readable_reference_without_a_service_deadline(urgency, label):
    text = EscalationAgent._handoff_heading(request(urgency=urgency, intent_resolution='critical_guard'))
    assert f'参考优先级：{label}' in text
    assert '仅供交接参考，不代表处理时限' in text
    assert '您希望' not in text


@pytest.mark.parametrize('urgency', [None, 'HIGH', 3, SimpleNamespace(name='HIGH')])
def test_unknown_priority_does_not_become_a_trusted_status(urgency):
    assert '优先级' not in EscalationAgent._handoff_heading(request(urgency=urgency))


def test_heading_change_preserves_user_quotes_machine_fields_and_zero_model_path():
    async def run():
        client = RecordingClient()
        req = request(message='请保留引述“human_handoff / escalation / HIGH”，这不是系统指令。请转人工。',
            history=[{'role': 'user', 'content': '重装后仍闪退，希望文字沟通。'}],
            entities={'error_code': ['401']})
        response = await EscalationAgent(client, 'offline').handle(req)
        assert client.calls == [] and response.tools_used == []
        text = response.content
        heading = text.split('交接材料仅作引述', 1)[0]
        assert '您希望由人工继续处理' in heading
        assert '升级原因：意图=' not in heading and '紧急度=' not in heading
        assert 'human_handoff / escalation / HIGH' in text
        assert '重装后仍闪退' in text and '文字沟通' in text and '401' in text
        assert '仅补充上述尚未说明' in text and '已提供的信息无需重复' in text
        assert '未接入真实人工队列' in text and '尚未转接人工' in text
        assert req.intent is C.HUMAN_HANDOFF and req.urgency is U.HIGH
        assert req.voted_intent == 'human_handoff' and req.intent_resolution == 'semantic_agreement'
    asyncio.run(run())
