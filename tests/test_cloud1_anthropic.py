"""CLOUD-1: Anthropic structured output, Haiku 5.5 request rules, unseeded retries, budget `stop`.

Labelled CPU tests: in-memory HTTP doubles and `httpx.MockTransport`, synthetic replies, a fake key file under
`tmp_path`. No network call and no real key.
"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

from hypothesis import given, settings, strategies as st
from jsonschema import Draft202012Validator
import httpx
import pytest
import yaml

from prompt_enhancer.config import ConfigurationError, attempt_seed_refusal, load_config
from prompt_enhancer.model_cloud import (ANTHROPIC_UNSUPPORTED_KEYWORDS, anthropic_output_schema, cloud_headers,
                                         cloud_request)
from prompt_enhancer.model_client import ModelCallError, RoleClient, _digest
from prompt_enhancer.model_mapping import (ANTHROPIC_MODEL_REQUEST_RULES, anthropic_reasoning_refusal,
                                           map_cloud_reasoning, provider_maps_seed, seed_field_note)
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_resources import BackendConcurrency, SingleRunBudgetLedger
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.model_transport import ModelTransport
from prompt_enhancer.reply_retry import Attempts, call_until_valid

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / 'contracts' / 'schemas'
KEY = 'synthetic-key-for-cloud1'
SEED_WARNING = ('PARAM_UNSUPPORTED', 'sampling.seed')

FULL = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'cloud1_test.v0', 'type': 'object', 'additionalProperties': False, 'required': ['n', 's'],
        'properties': {'n': {'type': 'integer', 'minimum': 1, 'maximum': 3},
                       's': {'type': 'string', 'minLength': 2},
                       'xs': {'type': 'array', 'items': {'type': 'integer', 'maximum': 9}, 'minItems': 1,
                              'maxItems': 2, 'uniqueItems': True}}}


# -- (A) the stripped copy ------------------------------------------------------------------------------------------
def test_strip_removes_only_unsupported_keywords_and_never_mutates_the_input():
    before = deepcopy(FULL)
    sent = anthropic_output_schema(FULL)
    assert FULL == before
    assert sent['properties']['n'] == {'type': 'integer'} and sent['properties']['s'] == {'type': 'string'}
    assert sent['properties']['xs'] == {'type': 'array', 'items': {'type': 'integer'}}
    assert sent['required'] == ['n', 's'] and sent['additionalProperties'] is False and sent['$id'] == 'cloud1_test.v0'
    assert sent['$schema'] == FULL['$schema']


def test_a_property_named_like_a_keyword_is_kept():
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['minimum'],
              'properties': {'minimum': {'type': 'integer', 'maximum': 4}, 'maxItems': {'type': 'string'}}}
    assert anthropic_output_schema(schema)['properties'] == {'minimum': {'type': 'integer'},
                                                            'maxItems': {'type': 'string'}}


@pytest.mark.parametrize('schema', [
    {'type': 'object', 'properties': {}},                                                    # no additionalProperties
    {'type': 'object', 'properties': {}, 'additionalProperties': True},
    {'type': 'object', 'properties': {}, 'additionalProperties': {'type': 'string'}},
    {'type': 'object', 'additionalProperties': False, 'properties': {
        'a': {'type': 'object', 'properties': {}}}},                                         # a nested one
    {'type': 'object', 'additionalProperties': False, 'properties': {          # a node with no object type
        'a': {'additionalProperties': {'type': 'string'}}}},
    {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'additionalProperties': True}}},
    {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'not': {'maximum': 3}}}},
    {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'oneOf': [{'type': 'null'}]}}},
    {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'if': {'minimum': 1}, 'then': {}}}},
    {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'$ref': 'other.v0'}}},
    {'type': 'object', 'additionalProperties': False, 'properties': {'a': {'$ref': '#/$defs/loop'}},
     '$defs': {'loop': {'type': 'object', 'additionalProperties': False,
                        'properties': {'b': {'$ref': '#/$defs/loop'}}}}},
])
def test_a_schema_anthropic_cannot_take_is_refused_not_changed(schema):
    with pytest.raises(ValueError):
        anthropic_output_schema(schema)


def test_comment_and_min_properties_are_removed_at_every_level():
    # T017-HAIKU keyword matrix: Haiku 5.5 answered 400 for both on an object node.
    schema = {'$comment': 'root note', 'type': 'object', 'additionalProperties': False, 'minProperties': 1,
              'required': ['a'], 'properties': {'a': {'$comment': 'leaf note', 'type': 'string'},
                                                '$comment': {'type': 'string'}}}
    sent = anthropic_output_schema(schema)
    assert sent == {'type': 'object', 'additionalProperties': False, 'required': ['a'],
                    'properties': {'a': {'type': 'string'}, '$comment': {'type': 'string'}}}


def schema_keys(node):
    """Every key at schema level, walking all subschema positions (property names are not keywords)."""
    if isinstance(node, list):
        return set().union(*map(schema_keys, node)) if node else set()
    if not isinstance(node, dict):
        return set()
    found = set(node)
    for key, value in node.items():
        if key in ('properties', '$defs', 'definitions'):
            found |= set().union(*map(schema_keys, value.values())) if value else set()
        elif key in ('items', 'anyOf', 'allOf'):
            found |= schema_keys(value)
    return found


# Independent of the module's constant, so dropping a keyword from it fails here: the documented constraints and the two
# keywords Haiku 5.5 refused on an object node in the T017-HAIKU keyword matrix.
REJECTED_BY_ANTHROPIC = frozenset((
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength',
    'maxItems', 'uniqueItems', 'minProperties', '$comment'))


@pytest.mark.parametrize('path', sorted(SCHEMAS.glob('*.schema.json')), ids=lambda p: p.name)
def test_every_contract_schema_anthropic_takes_is_sent_without_a_rejected_keyword(path):
    schema = json.loads(path.read_text(encoding='utf-8'))
    try:
        sent = anthropic_output_schema(schema)
    except ValueError:
        return  # refused at start-up instead (config.py), never sent
    assert not schema_keys(sent) & REJECTED_BY_ANTHROPIC


def test_the_contract_schema_sweep_is_not_vacuous():
    # The schema of the first live Haiku 400 (T017-HAIKU) must be accepted, so the sweep above really asserts.
    accepted = []
    for path in sorted(SCHEMAS.glob('*.schema.json')):
        try:
            anthropic_output_schema(json.loads(path.read_text(encoding='utf-8')))
        except ValueError:
            continue
        accepted.append(path.name)
    assert 'info_assessment.v0.schema.json' in accepted


def test_a_local_non_recursive_ref_is_kept():
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['a'], 'properties': {
        'a': {'$ref': '#/$defs/n'}}, '$defs': {'n': {'type': 'integer', 'maximum': 3}}}
    sent = anthropic_output_schema(schema)
    assert sent['properties']['a'] == {'$ref': '#/$defs/n'} and sent['$defs']['n'] == {'type': 'integer'}


@st.composite
def schema_with_values(draw, depth=0):
    """(schema, values strategy): a schema built only from supported applicators, with unsupported constraints."""
    kinds = ['integer', 'string', 'array', 'object', 'anyOf'] if depth < 3 else ['integer', 'string']
    kind = draw(st.sampled_from(kinds))
    if kind == 'integer':
        low = draw(st.integers(-5, 5))
        schema = {'type': 'integer'}
        if draw(st.booleans()):
            schema['minimum'] = low
        if draw(st.booleans()):
            schema['maximum'] = low + draw(st.integers(0, 8))
        if draw(st.booleans()):
            schema['exclusiveMinimum'] = low - 1
        return schema, st.integers(-14, 14)
    if kind == 'string':
        schema = {'type': 'string'}
        if draw(st.booleans()):
            schema['minLength'] = draw(st.integers(0, 3))
        if draw(st.booleans()):
            schema['maxLength'] = draw(st.integers(0, 6))
        return schema, st.text(alphabet='abc', max_size=7)
    if kind == 'array':
        item, values = draw(schema_with_values(depth + 1))
        schema = {'type': 'array', 'items': item}
        for key, value in (('minItems', draw(st.integers(0, 2))), ('maxItems', draw(st.integers(0, 4))),
                           ('uniqueItems', True)):
            if draw(st.booleans()):
                schema[key] = value
        return schema, st.lists(values, max_size=5)
    if kind == 'anyOf':
        (first, first_values), (second, second_values) = draw(schema_with_values(depth + 1)), draw(
            schema_with_values(depth + 1))
        return {'anyOf': [first, second]}, st.one_of(first_values, second_values)
    names = draw(st.lists(st.sampled_from(['a', 'b', 'minimum', 'maxItems']), min_size=1, max_size=3, unique=True))
    properties, values = {}, {}
    for name in names:
        properties[name], values[name] = draw(schema_with_values(depth + 1))
    required = [name for name in names if draw(st.booleans())]
    schema = {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}
    return schema, st.fixed_dictionaries({n: values[n] for n in required}, optional={
        n: values[n] for n in names if n not in required})


def removed_only(sent, full):
    """`sent` is `full` with keys removed and nothing else changed or added."""
    if isinstance(full, dict):
        return isinstance(sent, dict) and all(key in full and removed_only(value, full[key])
                                              for key, value in sent.items())
    if isinstance(full, list):
        return isinstance(sent, list) and len(sent) == len(full) and all(map(removed_only, sent, full))
    return sent == full


def keywords_left(schema):
    """Unsupported keywords still at schema level (property names are not keywords)."""
    found = ANTHROPIC_UNSUPPORTED_KEYWORDS & set(schema)
    for key, value in schema.items():
        if key == 'properties':
            for sub_schema in value.values():
                found |= keywords_left(sub_schema)
        elif key == 'items':
            found |= keywords_left(value)
        elif key in ('anyOf', 'allOf'):
            for sub_schema in value:
                found |= keywords_left(sub_schema)
    return found


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_property_stripped_copy_accepts_every_instance_the_full_schema_accepts(data):
    schema, values = data.draw(schema_with_values())
    wrapped = {'type': 'object', 'additionalProperties': False, 'required': ['x'], 'properties': {'x': schema}}
    sent = anthropic_output_schema(wrapped)
    assert removed_only(sent, wrapped) and not keywords_left(sent)
    full_check, sent_check = Draft202012Validator(wrapped), Draft202012Validator(sent)
    for _ in range(3):
        instance = {'x': data.draw(values)}
        assert not full_check.is_valid(instance) or sent_check.is_valid(instance)


@settings(max_examples=100, deadline=None)
@given(st.data())
def test_property_an_object_without_additional_properties_false_is_always_refused(data):
    schema, _ = data.draw(schema_with_values())
    wrapped = {'type': 'object', 'required': ['x'], 'properties': {'x': schema}}  # the root lacks the key
    with pytest.raises(ValueError, match='additionalProperties'):
        anthropic_output_schema(wrapped)


def test_stripping_can_loosen_and_the_client_side_check_still_restores_every_bound():
    sent = anthropic_output_schema(FULL)
    broken = {'n': 9, 's': 'a', 'xs': [1, 1, 1]}  # breaks maximum, minLength, maxItems, uniqueItems
    assert Draft202012Validator(sent).is_valid(broken) and not Draft202012Validator(FULL).is_valid(broken)


# -- (A) the body ---------------------------------------------------------------------------------------------------
def role(**changes):
    value = {'backend': 'cloud', 'prompt_id': 'p', 'max_tokens': 100, 'image': 'none',
             'sampling': {'temperature': 'none', 'top_p': 'none', 'top_k': 'none', 'seed': 'none', 'stop': 'none'},
             'reasoning': {'mode': 'auto', 'effort': 'medium', 'budget_tokens': 'none'},
             'output': {'format': 'json_schema', 'schema_id': 'cloud1_test.v0', 'strict': True,
                        'validate_response': True}}
    value.update(changes)
    return value


BACKEND = {'provider': 'anthropic', 'engine': {'name': 'anthropic'}, 'api_key_file': '.secrets/fake.key',
           'anthropic_version': '2025-01-01', 'anthropic_workspace_id': 'none', 'allow_images': False,
           'model': {'id': 'claude-haiku-5-5', 'revision': 'none'}, 'endpoints': {'chat': 'messages'},
           'on_budget_exhausted': 'stop', 'image': {'max_decode_pixels': 1000, 'max_side': 'none'}}


def test_body_carries_format_merged_with_effort_and_keeps_the_prompts_byte_for_byte():
    admin, user = 'ADMIN system  text\n', 'user  prompté "quoted"'
    body, warnings = cloud_request('anthropic', BACKEND, role(), admin, user, schema=FULL)
    assert body['output_config'] == {'effort': 'medium', 'format': {'type': 'json_schema',
                                                                    'schema': anthropic_output_schema(FULL)}}
    assert body['system'] == admin and body['messages'] == [{'role': 'user', 'content': user}]  # R-004, R-001
    assert warnings == []
    plain, _ = cloud_request('anthropic', BACKEND, role(output={
        'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}), admin, user)
    assert 'format' not in plain['output_config'] and plain['system'] == body['system']
    with_addendum, _ = cloud_request('anthropic', BACKEND, role(), admin, user, schema=FULL, addendum='ADD')
    assert with_addendum['system'] == [{'type': 'text', 'text': admin}, {'type': 'text', 'text': 'ADD'}]


def test_the_format_is_added_without_an_effort_and_never_overwrites_one():
    quiet = role(reasoning={'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'})
    body, _ = cloud_request('anthropic', BACKEND, quiet, 's', 'u', schema=FULL)
    assert set(body['output_config']) == {'format'}


def test_extra_body_cannot_replace_output_config_of_a_structured_request():
    backend = dict(BACKEND, extra_body={'output_config': {'effort': 'low'}})
    with pytest.raises(ValueError, match='output_config'):
        cloud_request('anthropic', backend, role(), 's', 'u', schema=FULL)


def test_a_structured_request_without_a_schema_or_with_json_object_is_refused():
    with pytest.raises(ValueError, match='needs a schema'):
        cloud_request('anthropic', BACKEND, role(), 's', 'u')
    with pytest.raises(ValueError, match='not yet verified'):
        cloud_request('anthropic', BACKEND, role(output={
            'format': 'json_object', 'schema_id': 'none', 'strict': False, 'validate_response': True}), 's', 'u')


def test_gemini_still_refuses_structured_output():
    with pytest.raises(ValueError, match='not yet verified'):
        cloud_request('gemini', {'model': {'id': 'g'}}, role(), 's', 'u', schema=FULL)


# -- (A) which registered schemas pass ------------------------------------------------------------------------------
ROLE_SCHEMAS = ('planner_reply.v0', 'planner_reply_generate.v0', 'verifier_result.v0', 'observer_reply.v0',
                'observer_reply.v1', 'info_assessment.v0', 'folder_naming.v0', 'card_builder_reply.v0',
                'alias_generator_reply.v0', 'image_observation.v0')
REFUSED_SCHEMAS = (('profile_manifest.v0', 'additionalProperties'), ('eval_scenario.v0', r'\$ref'))


@pytest.mark.parametrize('schema_id', ROLE_SCHEMAS)
def test_registered_role_schemas_pass_and_keep_every_non_stripped_key(schema_id):
    full, _ = SchemaRegistry(SCHEMAS).get(schema_id)
    sent = anthropic_output_schema(full)
    assert removed_only(sent, full)


@pytest.mark.parametrize(('schema_id', 'why'), REFUSED_SCHEMAS)
def test_registered_schemas_that_cannot_be_sent_are_refused(schema_id, why):
    full, _ = SchemaRegistry(SCHEMAS).get(schema_id)
    with pytest.raises(ValueError, match=why):
        anthropic_output_schema(full)


# -- the client: hashes, client-side validation, retries, stop ------------------------------------------------------
class AnthropicDouble:
    """Labelled transport double: Anthropic-shaped replies in order; keeps bodies and headers."""

    ledger = object()

    def __init__(self, *contents):
        self.contents, self.bodies, self.headers = list(contents), [], []

    async def post(self, route, body, headers):
        self.bodies.append(deepcopy(body))
        self.headers.append(dict(headers))
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': content}],
                                         'usage': {'input_tokens': 1, 'output_tokens': 1}},
                              request=httpx.Request('POST', 'https://example.invalid/messages'))


@pytest.fixture
def schemas(tmp_path):
    directory = tmp_path / 'schemas'
    directory.mkdir()
    (directory / 'cloud1_test.v0.schema.json').write_text(json.dumps(FULL), encoding='utf-8')
    return SchemaRegistry(directory)


def make_client(tmp_path, schemas, transport, **role_changes):
    (tmp_path / '.secrets').mkdir(exist_ok=True)
    (tmp_path / '.secrets' / 'fake.key').write_text(KEY, encoding='utf-8')
    local = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 't'},
             'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'}}
    config = {'vlm': {'roles': {'planner': role(**role_changes)}, 'backends': {'cloud': dict(BACKEND), 'main': local},
                      'default_backend': 'main'}}
    probe = ProbeResult('complete', {'json_schema': True, 'text': True}, {})
    return RoleClient(config, {'cloud': transport}, {'cloud': probe, 'main': probe}, schemas, project_root=tmp_path)


GOOD = json.dumps({'n': 2, 's': 'ab'})


def test_hashes_name_the_bytes_sent_and_the_full_schema_separately(tmp_path, schemas):
    transport = AnthropicDouble(GOOD)
    client = make_client(tmp_path, schemas, transport)
    value, record = asyncio.run(client.call('planner', b'ADMIN', 'user'))
    assert value == {'n': 2, 's': 'ab'} and record['status'] == 'OK'
    posted = transport.bodies[0]['output_config']['format']['schema']
    assert posted == anthropic_output_schema(FULL)
    assert record['sent_schema_hash'] == _digest(posted)  # over the bytes of the body, never the full schema
    assert record['schema_hash'] == schemas.get('cloud1_test.v0')[1] and 'client_schema_hash' not in record
    derived = deepcopy(FULL)
    derived['properties']['n']['maximum'] = 2  # a T017-FIX2 style per-request tightening
    value, record = asyncio.run(client.call('planner', b'ADMIN', 'user', schema=derived))
    assert record['client_schema_hash'] == _digest(derived) != record['sent_schema_hash']
    assert record['sent_schema_hash'] == _digest(transport.bodies[1]['output_config']['format']['schema'])
    assert transport.bodies[1]['output_config']['format']['schema'] == anthropic_output_schema(derived)
    assert transport.bodies[1]['output_config']['effort'] == 'medium'


@pytest.mark.parametrize('reply', [{'n': 9, 's': 'ab'}, {'n': 2, 's': 'a'}, {'n': 2, 's': 'ab', 'xs': [1, 1]},
                                   {'n': 2, 's': 'ab', 'xs': [1, 2, 3]}, {'n': 2}])
def test_a_reply_that_breaks_a_stripped_bound_is_still_refused_client_side(tmp_path, schemas, reply):
    transport = AnthropicDouble(json.dumps(reply))
    value, record = asyncio.run(make_client(tmp_path, schemas, transport).call('planner', b'ADMIN', 'user'))
    assert value is None and record['status'] == 'SCHEMA_INVALID'
    assert 'ab' not in json.dumps(record['warnings'])  # R-026: type names only, never the reply


def test_the_per_request_tightening_is_checked_client_side_although_not_sent(tmp_path, schemas):
    derived = deepcopy(FULL)
    derived['properties']['n']['maximum'] = 1
    transport = AnthropicDouble(GOOD)  # n = 2 breaks the derived maximum, which Anthropic never saw
    value, record = asyncio.run(make_client(tmp_path, schemas, transport).call('planner', b'ADMIN', 'user',
                                                                                 schema=derived))
    assert value is None and record['status'] == 'SCHEMA_INVALID'


def test_key_is_read_from_the_project_file_into_x_api_key_and_never_recorded(tmp_path, schemas):
    transport = AnthropicDouble('not json', GOOD)
    client = make_client(tmp_path, schemas, transport)
    _, record = asyncio.run(client.call('planner', b'ADMIN', 'user'))
    assert transport.headers[0]['x-api-key'] == KEY and 'Authorization' not in transport.headers[0]
    assert KEY not in json.dumps(record, default=str) and 'not json' not in json.dumps(record, default=str)
    assert cloud_headers('anthropic', BACKEND, tmp_path) == {'x-api-key': KEY, 'anthropic-version': '2025-01-01'}
    missing = dict(BACKEND, api_key_file='.secrets/absent.key')
    with pytest.raises(Exception) as caught:
        cloud_headers('anthropic', missing, tmp_path)
    assert KEY not in str(caught.value)


# -- (B) Haiku 5.5 request rules ------------------------------------------------------------------------------------
def reasoning(**changes):
    return {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none', **changes}


def test_the_rule_table_is_keyed_by_model_id_and_names_haiku():
    assert 'claude-haiku-5-5' in ANTHROPIC_MODEL_REQUEST_RULES
    assert anthropic_reasoning_refusal('claude-test-model', reasoning(mode='on', budget_tokens=2048)) is None


@pytest.mark.parametrize('changes', [dict(mode='on', budget_tokens=2048), dict(mode='on', budget_tokens=1024),
                                     dict(mode='off', effort='xhigh'), dict(mode='off', effort='max')])
def test_haiku_settings_the_api_answers_400_to_are_refused(changes):
    assert anthropic_reasoning_refusal('claude-haiku-5-5', reasoning(**changes))
    with pytest.raises(ValueError):
        cloud_request('anthropic', BACKEND, role(reasoning=reasoning(**changes), max_tokens=4096,
                                                 output={'format': 'none', 'schema_id': 'none', 'strict': False,
                                                         'validate_response': False}), 's', 'u')


@pytest.mark.parametrize('changes', [dict(), dict(mode='auto', effort='high'), dict(mode='off'),
                                     dict(mode='off', effort='high'), dict(mode='off', effort='low'),
                                     dict(mode='on')])
def test_haiku_settings_the_api_accepts_are_not_refused(changes):
    assert anthropic_reasoning_refusal('claude-haiku-5-5', reasoning(**changes)) is None


def test_off_maps_to_thinking_disabled_and_other_modes_keep_their_mapping():
    assert map_cloud_reasoning('anthropic', reasoning(mode='off'))[0] == {'thinking': {'type': 'disabled'}}
    assert map_cloud_reasoning('anthropic', reasoning(mode='off', effort='high'))[0] == {
        'thinking': {'type': 'disabled'}, 'output_config': {'effort': 'high'}}
    assert map_cloud_reasoning('anthropic', reasoning(mode='auto')) == ({}, [])
    assert map_cloud_reasoning('anthropic', reasoning(mode='on', budget_tokens=2048))[0] == {
        'thinking': {'type': 'enabled', 'budget_tokens': 2048}}
    assert map_cloud_reasoning('anthropic', reasoning(mode='on')) == ({}, [('PARAM_UNSUPPORTED', 'reasoning.mode')])
    body, _ = cloud_request('anthropic', BACKEND, role(reasoning=reasoning(mode='off', effort='low'), output={
        'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}), 's', 'u')
    assert body['thinking'] == {'type': 'disabled'} and body['output_config'] == {'effort': 'low'}


def test_unsent_sampling_stays_a_warning_and_none_states_it_without_one():
    noisy = role(sampling={'temperature': 0.2, 'top_p': 0.9, 'top_k': 5, 'seed': 7, 'stop': 'none'})
    body, warnings = cloud_request('anthropic', BACKEND, noisy, 's', 'u', schema=FULL)
    assert {'temperature', 'top_p', 'top_k', 'seed'}.isdisjoint(body)
    assert warnings == [('PARAM_UNSUPPORTED', 'sampling.temperature'), ('PARAM_UNSUPPORTED', 'sampling.top_p'),
                        ('PARAM_UNSUPPORTED', 'sampling.top_k'), ('PARAM_UNSUPPORTED', 'sampling.seed')]
    assert cloud_request('anthropic', BACKEND, role(), 's', 'u', schema=FULL)[1] == []


def load_profile(tmp_path, project_root, edit):
    profile = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    edit(profile)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    return load_config(path, 'kb status', project_root=project_root)


def bind_cloud(profile, schema_id='folder_naming.v0', **reasoning_changes):
    role_ = profile['vlm']['roles']['manifest_drafter']
    role_['backend'] = 'cloud_claude'
    role_['output'].update(format='json_schema', schema_id=schema_id, validate_response=True)
    role_['reasoning'].update(**reasoning_changes)


@pytest.mark.parametrize('changes', [dict(mode='on', effort='none', budget_tokens=2048),
                                     dict(mode='off', effort='xhigh', budget_tokens='none')])
def test_haiku_400_settings_are_refused_at_load(tmp_path, project_root, changes):
    with pytest.raises(ConfigurationError, match='reasoning.*claude-haiku-5-5'):
        load_profile(tmp_path, project_root, lambda p: bind_cloud(p, **changes))


@pytest.mark.parametrize('changes', [dict(mode='auto', effort='high', budget_tokens='none'),
                                     dict(mode='off', effort='high', budget_tokens='none')])
def test_haiku_accepted_settings_load(tmp_path, project_root, changes):
    config = load_profile(tmp_path, project_root, lambda p: bind_cloud(p, **changes))
    assert config['vlm']['roles']['manifest_drafter']['reasoning']['effort'] == 'high'


def test_a_role_bound_to_anthropic_may_send_json_schema_when_the_schema_passes(tmp_path, project_root):
    config = load_profile(tmp_path, project_root, lambda p: bind_cloud(p, 'folder_naming.v0'))
    assert config['vlm']['roles']['manifest_drafter']['output']['format'] == 'json_schema'


def test_a_role_bound_to_anthropic_with_a_schema_it_cannot_take_is_refused_at_load(tmp_path, project_root):
    with pytest.raises(ConfigurationError, match=r'profile_manifest\.v0 cannot be sent to Anthropic.*additionalProperties'):
        load_profile(tmp_path, project_root, lambda p: bind_cloud(p, 'profile_manifest.v0'))


def test_json_object_stays_refused_for_anthropic_and_json_schema_for_gemini(tmp_path, project_root):
    def object_format(profile):
        bind_cloud(profile)
        profile['vlm']['roles']['manifest_drafter']['output']['format'] = 'json_object'

    def gemini(profile):
        bind_cloud(profile)
        profile['vlm']['roles']['manifest_drafter']['backend'] = 'cloud_gemini'

    for edit in (object_format, gemini):
        with pytest.raises(ConfigurationError, match='cloud structured output mapping is not verified'):
            load_profile(tmp_path, project_root, edit)


def test_the_planner_checks_its_generate_schema_too(tmp_path, project_root):
    """The planner role names planner_reply.v0 but S2 sends planner_reply_generate.v0 for `generate`; both pass."""
    def planner(profile):
        profile['vlm']['roles']['planner']['backend'] = 'cloud_claude'

    config = load_profile(tmp_path, project_root, planner)
    assert config['vlm']['roles']['planner']['backend'] == 'cloud_claude'


# -- (D) the budget keys --------------------------------------------------------------------------------------------
def test_budget_override_3000_with_stop_loads(tmp_path, project_root):
    def edit(profile):
        profile['vlm']['backends']['cloud_claude'].update(call_budget=3000, on_budget_exhausted='stop')

    backend = load_profile(tmp_path, project_root, edit)['vlm']['backends']['cloud_claude']
    assert backend['call_budget'] == 3000 and backend['on_budget_exhausted'] == 'stop'


@pytest.mark.parametrize(('key', 'value'), [('call_budget', -1), ('call_budget', 'many'), ('call_budget', 2.5),
                                            ('on_budget_exhausted', 'continue'), ('on_budget_exhausted', None)])
def test_budget_refusals(tmp_path, project_root, key, value):
    def edit(profile):
        profile['vlm']['backends']['cloud_claude'].update({'call_budget': 3000, 'on_budget_exhausted': 'stop'})
        profile['vlm']['backends']['cloud_claude'][key] = value

    with pytest.raises(ConfigurationError, match=key):
        load_profile(tmp_path, project_root, edit)


def anthropic_transport(budget, handler):
    http = dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1, max_retries=0,
                retry_statuses=[], backoff_initial_s=0, backoff_max_s=0)
    backend = dict(BACKEND, base_urls=['https://example.invalid'], http=http)
    return ModelTransport('cloud', backend, BackendConcurrency({'cloud': 1}), SingleRunBudgetLedger({'cloud': budget}),
                          transport=httpx.MockTransport(handler))


class CountingLocal:
    """The default local backend: any request it receives means the run fell back (it must not)."""

    ledger = None

    def __init__(self):
        self.posts = 0

    async def post(self, route, body, headers):
        self.posts += 1
        raise AssertionError('stop must not fall back to the local backend')


def test_stop_ends_the_run_with_budget_exhausted_and_never_falls_back(tmp_path, schemas):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': GOOD}],
                                         'usage': {}})

    async def run():
        cloud, local = anthropic_transport(2, handler), CountingLocal()
        try:
            client = make_client(tmp_path, schemas, cloud)
            client.transports['main'] = local
            first = await client.call('planner', b'ADMIN', 'user')
            second = await client.call('planner', b'ADMIN', 'user')
            with pytest.raises(ModelCallError) as caught:
                await client.call('planner', b'ADMIN', 'user')
            return first, second, caught.value, local
        finally:
            await cloud.close()

    first, second, error, local = asyncio.run(run())
    assert first[1]['status'] == second[1]['status'] == 'OK' and len(seen) == 2
    assert error.code == 'BUDGET_EXHAUSTED' and local.posts == 0
    assert KEY not in str(error) and GOOD not in str(error)


def test_stop_also_ends_a_retry_loop_at_once(tmp_path, schemas):
    def handler(request):
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': GOOD}]})

    async def run():
        cloud = anthropic_transport(2, handler)
        try:
            client = make_client(tmp_path, schemas, cloud)
            log = Attempts()

            async def call(seed):
                return await client.call('planner', b'ADMIN', 'user', seed=seed)

            def refuse(value):
                raise ValueError('refused')

            with pytest.raises(ModelCallError) as caught:
                await call_until_valid(call, refuse, attempts=5, base_seed=None, log=log)
            return caught.value, log
        finally:
            await cloud.close()

    error, log = asyncio.run(run())
    assert error.code == 'BUDGET_EXHAUSTED' and len(log.entries) == 2  # 2 requests paid, then it stopped


# -- (C) the seed rule ----------------------------------------------------------------------------------------------
def test_the_seed_fact_comes_from_the_protocol_mapping():
    assert provider_maps_seed('openai_compatible') and provider_maps_seed('gemini')
    assert not provider_maps_seed('anthropic') and provider_maps_seed(None) and provider_maps_seed('unknown')
    assert seed_field_note('anthropic') == {'seed_field': 'none_in_protocol'} and seed_field_note('gemini') == {}


def scripted_calls(provider, kinds, *, warnings=()):
    """`call(seed)` returning scripted replies; records carry the provider and warnings, as the real client's do."""
    seen = []

    async def call(seed):
        seen.append(seed)
        kind = kinds[len(seen) - 1]
        record = {'provider': provider, 'status': 'OK', 'warnings': [tuple(item) for item in warnings]}
        return kind, record

    return call, seen


