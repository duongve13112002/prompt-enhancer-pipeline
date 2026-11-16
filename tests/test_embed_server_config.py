"""Embedding server configuration (T-018): an override and a refusal for every new key, through the public loader."""

from copy import deepcopy
from pathlib import Path
import re
import shutil

import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer.config import embed_server_address
from prompt_enhancer.embed_server.families import FAMILIES, check_weights_config
from prompt_enhancer.embed_server.settings import server_settings
from embed_server_fixtures import server_profile

ROOT = Path(__file__).resolve().parents[1]
B = 'vlm.backends.embedder.'
S = B + 'server.'


def set_key(profile, key, value):
    parts = key.split('.')
    node = profile
    for part in parts[:-1]:
        node = node[part]
    if value is DELETE:
        del node[parts[-1]]
    else:
        node[parts[-1]] = value
    return profile


DELETE = object()


def load_server(project_root, profile, name='embed-profile.yaml'):
    path = project_root / name
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    return load_config(path, 'embed serve', project_root=project_root)


@pytest.fixture
def profile(model_test_profile):
    return server_profile(model_test_profile, port=9101)


def test_every_value_comes_from_the_profile(project_root, profile):
    set_key(profile, 'runtime.gpus', [1])
    set_key(profile, 'runtime.vram_budget_gb', 2.5)
    set_key(profile, B + 'base_urls', ['http://localhost:9555'])
    set_key(profile, B + 'endpoints', {'embeddings': 'api/embed', 'health': 'api/ping'})
    set_key(profile, B + 'image.max_decode_pixels', 777)
    set_key(profile, B + 'model.id', 'owner/some-model')
    set_key(profile, B + 'model.revision', 'rev-1')
    set_key(profile, 'embedding.revision', 'rev-1')
    set_key(profile, S + 'forward_batch', 1)
    set_key(profile, S + 'max_length', 33)
    set_key(profile, S + 'image.max_soft_tokens', 70)
    set_key(profile, S + 'idle_exit_s', 9)
    set_key(profile, S + 'max_request_bytes', 12345)
    set_key(profile, S + 'max_image_bytes', 6000)
    set_key(profile, S + 'max_pending_requests', 3)
    set_key(profile, S + 'max_connections', 7)
    set_key(profile, S + 'max_discard_bytes', 54321)
    set_key(profile, S + 'read_timeout_s', 0.75)
    set_key(profile, S + 'attn_implementation', 'eager')
    set_key(profile, S + 'dtype', 'float32')
    set_key(profile, 'embedding.batch_size', 5)
    settings = server_settings(load_server(project_root, profile), project_root)
    assert (settings.gpu, settings.vram_budget_gb, settings.host, settings.port) == (1, 2.5, 'localhost', 9555)
    assert (settings.embeddings_path, settings.health_path) == ('/api/embed', '/api/ping')
    assert (settings.max_decode_pixels, settings.model_id, settings.revision) == (777, 'owner/some-model', 'rev-1')
    assert (settings.forward_batch, settings.max_length, settings.max_soft_tokens) == (1, 33, 70)
    assert (settings.idle_exit_s, settings.max_request_bytes, settings.max_image_bytes) == (9, 12345, 6000)
    assert (settings.attn_implementation, settings.dtype, settings.batch_size) == ('eager', 'float32', 5)
    assert (settings.max_pending_requests, settings.read_timeout_s) == (3, 0.75)
    assert (settings.max_connections, settings.max_discard_bytes) == (7, 54321)
    assert settings.family is FAMILIES['embedding_gemma2'] and settings.default_instruction is None
    assert settings.min_pixels is None and settings.max_pixels is None and settings.project_root == project_root


def test_the_connection_and_discard_limits_accept_their_exact_lower_bounds(project_root, profile):
    set_key(profile, S + 'max_connections', 8)  # equal to max_pending_requests: every admitted request has a connection
    set_key(profile, S + 'max_discard_bytes', 100000)  # equal to max_request_bytes
    settings = server_settings(load_server(project_root, profile), project_root)
    assert (settings.max_connections, settings.max_discard_bytes) == (8, 100000)


