import asyncio
from contextlib import contextmanager


def test_case_observation_links_run_case_chat_and_scores():
    async def run():
        from observability.experiments import observe_case, current_trace_metadata
        from observability.telemetry import get_telemetry
        events = []

        class Observation:
            def update(self, **fields):
                events.append(('update', fields))
            def score(self, name, value):
                events.append(('score', name, value))

        class Telemetry:
            @contextmanager
            def span(self, name, **fields):
                events.append(('span', name, fields))
                yield Observation()

        telemetry = Telemetry()
        class Runner:
            def __init__(self):
                self.telemetry = telemetry
            @observe_case
            async def case(self, runtime, case, knowledge, namespace, index):
                assert get_telemetry() is telemetry
                assert current_trace_metadata()['run_id'] == 'a' * 32
                assert current_trace_metadata()['case_id'] == 'sample'
                return {'status': 'passed', 'assertions': [{'status': 'passed'}]}
        result = await Runner().case(None, {'id': 'sample', 'scenario': 'billing'}, [], 'eval_' + 'a' * 32, 0)
        assert result['evaluation_overhead_usage']['model_calls'] == 0
        assert ('score', 'task_pass', 1.0) in events
        assert current_trace_metadata() == {}
        assert events[0][2]['metadata']['case_id'] == 'sample'
    asyncio.run(run())