def parse_good(value):
    if value != 'good':
        raise ValueError('refused')
    return value


@pytest.mark.parametrize('base_seed', [None, 4242])
def test_anthropic_retry_is_a_fresh_unseeded_sample_with_the_configured_count(base_seed):
    async def run():
        call, seen = scripted_calls('anthropic', ['bad', 'bad', 'good', 'never'], warnings=[SEED_WARNING])
        log = Attempts()
        parsed, _, _ = await call_until_valid(call, parse_good, attempts=3, base_seed=base_seed, log=log)
        return parsed, seen, log

    parsed, seen, log = asyncio.run(run())
    assert parsed == 'good' and seen == [None, None, None]  # three sends, none seeded
    assert [(e['attempt'], e['seed'], e['seed_field']) for e in log.entries] == [
        (0, None, 'none_in_protocol'), (1, None, 'none_in_protocol'), (2, None, 'none_in_protocol')]


def test_anthropic_retries_stop_at_the_configured_count():
    async def run():
        call, seen = scripted_calls('anthropic', ['bad'] * 9)
        with pytest.raises(ValueError):
            await call_until_valid(call, parse_good, attempts=2, base_seed=7, log=Attempts())
        return seen

    assert asyncio.run(run()) == [None, None]


def test_a_seeded_protocol_that_drops_the_seed_is_still_refused():
    async def run(provider):
        call, seen = scripted_calls(provider, ['bad', 'good'], warnings=[SEED_WARNING])
        log = Attempts()
        with pytest.raises(ModelCallError, match='CONFIGURATION_INVALID'):
            await call_until_valid(call, parse_good, attempts=2, base_seed=7, log=log)
        return seen, log

    for provider in ('openai_compatible', 'gemini'):
        seen, log = asyncio.run(run(provider))
        assert len(seen) == 1 and log.entries[0]['code'] == 'CONFIGURATION_INVALID' and 'seed_field' not in log.entries[0]


