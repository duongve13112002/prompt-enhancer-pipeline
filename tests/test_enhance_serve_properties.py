"""Properties of `enhance serve` (T-016 P3, R-001, R-015, R-024, R-026, R-027; docs/test-plan.md, Property).

Generated request bodies, headers and route paths. Each property states one rule: an accepted request reaches the pipeline
exactly as the client wrote it, every refusal is a precise HTTP error with a fixed message and no pipeline call or file
side effect, and no input makes the server answer anything but a well-formed HTTP response. The pure wire functions are
tested directly; the server is tested over real loopback sockets with a labelled stub pipeline that records its calls and
echoes the prompt (`mock_contract`)."""
import asyncio
from contextlib import contextmanager
import json
from threading import Thread

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from cli_fixtures import REQUEST_CAP
from prompt_enhancer import http_wire as wire
from prompt_enhancer.cli_input import Refusal, parse_request
from serve_fixtures import (Live, StubPipeline, StubResponse, exchange, free_port, raw_post, serve_config,
                            serving_settings, store_for, stub_envelope)

PURE = settings(deadline=None, max_examples=300)
NETWORK = settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)), max_size=120)
TOKEN = st.text(alphabet="!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=12)
VALUE = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40)


# --- the wire functions ------------------------------------------------------------------------------------------------

@PURE
@given(data=st.binary(max_size=400))
def test_parse_head_returns_a_head_or_a_wire_error_for_any_bytes(data):
    try:
        head = wire.parse_head(data)
    except wire.WireError as error:
        assert error.status in (400, 431) and error.code in ('bad_request', 'head_too_large')
        assert isinstance(error.message, str) and len(error.message) < 80
    else:
        assert isinstance(head.method, str) and head.target and all(name == name.lower() for name in head.headers)


@PURE
@given(method=TOKEN, target=st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=60),
       headers=st.dictionaries(TOKEN.map(str.lower), VALUE, max_size=8), version=st.sampled_from(['HTTP/1.1', 'HTTP/1.0']))
def test_a_well_formed_head_is_parsed_exactly(method, target, headers, version):
    for name in ('content-length', 'content-type', 'transfer-encoding', 'expect'):
        headers.pop(name, None)  # a repeat of these is refused; one occurrence is covered by the body tests
    raw = (f'{method} {target} {version}\r\n' + ''.join(f'{name}: {value}\r\n' for name, value in headers.items())
           + '\r\n').encode('ascii')
    head = wire.parse_head(raw)
    assert (head.method, head.target) == (method, target)
    assert head.headers == {name: value.strip(' \t') for name, value in headers.items()}


@PURE
@given(target=st.text(max_size=80), method=st.sampled_from(['GET', 'POST', 'PUT', 'DELETE', 'HEAD']))
def test_route_accepts_only_the_exact_routes(target, method):
    try:
        found = wire.route(method, target)
    except wire.WireError as error:
        assert error.status in (404, 405) and (error.allow in ('GET', 'POST')) == (error.status == 405)
        return
    if found.name == wire.HEALTH:
        assert target == '/health' and method == 'GET' and found.params == ()
    elif found.name == wire.ENHANCE:
        assert target == '/v1/enhance' and method == 'POST'
    else:
        assert method == 'GET' and target.startswith('/v1/attachments/') and len(found.params) == 2
        assert all(item and '/' not in item and '\\' not in item for item in found.params)


@PURE
@given(value=st.text(max_size=30), limit=st.integers(min_value=0, max_value=10 ** 12))
def test_body_length_is_the_declared_size_or_a_precise_refusal(value, limit):
    try:
        length = wire.body_length({'content-length': value}, limit, required=True)
    except wire.WireError as error:
        assert error.status in (400, 413) and (error.status == 413) == (
            value.isascii() and value.isdigit() and int(value) > limit)
    else:
        assert value.isascii() and value.isdigit() and length == int(value) <= limit


@PURE
@given(status=st.one_of(st.none(), st.text(max_size=12), st.integers()), extra=st.dictionaries(st.text(max_size=5), st.integers(), max_size=3))
def test_an_envelope_is_carried_by_200_only_when_its_status_is_one_of_the_schemas(status, extra):
    envelope = {**extra, 'status': status}
    if status in wire.ENVELOPE_STATUSES:
        assert wire.http_status_of(envelope) == 200
    else:
        with pytest.raises(ValueError):
            wire.http_status_of(envelope)


@PURE
@given(body=st.binary(max_size=200), status=st.sampled_from([200, 400, 404, 413, 503]))
def test_a_built_response_parses_back_with_its_exact_body(body, status):
    head, _, rest = wire.build_response(status, body).partition(b'\r\n\r\n')
    lines = head.decode('ascii').split('\r\n')
    assert lines[0].startswith(f'HTTP/1.1 {status} ') and f'Content-Length: {len(body)}' in lines and rest == body


# --- the server over real sockets -------------------------------------------------------------------------------------

class Background:
    """One loop on a thread with a `Live` server over a stub pipeline, so a property can run many examples against it."""
    def __init__(self, config, store, root):
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.requests = []

        async def echo(request):
            self.requests.append(request)
            return StubResponse(stub_envelope(output=request['prompt']))
        self.pipeline = StubPipeline(echo)
        self.live = self.call(self._start(config, store, root))
        self.port = self.live.port

    async def _start(self, config, store, root):
        return await Live(serving_settings(config), self.pipeline, store, root).__aenter__()

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(60)

    def close(self):
        report = self.call(self.live.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=30)
        self.loop.close()
        return report


