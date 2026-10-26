"""Account updates must not become security events from a generic adverb."""
import asyncio

import pytest

from core.intent_recognizer import IntentCategory
from tests.test_intent_recognizer import local_recognizer


@pytest.mark.parametrize('message', [
    '旧手机号停用了，怎样安全更换账户绑定手机？',
    '我过去说的手机号已经作废，怎样通过官方入口安全更换？',
    '我想安全地更新个人信息。',
])
def test_safe_account_update_does_not_force_a_security_intent(message):
    result = asyncio.run(local_recognizer(IntentCategory.ACCOUNT).recognize(message))

    assert result.intent is IntentCategory.ACCOUNT
    assert 'refined_by_pattern' not in result.source_scores


@pytest.mark.parametrize('message', [
    '请帮助我检查账户安全。',
    '请帮助我检查账号安全。',
    '请打开安全设置页面。',
    '账户出现异常登录。',
    '我的账户被盗了。',
    '我需要重置密码。',
    '如何开启两步验证？',
    '我想更换绑定手机号，因为账户发生异常登录。',
])
def test_explicit_security_task_or_event_still_refines_generic_account(message):
    result = asyncio.run(local_recognizer(IntentCategory.ACCOUNT).recognize(message))

    assert result.intent is IntentCategory.ACCOUNT_SECURITY
