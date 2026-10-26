import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import pytest


class Redis:
    def __init__(self):
        self.values = {}
        self.closed = False

    async def lpush(self, key, value):
        self.values.setdefault(key, []).insert(0, value)

    async def expire(self, key, ttl):
        pass

    async def llen(self, key):
        return len(self.values.get(key, []))

    async def lrange(self, key, start, end):
        return self.values.get(key, [])[start:end + 1]

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def scan_iter(self, match):
        for key in list(self.values):
            if key.startswith(match[:-1]):
                yield key

    async def aclose(self):
        self.closed = True


class Collection:
    def __init__(self):
        self.rows = {}

    def count(self):
        return len(self.rows)

    def add(self, ids, documents, metadatas):
        self.rows.update({key: (doc, meta) for key, doc, meta in zip(ids, documents, metadatas)})


class Chroma:
    def __init__(self):
        self.collections = {}
        self.deleted = []

    def get_or_create_collection(self, name, **kwargs):
        return self.collections.setdefault(name, Collection())

    def delete_collection(self, name):
        self.deleted.append(name)
        self.collections.pop(name, None)


def storage_types(monkeypatch):
    # The injected clients are the feature under test; no Chroma installation
    # or network connection is necessary to verify ownership boundaries.
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace())
    from memory.conversation_memory import MemoryManager
    from mcp.knowledge_base import KnowledgeBase
    return MemoryManager, KnowledgeBase


def test_memory_namespaces_are_isolated_and_default_names_are_compatible(monkeypatch):
    async def run():
        MemoryManager, _ = storage_types(monkeypatch)
        redis, chroma = Redis(), Chroma()
        first = MemoryManager(api_key='test', namespace='eval_' + 'a' * 32,
                              redis_client=redis, chroma_client=chroma)
        second = MemoryManager(api_key='test', namespace='eval_' + 'b' * 32,
                               redis_client=redis, chroma_client=chroma)
        default = MemoryManager(api_key='test', redis_client=redis, chroma_client=chroma)
        from memory.roles import MsgRole
        await first.add_message('same-user', 'same-conversation', MsgRole.USER, 'first')
        await second.add_message('same-user', 'same-conversation', MsgRole.USER, 'second')
        assert (await first._get_working_memory('same-user', 'same-conversation'))[0].content == 'first'
        assert (await second._get_working_memory('same-user', 'same-conversation'))[0].content == 'second'
        assert default._wm_key('u', 'c') == 'wm:u:c'
        assert set(chroma.collections) >= {'episodic', 'user_profile', 'eval_' + 'a' * 32 + '_episodic'}
        await first.cleanup()
        assert not any(key.startswith(first.namespace + ':') for key in redis.values)
        assert second._wm_key('same-user', 'same-conversation') in redis.values
        assert 'episodic' in chroma.collections
        with pytest.raises(ValueError, match='evaluation'):
            await default.cleanup()
        await first.close()
        await second.close()
        await default.close()
    asyncio.run(run())


def test_partial_memory_constructor_removes_only_acquired_evaluation_collections(monkeypatch):
    MemoryManager, _ = storage_types(monkeypatch)
    namespace = 'eval_' + 'e' * 32
    allocated = []
    monkeypatch.setattr('memory.conversation_memory.AsyncAnthropic', lambda **kwargs: allocated.append('provider'))
    monkeypatch.setattr('memory.conversation_memory.redis.from_url', lambda *args, **kwargs: allocated.append('redis'))

    class PartialChroma(Chroma):
        def get_or_create_collection(self, name, **kwargs):
            if name == namespace + '_user_profile':
                raise ValueError('profile collection unavailable')
            return super().get_or_create_collection(name, **kwargs)

    chroma = PartialChroma()
    chroma.get_or_create_collection('episodic')
    chroma.get_or_create_collection('user_profile')
    with pytest.raises(ValueError):
        MemoryManager(api_key='test', namespace=namespace, chroma_client=chroma)
    assert chroma.deleted == [namespace + '_episodic']
    assert set(chroma.collections) == {'episodic', 'user_profile'}
    assert allocated == []


