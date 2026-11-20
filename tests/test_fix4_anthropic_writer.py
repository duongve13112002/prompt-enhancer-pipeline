"""T017-FIX4 (R-004, R-015, R-026): the writer on an Anthropic backend sends the admin bundle schema, a bundle schema
Anthropic cannot take is refused with the file named, and a failed golden case keeps its configuration refusal message.

Labelled CPU tests: in-memory HTTP doubles, synthetic schemas, a fake key file under `tmp_path`; no network call."""
import asyncio
from copy import deepcopy
import json

import pytest

from assembly_fixtures import ir, plan
from prompt_enhancer.config import ConfigurationError, validate_anthropic_writer_schema, writer_provider
from prompt_enhancer.model_cloud import anthropic_output_schema
from prompt_enhancer.model_client import ModelCallError, RoleClient
from prompt_enhancer.model_probe import PROBE_COMPILE_MAX_TOKENS, load_probe_bundle
from prompt_enhancer.model_schemas import with_output_schema
from prompt_enhancer.profile_prompt import ProfileError
from prompt_enhancer.profiles import GOLDEN_ERROR_MESSAGE_CHARS, check_profile
from prompt_enhancer.writer import prepare_write
from test_cloud2_tool_forced import ProbeDouble, probe_setup, role as cloud_role
from profile_fixtures import RunnerDouble, set_manifest
from test_golden_diagnostics import marked_bundle
from validation_fixtures import environment as golden_environment
from test_writer import setup, view

SCHEMA_ID = 'admin_caption.v1'  # test-only bundle schema id
BUNDLE = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': SCHEMA_ID, 'type': 'object',
          'required': ['caption'], 'additionalProperties': False,
          'properties': {'caption': {'type': 'string', 'minLength': 1, 'maxLength': 400}}}
MANIFEST_BOUND = {'format': 'from_profile_manifest', 'schema_id': 'from_profile_manifest', 'strict': True,
                  'transport': 'response_format', 'validate_response': True}


def cloud_writer_config(model_test_profile, tmp_path, project_root, backend='cloud_claude'):
    """The synthetic profile with the writer bound to a test-only cloud backend (`main` keeps the local one)."""
    config = setup(model_test_profile, tmp_path, project_root)
    if backend != 'main':
        provider = 'anthropic' if backend == 'cloud_claude' else 'gemini'
        cloud = deepcopy(config['vlm']['backends']['main'])
        cloud.update(provider=provider, engine={'name': provider}, base_urls=['https://example.invalid'],
                     endpoints={'chat': 'messages' if provider == 'anthropic' else 'v1beta/models/synthetic:generateContent',
                                'health': 'health'}, api_key_file='.secrets/unused-synthetic.key', call_budget=3,
                     on_budget_exhausted='stop', allow_images=False)
        if provider == 'anthropic':
            cloud.update(anthropic_version='2025-01-01', anthropic_workspace_id='none')
        cloud.pop('addendum_layout', None)  # a cloud backend refuses the openai_compatible addendum keys
        config['vlm']['backends'][backend] = cloud
    config['vlm']['roles']['writer']['backend'] = backend
    return config


def bundle_view(config, schema):
    s0 = view(config)
    s0.profile.manifest['output_schema_id'] = SCHEMA_ID
    s0.profile.output_schema = (SCHEMA_ID, schema, 'digest')
    s0.profile.output_schema_id = SCHEMA_ID
    return s0


# -- the request-path guard (writer.prepare_write) --------------------------------------------------------------------
def test_an_anthropic_writer_accepts_a_bundle_schema_anthropic_can_take(model_test_profile, tmp_path, project_root):
    config = cloud_writer_config(model_test_profile, tmp_path, project_root)
    assert writer_provider(config) == 'anthropic'
    prepared = prepare_write(bundle_view(config, BUNDLE), plan(ir()), None, project_root=project_root)
    assert prepared.role['output']['format'] == 'json_schema' and prepared.role['output']['schema_id'] == SCHEMA_ID
    assert prepared.system == b'Admin\r\n'  # R-004: the admin bytes are untouched


@pytest.mark.parametrize('bad', [
    {**BUNDLE, 'additionalProperties': True},                                                   # an open object
    {**BUNDLE, 'properties': {'caption': {'oneOf': [{'type': 'string'}, {'type': 'null'}]}}},  # a refused applicator
    {**BUNDLE, 'properties': {'caption': {'$ref': 'https://example.invalid/x'}}},               # an outside $ref
])
def test_an_anthropic_writer_refuses_a_bundle_schema_anthropic_cannot_take_naming_the_file(
        model_test_profile, tmp_path, project_root, bad):
    config = cloud_writer_config(model_test_profile, tmp_path, project_root)
    with pytest.raises(ConfigurationError, match=r'output_schema\.json: cannot be sent to Anthropic structured output'):
        prepare_write(bundle_view(config, bad), plan(ir()), None, project_root=project_root)


