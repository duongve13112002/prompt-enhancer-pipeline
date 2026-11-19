"""`enhance serve` through the real entry point (T-016 P3, R-001, R-004, R-009, R-018, R-024, R-026, R-027).

Labelled doubles: the loopback model server (`FullServerDouble`, held and released by `Gate`) and the profile
registration/golden runner of `activate`. The CLI parser, configuration, HTTP server, S0-S6 coordinator, anchored
attachment writer and the data store are production code. Doubles are `mock_contract` evidence, never a live result.

The server runs as a hidden child process (`python -m prompt_enhancer enhance serve`) and is stopped cooperatively:
SIGTERM or SIGINT on POSIX, Ctrl+C or Ctrl+Break on Windows through `console_ctrl.py` (see `ServeProcess.interrupt`).
A forced kill is only the teardown fallback and fails the test that needed it."""
import asyncio
import json
import socket
import time

import pytest

from cli_fixtures import (KnowledgePlanner, attaching_config, finish_attaching_setup, finish_setup, model_server,
                          published_knowledge, run_cli, text)
from prompt_enhancer.storage import open_data_store
from serve_fixtures import (Gate, ServeProcess, add_serving, aexchange, aopen, exchange, free_port, get,
                            held_double, in_threads, listener_is_closed, post_json, raw_post, serve_config, serving,
                            write_config)
from test_cli_enhance import request_for, validate_envelope


def rows_of(config, project_root):
    return open_data_store(config, 'enhance serve', project_root=project_root).read_rows('request_traces')


def test_serve_delivers_generate_and_edit_envelopes_unchanged_and_stops_cleanly(model_test_profile, tmp_path, project_root):
    prompt = 'Một cô gái mặc áo dài đỏ 日本語 \U0001f338'  # no quote: the double keeps no literal text
    with model_server() as (handler, model_port):
        port_chosen = free_port()
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, port_chosen)
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        with serving(project_root, path) as (process, port):
            assert port == port_chosen  # the port comes from the profile, there is no flag
            status, headers, body = get(port, '/health')
            assert status == 200 and json.loads(body) == {'status': 'ok'} and headers['connection'] == 'close'
            envelopes = {}
            for task in ('generate', 'edit'):
                status, headers, body = post_json(port, request_for(task, prompt))
                assert status == 200, body
                assert headers['content-type'] == 'application/json; charset=utf-8'
                envelopes[task] = json.loads(body)
                validate_envelope(project_root, envelopes[task])
                assert envelopes[task]['output'] == prompt  # the degraded fallback is the prompt, byte for byte (R-001)
                assert envelopes[task]['retrieval'] == {'state': 'disabled', 'snapshot_id': None}
            assert process.stop() == 0
        assert process.stdout == b'' and process.lines[0].startswith('listening on 127.0.0.1:')
        assert not any(line.startswith(('failed', 'operational')) for line in process.lines)
        assert listener_is_closed(port_chosen)
        writer_calls = [call for call in handler.seen if call['messages'][0]['content'] == handler.writer_system]
        assert len(writer_calls) == 2  # the admin's system prompt reached the writer verbatim (R-004)
    rows = rows_of(config, project_root)
    assert {row['request_id']: row['status'] for row in rows} == {
        item['trace_id']: item['status'] for item in envelopes.values()}  # every envelope has its trace, none was altered
    assert {row['body']['prompt'] for row in rows} == {prompt}
    assert not (tmp_path / 'kb').exists()  # retrieval off: no knowledge-base access at all (R-009)


