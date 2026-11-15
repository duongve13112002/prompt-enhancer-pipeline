"""K1 configuration: an override and a refusal per key of the T-008 Step 0 table C, through the public loader."""

import asyncio
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import anyio
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
import httpx
from PIL import Image
import pytest
import yaml

from prompt_enhancer import ConfigurationError, k1_embed, load_config
import prompt_enhancer.config as pe_config
from prompt_enhancer.config import embedder_reads
from prompt_enhancer.k1_embed import embedder_settings
from prompt_enhancer.model_image import prepare_image_with_info
from prompt_enhancer.storage.identity import concept_id
from k1_fixtures import EmbeddingServer, data_uri_image, image_bytes, k0_handoff, k1_profile, make_source
from storage_fixtures import make_store
from test_k0_config import config, load  # noqa: F401  (the kb sync profile fixture and loader)

ROOT = Path(__file__).resolve().parents[1]


def set_key(profile, key, value):
    parts = key.split('.')
    node = profile
    for part in parts[:-1]:
        node = node[part]
    if value is DELETE:
        del node[parts[-1]]
    else:
        node[parts[-1]] = value


DELETE = object()


def vllm(profile):
    profile['vlm']['backends']['embedder']['engine'] = {'name': 'vllm', 'version': 'test-version'}
    profile['embedding']['request'].update(shape='vllm_chat_messages')
    profile['embedding']['batch_size'] = 1
    return profile


def second_backend(profile):
    backend = deepcopy(profile['vlm']['backends']['embedder'])
    backend['base_urls'] = ['http://127.0.0.1:9100']
    backend['endpoints'] = {'embeddings': 'alt/embed', 'health': 'health'}
    profile['vlm']['backends']['emb_b'] = backend
    return profile


OVERRIDES = [
    ('embedding.backend', 'emb_b', second_backend, lambda s: s.backend_name == 'emb_b'),
    ('vlm.backends.embedder.endpoints.embeddings', 'alt/embed', None,
     lambda s: s.backend['endpoints']['embeddings'] == 'alt/embed'),
    ('embedding.request.shape', 'vllm_chat_messages', vllm, lambda s: s.shape == 'vllm_chat_messages'),
    ('embedding.request.image_instruction', 'describe', None, lambda s: s.image_instruction == 'describe'),
    ('embedding.request.extra_body', {'continue_final_message': True}, vllm,
     lambda s: s.extra_body == {'continue_final_message': True}),
    ('embedding.request.text_template', 'query: {text}', None, lambda s: s.text_template == 'query: {text}'),
    ('embedding.dimension', 256, None, lambda s: s.dimension == 256),
    ('embedding.dimension_mode', 'client_prefix_l2', None, lambda s: s.dimension_mode == 'client_prefix_l2'),
    ('embedding.batch_size', 7, None, lambda s: s.batch_size == 7),
    ('vision.transport', 'data_uri', None, lambda s: s.vision['transport'] == 'data_uri'),
]


@pytest.mark.parametrize(('key', 'value', 'prepare', 'check'), OVERRIDES, ids=[row[0] for row in OVERRIDES])
def test_override_reaches_the_k1_settings(project_root, config, key, value, prepare, check):  # noqa: F811
    profile = deepcopy(config)
    if prepare is not None:
        prepare(profile)
    set_key(profile, key, value)
    assert check(embedder_settings(load(project_root, profile)))


REFUSALS = [
    ('embedding.backend', 'missing_backend', None, 'unknown backend'),
    ('embedding.backend', 'cloud_claude', None, 'local openai_compatible'),
    ('embedding.backend', DELETE, None, 'backend'),
    ('embedding.backend', 'main', None, 'no endpoints.embeddings'),
    ('vlm.backends.embedder.endpoints', {'health': 'health'}, None, 'endpoints'),
    ('vlm.backends.embedder.endpoints.embeddings', '../escape', None, 'endpoints.embeddings'),
    ('embedding.request.shape', 'grpc', None, 'shape'),
    ('embedding.request.shape', 'vllm_chat_messages', lambda p: p['embedding'].update(batch_size=1),
     'not documented for engine llama_cpp'),
    ('embedding.request.shape', 'openai_input_content', lambda p: vllm(p) and p['embedding']['request'].update(
        extra_body={}), 'not documented for engine vllm'),
    ('embedding.request.image_instruction', 12, None, 'image_instruction'),
    ('embedding.request.extra_body', {'temperature': 1}, vllm, 'extra_body'),
    ('embedding.request.extra_body', {'add_special_tokens': 'yes'}, vllm, 'extra_body'),
    ('embedding.request.extra_body', {'add_special_tokens': True}, None, 'only vllm_chat_messages'),
    ('embedding.request.text_template', 'no placeholder', None, 'exactly one'),
    ('embedding.request.text_template', '{text} {text}', None, 'exactly one'),
    ('embedding.dimension', 0, None, 'dimension'),
    ('embedding.dimension', True, None, 'dimension'),
    ('embedding.dimension_mode', 'request_field', None, 'not documented for engine llama_cpp'),
    ('embedding.dimension_mode', 'truncate', None, 'dimension_mode'),
    ('embedding.batch_size', 0, None, 'batch_size'),
    ('embedding.batch_size', True, None, 'batch_size'),
    ('embedding.batch_size', 2, vllm, 'one item per request'),
    ('vision.transport', 'file_uri', None, 'transport'),
    ('vision.transport', 'url', None, 'transport'),
    ('vision.transport', '', None, 'transport'),
    ('embedding.revision', 'other', None, 'differs from vlm.backends.embedder.model.revision'),
    ('embedding.chosen', 'not-a-candidate', None, 'not one of embedding.candidates'),
]


