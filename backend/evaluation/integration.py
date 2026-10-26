"""Opt-in integration probes. Preflight is offline; no fallback or baseline promotion.

Reports deliberately contain allowlisted evidence, never raw configuration,
provider replies, exception messages, or arbitrary SDK/server payloads.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_ENV = Path(__file__).resolve().parents[1] / '.env'
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'integration'
CONFIG_KEYS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL',
               'REDIS_URL', 'CHROMA_HOST', 'CHROMA_PORT', 'LANGFUSE_PUBLIC_KEY',
               'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL', 'LANGFUSE_ALLOW_TEST_ENDPOINT', 'ECHOMIND_HTTP_PROXY')


def load_config(env_file=None):
    """Explicit files take precedence only in this dict, never in os.environ."""
    from dotenv import dotenv_values
    config = {key: os.environ[key] for key in CONFIG_KEYS if key in os.environ}
    path = Path(env_file) if env_file is not None else DEFAULT_ENV
    if env_file is not None and not path.is_file():
        raise FileNotFoundError('Requested integration configuration file is missing')
    if path.is_file():
        values = dotenv_values(path, interpolate=False)
        config.update({key: values[key] for key in CONFIG_KEYS if values.get(key) is not None})
    return config


def _error_type(exc):
    name = type(exc).__name__
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) else 'RuntimeError'


def _failed(check_id, exc, *, evidence, connected=False):
    unavailable = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError))
    # redis exceptions are optional imports and must not leak their message/URL.
    unavailable = unavailable or type(exc).__name__ in {'ConnectionError', 'TimeoutError'}
    return {'id': check_id, 'status': 'blocked' if unavailable and not connected else 'failed',
            'evidence': evidence, 'error_type': _error_type(exc)}


def _inventory(config):
    versions = {}
    for package in ('anthropic', 'redis', 'chromadb', 'langfuse', 'httpx', 'pydantic'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {'id': 'configuration', 'status': 'passed', 'evidence': 'offline_inventory',
            'packages': versions,
            'configured': {key: bool(config.get(key)) for key in CONFIG_KEYS if key != 'ECHOMIND_HTTP_PROXY'},
            'network_accessed': False}


def _http_url(host, port):
    # HTTP service credentials must be supplied through a supported client,
    # never accidentally parsed from an untrusted host string.
    parsed = urlsplit(host if '://' in host else f'http://{host}')
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid Chroma endpoint')
    if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('Chroma host must not contain path or query')
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError('Invalid Chroma port')
    hostname = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    return f'{parsed.scheme}://{hostname}:{parsed.port or number}/api/v1'


async def probe_provider(config):
    """Exactly one bounded synthetic Messages call; this is not a chat eval."""
    from anthropic import AsyncAnthropic
    started = time.monotonic()
    async with httpx.AsyncClient(proxy=config.get('ECHOMIND_HTTP_PROXY') or None, timeout=35) as transport:
        async with AsyncAnthropic(api_key=config['ANTHROPIC_API_KEY'],
                                  base_url=config.get('ANTHROPIC_BASE_URL') or None,
                                  http_client=transport, max_retries=0, timeout=35) as client:
            response = await client.messages.create(
                model=config.get('ANTHROPIC_MODEL') or 'claude-3-5-sonnet-20241022',
                max_tokens=64, temperature=0,
                messages=[{'role': 'user', 'content': '请仅回复 OK。'}])
    blocks = response.content
    text_present = any(getattr(block, 'type', None) == 'text' and bool(getattr(block, 'text', '').strip())
                       for block in blocks)
    usage = {key: getattr(response.usage, key, None) for key in
             ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')}
    return {'id': 'provider', 'status': 'passed' if text_present else 'failed',
            'evidence': 'real_provider_protocol', 'text_present': text_present,
            'model': response.model if re.fullmatch(r'[A-Za-z0-9_./-]{1,96}', response.model or '') else 'other',
            'content_types': [block.type for block in blocks if block.type in {'text', 'tool_use', 'thinking', 'redacted_thinking'}],
            'usage': {key: value for key, value in usage.items() if isinstance(value, int) and value >= 0},
            'latency_ms': round((time.monotonic() - started) * 1000, 2),
            'stop_reason': response.stop_reason if response.stop_reason in {'end_turn', 'max_tokens', 'tool_use', 'stop_sequence'} else 'other'}


async def probe_redis(config):
    """Real Redis wire protocol + server TTL, only two new UUID-owned keys."""
    try:
        import redis.asyncio as redis
    except ImportError:
        return {'id': 'redis', 'status': 'blocked', 'evidence': 'real_service', 'reason': 'missing_sdk'}
    namespace = 'eval_' + uuid.uuid4().hex
    keys = [f'{namespace}:integration:list', f'{namespace}:integration:sentinel']
    client = None
    owned = False
    result = {'id': 'redis', 'evidence': 'redis_wire_protocol', 'namespace': namespace,
              'server_identity': 'unverified_redis_compatible'}
    try:
        client = redis.from_url(config.get('REDIS_URL') or 'redis://127.0.0.1:16379/0',
                                socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
        await client.ping()
        info = await client.info('server')
        # INFO alone cannot distinguish Redis from every compatible emulator.
        # Container/process provenance is a separate acceptance requirement.
        version = info.get('redis_version', '')
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)*', version):
            raise ValueError('Unrecognized Redis server identity')
        owned = True  # start cleanup before the first write (which can partly succeed)
        await client.set(keys[1], 'synthetic-sentinel', ex=60, nx=True)
        await client.rpush(keys[0], 'synthetic-turn')
        await client.expire(keys[0], 60)
        ttl = await client.ttl(keys[0])
        if await client.lrange(keys[0], 0, -1) != ['synthetic-turn']:
            raise ValueError('Redis list roundtrip did not match')
        if not 0 < ttl <= 60:
            raise ValueError('Redis server TTL was not applied')
        await client.delete(keys[0])
        if await client.get(keys[1]) != 'synthetic-sentinel':
            raise ValueError('Redis owned key isolation did not match')
        result.update(status='passed', server_version=version, ttl_seconds=ttl,
                      checks=['ping', 'list_roundtrip', 'server_ttl', 'owned_key_isolation'])
    except Exception as exc:
        result.update(_failed('redis', exc, evidence='redis_wire_protocol', connected=owned))
    finally:
        if client is not None:
            if owned:
                try:
                    await client.delete(*keys)
                    if await client.exists(*keys) != 0:
                        raise ValueError('Redis owned keys remain after cleanup')
                    result['cleanup'] = 'passed'
                except Exception as exc:
                    result.update(status='failed', cleanup='failed', cleanup_error_type=_error_type(exc))
            try:
                await client.aclose()
            except Exception as exc:
                result.update(status='failed', close_error_type=_error_type(exc))
    return result


async def probe_chroma(config, *, client=None):
    """Exercise v0.5.23 HTTP protocol using explicit vectors, no ONNX download.

    No call to application create_chroma_client: its persistent fallback must
    never turn an unreachable HTTP service into a passed integration probe.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as transport:
            return await probe_chroma(config, client=transport)
    name = 'eval_' + uuid.uuid4().hex + '_integration'
    params = {'tenant': 'default_tenant', 'database': 'default_database'}
    result = {'id': 'chroma', 'evidence': 'real_http_service', 'collection': name,
              'embedding': 'explicit_test_vectors', 'default_embedding_verified': False}
    owned = False
    base = None
    try:
        base = _http_url(config.get('CHROMA_HOST') or '127.0.0.1', config.get('CHROMA_PORT') or '18001')
        (await client.get(base + '/heartbeat')).raise_for_status()
        version_response = await client.get(base + '/version')
        version_response.raise_for_status()
        if version_response.json() != '0.5.23':
            raise ValueError('Chroma server must match the pinned protocol version')
        # A create can succeed server-side even when its response is lost.
        owned = True
        created = await client.post(base + '/collections', params=params,
                                    json={'name': name, 'metadata': {'integration': 'synthetic'}, 'get_or_create': False})
        created.raise_for_status()
        collection_id = created.json()['id']
        if not isinstance(collection_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', collection_id):
            raise ValueError('Invalid collection id')
        endpoint = base + '/collections/' + collection_id
        added = await client.post(endpoint + '/add', json={
            'ids': ['probe-1'], 'embeddings': [[1.0, 0.0, 0.0]],
            'documents': ['synthetic integration document'],
            'metadatas': [{'user_id': 'synthetic-user', 'conv_id': 'synthetic-conversation'}]})
        added.raise_for_status()
        queried = await client.post(endpoint + '/query', json={
            'query_embeddings': [[1.0, 0.0, 0.0]], 'n_results': 1,
            'where': {'$and': [{'user_id': 'synthetic-user'}, {'conv_id': 'synthetic-conversation'}]},
            'include': ['distances', 'metadatas']})
        queried.raise_for_status()
        if queried.json().get('ids') != [['probe-1']]:
            raise ValueError('Chroma query roundtrip did not match')
        result.update(status='passed', server_version='0.5.23',
                      checks=['heartbeat', 'version', 'create', 'explicit_vector_add', 'filtered_query'])
    except Exception as exc:
        result.update(_failed('chroma', exc, evidence='real_http_service', connected=owned))
    finally:
        if owned:
            try:
                deleted = await client.delete(base + '/collections/' + name, params=params)
                # Missing after a failed create is already clean.
                if deleted.status_code != 404:
                    deleted.raise_for_status()
                result['cleanup'] = 'passed'
            except Exception as exc:
                result.update(status='failed', cleanup='failed', cleanup_error_type=_error_type(exc))
    return result


async def probe_storage(config):
    results = []
    for name, probe in (('redis', probe_redis), ('chroma', probe_chroma)):
        try:
            results.append(await probe(config))
        except Exception as exc:
            results.append(_failed(name, exc, evidence='service_probe'))
    return results


def _new_report(config, stage):
    return {'schema_version': 1, 'run_id': 'integration_' + uuid.uuid4().hex,
              'timestamp': datetime.now(timezone.utc).isoformat(), 'stage': stage,
              'full_integration_verified': False, 'model_requests': 0,
              'checks': [_inventory(config)]}


def preflight_report(config):
    """Keep the offline CLI fully synchronous (even no asyncio socketpair)."""
    report = _new_report(config, 'preflight')
    report['checks'].append({'id': 'live_acceptance', 'status': 'blocked', 'evidence': 'not_executed',
                             'reason': 'explicit_live_stage_required'})
    report['status'] = 'blocked'
    return report


async def run_checks(config, *, stage='preflight', provider_probe=probe_provider, storage_probe=probe_storage,
                     langfuse_probe=None):
    if stage not in {'preflight', 'provider', 'storage', 'langfuse'}:
        raise ValueError('Unknown integration stage')
    if stage == 'preflight':
        return preflight_report(config)
    report = _new_report(config, stage)
    if stage == 'provider':
        if not config.get('ANTHROPIC_API_KEY'):
            report['checks'].append({'id': 'provider', 'status': 'blocked', 'evidence': 'not_executed',
                                     'reason': 'missing_credentials'})
        else:
            report['model_requests'] = 1
            try:
                report['checks'].append(await provider_probe(config))
            except Exception as exc:
                report['checks'].append(_failed('provider', exc, evidence='real_provider_protocol'))
    elif stage == 'langfuse':
        try:
            if langfuse_probe is None:
                from evaluation.langfuse_probe import probe_langfuse
                langfuse_probe = probe_langfuse
            report['checks'].append(await langfuse_probe(config))
        except Exception as exc:
            report['checks'].append(_failed('langfuse', exc, evidence='langfuse_public_api_readback'))
    else:
        try:
            report['checks'].extend(await storage_probe(config))
        except Exception as exc:
            report['checks'].append(_failed('storage', exc, evidence='real_service'))
    statuses = [item['status'] for item in report['checks'] if item['evidence'] != 'offline_inventory']
    report['status'] = ('failed' if 'failed' in statuses else
                        'partial' if 'passed' in statuses and 'blocked' in statuses else
                        'blocked' if 'blocked' in statuses else 'passed')
    return report


def save_report(report, output_dir=DEFAULT_OUTPUT):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r'integration_[a-f0-9]{32}', report['run_id']):
        raise ValueError('Invalid integration report id')
    path = directory / (report['run_id'] + '.json')
    with path.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['preflight', 'provider', 'storage', 'langfuse'], default='preflight')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env_file)
        report = preflight_report(config) if args.stage == 'preflight' else asyncio.run(run_checks(config, stage=args.stage))
        path = save_report(report, args.output_dir)
        print(json.dumps({'report': str(path), **report}, ensure_ascii=False, indent=2))
        return 0 if report['status'] == 'passed' else 2
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': _error_type(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
