"""Observer configuration leaves: observable overrides and pre-activity refusals; no live backend."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from k2_fixtures import folder
from k2_observe_fixtures import ObserverServer, invoke_observer, states
from test_k2_config import IMAGE_OVERRIDES, ROLE_OVERRIDES, set_key
from prompt_enhancer.config import ConfigurationError, load_config, validate_observation_reads
from prompt_enhancer.k2_observe import observe_images


PREFIX = 'vlm.roles.observer.'
OVERRIDES = {**ROLE_OVERRIDES, **IMAGE_OVERRIDES}
SINGLETONS = {'observe.one_image_per_call': True, 'observe.lazy_observation_of_rest': True,
              'observe.region_output': 'optional', 'observe.facet_schema_version': 'image_observation.v0',
              PREFIX + 'mode': 'generate', PREFIX + 'image.source': 'knowledge_reference',
              PREFIX + 'image.transport': 'data_uri', PREFIX + 'output.transport': 'response_format',
              PREFIX + 'output.validate_response': True}
READ_KEYS = [*SINGLETONS, 'observe.representatives_per_folder', 'observe.near_duplicate_cosine_distance',
             *(PREFIX + field for field in OVERRIDES)]


def refused_stage(env, config):
    return asyncio.run(observe_images(env.store, config, {'folders': [], 'concepts': [], 'folder_concepts': []},
        images=[], folder_images=[], image_embeddings=[], embedder_revision='labelled-k1', dim=2,
        knowledge_root=env.source))


@pytest.mark.parametrize(('field', 'value'), OVERRIDES.items())
def test_observer_role_leaf_override(k2_env, field, value):
    env = k2_env
    folder(env, 'f', 'A')
    server = ObserverServer()
    first = invoke_observer(env, server)
    original = next(row for row in states(env) if row['state'] == 'done')['call_records'][0]
    if field == 'backend':
        env.config['vlm']['backends']['alternate'] = deepcopy(env.config['vlm']['backends']['main'])
        env.config['vlm']['backends']['alternate']['model']['id'] = 'alternate-model'
    if field == 'prompt_id':
        (env.root / 'prompts/alternate_prompt.txt').write_bytes((env.root / 'prompts/observer_v0.txt').read_bytes() + b'\nTest revision.\n')
    if field == 'output.schema_id':
        schema = json.loads((env.root / 'contracts/schemas/observer_reply.v0.schema.json').read_bytes())
        schema['$id'] = value
        (env.root / f'contracts/schemas/{value}.schema.json').write_text(json.dumps(schema), encoding='utf-8')
    set_key(env.config, PREFIX + field, value)
    result = invoke_observer(env, server)
    assert not result['report']['failed'] and len(server.requests) == 2
    assert result['rows']['observations'][0]['context_key'] != first['rows']['observations'][0]['context_key']
    record = max(states(env), key=lambda row: row['recorded_at'])['call_records'][0]
    body = server.requests[-1]
    if field == 'backend':
        assert record['backend'] == 'alternate' and body['model'] == 'alternate-model'
    elif field == 'prompt_id':
        assert record['prompt_id'] == value
    elif field == 'max_tokens':
        assert body['max_tokens'] == value
    elif field.startswith('sampling.'):
        name = field.split('.')[1]
        if name == 'repetition_penalty':
            assert ['PARAM_UNSUPPORTED', field] in record['warnings']
        else:
            assert body[name] == value
    elif field == 'reasoning.mode':
        assert body['chat_template_kwargs'] == {'enable_thinking': True}
    elif field == 'reasoning.effort':
        assert body['reasoning_effort'] == value
    elif field == 'reasoning.budget_tokens':
        assert ['PARAM_UNSUPPORTED', field] in record['warnings']
    elif field == 'reasoning.template_kwargs_key':
        assert body['chat_template_kwargs'] == {'test_thinking': False}
    elif field == 'output.format':
        assert body['response_format'] == {'type': 'json_object'}
    elif field == 'output.schema_id':
        assert record['schema_id'] == value
    elif field == 'output.strict':
        assert body['response_format']['json_schema']['strict'] is False
    else:
        assert record['request_hash'] != original['request_hash']


@pytest.mark.parametrize('key', READ_KEYS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'invalid'])
def test_each_observe_and_observer_leaf_refuses_before_activity(k2_env, key, failure, monkeypatch):
    config = deepcopy(k2_env.config)
    invalid = 'unsupported'
    if key.endswith('prompt_id'):
        invalid = '../unsafe'
    if key.endswith('template_kwargs_key'):
        invalid = 1
    set_key(config, key, 'design_required' if failure == 'sentinel' else invalid, remove=failure == 'missing')
    def activity(*args, **kwargs):
        pytest.fail('configuration refusal must precede source reads, writer and client binding')
    import prompt_enhancer.k2_observe as module
    monkeypatch.setattr(module, '_read_verified', activity)
    monkeypatch.setattr(k2_env.store, 'writer', activity)
    monkeypatch.setattr(module, 'ModelClientRun', activity)
    with pytest.raises(ConfigurationError):
        refused_stage(k2_env, config)


@pytest.mark.parametrize('key', READ_KEYS)
def test_loader_refuses_each_observe_observer_missing_and_sentinel(k2_env, key):
    for missing in (False, True):
        config = deepcopy(k2_env.config)
        set_key(config, key, 'design_required', remove=missing)
        path = k2_env.root / 'observe-invalid.yaml'
        path.write_text(yaml.safe_dump(config), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb sync', project_root=k2_env.root)


@pytest.mark.parametrize('key', list(SINGLETONS))
def test_observe_singleton_policy_is_explicit(k2_env, key):
    folder(k2_env, 'f', 'A')
    server = ObserverServer()
    assert invoke_observer(k2_env, server)['report']['observed'] == 1
    set_key(k2_env.config, key, False if SINGLETONS[key] is True else 'unknown')
    with pytest.raises(ConfigurationError):
        refused_stage(k2_env, k2_env.config)
    assert len(server.requests) == 1


@pytest.mark.parametrize('value', [0, -1, True, 1.0, '1'])
def test_observe_quota_refusal(k2_env, value):
    k2_env.config['observe']['representatives_per_folder'] = value
    with pytest.raises(ConfigurationError, match='representatives_per_folder'):
        validate_observation_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize('value', [-0.01, 2.01, True, '0.1', float('nan'), float('inf')])
def test_grouping_distance_refusal(k2_env, value):
    k2_env.config['observe']['near_duplicate_cosine_distance'] = value
    with pytest.raises(ConfigurationError, match='near_duplicate_cosine_distance'):
        validate_observation_reads(k2_env.config, project_root=k2_env.root)


def test_representative_quota_and_grouping_distance_overrides(k2_env):
    from k2_observe_fixtures import k1_result
    folder(k2_env, 'f', 'A', count=3)
    inputs = k1_result(k2_env, vectors=[[1, 0], [0, 1], [1, 0]])
    first = invoke_observer(k2_env, inputs=inputs)
    k2_env.config['observe']['representatives_per_folder'] = 2
    second = invoke_observer(k2_env, inputs=inputs)
    assert len(first['report']['representatives']['f']) == 1
    assert len(second['report']['representatives']['f']) == 2
    assert len({row['near_duplicate_group'] for row in second['rows']['images']}) == 2
    k2_env.config['observe']['near_duplicate_cosine_distance'] = 1.5
    third = invoke_observer(k2_env, inputs=inputs)
    assert len({row['near_duplicate_group'] for row in third['rows']['images']}) == 1


def test_new_distance_sentinel_is_explicit_in_full_example_profiles():
    root = Path(__file__).resolve().parents[1]
    for name in ('enhancer.example.yaml', 'enhancer.dev.example.yaml'):
        assert yaml.safe_load((root / 'configs' / name).read_bytes())['observe']['near_duplicate_cosine_distance'] == 'fit_from_gt'
    assert 'observe' not in yaml.safe_load((root / 'configs/overlays/deploy-vllm.example.yaml').read_bytes())


@pytest.mark.parametrize('value', [0.25, 'fit_from_gt', 2.5])
@pytest.mark.parametrize('entry', ['explicit', 'overlay_of'])
def test_deployment_overlay_refuses_observe_fitted_values(k2_env, value, entry):
    env = k2_env
    base = env.root / 'overlay-base.yaml'
    base.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    overlay = env.root / 'observe-overlay.yaml'
    body = {'schema_version': 1, 'overlay_of': base.name,
            'observe': {'near_duplicate_cosine_distance': value}}
    overlay.write_text(yaml.safe_dump(body), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='observe: not a deployment overlay key'):
        if entry == 'explicit':
            load_config(base, 'kb status', overlay=overlay, project_root=env.root)
        else:
            load_config(overlay, 'kb status', project_root=env.root)


@pytest.mark.parametrize('observe', [{}, {'representatives_per_folder': 2}, {'one_image_per_call': False}, 'invalid'])
def test_deployment_overlay_refuses_other_observe_blocks(k2_env, observe):
    base = k2_env.root / 'overlay-base.yaml'
    base.write_text(yaml.safe_dump(k2_env.config), encoding='utf-8')
    overlay = k2_env.root / 'observe-overlay.yaml'
    overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': base.name, 'observe': observe}), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='observe: not a deployment overlay key'):
        load_config(overlay, 'kb status', project_root=k2_env.root)
