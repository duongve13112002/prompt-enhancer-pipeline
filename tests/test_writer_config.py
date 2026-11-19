"""P1 explicit writer overrides/refusals and effective in-memory client requests."""

import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json

from PIL import Image
import pytest

from assembly_fixtures import ir, plan
from prompt_enhancer.config import ConfigurationError, validate_writer_reads
from prompt_enhancer.model_client import BoundModelClient, CAPABILITIES, RoleClient
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.writer import render_brief, write
from test_model_client import DoubleTransport
from test_writer import setup, view


def set_key(config, key, value, *, remove=False):
    parent = config
    parts = key.split('.')
    for part in parts[:-1]:
        parent = parent[part]
    if remove:
        del parent[parts[-1]]
    else:
        parent[parts[-1]] = value


WRITER = 'vlm.roles.writer.'
ROLE_OVERRIDES = {
    'prompt_id': 'brief_alt', 'max_tokens': 257,
    'sampling.temperature': 0.6, 'sampling.top_p': 0.7, 'sampling.top_k': 17, 'sampling.min_p': 0.1,
    'sampling.frequency_penalty': 0.1, 'sampling.presence_penalty': 0.2, 'sampling.repetition_penalty': 1.2,
    'sampling.seed': 31, 'sampling.stop': ['END'], 'reasoning.mode': 'auto', 'reasoning.effort': 'low',
    'reasoning.budget_tokens': 32, 'reasoning.template_kwargs_key': 'alternate_thinking',
    'image.source': 'edit_source_only', 'image.max_side': 2, 'image.format': 'png', 'image.quality': 91,
    'output.strict': False,
}
POLICY_OVERRIDES = {
    'creativity.mode': 'restricted', 'creativity.allowed_additions': [], 'creativity.max_new_entities': 1,
    'creativity.max_added_text_blocks': 1, 'creativity.preserve': ['literal_text'], 'creativity.edit_cap': 'strict_scope',
}
BACKEND_OVERRIDES = {
    'engine.name': 'vllm', 'engine.version': 'version-two', 'base_urls': ['http://127.0.0.1:8181'],
    'endpoints.chat': 'different/chat', 'endpoints.health': 'different/health',
    'api_key_file': '.secrets/never-read-test-key',
    'model.id': 'model-two', 'model.revision': 'revision-two', 'model.source': 'test/source',
    'model.file': 'test.gguf', 'model.mmproj_file': 'test.mmproj', 'max_concurrency': 2,
    'http.connect_timeout_s': 2, 'http.read_timeout_s': 3, 'http.write_timeout_s': 2, 'http.pool_timeout_s': 2,
    'http.max_retries': 2, 'http.backoff_initial_s': 0.1, 'http.backoff_max_s': 0.2, 'http.retry_statuses': [429],
    'probe.max_requests': 2, 'probe.timeout_s': 2, 'image.max_decode_pixels': 2000, 'image.max_side': 4,
}
FIXED = {
    'writer.system_prompt_verbatim': False, 'writer.knowledge_block': 'instructions',
    WRITER + 'mode': 'edit', WRITER + 'image.transport': 'file', WRITER + 'output.format': 'none',
    WRITER + 'output.schema_id': 'none', WRITER + 'output.transport': 'engine_native',
    WRITER + 'output.validate_response': False,
    'vlm.backends.main.unsupported_param_policy': 'ignore',
}
PATHS = [*FIXED, *POLICY_OVERRIDES, *(WRITER + key for key in ROLE_OVERRIDES),
         *('vlm.backends.main.' + key for key in BACKEND_OVERRIDES), 'writer.runtime_addendum', 'rag.enabled',
         'vlm.backends.main.addendum_layout',
         'vlm.roles.writer.backend', 'vlm.default_backend', 'vlm.backends.main.provider',
         'vision.max_image_side', 'vision.format', 'vision.alpha_policy.mode', 'vision.alpha_policy.background']


