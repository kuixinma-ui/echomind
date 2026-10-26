"""Replay scenarios through isolated ChatService runtimes.

CLI defaults to validation. Only explicit ``--mode live`` constructs a real
runtime. A case passes only when every required assertion and turn succeeds;
hard failures cannot be averaged away. Optional semantic grading is never
replaced by keyword or regular-expression matching.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import math
import statistics
import time
import uuid
from datetime import datetime, timezone

from evaluation.scenarios import DEFAULT_DATASET, build_fingerprints, load_dataset, validate_dataset
from observability.experiments import observe_case
from observability.budgets import (EvaluationBudget, EvaluationBudgetExceeded, bind_budget,
                                   current_budget, mark_budget_error)


async def _await(value):
    return await value if inspect.isawaitable(value) else value


_MISSING = object()


def _path(value, path):
    for part in path.split('.'):
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _traces(target, tool=None):
    return [trace for trace in target.get('tool_traces', [])
            if isinstance(trace, dict) and (tool is None or trace.get('tool_name') == tool)]


def _evidence_ids(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'evidence_id', 'id'} and isinstance(item, str):
                found.add(item)
            found.update(_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_evidence_ids(item))
    return found


def _counts(items):
    return {'attempted': len(items), 'passed': sum(x['status'] == 'passed' for x in items),
            'failed': sum(x['status'] == 'failed' for x in items),
            'invalid': sum(bool(x.get('invalid', x['status'] == 'invalid')) for x in items),
            'timeout': sum(bool(x.get('timeout', x['status'] == 'timeout')) for x in items),
            'error': sum(bool(x.get('error_count', x['status'] == 'error')) for x in items),
            'hard_fail': sum(bool(x.get('hard_fail', x['status'] == 'hard_fail')) for x in items)}


def _latency_summary(samples):
    values = sorted(samples)
    def percentile(fraction):
        if not values:
            return None
        position = (len(values) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return round(values[low] + (values[high] - values[low]) * (position - low), 3)
    return {'valid_count': len(values), 'sample_count': len(values),
            'mean_ms': round(statistics.mean(values), 3) if values else None,
            'max_ms': max(values) if values else None, 'p50_ms': percentile(.5), 'p95_ms': percentile(.95),
            'method': 'linear_interpolation', 'small_sample_estimate': 0 < len(values) < 20}


class ScenarioRunner:
    def __init__(self, service_factory, judge=None, telemetry=None):
        self.service_factory = service_factory
        self.judge = judge
        self.telemetry = telemetry

    async def run(self, dataset, *, metadata=None, budget=None):
        budget = budget if budget is not None else current_budget()
        with bind_budget(budget):
            return await self._run(dataset, metadata=metadata, budget=budget)

    async def _run(self, dataset, *, metadata=None, budget=None):
        validation = validate_dataset(dataset)
        run_id = uuid.uuid4().hex
        namespace = f'eval_{run_id}'
        metadata = copy.deepcopy(metadata or {})
        # Caller snapshots are required for comparison; absent values stay absent.
        metadata.setdefault('fingerprints', {})
        metadata['fingerprints']['dataset'] = validation['fingerprint']
        from evaluation.scenarios import stable_hash
        metadata['fingerprints']['knowledge'] = stable_hash(dataset['knowledge'])
        metadata['coverage'] = 'isolated_chat_service'
        report = {'schema_version': 1, 'run_id': run_id, 'namespace': namespace,
                  'timestamp': datetime.now(timezone.utc).isoformat(),
                  'mode': metadata.pop('mode', 'replay'), 'status': 'completed',
                  'dataset': {k: validation[k] for k in ('id', 'version', 'status', 'fingerprint')},
                  'metadata': metadata, 'cases': [], 'cleanup': {'status': 'pending'}}
        runtime = None
        started = time.perf_counter()
        try:
            runtime = await _await(self.service_factory(namespace=namespace,
                                   knowledge_documents=copy.deepcopy(dataset['knowledge'])))
            for index, case in enumerate(dataset['cases']):
                try:
                    if budget is not None:
                        budget.check()
                    result = await self._case(runtime, case, dataset['knowledge'], namespace, index)
                except Exception as exc:
                    result = self._unavailable(case, exc)
                mark_budget_error(result, budget)
                report['cases'].append(result)
        except Exception as exc:
            report['runtime_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            if getattr(exc, 'cleanup_status', None):
                report['cleanup'] = {'status': exc.cleanup_status, 'error_type': getattr(exc, 'error_type', type(exc).__name__),
                                     'errors': list(getattr(exc, 'cleanup_errors', []))}
            report['cases'] = [self._unavailable(case, exc) for case in dataset['cases']]
        finally:
            if runtime is not None:
                try:
                    await _await(runtime.close(cleanup=True))
                    report['cleanup'] = {'status': 'completed'}
                except Exception as exc:
                    report['cleanup'] = {'status': 'error', 'type': type(exc).__name__, 'message': str(exc)}
                    report['status'] = 'error'
            elif report['cleanup']['status'] == 'pending':
                report['cleanup'] = {'status': 'not_created'}
        report['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)
        case_counts = _counts(report['cases'])
        turns = [turn for case in report['cases'] for turn in case['turns']]
        assertions = [rule for case in report['cases'] for rule in case['assertions']]
        report['summary'] = {'cases': case_counts, 'turns': _counts(turns), 'assertions': _counts(assertions)}
        report['summary']['turns']['executed'] = sum(bool(turn.get('executed')) for turn in turns)
        report.update(total=case_counts['attempted'], passed=case_counts['passed'],
                      pass_rate=round(case_counts['passed'] / case_counts['attempted'], 4),
                      invalid=case_counts['invalid'], timeout=case_counts['timeout'],
                      error=case_counts['error'], hard_fail=case_counts['hard_fail'])
        report['by_scenario'] = {group: _counts([case for case in report['cases'] if case['scenario'] == group])
                                 for group in validation['scenarios']}
        latencies = [turn['latency_ms'] for turn in turns if turn.get('executed') and 'actual' in turn]
        report['latency'] = _latency_summary(latencies)
        recalls = [r['scores']['recall'] for r in assertions if 'recall' in r.get('scores', {})]
        report['evidence_recall'] = {'mean': round(statistics.mean(recalls), 4) if recalls else None,
                                     'valid_count': len(recalls), 'method': 'mean_assertion_recall'}
        report['usage'] = self._usage(turns)
        report['evaluation_overhead_usage'] = {
            name: sum(case.get('evaluation_overhead_usage', {}).get(name, 0) for case in report['cases'])
            for name in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'model_calls')
        }
        report['valid_count'] = sum(c['status'] in {'passed', 'failed', 'hard_fail'} for c in report['cases'])
        if 'runtime_error' in report:
            report['status'] = 'error'
        if budget is not None:
            report['budget'] = budget.snapshot()
            if budget.stop_reason:
                report['status'] = 'error'
        self._observe(report)
        # Do not let NaN or non-serializable model payloads become stored results.
        json.dumps(report, allow_nan=False)
        return report

    @staticmethod
    def _unavailable(case, exc):
        return {'id': case['id'], 'scenario': case['scenario'], 'status': 'error',
                'error_count': 1, 'invalid': False, 'timeout': False, 'hard_fail': False,
                'error_detail': {'type': type(exc).__name__, 'message': str(exc)},
                'turns': [{'index': i, 'message': turn['message'], 'status': 'error', 'executed': False,
                           'latency_ms': 0, 'error_detail': str(exc)} for i, turn in enumerate(case['turns'])],
                'assertions': [{'id': rule['id'], 'type': rule['type'], 'status': 'invalid',
                                'reason': 'Runtime unavailable', 'scores': {}} for rule in case['assertions']]}

    @observe_case
    async def _case(self, runtime, case, knowledge, namespace, index):
        user = f'{namespace}_u{index}'
        # Dataset identifiers never override isolated users or conversation prefixes.
        sessions = {}
        def conv(session):
            if session not in sessions:
                sessions[session] = f'{namespace}_c{index}_{len(sessions)}'
            return sessions[session]
        from memory.roles import MsgRole
        for seed in case['initial_state'].get('messages', []):
            await _await(runtime.memory.add_message(user, conv(seed.get('session', 'current')),
                                                    MsgRole(seed['role']), seed['content']))
        archived_sessions = {}
        active_session = case['turns'][0].get('session', 'current')
        for session in dict.fromkeys(seed.get('session', 'current') for seed in case['initial_state'].get('messages', [])):
            if session != active_session:
                reference = await _await(runtime.memory.archive_session(user, conv(session)))
                if not isinstance(reference, dict) or not reference.get('evidence_id'):
                    raise ValueError('Initial session archival returned no memory evidence')
                archived_sessions[session] = reference
        result = {'id': case['id'], 'scenario': case['scenario'], 'description': case['description'],
                  'user_id': user, 'turns': [], 'assertions': [],
                  'setup': {'status': 'completed', 'archived_sessions': archived_sessions}}
        for i, turn in enumerate(case['turns']):
            record = {'index': i, 'message': turn['message'], 'expected_intent': turn['expected_intent'],
                      'conv_id': conv(turn.get('session', 'current')), 'executed': True}
            before = time.perf_counter()
            try:
                reply = await asyncio.wait_for(_await(runtime.service.chat(turn['message'], user, record['conv_id'],
                                                wait_for_profile=True)), case['budgets'].get('turn_timeout_s', 120))
                if not isinstance(reply, dict) or not isinstance(reply.get('response'), str) or not reply['response'].strip():
                    raise ValueError('ChatService must return a nonempty actual response')
                if not isinstance(reply.get('tool_traces', []), list):
                    raise ValueError('tool_traces must be an array')
                json.dumps(reply, allow_nan=False)
                record.update(status='passed', actual=reply)
            except asyncio.TimeoutError:
                record.update(status='timeout', error_detail='ChatService turn timed out')
            except Exception as exc:
                record.update(status='error', error_detail=str(exc), error_type=type(exc).__name__)
            record['latency_ms'] = round((time.perf_counter() - before) * 1000, 3)
            if record['status'] == 'passed' and record['latency_ms'] > case['budgets'].get('max_latency_ms', math.inf):
                record.update(status='failed', error_detail='Turn exceeded max_latency_ms budget')
            result['turns'].append(record)
        all_actual = [r.get('actual', {}) for r in result['turns']]
        task_target = {'turns': all_actual, 'tool_traces': [t for a in all_actual for t in a.get('tool_traces', [])],
                       'memory_evidence_ids': [eid for a in all_actual for eid in a.get('memory_evidence_ids', [])],
                       'tools_used': [t for a in all_actual for t in a.get('tools_used', [])],
                       'response': '\n'.join(a.get('response', '') for a in all_actual),
                       'latency_ms': sum(r['latency_ms'] for r in result['turns'])}
        for rule in case['assertions']:
            selected = result['turns'][rule['turn']] if 'turn' in rule else None
            target = selected.get('actual') if selected is not None else task_target
            if target is None or (selected is None and any('actual' not in t for t in result['turns'])):
                outcome = {'status': 'invalid', 'reason': 'Required service output unavailable', 'scores': {}}
            else:
                outcome = await self._assert(rule, target, case, knowledge, result['turns'], archived_sessions=archived_sessions)
            outcome.update({k: rule[k] for k in ('id', 'type', 'turn', 'scope') if k in rule})
            outcome.update(required=rule.get('required', True), hard=rule.get('hard', False))
            result['assertions'].append(outcome)
            if selected is not None and selected['status'] == 'passed' and outcome['status'] != 'passed' and outcome['required']:
                selected['status'] = outcome['status']
        required = [r for r in result['assertions'] if r['required'] or r['hard']]
        result['hard_fail'] = any(r['hard'] and r['status'] == 'failed' for r in result['assertions'])
        result['invalid'] = any(r['status'] == 'invalid' for r in required)
        result['timeout'] = any(t['status'] == 'timeout' for t in result['turns']) or any(r['status'] == 'timeout' for r in required)
        result['error_count'] = int(any(t['status'] == 'error' for t in result['turns']))
        result['status'] = ('hard_fail' if result['hard_fail'] else 'error' if result['error_count'] else
                            'timeout' if result['timeout'] else 'invalid' if result['invalid'] else
                            'failed' if any(r['status'] != 'passed' for r in required)
                            or any(t['status'] != 'passed' for t in result['turns']) else 'passed')
        return result

    async def _assert(self, rule, target, case, knowledge, turns, *, archived_sessions=None):
        kind = rule['type']
        status, reason, scores = 'failed', 'Assertion did not match actual service evidence', {}
        try:
            if kind == 'semantic':
                if self.judge is None:
                    return {'status': 'invalid', 'reason': 'Semantic assertion requires an evidence-aware Judge', 'scores': {}}
                evidence = [copy.deepcopy(d) for d in knowledge if d['id'] in rule['evidence']]
                question = turns[rule['turn']]['message'] if 'turn' in rule else '\n'.join(t['message'] for t in turns)
                initial_history = '\n'.join(f"{message.get('session', 'current')}/{message['role']}: {message['content']}"
                                            for message in case['initial_state'].get('messages', []))
                judge = getattr(self.judge, 'judge', self.judge)
                judged = await asyncio.wait_for(_await(judge(question, target['response'],
                            context=initial_history or None, expected=rule['expected'], evidence=evidence)), case['budgets'].get('judge_timeout_s', 60))
                if getattr(judged, 'judge_failed', False) or (isinstance(judged, dict) and judged.get('judge_failed')):
                    raise ValueError(getattr(judged, 'error', None) or 'Judge failed')
                values = judged if isinstance(judged, dict) else vars(judged)
                for name in ('relevance', 'accuracy', 'completeness', 'helpfulness'):
                    value = values.get(name)
                    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError(f'Invalid Judge score: {name}')
                    scores[name] = float(value)
                scores['overall'] = statistics.mean(scores.values())
                passed = all(scores[name] >= rule.get('threshold', .75)
                             for name in ('relevance', 'accuracy', 'completeness', 'helpfulness'))
            elif kind == 'intent':
                passed = target.get('intent') == rule['expected']
            elif kind == 'agent':
                passed = target.get('primary_agent', target.get('agent_type')) == rule['expected']
            elif kind in {'tool_called', 'tool_absent'}:
                traces = _traces(target, rule['tool'])
                passed = (not traces and rule['tool'] not in target.get('tools_used', [])) if kind == 'tool_absent' else any(
                    t.get('success') == rule.get('success', True) for t in traces)
            elif kind in {'tool_argument', 'tool_result'}:
                key = 'input' if kind == 'tool_argument' else 'result_summary'
                passed = any(t.get('success') is True
                             and _path(t.get(key, t.get('data', {})), rule['path']) == rule['expected']
                             for t in _traces(target, rule['tool']))
            elif kind == 'field_equals':
                passed = _path(target, rule['path']) == rule['expected']
            elif kind == 'field_present':
                value = _path(target, rule['path'])
                passed = value is not _MISSING and value is not None
            elif kind == 'evidence':
                successful = [t for t in _traces(target) if t.get('success') is True and t.get('result_success') is not False]
                documents = {doc for trace in successful for doc in trace.get('document_ids', []) if isinstance(doc, str)}
                # Legacy injected fixtures may expose structured data; live traces use document_ids.
                documents.update(_evidence_ids([t.get('data') for t in successful]))
                chunks = {eid for trace in successful for eid in trace.get('evidence_ids', []) if isinstance(eid, str)}
                passed = set(rule['expected']) <= documents and set(rule.get('evidence_ids', [])) <= chunks
                wanted_docs, wanted_chunks = set(rule['expected']), set(rule.get('evidence_ids', []))
                scores['recall'] = (len(wanted_docs & documents) + len(wanted_chunks & chunks)) / (len(wanted_docs) + len(wanted_chunks))
            elif kind == 'memory_recalled':
                reference = (archived_sessions or {}).get(rule['session'], {})
                expected = reference.get('evidence_id')
                passed = bool(expected and expected in target.get('memory_evidence_ids', []))
                scores['recall'] = float(passed)
            elif kind == 'latency':
                value = target.get('latency_ms')
                passed = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= rule['max_ms']
            else:
                raise ValueError('Unknown assertion type')
            if passed:
                status, reason = 'passed', 'Matched actual service evidence'
        except asyncio.TimeoutError:
            status, reason, scores = 'timeout', 'Judge timed out', {}
        except Exception as exc:
            status, reason, scores = 'invalid', str(exc), {}
        return {'status': status, 'reason': reason, 'scores': scores}

    @staticmethod
    def _usage(turns):
        totals, observed = {}, 0
        for turn in turns:
            usage = turn.get('actual', {}).get('usage')
            if isinstance(usage, dict):
                observed += 1
                for name, value in usage.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                        totals[name] = totals.get(name, 0) + value
        return {'observed_turns': observed, 'totals': totals, 'status': 'observed' if observed else 'unavailable'}

    def _observe(self, report):
        if self.telemetry is None:
            return
        try:
            with self.telemetry.span('evaluation.scenario', metadata={'run_id': report['run_id'],
                    'dataset': report['dataset'], 'summary': report['summary']}) as observation:
                if observation is not None:
                    observation.score('case_pass_rate', report['pass_rate'])
        except Exception:
            pass  # Export failures must not change experiment outcomes.


async def _live(args, dataset):
    from services.runtime import create_runtime, runtime_config_from_env
    from evaluation.report_store import ReportStore
    config = runtime_config_from_env()
    if not config.get('api_key') or config['api_key'].strip().lower() in {'test', 'dummy', 'changeme', 'your-api-key'}:
        raise ValueError('A configured ANTHROPIC_API_KEY is required for explicit live replay')
    config['judge_enabled'] = args.judge
    judge, client = None, None
    if args.judge:
        from anthropic import AsyncAnthropic
        from evaluation.evaluator import LLMJudge
        from observability.instrumentation import AnthropicClientProxy
        client = AnthropicClientProxy(AsyncAnthropic(api_key=config['api_key'], **({'base_url': config['base_url']} if config.get('base_url') else {})))
        judge = LLMJudge(client, config.get('judge_model', config['model']))
    try:
        async def factory(**kwargs):
            return await _await(create_runtime(config, **kwargs))
        runner = ScenarioRunner(factory, judge=judge)
        limits = {key: getattr(args, key, None) for key in ('max_model_calls', 'max_model_seconds')}
        budget = EvaluationBudget(**limits) if any(value is not None for value in limits.values()) else None
        report = await runner.run(dataset, metadata={'mode': 'live', 'fingerprints': build_fingerprints(dataset, config)}, budget=budget)
        ReportStore(args.report_dir).save(report)
        return report
    finally:
        if client is not None:
            await client.close()


def main(argv=None):
    from evaluation.report_store import DEFAULT_REPORT_DIR, ReportStore
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('validate', 'live'), default='validate')
    parser.add_argument('--dataset', default=str(DEFAULT_DATASET))
    parser.add_argument('--report-dir', default=str(DEFAULT_REPORT_DIR))
    parser.add_argument('--judge', action='store_true', help='Explicitly enable model-based semantic grading in live mode')
    parser.add_argument('--max-model-calls', type=int, help='Maximum SDK create attempts, including Judge/profile; internal HTTP retries are not counted')
    parser.add_argument('--max-model-seconds', type=float, help='Deadline for provider waits and new case starts; initialization/cleanup may take longer')
    parser.add_argument('--limit', type=int, default=5, help='Maximum live cases; defaults to five. Validation always checks all cases.')
    parser.add_argument('--scenario', action='append', default=[], help='Select a scenario group for live replay; repeatable')
    parser.add_argument('--promote-baseline', metavar='RUN_ID', help='Explicitly freeze one archived run')
    parser.add_argument('--compare', nargs=2, metavar=('LEFT_RUN_ID', 'RIGHT_RUN_ID'))
    parser.add_argument('--vary', nargs='*', default=[])
    args = parser.parse_args(argv)
    if args.promote_baseline:
        output = ReportStore(args.report_dir).promote_baseline(args.promote_baseline)
    elif args.compare:
        output = ReportStore(args.report_dir).compare(*args.compare, vary=args.vary)
    else:
        dataset = load_dataset(args.dataset)
        validation = validate_dataset(dataset)
        if args.mode == 'validate':
            output = {**validation, 'mode': 'validate', 'status': 'validated', 'dataset_status': validation['status']}
        else:
            if args.limit < 1:
                parser.error('--limit must be positive')
            if set(args.scenario) - set(validation['scenarios']):
                parser.error('Unknown scenario group')
            selected = copy.deepcopy(dataset)
            selected['cases'] = [case for case in selected['cases']
                                 if not args.scenario or case['scenario'] in args.scenario][:args.limit]
            output = asyncio.run(_live(args, selected))
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
