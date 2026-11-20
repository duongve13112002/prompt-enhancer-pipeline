"""Shared setup of the command-line tests (T-016): a real `python -m prompt_enhancer` subprocess against a loopback model
double. The double (`test_pipeline_s0_s6.FullServerDouble`) is the only labelled substitute; the profile bundle comes from
the real register/check logic with the labelled drafter/runner doubles of `intake_fixtures.activate`."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread

import httpx
import yaml

from assembly_fixtures import assembly_config
from intake_fixtures import activate, configuration
from k2_fixtures import folder
from k2_observe_fixtures import observation
from kb_sync_fixtures import SyncServer, invoke
from planner_fixtures import entity as planned_entity, link as planned_link, reply as planned_reply
from profile_fixtures import draft
from prompt_enhancer.profiles import resolve_profile
from test_pipeline_config import runtime
from test_pipeline_s0_s6 import FullServerDouble

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # Windows only; 0 elsewhere (PLATFORM_GPU_RULES)
REQUEST_CAP = 1_000_000  # test-only serving.http.max_request_bytes, not a deployment value
TRANSPORT_BOUND_S = 120  # test-only: connect/read/write/pool/probe timeout of the loopback double; equals the request deadline


def run_cli(args, project_root, *, timeout=120, env=None):
    """The real entry point in a hidden child process with faulthandler; bounded wait."""
    # PROMPT_ENHANCER_TEST_SRC points the child at a scratch copy of `src` (mutation runs); unset means this checkout.
    environ = dict(os.environ, PYTHONPATH=os.environ.get('PROMPT_ENHANCER_TEST_SRC', str(ROOT / 'src')),
                   PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', **(env or {}))
    environ.update(env or {})
    return subprocess.run([sys.executable, '-X', 'faulthandler', '-m', 'prompt_enhancer', *map(str, args)],
                          capture_output=True, timeout=timeout, env=environ, creationflags=NO_WINDOW, cwd=project_root)


def text(done):
    return done.stdout.decode('utf-8'), done.stderr.decode('utf-8', 'replace')


@contextmanager
def model_server(handler_base=FullServerDouble):
    """A loopback HTTP model double on an ephemeral port; yields (handler class, port)."""
    handler = type('Handler', (handler_base,), {'seen': [], 'answers': []})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def pipeline_config(model_test_profile, tmp_path, project_root, port, *, retrieval=False):
    """A complete, resolved request configuration for the loopback double (every value is test-only)."""
    config = (assembly_config if retrieval else configuration)(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    # test-only: the shared runtime() deadline of 5 s expired on a loaded machine and gave a deadline-failed envelope with
    # no attachments (T-016 P3 N1); no test here measures the pipeline deadline, so it is large enough never to be the cause
    config['runtime']['request_deadline_s'] = 120
    config['storage']['data_root'] = str(tmp_path / 'data')
    config['resolution']['random_seed'] = 7
    config['serving']['http'] = {'max_request_bytes': REQUEST_CAP}
    backend = config['vlm']['backends']['main']
    backend.update(base_urls=[f'http://127.0.0.1:{port}'],
                   probe={'max_requests': 12, 'timeout_s': TRANSPORT_BOUND_S, 'image_attempts': 1})
    # test-only: the shared 1 s connect/read timeouts (max_retries 0) expired on a loaded machine (CI 42; seen locally as
    # ConnectTimeout in the trace): the planner call failed, the unit fell back to `degraded` and the double never
    # recorded its prompt. No test here measures a transport timeout, so each is a hang bound like the deadline above
    backend['http'].update(connect_timeout_s=TRANSPORT_BOUND_S, read_timeout_s=TRANSPORT_BOUND_S,
                           write_timeout_s=TRANSPORT_BOUND_S, pool_timeout_s=TRANSPORT_BOUND_S)
    backend['image']['max_decode_pixels'] = 20_000_000
    if retrieval:
        config['vlm']['backends']['embedder']['base_urls'] = backend['base_urls']
    return config


def finish_setup(config, project_root, handler):
    """Activate the profile (real registration/check with labelled doubles) and teach the double its system prompts."""
    activate(config, project_root)
    handler.writer_system = resolve_profile(config, project_root=project_root).system_message.decode('utf-8')
    handler.verifier_system = (project_root / 'prompts/verifier_v0.txt').read_bytes().decode('utf-8')


class TwoViewSync(SyncServer):
    """Labelled sync double: the observer sees one view per image, so K3 (production) labels `apparel.color` stable."""
    def handler(self, request):
        body = json.loads(request.content)
        content = body['messages'][1]['content']
        payload = json.loads(content[0]['text'] if isinstance(content, list) else content)
        if 'concept_names' not in payload:
            return super().handler(request)
        self.requests.append(body)
        self.roles.append('observer')
        value = observation(range(len(payload['concept_names'])))
        value['view'] = 'front' if self.roles.count('observer') == 1 else 'back'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]})


class KnowledgePlanner(FullServerDouble):
    """Labelled planner double: links the first shortlisted concept and asks for its colour."""
    @staticmethod
    def planner_answer(data):
        reply = planned_reply(edit_kind='local_edit' if data['task'] == 'edit' else None)
        if data['shortlist']:
            reply.update(entities=[planned_entity('e1', 'Name')],
                         links=[planned_link('Name', 'resolved', position=0, entity_id='e1')],
                         knowledge_needs=[{'candidate_position': 0, 'facets': ['apparel.color']}])
        return reply


class DraftingServer(FullServerDouble):
    """Labelled drafter double: answers the `manifest_drafter` request with a synthetic manifest draft."""
    profile_id = 'p1'
    writer_system = verifier_system = None  # no scripted writer/verifier: such replies fall to the probe answer

    def scripted_answer(self, body):
        if body['messages'][0]['content'] == (ROOT / 'prompts' / 'manifest_drafter_v0.txt').read_bytes().decode('utf-8'):
            return draft(profile_id=self.profile_id, supported_tasks=['generate'])
        return None


def published_knowledge(k2_env, count=2):
    """A real `kb sync` of `count` images (labelled observer/embedder doubles), then the source tree is deleted."""
    source = folder(k2_env, 'f', 'Name\ndescription', count=count)
    k2_env.config['observe']['representatives_per_folder'] = 2
    k2_env.config['sync']['snapshots']['keep'] = 3  # test-only: keep retired snapshots for the rollback tests
    originals = {'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(): path.read_bytes()
                 for path in source.glob('*.png')}
    invoke(k2_env, TwoViewSync())
    shutil.rmtree(source)  # the original images exist only as snapshot copies from here on
    return originals


def attaching_config(k2_env, model_test_profile, tmp_path, project_root, port, *, retention_s=None):
    """A request configuration over the store `k2_env` published, with knowledge attachments on (test-only values)."""
    config = pipeline_config(model_test_profile, tmp_path, project_root, port, retrieval=True)
    config['storage']['kb_store'] = str(k2_env.store.root)
    config['index'] = deepcopy(k2_env.config['index'])  # the serving index settings are part of the snapshot fingerprint
    config['embedding']['query_text_limit'] = k2_env.config['embedding']['query_text_limit']
    config['edit'].update(attach_kb_reference_images=True, max_kb_reference_images=2)
    config['assembly']['default_outfit'] = 'include'
    config['validation']['verifier']['require_calibration'] = False
    if retention_s is not None:
        config['runtime']['trace']['retention_s'] = retention_s
    return config


def finish_attaching_setup(config, project_root, handler):
    activate(config, project_root, renderer={'accepts_reference_images': True, 'image_reference_syntax': 'none'})
    handler.writer_system = resolve_profile(config, project_root=project_root).system_message.decode('utf-8')
    handler.verifier_system = (project_root / 'prompts/verifier_v0.txt').read_bytes().decode('utf-8')


def write_yaml(project_root, tmp_path, config, name='config.yaml'):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return path


def write_request(tmp_path, request, name='request.json'):
    path = tmp_path / name
    path.write_bytes(json.dumps(request, ensure_ascii=False).encode('utf-8'))
    return path
