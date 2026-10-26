"""Real provider data types at the telemetry boundary; no model or server calls."""

from datetime import datetime, timezone
import json
import socket
from uuid import uuid4

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
import pytest

from tests.test_telemetry import FakeClient, load_telemetry


@pytest.mark.parametrize("trace_content", [False, True])
def test_completion_start_time_preserves_sdk_datetime_type(monkeypatch, trace_content):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    timestamp = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
    adapter = telemetry.LangfuseTelemetry(client, trace_content=trace_content)

    with adapter.span("llm", kind="generation") as observation:
        observation.update(completion_start_time=timestamp, metadata={"timestamp": timestamp})

    exported = client.observations[0].updates[0]
    assert exported["completion_start_time"] is timestamp
    # A typed SDK field does not make arbitrary metadata objects exportable.
    assert exported["metadata"] == {"timestamp": "[REDACTED]"}


@pytest.mark.parametrize("timestamp", ["2026-10-26T08:30:00Z", {"secret": "sk-hidden"}])
def test_invalid_completion_start_time_is_omitted_without_dropping_other_fields(monkeypatch, timestamp):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()

    with telemetry.LangfuseTelemetry(client).span("llm", kind="generation") as observation:
        observation.update(completion_start_time=timestamp, usage_details={"input_tokens": 2})

    assert client.observations[0].updates == [{"usage_details": {"input_tokens": 2}}]


@pytest.mark.parametrize("trace_content", [False, True])
def test_anthropic_message_blocks_export_only_allowlisted_sanitized_content(monkeypatch, trace_content):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()
    response = Message(
        id="msg-contract", type="message", role="assistant", model="test-model",
        stop_reason="tool_use", usage=Usage(input_tokens=2, output_tokens=3),
        content=[
            TextBlock(type="text", text="Hello alice@example.com 13812345678; Bearer hidden-token",
                      undocumented_field="extra private body"),
            ToolUseBlock(type="tool_use", id="toolu-contract", name="lookup",
                         input={"user_id": "alice", "api_key": "sk-secret", "city": "北京"},
                         undocumented_field="extra private body"),
        ],
    )
    adapter = telemetry.LangfuseTelemetry(client, trace_content=trace_content, hash_salt="test-salt")
    with adapter.span("llm", kind="generation", input={"content": response.content},
                      metadata={"api_key": response.content[0], "content": response.content}) as observation:
        observation.update(output=response.content)

    node = client.observations[0]
    assert node.fields["metadata"]["api_key"] == "[REDACTED]"
    if trace_content:
        blocks = node.updates[0]["output"]
        assert blocks[0] == {"type": "text", "text": "Hello [REDACTED] [REDACTED]; [REDACTED]"}
        assert blocks[1] == {
            "type": "tool_use", "id": "toolu-contract", "name": "lookup",
            "input": {"user_id": "hmac-sha256:b65aaf31e9e8f7d60cd4671c1bffb8b94580a8b176189ac9f31bae7c2e54cf37",
                      "api_key": "[REDACTED]", "city": "北京"},
        }
        assert node.fields["input"] == {"content": blocks}
        assert node.fields["metadata"]["content"] == blocks
    else:
        assert node.fields["input"] is None
        assert node.updates == []
        assert node.fields["metadata"]["content"] == "[REDACTED]"

    exported = json.dumps([node.fields, node.updates], ensure_ascii=False)
    for private in ("alice@example.com", "13812345678", "hidden-token", "sk-secret", "extra private body"):
        assert private not in exported
    assert response.content[0].text == "Hello alice@example.com 13812345678; Bearer hidden-token"
    assert response.content[1].input["api_key"] == "sk-secret"


def test_content_opt_in_does_not_call_arbitrary_object_serializers(monkeypatch):
    telemetry = load_telemetry(monkeypatch)
    client = FakeClient()

    class Untrusted:
        type = "text"
        text = "private body"

        def model_dump(self, **kwargs):
            pytest.fail("Arbitrary object serializer must not run")

        def __repr__(self):
            pytest.fail("Arbitrary object repr must not run")

    with telemetry.LangfuseTelemetry(client, trace_content=True).span("llm") as observation:
        observation.update(output=[Untrusted()])

    assert client.observations[0].updates == [{"output": ["[REDACTED]"]}]


