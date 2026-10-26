"""Offline boundary tests; the SDK double never creates a network client."""

import asyncio
import builtins
from contextvars import ContextVar
import importlib
import json
import logging
import sys
from types import SimpleNamespace

import pytest


def load_telemetry(monkeypatch):
    for key in (
        "ECHOMIND_TELEMETRY_ENABLED", "ECHOMIND_TRACE_CONTENT",
        "ECHOMIND_TELEMETRY_HASH_SALT", "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    telemetry = importlib.import_module("observability.telemetry")
    monkeypatch.setattr(telemetry, "_DEFAULT", None)
    monkeypatch.setattr(telemetry, "_BOUND", ContextVar("test_telemetry", default=None))
    return telemetry


class FakeObservation:
    def __init__(self, client, fields, parent):
        self.client = client
        self.fields = fields
        self.parent = parent
        self.updates = []
        self.scores = []

    def update(self, **fields):
        self.client.fail("update")
        self.updates.append(fields)

    def score(self, *, name, value):
        self.client.fail("score")
        self.scores.append((name, value))


class FakeClient:
    """Model v5's current-observation context and exception capture boundary."""

    def __init__(self, failure=None):
        self.failure = failure
        self.current = ContextVar("fake_current", default=None)
        self.observations = []
        self.exits = []
        self.flushed = 0
        self.stopped = 0

    def fail(self, step):
        if self.failure == step:
            raise RuntimeError("SDK failure containing sk-secret and customer body")

    def start_as_current_observation(self, *, name, as_type, input=None, metadata=None):
        self.fail("start")
        client = self
        observation = FakeObservation(
            client, {"name": name, "as_type": as_type, "input": input, "metadata": metadata},
            self.current.get(),
        )

        class Current:
            def __enter__(self):
                client.fail("enter")
                self.token = client.current.set(observation)
                client.observations.append(observation)
                return observation

            def __exit__(self, exc_type, exc, traceback):
                client.exits.append((exc_type, exc, traceback))
                client.current.reset(self.token)
                client.fail("exit")
                return client.failure == "suppress"

        return Current()

    def flush(self):
        self.fail("flush")
        self.flushed += 1

    def shutdown(self):
        self.fail("shutdown")
        self.stopped += 1


def test_disabled_does_not_import_sdk_or_read_credentials(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "langfuse" or name.startswith("langfuse."):
            pytest.fail("Disabled telemetry imported the optional SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    telemetry = load_telemetry(monkeypatch)
    getenv = telemetry.os.getenv

    def guarded_getenv(name, *args):
        if name.startswith("LANGFUSE_"):
            pytest.fail("Disabled telemetry read a credential")
        return getenv(name, *args)

    monkeypatch.setattr(telemetry.os, "getenv", guarded_getenv)
    adapter = telemetry.get_telemetry()
    assert isinstance(adapter, telemetry.NoopTelemetry)
    with adapter.span("request", input="private") as observation:
        observation.update(output="private")
        observation.score("quality", 1.0)
    adapter.flush()
    adapter.shutdown()


def test_enabled_without_sdk_falls_back_with_safe_diagnostic(monkeypatch, caplog):
    telemetry = load_telemetry(monkeypatch)
    monkeypatch.setenv("ECHOMIND_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-secret")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-secret")
    monkeypatch.setitem(sys.modules, "langfuse", None)
    with caplog.at_level(logging.WARNING):
        adapter = telemetry.get_telemetry()
    assert isinstance(adapter, telemetry.NoopTelemetry)
    assert "ModuleNotFoundError" in caplog.text
    assert "sk-secret" not in caplog.text


def test_missing_configuration_does_not_load_sdk(monkeypatch, caplog):
    telemetry = load_telemetry(monkeypatch)
    monkeypatch.setenv("ECHOMIND_TELEMETRY_ENABLED", "true")
    monkeypatch.setitem(sys.modules, "langfuse", None)
    with caplog.at_level(logging.WARNING):
        assert isinstance(telemetry.get_telemetry(), telemetry.NoopTelemetry)
    assert "configuration" in caplog.text
    assert "ModuleNotFoundError" not in caplog.text


def test_enabled_initializes_v5_once_with_explicit_configuration(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    configurations = []

    def sdk_factory(**kwargs):
        configurations.append(kwargs)
        return client

    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=sdk_factory))
    monkeypatch.setenv("ECHOMIND_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://telemetry.invalid")
    monkeypatch.setenv("ECHOMIND_TELEMETRY_HASH_SALT", "test-salt")
    monkeypatch.setenv("ECHOMIND_TRACE_CONTENT", "true")
    first = telemetry.get_telemetry()
    assert telemetry.get_telemetry() is first
    assert configurations == [{
        "public_key": "pk-test", "secret_key": "sk-test",
        "base_url": "https://telemetry.invalid", "debug": False,
    }]
    with first.span("llm", kind="generation", input="explicit content"):
        pass
    assert client.observations[0].fields["input"] == "explicit content"


def test_default_export_hides_content_and_recursively_redacts_metadata(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    adapter = telemetry.LangfuseTelemetry(client, hash_salt="test-salt")
    metadata = {
        "user_id": "alice", "conv_id": "conversation-1", "run_id": "run-1",
        "nested": [{"token": "bearer-secret", "email": "alice@example.com",
                    "message": "private customer message", "count": 2}],
        "error": ValueError("private error text"),
    }
    with adapter.span("request", input={"question": "private customer message"}, metadata=metadata) as obs:
        obs.update(output="private assistant response", metadata={"api_key": "sk-secret", "success": True})
    fields = client.observations[0].fields
    assert fields["input"] is None
    exported = fields["metadata"]
    assert exported["user_id"] == "hmac-sha256:b65aaf31e9e8f7d60cd4671c1bffb8b94580a8b176189ac9f31bae7c2e54cf37"
    assert exported["conv_id"] != "conversation-1"
    assert exported["run_id"] == "run-1"
    assert exported["nested"][0] == {"token": "[REDACTED]", "email": "[REDACTED]", "message": "[REDACTED]", "count": 2}
    assert exported["error"] == "ValueError"
    assert "output" not in client.observations[0].updates[0]
    assert client.observations[0].updates[0]["metadata"] == {"api_key": "[REDACTED]", "success": True}
    serialized = json.dumps([fields, client.observations[0].updates])
    for secret in ("alice@example.com", "private", "bearer-secret", "sk-secret", "conversation-1"):
        assert secret not in serialized
    assert metadata["user_id"] == "alice"


def test_content_opt_in_still_redacts_credentials_identifiers_and_errors(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    adapter = telemetry.LangfuseTelemetry(client, trace_content=True, hash_salt="test-salt")
    with adapter.span("llm", kind="generation", input={"text": "hello", "password": "secret"}) as obs:
        obs.update(output={"text": "world", "user_id": "alice"}, status_message=ValueError("secret"),
                   model="test-model", usage_details={"input": 2, "output": 3})
        obs.score("quality", 0.8)
    observation = client.observations[0]
    assert observation.fields["as_type"] == "generation"
    assert observation.fields["input"] == {"text": "hello", "password": "[REDACTED]"}
    assert observation.updates[0]["output"]["text"] == "world"
    assert observation.updates[0]["output"]["user_id"].startswith("hmac-sha256:")
    assert observation.updates[0]["status_message"] == "ValueError"
    assert observation.updates[0]["usage_details"] == {"input": 2, "output": 3}
    assert observation.scores == [("quality", 0.8)]


def test_identifier_hash_uses_configured_salt(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    clients = [FakeClient(), FakeClient(), FakeClient()]
    for client, salt in zip(clients, ["same", "same", "different"]):
        with telemetry.LangfuseTelemetry(client, hash_salt=salt).span("request", metadata={"user_id": "alice"}):
            pass
    ids = [client.observations[0].fields["metadata"]["user_id"] for client in clients]
    assert ids[0] == ids[1]
    assert ids[0] != ids[2]


def test_nested_content_key_variants_and_free_text_contacts_do_not_leak(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    with telemetry.LangfuseTelemetry(client).span("request", metadata={
        "nested": {"last_user_message": "private body", "tool_response": {"text": "private body"}},
        "note": "Contact alice@example.com or 13812345678; Bearer abc-secret",
        "input_tokens": 42,
    }):
        pass
    metadata = client.observations[0].fields["metadata"]
    assert metadata["nested"] == {"last_user_message": "[REDACTED]", "tool_response": "[REDACTED]"}
    assert metadata["note"] == "Contact [REDACTED] or [REDACTED]; [REDACTED]"
    assert metadata["input_tokens"] == 42


def test_enabled_sdk_constructor_failure_is_safe(monkeypatch, caplog):
    telemetry = load_telemetry(monkeypatch)
    monkeypatch.setenv("ECHOMIND_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    def broken_sdk(**kwargs):
        raise RuntimeError("sk-test customer body")

    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=broken_sdk))
    with caplog.at_level(logging.WARNING):
        assert isinstance(telemetry.get_telemetry(), telemetry.NoopTelemetry)
    assert "RuntimeError" in caplog.text
    assert "sk-test" not in caplog.text
    assert "customer body" not in caplog.text


def test_parallel_roots_on_shared_adapter_do_not_become_each_others_children(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    adapter = telemetry.LangfuseTelemetry(client)

    async def request(name):
        with telemetry.bind_telemetry(adapter):
            with telemetry.get_telemetry().span(name):
                await asyncio.sleep(0)
                with telemetry.get_telemetry().span(name + ".child"):
                    await asyncio.sleep(0)

    async def run():
        await asyncio.gather(request("left"), request("right"))

    asyncio.run(run())
    observations = {observation.fields["name"]: observation for observation in client.observations}
    for name in ("left", "right"):
        assert observations[name].parent is None
        assert observations[name + ".child"].parent is observations[name]
    assert client.current.get() is None


@pytest.mark.parametrize("failure", ["start", "enter", "update", "score", "exit", "flush", "shutdown"])
def test_sdk_errors_do_not_interrupt_business_or_log_payload(monkeypatch, caplog, failure):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient(failure)
    adapter = telemetry.LangfuseTelemetry(client)
    business_completed = False
    with caplog.at_level(logging.WARNING):
        with adapter.span("request") as observation:
            observation.update(output="private", metadata={"completed": True})
            observation.score("quality", 1)
            business_completed = True
        adapter.flush()
        adapter.shutdown()
    assert business_completed
    assert "RuntimeError" in caplog.text
    assert "sk-secret" not in caplog.text
    assert "customer body" not in caplog.text


@pytest.mark.parametrize("failure", [None, "start", "enter", "update", "exit", "suppress"])
def test_business_exception_propagates_without_message_or_stack_export(monkeypatch, failure):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient(failure)
    adapter = telemetry.LangfuseTelemetry(client)
    error = ValueError("customer private body")
    with pytest.raises(ValueError) as raised:
        with adapter.span("request"):
            raise error
    assert raised.value is error
    assert all(exit_args == (None, None, None) for exit_args in client.exits)
    exported = [observation.updates for observation in client.observations]
    assert "customer private body" not in json.dumps(exported)
    if failure in (None, "exit", "suppress"):
        assert exported[0] == [{"level": "ERROR", "status_message": "ValueError", "metadata": {"error_type": "ValueError"}}]


def test_nested_observations_and_parallel_bindings_keep_request_parent(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    left, right = FakeClient(), FakeClient()
    left_adapter, right_adapter = telemetry.LangfuseTelemetry(left), telemetry.LangfuseTelemetry(right)

    async def request(adapter, name):
        with telemetry.bind_telemetry(adapter):
            assert telemetry.get_telemetry() is adapter
            with telemetry.get_telemetry().span(name):
                await asyncio.sleep(0)
                with telemetry.get_telemetry().span("child", kind="tool"):
                    await asyncio.sleep(0)
            assert adapter._client.current.get() is None

    async def run():
        await asyncio.gather(request(left_adapter, "left"), request(right_adapter, "right"))

    asyncio.run(run())
    for client, name in ((left, "left"), (right, "right")):
        assert client.observations[0].fields["name"] == name
        assert client.observations[0].parent is None
        assert client.observations[1].parent is client.observations[0]
    assert isinstance(telemetry.get_telemetry(), telemetry.NoopTelemetry)


def test_binding_restores_outer_adapter_after_business_exception(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    outer, inner = telemetry.NoopTelemetry(), telemetry.NoopTelemetry()
    with telemetry.bind_telemetry(outer):
        with pytest.raises(RuntimeError):
            with telemetry.bind_telemetry(inner):
                assert telemetry.get_telemetry() is inner
                raise RuntimeError("failure")
        assert telemetry.get_telemetry() is outer
    assert telemetry.get_telemetry() is not outer


def test_flush_and_shutdown_delegate_to_enabled_client(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    adapter = telemetry.LangfuseTelemetry(client)
    adapter.flush()
    adapter.shutdown()
    assert (client.flushed, client.stopped) == (1, 1)