@pytest.mark.parametrize(('key', 'value', 'prepare', 'match'), REFUSALS,
                         ids=[f'{row[0]}={row[1]!r}' for row in REFUSALS])
def test_invalid_value_is_refused_at_load(project_root, config, key, value, prepare, match):  # noqa: F811
    profile = deepcopy(config)
    if prepare is not None:
        prepare(profile)
    set_key(profile, key, value)
    with pytest.raises(ConfigurationError, match=match):
        load(project_root, profile)


SENTINEL_KEYS = ['embedding.backend', 'embedding.request', 'embedding.request.shape',
                 'embedding.request.image_instruction', 'embedding.request.extra_body',
                 'embedding.request.text_template', 'embedding.dimension', 'embedding.dimension_mode',
                 'embedding.batch_size', 'vision.transport', 'vlm.backends.embedder.endpoints.embeddings',
                 'vlm.backends.embedder.base_urls', 'vlm.backends.embedder.http.read_timeout_s',
                 'vlm.backends.embedder.image.max_decode_pixels']


@pytest.mark.parametrize('key', SENTINEL_KEYS)
def test_unresolved_sentinel_is_refused_by_kb_sync_and_by_k1(project_root, config, key):  # noqa: F811
    profile = deepcopy(config)
    sentinel = 'design_required' if 'request' in key or key.endswith(('dimension_mode',)) else 'deploy_required'
    set_key(profile, key, sentinel)
    with pytest.raises(ConfigurationError, match='unresolved sentinel'):
        load(project_root, profile)
    path = project_root / 'k1-status.yaml'
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)  # a command that does not read K1 loads
    with pytest.raises(ConfigurationError, match=re.escape(key) + r'(\.0)?: unresolved sentinel'):
        embedder_settings(loaded)


E = 'vlm.backends.embedder.'
# Every key K1's standalone entry points read (T-008 fix round 1, table 2), containers before the leaves K1 indexes.
STANDALONE_READS = [
    'embedding.chosen', 'embedding.candidates', 'embedding.revision', 'embedding.backend', 'embedding.request',
    'embedding.request.shape', 'embedding.request.image_instruction', 'embedding.request.extra_body',
    'embedding.request.text_template', 'embedding.dimension', 'embedding.dimension_mode', 'embedding.batch_size',
    'vision.max_image_side', 'vision.format', 'vision.quality', 'vision.alpha_policy', 'vision.transport',
    E + 'provider', E + 'engine', E + 'engine.name', E + 'engine.version', E + 'base_urls', E + 'model.id',
    E + 'model.revision', E + 'endpoints.embeddings', E + 'http', E + 'http.connect_timeout_s',
    E + 'http.read_timeout_s', E + 'http.write_timeout_s', E + 'http.pool_timeout_s', E + 'http.max_retries',
    E + 'http.backoff_initial_s', E + 'http.backoff_max_s', E + 'http.retry_statuses', E + 'max_concurrency',
    E + 'image.max_decode_pixels']
NESTED_SENTINELS = [('embedding.candidates.0.id', lambda p: p['embedding']['candidates'][0]),
                    ('vision.alpha_policy.mode', lambda p: p['vision']['alpha_policy'])]


def test_standalone_read_set_is_the_list_k1_reads(model_test_profile):
    assert list(embedder_reads(k1_profile(model_test_profile), 'profile')) == STANDALONE_READS


def refuse_before_any_read_or_request(profile, match, monkeypatch, tmp_path):
    """Both standalone entry points refuse before a source read (recorded by a labelled double) or a request."""
    reads = []
    monkeypatch.setattr(k1_embed, 'read_source_file', lambda *args: reads.append(args))
    server = EmbeddingServer()
    with pytest.raises(ConfigurationError, match=match):
        asyncio.run(k1_embed.embed_images(None, profile, {'images': [], 'folder_images': []},
                                          knowledge_root=tmp_path, transport=server.transport()))
    with pytest.raises(ConfigurationError, match=match):
        asyncio.run(k1_embed.embed_concept_texts(None, profile, [], transport=server.transport()))
    assert reads == [] and server.requests == []


@pytest.mark.parametrize('key', STANDALONE_READS)
def test_standalone_entry_points_refuse_a_sentinel_in_every_read_key(model_test_profile, monkeypatch, tmp_path, key):
    profile = k1_profile(model_test_profile)
    set_key(profile, key, 'design_required')
    refuse_before_any_read_or_request(profile, re.escape(key) + ': unresolved sentinel design_required',
                                      monkeypatch, tmp_path)


@pytest.mark.parametrize(('key', 'node'), NESTED_SENTINELS, ids=[row[0] for row in NESTED_SENTINELS])
def test_standalone_entry_points_refuse_a_nested_sentinel(model_test_profile, monkeypatch, tmp_path, key, node):
    profile = k1_profile(model_test_profile)
    node(profile)[key.rsplit('.', 1)[1]] = 'design_required'
    refuse_before_any_read_or_request(profile, re.escape(key) + ': unresolved sentinel', monkeypatch, tmp_path)