SECRET = 'PRIVATE-PROMPT-MARKER'  # a request value that must never appear in a response (R-026)
HEAD = b'POST /v1/enhance HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
JSON_BODY = json.dumps({'prompt': SECRET, 'task': 'generate'}).encode()
TRANSPORT_CASES = {
    'unknown_path': (b'GET /nope HTTP/1.1\r\nHost: x\r\n\r\n', 404, 'not_found'),
    'query_on_a_route': (b'GET /health?x=1 HTTP/1.1\r\nHost: x\r\n\r\n', 404, 'not_found'),
    'dot_segments': (b'GET /v1/../health HTTP/1.1\r\nHost: x\r\n\r\n', 404, 'not_found'),
    'method_on_health': (b'POST /health HTTP/1.1\r\nContent-Length: 0\r\n\r\n', 405, 'method_not_allowed'),
    'method_on_enhance': (b'GET /v1/enhance HTTP/1.1\r\nHost: x\r\n\r\n', 405, 'method_not_allowed'),
    'no_length': (HEAD + b'\r\n', 411, 'length_required'),
    'chunked': (HEAD + b'Transfer-Encoding: chunked\r\n\r\n', 501, 'not_implemented'),
    'bad_length': (HEAD + b'Content-Length: 12x\r\n\r\n', 400, 'bad_request'),
    'repeated_length': (HEAD + b'Content-Length: 1\r\nContent-Length: 1\r\n\r\n1', 400, 'bad_request'),
    'huge_length': (HEAD + b'Content-Length: ' + b'9' * 40 + b'\r\n\r\n', 413, 'payload_too_large'),
    'oversize': (raw_post('/v1/enhance', JSON_BODY + b' ' * 400), 413, 'payload_too_large'),
    'wrong_type': (raw_post('/v1/enhance', JSON_BODY, content_type='text/plain'), 415, 'unsupported_media_type'),
    'other_charset': (raw_post('/v1/enhance', JSON_BODY, content_type='application/json; charset=latin-1'), 415,
                      'unsupported_media_type'),
    'not_json': (raw_post('/v1/enhance', b'{"prompt": "' + SECRET.encode() + b'", '), 400, 'invalid_request'),
    'not_utf8': (raw_post('/v1/enhance', b'{"prompt": "\xff\xfe' + SECRET.encode() + b'", "task": "generate"}'), 400,
                 'invalid_request'),
    'duplicate_key': (raw_post('/v1/enhance', b'{"prompt": "a", "prompt": "' + SECRET.encode() + b'", "task": "generate"}'),
                      400, 'invalid_request'),
    'unknown_field': (raw_post('/v1/enhance', json.dumps({'prompt': 'p', 'task': 'generate', 'extra': SECRET}).encode()),
                      400, 'invalid_request'),
    'edit_without_source': (raw_post('/v1/enhance', json.dumps({'prompt': SECRET, 'task': 'edit'}).encode()), 400,
                            'invalid_request'),
    'binary_head': (b'GET /health HTTP/1.1\r\nX: \xff\r\n\r\n', 400, 'bad_request'),
    'bad_version': (b'GET /health HTTP/9.9\r\n\r\n', 400, 'bad_request'),
    'head_too_large': (b'GET /health HTTP/1.1\r\nX: ' + b'a' * 20000 + b'\r\n\r\n', 431, 'head_too_large'),
    'unsupported_expectation': (HEAD + b'Expect: magic\r\nContent-Length: 2\r\n\r\n{}', 417, 'expectation_failed'),
}


def test_serve_refuses_transport_and_request_errors_precisely_and_touches_nothing(model_test_profile, tmp_path, project_root):
    with model_server() as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.http.max_request_bytes': 300})  # test-only: the oversize case is above it
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        calls_before = len(handler.seen)
        with serving(project_root, path) as (process, port):
            before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*'))
            for name, (payload, status, code) in TRANSPORT_CASES.items():
                got, headers, body = exchange(port, payload)
                assert got == status, (name, got, body)
                error = json.loads(body)
                assert list(error) == ['error'] and error['error']['code'] == code, (name, error)  # not an S6 envelope
                assert SECRET not in body.decode(), name  # a refusal names the field and keyword, never the value
                if status == 405:
                    assert headers['allow'] in ('GET', 'POST')
            assert len(handler.seen) == calls_before  # no model call for any of them
            assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*')) == before  # nothing created
            assert get(port, '/health')[0] == 200  # and the server is still serving
            assert process.stop() == 0
        assert not rows_of(config, project_root)