def test_a_seeded_protocol_keeps_its_records_and_distinct_seeds():
    async def run():
        call, seen = scripted_calls('openai_compatible', ['bad', 'good'])
        log = Attempts()
        await call_until_valid(call, parse_good, attempts=2, base_seed=7, log=log)
        return seen, log

    seen, log = asyncio.run(run())
    assert seen == [None, 8]
    assert [e['seed'] for e in log.entries] == [7, 8] and all('seed_field' not in e for e in log.entries)


def vlm_with(providers, *, policy='stop', seed='none'):
    backends = {name: {'provider': provider, 'on_budget_exhausted': policy} for name, provider in providers.items()}
    return {'roles': {'r': {'backend': 'cloud', 'sampling': {'seed': seed}}}, 'backends': backends,
            'default_backend': 'main'}


def test_load_time_seed_requirement_follows_the_reachable_protocols():
    cloud_only = {'cloud': 'anthropic', 'main': 'openai_compatible'}
    assert attempt_seed_refusal(vlm_with(cloud_only), 'r') is None                       # Anthropic, stop: no seed needed
    assert attempt_seed_refusal(vlm_with(cloud_only, seed=7), 'r') is None
    assert 'integer' in attempt_seed_refusal(vlm_with(cloud_only, policy='fallback_to_default_backend'), 'r')
    local = vlm_with(cloud_only)
    local['roles']['r']['backend'] = 'main'
    assert 'integer' in attempt_seed_refusal(local, 'r')                                  # llama.cpp keeps its rule
    assert attempt_seed_refusal(vlm_with(cloud_only, seed=7), 'r', backend_name='main') is None


