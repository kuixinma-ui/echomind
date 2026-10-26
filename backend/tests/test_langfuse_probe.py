"""Offline protocol tests: real exporter SDK, memory spans and mock HTTP only."""
import asyncio
import importlib
import json
import sys
from uuid import uuid4

import httpx
import pytest


CONFIG = {'LANGFUSE_BASE_URL': 'http://127.0.0.1:13000',
          'LANGFUSE_PUBLIC_KEY': 'test-public-credential',
          'LANGFUSE_SECRET_KEY': 'test-private-credential'}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def module():
    return importlib.import_module('evaluation.langfuse_probe')


@pytest.mark.parametrize('config', [{}, {'LANGFUSE_PUBLIC_KEY': 'private'},
                                    {k: v for k, v in CONFIG.items() if k != 'LANGFUSE_BASE_URL'},
                                    {**CONFIG, 'LANGFUSE_BASE_URL': 'https://cloud.langfuse.com'},
                                    {**CONFIG, 'LANGFUSE_BASE_URL': 'http://user:private@localhost:13000'},
                                    {**CONFIG, 'LANGFUSE_BASE_URL': 'http://localhost:13000/?secret=private'}])
def test_missing_or_unapproved_configuration_never_connects(config):
    calls = []
    def forbidden(request):
        calls.append(request)
        raise AssertionError('Unconfigured probes must not connect')
    result = asyncio.run(module().probe_langfuse(config, transport=httpx.MockTransport(forbidden)))
    assert result['status'] == 'blocked'
    assert result['model_requests'] == 0
    assert not calls
    assert 'private' not in json.dumps(result)


def test_missing_optional_sdk_is_blocked_before_any_request(monkeypatch):
    monkeypatch.setitem(sys.modules, 'langfuse', None)
    def forbidden(request):
        pytest.fail('Missing optional SDK must not access the API')
    result = asyncio.run(module().probe_langfuse(CONFIG, transport=httpx.MockTransport(forbidden)))
    assert result['status'] == 'blocked'
    assert result['reason'] == 'missing_sdk'


@pytest.fixture
def sdk_boundary(monkeypatch):
    langfuse = pytest.importorskip('langfuse')
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    real_client = langfuse.Langfuse
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    score_bodies = []
    clients = []
    monkeypatch.setenv('LANGFUSE_TRACING_ENABLED', 'true')
    monkeypatch.setenv('OTEL_SDK_DISABLED', 'false')
    monkeypatch.setenv('LANGFUSE_MEDIA_UPLOAD_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_DEBUG', 'false')

    def scores(request):
        assert request.url.path == '/api/public/scores'
        score_bodies.extend(json.loads(request.content))
        return httpx.Response(202, json={'message': 'Accepted'})

    def construct(**kwargs):
        # SDK resources are process singletons keyed by public key, including
        # after shutdown. A unique fictional identifier isolates each fixture.
        kwargs['public_key'] = 'test-public-' + uuid4().hex
        kwargs['span_exporter'] = exporter
        kwargs['tracer_provider'] = provider
        kwargs['httpx_client'] = httpx.Client(transport=httpx.MockTransport(scores))
        result = real_client(**kwargs)
        clients.append((result, kwargs['httpx_client']))
        return result

    monkeypatch.setattr(langfuse, 'Langfuse', construct)

    def wire_observations():
        rows = []
        for span in exporter.get_finished_spans():
            attributes = span.attributes
            metadata = {key.removeprefix('langfuse.observation.metadata.'): json.loads(value)
                        for key, value in attributes.items()
                        if key.startswith('langfuse.observation.metadata.')}
            rows.append({'id': format(span.context.span_id, '016x'),
                         'traceId': format(span.context.trace_id, '032x'),
                         'projectId': 'probe-project',
                         'parentObservationId': format(span.parent.span_id, '016x') if span.parent else None,
                         'isRootObservation': span.parent is None,
                         'type': attributes['langfuse.observation.type'].upper(),
                         'name': span.name, 'level': 'DEFAULT', 'statusMessage': None,
                         'startTime': '2026-10-26T00:00:00Z', 'endTime': '2026-10-26T00:00:01Z',
                         'completionStartTime': json.loads(attributes['langfuse.observation.completion_start_time'])
                         if 'langfuse.observation.completion_start_time' in attributes else None,
                         'metadata': metadata,
                         'model': attributes.get('langfuse.observation.model.name'),
                         # v4.70.0 RawUsageDetails preserves these keys and the
                         # worker adds total; total_tokens is not submitted.
                         'usageDetails': {**json.loads(attributes.get('langfuse.observation.usage_details', '{}')),
                                          **({'total': 5} if span.parent else {})},
                         'input': attributes.get('langfuse.observation.input'),
                         'output': attributes.get('langfuse.observation.output'),
                         'modelId': None, 'inputPrice': None, 'outputPrice': None, 'totalPrice': None})
        return rows

    def wire_scores():
        return [{'id': body.get('id', 'server-score'), 'projectId': 'probe-project',
                 'name': body['name'], 'dataType': 'NUMERIC', 'value': body['value'], 'source': 'API',
                 'timestamp': '2026-10-26T00:00:00Z', 'createdAt': '2026-10-26T00:00:00Z',
                 'updatedAt': '2026-10-26T00:00:00Z', 'environment': 'local-acceptance',
                 'subject': {'kind': 'observation', 'id': body['observationId'], 'traceId': body['traceId']}}
                for body in score_bodies]

    yield wire_observations, wire_scores
    for client, http in clients:
        client.shutdown()
        http.close()
    provider.shutdown()


