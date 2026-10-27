"""Budget interruptions cross the actual MCP retrieval and Agent boundaries."""
import asyncio

import pytest

from mcp.tool_manager import MCPToolManager, Tool
from observability.budgets import EvaluationBudgetExceeded
from tests.test_context_storage import Client
from tests.test_required_knowledge import request, setup


ROWS = [{'evidence_id': str(index), 'content': 'offline policy'} for index in range(6)]


@pytest.mark.parametrize('stage', ['rewrite', 'rerank', 'recall'])
def test_real_manager_budget_interruption_stops_preflight_before_answer(stage):
    error = EvaluationBudgetExceeded('model_calls')
    retrieval_client = Client([error])
    manager = MCPToolManager('offline', client=retrieval_client,
                             rewrite_policy='always' if stage == 'rewrite' else 'never')
    recalled = []

    async def search(params, context):
        recalled.append(params)
        if stage == 'recall':
            raise error
        return ROWS

    manager.register(Tool('knowledge_search', 'offline', search, {}))
    agent, answer_client, _ = setup(manager=manager)
    with pytest.raises(EvaluationBudgetExceeded) as raised:
        asyncio.run(agent.handle(request()))
    assert raised.value is error
    assert answer_client.calls == []
    assert recalled == ([] if stage == 'rewrite' else [{'query': request().message, 'top_k': 5}])
    stats = manager.retrieval_stats()
    assert stats['rewrite_fallbacks'] == stats['rerank_fallbacks'] == 0


@pytest.mark.parametrize('stage', ['handler', 'rerank'])
def test_direct_call_budget_interruption_bypasses_fallback_and_error_accounting(stage):
    async def run():
        error = EvaluationBudgetExceeded('deadline')
        manager = MCPToolManager('offline', client=Client([error]))
        fallbacks = []

        async def search(params, context):
            if stage == 'handler':
                raise error
            return ROWS

        def fallback(params, context, reason):
            fallbacks.append(reason)
            return ROWS

        tool = Tool('search', 'offline', search, {}, supports_rerank=True,
                    cache_ttl=30, fallback=fallback)
        manager.register(tool)
        with pytest.raises(EvaluationBudgetExceeded) as raised:
            await manager.call('search', {'query': 'policy'}, rerank_top_k=5)
        assert raised.value is error
        assert fallbacks == []
        assert manager._cache == {}
        assert tool.stats.failed == 0 and tool.breaker.fail_count == 0

    asyncio.run(run())


@pytest.mark.parametrize('async_fallback', [False, True])
@pytest.mark.parametrize('circuit_open', [False, True])
def test_fallback_budget_interruption_is_preserved(async_fallback, circuit_open):
    async def run():
        error = EvaluationBudgetExceeded('model_calls')
        manager = MCPToolManager('offline', client=Client())

        async def unavailable(params, context):
            raise RuntimeError('storage unavailable')

        def sync_fallback(params, context, reason):
            raise error

        async def awaitable_fallback(params, context, reason):
            raise error

        tool = Tool('search', 'offline', unavailable, {},
                    fallback=awaitable_fallback if async_fallback else sync_fallback)
        manager.register(tool)
        if circuit_open:
            for _ in range(tool.breaker.threshold):
                tool.breaker.record_failure()
        with pytest.raises(EvaluationBudgetExceeded) as raised:
            await manager.call('search', {'query': 'policy'})
        assert raised.value is error

    asyncio.run(run())


@pytest.mark.parametrize('error_type', [EvaluationBudgetExceeded, asyncio.CancelledError])
def test_parallel_recall_preserves_interruption_even_when_another_query_has_results(error_type):
    async def run():
        error = error_type('deadline')
        manager = MCPToolManager('offline', client=Client([['expanded policy']]), rerank_policy='never')

        async def search(params, context):
            if params['query'] == 'expanded policy':
                raise error
            return ROWS

        manager.register(Tool('knowledge_search', 'offline', search, {}))
        agent, answer_client, _ = setup(manager=manager)
        with pytest.raises(error_type) as raised:
            await agent.handle(request())
        if error_type is EvaluationBudgetExceeded:
            assert raised.value is error
        assert answer_client.calls == []

    asyncio.run(run())