def test_serve_active_limit_is_exact_through_the_real_pipeline(model_test_profile, tmp_path, project_root):
    """serving.max_concurrency active requests are held inside the model double (peak equals the limit); the next one is
    refused 503 before any model call; after the release the places are free again."""
    gate = Gate()
    with model_server(held_double(gate)) as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.max_concurrency': 2, 'serving.queue_limit': 0,
                                 'serving.http.max_connections': 4})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        try:
            with serving(project_root, path) as (process, port):
                with in_threads(2) as pool:
                    held = [pool.submit(post_json, port, request_for('generate')) for _ in range(2)]
                    gate.wait_for(2)
                    status, _, body = post_json(port, request_for('generate'))
                    assert status == 503 and json.loads(body)['error']['code'] == 'overloaded'
                    assert gate.peak == 2 and gate.entered == 2  # the refused request never reached the model
                    gate.release()
                    assert [future.result(timeout=60)[0] for future in held] == [200, 200]
                assert post_json(port, request_for('generate'))[0] == 200  # no leaked place
                assert gate.peak == 2
                assert process.stop() == 0
        finally:
            gate.release()
    assert len(rows_of(config, project_root)) == 3


def test_serve_connection_limit_is_exact_and_frees_with_the_connection(model_test_profile, tmp_path, project_root):
    with model_server() as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.max_concurrency': 1, 'serving.queue_limit': 1,
                                 'serving.http.max_connections': 2})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        with serving(project_root, path) as (process, port):
            idle = [socket.create_connection(('127.0.0.1', port)) for _ in range(2)]  # connected, nothing sent
            status, _, body = get(port, '/health')
            assert status == 503 and json.loads(body)['error']['code'] == 'overloaded'
            idle.pop().close()
            deadline = time.monotonic() + 30
            while (status := get(port, '/health')[0]) == 503:  # the server notices the close, then the place is free
                assert time.monotonic() < deadline, 'the connection place was never freed'
                time.sleep(0.05)
            assert status == 200
            idle.pop().close()
            assert process.stop() == 0


def test_serve_trace_capacity_overflow_is_503_and_leaves_no_orphan_reservation(model_test_profile, tmp_path, project_root):
    gate = Gate()
    with model_server(held_double(gate)) as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'runtime.trace.queue_limit': 1, 'serving.max_concurrency': 2, 'serving.queue_limit': 1,
                                 'serving.http.max_connections': 4})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        try:
            with serving(project_root, path) as (process, port):
                with in_threads(1) as pool:
                    first = pool.submit(post_json, port, request_for('generate'))
                    gate.wait_for(1)  # the first holds the only trace reservation
                    status, _, body = post_json(port, request_for('generate'))
                    error = json.loads(body)['error']
                    assert status == 503 and error['code'] == 'overloaded' and 'trace capacity' in error['message']
                    assert gate.entered == 1  # refused before any model call
                    gate.release()
                    assert first.result(timeout=60)[0] == 200
                for _ in range(100):  # the first trace write ends on its own thread; the place must come back
                    if (status := post_json(port, request_for('generate'))[0]) != 503:
                        break
                    time.sleep(0.05)
                assert status == 200  # the refused request left no reservation behind
                assert process.stop() == 0
        finally:
            gate.release()
    assert len(rows_of(config, project_root)) == 2


def test_serve_slow_body_hits_the_read_timeout_and_is_never_admitted(model_test_profile, tmp_path, project_root):
    with model_server() as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.http.read_timeout_s': 1})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        calls_before = len(handler.seen)
        with serving(project_root, path) as (process, port):
            body = json.dumps(request_for('generate')).encode()
            started = time.monotonic()
            status, _, answer = exchange(port, raw_post('/v1/enhance', body)[:-4])  # four bytes short, then silence
            assert status == 408 and json.loads(answer)['error']['code'] == 'request_timeout'
            assert time.monotonic() - started < 30
            assert len(handler.seen) == calls_before
            assert get(port, '/health')[0] == 200
            assert process.stop() == 0
        assert not rows_of(config, project_root)


