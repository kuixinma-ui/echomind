import asyncio
from types import SimpleNamespace

import pytest

from tests.test_context_storage import Redis, Chroma, Client


def memory(namespace='eval_' + 'a' * 32):
    from memory.conversation_memory import MemoryManager
    return MemoryManager(namespace=namespace, redis_client=Redis(), chroma_client=Chroma(),
                         client=Client([{'preferences': [], 'entities': {}}] * 4))


def test_explicit_session_archive_is_readable_across_sessions_and_user_isolated():
    async def run():
        from memory.roles import MsgRole
        manager = memory()
        await manager.add_message('alice', 'prior', MsgRole.USER, '订单号 #80002，昨天签收未拆封')
        reference = await manager.archive_session('alice', 'prior')
        assert reference['evidence_id'].startswith('mem_')
        current = await manager.get_context('alice', 'current', query='上次订单号')
        assert '#80002' in current.to_prompt_text()
        stranger = await manager.get_context('bob', 'current', query='上次订单号')
        assert '#80002' not in stranger.to_prompt_text()
        await manager.archive_session('alice', 'prior')
        assert manager._episodic.count() == 1
        await manager.close()
    asyncio.run(run())


def test_archive_storage_failure_is_explicit():
    async def run():
        from memory.roles import MsgRole
        manager = memory()
        await manager.add_message('alice', 'prior', MsgRole.USER, '订单号 #80002')
        def unavailable(**kwargs):
            raise RuntimeError('storage unavailable')
        manager._episodic.upsert = unavailable
        with pytest.raises(RuntimeError, match='storage unavailable'):
            await manager.archive_session('alice', 'prior')
        await manager.close()
    asyncio.run(run())


def scenario():
    return {'id': 'memory-test', 'version': '1', 'status': 'synthetic_pending_review',
            'knowledge': [{'id': 'policy', 'title': '演示政策', 'version': '1', 'content': '记住用户提供的订单，不编造业务状态。'}],
            'cases': [{'id': 'memory', 'scenario': 'cross_session_memory', 'description': '隔天查询订单',
                       'turns': [{'message': '上次订单号是什么？', 'expected_intent': 'refund'}],
                       'initial_state': {'messages': [{'session': 'prior', 'role': 'user', 'content': '订单号 #80002'}]},
                       'budgets': {'turn_timeout_s': 5}, 'assertions': [
                           {'id': 'memory', 'type': 'memory_recalled', 'turn': 0, 'session': 'prior', 'hard': True},
                           {'id': 'semantic', 'type': 'semantic', 'turn': 0, 'expected': '应记住订单号 #80002', 'evidence': ['policy']},
                       ]}]}


def test_removing_cross_session_memory_fails_even_when_judge_is_generous():
    async def run():
        from services.chat_service import ChatService
        from agents.agent_orchestrator import AgentType, OrchestratorResult
        from core.intent_recognizer import IntentCategory, UrgencyLevel
        from evaluation.scenario_runner import ScenarioRunner
        captured = []

        class Orchestrator:
            async def recognize_intent(self, *args, **kwargs):
                return SimpleNamespace(intent=IntentCategory.REFUND, intent_group='billing',
                                       urgency=UrgencyLevel.LOW, confidence=1, entities={}, source_scores={})
            async def run(self, req):
                answer = '继续核实订单 #80002。' if '#80002' in req.context else '我会协助核实，请提供订单号。'
                return OrchestratorResult(req.request_id, answer, AgentType.BILLING, req.intent)

        async def factory(remove=False, **kwargs):
            manager = memory(kwargs['namespace'])
            service = ChatService(Orchestrator(), manager)
            if remove:
                async def missing_archive(*args):
                    return {'evidence_id': 'mem_missing', 'source_conv': args[1]}
                manager.archive_session = missing_archive
            async def close(cleanup=False):
                await service.close()
                if cleanup:
                    await manager.cleanup()
                await manager.close()
            return SimpleNamespace(service=service, memory=manager, close=close)

        async def judge(question, response, **kwargs):
            captured.append(kwargs)
            return dict.fromkeys(('relevance', 'accuracy', 'completeness', 'helpfulness'), 1.0)

        positive = await ScenarioRunner(factory, judge=judge).run(scenario())
        negative = await ScenarioRunner(lambda **kw: factory(remove=True, **kw), judge=judge).run(scenario())
        assert positive['passed'] == 1
        assert negative['passed'] == 0 and negative['hard_fail'] == 1
        assert '#80002' in captured[0]['context']
        await asyncio.sleep(0)
    asyncio.run(run())