def test_runtime_close_continues_after_service_memory_and_client_failures():
    async def run():
        from services.runtime import Runtime
        events = []

        class Client:
            def __init__(self, name):
                self.name = name
            async def close(self):
                events.append(self.name)
                if self.name == 'manager':
                    raise OSError('client close failed')

        class Memory:
            async def cleanup(self):
                events.append('memory.cleanup')
                raise OSError('memory cleanup failed')
            async def close(self):
                events.append('memory.close')
                raise OSError('memory close failed')

        class Service:
            async def close(self):
                events.append('service.close')
                raise OSError('service close failed')

        knowledge = SimpleNamespace(cleanup=lambda: events.append('knowledge.cleanup'))
        orchestrator = SimpleNamespace(_composer=SimpleNamespace(_client=Client('composer')),
                                       _intent_recognizer=SimpleNamespace(client=Client('recognizer')))
        runtime = Runtime('eval_' + 'f' * 32, Service(), orchestrator, Memory(), knowledge,
                          SimpleNamespace(_client=Client('manager')), None)
        with pytest.raises(RuntimeError) as caught:
            await runtime.close(cleanup=True)
        assert set(events) == {'service.close', 'memory.cleanup', 'knowledge.cleanup', 'memory.close',
                               'manager', 'composer', 'recognizer'}
        assert len(caught.value.errors) == 4
    asyncio.run(run())


@pytest.mark.parametrize('cancel_stage', ['memory', 'knowledge_import'])
def test_runtime_cancel_waits_for_thread_acquisition_then_rolls_back(cancel_stage):
    async def run():
        from services.runtime import create_runtime
        entered, release = threading.Event(), threading.Event()
        events = []
        namespace = 'eval_' + 'a1' * 16

        def pause():
            entered.set()
            assert release.wait(5)

        class Memory:
            def __init__(self, **kwargs):
                if cancel_stage == 'memory':
                    pause()
            async def cleanup(self):
                events.append('memory.cleanup')
            async def close(self):
                events.append('memory.close')

        class Knowledge:
            def __init__(self, **kwargs):
                pass
            def add_documents(self, documents):
                pause()
            def cleanup(self):
                events.append('knowledge.cleanup')

        class Client:
            async def close(self):
                events.append('provider.close')

        class Orchestrator:
            def __init__(self, **kwargs):
                self._intent_recognizer = SimpleNamespace(client=Client())
            def set_shared_tools(self, tools):
                pass

        task = asyncio.create_task(create_runtime({'api_key': 'test'}, namespace=namespace,
                                                  knowledge_documents=[], memory_factory=Memory,
                                                  knowledge_factory=Knowledge, orchestrator_factory=Orchestrator))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.cleanup_status == 'rolled_back'
            assert {'memory.cleanup', 'memory.close', 'provider.close'} <= set(events)
            assert ('knowledge.cleanup' in events) is (cancel_stage == 'knowledge_import')
        finally:
            release.set()
    asyncio.run(run())


def test_knowledge_eval_collection_does_not_seed_or_delete_production(monkeypatch):
    _, KnowledgeBase = storage_types(monkeypatch)
    chroma = Chroma()
    name = 'eval_' + 'a' * 32 + '_knowledge_base'
    kb = KnowledgeBase(chroma_client=chroma, collection_name=name, seed_default=False)
    assert kb._collection.count() == 0
    kb.add_documents([{'title': 'fixture', 'content': 'test policy'}])
    assert kb._collection.count() == 1
    kb.cleanup()
    assert chroma.deleted == [name]
    production = KnowledgeBase(chroma_client=chroma, seed_default=False)
    with pytest.raises(ValueError, match='evaluation'):
        production.cleanup()


