"""The embedding server: stdlib `ThreadingHTTPServer`, one GPU lock, loopback only (T-018, decision 138).

`serve` binds first so `/health` answers `loading` while the weights load, then serves until a stop is requested or
the server has been idle for `server.idle_exit_s` seconds (AGENTS.md section 8: stop when the queue drains). The engine
is anything with `embed(items) -> EmbedResult`, `native_dimension` and `close()`: the real one is
`models.TransformersEngine`, tests pass a labelled double.

What the server owns, and the bound of each (fix rounds 1 and 2, R3, R4, RT1 to RT3):

- accepted sockets and handler threads: at most `server.max_connections` at once; the check happens before a thread is
  created, and an over-cap connection gets one best-effort 503 written by the accept thread and is closed;
- request bodies: at most `server.max_pending_requests` POSTs are admitted at once (a slot is taken before the body is
  read and held until the response is sent); the rest get 503 `overloaded` without a byte of body being buffered;
- time: one deadline of `server.read_timeout_s` for reading a request and another for sending its reply;
- bytes read after a refusal: at most `server.max_discard_bytes`, whatever length the client declares;
- logs: the first line of each kind of connection or engine error is written, later ones are counted and summed up in
  one line at shutdown; no thread waits for the console (`BoundedOutput`, fix round 4, RT3F1);
- stop: see `stop_server`.
"""

import collections
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import selectors
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from .protocol import EngineError, Limits, RequestError, build_response, error_body, parse_request

IDLE_POLL_S = 0.5  # implementation poll of the stop and idle checks; not an operational value
CANCEL_POLL_S = 0.1  # implementation slice of a blocked receive, so a stop is noticed within it; not an operational value
DRAIN_BLOCK = 64 * 1024  # safety-bound: bytes discarded per read after a reply; the total is `server.max_discard_bytes`
CONNECTION_THREAD = 'embed-conn'  # name of every handler thread, so a test or an operator can count them
OUTPUT_THREAD = 'embed-output'  # name of the console writer thread
CLOSE_THREAD = 'embed-close'  # name of the thread that closes the engine at stop
OUTPUT_QUEUE_LINES = 64  # safety-bound: operator lines waiting for a slow console; more are dropped and counted


class ShutdownIncomplete(RuntimeError):
    """The stop ended with a server-owned resource still live (a thread, a socket, an admitted request, the engine);
    the message names the kinds and counts, never request content. The process exit that follows frees what is left."""


class _DeadlineReader(io.RawIOBase):
    """A socket reader with one deadline for the whole request: each receive gets only the time that is left, so a
    client that sends a byte at a time cannot hold the connection past `server.read_timeout_s`.

    It also ends when `cut` is set (the stop, RT1): a receive waits in slices of `CANCEL_POLL_S` and looks at the event
    between them, because shutting a socket down does not wake a receive that waits with a timeout on Windows."""

    def __init__(self, sock, deadline, cut):
        self._sock, self._deadline, self._cut = sock, deadline, cut

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('the read deadline passed')
            if self._cut.is_set():
                raise ConnectionAbortedError('the server is stopping')
            self._sock.settimeout(min(remaining, CANCEL_POLL_S))
            try:
                return self._sock.recv_into(buffer)
            except TimeoutError:  # one slice passed: look at the deadline and the event again
                continue