def test_an_anthropic_writer_checks_a_project_contract_schema_too(model_test_profile, tmp_path, project_root):
    config = cloud_writer_config(model_test_profile, tmp_path, project_root)
    s0 = view(config)
    s0.profile.manifest['output_schema_id'] = 'profile_manifest.v0'  # a contract Anthropic cannot take (open objects)
    with pytest.raises(ConfigurationError, match=r'schema profile_manifest\.v0: cannot be sent to Anthropic'):
        prepare_write(s0, plan(ir()), None, project_root=project_root)


def test_a_gemini_writer_with_a_resolved_schema_stays_refused(model_test_profile, tmp_path, project_root):
    config = cloud_writer_config(model_test_profile, tmp_path, project_root, backend='cloud_gemini')
    assert writer_provider(config) == 'gemini'
    with pytest.raises(ConfigurationError, match='cloud structured output mapping is not verified'):
        prepare_write(bundle_view(config, BUNDLE), plan(ir()), None, project_root=project_root)


def test_a_cloud_writer_with_no_schema_and_the_local_writer_are_unchanged(model_test_profile, tmp_path, project_root):
    for backend in ('cloud_claude', 'cloud_gemini', 'main'):
        config = cloud_writer_config(model_test_profile, tmp_path, project_root, backend=backend)
        prepared = prepare_write(view(config), plan(ir()), None, project_root=project_root)  # output_schema_id none
        assert prepared.role['output']['format'] == 'none'
    local = cloud_writer_config(model_test_profile, tmp_path, project_root, backend='main')
    prepared = prepare_write(bundle_view(local, BUNDLE), plan(ir()), None, project_root=project_root)
    assert prepared.role['output']['format'] == 'json_schema'  # a local writer: no Anthropic check, the bundle schema as is


def test_the_check_returns_nothing_for_a_sendable_schema_and_names_the_source():
    validate_anthropic_writer_schema(BUNDLE, 'output_schema.json')
    with pytest.raises(ConfigurationError, match='^some_file.json: cannot be sent'):
        validate_anthropic_writer_schema({**BUNDLE, 'additionalProperties': True}, 'some_file.json')


# -- the request and the probe carry the RESOLVED (bundle) schema ----------------------------------------------------
def writer_client(tmp_path, refuse=False):
    root, config, registry = probe_setup(tmp_path, 'response_format')
    config['vlm']['roles']['writer'] = cloud_role('response_format', output=dict(MANIFEST_BOUND))
    bundle, _ = load_probe_bundle(root)
    double = ProbeDouble(bundle['text_answer'], refuse)
    client = RoleClient(config, {'cloud': double}, {}, registry, project_root=root)
    client.schemas = with_output_schema(registry, (SCHEMA_ID, BUNDLE, 'digest'))
    client.output_schema_id = SCHEMA_ID
    return client, double


def test_the_start_probe_posts_the_bundle_schema_not_a_registered_one(tmp_path):
    client, double = writer_client(tmp_path)
    # the double answers the real call with a probe reply (not a draft), so only the bodies matter here
    asyncio.run(client.call('writer', b'ADMIN', 'user', manifest={'output_schema_id': SCHEMA_ID}))
    compiled = [body for body in double.bodies if body['max_tokens'] == PROBE_COMPILE_MAX_TOKENS]
    assert len(compiled) == 1
    assert compiled[0]['output_config']['format']['schema'] == anthropic_output_schema(BUNDLE)
    assert 'minLength' not in json.dumps(compiled[0]['output_config']['format']['schema'])  # stripped, never unstripped
    assert compiled[0]['output_config']['format']['schema']['$id'] == SCHEMA_ID


def test_a_bundle_schema_the_backend_refuses_stops_the_writer_before_any_real_request(tmp_path):
    client, double = writer_client(tmp_path, refuse=True)
    with pytest.raises(ModelCallError, match=r'PROBE_FAILED.*vlm\.roles\.writer\.output\.transport') as raised:
        asyncio.run(client.call('writer', b'ADMIN', 'user', manifest={'output_schema_id': SCHEMA_ID}))
    assert raised.value.code == 'PROBE_FAILED'
    real = [body for body in double.bodies if body['max_tokens'] != PROBE_COMPILE_MAX_TOKENS
            and body.get('system') == 'ADMIN']
    assert real == []  # only probe bodies went out (their system text is empty)


def test_the_real_request_carries_the_stripped_bundle_schema_and_the_admin_system_text_first(tmp_path):
    client, double = writer_client(tmp_path)
    asyncio.run(client.call('writer', b'ADMIN', 'user', manifest={'output_schema_id': SCHEMA_ID}))
    real = [body for body in double.bodies if body.get('system') == 'ADMIN']
    assert len(real) == 1
    assert real[0]['output_config']['format']['schema'] == anthropic_output_schema(BUNDLE)
    assert real[0]['system'] == 'ADMIN' and real[0]['messages'][0]['content'] == 'user'
    assert BUNDLE['properties']['caption']['minLength'] == 1  # the input schema object is never mutated
    assert deepcopy(BUNDLE) == BUNDLE


