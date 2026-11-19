"""Fix round of T-016 P3 (R-015, R-017, R-024, R-025, R-027): what a long-lived server owes the lifecycle and the stop.

F1 the stop counts requests, not connections; F2 every response the pipeline produced ends in an acknowledgement or an
abandonment, so a server that runs for days never loses trace capacity to undelivered responses; F3 the expiry scan does
not run per request; F6 a request that arrives while a place is being handed to a waiter is queued, not turned away.

Labelled doubles: `StubPipeline` replaces S0-S6; `MonitoredPipeline` replaces it with a response whose ticket is monitored
and owned by the REAL `ResponseLifecycle`, because the shared test profile has a known conflict and no calibrated verifier,
so the real pipeline never selects monitoring here (checked by probe). Evidence class: `mock_contract`."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import shutil

import pytest

from prompt_enhancer import cli_serve
from prompt_enhancer.attachments import MAX_INDEX_BYTES, publish_attachments, read_attachment
from prompt_enhancer.pipeline import PipelineResponse
from prompt_enhancer.response_lifecycle import ResponseLifecycle
from prompt_enhancer.storage import StorageError
from serve_fixtures import (GOOD_REQUEST, Live, StubPipeline, StubResponse, aexchange, aopen, good_post, raw_post,
                            serving_settings, stub_envelope, until)
from test_cli_inputs import drop_link, make_link
from test_enhance_serve_lifecycle import Holder, env, run

DATA = b'attachment bytes of one response'
IMAGE = 'sha256:' + hashlib.sha256(DATA).hexdigest()


class MonitoredPipeline(StubPipeline):
    """Labelled double of `Pipeline`: the response of every request holds a monitored ticket of a real lifecycle."""

    def __init__(self, queue_limit=2, attachments=None):
        super().__init__(self._produce)
        self.rows, self.cleaned, self.ran, self.produced = [], 0, 0, 0
        self.attachments = attachments or {}
        self.lifecycle = ResponseLifecycle(
            {'request_deadline_s': 4, 'trace': {'retention_s': 20, 'queue_limit': queue_limit, 'workers': 1, 'drain_timeout_s': 5},
             'monitoring': {'queue_limit': 2, 'workers': 1, 'drain_timeout_s': 5}}, self.rows.append)

    async def _produce(self, request):
        token = self.lifecycle.reserve()  # LifecycleFull is the real "trace capacity" refusal

        async def monitor():
            self.ran += 1
            return {'status': 'checked'}

        async def cleanup():
            self.cleaned += 1
        ticket = self.lifecycle.submit(token, {'body': {}}, monitor=monitor, cleanup=cleanup)
        self.produced += 1
        return PipelineResponse(stub_envelope(trace_id=f'r-{self.produced}'), dict(self.attachments), ticket)

    async def close(self):
        await self.lifecycle.close()
        await super().close()


async def refused_write(writer, data):
    """Labelled double: the transport reports a write that failed (a slow or reset reader)."""
    return False


# --- F2: abandon -----------------------------------------------------------------------------------------------------

def test_undelivered_monitored_responses_never_exhaust_trace_capacity(model_test_profile, tmp_path, project_root):
    """The review's probe as a real test: more failed writes than `runtime.trace.queue_limit`, then the next request is
    still admitted. Before the fix every failed write kept its trace slot and its model-client owner until the stop."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        pipeline = MonitoredPipeline(queue_limit=2)
        async with Live(serving_settings(config), pipeline, store, project_root) as live:
            real_write, live.server._write = live.server._write, refused_write
            for _ in range(5):  # trace.queue_limit is 2
                status, _, body = await aexchange(live.port, good_post())
                assert status is None and body == b''  # nothing was delivered
                await until(lambda: live.server.open == 0 and pipeline.lifecycle._used == 0)
            assert pipeline.produced == 5 and pipeline.cleaned == 5 and pipeline.ran == 0  # owners closed, no monitor
            live.server._write = real_write
            status, _, body = await aexchange(live.port, good_post())
            assert status == 200 and json.loads(body)['trace_id'] == 'r-6'  # still admitted: no 503 for trace capacity
            await until(lambda: pipeline.ran == 1)
        assert live.report.clean and not pipeline.lifecycle.errors
        monitoring = [row['body']['monitoring'] for row in pipeline.rows]
        assert monitoring.count({'status': 'skipped', 'reason': 'delivery_unacknowledged'}) == 5
        assert monitoring.count({'status': 'checked'}) == 1 and pipeline.cleaned == 6

    run(scenario)


