"""S1 configuration gates with explicitly synthetic values; no model or network calls."""

from copy import deepcopy
import sys

import pytest
from hypothesis import given, strategies as st

from intake_fixtures import configuration
from prompt_enhancer.config import (
    CANDIDATE_READS, ConfigurationError, effective_request_config, embedder_reads,
    validate_candidates_reads, validate_embedder_reads,
)
from test_intake_config import leaf, load
from test_k0_config import config as kb_sync_config  # noqa: F401  (existing synthetic resolved sync fixture)


@pytest.fixture
def candidate_config(model_test_profile, tmp_path):
    config = configuration(model_test_profile, tmp_path)
    config['rag']['enabled'] = True
    return config


@pytest.mark.parametrize('key', CANDIDATE_READS)
@pytest.mark.parametrize('bad', ['missing', 'fit_from_gt', None, [], {}])
def test_candidates_read_refusal(candidate_config, project_root, key, bad):
    parent, name = leaf(candidate_config, key)
    if bad == 'missing':
        del parent[name]
    else:
        parent[name] = bad
    with pytest.raises(ConfigurationError, match=key.replace('.', r'\.')):
        validate_candidates_reads(candidate_config, project_root=project_root)


@pytest.mark.parametrize('key', CANDIDATE_READS)
def test_candidates_read_override(candidate_config, project_root, key):
    parent, name = leaf(candidate_config, key)
    value = parent[name]
    replacement = (not value if type(value) is bool else value + 1 if type(value) is int
                   else 'search: {text}' if key.endswith('template') else 'rrf')
    parent[name] = replacement
    validate_candidates_reads(candidate_config, project_root=project_root)
    assert leaf(candidate_config, key)[0][name] == replacement


@pytest.mark.parametrize('key', ['candidates.shortlist_size', 'candidates.rrf_constant',
                                'candidates.max_prompt_windows', 'embedding.query_text_template'])
def test_candidates_sentinel_refused_at_start(candidate_config, tmp_path, project_root, key):
    parent, name = leaf(candidate_config, key)
    parent[name] = 'fit_from_gt'
    with pytest.raises(ConfigurationError, match=key):
        load(candidate_config, tmp_path, project_root)


@pytest.mark.parametrize('key', ['embedding.query_text_limit', 'candidates.max_prompt_windows',
                                'candidates.sources.alias.depth', 'candidates.sources.lexical.depth',
                                'candidates.sources.dense.depth'])
@pytest.mark.parametrize('value', [True, False, 1.0, 0, -1, sys.maxsize + 1])
def test_candidates_native_integer_domain(candidate_config, project_root, key, value):
    parent, name = leaf(candidate_config, key)
    parent[name] = value
    with pytest.raises(ConfigurationError, match=key):
        validate_candidates_reads(candidate_config, project_root=project_root)


@pytest.mark.parametrize('key,value', [('candidates.fusion', 'weighted'),
                                    ('candidates.rrf_constant', float('nan')),
                                    ('candidates.rrf_constant', float('inf')),
                                    ('candidates.rrf_constant', -1),
                                    ('candidates.rrf_constant', True),
                                    ('candidates.rrf_constant', 10**400),
                                    ('embedding.query_text_template', 'no placeholder'),
                                    ('embedding.query_text_template', '{text}{text}')])
def test_candidates_cross_field_refusals(candidate_config, project_root, key, value):
    parent, name = leaf(candidate_config, key)
    parent[name] = value
    with pytest.raises(ConfigurationError, match=key):
        validate_candidates_reads(candidate_config, project_root=project_root)


def test_query_only_sentinels_do_not_block_k1(candidate_config, project_root):
    candidate_config['embedding']['query_text_template'] = 'design_required'
    candidate_config['embedding']['query_text_limit'] = 'design_required'
    validate_embedder_reads(candidate_config, 'test', {}, project_root)


