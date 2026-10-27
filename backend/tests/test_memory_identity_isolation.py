"""Distinct ordinary identities must not share Redis messages or summaries."""
import asyncio
import json

import pytest

from memory.conversation_memory import MemoryManager
from memory.roles import MsgRole
from services.storage import memory_key
from tests.test_context_storage import Chroma, Client, Redis


def manager():
    return MemoryManager(redis_client=Redis(), chroma_client=Chroma(), client=Client([]))


@pytest.mark.parametrize('first,second', [
    (('alice:orders', 'same'), ('alice', 'orders:same')),
    (('alice:orders:paid', 'same'), ('alice:orders', 'paid:same')),
    (('alice:orders', ''), ('alice', 'orders:')),
    (('', 'alice:orders:'), (':alice:orders', '')),
])
def test_ordinary_working_messages_do_not_cross_identity_boundaries(first, second):
    async def run():
        memory = manager()
        try:
            await memory.add_message(*first, MsgRole.USER, 'FIRST_PRIVATE_MESSAGE')
            await memory.add_message(*second, MsgRole.USER, 'SECOND_PRIVATE_MESSAGE')
            assert [m.content for m in await memory._get_working_memory(*first)] == ['FIRST_PRIVATE_MESSAGE']
            assert [m.content for m in await memory._get_working_memory(*second)] == ['SECOND_PRIVATE_MESSAGE']
        finally:
            await memory.close()
    asyncio.run(run())


def test_ordinary_summaries_do_not_cross_identity_boundaries():
    async def run():
        memory = manager()
        try:
            await memory._redis.setex(memory._summary_key('alice:orders', 'same'), 86400, 'FIRST_PRIVATE_SUMMARY')
            await memory._redis.setex(memory._summary_key('alice', 'orders:same'), 86400, 'SECOND_PRIVATE_SUMMARY')
            assert (await memory.get_context('alice:orders', 'same')).summary == 'FIRST_PRIVATE_SUMMARY'
            assert (await memory.get_context('alice', 'orders:same')).summary == 'SECOND_PRIVATE_SUMMARY'
        finally:
            await memory.close()
    asyncio.run(run())


def test_ordinary_unambiguous_legacy_messages_remain_readable():
    async def run():
        memory = manager()
        try:
            raw = json.dumps({'role':'user','content':'LEGACY_OWN_MESSAGE',
                              'ts':'2026-10-27T10:00:00','metadata':{}})
            await memory._redis.lpush('wm:alice:old', raw)
            await memory._redis.setex('summary:alice:old', 86400, 'LEGACY_OWN_SUMMARY')
            context = await memory.get_context('alice', 'old')
            assert [m.content for m in context.recent_messages] == ['LEGACY_OWN_MESSAGE']
            assert context.summary == 'LEGACY_OWN_SUMMARY'
            await memory.add_message('alice', 'old', MsgRole.USER, 'NEXT_OWN_MESSAGE')
            assert len(await memory._redis.lrange('wm:alice:old', 0, -1)) == 2
        finally:
            await memory.close()
    asyncio.run(run())


def test_ambiguous_legacy_messages_are_preserved_without_guessing_the_owner():
    async def run():
        memory = manager()
        try:
            key = 'wm:alice:orders:same'
            raw = json.dumps({'role':'user','content':'OWNER_UNPROVEN_LEGACY',
                              'ts':'2026-10-27T10:00:00','metadata':{}})
            await memory._redis.lpush(key, raw)
            assert await memory._get_working_memory('alice:orders', 'same') == []
            assert await memory._get_working_memory('alice', 'orders:same') == []
            await memory.add_message('alice:orders', 'same', MsgRole.USER, 'NEW_FIRST_MESSAGE')
            await memory.add_message('alice', 'orders:same', MsgRole.USER, 'NEW_SECOND_MESSAGE')
            assert await memory._redis.lrange(key, 0, -1) == [raw]
        finally:
            await memory.close()
    asyncio.run(run())


