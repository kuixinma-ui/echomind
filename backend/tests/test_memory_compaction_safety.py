"""Failure-safe compression over fake providers and in-memory storage only."""
import asyncio
from datetime import datetime, timedelta
import json
import threading
from types import SimpleNamespace

import pytest
from redis.exceptions import WatchError

from memory.conversation_memory import MemoryManager, Message
from memory.roles import MsgRole
from observability.budgets import EvaluationBudgetExceeded
from tests.test_context_storage import Collection, Chroma, Redis


class Provider:
    def __init__(self, *, fail_at=0, error=None, first=None):
        self.messages = self
        self.calls = 0
        self.fail_at = fail_at
        self.error = error
        self.first = first

    async def create(self, **kwargs):
        self.calls += 1
        if self.first and self.calls == 1:
            await self.first()
        if self.calls == self.fail_at:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=f'summary-{self.calls}')])

    async def close(self):
        pass


class Store(Collection):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.error = None
        self.calls = []
        self.gate = None

    def write(self, **kwargs):
        self.events.append('archive')
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.gate and len(self.calls) == 1:
            entered, release, finished = self.gate
            entered.set()
            assert release.wait(5)
            super().add(**kwargs)
            finished.set()
        else:
            super().add(**kwargs)

    add = write
    upsert = write


class Storage(Chroma):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def get_or_create_collection(self, name, **kwargs):
        return self.collections.setdefault(name, Store(self.events))


class CommitRedis(Redis):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.commit_error = None
        self.before_execute = None
        self.executions = 0
        self.deleted_keys = []

    async def delete(self, *keys):
        self.deleted_keys.extend(keys)
        return await super().delete(*keys)

    def pipeline(self, transaction=True):
        assert transaction is True
        pipe = super().pipeline(transaction=transaction)
        execute = pipe.execute

        async def attempt():
            self.executions += 1
            self.events.append('redis.commit')
            if self.before_execute:
                await self.before_execute()
            if self.commit_error:
                raise self.commit_error
            return await execute()

        pipe.execute = attempt
        return pipe


def manager(provider=None):
    events = []
    redis = CommitRedis(events)
    memory = MemoryManager(api_key='test', redis_client=redis, chroma_client=Storage(events),
                           client=provider or Provider())
    return memory, redis, memory._episodic, events


async def seed(memory, redis, count=15, summary='previous summary'):
    memory.COMPRESS_AT = 1000
    for i in range(count):
        await memory.add_message('alice', 'c', MsgRole.USER, f'fact-{i}', {'index': i})
    memory.COMPRESS_AT = 15
    if summary is not None:
        await redis.setex(memory._summary_key('alice', 'c'), 86400, summary)
    return list(redis.values[memory._wm_key('alice', 'c')])


def contents(redis, memory):
    return [json.loads(raw)['content'] for raw in reversed(redis.values[memory._wm_key('alice', 'c')])]


def test_failed_archive_does_not_publish_summary_or_trim_working_messages():
    async def run():
        memory, redis, archive, events = manager()
        before = await seed(memory, redis)
        events.clear()
        archive.error = OSError('synthetic archive failure')
        with pytest.raises(OSError, match='synthetic archive failure'):
            await memory._compress('alice', 'c')
        assert redis.values[memory._wm_key('alice', 'c')] == before
        assert await redis.get(memory._summary_key('alice', 'c')) == 'previous summary'
        assert archive.rows == {} and redis.executions == 0
        assert redis.deleted_keys == [] and events == ['archive']
        await memory.close()
    asyncio.run(run())


@pytest.mark.parametrize('error', [EvaluationBudgetExceeded('model_calls'), asyncio.CancelledError()])
@pytest.mark.parametrize('stage', ['summary', 'merge'])
def test_budget_and_cancellation_propagate_before_archive_or_commit(error, stage):
    async def run():
        provider = Provider(fail_at=1 if stage == 'summary' else 2, error=error)
        memory, redis, archive, events = manager(provider)
        before = await seed(memory, redis)
        with pytest.raises(type(error)):
            await memory._compress('alice', 'c')
        assert redis.values[memory._wm_key('alice', 'c')] == before
        assert await redis.get(memory._summary_key('alice', 'c')) == 'previous summary'
        assert archive.rows == {} and redis.executions == 0
        await memory.close()
    asyncio.run(run())


def test_failed_redis_commit_keeps_source_and_retry_reuses_confirmed_archive():
    async def run():
        memory, redis, archive, events = manager()
        before = await seed(memory, redis)
        redis.commit_error = OSError('synthetic commit failure')
        with pytest.raises(OSError, match='synthetic commit failure'):
            await memory._compress('alice', 'c')
        assert len(archive.rows) == 1
        archive_id = next(iter(archive.rows))
        assert redis.values[memory._wm_key('alice', 'c')] == before
        assert await redis.get(memory._summary_key('alice', 'c')) == 'previous summary'
        redis.commit_error = None
        assert await memory._compress('alice', 'c') is True
        assert list(archive.rows) == [archive_id] and len(archive.calls) == 2
        assert contents(redis, memory) == [f'fact-{i}' for i in range(10, 15)]
        assert redis.deleted_keys == []
        await memory.close()
    asyncio.run(run())


