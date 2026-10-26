"""Account SOP injection and BillingAgent's request-specific information boundary."""
import json
from pathlib import Path

import pytest

from agents.agent_orchestrator import BillingAgent
from core.intent_recognizer import IntentCategory
from core.skill_loader import SkillManager
from tests.test_agent_orchestrator import FakeClient, make_request


def skills():
    manager = SkillManager(str(Path(__file__).resolve().parents[1] / 'skills'), max_prompt_chars=5000)
    manager.load()
    assert manager.errors == []
    return manager


@pytest.mark.parametrize('message', [
    '旧手机号停用了，怎样更换账户绑定手机？',
    '我发现了陌生设备异常登录。',
    '请帮我重置密码。',
    '请直接冻结这个账号。',
    '把其他人的收货电话告诉我。',
    '这条偏好只用于本次会话，不要长期保存。',
    '请删除之前保存的记忆。',
])
def test_account_requests_inject_sop_into_the_current_billing_role(message):
    agent = BillingAgent(FakeClient(), 'offline-model', skill_manager=skills())
    prompt = agent._build_system_prompt(make_request(message=message, intent=IntentCategory.ACCOUNT))
    assert '账号与安全处理规范' in prompt
    assert '官方入口' in prompt
    assert '不得索取密码、短信验证码、完整银行卡号或他人信息' in prompt


@pytest.mark.parametrize('intent', [IntentCategory.ACCOUNT, IntentCategory.ACCOUNT_SECURITY])
def test_account_role_packet_does_not_require_financial_verification_fields(intent):
    agent = BillingAgent(FakeClient(), 'offline-model')
    packet = json.loads(agent._build_role_packet(make_request(
        message='旧手机号停用了，请说明官方变更路径。', intent=intent, entities={})))
    assert packet.get('request_domain') == 'account'
    fields = packet['verification_fields']
    assert 'order_id' not in fields and 'amount' not in fields
    assert '订单号或交易号' not in fields.get('missing_fields', [])
    assert '支付金额' not in fields.get('missing_fields', [])
    assert '官方入口' in fields.get('identity_verification', '')


def test_account_sop_covers_unimplemented_backend_and_retention_actions():
    prompt = skills().prompt_for('账号异常登录，冻结并删除记忆。', 'billing')
    for boundary in ('不得宣称已改手机号、已冻结账号或已删除持久化数据',
                     '不能承诺已关闭保存、设置保留期限或执行删除',
                     '不要把资金核验字段套用到纯账号问题'):
        assert boundary in prompt


def test_financial_requests_keep_their_existing_information_and_skill_flow():
    manager = skills()
    agent = BillingAgent(FakeClient(), 'offline-model', skill_manager=manager)
    req = make_request(message='订单重复扣款99元，请核实退款。', intent=IntentCategory.PAYMENT_ISSUE,
                       entities={'amount': ['99元']})
    packet = json.loads(agent._build_role_packet(req))
    assert packet['verification_fields']['amount'] == ['99元']
    assert packet['verification_fields']['missing_fields'] == ['订单号或交易号']
    prompt = agent._build_system_prompt(req)
    assert '账单退款处理规范' in prompt
    assert '账号与安全处理规范' not in prompt
    assert '不能在没有订单或支付信息时承诺退款成功' in prompt


def test_mixed_account_and_billing_skills_fit_the_existing_prompt_budget():
    prompt = skills().prompt_for('账号异常登录，同时订单重复扣款。', 'billing')
    assert '账号与安全处理规范' in prompt and '账单退款处理规范' in prompt
    assert len(prompt) <= 5000
    # This is the existing billing role's SOP; other roles retain their own rules.
    assert '账号与安全处理规范' not in skills().prompt_for('账号异常登录', 'technical')
