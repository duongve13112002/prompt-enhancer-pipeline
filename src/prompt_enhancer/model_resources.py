"""Run-scoped resources shared by asynchronous model calls."""

import asyncio
from dataclasses import dataclass


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


class BackendConcurrency:
    """One bounded semaphore per backend for the lifetime of a client group."""

    def __init__(self, limits):
        if any(type(value) is not int or value < 1 for value in limits.values()):
            raise ValueError('concurrency limits must be positive integers')
        self._semaphores = {name: asyncio.Semaphore(value) for name, value in limits.items()}

    def for_backend(self, name):
        return self._semaphores[name]
