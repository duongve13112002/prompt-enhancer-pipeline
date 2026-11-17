"""Connection ownership, shutdown order and refusal draining of the embedding server (T-018, fix round 2: RT1 to RT3).

Everything runs over real loopback sockets against a labelled engine double: no model, no GPU, no torch.
"""

from http.server import BaseHTTPRequestHandler
import io
import json
import os
import pickle
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from hypothesis import given, settings as hypothesis_settings, strategies as st
import httpx
import pytest

from prompt_enhancer.embed_server import server as server_module
from prompt_enhancer.embed_server.server import CONNECTION_THREAD, ShutdownIncomplete, drain
from embed_server_fixtures import EngineDouble, running_server
from test_embed_server_http import (HELD_S, Gate, exchange, hold_deadlines, make_settings, port_is_free, post,
                                    post_head, request)


def handler_threads():
    return [thread for thread in threading.enumerate() if thread.name == CONNECTION_THREAD and thread.is_alive()]


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def connect(settings):
    return socket.create_connection((settings.host, settings.port), timeout=10)


def read_to_end(sock):
    chunks = []
    try:
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    except OSError:  # a reset after the reply: what arrived is what counts
        pass
    return b''.join(chunks)


def hold(settings, readers, count=1):
    """Connect `count` idle holders and wait until the server gave each a handler. The barrier is the holders' own
    readers (recorded by `hold_deadlines`), not a thread count: a handler thread of the readiness probe satisfies a
    count while the holder itself is being turned away (T-018-R4). The caller clears `readers` after the server is up."""
    clients = [connect(settings) for _ in range(count)]
    assert wait_for(lambda: len(readers) == count), 'a holder was not given a handler thread'
    return clients


def expire(readers):
    """Let every held deadline pass now (the production `readinto` then ends the read)."""
    for reader in list(readers):
        reader._deadline = time.monotonic()


@pytest.fixture(autouse=True)
def no_handler_threads_before_the_test():
    assert wait_for(lambda: not handler_threads()), 'a handler thread of an earlier test is still alive'


class Recording(EngineDouble):
    """Labelled double: records when it is entered, left and closed, and whether it was used after the close."""

    def __init__(self, gate=None):
        super().__init__()
        self.events, self.gate = [], gate

    def embed(self, items):
        if self.closed:
            self.events.append('embed-after-close')
        self.events.append('embed-start')
        if self.gate is not None:
            self.gate.entered.set()
            assert self.gate.release.wait(HELD_S)
        try:
            return super().embed(items)
        finally:
            self.events.append('embed-end')

    def close(self):
        self.events.append(('close', len(handler_threads())))
        super().close()


# --- RT2: connections and handler threads are bounded before a thread exists ----------------------------------------

@pytest.mark.parametrize('cap', [2, 4])
def test_connections_over_the_cap_get_a_503_and_no_thread(project_root, model_test_profile, monkeypatch, cap):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=cap, max_pending_requests=1,
                                read_timeout_s=3)
    created = []

    class Counting(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self.name)

    readers = hold_deadlines(monkeypatch)  # the holders cannot expire by the clock while the test looks at the cap
    with running_server(settings) as (base, engine, stop, result):
        monkeypatch.setattr(threading, 'Thread', Counting)  # the accept loop is up: count what is created from now on
        readers.clear()  # the readiness probe's reader
        holders = hold(settings, readers, cap)
        try:
            assert wait_for(lambda: len(handler_threads()) == cap)
            assert created.count(CONNECTION_THREAD) == cap
            for _ in range(25):
                with connect(settings) as extra:
                    reply = read_to_end(extra)
                assert b' 503 ' in reply.split(b'\r\n')[0] and json.loads(reply.partition(b'\r\n\r\n')[2]) == {
                    'error': {'code': 'overloaded', 'message': 'too many connections'}}
            assert created.count(CONNECTION_THREAD) == cap and len(handler_threads()) == cap  # no thread for 25 more
            try:  # health holds no reserved slot; the reply to a request already sent is best effort (may be a reset)
                assert httpx.get(base + settings.health_path, timeout=5).status_code == 503
            except httpx.TransportError:
                pass
            expire(readers)  # the idle holders end at their (now passed) read deadline and give the slots back
            for holder in holders:
                holder.settimeout(8)
                assert holder.recv(10) == b''
        finally:
            for holder in holders:
                holder.close()
        assert wait_for(lambda: not handler_threads())
        assert httpx.get(base + settings.health_path, timeout=5).status_code == 200
        assert engine.calls == []


def test_many_slow_connections_never_exceed_the_cap(project_root, model_test_profile, monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=4, max_pending_requests=1,
                                read_timeout_s=4)  # held connections must outlive the 0.5 s look below with margin
    peak = [0]
    sampling = threading.Event()
    at_capacity = threading.Event()

    def sample():
        while not sampling.is_set():
            peak[0] = max(peak[0], len(handler_threads()))
            if peak[0] == settings.max_connections:
                at_capacity.set()
            time.sleep(0.005)

    sampler = threading.Thread(target=sample, name='sampler')
    readers = hold_deadlines(monkeypatch)
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        sampler.start()
        clients = []
        try:
            for _ in range(40):  # each sends half a request line and then goes quiet
                client = connect(settings)
                client.sendall(b'POST /v1/emb')
                clients.append(client)
            assert wait_for(lambda: len(readers) == 4)  # the four holders' own handlers
            assert at_capacity.wait(5), 'sampler did not observe the held connection capacity'
            assert len(handler_threads()) == 4
            expire(readers)  # the deadline ends every held connection
            assert wait_for(lambda: not handler_threads(), timeout=15)
        finally:
            sampling.set()
            sampler.join()
            for client in clients:
                client.close()
        assert peak[0] == 4
        assert httpx.get(base + settings.health_path, timeout=5).status_code == 200
        assert engine.calls == []


def test_a_flood_of_turned_away_connections_logs_once_and_is_summed_up_at_stop(project_root, model_test_profile, capfd,
                                                                              monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=1, max_pending_requests=1,
                                read_timeout_s=5)
    readers = hold_deadlines(monkeypatch)
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        (holder,) = hold(settings, readers)
        for _ in range(30):
            with connect(settings) as extra:
                read_to_end(extra)
        holder.close()
        stop.set()
        assert wait_for(lambda: 'reason' in result)
    lines = [json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith('{')]
    assert [line for line in lines if line['event'] == 'connection_refused'] == [
        {'event': 'connection_refused', 'type': 'overloaded'}]  # one line, not thirty
    assert [line for line in lines if line['event'] == 'log_summary'][0]['counts']['connection_refused:overloaded'] == 30


# --- RT1: one shutdown order -------------------------------------------------------------------------------------

def test_stop_closes_every_accepted_socket_and_joins_every_handler_before_the_engine(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, max_pending_requests=1, read_timeout_s=10)
    engine = Recording()
    with running_server(settings, engine) as (base, _, stop, result):
        idle = [connect(settings) for _ in range(3)]
        half_header = connect(settings)
        half_header.sendall(b'POST /v1/emb')
        upload = connect(settings)  # admitted, 10 of 1000 body bytes sent, then silence
        upload.sendall(post_head(settings, 1000) + b'0123456789')
        clients = idle + [half_header, upload]
        try:
            assert wait_for(lambda: len(handler_threads()) == 5)
            assert wait_for(lambda: post(base, settings, request(['a'])).status_code == 503)  # the upload holds the slot
            started = time.monotonic()
            stop.set()
            assert wait_for(lambda: 'reason' in result, timeout=5)  # not the 10 s read deadline
            assert time.monotonic() - started < 5
            assert handler_threads() == []  # every handler thread was joined, none left behind
            for client in clients:  # and every accepted socket is shut: a read ends at once, not by timing out
                client.settimeout(3)
                try:
                    client.recv(100)
                except TimeoutError:
                    pytest.fail('an accepted socket was left open by the stop')
                except OSError:
                    pass
        finally:
            for client in clients:
                client.close()
    assert result['reason'] == 'stop' and engine.events == [('close', 0)]  # closed after the last handler, once
    assert port_is_free(settings)


