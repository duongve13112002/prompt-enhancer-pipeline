"""T017-FIX3 (decisions 197-199; R-009, R-013): the golden retrieval switch and grounding judgement.

Labelled CPU replay: the production `Pipeline`, `GoldenAdapter`, intake, S4-S6 and `check_profile` run with
`rag.enabled: true`; every knowledge entry point (store open, S1 embedder and candidates, S3 assembly) is replaced by a
recorder, and the store open fails like an unreadable store, so an `enabled` case reaches the knowledge boundary
and reports the closed S1-boundary code while a `disabled` case in the same check touches nothing.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from planner_fixtures import entity, reply
from profile_fixtures import SECTIONED, DrafterDouble, bundle, draft, install_prompts, set_manifest, write_golden
from prompt_enhancer import profiles
from prompt_enhancer.pipeline import GoldenAdapter, Pipeline
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.profiles import check_profile, confirm_profile, golden_codes, register_profile
from prompt_enhancer.storage import StorageError
from validation_fixtures import environment


def registered(env, project_root):
    install_prompts(project_root)
    folder = bundle(Path(env.config['profiles']['root']).parent, 'p1', SECTIONED)
    asyncio.run(register_profile(env.config, 'p1', DrafterDouble(draft(profile_id='p1')), project_root=project_root))
    confirm_profile(env.config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    return folder


class OwnerDouble:
    """Labelled model-owner double: planner-bound with a minimal valid synthetic reply; the writer is the S5 double."""
    role_name = 'planner'

    def __init__(self, writer, bound=None):
        self.writer, self.bound = writer, bound if bound is not None else []

    def use_output_schema(self, output_schema):
        self.bound.append(output_schema)

    async def call(self, system, user, **kwargs):
        return reply(entities=[entity('e1', 'a bowl')]), {
            'role': 'planner', 'status': 'OK', 'request_hash': 'test-only', 'image_roles': [], 'warnings': []}

    async def for_role(self, role):
        return self.writer

    async def close(self):
        pass


@pytest.fixture
def ragon(model_test_profile, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.intake as intake_module
    import prompt_enhancer.k1_embed as k1_module
    import prompt_enhancer.pipeline as module
    import prompt_enhancer.storage as storage_module

    env = environment(model_test_profile, tmp_path, project_root)
    env.config['storage']['data_root'] = str(tmp_path / 'data')
    env.config['rag']['enabled'] = True
    folder = registered(env, project_root)
    env.writer.replies = ['kept'] * 8
    touched = []

    def store(config, *args, **kwargs):
        touched.append(('store', config['rag']['enabled']))
        raise StorageError('synthetic unreadable store')  # an S0 knowledge failure, no data

    def stage(name):
        def record(*args, **kwargs):
            touched.append((name, None))
            raise AssertionError(f'{name} ran')
        return record

    bound = []

    async def compose(*args, **kwargs):
        return OwnerDouble(env.writer, bound)

    monkeypatch.setattr(intake_module, 'open_knowledge_store', store)
    monkeypatch.setattr(storage_module, 'open_knowledge_store', store)
    monkeypatch.setattr(module, 'build_candidates', stage('s1'))
    monkeypatch.setattr(module, 'build_assembly', stage('s3'))
    monkeypatch.setattr(k1_module, 'Embedder', stage('embedder'))
    monkeypatch.setattr(module, 'compose_model_client', compose)
    return SimpleNamespace(env=env, folder=folder, touched=touched, root=project_root, bound=bound)


def check(ragon, adapter=None):
    async def run():
        async with Pipeline(ragon.env.config, project_root=ragon.root) as pipeline, \
                GoldenAdapter(pipeline) as runner:
            await check_profile(ragon.env.config, 'p1', adapter or runner, project_root=ragon.root)
    asyncio.run(run())


def results(ragon):
    return {case['id']: case for case in json.loads((ragon.folder / 'golden_results.json').read_bytes())['cases']}


def test_disabled_case_opens_no_store_while_enabled_case_still_retrieves(ragon):
    write_golden(ragon.folder, 'off', ['c1'], expect=('success', 'degraded'))
    set_manifest(ragon.folder, golden_tests=['off'])
    check(ragon)  # only the retrieval-off case: it passes and touches no knowledge entry point
    assert ragon.touched == []
    assert results(ragon)['off']['passed'] is True
    assert ragon.bound == []  # `output_schema_id: none`: nothing is bound, so the writer sends no schema
    write_golden(ragon.folder, 'on', ['c1'], retrieval='enabled', expect=('failed',))
    set_manifest(ragon.folder, golden_tests=['off', 'on'])
    check(ragon)  # the enabled case reaches the store (which fails: an S0 refusal, `failed`, its listed status)...
    assert ragon.touched == [('store', True)]  # ...while the disabled case of the same check opened nothing
    cases = results(ragon)
    assert cases['off']['passed'] is True and cases['on']['passed'] is True


def test_pipeline_binds_the_bundle_schema_to_each_request_client(ragon):
    """Decision 194: the request's checked view carries the bundle schema and the pipeline binds it per request."""
    schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'admin_caption.v1', 'type': 'string'}
    raw = json.dumps(schema).encode('utf-8')
    (ragon.folder / profiles.OUTPUT_SCHEMA_FILE).write_bytes(raw)
    path = ragon.folder / 'manifest.yaml'
    path.write_bytes(path.read_bytes().replace(b"output_schema_id: none", b"output_schema_id: admin_caption.v1"))
    write_golden(ragon.folder, 'off', ['c1'], expect=('success', 'degraded', 'failed'))
    set_manifest(ragon.folder, golden_tests=['off'])
    check(ragon)
    assert ragon.bound == [('admin_caption.v1', schema, __import__('hashlib').sha256(raw).hexdigest())]


