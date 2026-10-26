"""Offline intent regressions with only the external LLM response substituted."""

import asyncio
from types import SimpleNamespace

import pytest

from core.intent_recognizer import IntentCategory, IntentRecognizer


def local_recognizer(llm_intent):
    # No API client is constructed: embeddings exercise the production local path.
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer.client = SimpleNamespace()
    recognizer.threshold = 0.5
    recognizer._embedding_enabled = True
    recognizer._tpl_embeddings = {}
    recognizer._cache = {}
    recognizer.cache_hits = 0
    recognizer.cache_misses = 0

    async def llm_response(message, history=None):
        return {"intent": llm_intent, "confidence": 1.0, "reasoning": "offline fixture"}

    recognizer._llm_recognize = llm_response
    return recognizer


@pytest.mark.parametrize("message", [
    "你能承诺经理会在十分钟内回电并赔偿500元吗？",
    "请经理审核500.00元的补偿申请。",
    "请经理审核￥500的补偿申请。",
    "请经理审核500 USD的补偿申请。",
    "请经理审核5000元的补偿申请。",
])
def test_compensation_amount_does_not_override_escalation_with_http_error(message):
    result = asyncio.run(local_recognizer(IntentCategory.ESCALATION).recognize(message))

    assert result.intent is IntentCategory.ESCALATION


@pytest.mark.parametrize("message", [
    "结算页面显示500错误，刷新后仍然一样。",
    "页面出现 HTTP 500。",
    "网页报500错误。",
    "订单金额500元，但页面出现 HTTP 500。",
])
def test_actual_http_500_keeps_technical_crash_pattern(message):
    recognizer = local_recognizer(IntentCategory.TECHNICAL_CRASH)

    result = recognizer._pattern_recognize(message)

    assert result["intent"] is IntentCategory.TECHNICAL_CRASH