@pytest.mark.parametrize('value', [0.001, 3600, 2 ** 20])
def test_an_accepted_read_timeout_is_usable_by_a_socket(project_root, profile, value):
    """A value the validation accepts must not break the socket at the first request (the platform decides)."""
    import socket

    set_key(profile, S + 'read_timeout_s', value)
    settings = server_settings(load_server(project_root, profile), project_root)
    with socket.socket() as probe:
        probe.settimeout(settings.read_timeout_s)  # raises OverflowError for a value the socket cannot hold
        assert probe.gettimeout() > 0


def test_every_schema_server_key_reaches_the_settings_record(project_root, profile):
    """A validated key that no consumer receives would be ignored (review R1): the record carries each of them, except
    the keys another command reads, named here with that command's read set (T-018 P2: `reference_check`, read only
    by `embed reference-check`; optional for `serve`, which never reads it)."""
    import json
    from dataclasses import fields

    from prompt_enhancer.config import REFERENCE_CHECK_READS, _schema_at  # the loader's own schema lookup

    other_consumers = {'reference_check': REFERENCE_CHECK_READS}
    schema = json.loads((ROOT / 'contracts/schemas/enhancer_config.v0.schema.json').read_bytes())
    node = _schema_at(schema, 'vlm.backends.embedder.server')
    block = next(branch for branch in node['anyOf'] if branch.get('type') == 'object')
    carried = {field.name for field in fields(server_settings(load_server(project_root, profile), project_root))}
    for key, spec in block['properties'].items():
        if key in other_consumers:
            reads = other_consumers[key]
            assert f'server.{key}' in reads and {f'server.{key}.{name}' for name in spec['properties']} <= set(reads)
            continue
        names = list(spec['properties']) if key == 'image' else [key]
        assert set(names) <= carried, key
    # nothing optional or defaulted for the server; the other command refuses its keys missing (reference tests)
    assert set(block['required']) == set(block['properties']) - set(other_consumers)


def test_the_qwen_family_resolves_its_own_image_and_instruction_keys(project_root, model_test_profile):
    profile = server_profile(model_test_profile, family='qwen3_vl_embedding')
    set_key(profile, S + 'image.min_pixels', 100)
    set_key(profile, S + 'image.max_pixels', 5000)
    set_key(profile, S + 'default_instruction', 'Custom instruction.')
    settings = server_settings(load_server(project_root, profile), project_root)
    assert (settings.min_pixels, settings.max_pixels, settings.max_soft_tokens) == (100, 5000, None)
    assert settings.default_instruction == 'Custom instruction.' and settings.family.pooling == 'last_token'


