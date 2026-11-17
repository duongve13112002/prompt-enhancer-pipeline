from copy import deepcopy
from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config, load_profile, resolve_writer_format
from prompt_enhancer.config import SENTINELS, _command_reads


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'configs/enhancer.example.yaml'
PROFILE = ROOT / 'configs/profiles/_example'


def save_config(tmp_path, config):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return path


@pytest.fixture
def config():
    result = yaml.safe_load(BASE.read_bytes())
    result['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only: resolves deploy_required (T-006)
    return result


def test_config_override(config, tmp_path):
    config['storage']['kb_store'] = 'data/alternate'
    result = load_config(save_config(tmp_path, config), 'kb status')
    assert result['storage']['kb_store'] == 'data/alternate'


@pytest.mark.parametrize(('section', 'key', 'value'), [
    ('vision', 'max_image_side', 640),
    ('profiles', 'root', 'configs/other'),
    ('storage', 'data_root', 'data/other'),
    ('clarification', 'mode', 'ask'),
    ('rag', 'enabled', False),
    ('serving', 'prefix_cache', False),
])
def test_additional_overrides(config, tmp_path, section, key, value):
    config[section][key] = value
    result = load_config(save_config(tmp_path, config), 'kb status')
    assert result[section][key] == value


@pytest.mark.parametrize('failure', ['missing', 'unknown', 'wrong_type'])
def test_config_refusal(config, tmp_path, failure):
    if failure == 'missing':
        del config['storage']['kb_store']
    elif failure == 'unknown':
        config['storage']['unexpected'] = 'value'
    else:
        config['storage']['kb_store'] = 7
    path = save_config(tmp_path, config)
    key = {'missing': 'kb_store', 'unknown': 'unexpected', 'wrong_type': 'kb_store'}[failure]
    with pytest.raises(ConfigurationError, match='storage.*' + key) as error:
        load_config(path, 'kb status')
    assert str(path) in str(error.value)


@pytest.mark.parametrize('sentinel', sorted(SENTINELS))
def test_read_sentinel_is_refused(config, tmp_path, sentinel):
    config['storage']['kb_store'] = sentinel
    with pytest.raises(ConfigurationError, match='storage.kb_store.*' + sentinel):
        load_config(save_config(tmp_path, config), 'kb status')


def test_unread_sentinel_is_retained(config, tmp_path):
    config['knowledge']['root'] = 'deploy_required'
    assert load_config(save_config(tmp_path, config), 'kb status')['knowledge']['root'] == 'deploy_required'


def test_unread_nested_conditional_sentinel_is_retained(config, tmp_path):
    config['vlm']['roles']['planner']['output']['validate_response'] = 'design_required'
    result = load_config(save_config(tmp_path, config), 'kb status')
    assert result['vlm']['roles']['planner']['output']['validate_response'] == 'design_required'


def test_unknown_command(config, tmp_path):
    with pytest.raises(ConfigurationError, match='command.*unknown'):
        load_config(save_config(tmp_path, config), 'kb unknown')


@pytest.mark.parametrize('command', ['enhance once', 'enhance serve', 'eval run'])
def test_retrieval_read_set(config, command):
    config['rag']['enabled'] = True
    enabled = _command_reads(command, config)
    assert 'storage.kb_store' in enabled and 'embedding' in enabled and 'candidates' in enabled
    config['rag']['enabled'] = False
    disabled = _command_reads(command, config)
    assert 'rag.enabled' in disabled
    assert not {'storage.kb_store', 'embedding', 'candidates', 'assembly'} & set(disabled)
    assert 'vlm.roles.planner' in disabled


def test_retrieval_block_sentinel_is_reported(config, tmp_path):
    config['profiles']['default_profile_id'] = 'profile'
    config['rag'] = 'design_required'
    with pytest.raises(ConfigurationError, match='rag.*design_required'):
        load_config(save_config(tmp_path, config), 'enhance once')


def test_command_role_and_backend_validation(config, tmp_path):
    role = config['vlm']['roles']['manifest_drafter']
    role['max_tokens'] = 128
    role['sampling'] = dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0,
                            presence_penalty=0.0, frequency_penalty=0.0,
                            repetition_penalty=1.0, seed='none', stop='none')
    role['reasoning'] = dict(mode='off', effort='none', budget_tokens='none',
                             template_kwargs_key='enable_thinking')
    backend = config['vlm']['backends']['main']
    backend['engine'] = {'name': 'llama_cpp', 'version': 'test-version'}
    backend['base_urls'] = ['http://127.0.0.1:8080']
    backend['endpoints'] = {'chat': 'chat', 'health': 'health'}
    backend['model']['id'] = 'local-model'
    backend['model']['revision'] = 'revision'
    backend['max_concurrency'] = 1
    backend['http'] = dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                           max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[])
    backend['probe'] = dict(max_requests=1, timeout_s=1)
    backend['image'] = dict(max_decode_pixels=1000, max_side='none')
    path = save_config(tmp_path, config)
    assert load_config(path, 'profile register')['vlm']['roles']['manifest_drafter']['max_tokens'] == 128
    role['sampling']['temperature'] = 'design_required'
    with pytest.raises(ConfigurationError, match='vlm.roles.manifest_drafter.sampling.temperature'):
        load_config(save_config(tmp_path, config), 'profile register')
    role['sampling']['temperature'] = 0.2
    backend['model']['revision'] = 'pin_required'
    with pytest.raises(ConfigurationError, match='vlm.backends.main.model.revision'):
        load_config(save_config(tmp_path, config), 'profile register')