def test_stop_lets_the_forward_pass_finish_and_closes_the_engine_after_it(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=10)
    gate = Gate()
    engine = Recording(gate)
    with running_server(settings, engine) as (base, _, stop, result):
        answer = {}
        client = threading.Thread(target=lambda: answer.update(status=post(base, settings, request(['a'])).status_code))
        client.start()
        try:
            assert gate.entered.wait(10)
            stop.set()
            assert wait_for(lambda: port_is_free(settings), timeout=5)  # the listener closes first
            time.sleep(0.5)
            assert 'reason' not in result and engine.closed is False  # the engine is not closed under a running forward
        finally:
            gate.release.set()
            client.join(HELD_S)
        assert wait_for(lambda: 'reason' in result)
    assert answer['status'] == 200  # the request in the engine was answered, not cut
    assert engine.events == ['embed-start', 'embed-end', ('close', 0)]


def test_a_request_queued_for_the_gpu_is_refused_at_stop_and_never_reaches_a_closed_engine(project_root,
                                                                                         model_test_profile,
                                                                                         monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=10, max_pending_requests=2)
    parsed, original = [], server_module.parse_request

    def counting(body, limits):
        parsed.append(len(body))
        return original(body, limits)

    monkeypatch.setattr(server_module, 'parse_request', counting)
    gate = Gate()
    engine = Recording(gate)
    with running_server(settings, engine) as (base, _, stop, result):
        answers = []

        def ask():
            reply = post(base, settings, request(['a']))
            answers.append((reply.status_code, reply.json().get('error', {}).get('code')))

        clients = [threading.Thread(target=ask) for _ in range(2)]
        clients[0].start()
        assert gate.entered.wait(10)
        clients[1].start()
        try:
            assert wait_for(lambda: len(parsed) == 2)  # the second request has its body and goes on to the GPU lock
            time.sleep(0.3)  # and waits there behind the forward pass of the first
            stop.set()
            assert wait_for(lambda: port_is_free(settings), timeout=5)
        finally:
            gate.release.set()
            for client in clients:
                client.join(HELD_S)
        assert wait_for(lambda: 'reason' in result)
    assert sorted(answers, key=str) == [(200, None), (503, 'stopping')]
    assert engine.events == ['embed-start', 'embed-end', ('close', 0)]  # one forward, then the close; nothing after it


def test_a_forward_that_outlasts_the_stop_is_reported_and_the_engine_is_never_closed_under_it(project_root,
                                                                                               model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=0.5)
    gate = Gate()
    engine = Recording(gate)
    with running_server(settings, engine) as (base, _, stop, result):
        outcome = {}

        def ask():
            try:
                outcome['status'] = post(base, settings, request(['a'])).status_code
            except httpx.HTTPError as error:  # the stop cut the socket of a request the engine could not finish
                outcome['error'] = type(error).__name__

        client = threading.Thread(target=ask)
        client.start()
        try:
            assert gate.entered.wait(10)
            started = time.monotonic()
            stop.set()
            assert wait_for(lambda: 'error' in result, timeout=5)
            assert time.monotonic() - started < stop_bound(settings)  # a few read deadlines, not the length of the forward
            assert isinstance(result['error'], ShutdownIncomplete) and 'forward pass' in str(result['error'])
            assert engine.closed is False and 'embed-after-close' not in engine.events
            assert port_is_free(settings)
        finally:
            gate.release.set()
            client.join(HELD_S)
        assert wait_for(lambda: not handler_threads())
    assert engine.events == ['embed-start', 'embed-end'] and engine.closed is False


def test_the_command_reports_an_unfinished_stop_with_its_own_exit_status(monkeypatch, capsys):
    from prompt_enhancer.embed_server import __main__ as entry

    def unfinished(*args, **kwargs):
        raise ShutdownIncomplete('1 handler thread(s) still running')

    # labelled double: the command reads only the window that bounds its last line
    monkeypatch.setattr(entry, 'load_server_settings', lambda *args: SimpleNamespace(read_timeout_s=1))
    monkeypatch.setattr(entry, 'serve', unfinished)
    monkeypatch.setattr(entry.signal, 'signal', lambda *args: None)  # leave the test process's own handlers alone
    assert entry.main(['serve', '--config', 'unused.yaml']) == entry.INCOMPLETE == 3
    assert '1 handler thread(s) still running' in capsys.readouterr().err


# --- round 3 (RT2F1): a stop succeeds only when every server-owned resource is released -------------------------------

STAGES = ['decode', 'forward', 'build', 'send']  # where a handler is held when the stop comes


class Hold:
    """Labelled test gate: holds one handler at a named stage until the test releases it. `armed` keeps the health
    probes of the readiness check out of the way."""

    def __init__(self):
        self.armed, self.entered, self.release = threading.Event(), threading.Event(), threading.Event()

    def pause(self):
        if self.armed.is_set():
            self.entered.set()
            assert self.release.wait(HELD_S), 'the test never released the gate'


def hold_at(stage, monkeypatch):
    """Install a gate at `stage` of a request's life; return (gate, engine double to serve with)."""
    hold, engine = Hold(), EngineDouble()
    if stage == 'forward':
        engine = Gate()
        hold.entered, hold.release = engine.entered, engine.release
        hold.armed.set()
    elif stage == 'decode':
        parse = server_module.parse_request
        monkeypatch.setattr(server_module, 'parse_request', lambda body, limits: (hold.pause(), parse(body, limits))[1])
    elif stage == 'build':
        build = server_module.build_response
        monkeypatch.setattr(server_module, 'build_response', lambda *args: (hold.pause(), build(*args))[1])
    else:
        send = BaseHTTPRequestHandler.send_response
        monkeypatch.setattr(BaseHTTPRequestHandler, 'send_response',
                            lambda self, *args, **kwargs: (hold.pause(), send(self, *args, **kwargs))[1])
    return hold, engine


def send_valid_post(settings):
    payload = json.dumps(request(['a'])).encode()
    sock = connect(settings)
    sock.sendall(post_head(settings, len(payload)) + payload)
    return sock


@pytest.mark.parametrize('stage', STAGES)
def test_a_stop_with_a_handler_held_at_any_stage_is_reported_not_returned_as_success(project_root, model_test_profile,
                                                                                    monkeypatch, stage):
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=0.25)
    hold, double = hold_at(stage, monkeypatch)
    with running_server(settings, double) as (base, engine, stop, result):
        hold.armed.set()
        client = send_valid_post(settings)
        try:
            assert hold.entered.wait(5)
            stop.set()
            assert wait_for(lambda: 'reason' in result or 'error' in result, timeout=5)
            error = result.get('error')
            assert isinstance(error, ShutdownIncomplete), f'the stop returned {result} with a handler held at {stage}'
            assert '1 handler thread(s) still running' in str(error)
            assert '1 request(s) still admitted, 1 of them past their body read' in str(error)
            assert port_is_free(settings)
            assert engine.closed is (stage != 'forward')  # never closed under a forward; a decoder holds no GPU lock
        finally:
            hold.release.set()
            client.close()
        assert wait_for(lambda: not handler_threads())


