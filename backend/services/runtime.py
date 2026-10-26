"""Create conversation components without sharing evaluation and online state."""
from __future__ import annotations

import asyncio
import inspect
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agents.agent_orchestrator import AgentOrchestrator, build_shared_rag_tools
from core.llm_utils import structured_thinking_policy
from core.skill_loader import SkillManager
from mcp.tool_manager import MCPToolManager, Tool
from services.chat_service import ChatService
from services.storage import collection_name, require_evaluation_namespace, validate_namespace


def _safe_error_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) else 'RuntimeError'


class RuntimeCleanupError(RuntimeError):
    """All remaining cleanup actions ran; errors are available to the owner."""

    def __init__(self, errors):
        super().__init__('Runtime cleanup failed')
        self.errors = tuple(errors)


class RuntimeInitializationError(RuntimeError):
    """Safe initialization failure with the outcome of awaited rollback."""

    def __init__(self, error, cleanup_errors=()):
        super().__init__('Runtime initialization failed')
        self.error_type = _safe_error_type(error)
        self.cleanup_status = 'rollback_failed' if cleanup_errors else 'rolled_back'
        self.cleanup_errors = tuple(_safe_error_type(item) for item in cleanup_errors)


async def _finish_owned(task):
    """A cancelled caller must still recover the result owned by a worker."""
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    if cancellation is not None:
        # Retrieve worker exceptions, but preserve the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


async def _owned_thread(action):
    return await _finish_owned(asyncio.create_task(asyncio.to_thread(action)))


async def _cleanup_resources(runtime, cleanup):
    errors = []

    async def attempt(action):
        try:
            result = action()
            if inspect.isawaitable(result):
                await result
        except (Exception, asyncio.CancelledError) as exc:
            errors.append(exc)

    async def delete_collection(client, name):
        try:
            await asyncio.to_thread(client.delete_collection, name)
        except Exception as exc:
            # A constructor can fail before its collection is created.
            if type(exc).__name__ not in {'NotFoundError', 'InvalidCollectionException'}:
                raise

    memory, knowledge = runtime.memory, runtime.knowledge
    if runtime.service is not None:
        await attempt(runtime.service.close)
    if cleanup:
        if memory is not None:
            if hasattr(memory, '_runtime_collection_names'):
                redis = getattr(memory, '_redis', None)
                if redis is not None:
                    async def clear_redis():
                        async for key in redis.scan_iter(match=f'{runtime.namespace}:*'):
                            await redis.delete(key)
                    await attempt(clear_redis)
                for name in tuple(memory._runtime_collection_names):
                    await attempt(lambda name=name: delete_collection(memory._chroma, name))
            elif hasattr(memory, 'cleanup'):
                await attempt(memory.cleanup)
        if knowledge is not None and hasattr(knowledge, 'cleanup'):
            async def clear_knowledge():
                try:
                    await asyncio.to_thread(knowledge.cleanup)
                except Exception as exc:
                    if type(exc).__name__ not in {'NotFoundError', 'InvalidCollectionException'}:
                        raise
            await attempt(clear_knowledge)

    clients = []
    if memory is not None:
        if hasattr(memory, '_runtime_initialized'):
            clients.extend((getattr(memory, '_redis', None), getattr(memory, '_client', None)))
        elif hasattr(memory, 'close'):
            await attempt(memory.close)
        clients.append(getattr(memory, '_chroma', None))
    clients.extend((getattr(runtime.tool_manager, '_client', None),
                    getattr(getattr(runtime.orchestrator, '_composer', None), '_client', None),
                    getattr(getattr(runtime.orchestrator, '_intent_recognizer', None), 'client', None),
                    getattr(knowledge, '_client', None)))
    for client in {id(value): value for value in clients if value is not None}.values():
        close = getattr(client, 'aclose', None) or getattr(client, 'close', None)
        if close is not None:
            await attempt(close)
    return errors


def runtime_config_from_env():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
    return {
        'api_key': os.getenv('ANTHROPIC_API_KEY', ''),
        'base_url': os.getenv('ANTHROPIC_BASE_URL') or None,
        'model': os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022'),
        'judge_model': os.getenv('ECHOMIND_JUDGE_MODEL') or os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022'),
        'structured_thinking': structured_thinking_policy(),
        'redis_url': os.getenv('REDIS_URL', 'redis://redis:6379/0'),
        'chroma_host': os.getenv('CHROMA_HOST', 'chromadb'),
        'chroma_port': int(os.getenv('CHROMA_PORT', '8000')),
        'chroma_path': os.getenv('CHROMA_PERSIST_DIRECTORY', './data/chroma'),
        'chroma_mode': os.getenv('ECHOMIND_CHROMA_MODE') or None,
        'skills_dir': os.getenv('ECHOMIND_SKILLS_DIR', str(Path(__file__).resolve().parents[1] / 'skills')),
        'skills_max_prompt_chars': int(os.getenv('ECHOMIND_SKILLS_MAX_PROMPT_CHARS', '5000')),
        'rewrite_policy': os.getenv('ECHOMIND_QUERY_REWRITE_POLICY', 'always'),
        'rerank_policy': os.getenv('ECHOMIND_RERANK_POLICY', 'always'),
        'retrieval_timeout_s': float(os.getenv('ECHOMIND_RETRIEVAL_TIMEOUT_S', '30')),
    }


