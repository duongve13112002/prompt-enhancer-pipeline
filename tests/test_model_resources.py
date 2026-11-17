"""Bounded CPU checks for single-run model resource accounting."""

import asyncio

from hypothesis import given, settings, strategies as st
import pytest

from prompt_enhancer.model_resources import (CAPACITY_FIELDS, BackendConcurrency, BudgetExhausted,
                                             SingleRunBudgetLedger, await_owned, flow)


def test_repeated_stop_awaits_owned_flow_cleanup():
    async def exercise():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finished = []

        async def worker(item):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.append(item)

        task = asyncio.create_task(flow([0], worker, 1))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished == [0]
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(exercise())


def test_owned_cleanup_propagates_error_and_late_cancel():
    async def exercise():
        async def broken():
            raise ValueError('labelled cleanup error')

        with pytest.raises(ValueError, match='labelled cleanup'):
            await await_owned(broken())
        finished = []

        async def cleanup():
            await asyncio.sleep(.01)
            finished.append(True)

        task = asyncio.create_task(await_owned(cleanup()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True]

    asyncio.run(exercise())


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
        from concurrency_fixtures import ArrivalGate
        guard = BackendConcurrency({'main': 2})
        gate = ArrivalGate(2)
        active = peak = 0

        async def call():
            nonlocal active, peak
            async with guard.for_backend('main'):
                active += 1
                peak = max(active, peak)
                try:
                    await gate.arrive()
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


# RES-1 (decision 149): capacity records and the per-item `flow` helper.

class FakeClock:
    """Labelled test double: a clock that moves only when the test sets it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_capacity_window_integrates_in_flight_busy_and_idle():
    async def run():
        clock = FakeClock()
        guard = BackendConcurrency({'main': 2}, clock=clock)
        window = guard.open_window('main')
        a, b, c = (guard.for_backend('main') for _ in range(3))
        for when, step in ((1, a.__aenter__), (2, b.__aenter__), (4, lambda: a.__aexit__(None, None, None)),
                           (5, lambda: b.__aexit__(None, None, None)), (8, c.__aenter__),
                           (9, lambda: c.__aexit__(None, None, None))):
            clock.now = when
            await step()
        clock.now = 50  # idle time after the last request is not an idle gap
        return window.close()

    record = asyncio.run(run())
    assert tuple(record) == CAPACITY_FIELDS
    assert record == {'max_concurrency': 2, 'requests': 3, 'peak_in_flight': 2, 'busy_s': 5.0, 'idle_gap_s': 3.0,
                      'in_flight_slot_s': 7.0}


def test_capacity_window_opened_mid_flight_and_closed_window_stops_recording():
    async def run():
        clock = FakeClock()
        guard = BackendConcurrency({'main': 3}, clock=clock)
        slot = guard.for_backend('main')
        await slot.__aenter__()
        clock.now = 1
        window = guard.open_window('main')
        clock.now = 3
        await slot.__aexit__(None, None, None)
        first = window.close()
        async with guard.for_backend('main'):
            clock.now = 9
        return first

    record = asyncio.run(run())
    assert record['requests'] == 0 and record['peak_in_flight'] == 1
    assert record['busy_s'] == 2.0 and record['in_flight_slot_s'] == 2.0


@pytest.mark.parametrize('limit', [1, 3])
def test_in_flight_follows_configured_limit(limit):
    """Override test: the recorded peak equals the configured `max_concurrency`, never more."""
    async def run():
        from concurrency_fixtures import ArrivalGate
        guard = BackendConcurrency({'main': limit})
        gate = ArrivalGate(limit)
        window = guard.open_window('main')

        async def call():
            async with guard.for_backend('main'):
                await gate.arrive()

        await asyncio.gather(*(call() for _ in range(3 * limit)))
        return window.close()

    record = asyncio.run(run())
    assert record['peak_in_flight'] == limit and record['requests'] == 3 * limit
    assert record['max_concurrency'] == limit


def test_capacity_arrival_gate_is_bounded_and_stays_open():
    from concurrency_fixtures import ArrivalGate

    async def run():
        missing = ArrivalGate(2, timeout_s=0.01)
        with pytest.raises(AssertionError, match='1/2 capacity arrivals'):
            await missing.arrive()
        gate = ArrivalGate(2)
        await asyncio.gather(gate.arrive(), gate.arrive())
        await gate.arrive()  # a partial final group must not wait for a second arrival
        assert gate.arrived == 3

    asyncio.run(run())


def test_cancelled_wait_for_a_slot_is_not_counted():
    async def run():
        guard = BackendConcurrency({'main': 1})
        window = guard.open_window('main')
        async with guard.for_backend('main'):
            waiter = asyncio.create_task(guard.for_backend('main').__aenter__())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        return window.close()

    record = asyncio.run(run())
    assert record['requests'] == 1 and record['peak_in_flight'] == 1


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, None])
def test_flow_limit_refused(limit):
    async def work(item):
        return item

    with pytest.raises(ValueError):
        asyncio.run(flow([1], work, limit))


def test_flow_has_no_wave_barrier():
    """One slow item holds one slot only: every other item finishes while it is still running."""
    finished = []

    async def work(item):
        for _ in range(200 if item == 0 else 1):
            await asyncio.sleep(0)
        finished.append(item)
        return item * 10

    results = asyncio.run(flow(range(9), work, 3))
    assert results == [item * 10 for item in range(9)]
    assert finished[-1] == 0 and sorted(finished[:-1]) == list(range(1, 9))


def test_flow_stop_does_not_wait_for_running_items():
    """A stop cancels at once: items blocked on a reply that never comes are cancelled, not drained."""
    started, cancelled = [], []

    async def work(item):
        started.append(item)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(item)
            raise

    async def run():
        task = asyncio.create_task(flow(range(3), work, 2))
        while len(started) < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
    assert started == [0, 1] and sorted(cancelled) == [0, 1]


def test_flow_empty_and_admission_error():
    async def work(item):
        return item

    assert asyncio.run(flow([], work, 2)) == []

    def broken(item):
        raise LookupError(item)

    with pytest.raises(LookupError):
        asyncio.run(flow([1, 2], broken, 2))


class Scenario:
    """Labelled worker double: a plain function logs admission, its coroutine yields `delays[item]` times."""

    def __init__(self, delays, failing):
        self.delays, self.failing = delays, failing
        self.log, self.answers, self.in_flight, self.peak = [], [], 0, 0

    def __call__(self, item):
        self.log.append(('admit', item))
        return self._run(item)

    async def _run(self, item):
        self.log.append(('start', item))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            for _ in range(self.delays[item]):
                await asyncio.sleep(0)
            if item in self.failing:
                self.log.append(('fail', item))
                raise RuntimeError(f'item {item}')
            self.answers.append(item)  # the checkpoint of an answer received
            return item * 7
        except asyncio.CancelledError:
            self.log.append(('cancelled', item))
            raise
        finally:
            self.in_flight -= 1


def outcome(delays, failing, limit):
    scenario = Scenario(delays, failing)
    try:
        value = asyncio.run(flow(range(len(delays)), scenario, limit))
    except RuntimeError as error:
        value = str(error)
    return value, scenario


@settings(max_examples=150)
@given(st.lists(st.integers(1, 6), max_size=14), st.integers(1, 5), st.data())
def test_flow_equals_serial_with_failures(delays, limit, data):
    """Property (differential): any limit gives the serial results or the serial first error, keeps every answer the
    serial run keeps, never exceeds its limit, admits nothing after a failure and drains instead of cancelling."""
    failing = data.draw(st.sets(st.sampled_from(range(len(delays))), max_size=3)) if delays else set()
    serial, serial_run = outcome(delays, failing, 1)
    value, run = outcome(delays, failing, limit)
    assert value == serial
    assert set(serial_run.answers) <= set(run.answers)
    assert run.peak <= limit
    if delays and not failing:
        assert value == [item * 7 for item in range(len(delays))]
        assert run.peak == min(limit, len(delays))
    assert not any(kind == 'cancelled' for kind, _ in run.log)
    first_fail = next((index for index, (kind, _) in enumerate(run.log) if kind == 'fail'), len(run.log))
    assert not any(kind == 'admit' for kind, _ in run.log[first_fail:])
    admitted = {item for kind, item in run.log if kind == 'admit'}
    started = {item for kind, item in run.log if kind == 'start'}
    assert admitted == started == set(run.answers) | {item for kind, item in run.log if kind == 'fail'}


@settings(max_examples=100)
@given(st.lists(st.integers(1, 6), min_size=1, max_size=12), st.integers(1, 4), st.integers(0, 30))
def test_flow_stop_cancels_and_awaits_everything(delays, limit, stop_after):
    """Property: an external stop cancels running items at once, awaits them, admits nothing more and leaves no task."""
    scenario = Scenario(delays, set())

    async def run():
        task = asyncio.create_task(flow(range(len(delays)), scenario, limit))
        for _ in range(stop_after):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
            stopped = False
        except asyncio.CancelledError:
            stopped = True
        others = [other for other in asyncio.all_tasks() if other is not asyncio.current_task()]
        return stopped, others

    stopped, others = asyncio.run(run())
    assert others == []
    assert scenario.in_flight == 0
    cancelled = {item for kind, item in scenario.log if kind == 'cancelled'}
    started = {item for kind, item in scenario.log if kind == 'start'}
    assert started == set(scenario.answers) | cancelled  # an item admitted but never begun is simply dropped
    if not stopped:  # the stop came after the flow had finished
        assert sorted(scenario.answers) == list(range(len(delays))) and not cancelled
    if cancelled:
        last_cancel = max(index for index, (kind, _) in enumerate(scenario.log) if kind == 'cancelled')
        assert not any(kind == 'admit' for kind, _ in scenario.log[last_cancel:])