class BoundedOutput:
    """Operator output that never makes its caller wait for the console (fix round 4, RT3F1).

    `emit` queues a line (at most `OUTPUT_QUEUE_LINES`, plus one reserved slot for the stop summary) and returns; one
    daemon thread writes the queue. A line that finds the queue full or the output closed, or whose write fails, is
    counted in `dropped`. A console nobody reads therefore stalls only the writer thread, never the accept loop, a
    handler or the stop. The writer writes to the stream's raw file (`stream.buffer.raw`) when it has one, so a writer
    stuck on a full pipe holds no lock of the buffered `sys.stdout` that the interpreter exit would wait for. A stream
    without one (a pytest capture, a `StringIO`, a replaced or wrapped `sys.stdout`/`sys.stderr`) is written through
    `stream.write`: that write can block the writer thread (never the caller), and a buffered such stream can hold its
    own lock at exit. Production `sys.stdout` and `sys.stderr` on a console or a pipe take the raw path.

    Nothing here raises to the caller (fix round 5, RT4F2): when the OS refuses the writer thread, the output fails for
    good and every queued or later line is counted in `dropped`."""

    def __init__(self, stream):
        self._stream, self._capacity = stream, OUTPUT_QUEUE_LINES
        raw = getattr(getattr(stream, 'buffer', None), 'raw', None)
        self._raw = raw if isinstance(raw, io.RawIOBase) else None  # a console or pipe FileIO; anything else uses stream.write
        self._encoding = getattr(stream, 'encoding', None) or 'utf-8'
        self._cond = threading.Condition()  # held only for queue bookkeeping, never across a write
        self._lines = collections.deque()
        self._busy = False  # the writer holds a line it is writing
        self._closed = False
        self._started = False  # the first emit owns starting the writer
        self._failed = False  # the writer could not be started: nothing will ever be written
        self._writer = None
        self._dropped = 0

    @property
    def dropped(self):
        with self._cond:
            return self._dropped

    def emit(self, line, reserve=False):
        """Queue one line and return at once; False when it was dropped. `reserve` lets one line past the capacity
        (the stop summary, which carries the dropped count). Never raises."""
        with self._cond:
            if self._closed or self._failed or len(self._lines) >= self._capacity + (1 if reserve else 0):
                self._dropped += 1
                return False
            self._lines.append(line)
            self._cond.notify_all()
            start, self._started = not self._started, True
        return self._start_writer() if start else True

    def _start_writer(self):
        """Start the writer thread. When it cannot be created or started (`RuntimeError`: the OS refused a thread;
        `MemoryError`), the output is marked failed, the lines queued so far (by any caller) are counted as dropped and
        waiters are woken; False is returned, nothing is raised, so a diagnostic never replaces its caller's result."""
        try:
            self._writer = threading.Thread(target=self._run, name=OUTPUT_THREAD, daemon=True)
            self._writer.start()
            return True
        except Exception:
            with self._cond:
                self._failed = True
                self._dropped += len(self._lines)
                self._lines.clear()
                self._cond.notify_all()
            return False

    def close(self, timeout):
        """Take no more lines and wait at most `timeout` seconds for the queued ones; True when nothing is left waiting
        (every line was written or counted in `dropped`). The writer ends once the queue is empty; one stuck on the
        console stays behind as a daemon thread and holds no server resource (the process exit ends it)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
            return self._cond.wait_for(lambda: not self._lines and not self._busy, timeout)

    def _run(self):
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._lines or self._closed)
                if not self._lines:
                    return  # closed and everything written
                line = self._lines.popleft()
                self._busy = True
            written = False
            try:
                self._write(line + '\n')
                written = True
            except Exception:  # the console is gone or refuses the bytes: the line is lost and counted
                pass
            finally:
                with self._cond:
                    self._busy = False
                    self._dropped += 0 if written else 1
                    self._cond.notify_all()

    def _write(self, text):
        if self._raw is None:
            self._stream.write(text)
            self._stream.flush()
            return
        data = memoryview(text.encode(self._encoding, 'backslashreplace'))
        while data:
            count = self._raw.write(data)
            if count is None:  # a non-blocking file that is full: this thread alone waits a slice and tries again
                time.sleep(CANCEL_POLL_S)
                continue
            data = data[count:]


class _Log:
    """Bounded operator log: the first line of each (event, kind) is written, later ones only counted, and one summary
    line at shutdown names the counts and the lines the output dropped, so no client can make the log grow with its
    request rate and no caller waits for the console (`BoundedOutput`)."""

    def __init__(self, output):
        self._output = output
        self._lock = threading.Lock()
        self._counts = {}

    def note(self, event, kind):
        with self._lock:
            key = f'{event}:{kind}'
            self._counts[key] = self._counts.get(key, 0) + 1
            first = self._counts[key] == 1
        if first:
            self._output.emit(json.dumps({'event': event, 'type': kind}))

    def summary(self):
        with self._lock:
            repeated = {key: count for key, count in self._counts.items() if count > 1}
        dropped = self._output.dropped
        if repeated or dropped:
            line = {'event': 'log_summary', 'counts': repeated}
            if dropped:
                line['dropped_lines'] = dropped
            self._output.emit(json.dumps(line), reserve=True)


class _State:
    """What the handler threads share: the engine once loaded, the GPU lock, the admission slots and the activity clock."""

    def __init__(self, settings, clock):
        self.settings, self.clock = settings, clock
        self.engine = None
        self.gpu = threading.Lock()
        self.guard = threading.Condition()
        self.in_flight = 0  # admitted POSTs: from their headers to the end of their reply
        self.working = 0  # the part of them past the body read: decoding, waiting for the GPU, in the engine, replying
        self.closing = False  # set once, first thing at stop: nothing new is admitted or started
        self.cut = threading.Event()  # set at stop once admitted work had its grace: blocked reads end at once
        self.output = BoundedOutput(sys.stdout)  # the ready line, the log and the stop summary
        self.log = _Log(self.output)
        self.last_activity = clock()
        self.limits = Limits(model_id=settings.model_id, max_items=settings.batch_size,
                             max_image_bytes=settings.max_image_bytes, max_decode_pixels=settings.max_decode_pixels,
                             accepts_instruction=settings.family.accepts_instruction)

    def admit(self):
        """Take an admission slot; False when `server.max_pending_requests` requests are already admitted."""
        with self.guard:
            if self.closing or self.in_flight >= self.settings.max_pending_requests:
                return False
            self.in_flight += 1
            return True

    def release(self):
        with self.guard:
            self.in_flight -= 1
            self.last_activity = self.clock()
            self.guard.notify_all()

    def begin_closing(self):
        with self.guard:
            self.closing = True

    def work_started(self):
        with self.guard:
            self.working += 1

    def work_done(self):
        with self.guard:
            self.working -= 1
            self.guard.notify_all()

    def wait_work_done(self, timeout):
        """Wait until no request is past its body read; False when `timeout` seconds pass first. A request still
        reading its body is not waited for: the stop cuts it, since its client has not finished sending."""
        with self.guard:
            return self.guard.wait_for(lambda: self.working == 0, timeout)

    def idle_for(self):
        with self.guard:
            return 0.0 if self.in_flight else self.clock() - self.last_activity

    def counts(self):
        """(admitted requests, requests past their body read) as one consistent reading."""
        with self.guard:
            return self.in_flight, self.working


def drain(read, budget, block=DRAIN_BLOCK):
    """Read and drop at most `budget` bytes through `read(n)`, stopping at end of input; return the bytes dropped.

    Nothing is stored. The budget is the server's own (`server.max_discard_bytes`), never the client's declared length.
    """
    dropped = 0
    while dropped < budget:
        data = read(min(block, budget - dropped))
        if not data:
            break
        dropped += len(data)
    return dropped


def _refusal(status, code, message):
    """A complete HTTP/1.0 reply, for a connection that gets no handler thread."""
    body = error_body(code, message)
    head = (f'HTTP/1.0 {status.value} {status.phrase}\r\nServer: embed_server\r\nContent-Type: application/json\r\n'
            f'Content-Length: {len(body)}\r\n\r\n')
    return head.encode('ascii') + body


def _handler(state):
    settings = state.settings

    class Handler(BaseHTTPRequestHandler):
        server_version = 'embed_server'  # protocol-bound: the Server header
        protocol_version = 'HTTP/1.0'  # one request per connection: no keep-alive state to leak between requests

        def setup(self):
            super().setup()
            self._consumed = 0  # request body bytes this handler has read
            self.rfile.close()  # the stdlib reader has no deadline; replace it
            self.rfile = io.BufferedReader(_DeadlineReader(self.request, time.monotonic() + settings.read_timeout_s,
                                                           state.cut))

        def log_message(self, *args):
            pass  # one line per request on stderr is noise for a local test server

        def _send(self, status, body):
            deadline = time.monotonic() + settings.read_timeout_s  # one window for header and body: a client that
            self.send_response(status)  # never reads cannot hold the thread past it
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.connection.settimeout(settings.read_timeout_s)
            self.end_headers()
            self.connection.settimeout(max(deadline - time.monotonic(), 0.001))  # 0 would mean non-blocking
            self.wfile.write(body)

        def _fail(self, status, code, message):
            self._send(status, error_body(code, message))

        def _route(self):
            return urlsplit(self.path).path

        def do_GET(self):
            if self._route() != settings.health_path:
                return self._fail(404, 'not_found', 'unknown route')
            if state.closing:
                return self._fail(503, 'stopping', 'the server is stopping')
            loaded = state.engine is not None
            self._send(200 if loaded else 503, json.dumps(
                {'status': 'ok' if loaded else 'loading', 'loaded': loaded, 'model': settings.model_id,
                 'revision': settings.revision, 'family': settings.family.name, 'dtype': settings.dtype,
                 'device': f'cuda:{settings.gpu}'}).encode('utf-8'))

        def do_POST(self):
            if self._route() != settings.embeddings_path:
                return self._fail(404, 'not_found', 'unknown route')
            if self.headers.get('Transfer-Encoding') is not None:
                return self._fail(411, 'length_required', 'chunked bodies are not supported')
            length = self.headers.get('Content-Length', '')
            if not length.isascii() or not length.isdigit():
                return self._fail(411, 'length_required', 'Content-Length is required')
            if int(length) > settings.max_request_bytes:
                return self._fail(413, 'request_too_large', 'the body exceeds the configured limit')
            if state.closing:
                return self._fail(503, 'stopping', 'the server is stopping')
            if state.engine is None:
                return self._fail(503, 'loading', 'the model is still loading')
            if not state.admit():  # before the body is read: an overloaded server buffers and decodes nothing
                return self._fail(503, 'stopping' if state.closing else 'overloaded', 'too many requests are pending')
            try:
                try:
                    body = self.rfile.read(int(length))
                except TimeoutError:
                    return self._fail(408, 'request_timeout', 'the request was not delivered in time')
                self._consumed = len(body)
                if len(body) < int(length):  # the client closed early, or the server is stopping: nothing to answer
                    return self._fail(400, 'incomplete_body', 'the body ended before its declared length')
                state.work_started()
                try:
                    self._embed(body)
                finally:
                    state.work_done()
            finally:
                state.release()

        def _embed(self, body):
            try:
                items = parse_request(body, state.limits)
            except RequestError as error:
                return self._fail(error.status, error.code, error.message)
            try:
                with state.gpu:
                    if state.closing:  # the engine may be closed once the stop gets the lock: never start a forward
                        return self._fail(503, 'stopping', 'the server is stopping')
                    result = state.engine.embed(items)
                if len(result.vectors) != len(items):
                    raise EngineError('the engine returned a wrong number of vectors')
                response = build_response(settings.model_id, result.vectors, result.prompt_tokens)
            except EngineError as error:
                return self._fail(500, 'engine_failed', str(error))
            except Exception as error:  # a model failure never leaks a trace to the client; the type goes to the log
                state.log.note('engine_error', type(error).__name__)
                return self._fail(500, 'engine_failed', 'the embedding failed')
            self._send(200, response)

        def _refuse_method(self):
            self._fail(405, 'method_not_allowed', 'only GET health and POST embeddings exist')

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse_method

        def handle(self):
            super().handle()
            self._discard_unread_body()

        def _unread_body(self):
            """Declared body bytes not yet read; None when the length is unknown (a chunked body)."""
            headers = getattr(self, 'headers', None)
            if headers is None:
                return 0
            if headers.get('Transfer-Encoding') is not None:
                return None
            length = headers.get('Content-Length', '')
            return max(int(length) - self._consumed, 0) if length.isascii() and length.isdigit() else 0

        def _discard_unread_body(self):
            """After the reply: half-close, then read and drop what the client is still sending.

            Closing a socket that holds unread input makes Windows reset the connection, and the reset discards the
            reply the client has not read yet. Nothing is stored; the total is at most `server.max_discard_bytes`
            whatever length the client declares (beyond it the reply is best effort), and the time is bounded by the
            read deadline.
            """
            remaining = self._unread_body()
            if remaining == 0:
                return
            budget = settings.max_discard_bytes if remaining is None else min(remaining, settings.max_discard_bytes)
            try:
                self.connection.shutdown(socket.SHUT_WR)
                drain(self.rfile.read1, budget)
            except OSError:  # the client left, or the deadline passed: nothing more to preserve
                pass

    return Handler


class _Server(ThreadingHTTPServer):
    """`ThreadingHTTPServer` that owns its connections: a registry of accepted sockets and handler threads, a cap on
    it checked before a thread exists, and a stop that can cut and join them (the stdlib keeps no list of daemon
    threads and has no cap)."""

    # SO_REUSEADDR on Windows lets a second process bind the same listening port; on POSIX it only skips TIME_WAIT.
    allow_reuse_address = os.name != 'nt'

    def __init__(self, address, handler, state):
        self._state = state
        self._lock = threading.Lock()
        self._connections = {}  # accepted socket -> its handler thread (None while the thread is being created)
        self._stopping = threading.Event()
        super().__init__(address, handler)
        # `select` can report a connection that is gone before `accept` takes it; with a timeout the accept gives up
        # after one slice (a `TimeoutError`, which `_handle_request_noblock` drops as the `OSError` it is) instead of
        # waiting for the next client. `socket.accept` makes the accepted socket blocking again.
        self.socket.settimeout(CANCEL_POLL_S)

    def serve_forever(self, poll_interval=CANCEL_POLL_S):
        """The accept loop, owned here (fix round 4, RT3F1): the stdlib `shutdown()` waits for its loop with no
        timeout. This loop looks at a stop event every `poll_interval` seconds; `stop_accepting` sets it and returns at
        once, and the stop joins this thread within its window and names it when it is still alive. Nothing in the
        loop waits for a client or the console: a refusal is written on a non-blocking socket and logged through
        `BoundedOutput`."""
        selector_type = getattr(selectors, 'PollSelector', selectors.SelectSelector)  # what the stdlib loop uses
        try:
            with selector_type() as selector:
                selector.register(self, selectors.EVENT_READ)
                while not self._stopping.is_set():
                    try:
                        ready = selector.select(poll_interval)
                    except OSError:
                        if self._stopping.is_set():  # the listener was closed under the select: the stop
                            break
                        raise
                    if self._stopping.is_set():
                        break
                    if ready:
                        self._handle_request_noblock()
                    self.service_actions()
        except Exception as error:  # the stdlib would print a trace on stderr from this thread; the type is enough
            self._state.log.note('accept_error', type(error).__name__)

    def stop_accepting(self):
        """Ask the accept loop to end; never waits (the stop joins the accept thread within its window)."""
        self._stopping.set()

    def shutdown(self):
        """The stdlib `shutdown()` waits for the loop with no timeout; here it only asks (`stop_accepting`)."""
        self.stop_accepting()

    def process_request(self, request, client_address):
        with self._lock:  # the slot is taken before a thread exists
            full = self._state.closing or len(self._connections) >= self._state.settings.max_connections
            if not full:
                self._connections[request] = None
        if full:
            self._turn_away(request)
            return
        try:
            thread = threading.Thread(target=self._serve_connection, args=(request, client_address),
                                      name=CONNECTION_THREAD, daemon=True)
            with self._lock:
                self._connections[request] = thread
            thread.start()
        except BaseException:
            self._forget(request)
            self.shutdown_request(request)
            raise

    def _serve_connection(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                self._forget(request)

    def shutdown_request(self, request):
        """The stdlib guards only the `shutdown`; a `close` that raises would escape a handler's `finally` (a trace on
        stderr from that thread) or cut the stop short (fix round 5). Both are guarded here; a socket that stays
        open is still judged by the stop (registry, listener)."""
        try:
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.close_request(request)
        except OSError:
            pass

    def _forget(self, request):
        with self._lock:
            self._connections.pop(request, None)

    def _turn_away(self, request):
        """Answer an over-cap connection on the accept thread without blocking it: one small reply on a
        non-blocking socket, then close. Best effort: one block of an already buffered request is dropped first so the
        close is not a reset, but a request still arriving may make the client see a reset instead of the reply."""
        closing = self._state.closing
        self._state.log.note('connection_refused', 'stopping' if closing else 'overloaded')
        try:
            request.setblocking(False)
            try:
                request.recv(DRAIN_BLOCK)
            except OSError:  # nothing buffered yet (would block), or the client is gone
                pass
            request.send(_refusal(HTTPStatus.SERVICE_UNAVAILABLE, 'stopping' if closing else 'overloaded',
                                  'the server is stopping' if closing else 'too many connections'))
        except OSError:
            pass
        self.shutdown_request(request)

    def handle_error(self, request, client_address):
        # The stdlib prints a full trace per failed connection on stderr (a blocking write); one JSON line with the
        # type through `BoundedOutput` is enough (a client that resets or times out is routine) and, like the engine
        # errors, never carries a path or a message.
        self._state.log.note('connection_error', type(sys.exc_info()[1]).__name__)

    def cut_connections(self):
        """End every blocked read (the event) and shut down both directions of every accepted socket, which also wakes
        a handler blocked writing where the platform lets shutdown do it; a send always ends at its own deadline."""
        self._state.cut.set()
        with self._lock:
            sockets = list(self._connections)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def join_connections(self, timeout):
        """Join every handler thread within `timeout` seconds in total; return how many are still alive."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = [thread for thread in self._connections.values() if thread is not None]
        for thread in threads:
            try:
                thread.join(max(deadline - time.monotonic(), 0))
            except RuntimeError:  # registered, not started yet (the accept thread is inside its start): not alive,
                pass  # and its socket, still registered, is reported by the stop
        return sum(thread.is_alive() for thread in threads)

    def close_connections(self):
        """Close what is left (a handler that did not finish): no accepted socket outlives the stop."""
        with self._lock:
            sockets = list(self._connections)
        for sock in sockets:
            self.shutdown_request(sock)

    def registered(self):
        """How many accepted sockets are still registered (a finished handler has forgotten its own)."""
        with self._lock:
            return len(self._connections)


