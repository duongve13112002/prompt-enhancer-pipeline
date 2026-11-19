"""`enhance serve`: S0 to S6 over HTTP, bounded admission and a cooperative stop (T-016 P3, R-015, R-017, R-018, R-024).

Standard library only: one asyncio loop owned by this command, one `Pipeline`, one request per connection. Every wait has
a validated bound or a named safety-bound constant and a cleanup path; the table is in tasks/active/T-016-cli-run-guide.md
("Blocking operations of enhance serve"). The response of `POST /v1/enhance` is the S6 envelope exactly as the pipeline
built it, carried with the HTTP status `http_wire.http_status_of` gives; a transport refusal is a distinct error body that
never invents a trace id (R-024, R-027). The response is acknowledged (`after_delivery`) only after its bytes were written
and flushed to the transport; any failure before that abandons the ticket at once (`abandon()`), so the lifecycle records
`delivery_unacknowledged`, runs no monitor and frees the trace slot and the model-client owner without waiting for the stop. Exit status as `enhance once`: 0 a clean stop, 1 an operational failure
(bind, a stop that overran `serving.http.shutdown_timeout_s`, a lifecycle error), 2 a refusal before anything started.
"""
import asyncio
from collections import deque
from datetime import datetime, timezone
import signal
import sys

from . import http_wire as wire
from .attachments import AttachmentNotFound, publish_attachments, read_attachment, sweep_attachments
from .cli_input import Refusal, parse_request, refuse_linked_configuration
from .config import ConfigurationError, load_config, validate_serving
from .pipeline import Pipeline
from .response_lifecycle import LifecycleFull
from .storage import StorageError, open_data_store
from .trace_store import trace_expiry

REFUSED = 2  # protocol-bound exit status: refused before anything started
FAILED = 1  # protocol-bound exit status: operational failure
LINGER_S = 1.0  # safety-bound: how long a closed connection waits for the client's own close, so a reset cannot eat the response
TURNED_AWAY_LIMIT = 32  # safety-bound: connections over the limit that are answered 503 at once; more are reset
LISTEN_BACKLOG = 128  # safety-bound: the OS queue of connections not yet accepted
NOTE_LIMIT = 12  # safety-bound: stderr lines during service (a pipe holds about 4 KiB, so a reader that never reads cannot block)


class ServeFailure(Exception):
    """A start that failed after the arguments were accepted (for example the address cannot be bound): exit 1."""


class Notes:
    """Diagnostics on stderr: a fixed number of short lines of type names and counts, never a prompt, path or value."""

    def __init__(self, stream):
        self.stream, self.written, self.dropped = stream, 0, 0

    def emit(self, line, *, final=False):
        if self.written >= NOTE_LIMIT and not final:
            self.dropped += 1
            return
        self.written += 1
        try:
            print(line[:160], file=self.stream, flush=True)
        except (OSError, ValueError):  # a console that is gone does not replace the command's result
            self.dropped += 1


class StopReport:
    """What the stop found: a clean stop has no cancelled request and no overrun."""

    def __init__(self):
        self.cancelled = 0  # requests still running when `shutdown_timeout_s` passed, cancelled and joined
        self.listener_overran = self.close_overran = False

    @property
    def clean(self):
        return not (self.cancelled or self.listener_overran or self.close_overran)


class _Request:
    """Whether the whole request was read: unread input at close would reset the connection and lose the response."""
    consumed = False


async def _eof(reader):
    while await reader.read(65536):  # a client that keeps sending is ignored; only its close matters
        pass


class _Admission:
    """`limit` places that run and `queue` places that wait; a request that finds both full is refused at once.

    A place handed to a waiter counts as running from that moment, so a request that arrives while the hand-over still
    waits for its turn on the loop sees the true free capacity and is not turned away for a place that is being freed."""

    def __init__(self, limit, queue):
        self.limit, self.queue, self.running = limit, queue, 0
        self._waiters = deque()

    @property
    def waiting(self):
        return len(self._waiters)

    async def enter(self):
        """True once a place is held (the caller must `leave`); False when the request is refused."""
        if self.running < self.limit and not self._waiters:
            self.running += 1
            return True
        if len(self._waiters) >= self.queue:
            return False
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self.leave()  # the place had been handed over when the request was cancelled: pass it on
            elif waiter in self._waiters:  # a cancelled waiter that `leave` already popped is gone (retest R2)
                self._waiters.remove(waiter)
            raise
        return True

    def leave(self):
        self.running -= 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.running += 1
                waiter.set_result(None)
                return


