"""T017-FIX F2: generate requests send `planner_reply_generate.v0`, which forbids the edit fields structurally.

Labelled CPU tests: synthetic replies, in-memory HTTP doubles and real tiny snapshots; no model call. The replay
fixture is synthetic (shape of the J4a2 failure: a generate reply with seven `preserve` locators), copied from no run.
"""

import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from planner_fixtures import PlannerClientDouble, failure, intake_view, link, planner_config, prepared_root, reply
from prompt_enhancer.model_client import CAPABILITIES, BoundModelClient, RoleClient
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_resources import BudgetExhausted
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.planner import GENERATE_REPLY_SCHEMA, REPLY_SCHEMA, bounded_reply_schema, check_reply, plan
from test_planner import PROMPT, kb, literal_reply, png, shortlist_for  # noqa: F401 - kb is a fixture

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / 'contracts' / 'schemas'
EDIT_LISTS = ('edit_targets', 'delta', 'preserve', 'dependent_effects')
SEVEN = [{'locator': f'synthetic locator {index}', 'source': 'edit_kind_default'} for index in range(7)]


def derive_generate(base):
    """The documented transformation: edit_kind null, the four edit lists hold no item, all else unchanged."""
    value = deepcopy(base)
    value['properties']['edit_kind'] = {'type': 'null'}
    for field in EDIT_LISTS:
        value['properties'][field] = {'type': 'array', 'maxItems': 0,
                                      **{k: v for k, v in base['properties'][field].items() if k != 'type'}}
    return value


def comparable(schema):
    return {key: value for key, value in schema.items() if key not in ('$id', 'title', '$comment')}


def test_generate_schema_equals_the_transformation_of_the_shared_schema():
    base = json.loads((SCHEMAS / 'planner_reply.v0.schema.json').read_bytes())
    generate = json.loads((SCHEMAS / 'planner_reply_generate.v0.schema.json').read_bytes())
    assert generate['$id'] == GENERATE_REPLY_SCHEMA and base['$id'] == REPLY_SCHEMA
    assert comparable(generate) == comparable(derive_generate(base))
    assert json.dumps(comparable(generate), sort_keys=True) == json.dumps(comparable(derive_generate(base)),
                                                                          sort_keys=True)


def test_shared_edit_schema_is_not_narrowed():
    """Pre-mortem: the edit schema accidentally narrowed. planner_reply.v0 keeps open edit fields."""
    base = json.loads((SCHEMAS / 'planner_reply.v0.schema.json').read_bytes())
    for field in EDIT_LISTS:
        assert 'maxItems' not in base['properties'][field]
    assert base['properties']['edit_kind'] != {'type': 'null'}
    registry = SchemaRegistry(SCHEMAS)
    edit = reply(edit_kind='local_edit', preserve=SEVEN,
                 edit_targets=[{'id': 't1', 'entity_id': None, 'locator': 'the sign'}],
                 delta=[{'id': 'd1', 'target_id': 't1', 'operation': 'replace', 'description': 'x'}],
                 dependent_effects=[{'target_id': 't1', 'effect': 'none'}])
    registry.validate(REPLY_SCHEMA, edit)
    with pytest.raises(Exception):
        registry.validate(GENERATE_REPLY_SCHEMA, edit)


@pytest.mark.parametrize('field, value', [
    ('preserve', SEVEN), ('edit_targets', [{'id': 't1', 'entity_id': None, 'locator': 'x'}]),
    ('delta', [{'id': 'd1', 'target_id': 't1', 'operation': 'o', 'description': 'd'}]),
    ('dependent_effects', [{'target_id': 't1', 'effect': 'e'}]), ('edit_kind', 'local_edit'),
])
def test_generate_schema_refuses_each_edit_field(field, value):
    registry = SchemaRegistry(SCHEMAS)
    registry.validate(GENERATE_REPLY_SCHEMA, reply())
    registry.validate(REPLY_SCHEMA, reply(**{field: value}))
    with pytest.raises(Exception):
        registry.validate(GENERATE_REPLY_SCHEMA, reply(**{field: value}))


