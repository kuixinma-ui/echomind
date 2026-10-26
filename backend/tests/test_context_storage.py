import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.test_runtime import Redis as BaseRedis, Chroma as BaseChroma


class Redis(BaseRedis):
    async def setex(self, key, ttl, value):
        self.values[key] = value


class Collection:
    def __init__(self):
        self.rows = {}
        self.deletes = 0

    def count(self):
        return len(self.rows)

    def add(self, ids, documents, metadatas):
        self.rows.update({key: (doc, meta) for key, doc, meta in zip(ids, documents, metadatas)})

    upsert = add

    def delete(self, **kwargs):
        self.deletes += 1
        for key in kwargs.get('ids', []):
            self.rows.pop(key, None)

    def get(self, ids=None, where=None):
        rows = [(key, doc, meta) for key, (doc, meta) in self.rows.items()
                if (ids is None or key in ids)
                and (where is None or all(meta.get(k) == v for k, v in where.items()))]
        return {'ids': [r[0] for r in rows], 'documents': [r[1] for r in rows],
                'metadatas': [r[2] for r in rows]}

    def query(self, query_texts, n_results, **kwargs):
        # Chroma rejects a where object with two operators at the same level.
        where = kwargs.get('where')
        if where and len(where) > 1:
            raise ValueError('Chroma expects exactly one where operator')
        def matches(metadata, expression):
            if not expression:
                return True
            if '$and' in expression:
                return all(matches(metadata, child) for child in expression['$and'])
            return all(metadata.get(k) == v for k, v in expression.items())
        rows = [(key, row) for key, row in self.rows.items() if matches(row[1], where)][:n_results]
        return {'ids': [[r[0] for r in rows]], 'documents': [[r[1][0] for r in rows]],
                'metadatas': [[r[1][1] for r in rows]],
                'distances': [[0.1 + i * 0.01 for i in range(len(rows))]]}


class Chroma(BaseChroma):
    def get_or_create_collection(self, name, **kwargs):
        return self.collections.setdefault(name, Collection())


class Client:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.messages = self
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))])

    async def close(self):
        pass


def knowledge():
    from mcp.knowledge_base import KnowledgeBase
    return KnowledgeBase(chroma_client=Chroma(), seed_default=False)


def test_context_ref_roundtrip_preserves_source_and_version():
    from context import ContextRef
    ref = ContextRef(id='refund', type='knowledge', version='2', source='policy.md',
                     scope='public', effective_at='2026-02-01T00:00:00Z')
    assert ref.to_dict() == {'id': 'refund', 'type': 'knowledge', 'version': '2',
                             'source': 'policy.md', 'scope': 'public',
                             'effective_at': '2026-02-01T00:00:00Z'}


def test_knowledge_versions_are_addressable_and_latest_effective_is_selected():
    kb = knowledge()
    kb.add_documents([
        {'id': 'refund', 'title': '政策', 'content': '旧版七天', 'version': '1',
         'source': 'policy-v1.md', 'effective_at': '2026-01-01T00:00:00Z'},
        {'id': 'refund', 'title': '政策', 'content': '新版三十天', 'version': '2',
         'source': 'policy-v2.md', 'effective_at': '2026-02-01T00:00:00Z'},
        {'id': 'refund', 'title': '政策', 'content': '未来九十天', 'version': '3',
         'source': 'policy-v3.md', 'effective_at': '2027-01-01T00:00:00Z'},
    ])
    current = kb.search('退款', top_k=1, as_of='2026-03-01T00:00:00Z')
    assert current[0]['content'] == '新版三十天'
    assert current[0]['context_ref']['id'] == 'refund'
    assert current[0]['source'] == 'policy-v2.md'
    old = kb.search('退款', versions={'refund': '1'}, as_of='2026-03-01T00:00:00Z')
    assert old[0]['content'] == '旧版七天'
    assert old[0]['evidence_id'] != current[0]['evidence_id']
    assert len(kb._collection.rows) == 3


def test_knowledge_scope_filter_and_legacy_documents_remain_compatible():
    kb = knowledge()
    kb.add_documents([
        {'title': '公共', 'content': '公共政策'},
        {'id': 'staff', 'title': '内部', 'content': '员工政策', 'scope': 'staff'},
    ])
    assert [r['content'] for r in kb.search('政策')] == ['公共政策']
    scoped = kb.search('政策', scope='staff')
    assert {r['content'] for r in scoped} == {'公共政策', '员工政策'}
    assert all(r['evidence_id'] and r['context_ref']['version'] for r in scoped)