@pytest.mark.parametrize('kind', ['default', 'other'])
def test_serve_stops_cooperatively_when_idle(model_test_profile, tmp_path, project_root, kind):
    """SIGTERM or SIGINT (POSIX), Ctrl+C or Ctrl+Break (Windows): exit 0, no listener, no survivor, nothing forced."""
    with model_server() as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port())
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        process = ServeProcess(project_root, path)
        try:
            port = process.wait_ready()
            assert get(port, '/health')[0] == 200
            assert process.stop(kind=kind) == 0
            assert not process.forced
        finally:
            process.kill()
        assert not process.forced and listener_is_closed(port)
        assert [line.split(':')[0] for line in process.lines] == ['listening on 127.0.0.1', 'stopping', 'stopped']
        assert process.proc.poll() == 0


@pytest.mark.parametrize('sent', [b'', b'POST /v1/enhance HTTP/1.1\r\nHost: x\r\n'])
def test_serve_stop_with_an_idle_connection_is_clean_and_counts_no_cancelled_request(model_test_profile, tmp_path,
                                                                                       project_root, sent):
    """F1: a connection that never sent a request (a pre-connecting client or load balancer, or a head still arriving)
    is closed by the stop, is not work, and does not turn a normal stop into an overrun. The read timeout is longer than
    the stop bound, so only the stop can end it: before the fix this stop took the whole bound and exited 1."""
    with model_server() as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.http.read_timeout_s': 30, 'serving.http.shutdown_timeout_s': 1})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        process = ServeProcess(project_root, path)
        try:
            port = process.wait_ready()
            with socket.create_connection(('127.0.0.1', port), timeout=30) as idle:
                if sent:
                    idle.sendall(sent)
                assert get(port, '/health')[0] == 200  # the server is serving while the idle connection is open
                assert process.stop() == 0 and not process.forced, process.lines
                idle.settimeout(30)
                try:
                    answered = idle.recv(65536)
                except ConnectionResetError:  # unread head bytes make the close a reset: still nothing was answered
                    answered = b''
                assert answered == b''  # closed by the stop
        finally:
            process.kill()
        assert not process.forced and listener_is_closed(port)
        assert 'stopped: cancelled requests 0' in process.lines
        assert not any(line.startswith(('failed', 'operational')) for line in process.lines)


def test_serve_stop_cancels_a_request_that_outlasts_the_bound_and_reports_the_failure(model_test_profile, tmp_path,
                                                                                       project_root):
    """Q5: a request still running after `serving.http.shutdown_timeout_s` is cancelled and joined; the exit status is 1,
    never a clean stop, and no trace row or reservation is left for it."""
    gate = Gate()
    with model_server(held_double(gate)) as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.http.shutdown_timeout_s': 1})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        process = ServeProcess(project_root, path)
        try:
            port = process.wait_ready()
            with in_threads(1) as pool:
                running = pool.submit(post_json, port, request_for('generate'))
                gate.wait_for(1)
                code = process.stop()
                assert code == 1 and not process.forced, (code, process.lines)
                assert running.result(timeout=60)[0] is None  # the connection was closed, nothing was answered
        finally:
            gate.release()
            process.kill()
        assert not process.forced and listener_is_closed(port)
        assert any('stop overran serving.http.shutdown_timeout_s (cancelled requests 1' in line for line in process.lines)
    assert not rows_of(config, project_root)


