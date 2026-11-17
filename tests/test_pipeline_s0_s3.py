"""S0 -> S1 -> S2 -> S3 through the production entry points (T-014 P2; lesson 2026-11-17).

Nothing is injected into the request path. The only doubles are a labelled loopback HTTP model server (chat and
embeddings) and a tiny synthetic published snapshot; the admin profile is registered with the profile fixtures'
labelled drafter and runner doubles before the request starts.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from assembly_fixtures import KIMONO, assembly_config, facet
from intake_fixtures import activate
from k5_fixtures import published
from prompt_enhancer.assembly import build_assembly
from prompt_enhancer.candidates import build_candidates, query_embedder_settings
from prompt_enhancer.intake import intake
from prompt_enhancer.k1_embed import Embedder
from prompt_enhancer.model_client import ProbeCasesMissing, compose_model_client
from prompt_enhancer.planner import plan
from storage_fixtures import dataset

PROMPT = 'a kimono'


class ModelServerDouble(BaseHTTPRequestHandler):
    """Labelled double of the model server: fixed unit vectors for embeddings; for chat, a planner reply that
    links the shortlist entry named Kimono, read from the request's own JSON data (no model)."""

    seen = None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.seen.append((self.path, body))
        if self.path.endswith('/embeddings'):
            answer = {'data': [{'index': index, 'embedding': [1.0, 0.0, 0.0, 0.0]}
                               for index, _ in enumerate(body['input'])]}
        else:
            data = json.loads(body['messages'][-1]['content'] if isinstance(body['messages'][-1]['content'], str)
                              else body['messages'][-1]['content'][-1]['text'])
            position = next(item['position'] for item in data['shortlist'] if item['name'] == 'Kimono')
            reply = {'edit_kind': None, 'scene': {'location': None, 'medium': None, 'attributes': []},
                     'entities': [{'id': 'e1', 'kind': 'person', 'count': 1, 'mention': 'kimono', 'attributes': [],
                                   'action': None, 'relations': [], 'source': 'user'}],
                     'links': [{'mention': 'kimono', 'entity_id': 'e1', 'status': 'resolved',
                                'role': 'subject_identity', 'candidate_position': position,
                                'candidates': [{'position': position, 'context_fit': 'fits'}],
                                'needs_lookup': False, 'variants': []}],
                     'knowledge_needs': [{'candidate_position': position, 'facets': ['identity.hair']}],
                     'constraints': [], 'literal_texts': [], 'passthrough_tokens': [], 'edit_targets': [],
                     'delta': [], 'preserve': [], 'dependent_effects': [], 'unresolved': [],
                     'needs_clarification': False, 'clarification_candidates': []}
            answer = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(reply)}}]}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def server():
    seen = []
    handler = type('Handler', (ModelServerDouble,), {'seen': seen})
    instance = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance, seen
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


# Expected failure: exactly the missing production probe cases (KBS-F1), named by its own exception type, so any other
# ModelCallError fails the test. KBS-F1 rewrites that path and removes the condition; the PM hand-merges it, and this
# test must then pass outright with no mark (strict=True makes an unexpected pass fail until the mark is removed).
@pytest.mark.xfail(strict=True, raises=ProbeCasesMissing,
                   reason='KBS-F1: production capability probes (model_client.py raises ProbeCasesMissing)')
def test_generate_request_runs_s0_to_s3_on_production_entry_points(model_test_profile, tmp_path, project_root,
                                                                   server):
    instance, seen = server
    url = f'http://127.0.0.1:{instance.server_port}'
    config = assembly_config(model_test_profile, tmp_path)
    config['vlm']['backends']['main'].update(base_urls=[url], endpoints={'chat': 'v1/chat/completions',
                                                                         'health': 'health'})
    config['vlm']['backends']['embedder']['base_urls'] = [url]
    config['embedding']['query_text_limit'] = 1000  # labelled test-only
    config['candidates']['max_prompt_windows'] = 100000
    rows = dataset(revision=query_embedder_settings(config).revision)
    image = rows['images'][0]['image_id']
    for row in rows['cards']:
        if row['concept_id'] == KIMONO:
            row['facets'] = [facet('identity.hair', {'black': [image]}),
                             facet('incidental.background', {'garden': [image]})]
    env = published(project_root, tmp_path, rows=rows, settings=config)
    config['storage']['kb_store'] = str(env.store.root)
    activate(config, project_root)

    async def request():
        with intake({'prompt': PROMPT, 'task': 'generate'}, config, project_root=project_root) as s0:
            assert s0.payload['outcome'] == 'ready', s0.payload['error']
            effective = s0.payload['effective_config']
            embedder = Embedder(query_embedder_settings(effective))
            try:
                shortlist = await build_candidates(s0, embedder=embedder)
                assert shortlist['state'] == 'ready'
                client = await compose_model_client(effective, 'planner', project_root=project_root,
                                                    probe_cases={}, local_probe_plans={})
                try:
                    planned = await plan(s0, shortlist, client, project_root=project_root, embedder=embedder)
                finally:
                    await client.close()
                assert planned['intent_ir'] is not None, (planned['state'], planned['error'], planned['calls'])
            finally:
                await embedder.close()
            return s0.payload, shortlist, planned, build_assembly(s0, planned, project_root=project_root)

    payload, shortlist, planned, assembled = asyncio.run(request())
    assert planned['state'] == 'ready' and planned['intent_ir']['concept_links'][0]['concept_id'] == KIMONO
    pack = assembled['pack']
    assert assembled['retrieval_state'] == 'retrieved'
    assert [(fact['facet'], fact['value'], fact['bound_entity']) for fact in pack['facts']] == [
        ('identity.hair', 'black', 'e1')]
    assert pack['snapshot_id'] == payload['retrieval']['snapshot_id'] and pack['request_id'] == payload['request_id']
    assert any(path.endswith('/embeddings') for path, _ in seen)
    assert any(path.endswith('/chat/completions') for path, _ in seen)
