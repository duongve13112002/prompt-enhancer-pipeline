"""The embedding server over real loopback HTTP with a labelled engine double (T-018): no model, no GPU, no torch."""

import asyncio
from io import BytesIO
import json
import socket
import threading
import time

import httpx
from PIL import Image
import pytest
import yaml

from prompt_enhancer import load_config
from prompt_enhancer.embed_server.models import EmbedResult, ServerStartError
from prompt_enhancer.embed_server import server as server_module
from prompt_enhancer.embed_server.protocol import EngineError
from prompt_enhancer.embed_server.server import serve
from prompt_enhancer.embed_server.settings import server_settings
from prompt_enhancer.k1_embed import Embedder, EmbeddingError, embedder_settings
from embed_server_fixtures import DIMENSION, EngineDouble, free_port, running_server, server_profile
from k1_fixtures import image_bytes


def make_settings(project_root, model_test_profile, family='embedding_gemma2', **server):
    profile = server_profile(model_test_profile, port=free_port(), family=family)
    profile['vlm']['backends']['embedder']['server'].update(server)
    path = project_root / 'http-profile.yaml'
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    config = load_config(path, 'embed serve', project_root=project_root)
    return server_settings(config, project_root), profile


@pytest.fixture
def served(project_root, model_test_profile):
    settings, profile = make_settings(project_root, model_test_profile)
    with running_server(settings) as (base, engine, stop, result):
        yield settings, profile, base, engine, stop, result


HELD_S = 60  # test-only: how long a request held by a gate waits for its reply or its gate. A hold ends at the test's
# release, so this bounds only a test that never releases; a shorter wait fails a correct server when the machine
# stalls the test body for longer than it (T-018-R4: a 10 s client timeout ended two held requests under load).


def post(base, settings, payload, **kwargs):
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.post(base + settings.embeddings_path, content=content, **{'timeout': HELD_S, **kwargs})


def data_uri(data=None):
    import base64
    return 'data:image/png;base64,' + base64.b64encode(data or image_bytes()).decode('ascii')


def request(inputs):
    return {'model': 'synthetic-embedder', 'encoding_format': 'float', 'input': inputs}


def test_health_and_one_request(served):
    settings, _, base, engine, _, _ = served
    health = httpx.get(base + settings.health_path)
    assert health.status_code == 200
    assert health.json() == {'status': 'ok', 'loaded': True, 'model': 'synthetic-embedder', 'revision': 'test',
                             'family': 'embedding_gemma2', 'dtype': 'bfloat16', 'device': 'cuda:0'}
    answer = post(base, settings, request(['one', 'two']))
    assert answer.status_code == 200 and answer.headers['content-type'] == 'application/json'
    payload = answer.json()
    assert [item['index'] for item in payload['data']] == [0, 1]
    assert all(len(item['embedding']) == DIMENSION for item in payload['data'])
    assert payload['data'][0]['embedding'] != payload['data'][1]['embedding']
    assert [item.text for item in engine.calls[0]] == ['one', 'two']


def test_the_real_k1_client_gets_vectors_in_order_over_http(served):
    settings, profile, _, engine, _, _ = served
    embedder = Embedder(embedder_settings(profile))
    pictures = [image_bytes((6, 4), (1, 2, 3)), image_bytes((3, 5), (9, 9, 9))]

    async def run():
        try:
            return await embedder.embed_images(pictures), await embedder.embed_texts(['first', 'second'])
        finally:
            await embedder.close()

    images, texts = asyncio.run(run())
    calls = list(engine.calls)  # a snapshot: re-running the double records its own calls
    expected = [vector for call in calls for vector in engine.embed(call).vectors]
    flat = lambda rows: [value for row in rows for value in row]  # noqa: E731
    assert flat(images + texts) == pytest.approx(flat(expected[:4]), abs=1e-6)  # K1 stores float32
    assert images[0] != images[1] and texts[0] != texts[1]
    assert [item.image.size for item in engine.calls[0]] == [(6, 4), (3, 5)]
    assert [item.text for item in engine.calls[1]] == ['first', 'second']  # the text_template '{text}' is K1's


