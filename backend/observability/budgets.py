"""Explicit evaluation limits, shared by child tasks; no pricing estimates."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time


class EvaluationBudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('evaluation_budget_' + reason)


class EvaluationBudget:
    def __init__(self, *, max_model_calls=None, max_model_seconds=None, clock=time.monotonic):
        if max_model_calls is not None and (type(max_model_calls) is not int or max_model_calls < 1):
            raise ValueError('max_model_calls must be a positive integer')
        if max_model_seconds is not None and (type(max_model_seconds) not in (int, float)
                or not math.isfinite(max_model_seconds) or max_model_seconds <= 0):
            raise ValueError('max_model_seconds must be positive and finite')
        self.max_model_calls = max_model_calls
        self.max_model_seconds = max_model_seconds
        self.clock = clock
        self.started_at = clock()
        self.started_calls = 0
        self.blocked_calls = 0
        self.stop_reason = None

    def remaining_seconds(self):
        if self.max_model_seconds is None:
            return None
        return self.started_at + self.max_model_seconds - self.clock()

    def stop(self, reason):
        self.stop_reason = self.stop_reason or reason
        return EvaluationBudgetExceeded(self.stop_reason)

    def check(self):
        if self.stop_reason:
            raise EvaluationBudgetExceeded(self.stop_reason)
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise self.stop('deadline')
        if self.max_model_calls is not None and self.started_calls >= self.max_model_calls:
            raise self.stop('model_calls')

    def reserve(self):
        # No await between check and increment: asyncio child tasks share one
        # mutable budget but cannot interleave this reservation operation.
        try:
            self.check()
        except EvaluationBudgetExceeded:
            self.blocked_calls += 1
            raise
        self.started_calls += 1

    def snapshot(self):
        return {'max_model_calls': self.max_model_calls, 'max_model_seconds': self.max_model_seconds,
                'started_calls': self.started_calls, 'blocked_calls': self.blocked_calls,
                'stop_reason': self.stop_reason, 'elapsed_ms': round((self.clock() - self.started_at) * 1000, 3),
                'call_unit': 'sdk_create_attempts', 'http_retries_counted': False,
                'deadline_scope': 'provider_wait_and_case_start'}


_BUDGET = ContextVar('echomind_evaluation_budget', default=None)


def current_budget():
    return _BUDGET.get()


@contextmanager
def bind_budget(budget):
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def mark_budget_error(result, budget):
    """A component fallback must not turn a rejected request into a pass."""
    if budget is not None and budget.stop_reason:
        result.update(status='error', error_count=1, budget_exceeded=True,
                      budget_reason=budget.stop_reason,
                      error_detail={'type': 'EvaluationBudgetExceeded', 'reason': budget.stop_reason})
    return result