class Server:
    def __init__(self, settings, pipeline, store, root, retention_s, notes):
        self.settings, self.pipeline, self.store, self.root = settings, pipeline, store, root
        self.retention_s, self.notes = retention_s, notes
        self.stopping = False
        self.open = self.turned_away = 0  # connections holding a slot; 503 answers lingering
        self._admission = _Admission(settings['max_concurrency'], settings['queue_limit'])
        self._tasks, self._server = set(), None
        self._idle, self._requests = set(), set()  # handlers waiting for a request head; handlers whose head was read
        self._next_expiry, self._sweeping, self._noted = None, False, []  # attachment copies: see `_expire`

    @property
    def waiting(self):
        return self._admission.waiting  # requests queued for a place

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    # --- attachment expiry ---------------------------------------------------------------------------------------

    async def _sweep(self):
        """Remove expired copies and learn the earliest expiry left: one scan of the live sets, on the lifecycle pool."""
        self._sweeping, self._noted = True, []
        try:
            removed, failed, earliest = await self.pipeline.lifecycle.io(sweep_attachments, self.store, self._now())
        finally:
            self._sweeping = False
        if failed:
            self.notes.emit(f'warning: expired attachment cleanup failed for {failed} entries')
        known = [item for item in (earliest, *self._noted) if item is not None]  # sets published during the scan count
        self._next_expiry = min(known) if known else None

    async def _expire(self):
        """The cost of a request does not grow with the number of live sets: the live sets are scanned again only after
        the earliest known expiry has passed (the publications of this process keep that time up to date)."""
        if not self._sweeping and self._next_expiry is not None and self._next_expiry <= self._now():
            await self._sweep()

    def _note_expiry(self, expires_at):
        if self._sweeping:
            self._noted.append(expires_at)  # the running scan may have listed the sets before this one existed
        self._next_expiry = expires_at if self._next_expiry is None else min(self._next_expiry, expires_at)

    # --- start ---------------------------------------------------------------------------------------------------

    async def start(self):
        await self._sweep()
        try:
            self._server = await asyncio.start_server(self._connection, self.settings['host'], self.settings['port'],
                                                      limit=wire.MAX_HEAD_BYTES, backlog=LISTEN_BACKLOG)
        except OSError as error:  # asyncio closes every socket it opened before it raises
            raise ServeFailure(f'cannot listen on {self.settings["host"]}:{self.settings["port"]} '
                               f'({type(error).__name__})') from None
        return self._server.sockets[0].getsockname()[:2]

    # --- one connection ------------------------------------------------------------------------------------------

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            if self.stopping or self.open >= self.settings['max_connections']:
                await self._turn_away(reader, writer)
            else:
                self.open += 1
                try:
                    await self._exchange(reader, writer)
                finally:
                    self.open -= 1
        except Exception as error:  # the type only: nothing of a request may reach a log
            self.notes.emit(f'connection failed: {type(error).__name__}')
        finally:
            self._tasks.discard(task)
            self._idle.discard(task)
            self._requests.discard(task)
            self._close(writer)

    async def _turn_away(self, reader, writer):
        if self.turned_away >= TURNED_AWAY_LIMIT:
            writer.transport.abort()
            return
        self.turned_away += 1
        try:
            code = 'stopping' if self.stopping else 'overloaded'
            writer.write(wire.error_response(wire.WireError(503, code, 'the service cannot take another request now')))
            await self._linger(reader, writer)
        finally:
            self.turned_away -= 1

    async def _exchange(self, reader, writer):
        state, response, ack = _Request(), None, None
        try:
            async with asyncio.timeout(self.settings['total_deadline_s']) as budget:
                try:
                    response, ack = await self._respond(reader, writer, state)
                except wire.WireError as error:
                    response = wire.error_response(error)
                except ConnectionError:
                    raise
                except Exception as error:  # an unforeseen fault is a 500 with the type on stderr, never a traceback
                    self.notes.emit(f'request failed: {type(error).__name__}')
                    response = wire.error_response(wire.WireError(500, 'internal', 'the request failed'))
        except TimeoutError:  # only the service deadline reaches here: the read deadline is converted below
            error = (wire.WireError(504, 'deadline_exceeded', 'the service deadline passed') if budget.expired()
                     else wire.WireError(500, 'internal', 'the request failed'))
            response, ack = wire.error_response(error), None
        except ConnectionError:  # the client left: nothing to answer, nothing delivered
            return
        try:
            delivered = await self._finish(reader, writer, await self._write(writer, response), state.consumed)
            if delivered and ack is not None:
                try:
                    await ack.after_delivery()
                except Exception as error:  # a lifecycle fault is also counted in lifecycle.errors and fails the stop
                    self.notes.emit(f'acknowledgement failed: {type(error).__name__}')
        finally:
            await self._abandon(ack)  # a response that was not acknowledged is finalised now, not at the stop

    async def _abandon(self, response):
        """Every response the pipeline produced ends in `after_delivery` or here: its trace slot and model-client owner
        are released at once (a no-op once acknowledged). Paths: not written or not flushed, the client left or the
        service deadline passed after generation, a failed publication, a stop that cancelled the handler."""
        if response is None:
            return
        try:
            await response.abandon()
        except Exception as error:
            self.notes.emit(f'abandon failed: {type(error).__name__}')

    async def _read_request(self, reader, writer, state):
        """Head, route and body under one deadline of `read_timeout_s` (a trickling client cannot extend it)."""
        settings, task = self.settings, asyncio.current_task()
        try:
            async with asyncio.timeout(settings['read_timeout_s']):
                self._idle.add(task)  # no request yet: a stop closes this connection and does not count it as work
                try:
                    head = await wire.read_head(reader)
                finally:
                    self._idle.discard(task)
                self._requests.add(task)  # a request was received: a stop that has to cancel it is an overrun
                found = wire.route(head.method, head.target)
                if found.name != wire.ENHANCE:
                    wire.body_length(head.headers, 0, required=False)  # these routes take no body
                    state.consumed = True
                    return found, b''
                wire.require_json(head.headers)
                length = wire.body_length(head.headers, settings['max_request_bytes'], required=True)
                expect = head.headers.get('expect')
                if expect is not None:
                    if expect.lower() != '100-continue':
                        raise wire.WireError(417, 'expectation_failed', 'only 100-continue is supported')
                    writer.write(b'HTTP/1.1 100 Continue\r\n\r\n')
                body = await reader.readexactly(length)
                state.consumed = True
                return found, body
        except TimeoutError:
            raise wire.WireError(408, 'request_timeout', 'the request was not received in time') from None
        except asyncio.IncompleteReadError:
            raise ConnectionError('closed during the request body') from None

    async def _respond(self, reader, writer, state):
        found, body = await self._read_request(reader, writer, state)
        if found.name == wire.HEALTH:
            return wire.build_response(200, wire.json_bytes({'status': 'ok'})), None
        if found.name == wire.ATTACHMENT:
            data = await self._until_gone(reader, self._download(*found.params))
            return wire.build_response(200, data, 'application/octet-stream'), None
        try:
            # a parse of up to max_request_bytes is CPU work: it runs on the lifecycle's own pool, not on the loop
            request = await self.pipeline.lifecycle.io(parse_request, body, self.root)
        except Refusal as error:
            raise wire.WireError(400, 'invalid_request', str(error)) from None
        return await self._until_gone(reader, self._enhance(request))

    async def _until_gone(self, reader, work):
        """Run `work` until it ends or the client closes; the loser is cancelled and joined, never left running."""
        task, gone = asyncio.ensure_future(work), asyncio.ensure_future(_eof(reader))
        returned = False
        try:
            await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                raise ConnectionError('the client closed before the response')
            result = task.result()
            returned = True
            return result
        finally:
            for pending in (task, gone):
                pending.cancel()
            await asyncio.gather(task, gone, return_exceptions=True)
            if not returned and task.done() and not task.cancelled() and task.exception() is None:
                # The handler was cancelled (deadline or stop) in the loop turn in which `_enhance` finished: its
                # `(bytes, response)` result is dropped here, so the response is abandoned, never left holding its
                # trace slot until the stop (retest R1). A download result is bytes and holds nothing.
                result = task.result()
                if isinstance(result, tuple):
                    await self._abandon(result[1])

    # --- admission and generation --------------------------------------------------------------------------------

    async def _enhance(self, request):
        """At most `max_concurrency` run and `queue_limit` wait; a request that finds both full is refused at once."""
        if not await self._admission.enter():
            raise wire.WireError(503, 'overloaded', 'the service cannot take another request now')
        try:
            if self.stopping:
                raise wire.WireError(503, 'stopping', 'the service cannot take another request now')
            return await self._generate(request)
        finally:
            self._admission.leave()

    async def _generate(self, request):
        pipeline = self.pipeline
        await self._expire()
        try:
            response = await pipeline.enhance(request)
        except LifecycleFull:  # trace capacity is reserved before any model call: nothing ran, nothing is held
            raise wire.WireError(503, 'overloaded', 'trace capacity unavailable') from None
        except ConfigurationError:
            raise wire.WireError(400, 'invalid_request', 'request overrides change the deployment configuration') from None
        handed_over = False
        try:
            if response.attachment_bytes:
                # bytes and index exist before any metadata names them; a failure here is not a delivery
                expires_at = trace_expiry(self._now(), self.retention_s)
                try:
                    await pipeline.lifecycle.io(publish_attachments, self.store, response.envelope['trace_id'],
                                                response.attachment_bytes, expires_at)
                finally:
                    self._note_expiry(expires_at)  # a failed publication may leave a set that its index ages out
            status = wire.http_status_of(response.envelope)
            result = wire.build_response(status, wire.json_bytes(response.envelope)), response
            handed_over = True
            return result
        except (StorageError, OSError, OverflowError, ValueError, TypeError) as error:
            self.notes.emit(f'delivery failed: {type(error).__name__}')
            raise wire.WireError(500, 'delivery_failed', 'the response could not be prepared for delivery') from None
        finally:
            if not handed_over:  # a failed publication, or the request was cancelled after generation: never delivered
                await self._abandon(response)

    async def _download(self, trace_id, image_id):
        try:
            return await self.pipeline.lifecycle.io(read_attachment, self.store, trace_id, image_id)
        except AttachmentNotFound:
            raise wire.WireError(404, 'not_found', 'no such attachment') from None
        except (StorageError, OSError) as error:
            self.notes.emit(f'attachment read failed: {type(error).__name__}')
            raise wire.WireError(500, 'internal', 'the attachment could not be read') from None

    # --- bytes out -----------------------------------------------------------------------------------------------

    async def _write(self, writer, data):
        """Write and flush to the transport within `write_timeout_s`; True only when the flush completed."""
        try:
            writer.write(data)
            async with asyncio.timeout(self.settings['write_timeout_s']):
                await writer.drain()
            return True
        except TimeoutError:
            writer.transport.abort()  # the reader is too slow: the connection is dropped, the response not delivered
            self.notes.emit('write timed out')
        except OSError:  # includes ConnectionError
            pass
        return False

    async def _finish(self, reader, writer, written, consumed):
        """Close the connection and, for a written response, wait until the transport has released every byte.

        `drain()` alone is not a flush: the Windows proactor loop starts one large write at once and drains at once.
        A transport reports its connection lost only after its pending write ended, so the response counts as delivered
        when that completes within `write_timeout_s`; a reset, or a reader too slow to take the bytes, does not."""
        if written and not consumed:
            await self._linger(reader, writer)
        self._close(writer)
        if not written:
            return False
        try:
            async with asyncio.timeout(self.settings['write_timeout_s']):
                await writer.wait_closed()
            return True
        except TimeoutError:
            writer.transport.abort()
            self.notes.emit('write timed out')
        except OSError:
            pass
        return False

    async def _linger(self, reader, writer):
        """After an answer sent before the request was fully read: finish our side, then wait for the client's close."""
        try:
            writer.write_eof()
            async with asyncio.timeout(LINGER_S):
                while await reader.read(65536):
                    pass
        except (TimeoutError, OSError, RuntimeError, NotImplementedError):
            pass

    @staticmethod
    def _close(writer):
        try:
            writer.close()
        except (OSError, RuntimeError):
            pass

    # --- stop ----------------------------------------------------------------------------------------------------

    async def stop(self):
        """Close the listener, give running requests `shutdown_timeout_s`, cancel and join the rest, then close the pipeline.

        An overrun is reported, never hidden: a request that had to be cancelled, a listener that did not release, or a
        pipeline close that outlasted the bound makes the stop unclean. Every owned task and thread is still joined, so
        none survives the stop (an OS write that cannot be interrupted is waited for, not abandoned: Q5)."""
        bound = self.settings['shutdown_timeout_s']
        loop, report = asyncio.get_running_loop(), StopReport()
        self.stopping = True
        if self._server is not None:
            self.notes.emit(f'stopping: {len(self._tasks)} connection(s) open')
            self._server.close()
        deadline = loop.time() + bound
        for task in list(self._idle):  # connections that sent no request yet: closed, they are not work to finish
            task.cancel()
        while self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=max(0.0, deadline - loop.time()))
            if pending:
                report.cancelled += len(pending & self._requests)  # only a received request that is cut off is an overrun
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._server is not None:
            closed = asyncio.ensure_future(self._server.wait_closed())
            _, pending = await asyncio.wait({closed}, timeout=bound)
            if pending:
                report.listener_overran = True
                closed.cancel()
            await asyncio.gather(closed, return_exceptions=True)
        closing = asyncio.ensure_future(self.pipeline.close())
        _, pending = await asyncio.wait({closing}, timeout=bound)
        if pending:
            report.close_overran = True
            await closing  # joined all the same: the pipeline owns threads that must not outlive the process
        else:
            closing.result()
        return report