@pytest.mark.parametrize('key', STANDALONE_READS)
def test_standalone_entry_points_refuse_a_missing_read_key(model_test_profile, monkeypatch, tmp_path, key):
    profile = k1_profile(model_test_profile)
    set_key(profile, key, DELETE)
    refuse_before_any_read_or_request(profile, re.escape(key) + ': missing key', monkeypatch, tmp_path)


@pytest.mark.parametrize(('base_urls', 'route'), [([], 'v1/embeddings'), (['ftp://127.0.0.1'], 'v1/embeddings'),
                                                  (['http://127.0.0.1:8080'], '../escape')])
def test_standalone_entry_points_refuse_an_unusable_endpoint(model_test_profile, monkeypatch, tmp_path, base_urls,
                                                             route):
    profile = k1_profile(model_test_profile)
    profile['vlm']['backends']['embedder']['base_urls'] = base_urls
    profile['vlm']['backends']['embedder']['endpoints']['embeddings'] = route
    refuse_before_any_read_or_request(profile, 'vlm.backends.embedder', monkeypatch, tmp_path)


def test_candidates_sentinel_refuses_k1_after_a_command_that_does_not_read_it(project_root, config, monkeypatch,
                                                                               tmp_path):  # noqa: F811
    """Review F2 probe: a profile loaded for `kb status` keeps `embedding.candidates: design_required`."""
    profile = deepcopy(config)
    profile['embedding']['candidates'] = 'design_required'
    path = project_root / 'k1-candidates.yaml'
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    refuse_before_any_read_or_request(loaded, 'embedding.candidates: unresolved sentinel design_required',
                                      monkeypatch, tmp_path)


ARROW_LIST_MAX = 2 ** 31 - 1  # test-only oracle: pyarrow `list_(value_type, int list_size)` takes a C int


def test_dimension_bound_is_arrows_list_size_in_the_schema():
    """The schema is the one source of the bound; both paths apply it (fix round 3 retired the code mirror)."""
    schema = json.loads((ROOT / 'contracts' / 'schemas' / 'enhancer_config.v0.schema.json').read_text('utf-8'))
    node = schema['properties']['embedding']['anyOf'][0]['properties']['dimension']['anyOf'][0]
    assert node == {'type': 'integer', 'minimum': 1, 'maximum': ARROW_LIST_MAX}


def test_dimension_at_the_upper_bound_loads_and_builds_empty_tables(project_root, config, tmp_path):  # noqa: F811
    profile = deepcopy(config)
    profile['embedding']['dimension'] = ARROW_LIST_MAX
    loaded = load(project_root, profile)
    assert embedder_settings(loaded).dimension == ARROW_LIST_MAX
    store = make_store(project_root, tmp_path / 'kb')
    server = EmbeddingServer()
    images = asyncio.run(k1_embed.embed_images(store, loaded, {'images': [], 'folder_images': []},
                                               knowledge_root=tmp_path, transport=server.transport()))
    texts = asyncio.run(k1_embed.embed_concept_texts(store, loaded, [], transport=server.transport()))
    for table in (images['image_embeddings'], texts['concept_text_embeddings']):  # empty: nothing is allocated
        assert table.num_rows == 0 and table.schema.field('vector').type.list_size == ARROW_LIST_MAX
    assert server.requests == []


def test_dimension_above_the_upper_bound_is_refused_by_name_at_load(project_root, config):  # noqa: F811
    profile = deepcopy(config)
    profile['embedding']['dimension'] = ARROW_LIST_MAX + 1
    with pytest.raises(ConfigurationError, match=r'embedding\.dimension'):
        load(project_root, profile)


@pytest.mark.parametrize('value', [ARROW_LIST_MAX + 1, 2 ** 64, 0, -1, True, 4.0, '4'])
def test_standalone_entry_points_refuse_an_unrepresentable_dimension(model_test_profile, monkeypatch, tmp_path,
                                                                      value):
    profile = k1_profile(model_test_profile, dimension=value)
    refuse_before_any_read_or_request(profile, r'embedding\.dimension', monkeypatch, tmp_path)


@pytest.mark.parametrize('value', [0, 101, 2 ** 64, True, 80.0])
def test_standalone_entry_points_refuse_a_quality_outside_the_schema_range(model_test_profile, monkeypatch,
                                                                           tmp_path, value):
    """Fix round 2 audit: `2**64` used to reach Pillow's encoder (OverflowError) after the sources were read."""
    profile = k1_profile(model_test_profile)
    profile['vision']['quality'] = value
    refuse_before_any_read_or_request(profile, r'vision\.quality: ', monkeypatch, tmp_path)


def test_unrepresentable_dimension_is_refused_before_any_request_on_nonempty_input(project_root, tmp_path,
                                                                                     model_test_profile):
    """Retest 1 probe `test_dimension_numeric_boundary`: one image and one text, refused before the server."""
    source = make_source(tmp_path / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'kb')
    profile = k1_profile(model_test_profile, dimension=ARROW_LIST_MAX + 1)
    server = EmbeddingServer()
    with pytest.raises(ConfigurationError, match=r'embedding\.dimension'):
        asyncio.run(k1_embed.embed_images(store, profile, k0_handoff(store, source), knowledge_root=source,
                                          transport=server.transport()))
    with pytest.raises(ConfigurationError, match=r'embedding\.dimension'):
        asyncio.run(k1_embed.embed_concept_texts(store, profile, [k1_embed.ConceptText(
            concept_id('Kimono'), 'name', 'Kimono')], transport=server.transport()))
    assert server.requests == []


