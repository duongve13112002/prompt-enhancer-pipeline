"""Labelled deterministic HTTP doubles for real K0-K5 runs; no network or inference."""

import asyncio
import json

import httpx

from k1_fixtures import EmbeddingServer
from k2_fixtures import NamingServer, assessment, naming
from k2_observe_fixtures import observation
from k3_fixtures import reply_for
from prompt_enhancer.kb_sync import sync_knowledge
from prompt_enhancer.model_client import BoundModelClient


class SyncServer(NamingServer):
    def __init__(self):
        super().__init__()
        self.embedder = EmbeddingServer()
        self.roles = []
        self.fail_role = None
        self.fail_concept = None

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        content = body['messages'][1]['content']
        payload = json.loads(content[0]['text'] if isinstance(content, list) else content)
        if 'concept_names' in payload:
            role, reply = 'observer', observation(range(len(payload['concept_names'])))
        elif 'facets' in payload:
            role, reply = 'card_builder', reply_for(payload)
        elif 'card_summary' in payload:
            role = 'alias_generator'
            reply = {'aliases': [{'text': payload['concept_name'] + ' alias', 'language': 'test-language'}]}
        elif isinstance(content, list):
            role, reply = 'folder_namer', naming()
        else:
            role, reply = 'info_assessor', assessment()
        self.roles.append(role)
        if role == self.fail_role and (self.fail_concept is None or payload.get('concept_name') == self.fail_concept):
            reply = {'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS': 'invalid'}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                    'message': {'content': json.dumps(reply)}}]})


def invoke(env, server, *, plan=False, force=False):
    async def execute():
        run = server.run(env)
        try:
            return await asyncio.wait_for(sync_knowledge(env.config, project_root=env.root,
                client=BoundModelClient(run, 'observer'), transport=server.embedder.transport(),
                plan=plan, force=force), timeout=45)
        finally:
            await run.close()
    return asyncio.run(execute())


def inventory(root):
    entries = {'.': None} if root.exists() else {}
    entries.update({path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
                    for path in root.rglob('*')})
    return entries