@pytest.mark.parametrize('stage', STAGES)
def test_the_command_exits_with_status_3_for_a_handler_held_at_any_stage(project_root, model_test_profile, monkeypatch,
                                                                         capsys, stage):
    from prompt_enhancer.embed_server import __main__ as entry

    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=0.25)
    hold, double = hold_at(stage, monkeypatch)
    real_serve, held, problems = entry.serve, [], []

    def serve_and_stop(settings_, load, *, stop):
        def client():
            try:
                base = f'http://{settings_.host}:{settings_.port}'
                assert wait_for(lambda: _healthy(base + settings_.health_path))
                hold.armed.set()
                held.append(send_valid_post(settings_))
                assert hold.entered.wait(10)
            except BaseException as error:  # reported by the test thread below
                problems.append(error)
            finally:
                stop.set()

        threading.Thread(target=client, daemon=True).start()
        return real_serve(settings_, load, stop=stop, announce=lambda _: None)

    monkeypatch.setattr(entry, 'load_server_settings', lambda *args: settings)
    monkeypatch.setattr(entry, '_loader', lambda _: double)
    monkeypatch.setattr(entry, 'serve', serve_and_stop)
    monkeypatch.setattr(entry.signal, 'signal', lambda *args: None)
    try:
        status = entry.main(['serve', '--config', 'unused.yaml'])
    finally:
        hold.release.set()
        for sock in held:
            sock.close()
    assert not problems, problems
    assert status == entry.INCOMPLETE == 3
    assert '1 handler thread(s) still running' in capsys.readouterr().err
    assert wait_for(lambda: not handler_threads())


def _healthy(url):
    try:
        return httpx.get(url, timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def stop_directly(project_root, model_test_profile, engine=None):
    """A real server with its accept thread running and an engine double; the test perturbs its state (labelled
    doubles at the seams) and then calls `finish`. Return (settings, state, server, accept thread)."""
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=0.2)
    state = server_module._State(settings, time.monotonic)
    state.engine = EngineDouble() if engine is None else engine
    server = server_module._Server((settings.host, settings.port), server_module._handler(state), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return settings, state, server, thread


def finish(state, server, thread, accept=None):
    """Run `stop_server`; return its `ShutdownIncomplete` or None. The listener is closed whatever the stop did."""
    try:
        server_module.stop_server(server, thread if accept is None else accept, state)
        return None
    except ShutdownIncomplete as error:
        return error
    finally:
        server.socket.close()


def test_a_stop_with_nothing_left_returns_with_everything_released(project_root, model_test_profile):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    assert finish(state, server, thread) is None
    assert (state.engine, state.counts(), server.registered(), thread.is_alive()) == (None, (0, 0), 0, False)
    assert server.socket.fileno() == -1


@pytest.mark.parametrize('in_flight, working', [(1, 0), (0, 1), (2, 2)])
def test_an_admission_slot_still_held_is_reported(project_root, model_test_profile, in_flight, working):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    state.in_flight, state.working = in_flight, working  # a slot nobody releases: no handler, no socket involved
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete)
    assert f'{in_flight} request(s) still admitted, {working} of them past their body read' in str(error)


def test_a_socket_still_registered_is_reported_and_closed(project_root, model_test_profile):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    gone = threading.Thread(target=lambda: None)
    gone.start()
    gone.join()
    leftover = socket.socket()
    server._connections[leftover] = gone  # its thread ended without forgetting it
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and '1 accepted socket(s) still registered' in str(error)
    assert leftover.fileno() == -1  # the stop closed it anyway


def test_a_handler_thread_the_join_could_not_end_is_reported(project_root, model_test_profile, monkeypatch):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    monkeypatch.setattr(server_module._Server, 'join_connections', lambda self, timeout: 2)  # labelled double
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and '2 handler thread(s) still running' in str(error)


def test_a_listener_that_did_not_close_is_reported(project_root, model_test_profile, monkeypatch):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    monkeypatch.setattr(server_module._Server, 'server_close', lambda self: None)  # labelled double
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and 'listening socket is still open' in str(error)


def test_an_accept_thread_that_did_not_end_is_reported(project_root, model_test_profile):
    class Stuck:  # labelled double: joins to nothing and says it is alive
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    _, state, server, thread = stop_directly(project_root, model_test_profile)
    error = finish(state, server, thread, accept=Stuck())
    assert isinstance(error, ShutdownIncomplete) and 'accept thread is still running' in str(error)


def test_an_engine_that_cannot_be_closed_is_reported(project_root, model_test_profile):
    class Stubborn(EngineDouble):  # labelled double
        def close(self):
            raise RuntimeError('secret detail that must not reach the message')

    engine = Stubborn()
    _, state, server, thread = stop_directly(project_root, model_test_profile, engine)
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and 'the engine close failed (RuntimeError)' in str(error)
    assert 'secret detail' not in str(error) and state.engine is engine
    assert state.gpu.acquire(timeout=0)  # the lock was given back
    state.gpu.release()


def test_the_message_names_every_survivor_and_no_request_content(project_root, model_test_profile):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    state.in_flight = 1
    state.gpu.acquire()  # a forward that never ends
    try:
        error = finish(state, server, thread)
    finally:
        state.gpu.release()
    text = str(error)
    assert '1 request(s) still admitted' in text and 'GPU lock was not free' in text and 'forward pass' in text


def test_nothing_is_admitted_once_the_stop_began_and_work_is_waited_for_only_past_its_body(project_root,
                                                                                       model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, max_pending_requests=2)
    state = server_module._State(settings, time.monotonic)
    assert state.admit()
    assert state.wait_work_done(0) is True  # admitted, but still reading its body: not waited for
    state.work_started()
    assert state.wait_work_done(0.05) is False  # past its body and not done: the stop waits, up to its window
    state.work_done()
    assert state.wait_work_done(0) is True
    state.release()
    state.begin_closing()
    assert state.admit() is False and state.in_flight == 0  # free slots, yet nothing new once the stop began


# --- RT3: refusal draining has a server-owned byte budget -------------------------------------------------------------

@given(budget=st.integers(0, 600), available=st.integers(0, 600), block=st.integers(1, 64))
@hypothesis_settings(max_examples=200, deadline=None)
def test_drain_reads_at_most_the_budget_in_blocks_and_stops_at_the_end_of_input(budget, available, block):
    requested, left = [], [available]

    def read(count):
        requested.append(count)
        taken = min(count, left[0])
        left[0] -= taken
        return b'x' * taken

    dropped = drain(read, budget, block)
    assert dropped == min(budget, available)
    assert all(1 <= count <= block for count in requested) and sum(requested) <= budget + block
    assert left[0] == available - dropped


@pytest.fixture
def drained(monkeypatch):
    """Records what each `drain` call dropped (the real one runs): the server's own byte accounting."""
    dropped = []
    real = server_module.drain

    def spy(read, budget, block=server_module.DRAIN_BLOCK):
        count = real(read, budget, block)
        dropped.append(count)
        return count

    monkeypatch.setattr(server_module, 'drain', spy)
    return dropped


def push_until_closed(sock, limit=64 * 1024 * 1024):
    """Send blocks until the server stops reading and the connection breaks; return the bytes sent, or None."""
    block, sent = b'x' * 65536, 0
    sock.settimeout(10)
    try:
        while sent < limit:
            sock.sendall(block)
            sent += len(block)
    except OSError:
        return sent
    return None


