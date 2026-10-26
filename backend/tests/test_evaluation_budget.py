"""Run-scoped model budgets must fail closed across caught provider failures."""
import asyncio
import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from observability.budgets import EvaluationBudget, EvaluationBudgetExceeded, bind_budget
from observability.instrumentation import AnthropicClientProxy, bind_usage


def test_rejected_calls_never_reach_provider_or_count_as_sent():
    async def run():
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=3, output_tokens=2))
        proxy = AnthropicClientProxy(SimpleNamespace(messages=SimpleNamespace(create=create)))
        budget = EvaluationBudget(max_model_calls=1)
        with bind_budget(budget), bind_usage() as usage:
            await proxy.messages.create(model='synthetic')
            with pytest.raises(EvaluationBudgetExceeded, match='model_calls'):
                await proxy.messages.create(model='synthetic')
        assert len(calls) == usage.model_calls == budget.started_calls == 1
        assert budget.blocked_calls == 1
    asyncio.run(run())


def test_concurrent_tasks_share_one_budget_and_separate_runs_restore_context():
    async def run():
        calls = []
        async def create(**kwargs):
            calls.append(1)
            await asyncio.sleep(0)
            return SimpleNamespace(content=[])
        proxy = AnthropicClientProxy(SimpleNamespace(messages=SimpleNamespace(create=create)))
        with bind_budget(EvaluationBudget(max_model_calls=2)):
            outcomes = await asyncio.gather(*(proxy.messages.create() for _ in range(5)), return_exceptions=True)
        assert len(calls) == 2
        assert sum(isinstance(x, EvaluationBudgetExceeded) for x in outcomes) == 3
        await proxy.messages.create()  # no inherited budget after context exit
        assert len(calls) == 3
    asyncio.run(run())


def test_deadline_cancels_an_inflight_provider():
    async def run():
        cancelled = asyncio.Event()
        async def create(**kwargs):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()
        proxy = AnthropicClientProxy(SimpleNamespace(messages=SimpleNamespace(create=create)))
        budget = EvaluationBudget(max_model_seconds=.03)
        with bind_budget(budget):
            with pytest.raises(EvaluationBudgetExceeded, match='deadline'):
                await proxy.messages.create()
        assert cancelled.is_set()
        assert budget.snapshot()['stop_reason'] == 'deadline'
    asyncio.run(run())


def test_runner_keeps_denominator_cleanup_and_does_not_hide_caught_budget_errors():
    from evaluation.scenario_runner import ScenarioRunner
    from tests.test_scenarios import Runtime, dataset, reply
    async def run():
        base = dataset()
        extra = copy.deepcopy(base['cases'][0]);extra['id'] = 'second'
        base['cases'].append(extra)
        runtime = Runtime()
        calls = []
        scores = []
        class Telemetry:
            @contextmanager
            def span(self, *args, **kwargs):
                yield SimpleNamespace(update=lambda **kwargs: None,
                                      score=lambda name, value: scores.append((name, value)))
        async def create(**kwargs):
            calls.append(1)
            return SimpleNamespace(content=[])
        proxy = AnthropicClientProxy(SimpleNamespace(messages=SimpleNamespace(create=create)))
        async def swallowing_chat(*args, **kwargs):
            for _ in range(2):
                try:
                    await proxy.messages.create()
                except EvaluationBudgetExceeded:
                    pass  # simulate a component's ordinary fallback
            return reply()
        runtime.service.chat = swallowing_chat
        report = await ScenarioRunner(lambda **kwargs: runtime, telemetry=Telemetry()).run(
            base, budget=EvaluationBudget(max_model_calls=1))
        assert report['total'] == 2 and report['passed'] == 0
        assert all(case['status'] == 'error' for case in report['cases'])
        assert len(calls) == 1 and runtime.closed is True
        assert report['budget']['stop_reason'] == 'model_calls'
        assert report['status'] == 'error'
        assert ('task_pass', 0.0) in scores and ('task_pass', 1.0) not in scores
    asyncio.run(run())


@pytest.mark.parametrize('kwargs', [{'max_model_calls':0},{'max_model_calls':True},
                                    {'max_model_seconds':float('nan')},{'max_model_seconds':-1}])
def test_invalid_budget_is_rejected(kwargs):
    with pytest.raises(ValueError):
        EvaluationBudget(**kwargs)


def test_provider_timeout_before_budget_deadline_is_not_mislabeled():
    async def run():
        async def create(**kwargs):
            raise asyncio.TimeoutError()
        proxy = AnthropicClientProxy(SimpleNamespace(messages=SimpleNamespace(create=create)))
        budget = EvaluationBudget(max_model_seconds=10)
        with bind_budget(budget):
            with pytest.raises(asyncio.TimeoutError):
                await proxy.messages.create()
        assert budget.stop_reason is None
    asyncio.run(run())


def test_interrupted_reports_cannot_promote_or_compare(tmp_path):
    from evaluation.report_store import ReportStore
    from tests.test_report_store import report
    store = ReportStore(tmp_path)
    left = report('budget-left')
    right = report('budget-right')
    right['budget'] = {'stop_reason': 'model_calls'}
    store.save(left);store.save(right)
    with pytest.raises(ValueError, match='budget'):
        store.promote_baseline('budget-right')
    result = store.compare('budget-left', 'budget-right')
    assert not result['comparable'] and 'budget' in result['reason'].lower()
