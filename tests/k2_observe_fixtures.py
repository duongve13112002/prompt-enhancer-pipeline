"""Labelled observer transport double and synthetic K1 result; no network, embedding or inference."""

import asyncio
from copy import deepcopy
import json

import httpx

from k2_fixtures import NamingServer, scan
from prompt_enhancer.k2_observe import observe_images
from prompt_enhancer.model_client import BoundModelClient


def observation(positions=(0,), *, text='visible attribute', facet='apparel.color'):
    return {'view': 'front', 'quality': 1.75, 'observations': [
        {'concept_position': index, 'facet_group': facet.split('.')[0], 'facet': facet, 'value': text,
         'visibility': 'clear', 'region': None} for index in positions]}


class ObserverServer(NamingServer):
    """In-process scripted observer reply; real RoleClient handles mapping, image preparation and validation."""
    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        data = json.loads(body['messages'][1]['content'][0]['text'])
        value = (self.reply(data, body) if callable(self.reply) else deepcopy(self.reply)
                 if self.reply is not None else observation(range(len(data['concept_names']))))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(value, ensure_ascii=True)}}]})

    def run(self, env):
        run = super().run(env)
        # The observer may bind an alternate backend in the configuration override tests.
        from prompt_enhancer.model_probe import ProbeResult
        from prompt_enhancer.model_transport import ModelTransport
        name = env.config['vlm']['roles']['observer']['backend']
        if name in env.config['vlm']['backends'] and name not in run.transports:
            run.transports[name] = ModelTransport(name, env.config['vlm']['backends'][name], run.concurrency,
                run.ledger, transport=httpx.MockTransport(self.handler))
            run.probes[name] = ProbeResult('complete', self.capabilities, {})
        return run


def k1_result(env, *, vectors=None):
    scanned = scan(env)
    images = [dict(row, decode_status='ok', format='PNG', width=12, height=9) for row in scanned['rows']['images']]
    ids = sorted(row['image_id'] for row in images)
    vector_rows = [{'image_id': identity, 'embedder_revision': 'labelled-k1', 'dim': 2,
                    'vector': vectors[index] if vectors is not None else [1.0, 0.0]}
                   for index, identity in enumerate(ids)]
    return {'handoff': scanned['handoff']['k2'], 'images': images,
            'folder_images': scanned['rows']['folder_images'], 'image_embeddings': vector_rows,
            'embedder_revision': 'labelled-k1', 'dim': 2}


def invoke_observer(env, server=None, *, inputs=None):
    server = server if server is not None else ObserverServer()
    inputs = k1_result(env) if inputs is None else inputs

    async def execute():
        run = server.run(env)
        try:
            return await observe_images(env.store, env.config, knowledge_root=env.source,
                client=BoundModelClient(run, 'observer'), **inputs)
        finally:
            await run.close()
    return asyncio.run(execute())


def states(env):
    with env.store.writer() as writer:
        return [row for row in writer._state_rows().values() if row['stage'] == 'k2_observation']