REFUSALS = [
    (S + 'family', 'other', 'family'), (S + 'family', 5, 'family'),
    (S + 'dtype', 'float64', 'dtype'), (S + 'dtype', 'float16', 'dtype'),  # float16: refused for EmbeddingGemma 2
    (S + 'attn_implementation', 'flash_attention_2', 'attn_implementation'),
    (S + 'forward_batch', 0, 'forward_batch'), (S + 'forward_batch', 3, 'forward_batch'),  # above embedding.batch_size 2
    (S + 'forward_batch', 1.5, 'forward_batch'), (S + 'forward_batch', True, 'forward_batch'),
    (S + 'max_length', 0, 'max_length'), (S + 'max_length', 'long', 'max_length'),
    (S + 'image.max_soft_tokens', 0, 'max_soft_tokens'), (S + 'image.max_soft_tokens', 'none', 'max_soft_tokens'),
    (S + 'image.min_pixels', 4096, 'min_pixels'), (S + 'image.max_pixels', 4096, 'max_pixels'),
    (S + 'default_instruction', '', 'default_instruction'), (S + 'default_instruction', 'be brief', 'default_instruction'),
    (S + 'idle_exit_s', -1, 'idle_exit_s'), (S + 'idle_exit_s', 1.5, 'idle_exit_s'),
    (S + 'max_request_bytes', 0, 'max_request_bytes'), (S + 'max_request_bytes', 100, 'max_request_bytes'),
    (S + 'max_image_bytes', 0, 'max_image_bytes'),
    (S + 'max_pending_requests', 0, 'max_pending_requests'), (S + 'max_pending_requests', 1.5, 'max_pending_requests'),
    (S + 'max_pending_requests', True, 'max_pending_requests'), (S + 'max_pending_requests', 'many', 'max_pending_requests'),
    (S + 'max_connections', 0, 'max_connections'), (S + 'max_connections', 1.5, 'max_connections'),
    (S + 'max_connections', True, 'max_connections'), (S + 'max_connections', 'many', 'max_connections'),
    (S + 'max_connections', 7, 'max_connections'),  # below max_pending_requests 8: each admitted request holds a connection
    (S + 'max_discard_bytes', 0, 'max_discard_bytes'), (S + 'max_discard_bytes', 1.5, 'max_discard_bytes'),
    (S + 'max_discard_bytes', True, 'max_discard_bytes'), (S + 'max_discard_bytes', 'all', 'max_discard_bytes'),
    (S + 'max_discard_bytes', 99999, 'max_discard_bytes'),  # below max_request_bytes 100000
    (S + 'read_timeout_s', 0, 'read_timeout_s'), (S + 'read_timeout_s', -1, 'read_timeout_s'),
    (S + 'read_timeout_s', 'soon', 'read_timeout_s'), (S + 'read_timeout_s', True, 'read_timeout_s'),
    (S + 'read_timeout_s', float('inf'), 'read_timeout_s'),
    (S + 'read_timeout_s', 1e300, 'read_timeout_s'), (S + 'read_timeout_s', 10 ** 400, 'read_timeout_s'),  # above the timer limit
    (B + 'base_urls', ['https://127.0.0.1:9100'], 'base_urls'),
    (B + 'base_urls', ['http://10.0.0.5:9100'], 'base_urls'),
    (B + 'base_urls', ['http://127.0.0.1'], 'base_urls'),
    (B + 'base_urls', ['http://127.0.0.1:0'], 'base_urls'),
    (B + 'base_urls', ['http://127.0.0.1:70000'], 'base_urls'),
    (B + 'base_urls', ['http://user:pw@127.0.0.1:9100'], 'base_urls'),
    (B + 'base_urls', [], 'base_urls'),
    (B + 'endpoints', {'embeddings': 'same', 'health': 'same'}, 'endpoints'),
    (B + 'endpoints', {'embeddings': '../up', 'health': 'health'}, 'base_urls'),
    (B + 'engine.name', 'llama_cpp', 'engine.name'), (B + 'engine.name', 'unknown_engine', 'engine'),
    (B + 'model.revision', 'rev-other', 'embedding.revision'),
    (B + 'image.max_decode_pixels', 0, 'max_decode_pixels'),
    ('runtime.gpus', [0, 1], 'runtime.gpus'), ('runtime.gpus', [], 'runtime.gpus'), ('runtime.gpus', 'auto', 'runtime.gpus'),
    ('runtime.gpus', [-1], 'gpus'), ('runtime.vram_budget_gb', 0, 'vram_budget_gb'),
    ('runtime.vram_budget_gb', 'plenty', 'vram_budget_gb'),
    ('embedding.request.shape', 'vllm_chat_messages', 'vllm_chat_messages'),
    ('embedding.request.image_instruction', 'Represent the input.', 'image_instruction'),
    ('embedding.dimension_mode', 'request_field', 'dimension_mode'),
    ('embedding.backend', 'no_such_backend', 'embedding.backend'),
    ('embedding.batch_size', 0, 'batch_size'),
]


@pytest.mark.parametrize('key, value, match', REFUSALS, ids=[f'{key}={value!r}' for key, value, _ in REFUSALS])
def test_refusal(project_root, profile, key, value, match):
    with pytest.raises(ConfigurationError, match=re.escape(match)):
        load_server(project_root, set_key(profile, key, value))


@pytest.mark.parametrize('family, kernel', [('embedding_gemma2', 'eager'), ('qwen3_vl_embedding', 'sdpa'),
                                            ('qwen3_vl_embedding', 'eager')])