def test_runtime_factory_isolates_components_and_closes_successfully(monkeypatch):
    async def run():
        from services.runtime import create_runtime
        instances = []

        class Memory:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.events = []
                instances.append(self)
            async def close(self):
                self.events.append('close')
            async def cleanup(self):
                self.events.append('cleanup')

        class Knowledge:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.docs = []
            def add_documents(self, docs):
                self.docs.extend(docs)
            async def search_handler(self, params, context):
                return []
            def cleanup(self):
                self.cleaned = True

        class Orchestrator:
            def __init__(self, **kwargs):
                self._intent_recognizer = SimpleNamespace()
            def set_shared_tools(self, tools):
                self.tools = tools

        config = {'api_key': 'test', 'model': 'test-model'}
        kwargs = {'memory_factory': Memory, 'knowledge_factory': Knowledge, 'orchestrator_factory': Orchestrator}
        first = await create_runtime(config, namespace='eval_' + 'a' * 32,
                               knowledge_documents=[{'title': 'snapshot', 'content': 'policy'}], **kwargs)
        second = await create_runtime(config, namespace='eval_' + 'b' * 32, knowledge_documents=[], **kwargs)
        assert first.orchestrator is not second.orchestrator
        assert first.tool_manager is not second.tool_manager
        assert first.knowledge.kwargs['seed_default'] is False
        assert first.memory.kwargs['namespace'] != second.memory.kwargs['namespace']
        assert first.memory.kwargs['chroma_mode'] == 'http'
        assert first.knowledge.kwargs['chroma_mode'] == 'http'
        local = await create_runtime({**config, 'chroma_mode': 'local'}, namespace='eval_' + 'd' * 32,
                                     knowledge_documents=[], **kwargs)
        assert local.memory.kwargs['chroma_mode'] == 'local'
        await local.close(cleanup=True)
        await first.close(cleanup=True)
        await second.close(cleanup=True)
        assert first.memory.events == ['cleanup', 'close']
        assert first.knowledge.cleaned
        with pytest.raises(ValueError, match='namespace'):
            await create_runtime(config, namespace='../production', **kwargs)
    asyncio.run(run())


@pytest.mark.parametrize('failure_stage', ['knowledge_import', 'knowledge_creation', 'knowledge_constructor', 'tool_policy',
                                          'partial_memory', 'cleanup_failure'])
def test_runtime_initialization_failure_rolls_back_only_owned_namespace(monkeypatch, failure_stage):
    async def run():
        from services.runtime import create_runtime
        from mcp.tool_manager import MCPToolManager
        MemoryManager, KnowledgeBase = storage_types(monkeypatch)
        namespace = 'eval_' + 'c' * 32
        class NotFoundError(Exception):
            pass

        class FailingChroma(Chroma):
            def get_or_create_collection(self, name, **kwargs):
                if failure_stage == 'partial_memory' and name == namespace + '_user_profile':
                    raise ValueError('profile collection failed')
                if failure_stage == 'knowledge_creation' and name == namespace + '_knowledge_base':
                    raise ValueError('knowledge collection was not created')
                return super().get_or_create_collection(name, **kwargs)
            def delete_collection(self, name):
                if failure_stage == 'cleanup_failure' and name == namespace + '_episodic':
                    raise OSError('cleanup unavailable')
                if failure_stage == 'knowledge_creation' and name == namespace + '_knowledge_base':
                    raise NotFoundError('collection does not exist')
                return super().delete_collection(name)
        redis, chroma = Redis(), FailingChroma()
        for name in ('episodic', 'user_profile', 'knowledge_base', 'eval_' + 'd' * 32 + '_episodic'):
            chroma.get_or_create_collection(name)
        redis.values['wm:production:c'] = ['keep']
        redis.values[namespace + ':wm:owned'] = ['remove']
        clients = []

        class Client:
            closed = False
            async def close(self):
                self.closed = True

        def new_client():
            result = Client()
            clients.append(result)
            return result

        class Memory(MemoryManager):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, redis_client=redis, chroma_client=chroma, client=new_client())

        class Knowledge(KnowledgeBase):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, chroma_client=chroma)
                if failure_stage == 'knowledge_constructor':
                    raise ValueError('collection constructed before failure')
            def add_documents(self, documents):
                super().add_documents(documents)
                if failure_stage == 'knowledge_import':
                    raise ValueError('secret document content must not be reported')

        class Orchestrator:
            def __init__(self, **kwargs):
                self._composer = SimpleNamespace(_client=new_client())
                self._intent_recognizer = SimpleNamespace(client=new_client())
            def set_shared_tools(self, tools):
                pass

        with pytest.raises(RuntimeError) as caught:
            await create_runtime({'api_key': 'test', 'rewrite_policy': 'invalid'}, namespace=namespace,
                                 knowledge_documents=[{'title': 'policy', 'content': 'fixture'}],
                                 memory_factory=Memory, knowledge_factory=Knowledge,
                                 orchestrator_factory=Orchestrator, tool_manager_factory=MCPToolManager)
        assert caught.value.cleanup_status == ('rollback_failed' if failure_stage == 'cleanup_failure' else 'rolled_back')
        assert caught.value.error_type == 'ValueError'
        assert caught.value.cleanup_errors == (('OSError',) if failure_stage == 'cleanup_failure' else ())
        assert 'secret' not in str(caught.value)
        deleted = {namespace + '_episodic'} if failure_stage == 'partial_memory' else {
            namespace + '_episodic', namespace + '_user_profile', namespace + '_knowledge_base'}
        remaining = {'episodic', 'user_profile', 'knowledge_base', 'eval_' + 'd' * 32 + '_episodic'}
        if failure_stage == 'cleanup_failure':
            deleted.remove(namespace + '_episodic')
            remaining.add(namespace + '_episodic')
        elif failure_stage == 'knowledge_creation':
            deleted.remove(namespace + '_knowledge_base')
        assert set(chroma.deleted) == deleted
        assert set(chroma.collections) == remaining
        assert redis.values == {'wm:production:c': ['keep']}
        assert redis.closed and all(client.closed for client in clients)
    asyncio.run(run())


