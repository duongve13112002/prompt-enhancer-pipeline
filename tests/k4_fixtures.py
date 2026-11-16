"""Tiny synthetic current rows and a labelled in-process alias transport double; no network."""

import asyncio
from copy import deepcopy
import json

import httpx

from k2_fixtures import NamingServer
from prompt_enhancer.k4_aliases import build_aliases
from prompt_enhancer.model_client import BoundModelClient
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_transport import ModelTransport
from prompt_enhancer.storage import concept_id, normalise_name
from storage_fixtures import dataset


def inputs(names=('Concept',)):
    handoff = {'folders': [], 'concepts': [], 'folder_concepts': []}
    cards = []
    template = dataset()['cards'][0]
    for index, name in enumerate(names):
        identity, folder = concept_id(name), f'folder-{index}'
        handoff['concepts'].append({'concept_id': identity, 'normalised_name': normalise_name(name),
                                    'display_name': name, 'kind': 'object', 'kind_origin': 'generated'})
        handoff['folders'].append({'folder_id': folder, 'info_file_hash': None, 'declared_names': [name],
                                   'declared_descriptions': 'Labelled description', 'name_source': 'info_file', 'error': ''})
        handoff['folder_concepts'].append({'folder_id': folder, 'concept_id': identity, 'position': 0,
                                           'name_as_written': name, 'description': 'Labelled description',
                                           'description_origin': 'info_file', 'description_revision': None})
        card = deepcopy(template)
        card.update(concept_id=identity, name=name, summary=f'Labelled summary {name}')
        cards.append(card)
    return {'handoff': handoff, 'cards': cards, 'overrides': {'data': {}, 'file_hash': 'labelled-test'}}


class AliasServer(NamingServer):
    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        payload = json.loads(body['messages'][1]['content'])
        answer = self.reply(payload, body) if callable(self.reply) else deepcopy(self.reply)
        if answer is None:
            answer = {'aliases': [{'text': payload['concept_name'] + ' alias', 'language': 'test-language'}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                    'message': {'content': json.dumps(answer)}}]})

    def run(self, env):
        run = super().run(env)
        name = env.config['vlm']['roles']['alias_generator']['backend']
        if name in env.config['vlm']['backends'] and name not in run.transports:
            run.transports[name] = ModelTransport(name, env.config['vlm']['backends'][name], run.concurrency,
                                                 run.ledger, transport=httpx.MockTransport(self.handler))
            run.probes[name] = ProbeResult('complete', self.capabilities, {})
        return run


def invoke_aliases(env, data, server=None):
    server = AliasServer() if server is None else server

    async def execute():
        run = server.run(env)
        try:
            return await asyncio.wait_for(build_aliases(env.store, env.config,
                client=BoundModelClient(run, 'alias_generator'), **data), timeout=20)
        finally:
            await run.close()
    return asyncio.run(execute())


def states(env, stage='k4_generation'):
    with env.store.writer() as writer:
        return [row for row in writer._state_rows().values() if row['stage'] == stage]
