"""Admission, deadlines, delivery acknowledgement and the stop of `enhance serve`, in process (T-016 P3, R-015, R-017, R-024).

The HTTP server (`cli_serve.Server`) is production code over real sockets; the pipeline is a labelled stub
(`StubPipeline`: a held coroutine instead of S0-S6) so that every boundary is exact: a test waits on an observable state
(`until`) and releases a held request explicitly, never inferring capacity from a sleep. The same boundaries through the
real entry point and the real pipeline are in `test_enhance_serve.py`. Evidence class: `mock_contract`."""
import asyncio
import hashlib
import json
import socket
from types import SimpleNamespace
import threading

import pytest

from cli_fixtures import pipeline_config
from prompt_enhancer.cli_serve import ServeFailure, Server
from serve_fixtures import (GOOD_REQUEST, Live, StubPipeline, StubResponse, aexchange, aopen, free_port, good_post,
                            raw_post, serve_config, serving_settings, store_for, stub_envelope, until)


def env(model_test_profile, tmp_path, project_root, **dotted):
    config = serve_config(model_test_profile, tmp_path, project_root, 1, free_port(), **dotted)
    return config, store_for(config, project_root)


class Holder:
    """A stub behaviour that holds every request until `release()`, counting requests inside it."""
    def __init__(self):
        self.gate, self.inflight, self.peak = asyncio.Event(), 0, 0

    async def __call__(self, request):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await self.gate.wait()
        finally:
            self.inflight -= 1
        return StubResponse(stub_envelope())

    def release(self):
        self.gate.set()


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 120))


