"""Caller-owned bounded trace I/O and delivery-acknowledged monitoring."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy


class LifecycleFull(RuntimeError):
    pass


class DeliveryTicket:
    def __init__(self, lifecycle, row, monitor, cleanup):
        self.lifecycle, self.row, self.monitor, self.cleanup = lifecycle, row, monitor, cleanup
        self.delivered = self.finalized = False

    async def after_delivery(self):
        if self.delivered or self.finalized or self.lifecycle.closed:
            return
        self.delivered = True
        await self.lifecycle._deliver(self)


class ResponseLifecycle:
    def __init__(self, runtime, sink):
        self.settings, self.sink = deepcopy(runtime), sink
        self.errors, self.closed = [], False
        self._pool = ThreadPoolExecutor(max_workers=runtime['trace']['workers'], thread_name_prefix='pe-trace')
        self._reserved, self._tickets, self._writes, self._monitors = set(), set(), set(), set()
        self._deliveries = set()
        self._used = 0
        self._closing = None
        self._monitor_lock = None if 'monitoring' not in runtime else asyncio.Semaphore(runtime['monitoring']['workers'])

    def reserve(self):
        if self.closed or self._used >= self.settings['trace']['queue_limit']:
            raise LifecycleFull('trace capacity unavailable before generation')
        token = object()
        self._reserved.add(token)
        self._used += 1
        return token

    async def io(self, function, *args):
        """One owned maintenance operation, using the same configured disk-worker bound."""
        future = asyncio.wrap_future(self._pool.submit(function, *args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await future
            raise

    def cancel(self, token):
        if token in self._reserved:
            self._reserved.remove(token)
            self._used -= 1

    def submit(self, token, row, *, monitor=None, cleanup=None):
        if self.closed or token not in self._reserved:
            raise ValueError('unowned trace reservation')
        ticket = DeliveryTicket(self, deepcopy(row), monitor, cleanup)
        if monitor is None:
            self._write(ticket)
        else:
            self._tickets.add(ticket)
        self._reserved.remove(token)
        return ticket

    def _write(self, ticket):
        future = asyncio.wrap_future(self._pool.submit(self.sink, ticket.row))
        ticket.finalized = True
        self._tickets.discard(ticket)
        self._writes.add(future)
        def finished(done):
            self._writes.discard(done)
            self._used -= 1
            if not done.cancelled() and done.exception() is not None:
                self.errors.append('TRACE_WRITE_FAILED')
        future.add_done_callback(finished)

    async def _deliver(self, ticket):
        if ticket.finalized:
            return
        settings = self.settings.get('monitoring')
        work = None
        if settings is not None and len(self._monitors) < settings['queue_limit']:
            work = asyncio.create_task(self._monitor(ticket, settings))
            self._monitors.add(work)
        delivery = asyncio.create_task(self._finish_delivery(ticket, work, 'capacity'))
        self._deliveries.add(delivery)
        delivery.add_done_callback(self._deliveries.discard)
        if work is None:
            try:
                await asyncio.shield(delivery)
            except asyncio.CancelledError:
                await asyncio.shield(delivery)
                raise

    async def _monitor(self, ticket, settings):
        async with self._monitor_lock:
            async with asyncio.timeout(settings['drain_timeout_s']):
                return await ticket.monitor()

    async def _finish_delivery(self, ticket, work, skip_reason):
        if work is None:
            ticket.row['body']['monitoring'] = {'status': 'skipped', 'reason': skip_reason}
        else:
            try:
                ticket.row['body']['monitoring'] = await work
            except (Exception, asyncio.CancelledError):
                ticket.row['body']['monitoring'] = {'status': 'failed'}
            finally:
                self._monitors.discard(work)
        try:
            await self._cleanup(ticket)
        finally:
            try:
                self._write(ticket)
            except Exception:
                ticket.finalized = True
                self._tickets.discard(ticket)
                self._used -= 1
                self.errors.append('TRACE_WRITE_FAILED')

    async def _cleanup(self, ticket):
        if ticket.cleanup is not None:
            try:
                await ticket.cleanup()
            except (Exception, asyncio.CancelledError):
                self.errors.append('RESOURCE_CLOSE_FAILED')

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._drain())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            await self._closing
            raise

    async def _drain(self):
        self.closed = True
        for token in list(self._reserved):
            self.cancel(token)
        try:
            for ticket in list(self._tickets):
                if not ticket.delivered:
                    ticket.delivered = True
                    await self._finish_delivery(ticket, None, 'delivery_unacknowledged')
            if self._monitors:
                _, pending = await asyncio.wait(self._monitors, timeout=self.settings['monitoring']['drain_timeout_s'])
                for task in pending:
                    task.cancel()
            if self._deliveries:
                await asyncio.gather(*self._deliveries)
            if self._writes:
                _, pending = await asyncio.wait(self._writes, timeout=self.settings['trace']['drain_timeout_s'])
                if pending:
                    self.errors.append('TRACE_DRAIN_TIMEOUT')
                # OS disk writes cannot be safely killed; join owned operations rather than orphaning threads.
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._pool.shutdown(wait=True, cancel_futures=False)
