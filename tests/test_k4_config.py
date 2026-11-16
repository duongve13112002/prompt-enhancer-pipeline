"""Each K4 configuration leaf has an observed override or an explicit safety/protocol refusal."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from k4_fixtures import AliasServer, inputs, invoke_aliases, states
from test_k2_config import ROLE_OVERRIDES, set_key
from test_k2_observe_shared_config import B, OVERRIDES as SHARED
from prompt_enhancer.config import ConfigurationError, load_config, validate_alias_reads
from prompt_enhancer.k4_aliases import build_aliases
from storage_fixtures import make_store

PREFIX = 'vlm.roles.alias_generator.'
ALIAS_KEYS = ['aliases.' + field for field in ('generated', 'collision_policy', 'review_queue', 'only_add_candidates', 'languages')]
ROLE_KEYS = [PREFIX + field for field in [*ROLE_OVERRIDES, 'mode', 'image', 'output.transport', 'output.validate_response']]
SHARED_OVERRIDES = {key: value for key, value in SHARED.items() if not key.startswith('vision.')}
SHARED_KEYS = [*SHARED_OVERRIDES, B + 'provider', B + 'api_key_file', B + 'unsupported_param_policy',
               'vlm.default_backend', 'storage.kb_store', 'storage.data_root', 'storage.lock.timeout_s',
               'storage.lock.poll_s', 'storage.table_format', 'sync.resume', 'knowledge.root']


@pytest.mark.parametrize('key', ALIAS_KEYS + ROLE_KEYS + SHARED_KEYS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'wrong_type'])
def test_each_alias_read_leaf_refuses_before_activity(k2_env, monkeypatch, key, failure):
    import prompt_enhancer.k4_aliases as module
    changed = deepcopy(k2_env.config)
    set_key(changed, key, 'design_required' if failure == 'sentinel' else {'invalid': True}, remove=failure == 'missing')
    monkeypatch.setattr(k2_env.store, 'writer', lambda: pytest.fail('writer opened'))
    monkeypatch.setattr(module, 'ModelClientRun', lambda *a, **k: pytest.fail('client created'))
    with pytest.raises(ConfigurationError):
        asyncio.run(build_aliases(k2_env.store, changed, **inputs()))
    assert not k2_env.store.root.exists()


@pytest.mark.parametrize('key', ALIAS_KEYS + ROLE_KEYS)
def test_loader_alias_refusals(k2_env, key):
    for missing in (False, True):
        changed = deepcopy(k2_env.config)
        set_key(changed, key, 'design_required', remove=missing)
        path = k2_env.root / 'alias-test-profile.yaml'
        path.write_text(yaml.safe_dump(changed), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb sync', project_root=k2_env.root)


@pytest.mark.parametrize(('key', 'value'), [
    ('aliases.collision_policy', 'accept'), ('aliases.review_queue', False), ('aliases.only_add_candidates', False),
    ('aliases.languages', []), ('aliases.languages', ['']), ('aliases.languages', ['design_required']),
    (PREFIX + 'image', {'source': 'knowledge_reference'}), (PREFIX + 'mode', 'embed'),
    (PREFIX + 'output.transport', 'engine_native'), (PREFIX + 'output.validate_response', False),
    (PREFIX + 'prompt_id', '../unsafe'), (PREFIX + 'output.schema_id', 'alias_set.v0'),
    (PREFIX + 'backend', 'missing'), (PREFIX + 'max_tokens', True), (PREFIX + 'max_tokens', 1.5),
    (PREFIX + 'sampling.temperature', float('nan')), (PREFIX + 'sampling.top_p', float('inf')),
])
def test_invalid_alias_policies_refused(k2_env, key, value):
    set_key(k2_env.config, key, value)
    with pytest.raises(ConfigurationError):
        validate_alias_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize(('field', 'value'), ROLE_OVERRIDES.items())
def test_alias_role_override_observed(k2_env, field, value):
    data, server = inputs(), AliasServer()
    invoke_aliases(k2_env, data, server)
    original = states(k2_env)[0]['call_records'][0]
    if field == 'backend':
        k2_env.config['vlm']['backends']['alternate'] = deepcopy(k2_env.config['vlm']['backends']['main'])
        k2_env.config['vlm']['backends']['alternate']['model']['id'] = 'alternate-alias-model'
    elif field == 'prompt_id':
        (k2_env.root / 'prompts/alternate_prompt.txt').write_bytes(
            (k2_env.root / 'prompts/alias_generator_v0.txt').read_bytes() + b'\nTest-only revision.\n')
    elif field == 'output.schema_id':
        schema = json.loads((k2_env.root / 'contracts/schemas/alias_generator_reply.v0.schema.json').read_bytes())
        schema['$id'] = value
        (k2_env.root / f'contracts/schemas/{value}.schema.json').write_text(json.dumps(schema), encoding='utf-8')
    set_key(k2_env.config, PREFIX + field, value)
    result = invoke_aliases(k2_env, data, server)
    assert not result['report']['failed'] and len(server.requests) == 2
    record = max(states(k2_env), key=lambda row: row['recorded_at'])['call_records'][0]
    assert record['request_hash'] != original['request_hash']
    body = server.requests[-1]
    if field in ('backend', 'prompt_id'):
        assert record[field] == value
    elif field == 'max_tokens':
        assert body['max_tokens'] == value
    elif field.startswith('sampling.'):
        if field == 'sampling.repetition_penalty':
            assert ['PARAM_UNSUPPORTED', field] in record['warnings']
        else:
            assert body[field.split('.')[1]] == value
    elif field == 'reasoning.mode':
        assert body['chat_template_kwargs']['enable_thinking'] is True
    elif field == 'reasoning.effort':
        assert body['reasoning_effort'] == value
    elif field == 'reasoning.budget_tokens':
        assert ['PARAM_UNSUPPORTED', field] in record['warnings']
    elif field == 'reasoning.template_kwargs_key':
        assert body['chat_template_kwargs'] == {'test_thinking': False}
    elif field == 'output.format':
        assert body['response_format']['type'] == 'json_object'
    elif field == 'output.schema_id':
        assert record['schema_id'] == value
    elif field == 'output.strict':
        assert body['response_format']['json_schema']['strict'] is False


@pytest.mark.parametrize(('key', 'value'), SHARED_OVERRIDES.items())
def test_alias_backend_override_changes_only_semantic_cache_record(k2_env, key, value):
    data, server = inputs(), AliasServer()
    before = invoke_aliases(k2_env, data, server)
    set_key(k2_env.config, key, value)
    if key == B + 'http.backoff_initial_s':
        set_key(k2_env.config, B + 'http.backoff_max_s', 0.5)
    after = invoke_aliases(k2_env, data, server)
    semantic = key.startswith((B + 'engine.', B + 'model.'))
    assert not after['report']['failed'] and len(server.requests) == (2 if semantic else 1)
    assert (before['artifacts'] != after['artifacts']) == semantic


def test_alias_languages_override_and_default_backend(k2_env):
    data, server = inputs(), AliasServer()
    invoke_aliases(k2_env, data, server)
    k2_env.config['aliases']['languages'] = ['test-A', 'test-B']
    result = invoke_aliases(k2_env, data, server)
    assert result['report']['generated'] == 1
    assert json.loads(server.requests[-1]['messages'][1]['content'])['languages'] == ['test-A', 'test-B']
    k2_env.config['vlm']['backends']['alternate'] = deepcopy(k2_env.config['vlm']['backends']['main'])
    k2_env.config['vlm']['default_backend'] = 'alternate'
    changed = invoke_aliases(k2_env, data, server)
    assert changed['artifacts'] == result['artifacts'] and len(server.requests) == 2


def test_loader_alias_preflight_success_then_unsupported_policy(k2_env):
    path = k2_env.root / 'alias-loader.yaml'
    path.write_text(yaml.safe_dump(k2_env.config), encoding='utf-8')
    loaded = load_config(path, 'kb sync', project_root=k2_env.root)
    assert loaded['aliases']['languages'] == ['test-language']
    k2_env.config['aliases']['review_queue'] = False
    path.write_text(yaml.safe_dump(k2_env.config), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='review_queue'):
        load_config(path, 'kb sync', project_root=k2_env.root)


def test_alias_store_path_and_lock_overrides_observed(k2_env):
    config = k2_env.config
    config['knowledge']['root'] += '-unused'
    config['storage']['kb_store'] += '-alternate'
    config['storage']['data_root'] += '-alternate'
    config['storage']['lock'] = {'timeout_s': 6, 'poll_s': 0.02}
    k2_env.store = make_store(k2_env.root, k2_env.root / config['storage']['kb_store'], timeout_s=6, poll_s=0.02)
    result = invoke_aliases(k2_env, inputs())
    assert not result['report']['failed'] and k2_env.store.root.exists()
    assert k2_env.store.timeout_s == 6 and k2_env.store.poll_s == 0.02
    assert not (k2_env.root / config['knowledge']['root']).exists()


def test_dev_alias_languages_resolve_and_base_sync_refuses(k2_env):
    repo = Path(__file__).resolve().parents[1]
    profiles = k2_env.root / 'configs'
    profiles.mkdir(exist_ok=True)
    for name in ('enhancer.example.yaml', 'enhancer.dev.example.yaml'):
        (profiles / name).write_bytes((repo / 'configs' / name).read_bytes())
    assert yaml.safe_load((profiles / 'enhancer.example.yaml').read_bytes())['aliases']['languages'] == 'design_required'
    # Resolve unrelated settings with labelled synthetic values; languages inherit the actual example lineage.
    resolved = deepcopy(k2_env.config)
    del resolved['aliases']
    for profile, parent in (('dev', 'enhancer.dev.example.yaml'), ('base', 'enhancer.example.yaml')):
        path = profiles / f'alias-{profile}-test.yaml'
        path.write_text(yaml.safe_dump(dict(resolved, extends=parent)), encoding='utf-8')
        if profile == 'base':
            with pytest.raises(ConfigurationError, match='aliases.languages: unresolved sentinel design_required'):
                load_config(path, 'kb sync', project_root=k2_env.root)
        else:
            dev = load_config(path, 'kb sync', project_root=k2_env.root)
    assert dev['aliases']['languages'] == ['vi', 'en', 'ja', 'zh']
    k2_env.config = dev
    server = AliasServer()
    result = invoke_aliases(k2_env, inputs(), server)
    assert not result['report']['failed']
    assert json.loads(server.requests[0]['messages'][1]['content'])['languages'] == ['vi', 'en', 'ja', 'zh']