@hsettings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.one_of(st.integers(-3, 3), st.integers(ARROW_LIST_MAX - 3, ARROW_LIST_MAX + 3), st.integers()))
def test_dimension_is_accepted_exactly_inside_the_representable_range(model_test_profile, value):
    profile = k1_profile(model_test_profile, dimension=value)
    if 1 <= value <= ARROW_LIST_MAX:
        assert embedder_settings(profile).dimension == value
    else:
        with pytest.raises(ConfigurationError, match=r'embedding\.dimension'):
            embedder_settings(profile)


def test_unread_embedder_probe_settings_do_not_block_kb_sync(project_root, config):  # noqa: F811
    profile = deepcopy(config)
    profile['vlm']['backends']['embedder']['probe'] = {'max_requests': 'design_required', 'timeout_s': 'design_required'}
    assert load(project_root, profile)['embedding']['backend'] == 'embedder'


def test_role_and_default_backend_need_a_chat_route(project_root, config):  # noqa: F811
    profile = deepcopy(config)
    profile['vlm']['roles']['observer']['backend'] = 'embedder'
    with pytest.raises(ConfigurationError, match='vlm.roles.observer.backend: .*no endpoints.chat'):
        load(project_root, profile)
    profile = deepcopy(config)
    profile['vlm']['default_backend'] = 'embedder'
    with pytest.raises(ConfigurationError, match='vlm.default_backend: .*no endpoints.chat'):
        load(project_root, profile)


# Fix round 3 (R2F2, Gemini F-01): every number K1 reads, against its schema domain, on both paths. The domains are a
# test-only oracle transcribed from contracts/schemas/enhancer_config.v0.schema.json; the code reads the schema.
INT, INT_OR_NONE, POSITIVE, NONNEGATIVE = 'integer', 'integer or none', 'number > 0', 'number >= 0'
NUMERIC_READS = [  # (key, domain, lower, upper, a non-default value that must reach the settings)
    ('embedding.dimension', INT, 1, ARROW_LIST_MAX, 256),
    ('embedding.batch_size', INT, 1, None, 7),
    ('vision.max_image_side', INT_OR_NONE, 1, None, 3),
    ('vision.quality', INT, 1, 100, 55),
    (E + 'max_concurrency', INT, 1, None, 3),
    (E + 'image.max_decode_pixels', INT, 1, None, 5000),
    (E + 'http.connect_timeout_s', POSITIVE, None, None, 2.5),
    (E + 'http.read_timeout_s', POSITIVE, None, None, 2.5),
    (E + 'http.write_timeout_s', POSITIVE, None, None, 2.5),
    (E + 'http.pool_timeout_s', POSITIVE, None, None, 2.5),
    (E + 'http.max_retries', INT, 0, None, 2),
    (E + 'http.backoff_initial_s', NONNEGATIVE, None, None, 0.5),
    (E + 'http.backoff_max_s', NONNEGATIVE, None, None, 1.5),
]
NUMERIC_KEYS = [row[0] for row in NUMERIC_READS]


FLOAT_MAX = sys.float_info.max  # test-only oracle (IEEE 754 binary64)
FIRST_UNCONVERTIBLE = 2 ** 1024 - 2 ** 970  # test-only oracle: the least int that float() cannot convert (timer_probe)


def converts_to_finite_float(value):
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def in_domain(domain, lower, upper, value):
    if domain == INT_OR_NONE and value == 'none':
        return True
    if domain in (INT, INT_OR_NONE):
        return type(value) is int and value >= lower and (upper is None or value <= upper)
    if type(value) not in (int, float) or not converts_to_finite_float(value):  # fix round 4: the timer clocks
        return False
    return value > 0 if domain == POSITIVE else value >= 0


def numeric_profile(profile, key, value):
    """Set one numeric read value; the backoff pair is opened wide so only the value's own domain decides."""
    http = profile['vlm']['backends']['embedder']['http']
    if key.endswith('backoff_initial_s'):
        # test-only: the largest valid timer (an exact int compares above FLOAT_MAX), so at or above any valid initial
        http['backoff_max_s'] = FIRST_UNCONVERTIBLE - 1
    set_key(profile, key, value)
    return profile


def read_value(settings, key):
    if key.startswith(E):
        node = settings.backend
        for part in key[len(E):].split('.'):
            node = node[part]
        return node
    if key.startswith('vision.'):
        return settings.vision[key.split('.')[1]]
    return getattr(settings, key.split('.')[1])


INVALID = [-1, 0, True, False, 2.0, float('nan'), float('inf'), float('-inf'), '1', '', None, 10 ** 400,
           FIRST_UNCONVERTIBLE]


def invalid_cases():
    for key, domain, lower, upper, _ in NUMERIC_READS:
        for value in INVALID + ([upper + 1] if upper else []):
            if not in_domain(domain, lower, upper, value):
                yield key, value


