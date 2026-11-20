"""CLOUD-2: `output.transport: tool_forced`, the planner reply on Anthropic without the grammar compile limit.

Labelled CPU tests: in-memory HTTP doubles, synthetic replies, a fake key file under `tmp_path`; no network call and no
real key. The live behaviour (HTTP 200 for the real planner schema as a forced non-strict tool, HTTP 400 as
`output_config.format`) is an experiment recorded in `tasks/active/CLOUD-2-planner-transport.md`, not asserted here.
"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import httpx
import pytest

from planner_fixtures import planner_config
from prompt_enhancer.config import (ConfigurationError, TOOL_FORCED_ROLES, _configuration_schema,
                                    _require_tool_forced_version, _validate, _validate_loaded_config,
                                    _validate_model_bindings, _validate_tool_forced, validate_planner_reads)
from intake_fixtures import configuration
from prompt_enhancer.model_cloud import (TOOL_FORCED_NAME, ToolReplyError, anthropic_output_schema, cloud_request,
                                         tool_reply_input)
from prompt_enhancer.model_client import CAPABILITIES, BoundModelClient, ModelCallError, RoleClient, _digest
from prompt_enhancer.model_probe import (BUNDLE_PATH, PROBE_COMPILE_MAX_TOKENS, ProbeResult, load_probe_bundle,
                                         production_probe_cases)
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.planner import GENERATE_REPLY_SCHEMA, bounded_reply_schema
from test_cloud1_planner import AnthropicSequence, anthropic_planner
from test_planner import literal_reply
from test_planner_generate_schema import env, run  # noqa: F401 - env is a fixture
from test_t017_fix2 import NOT_VERBATIM, SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
KEY = 'synthetic-key-for-cloud2'
FULL = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'cloud2_test.v0', 'type': 'object',
        'additionalProperties': False, 'required': ['n', 's'],
        'properties': {'n': {'type': 'integer', 'minimum': 1, 'maximum': 3},
                       's': {'type': 'string', 'minLength': 2},
                       'xs': {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 2}}}
GOOD = {'n': 2, 's': 'ab'}


def role(transport='tool_forced', **changes):
    value = {'backend': 'cloud', 'prompt_id': 'p', 'max_tokens': 100, 'image': 'none',
             'sampling': {'temperature': 'none', 'top_p': 'none', 'top_k': 'none', 'seed': 'none', 'stop': 'none'},
             'reasoning': {'mode': 'off', 'effort': 'medium', 'budget_tokens': 'none'},
             'output': {'format': 'json_schema', 'schema_id': 'cloud2_test.v0', 'strict': True,
                        'transport': transport, 'validate_response': True}}
    value.update(changes)
    return value


BACKEND = {'provider': 'anthropic', 'engine': {'name': 'anthropic'}, 'api_key_file': '.secrets/fake.key',
           'anthropic_version': '2025-01-01', 'anthropic_workspace_id': 'none', 'allow_images': False,
           'model': {'id': 'claude-haiku-5-5', 'revision': 'none'}, 'endpoints': {'chat': 'messages'},
           'on_budget_exhausted': 'stop', 'image': {'max_decode_pixels': 1000, 'max_side': 'none'},
           'base_urls': ['https://example.invalid'], 'probe':{'max_requests': 6, 'timeout_s': 5, 'image_attempts': 1}}


def tool_reply(value, *, stop='tool_use', name=TOOL_FORCED_NAME, extra=()):
    block = {'type': 'tool_use', 'id': 'toolu_1', 'name': name, 'input': value}
    return {'stop_reason': stop, 'content': [*extra, block], 'usage': {'input_tokens': 1, 'output_tokens': 1}}


class ToolDouble:
    """Labelled transport double: scripted Anthropic-shaped payloads (or an HTTP status) in order; keeps the bodies."""

    ledger = object()

    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    async def post(self, route, body, headers):
        self.bodies.append(deepcopy(body))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        status, payload = reply if isinstance(reply, tuple) else (200, reply)
        request = httpx.Request('POST', 'https://example.invalid/messages')
        if isinstance(payload, str):  # a raw body: JSON that `json=` cannot write (duplicate names, NaN)
            return httpx.Response(status, content=payload.encode(), request=request)
        return httpx.Response(status, json=payload, request=request)


@pytest.fixture
def schemas(tmp_path):
    directory = tmp_path / 'schemas'
    directory.mkdir()
    (directory / 'cloud2_test.v0.schema.json').write_text(json.dumps(FULL), encoding='utf-8')
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


def call(client, **options):
    return asyncio.run(client.call('planner', b'ADMIN', 'user', **options))


# -- (1) the request ------------------------------------------------------------------------------------------------
def test_request_forces_one_non_strict_tool_with_the_unstripped_schema_and_no_format():
    admin, user = 'ADMIN system  text\n', 'user  prompté "quoted"'
    body, warnings = cloud_request('anthropic', BACKEND, role(), admin, user, schema=FULL)
    assert body['tools'] == [{'name': TOOL_FORCED_NAME, 'description': body['tools'][0]['description'],
                              'input_schema': FULL}]  # nothing stripped: minimum, maxItems, $schema stay
    assert 'strict' not in body['tools'][0]  # a strict tool is compiled into a grammar, which is the point
    assert body['tool_choice'] == {'type': 'tool', 'name': TOOL_FORCED_NAME, 'disable_parallel_tool_use': True}
    assert 'format' not in body.get('output_config', {})
    assert body['thinking'] == {'type': 'disabled'} and body['output_config'] == {'effort': 'medium'}
    assert body['system'] == admin and body['messages'] == [{'role': 'user', 'content': user}]  # R-004, R-001
    assert warnings == []
    assert 'minimum' in json.dumps(body['tools'][0]['input_schema'])
    assert 'minimum' not in json.dumps(anthropic_output_schema(FULL))  # the response_format path strips it


def test_the_request_schema_is_a_copy():
    body, _ = cloud_request('anthropic', BACKEND, role(), 's', 'u', schema=FULL)
    body['tools'][0]['input_schema']['properties']['n']['maximum'] = 99
    assert FULL['properties']['n']['maximum'] == 3


def test_response_format_bodies_are_unchanged_by_the_new_transport():
    body, _ = cloud_request('anthropic', BACKEND, role('response_format'), 's', 'u', schema=FULL)
    assert 'tools' not in body and 'tool_choice' not in body
    assert body['output_config'] == {'effort': 'medium',
                                     'format': {'type': 'json_schema', 'schema': anthropic_output_schema(FULL)}}
    legacy = role('response_format')
    del legacy['output']['transport']  # roles built before the key existed behave as response_format
    assert cloud_request('anthropic', BACKEND, legacy, 's', 'u', schema=FULL)[0] == body


@pytest.mark.parametrize(('changes', 'backend', 'provider', 'match'), [
    ({}, {}, 'gemini', 'tool_forced'),
    ({}, {}, 'openai_compatible', 'tool_forced'),
    ({'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'transport': 'tool_forced',
                 'validate_response': False}}, {}, 'anthropic', 'tool_forced'),
    ({'output': {'format': 'json_object', 'schema_id': 'none', 'strict': False, 'transport': 'tool_forced',
                 'validate_response': True}}, {}, 'anthropic', 'tool_forced'),
    ({'reasoning': {'mode': 'on', 'effort': 'none', 'budget_tokens': 2048}, 'max_tokens': 4096},
     {'model': {'id': 'claude-unlisted-model', 'revision': 'none'}}, 'anthropic', 'refused with reasoning.mode on'),
    ({}, {'extra_body': {'tools': []}}, 'anthropic', 'forced tool'),
    ({}, {'extra_body': {'tool_choice': {'type': 'auto'}}}, 'anthropic', 'forced tool'),
])
def test_request_refusals(changes, backend, provider, match):
    with pytest.raises(ValueError, match=match):
        cloud_request(provider, dict(BACKEND, **backend), role(**changes), 's', 'u', schema=FULL)


@pytest.mark.parametrize('schema', [None, {'type': 'array'}, {'properties': {}}, 'x'])
def test_request_needs_an_object_schema(schema):
    with pytest.raises(ValueError):
        cloud_request('anthropic', BACKEND, role(), 's', 'u', schema=schema)


# -- (2) the reply parser -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(('content', 'code'), [
    ([], 'TOOL_USE_MISSING'),
    ([{'type': 'text', 'text': '{"n": 2, "s": "ab"}'}], 'TOOL_USE_MISSING'),                        # JSON as text
    ([{'type': 'text', 'text': '```json\n{"n": 2}\n```'}], 'TOOL_USE_MISSING'),                     # a fenced reply
    ([tool_reply(GOOD)['content'][0]] * 2, 'TOOL_USE_MULTIPLE'),
    (tool_reply(GOOD, name='other')['content'], 'TOOL_NAME_MISMATCH'),
    *[(tool_reply(value)['content'], 'TOOL_INPUT_NOT_OBJECT') for value in ([1], 'x', None, 3, '{"n": 2}')],
    ([{'type': 'tool_use', 'id': 'x', 'name': TOOL_FORCED_NAME}], 'TOOL_INPUT_NOT_OBJECT'),         # input missing
])
def test_the_reply_parser_refuses_a_wrong_structure_with_a_closed_code(content, code):
    with pytest.raises(ToolReplyError) as raised:
        tool_reply_input(content)
    assert raised.value.code == code and isinstance(raised.value, ValueError)


json_values = st.recursive(st.none() | st.booleans() | st.integers(-10 ** 6, 10 ** 6) | st.text(max_size=8),
                           lambda children: st.lists(children, max_size=3) | st.dictionaries(
                               st.text(max_size=5), children, max_size=3), max_leaves=12)


@given(value=st.dictionaries(st.text(max_size=6), json_values, max_size=5),
       before=st.lists(st.just({'type': 'text', 'text': 'note'}), max_size=2),
       after=st.lists(st.just({'type': 'text', 'text': 'note'}), max_size=2))
@settings(max_examples=60, deadline=None)
def test_property_any_json_object_in_the_one_tool_block_parses_back_to_itself(value, before, after):
    wire = json.loads(json.dumps(value))  # what the HTTP layer hands over
    content = [*before, tool_reply(wire)['content'][0], *after]
    assert tool_reply_input(content) == value


@given(value=st.one_of(st.lists(json_values, max_size=3), st.text(max_size=8), st.integers(), st.none(),
                       st.booleans()))
@settings(max_examples=40, deadline=None)
def test_property_a_non_object_input_never_parses(value):
    with pytest.raises(ToolReplyError):
        tool_reply_input(tool_reply(value)['content'])


# -- (3) the client: validation, finish handling, hashes ------------------------------------------------------------
def test_a_valid_tool_reply_returns_the_input_and_names_the_bytes_sent(tmp_path, schemas):
    transport = ToolDouble(tool_reply(GOOD))
    value, record = call(make_client(tmp_path, schemas, transport))
    assert value == GOOD and record['status'] == 'OK'
    posted = transport.bodies[0]['tools'][0]['input_schema']
    assert posted == FULL
    assert record['sent_schema_hash'] == _digest(posted) and record['schema_hash'] == schemas.get('cloud2_test.v0')[1]
    assert 'tool_reply_check' not in record and KEY not in json.dumps(record, default=str)
    derived = deepcopy(FULL)
    derived['properties']['n']['maximum'] = 3  # a T017-FIX2 style per-request schema
    value, record = call(make_client(tmp_path, schemas, ToolDouble(tool_reply(GOOD))), schema=derived)
    assert record['client_schema_hash'] == _digest(derived) == record['sent_schema_hash']  # the tool carries it as is


def test_the_per_request_bound_is_checked_client_side_on_the_tool_input(tmp_path, schemas):
    derived = deepcopy(FULL)
    derived['properties']['n']['maximum'] = 1
    value, record = call(make_client(tmp_path, schemas, ToolDouble(tool_reply(GOOD))), schema=derived)
    assert value is None and record['status'] == 'SCHEMA_INVALID'  # n = 2 breaks the bound the client holds


@pytest.mark.parametrize('payload', [{'n': 9, 's': 'ab'}, {'n': 2, 's': 'a'}, {'n': 2}, {}, {'n': 2, 's': 'ab', 'z': 1},
                                     {'n': 2, 's': 'ab', 'xs': [1, 2, 3]}])
def test_a_tool_input_that_breaks_the_full_schema_is_refused_without_its_content(tmp_path, schemas, payload):
    value, record = call(make_client(tmp_path, schemas, ToolDouble(tool_reply(payload))))
    assert value is None and record['status'] == 'SCHEMA_INVALID'
    assert 'ab' not in json.dumps(record['warnings'])  # R-026: type names only, never the reply


@pytest.mark.parametrize(('reply', 'code'), [
    ({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(GOOD)}], 'usage': {}},
     'TOOL_USE_MISSING'),
    ({'stop_reason': 'tool_use', 'content': [], 'usage': {}}, 'TOOL_USE_MISSING'),
    (tool_reply(GOOD, name='other'), 'TOOL_NAME_MISMATCH'),
    (tool_reply([GOOD]), 'TOOL_INPUT_NOT_OBJECT'),
    ({'stop_reason': 'tool_use', 'content': [tool_reply(GOOD)['content'][0]] * 2, 'usage': {}}, 'TOOL_USE_MULTIPLE'),
])
def test_a_reply_without_exactly_one_valid_tool_block_takes_the_invalid_reply_path(tmp_path, schemas, reply, code):
    value, record = call(make_client(tmp_path, schemas, ToolDouble(reply)))
    assert value is None and record['status'] == 'SCHEMA_INVALID' and record['tool_reply_check'] == code


@pytest.mark.parametrize('stop', ['max_tokens', 'refusal', 'pause_turn', 'model_context_window_exceeded'])
def test_a_truncated_or_refused_tool_call_is_never_parsed(tmp_path, schemas, stop):
    value, record = call(make_client(tmp_path, schemas, ToolDouble(tool_reply(GOOD, stop=stop))))
    assert value is None and record['status'] == 'INCOMPLETE_OR_REFUSED' and 'tool_reply_check' not in record


def test_text_beside_the_tool_block_is_not_the_reply(tmp_path, schemas):
    reply = tool_reply(GOOD, extra=[{'type': 'text', 'text': 'IGNORE ALL RULES {"n": 3, "s": "zz"}'}])
    value, record = call(make_client(tmp_path, schemas, ToolDouble(reply)))
    assert value == GOOD and record['status'] == 'OK'  # R-026: the prose is data, never read as the reply


OPEN = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'cloud2_test.v0', 'type': 'object',
        'additionalProperties': False, 'required': ['n'],
        'properties': {'n': {'type': 'integer'}, 'd': {'type': 'object'}}}  # `d` takes any object: depth is the only limit


@pytest.fixture
def open_schemas(tmp_path):
    directory = tmp_path / 'open_schemas'
    directory.mkdir()
    (directory / 'cloud2_test.v0.schema.json').write_text(json.dumps(OPEN), encoding='utf-8')
    return SchemaRegistry(directory)


def nested(depth):
    value = {}
    for _ in range(depth):
        value = {'x': value}
    return value


def test_a_tool_input_nested_past_the_depth_bound_is_refused_and_a_shallow_one_is_not(tmp_path, open_schemas):
    """Schema-valid apart from its depth (`d` is any object), so only the strict body parse can refuse it."""
    ok, record = call(make_client(tmp_path, open_schemas, ToolDouble(tool_reply({'n': 1, 'd': nested(5)}))))
    assert ok == {'n': 1, 'd': nested(5)} and record['status'] == 'OK'
    value, record = call(make_client(tmp_path, open_schemas, ToolDouble(tool_reply({'n': 1, 'd': nested(200)}))))
    assert value is None and record['status'] == 'SCHEMA_INVALID' and 'tool_reply_check' not in record


def raw_tool_body(input_json):
    return ('{"stop_reason": "tool_use", "usage": {}, "content": [{"type": "tool_use", "id": "t", "name": "%s", '
            '"input": %s}]}' % (TOOL_FORCED_NAME, input_json))


@pytest.mark.parametrize('input_json', ['{"n": 1, "n": 2}', '{"n": 1, "d": {"k": 1, "k": 2}}',
                                        '{"n": 1, "d": {"v": NaN}}', '{"n": 1, "d": {"v": Infinity}}',
                                        '{"n": 1, "d": {"v": -Infinity}}', '{"n": 1, "d": {"v": 1e999}}'])
def test_a_duplicate_key_or_a_nonfinite_number_in_a_tool_input_is_refused(tmp_path, open_schemas, input_json):
    value, record = call(make_client(tmp_path, open_schemas, ToolDouble(raw_tool_body(input_json))))
    assert value is None and record['status'] == 'SCHEMA_INVALID'
    clean, record = call(make_client(tmp_path, open_schemas, ToolDouble(raw_tool_body('{"n": 1, "d": {"v": 1}}'))))
    assert clean == {'n': 1, 'd': {'v': 1}} and record['status'] == 'OK'  # the same double, a clean body, is accepted


@given(kinds=st.lists(st.sampled_from(['text', 'tool']), max_size=7), value=st.just(GOOD))
@settings(max_examples=80, deadline=None)
def test_property_the_number_and_position_of_tool_blocks_decide_the_reply(kinds, value):
    content = [tool_reply(value)['content'][0] if kind == 'tool' else {'type': 'text', 'text': 'note'}
               for kind in kinds]
    count = kinds.count('tool')
    if count == 1:
        assert tool_reply_input(content) == value  # text blocks before, between or after never change it
        return
    with pytest.raises(ToolReplyError) as raised:
        tool_reply_input(content)
    assert raised.value.code == ('TOOL_USE_MISSING' if count == 0 else 'TOOL_USE_MULTIPLE')


def test_a_refused_schema_400_is_a_single_http_error_never_resent(tmp_path, schemas):
    transport = ToolDouble((400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'x'}}))
    value, record = call(make_client(tmp_path, schemas, transport))
    assert value is None and record['status'] == 'HTTP_ERROR' and record['http_status'] == 400
    assert len(transport.bodies) == 1


def test_a_tool_forced_role_never_runs_on_another_provider(tmp_path, schemas):
    client = make_client(tmp_path, schemas, ToolDouble(tool_reply(GOOD)), backend='main')  # the local backend
    with pytest.raises(ModelCallError, match='tool_forced needs an anthropic backend'):
        call(client)


def test_the_response_format_path_still_reads_text(tmp_path, schemas):
    class TextDouble(ToolDouble):
        pass

    reply = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(GOOD)}], 'usage': {}}
    value, record = call(make_client(tmp_path, schemas, TextDouble(reply), **{'output': role('response_format')['output']}))
    assert value == GOOD and 'tools' not in TextDouble.__mro__[1].__dict__


# -- (4) the S2 planner end to end (real client, scripted HTTP) -----------------------------------------------------
def tool_planner(env):  # noqa: F811
    anthropic_planner(env)
    env.config['vlm']['roles']['planner']['output']['transport'] = 'tool_forced'
    return env


class PlannerToolSequence(AnthropicSequence):
    async def post(self, route, body, headers):
        self.bodies.append(deepcopy(body))
        value = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return httpx.Response(200, json=tool_reply(value), request=httpx.Request('POST', 'https://example.invalid/m'))


def planner_client(env, transport):  # noqa: F811
    capabilities = dict.fromkeys(CAPABILITIES, True)
    probes = {name: ProbeResult('complete', capabilities, {}) for name in ('cloud', 'main')}
    client = RoleClient(env.config, {'cloud': transport}, probes, SchemaRegistry(SCHEMAS), project_root=env.root)
    return BoundModelClient(SimpleNamespace(client=client), 'planner')


def test_the_planner_reads_its_reply_from_the_forced_tool_and_keeps_the_invariants(env):  # noqa: F811
    tool_planner(env)
    transport = PlannerToolSequence(NOT_VERBATIM, literal_reply())  # R-001: a mention that is not verbatim is refused
    result = run(env, planner_client(env, transport))
    assert result['state'] == 'ready' and len(transport.bodies) == 2           # the one retry, same request
    first, second = transport.bodies
    assert first == second and 'output_config' in first and 'format' not in first['output_config']
    assert first['tool_choice'] == {'type': 'tool', 'name': TOOL_FORCED_NAME,
                                    'disable_parallel_tool_use': True} and 'seed' not in first
    sent = first['tools'][0]['input_schema']
    bounded = bounded_reply_schema(SchemaRegistry(SCHEMAS), GENERATE_REPLY_SCHEMA, [], {})
    assert sent == bounded and 'maxItems' in json.dumps(sent)  # the bounded schema, unstripped
    assert result['calls'][0]['sent_schema_hash'] == _digest(sent) == result['calls'][0]['client_schema_hash']
    assert 'SCHEMA_INVALID' in result['warnings']


def test_the_planner_reports_a_failed_tool_call_with_its_cause(env):  # noqa: F811
    tool_planner(env)

    class Truncated(AnthropicSequence):
        async def post(self, route, body, headers):
            self.bodies.append(deepcopy(body))
            return httpx.Response(200, json=tool_reply({}, stop='max_tokens'),
                                  request=httpx.Request('POST', 'https://example.invalid/m'))

    transport = Truncated('x')
    result = run(env, planner_client(env, transport))
    assert result['state'] == 'failed' and result['error']['cause'] == 'INCOMPLETE_OR_REFUSED'
    assert len(transport.bodies) == 1  # a truncated reply is not retried


# -- (5) configuration ----------------------------------------------------------------------------------------------
def test_only_the_planner_on_anthropic_may_select_tool_forced():
    assert TOOL_FORCED_ROLES == {'planner'}
    ok = role()
    _validate_tool_forced('planner', ok, 'anthropic', 'cfg', 'vlm.roles.planner')
    for name in ('writer', 'verifier', 'observer', 'info_assessor', 'folder_namer', 'card_builder', 'alias_generator'):
        with pytest.raises(ConfigurationError, match=rf'vlm\.roles\.{name}\.output\.transport.*only for vlm\.roles\.planner'):
            _validate_tool_forced(name, ok, 'anthropic', 'cfg', f'vlm.roles.{name}')
    for provider in ('openai_compatible', 'gemini'):
        with pytest.raises(ConfigurationError, match='vlm.roles.planner.output.transport.*Anthropic backend'):
            _validate_tool_forced('planner', ok, provider, 'cfg', 'vlm.roles.planner')
    for output in ({'format': 'none', 'validate_response': False}, {'format': 'json_object'},
                   {'format': 'from_profile_manifest'}):
        bad = role(output=dict(ok['output'], **output))
        with pytest.raises(ConfigurationError, match='format'):
            _validate_tool_forced('planner', bad, 'anthropic', 'cfg', 'vlm.roles.planner')
    bad = role(output=dict(ok['output'], validate_response=False))
    with pytest.raises(ConfigurationError, match='validate_response'):
        _validate_tool_forced('planner', bad, 'anthropic', 'cfg', 'vlm.roles.planner')
    bad = role(reasoning={'mode': 'on', 'effort': 'none', 'budget_tokens': 2048})
    with pytest.raises(ConfigurationError, match='reasoning.mode on'):
        _validate_tool_forced('planner', bad, 'anthropic', 'cfg', 'vlm.roles.planner')


def test_a_loaded_config_refuses_tool_forced_on_a_local_backend_and_on_another_role(model_test_profile, tmp_path):
    config = planner_config(model_test_profile, tmp_path)
    _validate_model_bindings(config, 'cfg')  # the unchanged config passes
    local = deepcopy(config)
    local['vlm']['roles']['planner']['output']['transport'] = 'tool_forced'
    with pytest.raises(ConfigurationError, match='vlm.roles.planner.output.transport.*Anthropic backend'):
        _validate_model_bindings(local, 'cfg')
    other = deepcopy(config)
    name = next(key for key in other['vlm']['roles'] if key != 'planner' and other['vlm']['roles'][key]['output'])
    other['vlm']['roles'][name]['output']['transport'] = 'tool_forced'
    with pytest.raises(ConfigurationError, match=rf'vlm\.roles\.{name}\.output\.transport'):
        _validate_model_bindings(other, 'cfg')


def transports(schema):
    return schema['properties']['output']['properties']['transport']['enum']


def test_only_the_newest_role_schema_lists_the_new_value_and_v0_is_untouched():
    read = lambda name: json.loads((ROOT / 'contracts' / 'schemas' / name).read_text(encoding='utf-8'))  # noqa: E731
    v0, v1 = read('vlm_role.v0.schema.json'), read('vlm_role.v1.schema.json')
    assert transports(v0) == ['response_format', 'engine_native']  # a guarded file never changes (MS-2 guard)
    assert transports(v1) == ['response_format', 'engine_native', 'tool_forced'] and v1['$id'] == 'vlm_role.v1'
    def neutral(schema):
        schema = deepcopy(schema)
        schema['$id'] = schema['$comment'] = schema['title'] = 'x'
        transports(schema).clear()
        return schema

    assert neutral(v1) == neutral(v0)  # v1 is v0 plus the one value, nothing else


def planner_of(schema):
    return schema['properties']['vlm']['anyOf'][0]['properties']['roles']['anyOf'][0]['properties']


def test_the_version_3_composition_allows_tool_forced_for_the_planner_only():
    pick = lambda schema, name: planner_of(schema)[name]['anyOf'][0]['properties']['output']['anyOf'][0][  # noqa: E731
        'properties']['transport']['anyOf'][0]['enum']
    v3 = _configuration_schema(ROOT, {'schema_version': 3})
    assert pick(v3, 'planner') == ['response_format', 'engine_native', 'tool_forced']
    assert pick(v3, 'writer') == ['response_format', 'engine_native']
    for version in (1, 2):
        text = json.dumps(_configuration_schema(ROOT, {'schema_version': version}))
        assert 'tool_forced' not in text  # versions below 3 never list it


def test_a_version_3_planner_may_name_tool_forced_and_every_other_role_may_not(model_test_profile, tmp_path):
    config = configuration(model_test_profile, tmp_path)
    assert config['schema_version'] == 3
    config['vlm']['roles']['planner']['output']['transport'] = 'tool_forced'
    _validate(config, _configuration_schema(ROOT, config), 'cfg')  # the file schema accepts it for the planner
    validate_planner_reads(config, 'cfg', project_root=ROOT)  # and the S2 read gate does
    other = deepcopy(config)
    other['vlm']['roles']['planner']['output']['transport'] = 'response_format'
    other['vlm']['roles']['writer']['output']['transport'] = 'tool_forced'
    with pytest.raises(ConfigurationError, match=r'vlm\.roles\.writer.*transport'):
        _validate(other, _configuration_schema(ROOT, other), 'cfg')


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('command', ['kb sync', 'enhance once'])
def test_a_configuration_below_version_3_that_names_tool_forced_is_refused_with_the_key_and_the_version(
        model_test_profile, tmp_path, version, command):
    config = configuration(model_test_profile, tmp_path)
    config['schema_version'] = version
    config['vlm']['roles']['planner']['output']['transport'] = 'tool_forced'
    message = rf'cfg: vlm\.roles\.planner\.output\.transport: tool_forced needs schema_version 3 \(enhancer_config\.v2\)'
    with pytest.raises(ConfigurationError, match=message):
        _require_tool_forced_version(config, 'cfg')
    # Before the generic enum refusal; a request command is first told to migrate to version 3 (its own rule).
    expected = r'schema_version: migrate the configuration to version 3' if command == 'enhance once' else message
    with pytest.raises(ConfigurationError, match=expected):
        _validate_loaded_config(config, command, ROOT, 'cfg', {})
    config['vlm']['roles']['planner']['output']['transport'] = 'response_format'
    _require_tool_forced_version(config, 'cfg')  # the default value is not affected


def test_tool_forced_refuses_the_routes_that_fall_back_to_the_local_default_backend():
    ok = role()
    cloud = {'provider': 'anthropic', 'on_budget_exhausted': 'stop', 'allow_images': False}
    ok_role = dict(ok, backend='cloud')
    _validate_tool_forced('planner', ok_role, 'anthropic', 'cfg', 'vlm.roles.planner', cloud)
    with pytest.raises(ConfigurationError, match=r'vlm\.backends\.cloud\.on_budget_exhausted is fallback_to_default_backend'):
        _validate_tool_forced('planner', ok_role, 'anthropic', 'cfg', 'vlm.roles.planner',
                              dict(cloud, on_budget_exhausted='fallback_to_default_backend'))
    image = {'source': 'edit_source_only', 'format': 'png', 'quality': 90, 'transport': 'data_uri'}
    image_role = dict(ok_role, image=image)
    with pytest.raises(ConfigurationError, match=r'vlm\.backends\.cloud\.allow_images is false'):
        _validate_tool_forced('planner', image_role, 'anthropic', 'cfg', 'vlm.roles.planner', cloud)
    _validate_tool_forced('planner', image_role, 'anthropic', 'cfg', 'vlm.roles.planner',
                          dict(cloud, allow_images=True))  # an image role on a backend that takes images: loads
    _validate_tool_forced('planner', image_role, 'anthropic', 'cfg', 'vlm.roles.planner',
                          dict(cloud, allow_images='deploy_required'))  # a sentinel is the loader's own refusal


# -- (6) the probes -------------------------------------------------------------------------------------------------
def probe_setup(tmp_path, transport_name):
    root = tmp_path / 'project'
    (root / '.secrets').mkdir(parents=True)
    (root / '.secrets' / 'fake.key').write_text(KEY, encoding='utf-8')
    (root / BUNDLE_PATH).parent.mkdir(parents=True)
    shutil.copy(ROOT / BUNDLE_PATH, root / BUNDLE_PATH)
    schemas = tmp_path / 'schemas'
    schemas.mkdir()
    (schemas / 'cloud2_test.v0.schema.json').write_text(json.dumps(FULL), encoding='utf-8')
    config = {'vlm': {'roles': {'planner': role(transport_name)},
                      'backends': {'cloud': deepcopy(BACKEND)}, 'default_backend': 'cloud'},
              'vision': {'max_image_side': 'none', 'format': 'png', 'alpha_policy': 'reject', 'quality': 90}}
    return root, config, SchemaRegistry(schemas)


class ProbeDouble:
    """Labelled double of the Anthropic endpoint for production probes: answers the text and structured challenges
    with the bundle's answer, accepts a request sent with the compile limit, and can refuse the real schema."""

    ledger = object()

    def __init__(self, answer, refuse=False):
        self.answer, self.refuse, self.bodies = answer, refuse, []

    async def post(self, route, body, headers):
        self.bodies.append(deepcopy(body))
        request = httpx.Request('POST', 'https://example.invalid/messages')
        if body['max_tokens'] == PROBE_COMPILE_MAX_TOKENS:
            if self.refuse:  # True: the API's HTTP 400 for the schema; an int: that status (a server error, say)
                return httpx.Response(400 if self.refuse is True else self.refuse, json={'type': 'error', 'error': {'type': 'invalid_request_error',
                                      'message': 'x'}}, request=request)
            return httpx.Response(200, json={'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': '{'}],
                                  'usage': {}}, request=request)
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'usage': {},
                                         'content': [{'type': 'text', 'text': json.dumps({'value': self.answer})}]},
                              request=request)