@pytest.mark.parametrize('discard', [150000, 250000])
@pytest.mark.parametrize('head', ['declared_huge', 'chunked', 'unparsable_line'])
def test_the_discard_is_capped_by_the_server_budget_whatever_the_client_declares(project_root, model_test_profile,
                                                                                 drained, discard, head):
    settings, _ = make_settings(project_root, model_test_profile, max_discard_bytes=discard, read_timeout_s=8)
    assert settings.max_request_bytes == 100000
    declared = {  # the last one: a request line beyond the stdlib's 65536 bytes, answered 414 before any header exists
        'declared_huge': f'POST {settings.embeddings_path} HTTP/1.1\r\nContent-Length: 1000000000000\r\n\r\n',
        'chunked': f'POST {settings.embeddings_path} HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
        'unparsable_line': 'GET /' + 'a' * 70000}[head]
    with running_server(settings) as (base, engine, stop, result):
        with connect(settings) as raw:
            raw.sendall(declared.encode())
            sent = push_until_closed(raw)
        assert sent is not None, 'the server kept reading past its budget'
        assert wait_for(lambda: drained)
        assert drained == [discard]  # exactly the server's budget, not the declared terabyte
        assert wait_for(lambda: not handler_threads())
        assert engine.calls == []


def test_a_body_smaller_than_the_budget_is_drained_to_its_declared_end(project_root, model_test_profile, drained):
    settings, _ = make_settings(project_root, model_test_profile, max_discard_bytes=400000)
    with running_server(settings) as (base, engine, stop, result):
        status, payload = exchange(settings, post_head(settings, 300000), b'x' * 300000)  # over the 100000 limit
        assert status == 413 and payload['error']['code'] == 'request_too_large'
        assert wait_for(lambda: drained)
        assert drained == [300000]  # the declared length is the smaller of the two bounds


# --- T-018-R4: over-cap refusals linger on the accept loop and are drained, bounded by existing keys ---------------------

def server_instances(monkeypatch):
    """Labelled seam: records every `_Server` the test creates, to read its lingering set."""
    created, real = [], server_module._Server.__init__

    def init(self, *args, **kwargs):
        real(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(server_module._Server, '__init__', init)
    return created


@pytest.fixture
def lingered(monkeypatch):
    """Labelled seam: one record per over-cap socket kept for its drain, written when it ends: the bytes it dropped
    (its budget less what was left) and how long it lingered. The production methods run unchanged."""
    records, started = [], {}
    real_linger, real_end = server_module._Server._linger, server_module._Server._end_lingering

    def linger(self, request):
        before = time.monotonic()  # taken before the deadline is set: the measured time is never below the deadline's
        kept = real_linger(self, request)
        if kept:
            started[request] = before
        return kept

    def end(self, request):
        with self._lock:
            entry = self._lingering.get(request)
        if entry is not None:
            records.append({'dropped': self._state.settings.max_discard_bytes - entry[1],
                            'lingered': time.monotonic() - started[request]})
        return real_end(self, request)

    monkeypatch.setattr(server_module._Server, '_linger', linger)
    monkeypatch.setattr(server_module._Server, '_end_lingering', end)
    return records


@pytest.mark.parametrize('discard', [150000, 250000])
def test_an_over_cap_refusal_drops_at_most_the_server_budget_whatever_the_client_sends(project_root,
                                                                                       model_test_profile,
                                                                                       monkeypatch, lingered, discard):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=1, max_pending_requests=1,
                                max_discard_bytes=discard, read_timeout_s=8)
    readers = hold_deadlines(monkeypatch)
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        (held,) = hold(settings, readers)
        try:
            with connect(settings) as extra:
                sent = push_until_closed(extra)
            assert sent is not None, 'the server kept reading past its budget'
            assert wait_for(lambda: lingered)
            assert [record['dropped'] for record in lingered] == [discard]  # exactly the budget, whatever was sent
        finally:
            held.close()
        assert engine.calls == []


def test_a_quiet_lingering_refusal_is_closed_when_the_read_deadline_passes(project_root, model_test_profile,
                                                                            monkeypatch, lingered):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=1, max_pending_requests=1,
                                read_timeout_s=0.3)
    readers = hold_deadlines(monkeypatch)
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        (held,) = hold(settings, readers)
        try:
            with connect(settings) as extra:  # sends a piece of a request, then stays open and quiet
                extra.sendall(b'POST /v1/emb')
                assert b' 503 ' in read_to_end(extra).split(b'\r\n')[0]
                assert wait_for(lambda: lingered)  # eventually closed by the server, not by the client
                assert lingered[0]['lingered'] >= settings.read_timeout_s  # never before its deadline
        finally:
            held.close()


@pytest.mark.parametrize('cap', [2, 4])
def test_the_lingering_refusals_are_bounded_by_max_connections_and_the_stop_closes_them(project_root,
                                                                                         model_test_profile,
                                                                                         monkeypatch, cap):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=cap, max_pending_requests=1,
                                read_timeout_s=30)
    created = server_instances(monkeypatch)
    readers = hold_deadlines(monkeypatch)
    extras = []
    holders = []
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        holders = hold(settings, readers, cap)
        try:
            for _ in range(cap + 3):  # each stays open and quiet, so it lingers until a bound or the stop ends it
                extras.append(connect(settings))
                assert b' 503 ' in read_to_end(extras[-1]).split(b'\r\n')[0]  # all are answered, kept or not
            assert created[0].lingering() == cap  # the rest were closed at once, best effort
            stop.set()
            assert wait_for(lambda: 'reason' in result, timeout=stop_bound(settings))
            assert created[0].lingering() == 0 and result == {'reason': 'stop'}
        finally:
            for client in extras + holders:
                client.close()


def test_the_lingering_bound_is_capped_by_the_selector_capacity(project_root, model_test_profile, monkeypatch):
    import selectors

    if os.name == 'nt':  # the platform fact the constant stands for (see its comment): `select` holds 512 sockets
        assert server_module.SELECT_CAPACITY == 512
    capacity = server_module.SELECT_CAPACITY
    assert server_module.linger_bound(10 ** 6, selectors.SelectSelector) == capacity - 1  # the listener takes one
    assert server_module.linger_bound(3, selectors.SelectSelector) == 3
    assert server_module.linger_bound(10 ** 6, object) == 10 ** 6  # a selector without the limit is not clamped
    monkeypatch.setattr(server_module, 'SELECT_CAPACITY', 4)
    monkeypatch.setattr(server_module, '_selector_type', lambda: selectors.SelectSelector)
    settings, _ = make_settings(project_root, model_test_profile, max_connections=8, max_pending_requests=8,
                                read_timeout_s=30)
    created = server_instances(monkeypatch)
    readers = hold_deadlines(monkeypatch)
    extras = []
    holders = []
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        holders = hold(settings, readers, 8)
        try:
            for _ in range(6):
                extras.append(connect(settings))
                assert b' 503 ' in read_to_end(extras[-1]).split(b'\r\n')[0]
            assert created[0].lingering() == 3  # max_connections 8, but the capacity 4 less the listener
        finally:
            for client in extras + holders:
                client.close()


# The property of a refusal the server decides to send (docs/test-plan.md, Property): its status line reaches the client
# complete before the close whatever the size of the body the client already sent, up to `server.max_discard_bytes`;
# beyond that the reply is best effort (the close holds unread bytes and Windows may reset), and the one thing still
# required is that the connection ends and that a reply which does arrive is a complete status line.

