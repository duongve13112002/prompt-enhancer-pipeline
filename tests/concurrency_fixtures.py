"""Test-only synchronization for capacity evidence; no runtime scheduling policy."""
import asyncio


class ArrivalGate:
    """Hold the first target arrivals together, then stay open for partial tails and later calls."""

    def __init__(self, target, timeout_s=5):
        self.target, self.timeout_s = target, timeout_s
        self.arrived = 0
        self.ready = asyncio.Event()

    async def arrive(self):
        self.arrived += 1
        if self.arrived >= self.target:
            self.ready.set()
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=self.timeout_s)
        except TimeoutError as error:
            raise AssertionError(f'only {self.arrived}/{self.target} capacity arrivals before timeout') from error