def _close_engine(state, deadline, window):
    """Close the engine while holding the GPU lock; return None, or the note of a survivor.

    The lock is awaited until `deadline`. The close runs on its own daemon thread, which holds the lock until it ends,
    and is joined within `window`: a close that does not finish (the collector, the driver) leaves the engine and the
    lock to that thread and is reported, since no forward can start anyway (`closing` is checked under the lock)."""
    if state.engine is None:
        return None
    if not state.gpu.acquire(timeout=max(deadline - time.monotonic(), 0)):
        return f'the engine was not closed: the GPU lock was not free within {window} s (a forward pass is running)'
    failure = []

    def close():
        try:
            state.engine.close()
            state.engine = None
        except Exception as error:  # the engine still holds what it holds: report it, with its type only
            failure.append(type(error).__name__)
        finally:
            state.gpu.release()

    try:
        closer = threading.Thread(target=close, name=CLOSE_THREAD, daemon=True)
        closer.start()
    except BaseException as error:  # an Exception means the thread never ran (`threading.py`: `_start_new_thread`
        state.gpu.release()  # raised), so the lock is ours to give back; no synchronous close (it has no bound)
        if not isinstance(error, Exception):
            raise
        return f'the engine was not closed: its closing thread could not start ({type(error).__name__})'
    closer.join(window)
    if closer.is_alive():
        return f'the engine close did not finish within {window} s'
    return f'the engine close failed ({failure[0]})' if failure else None


