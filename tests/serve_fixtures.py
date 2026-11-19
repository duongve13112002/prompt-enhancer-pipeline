"""Shared setup of the `enhance serve` tests (T-016 P3): the real `python -m prompt_enhancer enhance serve` in a hidden
child process, a loopback model double that can be held and released explicitly, and small raw HTTP clients.

Labelled doubles: the loopback model server (`FullServerDouble`, held by `Gate`) and the profile registration/golden
runner of `activate`. Capacity is never inferred from sleeps: a test waits on `Gate.wait_for(n)` until n requests are
inside the double, and releases them with `Gate.release()`. Evidence class: `mock_contract`."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Condition, Event, Thread
import time

from cli_fixtures import NO_WINDOW, REQUEST_CAP, ROOT, pipeline_config, write_yaml
from test_pipeline_s0_s6 import FullServerDouble

READY = 'listening on '  # the stderr line `enhance serve` prints once the listener is bound
WAIT_S = 60  # test-only bound on every wait for a child process or a held request
CONSOLE_CTRL = Path(__file__).with_name('console_ctrl.py')


def free_port():
    """A loopback port nobody holds right now (test-only: a deployment names its port in the profile)."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def serve_config(model_test_profile, tmp_path, project_root, model_port, listen_port, *, retrieval=False, **dotted):
    """A complete serving configuration for the loopback double; every value is test-only. `dotted` overrides
    `serving.*` and `runtime.*` keys, for example `**{'serving.http.read_timeout_s': 1}` through a dict."""
    config = pipeline_config(model_test_profile, tmp_path, project_root, model_port, retrieval=retrieval)
    return add_serving(config, listen_port, **dotted)


def add_serving(config, listen_port, **dotted):
    """The `serving` leaves `enhance serve` reads, on a configuration built by `pipeline_config` or `attaching_config`."""
    config['serving'].update(host='127.0.0.1', port=listen_port, max_concurrency=2, queue_limit=1, total_deadline_s=30)
    # test-only: a request held in the model double must outlast the pipeline's own timers, so that it is the test, not a
    # model timeout, that ends the hold (the shared test profile reads for 1 s)
    config['vlm']['backends']['main']['http']['read_timeout_s'] = 30
    config['runtime']['request_deadline_s'] = 20
    config['serving']['http'] = {'max_request_bytes': REQUEST_CAP, 'max_connections': 4, 'read_timeout_s': 5,
                                 'write_timeout_s': 5, 'shutdown_timeout_s': 5}
    for key, value in dotted.items():
        parts = key.split('.')
        node = config
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return config


class Gate:
    """Holds every planner request of the model double until `release`; counts requests inside the double."""

    def __init__(self):
        self._cond, self.inflight, self.peak, self.entered = Condition(), 0, 0, 0
        self._open = Event()

    def hold(self):
        with self._cond:
            self.inflight += 1
            self.entered += 1
            self.peak = max(self.peak, self.inflight)
            self._cond.notify_all()
        try:
            assert self._open.wait(WAIT_S), 'a held request was never released'
        finally:
            with self._cond:
                self.inflight -= 1
                self._cond.notify_all()

    def wait_for(self, count):
        """Block until `count` requests are inside the double at once; the test fails after WAIT_S."""
        with self._cond:
            assert self._cond.wait_for(lambda: self.inflight >= count, WAIT_S), (
                f'{self.inflight} of {count} requests reached the model double')

    def wait_idle(self):
        with self._cond:
            assert self._cond.wait_for(lambda: self.inflight == 0, WAIT_S)

    def release(self):
        self._open.set()


def held_double(gate, base=FullServerDouble):
    """A labelled model double whose planner answer waits for `gate`."""
    parent = base.planner_answer

    def held(data):
        gate.hold()
        return parent(data)
    return type('HeldDouble', (base,), {'planner_answer': staticmethod(held)})


def exchange(port, payload, *, timeout=WAIT_S, shutdown_write=False):
    """Send raw bytes, read until the server closes; returns (status, headers dict, body bytes) or (None, {}, b'')."""
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as sock:
        sock.sendall(payload)
        if shutdown_write:
            sock.shutdown(socket.SHUT_WR)
        data = b''
        try:
            while chunk := sock.recv(65536):
                data += chunk
        except (ConnectionError, socket.timeout):
            pass
    return parse_response(data)


