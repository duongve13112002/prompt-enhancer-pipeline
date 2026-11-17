"""Run-scoped resources shared by asynchronous model calls."""

import asyncio
from dataclasses import dataclass
import time


class BudgetExhausted(RuntimeError):
    """The caller's grant has no unreserved requests left."""


@dataclass(eq=False)
class Reservation:
    """An opaque, single-use reservation belonging to one ledger."""

    owner: object
    state: str = 'reserved'


class SingleRunBudgetLedger:
    """In-process ledger; never share a grant across processes or restarts."""

    def __init__(self, limits):
        if any(type(value) is not int or value < 0 for value in limits.values()):
            raise ValueError('budget limits must be non-negative integers')
        self._remaining = dict(limits)
        self._lock = asyncio.Lock()
        self._active = {}

    async def reserve(self, backend):
        async with self._lock:
            if backend not in self._remaining:
                raise ValueError(f'no budget for backend {backend!r}')
            if self._remaining[backend] == 0:
                raise BudgetExhausted(backend)
            self._remaining[backend] -= 1
            token = Reservation(self)
            self._active[token] = backend
            return token

    def _check(self, token):
        if not isinstance(token, Reservation) or token.owner is not self or token not in self._active:
            raise ValueError('invalid or already consumed reservation')

    async def commit(self, token):
        async with self._lock:
            self._check(token)
            del self._active[token]
            token.state = 'committed'

    async def release(self, token):
        async with self._lock:
            self._check(token)
            backend = self._active.pop(token)
            self._remaining[backend] += 1
            token.state = 'released'


# Protocol-bound keys of one capacity record (`report['capacity'][<backend>]`, RES-1 PM answer 6).
CAPACITY_FIELDS = ('max_concurrency', 'requests', 'peak_in_flight', 'busy_s', 'idle_gap_s', 'in_flight_slot_s')
TIMING_FIELDS = ('busy_s', 'idle_gap_s', 'in_flight_slot_s')  # wall-clock fields; differential tests skip them


class CapacityWindow:
    """Capacity use of one backend between `open_window` and `close` (decision 149: logs show requests in flight).

    A request is in flight while it holds a backend slot, back-off waits included. Time is integrated only from the
    first event after opening to the last one before closing, so the idle time before a stage's first request and
    after its last one never counts as an idle gap. `in_flight_slot_s` integrates the in-flight count: the mean in
    flight is `in_flight_slot_s / (busy_s + idle_gap_s)`, and the unused capacity is
    `max_concurrency * (busy_s + idle_gap_s) - in_flight_slot_s`. That product is left to the reader because an
    exact profile integer may exceed the float range (T-008 integer audit).
    """

    def __init__(self, meter):
        self._meter = meter
        self.requests = 0
        self.peak = meter.in_flight
        self.busy = self.idle = self.slots = 0.0
        self._last = meter.clock() if meter.in_flight else None

    def _advance(self, now, in_flight):
        if self._last is not None:
            elapsed = now - self._last
            if in_flight:
                self.busy += elapsed
                self.slots += in_flight * elapsed  # in_flight never exceeds the requests actually admitted
            else:
                self.idle += elapsed
        self._last = now

    def close(self):
        self._meter.windows.discard(self)
        return {'max_concurrency': self._meter.limit, 'requests': self.requests, 'peak_in_flight': self.peak,
                'busy_s': self.busy, 'idle_gap_s': self.idle, 'in_flight_slot_s': self.slots}


class _Meter:
    def __init__(self, limit, clock):
        self.limit, self.clock = limit, clock
        self.in_flight = 0
        self.windows = set()

    def change(self, delta):
        now = self.clock()
        for window in self.windows:
            window._advance(now, self.in_flight)
        self.in_flight += delta
        for window in self.windows:
            if delta > 0:
                window.requests += 1
                window.peak = max(window.peak, self.in_flight)


class _Slot:
    """One backend slot: the semaphore bounds it, the meter counts it once it is held."""

    def __init__(self, semaphore, meter):
        self._semaphore, self._meter = semaphore, meter

    async def __aenter__(self):
        await self._semaphore.acquire()
        self._meter.change(+1)
        return self

    async def __aexit__(self, *exc):
        self._meter.change(-1)
        self._semaphore.release()


