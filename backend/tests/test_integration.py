"""Safety and evidence boundaries for the opt-in live integration CLI."""
import asyncio
import json

import httpx

from evaluation.integration import load_config, run_checks, save_report


def test_cli_preflight_does_not_create_an_event_loop(tmp_path, monkeypatch):
    import evaluation.integration as integration
    def forbidden(*args, **kwargs):
        raise AssertionError('offline preflight must not create a socketpair/event loop')
    monkeypatch.setattr(integration.asyncio, 'run', forbidden)
    config_file = tmp_path / '.env'
    config_file.write_text('', encoding='utf-8')
    assert integration.main(['--env-file', str(config_file), '--output-dir', str(tmp_path)]) == 2


def test_explicit_config_overrides_ambient_without_mutating_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://ambient.invalid')
    config_file = tmp_path / '.env'
    config_file.write_text('ANTHROPIC_API_KEY=secret-credential\nANTHROPIC_BASE_URL=https://api.example/anthropic\n', encoding='utf-8')
    config = load_config(config_file)
    assert config['ANTHROPIC_BASE_URL'] == 'https://api.example/anthropic'
    import os
    assert os.environ['ANTHROPIC_BASE_URL'] == 'https://ambient.invalid'


def test_default_preflight_never_connects_and_does_not_claim_live_success():
    async def forbidden(*args, **kwargs):
        raise AssertionError('preflight must not connect')
    config = {'ANTHROPIC_API_KEY': 'private-key', 'REDIS_URL': 'redis://user:private-pass@localhost:1/0',
              'LANGFUSE_SECRET_KEY': 'private-lf', 'ANTHROPIC_BASE_URL': 'https://host.invalid/?key=private-query'}
    report = asyncio.run(run_checks(config, provider_probe=forbidden, storage_probe=forbidden))
    assert report['stage'] == 'preflight'
    assert report['status'] == 'blocked'
    assert report['full_integration_verified'] is False
    payload = json.dumps(report)
    assert all(secret not in payload for secret in ['private-key', 'private-pass', 'private-lf', 'private-query'])
    assert report['model_requests'] == 0


def test_missing_provider_is_blocked_without_request():
    async def forbidden(*args, **kwargs):
        raise AssertionError('missing credentials must not connect')
    report = asyncio.run(run_checks({}, stage='provider', provider_probe=forbidden))
    assert report['status'] == 'blocked'
    assert report['model_requests'] == 0


def test_langfuse_stage_is_explicit_and_missing_config_does_not_connect():
    report = asyncio.run(run_checks({}, stage='langfuse'))
    assert report['status'] == 'blocked'
    assert report['model_requests'] == 0
    assert report['checks'][-1]['reason'] == 'missing_configuration'
    assert report['full_integration_verified'] is False


def test_langfuse_readback_does_not_become_full_scenario_acceptance():
    async def readback(config):
        return {'id': 'langfuse', 'status': 'passed', 'evidence': 'langfuse_public_api_readback',
                'model_requests': 0, 'trace_id': 'a' * 32}
    report = asyncio.run(run_checks({}, stage='langfuse', langfuse_probe=readback))
    assert report['status'] == 'passed'
    assert report['model_requests'] == 0
    assert report['full_integration_verified'] is False


def test_exception_messages_never_enter_report():
    async def broken(config):
        raise RuntimeError('private-key https://user:password@private-host')
    report = asyncio.run(run_checks({'ANTHROPIC_API_KEY': 'private-key'}, stage='provider', provider_probe=broken))
    assert report['status'] == 'failed'
    assert report['checks'][-1]['error_type'] == 'RuntimeError'
    assert 'private-key' not in json.dumps(report)
    assert 'password' not in json.dumps(report)
    assert report['model_requests'] == 1  # attempt, even when the request raises


def test_storage_failure_is_not_replaced_by_local_or_fake_storage():
    async def unavailable(config):
        return [{'id': 'redis', 'status': 'blocked', 'evidence': 'real_service', 'reason': 'connection_unavailable'},
                {'id': 'chroma', 'status': 'blocked', 'evidence': 'real_http_service', 'reason': 'connection_unavailable'}]
    report = asyncio.run(run_checks({}, stage='storage', storage_probe=unavailable))
    assert report['status'] == 'blocked'
    assert report['full_integration_verified'] is False
    assert report['model_requests'] == 0