@dataclass
class Runtime:
    namespace: str
    service: ChatService
    orchestrator: object
    memory: object
    knowledge: object
    tool_manager: object
    skills: object
    _closed: bool = False

    @property
    def recognizer(self):
        return self.orchestrator._intent_recognizer

    async def close(self, cleanup=False):
        if cleanup:
            require_evaluation_namespace(self.namespace)
        if self._closed:
            return
        self._closed = True
        errors = await _finish_owned(asyncio.create_task(_cleanup_resources(self, cleanup)))
        if errors:
            raise RuntimeCleanupError(errors) from errors[0]


async def create_runtime(config, *, namespace='', knowledge_documents=None, telemetry=None,
                   memory_factory=None, knowledge_factory=None, orchestrator_factory=None,
                   tool_manager_factory=None):
    """Acquire a runtime, awaiting rollback before a failure reaches its caller.

    Ordinary class factories are retained during __init__. Other callable
    factories must unwind any hidden allocations themselves if they raise.
    """
    validate_namespace(namespace)
    if namespace:
        require_evaluation_namespace(namespace)
    if not config.get('api_key'):
        raise ValueError('ANTHROPIC_API_KEY is required for live runtime')
    if memory_factory is None:
        from memory.conversation_memory import MemoryManager
        memory_factory = MemoryManager
    if knowledge_factory is None:
        from mcp.knowledge_base import KnowledgeBase
        knowledge_factory = KnowledgeBase
    runtime = Runtime(namespace, None, None, None, None, None, None)

    def acquire(attribute, factory, **kwargs):
        # Ordinary classes can be retained before __init__ runs, so failure
        # after a collection/client allocation still leaves an owned object.
        if type(factory) is type and factory.__new__ is object.__new__:
            value = object.__new__(factory)
            setattr(runtime, attribute, value)
            factory.__init__(value, **kwargs)
        else:
            value = factory(**kwargs)
            setattr(runtime, attribute, value)
        return value

    def fallback(params, context, error):
        return [{'title': '知识库降级结果', 'content': '知识库暂时不可用，请稍后重试或转人工核实。',
                 'score': 0.0, 'fallback': True}]

    try:
        credentials = {key: config[key] for key in ('api_key', 'base_url', 'model') if config.get(key)}
        skills = SkillManager(root_dir=config.get('skills_dir', str(Path(__file__).resolve().parents[1] / 'skills')),
                              max_prompt_chars=config.get('skills_max_prompt_chars', 5000))
        runtime.skills = skills
        skills.load()
        orchestrator = acquire('orchestrator', orchestrator_factory or AgentOrchestrator,
                               **credentials, skill_manager=skills)
        chroma_mode = config.get('chroma_mode') or ('http' if namespace else 'auto')
        memory = await _owned_thread(lambda: acquire('memory', memory_factory, **credentials, namespace=namespace, chroma_mode=chroma_mode,
            **{key: config[key] for key in ('redis_url', 'chroma_host', 'chroma_port', 'chroma_path') if key in config}))
        knowledge = await _owned_thread(lambda: acquire('knowledge', knowledge_factory,
            collection_name=collection_name(namespace, 'knowledge_base'), seed_default=not bool(namespace), chroma_mode=chroma_mode,
            **{key: config[key] for key in ('chroma_host', 'chroma_port', 'chroma_path') if key in config}))
        if knowledge_documents is not None:
            await _owned_thread(lambda: knowledge.add_documents(knowledge_documents))
        manager = acquire('tool_manager', tool_manager_factory or MCPToolManager, **credentials,
            **{key: config[key] for key in ('rewrite_policy', 'rerank_policy', 'retrieval_timeout_s') if key in config})

        async def search_knowledge(params, context):
            # The runtime owns its data boundary. Agent/tool arguments and
            # caller context must not widen it to another stored scope.
            scoped_params = {**params, 'scope': 'evaluation' if namespace else 'public'}
            return await knowledge.search_handler(scoped_params, context)

        manager.register(Tool(name='knowledge_search', description='检索当前知识快照',
                              handler=search_knowledge,
                              schema={'type': 'object', 'properties': {'query': {'type': 'string'}, 'top_k': {'type': 'integer'}},
                                      'required': ['query']}, cache_ttl=300.0, supports_rerank=True, fallback=fallback))
        orchestrator.set_shared_tools(build_shared_rag_tools(manager))
        runtime.service = ChatService(orchestrator, memory, telemetry=telemetry)
        from observability.instrumentation import instrument_runtime
        return instrument_runtime(runtime)
    except BaseException as exc:
        cleanup_task = asyncio.create_task(_cleanup_resources(runtime, bool(namespace)))
        try:
            errors = await _finish_owned(cleanup_task)
        except asyncio.CancelledError as cancellation:
            errors = cleanup_task.result()
            exc = cancellation
        runtime._closed = True
        if isinstance(exc, asyncio.CancelledError):
            exc.cleanup_status = 'rollback_failed' if errors else 'rolled_back'
            exc.error_type = 'CancelledError'
            raise exc
        if not isinstance(exc, Exception):
            raise
        raise RuntimeInitializationError(exc, errors) from exc