INVALID_CASES = list(invalid_cases())


@pytest.mark.parametrize(('key', 'value'), INVALID_CASES, ids=[f'{key}={value!r}' for key, value in INVALID_CASES])
def test_standalone_entry_points_refuse_an_invalid_number_by_name(model_test_profile, monkeypatch, tmp_path, key,
                                                                    value):
    profile = numeric_profile(k1_profile(model_test_profile), key, value)
    refuse_before_any_read_or_request(profile, re.escape(key) + ': ', monkeypatch, tmp_path)


@pytest.mark.parametrize(('key', 'value'), INVALID_CASES, ids=[f'{key}={value!r}' for key, value in INVALID_CASES])
def test_kb_sync_refuses_an_invalid_number_by_name(project_root, config, key, value):  # noqa: F811
    with pytest.raises(ConfigurationError, match=re.escape(key) + ': '):
        load(project_root, numeric_profile(deepcopy(config), key, value))


@pytest.mark.parametrize(('key', 'override'), [(row[0], row[4]) for row in NUMERIC_READS], ids=NUMERIC_KEYS)
def test_numeric_override_reaches_the_settings_on_both_paths(project_root, config, key, override):  # noqa: F811
    assert read_value(embedder_settings(numeric_profile(deepcopy(config), key, override)), key) == override
    assert read_value(embedder_settings(load(project_root, numeric_profile(deepcopy(config), key, override))),
                      key) == override


numeric_values = st.one_of(st.integers(-3, 3), st.integers(), st.booleans(), st.none(), st.text(max_size=2),
                           st.floats(), st.sampled_from([2.0, 0.5, -0.0, 1e308, float('nan'), float('inf'), 'none']),
                           st.sampled_from([10 ** 300, 10 ** 400, FLOAT_MAX, FIRST_UNCONVERTIBLE,
                                            FIRST_UNCONVERTIBLE - 1, -FIRST_UNCONVERTIBLE]),
                           st.integers(min_value=2 ** 1000, max_value=2 ** 1100))


@hsettings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.sampled_from(NUMERIC_READS), numeric_values)
def test_standalone_preflight_accepts_exactly_the_schema_domain(model_test_profile, row, value):
    key, domain, lower, upper, _ = row
    profile = numeric_profile(k1_profile(model_test_profile), key, value)
    if in_domain(domain, lower, upper, value) and not (isinstance(value, str) and value in pe_config.SENTINELS):
        assert read_value(embedder_settings(profile), key) == value
    else:
        with pytest.raises(ConfigurationError, match=re.escape(key)):
            embedder_settings(profile)


STATUSES = E + 'http.retry_statuses'
BAD_STATUSES = [[99], [600], [True], [503.0], [503, 503], 503, ['503'], [float('nan')]]


@pytest.mark.parametrize('value', BAD_STATUSES, ids=[repr(value) for value in BAD_STATUSES])
def test_retry_statuses_are_refused_by_name_on_both_paths(project_root, config, monkeypatch, tmp_path,
                                                          value):  # noqa: F811
    refuse_before_any_read_or_request(numeric_profile(deepcopy(config), STATUSES, value), re.escape(STATUSES),
                                      monkeypatch, tmp_path)
    with pytest.raises(ConfigurationError, match=re.escape(STATUSES)):
        load(project_root, numeric_profile(deepcopy(config), STATUSES, value))


def test_retry_statuses_override_reaches_the_settings_on_both_paths(project_root, config):  # noqa: F811
    profile = numeric_profile(deepcopy(config), STATUSES, [429, 503])
    assert read_value(embedder_settings(profile), STATUSES) == [429, 503]
    assert read_value(embedder_settings(load(project_root, profile)), STATUSES) == [429, 503]


def test_backoff_order_is_refused_by_name_on_both_paths(project_root, config, monkeypatch, tmp_path):  # noqa: F811
    profile = deepcopy(config)
    profile['vlm']['backends']['embedder']['http'].update(backoff_initial_s=2, backoff_max_s=1)
    refuse_before_any_read_or_request(profile, re.escape(E + 'http.backoff_max_s'), monkeypatch, tmp_path)
    with pytest.raises(ConfigurationError, match=re.escape(E + 'http.backoff_max_s')):
        load(project_root, profile)


@pytest.mark.parametrize('path', ['image', 'text'])
def test_negative_batch_is_refused_before_any_read_or_checkpoint(project_root, tmp_path, model_test_profile,
                                                                 monkeypatch, path):
    """Retest 2 probe `test_negative_batch_checkpoint`: `-1` used to return an empty, apparently successful result."""
    source = make_source(tmp_path / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'kb')
    handoff = k0_handoff(store, source)
    reads = []
    monkeypatch.setattr(k1_embed, 'read_source_file', lambda *args: reads.append(args))  # labelled recorder
    server = EmbeddingServer()
    profile = k1_profile(model_test_profile, batch_size=-1)
    with pytest.raises(ConfigurationError, match=r'embedding\.batch_size: '):
        if path == 'image':
            asyncio.run(k1_embed.embed_images(store, profile, handoff, knowledge_root=source,
                                              transport=server.transport()))
        else:
            asyncio.run(k1_embed.embed_concept_texts(store, profile, [k1_embed.ConceptText(
                concept_id('Kimono'), 'name', 'Kimono')], transport=server.transport()))
    assert reads == [] and server.requests == []