def test_k1_reports_a_server_refusal_as_an_http_error(served):
    settings, profile, _, engine, _, _ = served
    profile['embedding']['request']['image_instruction'] = 'unwanted instruction'  # EmbeddingGemma 2 has none
    embedder = Embedder(embedder_settings(profile))

    async def run():
        try:
            await embedder.embed_images([image_bytes()])
        finally:
            await embedder.close()

    with pytest.raises(EmbeddingError) as caught:
        asyncio.run(run())
    assert caught.value.code == 'HTTP_ERROR' and engine.calls == []


def test_the_instruction_part_reaches_the_engine_for_a_family_that_has_one(project_root, model_test_profile):
    settings, profile = make_settings(project_root, model_test_profile, family='qwen3_vl_embedding')
    with running_server(settings) as (base, engine, _, _):
        embedder = Embedder(embedder_settings(profile))

        async def run():
            try:
                await embedder.embed_images([image_bytes()])
            finally:
                await embedder.close()

        asyncio.run(run())
        assert [item.instruction for item in engine.calls[0]] == ['Represent the input.']


@pytest.mark.parametrize('method', ['put', 'delete', 'patch', 'options'])
def test_other_methods_and_routes_are_refused(served, method):
    settings, _, base, engine, _, _ = served
    assert httpx.request(method, base + settings.embeddings_path).status_code == 405
    assert httpx.get(base + '/nope').status_code == 404
    assert httpx.post(base + '/nope', content=b'{}').status_code == 404
    assert httpx.get(base + settings.embeddings_path).status_code == 404  # GET is health only
    assert httpx.post(base + settings.health_path, content=b'{}').status_code == 404
    assert engine.calls == []


def test_refusals_carry_a_json_error_and_never_reach_the_engine(served):
    settings, _, base, engine, _, _ = served
    for payload, status, code in [(b'not json', 400, 'invalid_json'), (request(['a'] * 3), 400, 'too_many_inputs'),
                                  (dict(request(['a']), model='other'), 400, 'unknown_model'),
                                  (dict(request(['a']), dimensions=2), 400, 'unsupported_field'),
                                  (request([{'content': [{'type': 'image_url', 'image_url': {'url': 'http://x/y.png'}}]}]),
                                   400, 'unsupported_image_url')]:
        answer = post(base, settings, payload)
        assert answer.status_code == status and answer.json()['error']['code'] == code
    assert engine.calls == []


def status_line(raw):
    """The reply's first line: read until it is complete or the connection ends (one `recv` may return a part of it)."""
    data = b''
    while b'\r\n' not in data:
        try:
            block = raw.recv(4096)
        except OSError:  # a reset: what arrived is what counts
            break
        if not block:
            break
        data += block
    return data.split(b'\r\n')[0]


def test_oversized_and_unframed_bodies(served):
    settings, _, base, engine, _, _ = served
    too_big = b'{"model": "x", "input": ["' + b'a' * settings.max_request_bytes + b'"]}'
    assert post(base, settings, too_big).status_code == 413
    exact = json.dumps(request(['a'])).encode()
    exact += b' ' * (settings.max_request_bytes - len(exact))  # JSON whitespace pads to the exact limit
    assert len(exact) == settings.max_request_bytes and post(base, settings, exact).status_code == 200
    assert post(base, settings, exact + b' ').status_code == 413  # one byte over
    with socket.create_connection((settings.host, settings.port), timeout=5) as raw:  # no Content-Length
        raw.sendall(f'POST {settings.embeddings_path} HTTP/1.0\r\n\r\n'.encode())
        assert b' 411 ' in status_line(raw)
    with socket.create_connection((settings.host, settings.port), timeout=5) as raw:  # chunked
        raw.sendall(f'POST {settings.embeddings_path} HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n'.encode())
        assert b' 411 ' in status_line(raw)
    with socket.create_connection((settings.host, settings.port), timeout=5) as raw:  # a bad length
        raw.sendall(f'POST {settings.embeddings_path} HTTP/1.0\r\nContent-Length: -5\r\n\r\n'.encode())
        assert b' 411 ' in status_line(raw)
    assert len(engine.calls) == 1  # only the request of exactly the limit reached the engine


