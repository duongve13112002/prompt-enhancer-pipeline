"""Production S0-S6 entry point; only the loopback model server/profile setup are labelled doubles."""
import asyncio
import base64
from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
from threading import Thread

import pytest

from intake_fixtures import activate, configuration
from assembly_fixtures import assembly_config
from k5_fixtures import published
from storage_fixtures import dataset
from prompt_enhancer.candidates import query_embedder_settings
from planner_fixtures import reply
from test_planner import png
from test_pipeline_s0_s3 import ModelServerDouble
from test_pipeline_config import runtime
from prompt_enhancer.pipeline import GoldenAdapter, Pipeline
from prompt_enhancer.profiles import resolve_profile
from prompt_enhancer.storage import open_data_store


class FullServerDouble(ModelServerDouble):
    """Capability answers from probe pixels; scripted planner/writer, no real model."""
    @staticmethod
    def is_planner(body):
        parts = body['messages'][-1]['content']
        text = parts if isinstance(parts, str) else next((item['text'] for item in parts if item['type'] == 'text'), '')
        try:
            value = json.loads(text)
            return isinstance(value, dict) and 'shortlist' in value
        except ValueError:
            return False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.seen.append(body)
        if self.path.endswith('/embeddings'):
            raw = json.dumps({'data': [{'index': i, 'embedding': [1., 0., 0., 0.]}
                                        for i, _ in enumerate(body['input'])]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        parts = body['messages'][-1]['content']
        text = parts if isinstance(parts, str) else next((item['text'] for item in parts if item['type'] == 'text'), '')
        if self.is_planner(body):
            data = json.loads(text)
            answer = reply(edit_kind='local_edit' if data['task'] == 'edit' else None)
        elif body['messages'][0]['content'] == self.writer_system:
            answer = 'kept original intent'
        elif body['messages'][0]['content'] == self.verifier_system:
            answer = {'results': [{'constraint_id': item['id'], 'verdict': 'pass',
                                   'evidence_span': 'kept original intent', 'note': ''}
                                  for item in json.loads(text)['obligations']]}
        else:
            answer = self.probe(body)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        self.answers.append({'planner': self.is_planner(body), 'text': text, 'answer': answer})
        raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.parametrize('task', ['generate', 'edit'])
@pytest.mark.parametrize('retrieval', [False, True, 'warning'])
@pytest.mark.parametrize('golden', [False, True])
def test_production_s0_s6_retrieval_off_without_kb(model_test_profile, tmp_path, project_root, monkeypatch, task, retrieval, golden):
    seen, answers = [], []
    handler = type('Handler', (FullServerDouble,), {'seen': seen, 'answers': answers})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = (assembly_config if retrieval else configuration)(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    config['storage']['data_root'] = str(tmp_path / 'data')
    config['resolution']['random_seed'] = 7
    backend = config['vlm']['backends']['main']
    backend.update(base_urls=[f'http://127.0.0.1:{server.server_port}'],
                   # test-only: KBS-F2 proves each request-shaping context separately; a verifier-on case needs 11
                   # probe sends (planner and writer only: 9), so 12 passes and still fails if the count grows; an
                   # exhausted allowance would silently turn the expected status into `degraded`.
                   probe={'max_requests': 12, 'timeout_s': 2})
    backend['image']['max_decode_pixels'] = 20_000_000
    if retrieval:
        config['vlm']['backends']['embedder']['base_urls'] = backend['base_urls']
        env = published(project_root, tmp_path, rows=dataset(revision=query_embedder_settings(config).revision),
                        settings=config)
        config['storage']['kb_store'] = str(env.store.root)
    if retrieval == 'warning':
        config['embedding']['query_text_limit'] = 1
        config['validation']['verifier']['require_calibration'] = False
    activate(config, project_root)
    handler.writer_system = resolve_profile(config, project_root=project_root).system_message.decode('utf-8')
    handler.verifier_system = (project_root / 'prompts/verifier_v0.txt').read_bytes().decode('utf-8')
    if not retrieval:
        def forbidden(*args, **kwargs):
            raise AssertionError('retrieval-off must not construct knowledge stages or embedders')
        monkeypatch.setattr('prompt_enhancer.storage.open_knowledge_store', forbidden)
        monkeypatch.setattr('prompt_enhancer.intake.KnowledgeGateway', forbidden)
        monkeypatch.setattr('prompt_enhancer.pipeline.build_candidates', forbidden)
        monkeypatch.setattr('prompt_enhancer.pipeline.build_assembly', forbidden)
        monkeypatch.setattr('prompt_enhancer.k1_embed.Embedder', forbidden)
    request = {'prompt': 'original intent', 'task': task}
    if task == 'edit':
        request['source_image'] = base64.b64encode(png()).decode()
    async def run():
        async with Pipeline(config, project_root=project_root) as pipeline:
            if golden:
                view = replace(resolve_profile(config, project_root=project_root), activation=None)
                return await GoldenAdapter(pipeline).run(view, request)
            response = await pipeline.enhance(request)
            await response.after_delivery()
        return response.envelope
    try:
        envelope = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    if not retrieval:
        assert not (tmp_path / 'kb').exists()
    rows = open_data_store(config, 'enhance once', project_root=project_root).read_rows('request_traces')
    assert len(rows) == 1 and rows[0]['request_id'] == envelope['trace_id']
    assert rows[0]['status'] == envelope['status'] and seen
    capture = {'request': request['prompt'], 'task': task, 'retrieval_enabled': retrieval,
               's1': rows[0]['body']['shortlist'], 's2': rows[0]['body']['plan'],
               's3': rows[0]['body']['assembly'], 'envelope': envelope, 'http_answers': answers}
    body = rows[0]['body']
    assert body['plan']['state'] == 'ready' and body['plan']['intent_ir'] is not None, body['plan']
    assert body['writer']['state'] == 'draft' and body['writer']['output'] == 'kept original intent'
    assert body['configured_model_ids'] == {'writer': backend['model']['id'], 'verifier': backend['model']['id']}
    if retrieval:
        assert body['shortlist']['state'] == 'ready'
        if retrieval == 'warning':
            warning = next(item for item in body['shortlist']['warnings'] if item['code'] == 'DENSE_QUERY_TOO_LONG')
            assert warning['span']['text'] == request['prompt']
            assert rows[0]['warnings'].count(warning['code']) == 1 and warning['code'] not in envelope['warnings']
        else:
            assert body['shortlist']['candidates']
        assert body['assembly']['retrieval_state'] == 'skipped_no_knowledge_needed'
    else:
        assert body['shortlist'] is None and body['assembly'] is None
    assert envelope['retrieval']['state'] == ('skipped_no_knowledge_needed' if retrieval else 'disabled'), capture
    assert (envelope['retrieval']['snapshot_id'] is not None) == bool(retrieval)
    # The known conflict requires verification. The warning fixture explicitly allows an uncalibrated gate.
    assert envelope['status'] == ('success' if retrieval == 'warning' else 'degraded')
    assert envelope['output'] is not None
    assert ('VERIFIER_UNCALIBRATED' in envelope['warnings']) == (retrieval == 'warning')


@pytest.mark.parametrize('corrupt', [False, True])
def test_published_image_attaches_after_source_removed_and_corrupt_copy_refuses_before_s4(
        k2_env, model_test_profile, tmp_path, project_root, monkeypatch, corrupt):
    """Real kb sync publish, source deleted, edit request with attachments on through the production Pipeline.

    Labelled doubles: the loopback model server, the S2 plan (fixed concept links) and the K3 card stability of a
    two-image fixture. Publish, intake, S3 selection, the pre-S4 byte check, S6 read and the hold are production code.
    """
    import hashlib
    import shutil
    from assembly_fixtures import entity, ir, link, plan as fixed_plan
    from k2_fixtures import folder
    from kb_sync_fixtures import SyncServer, invoke
    import prompt_enhancer.pipeline as pipeline_module

    source = folder(k2_env, 'f', 'Name\ndescription', count=2)
    originals = {path.name: path.read_bytes() for path in source.glob('*.png')}
    invoke(k2_env, SyncServer())
    shutil.rmtree(source)  # the original images exist only as snapshot copies from here on
    with k2_env.store.pin() as pin:
        concept = pin.read_rows('concepts')[0]['concept_id']
        sid, directory = pin.snapshot_id, pin._directory
        ids = {row['image_id'] for row in pin.read_rows('images')}
        if corrupt:  # same-size damage: only a byte check can see it
            for image_id in ids:
                path = k2_env.store.path(f'{directory}/images/{image_id[7:]}')
                data = path.read_bytes()
                path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    assert ids == {'sha256:' + hashlib.sha256(data).hexdigest() for data in originals.values()}

    seen, answers = [], []
    handler = type('Handler', (FullServerDouble,), {'seen': seen, 'answers': answers})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = assembly_config(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    config['runtime']['trace']['retention_s'] = 2
    config['storage']['data_root'] = str(tmp_path / 'data')
    config['storage']['kb_store'] = str(k2_env.store.root)
    config['resolution']['random_seed'] = 7
    config['edit'].update(attach_kb_reference_images=True, max_kb_reference_images=2)
    config['assembly']['default_outfit'] = 'include'
    config['validation']['verifier']['require_calibration'] = False
    backend = config['vlm']['backends']['main']
    backend.update(base_urls=[f'http://127.0.0.1:{server.server_port}'], probe={'max_requests': 12, 'timeout_s': 2})
    backend['image']['max_decode_pixels'] = 20_000_000
    config['vlm']['backends']['embedder']['base_urls'] = backend['base_urls']
    activate(config, project_root, renderer={'accepts_reference_images': True, 'image_reference_syntax': 'none'})
    handler.writer_system = resolve_profile(config, project_root=project_root).system_message.decode('utf-8')
    handler.verifier_system = (project_root / 'prompts/verifier_v0.txt').read_bytes().decode('utf-8')

    async def labelled_plan(s0, shortlist, owner, **kwargs):
        return fixed_plan(ir([link(concept, 'subject_identity', 'e1')], [(concept, ['apparel.color'])],
                              [entity('e1', 'Name')], task='edit'))

    real_assembly = pipeline_module.build_assembly

    def labelled_stable_assembly(s0, planned, **kwargs):
        read_rows = s0.snapshot.read_rows

        def stable_cards(name):
            rows = read_rows(name)
            if name == 'cards':
                for row in rows:
                    for facet in row['facets']:
                        facet['stability'] = 'stable'
            return rows

        s0.snapshot.read_rows = stable_cards
        return real_assembly(s0, planned, **kwargs)

    monkeypatch.setattr(pipeline_module, 'plan', labelled_plan)
    monkeypatch.setattr(pipeline_module, 'build_assembly', labelled_stable_assembly)
    request = {'prompt': 'original intent', 'task': 'edit', 'source_image': base64.b64encode(png()).decode()}
    holds = k2_env.store.root / 'holds' / sid
    outcome = {}

    def hold_files():
        return sorted(holds.glob('*.parquet')) if holds.exists() else []

    async def run():
        async with Pipeline(config, project_root=project_root) as pipeline:
            response = await pipeline.enhance(request)
            outcome['holds_at_delivery'] = hold_files()
            outcome['leases_at_delivery'] = list((k2_env.store.root / 'leases').glob('*/*.lease'))
            await response.after_delivery()
            outcome['holds_after_delivery'] = hold_files()
            if not corrupt:  # the refusal variant keeps its trace row to read; the 2 s retention is not awaited
                await asyncio.sleep(2.2)  # retention passes; closing expires the trace and releases its hold
        outcome['holds_after_expiry'] = hold_files()
        return response

    try:
        response = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    envelope = response.envelope
    writer_calls = [body for body in seen if body['messages'][0]['content'] == handler.writer_system]
    assert outcome['leases_at_delivery'] == []  # the request lease is released in both variants
    if corrupt:
        rows = open_data_store(config, 'enhance once', project_root=project_root).read_rows('request_traces')
        assert envelope['status'] == 'failed' and envelope['output'] is None and envelope['attachments'] == []
        assert response.attachment_bytes == {}
        assert not writer_calls  # refused after S3 and before S4: the writer was never called
        assert rows[0]['body']['intake_error'] == {'code': 'KNOWLEDGE_FAILED',
                                                   'message': 'image copies unavailable; run kb sync to republish'}
        assert outcome['holds_at_delivery'] == []
        return
    assert writer_calls
    attached = {item['image_id'] for item in envelope['attachments']}
    assert attached and attached <= ids
    assert all(item['role'] == 'knowledge_reference' for item in envelope['attachments'])
    assert set(response.attachment_bytes) == attached
    assert all(data in originals.values() and 'sha256:' + hashlib.sha256(data).hexdigest() == image_id
               for image_id, data in response.attachment_bytes.items())  # the original bytes, hash-equal to the id
    assert len(outcome['holds_at_delivery']) == 1  # taken before the request lease was released
    assert len(outcome['holds_after_delivery']) == 1  # the hold protects the snapshot while the trace is retained
    assert outcome['holds_after_expiry'] == []  # released when the trace expires, not before
