"""Bounded caller-owned delivery lifecycle; labelled async/sync sinks."""
import asyncio
from copy import deepcopy
from threading import Event

import pytest

from prompt_enhancer.response_lifecycle import ResponseLifecycle, LifecycleFull


def test_close_joins_capacity_skipped_delivery():
    async def run():
        rows = []
        config = settings()
        config['trace']['queue_limit'] = 2
        life = ResponseLifecycle(config, rows.append)
        first_release, skipped, skipped_release = (asyncio.Event() for _ in range(3))

        async def monitor():
            await first_release.wait()
            return {'status': 'checked'}

        async def cleanup():
            skipped.set()
            await skipped_release.wait()

        first = life.submit(life.reserve(), {'body': {}}, monitor=monitor)
        second = life.submit(life.reserve(), {'body': {}}, monitor=monitor, cleanup=cleanup)
        await first.after_delivery()
        delivery = asyncio.create_task(second.after_delivery())
        await skipped.wait()
        closing = asyncio.create_task(life.close())
        try:
            first_release.set()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(closing), .05)
        finally:
            skipped_release.set()
            outcomes = await asyncio.gather(delivery, closing, return_exceptions=True)
        assert outcomes == [None, None]
        assert len(rows) == 2 and life._used == 0
    asyncio.run(run())


def test_failed_trace_copy_keeps_reservation_cancelable():
    class BrokenRow:
        def __deepcopy__(self, memo):
            raise ValueError('labelled copy error')

    async def run():
        life = ResponseLifecycle(settings(), lambda row: None)
        try:
            token = life.reserve()
            with pytest.raises(ValueError, match='copy error'):
                life.submit(token, BrokenRow())
            life.cancel(token)
            assert life._used == 0
        finally:
            await life.close()
    asyncio.run(run())


def test_monitor_cancelled_before_start_still_finalizes_delivery():
    async def run():
        rows, closed = [], []
        life = ResponseLifecycle(settings(), rows.append)

        async def monitor():
            pytest.fail('cancelled monitoring started')

        async def cleanup():
            closed.append(True)

        ticket = life.submit(life.reserve(), {'body': {}}, monitor=monitor, cleanup=cleanup)
        await ticket.after_delivery()
        next(iter(life._monitors)).cancel()
        await life.close()
        assert closed == [True] and life._used == 0
        assert rows[0]['body']['monitoring'] == {'status': 'failed'}
    asyncio.run(run())


def settings():
    return {'request_deadline_s': 4, 'trace': {'retention_s': 20, 'queue_limit': 1, 'workers': 1,
            'drain_timeout_s': 1}, 'monitoring': {'queue_limit': 1, 'workers': 1, 'drain_timeout_s': 1}}


def test_trace_backpressure_and_nonblocking_delivery():
    async def run():
        entered, release = Event(), Event()
        def sink(row):
            entered.set()
            assert release.wait(3)
        life = ResponseLifecycle(settings(), sink)
        token = life.reserve()
        with pytest.raises(LifecycleFull):
            life.reserve()
        life.submit(token, {'value': 'trace'})
        # Submission never waits for synchronous disk work.
        assert await asyncio.to_thread(entered.wait, 2)
        release.set()
        await life.close()
        assert life.closed and not life.errors
    asyncio.run(run())


def test_monitor_only_after_delivery_and_never_mutates_response():
    async def run():
        rows, calls = [], []
        life = ResponseLifecycle(settings(), rows.append)
        async def monitor():
            calls.append('verifier')
            return {'verdict': 'fail'}
        token = life.reserve()
        response = {'status': 'success', 'output': 'kept'}
        ticket = life.submit(token, {'body': {}, **deepcopy(response)}, monitor=monitor)
        await asyncio.sleep(0)
        assert calls == []
        await ticket.after_delivery()
        await life.close()
        assert calls == ['verifier'] and response == {'status': 'success', 'output': 'kept'}
        assert rows[0]['body']['monitoring'] == {'verdict': 'fail'}
    asyncio.run(run())