def test_engine_failures_are_a_500_without_a_trace(project_root, model_test_profile, capfd):
    settings, _ = make_settings(project_root, model_test_profile)
    cases = [(EngineError('out of memory on the GPU'), 'out of memory on the GPU'),
             (RuntimeError('secret internal detail /home/user/path'), 'the embedding failed')]
    for failure, message in cases:
        with running_server(settings, EngineDouble(fail=failure)) as (base, engine, _, _):
            answer = post(base, settings, request(['a']))
            assert answer.status_code == 500 and answer.json()['error'] == {'code': 'engine_failed', 'message': message}
            assert 'secret' not in answer.text and 'Traceback' not in answer.text
    assert 'RuntimeError' in capfd.readouterr().out  # the type is logged for the operator, not the message


def test_a_wrong_vector_count_or_a_non_finite_value_is_a_500(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile)

    class Short(EngineDouble):
        def embed(self, items):
            return EmbedResult(super().embed(items).vectors[:-1], 1)

    class Nan(EngineDouble):
        def embed(self, items):
            return EmbedResult([[float('nan')] * DIMENSION for _ in items], 1)

    for engine in (Short(), Nan()):
        with running_server(settings, engine) as (base, _, _, _):
            answer = post(base, settings, request(['a', 'b']))
            assert answer.status_code == 500 and answer.json()['error']['code'] == 'engine_failed'


def test_requests_serialise_on_one_gpu_lock(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile)

    class Counting(EngineDouble):
        active = peak = 0

        def embed(self, items):
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            try:
                return super().embed(items)
            finally:
                type(self).active -= 1

    with running_server(settings, Counting(delay=0.2)) as (base, engine, _, _):
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(post(base, settings, request(['a'])).status_code))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert answers == [200] * 4 and Counting.peak == 1 and len(engine.calls) == 4


def test_health_says_loading_until_the_engine_is_ready(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile)
    release, result = threading.Event(), {}

    def slow_load(_):
        release.wait(HELD_S)  # held until the test releases it: "loading" must not end by the clock (T-018-R4)
        return EngineDouble()

    stop = threading.Event()
    thread = threading.Thread(target=lambda: result.update(reason=serve(settings, slow_load, stop=stop,
                                                                         announce=lambda _: None)), daemon=True)
    thread.start()
    base = f'http://{settings.host}:{settings.port}'
    deadline = time.monotonic() + 10
    while True:
        try:
            health = httpx.get(base + settings.health_path, timeout=1)
            break
        except httpx.HTTPError:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    assert health.status_code == 503 and health.json()['status'] == 'loading' and health.json()['loaded'] is False
    assert post(base, settings, request(['a'])).status_code == 503
    release.set()
    deadline = time.monotonic() + 10
    while httpx.get(base + settings.health_path, timeout=1).status_code != 200:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    stop.set()
    thread.join(10)
    assert result['reason'] == 'stop'


def port_is_free(settings):
    """True when nothing listens: a connection is refused (binding is unreliable across Windows and POSIX)."""
    try:
        socket.create_connection((settings.host, settings.port), timeout=2).close()
    except OSError:
        return True
    return False


def test_stop_closes_the_port_and_the_engine(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile)
    with running_server(settings) as (base, engine, stop, result):
        assert not port_is_free(settings)
    assert result['reason'] == 'stop' and engine.closed and port_is_free(settings)


class FakeClock:
    """Labelled test double of `serve`'s activity clock: time moves only when the test advances it."""

    def __init__(self):
        self._now, self._lock = 1000.0, threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        with self._lock:
            self._now += seconds


class CountingStop(threading.Event):
    """Labelled test double of the stop event: counts the idle loop's polls (`wait` calls)."""

    def __init__(self):
        super().__init__()
        self.polls = 0

    def wait(self, timeout=None):
        self.polls += 1
        return super().wait(timeout)


def _until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_idle_exit_comes_at_the_first_poll_after_idle_exit_s_on_an_injected_clock(project_root, model_test_profile):
    """docs/stages/K1-embed.md: the idle exit comes at most IDLE_POLL_S after idle_exit_s. Checked in polls on a fake
    clock (C3, P3 fix round 1): no exit while the idle time is below the limit, exit at the next poll once it is
    reached; no wall-clock bound, so machine load cannot fail it."""
    settings, _ = make_settings(project_root, model_test_profile, idle_exit_s=1)
    clock, stop, ready = FakeClock(), CountingStop(), threading.Event()
    with running_server(settings, stop=stop, clock=clock, announce=lambda _: ready.set()) as (base, _, _, result):
        assert ready.wait(10)
        clock.advance(0.5)  # binary-exact steps: 0.5 + 0.5 is exactly idle_exit_s
        seen = stop.polls
        assert _until(lambda: stop.polls >= seen + 2)  # two full polls below the limit
        assert 'reason' not in result
        clock.advance(0.5)
        reached = stop.polls
        assert _until(lambda: 'reason' in result)
        assert result['reason'] == 'idle'
        assert stop.polls <= reached + 1  # the poll in progress, or at most the one that had just begun


