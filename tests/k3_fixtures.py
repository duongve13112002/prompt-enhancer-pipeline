"""Labelled K3 transport double and tiny stored-row fixtures; no network or inference."""

import asyncio
from copy import deepcopy
import json

import httpx

from k2_fixtures import NamingServer, folder, invoke
from k2_observe_fixtures import k1_result
from prompt_enhancer.k3_cards import build_cards
from prompt_enhancer.model_client import BoundModelClient
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_transport import ModelTransport
from prompt_enhancer.storage.store import new_snapshot_id


def reply_for(payload, *, lint=False):
    facets = []
    for facet in payload['facets']:
        groups = {}
        for row in payload['observations']:
            if row['facet_position'] == facet['facet_position'] and row['visibility'] in ('clear', 'partial'):
                # Labelled scripted equality double; production delegates equivalence to its model.
                groups.setdefault(row['value'], []).append(row['observation_position'])
        facets.append({'facet_position': facet['facet_position'], 'groups': [
            {'value': value, 'observation_positions': indices} for value, indices in sorted(groups.items())]})
    views = {}
    for row in payload['observations']:
        views.setdefault(row['view'], []).append(row['observation_position'])
    return {'kind': 'character', 'facets': facets, 'view_groups': [
        {'value': value, 'observation_positions': indices} for value, indices in sorted(views.items())],
        'summary': 'Labelled card summary', 'lint_findings': [
            {'code': 'CONTRADICTION', 'message': 'Labelled contradiction', 'facet_positions': [0],
             'observation_positions': [0], 'curated_positions': [0]}] if lint else []}


class CardServer(NamingServer):
    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        payload = json.loads(body['messages'][1]['content'])
        answer = self.reply(payload, body) if callable(self.reply) else deepcopy(self.reply)
        if answer is None:
            answer = reply_for(payload)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                'message': {'content': json.dumps(answer)}}]})

    def run(self, env):
        run = super().run(env)
        name = env.config['vlm']['roles']['card_builder']['backend']
        if name in env.config['vlm']['backends'] and name not in run.transports:
            run.transports[name] = ModelTransport(name, env.config['vlm']['backends'][name], run.concurrency,
                run.ledger, transport=httpx.MockTransport(self.handler))
            run.probes[name] = ProbeResult('complete', self.capabilities, {})
        return run


def inputs(env, values=('A', 'B', 'B', 'B', 'B'), *, duplicate=False, visibility='clear'):
    folder(env, 'f', 'Concept\nHuman description', count=len(values))
    env.config['cards']['min_visible_images'] = 2
    current = k1_result(env)
    named = invoke(env)
    images = sorted(current['images'], key=lambda row: row['image_id'])
    observations = []
    from prompt_enhancer.k2_naming import _digest
    for index, (image, value) in enumerate(zip(images, values)):
        image['near_duplicate_group'] = 'one-group' if duplicate else 'group-' + str(index)
        observations.append({'context_key': _digest({'context': index}), 'image_id': image['image_id'],
            'concept_id': named['rows']['concepts'][0]['concept_id'], 'facet': 'identity.hair',
            'model_revision': 'observer-test', 'prompt_id': 'observer_v0',
            'facet_schema_version': 'image_observation.v0', 'facet_group': 'identity', 'value': value,
            'visibility': visibility, 'region': None, 'view': 'front' if index % 2 else 'side',
            'quality': 1.0, 'origin': 'generated'})
    return {'handoff': named['rows'], 'observations': observations, 'images': images,
            'folder_images': current['folder_images'], 'overrides': named['effective_overrides'],
            'snapshot_id': new_snapshot_id()}


def invoke_cards(env, data, server=None):
    server = CardServer() if server is None else server

    async def execute():
        run = server.run(env)
        try:
            return await build_cards(env.store, env.config, client=BoundModelClient(run, 'card_builder'), **data)
        finally:
            await run.close()
    return asyncio.run(execute())


def checkpoint_rows(env, stage):
    with env.store.writer() as writer:
        return [row for row in writer._state_rows().values() if row['stage'] == stage]
