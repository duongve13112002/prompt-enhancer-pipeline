"""Every P1 setting: observed non-default behavior, or the PM-approved singleton exception, and refusals."""

from copy import deepcopy
import json

import pytest
import yaml

from prompt_enhancer.config import COMMAND_READS, ConfigurationError, load_config, validate_naming_reads
from k2_fixtures import NamingServer, assessment, folder, image_bytes, invoke, naming


def set_key(config, key, value, *, remove=False):
    parts = key.split('.')
    parent = config
    for part in parts[:-1]:
        parent = parent[part]
    if remove:
        del parent[parts[-1]]
    else:
        parent[parts[-1]] = value


ROLE_OVERRIDES = {
    'backend': 'alternate', 'prompt_id': 'alternate_prompt', 'max_tokens': 257,
    'sampling.temperature': 0.65, 'sampling.top_p': 0.75, 'sampling.top_k': 7, 'sampling.min_p': 0.1,
    'sampling.frequency_penalty': 0.1, 'sampling.presence_penalty': 0.2, 'sampling.repetition_penalty': 1.2,
    'sampling.seed': 42, 'sampling.stop': ['END'], 'reasoning.mode': 'on', 'reasoning.effort': 'low',
    'reasoning.budget_tokens': 20, 'reasoning.template_kwargs_key': 'test_thinking',
    'output.format': 'json_object', 'output.schema_id': 'alternate_schema', 'output.strict': False,
}
IMAGE_OVERRIDES = {'image.max_side': 7, 'image.format': 'png', 'image.quality': 30}
SINGLETONS = {
    'knowledge.naming.on_no_info_file': 'model_names_from_images',
    'knowledge.naming.on_unusable_info_file': 'model_names_from_images',
    'knowledge.naming.selection': 'random', 'knowledge.naming.on_no_images': 'skip',
    **{f'vlm.roles.{role}.{field}': value for role in ('info_assessor', 'folder_namer')
       for field, value in {'mode': 'generate', 'output.transport': 'response_format',
                            'output.validate_response': True}.items()},
    'vlm.roles.info_assessor.image': 'none',
    'vlm.roles.folder_namer.image.source': 'knowledge_reference',
    'vlm.roles.folder_namer.image.transport': 'data_uri',
}
NAMING_OVERRIDES = {'knowledge.naming.assess_info_file': False, 'knowledge.naming.sample_size': 1,
                    'knowledge.naming.rename_on_change': 0.25, 'knowledge.naming.report': False,
                    'knowledge.overrides_file': 'alternate.yaml'}
ROLE_CASES = [(role, field, value) for role in ('info_assessor', 'folder_namer') for field, value in ROLE_OVERRIDES.items()]
ROLE_CASES += [('folder_namer', field, value) for field, value in IMAGE_OVERRIDES.items()]
READ_KEYS = list(SINGLETONS) + list(NAMING_OVERRIDES) + [f'vlm.roles.{role}.{field}' for role, field, _ in ROLE_CASES]


def latest_record(env, role):
    stage = 'k2_assessment' if role == 'info_assessor' else 'k2_naming'
    with env.store.writer() as writer:
        records = [row for row in writer._state_rows().values() if row['stage'] == stage and row['call_records']]
    return max(records, key=lambda row: row['recorded_at'])['call_records'][-1]


@pytest.mark.parametrize(('role', 'field', 'value'), ROLE_CASES, ids=lambda value: str(value))
def test_role_leaf_override_changes_request_or_records_supported_warning(k2_env, role, field, value):
    env = k2_env
    folder(env, 'declared', 'Declared\nHuman text')
    folder(env, 'pending', count=3)
    server = NamingServer()
    invoke(env, server)
    previous = latest_record(env, role)
    role_config = env.config['vlm']['roles'][role]
    if field == 'backend':
        env.config['vlm']['backends']['alternate'] = deepcopy(env.config['vlm']['backends']['main'])
        env.config['vlm']['backends']['alternate']['model']['id'] = 'alternate-test-model'
    if field == 'prompt_id':
        source = env.root / 'prompts' / (role_config['prompt_id'] + '.txt')
        (env.root / 'prompts' / 'alternate_prompt.txt').write_bytes(source.read_bytes() + b'\nTest-only revision.\n')
    if field == 'output.schema_id':
        source = env.root / 'contracts' / 'schemas' / (role_config['output']['schema_id'] + '.schema.json')
        schema = json.loads(source.read_bytes())
        schema['$id'] = 'alternate_schema'
        (source.parent / 'alternate_schema.schema.json').write_text(json.dumps(schema), encoding='utf-8')
    set_key(env.config, f'vlm.roles.{role}.{field}', value)
    result = invoke(env, server)
    assert not result['report']['failed']
    record = latest_record(env, role)
    assert record['request_hash'] != previous['request_hash']
    body = next(body for body in reversed(server.requests) if
                isinstance(body['messages'][1]['content'], list) == (role == 'folder_namer'))
    if field == 'backend':
        assert body['model'] == 'alternate-test-model'
    elif field == 'prompt_id':
        assert body['messages'][0]['content'].endswith('Test-only revision.\n')
    elif field == 'max_tokens':
        assert body['max_tokens'] == value
    elif field.startswith('sampling.'):
        name = field.split('.')[1]
        if name == 'repetition_penalty':
            assert ['PARAM_UNSUPPORTED', field] in record['warnings']
        else:
            assert body[name] == value
    elif field == 'reasoning.mode':
        assert body['chat_template_kwargs']['enable_thinking'] is True
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
        assert record['effective_body_hash'] != previous['effective_body_hash']