def test_idle_exit_waits_for_admitted_work_and_counts_from_its_release_on_an_injected_clock(
        project_root, model_test_profile, monkeypatch):
    """C1 (P3 fix round 2), proved without a wall-clock bound. A held engine gate keeps one request admitted; the fake
    clock then runs far past `idle_exit_s`: no exit while the work is held. After the release the idle time counts from
    the release (the held seconds are not counted): no exit below `idle_exit_s`, exit once it is reached. Labelled
    seams: `_State.admit` and `_State.release` are wrapped to report the real admission and release (a started client
    thread is not an admitted request), the clock, the stop event and the engine gate are doubles; the server logic
    is the production code."""
    admitted, released = threading.Event(), threading.Event()
    real_admit, real_release = server_module._State.admit, server_module._State.release

    def admit(state):
        taken = real_admit(state)
        if taken:
            admitted.set()
        return taken

    def release(state):
        real_release(state)
        released.set()

    monkeypatch.setattr(server_module._State, 'admit', admit)
    monkeypatch.setattr(server_module._State, 'release', release)
    settings, _ = make_settings(project_root, model_test_profile, idle_exit_s=1)
    clock, stop, ready, gate, outcome = FakeClock(), CountingStop(), threading.Event(), Gate(), {}

    def ask(base):
        try:
            outcome['status'] = post(base, settings, request(['a'])).status_code
        except Exception as error:  # reported by the assertions below, never lost in the thread
            outcome['error'] = repr(error)

    def polls_without_exit(result, count=3):
        seen = stop.polls
        assert _until(lambda: stop.polls >= seen + count)  # whole polls happened, and none ended the server
        assert 'reason' not in result

    with running_server(settings, gate, stop=stop, clock=clock, announce=lambda _: ready.set()) as (base, _, _, result):
        assert ready.wait(10)
        client = threading.Thread(target=ask, args=(base,))
        client.start()
        try:
            assert admitted.wait(10) and gate.entered.wait(10)  # admitted, and inside the engine
            clock.advance(5.0)  # five times idle_exit_s while the request is held
            polls_without_exit(result)
        finally:
            gate.release.set()
            client.join(HELD_S)
        assert outcome == {'status': 200}, outcome
        assert released.wait(10)
        polls_without_exit(result)  # the clock has not moved since the release: idle time is zero, not five seconds
        clock.advance(0.5)
        polls_without_exit(result)  # half of idle_exit_s
        clock.advance(0.5)  # binary-exact: idle_exit_s reached
        assert _until(lambda: 'reason' in result)
        assert result['reason'] == 'idle'


def test_idle_exit_zero_never_exits(project_root, model_test_profile):
    """`idle_exit_s: 0` means never: a clock a million seconds on, many polls, the server still answers."""
    settings, _ = make_settings(project_root, model_test_profile, idle_exit_s=0)
    clock, stop, ready = FakeClock(), CountingStop(), threading.Event()
    with running_server(settings, stop=stop, clock=clock, announce=lambda _: ready.set()) as (base, engine, _, result):
        assert ready.wait(10)
        clock.advance(1e6)
        seen = stop.polls
        assert _until(lambda: stop.polls >= seen + 3)
        assert 'reason' not in result and httpx.get(base + settings.health_path).status_code == 200


def test_idle_exit_comes_by_itself(project_root, model_test_profile):
    """Real-clock integration check, deliberately broad: the idle exit happens eventually and releases the engine and
    the port. The exact rules (no early exit, exit at the next poll, held work) are the injected-clock tests above;
    no lower bound is measured here because no observation of the test is the server's activity-clock origin (RT1-F1)."""
    settings, _ = make_settings(project_root, model_test_profile, idle_exit_s=1)
    with running_server(settings) as (base, engine, _, result):
        assert _until(lambda: 'reason' in result, seconds=30)
    assert result['reason'] == 'idle' and engine.closed and port_is_free(settings)