def test_runtime_cancellation_during_rollback_preserves_cancel_and_finishes_cleanup():
    async def run():
        from services.runtime import create_runtime
        entered, release = asyncio.Event(), asyncio.Event()
        events = []

        class Memory:
            def __init__(self, **kwargs):
                pass
            async def cleanup(self):
                entered.set()
                await release.wait()
                events.append('cleanup')
            async def close(self):
                events.append('close')

        class Knowledge:
            def __init__(self, **kwargs):
                raise ValueError('construction failed')
            def cleanup(self):
                events.append('knowledge.cleanup')

        class Orchestrator:
            def __init__(self, **kwargs):
                pass

        task = asyncio.create_task(create_runtime({'api_key': 'test'}, namespace='eval_' + '12' * 16,
                                                  memory_factory=Memory, knowledge_factory=Knowledge,
                                                  orchestrator_factory=Orchestrator))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.cleanup_status == 'rolled_back'
        assert set(events) == {'cleanup', 'close', 'knowledge.cleanup'}
    asyncio.run(run())


def test_runtime_rolls_back_orchestrator_client_when_recognizer_construction_fails(monkeypatch):
    async def run():
        import agents.agent_orchestrator as agents
        from services.runtime import RuntimeInitializationError, create_runtime
        clients = []

        class Client:
            closed = False
            async def close(self):
                self.closed = True

        def new_client(**kwargs):
            client = Client()
            clients.append(client)
            return client

        def fail_recognizer(**kwargs):
            raise ValueError('recognizer initialization failed')

        def no_storage(**kwargs):
            pytest.fail('storage construction should not start')

        monkeypatch.setattr(agents, 'AsyncAnthropic', new_client)
        monkeypatch.setattr(agents, 'IntentRecognizer', fail_recognizer)
        with pytest.raises(RuntimeInitializationError) as caught:
            await create_runtime({'api_key': 'test'}, namespace='eval_' + '34' * 16,
                                 memory_factory=no_storage, knowledge_factory=no_storage)
        assert caught.value.cleanup_status == 'rolled_back'
        assert caught.value.error_type == 'ValueError'
        assert clients and all(client.closed for client in clients)
    asyncio.run(run())
