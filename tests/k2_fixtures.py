"""Labelled transport/capability doubles and tiny synthetic fixtures for K2; no network or model."""

import asyncio
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import shutil

import httpx
from PIL import Image

from prompt_enhancer.k0_sync import sync
from prompt_enhancer.k2_naming import name_folders
from prompt_enhancer.model_client import BoundModelClient, ModelClientRun
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_transport import ModelTransport
from storage_fixtures import make_store

REPO = Path(__file__).resolve().parents[1]


def image_bytes(color=(30, 50, 70), size=(12, 9), mode='RGB'):
    output = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format='PNG')
    return output.getvalue()


def environment(project_root, tmp_path, profile):
    config = deepcopy(profile)
    source = tmp_path / 'source'
    source.mkdir()
    store_path = tmp_path / 'store'
    config['knowledge'].update(root=source.relative_to(project_root).as_posix(),
                               image_extensions=['.png'], overrides_file='kb_overrides.yaml')
    config['knowledge']['naming'].update(rename_on_change=0.5, sample_size=2)
    config['sync'].update(mass_deletion_guard={'max_removed_fraction': 1}, snapshots={'keep': 1},
                          gc={'retention': '1d'})
    config['embedding']['query_text_limit'] = 128
    config['observe'].update(representatives_per_folder=1, facet_schema_version='image_observation.v0', reply_attempts=1)
    config['cards'].update(min_visible_images=1, reply_attempts=1)  # test-only: one request, as before decisions 183 and 188
    config['index'].update(engine='numpy_exact', metric='inner_product', build={'block_rows': 2})
    config['index']['lexical']['ngram_size'] = 2
    config['storage'].update(kb_store=store_path.relative_to(project_root).as_posix(),
                             data_root=(tmp_path / 'data').relative_to(project_root).as_posix(),
                             lock={'timeout_s': 5, 'poll_s': 0.01})
    config['vlm']['backends']['main']['image']['max_decode_pixels'] = 20_000_000
    shutil.copytree(REPO / 'prompts', project_root / 'prompts', dirs_exist_ok=True)
    store = make_store(project_root, store_path, timeout_s=config['storage']['lock']['timeout_s'],
                       poll_s=config['storage']['lock']['poll_s'])
    return SimpleNamespace(config=config, root=project_root, source=source, store=store)


def folder(env, name, info=None, count=1, payload=None):
    path = env.source / name
    path.mkdir(parents=True, exist_ok=True)
    if info is not None:
        (path / 'info.txt').write_bytes(info.encode('utf-8'))
    for index in range(count):
        (path / f'{index}.png').write_bytes(payload if payload is not None else image_bytes((index * 20, 30, 50)))
    return path


def scan(env):
    return sync(env.store, env.source, info_names=['info.txt'], encoding='utf-8', extensions=['.png'],
                separator=env.config['knowledge']['concept_separator'], workers=1, threshold=1)


def assessment(usable=True, assignments=None):
    return {'usable': usable, 'reason': 'Labelled assessor double', 'assigned_descriptions': assignments}


def naming(names=('Generated',), *, folder_fits=False, info_fits=None, consistent=True):
    return {'folder_name_fits': folder_fits, 'info_file_fits': info_fits, 'images_consistent': consistent,
            'concepts': [{'name': name, 'description': f'Double description of {name}'} for name in names],
            'reason': 'Labelled naming double'}


class FixedDraw:
    """Labelled ordered-sampling double; requested content must exist in the candidate set."""

    def __init__(self, image_ids):
        self.image_ids = list(image_ids)

    def sample(self, candidates, size):
        assert len(self.image_ids) == size
        assert len(set(self.image_ids)) == size and set(self.image_ids) <= set(candidates)
        return list(self.image_ids)


class NamingServer:
    """Labelled HTTP transport double. Scripted answers exercise T-009 without inference or sockets."""

    def __init__(self, responses=None, *, capabilities=None):
        self.responses = responses or {}
        self.requests = []
        # Labelled capability evidence double; actual proof behavior has separate bounded probe tests.
        self.capabilities = capabilities if capabilities is not None else {
            'image': True, 'multiple_images': True, 'json_schema': True, 'json_object': True,
            'reasoning.mode': True, 'reasoning.effort': True,
            **{f'sampling.{field}': True for field in ('temperature', 'top_p', 'top_k', 'min_p',
                'frequency_penalty', 'presence_penalty', 'repetition_penalty', 'seed', 'stop')}}

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        content = body['messages'][1]['content']
        image_call = isinstance(content, list)
        data = json.loads(content[0]['text'] if image_call else content)
        role = 'folder_namer' if image_call else 'info_assessor'
        # Production payloads expose only the basename hint, never the folder's pipeline identity.
        key = next((key for key in self.responses if key[0] == role
                    and key[1].rsplit('/', 1)[-1] == data['folder_name']), None)
        answer = self.responses.get(key, naming() if image_call else assessment())
        if callable(answer):
            answer = answer(data, body)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                     'message': {'content': json.dumps(answer)}}]})

    def run(self, env):
        run = ModelClientRun(env.config, env.root, probe_cases={}, local_probe_plans={})
        names = {env.config['vlm']['default_backend']}
        names.update(env.config['vlm']['roles'][role].get('backend') for role in ('info_assessor', 'folder_namer'))
        for name in names:
            if name not in env.config['vlm']['backends']:
                continue  # invalid bindings must reach the real stage's preflight, not fail inside this fixture
            run.transports[name] = ModelTransport(name, env.config['vlm']['backends'][name], run.concurrency,
                                                   run.ledger, transport=httpx.MockTransport(self.handler))
            run.probes[name] = ProbeResult('complete', self.capabilities, {})
        return run


def invoke(env, server=None, *, result=None, rng=None):
    server = server if server is not None else NamingServer()
    result = scan(env) if result is None else result

    async def execute():
        run = server.run(env)
        try:
            return await name_folders(env.store, env.config, result['handoff']['k2'], images=result['rows']['images'],
                                      folder_images=result['rows']['folder_images'], knowledge_root=env.source,
                                      client=BoundModelClient(run, 'info_assessor'), rng=rng)
        finally:
            await run.close()
    return asyncio.run(execute())


def source_tree(source):
    return {path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
            for path in source.rglob('*') if path.is_file()}