@contextmanager
def background(model_test_profile, tmp_path, project_root, **dotted):
    config = serve_config(model_test_profile, tmp_path, project_root, 1, free_port(), **dotted)
    server = Background(config, store_for(config, project_root), project_root)
    try:
        yield server
    finally:
        report = server.close()
    assert report.clean


def tree(path):
    return sorted(str(item.relative_to(path)) for item in path.rglob('*'))


REQUESTS = st.fixed_dictionaries({'prompt': TEXT, 'task': st.just('generate')},
                                 optional={'profile_id': st.text(min_size=1, max_size=12),
                                           'overrides': st.dictionaries(st.text(max_size=6), st.integers() | TEXT, max_size=3)})


def test_an_accepted_request_reaches_the_pipeline_exactly_as_written(model_test_profile, tmp_path, project_root):
    """R-001: the prompt, in any text and either JSON spelling, comes back from the pipeline unchanged."""
    with background(model_test_profile, tmp_path, project_root) as server:
        @NETWORK
        @given(request=REQUESTS, ascii_only=st.booleans())
        def check(request, ascii_only):
            before = len(server.requests)
            body = json.dumps(request, ensure_ascii=ascii_only).encode('utf-8')
            status, _, answer = exchange(server.port, raw_post('/v1/enhance', body))
            assert status == 200 and len(server.requests) == before + 1
            assert server.requests[-1] == request  # exactly what the client wrote
            assert json.loads(answer)['output'] == request['prompt']
        check()


def test_any_body_is_accepted_or_refused_exactly_as_the_request_reader_decides(model_test_profile, tmp_path, project_root):
    with background(model_test_profile, tmp_path, project_root) as server:
        @NETWORK
        @given(body=st.binary(max_size=300) | st.builds(lambda v: json.dumps(v, ensure_ascii=False).encode('utf-8'),
                                                         st.recursive(st.none() | st.booleans() | st.integers() | TEXT,
                                                                      lambda inner: st.lists(inner, max_size=3) | st.dictionaries(
                                                                          st.sampled_from(['prompt', 'task', 'x', 'source_image']),
                                                                          inner, max_size=3), max_leaves=6)))
        def check(body):
            before, listing = len(server.requests), tree(tmp_path)
            try:
                parse_request(body, project_root)
                expected = None
            except Refusal as refusal:
                expected = str(refusal)
            status, _, answer = exchange(server.port, raw_post('/v1/enhance', body))
            if expected is None:
                assert status == 200 and len(server.requests) == before + 1
            else:
                error = json.loads(answer)['error']
                assert status == 400 and error == {'code': 'invalid_request', 'message': expected}  # the same precise refusal
                assert len(server.requests) == before and tree(tmp_path) == listing  # no call, no file
        check()


HEADER_LINES = st.lists(st.tuples(TOKEN, VALUE), max_size=6)


def test_arbitrary_headers_never_break_the_server_and_a_refusal_has_no_side_effect(model_test_profile, tmp_path, project_root):
    with background(model_test_profile, tmp_path, project_root, **{'serving.http.max_request_bytes': 400}) as server:
        @NETWORK
        @given(lines=HEADER_LINES, length=st.one_of(st.none(), VALUE, st.integers(min_value=0, max_value=900).map(str)),
               kind=st.one_of(st.none(), VALUE), prompt=TEXT)
        def check(lines, length, kind, prompt):
            body = json.dumps({'prompt': prompt, 'task': 'generate'}).encode('utf-8')
            head = 'POST /v1/enhance HTTP/1.1\r\n' + ''.join(f'{name}: {value}\r\n' for name, value in lines)
            if kind is not None:
                head += f'Content-Type: {kind}\r\n'
            if length is not None:
                head += f'Content-Length: {length}\r\n'
            before, listing = len(server.requests), tree(tmp_path)
            status, _, answer = exchange(server.port, (head + '\r\n').encode('ascii') + body, timeout=15)
            assert status in (200, 400, 408, 411, 413, 415, 417, 501), (status, head)  # a response, never a crash or a 500
            if status != 200:
                assert json.loads(answer)['error']['code'] and len(server.requests) == before and tree(tmp_path) == listing
        check()
        assert exchange(server.port, b'GET /health HTTP/1.1\r\n\r\n')[0] == 200  # and it still serves


PATHS = st.one_of(st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=60).map(lambda t: '/' + t),
                  st.just('/health'), st.just('/v1/enhance'),
                  st.tuples(TEXT.filter(lambda t: '/' not in t and ' ' not in t),
                            TEXT.filter(lambda t: '/' not in t and ' ' not in t)).map(
                                lambda ids: f'/v1/attachments/{ids[0]}/{ids[1]}'))


def test_any_route_path_is_served_refused_or_not_found_and_touches_nothing(model_test_profile, tmp_path, project_root):
    with background(model_test_profile, tmp_path, project_root) as server:
        @NETWORK
        @given(target=PATHS, method=st.sampled_from(['GET', 'POST']))
        def check(target, method):
            try:
                request = f'{method} {target} HTTP/1.1\r\nContent-Length: 0\r\n\r\n'.encode('utf-8')
            except UnicodeEncodeError:
                return
            before, listing = len(server.requests), tree(tmp_path)
            status, headers, answer = exchange(server.port, request, timeout=15)
            if (method, target) == ('GET', '/health'):
                assert status == 200
            elif target == '/v1/enhance' and method == 'POST':
                assert status == 415  # no Content-Type: refused before the empty body is read
            else:
                assert status in (400, 404, 405, 431), (method, target, status)  # nothing published: nothing is found
                assert json.loads(answer)['error']['code'] in ('not_found', 'method_not_allowed', 'bad_request', 'head_too_large')
            assert len(server.requests) == before and tree(tmp_path) == listing
        check()