def test_a_kernel_the_family_supports_is_accepted(project_root, model_test_profile, family, kernel):
    profile = set_key(server_profile(model_test_profile, family=family), S + 'attn_implementation', kernel)
    assert server_settings(load_server(project_root, profile), project_root).attn_implementation == kernel


def test_embedding_gemma_2_with_sdpa_is_refused_by_the_shared_validator(project_root, profile):
    # F6: transformers 5.23.0 refuses sdpa for its audio tower at load; the profile check refuses it first
    with pytest.raises(ConfigurationError, match=r'server\.attn_implementation: sdpa is not supported by '
                                                 r'embedding_gemma2.*expected one of eager'):
        load_server(project_root, set_key(profile, S + 'attn_implementation', 'sdpa'))


QWEN_REFUSALS = [
    (S + 'image.min_pixels', 'none', 'min_pixels'), (S + 'image.max_pixels', 'none', 'max_pixels'),
    (S + 'image.max_soft_tokens', 70, 'max_soft_tokens'), (S + 'image.min_pixels', 9999, 'min_pixels'),  # above max 4096
    (S + 'image.max_pixels', 0, 'max_pixels'),
]


@pytest.mark.parametrize('key, value, match', QWEN_REFUSALS, ids=[f'{key}={value!r}' for key, value, _ in QWEN_REFUSALS])
def test_qwen_refusal(project_root, model_test_profile, key, value, match):
    profile = server_profile(model_test_profile, family='qwen3_vl_embedding')
    with pytest.raises(ConfigurationError, match=re.escape(match)):
        load_server(project_root, set_key(profile, key, value))


READS = [S + field for field in ('family', 'dtype', 'attn_implementation', 'forward_batch', 'max_length', 'image',
                                 'default_instruction', 'idle_exit_s', 'max_request_bytes', 'max_image_bytes',
                                 'max_pending_requests', 'max_connections', 'max_discard_bytes', 'read_timeout_s')] + [
    B + 'base_urls', B + 'model.id', B + 'model.revision', B + 'endpoints.embeddings', B + 'endpoints.health',
    B + 'image.max_decode_pixels', B + 'engine.version', 'runtime.gpus', 'runtime.vram_budget_gb', 'embedding.backend',
    'embedding.batch_size', 'embedding.dimension', 'embedding.dimension_mode', 'embedding.revision',
    'embedding.request.shape', 'embedding.request.image_instruction']


@pytest.mark.parametrize('key', READS)
def test_a_sentinel_in_a_read_key_is_refused_at_start(project_root, profile, key):
    leaf = key.rpartition('.')[2]
    value = ['deploy_required'] if leaf == 'base_urls' else 'deploy_required'
    with pytest.raises(ConfigurationError, match=r'(unresolved sentinel|' + re.escape(leaf) + ')'):
        load_server(project_root, set_key(profile, key, value))


@pytest.mark.parametrize('key', [S + 'family', S + 'dtype', S + 'forward_batch', S + 'image', S + 'idle_exit_s',
                                 S + 'max_request_bytes', S + 'max_image_bytes', S + 'default_instruction',
                                 S + 'max_pending_requests', S + 'read_timeout_s', S + 'max_connections',
                                 S + 'max_discard_bytes', B + 'endpoints.health', B + 'image'])
def test_a_missing_key_is_refused(project_root, profile, key):
    with pytest.raises(ConfigurationError):
        load_server(project_root, set_key(profile, key, DELETE))


def test_an_embed_server_backend_must_hold_a_server_block(project_root, profile):
    with pytest.raises(ConfigurationError, match='server'):
        load_server(project_root, set_key(profile, S.rstrip('.'), DELETE))


def test_the_embedding_server_engine_is_a_known_engine_for_k1_too(project_root, profile):
    from prompt_enhancer.k1_embed import embedder_settings

    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only: resolves deploy_required
    config = load_config(_write(project_root, profile), 'kb status', project_root=project_root)
    assert embedder_settings(config).engine == {'name': 'embed_server', 'version': 'test-version'}