def test_a_failed_load_propagates_and_frees_the_port(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile)

    def failing(_):
        raise ServerStartError('no such weights')

    with pytest.raises(ServerStartError, match='no such weights'):
        serve(settings, failing, stop=threading.Event(), announce=lambda _: None)
    assert port_is_free(settings)


def test_a_busy_port_is_an_os_error(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile)
    with socket.socket() as holder:
        holder.bind((settings.host, settings.port))
        holder.listen()
        with pytest.raises(OSError):
            serve(settings, lambda _: EngineDouble(), stop=threading.Event(), announce=lambda _: None)


def test_the_ready_line_names_the_pid_and_port(project_root, model_test_profile):
    import os
    settings, _ = make_settings(project_root, model_test_profile)
    lines, stop = [], threading.Event()
    thread = threading.Thread(target=lambda: serve(settings, lambda _: EngineDouble(), stop=stop, announce=lines.append))
    thread.start()
    deadline = time.monotonic() + 10
    while not lines and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(10)
    assert json.loads(lines[0]) == {'event': 'ready', 'pid': os.getpid(), 'host': settings.host, 'port': settings.port,
                                    'model': 'synthetic-embedder'}


def test_an_image_in_the_request_is_decoded_to_rgb_for_the_engine(served):
    settings, _, base, engine, _, _ = served
    buffer = BytesIO()
    Image.new('RGBA', (3, 2), (10, 20, 30, 40)).save(buffer, 'PNG')
    assert post(base, settings, request([{'content': [{'type': 'image_url',
                                                       'image_url': {'url': data_uri(buffer.getvalue())}}]}])).status_code == 200
    assert engine.calls[0][0].image.mode == 'RGB' and engine.calls[0][0].image.size == (3, 2)


# --- fix round 1: refusals reach the client (R4), bounded admission and reads (R3), bindable addresses (R5) ---------

def exchange(settings, head, body=b''):
    """One raw HTTP exchange: send `head` and `body`, read to the end; return (status, parsed JSON body or None).

    `sendall` must not fail even though the server answers before it has read the body: that is the refusal reaching
    the client (a Windows reset of a connection with unread input discards the reply).
    """
    with socket.create_connection((settings.host, settings.port), timeout=10) as raw:
        raw.sendall(head + body)
        chunks = []
        while True:
            block = raw.recv(65536)
            if not block:
                break
            chunks.append(block)
    reply = b''.join(chunks)
    status = int(reply.split(b' ', 2)[1])
    payload = reply.partition(b'\r\n\r\n')[2]
    return status, (json.loads(payload) if payload else None)


def post_head(settings, length, path=None):
    return f'POST {path or settings.embeddings_path} HTTP/1.0\r\nContent-Length: {length}\r\n\r\n'.encode()