def refusal_exchange(settings, head, body):
    """Send `head` and `body` as a client that writes everything before reading; return (reply, seconds)."""
    started = time.monotonic()
    with connect(settings) as raw:
        raw.settimeout(10)
        try:
            raw.sendall(head + body)
        except OSError:  # the server stopped reading at its budget: the reply is what remains to read
            pass
        reply = read_to_end(raw)
    return reply, time.monotonic() - started


def assert_refusal_property(reply, seconds, size, budget, status):
    prefix = f'HTTP/1.0 {status} '.encode()
    assert seconds < 8, 'the connection did not end'
    if size <= budget:
        assert reply.startswith(prefix) and b'\r\n' in reply, f'{size} bytes pending, budget {budget}: {reply[:40]!r}'
    else:
        assert reply == b'' or (reply.startswith(prefix) and b'\r\n' in reply)


@hypothesis_settings(max_examples=30, deadline=None)
@given(size=st.one_of(st.integers(0, 100000), st.just(100000), st.integers(100001, 200000)))
def _route_refusal_property(settings, size):
    reply, seconds = refusal_exchange(settings, post_head(settings, size, '/nope'), b'x' * size)
    assert_refusal_property(reply, seconds, size, settings.max_discard_bytes, 404)


def test_a_route_refusal_reaches_the_client_for_bodies_below_at_and_above_the_budget(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, max_discard_bytes=100000, read_timeout_s=10)
    with running_server(settings):
        _route_refusal_property(settings)


@hypothesis_settings(max_examples=30, deadline=None)
@given(size=st.one_of(st.integers(100001, 150000), st.just(150000), st.integers(150001, 300000)))
def _oversize_refusal_property(settings, size):
    reply, seconds = refusal_exchange(settings, post_head(settings, size), b'x' * size)
    assert_refusal_property(reply, seconds, size, settings.max_discard_bytes, 413)


def test_an_oversize_refusal_reaches_the_client_for_bodies_below_at_and_above_the_budget(project_root,
                                                                                         model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, max_discard_bytes=150000, read_timeout_s=10)
    assert settings.max_request_bytes == 100000
    with running_server(settings):
        _oversize_refusal_property(settings)


@hypothesis_settings(max_examples=30, deadline=None)
@given(size=st.one_of(st.integers(0, 100000), st.just(100000), st.integers(100001, 200000)))
def _over_cap_refusal_property(settings, size):
    reply, seconds = refusal_exchange(settings, post_head(settings, size), b'x' * size)
    assert_refusal_property(reply, seconds, size, settings.max_discard_bytes, 503)


def test_an_over_cap_refusal_reaches_the_client_for_bodies_below_at_and_above_the_budget(project_root,
                                                                                         model_test_profile,
                                                                                         monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=4, max_pending_requests=1,
                                max_discard_bytes=100000, read_timeout_s=30)
    readers = hold_deadlines(monkeypatch)
    with running_server(settings) as (base, engine, stop, result):
        readers.clear()
        holders = hold(settings, readers, 4)
        try:
            _over_cap_refusal_property(settings)
        finally:
            for holder in holders:
                holder.close()


# --- round 4 (RT3F1): nothing on the accept thread or in the stop waits for the console or for the accept loop ----------

OUTPUT_THREAD = server_module.OUTPUT_THREAD
SLACK_S = 5.0  # test-only: scheduling tolerance on top of the documented stop bound (a hang is unbounded)


def stop_bound(settings):
    """The documented worst case of a stop (`stop_server`): five windows, plus the test's scheduling slack."""
    return 5 * settings.read_timeout_s + SLACK_S


def output_threads():
    return [thread for thread in threading.enumerate() if thread.name == OUTPUT_THREAD and thread.is_alive()]