def _write(project_root, profile):
    path = project_root / 'embed-profile.yaml'
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    return path


def test_the_vllm_shape_is_refused_for_the_embedding_server_engine_by_the_shared_rule(project_root, profile):
    set_key(profile, 'embedding.request.shape', 'vllm_chat_messages')
    set_key(profile, 'embedding.batch_size', 1)
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only: resolves deploy_required
    with pytest.raises(ConfigurationError, match='vllm_chat_messages is not documented for engine embed_server'):
        load_config(_write(project_root, profile), 'kb status', project_root=project_root)


def test_the_embed_serve_command_is_known_and_reads_the_runtime_keys(project_root, profile):
    set_key(profile, 'runtime.vram_budget_gb', 'deploy_required')
    with pytest.raises(ConfigurationError, match='runtime.vram_budget_gb: unresolved sentinel'):
        load_server(project_root, profile)


@pytest.mark.parametrize('url, expected', [('http://127.0.0.1:8080', ('127.0.0.1', 8080)),
                                           ('http://localhost:1', ('localhost', 1)),
                                           ('http://127.0.0.2:65535', ('127.0.0.2', 65535))])
def test_embed_server_address(url, expected):
    assert embed_server_address(url) == expected


# IPv6 loopback is refused: the server binds one IPv4 socket, so an accepted `[::1]` could never start (review R5;
# every accepted address is bound in tests/test_embed_server_http.py::test_every_accepted_address_can_bind).
@pytest.mark.parametrize('url', ['https://127.0.0.1:1', 'http://example.com:80', 'http://127.0.0.1', 'http://:80',
                                 'http://127.0.0.1:0', 'ftp://127.0.0.1:1', '127.0.0.1:80', 'http://0.0.0.0:80',
                                 'http://[::1]:65535', 'http://[::ffff:127.0.0.1]:80', 'http://[0:0:0:0:0:0:0:1]:80'])
def test_embed_server_address_refusals(url):
    with pytest.raises(ValueError):
        embed_server_address(url)


# --- the weights' own config.json ----------------------------------------------------------------------------

GEMMA = {'model_type': 'embedding_gemma2', 'text_config': {'embedding_dim': 768}}
QWEN = {'model_type': 'qwen3_vl', 'text_config': {'hidden_size': 2048}}


def test_weights_config_accepts_a_matching_native_or_truncated_dimension():
    assert check_weights_config(FAMILIES['embedding_gemma2'], GEMMA, 768, 'native') == 768
    assert check_weights_config(FAMILIES['embedding_gemma2'], GEMMA, 256, 'client_prefix_l2') == 768
    assert check_weights_config(FAMILIES['qwen3_vl_embedding'], QWEN, 2048, 'native') == 2048


@pytest.mark.parametrize('family, config, dimension, mode', [
    ('embedding_gemma2', QWEN, 768, 'native'),  # the other family's weights
    ('qwen3_vl_embedding', GEMMA, 2048, 'native'),
    ('embedding_gemma2', dict(GEMMA, auto_map={'AutoModel': 'x.Y'}), 768, 'native'),  # remote code
    ('embedding_gemma2', {'model_type': 'embedding_gemma2'}, 768, 'native'),
    ('embedding_gemma2', dict(GEMMA, text_config={'embedding_dim': True}), 768, 'native'),
    ('embedding_gemma2', GEMMA, 512, 'native'), ('embedding_gemma2', GEMMA, 769, 'client_prefix_l2'),
    ('embedding_gemma2', GEMMA, 0, 'client_prefix_l2'), ('embedding_gemma2', [], 768, 'native'),
    ('embedding_gemma2', None, 768, 'native')])
def test_weights_config_refusals(family, config, dimension, mode):
    with pytest.raises(ValueError):
        check_weights_config(FAMILIES[family], config, dimension, mode)


# --- the shipped example profiles ---------------------------------------------------------------------------

def _shipped(tmp_path):
    folder = tmp_path / 'configs'
    folder.mkdir()
    for name in ('enhancer.example.yaml', 'enhancer.dev.example.yaml', 'enhancer.dev-qwen3vl2b.example.yaml'):
        shutil.copy(ROOT / 'configs' / name, folder / name)
    return folder


