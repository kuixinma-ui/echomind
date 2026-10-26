"""The complete conversation boundary shared by HTTP and evaluation runners."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import contextmanager, nullcontext
from weakref import WeakValueDictionary

from agents.agent_orchestrator import Request
from memory.roles import MsgRole
from observability.telemetry import bind_telemetry, get_telemetry
from observability.instrumentation import bind_usage
from observability.experiments import current_trace_metadata
from context.memory_evidence import memory_evidence_id

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, orchestrator, memory, telemetry=None, *, clock=time.perf_counter):
        self.orchestrator = orchestrator
        self.memory = memory
        self.telemetry = telemetry if telemetry is not None else get_telemetry()
        self.clock = clock
        self._tasks: set[asyncio.Task] = set()
        self._locks = WeakValueDictionary()
        self._profile_locks = WeakValueDictionary()
        self._closed = False

    @property
    def pending_tasks(self):
        return tuple(task for task in self._tasks if not task.done())

    def _lock(self, table, key):
        lock = table.get(key)
        if lock is None:
            lock = asyncio.Lock()
            table[key] = lock
        return lock

    def _span(self, name, **kwargs):
        return self.telemetry.span(name, **kwargs) if self.telemetry else nullcontext(None)

    @contextmanager
    def _stage(self, name, timings, *, kind='span'):
        started = self.clock()
        try:
            with self._span(name, kind=kind):
                yield
        finally:
            timings[name] = round((self.clock() - started) * 1000, 3)

    async def _update_profile(self, user_id, conv_id):
        try:
            async with self._lock(self._profile_locks, user_id):
                with self._span('memory.profile'):
                    updated = await self.memory.update_profile(user_id, conv_id)
                    if updated is False:
                        return False
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Profile update failed (%s)', type(exc).__name__)
            return False

    async def chat(self, message, user_id, conv_id=None, *, wait_for_profile=False):
        if self._closed:
            raise RuntimeError('ChatService is closed')
        if not isinstance(message, str) or not message.strip():
            raise ValueError('message must not be empty')
        conv_id = conv_id or str(uuid.uuid4())
        started = self.clock()
        timings = {}
        # A conversation must preserve user/assistant ordering while unrelated
        # conversations remain concurrent. Weak locks do not retain user IDs.
        async with self._lock(self._locks, (user_id, conv_id)):
            if self._closed:
                raise RuntimeError('ChatService is closed')
            with bind_telemetry(self.telemetry), bind_usage() as usage, self._span('chat', kind='agent', input={'message': message},
                            metadata={**current_trace_metadata(), 'user_id': user_id, 'conv_id': conv_id}) as observation:
                with self._stage('memory_read', timings):
                    memory_context = await self.memory.get_context(user_id, conv_id, query=message)
                history = [{'role': item.role.value, 'content': item.content}
                           for item in memory_context.recent_messages[-5:]] or None
                with self._stage('intent', timings):
                    intent = await self.orchestrator.recognize_intent(message, history=history)
                req = Request(message=message, user_id=user_id, conv_id=conv_id,
                              context=memory_context.to_prompt_text(), history=history,
                              entities=intent.entities, intent=intent.intent,
                              intent_group=intent.intent_group, urgency=intent.urgency,
                              intent_confidence=intent.confidence)
                with self._stage('orchestration', timings, kind='agent'):
                    result = await self.orchestrator.run(req)
                with self._stage('memory_write', timings):
                    await self.memory.add_message(user_id, conv_id, MsgRole.USER, message)
                    await self.memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)
                task = asyncio.create_task(self._update_profile(user_id, conv_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                profile_status = 'scheduled'
                if wait_for_profile:
                    with self._stage('profile', timings):
                        profile_status = 'completed' if await task else 'failed'
                response = {
                    'conv_id': conv_id, 'request_id': result.request_id,
                    'response': result.response,
                    'intent': result.intent.value if result.intent else 'other',
                    'intent_group': intent.intent_group,
                    'agent_type': result.agent_type.value,
                    'agent_types': [value.value for value in result.agent_types],
                    'primary_agent': (result.primary_agent or result.agent_type).value,
                    'supporting_agents': [value.value for value in result.supporting_agents],
                    'tools_used': result.tools_used, 'tool_traces': result.tool_traces,
                    'routing_reason': result.routing_reason,
                    'routing_confidence': result.routing_confidence,
                    'escalated': result.escalated,
                    'latency_ms': round((self.clock() - started) * 1000, 3),
                    'orchestrator_latency_ms': result.latency_ms,
                    'stage_latency_ms': timings,
                    'knowledge_used': 'search_knowledge_base' in result.tools_used,
                    'entities': intent.entities, 'intent_confidence': intent.confidence,
                    'intent_source_scores': intent.source_scores,
                    'profile_status': profile_status,
                    'usage': usage.snapshot(),
                    'usage_scope': 'including_profile' if wait_for_profile else 'through_response',
                    'memory_evidence_ids': [memory_evidence_id(text) for text in getattr(memory_context, 'relevant_history', [])],
                }
                if observation is not None:
                    observation.update(output={'response': result.response}, metadata={
                        'request_id': result.request_id, 'intent': response['intent'],
                        'agent_types': response['agent_types'], 'tools_used': result.tools_used,
                        'stage_latency_ms': timings,
                    })
                return response

    async def close(self):
        self._closed = True
        tasks = self.pending_tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