def test_serve_stop_lets_a_running_request_finish_within_the_bound(model_test_profile, tmp_path, project_root):
    gate = Gate()
    with model_server(held_double(gate)) as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.http.shutdown_timeout_s': 30})
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        process = ServeProcess(project_root, path)
        try:
            port = process.wait_ready()
            with in_threads(1) as pool:
                running = pool.submit(post_json, port, request_for('generate'))
                gate.wait_for(1)
                process.interrupt()
                deadline = time.monotonic() + 60
                while not any(line.startswith('stopping') for line in process.lines):  # the stop has begun
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                gate.release()
                status, _, body = running.result(timeout=60)
                assert status == 200 and validate_envelope(project_root, json.loads(body)) is None
            assert process.wait() == 0 and not process.forced
        finally:
            gate.release()
            process.kill()
        assert listener_is_closed(port)
    assert len(rows_of(config, project_root)) == 1


def test_serve_exits_1_when_the_port_is_taken_and_leaves_nothing_behind(model_test_profile, tmp_path, project_root):
    with model_server() as (handler, model_port), socket.socket() as holder:
        holder.bind(('127.0.0.1', 0))
        holder.listen()
        taken = holder.getsockname()[1]
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, taken)
        finish_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        calls_before = len(handler.seen)
        process = ServeProcess(project_root, path)
        try:
            assert process.wait(60) == 1
        finally:
            process.kill()
        assert not process.forced and process.stdout == b''
        assert process.lines == [f'failed: cannot listen on 127.0.0.1:{taken} (OSError)']  # no traceback, no ready line
        assert len(handler.seen) == calls_before
    assert not rows_of(config, project_root)


@pytest.mark.parametrize('leaf, value', [('serving.host', 'deploy_required'), ('serving.port', 'deploy_required'),
                                         ('serving.port', 0), ('serving.port', 65536),
                                         ('serving.http.max_connections', 1)])
def test_serve_refuses_a_bad_serving_value_before_it_listens(model_test_profile, tmp_path, project_root, leaf, value):
    with model_server() as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'serving.max_concurrency': 2, 'serving.queue_limit': 1, leaf: value})
        path = write_config(project_root, tmp_path, config)
        before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*'))
        done = run_cli(['enhance', 'serve', '--config', path, '--project-root', project_root], project_root, timeout=60)
        out, err = text(done)
        assert done.returncode == 2 and out == '' and err.startswith('refused: ') and leaf in err, (done.returncode, err)
        assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob('*')) == before
        assert not handler.seen


# --- attachments -------------------------------------------------------------------------------------------------------

def test_serve_publishes_attachments_before_delivery_and_serves_only_that_index(k2_env, model_test_profile, tmp_path,
                                                                               project_root):
    originals = published_knowledge(k2_env)
    base = tmp_path / 'data' / 'attachments'
    for name, expires in (('r-old', '2000-01-01T00:00:00+00:00'), ('r-live', '2999-01-01T00:00:00+00:00')):
        (base / name).mkdir(parents=True)  # one set already expired, one still live, before the server starts
        (base / name / 'index.json').write_text(json.dumps({'trace_id': name, 'expires_at': expires, 'images': []}))
    with model_server(KnowledgePlanner) as (handler, model_port):
        config = add_serving(attaching_config(k2_env, model_test_profile, tmp_path, project_root, model_port), free_port())
        finish_attaching_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        with serving(project_root, path) as (process, port):
            assert sorted(item.name for item in base.iterdir()) == ['r-live']  # the expired set was removed at start
            status, _, body = post_json(port, request_for('edit', 'Name'))
            assert status == 200, body
            envelope = json.loads(body)
            validate_envelope(project_root, envelope)
            delivered = [item['image_id'] for item in envelope['attachments']]
            assert delivered and set(delivered) <= set(originals) and envelope['warnings'] == []
            for image_id in delivered:  # the bytes the envelope names are served exactly, and only through its index
                status, headers, data = get(port, f'/v1/attachments/{envelope["trace_id"]}/{image_id}')
                assert status == 200 and data == originals[image_id]
                assert headers['content-type'] == 'application/octet-stream'
            stranger = next(iter(set(originals) - set(delivered)), 'sha256:' + '0' * 64)
            for path_ in (f'/v1/attachments/{"f" * 32}/{delivered[0]}', f'/v1/attachments/{envelope["trace_id"]}/{stranger}',
                          f'/v1/attachments/{envelope["trace_id"]}/index.json', '/v1/attachments/r-live/' + delivered[0],
                          f'/v1/attachments/..%2f..%2fserve.yaml/{delivered[0]}'):
                status, _, body = get(port, path_)
                assert status == 404 and json.loads(body)['error']['code'] == 'not_found', path_
            assert process.stop() == 0
    rows = rows_of(config, project_root)
    assert rows[0]['body']['attachments_retained'] is True


