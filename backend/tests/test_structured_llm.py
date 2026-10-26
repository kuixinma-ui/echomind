"""Structured Anthropic-compatible calls; all model boundaries are offline."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from core import llm_utils


def response(text=None, *, stop_reason='end_turn'):
    blocks = [SimpleNamespace(type='thinking', thinking='private internal reasoning')]
    if text is not None:
        blocks.append(SimpleNamespace(type='text', text=text))
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


@pytest.mark.parametrize('payload,kind,expected', [
    ('```json\n{"answer": 1}\n```', dict, {'answer': 1}),
    ('```json\n[1, 0]\n```', list, [1, 0]),
    ('Result: {"answer": 1}\nDone.', dict, {'answer': 1}),
])
def test_parser_reads_text_blocks_and_fenced_structures(payload, kind, expected):
    message = response(payload)
    assert llm_utils.parse_structured_response(message, kind) == expected


def test_parser_joins_text_blocks_without_reading_thinking_or_tool_blocks():
    message = SimpleNamespace(stop_reason='end_turn', content=[
        {'type': 'thinking', 'thinking': '{"wrong": true}'},
        {'type': 'text', 'text': '```json\n{"answer":'},
        {'type': 'tool_use', 'text': 'untrusted tool body', 'input': {}},
        {'type': 'text', 'text': '1}\n```'},
    ])
    assert llm_utils.parse_structured_response(message, dict) == {'answer': 1}


@pytest.mark.parametrize('message,kind,reason', [
    (response(stop_reason='max_tokens'), dict, 'max_tokens'),
    (response(), dict, 'empty_text'),
    (response('{"answer": 1}', stop_reason='max_tokens'), dict, 'max_tokens'),
    (response('{"private":'), dict, 'invalid_json'),
    (response('[{"answer": 1}]'), dict, 'wrong_type'),
    (response('{"answer": 1}'), list, 'wrong_type'),
])
def test_parser_rejects_empty_truncated_or_wrong_shape_without_payload(message, kind, reason):
    with pytest.raises(llm_utils.StructuredResponseError) as captured:
        llm_utils.parse_structured_response(message, kind)
    assert captured.value.reason == reason
    assert str(captured.value) == 'structured_response_' + reason
    assert 'private' not in str(captured.value)


@pytest.mark.parametrize('mode,expected', [('provider_default', {}),
                                        ('disabled', {'extra_body': {'thinking': {'type': 'disabled'}}})])
def test_vendor_thinking_extension_requires_explicit_policy(monkeypatch, mode, expected):
    monkeypatch.delenv('ECHOMIND_STRUCTURED_THINKING', raising=False)
    assert llm_utils.structured_request_options() == {}
    monkeypatch.setenv('ECHOMIND_STRUCTURED_THINKING', mode)
    assert llm_utils.structured_request_options() == expected


def test_invalid_thinking_policy_is_not_silently_guessed(monkeypatch):
    monkeypatch.setenv('ECHOMIND_STRUCTURED_THINKING', 'enabled')
    with pytest.raises(ValueError, match='structured thinking policy'):
        llm_utils.structured_request_options()


@pytest.mark.parametrize('mode', ['provider_default', 'disabled'])
def test_pinned_sdk_serializes_explicit_extension_as_top_level_body(monkeypatch, mode):
    from anthropic import AsyncAnthropic
    monkeypatch.setenv('ECHOMIND_STRUCTURED_THINKING', mode)
    bodies = []
    def transport(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'msg-offline', 'type': 'message', 'role': 'assistant',
             'model': 'offline', 'content': [{'type': 'text', 'text': '```json\n{"ok": true}\n```'}],
             'stop_reason': 'end_turn', 'stop_sequence': None,
             'usage': {'input_tokens': 1, 'output_tokens': 8}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            async with AsyncAnthropic(api_key='offline-not-a-real-key', base_url='https://offline.invalid',
                                      http_client=http, max_retries=0) as client:
                result = await client.messages.create(model='offline', max_tokens=256,
                    messages=[{'role': 'user', 'content': 'Synthetic JSON request'}],
                    **llm_utils.structured_request_options())
                assert llm_utils.parse_structured_response(result, dict) == {'ok': True}
    asyncio.run(run())
    assert bodies[0]['max_tokens'] == 256
    if mode == 'disabled':
        assert bodies[0]['thinking'] == {'type': 'disabled'}
    else:
        assert 'thinking' not in bodies[0]


class Client:
    def __init__(self, payloads):
        self.messages = self
        self.payloads = iter(payloads)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.payloads)

    async def close(self):
        pass


@pytest.mark.parametrize('mode', ['provider_default', 'disabled'])
def test_five_structured_call_sites_keep_budgets_business_validation_and_policy(monkeypatch, mode):
    from core.intent_recognizer import IntentCategory, IntentRecognizer
    from evaluation.evaluator import LLMJudge
    from mcp.tool_manager import MCPToolManager
    from memory.conversation_memory import MemoryManager
    from memory.roles import MsgRole
    from tests.test_context_storage import Chroma, Redis
    monkeypatch.setenv('ECHOMIND_STRUCTURED_THINKING', mode)
    client = Client([response('```json\n{"relevance":1,"accuracy":0.8,"completeness":1,"helpfulness":1}\n```'),
                     response('```json\n{"intent":"invoice","confidence":0.9,"reasoning":"fixture"}\n```'),
                     response('```json\n["invoice policy"]\n```'), response('```json\n[1,0]\n```'),
                     response('```json\n{"preferences":["email"],"entities":{}}\n```')])
    async def run():
        score = await LLMJudge(client, 'offline').judge('Q', 'A')
        assert not score.judge_failed and score.accuracy == 0.8
        recognizer = IntentRecognizer.__new__(IntentRecognizer)
        recognizer.client, recognizer.model = client, 'offline'
        recognized = await recognizer._llm_recognize('Invoice please', None)
        assert recognized['intent'] is IntentCategory.INVOICE
        tools = MCPToolManager('offline', client=client)
        assert await tools.rewrite_query('invoice') == ['invoice', 'invoice policy']
        ordered, reranked = await tools._rerank_with_status('invoice', [{'id': 'first'}, {'id': 'second'}], 1)
        assert reranked and ordered == [{'id': 'second'}]
        memory = MemoryManager(namespace='eval_' + 'e' * 32, redis_client=Redis(), chroma_client=Chroma(), client=client)
        await memory.add_message('synthetic-user', 'synthetic-conversation', MsgRole.USER, 'Contact by email')
        assert await memory.update_profile('synthetic-user', 'synthetic-conversation') is True
        assert (await memory._get_profile('synthetic-user'))['preferences'] == ['email']
        await memory.close()
    asyncio.run(run())
    assert [call['max_tokens'] for call in client.calls] == [256, 256, 256, 256, 512]
    assert all(call.get('extra_body') == ({'thinking': {'type': 'disabled'}} if mode == 'disabled' else None)
               for call in client.calls)


def test_max_tokens_judge_remains_unscored_with_safe_reason(monkeypatch):
    from evaluation.evaluator import LLMJudge
    monkeypatch.setenv('ECHOMIND_STRUCTURED_THINKING', 'provider_default')
    score = asyncio.run(LLMJudge(Client([response(stop_reason='max_tokens')]), 'offline').judge('Q', 'A'))
    assert score.judge_failed and score.overall is None
    assert score.error == 'structured_response_max_tokens'


def test_runtime_and_fingerprints_record_thinking_policy_without_reading_dotenv(monkeypatch, tmp_path):
    import dotenv
    from evaluation.scenarios import build_fingerprints
    from services.runtime import runtime_config_from_env
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setenv('ECHOMIND_STRUCTURED_THINKING', 'disabled')
    assert runtime_config_from_env()['structured_thinking'] == 'disabled'
    dataset = {'knowledge': []}
    common = {'skills_dir': str(tmp_path), 'model': 'offline', 'judge_enabled': True}
    before = build_fingerprints(dataset, {**common, 'structured_thinking': 'provider_default'})
    after = build_fingerprints(dataset, {**common, 'structured_thinking': 'disabled'})
    assert before['judge'] != after['judge']
    assert before['retrieval'] != after['retrieval']
    assert before['dataset'] == after['dataset']
