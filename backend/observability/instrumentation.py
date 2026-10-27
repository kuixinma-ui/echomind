"""Small runtime boundaries for observations and actual provider token usage.

Counters are request scoped. Asyncio children inherit the same mutable counter,
while each ``bind_usage()`` creates a fresh one. A response snapshot excludes
later online profile work; awaited evaluation profile work is included. No
pricing table or estimated dollar cost is maintained here.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import math
from typing import Any, Iterator

from observability.telemetry import get_telemetry
from observability.budgets import current_budget


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_MODEL_PARAMETERS = ("max_tokens", "temperature", "top_p", "top_k", "stop_sequences")


@dataclass
class UsageCounter:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model_calls: int = 0

    def snapshot(self) -> dict[str, int]:
        """Return a detached total, including attempted provider calls."""
        return {name: getattr(self, name) for name in (*_TOKEN_FIELDS, "model_calls")}


_USAGE: ContextVar[UsageCounter | None] = ContextVar("echomind_usage", default=None)


@contextmanager
def bind_usage() -> Iterator[UsageCounter]:
    counter = UsageCounter()
    token = _USAGE.set(counter)
    try:
        yield counter
    finally:
        _USAGE.reset(token)


def current_usage() -> UsageCounter | None:
    return _USAGE.get()


def _value(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _response_usage(response: Any) -> dict[str, int]:
    usage = _value(response, "usage")
    result = {}
    for name in _TOKEN_FIELDS:
        value = _value(usage, name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
    return result


def _generation_diagnostics(response: Any, max_tokens: Any) -> dict[str, Any]:
    """Record bounded protocol facts; never include text, thinking or tool input."""
    reason = _value(response, 'stop_reason')
    known_reasons = {'end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'pause_turn', 'refusal'}
    reason = reason if type(reason) is str and reason in known_reasons else (
        'not_reported' if reason is None else 'other')
    content = _value(response, 'content')
    blocks = content if isinstance(content, (list, tuple)) else ()
    known_types = {'text', 'tool_use', 'thinking', 'redacted_thinking'}
    block_types, has_text = [], False
    for block in blocks:
        kind = _value(block, 'type')
        kind = kind if type(kind) is str and kind in known_types else 'other'
        if kind not in block_types:
            block_types.append(kind)
        text = _value(block, 'text') if kind == 'text' else None
        has_text = has_text or (type(text) is str and bool(text.strip()))
    output_tokens = _response_usage(response).get('output_tokens')
    reached = type(max_tokens) is int and max_tokens > 0 and type(output_tokens) is int and output_tokens >= max_tokens
    return {'stop_reason': reason, 'block_types': block_types,
            'has_visible_text': has_text, 'max_tokens_reached': reached}


class _MessagesProxy:
    def __init__(self, messages: Any) -> None:
        self._messages = messages

    def __getattr__(self, name: str) -> Any:
        return getattr(self._messages, name)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        budget = current_budget()
        if budget is not None:
            budget.reserve()
        counter = current_usage()
        if counter is not None:
            counter.model_calls += 1
        with get_telemetry().span(
            "anthropic.messages.create", kind="generation",
            input={key: kwargs[key] for key in ("messages", "system") if key in kwargs},
            metadata={"provider": "anthropic"},
        ) as observation:
            observation.update(model=kwargs.get("model"), model_parameters={
                name: kwargs[name] for name in _MODEL_PARAMETERS if name in kwargs
            })
            try:
                remaining = budget.remaining_seconds() if budget is not None else None
                if remaining is None:
                    response = await self._messages.create(*args, **kwargs)
                else:
                    if remaining <= 0:
                        raise budget.stop('deadline')
                    task = asyncio.current_task()
                    cancellations_before = task.cancelling()
                    deadline = asyncio.timeout(remaining)
                    try:
                        async with deadline:
                            response = await self._messages.create(*args, **kwargs)
                    except Exception:
                        if task.cancelling() > cancellations_before:
                            raise asyncio.CancelledError() from None
                        if deadline.expired():
                            raise budget.stop('deadline') from None
                        raise
                    if task.cancelling() > cancellations_before:
                        raise asyncio.CancelledError()
                    # A provider may swallow cancellation and return a response.
                    if deadline.expired():
                        raise budget.stop('deadline')
            except BaseException as error:
                observation.update(level="ERROR", status_message=type(error).__name__)
                raise
            usage = _response_usage(response)
            if counter is not None:
                for name, value in usage.items():
                    setattr(counter, name, getattr(counter, name) + value)
            observation.update(model=_value(response, "model") or kwargs.get("model"),
                               usage_details=usage, output=_value(response, "content"),
                               metadata={'generation_diagnostics': _generation_diagnostics(response, kwargs.get('max_tokens'))})
            return response


class AnthropicClientProxy:
    """Intercept messages.create, retaining the original client lifecycle/API."""
    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = _MessagesProxy(client.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def instrument_runtime(runtime: Any) -> Any:
    """Wrap each distinct runtime Anthropic client once; safe to repeat."""
    orchestrator = getattr(runtime, "orchestrator", None)
    owners = [(getattr(runtime, "memory", None), "_client"),
              (getattr(runtime, "tool_manager", None), "_client"),
              (getattr(orchestrator, "_composer", None), "_client"),
              (getattr(orchestrator, "_intent_recognizer", None), "client")]
    for agents in getattr(orchestrator, "_pool", {}).values():
        owners.extend((agent, "_client") for agent in agents)
    wrapped: dict[int, AnthropicClientProxy] = {}
    for owner, attribute in owners:
        client = getattr(owner, attribute, None)
        if isinstance(client, AnthropicClientProxy):
            wrapped[id(client._client)] = client
    for owner, attribute in owners:
        client = getattr(owner, attribute, None)
        if client is None or isinstance(client, AnthropicClientProxy):
            continue
        # Test runtimes and disabled providers may expose no Anthropic resource.
        if not callable(getattr(getattr(client, "messages", None), "create", None)):
            continue
        proxy = wrapped.get(id(client))
        if proxy is None:
            proxy = wrapped[id(client)] = AnthropicClientProxy(client)
        setattr(owner, attribute, proxy)
    return runtime


def observe_agent(function):
    @wraps(function)
    async def observed(self, req, *args, **kwargs):
        agent_type = self.agent_type.value
        with get_telemetry().span(f"agent.{agent_type}", kind="agent", input=req.message,
                                  metadata={"agent_type": agent_type, "request_id": req.request_id}) as observation:
            response = await function(self, req, *args, **kwargs)
            observation.update(output=response.content, metadata={"agent_type": agent_type,
                "success": response.success, "escalated": response.escalate,
                "tools_used": list(response.tools_used)}, level="DEFAULT" if response.success else "ERROR")
            return response
    return observed


def observe_orchestration(function):
    @wraps(function)
    async def observed(self, req, *args, **kwargs):
        with get_telemetry().span(function.__name__, kind="chain", metadata={"request_id": req.request_id}):
            return await function(self, req, *args, **kwargs)
    return observed


def observe_routing(function):
    @wraps(function)
    def observed(self, req, *args, **kwargs):
        with get_telemetry().span("routing", kind="chain", metadata={
            "intent": req.intent.value if req.intent else None,
            "urgency": req.urgency.name if req.urgency else None,
        }) as observation:
            decision = function(self, req, *args, **kwargs)
            observation.update(metadata={"primary_agent": decision.primary_agent.value,
                "supporting_agents": [agent.value for agent in decision.supporting_agents],
                "confidence": decision.confidence, "reason": decision.reason})
            return decision
    return observed


def tool_result_evidence(result: Any) -> dict[str, Any]:
    """Export bounded IDs and selected business facts, never raw tool results."""
    evidence_ids = []
    document_ids = []
    context_refs = []
    summary = {}
    if isinstance(result, dict):
        for row in result.get("results", []) if isinstance(result.get("results"), list) else []:
            if not isinstance(row, dict):
                continue
            reference = row.get("context_ref")
            reference = reference if isinstance(reference, dict) else {}
            # A document ID names the source. An evidence ID names the selected
            # version/chunk; never replace it with an untyped result row ID.
            for identifiers, identifier in (
                (evidence_ids, row.get("evidence_id") or reference.get("evidence_id")),
                (document_ids, reference.get("id") or row.get("document_id")),
            ):
                if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                    identifier = str(identifier)[:256]
                    if identifier and identifier not in identifiers and len(identifiers) < 50:
                        identifiers.append(identifier)
            safe_reference = {}
            for key in ("id", "version", "scope", "source", "effective_at", "evidence_id"):
                value = reference.get(key)
                if isinstance(value, str):
                    safe_reference[key] = value[:256]
                elif isinstance(value, int) and not isinstance(value, bool):
                    safe_reference[key] = value
            if safe_reference and safe_reference not in context_refs and len(context_refs) < 50:
                context_refs.append(safe_reference)
            if all(len(values) >= 50 for values in (evidence_ids, document_ids, context_refs)):
                break
        for key in ("success", "can_confirm_refund", "server_log_checked", "reproduced",
                    "context_available", "sensitive_data_required"):
            if isinstance(result.get(key), bool):
                summary[key] = result[key]
        for key in ("missing_fields", "required_fields"):
            if isinstance(result.get(key), list):
                summary[key] = [value[:120] for value in result[key][:50] if isinstance(value, str)]
        if isinstance(result.get("fields"), dict):
            summary["fields"] = {key[:80]: value for key, value in list(result["fields"].items())[:50]
                                 if isinstance(key, str) and isinstance(value, bool)}
        for key in ("difference", "amount_a", "amount_b"):
            value = result.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                summary[key] = value
        for key in ("triage", "status"):
            if isinstance(result.get(key), str):
                summary[key] = result[key][:80]
    return {"evidence_ids": evidence_ids, "document_ids": document_ids,
            "context_refs": context_refs, "result_summary": summary}