class Gate(EngineDouble):
    """Labelled engine double whose forward blocks until the test releases it."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def embed(self, items):
        self.entered.set()
        assert self.release.wait(HELD_S)
        return super().embed(items)


@pytest.mark.parametrize('label, head, body, status, code', [
    ('unknown route with a body', lambda s: post_head(s, 4000, '/nope'), b'x' * 4000, 404, 'not_found'),
    ('PUT with a body', lambda s: f'PUT {s.embeddings_path} HTTP/1.0\r\nContent-Length: 30000\r\n\r\n'.encode(),
     b'x' * 30000, 405, 'method_not_allowed'),
    ('GET with a body', lambda s: 'GET /nope HTTP/1.0\r\nContent-Length: 5000\r\n\r\n'.encode(), b'x' * 5000, 404,
     'not_found'),
    ('chunked body', lambda s: f'POST {s.embeddings_path} HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n'.encode(),
     b'5\r\nhello\r\n0\r\n\r\n', 411, 'length_required'),
    ('body over the limit', lambda s: post_head(s, 300000), b'x' * 300000, 413, 'request_too_large'),
    ('invalid JSON', lambda s: post_head(s, 60), b'x' * 60, 400, 'invalid_json'),
])
def test_refusals_reach_the_client_with_unread_body_bytes_pending(served, label, head, body, status, code):
    settings, _, base, engine, _, _ = served
    for _ in range(4):  # the loss on Windows was timing dependent: repeat
        got_status, payload = exchange(settings, head(settings), body)
        assert got_status == status and payload['error']['code'] == code, label
    assert engine.calls == []


def test_a_reply_does_not_wait_for_a_body_that_never_comes(project_root, model_test_profile):
    """The discard of an unread body stops at the read deadline: a client that went quiet holds nothing longer."""
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=1)
    with running_server(settings) as (base, engine, _, _):
        started = time.monotonic()
        status, payload = exchange(settings, post_head(settings, 300000, '/nope'))  # promises 300 kB, sends none
        assert status == 404 and payload['error']['code'] == 'not_found'
        assert time.monotonic() - started < 3.5


def _reply(sock):
    reply = b''
    while True:
        block = sock.recv(4096)
        if not block:
            return reply
        reply += block


def hold_deadlines(monkeypatch):
    """Labelled seam: every connection's `server._DeadlineReader` starts with no deadline and is recorded (the returned
    list); its production `readinto` runs unchanged, and a test lets a deadline pass by setting `_deadline` itself."""
    readers = []

    class HeldDeadline(server_module._DeadlineReader):
        def __init__(self, sock, deadline, cut):
            super().__init__(sock, float('inf'), cut)
            readers.append(self)

    monkeypatch.setattr(server_module, '_DeadlineReader', HeldDeadline)
    return readers


def test_a_stalled_upload_holds_its_slot_until_its_deadline_then_gets_408(project_root, model_test_profile,
                                                                          monkeypatch):
    """C4 (P3 fix round 1): an admission barrier and a test-controlled expiry, so the overload probe cannot race the
    read deadline. Labelled seams: `_State.admit` is wrapped to report the first admission, and the server's
    `_DeadlineReader` starts with no deadline and records itself; its production `readinto` runs unchanged."""
    admitted = threading.Event()
    real_admit = server_module._State.admit

    def admit(state):
        taken = real_admit(state)
        if taken:
            admitted.set()
        return taken

    monkeypatch.setattr(server_module._State, 'admit', admit)
    readers = hold_deadlines(monkeypatch)
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=1, max_pending_requests=1)
    with running_server(settings) as (base, engine, _, _):
        readers.clear()  # the readiness probes' readers; the next connection is the stalled upload
        stalled = socket.create_connection((settings.host, settings.port), timeout=10)
        try:
            stalled.sendall(post_head(settings, 1000) + b'0123456789')  # 10 of 1000 bytes, then silence
            assert admitted.wait(10)  # the slot is taken, and with no deadline it cannot expire meanwhile
            seen = post(base, settings, request(['a']))
            assert seen.status_code == 503 and seen.json()['error']['code'] == 'overloaded'
            readers[0]._deadline = time.monotonic()  # the stalled upload's deadline passes now
            started = time.monotonic()
            reply = _reply(stalled)
            assert b' 408 ' in reply.split(b'\r\n')[0] and b'request_timeout' in reply
            assert time.monotonic() - started < 3.5
        finally:
            stalled.close()
        assert post(base, settings, request(['a'])).status_code == 200  # the slot came back


def test_a_stalled_upload_gets_408_at_the_real_read_deadline(project_root, model_test_profile):
    """The production deadline itself, with no overload probe to race: a stalled upload alone gets 408 within
    `read_timeout_s` plus slack."""
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=1, max_pending_requests=1)
    with running_server(settings) as (base, engine, _, _):
        stalled = socket.create_connection((settings.host, settings.port), timeout=10)
        try:
            started = time.monotonic()
            stalled.sendall(post_head(settings, 1000) + b'0123456789')
            reply = _reply(stalled)
            assert b' 408 ' in reply.split(b'\r\n')[0] and b'request_timeout' in reply
            assert time.monotonic() - started < 1 + 2.5
        finally:
            stalled.close()
        assert post(base, settings, request(['a'])).status_code == 200
        assert len(engine.calls) == 1


def test_a_trickling_upload_cannot_outlast_the_total_deadline(project_root, model_test_profile):
    """One byte every 0.3 s would never trip a per-receive timeout of 1 s; the deadline covers the whole request."""
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=1)
    with running_server(settings) as (base, engine, _, _):
        with socket.create_connection((settings.host, settings.port), timeout=10) as raw:
            raw.sendall(post_head(settings, 100))
            started, reply = time.monotonic(), b''
            raw.settimeout(0.3)
            for _ in range(40):
                try:
                    raw.sendall(b'x')
                    reply = raw.recv(4096)  # waits up to 0.3 s: the pace of the trickle
                except TimeoutError:
                    continue
                except OSError:
                    break
                if reply:
                    break
            assert b' 408 ' in reply.split(b'\r\n')[0]
            assert time.monotonic() - started < 3.5
        assert engine.calls == []


def test_a_connection_that_never_finishes_its_headers_is_closed_when_its_deadline_passes(
        project_root, model_test_profile, monkeypatch):
    """Not closed before its deadline, closed without a reply once it passes. The deadline is the held-deadline seam's,
    so no wall-clock lower bound is measured from an observation later than the connection's own deadline origin."""
    readers = hold_deadlines(monkeypatch)
    settings, _ = make_settings(project_root, model_test_profile, read_timeout_s=1)
    with running_server(settings) as (base, engine, _, _):
        readers.clear()  # the readiness probes' readers
        with socket.create_connection((settings.host, settings.port), timeout=10) as raw:
            raw.sendall(b'POST /v1/emb')
            assert _until(lambda: readers)  # the connection's handler is reading, with no deadline
            raw.settimeout(0.5)
            with pytest.raises(TimeoutError):
                raw.recv(4096)  # still open: its deadline has not passed
            readers[0]._deadline = time.monotonic()  # it passes now
            raw.settimeout(10)
            assert raw.recv(4096) == b''  # closed without a reply: there was no complete request
        assert httpx.get(base + settings.health_path).status_code == 200