class BackendConcurrency:
    """One bounded, metered semaphore per backend for the lifetime of a client group.

    `clock` is a seam for labelled tests; production uses the monotonic clock.
    """

    def __init__(self, limits, *, clock=time.monotonic):
        if any(type(value) is not int or value < 1 for value in limits.values()):
            raise ValueError('concurrency limits must be positive integers')
        self._semaphores = {name: asyncio.Semaphore(value) for name, value in limits.items()}
        self._meters = {name: _Meter(value, clock) for name, value in limits.items()}

    def for_backend(self, name):
        return _Slot(self._semaphores[name], self._meters[name])

    def limit(self, name):
        return self._meters[name].limit

    def names(self):
        return sorted(self._meters)

    def open_window(self, name):
        """Start recording one backend's capacity use; `close()` returns the capacity record."""
        meter = self._meters[name]
        window = CapacityWindow(meter)
        meter.windows.add(window)
        return window


class CapacityLog:
    """The capacity records of one stage: `report['capacity'][<backend>]` for every backend it sent a request to.

    `watch` may be called with a lazily created run's `BackendConcurrency` at any time; windows open on the first call,
    so requests sent before it are not counted.
    """

    def __init__(self):
        self._windows = {}

    def watch(self, concurrency):
        for name in concurrency.names():
            if name not in self._windows:
                self._windows[name] = concurrency.open_window(name)

    def close(self):
        records = {name: window.close() for name, window in sorted(self._windows.items())}
        self._windows = {}
        return {name: record for name, record in records.items() if record['requests']}


class OrderedTurns:
    """Turns taken in index order: `wait(i)` returns once every index below `i` has been released.

    A stage keeps an order-sensitive step (a seeded draw, a first-writer-wins update) in serial order while its model
    calls run concurrently. Every index must be released exactly once, also on failure; `release` is synchronous, so it
    is safe in a `finally` during cancellation.
    """

    def __init__(self, count):
        self._released = [False] * count
        self._next = 0
        self._waiters = {}

    async def wait(self, index):
        if self._next < index:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[index] = waiter
            try:
                await waiter
            finally:
                self._waiters.pop(index, None)

    def release(self, index):
        self._released[index] = True
        while self._next < len(self._released) and self._released[self._next]:
            self._next += 1
        waiter = self._waiters.get(self._next)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


async def await_owned(awaitable):
    """Finish owned cleanup despite repeated caller cancellation, then propagate that cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def flow(items, worker, limit):
    """Run `worker(item)` for every item with at most `limit` in flight, results in item order (decision 149).

    Per-item flow: the next item starts as soon as any running one finishes, so one slow item never holds a wave.
    `worker(item)` is called when the item is admitted and must return an awaitable. On a worker error nothing new is
    admitted, the items already running are drained so every answer they received is kept, and then the error of the
    earliest failed item (in item order, so the choice never depends on timing) is raised. On an external stop (the
    flow itself cancelled) every running item is cancelled at once and awaited before the cancellation propagates.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError('flow limit must be a positive integer')
    items = list(items)
    results = [None] * len(items)
    errors = {}
    running = {}
    admitted = 0
    try:
        while running or (admitted < len(items) and not errors):
            while not errors and admitted < len(items) and len(running) < limit:
                try:
                    running[asyncio.ensure_future(worker(items[admitted]))] = admitted
                except Exception as error:  # a worker that fails before returning its awaitable fails its item
                    errors[admitted] = error
                admitted += 1
            if not running:
                continue
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in sorted(done, key=running.get):
                index = running.pop(task)
                if task.cancelled():
                    errors[index] = asyncio.CancelledError('flow item was cancelled')
                elif task.exception() is not None:
                    errors[index] = task.exception()
                else:
                    results[index] = task.result()
    except BaseException:
        for task in running:
            task.cancel()
        await await_owned(asyncio.gather(*running, return_exceptions=True))
        raise
    if errors:
        raise errors[min(errors)]
    return results