@pytest.fixture
def env(model_test_profile, tmp_path, project_root):
    return SimpleNamespace(config=planner_config(model_test_profile, tmp_path), root=prepared_root(project_root))


def run(env, client, prompt=PROMPT, *, task='generate', source=None, shortlist=None, snapshot=None):
    intake = intake_view(env.config, prompt, task=task, source=source, snapshot=snapshot)
    return asyncio.run(plan(intake, shortlist, client, project_root=env.root))


def test_generate_sends_the_generate_schema_on_the_first_call_and_the_retry(env):
    client = PlannerClientDouble(failure('SCHEMA_INVALID'), literal_reply())
    result = run(env, client)
    assert result['state'] == 'ready' and len(client.calls) == 2
    assert [call['schema_id'] for call in client.calls] == [GENERATE_REPLY_SCHEMA] * 2


def test_generate_second_chance_sends_the_generate_schema(kb):  # noqa: F811 - pytest fixture
    kb.config['planner']['second_chance_rounds'] = 1
    kb.config['candidates']['shortlist_size'] = 1
    prompt = 'Kimono with a Donut'
    first = reply(links=[link('Kimono', 'resolved', position=0), link('Donut', 'no_match', needs_lookup=True)])
    second = reply(links=[link('Kimono', 'resolved', position=0), link('Donut', 'resolved', position=1,
                                                                        role='object')])
    client = PlannerClientDouble(first, second)
    intake = intake_view(kb.config, prompt, snapshot=kb.snapshot)
    asyncio.run(plan(intake, shortlist_for(kb, prompt), client, project_root=kb.root))
    assert len(client.calls) == 2
    assert [call['schema_id'] for call in client.calls] == [GENERATE_REPLY_SCHEMA] * 2


def test_edit_double_receives_no_schema_override(env):
    edit = reply(edit_kind='local_edit', edit_targets=[{'id': 't1', 'entity_id': None, 'locator': 'the sign'}],
                 delta=[{'id': 'd1', 'target_id': 't1', 'operation': 'replace', 'description': 'new'}],
                 preserve=[{'locator': 'everything else', 'source': 'edit_kind_default'}],
                 dependent_effects=[])
    client = PlannerClientDouble(edit)
    source = {'image_id': 'sha256:test', 'role': 'source_image', 'data': base64.b64encode(png()).decode('ascii')}
    result = run(env, client, task='edit', source=source)
    assert result['state'] == 'ready' and client.calls[0]['schema_id'] is None


def test_planner_invariant_still_refuses_edit_fields_for_generate():
    """Defence kept at planner.py: a generate reply with edit fields fails the invariant even if a schema let it."""
    with pytest.raises(ValueError):
        check_reply(reply(preserve=SEVEN), 'prompt', 'generate', [], {}, frozenset())


class SequenceTransport:
    """Labelled HTTP double returning the scripted replies in order and keeping every posted body."""

    def __init__(self, *contents):
        self.contents, self.bodies = list(contents), []

    async def post(self, route, body, headers):
        self.bodies.append(deepcopy(body))
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                                         'usage': {}}, request=httpx.Request('POST', 'http://127.0.0.1/chat'))


def real_client(env, transports):
    probes = {name: ProbeResult('complete', dict.fromkeys(CAPABILITIES, True), {}) for name in transports}
    client = RoleClient(env.config, transports, probes, SchemaRegistry(env.root / 'contracts' / 'schemas'),
                        project_root=env.root)
    return BoundModelClient(SimpleNamespace(client=client), 'planner'), client


def sent_schema(body):
    return body['response_format']['json_schema']['schema']


def test_generate_request_body_carries_the_generate_schema(env):
    transport = SequenceTransport(json.dumps(literal_reply()))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound)
    assert result['state'] == 'ready'
    # T017-FIX2: the generate file, bounded to the (empty) shortlist this request sent; the record names the file
    assert sent_schema(transport.bodies[0]) == bounded_reply_schema(SchemaRegistry(SCHEMAS), GENERATE_REPLY_SCHEMA,
                                                                    [], {})
    assert sent_schema(transport.bodies[0])['properties']['preserve']['maxItems'] == 0
    assert result['calls'][0]['schema_id'] == GENERATE_REPLY_SCHEMA


