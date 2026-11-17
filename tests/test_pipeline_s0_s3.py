"""S0 -> S1 -> S2 -> S3 through the production entry points (T-014 P2; lesson 2026-11-17).

Nothing is injected into the request path. The only doubles are a labelled loopback HTTP model server (chat and
embeddings) and a tiny synthetic published snapshot; the admin profile is registered with the profile fixtures'
labelled drafter and runner doubles before the request starts.
"""

import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread

from PIL import Image
import pytest

from assembly_fixtures import KIMONO, assembly_config, facet
from intake_fixtures import activate
from k5_fixtures import published
from prompt_enhancer.assembly import build_assembly
from prompt_enhancer.candidates import build_candidates, query_embedder_settings
from prompt_enhancer.intake import intake
from prompt_enhancer.k1_embed import Embedder
from prompt_enhancer.model_client import compose_model_client
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
        elif not self.is_planner(body):
            answer = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.probe(body))}}]}
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

    @staticmethod
    def is_planner(body):
        content = body['messages'][-1]['content']
        text = content if isinstance(content, str) else content[-1].get('text', '')
        try:
            return isinstance(json.loads(text), dict) and 'shortlist' in json.loads(text)
        except ValueError:
            return False

    @staticmethod
    def probe(body):
        """Production capability probes (KBS-F1): image challenges are answered from pixels, text challenges echo
        the literal object of the instruction, as the KBS-F1 production double does."""
        content = body['messages'][-1]['content']
        counts = []
        for part in content if isinstance(content, list) else ():
            if part['type'] == 'image_url':
                encoded = part['image_url']['url'].split(',', 1)[1]
                with Image.open(BytesIO(base64.b64decode(encoded))) as image:
                    row = [sum(image.convert('RGB').getpixel((x, image.height // 2))) < 384
                           for x in range(image.width)]
                    counts.append(sum(dark and (x == 0 or not row[x - 1]) for x, dark in enumerate(row)))
        if counts:
            return {'value': counts[0] * 10 + counts[1] if len(counts) == 2 else counts[0]}
        text = content if isinstance(content, str) else content[0]['text']
        return json.JSONDecoder().raw_decode(text[text.index('{'):])[0]

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


# Production entry points with nothing injected: since KBS-F1 the client builds its own capability probes.
def test_generate_request_runs_s0_to_s3_on_production_entry_points(model_test_profile, tmp_path, project_root,
                                                                   server):
    instance, seen = server
    url = f'http://127.0.0.1:{instance.server_port}'
    config = assembly_config(model_test_profile, tmp_path)
    config['vlm']['backends']['main'].update(base_urls=[url], endpoints={'chat': 'v1/chat/completions',
                                                                         'health': 'health'})
    config['vlm']['backends']['embedder']['base_urls'] = [url]
    config['embedding']['query_text_limit'] = 1000  # labelled test-only
    # Labelled test-only: room for the production capability probes and their bundled 96x64 images (KBS-F1).
    config['vlm']['backends']['main']['probe'] = {'max_requests': 10, 'timeout_s': 2}
    config['vlm']['backends']['main']['image']['max_decode_pixels'] = 20_000_000
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
                client = await compose_model_client(effective, 'planner', project_root=project_root)
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