def test_a_failed_publication_abandons_the_response_and_frees_its_trace_slot(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)
    (tmp_path / 'data').mkdir(exist_ok=True)
    blocker = tmp_path / 'data' / 'attachments'
    blocker.write_bytes(b'a file where the attachment directory must be')

    async def scenario():
        pipeline = MonitoredPipeline(queue_limit=2, attachments={IMAGE: DATA})
        async with Live(serving_settings(config), pipeline, store, project_root) as live:
            for _ in range(3):  # more than trace.queue_limit
                status, _, body = await aexchange(live.port, good_post())
                assert status == 500 and json.loads(body)['error']['code'] == 'delivery_failed'
                await until(lambda: live.server.open == 0 and pipeline.lifecycle._used == 0)
            assert pipeline.produced == 3 and pipeline.cleaned == 3 and pipeline.ran == 0
            blocker.unlink()
            assert (await aexchange(live.port, good_post()))[0] == 200  # capacity was never lost
        assert live.report.clean

    run(scenario)


class HeldPublication:
    """Labelled double of `ResponseLifecycle.io`: the attachment publication waits for `gate`, everything else runs."""

    def __init__(self):
        self.gate, self.started = asyncio.Event(), 0

    async def io(self, function, *args):
        if function is publish_attachments:
            self.started += 1
            await self.gate.wait()
        return await asyncio.to_thread(function, *args)


@pytest.mark.parametrize('path', ['client_leaves', 'service_deadline', 'stop_cancels'])
def test_a_response_cut_off_after_generation_is_abandoned_exactly_once(model_test_profile, tmp_path, project_root, path):
    """The pipeline has produced a response and the request ends before its write: the client left, the service deadline
    passed, or the stop cancelled the handler. Each is abandoned once and never acknowledged."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        response = StubResponse(stub_envelope(), {IMAGE: DATA})

        async def behaviour(request):
            return response
        pipeline, held = StubPipeline(behaviour), HeldPublication()
        pipeline.lifecycle.io = held.io
        settings = serving_settings(config, total_deadline_s=0.4 if path == 'service_deadline' else 30,
                                    shutdown_timeout_s=0.3)
        live = await Live(settings, pipeline, store, project_root).__aenter__()
        try:
            if path == 'client_leaves':
                reader, writer = await aopen(live.port)
                writer.write(good_post())
                await until(lambda: held.started == 1)
                writer.close()
                await until(lambda: live.server.open == 0)
            elif path == 'service_deadline':
                status, _, body = await aexchange(live.port, good_post())
                assert status == 504 and json.loads(body)['error']['code'] == 'deadline_exceeded'
            else:
                running = asyncio.create_task(aexchange(live.port, good_post()))
                await until(lambda: held.started == 1)
                report = await live.stop()
                assert report.cancelled == 1 and not report.clean  # a received request that had to be cut off
                assert (await running)[0] is None
            assert response.abandoned == 1 and response.acknowledged == 0
        finally:
            held.gate.set()
            await live.stop()

    run(scenario)


# --- F1: the stop counts requests, not connections ---------------------------------------------------------------------

@pytest.mark.parametrize('sent', [b'', b'POST /v1/enhance HTTP/1.1\r\nHost: x\r\n'])
def test_a_connection_without_a_request_at_the_stop_is_closed_and_not_counted(model_test_profile, tmp_path, project_root, sent):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        pipeline = StubPipeline(Holder())
        settings = serving_settings(config, read_timeout_s=30, shutdown_timeout_s=5)
        live = await Live(settings, pipeline, store, project_root).__aenter__()
        reader, writer = await aopen(live.port)
        if sent:
            writer.write(sent)  # a head that has not arrived completely is not a request either
        await until(lambda: live.server._idle)
        loop = asyncio.get_running_loop()
        started = loop.time()
        report = await live.stop()
        assert loop.time() - started < 2.5  # closed at once, not after the 5 s the stop would wait for a running request
        assert report.clean and report.cancelled == 0 and not live.server._tasks and not pipeline.requests
        try:
            assert await asyncio.wait_for(reader.read(65536), 10) == b''  # closed, nothing answered
        except ConnectionResetError:
            pass
        writer.close()

    run(scenario)


def test_a_turned_away_connection_that_is_still_lingering_at_the_stop_is_not_a_cancelled_request(
        model_test_profile, tmp_path, project_root):
    """A 503 answer waits up to `LINGER_S` for the client's own close; a stop bound shorter than that cuts it off, which
    is not an overrun of any request."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        settings = serving_settings(config, max_connections=1, read_timeout_s=30, shutdown_timeout_s=0.1)
        live = await Live(settings, StubPipeline(Holder()), store, project_root).__aenter__()
        first = await aopen(live.port)  # takes the only connection place and sends nothing
        await until(lambda: live.server.open == 1)
        reader, writer = await aopen(live.port)  # turned away with 503, then lingers because this client keeps its side open
        assert (await asyncio.wait_for(reader.readuntil(b'\r\n'), 10)).startswith(b'HTTP/1.1 503')
        await until(lambda: live.server.turned_away == 1)
        report = await live.stop()
        assert report.cancelled == 0 and report.clean
        writer.close()
        first[1].close()

    run(scenario)