def test_edit_request_body_carries_the_shared_schema_and_accepts_preserve(env):
    edit = reply(edit_kind='local_edit', edit_targets=[{'id': 't1', 'entity_id': None, 'locator': 'the sign'}],
                 delta=[{'id': 'd1', 'target_id': 't1', 'operation': 'replace', 'description': 'new'}],
                 preserve=SEVEN, dependent_effects=[])
    transport = SequenceTransport(json.dumps(edit))
    bound, _ = real_client(env, {'main': transport})
    source = {'image_id': 'sha256:test', 'role': 'source_image', 'data': base64.b64encode(png()).decode('ascii')}
    result = run(env, bound, task='edit', source=source)
    assert result['state'] == 'ready' and len(result['intent_ir']['preserve']) == 7
    # T017-FIX2: the shared file bounded to the (empty) shortlist; its open edit fields are untouched
    sent = sent_schema(transport.bodies[0])
    assert sent == bounded_reply_schema(SchemaRegistry(SCHEMAS), REPLY_SCHEMA, [], {})
    assert sent['properties']['preserve'] == json.loads(
        (SCHEMAS / 'planner_reply.v0.schema.json').read_bytes())['properties']['preserve']
    assert result['calls'][0]['schema_id'] == REPLY_SCHEMA


def test_replay_seven_locator_generate_reply_is_refused_by_the_client_schema(env):
    """Replay F2 (synthetic): the server ignores maxItems and answers a generate request with seven preserve
    locators twice. Client validation refuses both against the generate schema; S2 degrades, never plans edits."""
    transport = SequenceTransport(json.dumps(reply(preserve=SEVEN)))
    bound, _ = real_client(env, {'main': transport})
    result = run(env, bound)
    assert len(transport.bodies) == 2
    assert all(sent_schema(body)['properties']['preserve']['maxItems'] == 0 for body in transport.bodies)
    assert result['state'] == 'degraded' and result['intent_ir'] is None
    assert result['error']['code'] == 'PLANNER_REPLY_INVALID'
    assert [call['status'] for call in result['calls']] == ['SCHEMA_INVALID', 'SCHEMA_INVALID']


def test_budget_fallback_still_sends_the_generate_schema(env):
    """Pre-mortem: no path (here the budget fallback) drops back to the shared schema for a generate request."""

    class Exhausted:
        bodies = []

        async def post(self, route, body, headers):
            self.bodies.append(body)
            raise BudgetExhausted('main')

    backends = env.config['vlm']['backends']
    backends['spare'] = deepcopy(backends['main'])
    backends['spare']['probe'] = {'max_requests': len(CAPABILITIES), 'timeout_s': 1}  # test-only probe allowance
    backends['main']['on_budget_exhausted'] = 'fallback_to_default_backend'
    env.config['vlm']['default_backend'] = 'spare'
    first, spare = Exhausted(), SequenceTransport(json.dumps(literal_reply()))
    bound, client = real_client(env, {'main': first, 'spare': spare})
    client.probes.pop('spare')
    client.probe_cases['spare'] = {name: ({'probe': True}, lambda response: True)  # labelled double probes
                                   for name in CAPABILITIES}
    result = run(env, bound)
    assert result['state'] == 'ready', (result['error'], result['calls'])
    assert sent_schema(first.bodies[0])['properties']['preserve']['maxItems'] == 0
    assert sent_schema(spare.bodies[-1])['properties']['preserve']['maxItems'] == 0


@pytest.mark.parametrize('value', [1, 'none', 'unregistered.v0'])
def test_invalid_schema_override_refuses_before_http(env, value):
    transport = SequenceTransport(json.dumps(reply()))
    bound, client = real_client(env, {'main': transport})
    with pytest.raises(Exception):
        asyncio.run(client.call('planner', b'admin', 'data', schema_id=value))
    assert transport.bodies == []
