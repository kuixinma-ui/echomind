"""Strict, network-free scenario loading and snapshot fingerprints.

Assertions target an explicit zero-based ``turn`` or ``scope='task'``. Task
field paths start at {turns: [service replies], tool_traces: [...], ...}.
No recorded response is accepted as a substitute for calling ChatService.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from core.llm_utils import structured_thinking_policy

DEFAULT_DATASET = Path(__file__).parent / 'datasets' / 'customer_service_v1.json'
INTENTS = frozenset(('query', 'complaint', 'request', 'greeting', 'escalation', 'technical',
                     'billing', 'account', 'feedback', 'order_status', 'logistics', 'refund',
                     'invoice', 'payment_issue', 'account_security', 'technical_login',
                     'technical_crash', 'human_handoff', 'other'))
RULE_FIELDS = {
    'intent': {'expected'}, 'agent': {'expected'},
    'tool_called': {'tool', 'success'}, 'tool_absent': {'tool'},
    'tool_argument': {'tool', 'path', 'expected'}, 'tool_result': {'tool', 'path', 'expected'},
    'field_equals': {'path', 'expected'}, 'field_present': {'path'},
    'evidence': {'expected', 'evidence_ids'}, 'semantic': {'expected', 'evidence', 'threshold'},
    'latency': {'max_ms'},
    'memory_recalled': {'session'},
}
COMMON_RULE_FIELDS = {'id', 'type', 'turn', 'scope', 'required', 'hard'}
FINGERPRINT_FIELDS = ('dataset', 'model', 'prompt', 'skills', 'knowledge', 'code', 'judge', 'retrieval')


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise ValueError(f'{where} must be an object')
    unknown = set(obj) - allowed
    if unknown:
        raise ValueError(f'{where}: unknown fields {sorted(unknown)}')


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{where} must be nonempty text')


def _number(value, where, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{where} must be finite numeric')
    if value <= minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{where} is out of range')


def load_dataset(path=DEFAULT_DATASET):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    validate_dataset(data)
    return data


def validate_dataset(dataset):
    _keys(dataset, {'id', 'version', 'status', 'description', 'knowledge', 'cases'}, 'dataset')
    for name in ('id', 'version', 'status'):
        _text(dataset.get(name), f'dataset.{name}')
    knowledge = dataset.get('knowledge')
    if not isinstance(knowledge, list) or not knowledge:
        raise ValueError('knowledge must contain a fixed evidence snapshot')
    docs = set()
    for index, doc in enumerate(knowledge):
        _keys(doc, {'id', 'title', 'content', 'version', 'source', 'scope', 'effective_at', 'type'}, f'knowledge[{index}]')
        for name in ('id', 'title', 'content', 'version'):
            _text(doc.get(name), f'knowledge[{index}].{name}')
        if doc['id'] in docs:
            raise ValueError('Duplicate knowledge id')
        docs.add(doc['id'])
    cases = dataset.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases must contain at least one case')
    case_ids, groups, intents, turn_count, assertion_count = set(), {}, set(), 0, 0
    for case in cases:
        _keys(case, {'id', 'scenario', 'description', 'turns', 'initial_state', 'assertions', 'budgets', 'tags'}, 'case')
        for name in ('id', 'scenario', 'description'):
            _text(case.get(name), f'case.{name}')
        if case['id'] in case_ids:
            raise ValueError(f'Duplicate case id: {case["id"]}')
        case_ids.add(case['id'])
        groups[case['scenario']] = groups.get(case['scenario'], 0) + 1
        turns = case.get('turns')
        if not isinstance(turns, list) or not turns:
            raise ValueError('case.turns must be nonempty')
        for turn in turns:
            _keys(turn, {'message', 'expected_intent', 'session'}, 'turn')
            _text(turn.get('message'), 'turn.message')
            if turn.get('expected_intent') not in INTENTS:
                raise ValueError('turn.expected_intent is unknown')
            if 'session' in turn:
                _text(turn['session'], 'turn.session')
            intents.add(turn['expected_intent'])
        turn_count += len(turns)
        state = case.get('initial_state')
        _keys(state, {'messages'}, 'initial_state')
        if not isinstance(state.get('messages', []), list):
            raise ValueError('initial_state.messages must be an array')
        for message in state.get('messages', []):
            _keys(message, {'session', 'role', 'content'}, 'initial message')
            _text(message.get('content'), 'initial message.content')
            _text(message.get('session', 'current'), 'initial message.session')
            if message.get('role') not in {'user', 'assistant', 'system'}:
                raise ValueError('initial message.role is unknown')
        budgets = case.get('budgets')
        _keys(budgets, {'turn_timeout_s', 'judge_timeout_s', 'max_latency_ms'}, 'budgets')
        for name, value in budgets.items():
            _number(value, f'budgets.{name}')
        assertions = case.get('assertions')
        if not isinstance(assertions, list) or not assertions:
            raise ValueError('case.assertions must be nonempty')
        ids = set()
        for rule in assertions:
            if not isinstance(rule, dict) or rule.get('type') not in RULE_FIELDS:
                raise ValueError('Unknown assertion rule type')
            kind = rule['type']
            _keys(rule, COMMON_RULE_FIELDS | RULE_FIELDS[kind], 'assertion')
            _text(rule.get('id'), 'assertion.id')
            if rule['id'] in ids:
                raise ValueError('Duplicate assertion id')
            ids.add(rule['id'])
            if ('turn' in rule) == ('scope' in rule):
                raise ValueError('Assertion requires exactly one turn or task scope')
            if 'turn' in rule and (isinstance(rule['turn'], bool) or not isinstance(rule['turn'], int)
                                   or not 0 <= rule['turn'] < len(turns)):
                raise ValueError('Assertion turn is outside case')
            if 'scope' in rule and rule['scope'] != 'task':
                raise ValueError('Only task scope is supported')
            for flag in ('required', 'hard', 'success'):
                if flag in rule and not isinstance(rule[flag], bool):
                    raise ValueError(f'Assertion {flag} must be boolean')
            needed = RULE_FIELDS[kind] - {'success', 'threshold', 'evidence_ids'}
            if not needed <= rule.keys():
                raise ValueError(f'{kind} missing fields: {sorted(needed - rule.keys())}')
            for name in ('tool', 'path'):
                if name in rule:
                    _text(rule[name], f'assertion.{name}')
            if kind == 'intent' and rule['expected'] not in INTENTS:
                raise ValueError('Assertion expected intent is unknown')
            if kind == 'semantic':
                _text(rule['expected'], 'semantic.expected')
                evidence = rule['evidence']
                if not isinstance(evidence, list) or not evidence or any(e not in docs for e in evidence):
                    raise ValueError('Semantic assertions require known fixed evidence ids')
                _number(rule.get('threshold', .75), 'semantic.threshold', maximum=1)
            if kind == 'memory_recalled':
                _text(rule.get('session'), 'memory_recalled.session')
                if rule['session'] not in {message.get('session', 'current') for message in state.get('messages', [])}:
                    raise ValueError('Memory recall assertion requires a seeded session')
            if kind == 'evidence' and (not isinstance(rule['expected'], list) or not rule['expected']
                                      or any(e not in docs for e in rule['expected'])):
                raise ValueError('Evidence assertion requires known evidence ids')
            if 'evidence_ids' in rule and (not isinstance(rule['evidence_ids'], list)
                    or not rule['evidence_ids'] or any(not isinstance(e, str) or not e.startswith('ctx_') for e in rule['evidence_ids'])):
                raise ValueError('evidence_ids must contain explicit versioned ctx_ chunk ids')
            if kind == 'latency':
                _number(rule['max_ms'], 'latency.max_ms')
        assertion_count += len(assertions)
    # Reject NaN and non-JSON objects anywhere, including expected structures.
    try:
        stable_hash(dataset)
    except (TypeError, ValueError) as exc:
        raise ValueError('Dataset must contain finite JSON values') from exc
    return {'id': dataset['id'], 'version': dataset['version'], 'status': dataset['status'],
            'case_count': len(cases), 'turn_count': turn_count, 'assertion_count': assertion_count,
            'scenario_count': len(groups), 'scenarios': groups, 'intents': sorted(intents),
            'fingerprint': stable_hash(dataset)}


def _tree_snapshot(root, pattern='*'):
    root = Path(root)
    if not root.exists():
        return {'missing': True}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob(pattern)) if p.is_file()
            and not {'__pycache__', '.git', 'reports', 'tests'} & set(p.relative_to(root).parts)}


def build_fingerprints(dataset, config):
    """Hash actual local snapshots; never copy credentials or full config to reports."""
    backend = Path(__file__).resolve().parents[1]
    skills = _tree_snapshot(config.get('skills_dir', backend / 'skills'))
    prompt = {'sources': {p.name: p.read_text(encoding='utf-8') for p in (backend / 'agents').glob('*.py')},
              'skills_max_prompt_chars': config.get('skills_max_prompt_chars', 5000)}
    structured = {'thinking': structured_thinking_policy(config.get('structured_thinking')),
                  'implementation': hashlib.sha256((backend / 'core' / 'llm_utils.py').read_bytes()).hexdigest()}
    judge = {'enabled': bool(config.get('judge_enabled', False)),
             'model': config.get('judge_model', config.get('model', '')),
             'structured_response': structured,
             'implementation': hashlib.sha256((backend / 'evaluation' / 'evaluator.py').read_bytes()).hexdigest()}
    retrieval = {name: config.get(name, default) for name, default in
                 (('rewrite_policy', 'always'), ('rerank_policy', 'always'), ('retrieval_top_k', 5),
                  ('retrieval_timeout_s', 30.0), ('adaptive_rules_version', '1'))}
    retrieval['structured_response'] = structured
    model = {'default': config.get('model', 'unspecified'),
             'provider_endpoint': stable_hash(config.get('base_url') or 'anthropic_default'),
             'agents': {role: os.getenv(f'ECHOMIND_{role.upper()}_MODEL') or config.get('model', 'unspecified')
                        for role in ('general', 'technical', 'billing', 'escalation')},
             'parameters': {name: value for name, value in os.environ.items()
                            if name.startswith('ECHOMIND_') and name.endswith(('_MAX_TOKENS', '_TEMPERATURE'))}}
    return {'dataset': stable_hash(dataset), 'model': stable_hash(model),
            'prompt': stable_hash(prompt), 'skills': stable_hash(skills),
            'knowledge': stable_hash(dataset['knowledge']), 'code': stable_hash(_tree_snapshot(backend, '*.py')),
            'judge': stable_hash(judge), 'retrieval': stable_hash(retrieval)}