def test_the_example_profile_snippet_for_a_haiku_role_loads_and_sends_none_of_the_unsupported_fields(tmp_path, project_root):
    """The commented role snippet next to `cloud_claude` in configs/enhancer.example.yaml, applied as written."""
    def edit(profile):
        bind_cloud(profile, mode='auto', effort='medium', budget_tokens='none')
        role_ = profile['vlm']['roles']['manifest_drafter']
        role_['sampling'] = {'temperature': 1.0, 'top_p': 1.0, 'top_k': 0, 'min_p': 0, 'frequency_penalty': 0,
                             'presence_penalty': 0, 'repetition_penalty': 1.0, 'seed': 'none', 'stop': 'none'}

    config = load_profile(tmp_path, project_root, edit)
    full, _ = SchemaRegistry(SCHEMAS).get('folder_naming.v0')
    body, warnings = cloud_request('anthropic', config['vlm']['backends']['cloud_claude'],
                                   config['vlm']['roles']['manifest_drafter'], 's', 'u', schema=full)
    assert {name for _, name in warnings} == {'sampling.temperature', 'sampling.top_p', 'sampling.top_k',
                                              'sampling.min_p', 'sampling.frequency_penalty',
                                              'sampling.presence_penalty', 'sampling.repetition_penalty'}
    assert body['output_config']['effort'] == 'medium' and not {'temperature', 'top_p', 'top_k', 'seed'} & set(body)