def parse_response(data):
    if not data:
        return None, {}, b''
    head, _, body = data.partition(b'\r\n\r\n')
    lines = head.decode('ascii').split('\r\n')
    headers = {name.lower(): value.strip() for name, _, value in (line.partition(':') for line in lines[1:])}
    return int(lines[0].split(' ')[1]), headers, body


def raw_post(path, body, *, content_type='application/json', extra=''):
    head = f'POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n{extra}\r\n'
    return head.encode('ascii') + body


def post_json(port, request, *, path='/v1/enhance', timeout=WAIT_S):
    body = json.dumps(request, ensure_ascii=False).encode('utf-8')
    status, headers, data = exchange(port, raw_post(path, body), timeout=timeout)
    return status, headers, data


def get(port, path, *, timeout=WAIT_S):
    return exchange(port, f'GET {path} HTTP/1.1\r\nHost: x\r\n\r\n'.encode('ascii'), timeout=timeout)


def in_threads(count):
    return ThreadPoolExecutor(max_workers=count)


class ServeProcess:
    """`python -m prompt_enhancer enhance serve` in a hidden child with faulthandler; stderr is read continuously."""

    def __init__(self, project_root, config_path, *, extra=()):
        environ = dict(os.environ, PYTHONPATH=os.environ.get('PROMPT_ENHANCER_TEST_SRC', str(ROOT / 'src')),
                       PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
        if os.name == 'nt':
            # A harness may start this process with Ctrl+C ignored, and a child inherits that. Clearing it here is the
            # normal state of a console process and lets the child's own handlers see the event.
            ctypes.WinDLL('kernel32').SetConsoleCtrlHandler(None, False)
        self.proc = subprocess.Popen(
            [sys.executable, '-X', 'faulthandler', '-m', 'prompt_enhancer', 'enhance', 'serve', '--config',
             str(config_path), '--project-root', str(project_root), *map(str, extra)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ, creationflags=NO_WINDOW, cwd=project_root)
        self.lines, self.stdout, self.forced = [], b'', False
        self._ready = Event()
        self._threads = [Thread(target=self._read_err, daemon=True), Thread(target=self._read_out, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _read_err(self):
        for raw in self.proc.stderr:
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            self.lines.append(line)
            if line.startswith(READY):
                self._ready.set()

    def _read_out(self):
        self.stdout = self.proc.stdout.read()

    def wait_ready(self, timeout=WAIT_S):
        """The port the listener is bound to, once the ready line is printed; fails when the child exits first."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready.wait(0.05):
                line = next(item for item in self.lines if item.startswith(READY))
                return int(line.rsplit(':', 1)[1])
            if self.proc.poll() is not None:
                break
        raise AssertionError(f'the server did not become ready: exit {self.proc.poll()}, stderr {self.lines}')

    def interrupt(self, kind='default'):
        """The cooperative stop. `default` is SIGTERM on POSIX and Ctrl+C on Windows; `other` is SIGINT on POSIX and
        Ctrl+Break (SIGBREAK in Python) on Windows. Windows: a console-less test process cannot send a console event
        itself, and TerminateProcess is not a cooperative stop, so `console_ctrl.py` attaches to the server's own hidden
        console (CREATE_NO_WINDOW gives every child one) and generates the event there."""
        if os.name == 'nt':
            done = subprocess.run([sys.executable, '-B', str(CONSOLE_CTRL), str(self.proc.pid),
                                   'break' if kind == 'other' else 'c'], timeout=WAIT_S, creationflags=NO_WINDOW,
                                  capture_output=True)
            assert done.returncode == 0, done.stderr
        else:
            self.proc.send_signal(signal.SIGINT if kind == 'other' else signal.SIGTERM)

    def stop(self, timeout=WAIT_S, kind='default'):
        """Cooperative stop, then the exit status; the child must leave on its own."""
        self.interrupt(kind)
        return self.wait(timeout)

    def wait(self, timeout=WAIT_S):
        code = self.proc.wait(timeout)
        for thread in self._threads:
            thread.join(timeout=10)
        return code

    @property
    def stderr(self):
        return '\n'.join(self.lines)

    def kill(self):
        """Teardown fallback only: a forced kill is never counted as a clean stop (`forced` records that it ran)."""
        if self.proc.poll() is None:
            self.forced = True
            self.proc.kill()
            self.proc.wait(timeout=30)
        for thread in self._threads:
            thread.join(timeout=10)


@contextmanager
def serving(project_root, config_path, *, extra=()):
    """Start the server, yield (process, port); on exit stop it cooperatively when it still runs."""
    process = ServeProcess(project_root, config_path, extra=extra)
    try:
        yield process, process.wait_ready()
    finally:
        try:
            if process.proc.poll() is None:
                process.stop()
        finally:
            process.kill()
    assert not process.forced, 'the server did not stop on its own'


def listener_is_closed(port):
    with socket.socket() as probe:
        probe.settimeout(5)
        return probe.connect_ex(('127.0.0.1', port)) != 0


def write_config(project_root, tmp_path, config, name='serve.yaml'):
    return write_yaml(project_root, tmp_path, config, name)


# --- in-process serving: a labelled stub pipeline and asyncio clients (white-box tests of admission and stop) ------------

import asyncio
import io

from prompt_enhancer.cli_serve import Notes, Server
from prompt_enhancer.config import validate_serving
from prompt_enhancer.storage import open_data_store


class StubLifecycle:
    """Labelled double of `ResponseLifecycle.io`: runs the function on a worker thread of the loop's default executor."""
    def __init__(self):
        self.errors = []

    async def io(self, function, *args):
        return await asyncio.to_thread(function, *args)


class StubResponse:
    """Labelled double of `PipelineResponse`: an envelope, attachment bytes and an acknowledgement spy."""
    def __init__(self, envelope, attachment_bytes=None, log=None):
        self.envelope, self.attachment_bytes, self.acknowledged, self._log = envelope, attachment_bytes or {}, 0, log
        self.abandoned = 0

    async def after_delivery(self):
        self.acknowledged += 1
        if self._log is not None:
            self._log.append('acknowledged')

    async def abandon(self):
        """Like the real ticket: a no-op once acknowledged, counted once otherwise."""
        if not self.acknowledged and not self.abandoned:
            self.abandoned += 1
            if self._log is not None:
                self._log.append('abandoned')


def stub_envelope(output='ok', status='success', trace_id='r-stub'):
    return {'status': status, 'output': output, 'attachments': [], 'source_binding': None,
            'retrieval': {'state': 'disabled', 'snapshot_id': None}, 'warnings': [], 'trace_id': trace_id}


class StubPipeline:
    """Labelled double of `Pipeline` for the HTTP layer: `behaviour(request)` is an async function returning a
    `StubResponse` (or raising); `close_gate`, when given, holds `close()` until it is set."""
    def __init__(self, behaviour, *, close_gate=None):
        self.behaviour, self.close_gate = behaviour, close_gate
        self.lifecycle, self.requests, self.cancelled, self.closed = StubLifecycle(), [], 0, False

    async def enhance(self, request):
        self.requests.append(request)
        try:
            return await self.behaviour(request)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def close(self):
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True


def serving_settings(config, **changes):
    """`validate_serving` of a test configuration with any value replaced (test-only: a stub needs no profile)."""
    return {**validate_serving(config, project_root=ROOT), **changes}


class Live:
    """One in-process server over a stub or real pipeline, stopped by `stop()`; `report` holds the stop report."""
    def __init__(self, settings, pipeline, store, root, retention_s=60):
        self.notes = Notes(io.StringIO())
        self.server = Server(settings, pipeline, store, root, retention_s, self.notes)
        self.pipeline, self.report, self.port = pipeline, None, None

    async def __aenter__(self):
        self.port = (await self.server.start())[1]
        return self

    async def stop(self):
        if self.report is None:
            self.report = await self.server.stop()
        return self.report

    async def __aexit__(self, *exc):
        await self.stop()


async def until(condition, *, timeout=WAIT_S):
    """Wait, yielding to the loop, until `condition()` holds (an observable state, never a guess at timing)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, 'the awaited state never came'
        await asyncio.sleep(0.005)


async def aexchange(port, payload, *, timeout=WAIT_S):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(payload)
        data = b''
        async with asyncio.timeout(timeout):
            while chunk := await reader.read(65536):
                data += chunk
    except (ConnectionError, TimeoutError):
        pass
    finally:
        writer.close()
    return parse_response(data)


async def aopen(port):
    return await asyncio.open_connection('127.0.0.1', port)


GOOD_REQUEST = {'prompt': 'a prompt', 'task': 'generate'}


def good_post(request=None):
    return raw_post('/v1/enhance', json.dumps(request or GOOD_REQUEST).encode('utf-8'))


def store_for(config, project_root):
    return open_data_store(config, 'enhance serve', project_root=project_root)
