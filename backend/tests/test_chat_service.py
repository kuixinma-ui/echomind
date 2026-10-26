import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agents.agent_orchestrator import AgentType, OrchestratorResult
from core.intent_recognizer import IntentCategory, UrgencyLevel


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class Memory:
    def __init__(self, clock=None):
        self.messages = []
        self.profiles = []
        self.clock = clock or Clock()
        self.profile_gate = None
        self.profile_error = False

    async def get_context(self, user_id, conv_id, query):
        self.clock.value += 0.01
        recent = [SimpleNamespace(role=SimpleNamespace(value=r), content=c)
                  for u, v, r, c in self.messages if (u, v) == (user_id, conv_id)]
        return SimpleNamespace(recent_messages=recent, to_prompt_text=lambda: 'known history')

    async def add_message(self, user_id, conv_id, role, content):
        self.clock.value += 0.005
        self.messages.append((user_id, conv_id, role.value, content))

    async def update_profile(self, user_id, conv_id):
        if self.profile_gate:
            await self.profile_gate.wait()
        if self.profile_error:
            raise RuntimeError('profile unavailable')
        self.clock.value += 0.04
        self.profiles.append((user_id, conv_id))


class Orchestrator:
    def __init__(self, clock=None):
        self.clock = clock or Clock()
        self.requests = []
        self.histories = []

    async def recognize_intent(self, message, history=None):
        self.clock.value += 0.02
        self.histories.append(history)
        return SimpleNamespace(intent=IntentCategory.TECHNICAL_LOGIN, intent_group='technical',
                               entities={'error_code': ['401']}, urgency=UrgencyLevel.MEDIUM,
                               confidence=0.9, source_scores={'rule': 0.8})

    async def run(self, req):
        self.clock.value += 0.03
        self.requests.append(req)
        return OrchestratorResult(request_id=req.request_id, response='检查认证配置',
                                  agent_type=AgentType.TECHNICAL, intent=req.intent,
                                  latency_ms=30, agent_types=[AgentType.TECHNICAL],
                                  tools_used=['lookup_error_code'],
                                  tool_traces=[{'tool_name': 'lookup_error_code', 'input': {'error_code': '401'}}])


def new_service(orchestrator, memory, **kwargs):
    from services.chat_service import ChatService
    return ChatService(orchestrator, memory, **kwargs)


def test_chat_covers_memory_entities_history_and_total_timing():
    async def run():
        clock = Clock()
        memory, orch = Memory(clock), Orchestrator(clock)
        service = new_service(orch, memory, clock=clock)
        result = await service.chat('登录401', 'u1', 'c1', wait_for_profile=True)
        assert result['latency_ms'] == pytest.approx(110)
        assert result['orchestrator_latency_ms'] == 30
        assert set(result['stage_latency_ms']) >= {'memory_read', 'intent', 'orchestration', 'memory_write', 'profile'}
        assert result['entities'] == {'error_code': ['401']}
        assert result['tool_traces'][0]['tool_name'] == 'lookup_error_code'
        assert orch.requests[0].entities == result['entities']
        assert [m[2] for m in memory.messages] == ['user', 'assistant']
        assert memory.profiles == [('u1', 'c1')]
        await service.chat('还是失败', 'u1', 'c1', wait_for_profile=True)
        assert orch.histories[1][0]['content'] == '登录401'
        await service.close()
    asyncio.run(run())


def test_profile_failure_is_reported_without_losing_response():
    async def run():
        memory = Memory()
        memory.profile_error = True
        service = new_service(Orchestrator(), memory)
        result = await service.chat('登录401', 'u', wait_for_profile=True)
        assert result['response'] == '检查认证配置'
        assert result['profile_status'] == 'failed'
        await service.close()
    asyncio.run(run())


def test_close_cancels_pending_profile_and_rejects_new_chat():
    async def run():
        memory = Memory()
        memory.profile_gate = asyncio.Event()
        service = new_service(Orchestrator(), memory)
        result = await service.chat('登录401', 'u', 'c')
        assert result['profile_status'] == 'scheduled'
        await asyncio.sleep(0)
        await service.close()
        assert not service.pending_tasks
        with pytest.raises(RuntimeError, match='closed'):
            await service.chat('again', 'u', 'c')
    asyncio.run(run())


def test_cancelled_chat_does_not_leave_profile_task():
    async def run():
        memory = Memory()
        memory.profile_gate = asyncio.Event()
        service = new_service(Orchestrator(), memory)
        task = asyncio.create_task(service.chat('登录401', 'u', 'c', wait_for_profile=True))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await service.close()
        assert not service.pending_tasks
    asyncio.run(run())


def test_api_chat_uses_shared_service_and_preserves_response_contract(monkeypatch):
    async def run():
        from api import main
        service = new_service(Orchestrator(), Memory())
        monkeypatch.setattr(main, '_chat_service', service, raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/chat', json={'message': '登录401', 'user_id': 'u', 'conv_id': 'c'})
        assert response.status_code == 200
        data = response.json()
        assert data['conv_id'] == 'c'
        assert data['entities']['error_code'] == ['401']
        assert data['orchestrator_latency_ms'] == 30
        assert data['stage_latency_ms']['memory_read'] >= 0
        await service.close()
    asyncio.run(run())


def test_profile_explicit_failure_and_telemetry_context_are_preserved():
    async def run():
        from observability.telemetry import NoopTelemetry, get_telemetry
        from observability.instrumentation import current_usage
        telemetry = NoopTelemetry()
        memory, orch = Memory(), Orchestrator()
        async def failed_profile(*args):
            return False
        memory.update_profile = failed_profile
        original = orch.run
        async def observed(req):
            assert get_telemetry() is telemetry
            assert current_usage() is not None
            return await original(req)
        orch.run = observed
        service = new_service(orch, memory, telemetry=telemetry)
        result = await service.chat('登录401', 'u', 'c', wait_for_profile=True)
        assert result['profile_status'] == 'failed'
        assert result['usage']['model_calls'] == 0  # A fake orchestrator made no provider calls.
        assert current_usage() is None
        await service.close()
    asyncio.run(run())