def test_ambiguous_legacy_summary_is_preserved_and_not_reused():
    async def run():
        memory = manager()
        try:
            key = 'summary:alice:orders:same'
            await memory._redis.setex(key, 86400, 'OWNER_UNPROVEN_LEGACY_SUMMARY')
            assert (await memory.get_context('alice:orders', 'same')).summary == ''
            assert (await memory.get_context('alice', 'orders:same')).summary == ''
            assert await memory._redis.get(key) == 'OWNER_UNPROVEN_LEGACY_SUMMARY'
        finally:
            await memory.close()
    asyncio.run(run())


def test_existing_colon_user_profile_audit_remains_readable():
    async def run():
        memory = manager()
        try:
            audit = {'version':'1','source_conv':'old'}
            await memory._redis.lpush('profile_audit:alice:orders:', json.dumps(audit))
            assert await memory.get_profile_audit('alice:orders') == [audit]
        finally:
            await memory.close()
    asyncio.run(run())


def test_boundary_shifts_and_empty_components_do_not_alias_keys():
    identities = [('a:b:c', ''), ('a:b', 'c:'), ('a', 'b:c:'), ('', 'a:b:c:')]
    for kind in ('wm','summary'):
        assert len({memory_key('',kind,*pair) for pair in identities}) == len(identities)


def test_colon_identity_does_not_use_an_ordinary_user_named_v2():
    # A naive kind:v2:digest encoding aliases an existing ordinary user 'v2'.
    import hashlib
    pair = ('alice:orders','same')
    digest = hashlib.sha256(json.dumps(list(pair), ensure_ascii=True).encode()).hexdigest()
    assert memory_key('','wm',*pair) != memory_key('','wm','v2',digest)


def test_empty_conversation_does_not_read_an_ambiguous_legacy_key():
    async def run():
        memory = manager()
        try:
            raw = json.dumps({'role':'user','content':'OTHER_OWNER_LEGACY',
                              'ts':'2026-10-27T10:00:00','metadata':{}})
            await memory._redis.lpush('wm:alice:orders:', raw)
            await memory._redis.setex('summary:alice:orders:',86400,'OTHER_OWNER_SUMMARY')
            for identity in [('alice:orders',''),('alice','orders:')]:
                context = await memory.get_context(*identity)
                assert context.recent_messages == [] and context.summary == ''
            assert await memory._redis.lrange('wm:alice:orders:',0,-1) == [raw]
            assert await memory._redis.get('summary:alice:orders:') == 'OTHER_OWNER_SUMMARY'
        finally:
            await memory.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad_field,action', [
    (field,action) for field in ('user','conv') for action in ('add','read','archive','profile','audit')
    if action!='audit' or field=='user'
])
def test_invalid_unicode_identity_is_rejected_before_accessing_memory(bad_field,action):
    async def run():
        from copy import deepcopy
        memory = manager()
        user = 'alice' + ('\ud800' if bad_field=='user' else '')
        conv = 'old' + ('\udfff' if bad_field=='conv' else '')
        try:
            await memory.add_message('alice','old',MsgRole.USER,'EXISTING_OWNER_PRIVATE')
            before = deepcopy(memory._redis.values)
            with pytest.raises(ValueError,match='Unicode'):
                if action=='add':await memory.add_message(user,conv,MsgRole.USER,'MUST_NOT_WRITE')
                elif action=='read':await memory.get_context(user,conv)
                elif action=='archive':await memory.archive_session(user,conv)
                elif action=='profile':await memory.update_profile(user,conv)
                else:await memory.get_profile_audit(user)
            assert memory._redis.values == before
            assert memory._client.calls == 0
        finally:
            await memory.close()
    asyncio.run(run())


def test_profile_seed_rejects_invalid_unicode_source_conversation():
    async def run():
        memory = MemoryManager(namespace='eval_'+'d'*32, redis_client=Redis(),
                               chroma_client=Chroma(), client=Client([]))
        try:
            with pytest.raises(ValueError,match='Unicode'):
                await memory.seed_profile('alice',{'preferences':['text']},source_conv='old\ud800')
            assert memory._profile.count() == 0 and memory._redis.values == {}
        finally:
            await memory.close()
    asyncio.run(run())