@pytest.mark.parametrize(('section', 'field', 'sentinel'), [
    ('http', 'connect_timeout_s', 'deploy_required'), ('http', 'read_timeout_s', 'deploy_required'),
    ('http', 'write_timeout_s', 'deploy_required'), ('http', 'pool_timeout_s', 'deploy_required'),
    ('http', 'max_retries', 'design_required'), ('http', 'backoff_initial_s', 'design_required'),
    ('http', 'backoff_max_s', 'design_required'), ('http', 'retry_statuses', 'design_required'),
    ('probe', 'max_requests', 'design_required'), ('probe', 'timeout_s', 'design_required'),
    ('image', 'max_decode_pixels', 'deploy_required'),
    ('engine', 'name', 'deploy_required'), ('engine', 'version', 'pin_required'),
])
def test_model_backend_read_refuses_operational_sentinel(config, tmp_path, section, field, sentinel):
    role = config['vlm']['roles']['manifest_drafter']
    role['max_tokens'] = 128
    role['sampling'] = dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0,
                            presence_penalty=0.0, frequency_penalty=0.0,
                            repetition_penalty=1.0, seed='none', stop='none')
    role['reasoning'] = dict(mode='off', effort='none', budget_tokens='none',
                             template_kwargs_key='enable_thinking')
    backend = config['vlm']['backends']['main']
    backend['engine'] = {'name': 'llama_cpp', 'version': 'test-version'}
    backend['base_urls'] = ['http://127.0.0.1:8080']
    backend['endpoints'] = {'chat': 'chat', 'health': 'health'}
    backend['model']['id'] = 'local-model'
    backend['model']['revision'] = 'revision'
    backend['max_concurrency'] = 1
    backend['http'] = dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                           max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[])
    backend['probe'] = dict(max_requests=1, timeout_s=1)
    backend['image'] = dict(max_decode_pixels=1000, max_side='none')
    backend[section][field] = sentinel
    with pytest.raises(ConfigurationError, match=field + '.*unresolved sentinel'):
        load_config(save_config(tmp_path, config), 'profile register')
def test_yaml_boolean_enum_trap(config, tmp_path):
    path = save_config(tmp_path, config)
    text = path.read_text(encoding='utf-8').replace('mode: best_effort', 'mode: off', 1)
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ConfigurationError, match='clarification'):
        load_config(path, 'kb status')


