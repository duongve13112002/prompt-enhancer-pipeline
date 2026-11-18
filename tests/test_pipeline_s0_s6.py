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