def test_serve_publication_failure_is_an_error_not_a_delivery(k2_env, model_test_profile, tmp_path, project_root):
    published_knowledge(k2_env)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'attachments').write_bytes(b'a file where the attachment directory must be')
    with model_server(KnowledgePlanner) as (handler, model_port):
        config = add_serving(attaching_config(k2_env, model_test_profile, tmp_path, project_root, model_port), free_port())
        finish_attaching_setup(config, project_root, handler)
        path = write_config(project_root, tmp_path, config)
        with serving(project_root, path) as (process, port):
            status, _, body = post_json(port, request_for('edit', 'Name'))
            assert status == 500 and json.loads(body) == {'error': {
                'code': 'delivery_failed', 'message': 'the response could not be prepared for delivery'}}
            assert get(port, '/health')[0] == 200
            assert process.stop() == 0
        assert any(line.startswith('delivery failed: ') for line in process.lines)


# --- the real pipeline in this process: acknowledgement and cancellation ----------------------------------------------

def test_the_real_pipeline_is_acknowledged_once_per_delivered_response_and_not_for_a_client_that_left(
        model_test_profile, tmp_path, project_root, monkeypatch):
    from prompt_enhancer.pipeline import Pipeline, PipelineResponse
    from serve_fixtures import Live, serving_settings, store_for
    gate = Gate()
    events = []
    real_after = PipelineResponse.after_delivery

    async def spy(self):  # labelled spy: records the acknowledgement, then runs the production one
        events.append(self.envelope['trace_id'])
        await real_after(self)
    monkeypatch.setattr(PipelineResponse, 'after_delivery', spy)
    with model_server(held_double(gate)) as (handler, model_port):
        config = serve_config(model_test_profile, tmp_path, project_root, model_port, free_port(),
                              **{'runtime.trace.queue_limit': 1})
        finish_setup(config, project_root, handler)
        store = store_for(config, project_root)

        async def scenario():
            loop = asyncio.get_running_loop()
            async with Pipeline(config, project_root=project_root) as pipeline:
                async with Live(serving_settings(config), pipeline, store, project_root) as live:
                    lifecycle = pipeline.lifecycle
                    reader, writer = await aopen(live.port)  # a client that leaves while the model call is held
                    writer.write(raw_post('/v1/enhance', json.dumps(request_for('generate')).encode()))
                    await loop.run_in_executor(None, gate.wait_for, 1)
                    assert lifecycle._used == 1  # the reservation is taken
                    writer.close()
                    for _ in range(2000):
                        if lifecycle._used == 0 and live.server.open == 0:
                            break
                        await asyncio.sleep(0.01)
                    assert lifecycle._used == 0 and not lifecycle._reserved and events == []  # freed, not acknowledged
                    gate.release()
                    status, _, body = await aexchange(live.port, raw_post(
                        '/v1/enhance', json.dumps(request_for('generate')).encode()))
                    assert status == 200
                    for _ in range(2000):
                        if events:
                            break
                        await asyncio.sleep(0.01)
                    assert events == [json.loads(body)['trace_id']]  # acknowledged once, for the delivered response
        try:
            asyncio.run(asyncio.wait_for(scenario(), 120))
        finally:
            gate.release()
    assert [row['request_id'] for row in rows_of(config, project_root)] == events  # the cancelled request left no row