def protocol_handler(sdk_boundary, *, mutate=None, delay_scores=False):
    observations, scores = sdk_boundary
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == '/api/public/projects':
            return httpx.Response(200, json={'data': [{'id': 'probe-project', 'name': 'Probe',
                                  'organization': {'id': 'probe-org', 'name': 'Probe'}, 'metadata': {}}]})
        if request.url.path == '/api/public/v2/observations':
            data = observations()
            assert all(row['traceId'] == request.url.params['traceId'] for row in data)
            if mutate:
                mutate(data, None)
            return httpx.Response(200, json={'data': data, 'meta': {}})
        assert request.url.path == '/api/public/v3/scores'
        assert request.url.params['fields'] == 'core,subject'
        data = scores()
        if mutate:
            mutate(None, data)
        if delay_scores and sum(r.url.path.endswith('/v3/scores') for r in requests) == 1:
            data = []
        return httpx.Response(200, json={'data': data, 'meta': {'limit': 100}})
    return handler, requests


def test_real_sdk_payload_and_server_readback_are_both_required(sdk_boundary):
    handler, requests = protocol_handler(sdk_boundary, delay_scores=True)
    clock = Clock()
    result = asyncio.run(module().probe_langfuse(CONFIG, timeout_s=6, transport=httpx.MockTransport(handler),
                                                clock=clock, sleep=clock.sleep))
    assert result['status'] == 'passed'
    assert result['attempts'] == 2  # flush + observations without the score must not pass
    assert result['checks']['parentage'] and result['checks']['score_subject']
    assert result['checks']['run_metadata'] and result['checks']['usage']
    assert len(result['trace_id']) == 32 and len(result['generation_id']) == 16
    assert result['model_requests'] == 0 and result['full_integration_verified'] is False
    payload = json.dumps(result)
    assert CONFIG['LANGFUSE_SECRET_KEY'] not in payload
    assert CONFIG['LANGFUSE_PUBLIC_KEY'] not in payload
    assert 'synthetic-private' not in payload


@pytest.mark.parametrize('fault,failed_check', [('parent', 'parentage'), ('usage', 'usage'),
                                               ('metadata', 'run_metadata'), ('subject', 'score_subject'),
                                               ('type', 'observation_types')])
def test_mismatched_server_evidence_cannot_become_passed(sdk_boundary, fault, failed_check):
    def mutate(observations, scores):
        if observations:
            generation = next(row for row in observations if row['type'] == 'GENERATION')
            root = next(row for row in observations if row['isRootObservation'])
            if fault == 'parent': generation['parentObservationId'] = 'different-parent'
            if fault == 'usage': generation['usageDetails'] = {'input_tokens': 999, 'output_tokens': 3}
            if fault == 'metadata': root['metadata']['run_id'] = 'another-run'
            if fault == 'type': generation['type'] = 'SPAN'
        if scores and fault == 'subject': scores[0]['subject']['traceId'] = 'another-trace'
    handler, _ = protocol_handler(sdk_boundary, mutate=mutate)
    clock = Clock()
    result = asyncio.run(module().probe_langfuse(CONFIG, timeout_s=3, transport=httpx.MockTransport(handler),
                                                clock=clock, sleep=clock.sleep))
    assert result['status'] == 'failed'
    assert result['reason'] == 'readback_timeout'
    assert result['checks'][failed_check] is False
    assert clock.now <= 3


def test_content_returned_by_platform_is_a_failure_without_body_in_report(sdk_boundary):
    def mutate(observations, scores):
        if observations:
            observations[0]['input'] = 'private-server-body'
    handler, _ = protocol_handler(sdk_boundary, mutate=mutate)
    clock = Clock()
    result = asyncio.run(module().probe_langfuse(CONFIG, timeout_s=3, transport=httpx.MockTransport(handler),
                                                clock=clock, sleep=clock.sleep))
    assert result['status'] == 'failed'
    assert not result['checks']['content_default_off']
    assert 'private-server-body' not in json.dumps(result)


def test_server_usage_total_must_match_exact_synthetic_counts(sdk_boundary):
    def mutate(observations, scores):
        if observations:
            generation = next(row for row in observations if row['type'] == 'GENERATION')
            generation['usageDetails']['total'] = 999
    handler, _ = protocol_handler(sdk_boundary, mutate=mutate)
    clock = Clock()
    result = asyncio.run(module().probe_langfuse(CONFIG, timeout_s=3, transport=httpx.MockTransport(handler),
                                                clock=clock, sleep=clock.sleep))
    assert result['status'] == 'failed'
    assert result['checks']['usage'] is False


@pytest.mark.parametrize('status', [401, 404])
def test_rejected_api_is_failed_without_retry_or_response_body(sdk_boundary, status):
    def reject(request):
        return httpx.Response(status, json={'message': 'private-server-details'})
    result = asyncio.run(module().probe_langfuse(CONFIG, transport=httpx.MockTransport(reject)))
    assert result['status'] == 'failed'
    assert result['http_status'] == status
    assert 'private-server-details' not in json.dumps(result)


def test_unreachable_platform_is_blocked_without_exception_message(sdk_boundary):
    def reject(request):
        raise httpx.ConnectError('private-credential at private-server')
    result = asyncio.run(module().probe_langfuse(CONFIG, transport=httpx.MockTransport(reject)))
    assert result['status'] == 'blocked'
    assert result['error_type'] == 'ConnectError'
    assert 'private-' not in json.dumps(result)