def test_requests_beyond_the_admission_bound_are_refused_before_they_are_decoded(project_root, model_test_profile,
                                                                                monkeypatch):
    settings, _ = make_settings(project_root, model_test_profile, max_pending_requests=2)
    parsed, original = [], server_module.parse_request

    def counting(body, limits):
        parsed.append(len(body))
        return original(body, limits)

    monkeypatch.setattr(server_module, 'parse_request', counting)
    gate = Gate()
    one_image = request([{'content': [{'type': 'image_url', 'image_url': {'url': data_uri()}}]}])
    with running_server(settings, gate) as (base, engine, _, _):
        answers = []
        holders = [threading.Thread(target=lambda: answers.append(post(base, settings, one_image).status_code))
                   for _ in range(2)]
        for thread in holders:
            thread.start()
        try:
            assert gate.entered.wait(10)  # one request is in the engine, the other waits for the GPU lock
            deadline = time.monotonic() + 10
            while len(parsed) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(parsed) == 2
            refused = [post(base, settings, one_image) for _ in range(3)]
            assert [r.status_code for r in refused] == [503] * 3
            assert all(r.json()['error']['code'] == 'overloaded' for r in refused)
            assert len(parsed) == 2  # the refused bodies were never parsed, so never decoded
            # a refused request is answered without its body: it promises 50 kB and sends nothing
            status, payload = exchange(settings, post_head(settings, 50000))
            assert status == 503 and payload['error']['code'] == 'overloaded' and len(parsed) == 2
            assert httpx.get(base + settings.health_path).status_code == 200  # health needs no slot
        finally:
            gate.release.set()
            for thread in holders:
                thread.join(HELD_S)
        assert answers == [200, 200]
        assert post(base, settings, one_image).status_code == 200  # both slots were released


def test_the_admission_bound_is_what_the_profile_says(project_root, model_test_profile):
    settings, _ = make_settings(project_root, model_test_profile, max_pending_requests=1)
    gate = Gate()
    with running_server(settings, gate) as (base, engine, _, _):
        holder = threading.Thread(target=lambda: post(base, settings, request(['a'])))
        holder.start()
        try:
            assert gate.entered.wait(10)
            assert post(base, settings, request(['a'])).status_code == 503  # a bound of 1 admits one
        finally:
            gate.release.set()
            holder.join(HELD_S)


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '127.0.0.2'])
def test_every_accepted_address_can_bind(project_root, model_test_profile, host):
    """Review R5: an address the validation accepts must bind and serve (IPv6 is refused by the validation)."""
    from dataclasses import replace

    from prompt_enhancer.config import embed_server_address

    settings, _ = make_settings(project_root, model_test_profile)
    assert embed_server_address(f'http://{host}:{settings.port}') == (host, settings.port)  # accepted
    with running_server(replace(settings, host=host)) as (base, engine, stop, result):
        assert httpx.get(base + settings.health_path).status_code == 200
    assert result['reason'] == 'stop'