LOCAL_VALUES = {'storage': {'lock': {'timeout_s': 5, 'poll_s': 0.05}},  # test-only values for the deploy_required keys
                'runtime': {'gpus': [0], 'vram_budget_gb': 6}}
SERVER_VALUES = {'attn_implementation': 'eager',  # eager: the kernel both families support (F6)
                 'forward_batch': 4, 'idle_exit_s': 0, 'max_request_bytes': 4000000,
                 'max_image_bytes': 2000000, 'max_pending_requests': 4, 'max_connections': 8,
                 'max_discard_bytes': 8000000, 'read_timeout_s': 5}


def _backend_values(port):
    return {'engine': {'name': 'embed_server', 'version': 'test-version'},  # test-only version pin
            'base_urls': [f'http://127.0.0.1:{port}'], 'image': {'max_decode_pixels': 100000000},
            'http': {'connect_timeout_s': 1, 'read_timeout_s': 1, 'write_timeout_s': 1, 'pool_timeout_s': 1,
                     'max_retries': 0, 'backoff_initial_s': 0, 'backoff_max_s': 0, 'retry_statuses': []},
            'server': SERVER_VALUES}


@pytest.mark.parametrize('extends, backend, family, dimension', [
    ('enhancer.dev.example.yaml', 'embedder', 'embedding_gemma2', 768),
    ('enhancer.dev-qwen3vl2b.example.yaml', 'embedder_qwen3vl2b', 'qwen3_vl_embedding', 2048)])
def test_the_dev_profiles_start_the_server_once_the_machine_values_are_set(project_root, tmp_path, extends, backend,
                                                                             family, dimension):
    folder = _shipped(tmp_path)
    local = folder / 'local.yaml'
    local.write_text(yaml.safe_dump(dict(LOCAL_VALUES, schema_version=1, extends=extends,
                                        vlm={'backends': {backend: _backend_values(9300)}})), encoding='utf-8')
    config = load_config(local, 'embed serve', project_root=project_root)
    settings = server_settings(config, project_root)
    assert (settings.backend_name, settings.family.name, settings.dimension, settings.port) == (
        backend, family, dimension, 9300)
    bare = folder / 'bare.yaml'
    bare.write_text(f'schema_version: 1\nextends: {extends}\nstorage: {{lock: {{timeout_s: 5, poll_s: 0.05}}}}\n',
                    encoding='utf-8')
    with pytest.raises(ConfigurationError, match='unresolved sentinel'):  # the shipped example is not runnable as is
        load_config(bare, 'embed serve', project_root=project_root)


def test_the_full_profile_carries_the_8b_backend_and_candidate(project_root, tmp_path):
    folder = _shipped(tmp_path)
    full = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    assert 'qwen3-vl-embedding-8b' in [item['id'] for item in full['embedding']['candidates']]
    local = folder / 'eight.yaml'
    local.write_text(yaml.safe_dump(dict(LOCAL_VALUES, schema_version=1, extends='enhancer.example.yaml',
        embedding={'backend': 'embedder_qwen3vl8b', 'revision': '2c4565515e0f265c6511776e7193b22c0968ddc7',
                   'dimension': 4096, 'dimension_mode': 'native', 'batch_size': 4,
                   'request': {'shape': 'openai_input_content', 'image_instruction': "Represent the user's input.",
                               'extra_body': {}, 'text_template': '{text}'}},
        vlm={'backends': {'embedder_qwen3vl8b': dict(_backend_values(9400), max_concurrency=1)}})), encoding='utf-8')
    settings = server_settings(load_config(local, 'embed serve', project_root=project_root), project_root)
    assert settings.model_id == 'Qwen/Qwen3-VL-Embedding-8B' and settings.dimension == 4096


def test_the_deepcopy_helper_leaves_the_fixture_alone(model_test_profile):
    before = deepcopy(model_test_profile)
    server_profile(model_test_profile)
    assert model_test_profile == before
