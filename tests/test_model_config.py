"""Configuration schema checks for backend operational settings."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from prompt_enhancer.config import ConfigurationError, load_config


ROOT = Path(__file__).resolve().parents[1]
VALUES = [
    ('http', 'connect_timeout_s', 2, 0), ('http', 'read_timeout_s', 3, 0),
    ('http', 'write_timeout_s', 4, 0), ('http', 'pool_timeout_s', 5, 0),
    ('http', 'max_retries', 2, -1), ('http', 'backoff_initial_s', 2, -1),
    ('http', 'backoff_max_s', 4, -1), ('http', 'retry_statuses', [503], [999]),
    ('probe', 'max_requests', 2, 0), ('probe', 'timeout_s', 3, 0),
    ('image', 'max_decode_pixels', 500, 0),
]


def test_alpha_policy_profile_override_and_refusal(tmp_path, project_root):
    from io import BytesIO
    from PIL import Image
    from prompt_enhancer.model_image import prepare_vision_image

    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}
    profile['vision'].update(format='png', quality=80)
    source = BytesIO()
    Image.new('RGBA', (2, 2), (0, 0, 0, 0)).save(source, 'PNG')
    path = tmp_path / 'config.yaml'
    for value in ({'mode': 'refuse'}, {'mode': 'flatten', 'background': '#123456'}):
        profile['vision']['alpha_policy'] = value
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        vision = load_config(path, 'kb status', project_root=project_root)['vision']
        assert vision['alpha_policy'] == value
        if value['mode'] == 'refuse':
            with pytest.raises(ValueError, match='alpha_policy refuses'):
                prepare_vision_image(source.getvalue(), vision, 4)
        else:
            output = prepare_vision_image(source.getvalue(), vision, 4)
            with Image.open(BytesIO(output)) as image:
                assert image.getpixel((0, 0)) == (18, 52, 86)
    for invalid in ({'mode': 'flatten'}, {'mode': 'flatten', 'background': 'white'},
                    {'mode': 'refuse', 'background': '#FFFFFF'}, {'mode': 'other'}, None):
        if invalid is None:
            profile['vision'].pop('alpha_policy')
        else:
            profile['vision']['alpha_policy'] = invalid
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='alpha_policy'):
            load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize(('section', 'key', 'override', 'invalid'), VALUES)
def test_backend_operational_schema(tmp_path, project_root, section, key, override, invalid):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    backend = profile['vlm']['backends']['main']
    backend['http'] = dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                           max_retries=0, backoff_initial_s=0, backoff_max_s=4, retry_statuses=[])
    backend['probe'] = dict(max_requests=1, timeout_s=1, image_attempts=1)
    backend['image'] = dict(max_decode_pixels=100, max_side='none', max_images_per_call=1)  # test-only limit (decision 179)
    backend[section][key] = override
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    assert loaded['vlm']['backends']['main'][section][key] == override
    for value in (invalid, None):
        bad = deepcopy(profile)
        if value is None:
            del bad['vlm']['backends']['main'][section][key]
        else:
            bad['vlm']['backends']['main'][section][key] = value
        path.write_text(yaml.safe_dump(bad), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb status', project_root=project_root)


def test_nonfinite_and_backoff_order_refused(tmp_path, project_root):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    backend = profile['vlm']['backends']['main']
    backend['http']['connect_timeout_s'] = float('inf')
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='finite'):
        load_config(path, 'kb status', project_root=project_root)
    backend['http']['connect_timeout_s'] = 1
    backend['http']['backoff_initial_s'] = 3
    backend['http']['backoff_max_s'] = 2
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='backoff_max_s'):
        load_config(path, 'kb status', project_root=project_root)


def test_anthropic_version_override_and_refusal(tmp_path, project_root):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    backend = profile['vlm']['backends']['cloud_claude']
    path = tmp_path / 'config.yaml'
    backend['anthropic_version'] = '2025-01-01'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    assert loaded['vlm']['backends']['cloud_claude']['anthropic_version'] == '2025-01-01'
    for invalid in ('invalid\nheader', None):
        if invalid is None:
            del backend['anthropic_version']
        else:
            backend['anthropic_version'] = invalid
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb status', project_root=project_root)


def test_anthropic_workspace_override_and_refusal(tmp_path, project_root):
    from prompt_enhancer.model_cloud import cloud_headers

    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}
    backend = profile['vlm']['backends']['cloud_claude']
    backend['anthropic_version'] = '2025-01-01'
    directory = tmp_path / '.secrets'
    directory.mkdir()
    # The synthetic key takes the file name the example profile names, so renaming it there needs no test edit.
    (directory / backend['api_key_file'].split('/')[-1]).write_text('synthetic', encoding='utf-8')
    path = tmp_path / 'config.yaml'
    for value in ('workspace-123', 'none'):
        backend['anthropic_workspace_id'] = value
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        loaded = load_config(path, 'kb status', project_root=project_root)
        assert loaded['vlm']['backends']['cloud_claude']['anthropic_workspace_id'] == value
        headers = cloud_headers('anthropic', loaded['vlm']['backends']['cloud_claude'], tmp_path)
        if value == 'none':
            assert 'anthropic-workspace-id' not in headers
        else:
            assert headers['anthropic-workspace-id'] == value
    for invalid in ('', 'bad\nheader', 'é', None):
        if invalid is None:
            backend.pop('anthropic_workspace_id')
        else:
            backend['anthropic_workspace_id'] = invalid
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='anthropic_workspace_id'):
            load_config(path, 'kb status', project_root=project_root)


def test_selected_anthropic_workspace_sentinel_refused(project_root):
    import asyncio
    from prompt_enhancer.model_client import ModelClientRun

    local = {'provider': 'openai_compatible', 'max_concurrency': 1, 'image': {'max_decode_pixels': 100}}
    cloud = {'provider': 'anthropic', 'max_concurrency': 1, 'image': {'max_decode_pixels': 100},
             'anthropic_workspace_id': 'deploy_required'}
    config = {'vlm': {'default_backend': 'main', 'backends': {'main': local, 'cloud': cloud},
                      'roles': {'planner': {'backend': 'cloud', 'image': 'none'}}}}
    run = ModelClientRun(config, project_root, probe_cases={}, local_probe_plans={})
    run.transports['main'] = object()
    with pytest.raises(ConfigurationError, match='anthropic_workspace_id.*unresolved sentinel'):
        asyncio.run(run.for_role('planner'))


def test_multiple_backend_urls_refused_with_setting_name(tmp_path, project_root):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}
    profile['vlm']['backends']['main']['base_urls'] = ['http://127.0.0.1:1', 'http://127.0.0.1:2']
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match=r'vlm\.backends\.main\.base_urls: multiple URLs'):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize(('name', 'valid', 'invalid'), [
    ('main', {'name': 'vllm', 'version': '0.6'}, {'name': 'anthropic', 'version': '0.6'}),
    ('main', {'name': 'llama_cpp', 'version': 'b1234'}, {'name': 'llama_cpp'}),
    ('main', {'name': 'vllm', 'version': '0.6'}, {'name': 'vllm', 'version': ''}),
    ('cloud_claude', {'name': 'anthropic'}, {'name': 'gemini'}),
    ('cloud_gemini', {'name': 'gemini'}, {'name': 'anthropic'}),
    ('cloud_claude', {'name': 'anthropic'}, {'name': 'anthropic', 'version': 'duplicate'}),
])
def test_engine_profile_override_and_refusal(tmp_path, project_root, name, valid, invalid):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    backend = profile['vlm']['backends'][name]
    path = tmp_path / 'config.yaml'
    backend['engine'] = valid
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    assert loaded['vlm']['backends'][name]['engine'] == valid
    backend['engine'] = invalid
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(path, 'kb status', project_root=project_root)


def test_gemini_engine_version_is_taken_from_route(tmp_path, project_root):
    from prompt_enhancer.config import resolve_backend_engine

    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    backend = profile['vlm']['backends']['cloud_gemini']
    backend['endpoints'] = {'chat': 'v1beta/models/test:generateContent', 'health': 'health'}
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    assert resolve_backend_engine(loaded['vlm']['backends']['cloud_gemini'], 'cloud_gemini') == {
        'name': 'gemini', 'version': 'v1beta'}
    backend['endpoints']['chat'] = 'models/test:generateContent'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='version'):
        load_config(path, 'kb status', project_root=project_root)


# Values exercise the actual command read set; malformed values must not reach a call.
ROLE_VALUES = [
    ('prompt_id', 'alternate_prompt', ''),
    ('backend', 'alternate', 'missing_backend'),
    ('max_tokens', 256, 0),
    ('sampling.temperature', 0.6, -1),
    ('sampling.top_p', 0.7, 2),
    ('sampling.top_k', 23, -1),
    ('sampling.min_p', 0.3, 2),
    ('sampling.presence_penalty', 0.3, 'bad'),
    ('sampling.frequency_penalty', 0.4, 'bad'),
    ('sampling.repetition_penalty', 1.3, 0),
    ('sampling.seed', 123, 'bad'),
    ('sampling.stop', ['END'], 3),
    ('reasoning.mode', 'on', 'bad'),
    ('reasoning.effort', 'high', 'bad'),
    ('reasoning.budget_tokens', 12, -1),
    ('reasoning.template_kwargs_key', 'custom_switch', 2),
    ('image.source', 'user_reference', 'alternate_source'),  # T-014: a closed enum of image sources
    ('image.max_side', 640, 0),
    ('image.format', 'png', ''),
    ('image.quality', 75, 0),
    ('image.transport', 'alternate_transport', ''),
    ('output.format', 'json_object', 'bad'),
    ('output.schema_id', 'profile_manifest.v0', 'missing.v0'),
    ('output.strict', False, 'bad'),
    ('output.transport', 'engine_native', 'bad'),
    ('output.validate_response', True, 'bad'),
]


@pytest.mark.parametrize(('key', 'override', 'invalid'), ROLE_VALUES)
def test_role_values_through_command_load(tmp_path, project_root, key, override, invalid):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    role = profile['vlm']['roles']['manifest_drafter']
    role.update(max_tokens=128,
                sampling=dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0,
                              presence_penalty=0.0, frequency_penalty=0.0,
                              repetition_penalty=1.0, seed='none', stop='none'),
                reasoning=dict(mode='auto', effort='none', budget_tokens='none',
                               template_kwargs_key='enable_thinking'),
                image=dict(source='source_image', max_side='from_vision', format='from_vision',
                           quality=90, transport='data_uri'),
                output=dict(format='none', schema_id='none', strict=True,
                            transport='response_format', validate_response=False))
    if key == 'output.format':
        role['output']['validate_response'] = True
    backend = profile['vlm']['backends']['main']
    backend['engine'] = {'name': 'llama_cpp', 'version': 'test-version'}
    backend.update(base_urls=['http://127.0.0.1:8080'], endpoints={'chat': 'chat', 'health': 'health'}, max_concurrency=1,
                   http=dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1,
                             pool_timeout_s=1, max_retries=0, backoff_initial_s=0,
                             backoff_max_s=0, retry_statuses=[]),
                   probe=dict(max_requests=1, timeout_s=1, image_attempts=1),
                   image=dict(max_decode_pixels=1000, max_side='none', max_images_per_call=1),  # test-only limit (decision 179)
                   addendum_layout='separate_system_message',  # test-only layout (T017-FIX)
                   addendum_separator='none')  # the base example states deploy_required; separate takes none (decision 193)
    backend['model'].update(id='local-model', revision='revision')
    profile['vlm']['backends']['alternate'] = deepcopy(backend)
    keys = key.split('.')

    def put(target, value):
        for part in keys[:-1]:
            target = target[part]
        target[keys[-1]] = value

    path = tmp_path / 'config.yaml'
    put(role, override)
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'profile register', project_root=project_root)
    value = loaded['vlm']['roles']['manifest_drafter']
    for part in keys:
        value = value[part]
    assert value == override
    put(role, invalid)
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(path, 'profile register', project_root=project_root)


CONFIG_VALUES = [
    ('vision.max_image_side', 640, 0),
    ('vision.format', 'png', 12),
    ('vision.quality', 75, -1),
    ('vision.transport', 'data_uri', 'alternate_transport'),  # T-008: an enum whose only value is data_uri
    ('vlm.backends.main.base_urls', ['http://127.0.0.1:8080'], []),
    ('vlm.backends.main.base_urls', ['http://127.0.0.1:8080'], ['']),
    ('vlm.backends.main.base_urls', ['http://127.0.0.1:8080'], ['http://127.0.0.1:8080', 'http://127.0.0.1:8081']),
    ('vlm.backends.main.endpoints', {'chat': 'chat', 'health': 'health'}, {}),
    ('vlm.backends.main.endpoints', {'chat': 'chat', 'health': 'health'}, {'chat': '', 'health': 'health'}),
    ('vlm.backends.main.model.id', 'alternate-model', 12),
    ('vlm.backends.main.model.revision', 'alternate-revision', 12),
    ('vlm.backends.main.max_concurrency', 3, 0),
    ('vlm.backends.main.extra_body', {'temperature': 0.7}, 12),
    ('vlm.backends.cloud_claude.call_budget', 5, -1),
    ('vlm.backends.cloud_claude.api_key_file', '.secrets/alternate.key', 'none'),
    ('vlm.backends.cloud_claude.api_key_file', '.secrets/alternate.key', '.secrets/nested/key'),
    ('vlm.backends.cloud_claude.allow_images', False, 'bad'),
    ('vlm.backends.cloud_claude.on_budget_exhausted', 'stop', 'bad'),
]


@pytest.mark.parametrize(('key', 'override', 'invalid'), CONFIG_VALUES)
def test_config_values_through_profile_load(tmp_path, project_root, key, override, invalid):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    keys = key.split('.')

    def node(data):
        for part in keys[:-1]:
            data = data[part]
        return data

    path = tmp_path / 'config.yaml'
    node(profile)[keys[-1]] = override
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    assert node(loaded)[keys[-1]] == override
    node(profile)[keys[-1]] = invalid
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(path, 'kb status', project_root=project_root)


def test_endpoint_missing_is_refused(tmp_path, project_root):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    path = tmp_path / 'config.yaml'
    backend = profile['vlm']['backends']['main']
    del backend['endpoints']
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='endpoints'):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize(('base', 'route'), [
    ('http://example.org', 'chat'), ('ftp://example.org', 'chat'),
    ('https:///missing', 'chat'), ('https://example.org', '//evil/path'),
    ('https://example.org', '../path'), ('https://example.org', '%2e%2e/path'),
    ('deploy_required', '../path'), ('http://example.org', 'deploy_required'),
])
def test_unsafe_url_refused_at_profile_load(tmp_path, project_root, base, route):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    backend = profile['vlm']['backends']['main']
    backend['base_urls'] = [base]
    backend['endpoints'] = {'chat': route, 'health': 'health'}
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='base_urls|endpoints.chat'):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize(('changes', 'valid', 'invalid', 'message'), [
    ([], ('vlm.default_backend', 'main'), ('vlm.default_backend', 'unknown'), 'default_backend'),
    ([], ('vlm.default_backend', 'main'), ('vlm.default_backend', 'cloud_claude'), 'default_backend'),
    ([], ('vlm.roles.manifest_drafter.backend', 'main'), ('vlm.roles.manifest_drafter.backend', 'unknown'), 'backend'),
    ([('vlm.roles.manifest_drafter.backend', 'cloud_claude'), ('vlm.roles.manifest_drafter.reasoning.mode', 'on'),
      ('vlm.backends.cloud_claude.model.id', 'claude-test-model')],  # not Haiku 5.5: the generic budget bounds apply
     ('vlm.roles.manifest_drafter.reasoning.budget_tokens', 1024),
     ('vlm.roles.manifest_drafter.reasoning.budget_tokens', 1023), 'budget_tokens'),
    ([('vlm.roles.manifest_drafter.backend', 'cloud_claude'), ('vlm.roles.manifest_drafter.reasoning.mode', 'on'),
      ('vlm.backends.cloud_claude.model.id', 'claude-test-model')],  # not Haiku 5.5: the generic budget bounds apply
     ('vlm.roles.manifest_drafter.reasoning.budget_tokens', 1024),
     ('vlm.roles.manifest_drafter.reasoning.budget_tokens', 2048), 'budget_tokens'),
    ([('vlm.roles.manifest_drafter.backend', 'cloud_claude'), ('vlm.roles.manifest_drafter.output.validate_response', True)],
     ('vlm.roles.manifest_drafter.output.format', 'none'),
     ('vlm.roles.manifest_drafter.output.format', 'json_object'), 'output.format'),
    ([('vlm.roles.manifest_drafter.backend', 'cloud_gemini')],
     ('vlm.roles.manifest_drafter.image', 'none'),
     ('vlm.roles.manifest_drafter.image', {'source': 'source_image', 'format': 'png', 'quality': 90, 'transport': 'data_uri'}), 'image'),
    ([('vlm.roles.manifest_drafter.backend', 'cloud_gemini'),
      ('vlm.roles.manifest_drafter.image', {'source': 'source_image', 'format': 'png', 'quality': 90, 'transport': 'data_uri'})],
     ('vlm.backends.cloud_gemini.allow_images', False),
     ('vlm.backends.cloud_gemini.allow_images', True), 'image'),
    ([], ('vlm.backends.cloud_gemini.base_urls', ['https://example.org']),
     ('vlm.backends.cloud_gemini.base_urls', ['provider_default']), 'provider_default'),
    ([], ('vlm.roles.planner.image.format', 'png'), ('vlm.roles.planner.image.format', 'not-an-encoder'), 'encoder'),
    ([], ('vlm.roles.planner.image.quality', 75), ('vlm.roles.planner.image.quality', None), 'quality'),
    ([], ('vlm.roles.manifest_drafter.sampling.temperature', 0.5),
     ('vlm.roles.manifest_drafter.sampling.temperature', float('inf')), 'finite'),
    ([('vlm.roles.manifest_drafter.output.validate_response', True),
      ('vlm.roles.manifest_drafter.output.schema_id', 'none')],
     ('vlm.roles.manifest_drafter.output.format', 'json_object'),
     ('vlm.roles.manifest_drafter.output.format', 'json_schema'), 'schema_id'),
    ([], ('vision.quality', 75), ('vision.quality', 101), 'quality'),
    ([], ('vlm.backends.main.model.id', 'configured'), ('vlm.backends.main.model.id', ''), 'model.id'),
    ([], ('vlm.backends.main.model.revision', 'revision'), ('vlm.backends.main.model.revision', ''), 'model.revision'),
])
def test_binding_rules_through_profile_load(tmp_path, project_root, changes, valid, invalid, message):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    role = profile['vlm']['roles']['manifest_drafter']
    role.update(max_tokens=2048, image='none',
                sampling=dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0,
                              presence_penalty=0.0, frequency_penalty=0.0,
                              repetition_penalty=1.0, seed='none', stop='none'),
                reasoning=dict(mode='auto', effort='none', budget_tokens='none', template_kwargs_key='switch'),
                output=dict(format='none', schema_id='none', strict=False,
                            transport='response_format', validate_response=False))
    profile['vision']['quality'] = 80
    profile['vlm']['roles']['planner']['image']['quality'] = 80
    profile['vlm']['roles']['writer']['image']['quality'] = 80

    def put(key, value):
        parts = key.split('.')
        target = profile
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value

    for key, value in changes:
        put(key, value)
    path = tmp_path / 'config.yaml'
    put(*valid)
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    load_config(path, 'kb status', project_root=project_root)
    put(*invalid)
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match=message):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize('field', ['source', 'format', 'quality', 'transport'])
def test_image_members_required_and_max_side_can_inherit(tmp_path, project_root, field):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    image = profile['vlm']['roles']['planner']['image']
    path = tmp_path / 'config.yaml'
    del image['max_side']
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    assert 'max_side' not in load_config(path, 'kb status', project_root=project_root)['vlm']['roles']['planner']['image']
    del image[field]
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError, match=field):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize('field', ['temperature', 'top_p', 'top_k', 'min_p', 'presence_penalty',
                                   'frequency_penalty', 'repetition_penalty', 'seed'])
@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan')])
def test_nonfinite_sampling_refused(tmp_path, project_root, field, value):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    profile['vlm']['roles']['planner']['sampling'][field] = value
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize('backend', ['main', 'cloud_gemini'])
def test_anthropic_limit_does_not_apply_to_other_providers(tmp_path, project_root, backend):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    role = profile['vlm']['roles']['manifest_drafter']
    role['backend'] = backend
    role['max_tokens'] = 100
    role['output'].update(format='none', schema_id='none', validate_response=False)
    role['reasoning'].update(mode='on', budget_tokens=1)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    assert load_config(path, 'kb status', project_root=project_root)['vlm']['roles']['manifest_drafter']['reasoning']['budget_tokens'] == 1