def stop_server(server, accept_thread, state):
    """The one shutdown order (RT1); returns only when every owned resource is released, else raises
    `ShutdownIncomplete` naming what is still live.

    1. `closing` is set: no request is admitted, queued forwards are refused, new connections are turned away.
    2. The accept loop is asked to stop (no wait) and its thread is joined within `server.read_timeout_s`; the
       listening socket closes whether it ended or not.
    3. Requests past their body read (decoding, queued for the GPU, in the engine, building or sending the reply) get
       up to `server.read_timeout_s` to finish; their own send deadline ends a reply sooner, a decoder or the engine
       is not interrupted. A client still sending its body is not waited for.
    4. Every accepted socket is shut down, which wakes handlers blocked reading or writing.
    5. Handler threads are joined within a second `server.read_timeout_s`; sockets still registered are closed.
    6. The engine is closed only while holding the GPU lock, so no forward pass runs under it; the lock is awaited
       for what is left of that second window, and the engine stays open (never raced) when it is not free. The close
       itself gets its own `server.read_timeout_s` (`_close_engine`).
    7. The log summary is queued (it names the lines the output dropped) and the output gets at most one more
       `server.read_timeout_s` to deliver; a console nobody reads loses those lines, it does not hold the stop.
    8. Success means all of: accept thread joined, listener closed, no handler thread alive, no socket registered, no
       admitted request, engine closed. Each survivor is named in the exception (kinds and counts only). Undelivered
       output is not a survivor: it holds no port, thread slot, admission or GPU memory.

    Each step is bounded, so the whole stop takes at most `5 * server.read_timeout_s` plus scheduling (accept join,
    grace, handler join with the GPU lock, engine close, output); without a stuck part it is about the grace and the
    join. No step raises past the next one (fix round 5): a socket close that fails, a handler not started yet, a
    closing thread or an output writer the OS refuses each leave a survivor to report (or a dropped line), never a
    skipped step.
    """
    window = state.settings.read_timeout_s
    state.begin_closing()
    server.stop_accepting()
    accept_thread.join(window)
    try:
        server.server_close()
    except OSError:  # judged below by the listener check; the rest of the stop still runs
        pass
    state.wait_work_done(window)
    server.cut_connections()
    deadline = time.monotonic() + window
    alive = server.join_connections(window)
    server.close_connections()
    engine_note = _close_engine(state, deadline, window)
    state.log.summary()
    state.output.close(window)
    in_flight, working = state.counts()
    registered = server.registered()
    survivors = [
        (accept_thread.is_alive(), 'the accept thread is still running'),
        (server.socket.fileno() != -1, 'the listening socket is still open'),
        (alive, f'{alive} handler thread(s) still running'),
        (registered, f'{registered} accepted socket(s) still registered'),
        (in_flight or working, f'{in_flight} request(s) still admitted, {working} of them past their body read'),
        (engine_note, engine_note),
    ]
    named = [what for present, what in survivors if present]
    if named:
        raise ShutdownIncomplete('the stop did not release everything within its window: ' + '; '.join(named))


