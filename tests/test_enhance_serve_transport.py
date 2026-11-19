"""Experiments behind two design claims of `enhance serve` (T-016 P3, F7; evidence class `experiment`, Windows only).

D10 (`cli_serve._finish`): on the Windows proactor loop `StreamWriter.drain()` returns before the transport has released
a large write, so a response counts as delivered only after `close()` and `wait_closed()` within `write_timeout_s`.
Real-socket tests: a Windows loopback accepts a very large write from a peer that never reads, so a slow reader cannot
be made to block a socket here, and the write bounds are tested with a labelled `FakeWriter` instead
(`test_enhance_serve_lifecycle.py::test_the_write_deadline_bounds_every_wait_of_a_response`).
Both tests are skipped off Windows: the claims are about the proactor loop and the Windows TCP stack, not about POSIX."""
import asyncio
import os
import socket

import pytest

from serve_fixtures import WAIT_S

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='observed on the Windows proactor loop only')
SIZE = 32 << 20  # the size the design notes quote


async def serve_one(handler):
    """One loopback connection whose client never reads; `handler(reader, writer)` runs on the server side."""
    done = asyncio.get_running_loop().create_future()

    async def wrapped(reader, writer):
        try:
            done.set_result(await handler(reader, writer))
        except BaseException as error:  # reported to the test, which owns the assertions
            done.set_exception(error)
    server = await asyncio.start_server(wrapped, '127.0.0.1', 0)
    client = socket.create_connection(('127.0.0.1', server.sockets[0].getsockname()[1]))
    try:
        return await asyncio.wait_for(done, WAIT_S)
    finally:
        client.close()
        server.close()
        await server.wait_closed()


def test_a_windows_loopback_accepts_32_mib_from_a_peer_that_never_reads():
    async def handler(reader, writer):
        writer.write(b'x' * SIZE)
        await asyncio.wait_for(writer.drain(), 10)
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 10)  # released although nobody read a byte
        return True
    assert asyncio.run(serve_one(handler))


def test_proactor_drain_returns_while_the_transport_has_not_yet_released_the_write():
    async def handler(reader, writer):
        transport = writer.transport
        writer.write(b'x' * SIZE)
        await writer.drain()  # returns at once: the in-flight send is not counted as buffered
        state = (type(asyncio.get_running_loop()).__name__, transport.get_write_buffer_size(),
                 transport._write_fut is not None)  # private attribute of the proactor transport: experiment only
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 10)
        return state, transport._write_fut is None
    (loop_name, buffered, send_in_flight), released = asyncio.run(serve_one(handler))
    assert loop_name == 'ProactorEventLoop'
    assert buffered == 0 and send_in_flight  # drain() alone said "done" while the send was still the transport's
    assert released  # only wait_closed() after close() ends when the transport has let go of it