@pytest.mark.parametrize('conflict', ['append', 'summary', 'watch_error'])
def test_changed_snapshot_or_watch_conflict_never_publishes_stale_result(conflict):
    async def run():
        memory, redis, archive, events = manager()
        before = await seed(memory, redis)
        key = memory._wm_key('alice', 'c')
        skey = memory._summary_key('alice', 'c')

        async def change_before_watch():
            if conflict == 'append':
                await redis.lpush(key, json.dumps({'role': 'user', 'content': 'new arrival',
                    'ts': datetime.now().isoformat(), 'metadata': {}}))
            elif conflict == 'summary':
                await redis.setex(skey, 86400, 'newer summary')

        memory._client.first = change_before_watch
        if conflict == 'watch_error':
            redis.commit_error = WatchError('synthetic concurrent write')
        assert await memory._compress('alice', 'c') is False
        assert len(archive.rows) == 1
        if conflict == 'append':
            assert contents(redis, memory) == [f'fact-{i}' for i in range(15)] + ['new arrival']
        else:
            assert redis.values[key] == before
        assert await redis.get(skey) == ('newer summary' if conflict == 'summary' else 'previous summary')
        assert redis.deleted_keys == []
        await memory.close()
    asyncio.run(run())


def test_write_after_watch_is_detected_by_transaction():
    async def run():
        memory, redis, archive, events = manager()
        await seed(memory, redis)
        key = memory._wm_key('alice', 'c')

        async def concurrent_write():
            await redis.lpush(key, json.dumps({'role': 'user', 'content': 'after watch',
                'ts': datetime.now().isoformat(), 'metadata': {}}))

        redis.before_execute = concurrent_write
        assert await memory._compress('alice', 'c') is False
        assert len(archive.rows) == 1
        assert contents(redis, memory)[-1] == 'after watch' and len(contents(redis, memory)) == 16
        assert await redis.get(memory._summary_key('alice', 'c')) == 'previous summary'
        await memory.close()
    asyncio.run(run())


def test_backlog_beyond_context_window_archives_oldest_bounded_batch_only():
    async def run():
        memory, redis, archive, events = manager()
        await seed(memory, redis, count=35)
        assert len(await memory._get_working_memory('alice', 'c')) == 20
        assert len(await memory._get_working_memory('alice', 'c', complete=True)) == 35
        assert await memory._compress('alice', 'c') is True
        assert contents(redis, memory) == [f'fact-{i}' for i in range(10, 35)]
        stored = next(iter(archive.rows.values()))[1]
        typed = json.loads(stored[memory.HANDOFF_METADATA_KEY])
        assert [row['content'] for row in typed] == [f'fact-{i}' for i in range(10)]
        assert len(await memory._get_working_memory('alice', 'c')) == 20
        assert await memory._compress('alice', 'c') is True
        assert contents(redis, memory) == [f'fact-{i}' for i in range(20, 35)]
        assert len(archive.rows) == 2
        await memory.close()
    asyncio.run(run())


def test_explicit_session_archive_includes_pending_prefix_beyond_context_window():
    async def run():
        memory, redis, archive, events = manager()
        await seed(memory, redis, count=35)
        assert len(await memory._get_working_memory('alice', 'c')) == 20
        result = await memory.archive_session('alice', 'c')
        assert result['message_count'] == 35
        document = next(iter(archive.rows.values()))[0]
        assert 'user: fact-0\n' in document and 'user: fact-34' in document
        assert len(contents(redis, memory)) == 35
        await memory.close()
    asyncio.run(run())


def test_archive_identity_is_stable_and_includes_original_message_time():
    async def run():
        memory, redis, archive, events = manager()
        timestamp = datetime(2026, 10, 27, 12)
        first = [Message(MsgRole.USER, 'same text', timestamp)]
        later = [Message(MsgRole.USER, 'same text', timestamp + timedelta(seconds=1))]
        await memory._store_episodic('alice', 'c', 'user: same text', 'summary one', user_messages=first)
        await memory._store_episodic('alice', 'c', 'user: same text', 'summary retry', user_messages=first)
        assert len(archive.rows) == 1
        await memory._store_episodic('alice', 'c', 'user: same text', 'another batch', user_messages=later)
        assert len(archive.rows) == 2
        await memory._store_episodic('alice', 'c', 'private legacy text', 'summary')
        await memory._store_episodic('alice', 'c', 'private legacy text', 'changed summary')
        assert len(archive.rows) == 3
        await memory.close()
    asyncio.run(run())


def test_cancelled_archive_thread_may_finish_but_source_is_retained_and_retry_is_idempotent():
    async def run():
        memory, redis, archive, events = manager()
        before = await seed(memory, redis)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        archive.gate = (entered, release, finished)
        task = asyncio.create_task(memory._compress('alice', 'c'))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert redis.values[memory._wm_key('alice', 'c')] == before
            assert await redis.get(memory._summary_key('alice', 'c')) == 'previous summary'
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
        assert len(archive.rows) == 1
        assert await memory._compress('alice', 'c') is True
        assert len(archive.rows) == 1 and contents(redis, memory) == [f'fact-{i}' for i in range(10, 15)]
        await memory.close()
    asyncio.run(run())


def test_ordinary_summary_failure_keeps_existing_fallback_with_confirmed_archive():
    async def run():
        provider = Provider(fail_at=1, error=RuntimeError('ordinary summary failure'))
        memory, redis, archive, events = manager(provider)
        await seed(memory, redis, summary=None)
        events.clear()
        assert await memory._compress('alice', 'c') is True
        assert len(archive.rows) == 1
        assert await redis.get(memory._summary_key('alice', 'c')) == '对话包含 10 条消息（摘要生成失败）'
        assert contents(redis, memory) == [f'fact-{i}' for i in range(10, 15)]
        assert events == ['archive', 'redis.commit']
        await memory.close()
    asyncio.run(run())
