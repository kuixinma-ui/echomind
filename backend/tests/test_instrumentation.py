"""Offline contracts for provider usage and runtime observation structure."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from agents.agent_orchestrator import AgentOrchestrator, AgentType, EscalationAgent, Request, TechnicalAgent
from agents.tools import make_tool
from core.intent_recognizer import IntentCategory, UrgencyLevel
from observability.telemetry import bind_telemetry


class RecordingTelemetry:
    def __init__(self):
        self.records = []
        self.active = ContextVar("recording_parent", default=None)

    @contextmanager
    def span(self, name, *, kind="span", input=None, metadata=None):
        record = {"name": name, "kind": kind, "parent": self.active.get(),
                  "input": input, "metadata": metadata or {}, "updates": {}}
        self.records.append(record)
        token = self.active.set(record)
        observation = SimpleNamespace(update=lambda **fields: record["updates"].update(fields))
        try:
            yield observation
        finally:
            self.active.reset(token)


def response(*, content=None, usage=None):
    return SimpleNamespace(content=content or [{"type": "text", "text": "done"}],
                           model="provider-model", usage=usage)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = self
        self.closed = 0
        self.timeout = 30

    async def create(self, **kwargs):
        await asyncio.sleep(0)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self):
        self.closed += 1


def runtime_for(client):
    agents = [SimpleNamespace(_client=client), SimpleNamespace(_client=client)]
    return SimpleNamespace(orchestrator=SimpleNamespace(
        _pool={"general": agents}, _composer=SimpleNamespace(_client=client),
        _intent_recognizer=SimpleNamespace(client=client)),
        memory=SimpleNamespace(_client=client), tool_manager=SimpleNamespace(_client=client))


def req(**overrides):
    values = dict(message="login issue", user_id="u", conv_id="c", intent=IntentCategory.TECHNICAL_LOGIN,
                  intent_group="technical", urgency=UrgencyLevel.LOW)
    return Request(**{**values, **overrides})


def test_instrumented_client_keeps_response_records_real_usage_and_wraps_shared_client_once():
    from observability.instrumentation import bind_usage, current_usage, instrument_runtime

    actual = response(usage=SimpleNamespace(input_tokens=13, output_tokens=7,
                      cache_read_input_tokens=5, cache_creation_input_tokens=3))
    original = FakeClient(actual)
    runtime = runtime_for(original)
    assert instrument_runtime(runtime) is runtime
    proxy = runtime.memory._client
    instrument_runtime(runtime)
    assert runtime.orchestrator._pool["general"][0]._client is proxy
    assert runtime.orchestrator._composer._client is proxy
    assert runtime.orchestrator._intent_recognizer.client is proxy
    assert runtime.tool_manager._client is proxy
    assert runtime.memory._client is proxy
    telemetry = RecordingTelemetry()

    async def exercise():
        with bind_usage() as usage, bind_telemetry(telemetry):
            assert current_usage() is usage
            with telemetry.span("chat"):
                result = await proxy.messages.create(model="requested-model", max_tokens=100, temperature=0.2,
                                                     messages=[{"role": "user", "content": "hi"}])
            snapshot = usage.snapshot()
        assert current_usage() is None
        assert proxy.timeout == 30
        await proxy.close()
        return result, snapshot

    result, usage = asyncio.run(exercise())
    assert result is actual
    assert original.closed == 1
    assert usage == {"input_tokens": 13, "output_tokens": 7, "cache_read_input_tokens": 5,
                     "cache_creation_input_tokens": 3, "model_calls": 1}
    generation = telemetry.records[1]
    assert generation["kind"] == "generation"
    assert generation["parent"] is telemetry.records[0]
    assert generation["updates"]["model"] == "provider-model"
    assert generation["updates"]["model_parameters"] == {"max_tokens": 100, "temperature": 0.2}
    assert generation["updates"]["usage_details"] == {"input_tokens": 13, "output_tokens": 7,
            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3}
    assert "cost_details" not in generation["updates"]


def test_parallel_tasks_share_request_usage_but_interleaved_requests_are_isolated():
    from observability.instrumentation import bind_usage, instrument_runtime

    class ConcurrentClient(FakeClient):
        async def create(self, **kwargs):
            await asyncio.sleep(0.001)
            return response(usage={"input_tokens": kwargs["max_tokens"], "output_tokens": 2})

    proxy = instrument_runtime(runtime_for(ConcurrentClient())).memory._client

    async def one_request(tokens):
        with bind_usage() as usage:
            await asyncio.gather(*(proxy.messages.create(model="test", max_tokens=count) for count in tokens))
            return usage.snapshot()

    async def exercise():
        return await asyncio.gather(one_request([3, 5]), one_request([17, 19]))

    first, second = asyncio.run(exercise())
    assert first == {"input_tokens": 8, "output_tokens": 4, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0, "model_calls": 2}
    assert second == {"input_tokens": 36, "output_tokens": 4, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "model_calls": 2}


def test_missing_usage_and_provider_failure_never_invent_tokens_or_swallow_error():
    from observability.instrumentation import bind_usage, instrument_runtime

    error = RuntimeError("provider unavailable")
    expected = response()
    proxy = instrument_runtime(runtime_for(FakeClient(expected, error))).memory._client
    telemetry = RecordingTelemetry()

    async def exercise():
        with bind_usage() as usage, bind_telemetry(telemetry):
            assert await proxy.messages.create(model="test") is expected
            with pytest.raises(RuntimeError) as caught:
                await proxy.messages.create(model="test")
            assert caught.value is error
            return usage.snapshot()

    assert asyncio.run(exercise()) == {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0, "model_calls": 2}
    assert telemetry.records[0]["updates"]["usage_details"] == {}
    assert telemetry.records[1]["updates"]["status_message"] == "RuntimeError"


def test_agent_tool_generation_nesting_and_structured_evidence_survive_later_llm_failure():
    from observability.instrumentation import instrument_runtime

    tool_use = {"type": "tool_use", "id": "call-1", "name": "search_knowledge_base", "input": {}}
    client = FakeClient(response(content=[tool_use]), RuntimeError("later failure"))
    agent = TechnicalAgent(client, "test")
    agent.set_shared_tools({"search_knowledge_base": make_tool("search_knowledge_base", "test", {},
        lambda request, args: {"success": True, "results": [{"evidence_id": "ev-1", "content": "private"},
            {"evidence_id": "ev-2"}, {"context_ref": {"evidence_id": "ev-3"}}], "can_confirm_refund": False,
            "server_log_checked": False, "missing_fields": ["date"], "secret": "private"})})
    runtime = runtime_for(client)
    runtime.orchestrator._pool = {AgentType.TECHNICAL: [agent]}
    instrument_runtime(runtime)
    telemetry = RecordingTelemetry()

    async def exercise():
        with bind_telemetry(telemetry), telemetry.span("chat"):
            return await agent.handle(req())

    result = asyncio.run(exercise())
    assert result.success is False
    assert result.tools_used == ["search_knowledge_base"]
    trace = result.tool_traces[0]
    assert trace["evidence_ids"] == ["ev-1", "ev-2", "ev-3"]
    assert trace["result_summary"] == {"success": True, "can_confirm_refund": False,
                                        "server_log_checked": False, "missing_fields": ["date"]}
    nodes = {node["name"]: node for node in telemetry.records if node["kind"] != "generation"}
    agent_node = next(node for node in telemetry.records if node["kind"] == "agent")
    tool_node = next(node for node in telemetry.records if node["kind"] == "tool")
    assert agent_node["parent"] is nodes["chat"]
    assert tool_node["parent"] is agent_node
    assert tool_node["updates"]["metadata"]["evidence_ids"] == ["ev-1", "ev-2", "ev-3"]
    assert all(node["parent"] is agent_node for node in telemetry.records if node["kind"] == "generation")
    assert agent_node["updates"]["metadata"]["success"] is False


@pytest.mark.parametrize("name,args", [("unregistered", {}), ("lookup_error_code", {}), ("explode", {})])
def test_tool_observation_preserves_rejected_and_failed_attempts(name, args):
    tool_use = {"type": "tool_use", "id": "bad", "name": name, "input": args}
    agent = TechnicalAgent(FakeClient(response(content=[tool_use]), response()), "test")

    def explode(request, args):
        raise ValueError("unavailable")

    agent.set_shared_tools({"explode": make_tool("explode", "test", {}, explode)})
    telemetry = RecordingTelemetry()

    async def exercise():
        with bind_telemetry(telemetry):
            return await agent.handle(req())

    result = asyncio.run(exercise())
    assert result.success is True
    assert result.tool_traces[0]["success"] is False
    node = next(node for node in telemetry.records if node["kind"] == "tool")
    assert node["updates"]["metadata"]["success"] is False
    assert node["updates"]["level"] == "ERROR"


def test_escalation_agent_and_routing_observations_have_structure_without_llm():
    orchestrator = AgentOrchestrator(api_key="offline")
    telemetry = RecordingTelemetry()

    async def exercise():
        with bind_telemetry(telemetry):
            return await orchestrator.run(req(intent=IntentCategory.HUMAN_HANDOFF))

    result = asyncio.run(exercise())
    assert result.escalated is True
    assert result.agent_type is AgentType.ESCALATION
    route = next(node for node in telemetry.records if node["name"] == "routing")
    assert route["updates"]["metadata"]["primary_agent"] == "escalation"
    escalation = next(node for node in telemetry.records if node["kind"] == "agent")
    assert escalation["metadata"]["agent_type"] == "escalation"
    assert route["parent"] is escalation["parent"]
    assert all(node["kind"] != "generation" for node in telemetry.records)


def test_no_telemetry_keeps_agent_response_and_usage_contract(monkeypatch):
    from observability.instrumentation import bind_usage, instrument_runtime

    monkeypatch.delenv("ECHOMIND_TELEMETRY_ENABLED", raising=False)
    client = FakeClient(response(usage={"input_tokens": 9, "output_tokens": 4}))
    agent = TechnicalAgent(client, "test")
    runtime = runtime_for(client)
    runtime.orchestrator._pool = {AgentType.TECHNICAL: [agent]}
    instrument_runtime(runtime)

    async def exercise():
        with bind_usage() as usage:
            result = await agent.handle(req())
            return result, usage.snapshot()

    result, usage = asyncio.run(exercise())
    assert result.success is True
    assert result.content == "done"
    assert usage["input_tokens"] == 9
    assert usage["model_calls"] == 1


def test_versioned_tool_evidence_keeps_document_ids_separate_and_context_refs_private():
    from observability.instrumentation import tool_result_evidence

    result = tool_result_evidence({"results": [
        {"id": "chunk-0", "evidence_id": "ctx_chunk_hash_v2", "context_ref": {
            "id": "refund", "version": "v2", "scope": "shared", "source": "knowledge",
            "effective_at": "2026-10-26T00:00:00Z", "evidence_id": "ctx_chunk_hash_v2",
            "content": "private document", "user_id": "private user", "embedding": [1, 2]}},
        {"evidence_id": "ctx_chunk_hash_v2", "context_ref": {"id": "refund", "version": "v2"}},
        {"document_id": "invoice", "evidence_id": "ctx_invoice_hash_v1"},
        {"id": "untyped-row-id", "context_ref": "opaque-value"},
    ]})

    assert result["evidence_ids"] == ["ctx_chunk_hash_v2", "ctx_invoice_hash_v1"]
    assert result["document_ids"] == ["refund", "invoice"]
    assert result["context_refs"] == [
        {"id": "refund", "version": "v2", "scope": "shared", "source": "knowledge",
         "effective_at": "2026-10-26T00:00:00Z", "evidence_id": "ctx_chunk_hash_v2"},
        {"id": "refund", "version": "v2"},
    ]
    assert result["result_summary"] == {}


def test_tool_document_references_are_deduplicated_bounded_and_ignore_nested_payloads():
    from observability.instrumentation import tool_result_evidence

    rows = [{"document_id": "x" * 300, "evidence_id": "y" * 300, "context_ref": {
        "id": "x" * 300, "version": {"secret": "private"}, "scope": ["private"],
        "source": "knowledge", "effective_at": None, "evidence_id": "y" * 300}}] * 2
    rows.extend({"document_id": f"doc-{index}", "evidence_id": f"ctx-{index}",
                 "context_ref": {"id": f"doc-{index}", "version": 1}} for index in range(70))
    result = tool_result_evidence({"results": rows})

    assert len(result["document_ids"]) == 50
    assert len(result["evidence_ids"]) == 50
    assert len(result["context_refs"]) == 50
    assert result["document_ids"][0] == "x" * 256
    assert result["evidence_ids"][0] == "y" * 256
    assert result["context_refs"][0] == {"id": "x" * 256, "source": "knowledge",
                                          "evidence_id": "y" * 256}