@pytest.mark.parametrize(('key', 'value'), NAMING_OVERRIDES.items())
def test_naming_override_has_observable_effect(k2_env, key, value):
    env = k2_env
    folder(env, 'declared', 'Declared\nHuman text')
    folder(env, 'pending', count=3)
    if key == 'knowledge.naming.rename_on_change':
        server = NamingServer()
        invoke(env, server)
        (env.source / 'pending' / 'extra.png').write_bytes(image_bytes((240, 50, 90)))
        invoke(env, server)
        assert len(server.requests) == 2  # 1/3 is below the original 0.5 threshold.
        set_key(env.config, key, value)
        changed = invoke(env, server)
        assert len(server.requests) == 3
        assert changed['report']['redraws'][-1]['reason'] == 'image_set_change'
        return
    if key == 'knowledge.overrides_file':
        (env.source / value).write_text('Generated: {concept_type: object}', encoding='utf-8')
    set_key(env.config, key, value)
    server = NamingServer()
    result = invoke(env, server)
    if key.endswith('assess_info_file'):
        assert len(server.requests) == 1
    elif key.endswith('sample_size'):
        assert len(result['report']['model_named_folders'][0]['sampled_image_ids']) == value
    elif key.endswith('report'):
        assert not result['report']['model_named_folders'] and len(server.requests) == 2
    elif key.endswith('overrides_file'):
        assert next(row for row in result['rows']['concepts'] if row['display_name'] == 'Generated')['kind'] == 'object'


@pytest.mark.parametrize('key', list(SINGLETONS))
def test_supported_singleton_behavior_is_explicit(k2_env, key):
    configured = k2_env.config
    for part in key.split('.'):
        configured = configured[part]
    assert configured == SINGLETONS[key]
    set_key(k2_env.config, key, SINGLETONS[key])
    folder(k2_env, 'declared', 'Declared\nHuman text')
    folder(k2_env, 'pending', count=1)
    folder(k2_env, 'empty', count=0)
    folder(k2_env, 'unclear', 'Old\nHuman text')
    server = NamingServer({('info_assessor', 'unclear'): assessment(False),
                           ('folder_namer', 'unclear'): naming(['New'], info_fits=False)})
    result = invoke(k2_env, server)
    assert not result['report']['failed'] and result['report']['skipped_folders'] == ['empty']
    assert len(server.requests) == 4
    set_key(k2_env.config, key, False if SINGLETONS[key] is True else 'unsupported')
    before = len(server.requests)
    with pytest.raises(ConfigurationError):
        invoke(k2_env, server)
    assert len(server.requests) == before


@pytest.mark.parametrize('key', READ_KEYS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'unknown'])
def test_each_read_leaf_refuses_before_any_model_call(k2_env, key, failure):
    invalid = 'unknown'
    if key.endswith('prompt_id'):
        invalid = '../unknown'
    elif key == 'knowledge.overrides_file':
        invalid = '../unknown.yaml'
    elif key.endswith('reasoning.template_kwargs_key'):
        invalid = 1  # arbitrary string switch names are valid; a non-string is outside the declared domain
    set_key(k2_env.config, key, 'design_required' if failure == 'sentinel' else invalid,
             remove=failure == 'missing')
    server = NamingServer()
    empty = {'handoff': {'k2': {'folders': [], 'concepts': [], 'folder_concepts': []}},
             'rows': {'images': [], 'folder_images': []}}
    with pytest.raises(ConfigurationError):
        invoke(k2_env, server, result=empty)
    assert not server.requests


@pytest.mark.parametrize('key', READ_KEYS)
def test_public_loader_also_refuses_each_missing_or_sentinel_leaf(k2_env, key):
    env = k2_env
    for remove in (False, True):
        config = deepcopy(env.config)
        set_key(config, key, 'design_required', remove=remove)
        path = env.root / 'k2-test-profile.yaml'
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb sync', project_root=env.root)


@pytest.mark.parametrize('value', [0, -1, True, 1.0, '1'])
def test_zero_and_noninteger_sample_size_refused(k2_env, value):
    k2_env.config['knowledge']['naming']['sample_size'] = value
    with pytest.raises(ConfigurationError, match='sample_size'):
        validate_naming_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True, '0.5'])
def test_invalid_rename_share_refused(k2_env, value):
    k2_env.config['knowledge']['naming']['rename_on_change'] = value
    with pytest.raises(ConfigurationError, match='rename_on_change'):
        validate_naming_reads(k2_env.config, project_root=k2_env.root)


@pytest.mark.parametrize('raw', ['Unknown: {rename: Other}', 'Name: {facts: [1]}', 'Name: &a {facts: [*a]}'])
def test_malformed_existing_overrides_refuse_before_model_calls(k2_env, raw):
    folder(k2_env, 'pending')
    (k2_env.source / 'kb_overrides.yaml').write_text(raw, encoding='utf-8')
    server = NamingServer()
    with pytest.raises(ConfigurationError):
        invoke(k2_env, server)
    assert not server.requests


def test_overrides_none_disables_file_and_missing_file_is_absent(k2_env):
    folder(k2_env, 'pending')
    assert invoke(k2_env)['overrides']['file_hash'] is None
    (k2_env.source / 'kb_overrides.yaml').write_text('not a mapping', encoding='utf-8')
    k2_env.config['knowledge']['overrides_file'] = 'none'
    assert invoke(k2_env)['overrides'] == {'file_hash': None, 'data': {}}


def test_kb_sync_read_set_is_not_narrowed():
    for key in ('knowledge', 'sync', 'embedding', 'observe', 'cards', 'aliases', 'index', 'vision',
                'vlm.roles.observer', 'vlm.roles.info_assessor', 'vlm.roles.folder_namer',
                'vlm.roles.card_builder', 'vlm.roles.alias_generator'):
        assert key in COMMAND_READS['kb sync']