# -- the profile load check (profile check activation and profile resolve share `_view`) -------------------------------
def anthropic_writer_environment(model_test_profile, tmp_path, project_root, bundle_schema):
    env = golden_environment(model_test_profile, tmp_path, project_root)
    folder = marked_bundle(env, project_root)
    set_manifest(folder, golden_tests=['g1'], output_schema_id=SCHEMA_ID)
    (folder / 'output_schema.json').write_text(json.dumps(bundle_schema), encoding='utf-8')
    backends = env.config['vlm']['backends']
    cloud = deepcopy(backends['main'])
    cloud.update(provider='anthropic', engine={'name': 'anthropic'}, base_urls=['https://example.invalid'],
                 endpoints={'chat': 'messages', 'health': 'health'}, api_key_file='.secrets/unused-synthetic.key',
                 call_budget=3, on_budget_exhausted='stop', allow_images=False,
                 anthropic_version='2025-01-01', anthropic_workspace_id='none')
    cloud.pop('addendum_layout', None)
    backends['cloud'] = cloud
    env.config['vlm']['roles']['writer']['backend'] = 'cloud'
    return env, folder


def test_profile_check_refuses_a_bundle_schema_anthropic_cannot_take_before_any_case_runs(
        model_test_profile, tmp_path, project_root):
    env, folder = anthropic_writer_environment(model_test_profile, tmp_path, project_root,
                                               {**BUNDLE, 'additionalProperties': True})
    runner = RunnerDouble()
    with pytest.raises(ProfileError, match=r'output_schema\.json: cannot be sent to Anthropic') as raised:
        asyncio.run(check_profile(env.config, 'p1', runner, project_root=project_root))
    assert raised.value.code == 'PROFILE_OUTPUT_SCHEMA_INVALID' and runner.calls == []
    assert not (folder / 'activation.json').exists()


def test_profile_check_accepts_a_sendable_bundle_schema_on_an_anthropic_writer(
        model_test_profile, tmp_path, project_root):
    env, folder = anthropic_writer_environment(model_test_profile, tmp_path, project_root, BUNDLE)
    runner = RunnerDouble()
    asyncio.run(check_profile(env.config, 'p1', runner, project_root=project_root))
    assert len(runner.calls) == 1 and (folder / 'activation.json').exists()


def test_a_local_writer_keeps_an_open_bundle_schema(model_test_profile, tmp_path, project_root):
    env, folder = anthropic_writer_environment(model_test_profile, tmp_path, project_root,
                                               {**BUNDLE, 'additionalProperties': True})
    env.config['vlm']['roles']['writer']['backend'] = 'main'  # the local writer: no Anthropic restriction applies
    asyncio.run(check_profile(env.config, 'p1', RunnerDouble(), project_root=project_root))
    assert (folder / 'activation.json').exists()


# -- golden_results.json keeps a configuration refusal's message ------------------------------------------------------
def golden_case(model_test_profile, tmp_path, project_root, error):
    env = golden_environment(model_test_profile, tmp_path, project_root)
    folder = marked_bundle(env, project_root)
    set_manifest(folder, golden_tests=['g1'])
    with pytest.raises(ProfileError, match='PROFILE_GOLDEN_FAILED'):
        asyncio.run(check_profile(env.config, 'p1', RunnerDouble(error=error), project_root=project_root))
    raw = (folder / 'golden_results.json').read_bytes()
    return json.loads(raw)['cases'][0], raw


def test_a_failed_golden_case_keeps_the_configuration_refusal_message(model_test_profile, tmp_path, project_root):
    message = 'vlm.roles.writer.output: some key is refused'
    case, _ = golden_case(model_test_profile, tmp_path, project_root, ConfigurationError(message))
    assert case['error'] == 'ConfigurationError' and case['error_message'] == message


def test_the_kept_message_is_bounded(model_test_profile, tmp_path, project_root):
    case, _ = golden_case(model_test_profile, tmp_path, project_root,
                          ConfigurationError('k' * (GOLDEN_ERROR_MESSAGE_CHARS + 500)))
    assert case['error_message'] == 'k' * GOLDEN_ERROR_MESSAGE_CHARS


@pytest.mark.parametrize('error', [ModelCallError('PROBE_MALFORMED: ZQX-reply-text'), ValueError('ZQX-reply-text')])
def test_another_exception_stays_a_class_and_a_code_never_its_text(model_test_profile, tmp_path, project_root, error):
    case, raw = golden_case(model_test_profile, tmp_path, project_root, error)
    assert 'error_message' not in case and b'ZQX-reply-text' not in raw  # R-026