@pytest.mark.parametrize('active, queue, connections', [(1, 0, 1), (2, 0, 2), (1, 1, 2), (2, 1, 5), (3, 2, 5)])
def test_admission_boundary_is_exact(model_test_profile, tmp_path, project_root, active, queue, connections):
    """`active` run, `queue` wait, the next request is refused 503 before it reaches the pipeline, and the places are
    free again once the held requests finish: the peak inside the pipeline equals `serving.max_concurrency`."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        pipeline = StubPipeline(holder)
        settings = serving_settings(config, max_concurrency=active, queue_limit=queue, max_connections=connections)
        async with Live(settings, pipeline, store, project_root) as live:
            admitted = [asyncio.create_task(aexchange(live.port, good_post())) for _ in range(active)]
            await until(lambda: holder.inflight == active)
            admitted += [asyncio.create_task(aexchange(live.port, good_post())) for _ in range(queue)]
            await until(lambda: live.server.waiting == queue)
            assert live.server.open == active + queue
            status, _, body = await aexchange(live.port, good_post())
            assert status == 503 and json.loads(body)['error']['code'] == 'overloaded'
            assert len(pipeline.requests) == active and holder.peak == active  # the refused one never ran
            holder.release()
            assert [(await task)[0] for task in admitted] == [200] * (active + queue)
            await until(lambda: live.server.open == 0 and live.server.waiting == 0)
            assert holder.peak == active and live.server._admission.running == 0  # no leaked place
            assert (await aexchange(live.port, good_post()))[0] == 200
        assert live.report.clean

    run(scenario)


def test_connection_limit_is_exact_and_turned_away_connections_hold_no_place(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        settings = serving_settings(config, max_concurrency=1, queue_limit=1, max_connections=3)
        async with Live(settings, StubPipeline(Holder()), store, project_root) as live:
            idle = [await aopen(live.port) for _ in range(3)]  # three connections that have sent nothing
            await until(lambda: live.server.open == 3)
            status, _, body = await aexchange(live.port, b'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')
            assert status == 503 and json.loads(body)['error']['code'] == 'overloaded'
            assert live.server.open == 3  # a refused connection is not a place held
            await until(lambda: live.server.turned_away == 0)
            idle.pop()[1].close()
            await until(lambda: live.server.open == 2)
            status, _, body = await aexchange(live.port, b'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')
            assert status == 200 and json.loads(body) == {'status': 'ok'}
            for _, writer in idle:
                writer.close()
        assert live.report.clean

    run(scenario)


def test_a_queued_request_whose_client_leaves_gives_its_place_back(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        pipeline = StubPipeline(holder)
        settings = serving_settings(config, max_concurrency=1, queue_limit=1, max_connections=4)
        async with Live(settings, pipeline, store, project_root) as live:
            running = asyncio.create_task(aexchange(live.port, good_post()))
            await until(lambda: holder.inflight == 1)
            reader, writer = await aopen(live.port)
            writer.write(good_post())
            await until(lambda: live.server.waiting == 1)
            writer.close()  # the queued client leaves
            await until(lambda: live.server.waiting == 0 and live.server.open == 1)
            assert pipeline.cancelled == 0 and len(pipeline.requests) == 1  # the running one is untouched
            replacement = asyncio.create_task(aexchange(live.port, good_post()))
            await until(lambda: live.server.waiting == 1)  # the freed place is usable
            holder.release()
            assert (await running)[0] == 200 and (await replacement)[0] == 200
            await until(lambda: live.server.open == 0)
            assert live.server._admission.running == 0
        assert live.report.clean

    run(scenario)


def test_an_active_request_whose_client_leaves_is_cancelled_and_frees_its_slot(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        pipeline = StubPipeline(holder)
        settings = serving_settings(config, max_concurrency=1, queue_limit=0, max_connections=2)
        async with Live(settings, pipeline, store, project_root) as live:
            reader, writer = await aopen(live.port)
            writer.write(good_post())
            await until(lambda: holder.inflight == 1)
            writer.close()
            await until(lambda: pipeline.cancelled == 1 and live.server.open == 0)
            assert holder.inflight == 0 and live.server._admission.running == 0
            holder.release()
            assert (await aexchange(live.port, good_post()))[0] == 200  # the slot is usable at once
        assert live.report.clean

    run(scenario)


def test_the_service_deadline_cancels_the_pipeline_and_answers_504(model_test_profile, tmp_path, project_root):
    """Deadline scope: queue time, body and generation are inside `serving.total_deadline_s`."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        pipeline = StubPipeline(holder)
        settings = serving_settings(config, max_concurrency=1, queue_limit=1, total_deadline_s=0.3)
        async with Live(settings, pipeline, store, project_root) as live:
            status, _, body = await aexchange(live.port, good_post())
            assert status == 504 and json.loads(body)['error']['code'] == 'deadline_exceeded'
            assert 'trace_id' not in body.decode()  # a transport error never invents an envelope field
            assert pipeline.cancelled == 1 and live.server.open == 0 and live.server._admission.running == 0
            # a request that only waits in the queue is bounded by the same deadline
            first = asyncio.create_task(aexchange(live.port, good_post()))
            await until(lambda: holder.inflight == 1)
            queued_status, _, _ = await aexchange(live.port, good_post())
            assert queued_status == 504
            assert (await first)[0] == 504
            await until(lambda: live.server.open == 0 and live.server.waiting == 0)
        assert live.report.clean

    run(scenario)


