"""The timeout owner, rather than a second clock sample, defines budget failure."""
import asyncio
from types import SimpleNamespace

import pytest

from observability.budgets import EvaluationBudget, EvaluationBudgetExceeded, bind_budget
from observability.instrumentation import AnthropicClientProxy, bind_usage


def proxy_for(create):
    return AnthropicClientProxy(SimpleNamespace(messages=SimpleNamespace(create=create)))


def test_budget_timer_marks_deadline_even_when_budget_clock_has_not_advanced():
    async def run():
        cancelled = asyncio.Event()

        async def create(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        budget = EvaluationBudget(max_model_seconds=.001, clock=lambda: 100)
        with bind_budget(budget):
            with pytest.raises(EvaluationBudgetExceeded, match='deadline'):
                await proxy_for(create).messages.create()
        assert cancelled.is_set()
        assert budget.remaining_seconds() > 0
        assert budget.stop_reason == 'deadline'
        assert budget.started_calls == 1

    asyncio.run(run())


def test_provider_timeout_is_not_budget_timeout_when_budget_clock_jumps_forward():
    async def run():
        now = [100]
        original = asyncio.TimeoutError('provider timeout')

        async def create(**kwargs):
            now[0] = 200
            raise original

        budget = EvaluationBudget(max_model_seconds=10, clock=lambda: now[0])
        with bind_budget(budget):
            with pytest.raises(asyncio.TimeoutError) as raised:
                await proxy_for(create).messages.create()
        assert raised.value is original
        assert budget.remaining_seconds() < 0
        assert budget.stop_reason is None

    asyncio.run(run())


def test_parent_cancellation_remains_cancellation_and_reaches_provider():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def create(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        budget = EvaluationBudget(max_model_seconds=10)
        with bind_budget(budget):
            task = asyncio.create_task(proxy_for(create).messages.create())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()
        assert budget.stop_reason is None

    asyncio.run(run())


def test_provider_suppressing_budget_cancellation_cannot_report_success():
    async def run():
        cancelled = asyncio.Event()

        async def create(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=3))

        budget = EvaluationBudget(max_model_seconds=.001, clock=lambda: 100)
        with bind_budget(budget), bind_usage() as usage:
            with pytest.raises(EvaluationBudgetExceeded, match='deadline'):
                await proxy_for(create).messages.create()
        assert cancelled.is_set()
        assert budget.stop_reason == 'deadline'
        assert usage.model_calls == 1
        assert usage.input_tokens == 0  # no successful response was published

    asyncio.run(run())


def test_success_before_budget_timer_keeps_response_and_usage():
    async def run():
        response = SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=3, output_tokens=2))

        async def create(**kwargs):
            await asyncio.sleep(0)
            return response

        budget = EvaluationBudget(max_model_seconds=10)
        with bind_budget(budget), bind_usage() as usage:
            assert await proxy_for(create).messages.create() is response
        assert budget.stop_reason is None
        assert usage.model_calls == 1
        assert usage.input_tokens == 3
        assert usage.output_tokens == 2

    asyncio.run(run())


def test_provider_translating_budget_cancellation_cannot_hide_deadline():
    async def run():
        async def create(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError('provider translated cancellation') from None

        budget = EvaluationBudget(max_model_seconds=.001, clock=lambda: 100)
        with bind_budget(budget):
            with pytest.raises(EvaluationBudgetExceeded, match='deadline'):
                await proxy_for(create).messages.create()
        assert budget.stop_reason == 'deadline'

    asyncio.run(run())


def test_external_cancellation_during_deadline_cleanup_keeps_cancellation():
    async def run():
        cleaning_up = asyncio.Event()

        async def create(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning_up.set()
                await asyncio.Event().wait()

        budget = EvaluationBudget(max_model_seconds=.001, clock=lambda: 100)
        with bind_budget(budget):
            task = asyncio.create_task(proxy_for(create).messages.create())
            await cleaning_up.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert budget.stop_reason is None

    asyncio.run(run())


def test_budget_expiring_before_provider_entry_blocks_immediate_response():
    async def run():
        samples = iter([100, 100, 200])
        calls = []

        async def create(**kwargs):
            calls.append(1)
            return SimpleNamespace(content=[])

        budget = EvaluationBudget(max_model_seconds=10, clock=lambda: next(samples))
        with bind_budget(budget):
            with pytest.raises(EvaluationBudgetExceeded, match='deadline'):
                await proxy_for(create).messages.create()
        assert calls == []
        assert budget.stop_reason == 'deadline'

    asyncio.run(run())


@pytest.mark.parametrize('translated', [False, True])
def test_provider_cannot_hide_parent_cancellation(translated):
    async def run():
        entered = asyncio.Event()

        async def create(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if translated:
                    raise RuntimeError('provider translated parent cancellation') from None
                return SimpleNamespace(content=[])

        budget = EvaluationBudget(max_model_seconds=10)
        with bind_budget(budget):
            task = asyncio.create_task(proxy_for(create).messages.create())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert budget.stop_reason is None

    asyncio.run(run())