def test_shipped_profiles_load_and_k1_refuses_their_design_sentinels(project_root, tmp_path):
    folder = tmp_path / 'configs'
    folder.mkdir()
    for name in ('enhancer.example.yaml', 'enhancer.dev.example.yaml'):
        shutil.copy(ROOT / 'configs' / name, folder / name)
    local = folder / 'dev.local.yaml'
    local.write_text('schema_version: 1\nextends: enhancer.dev.example.yaml\n'
                     'storage: {lock: {timeout_s: 5, poll_s: 0.05}}\n', encoding='utf-8')  # test-only lock values
    dev = load_config(local, 'kb status', project_root=project_root)
    assert dev['embedding']['backend'] == 'embedder' and dev['embedding']['dimension'] == 768
    assert dev['vlm']['backends']['embedder']['endpoints']['embeddings'] == 'v1/embeddings'
    with pytest.raises(ConfigurationError, match='unresolved sentinel'):
        embedder_settings(dev)
    full = folder / 'full.local.yaml'
    full.write_text('schema_version: 1\nextends: enhancer.example.yaml\n'
                    'storage: {lock: {timeout_s: 5, poll_s: 0.05}}\n', encoding='utf-8')
    example = load_config(full, 'kb status', project_root=project_root)
    with pytest.raises(ConfigurationError, match='embedding.backend: unresolved sentinel deploy_required'):
        embedder_settings(example)


# Fix round 4 (R3F1, R3F2): the domain each consumer needs, on both paths, before any read or request.
TIMERS = [E + 'http.' + field for field in ('connect_timeout_s', 'read_timeout_s', 'write_timeout_s',
                                            'pool_timeout_s', 'backoff_initial_s', 'backoff_max_s')]
FORMAT_REFUSAL = r'vision\.format: no installed Pillow encoder'


def independent_role_formats(profile):
    """Retest 3: every role image format names an installed encoder itself, so only K1's own vision.format counts."""
    for role in profile['vlm']['roles'].values():
        if isinstance(role, dict) and isinstance(role.get('image'), dict):
            role['image']['format'] = 'jpeg'
    return profile


def k1_on_a_real_source(project_root, folder, profile, transport):
    """Both entry points: a real one-image K0 handoff and one concept text."""
    source = make_source(folder / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, folder / 'kb')
    handoff = k0_handoff(store, source)
    images = asyncio.run(k1_embed.embed_images(store, profile, handoff, knowledge_root=source, transport=transport))
    texts = asyncio.run(k1_embed.embed_concept_texts(
        store, profile, [k1_embed.ConceptText(concept_id('Kimono'), 'name', 'Kimono')], transport=transport))
    return images, texts


def refuse_on_a_real_source(project_root, tmp_path, monkeypatch, profile, match):
    """Each entry point is refused by name with no source read (labelled recorder) and no request (labelled double)."""
    source = make_source(tmp_path / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'kb')
    handoff = k0_handoff(store, source)
    reads = []
    monkeypatch.setattr(k1_embed, 'read_source_file', lambda *args: reads.append(args))
    server = EmbeddingServer()
    with pytest.raises(ConfigurationError, match=match):
        asyncio.run(k1_embed.embed_images(store, profile, handoff, knowledge_root=source,
                                          transport=server.transport()))
    with pytest.raises(ConfigurationError, match=match):
        asyncio.run(k1_embed.embed_concept_texts(store, profile, [k1_embed.ConceptText(
            concept_id('Kimono'), 'name', 'Kimono')], transport=server.transport()))
    assert reads == [] and server.requests == []


@pytest.mark.parametrize('fmt', ['', 'no_such_encoder'])
def test_vision_format_without_an_installed_encoder_is_refused_before_any_read(project_root, config, tmp_path,
                                                                               monkeypatch, fmt):  # noqa: F811
    """Retest 3 probe `test_deferred_format` (R3F1): `''` used to fall back to the source format and send
    `data:image/;base64`; an unknown name raised KeyError after the source read."""
    profile = independent_role_formats(deepcopy(config))
    profile['vision']['format'] = fmt
    with pytest.raises(ConfigurationError, match=FORMAT_REFUSAL):
        load(project_root, profile)
    refuse_on_a_real_source(project_root, tmp_path, monkeypatch, profile, FORMAT_REFUSAL)


@pytest.mark.parametrize('fmt', ['', 'no_such_encoder'])
def test_a_configured_format_is_never_replaced_by_the_source_format(fmt):
    data = image_bytes()
    with pytest.raises(ValueError, match='output_format: no installed encoder'):
        prepare_image_with_info(data, 'none', 1000, alpha_policy={'mode': 'refuse'}, output_format=fmt)
    # None is a caller that configures no format (T-009's default path): the unchanged source bytes stay.
    assert prepare_image_with_info(data, 'none', 1000, alpha_policy={'mode': 'refuse'})[0] == data