def test_a_slow_body_is_answered_408_by_the_read_deadline_and_never_admitted(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        pipeline = StubPipeline(Holder())
        settings = serving_settings(config, read_timeout_s=0.4)
        async with Live(settings, pipeline, store, project_root) as live:
            body = json.dumps(GOOD_REQUEST).encode()
            reader, writer = await aopen(live.port)
            writer.write(raw_post('/v1/enhance', body)[:-5])  # five bytes short, then silence
            loop = asyncio.get_running_loop()
            started = loop.time()
            data = await asyncio.wait_for(reader.read(65536), 20)
            assert data.startswith(b'HTTP/1.1 408 ') and loop.time() - started < 10
            writer.close()
            assert not pipeline.requests
            await until(lambda: live.server.open == 0)

            # a client that trickles one byte at a time cannot extend the single deadline
            reader, writer = await aopen(live.port)
            head = f'POST /v1/enhance HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n'
            writer.write(head.encode())
            answered = asyncio.ensure_future(reader.read(65536))
            sent = 0
            while not answered.done() and sent < len(body) - 1:
                writer.write(body[sent:sent + 1])
                sent += 1
                await asyncio.wait({answered}, timeout=0.05)
            assert (await asyncio.wait_for(answered, 20)).startswith(b'HTTP/1.1 408 ')
            writer.close()
            assert not pipeline.requests
        assert live.report.clean

    run(scenario)


class FakeWriter:
    """Labelled double of an asyncio StreamWriter whose peer is slow or gone: `hang` is where the transport blocks
    (`drain`: the buffer never empties; `closed`: the pending write never ends; `reset`: the connection is lost).
    A real loopback socket cannot be made to block deterministically: a Windows loopback stack accepts 32 MiB from a
    client that never reads."""
    def __init__(self, hang=None):
        self.hang, self.aborted, self.closed, self.written = hang, False, False, b''
        self.transport = SimpleNamespace(abort=self.abort)

    def abort(self):
        self.aborted = True

    def write(self, data):
        self.written += data

    async def drain(self):
        if self.hang == 'drain':
            await asyncio.Event().wait()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.hang == 'closed':
            await asyncio.Event().wait()
        if self.hang == 'reset':
            raise ConnectionResetError('connection lost')


@pytest.mark.parametrize('hang, delivered, noted', [(None, True, False), ('drain', False, True), ('closed', False, True),
                                                    ('reset', False, False)])
def test_the_write_deadline_bounds_every_wait_of_a_response(model_test_profile, tmp_path, project_root, hang, delivered, noted):
    """A response is delivered only when it was written, flushed and released by the transport within
    `serving.http.write_timeout_s`; a reader that is too slow gets its connection aborted, and a reset is not a delivery."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        settings = serving_settings(config, write_timeout_s=0.2)
        live = Live(settings, StubPipeline(Holder()), store, project_root)
        writer = FakeWriter(hang)
        loop = asyncio.get_running_loop()
        started = loop.time()
        written = await live.server._write(writer, b'HTTP/1.1 200 OK\r\n\r\n')
        assert await live.server._finish(None, writer, written, True) is delivered
        assert loop.time() - started < 10  # a few bounds, never the length of a stalled peer
        assert writer.closed and writer.aborted is (hang in ('drain', 'closed'))
        assert ('write timed out' in live.notes.stream.getvalue()) is noted

    run(scenario)


class OrderedServer(Server):
    """Labelled test double of the transport: records when the response bytes were flushed."""
    log = None

    async def _write(self, writer, data):
        delivered = await super()._write(writer, data)
        if delivered and data.startswith(b'HTTP/1.1 200'):
            self.log.append('flushed')
        return delivered


def test_the_response_is_acknowledged_once_and_only_after_its_bytes_were_flushed(
        model_test_profile, tmp_path, project_root, monkeypatch):
    config, store = env(model_test_profile, tmp_path, project_root)
    log = []
    OrderedServer.log = log
    monkeypatch.setattr('serve_fixtures.Server', OrderedServer)

    async def scenario():
        response = StubResponse(stub_envelope(), log=log)

        async def behaviour(request):
            return response
        async with Live(serving_settings(config), StubPipeline(behaviour), store, project_root) as live:
            status, _, body = await aexchange(live.port, good_post())
            assert status == 200 and json.loads(body) == response.envelope  # the envelope, unchanged
            await until(lambda: response.acknowledged == 1)
        assert log == ['flushed', 'acknowledged'] and response.abandoned == 0  # a delivered response is never abandoned

    run(scenario)


@pytest.mark.parametrize('failure', ['bad_request', 'pipeline_raises', 'client_leaves', 'publication_fails', 'write_fails'])
def test_no_acknowledgement_without_a_delivery(model_test_profile, tmp_path, project_root, failure):
    config, store = env(model_test_profile, tmp_path, project_root)
    data = b'attachment bytes'
    image = 'sha256:' + hashlib.sha256(data).hexdigest()
    if failure == 'publication_fails':
        (tmp_path / 'data').mkdir(exist_ok=True)
        (tmp_path / 'data' / 'attachments').write_bytes(b'a file where the directory must be')

    async def scenario():
        gate = asyncio.Event()
        response = StubResponse(stub_envelope(), {image: data} if failure == 'publication_fails' else None)

        async def behaviour(request):
            if failure == 'pipeline_raises':
                raise RuntimeError('boom')
            if failure == 'client_leaves':
                await gate.wait()
            return response
        pipeline = StubPipeline(behaviour)
        async with Live(serving_settings(config), pipeline, store, project_root) as live:
            if failure == 'write_fails':
                async def refused(writer, data):  # labelled double: the transport reports a write that failed
                    return False
                live.server._write = refused
                status, _, body = await aexchange(live.port, good_post())
                assert status is None and body == b''
            elif failure == 'bad_request':
                status, _, body = await aexchange(live.port, raw_post('/v1/enhance', b'{"prompt": 1}'))
                assert status == 400 and not pipeline.requests
            elif failure == 'client_leaves':
                reader, writer = await aopen(live.port)
                writer.write(good_post())
                await until(lambda: pipeline.requests)
                writer.close()
                await until(lambda: pipeline.cancelled == 1)
            else:
                status, _, body = await aexchange(live.port, good_post())
                assert status == 500 and json.loads(body)['error']['code'] == (
                    'delivery_failed' if failure == 'publication_fails' else 'internal')
                assert 'trace_id' not in body.decode()
            await until(lambda: live.server.open == 0)
            assert response.acknowledged == 0
            # a response that existed and was not delivered is abandoned at once, exactly once (F2)
            assert response.abandoned == (1 if failure in ('write_fails', 'publication_fails') else 0)
        assert live.report.clean

    run(scenario)


def test_published_attachments_download_only_through_the_responses_index(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)
    data = bytes(range(256)) * 20
    image = 'sha256:' + hashlib.sha256(data).hexdigest()
    other = 'sha256:' + hashlib.sha256(b'not published').hexdigest()

    async def scenario():
        async def behaviour(request):
            return StubResponse(stub_envelope(trace_id='r-served'), {image: data})
        async with Live(serving_settings(config), StubPipeline(behaviour), store, project_root) as live:
            assert (await aexchange(live.port, good_post()))[0] == 200  # publishes before delivering
            status, headers, body = await aexchange(live.port, f'GET /v1/attachments/r-served/{image} HTTP/1.1\r\n\r\n'.encode())
            assert status == 200 and body == data and headers['content-type'] == 'application/octet-stream'
            encoded = image.replace(':', '%3A')
            assert (await aexchange(live.port, f'GET /v1/attachments/r-served/{encoded} HTTP/1.1\r\n\r\n'.encode()))[2] == data
            for path in (f'/v1/attachments/r-other/{image}', f'/v1/attachments/r-served/{other}',
                         '/v1/attachments/r-served/index.json', '/v1/attachments/r-served/' + image[7:],
                         '/v1/attachments/..%2f..%2fserve.yaml/' + image, '/v1/attachments/r-served/%2e%2e',
                         '/v1/attachments/r-served', '/v1/attachments/r-served/' + image + '/x'):
                status, _, body = await aexchange(live.port, f'GET {path} HTTP/1.1\r\n\r\n'.encode())
                assert status == 404 and json.loads(body)['error']['code'] == 'not_found', path
            status, headers, _ = await aexchange(live.port, f'POST /v1/attachments/r-served/{image} HTTP/1.1\r\n\r\n'.encode())
            assert status == 405 and headers['allow'] == 'GET'
        assert live.report.clean

    run(scenario)


def test_an_expired_or_damaged_copy_is_not_served(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)
    data = b'original bytes'
    image = 'sha256:' + hashlib.sha256(data).hexdigest()

    async def scenario():
        async def behaviour(request):
            return StubResponse(stub_envelope(trace_id='r-expiring'), {image: data})
        async with Live(serving_settings(config), StubPipeline(behaviour), store, project_root, retention_s=60) as live:
            assert (await aexchange(live.port, good_post()))[0] == 200
            path = f'GET /v1/attachments/r-expiring/{image} HTTP/1.1\r\n\r\n'.encode()
            assert (await aexchange(live.port, path))[0] == 200
            copy = tmp_path / 'data' / 'attachments' / 'r-expiring' / image[7:]
            copy.write_bytes(b'tampered bytes')  # the size differs from the index
            status, _, body = await aexchange(live.port, path)
            assert status == 500 and b'tampered' not in body
            copy.write_bytes(b'x' * len(data))  # the size matches, the hash does not
            assert (await aexchange(live.port, path))[0] == 500
            copy.write_bytes(data)
            assert (await aexchange(live.port, path))[0] == 200
            index = copy.with_name('index.json')
            record = json.loads(index.read_text())
            record['expires_at'] = '2000-01-01T00:00:00+00:00'
            index.write_text(json.dumps(record))
            assert (await aexchange(live.port, path))[0] == 404  # expired: gone for the client at once
        assert live.report.clean

    run(scenario)


def test_start_removes_expired_attachment_directories_before_listening(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)
    base = tmp_path / 'data' / 'attachments'
    for name, expires in (('r-old', '2000-01-01T00:00:00+00:00'), ('r-live', '2999-01-01T00:00:00+00:00')):
        (base / name).mkdir(parents=True)
        (base / name / 'index.json').write_text(json.dumps({'trace_id': name, 'expires_at': expires, 'images': []}))

    async def scenario():
        async with Live(serving_settings(config), StubPipeline(Holder()), store, project_root):
            assert sorted(path.name for path in base.iterdir()) == ['r-live']

    run(scenario)


# --- stop -------------------------------------------------------------------------------------------------------------

def test_stop_lets_a_running_request_finish_within_the_bound_and_is_clean(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        pipeline = StubPipeline(holder)
        settings = serving_settings(config, max_concurrency=1, queue_limit=1, shutdown_timeout_s=30)
        live = await Live(settings, pipeline, store, project_root).__aenter__()
        running = asyncio.create_task(aexchange(live.port, good_post()))
        await until(lambda: holder.inflight == 1)
        queued = asyncio.create_task(aexchange(live.port, good_post()))
        await until(lambda: live.server.waiting == 1)
        stopping = asyncio.create_task(live.stop())
        await until(lambda: live.server.stopping)
        assert not stopping.done()  # it waits for the running request
        with pytest.raises(OSError):
            await aopen(live.port)  # the listener is closed first
        holder.release()
        assert (await running)[0] == 200
        status, _, body = await queued
        assert status == 503 and json.loads(body)['error']['code'] == 'stopping' and len(pipeline.requests) == 1
        report = await stopping
        assert report.clean and pipeline.closed and not live.server._tasks
        assert asyncio.all_tasks() == {asyncio.current_task()}  # no handler, watcher or close task survives

    run(scenario)


def test_a_request_that_outlasts_the_stop_is_cancelled_joined_and_reported(model_test_profile, tmp_path, project_root):
    """Q5: an overrun is an operational failure, never a clean stop with a survivor."""
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        holder = Holder()
        pipeline = StubPipeline(holder)
        settings = serving_settings(config, shutdown_timeout_s=0.3)
        live = await Live(settings, pipeline, store, project_root).__aenter__()
        running = asyncio.create_task(aexchange(live.port, good_post()))
        await until(lambda: holder.inflight == 1)
        report = await live.stop()
        assert report.cancelled == 1 and not report.clean
        assert pipeline.cancelled == 1 and holder.inflight == 0 and pipeline.closed  # cancelled, joined, then closed
        assert (await running)[0] is None  # no answer, no acknowledgement
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run(scenario)


def test_a_pipeline_close_that_outlasts_the_bound_is_reported_but_still_joined(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        gate = asyncio.Event()
        pipeline = StubPipeline(Holder(), close_gate=gate)
        live = await Live(serving_settings(config, shutdown_timeout_s=0.2), pipeline, store, project_root).__aenter__()
        stopping = asyncio.create_task(live.stop())
        await asyncio.sleep(1.0)  # five bounds: only a join-less stop would have returned
        assert not stopping.done() and not pipeline.closed
        gate.set()
        report = await stopping
        assert report.close_overran and not report.clean and pipeline.closed  # joined all the same

    run(scenario)


def test_stop_with_nothing_running_is_clean_and_closes_the_listener(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        pipeline = StubPipeline(Holder())
        live = await Live(serving_settings(config), pipeline, store, project_root).__aenter__()
        port = live.port
        report = await live.stop()
        assert report.clean and pipeline.closed
        with pytest.raises(OSError):
            await aopen(port)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run(scenario)


# --- start failures ---------------------------------------------------------------------------------------------------

def test_an_occupied_port_fails_the_start_and_leaves_no_listener(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        with socket.socket() as holder:
            holder.bind(('127.0.0.1', 0))
            holder.listen()
            settings = serving_settings(config, port=holder.getsockname()[1])
            server = Server(settings, StubPipeline(Holder()), store, project_root, 60, Live(settings, None, store, project_root).notes)
            with pytest.raises(ServeFailure, match='cannot listen on 127.0.0.1:'):
                await server.start()
            assert (await server.stop()).clean
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run(scenario)


def test_a_partial_bind_closes_the_listener_it_did_open(model_test_profile, tmp_path, project_root):
    """`localhost` resolves to several addresses; when one of them cannot be bound the others are closed again."""
    infos = {info[0] for info in socket.getaddrinfo('localhost', 0, type=socket.SOCK_STREAM)}
    if not {socket.AF_INET, socket.AF_INET6} <= infos or not socket.has_ipv6:
        pytest.skip('localhost does not resolve to both an IPv4 and an IPv6 address here')
    config, store = env(model_test_profile, tmp_path, project_root)

    async def scenario():
        port = free_port()
        with socket.socket(socket.AF_INET6) as holder:
            try:
                holder.bind(('::1', port))
            except OSError:
                pytest.skip('the IPv6 loopback cannot be bound here')
            holder.listen()
            settings = serving_settings(config, host='localhost', port=port)
            server = Server(settings, StubPipeline(Holder()), store, project_root, 60, Live(settings, None, store, project_root).notes)
            with pytest.raises(ServeFailure):
                await server.start()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))  # the IPv4 listener that had been opened is gone

    run(scenario)


def test_a_failure_before_the_listener_exists_leaves_the_port_unbound(model_test_profile, tmp_path, project_root):
    config, store = env(model_test_profile, tmp_path, project_root)

    class Failing(StubPipeline):
        def __init__(self):
            super().__init__(Holder())
            self.lifecycle.io = self.broken

        async def broken(self, function, *args):
            raise RuntimeError('the attachment directory cannot be listed')

    async def scenario():
        settings = serving_settings(config)
        pipeline = Failing()
        live = Live(settings, pipeline, store, project_root)
        with pytest.raises(RuntimeError):
            await live.__aenter__()
        assert (await live.stop()).clean
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', settings['port']))

    run(scenario)


def test_the_real_pipeline_leaves_no_thread_task_reservation_or_row_after_a_stop_with_a_request_running(
        model_test_profile, tmp_path, project_root):
    from cli_fixtures import finish_setup, model_server
    from prompt_enhancer.pipeline import Pipeline
    from serve_fixtures import Gate, held_double
    from test_cli_enhance import request_for

    gate = Gate()
    with model_server(held_double(gate)) as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.http.shutdown_timeout_s': 0.5})
        finish_setup(config, project_root, handler)
        store = store_for(config, project_root)

        async def scenario():
            async with Pipeline(config, project_root=project_root) as pipeline:
                live = await Live(serving_settings(config), pipeline, store, project_root).__aenter__()
                loop = asyncio.get_running_loop()
                running = asyncio.create_task(aexchange(live.port, raw_post(
                    '/v1/enhance', json.dumps(request_for('generate')).encode())))
                await loop.run_in_executor(None, gate.wait_for, 1)
                report = await live.stop()
                assert report.cancelled == 1 and not report.clean
                assert (await running)[0] is None
                lifecycle = pipeline.lifecycle
                assert not lifecycle._reserved and lifecycle._used == 0  # no orphan trace reservation
                assert asyncio.all_tasks() == {asyncio.current_task()}
            assert not [thread for thread in threading.enumerate() if thread.name.startswith('pe-trace')]
            with pytest.raises(OSError):
                await aopen(live.port)
        try:
            asyncio.run(asyncio.wait_for(scenario(), 120))
        finally:
            gate.release()
    from prompt_enhancer.storage import open_data_store
    assert open_data_store(config, 'enhance serve', project_root=project_root).read_rows('request_traces') == []
