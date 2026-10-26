import asyncio
from contextlib import contextmanager

import pytest

from mcp.tool_manager import MCPToolManager, Tool
from tests.test_context_storage import Client


def manager(responses=(), **kwargs):
    client = Client(responses)
    instance = MCPToolManager('test', client=client, **kwargs)
    return instance, client


def register(instance, rows):
    async def search(params, context):
        return [{**row, 'score': 0.1 if params['query'] == '退款流程' else 0.9} for row in rows]
    instance.register(Tool('search', 'test search', search, {}, supports_rerank=True))


def test_never_policy_skips_model_and_snapshot_is_fixed():
    async def run():
        instance, client = manager(rewrite_policy='never', rerank_policy='never')
        register(instance, [{'evidence_id': 'one', 'content': 'policy'}])
        before = instance.retrieval_snapshot()
        result = await instance.search_with_rewrite('search', '退款流程', 1)
        assert result.success and result.data[0]['evidence_id'] == 'one'
        assert result.reranked is False and client.calls == 0
        assert instance.retrieval_snapshot() == before
        assert before['rewrite_policy'] == 'never' and before['rerank_policy'] == 'never'
        assert instance.retrieval_stats()['rewrite_calls'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('query', ['你好', '订单号: ORD-12345 状态查询'])
def test_adaptive_skips_greeting_and_structured_rewrite_and_small_rerank(query):
    async def run():
        instance, client = manager(rewrite_policy='adaptive', rerank_policy='adaptive')
        register(instance, [{'evidence_id': 'one'}])
        result = await instance.search_with_rewrite('search', query, 1)
        assert result.success and result.reranked is False
        assert client.calls == 0
    asyncio.run(run())


def test_always_rewrites_dedupes_by_evidence_and_records_real_rerank():
    async def run():
        instance, client = manager([['退货政策', '退款流程'], [1, 0]])
        register(instance, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        result = await instance.search_with_rewrite('search', '退款流程', 1)
        assert result.success and result.reranked is True
        assert result.data == [{'evidence_id': 'two', 'score': 0.1}]
        assert client.calls == 2
        stats = instance.retrieval_stats()
        assert stats['rewrite_calls'] == 1 and stats['rerank_calls'] == 1
        assert stats['candidate_count'] == 2
    asyncio.run(run())


@pytest.mark.parametrize('response', [TimeoutError('offline'), [99], [0, 0], ['0']])
def test_failed_or_invalid_rerank_returns_original_order_with_false_marker(response):
    async def run():
        instance, client = manager([response], rewrite_policy='never')
        register(instance, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        result = await instance.search_with_rewrite('search', '退款流程', 1)
        assert result.success and result.reranked is False
        assert result.data[0]['evidence_id'] == 'one'
        assert instance.retrieval_stats()['rerank_fallbacks'] == 1
    asyncio.run(run())


def test_direct_tool_call_respects_never_and_real_fallback_marker():
    async def run():
        disabled, _ = manager(rerank_policy='never')
        register(disabled, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        result = await disabled.call('search', {'query': '退款流程'}, rerank_top_k=1)
        assert result.success and result.reranked is False and len(result.data) == 1
        failed, _ = manager([TimeoutError('offline')])
        register(failed, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        result = await failed.call('search', {'query': '退款流程'}, rerank_top_k=1)
        assert result.success and result.reranked is False
    asyncio.run(run())


def test_empty_recall_and_tool_timeout_do_not_trigger_rerank():
    async def run():
        instance, client = manager(rewrite_policy='never')
        register(instance, [])
        empty = await instance.search_with_rewrite('search', '退款流程')
        assert empty.success is False and empty.data == []
        async def slow(params, context):
            await asyncio.sleep(1)
        instance.register(Tool('slow', 'timeout', slow, {}, timeout_s=0.001))
        timed_out = await instance.search_with_rewrite('slow', '退款流程')
        assert timed_out.success is False and client.calls == 0
    asyncio.run(run())


def test_retrieval_and_rerank_use_bound_telemetry():
    from observability.telemetry import bind_telemetry
    class Telemetry:
        def __init__(self):
            self.names = []
        @contextmanager
        def span(self, name, **kwargs):
            self.names.append(name)
            yield self
        def update(self, **fields):
            pass
    async def run():
        adapter = Telemetry()
        instance, _ = manager([[1, 0]], rewrite_policy='never')
        register(instance, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        with bind_telemetry(adapter):
            await instance.search_with_rewrite('search', '退款流程', 1)
        assert 'retrieval' in adapter.names and 'retrieval.rerank' in adapter.names
    asyncio.run(run())


def test_invalid_policy_is_rejected_before_any_model_call():
    with pytest.raises(ValueError, match='policy'):
        manager(rewrite_policy='sometimes')


def test_adaptive_rewrites_complex_query_and_reranks_many_candidates():
    async def run():
        instance, client = manager([['退货政策'], [5, 4, 3, 2, 1, 0]],
                                   rewrite_policy='adaptive', rerank_policy='adaptive')
        register(instance, [{'evidence_id': str(i)} for i in range(6)])
        result = await instance.search_with_rewrite('search', '拆开后发现损坏，如何退货退款', 1)
        assert result.reranked is True and result.data[0]['evidence_id'] == '5'
        assert client.calls == 2
    asyncio.run(run())


def test_model_timeout_is_bounded_and_cancels_rewrite_and_rerank():
    class SlowClient:
        def __init__(self):
            self.messages = self
            self.cancelled = 0
        async def create(self, **kwargs):
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled += 1
    async def run():
        client = SlowClient()
        instance = MCPToolManager('test', client=client, retrieval_timeout_s=0.001)
        register(instance, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        result = await instance.search_with_rewrite('search', '退款流程', 1)
        assert result.success and result.reranked is False
        assert client.cancelled == 2
        assert instance.retrieval_stats()['rewrite_fallbacks'] == 1
        assert instance.retrieval_stats()['rerank_fallbacks'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('response, expected', [([1, 0], True), (TimeoutError('offline'), False)])
def test_cache_preserves_actual_rerank_status(response, expected):
    async def run():
        instance, client = manager([response])
        register(instance, [{'evidence_id': 'one'}, {'evidence_id': 'two'}])
        instance._tools['search'].cache_ttl = 30
        first = await instance.call('search', {'query': '退款流程'}, rerank_top_k=1)
        cached = await instance.call('search', {'query': '退款流程'}, rerank_top_k=1)
        assert first.reranked is expected and cached.reranked is expected
        assert cached.cached is True and cached.data == first.data and client.calls == 1
    asyncio.run(run())