def _install_stop_signals(loop, stop):
    """SIGINT, SIGTERM and (Windows) SIGBREAK set the stop event; the previous handlers come back with `restore`."""
    previous = []
    try:
        for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
            number = getattr(signal, name, None)
            if number is not None:
                previous.append((number, signal.signal(number, lambda *_: loop.call_soon_threadsafe(stop.set))))
    except ValueError:  # not the main thread
        pass

    def restore():
        for number, handler in previous:
            signal.signal(number, signal.SIG_DFL if handler is None else handler)
    return restore


async def _serve(config, root, settings, notes):
    store = open_data_store(config, 'enhance serve', project_root=root)
    loop, stop = asyncio.get_running_loop(), asyncio.Event()
    async with Pipeline(config, project_root=root) as pipeline:
        server = Server(settings, pipeline, store, root, config['runtime']['trace']['retention_s'], notes)
        restore = _install_stop_signals(loop, stop)
        try:
            try:
                host, port = await server.start()
                notes.emit(f'listening on {host}:{port}')
                await stop.wait()
            finally:
                report = await server.stop()
        finally:
            restore()
    errors = sorted(set(pipeline.lifecycle.errors))
    notes.emit(f'stopped: cancelled requests {report.cancelled}', final=True)
    status = 0
    if not report.clean:
        notes.emit(f'failed: stop overran serving.http.shutdown_timeout_s (cancelled requests {report.cancelled}, '
                   f'listener {report.listener_overran}, pipeline close {report.close_overran})', final=True)
        status = FAILED
    if errors:
        notes.emit('operational: ' + ' '.join(errors), final=True)
        status = FAILED
    if notes.dropped:
        notes.emit(f'diagnostics dropped: {notes.dropped}', final=True)
    return status


def run_serve(args, root):
    notes = Notes(sys.stderr)
    try:
        refuse_linked_configuration(args, root)
        config = load_config(args.config, 'enhance serve', overlay=args.overlay, project_root=root)
        settings = validate_serving(config, project_root=root)
    except (ConfigurationError, Refusal) as error:
        print(f'refused: {error}', file=sys.stderr)
        return REFUSED
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return FAILED
    except Exception as error:  # the type only: nothing before the listener may end in a traceback or echo a path
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED
    try:
        return asyncio.run(_serve(config, root, settings, notes))
    except (ConfigurationError, StorageError) as error:
        print(f'refused: {error}', file=sys.stderr)  # pipeline construction and the store state a setting problem
        return REFUSED
    except ServeFailure as error:
        print(f'failed: {error}', file=sys.stderr)
        return FAILED
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return FAILED
    except Exception as error:  # the type only: a message may carry a path or request content
        print(f'failed: {type(error).__name__}', file=sys.stderr)
        return FAILED