def test_kb_sync_does_not_read_query_template(kb_sync_config, project_root):
    from prompt_enhancer.config import _command_reads
    from test_k0_config import load as load_sync

    assert kb_sync_config['embedding']['query_text_template'] == 'design_required'
    reads = _command_reads('kb sync', kb_sync_config)
    assert 'embedding.query_text_template' not in reads and 'embedding' not in reads
    assert load_sync(project_root, kb_sync_config)['embedding']['query_text_template'] == 'design_required'


@pytest.mark.parametrize('key', [k for k in embedder_reads(
    {'embedding': {'backend': 'test'}, 'vlm': {'backends': {'test': {'engine': {'name': 'llama_cpp'}}}}},
    'test') if k.startswith('embedding.')])
def test_candidates_transitive_embedding_refused(candidate_config, project_root, key):
    parent, name = leaf(candidate_config, key)
    del parent[name]
    with pytest.raises(ConfigurationError, match=key):
        validate_candidates_reads(candidate_config, project_root=project_root)


def test_base_key_diagnostic_and_override_repair(candidate_config, project_root):
    candidate_config['rag']['enabled'] = False
    candidate_config['vision']['quality'] = 'design_required'
    candidate_config['request_overrides']['allowed_keys'] = ['vision.quality']
    effective = effective_request_config(candidate_config, {'vision.quality': 70}, project_root=project_root)
    assert effective['vision']['quality'] == 70
    assert candidate_config['vision']['quality'] == 'design_required'
    broken = deepcopy(candidate_config)
    broken['vision']['quality'] = 'private/value'
    with pytest.raises(ConfigurationError, match='configuration: vision.quality: invalid base configuration'):
        effective_request_config(broken, {}, project_root=project_root)


@given(st.one_of(st.booleans(), st.floats(allow_nan=True, allow_infinity=True)))
def test_integer_parser_never_coerces(value):
    from prompt_enhancer.config import _EXACT_INTEGERS
    assert not _EXACT_INTEGERS({'type': 'integer'}).is_valid(value)


@pytest.mark.parametrize('field', ['chosen', 'revision', 'backend', 'dimension', 'dimension_mode', 'batch_size',
                                 'candidates', 'request.text_template', 'request.image_instruction',
                                 'request.shape', 'request.extra_body'])
def test_embedding_overrides_reach_query_settings(candidate_config, field):
    from prompt_enhancer.candidates import query_embedder_settings
    from prompt_enhancer.k1_embed import embedder_settings

    config = candidate_config
    before = query_embedder_settings(config)
    embedding = config['embedding']
    if field == 'chosen':
        embedding['chosen'] = 'bge-m3+siglip2'
    elif field == 'revision':
        embedding['revision'] = config['vlm']['backends']['embedder']['model']['revision'] = 'test-other'
    elif field == 'backend':
        config['vlm']['backends']['other'] = deepcopy(config['vlm']['backends']['embedder'])
        embedding['backend'] = 'other'
    elif field == 'dimension':
        embedding['dimension'] = 3
    elif field == 'dimension_mode':
        embedding['dimension_mode'] = 'client_prefix_l2'
    elif field == 'batch_size':
        embedding['batch_size'] = 3
    elif field == 'candidates':
        # The candidate list is a validated selection domain, not a query-body parameter.
        embedding['candidates'] = [{'id': embedding['chosen'], 'note': 'test-only'}]
    elif field in ('request.shape', 'request.extra_body'):
        embedding['request']['shape'] = 'vllm_chat_messages'
        embedding['batch_size'] = 1
        config['vlm']['backends']['embedder']['engine']['name'] = 'vllm'
        embedding['request']['extra_body'] = {'continue_final_message': True}
    else:
        embedding['request'][field.split('.')[1]] = 'document: {text}' if field.endswith('text_template') else 'test-other'
    after = query_embedder_settings(config)
    document = embedder_settings(config)
    assert after.text_template == config['embedding']['query_text_template']
    assert replace_template(after, document.text_template) == document
    if field == 'candidates':
        assert before == after and len(embedding['candidates']) == 1
    elif field == 'request.text_template':
        assert before == after and document.text_template == 'document: {text}'
    else:
        assert before != after


def replace_template(settings, template):
    from dataclasses import replace
    return replace(settings, text_template=template)