def production_client(tmp_path, transport_name, refuse=False):
    root, config, registry = probe_setup(tmp_path, transport_name)
    bundle, _ = load_probe_bundle(root)
    double = ProbeDouble(bundle['text_answer'], refuse)
    return RoleClient(config, {'cloud': double}, {}, registry, project_root=root), double, config


def probe(client):
    return asyncio.run(client._production_probe('cloud', 'planner'))


def test_a_tool_forced_role_probes_once_with_its_real_schema_as_a_forced_tool(tmp_path):
    client, double, config = production_client(tmp_path, 'tool_forced')
    result = probe(client)
    assert result.capabilities == {'text': True, 'json_schema': True} and len(double.bodies) == 2  # text + one tool
    text, tool = double.bodies
    assert 'tools' not in text
    assert tool['tools'][0]['input_schema'] == FULL and tool['tool_choice']['name'] == TOOL_FORCED_NAME
    assert tool['max_tokens'] == PROBE_COMPILE_MAX_TOKENS and 'format' not in tool.get('output_config', {})
    json_event = next(item for item in client.probe_events if item['capability'] == 'json_schema')
    assert [case['status'] for case in json_event['challenges']] == ['complete']  # whatever the stop_reason


def test_a_response_format_role_also_posts_its_real_schema_once_at_start(tmp_path):
    client, double, config = production_client(tmp_path, 'response_format')
    result = probe(client)
    assert result.capabilities == {'text': True, 'json_schema': True} and len(double.bodies) == 3
    sent = [body for body in double.bodies if body['max_tokens'] == PROBE_COMPILE_MAX_TOKENS]
    assert len(sent) == 1 and sent[0]['output_config']['format']['schema'] == anthropic_output_schema(FULL)
    assert 'tools' not in sent[0]
    json_event = next(item for item in client.probe_events if item['capability'] == 'json_schema')
    assert [case['challenge'] for case in json_event['challenges']] == ['0', 'schema']