def test_undelivered_ticket_is_finalized_and_monitor_timeout_is_owned():
    async def run():
        rows = []
        config = settings()
        config['monitoring']['drain_timeout_s'] = .01
        life = ResponseLifecycle(config, rows.append)
        stopped = asyncio.Event()
        async def monitor():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        ticket = life.submit(life.reserve(), {'body': {}}, monitor=monitor)
        await ticket.after_delivery()
        await life.close()
        assert stopped.is_set() and life.closed
        assert rows[0]['body']['monitoring']['status'] == 'failed'
    asyncio.run(run())


@pytest.mark.parametrize('workers', [1, 2])
def test_trace_worker_override_controls_parallel_io(workers):
    async def run():
        config = settings()
        config['trace'].update(workers=workers, queue_limit=2)
        entered, release = [Event(), Event()], Event()
        def sink(index):
            entered[index].set()
            assert release.wait(3)
        life = ResponseLifecycle(config, sink)
        life.submit(life.reserve(), 0)
        life.submit(life.reserve(), 1)
        try:
            assert await asyncio.to_thread(entered[0].wait, 2)
            second = await asyncio.to_thread(entered[1].wait, .05)
            assert second == (workers == 2)
        finally:
            release.set()
            await life.close()
    asyncio.run(run())


@pytest.mark.parametrize('workers', [1, 2])
def test_monitor_worker_override_controls_parallel_calls(workers):
    async def run():
        config = settings()
        config['trace']['queue_limit'] = 3
        config['monitoring'].update(workers=workers, queue_limit=2)
        life, entered, release = ResponseLifecycle(config, lambda row: None), [], asyncio.Event()
        async def monitor():
            entered.append(True)
            await release.wait()
            return {'status': 'checked'}
        for _ in range(3):
            ticket = life.submit(life.reserve(), {'body': {}}, monitor=monitor)
            await ticket.after_delivery()
        await asyncio.sleep(.01)
        assert len(entered) == workers and len(life._monitors) == 2
        release.set()
        await life.close()
        assert not life.errors
    asyncio.run(run())


def test_trace_failure_is_reported_and_undelivered_owner_closed():
    async def run():
        closed = []
        def broken(row):
            raise OSError('labelled disk failure')
        async def cleanup():
            closed.append(True)
        life = ResponseLifecycle(settings(), broken)
        life.submit(life.reserve(), {'body': {}}, monitor=lambda: None, cleanup=cleanup)
        await life.close()
        assert closed == [True] and life.errors == ['TRACE_WRITE_FAILED']
    asyncio.run(run())


def test_trace_drain_timeout_reports_and_joins_owned_io():
    async def run():
        entered, release = Event(), Event()
        config = settings()
        config['trace']['drain_timeout_s'] = .01
        def sink(row):
            entered.set()
            assert release.wait(3)
        life = ResponseLifecycle(config, sink)
        life.submit(life.reserve(), {'body': {}})
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(life.close())
        try:
            async def reported():
                while not life.errors:
                    await asyncio.sleep(0)
            await asyncio.wait_for(reported(), 2)
            assert not closing.done() and life.errors == ['TRACE_DRAIN_TIMEOUT']
        finally:
            release.set()
            await closing
        assert not life._writes and life._used == 0
    asyncio.run(run())


def test_skipped_monitor_closes_its_owned_client():
    async def run():
        config = settings()
        config['trace']['queue_limit'] = 2
        life, closed, release = ResponseLifecycle(config, lambda row: None), [], asyncio.Event()
        async def monitor():
            await release.wait()
        async def cleanup():
            closed.append(True)
        first = life.submit(life.reserve(), {'body': {}}, monitor=monitor, cleanup=cleanup)
        second = life.submit(life.reserve(), {'body': {}}, monitor=monitor, cleanup=cleanup)
        await first.after_delivery()
        await second.after_delivery()
        release.set()
        await life.close()
        assert len(closed) == 2
    asyncio.run(run())
