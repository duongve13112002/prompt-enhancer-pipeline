"""`enhance once` through the real entry point (T-016 P2, R-001, R-004, R-009, R-024, R-026).

Labelled doubles: the loopback model server (`FullServerDouble`) and the profile registration/golden runner of
`activate`. Everything else, the CLI parser, request reader, S0-S6 coordinator, anchored attachment writer and the data
store, is production code. Doubles are `mock_contract` evidence, never a live result."""
import base64
from copy import deepcopy
import json

import pytest

from cli_fixtures import (REQUEST_CAP, KnowledgePlanner, attaching_config, finish_attaching_setup, finish_setup,
                          model_server, pipeline_config, published_knowledge, run_cli, text, write_request, write_yaml)
from prompt_enhancer.storage import open_data_store
from test_planner import png


def once(project_root, tmp_path, config, request, *, name='request.json', extra=()):
    path = write_yaml(project_root, tmp_path, config)
    return run_cli(['enhance', 'once', '--config', path, '--request', write_request(tmp_path, request, name),
                    '--project-root', project_root, *extra], project_root)


def validate_envelope(project_root, envelope):
    """stdout is a schema-valid response envelope (contracts/schemas/response_envelope.v1)."""
    from jsonschema import Draft202012Validator
    schema = json.loads((project_root / 'contracts/schemas/response_envelope.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(envelope)


def request_for(task, prompt='original intent'):
    request = {'prompt': prompt, 'task': task}
    if task == 'edit':
        request['source_image'] = base64.b64encode(png()).decode()
    return request


@pytest.mark.parametrize('task', ['generate', 'edit'])
def test_once_retrieval_off_with_the_knowledge_base_absent(model_test_profile, tmp_path, project_root, task):
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        finish_setup(config, project_root, handler)
        done = once(project_root, tmp_path, config, request_for(task))
        out, err = text(done)
        assert done.returncode == 0, err
        envelope = json.loads(out)
        validate_envelope(project_root, envelope)
        assert out.count('\n') == 1 and envelope['status'] == 'degraded' and envelope['output'] == 'original intent'
        assert envelope['retrieval'] == {'state': 'disabled', 'snapshot_id': None, 'error': None}
        assert envelope['attachments'] == []
        assert handler.seen
    assert not (tmp_path / 'kb').exists()  # retrieval off: no knowledge-base access at all (R-009)
    rows = open_data_store(config, 'enhance once', project_root=project_root).read_rows('request_traces')
    assert [row['request_id'] for row in rows] == [envelope['trace_id']] and rows[0]['status'] == envelope['status']
    assert not (tmp_path / 'data' / 'attachments').exists()


def test_once_preserves_the_prompt_bytes_and_the_system_text(model_test_profile, tmp_path, project_root):
    prompt = 'M\u1ed9t c\u00f4 g\u00e1i m\u1eb7c \u00e1o d\u00e0i \u0111\u1ecf \u65e5\u672c\u8a9e \U0001f338'  # no quote: the double's writer reply keeps no literal text
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        finish_setup(config, project_root, handler)
        done = once(project_root, tmp_path, config, request_for('generate', prompt))
        out, err = text(done)
        assert done.returncode == 0, err
    envelope = json.loads(out)
    assert envelope['output'] == prompt  # the degraded fallback is the user's prompt, byte for byte (R-001)
    rows = open_data_store(config, 'enhance once', project_root=project_root).read_rows('request_traces')
    assert rows[0]['body']['prompt'] == prompt  # and the trace holds the request prompt exactly
    writer_calls = [body for body in handler.seen if body['messages'][0]['content'] == handler.writer_system]
    assert writer_calls  # the admin's system prompt reached the writer verbatim (R-004)


SECRET = 'PRIVATE-PROMPT-MARKER'  # a request value that must never appear in a refusal message (R-026)
GOOD = {'prompt': SECRET, 'task': 'generate'}
REFUSALS = {
    'missing_file': None,
    'malformed_json': b'{"prompt": "' + SECRET.encode() + b'", ',
    'not_utf8': b'{"prompt": "\xff\xfe' + SECRET.encode() + b'", "task": "generate"}',
    'duplicate_key': b'{"prompt": "a", "prompt": "' + SECRET.encode() + b'", "task": "generate"}',
    'not_an_object': b'["' + SECRET.encode() + b'"]',
    'unknown_field': {**GOOD, 'extra': SECRET},
    'edit_without_source': {'prompt': SECRET, 'task': 'edit'},
    'generate_with_source': {**GOOD, 'source_image': base64.b64encode(png()).decode()},
    'bad_base64': {'prompt': SECRET, 'task': 'edit', 'source_image': '***' + SECRET},
    'oversize': {'prompt': SECRET * 20, 'task': 'generate'},
}


@pytest.mark.parametrize('case', sorted(REFUSALS))
def test_once_refuses_a_bad_request_before_any_side_effect(model_test_profile, tmp_path, project_root, case):
    body = REFUSALS[case]
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        config['serving']['http']['max_request_bytes'] = 300  # test-only: the oversize case is a request above it
        finish_setup(config, project_root, handler)
        calls_before = len(handler.seen)
        path = write_yaml(project_root, tmp_path, config)
        request = tmp_path / 'raw.json'
        if isinstance(body, bytes):
            request.write_bytes(body)
        elif body is not None:
            request = write_request(tmp_path, body)
        before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
        done = run_cli(['enhance', 'once', '--config', path, '--request', request, '--project-root', project_root],
                       project_root)
        out, err = text(done)
        assert len(handler.seen) == calls_before  # no model call
    assert done.returncode == 2 and out == '' and err.startswith('refused: '), (done.returncode, err)
    assert SECRET not in err  # the refusal names the field and keyword, never the value
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before  # no file, store or trace created


@pytest.mark.parametrize('where', ['outside_root', 'directory'])
def test_once_refuses_a_request_path_outside_the_root_or_not_a_file(model_test_profile, tmp_path, project_root, where):
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        path = write_yaml(project_root, tmp_path, config)
        target = tmp_path
        if where == 'outside_root':  # a valid request that exists, so only containment can refuse it
            target = project_root.parent / 'elsewhere.json'
            target.write_bytes(json.dumps(GOOD).encode('utf-8'))
        try:
            done = run_cli(['enhance', 'once', '--config', path, '--request', target, '--project-root', project_root],
                           project_root)
        finally:
            if where == 'outside_root':
                target.unlink()
        assert not handler.seen
    out, err = text(done)
    assert done.returncode == 2 and out == '' and err.startswith('refused: --request: ')
    assert not (tmp_path / 'data').exists()


def test_once_refuses_configuration_gates_before_the_request_is_read(model_test_profile, tmp_path, project_root):
    gates = {}
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        del config['serving']['http']
        gates['missing_limit'] = deepcopy(config)
        config['serving']['http'] = {'max_request_bytes': 'deploy_required'}
        gates['sentinel_limit'] = deepcopy(config)
        config['serving']['http'] = {'max_request_bytes': True}
        gates['boolean_limit'] = deepcopy(config)
        config['serving']['http'] = {'max_request_bytes': 0}
        gates['zero_limit'] = deepcopy(config)
        config['serving']['http'] = {'max_request_bytes': REQUEST_CAP}
        config['schema_version'] = 1
        gates['old_version'] = deepcopy(config)
        request = write_request(tmp_path, GOOD)
        for name, broken in gates.items():
            path = write_yaml(project_root, tmp_path, broken, f'{name}.yaml')
            done = run_cli(['enhance', 'once', '--config', path, '--request', request, '--project-root', project_root],
                           project_root)
            out, err = text(done)
            assert done.returncode == 2 and out == '' and err.startswith('refused: '), (name, err)
        assert not handler.seen and not (tmp_path / 'data').exists()


def test_once_limit_override_changes_what_is_refused(model_test_profile, tmp_path, project_root):
    """`serving.http.max_request_bytes` is the byte cap: the same file is refused below it and accepted at it."""
    with model_server() as (handler, port):
        config = pipeline_config(model_test_profile, tmp_path, project_root, port)
        finish_setup(config, project_root, handler)
        size = len(write_request(tmp_path, GOOD).read_bytes())
        config['serving']['http']['max_request_bytes'] = size - 1
        done = once(project_root, tmp_path, config, GOOD)
        assert done.returncode == 2 and 'serving.http.max_request_bytes' in text(done)[1] and not handler.seen
        config['serving']['http']['max_request_bytes'] = size
        done = once(project_root, tmp_path, config, GOOD)
        assert done.returncode == 0, text(done)[1]


def test_once_deletes_expired_attachment_copies_on_the_next_run(k2_env, model_test_profile, tmp_path, project_root):
    import time
    originals = published_knowledge(k2_env)
    with model_server(KnowledgePlanner) as (handler, port):
        config = attaching_config(k2_env, model_test_profile, tmp_path, project_root, port, retention_s=1)
        finish_attaching_setup(config, project_root, handler)
        first = json.loads(text(once(project_root, tmp_path, config, request_for('edit', 'Name')))[0])
        kept = tmp_path / 'data' / 'attachments' / first['trace_id']
        assert kept.is_dir() and first['attachments']
        time.sleep(1.5)  # past the retention in the index
        untouched = tmp_path / 'data' / 'attachments' / ('f' * 32)  # an unindexed, non-empty directory is never guessed at
        untouched.mkdir()
        (untouched / 'stray').write_bytes(b'x')
        done = once(project_root, tmp_path, config, request_for('generate', 'Name'))
        assert done.returncode == 0, text(done)[1]
        assert 'warning' not in text(done)[1]  # the stray directory is skipped, not counted as a failed removal
    assert not kept.exists()  # expired by its index, removed with every file
    assert (untouched / 'stray').read_bytes() == b'x'
    assert set(originals) >= {item['image_id'] for item in first['attachments']}


def test_publication_writes_exact_bytes_refuses_a_mismatch_and_never_merges_a_trace(model_test_profile, tmp_path,
                                                                                    project_root):
    import hashlib
    from datetime import datetime, timedelta, timezone
    from prompt_enhancer.attachments import attachment_path, expire_attachments, publish_attachments
    from prompt_enhancer.storage import StorageError
    config = pipeline_config(model_test_profile, tmp_path, project_root, 1)
    store = open_data_store(config, 'enhance once', project_root=project_root)
    data = b'\x89PNG labelled bytes'
    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(seconds=60)
    with pytest.raises(StorageError):  # bytes that are not the named image: nothing is written
        publish_attachments(store, 'a' * 32, {image_id: data + b'x'}, expires)
    assert not (tmp_path / 'data' / 'attachments' / ('a' * 32)).exists()
    for bad in ('sha256:xyz', 'md5:' + 'a' * 32, image_id.upper(), '../' + image_id):
        with pytest.raises(StorageError):
            attachment_path('a' * 32, bad)
    with pytest.raises(StorageError):  # a naive expiry could never be compared
        publish_attachments(store, 'a' * 32, {image_id: data}, datetime.now())
    (written,) = publish_attachments(store, 'a' * 32, {image_id: data}, expires)
    assert (tmp_path / 'data' / written).read_bytes() == data
    with pytest.raises(FileExistsError):  # the trace's directory exists: refused, never merged or overwritten
        publish_attachments(store, 'a' * 32, {image_id: data}, expires)
    assert (tmp_path / 'data' / written).read_bytes() == data  # the failed second call removed nothing of the first
    assert expire_attachments(store, now=expires - timedelta(seconds=1)) == (0, 0)  # not yet expired
    assert (tmp_path / 'data' / written).read_bytes() == data
    assert expire_attachments(store, now=expires + timedelta(seconds=1)) == (1, 0)
    assert not (tmp_path / 'data' / 'attachments' / ('a' * 32)).exists()


def run_in_process(monkeypatch, capsys, k2_env, model_test_profile, tmp_path, project_root, patch):
    """`run_once` inside this process, so a failure can be injected after the pipeline has produced a response."""
    from types import SimpleNamespace
    from prompt_enhancer import cli_enhance
    from prompt_enhancer.pipeline import PipelineResponse
    published_knowledge(k2_env)
    with model_server(KnowledgePlanner) as (handler, port):
        config = attaching_config(k2_env, model_test_profile, tmp_path, project_root, port)
        finish_attaching_setup(config, project_root, handler)
        args = SimpleNamespace(config=str(write_yaml(project_root, tmp_path, config)), overlay=None,
                               request=str(write_request(tmp_path, request_for('edit', 'Name'))))
        events = []
        real_after = PipelineResponse.after_delivery

        async def spy(self):  # labelled spy: records the acknowledgement, then runs the production one
            events.append('acknowledged')
            await real_after(self)

        monkeypatch.setattr(PipelineResponse, 'after_delivery', spy)
        patch(monkeypatch, cli_enhance, events)
        code = cli_enhance.run_once(args, project_root)
    return code, capsys.readouterr(), events, tmp_path / 'data' / 'attachments'


def test_once_acknowledges_only_after_the_envelope_was_written(monkeypatch, capsys, k2_env, model_test_profile,
                                                                tmp_path, project_root):
    def spy_emit(monkeypatch, module, events):
        real = module.emit_json
        monkeypatch.setattr(module, 'emit_json', lambda result: (events.append('written'), real(result))[1])

    code, captured, events, attachments = run_in_process(monkeypatch, capsys, k2_env, model_test_profile, tmp_path,
                                                         project_root, spy_emit)
    assert code == 0 and events == ['written', 'acknowledged']
    envelope = json.loads(captured.out)
    assert (attachments / envelope['trace_id'] / 'index.json').is_file()  # copies exist before the envelope names them


def test_once_does_not_acknowledge_when_stdout_cannot_be_written(monkeypatch, capsys, k2_env, model_test_profile,
                                                                 tmp_path, project_root):
    def closed_stdout(monkeypatch, module, events):
        def broken(result):
            raise BrokenPipeError('labelled closed stdout')
        monkeypatch.setattr(module, 'emit_json', broken)

    code, captured, events, attachments = run_in_process(monkeypatch, capsys, k2_env, model_test_profile, tmp_path,
                                                         project_root, closed_stdout)
    assert code == 1 and captured.out == '' and 'failed: delivery (BrokenPipeError)' in captured.err
    assert events == []  # R-024: an undelivered response is never acknowledged


def test_once_does_not_deliver_or_acknowledge_when_attachment_publication_fails(
        monkeypatch, capsys, k2_env, model_test_profile, tmp_path, project_root):
    from prompt_enhancer.storage import StorageError

    def failing_publication(monkeypatch, module, events):
        def broken(*args):
            raise StorageError('labelled publication failure')
        monkeypatch.setattr(module, 'publish_attachments', broken)

    code, captured, events, attachments = run_in_process(monkeypatch, capsys, k2_env, model_test_profile, tmp_path,
                                                         project_root, failing_publication)
    assert code == 1 and captured.out == ''  # no envelope names an image that has no copy
    assert 'failed: delivery (StorageError)' in captured.err and events == []
    assert not attachments.exists() or not any(attachments.iterdir())


def test_once_generate_with_retrieval_on_uses_the_published_snapshot_and_attaches_nothing(
        k2_env, model_test_profile, tmp_path, project_root):
    published_knowledge(k2_env)
    with model_server(KnowledgePlanner) as (handler, port):
        config = attaching_config(k2_env, model_test_profile, tmp_path, project_root, port)
        finish_attaching_setup(config, project_root, handler)
        done = once(project_root, tmp_path, config, request_for('generate', 'Name'))
        out, err = text(done)
    assert done.returncode == 0, err
    envelope = json.loads(out)
    validate_envelope(project_root, envelope)
    assert envelope['retrieval']['state'] == 'retrieved' and envelope['retrieval']['snapshot_id']
    assert envelope['attachments'] == [] and envelope['warnings'] == []  # only an edit attaches reference images
    assert not (tmp_path / 'data' / 'attachments').exists()


def test_once_delivers_retained_attachments_as_hash_equal_files(k2_env, model_test_profile, tmp_path, project_root):
    originals = published_knowledge(k2_env)
    with model_server(KnowledgePlanner) as (handler, port):
        config = attaching_config(k2_env, model_test_profile, tmp_path, project_root, port)
        finish_attaching_setup(config, project_root, handler)
        done = once(project_root, tmp_path, config, request_for('edit', 'Name'))  # the planner mention must occur in the prompt
        out, err = text(done)
    assert done.returncode == 0, err
    envelope = json.loads(out)
    validate_envelope(project_root, envelope)
    assert envelope['retrieval']['state'] == 'retrieved'  # status is `degraded` as in every double-writer run: the writer double is not the verifier's text
    delivered = {item['image_id'] for item in envelope['attachments']}
    assert delivered and delivered <= set(originals) and envelope['warnings'] == []
    folder_path = tmp_path / 'data' / 'attachments' / envelope['trace_id']
    assert sorted(path.name for path in folder_path.iterdir()) == sorted(['index.json', *(key[7:] for key in delivered)])
    for image_id in delivered:
        assert (folder_path / image_id[7:]).read_bytes() == originals[image_id]  # hash-equal, byte for byte (KBS-F3)
    index = json.loads((folder_path / 'index.json').read_bytes())
    assert index['trace_id'] == envelope['trace_id'] and {item['image_id'] for item in index['images']} == delivered
    rows = open_data_store(config, 'enhance once', project_root=project_root).read_rows('request_traces')
    assert rows[0]['body']['attachments_retained'] is True