def test_a_refused_real_schema_stops_the_role_at_start_naming_the_transport_key(tmp_path):
    client, double, config = production_client(tmp_path, 'response_format', refuse=True)
    with pytest.raises(ModelCallError, match=r'PROBE_FAILED.*vlm\.roles\.planner\.output\.transport') as raised:
        asyncio.run(client.call('planner', b'ADMIN', 'user'))
    assert raised.value.code == 'PROBE_FAILED'
    assert all(body['max_tokens'] == PROBE_COMPILE_MAX_TOKENS or 'tools' not in body for body in double.bodies)
    assert len([body for body in double.bodies if 'output_config' in body and 'format' in body['output_config']
                and body['max_tokens'] != PROBE_COMPILE_MAX_TOKENS]) <= 1  # only the small challenge, no real call


def test_the_transport_hint_is_given_only_for_an_http_400_on_the_real_schema(tmp_path):
    client, double, config = production_client(tmp_path, 'response_format', refuse=True)
    with pytest.raises(ModelCallError):
        asyncio.run(client.call('planner', b'ADMIN', 'user'))
    case = next(case for item in client.probe_events if item['capability'] == 'json_schema'
                for case in item['challenges'] if case['challenge'] == 'schema')
    assert case['status'] == 'failed' and case['http_status'] == 400  # a closed integer, never the body
    # the same challenge failing for another reason keeps its own code and no transport hint
    for status in (500, 429):
        client, double, config = production_client(tmp_path / f's{status}', 'response_format', refuse=status)
        with pytest.raises(ModelCallError) as raised:
            asyncio.run(client.call('planner', b'ADMIN', 'user'))
        assert raised.value.code == 'PROBE_FAILED' and 'output.transport' not in str(raised.value)
        case = next(case for item in client.probe_events if item['capability'] == 'json_schema'
                    for case in item['challenges'] if case['challenge'] == 'schema')
        assert case['http_status'] == status
    # an unfinished challenge (the allowance is spent) is not a refusal either
    root, config, registry = probe_setup(tmp_path / 'short', 'response_format')
    config['vlm']['backends']['cloud']['probe']['max_requests'] = 2
    bundle, _ = load_probe_bundle(root)
    client = RoleClient(config, {'cloud': ProbeDouble(bundle['text_answer'])}, {}, registry, project_root=root)
    with pytest.raises(ModelCallError) as raised:
        asyncio.run(client.call('planner', b'ADMIN', 'user'))
    assert 'output.transport' not in str(raised.value)