def test_invalid_role(config, tmp_path):
    config['vlm']['roles']['planner']['reasoning'] = deepcopy(config['vlm']['roles']['planner']['reasoning'])
    config['vlm']['roles']['planner']['reasoning']['mode'] = False
    with pytest.raises(ConfigurationError, match='vlm.roles.planner.reasoning.mode'):
        load_config(save_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('role', ['planner', 'writer', 'verifier', 'observer', 'info_assessor',
                                  'folder_namer', 'card_builder', 'alias_generator', 'manifest_drafter',
                                  'scenario_generator'])
def test_role_mode_refused(config, tmp_path, role):
    config['vlm']['roles'][role]['mode'] = 'unsupported'
    with pytest.raises(ConfigurationError, match='vlm.roles.' + role + '.mode'):
        load_config(save_config(tmp_path, config), 'kb status')


def test_unknown_named_schema(config, tmp_path):
    config['vlm']['roles']['planner']['output']['schema_id'] = 'missing.v0'
    with pytest.raises(ConfigurationError, match='vlm.roles.planner.output.schema_id'):
        load_config(save_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('policy', [None, 'stop'])
@pytest.mark.parametrize('name', ['main', 'cloud_claude', 'cloud_gemini'])
def test_backend_policy_refused(config, tmp_path, policy, name):
    backend = config['vlm']['backends'][name]
    if policy is None:
        del backend['unsupported_param_policy']
    else:
        backend['unsupported_param_policy'] = policy
    with pytest.raises(ConfigurationError, match='unsupported_param_policy'):
        load_config(save_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('name', ['cloud_claude', 'cloud_gemini'])
@pytest.mark.parametrize('key', ['call_budget', 'on_budget_exhausted', 'allow_images'])
def test_cloud_backend_requires_policy_fields(config, tmp_path, name, key):
    del config['vlm']['backends'][name][key]
    with pytest.raises(ConfigurationError, match='vlm.backends.' + name + '.' + key):
        load_config(save_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize(('name', 'key', 'value'), [
    ('main', 'provider', 'anthropi'),
    ('cloud_claude', 'on_budget_exhausted', 'ignore'),
])
def test_backend_enum_refused(config, tmp_path, name, key, value):
    config['vlm']['backends'][name][key] = value
    with pytest.raises(ConfigurationError, match='vlm.backends.' + name + '.' + key):
        load_config(save_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('role', ['planner', 'writer', 'verifier', 'observer', 'info_assessor',
                                  'folder_namer', 'card_builder', 'alias_generator', 'manifest_drafter',
                                  'scenario_generator'])
def test_role_validation_override_without_schema(config, tmp_path, role):
    output = config['vlm']['roles'][role]['output']
    output.update(format='none', schema_id='none', validate_response=False)
    result = load_config(save_config(tmp_path, config), 'kb status')
    assert result['vlm']['roles'][role]['output']['validate_response'] is False
    output.update(format='json_schema', schema_id='intent_ir.v0')
    with pytest.raises(ConfigurationError, match='vlm.roles.' + role + '.output.validate_response'):
        load_config(save_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('payload', [b'alpha\nbeta\n', b'alpha\r\nbeta\r\n'])
def test_prompt_byte_identity(tmp_path, payload):
    (tmp_path / 'manifest.yaml').write_bytes((PROFILE / 'manifest.yaml').read_bytes())
    (tmp_path / 'system_prompt.txt').write_bytes(payload)
    _, prompt = load_profile(tmp_path)
    assert prompt == payload


def test_prompt_missing(tmp_path):
    (tmp_path / 'manifest.yaml').write_bytes((PROFILE / 'manifest.yaml').read_bytes())
    with pytest.raises(ConfigurationError, match='system_prompt.txt'):
        load_profile(tmp_path)


def test_prompt_unreadable(tmp_path, monkeypatch):
    (tmp_path / 'manifest.yaml').write_bytes((PROFILE / 'manifest.yaml').read_bytes())
    original = Path.read_bytes

    def fail_prompt(path):
        if path.name == 'system_prompt.txt':
            raise PermissionError('labelled permission double')
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', fail_prompt)
    with pytest.raises(ConfigurationError, match='system_prompt.txt.*permission double'):
        load_profile(tmp_path)


def test_writer_resolution(config):
    role = config['vlm']['roles']['writer']
    original = deepcopy(role)
    assert resolve_writer_format(role, {'output_schema_id': 'none'})['output']['format'] == 'none'
    result = resolve_writer_format(role, {'output_schema_id': 'intent_ir.v0'})
    assert result['output']['format'] == 'json_schema'
    assert result['output']['schema_id'] == 'intent_ir.v0'
    assert role == original
    with pytest.raises(ConfigurationError, match='output_schema_id'):
        resolve_writer_format(role, {})
    with pytest.raises(ConfigurationError, match='schema_id'):
        resolve_writer_format(role, {'output_schema_id': 'missing.v0'})


@pytest.mark.parametrize(('key', 'value'), [
    ('profile_id', 'alternative'),
    ('status', 'confirmed'),
    ('template_format', 'plain'),
    ('output_kind', 'prose'),
    ('output_schema_id', 'intent_ir.v0'),
    ('output_language', 'Vietnamese'),
    ('quote_semantics', 'alternative'),
    ('supported_tasks', ['generate']),
])
def test_manifest_override(tmp_path, key, value):
    manifest = yaml.safe_load((PROFILE / 'manifest.yaml').read_bytes())
    manifest[key] = value
    (tmp_path / 'manifest.yaml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
    (tmp_path / 'system_prompt.txt').write_bytes(b'example')
    loaded, _ = load_profile(tmp_path)
    assert loaded[key] == value


@pytest.mark.parametrize('failure', ['missing', 'unknown', 'wrong_type'])
def test_manifest_refusal(tmp_path, failure):
    manifest = yaml.safe_load((PROFILE / 'manifest.yaml').read_bytes())
    if failure == 'missing':
        del manifest['output_kind']
    elif failure == 'unknown':
        manifest['unknown'] = 'value'
    else:
        manifest['output_kind'] = False
    (tmp_path / 'manifest.yaml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
    (tmp_path / 'system_prompt.txt').write_bytes(b'example')
    with pytest.raises(ConfigurationError, match='manifest.yaml'):
        load_profile(tmp_path)


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize(('key', 'replacement', 'invalid'), [
    ('id', 'new-id', False),
    ('profile_rule', 'alternate rule', False),
    ('conflicts_with', 'alternate policy', False),
    ('decision', 'profile_wins', 'invalid'),
    ('decided_by_admin', True, 'invalid'),
])
def test_manifest_conflict_fields(tmp_path, index, key, replacement, invalid):
    manifest = yaml.safe_load((PROFILE / 'manifest.yaml').read_bytes())
    manifest['conflicts'][index][key] = replacement
    path = tmp_path / 'manifest.yaml'
    path.write_text(yaml.safe_dump(manifest), encoding='utf-8')
    (tmp_path / 'system_prompt.txt').write_bytes(b'example')
    loaded, _ = load_profile(tmp_path)
    assert loaded['conflicts'][index][key] == replacement
    manifest['conflicts'][index][key] = invalid
    path.write_text(yaml.safe_dump(manifest), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='conflicts'):
        load_profile(tmp_path)


@pytest.mark.parametrize('index', range(3))
@pytest.mark.parametrize('key', ['id', 'note'])
def test_embedding_candidate_fields(config, tmp_path, index, key):
    config['embedding']['candidates'][index][key] = 'alternative'
    result = load_config(save_config(tmp_path, config), 'kb status')
    assert result['embedding']['candidates'][index][key] == 'alternative'
    config['embedding']['candidates'][index][key] = False
    with pytest.raises(ConfigurationError, match='embedding.candidates'):
        load_config(save_config(tmp_path, config), 'kb status')


def test_merge_cycle(tmp_path):
    path = tmp_path / 'cycle.yaml'
    path.write_text('schema_version: 1\nextends: cycle.yaml\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='cycle'):
        load_config(path, 'kb status')


def test_relative_extends_and_overlay(config, tmp_path):
    save_config(tmp_path, config)
    child = tmp_path / 'child.yaml'
    child.write_text('schema_version: 1\nextends: config.yaml\nstorage:\n  kb_store: data/derived\n', encoding='utf-8')
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text('schema_version: 1\noverlay_of: config.yaml\nstorage:\n  kb_store: data/overlay\n', encoding='utf-8')
    assert load_config(child, 'kb status')['storage']['kb_store'] == 'data/derived'
    assert load_config(overlay, 'kb status')['storage']['kb_store'] == 'data/overlay'
    assert load_config(child, 'kb status', overlay=overlay)['storage']['kb_store'] == 'data/overlay'
    child.write_text('schema_version: 1\nextends: missing.yaml\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='missing.yaml'):
        load_config(child, 'kb status')
    overlay.write_text('schema_version: 1\noverlay_of: missing.yaml\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='missing.yaml'):
        load_config(overlay, 'kb status')
    with pytest.raises(ConfigurationError, match='overlay_of.*does not match'):
        load_config(tmp_path / 'config.yaml', 'kb status', overlay=overlay)


def test_error_names_override_source(config, tmp_path):
    base = save_config(tmp_path, config)
    child = tmp_path / 'child.yaml'
    child.write_text('schema_version: 1\nextends: config.yaml\nvision:\n  quality: false\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='child.yaml.*vision.quality'):
        load_config(child, 'kb status')
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text('schema_version: 1\noverlay_of: config.yaml\nstorage:\n  kb_store: deploy_required\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='overlay.yaml.*storage.kb_store'):
        load_config(base, 'kb status', overlay=overlay)


def test_overlay_rejects_policy(config, tmp_path):
    base = save_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text('schema_version: 1\noverlay_of: config.yaml\nvlm:\n  backends:\n    main:\n      unsupported_param_policy: warn\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='unsupported_param_policy'):
        load_config(base, 'kb status', overlay=overlay)


@pytest.mark.parametrize(('key', 'value'), [
    ('endpoints', {'chat': 'new-chat', 'health': '/new-health'}),
    ('extra_body', {'chat_template_kwargs': {'enable_thinking': True}}),
    ('http', {'connect_timeout_s': 1, 'read_timeout_s': 2, 'write_timeout_s': 3, 'pool_timeout_s': 4,
              'max_retries': 1, 'backoff_initial_s': 0, 'backoff_max_s': 1, 'retry_statuses': [503]}),
    ('probe', {'max_requests': 2, 'timeout_s': 3}),
    ('image', {'max_decode_pixels': 1000, 'max_side': 512}),  # decision 144: a deployment sets its backend cap
    ('anthropic_version', '2025-01-01'),
    ('anthropic_workspace_id', 'workspace-123'),
    ('engine', {'name': 'vllm', 'version': 'test-version'}),
])
def test_overlay_engine_overrides(config, tmp_path, key, value):
    base = save_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': 'config.yaml', 'vlm': {'backends': {'main': {key: value}}}}), encoding='utf-8')
    result = load_config(base, 'kb status', overlay=overlay)
    assert result['vlm']['backends']['main'][key] == value
    overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': 'config.yaml', 'vlm': {'backends': {'main': {key: False}}}}), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='vlm.backends.main'):
        load_config(base, 'kb status', overlay=overlay)


def test_overlay_thinking_type(config, tmp_path):
    base = save_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    body = {'schema_version': 1, 'overlay_of': 'config.yaml', 'vlm': {'backends': {'main': {
        'extra_body': {'chat_template_kwargs': {'enable_thinking': 'invalid'}}}}}}
    overlay.write_text(yaml.safe_dump(body), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='enable_thinking'):
        load_config(base, 'kb status', overlay=overlay)


def test_version_refused(config, tmp_path):
    config['schema_version'] = 2
    with pytest.raises(ConfigurationError, match='schema_version'):
        load_config(save_config(tmp_path, config), 'kb status')


def test_profile_and_overlay_versions_refused(config, tmp_path):
    base = save_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text('schema_version: true\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='overlay.yaml.*schema_version'):
        load_config(base, 'kb status', overlay=overlay)
    manifest = yaml.safe_load((PROFILE / 'manifest.yaml').read_bytes())
    manifest['schema_version'] = 2
    (tmp_path / 'manifest.yaml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='manifest.yaml.*schema_version'):
        load_profile(tmp_path)


def test_path_outside_project_is_refused():
    with pytest.raises(ConfigurationError, match='outside project root'):
        load_config(ROOT.parent / 'outside.yaml', 'kb status')


@settings(max_examples=20, deadline=None)
@given(value=st.integers() | st.booleans() | st.none() | st.lists(st.integers()))
def test_storage_path_rejects_wrong_types(value, tmp_path_factory):
    # Hypothesis supplies bounded scalar/list inputs; each is structurally not a path string.
    config = yaml.safe_load(BASE.read_bytes())
    config['storage']['kb_store'] = value
    path = save_config(tmp_path_factory.mktemp('property'), config)
    with pytest.raises(ConfigurationError):
        load_config(path, 'kb status')