def test_the_encoder_registry_is_complete_in_a_fresh_process():
    """A fresh interpreter knows only Pillow's preinit encoders (BMP, GIF, JPEG, PPM, PNG; PIL/Image.py:338-374), so
    installed_encoder registers every plugin first, or an installed WEBP encoder would be refused."""
    code = 'from prompt_enhancer.config import installed_encoder; print(installed_encoder("webp"))'
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run([sys.executable, '-B', '-X', 'faulthandler', '-c', code], env=env, capture_output=True,
                            text=True, timeout=120, creationflags=flags)
    assert (result.returncode, result.stdout.strip()) == (0, 'True'), result.stderr


@pytest.mark.parametrize(('fmt', 'decoded'), [('png', 'PNG'), ('webp', 'WEBP')])
def test_non_default_encoder_reaches_the_request_body_on_both_paths(project_root, config, tmp_path, fmt,
                                                                    decoded):  # noqa: F811
    profile = independent_role_formats(deepcopy(config))
    profile['vision']['format'] = fmt  # the conftest default is jpeg
    for name, settings in (('standalone', profile), ('loader', load(project_root, profile))):
        server = EmbeddingServer()
        images, _ = k1_on_a_real_source(project_root, tmp_path / name, settings, server.transport())
        assert images['report']['embedded'] == 1
        header, image = data_uri_image(server.requests[0][1]['input'][0]['content'][0])
        assert header == f'data:image/{fmt};base64' and image.format == decoded


def test_the_float_rule_covers_exactly_the_schema_numbers_k1_reads(model_test_profile):
    schema = json.loads((ROOT / 'contracts/schemas/enhancer_config.v0.schema.json').read_text(encoding='utf-8'))
    numbers = [key for key in embedder_reads(k1_profile(model_test_profile), 'profile')
               if 'number' in pe_config._schema_types(pe_config._schema_at(schema, key))]
    assert numbers == TIMERS


@pytest.mark.parametrize('value', [10 ** 400, FIRST_UNCONVERTIBLE])
@pytest.mark.parametrize('key', TIMERS)
def test_timer_that_does_not_convert_to_a_float_is_refused_before_any_read(project_root, config, tmp_path,
                                                                           monkeypatch, key, value):  # noqa: F811
    """Retest 3 probe `test_deferred_timeout` (R3F2): the integer passed both paths and AnyIO raised OverflowError."""
    profile = numeric_profile(deepcopy(config), key, value)
    match = re.escape(key) + ': the number does not convert to a finite float'
    with pytest.raises(ConfigurationError, match=match):
        load(project_root, profile)
    refuse_on_a_real_source(project_root, tmp_path, monkeypatch, profile, match)


@pytest.mark.parametrize('value', [10 ** 300, FLOAT_MAX, FIRST_UNCONVERTIBLE - 1])
@pytest.mark.parametrize('key', TIMERS)
def test_large_convertible_timer_is_accepted_on_both_paths(project_root, config, key, value):  # noqa: F811
    profile = numeric_profile(deepcopy(config), key, value)
    assert read_value(embedder_settings(profile), key) == value
    assert read_value(embedder_settings(load(project_root, profile)), key) == value


def test_large_timers_reach_the_real_deadline_primitive(project_root, config, tmp_path):  # noqa: F811
    """The retest 3 timeout probe with valid values: a labelled HTTP double (no socket) enters the installed
    `anyio.fail_after` with each timeout the request carries, as httpcore does, then answers as EmbeddingServer."""
    profile = deepcopy(config)
    profile['vlm']['backends']['embedder']['http'].update(connect_timeout_s=10 ** 300, read_timeout_s=10 ** 300,
                                                          write_timeout_s=FLOAT_MAX, pool_timeout_s=2.5)
    server, deadlines = EmbeddingServer(), []

    async def labelled_deadline_double(request):
        for kind in ('connect', 'read', 'write', 'pool'):
            with anyio.fail_after(request.extensions['timeout'][kind]) as scope:
                deadlines.append(scope.deadline)
        return server.handler(request)

    for name, settings in (('standalone', profile), ('loader', load(project_root, profile))):
        images, texts = k1_on_a_real_source(project_root, tmp_path / name, settings,
                                            httpx.MockTransport(labelled_deadline_double))
        assert images['report']['embedded'] == 1 and texts['report']['embedded'] == 1
    assert len(deadlines) == 4 * 4 and all(math.isfinite(deadline) for deadline in deadlines)


def test_huge_integer_counters_embed_on_both_paths(project_root, config, tmp_path, monkeypatch):  # noqa: F811
    """The audit's integer rows: K1's integer consumers are Python ints (range, slices, Semaphore, comparisons,
    Image.MAX_IMAGE_PIXELS), so a huge exact integer is valid and works; only timers have a float limit."""
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', Image.MAX_IMAGE_PIXELS)  # restores the process-wide backstop
    huge = 10 ** 400
    profile = deepcopy(config)
    profile['embedding']['batch_size'] = huge
    profile['vision']['max_image_side'] = huge
    backend = profile['vlm']['backends']['embedder']
    backend['max_concurrency'] = huge
    backend['http']['max_retries'] = huge
    backend['image']['max_decode_pixels'] = huge
    for name, settings in (('standalone', profile), ('loader', load(project_root, profile))):
        server = EmbeddingServer()
        images, texts = k1_on_a_real_source(project_root, tmp_path / name, settings, server.transport())
        assert images['report']['embedded'] == 1 and texts['report']['embedded'] == 1
        assert len(server.requests) == 2