@pytest.mark.parametrize('key', PATHS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel'])
def test_every_writer_leaf_refuses_before_call(model_test_profile, tmp_path, project_root, key, failure):
    config = setup(model_test_profile, tmp_path, project_root)
    set_key(config, key, 'design_required', remove=failure == 'missing')
    with pytest.raises(ConfigurationError):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('key,value', list(FIXED.items()))
def test_fixed_protocol_and_safety_values_refuse(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    set_key(config, key, value)
    with pytest.raises(ConfigurationError):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('key,value', list(POLICY_OVERRIDES.items()))
def test_creativity_override_reaches_brief(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    set_key(config, key, value)
    validate_writer_reads(config, project_root=project_root)
    data = json.loads(render_brief(view(config), plan(ir()), None, b'{{data}}'))
    assert data['creativity'][key.split('.')[-1]] == value


@pytest.mark.parametrize('key,value', list(BACKEND_OVERRIDES.items()))
def test_backend_override_reaches_validated_binding(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    set_key(config, 'vlm.backends.main.' + key, value)
    role = validate_writer_reads(config, project_root=project_root)
    # The returned binding selects precisely the explicit backend; transport behavior stays T-009-owned.
    selected = config['vlm']['backends'][role['backend']]
    for part in key.split('.'):
        selected = selected[part]
    assert selected == value


def request(config, project_root, *, transparent=False, structured=False):
    s0 = view(config, task='edit')
    source = BytesIO()
    image = Image.new('RGBA' if transparent else 'RGB', (8, 8), (177, 73, 20, 0) if transparent else 'white')
    image.paste('black', (0, 0, 4, 4))
    image.save(source, format='PNG')
    s0.payload['source_image']['data'] = base64.b64encode(source.getvalue()).decode()
    if structured:
        s0.profile.manifest['output_schema_id'] = 'verifier_result.v0'
    transport = DoubleTransport('{"results":[]}' if structured else 'draft')  # labelled HTTP double
    client = RoleClient(config, {'main': transport},
                        {'main': ProbeResult('complete', dict.fromkeys(CAPABILITIES, True), {})},
                        SchemaRegistry(project_root / 'contracts' / 'schemas'), project_root=project_root)
    bound = BoundModelClient(type('RunDouble', (), {'client': client})(), 'writer')
    result = asyncio.run(write(s0, plan(ir(task='edit')), None, bound, project_root=project_root))
    return transport.body, json.loads(result['calls'][0]['record_json'])


def image_part(body):
    parts = body['messages'][-1]['content']
    uri = next(part['image_url']['url'] for part in parts if part['type'] == 'image_url')
    return base64.b64decode(uri.split(',', 1)[1])


def assert_inference_override(key, value, baseline, body, record):
    if key in ('sampling.repetition_penalty', 'reasoning.budget_tokens'):
        assert body == baseline
        assert ['PARAM_UNSUPPORTED', key] in record['warnings']
    elif key == 'reasoning.mode':
        assert 'chat_template_kwargs' in baseline and 'chat_template_kwargs' not in body
    elif key == 'reasoning.template_kwargs_key':
        assert body['chat_template_kwargs'] == {value: False}
    elif key == 'reasoning.effort':
        assert body['reasoning_effort'] == value
    else:
        assert body[key.rsplit('.', 1)[-1]] == value


@pytest.mark.parametrize('key,value', list(ROLE_OVERRIDES.items()))
def test_writer_role_override_changes_effective_request(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    if key in ('image.format', 'image.quality'):
        config['vision']['format'] = 'jpeg'
    structured = key == 'output.strict'
    baseline_body, _ = request(config, project_root, structured=structured)
    set_key(config, WRITER + key, value)
    if key == 'prompt_id':
        (project_root / 'prompts/brief_alt.txt').write_bytes(b'Alternate brief\n{{data}}')
    body, record = request(config, project_root, structured=structured)
    if key == 'image.source':
        assert body == baseline_body  # Both values send only the edit source on the cards path.
    elif key == 'image.max_side':
        assert Image.open(BytesIO(image_part(body))).size == (value, value)
    elif key == 'image.format':
        assert Image.open(BytesIO(image_part(body))).format == value.upper()
    elif key == 'image.quality':
        assert image_part(body) != image_part(baseline_body)
    elif key == 'output.strict':
        assert body['response_format']['json_schema']['strict'] is value
    elif key == 'prompt_id':
        assert body['messages'][-1]['content'][0]['text'].startswith('Alternate brief\n')
    else:
        assert_inference_override(key, value, baseline_body, body, record)
    assert body['messages'][0] == baseline_body['messages'][0]
    assert body['messages'][0]['content'].encode('utf-8') == b'Admin\r\n'


def test_rag_and_addendum_overrides_are_explicit(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['writer']['runtime_addendum'] = False
    validate_writer_reads(config, project_root=project_root)
    config['rag']['enabled'] = True
    validate_writer_reads(config, project_root=project_root)
    with pytest.raises(ConfigurationError, match='rag.evidence_mode'):
        config['rag']['evidence_mode'] = 'live'
        validate_writer_reads(config, project_root=project_root)


def test_backend_selection_override_and_refusal(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['vlm']['backends']['second'] = deepcopy(config['vlm']['backends']['main'])
    config['vlm']['roles']['writer']['backend'] = 'second'
    config['vlm']['default_backend'] = 'second'
    assert validate_writer_reads(config, project_root=project_root)['backend'] == 'second'
    config['vlm']['roles']['writer']['backend'] = 'absent'
    with pytest.raises(ConfigurationError):
        validate_writer_reads(config, project_root=project_root)


def test_writer_secret_path_is_lexical_only(model_test_profile, tmp_path, project_root, monkeypatch):
    config = setup(model_test_profile, tmp_path, project_root)
    # Only the configured string is tested; no credential file or secret directory is accessed.
    config['vlm']['backends']['main']['api_key_file'] = 'outside-key'
    with pytest.raises(ConfigurationError, match='api_key_file'):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('key,value', [
    ('vision.max_image_side', 3), ('vision.format', 'webp'), ('vision.alpha_policy.background', '#102030'),
])
def test_shared_vision_override_changes_request(model_test_profile, tmp_path, project_root, key, value):
    config = setup(model_test_profile, tmp_path, project_root)
    baseline, _ = request(config, project_root, transparent=True)
    set_key(config, key, value)
    changed, _ = request(config, project_root, transparent=True)
    before, after = (Image.open(BytesIO(image_part(body))) for body in (baseline, changed))
    if key == 'vision.max_image_side':
        assert after.size == (value, value) and after.size != before.size
    elif key == 'vision.format':
        assert after.format == value.upper() and after.format != before.format
    else:
        assert after.getpixel((7, 7)) == (16, 32, 48) and before.getpixel((7, 7)) != (16, 32, 48)


def test_rag_mode_refusal_is_conditional(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['rag']['evidence_mode'] = 'design_required'
    validate_writer_reads(config, project_root=project_root)
    config['rag']['enabled'] = True
    with pytest.raises(ConfigurationError, match='rag.evidence_mode'):
        validate_writer_reads(config, project_root=project_root)


@pytest.mark.parametrize('field', ['messages', 'system', 'model'])
def test_engine_extra_body_cannot_replace_writer_identity(model_test_profile, tmp_path, project_root, field):
    config = setup(model_test_profile, tmp_path, project_root)
    config['vlm']['backends']['main']['extra_body'] = {field: 'override'}
    with pytest.raises(ConfigurationError, match='extra_body.' + field):
        validate_writer_reads(config, project_root=project_root)


def test_engine_extra_body_override_reaches_request(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    config['vlm']['backends']['main']['extra_body'] = {'custom_engine_flag': True}
    body, _ = request(config, project_root)
    assert body['custom_engine_flag'] is True


def test_alpha_policy_override_reaches_request(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    before, _ = request(config, project_root, transparent=True)
    assert image_part(before)
    config['vision']['alpha_policy'] = {'mode': 'refuse'}
    body, record = request(config, project_root, transparent=True)
    assert body is None and record['status'] != 'OK'


def cloud_configuration(model_test_profile, tmp_path, project_root):
    config = setup(model_test_profile, tmp_path, project_root)
    backend = deepcopy(config['vlm']['backends']['main'])
    backend.update(provider='anthropic', engine={'name': 'anthropic'}, base_urls=['https://example.invalid'],
                   api_key_file='.secrets/never-read-test-key', call_budget=2, on_budget_exhausted='stop',
                   allow_images=False, anthropic_version='2025-01-01', anthropic_workspace_id='none')
    backend.pop('addendum_layout')  # a cloud backend refuses the openai_compatible addendum keys (T017-FIX)
    config['vlm']['backends']['synthetic_cloud'] = backend
    config['vlm']['roles']['writer'].update(backend='synthetic_cloud', image='none')
    return config


def test_cloud_writer_placeholder_resolves_before_provider_refusal(model_test_profile, tmp_path, project_root):
    from test_writer import WriterDouble
    config = cloud_configuration(model_test_profile, tmp_path, project_root)
    client, s0 = WriterDouble(), view(config)
    result = asyncio.run(write(s0, plan(ir()), None, client, project_root=project_root))
    assert result['state'] == 'draft' and len(client.calls) == 1
    s0.profile.manifest['output_schema_id'] = 'verifier_result.v0'
    with pytest.raises(ConfigurationError, match='output.*cloud structured'):
        asyncio.run(write(s0, plan(ir()), None, client, project_root=project_root))
    assert len(client.calls) == 1


@pytest.mark.parametrize('field,alternate', [('anthropic_version', '2025-02-01'), ('anthropic_workspace_id', 'workspace'),
                                           ('call_budget', 3), ('on_budget_exhausted', 'fallback_to_default_backend'),
                                           ('allow_images', True)])
def test_selected_cloud_values_override_and_refuse(model_test_profile, tmp_path, project_root, field, alternate):
    config = cloud_configuration(model_test_profile, tmp_path, project_root)
    backend = config['vlm']['backends']['synthetic_cloud']
    backend[field] = alternate
    assert validate_writer_reads(config, project_root=project_root)['backend'] == 'synthetic_cloud'
    backend[field] = 'design_required'
    with pytest.raises(ConfigurationError, match=field):
        validate_writer_reads(config, project_root=project_root)
    del backend[field]
    with pytest.raises(ConfigurationError, match=field):
        validate_writer_reads(config, project_root=project_root)
