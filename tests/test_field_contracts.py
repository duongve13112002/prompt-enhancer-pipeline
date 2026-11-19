"""Check configuration fields against real example data and the public loader."""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config, load_profile
from prompt_enhancer.config import _configuration_schema


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
SCHEMA = _configuration_schema(ROOT, EXAMPLE)
VALIDATOR = Draft202012Validator(SCHEMA)


def fields(value, prefix=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from fields(child, prefix + (key,))
    else:
        yield prefix


def select_representatives():
    # Backends and roles share schemas; retain one of each structure and each ordinary key.
    seen = set()
    for path in fields(EXAMPLE):
        normalized = list(path)
        if len(path) > 2 and path[:2] in (('vlm', 'roles'), ('vlm', 'backends')):
            normalized[2] = '*'
        normalized = tuple(normalized)
        if normalized not in seen:
            seen.add(normalized)
            yield path


def node(config, path):
    for key in path[:-1]:
        config = config[key]
    return config, path[-1]


def prepare(config, path):
    config['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only: resolves deploy_required (T-006)
    if path[:3] == ('vlm', 'backends', 'main') and (path[-1] == 'provider' or path[-2:] == ('engine', 'name')):
        config['vlm']['backends']['alternate'] = deepcopy(config['vlm']['backends']['main'])
        config['vlm']['default_backend'] = 'alternate'
        for role in config['vlm']['roles'].values():
            role['backend'] = 'alternate'
        config['vlm']['backends']['main'].update(call_budget=3, on_budget_exhausted='stop', allow_images=False,
                                               anthropic_version='2025-01-01', anthropic_workspace_id='none', api_key_file='.secrets/example.key')
    elif path == ('vlm', 'default_backend') or path[:2] == ('vlm', 'roles') and path[-1] == 'backend':
        config['vlm']['backends']['alternate'] = deepcopy(config['vlm']['backends']['main'])
    elif path == ('embedding', 'backend'):  # T-008: the embedder is a named backend with an embeddings route
        config['vlm']['backends']['alternate'] = deepcopy(config['vlm']['backends']['main'])
        config['vlm']['backends']['alternate']['endpoints'] = {'embeddings': 'v1/embeddings', 'health': 'health'}
        for key in ('addendum_layout', 'addendum_separator'):  # T017-FIX: refused on a chat-less backend
            config['vlm']['backends']['alternate'].pop(key, None)


def set_candidate(config, path, candidate):
    """Keep dependent engine/provider fields coherent using the shipped examples."""
    if path[:2] == ('vlm', 'backends'):
        is_provider = path[-1] == 'provider'
        is_engine_name = path[-2:] == ('engine', 'name')
        names = {'openai_compatible': 'main', 'vllm': 'main', 'llama_cpp': 'main',
                 'anthropic': 'cloud_claude', 'gemini': 'cloud_gemini'}
        if (is_provider or is_engine_name) and isinstance(candidate, str) and candidate in names:
            backend = deepcopy(EXAMPLE['vlm']['backends'][names[candidate]])
            if names[candidate] == 'main':
                backend['engine']['name'] = 'llama_cpp'
            config['vlm']['backends'][path[2]] = backend
    parent, key = node(config, path)
    parent[key] = candidate
    return parent, key


def options(value):
    return [False, True, 0, 1, 2,
            3,  # T-016 P4: `evaluation.judge.min_scorers` has a schema floor of 2 and is configurable above it
            0.5, '', 'alternative', 'none', 'auto', 'ask', 'off', 'on',
            'draft', 'confirmed', 'active', 'plain', 'sectioned',
            'medium', 'cards', 'live', 'json_schema', 'json_object', 'engine_native',
            'response_format', 'from_vision', 'intent_ir.v0', 'verifier_result.v0', '2025-01-01',
            'openai_compatible', 'anthropic', 'gemini', 'vllm', 'llama_cpp', 'fallback_to_default_backend', 'stop', 'arrow', '7d', 'refuse', 'flatten', '#123456',
            'openai_input_content', 'native', 'numpy_exact', 'cosine', 'inner_product', 'char_ngram',
            'user_reference',  # T-014: role image.source is a closed enum of five values
            'include', 'omit',  # T-014 P2: assembly.default_outfit and resolution.on_variant_unspecified enums
            'separate_system_message', 'joined_system_message',  # T017-FIX: backend addendum_layout enum
            [],
            ['alternative'], ['generate'], ['edit'],
            ['identity'],  # T-014 P2: scope-matrix rows are lists of facet groups
            {}, {'other': 'alternative'},
            {'chat': 'chat', 'health': 'health'}, {'block_rows': 2},
            {'source': 'source_image', 'format': 'png', 'quality': 80, 'transport': 'data_uri'},
            value + '_alternative' if isinstance(value, str) else 'alternative']


PATHS = list(fields(EXAMPLE))


def test_protocol_only_fields():
    constrained = []
    for path in select_representatives():
        config = deepcopy(EXAMPLE)
        prepare(config, path)
        parent, key = node(config, path)
        original = parent[key]
        for candidate in options(original):
            if candidate == original and type(candidate) is type(original):
                continue
            parent, key = set_candidate(config, path, candidate)
            if VALIDATOR.is_valid(config):
                break
        else:
            constrained.append('.'.join(path))
    assert constrained == ['schema_version', 'knowledge.info_file.on_description_count_mismatch',
                           'index.lexical.kind', 'candidates.fusion',
                           'assembly.fact_order',  # T-014 P2: need_then_support is the one implemented order
                           'vision.alpha_policy.mode', 'vision.transport', 'vlm.backends.main.unsupported_param_policy',
                           # T-018: the embedding server's three enum-valued keys (the first backend that has them)
                           'vlm.backends.embedder_qwen3vl8b.server.family', 'vlm.backends.embedder_qwen3vl8b.server.dtype',
                           'vlm.backends.embedder_qwen3vl8b.server.attn_implementation',
                           'vlm.roles.planner.mode', 'vlm.roles.planner.output.validate_response']


def test_all_constrained_fields_are_known():
    unexpected = []
    for path in PATHS:
        config = deepcopy(EXAMPLE)
        prepare(config, path)
        parent, key = node(config, path)
        original = parent[key]
        for candidate in options(original):
            if candidate == original and type(candidate) is type(original):
                continue
            parent, key = set_candidate(config, path, candidate)
            if VALIDATOR.is_valid(config):
                break
        else:
            allowed = (path == ('schema_version',)
                       or path == ('knowledge', 'info_file', 'on_description_count_mismatch')
                       or path == ('index', 'lexical', 'kind')  # K5: character n-grams are the only protocol
                       or path == ('candidates', 'fusion')  # S1: only unweighted RRF is implemented until fitting
                       or path == ('assembly', 'fact_order')  # S3: need_then_support is the one implemented order
                       or path == ('vision', 'alpha_policy', 'mode')  # Refuse also removes the background member.
                       or path == ('vision', 'transport')  # T-008: protocol-bound, data_uri is the only value
                       or path[:2] == ('vlm', 'backends') and path[-1] == 'unsupported_param_policy'
                       # T-018: enum-valued embedding server keys (the schema's closed value sets)
                       or path[:2] == ('vlm', 'backends') and path[3:] in (('server', 'family'), ('server', 'dtype'),
                                                                          ('server', 'attn_implementation'))
                       or path[:2] == ('vlm', 'roles') and path[-1] == 'mode' and len(path) == 4
                       or path[:2] == ('vlm', 'roles') and path[-2:] == ('output', 'validate_response'))
            if not allowed:
                unexpected.append('.'.join(path))
    assert not unexpected


@pytest.mark.parametrize('path', PATHS, ids=lambda path: '.'.join(path))
def test_config_override_and_refusal(path, tmp_path):
    config = deepcopy(EXAMPLE)
    prepare(config, path)
    parent, key = node(config, path)
    original = parent[key]
    for candidate in options(original):
        if candidate == original and type(candidate) is type(original):
            continue
        parent, key = set_candidate(config, path, candidate)
        if VALIDATOR.is_valid(config):
            break
    else:
        pytest.skip('No alternative found: schema may constrain this field to a protocol constant')
    # Schema IDs must additionally name an existing file; use a known schema for these.
    if path[-1] == 'schema_id' and path[:2] == ('vlm', 'roles'):
        parent[key] = 'verifier_result.v0' if original == 'intent_ir.v0' else 'intent_ir.v0'
    if path == ('vlm', 'default_backend') or path[:2] == ('vlm', 'roles') and path[-1] == 'backend':
        parent[key] = 'alternate'
    if path == ('embedding', 'backend'):
        parent[key] = 'alternate'
    if path == ('embedding', 'chosen'):  # T-008: the chosen embedder is one of the listed candidates
        parent[key] = EXAMPLE['embedding']['candidates'][1]['id']
    if path == ('request_overrides', 'allowed_keys'):
        parent[key] = ['rag.enabled']  # S0: exact existing leaf paths, not arbitrary strings.
    if path == ('embedding', 'request', 'text_template'):  # T-008: exactly one {text} placeholder
        parent[key] = 'query: {text}'
    if path == ('vision', 'format') or path[:2] == ('vlm', 'roles') and path[-2:] == ('image', 'format'):
        parent[key] = 'png' if original != 'png' else 'jpeg'
    if path[-1] == 'api_key_file' and path[:2] == ('vlm', 'backends'):
        parent[key] = '.secrets/alternative.key'
    if path[-1] == 'addendum_layout' and parent[key] == 'separate_system_message':
        parent.pop('addendum_separator', None)  # T017-FIX: the separate layout refuses a separator
    if path[-1] == 'base_urls' and path[:2] == ('vlm', 'backends'):
        parent[key] = ['https://example.org/alternative']
    if path == ('vlm', 'backends', 'cloud_gemini', 'endpoints'):
        parent[key] = {'chat': 'v1beta/models/example:generateContent', 'health': 'health'}
    if path in {('storage', 'data_root'), ('storage', 'kb_store'), ('profiles', 'root')}:
        parent[key] = 'data/alternative'
    if path == ('knowledge', 'root'):  # never inside `data_root` (decision 175: a protected directory)
        parent[key] = 'alternative-kb'
    if path == ('knowledge', 'overrides_file'):
        parent[key] = 'alternative.yaml'
    if path == ('profiles', 'default_profile_id'):
        parent[key] = 'alternative-profile'
    if path[:2] == ('vlm', 'backends') and path[-1] in ('file', 'mmproj_file'):
        parent[key] = 'alternative.gguf'
    config_path = tmp_path / 'override.yaml'
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    try:
        loaded = load_config(config_path, 'kb status')
    except ConfigurationError as error:
        pytest.fail(f'Override {".".join(path)}: {error}')
    loaded_parent, loaded_key = node(loaded, path)
    assert loaded_parent[loaded_key] == parent[key]
    for invalid in [None, 0.123, False, [], {}, 'invalid-value']:
        parent[key] = invalid
        if not VALIDATOR.is_valid(config):
            break
    else:
        pytest.skip('No invalid candidate found for unrestricted mapping')
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(config_path, 'kb status')


MANIFEST = yaml.safe_load((ROOT / 'configs/profiles/_example/manifest.yaml').read_bytes())
MANIFEST_SCHEMA = json.loads((ROOT / 'contracts/schemas/profile_manifest.v0.schema.json').read_bytes())
MANIFEST_VALIDATOR = Draft202012Validator(MANIFEST_SCHEMA)


@pytest.mark.parametrize('path', list(fields(MANIFEST)), ids=lambda path: '.'.join(path))
def test_manifest_fields_override_and_refusal(path, tmp_path):
    manifest = deepcopy(MANIFEST)
    parent, key = node(manifest, path)
    original = parent[key]
    for candidate in options(original):
        if candidate == original and type(candidate) is type(original):
            continue
        parent[key] = candidate
        if MANIFEST_VALIDATOR.is_valid(manifest) and not (path == ('schema_version',) and candidate != 1):
            break
    else:
        pytest.skip('No supported alternative found for manifest field')
    manifest_path = tmp_path / 'manifest.yaml'
    manifest_path.write_text(yaml.safe_dump(manifest), encoding='utf-8')
    (tmp_path / 'system_prompt.txt').write_bytes(b'test prompt')
    loaded, _ = load_profile(tmp_path)
    loaded_parent, loaded_key = node(loaded, path)
    assert loaded_parent[loaded_key] == candidate
    for invalid in [None, 0.123, False, [], {}, '']:
        parent[key] = invalid
        if not MANIFEST_VALIDATOR.is_valid(manifest):
            break
    else:
        pytest.skip('No invalid candidate found for manifest field')
    manifest_path.write_text(yaml.safe_dump(manifest), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_profile(tmp_path)