def test_a_request_whose_head_was_read_still_counts_when_the_stop_cuts_it_off(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        settings = serving_settings(config, read_timeout_s=30, shutdown_timeout_s=0.3)
        live = await Live(settings, StubPipeline(Holder()), store, project_root).__aenter__()
        reader, writer = await aopen(live.port)
        writer.write(raw_post('/v1/enhance', json.dumps(GOOD_REQUEST).encode())[:-5])  # head complete, body five bytes short
        await until(lambda: live.server._requests)
        report = await live.stop()
        assert report.cancelled == 1 and not report.clean  # it was received, so cutting it off is an overrun
        writer.close()

    run(scenario)


# --- F6: the place that is being handed over is not a full queue --------------------------------------------------------

def test_a_request_arriving_while_a_place_is_handed_over_is_queued_not_turned_away(model_test_profile, tmp_path, project_root):
    """One place, one queue slot. The running request ends, its place goes to the queued one, and in the same loop turn a
    third request arrives: the queue slot is free again, so the third waits instead of getting a 503."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        settings = serving_settings(config, max_concurrency=1, queue_limit=1)
        async with Live(settings, StubPipeline(holder), store, project_root) as live:
            server = live.server
            first = asyncio.create_task(server._enhance(GOOD_REQUEST))
            await until(lambda: holder.inflight == 1)
            second = asyncio.create_task(server._enhance(GOOD_REQUEST))
            await until(lambda: server.waiting == 1)
            holder.release()  # schedules the wake-up of the first request ...
            third = asyncio.create_task(server._enhance(GOOD_REQUEST))  # ... and this one's first step comes right after it
            outcomes = await asyncio.gather(first, second, third, return_exceptions=True)
            assert all(isinstance(item, tuple) for item in outcomes), outcomes  # none was refused
            assert server._admission.running == 0 and server.waiting == 0

    run(scenario)


def test_a_waiter_that_is_cancelled_after_the_hand_over_passes_its_place_on(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        settings = serving_settings(config, max_concurrency=1, queue_limit=2)
        async with Live(settings, StubPipeline(holder), store, project_root) as live:
            server = live.server
            first = asyncio.create_task(server._enhance(GOOD_REQUEST))
            await until(lambda: holder.inflight == 1)
            second = asyncio.create_task(server._enhance(GOOD_REQUEST))
            third = asyncio.create_task(server._enhance(GOOD_REQUEST))
            await until(lambda: server.waiting == 2)
            holder.release()  # the first request ends in the next loop turn and hands its place to `second` ...
            asyncio.get_running_loop().call_soon(second.cancel)  # ... and `second` is cancelled before it resumes
            outcomes = await asyncio.gather(first, second, third, return_exceptions=True)
            assert isinstance(outcomes[0], tuple) and isinstance(outcomes[1], asyncio.CancelledError)
            assert isinstance(outcomes[2], tuple)  # the place went on to `third`, it was not lost
            assert server._admission.running == 0 and server.waiting == 0

    run(scenario)


def test_a_cancelled_waiter_that_leave_already_popped_still_raises_cancelled():
    """Retest R2: the waiter's future is cancelled, then another request's `leave` pops it before the waiter resumes;
    the waiter must see its CancelledError, not a ValueError from a second removal, and no place may leak."""
    async def scenario():
        admission = cli_serve._Admission(1, 1)
        assert await admission.enter()
        waiter = asyncio.create_task(admission.enter())
        await until(lambda: admission.waiting == 1)
        waiter.cancel()
        admission.leave()
        outcome = (await asyncio.gather(waiter, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        assert admission.running == 0 and admission.waiting == 0

    asyncio.run(scenario())


def test_a_cancel_landing_in_the_turn_generation_finished_abandons_the_response():
    """Retest R1: the handler is cancelled (deadline or stop) after `_enhance` finished but before the handler resumed;
    the dropped `(bytes, response)` result is abandoned at once instead of holding its trace slot until the stop."""
    abandoned = []

    async def scenario():
        server = object.__new__(cli_serve.Server)  # labelled double: only `_until_gone` and `_abandon` run

        async def record(response):
            abandoned.append(response)
        server._abandon = record
        reader = asyncio.StreamReader()  # a client that neither sends nor closes
        marker = object()
        handler = None

        async def work():
            handler.cancel()  # the cancel lands in the same loop turn in which the work finishes
            return b'bytes', marker
        handler = asyncio.ensure_future(server._until_gone(reader, work()))
        outcome = (await asyncio.gather(handler, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        assert abandoned == [marker]

        async def download():  # a download result holds nothing, so nothing is abandoned
            handler2.cancel()
            return b'bytes'
        handler2 = asyncio.ensure_future(server._until_gone(asyncio.StreamReader(), download()))
        await asyncio.gather(handler2, return_exceptions=True)
        assert abandoned == [marker]

    asyncio.run(scenario())


# --- F5: a download opens once, without following a link ---------------------------------------------------------------

def published(store, data=DATA):
    publish_attachments(store, 'r-x', {IMAGE: data}, datetime.now(timezone.utc) + timedelta(seconds=600))
    return store.root / 'attachments' / 'r-x'


def test_read_attachment_returns_the_published_bytes_and_refuses_what_differs_from_its_index(
        model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)
    directory = published(store)
    assert read_attachment(store, 'r-x', IMAGE) == DATA
    copy = directory / IMAGE[7:]
    copy.write_bytes(DATA + b'x' * (3 << 20))  # grown after publication: refused by the size bound of the open handle
    with pytest.raises(StorageError, match='differs from its index'):
        read_attachment(store, 'r-x', IMAGE)
    copy.write_bytes(DATA[:-1])  # shorter than the index says
    with pytest.raises(StorageError, match='differs from its index'):
        read_attachment(store, 'r-x', IMAGE)
    copy.write_bytes(DATA)
    index = directory / 'index.json'
    record = json.loads(index.read_text())
    for bad in (-1, True, None, '33', 1.5):  # a size that is not a non-negative integer is a damaged index
        record['images'][0]['size_bytes'] = bad
        index.write_text(json.dumps(record))
        with pytest.raises(StorageError, match='index malformed'):
            read_attachment(store, 'r-x', IMAGE)
    index.write_bytes(b' ' * (MAX_INDEX_BYTES + 1))  # an index larger than its safety bound is not read into memory
    with pytest.raises(StorageError, match='index unreadable'):
        read_attachment(store, 'r-x', IMAGE)


def test_a_link_in_the_attachment_tree_is_refused_not_followed(model_test_profile, tmp_path, project_root):
    """The set directory is replaced by a link to a directory that holds a perfectly valid copy of the set: following it
    would serve the right bytes, so only a no-follow open refuses it."""
    config, store = env(model_test_profile, tmp_path, project_root)
    directory = published(store)
    outside = tmp_path / 'outside'
    outside.mkdir()
    shutil.move(str(directory), str(outside / 'r-x'))
    make_link(directory, outside / 'r-x')
    try:
        assert (outside / 'r-x' / IMAGE[7:]).read_bytes() == DATA  # the target is valid
        with pytest.raises(StorageError):
            read_attachment(store, 'r-x', IMAGE)
    finally:
        drop_link(directory)


# --- F3: the expiry scan is not per request ----------------------------------------------------------------------------

def test_the_live_attachment_sets_are_scanned_again_only_after_the_earliest_expiry(model_test_profile, tmp_path, project_root,
                                                                                    monkeypatch):
    config, store = env(model_test_profile, tmp_path, project_root)
    scans = []
    real_sweep = cli_serve.sweep_attachments

    def spy(store_, now=None):  # labelled spy: counts the scans, then runs the production one
        scans.append(now)
        return real_sweep(store_, now)
    monkeypatch.setattr(cli_serve, 'sweep_attachments', spy)
    clock = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    base = tmp_path / 'data' / 'attachments'

    async def scenario():
        counter = [0]

        async def behaviour(request):
            counter[0] += 1
            return StubResponse(stub_envelope(trace_id=f'r-{counter[0]}'), {IMAGE: DATA})
        live = Live(serving_settings(config), StubPipeline(behaviour), store, project_root, retention_s=60)
        live.server._now = lambda: clock[0]
        async with live:
            assert len(scans) == 1  # the start
            for _ in range(5):
                assert (await aexchange(live.port, good_post()))[0] == 200
            assert len(scans) == 1 and len(list(base.iterdir())) == 5  # five requests, no scan, five live sets
            clock[0] += timedelta(seconds=59)
            assert (await aexchange(live.port, good_post()))[0] == 200
            assert len(scans) == 1  # the earliest expiry (60 s) has not passed
            clock[0] += timedelta(seconds=2)  # the first five have now expired, the sixth has not
            assert (await aexchange(live.port, good_post()))[0] == 200
            assert len(scans) == 2 and sorted(item.name for item in base.iterdir()) == ['r-6', 'r-7']
            for _ in range(3):
                assert (await aexchange(live.port, good_post()))[0] == 200
            assert len(scans) == 2  # the next earliest expiry is known again
            clock[0] += timedelta(seconds=300)
            assert (await aexchange(live.port, good_post()))[0] == 200
            assert len(scans) == 3 and [item.name for item in base.iterdir()] == ['r-11']
        assert live.report.clean

    run(scenario)
