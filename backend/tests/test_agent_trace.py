"""Offline regressions for per-request tool execution and orchestration evidence."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agents.agent_orchestrator import (
    AgentOrchestrator,
    AgentType,
    BillingAgent,
    GeneralAgent,
    Request,
    TechnicalAgent,
)
from agents.tools import make_tool
from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel


def text_response(text="处理完成"):
    return SimpleNamespace(content=[{"type": "text", "text": text}])


def tool_response(name="lookup_error_code", args=None, call_id="tool_1"):
    return SimpleNamespace(content=[{
        "type": "tool_use", "id": call_id, "name": name,
        "input": {"error_code": "401"} if args is None else args,
    }])


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def request(**kwargs):
    values = {
        "message": "登录报错 401", "user_id": "u1", "conv_id": "c1",
        "intent": IntentCategory.TECHNICAL_LOGIN, "intent_group": "technical",
        "urgency": UrgencyLevel.LOW,
    }
    values.update(kwargs)
    return Request(**values)


@pytest.mark.parametrize("name,args,error", [
    ("issue_refund", {}, "白名单"),
    ("lookup_error_code", {"error_code": "401", "secret": "nope"}, "不允许"),
    ("lookup_error_code", {}, "缺少必需参数"),
    ("lookup_error_code", "invalid", "JSON 对象"),
    ("lookup_error_code", [], "JSON 对象"),
])
def test_rejected_tool_attempt_keeps_failure_trace_and_returns_error_to_model(name, args, error):
    client = ScriptedClient(tool_response(name, args), text_response())
    result = asyncio.run(TechnicalAgent(client, "test-model").handle(request()))

    assert result.success is True
    assert result.tools_used == []
    assert len(result.tool_traces) == 1
    trace = result.tool_traces[0]
    assert trace["tool_use_id"] == "tool_1"
    assert trace["tool_name"] == name
    assert trace["input"] == args
    assert trace["success"] is False
    assert error in trace["error"]
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tool_1"
    assert json.loads(tool_result["content"])["success"] is False


@pytest.mark.parametrize("raises", [True, False])
def test_executed_tool_distinguishes_execution_error_from_unsuccessful_business_result(raises):
    def handler(req, args):
        if raises:
            raise RuntimeError("backend unavailable")
        return {"success": False, "error": "record missing", "cached": True, "reranked": True}

    client = ScriptedClient(tool_response("check_record", {}), text_response())
    agent = TechnicalAgent(client, "test-model")
    agent.set_shared_tools({"check_record": make_tool("check_record", "读取记录", {}, handler)})

    result = asyncio.run(agent.handle(request()))

    assert result.success is True
    assert result.tools_used == ["check_record"]
    assert len(result.tool_traces) == 1
    trace = result.tool_traces[0]
    assert trace["success"] is (not raises)
    assert trace["result_success"] is (None if raises else False)
    assert trace["error"] == ("backend unavailable" if raises else "record missing")
    assert trace["cached"] is (not raises)
    assert trace["reranked"] is (not raises)
    assert json.loads(client.calls[1]["messages"][-1]["content"][0]["content"])["success"] is False


def test_model_failure_after_tool_execution_retains_completed_tool_evidence():
    client = ScriptedClient(tool_response(), RuntimeError("provider unavailable"))

    result = asyncio.run(TechnicalAgent(client, "test-model").handle(request()))

    assert result.success is False
    assert result.tools_used == ["lookup_error_code"]
    assert len(result.tool_traces) == 1
    assert result.tool_traces[0]["tool_use_id"] == "tool_1"
    assert result.tool_traces[0]["success"] is True


def test_maximum_tool_rounds_preserve_all_attempts_and_stop_requesting_model():
    client = ScriptedClient(*(tool_response(call_id=f"call_{i}") for i in range(4)))

    result = asyncio.run(TechnicalAgent(client, "test-model").handle(request()))

    assert result.success is False
    assert len(client.calls) == 3
    assert result.tools_used == ["lookup_error_code"] * 3
    assert [trace["tool_use_id"] for trace in result.tool_traces] == ["call_0", "call_1", "call_2"]


def test_interleaved_requests_share_agent_without_sharing_tool_evidence():
    async def exercise():
        a_waiting = asyncio.Event()
        b_done = asyncio.Event()

        class InterleavedClient:
            messages = None

            def __init__(self):
                self.messages = self
                self.rounds = {"request-A": 0, "request-B": 0}

            async def create(self, **kwargs):
                marker = next(message["content"] for message in kwargs["messages"]
                              if message["content"] in ("request-A", "request-B"))
                index = self.rounds[marker]
                self.rounds[marker] += 1
                if marker == "request-A":
                    if index == 0:
                        return tool_response(args={"error_code": "401"}, call_id="a_call")
                    a_waiting.set()
                    await b_done.wait()
                    return text_response("A finished")
                return tool_response(args={"error_code": "500"}, call_id=f"b_call_{index}")

        agent = TechnicalAgent(InterleavedClient(), "test-model")
        task_a = asyncio.create_task(agent.handle(request(message="request-A")))
        await a_waiting.wait()
        result_b = await agent.handle(request(message="request-B", user_id="u2", conv_id="c2"))
        b_done.set()
        result_a = await task_a
        return result_a, result_b

    result_a, result_b = asyncio.run(asyncio.wait_for(exercise(), timeout=2))

    assert result_a.success is True
    assert result_b.success is False
    assert result_a.tools_used == ["lookup_error_code"]
    assert result_b.tools_used == ["lookup_error_code"] * 3
    assert [trace["tool_use_id"] for trace in result_a.tool_traces] == ["a_call"]
    assert [trace["input"] for trace in result_a.tool_traces] == [{"error_code": "401"}]
    assert [trace["tool_use_id"] for trace in result_b.tool_traces] == ["b_call_0", "b_call_1", "b_call_2"]
    assert all(trace["input"] == {"error_code": "500"} for trace in result_b.tool_traces)


@pytest.mark.parametrize("initial_entities", [{}, {"date": ["2026-10-25"]}])
def test_direct_orchestration_passes_recognized_entities_to_role_and_tool(initial_entities):
    class Recognizer:
        async def recognize(self, message, history=None):
            return IntentResult(
                intent=IntentCategory.REFUND, confidence=0.95, urgency=UrgencyLevel.LOW,
                intent_group="billing", entities={"order_id": ["ORDER123"], "amount": ["99 元"]},
                reasoning="fixture", latency_ms=1,
            )

    client = ScriptedClient(tool_response("check_billing_fields", {}), text_response())
    orchestrator = AgentOrchestrator(api_key="offline-test")
    orchestrator._intent_recognizer = Recognizer()
    orchestrator._pool[AgentType.BILLING] = [BillingAgent(client, "test-model")]
    req = request(message="申请退款", intent=None, entities=deepcopy(initial_entities))

    result = asyncio.run(orchestrator.run(req))

    assert result.agent_type is AgentType.BILLING
    assert req.entities == {"order_id": ["ORDER123"], "amount": ["99 元"], **initial_entities}
    role_packet = next(message["content"] for message in client.calls[0]["messages"]
                       if message["content"].startswith("[角色输入契约]"))
    assert json.loads(role_packet.split("\n", 1)[1])["verification_fields"]["order_id"] == ["ORDER123"]
    tool_result = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"])
    assert tool_result["fields"]["order_id"] is True
    assert tool_result["fields"]["amount"] is True
    assert len(result.tool_traces) == 1
    assert orchestrator.get_tool_trace(result.request_id)["tool_calls"] == result.tool_traces


def test_orchestrator_fallback_keeps_failed_primary_and_successful_fallback_traces():
    orchestrator = AgentOrchestrator(api_key="offline-test")
    technical_client = ScriptedClient(tool_response(), RuntimeError("provider unavailable"))
    general_client = ScriptedClient(tool_response("inspect_request_context", {}), text_response("fallback"))
    orchestrator._pool[AgentType.TECHNICAL] = [TechnicalAgent(technical_client, "test-model")]
    orchestrator._pool[AgentType.GENERAL] = [GeneralAgent(general_client, "test-model")]

    result = asyncio.run(orchestrator.run(request()))

    assert result.response == "fallback"
    assert result.tools_used == ["lookup_error_code", "inspect_request_context"]
    assert [(trace["agent_type"], trace["tool_name"]) for trace in result.tool_traces] == [
        ("technical", "lookup_error_code"), ("general", "inspect_request_context"),
    ]