def test_profile_corrections_record_diff_and_user_isolation_without_delete_window():
    async def run():
        from memory.conversation_memory import MemoryManager
        from memory.roles import MsgRole
        redis, chroma = Redis(), Chroma()
        manager = MemoryManager(api_key='test', namespace='eval_' + 'a' * 32,
                                redis_client=redis, chroma_client=chroma)
        manager._client = Client([
            {'city': '上海', 'obsolete': True}, {'city': '北京', 'preference': '短信'},
        ])
        await manager.add_message('alice', 'c1', MsgRole.USER, '我在上海')
        await manager.update_profile('alice', 'c1')
        await manager.add_message('alice', 'c2', MsgRole.USER, '更正为北京，删除旧标记，偏好短信')
        await manager.update_profile('alice', 'c2')
        assert await manager._get_profile('alice') == {'city': '北京', 'preference': '短信'}
        audit = await manager.get_profile_audit('alice')
        assert len(audit) == 2
        assert audit[1]['added'] == {'preference': '短信'}
        assert audit[1]['updated'] == {'city': {'before': '上海', 'after': '北京'}}
        assert audit[1]['deleted'] == {'obsolete': True}
        assert audit[1]['source_conv'] == 'c2'
        assert audit[1]['version'] == '2'
        assert audit[1]['inferred'] is True
        assert await manager.get_profile_audit('bob') == []
        assert manager._profile.deletes == 0
        await manager.cleanup()
        assert redis.values == {}
        await manager.close()
    asyncio.run(run())


def test_profile_update_returns_explicit_failure_instead_of_silent_success():
    async def run():
        from memory.conversation_memory import MemoryManager
        from memory.roles import MsgRole
        manager = MemoryManager(api_key='test', redis_client=Redis(), chroma_client=Chroma())
        manager._client = Client([TimeoutError('offline')])
        await manager.add_message('alice', 'c1', MsgRole.USER, '偏好短信')
        assert await manager.update_profile('alice', 'c1') is False
        assert await manager.update_profile('alice', 'empty') is True
        await manager.close()
    asyncio.run(run())


def test_profile_accepts_injected_model_client_and_prompt_labels_inference():
    async def run():
        from memory.conversation_memory import MemoryManager
        from memory.roles import MsgRole
        manager = MemoryManager(api_key='test', redis_client=Redis(), chroma_client=Chroma(),
                                client=Client([{'city': '北京'}]))
        await manager.add_message('alice', 'c1', MsgRole.USER, '我在北京')
        assert await manager.update_profile('alice', 'c1') is True
        context = await manager.get_context('alice', 'c1')
        assert '模型推断' in context.to_prompt_text()
        await manager.close()
    asyncio.run(run())


def test_episodic_filter_uses_valid_chroma_conjunction_and_stays_within_user():
    async def run():
        from memory.conversation_memory import MemoryManager
        manager = MemoryManager(api_key='test', redis_client=Redis(), chroma_client=Chroma(), client=Client())
        manager._episodic.add(ids=['alice', 'bob'], documents=['Alice previous preference', 'Bob private detail'],
                              metadatas=[{'user_id': 'alice', 'conv_id': 'c1'}, {'user_id': 'bob', 'conv_id': 'c2'}])
        history = await manager._search_episodic('alice', 'c1', 'preference')
        assert history == ['Alice previous preference']
        await manager.close()
    asyncio.run(run())


def test_compression_preserves_chronological_order_of_retained_messages():
    async def run():
        from memory.conversation_memory import MemoryManager
        from memory.roles import MsgRole
        manager = MemoryManager(api_key='test', redis_client=Redis(), chroma_client=Chroma(), client=Client(['summary']))
        for index in range(15):
            await manager.add_message('alice', 'c1', MsgRole.USER, f'message-{index}')
        retained = await manager._get_working_memory('alice', 'c1')
        assert [message.content for message in retained] == ['message-10', 'message-11', 'message-12', 'message-13', 'message-14']
        await manager.close()
    asyncio.run(run())


def test_profile_seed_requires_unique_eval_namespace_and_audits_provenance():
    async def run():
        from memory.conversation_memory import MemoryManager
        shared_redis, chroma = Redis(), Chroma()
        production = MemoryManager(api_key='test', redis_client=shared_redis, chroma_client=chroma)
        with pytest.raises(ValueError, match='evaluation'):
            await production.seed_profile('alice', {'city': '北京'})
        first = MemoryManager(api_key='test', namespace='eval_' + 'a' * 32,
                              redis_client=shared_redis, chroma_client=chroma)
        second = MemoryManager(api_key='test', namespace='eval_' + 'b' * 32,
                               redis_client=shared_redis, chroma_client=chroma)
        audit = await first.seed_profile('alice', {'city': '北京'})
        assert audit['experiment_seed'] is True
        assert audit['inferred'] is False
        assert audit['source_conv'] == 'scenario_initial_state'
        assert await second._get_profile('alice') == {}
        await second.seed_profile('alice', {'city': '上海'})
        await first.cleanup()
        assert len(await second.get_profile_audit('alice')) == 1
        assert await second._get_profile('alice') == {'city': '上海'}
        await production.close()
        await first.close()
        await second.close()
    asyncio.run(run())
