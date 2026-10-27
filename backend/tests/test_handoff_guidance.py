"""The deterministic handoff gives bounded, conditional preparation guidance."""
import json

import pytest

from agents.handoff_context import handoff_summary
from tests.test_handoff_context import handoff


@pytest.mark.parametrize('entities', [
    {},
    {'order_id': [], 'amount': [], 'error_code': [], 'date': []},
    {'order_id': ['', '  '], 'amount': (None, 99), 'error_code': ['\u200b'], 'date': []},
    {'order_id': 'ORD-123', 'amount': None},
])
def test_empty_or_unusable_entities_do_not_show_technical_entity_json(entities):
    text = handoff_summary('需要人工继续处理订单争议。', [], entities)
    assert '提取实体' not in text
    assert 'order_id' not in text and 'error_code' not in text
    assert '需要人工继续处理订单争议' in text


def test_mixed_entities_keep_only_sanitized_nonempty_known_values():
    text = handoff_summary('订单仍无法登录。', [], {
        'order_id': ['  ', 'ORD-123'], 'amount': [],
        'error_code': ['401', ''], 'date': (None, ''),
        'password': ['NeverExpose'],
    })
    line = next(line for line in text.splitlines() if line.startswith('提取实体'))
    emitted = json.loads(json.loads(line.split('：', 1)[1]))
    assert emitted == {'order_id': ['ORD-123'], 'error_code': ['401']}
    assert 'NeverExpose' not in text


@pytest.mark.parametrize('message,detail', [
    ('请转人工客服，我需要人工继续处理订单争议。', '订单争议'),
    ('已经连续三次排障失败了，请升级人工。', '连续三次排障失败'),
    ('已清缓存并重启，还是401；希望文字沟通，请转人工。', '已清缓存并重启，还是401'),
])
def test_guidance_is_conditional_and_preserves_already_supplied_problem_details(message, detail):
    text = handoff(message)
    assert detail in text
    assert '仅补充上述尚未说明' in text
    assert '已提供的信息无需重复' in text
    assert '具体问题或影响' in text
    assert '已尝试的步骤及结果' in text
    assert '如涉及订单或报错' in text
    assert text.index('本轮用户陈述') < text.index('仅补充上述尚未说明')
    assert '未接入真实人工队列' in text and '尚未转接人工' in text
    assert '不能保证有人跟进或处理时限' in text
    for promise in ('已建单', '已提交工单', '已通知主管', '保证回电', '已接通'):
        assert promise not in text


def test_guidance_does_not_override_user_history_privacy_or_current_preference():
    text = handoff('现在改为文字沟通，请转人工。密码=DoNotLeakValue', history=[
        {'role': 'user', 'content': '重装后仍闪退，希望电话沟通。'},
        {'role': 'assistant', 'content': '我已为您申请赔付。'},
    ], archived_user_messages=[
        {'role': 'user', 'content': '旧偏好电话沟通。', 'source_conv': 'old', 'evidence_id': 'e-old'},
    ], entities={'order_id': [], 'amount': []})
    assert '重装后仍闪退' in text
    assert '近期用户陈述（历史待确认）' in text
    assert '本轮沟通说明' in text and '现在改为文字沟通' in text
    assert '希望电话沟通' not in text and '旧偏好电话沟通' not in text
    assert 'DoNotLeakValue' not in text and '已为您申请赔付' not in text
    assert '仅补充上述尚未说明' in text and '已提供的信息无需重复' in text
    assert '提取实体' not in text