def test_enabled_case_envelope_carries_the_closed_knowledge_code(ragon):
    seen = []

    class Recording(GoldenAdapter):
        async def run_case(self, profile, request):
            envelope, diagnostics = await super().run_case(profile, request)
            seen.append((request['retrieval'], envelope['status'], envelope['retrieval']))
            return envelope, diagnostics

    write_golden(ragon.folder, 'on', ['c1'], retrieval='enabled', expect=('failed',))
    write_golden(ragon.folder, 'off', ['c1'], expect=('success', 'degraded'))
    set_manifest(ragon.folder, golden_tests=['on', 'off'])

    async def run():
        async with Pipeline(ragon.env.config, project_root=ragon.root) as pipeline, Recording(pipeline) as runner:
            await check_profile(ragon.env.config, 'p1', runner, project_root=ragon.root)
    asyncio.run(run())
    assert seen[0] == ('enabled', 'failed', {'state': 'failed', 'snapshot_id': None, 'error': 'KNOWLEDGE_FAILED'})
    assert seen[1][0] == 'disabled' and seen[1][2] == {'state': 'disabled', 'snapshot_id': None, 'error': None}
    assert ragon.touched == [('store', True)]


def test_retired_golden_v0_case_is_refused_by_name(ragon):
    write_golden(ragon.folder, 'g1', ['c1'])
    path = ragon.folder / 'golden/g1.yaml'
    case = json.loads(json.dumps(__import__('yaml').safe_load(path.read_bytes())))
    case['schema_version'] = 1
    del case['request']['retrieval'], case['expect']['grounding']
    path.write_text(json.dumps(case), encoding='utf-8')
    set_manifest(ragon.folder, golden_tests=['g1'])
    with pytest.raises(ProfileError, match=r'golden_case\.v0\) is retired.*golden_case\.v1'):
        check(ragon)
    assert ragon.touched == []


def test_enabled_case_under_retrieval_off_is_refused_before_any_run(ragon):
    ragon.env.config['rag']['enabled'] = False
    write_golden(ragon.folder, 'off', ['c1'])
    write_golden(ragon.folder, 'on', ['c1'], retrieval='enabled')
    set_manifest(ragon.folder, golden_tests=['off', 'on'])
    ran = []

    class Never:
        identity = 'test-double:never'

        async def run(self, view, request):
            ran.append(request)

    with pytest.raises(ProfileError, match='on: request.retrieval is enabled but rag.enabled is false'):
        check(ragon, Never())
    assert ran == [] and ragon.touched == []


def case(retrieval='enabled', grounding='required', status=('success', 'repaired')):
    return {'request': {'retrieval': retrieval}, 'expect': {'status': list(status), 'grounding': grounding}}


def envelope(status, state):
    return {'status': status, 'retrieval': {'state': state}}


@pytest.mark.parametrize(('value', 'result', 'codes'), [
    (case(), envelope('success', 'retrieved'), []),
    (case(), envelope('repaired', 'retrieved'), []),
    (case(), envelope('degraded', 'retrieved'), [profiles.GOLDEN_STATUS_UNEXPECTED, profiles.GOLDEN_GROUNDING_DEGRADED]),
    (case(status=('success', 'degraded')), envelope('degraded', 'retrieved'), [profiles.GOLDEN_GROUNDING_DEGRADED]),
    (case(), envelope('success', 'empty'), [profiles.GOLDEN_NOT_GROUNDED]),
    (case(), envelope('success', 'not_reached'), [profiles.GOLDEN_NOT_GROUNDED]),
    (case(), envelope('degraded', 'failed'), [profiles.GOLDEN_STATUS_UNEXPECTED, profiles.GOLDEN_GROUNDING_DEGRADED,
                                              profiles.GOLDEN_NOT_GROUNDED]),
    (case('disabled', 'not_required', ('success',)), envelope('success', 'disabled'), []),
    (case('disabled', 'not_required', ('success',)), envelope('success', 'retrieved'),
     [profiles.GOLDEN_RETRIEVAL_NOT_DISABLED]),
    (case('enabled', 'not_required', ('degraded',)), envelope('degraded', 'failed'), []),
])
def test_golden_codes(value, result, codes):
    """Decision 199: degraded never passes a grounding case, even when a hand-edited case lists it."""
    assert golden_codes(value, result) == codes


@pytest.mark.parametrize('expect', [{'status': ['degraded'], 'grounding': 'required'},
                                    {'status': ['success', 'degraded'], 'grounding': 'required'}])
def test_case_schema_refuses_degraded_on_a_grounding_case(ragon, expect):
    write_golden(ragon.folder, 'g1', ['c1'], retrieval='enabled', grounding='required')
    path = ragon.folder / 'golden/g1.yaml'
    value = __import__('yaml').safe_load(path.read_bytes())
    value['expect'] = expect
    path.write_text(json.dumps(value), encoding='utf-8')
    set_manifest(ragon.folder, golden_tests=['g1'])
    with pytest.raises(ProfileError, match='PROFILE_GOLDEN_INVALID'):
        check(ragon)
    assert ragon.touched == []
