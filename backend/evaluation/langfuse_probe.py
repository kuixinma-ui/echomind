"""Opt-in Langfuse platform readback using synthetic data and no model calls.

Only explicit configuration is used. This module never loads dotenv files and
never falls back to Cloud, a fake exporter, or an in-memory platform.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from observability.telemetry import LangfuseTelemetry, bind_telemetry, get_telemetry


_LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1', 'langfuse-web'}
_FIELDS = 'basic,time,metadata,io,model,usage'
_MODEL = 'synthetic-no-model-call'
_SCORE = 'readback_probe'
_CASE = 'synthetic_no_model'


class _RejectedRead(Exception):
    def __init__(self, status):
        self.status = status


def _error_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) else 'RuntimeError'


def _endpoint(config):
    value = config.get('LANGFUSE_BASE_URL')
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {'', '/'} or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return None
        approved = config.get('LANGFUSE_ALLOW_TEST_ENDPOINT')
        if parsed.hostname not in _LOCAL_HOSTS and approved not in (True, 'true'):
            return None
    except (ValueError, TypeError):
        return None
    return value.rstrip('/')


def _decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _safe_id(value, config):
    return (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None
            and value not in {config.get('LANGFUSE_PUBLIC_KEY'), config.get('LANGFUSE_SECRET_KEY')})


def _emit(client_class, config, base, run_id):
    stamp = datetime.now(timezone.utc)
    with httpx.Client(timeout=5, trust_env=False) as sdk_http:
        client = client_class(public_key=config['LANGFUSE_PUBLIC_KEY'],
                              secret_key=config['LANGFUSE_SECRET_KEY'], base_url=base,
                              httpx_client=sdk_http, timeout=5, debug=False,
                              environment='local-acceptance', sample_rate=1.0, flush_at=1)
        adapter = LangfuseTelemetry(client, trace_content=False)
        try:
            with bind_telemetry(adapter):
                observed = get_telemetry()
                with observed.span('integration.langfuse', kind='agent',
                                   input='synthetic-private-request', metadata={
                                       'run_id': run_id, 'case_id': _CASE,
                                       'user_id': 'synthetic-user',
                                       'api_key': 'synthetic-private-value'}):
                    trace_id = client.get_current_trace_id()
                    root_id = client.get_current_observation_id()
                    with observed.span('integration.langfuse.generation', kind='generation',
                                       input='synthetic-private-request') as generation:
                        generation_id = client.get_current_observation_id()
                        generation.update(model=_MODEL, completion_start_time=stamp,
                                          usage_details={'input_tokens': 2, 'output_tokens': 3},
                                          output='synthetic-private-response')
                        generation.score(_SCORE, 0.75)
            adapter.flush()
        finally:
            adapter.shutdown()
    if (not re.fullmatch(r'[0-9a-f]{32}', trace_id or '')
            or not all(re.fullmatch(r'[0-9a-f]{16}', item or '') for item in (root_id, generation_id))):
        raise ValueError('Telemetry context missing')
    return {'trace_id': trace_id, 'root_id': root_id, 'generation_id': generation_id, 'stamp': stamp}


def _validate(observations, scores, expected):
    if not isinstance(observations, list) or not isinstance(scores, list):
        raise ValueError('Unexpected public API shape')
    if any(not isinstance(item, dict) for item in observations + scores):
        raise ValueError('Unexpected public API item')
    nodes = {item.get('id'): item for item in observations}
    root = nodes.get(expected['root_id'], {})
    generation = nodes.get(expected['generation_id'], {})
    metadata = _decoded(root.get('metadata')) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    stamp = generation.get('completionStartTime')
    try:
        actual_stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')) if isinstance(stamp, str) else None
        stamp_matches = actual_stamp is not None and abs((actual_stamp - expected['stamp']).total_seconds()) < 0.001
    except (ValueError, TypeError):
        stamp_matches = False
    usage = generation.get('usageDetails') or {}
    subject = {'kind': 'observation', 'id': expected['generation_id'], 'traceId': expected['trace_id']}
    matching_scores = [item for item in scores
                       if item.get('name') == _SCORE and item.get('dataType') == 'NUMERIC'
                       and type(item.get('value')) in {int, float} and item['value'] == 0.75
                       and item.get('source') == 'API' and item.get('projectId') == expected['project_id']
                       and item.get('subject') == subject]
    checks = {
        'trace_context': bool(root and generation) and all(item.get('traceId') == expected['trace_id']
                                  and item.get('projectId') == expected['project_id'] for item in (root, generation)),
        'observation_types': root.get('type') == 'AGENT' and generation.get('type') == 'GENERATION',
        'parentage': bool(root) and root.get('isRootObservation') is True
                     and root.get('parentObservationId') is None
                     and generation.get('parentObservationId') == expected['root_id'],
        'run_metadata': metadata.get('run_id') == expected['run_id'] and metadata.get('case_id') == _CASE,
        # No total_tokens is sent, so v4.70.0 uses RawUsageDetails, preserves
        # these names, and the worker adds total. Do not accept guessed aliases.
        'usage': isinstance(usage, dict) and usage.get('input_tokens') == 2 and usage.get('output_tokens') == 3
                 and usage.get('total') == 5
                 and generation.get('model') == _MODEL,
        'completion_start_time': stamp_matches,
        'ended': bool(root.get('endTime')) and bool(generation.get('endTime')),
        'content_default_off': bool(root and generation) and all(_decoded(item.get(key)) in (None, '')
                                  for item in (root, generation) for key in ('input', 'output')),
        'redaction': metadata.get('api_key') == '[REDACTED]'
                     and re.fullmatch(r'hmac-sha256:[0-9a-f]{64}', str(metadata.get('user_id', ''))) is not None,
        'score_subject': bool(matching_scores),
    }
    return checks, matching_scores


async def probe_langfuse(config, *, timeout_s=120, transport=None, clock=time.monotonic, sleep=asyncio.sleep):
    """Write one synthetic trace and require observations v2 + scores v3 readback.

    ``timeout_s`` bounds post-flush polling; each HTTP request is bounded to 5s.
    ``transport``, ``clock`` and ``sleep`` are explicit offline test seams. The
    default path constructs the real SDK and uses its real OTLP exporter.
    Non-local test services additionally require LANGFUSE_ALLOW_TEST_ENDPOINT=true.
    """
    result = {'id': 'langfuse', 'status': 'blocked', 'evidence': 'langfuse_public_api_readback',
              'model_requests': 0, 'full_integration_verified': False, 'attempts': 0}
    if not all(isinstance(config.get(key), str) and config[key].strip()
               for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')):
        return {**result, 'reason': 'missing_configuration'}
    base = _endpoint(config)
    if base is None:
        return {**result, 'reason': 'endpoint_not_allowed'}
    try:
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('Invalid timeout')
    except (TypeError, ValueError):
        return {**result, 'status': 'failed', 'reason': 'invalid_timeout', 'error_type': 'ValueError'}
    try:
        from langfuse import Langfuse
    except ImportError:
        return {**result, 'reason': 'missing_sdk'}
    started = clock()
    submitted = False
    try:
        async with httpx.AsyncClient(base_url=base, auth=httpx.BasicAuth(config['LANGFUSE_PUBLIC_KEY'],
                                      config['LANGFUSE_SECRET_KEY']), transport=transport,
                                      trust_env=False, follow_redirects=False) as http:
            async def get_json(path, params, deadline):
                remaining = deadline - clock()
                if remaining <= 0:
                    return None
                response = await asyncio.wait_for(http.get(path, params=params, timeout=5),
                                                  timeout=min(5.0, remaining))
                result['http_status'] = response.status_code
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    return None
                if response.status_code != 200:
                    raise _RejectedRead(response.status_code)
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('Unexpected public API shape')
                return payload

            projects = await get_json('/api/public/projects', None, clock() + min(5, timeout_s))
            if projects is None:
                return {**result, 'reason': 'platform_unavailable'}
            if (len(projects['data']) != 1 or not isinstance(projects['data'][0], dict)
                    or not _safe_id(projects['data'][0].get('id'), config)):
                raise ValueError('Unexpected project identity')
            result['project_id'] = projects['data'][0]['id']
            result['run_id'] = 'langfuse_' + uuid4().hex
            emitted = await asyncio.to_thread(_emit, Langfuse, config, base, result['run_id'])
            submitted = True
            expected = {**emitted, 'run_id': result['run_id'], 'project_id': result['project_id']}
            result.update({key: emitted[key] for key in ('trace_id', 'root_id', 'generation_id')})
            deadline = clock() + timeout_s
            for _ in range(math.ceil(timeout_s / 2) + 2):
                if clock() >= deadline:
                    break
                result['attempts'] += 1
                try:
                    observations = await get_json('/api/public/v2/observations', {
                        'traceId': emitted['trace_id'], 'fields': _FIELDS, 'limit': 100}, deadline)
                    scores = await get_json('/api/public/v3/scores', {
                        'traceId': emitted['trace_id'], 'observationId': emitted['generation_id'],
                        'name': _SCORE, 'fields': 'core,subject', 'limit': 100}, deadline)
                    if observations is not None and scores is not None:
                        if any(item.get('meta', {}).get('cursor') for item in (observations, scores)):
                            raise ValueError('Unexpected extra pages for isolated probe')
                        checks, matching_scores = _validate(observations['data'], scores['data'], expected)
                        result['checks'] = checks
                        if all(checks.values()):
                            score_id = matching_scores[0].get('id')
                            if not _safe_id(score_id, config):
                                raise ValueError('Unexpected score identity')
                            result.update(status='passed', score_id=score_id)
                            break
                except (httpx.TransportError, asyncio.TimeoutError) as error:
                    result['error_type'] = _error_type(error)
                remaining = deadline - clock()
                if remaining > 0:
                    await sleep(min(2.0, remaining))
            if result['status'] != 'passed':
                result.update(status='failed', reason='readback_timeout')
    except _RejectedRead as error:
        result.update(status='failed', reason='read_api_rejected', http_status=error.status)
    except (httpx.TransportError, asyncio.TimeoutError) as error:
        result.update(status='failed' if submitted else 'blocked', reason='platform_unavailable',
                      error_type=_error_type(error))
    except Exception as error:
        result.update(status='failed', reason='probe_error', error_type=_error_type(error))
    result['latency_ms'] = round(max(0, clock() - started) * 1000, 2)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args(argv)
    keys = ('LANGFUSE_BASE_URL', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_ALLOW_TEST_ENDPOINT')
    config = {key: os.environ[key] for key in keys if key in os.environ}
    report = asyncio.run(probe_langfuse(config, timeout_s=args.timeout))
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