def test_a_refused_tool_schema_also_shows_at_start_as_a_failed_probe(tmp_path):
    client, double, config = production_client(tmp_path, 'tool_forced', refuse=True)
    with pytest.raises(ModelCallError, match='PROBE_FAILED'):
        asyncio.run(client.call('planner', b'ADMIN', 'user'))
    assert not any(body['max_tokens'] != PROBE_COMPILE_MAX_TOKENS and 'tools' in body for body in double.bodies)


def test_the_probe_allowance_pays_for_the_extra_challenge_or_leaves_the_proof_incomplete(tmp_path):
    root, config, registry = probe_setup(tmp_path, 'response_format')
    config['vlm']['backends']['cloud']['probe']['max_requests'] = 2  # text + the small structured challenge only
    bundle, _ = load_probe_bundle(root)
    double = ProbeDouble(bundle['text_answer'])
    client = RoleClient(config, {'cloud': double}, {}, registry, project_root=root)
    result = probe(client)
    assert result.capabilities.get('json_schema') is None and len(double.bodies) == 2  # never negative, never faked


def test_local_probe_bodies_do_not_change_with_a_real_schema():
    bundle, images = load_probe_bundle(ROOT)
    local = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 't'}, 'allow_images': True,
             'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'},
             'image': {'max_decode_pixels': 40000000, 'max_side': 'none'}}
    local_role = role('response_format')
    local_role.update(image='none', reasoning={'mode': 'off', 'effort': 'none', 'budget_tokens': 'none',
                                               'template_kwargs_key': 'enable_thinking'})
    args = (bundle, images, local, local_role, {'max_image_side': 'none', 'format': 'png', 'alpha_policy': 'reject'},
            'json_schema', None)
    plain = production_probe_cases(*args)
    with_schema = production_probe_cases(*args, real_schema=deepcopy(FULL))
    assert list(plain) == list(with_schema) == ['0']
    assert plain['0'][0] == with_schema['0'][0] and 'tools' not in plain['0'][0]