# Fix round 5 (R4F1, N1). The oracle is Pillow's initialized registry, never a list of format or media type words.
Image.init()
ENCODERS = sorted(Image.SAVE)
SENT = [name for name in ENCODERS if str(Image.MIME.get(name, '')).startswith('image/')]
NOT_SENT = [name for name in ENCODERS if name not in SENT]
MEDIA_TYPE_REFUSAL = r'vision\.format: the Pillow encoder .* registers no image/ media type'


def test_registered_media_type_reaches_the_request_body_on_both_paths(project_root, config,
                                                                      tmp_path):  # noqa: F811
    """Retest 4 probe `test_mime_registry_mapping` (R4F1): JPEG2000 bytes were sent as image/jpeg2000, the encoder
    name, while Pillow registers image/jp2 (PIL/Jpeg2KImagePlugin.py:442)."""
    profile = independent_role_formats(deepcopy(config))
    profile['vision']['format'] = 'jpeg2000'
    for name, settings in (('standalone', profile), ('loader', load(project_root, profile))):
        server = EmbeddingServer()
        images, _ = k1_on_a_real_source(project_root, tmp_path / name, settings, server.transport())
        assert images['report']['embedded'] == 1
        header, image = data_uri_image(server.requests[0][1]['input'][0]['content'][0])
        assert image.format == 'JPEG2000' and header == f'data:{Image.MIME[image.format]};base64'
        assert header == 'data:image/jp2;base64'


@pytest.mark.parametrize('encoder', SENT)
def test_every_installed_encoder_sends_its_registered_media_type(model_test_profile, encoder):
    """The request body of each installed encoder with a registered image/ type states exactly that type."""
    profile = k1_profile(model_test_profile)
    profile['vision']['format'] = encoder.lower()  # the configured spelling; the registry keys are upper case
    embedder = k1_embed.Embedder(embedder_settings(profile))
    try:
        body, = embedder.image_bodies([b'labelled image bytes'])
    finally:
        asyncio.run(embedder.close())
    header = body['input'][0]['content'][-1]['image_url']['url'].split(',', 1)[0]
    assert header == f'data:{Image.MIME[encoder]};base64'


@pytest.mark.parametrize('encoder', NOT_SENT)
def test_an_encoder_without_a_registered_image_media_type_is_refused_before_any_read(
        project_root, config, tmp_path, monkeypatch, encoder):  # noqa: F811
    profile = independent_role_formats(deepcopy(config))
    profile['vision']['format'] = encoder.lower()
    with pytest.raises(ConfigurationError, match=MEDIA_TYPE_REFUSAL):
        load(project_root, profile)
    refuse_on_a_real_source(project_root, tmp_path, monkeypatch, profile, MEDIA_TYPE_REFUSAL)


def test_a_role_format_without_a_registered_image_media_type_is_refused_at_load(project_root, config):  # noqa: F811
    profile = independent_role_formats(deepcopy(config))
    role = next(role for role in profile['vlm']['roles'].values()
                if isinstance(role, dict) and isinstance(role.get('image'), dict))
    role['image']['format'] = NOT_SENT[0].lower()
    with pytest.raises(ConfigurationError, match=r'image\.format: the Pillow encoder .* registers no image/'):
        load(project_root, profile)


@pytest.mark.parametrize(('initial', 'cap'), [(0.0, 0.0), (0, 0), (5e-324, 1e-9)])
@pytest.mark.parametrize('path', ['image', 'text'])
def test_a_long_retry_sequence_ends_in_the_failed_item_checkpoint(project_root, config, tmp_path, path, initial,
                                                                  cap):  # noqa: F811
    """Retest 4 probe `test_inherited_retry_product` (N1): with 1025 retries and zero backoffs the old
    `backoff_initial_s * 2**attempt` raised OverflowError at attempt 1024, aborting the stage with no checkpoint.
    Real asyncio.sleep with zero or tiny capped delays; the HTTP double (labelled) always answers 503."""
    profile = deepcopy(config)
    profile['vlm']['backends']['embedder']['http'].update(max_retries=1025, backoff_initial_s=initial,
                                                          backoff_max_s=cap, retry_statuses=[503])
    loaded = load(project_root, profile)
    settings = embedder_settings(loaded)
    source = make_source(tmp_path / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'kb')
    handoff = k0_handoff(store, source)
    calls = []

    def labelled_retry_double(request):
        calls.append(request)
        return httpx.Response(503, json={'error': 'labelled retry double'})

    transport = httpx.MockTransport(labelled_retry_double)
    if path == 'image':
        result = asyncio.run(k1_embed.embed_images(store, loaded, handoff, knowledge_root=source,
                                                   transport=transport))
        stage, key = k1_embed.IMAGE_STAGE, k1_embed.image_key(settings, handoff['images'][0]['image_id'])
    else:
        result = asyncio.run(k1_embed.embed_concept_texts(
            store, loaded, [k1_embed.ConceptText(concept_id('Kimono'), 'name', 'Kimono')], transport=transport))
        stage, key = k1_embed.TEXT_STAGE, k1_embed.text_key(settings, 'Kimono')
    assert list(result['report']['failed'].values()) == ['HTTP_ERROR'] and len(calls) == 1026
    with store.writer() as writer:
        assert writer.checkpoints(stage).state(key)['state'] == 'failed'