@pytest.fixture
def real_langfuse_boundary(monkeypatch):
    langfuse = pytest.importorskip("langfuse", reason="Optional exporter SDK is not installed")
    import httpx
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    def forbidden_connection(*args, **kwargs):
        pytest.fail("SDK contract tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden_connection)
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_MEDIA_UPLOAD_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_DEBUG", "false")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(202, json={"message": "Accepted"})

    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    client = langfuse.Langfuse(
        public_key="pk-sdk-contract-" + uuid4().hex, secret_key="sk-sdk-contract",
        base_url="https://langfuse.invalid", debug=False,
        environment="sdk-contract", release="sdk-contract", flush_at=1, flush_interval=0.01,
        tracer_provider=provider, span_exporter=exporter, httpx_client=http_client,
    )
    try:
        yield client, exporter, requests
    finally:
        client.shutdown()
        provider.shutdown()
        http_client.close()


@pytest.mark.parametrize("trace_content", [False, True])
def test_real_langfuse_serializes_content_datetime_and_score_without_private_errors(
    monkeypatch, real_langfuse_boundary, trace_content,
):
    telemetry = load_telemetry(monkeypatch)
    client, exporter, requests = real_langfuse_boundary
    adapter = telemetry.LangfuseTelemetry(client, trace_content=trace_content, hash_salt="test-salt")
    timestamp = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
    with adapter.span("sdk.request", metadata={"run_id": "contract-run", "user_id": "alice"}):
        with adapter.span("sdk.generation", kind="generation", input={"message": "Meaningful request"}) as observation:
            observation.update(
                completion_start_time=timestamp, model="test-model",
                usage_details={"input_tokens": 2, "output_tokens": 3},
                output=[TextBlock(type="text", text="Meaningful answer alice@example.com; Bearer private-key",
                                  undocumented_field="extra private body")],
            )
            observation.score("quality", 0.75)
        with pytest.raises(ValueError, match="customer private body"):
            with adapter.span("sdk.error", kind="tool"):
                raise ValueError("customer private body sk-hidden")
    adapter.flush()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"sdk.request", "sdk.generation", "sdk.error"}
    root, generation, error = (spans[name] for name in ("sdk.request", "sdk.generation", "sdk.error"))
    assert root.parent is None
    assert generation.parent.span_id == error.parent.span_id == root.context.span_id
    assert generation.context.trace_id == root.context.trace_id == error.context.trace_id
    attributes = generation.attributes
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["langfuse.observation.model.name"] == "test-model"
    assert attributes["langfuse.observation.completion_start_time"] == '"2026-10-26T08:30:00Z"'
    assert json.loads(attributes["langfuse.observation.usage_details"]) == {"input_tokens": 2, "output_tokens": 3}
    assert json.loads(root.attributes["langfuse.observation.metadata.run_id"]) == "contract-run"
    assert json.loads(root.attributes["langfuse.observation.metadata.user_id"]).startswith("hmac-sha256:")
    if trace_content:
        assert json.loads(attributes["langfuse.observation.input"]) == {"message": "Meaningful request"}
        assert json.loads(attributes["langfuse.observation.output"]) == [
            {"type": "text", "text": "Meaningful answer [REDACTED]; [REDACTED]"},
        ]
    else:
        assert "langfuse.observation.input" not in attributes
        assert "langfuse.observation.output" not in attributes
    assert error.attributes["langfuse.observation.status_message"] == "ValueError"
    assert not error.events

    assert len(requests) == 1
    assert requests[0].url.path == "/api/public/scores"
    score = json.loads(requests[0].content)[0]
    assert score["name"] == "quality" and score["value"] == 0.75
    assert score["traceId"] == format(root.context.trace_id, "032x")
    assert score["observationId"] == format(generation.context.span_id, "016x")
    exported = json.dumps([dict(span.attributes) for span in spans.values()]) + requests[0].content.decode()
    for private in ("alice@example.com", "private-key", "customer private body", "sk-hidden", "extra private body"):
        assert private not in exported
