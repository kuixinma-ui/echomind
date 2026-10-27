"""Returned service usage survives rejection of an unusable actual response."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from evaluation.scenario_runner import ScenarioRunner
from observability.budgets import EvaluationBudget, current_budget


TRACE_USAGE = {
    'input_tokens': 565,
    'output_tokens': 1176,
    'cache_read_input_tokens': 896,
    'cache_creation_input_tokens': 0,
    'model_calls': 3,
}


def memory_case():
    return {
        'id': 'usage_rejection', 'version': '1', 'status': 'synthetic_pending_review',
        'knowledge': [{'id': 'payment', 'title': 'Payment policy', 'content': 'Synthetic policy', 'version': '1'}],
        'cases': [{
            'id': 'memory_09', 'scenario': 'cross_session_memory', 'description': 'Correct prior payment facts',
            'turns': [{'message': 'One bank payment was reversed.', 'expected_intent': 'payment_issue'}],
            'initial_state': {'messages': [{'session': 'prior', 'role': 'user', 'content': 'Two payments of 88.'}]},
            'budgets': {'turn_timeout_s': 1},
            'assertions': [
                {'id': 'intent', 'type': 'intent', 'turn': 0, 'expected': 'payment_issue'},
                {'id': 'answer', 'type': 'field_present', 'turn': 0, 'path': 'response'},
                {'id': 'routing', 'type': 'agent', 'turn': 0, 'expected': 'billing'},
                {'id': 'latency', 'type': 'latency', 'turn': 0, 'max_ms': 120000},
                {'id': 'policy', 'type': 'semantic', 'scope': 'task', 'expected': 'Use current facts', 'evidence': ['payment']},
                {'id': 'memory', 'type': 'memory_recalled', 'turn': 0, 'session': 'prior', 'hard': True},
            ],
        }],
    }


class ReturnedReplyRuntime:
    def __init__(self, reply):
        self.reply = reply
        self.closed = False
        self.service = SimpleNamespace(chat=self.chat)
        self.memory = SimpleNamespace(add_message=self.add_message, archive_session=self.archive_session)

    async def chat(self, *args, **kwargs):
        return self.reply

    async def add_message(self, *args):
        pass

    async def archive_session(self, user_id, conv_id):
        return {'evidence_id': 'mem_fixture', 'source_conv': conv_id, 'message_count': 1}

    async def close(self, cleanup=False):
        self.closed = cleanup


def run_reply(reply, *, data=None):
    runtime = ReturnedReplyRuntime(reply)
    report = asyncio.run(ScenarioRunner(lambda **_: runtime).run(data or memory_case()))
    assert runtime.closed
    return report


def test_empty_reply_keeps_observed_usage_without_becoming_valid_actual():
    reply = {'response': '', 'tool_traces': [], 'usage': {**TRACE_USAGE, 'api_key': 'secret-sentinel', 'arbitrary': 42}}
    report = run_reply(reply)
    case = report['cases'][0]
    turn = case['turns'][0]
    assert case['status'] == 'error' and case['invalid'] and case['error_count'] == 1
    assert turn['status'] == 'error' and turn['error_type'] == 'ValueError'
    assert turn['error_detail'] == 'ChatService must return a nonempty actual response'
    assert 'actual' not in turn
    assert len(case['assertions']) == 6
    assert all(rule['status'] == 'invalid' for rule in case['assertions'])
    assert report['passed'] == 0 and report['total'] == 1 and report['valid_count'] == 0
    assert report['usage'] == {'observed_turns': 1, 'totals': TRACE_USAGE, 'status': 'observed'}
    assert turn['usage'] == TRACE_USAGE
    reply['usage']['input_tokens'] = 999
    assert turn['usage']['input_tokens'] == 565
    encoded = json.dumps(report, allow_nan=False)
    assert 'secret-sentinel' not in encoded and 'api_key' not in encoded and 'arbitrary' not in encoded


@pytest.mark.parametrize('unusable', [-1, True, 1.5, float('nan'), float('inf'), '12', None, {}])
def test_rejected_reply_usage_discards_invalid_values_and_unknown_keys(unusable):
    report = run_reply({'response': '', 'usage': {
        'input_tokens': unusable, 'output_tokens': 0, 'cache_read_input_tokens': unusable,
        'cache_creation_input_tokens': unusable, 'model_calls': 3,
        'untrusted_counter': 200, 'secret': 'secret-sentinel',
    }})
    assert report['usage']['totals'] == {'output_tokens': 0, 'model_calls': 3}
    assert report['cases'][0]['turns'][0]['usage'] == {'output_tokens': 0, 'model_calls': 3}
    assert 'secret-sentinel' not in json.dumps(report, allow_nan=False)


def test_valid_reply_usage_is_counted_once():
    data = memory_case()
    data['cases'][0]['assertions'] = [{'id': 'intent', 'type': 'intent', 'turn': 0, 'expected': 'payment_issue'}]
    report = run_reply({'response': 'Use the reversed payment status.', 'intent': 'payment_issue',
                        'tool_traces': [], 'usage': copy.deepcopy(TRACE_USAGE)}, data=data)
    turn = report['cases'][0]['turns'][0]
    assert report['passed'] == 1 and turn['actual']['response']
    assert report['usage'] == {'observed_turns': 1, 'totals': TRACE_USAGE, 'status': 'observed'}


def test_pre_reply_error_retains_attempt_budget_without_inventing_usage():
    class FailedRuntime(ReturnedReplyRuntime):
        async def chat(self, *args, **kwargs):
            current_budget().reserve()
            raise RuntimeError('Provider did not return a reply')

    runtime = FailedRuntime(None)
    report = asyncio.run(ScenarioRunner(lambda **_: runtime).run(
        memory_case(), budget=EvaluationBudget(max_model_calls=10)))
    assert report['cases'][0]['status'] == 'error'
    assert report['budget']['started_calls'] == 1
    assert report['usage'] == {'observed_turns': 0, 'totals': {}, 'status': 'unavailable'}
    assert 'usage' not in report['cases'][0]['turns'][0]


def test_legacy_valid_turn_usage_remains_available_without_new_capture_field():
    assert ScenarioRunner._usage([{'actual': {'usage': {**TRACE_USAGE, 'unknown': 99, 'api_key': 'secret-sentinel'}}}]) == {
        'observed_turns': 1, 'totals': TRACE_USAGE, 'status': 'observed',
    }