def serve(settings, load, *, stop=None, announce=None, clock=time.monotonic):
    """Serve until `stop` is set or the idle limit passes; return `'stop'` or `'idle'`.

    `load(settings)` returns the engine (it verifies and loads the weights); an exception from it stops the server
    and propagates, with the port closed. The stop order is `stop_server`'s. The ready line goes to `announce`, by
    default the server's `BoundedOutput` on stdout, so nothing of the server waits in `sys.stdout` at exit.
    """
    stop = stop or threading.Event()
    state = _State(settings, clock)
    announce = announce or state.output.emit
    server = _Server((settings.host, settings.port), _handler(state), state)
    thread = threading.Thread(target=server.serve_forever, name='embed-http', daemon=True)
    try:
        thread.start()
    except BaseException:  # nothing else is owned yet: close the listener, the caller reports the start error
        try:
            server.server_close()
        except OSError:
            pass
        raise
    reason = 'stop'
    try:
        state.engine = load(settings)
        state.last_activity = clock()
        announce(json.dumps({'event': 'ready', 'pid': os.getpid(), 'host': settings.host, 'port': settings.port,
                             'model': settings.model_id}))
        while not stop.wait(IDLE_POLL_S):
            if settings.idle_exit_s and state.idle_for() >= settings.idle_exit_s:
                reason = 'idle'
                break
    finally:
        stop_server(server, thread, state)
    return reason