def test_successful_provider_is_only_a_protocol_check(tmp_path):
    async def provider(config):
        return {'id': 'provider', 'status': 'passed', 'evidence': 'real_provider_protocol',
                'usage': {'input_tokens': 4, 'output_tokens': 2}}
    report = asyncio.run(run_checks({'ANTHROPIC_API_KEY': 'secret'}, stage='provider', provider_probe=provider))
    assert report['status'] == 'passed'
    assert report['full_integration_verified'] is False
    saved = save_report(report, tmp_path)
    assert json.loads(saved.read_text(encoding='utf-8')) == report
    assert 'secret' not in saved.read_text(encoding='utf-8')


def test_chroma_probe_uses_http_vectors_and_deletes_only_its_collection():
    from evaluation.integration import probe_chroma
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        path = request.url.path
        if path.endswith('/heartbeat'):
            return httpx.Response(200, json={'nanosecond heartbeat': 123})
        if path.endswith('/version'):
            return httpx.Response(200, json='0.5.23')
        if request.method == 'POST' and path.endswith('/collections'):
            return httpx.Response(200, json={'id': 'owned-id'})
        if path.endswith('/query'):
            return httpx.Response(200, json={'ids': [['probe-1']], 'distances': [[0.0]]})
        return httpx.Response(200, json=True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_chroma({'CHROMA_HOST': 'localhost', 'CHROMA_PORT': '18001'}, client=client)
    result = asyncio.run(run())
    assert result['status'] == 'passed'
    assert result['embedding'] == 'explicit_test_vectors'
    creates = [body for method, path, body in calls if method == 'POST' and path.endswith('/collections')]
    assert len(creates) == 1 and creates[0]['name'].startswith('eval_')
    deletes = [path for method, path, body in calls if method == 'DELETE']
    assert deletes == ['/api/v1/collections/' + creates[0]['name']]
    assert not any('reset' in path for _, path, _ in calls)


def test_chroma_protocol_failure_still_cleans_owned_collection():
    from evaluation.integration import probe_chroma
    deleted = []
    def handler(request):
        if request.method == 'DELETE':
            deleted.append(request.url.path)
            return httpx.Response(200, json=True)
        if request.url.path.endswith('/collections'):
            return httpx.Response(200, json={'id': 'owned-id'})
        if request.url.path.endswith('/add'):
            return httpx.Response(500, json={'error': 'private-server-details'})
        return httpx.Response(200, json='0.5.23')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_chroma({}, client=client)
    result = asyncio.run(run())
    assert result['status'] == 'failed'
    assert result['cleanup'] == 'passed'
    assert len(deleted) == 1
    assert 'private-server-details' not in json.dumps(result)


def test_redis_bad_ttl_fails_and_still_cleans_owned_keys(monkeypatch):
    import redis.asyncio as redis
    from evaluation.integration import probe_redis
    class Client:
        def __init__(self):
            self.values = {}
            self.closed = False
        async def ping(self): pass
        async def info(self, section): return {'redis_version': '7.4.2'}
        async def set(self, key, value, **kwargs): self.values[key] = value
        async def rpush(self, key, value): self.values[key] = [value]
        async def expire(self, *args): pass
        async def ttl(self, key): return -1
        async def lrange(self, key, *args): return self.values[key]
        async def delete(self, *keys):
            for key in keys: self.values.pop(key, None)
        async def exists(self, *keys): return sum(key in self.values for key in keys)
        async def aclose(self): self.closed = True
    client = Client()
    monkeypatch.setattr(redis, 'from_url', lambda *args, **kwargs: client)
    report = asyncio.run(probe_redis({}))
    assert report['status'] == 'failed'
    assert report['cleanup'] == 'passed'
    assert report['namespace'].startswith('eval_')
    assert not client.values and client.closed


def test_redis_close_failure_retains_evidence_and_chroma_still_runs(monkeypatch):
    import redis.asyncio as redis
    import evaluation.integration as integration
    class Client:
        async def ping(self): raise ConnectionError('private')
        async def aclose(self): raise OSError('private-close')
    monkeypatch.setattr(redis, 'from_url', lambda *args, **kwargs: Client())
    async def chroma(config):
        return {'id': 'chroma', 'status': 'passed', 'evidence': 'test_double'}
    monkeypatch.setattr(integration, 'probe_chroma', chroma)
    reports = asyncio.run(integration.probe_storage({}))
    assert [item['id'] for item in reports] == ['redis', 'chroma']
    assert reports[0]['status'] == 'failed'
    assert reports[0]['close_error_type'] == 'OSError'
    assert reports[0]['namespace'].startswith('eval_')
    assert 'private' not in json.dumps(reports)