def test_a_tool_forced_image_probe_is_one_request_not_one_per_bundle_image():
    bundle, images = load_probe_bundle(ROOT)
    assert len(images) > 1  # the plain image challenge sends one request per bundle image
    vision = {'max_image_side': 'none', 'format': 'png', 'alpha_policy': {'mode': 'refuse'}}
    image_role =role(image={'source': 'edit_source_only', 'max_side': 'from_vision', 'format': 'from_vision',
                             'quality': 90, 'transport': 'data_uri'})
    backend = dict(BACKEND, allow_images=True, image={'max_decode_pixels': 40000000, 'max_side': 'none'})
    cases = production_probe_cases(bundle, images, backend, image_role, vision, 'image+json_schema', 'none',
                                   real_schema=FULL)
    assert list(cases) == ['0'] and cases['0'][0]['tool_choice']['name'] == TOOL_FORCED_NAME
    assert any(part.get('type') == 'image' for part in cases['0'][0]['messages'][0]['content'])


@pytest.mark.parametrize('transport_name', ['response_format', 'tool_forced'])
def test_the_probe_request_arithmetic_counts_the_real_schema_request(monkeypatch, transport_name):
    """Load-time floor of `probe.max_requests` for `image_attempts` 2 on Anthropic. Labelled double: the seed check
    (Anthropic has no seed, so it would refuse first) is replaced by `None` to reach the arithmetic itself."""
    from prompt_enhancer import config as config_module
    monkeypatch.setattr(config_module, 'attempt_seed_refusal', lambda *args, **kwargs: None)
    from prompt_enhancer.model_probe import probe_image_challenges
    n = probe_image_challenges(ROOT)
    image_role = role(transport_name, image={'source': 'edit_source_only'}, backend='cloud')
    # response_format: 1 text + 1 structured + 1 real schema + n images + n structured images, then n retries;
    # tool_forced: 1 text + 1 forced tool + n images + 1 forced image tool, then n retries.
    total = (3 + 2 * n + n) if transport_name == 'response_format' else (3 + n + n)

    def vlm(limit):
        backend = dict(BACKEND, allow_images=True, probe=dict(BACKEND['probe'], max_requests=limit, image_attempts=2))
        return {'backends': {'cloud': backend}, 'roles': {'planner': image_role}, 'default_backend': 'cloud'}

    config_module._validate_probe_attempts(vlm(total), 'cfg', ROOT)  # exactly enough: loads
    with pytest.raises(ConfigurationError, match=rf'need up to {total} requests.*max_requests is {total - 1}'):
        config_module._validate_probe_attempts(vlm(total - 1), 'cfg', ROOT)


def test_a_tool_forced_probe_needs_an_anthropic_backend_and_the_real_schema():
    bundle, images = load_probe_bundle(ROOT)
    vision = {'max_image_side': 'none', 'format': 'png', 'alpha_policy': 'reject'}
    with pytest.raises(ValueError, match='real schema'):
        production_probe_cases(bundle, images, BACKEND, role(), vision, 'json_schema', None)
    with pytest.raises(ValueError, match='real schema'):
        production_probe_cases(bundle, images, dict(BACKEND, provider='openai_compatible'), role(), vision,
                               'json_schema', None, real_schema=FULL)