def full_pipe():
    """A real anonymous OS pipe whose buffer is already full: (read fd, write fd, filler bytes). Nothing reads it
    until the test drains it, so any write to it blocks the writing thread."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    filled = 0
    for size in (4096, 1):  # the last single bytes leave no room for a short line (POSIX writes up to PIPE_BUF whole)
        try:
            while True:
                filled += os.write(write_fd, b'x' * size)
        except BlockingIOError:
            pass
    os.set_blocking(write_fd, True)
    return read_fd, write_fd, filled


def pipe_stream(write_fd):
    """A text stream built like `sys.stdout` on a pipe (text over buffered over raw file), on the full pipe."""
    return io.TextIOWrapper(io.BufferedWriter(io.FileIO(write_fd, 'w', closefd=False)), encoding='utf-8')


def drain_pipe(read_fd, filled, until, timeout=10.0):
    """Read the pipe until `until(lines)` holds for the JSON lines written after the filler; return those lines."""
    os.set_blocking(read_fd, False)
    data, deadline = b'', time.monotonic() + timeout
    while True:
        try:
            data += os.read(read_fd, 65536)
        except BlockingIOError:
            time.sleep(0.01)
        lines = [json.loads(line) for line in data[filled:].decode('utf-8').splitlines() if line.strip()]
        if until(lines) or time.monotonic() > deadline:
            return lines


def close_pipe(read_fd, write_fd, stream):
    os.close(read_fd)  # a writer still blocked on it gets an error, counts its line and ends
    try:
        stream.close()
    except OSError:  # its buffer is empty; a broken pipe on flush is not the test's concern
        pass
    os.close(write_fd)


def test_a_refusal_logged_into_a_full_pipe_does_not_hold_the_accept_loop_or_the_stop(project_root, model_test_profile,
                                                                                      monkeypatch):
    # The retest-3 probe made protective: a real full OS pipe as stdout, no double for the accept loop or the stop.
    settings, _ = make_settings(project_root, model_test_profile, max_connections=1, max_pending_requests=1,
                                read_timeout_s=0.5)
    read_fd, write_fd, filled = full_pipe()
    stream = pipe_stream(write_fd)
    monkeypatch.setattr(sys, 'stdout', stream)
    readers = hold_deadlines(monkeypatch)  # the holder keeps its slot whatever the machine load does to the clock
    held = None
    try:
        with running_server(settings) as (_, engine, stop, result):
            readers.clear()
            (held,) = hold(settings, readers)
            with connect(settings) as extra:  # over the cap: refused and logged on the accept thread
                assert b' 503 ' in read_to_end(extra).split(b'\r\n')[0]  # the log line did not hold the accept thread
            started = time.monotonic()
            stop.set()
            assert wait_for(lambda: result, timeout=stop_bound(settings)), 'the stop waited for the console'
            assert time.monotonic() - started < stop_bound(settings)
            assert result == {'reason': 'stop'} and engine.closed and port_is_free(settings)
            assert handler_threads() == []
        lines = drain_pipe(read_fd, filled, lambda lines: lines)  # the line is delivered once the reader reads
        assert {'event': 'connection_refused', 'type': 'overloaded'} in lines
        assert wait_for(lambda: not output_threads())  # the writer ends once its queue is written
    finally:
        if held is not None:
            held.close()
        close_pipe(read_fd, write_fd, stream)


def test_a_stop_summary_that_cannot_be_written_does_not_hold_the_stop(project_root, model_test_profile, monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, max_connections=1, max_pending_requests=1,
                                read_timeout_s=0.5)
    read_fd, write_fd, filled = full_pipe()
    stream = pipe_stream(write_fd)
    monkeypatch.setattr(sys, 'stdout', stream)
    readers = hold_deadlines(monkeypatch)
    held = None
    try:
        with running_server(settings) as (_, engine, stop, result):
            readers.clear()
            (held,) = hold(settings, readers)
            for _ in range(3):  # three refusals of one kind: one line now, the count in the stop summary
                with connect(settings) as extra:
                    read_to_end(extra)
            started = time.monotonic()
            stop.set()
            assert wait_for(lambda: result, timeout=stop_bound(settings)), 'the stop waited for its summary line'
            assert time.monotonic() - started < stop_bound(settings)
            assert result == {'reason': 'stop'} and engine.closed
        lines = drain_pipe(read_fd, filled, lambda lines: any(line['event'] == 'log_summary' for line in lines))
        summary = [line for line in lines if line['event'] == 'log_summary']
        assert summary == [{'event': 'log_summary', 'counts': {'connection_refused:overloaded': 3}}]
        assert wait_for(lambda: not output_threads())
    finally:
        if held is not None:
            held.close()
        close_pipe(read_fd, write_fd, stream)


def test_lines_beyond_the_output_queue_are_dropped_counted_and_reported_in_the_summary():
    read_fd, write_fd, filled = full_pipe()
    stream = pipe_stream(write_fd)
    try:
        output = server_module.BoundedOutput(stream)
        log = server_module._Log(output)
        kinds = server_module.OUTPUT_QUEUE_LINES + 10  # one first line per kind, more than the queue holds
        started = time.monotonic()
        for index in range(kinds):
            log.note('connection_error', f'Kind{index}')
        log.summary()
        assert time.monotonic() - started < 1.0  # no call waited for the console
        assert output.close(0.1) is False  # nothing could be written yet
        lines = drain_pipe(read_fd, filled, lambda lines: any(line['event'] == 'log_summary' for line in lines))
        events = [line for line in lines if line['event'] == 'connection_error']
        summary = [line for line in lines if line['event'] == 'log_summary']
        assert len(summary) == 1 and summary[0]['counts'] == {}
        dropped = summary[0]['dropped_lines']
        assert dropped >= kinds - server_module.OUTPUT_QUEUE_LINES - 1  # the queue plus the line being written
        assert len(events) + dropped == kinds  # every line is either written or counted
        assert wait_for(lambda: not output_threads())
    finally:
        close_pipe(read_fd, write_fd, stream)


def test_a_stuck_accept_loop_is_reported_within_the_window_and_the_listener_still_closes(project_root,
                                                                                        model_test_profile,
                                                                                        monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=0.25)
    entered, release, armed = threading.Event(), threading.Event(), threading.Event()
    original = server_module._Server.process_request

    def stuck(self, request, client_address):  # labelled double: the accept thread blocks inside its own work
        if armed.is_set():
            entered.set()
            assert release.wait(HELD_S), 'the test never released the accept loop'
        return original(self, request, client_address)

    monkeypatch.setattr(server_module._Server, 'process_request', stuck)
    client = None
    try:
        with running_server(settings) as (_, engine, stop, result):
            armed.set()
            client = connect(settings)
            assert entered.wait(5)
            started = time.monotonic()
            stop.set()
            assert wait_for(lambda: result, timeout=stop_bound(settings)), 'the stop waited for the accept loop'
            assert time.monotonic() - started < stop_bound(settings)
            error = result.get('error')
            assert isinstance(error, ShutdownIncomplete) and 'the accept thread is still running' in str(error)
            assert port_is_free(settings) and engine.closed
            release.set()
    finally:
        release.set()
        if client is not None:
            client.close()
    assert wait_for(lambda: not [thread for thread in threading.enumerate() if thread.name == 'embed-http'])


def test_an_engine_close_that_does_not_finish_is_reported_within_the_window(project_root, model_test_profile):
    release = threading.Event()

    class Slow(EngineDouble):  # labelled double: a close that hangs (the collector or the driver)
        def close(self):
            assert release.wait(HELD_S), 'the test never released the close'
            super().close()

    engine = Slow()
    settings, state, server, thread = stop_directly(project_root, model_test_profile, engine)
    try:
        started = time.monotonic()
        error = finish(state, server, thread)
        assert time.monotonic() - started < stop_bound(settings)
        assert isinstance(error, ShutdownIncomplete)
        assert f'the engine close did not finish within {settings.read_timeout_s} s' in str(error)
        assert not state.gpu.acquire(timeout=0)  # the closing thread still holds the lock: no forward can start
    finally:
        release.set()
    assert wait_for(lambda: engine.closed and state.engine is None)
    assert state.gpu.acquire(timeout=5)  # given back once the close ended
    state.gpu.release()


def test_an_accept_with_nothing_pending_gives_up_within_one_slice(project_root, model_test_profile):
    # `select` can report a connection that is gone before `accept` takes it; this is the accept that follows.
    settings, _ = make_settings(project_root, model_test_profile)
    state = server_module._State(settings, time.monotonic)
    server = server_module._Server((settings.host, settings.port), server_module._handler(state), state)
    try:
        accept = threading.Thread(target=server._handle_request_noblock, daemon=True)
        accept.start()
        accept.join(2)
        assert not accept.is_alive(), 'the accept waited for a client that never came'
    finally:
        server.server_close()
    accept.join(5)


EXIT_HARNESS = r'''
import json, pickle, socket, sys, threading, time
from prompt_enhancer.embed_server import __main__ as entry
from embed_server_fixtures import EngineDouble  # labelled engine double

settings_path, record_path, mode = sys.argv[1:4]
with open(settings_path, 'rb') as handle:
    settings = pickle.load(handle)


class Stubborn(EngineDouble):  # labelled double: a close that fails leaves the stop incomplete
    def close(self):
        raise RuntimeError('labelled close failure')


engine = Stubborn() if mode == 'incomplete' else EngineDouble()
real_serve, times = entry.serve, {}


def serve_and_stop(settings_, load, *, stop):
    def client():
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    held = socket.create_connection((settings_.host, settings_.port), timeout=5)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
            time.sleep(0.1)  # the held connection has its handler; the next one is over the cap
            with socket.create_connection((settings_.host, settings_.port), timeout=5) as extra:
                extra.recv(4096)  # its 503: the refusal was logged on the accept thread into the full stdout
        finally:
            times['stop'] = time.monotonic()
            stop.set()

    threading.Thread(target=client, daemon=True).start()
    return real_serve(settings_, load, stop=stop)  # the default announce: the ready line goes to the full stdout


entry.load_server_settings = lambda *args: settings
entry._loader = lambda _: engine
entry.serve = serve_and_stop
status = entry.main(['serve', '--config', 'unused.yaml'])
with open(record_path, 'w', encoding='utf-8') as handle:
    json.dump({'status': status, 'stop_to_return': time.monotonic() - times['stop']}, handle)
sys.exit(status)
'''


@pytest.mark.parametrize('mode, status', [('complete', 0), ('incomplete', 3)])
def test_the_command_exits_with_its_status_while_stdout_and_stderr_are_full_pipes(project_root, model_test_profile,
                                                                                  tmp_path, mode, status):
    from pathlib import Path
    from test_embed_server_cli import NO_WINDOW

    settings, _ = make_settings(project_root, model_test_profile, max_connections=1, max_pending_requests=1,
                                read_timeout_s=0.5)
    settings_path, record_path = tmp_path / 'settings.pickle', tmp_path / 'record.json'
    settings_path.write_bytes(pickle.dumps(settings))
    pipes = [full_pipe(), full_pipe()]  # stdout, stderr: nobody reads either while the command runs
    source = Path(server_module.__file__).resolve().parents[2]  # the package this test imported, for the child too
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(source), str(Path(__file__).resolve().parent)]),
               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    try:
        child = subprocess.Popen([sys.executable, '-X', 'faulthandler', '-c', EXIT_HARNESS, str(settings_path),
                                  str(record_path), mode], stdin=subprocess.DEVNULL, stdout=pipes[0][1],
                                 stderr=pipes[1][1], env=env, cwd=tmp_path, creationflags=NO_WINDOW)
        try:
            code = child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(10)
            pytest.fail('the command did not exit while its output pipes were full')
        assert code == status
        record = json.loads(record_path.read_text(encoding='utf-8'))
        assert record['status'] == status
        # the stop bound plus the window of the command's last line
        assert record['stop_to_return'] < stop_bound(settings) + settings.read_timeout_s
    finally:
        for read_fd, write_fd, _ in pipes:
            os.close(read_fd)
            os.close(write_fd)


# --- round 5 (RT4F1, RT4F2): nothing the stop or the command calls raises past its bookkeeping or its status ----------

CLOSE_THREAD = server_module.CLOSE_THREAD


def refuse_thread_start(monkeypatch, name, before=None):
    """Labelled double of the OS refusing one thread (`RuntimeError`, what `threading.Thread.start` raises when
    `_start_new_thread` fails); every other thread starts for real. `before` runs first, inside the failing start."""
    real_start = threading.Thread.start

    def start(self):
        if self.name == name:
            if before is not None:
                before()
            raise RuntimeError('labelled OS thread-start refusal')
        return real_start(self)

    monkeypatch.setattr(threading.Thread, 'start', start)


def test_an_engine_closer_that_cannot_start_is_reported_and_the_stop_still_completes(project_root, model_test_profile,
                                                                                     monkeypatch):
    # The retest-4 probe made protective: a real server and accept thread; only the closer's start is refused.
    settings, state, server, thread = stop_directly(project_root, model_test_profile)
    engine = state.engine
    refuse_thread_start(monkeypatch, CLOSE_THREAD)
    try:
        started = time.monotonic()
        error = finish(state, server, thread)
        assert time.monotonic() - started < stop_bound(settings)
        assert isinstance(error, ShutdownIncomplete)
        assert 'the engine was not closed: its closing thread could not start (RuntimeError)' in str(error)
        assert 'labelled' not in str(error)  # the type only
        assert state.engine is engine and not engine.closed  # no synchronous close fallback
        assert state.output._closed and not thread.is_alive() and server.socket.fileno() == -1  # later steps ran
        assert state.gpu.acquire(timeout=0)  # the lock was given back
        state.gpu.release()
    finally:
        engine.close()


def test_an_output_writer_that_cannot_start_counts_every_line_and_never_raises(monkeypatch):
    stream = io.StringIO()
    output = server_module.BoundedOutput(stream)
    # another caller queues a line while the first emit is inside the start that fails
    refuse_thread_start(monkeypatch, OUTPUT_THREAD, before=lambda: output.emit('queued while starting'))
    assert output.emit('first') is False
    assert output.dropped == 2  # the first line and the one queued meanwhile
    assert output.emit('later') is False and output.dropped == 3  # the output failed for good
    started = time.monotonic()
    assert output.close(0.5) is True and time.monotonic() - started < 0.4  # nothing is left waiting
    assert stream.getvalue() == '' and not output._writer.is_alive()


def test_the_last_line_keeps_its_status_when_its_writer_cannot_start(monkeypatch):
    from prompt_enhancer.embed_server import __main__ as entry

    refuse_thread_start(monkeypatch, OUTPUT_THREAD)
    started = time.monotonic()
    assert entry._last_line('labelled incomplete stop', SimpleNamespace(read_timeout_s=0.2), entry.INCOMPLETE) == 3
    assert time.monotonic() - started < 0.2 + SLACK_S


class LabelledUnexpected(Exception):
    """Labelled double of an error nobody planned for (a defect, a library error of another type)."""


@pytest.mark.parametrize('error', [LabelledUnexpected('secret detail of the error'), KeyboardInterrupt()])
def test_an_unexpected_error_once_the_profile_is_read_is_one_type_line_and_status_1(monkeypatch, capsys, error):
    from prompt_enhancer.embed_server import __main__ as entry

    def broken(*args, **kwargs):
        raise error

    # labelled double: the command reads only the window that bounds its last line
    monkeypatch.setattr(entry, 'load_server_settings', lambda *args: SimpleNamespace(read_timeout_s=1))
    monkeypatch.setattr(entry, 'serve', broken)
    monkeypatch.setattr(entry.signal, 'signal', lambda *args: None)  # leave the test process's own handlers alone
    assert entry.main(['serve', '--config', 'unused.yaml']) == entry.FAILED == 1
    captured = capsys.readouterr()
    assert captured.err == f'failed: {type(error).__name__}\n' and captured.out == ''  # no trace, no message


@pytest.mark.parametrize('case', ['argparse', 'configuration'])
def test_nothing_is_started_or_left_open_before_the_profile_is_read(project_root, model_test_profile, monkeypatch,
                                                                    capsys, case):
    # The one accepted direct write (no settings, no window) is acceptable only because nothing is owned yet.
    import yaml

    from embed_server_fixtures import free_port, server_profile
    from prompt_enhancer.embed_server import __main__ as entry

    started, sockets = [], []
    real_start, real_init = threading.Thread.start, socket.socket.__init__

    def start(self):  # recording wrapper, the start itself is real
        started.append(self.name)
        return real_start(self)

    def init(self, *args, **kwargs):  # recording wrapper, the socket itself is real
        real_init(self, *args, **kwargs)
        sockets.append(self)

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(socket.socket, '__init__', init)
    if case == 'argparse':
        with pytest.raises(SystemExit) as exited:
            entry.main(['serve'])  # --config missing: argparse prints its usage and exits
        assert exited.value.code == 2 and 'usage' in capsys.readouterr().err
    else:
        profile = server_profile(model_test_profile, port=free_port())
        profile['vlm']['backends']['embedder']['server']['dtype'] = 'float16'  # refused for this family
        path = project_root / 'refused-profile.yaml'
        path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
        assert entry.main(['serve', '--config', str(path), '--project-root', str(project_root)]) == entry.REFUSED
        assert capsys.readouterr().err.startswith('refused: ')
    assert started == []
    assert [sock for sock in sockets if sock.fileno() != -1] == []


def test_an_accept_thread_that_cannot_start_leaves_no_listener(project_root, model_test_profile, monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile)
    refuse_thread_start(monkeypatch, 'embed-http')
    servers, real_init = [], server_module._Server.__init__

    def init(self, *args, **kwargs):  # recording wrapper, the server itself is real
        real_init(self, *args, **kwargs)
        servers.append(self)

    monkeypatch.setattr(server_module._Server, '__init__', init)
    loaded = []
    try:
        with pytest.raises(RuntimeError, match='labelled'):
            server_module.serve(settings, lambda _: loaded.append(True) or EngineDouble(), stop=threading.Event())
        assert loaded == [] and len(servers) == 1
        assert servers[0].socket.fileno() == -1  # closed by serve, not left to the garbage collector
    finally:
        for server in servers:
            server.socket.close()


def test_a_listener_close_that_fails_is_reported_and_the_stop_still_runs(project_root, model_test_profile,
                                                                         monkeypatch):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    engine = state.engine

    def failing(self):  # labelled double: the platform refuses the close
        raise OSError('labelled close failure')

    monkeypatch.setattr(server_module._Server, 'server_close', failing)
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and 'the listening socket is still open' in str(error)
    assert engine.closed and state.engine is None and state.output._closed  # every later step ran


def test_a_handler_registered_but_not_started_yet_is_reported_not_raised(project_root, model_test_profile):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    leftover = socket.socket()
    # labelled state: the accept thread registered this handler and is still inside its start
    server._connections[leftover] = threading.Thread(target=lambda: None, name=CONNECTION_THREAD)
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and '1 accepted socket(s) still registered' in str(error)
    assert leftover.fileno() == -1 and state.engine is None  # the stop went on: socket closed, engine closed


def test_a_socket_whose_close_fails_is_reported_and_does_not_end_the_stop(project_root, model_test_profile):
    class Unclosable:  # labelled socket double: the platform refuses both its shutdown and its close
        def __init__(self):
            self.calls = []

        def shutdown(self, how):
            self.calls.append('shutdown')
            raise OSError('labelled shutdown failure')

        def close(self):
            self.calls.append('close')
            raise OSError('labelled close failure')

        def fileno(self):  # still open after the refused close
            return 1234

    _, state, server, thread = stop_directly(project_root, model_test_profile)
    gone = threading.Thread(target=lambda: None)
    gone.start()
    gone.join()
    sock = Unclosable()
    server._connections[sock] = gone  # its handler ended without forgetting it
    error = finish(state, server, thread)
    assert isinstance(error, ShutdownIncomplete) and '1 accepted socket(s) still registered' in str(error)
    assert '1 accepted socket(s) could not be closed' in str(error)
    assert sock.calls.count('close') == 1 and state.engine is None  # tried once, and the stop went on


class CloseRefused:
    """Labelled socket double over a real socket: its close is refused (RT5F1). With `releases` the refusal comes
    after the handle was released, as a native close may do; without it the real socket stays open."""

    def __init__(self, real, releases=False):
        self.real, self.releases, self.closes = real, releases, 0

    def fileno(self):
        return self.real.fileno()

    def setblocking(self, flag):
        self.real.setblocking(flag)

    def recv(self, size):
        return self.real.recv(size)

    def send(self, data):
        return self.real.send(data)

    def shutdown(self, how):
        self.real.shutdown(how)

    def close(self):
        self.closes += 1
        if self.releases:
            self.real.close()
        raise OSError('labelled close failure')


def _close_through(server, monkeypatch, path, sock):
    """Run the real cleanup of one path on `sock`: a finished handler, or a turned-away connection."""
    if path == 'handler':
        # labelled double: the request was already served; the real `_serve_connection` cleanup runs
        monkeypatch.setattr(server_module._Server, 'finish_request', lambda self, request, address: None)
        with server._lock:
            server._connections[sock] = threading.current_thread()
        server._serve_connection(sock, ('127.0.0.1', 0))
    else:
        server._turn_away(sock)


@pytest.mark.parametrize('path', ['handler', 'refusal'])
def test_a_close_failure_that_leaves_the_socket_open_stays_a_survivor(project_root, model_test_profile, monkeypatch,
                                                                     path):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    near, far = socket.socketpair()
    sock = CloseRefused(near)
    try:
        _close_through(server, monkeypatch, path, sock)
        assert server.registered() == 0  # the handler registry let it go, the stop must still see it
        error = finish(state, server, thread)
        assert isinstance(error, ShutdownIncomplete) and '1 accepted socket(s) could not be closed' in str(error)
        assert near.fileno() != -1 and sock.closes == 2  # retried once at the stop, still open
        assert state.engine is None and state.output._closed  # the rest of the stop ran
    finally:
        near.close()
        far.close()


@pytest.mark.parametrize('path', ['handler', 'refusal'])
def test_a_close_failure_that_released_the_socket_is_not_a_survivor(project_root, model_test_profile, monkeypatch,
                                                                   path):
    _, state, server, thread = stop_directly(project_root, model_test_profile)
    near, far = socket.socketpair()
    sock = CloseRefused(near, releases=True)
    try:
        _close_through(server, monkeypatch, path, sock)
        assert finish(state, server, thread) is None
        assert near.fileno() == -1 and sock.closes == 1  # nothing left to retry
    finally:
        near.close()
        far.close()


FAILURE_HARNESS = r'''
import json, pickle, sys, threading, time
from prompt_enhancer.embed_server import __main__ as entry
from prompt_enhancer.embed_server import server as srv
from embed_server_fixtures import EngineDouble  # labelled engine double

settings_path, record_path, mode = sys.argv[1:4]
with open(settings_path, 'rb') as handle:
    settings = pickle.load(handle)
times, real_start, real_serve = {}, threading.Thread.start, srv.serve


class LabelledUnexpected(Exception):  # labelled double of an unplanned error
    pass


def start(self):  # labelled double: the OS refuses the closer (closer mode) or the stderr writer (writer mode)
    if (mode, self.name) in (('closer', srv.CLOSE_THREAD), ('writer', srv.OUTPUT_THREAD)):
        times['failure'] = time.monotonic()
        raise RuntimeError('labelled OS thread-start refusal')
    return real_start(self)


def serve(settings_, load, *, stop):
    if mode == 'closer':  # the real server: the stop runs at once and meets the refused closer
        stop.set()
        return real_serve(settings_, load, stop=stop)
    times['failure'] = time.monotonic()
    if mode == 'writer':  # labelled trigger of the command's incomplete-stop line
        raise srv.ShutdownIncomplete('labelled incomplete stop')
    raise LabelledUnexpected('labelled detail')


threading.Thread.start = start
entry.load_server_settings = lambda *args: settings
entry._loader = lambda _: EngineDouble()
entry.serve = serve
status = entry.main(['serve', '--config', 'unused.yaml'])
with open(record_path, 'w', encoding='utf-8') as handle:
    json.dump({'status': status, 'failure_to_return': time.monotonic() - times['failure']}, handle)
sys.exit(status)
'''


@pytest.mark.parametrize('mode, status', [('closer', 3), ('writer', 3), ('unexpected', 1)])
def test_a_failure_path_keeps_its_status_and_exits_while_stdout_and_stderr_are_full_pipes(project_root,
                                                                                         model_test_profile, tmp_path,
                                                                                         mode, status):
    # The retest-4 CLI probes made protective, plus the unexpected-error path: real subprocess, both pipes full.
    from pathlib import Path
    from test_embed_server_cli import NO_WINDOW

    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=0.5)
    settings_path, record_path = tmp_path / 'settings.pickle', tmp_path / 'record.json'
    settings_path.write_bytes(pickle.dumps(settings))
    pipes = [full_pipe(), full_pipe()]  # stdout, stderr: nobody reads either while the command runs
    source = Path(server_module.__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(source), str(Path(__file__).resolve().parent)]),
               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    child = None
    try:
        child = subprocess.Popen([sys.executable, '-X', 'faulthandler', '-c', FAILURE_HARNESS, str(settings_path),
                                  str(record_path), mode], stdin=subprocess.DEVNULL, stdout=pipes[0][1],
                                 stderr=pipes[1][1], env=env, cwd=tmp_path, creationflags=NO_WINDOW)
        try:
            code = child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail('the command did not exit while its output pipes were full')
        assert code == status
        record = json.loads(record_path.read_text(encoding='utf-8'))
        assert record['status'] == status
        assert record['failure_to_return'] < stop_bound(settings) + settings.read_timeout_s
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(10)
        for read_fd, write_fd, _ in pipes:
            os.close(read_fd)
            os.close(write_fd)
