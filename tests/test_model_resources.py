"""Bounded CPU checks for single-run model resource accounting."""

import asyncio

import pytest

from prompt_enhancer.model_resources import BackendConcurrency, BudgetExhausted, SingleRunBudgetLedger


def test_budget_concurrent_reservations_and_single_use():
    async def run():
        ledger = SingleRunBudgetLedger({'cloud': 3})

        async def reserve():
            try:
                return await ledger.reserve('cloud')
            except BudgetExhausted:
                return None

        tokens = [token for token in await asyncio.gather(*(reserve() for _ in range(20))) if token]
        assert len(tokens) == 3
        await ledger.commit(tokens[0])
        await ledger.release(tokens[1])
        with pytest.raises(ValueError):
            await ledger.release(tokens[1])
        await ledger.commit(await ledger.reserve('cloud'))
        with pytest.raises(BudgetExhausted):
            await ledger.reserve('cloud')

    asyncio.run(run())


def test_concurrency_limit_and_cancellation():
    async def run():
        guard = BackendConcurrency({'main': 2})
        active = peak = 0

        async def call():
            nonlocal active, peak
            async with guard.for_backend('main'):
                active += 1
                peak = max(active, peak)
                try:
                    await asyncio.sleep(0.01)
                finally:
                    active -= 1

        await asyncio.gather(*(call() for _ in range(8)))
        assert peak == 2
        task = asyncio.create_task(call())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(call(), timeout=1)
        assert active == 0

    asyncio.run(run())


@pytest.mark.parametrize('value', [-1, True, 1.5])
def test_invalid_limits_refused(value):
    with pytest.raises(ValueError):
        SingleRunBudgetLedger({'cloud': value})
    with pytest.raises(ValueError):
        BackendConcurrency({'main': value})


def test_zero_concurrency_refused():
    with pytest.raises(ValueError):
        BackendConcurrency({'main': 0})
