"""Provider finish diagnostics without content export or extra requests."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from observability.instrumentation import AnthropicClientProxy, bind_usage
from observability.telemetry import LangfuseTelemetry, bind_telemetry
from tests.test_telemetry import FakeClient as TraceClient


def exercise(response, **kwargs):
    calls = []

    async def create(**options):
        calls.append(options)
        return response

    provider = SimpleNamespace(messages=SimpleNamespace(create=create))
    trace = TraceClient()

    async def run():
        with bind_usage() as counter, bind_telemetry(LangfuseTelemetry(trace, trace_content=False)):
            actual = await AnthropicClientProxy(provider).messages.create(model='test-model', **kwargs)
            return actual, counter.snapshot()

    actual, usage = asyncio.run(run())
    updates = trace.observations[0].updates
    return actual, usage, calls, updates


@pytest.mark.parametrize('as_dict', [False, True])
def test_generation_diagnostics_preserve_response_and_count_without_exporting_thinking(as_dict):
    value = {'content': [{'type': 'thinking', 'thinking': 'private-reasoning-sentinel'}],
             'stop_reason': 'max_tokens', 'model': 'test-model',
             'usage': {'input_tokens': 282, 'output_tokens': 1100, 'cache_read_input_tokens': 896}}
    response = value if as_dict else SimpleNamespace(**value)
    actual, usage, calls, updates = exercise(response, max_tokens=1100)
    assert actual is response
    assert len(calls) == 1 and usage['model_calls'] == 1
    assert usage['output_tokens'] == 1100
    diagnostics = updates[-1]['metadata']['generation_diagnostics']
    assert diagnostics == {'stop_reason': 'max_tokens', 'block_types': ['thinking'],
                           'has_visible_text': False, 'max_tokens_reached': True}
    encoded = json.dumps(updates)
    assert 'private-reasoning-sentinel' not in encoded
    assert all('input' not in update and 'output' not in update for update in updates)


def test_visible_text_and_tool_blocks_are_classified_without_their_payloads():
    response = SimpleNamespace(content=[SimpleNamespace(type='text', text='visible-secret-sentinel'),
        {'type': 'tool_use', 'input': {'api_key': 'sk-payload-sentinel'}},
        {'type': 'text', 'text': 'another text'}, {'type': 'redacted_thinking'}],
        stop_reason='tool_use', usage={'output_tokens': 5})
    _, _, _, updates = exercise(response, max_tokens=100)
    assert updates[-1]['metadata']['generation_diagnostics'] == {
        'stop_reason': 'tool_use', 'block_types': ['text', 'tool_use', 'redacted_thinking'],
        'has_visible_text': True, 'max_tokens_reached': False}
    assert 'visible-secret-sentinel' not in json.dumps(updates)
    assert 'sk-payload-sentinel' not in json.dumps(updates)


@pytest.mark.parametrize('reason', ['Bearer sk-secret-sentinel', None, 123, {'secret': 'sentinel'}])
def test_unknown_or_missing_diagnostics_do_not_echo_arbitrary_provider_values(reason):
    response = {'content': [{'type': 'sk-untrusted-type', 'text': 'must-not-export'},
                            {'type': 'text', 'text': '   '}],
                'stop_reason': reason, 'usage': {'output_tokens': 3}}
    _, _, _, updates = exercise(response)
    diag = updates[-1]['metadata']['generation_diagnostics']
    assert diag['stop_reason'] == ('not_reported' if reason is None else 'other')
    assert diag['block_types'] == ['other', 'text']
    assert diag['has_visible_text'] is False and diag['max_tokens_reached'] is False
    assert 'sk-untrusted-type' not in json.dumps(updates)
    assert 'sk-secret-sentinel' not in json.dumps(updates)


def test_missing_or_malformed_content_is_safe_and_not_invented_text():
    for content in (None, 'private-unstructured-sentinel', {}, 42):
        _, _, _, updates = exercise({'content': content, 'usage': {'output_tokens': True}}, max_tokens=1)
        diag = updates[-1]['metadata']['generation_diagnostics']
        assert diag['block_types'] == [] and diag['has_visible_text'] is False
        assert diag['max_tokens_reached'] is False
        assert 'private-unstructured-sentinel' not in json.dumps(updates)
