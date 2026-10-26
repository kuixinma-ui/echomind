"""Link application observations to the local scenario run and case."""
from contextvars import ContextVar
from functools import wraps

from observability.instrumentation import bind_usage
from observability.telemetry import bind_telemetry, get_telemetry
from observability.budgets import current_budget, mark_budget_error

_TRACE_METADATA = ContextVar('echomind_experiment_metadata', default=None)


def current_trace_metadata():
    return dict(_TRACE_METADATA.get() or {})


def observe_case(function):
    @wraps(function)
    async def observed(self, runtime, case, knowledge, namespace, index):
        metadata = {'run_id': namespace.removeprefix('eval_'), 'case_id': case['id'],
                    'scenario': case['scenario']}
        token = _TRACE_METADATA.set(metadata)
        adapter = self.telemetry if self.telemetry is not None else get_telemetry()
        try:
            with bind_telemetry(adapter), bind_usage() as overhead, adapter.span(
                    'evaluation.case', kind='evaluator', metadata=metadata) as observation:
                result = await function(self, runtime, case, knowledge, namespace, index)
                mark_budget_error(result, current_budget())
                result['evaluation_overhead_usage'] = overhead.snapshot()
                observation.update(output={'status': result['status']}, metadata={
                    **metadata, 'assertions': len(result.get('assertions', []))})
                observation.score('task_pass', float(result['status'] == 'passed'))
                observation.score('hard_fail', float(result.get('hard_fail', False)))
                observation.score('invalid', float(result.get('invalid', False)))
                return result
        finally:
            _TRACE_METADATA.reset(token)
    return observed
