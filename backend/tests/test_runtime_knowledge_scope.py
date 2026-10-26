"""Runtime-owned knowledge scopes exercised through the real knowledge handler."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.test_context_storage import Chroma


@pytest.mark.parametrize('namespace,expected', [
    ('', {'public-policy'}),
    ('eval_' + 'ad' * 16, {'public-policy', 'evaluation-policy'}),
])
@pytest.mark.parametrize('requested_scope', [None, 'evaluation', 'internal'])
def test_runtime_knowledge_scope_is_owned_and_cannot_be_elevated(namespace, expected, requested_scope):
    async def run():
        from mcp.knowledge_base import KnowledgeBase
        from mcp.tool_manager import MCPToolManager
        from services.runtime import create_runtime

        class Memory:
            def __init__(self, **kwargs): pass
            async def cleanup(self): pass
            async def close(self): pass

        class Orchestrator:
            def __init__(self, **kwargs): self._pool = {}
            def set_shared_tools(self, tools): self.tools = tools

        class ForbiddenClient:
            @property
            def messages(self): return self
            async def create(self, **kwargs):
                raise AssertionError('Scope tests must not call a provider')
            async def close(self): pass

        def knowledge_factory(**kwargs):
            return KnowledgeBase(**{**kwargs, 'seed_default': False, 'chroma_client': Chroma()})

        def manager_factory(**kwargs):
            return MCPToolManager(**{**kwargs, 'client': ForbiddenClient(),
                                     'rewrite_policy': 'never', 'rerank_policy': 'never'})

        docs = [{'id': scope + '-policy', 'title': scope, 'content': 'synthetic ' + scope,
                 'scope': scope, 'version': '1'} for scope in ('public', 'evaluation', 'internal')]
        runtime = await create_runtime({'api_key': 'test'}, namespace=namespace,
                                       knowledge_documents=docs, memory_factory=Memory,
                                       knowledge_factory=knowledge_factory, orchestrator_factory=Orchestrator,
                                       tool_manager_factory=manager_factory)
        try:
            params = {'query': 'synthetic', 'top_k': 10}
            if requested_scope is not None:
                params['scope'] = requested_scope
            before = dict(params)
            tool = runtime.tool_manager._tools['knowledge_search']
            # Call the registered handler directly so even params bypassing the
            # public JSON schema cannot widen the runtime's permitted scope.
            result = await tool.handler(params, {'scope': 'internal'})
            assert {item['context_ref']['id'] for item in result} == expected
            assert params == before
            # The normal shared tool path must use the same trusted handler.
            shared = runtime.orchestrator.tools['search_knowledge_base']
            answer = await shared.handler(SimpleNamespace(message='synthetic'), {'query': 'synthetic', 'top_k': 10})
            assert answer['success'] is True
            assert {item['context_ref']['id'] for item in answer['results']} == expected
        finally:
            await runtime.close(cleanup=bool(namespace))
    asyncio.run(run())
