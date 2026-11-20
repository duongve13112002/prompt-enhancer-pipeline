"""CLOUD-1 (C): the S2 planner retry follows the protocol's seed field, not a model or provider name.

Labelled CPU tests: a scripted client double and an Anthropic-shaped HTTP double; no network, fake key under tmp_path.
"""

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx

from planner_fixtures import PlannerClientDouble, failure, record
from prompt_enhancer.model_client import CAPABILITIES, BoundModelClient, RoleClient, _digest
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.planner import GENERATE_REPLY_SCHEMA, bounded_reply_schema, refuses_retry
from test_planner import literal_reply
from test_planner_generate_schema import env, run  # noqa: F401 - env is a fixture
from test_t017_fix2 import BASE_SEED, NOT_VERBATIM, SCHEMAS, seeded

DROPPED = ('PARAM_UNSUPPORTED', 'sampling.seed')
KEY = 'synthetic-planner-key'


def dropping(provider, status='SCHEMA_INVALID'):
    return None, record(status, provider=provider, warnings=[DROPPED])


def test_refuses_retry_is_keyed_on_the_protocol_seed_field():
    assert refuses_retry({'provider': 'openai_compatible', 'warnings': [DROPPED]})
    assert refuses_retry({'provider': 'gemini', 'warnings': [DROPPED]})
    assert refuses_retry({'warnings': [DROPPED]})                                  # unknown provider: today's rule
    assert not refuses_retry({'provider': 'anthropic', 'warnings': [DROPPED]})    # no seed field in the protocol
    assert not refuses_retry({'provider': 'openai_compatible', 'warnings': []})


def test_a_seeded_backend_that_drops_the_seed_still_refuses_the_planner_retry(env):  # noqa: F811
    client = PlannerClientDouble(dropping('openai_compatible'), literal_reply())
    result = run(seeded(env), client)
    assert len(client.calls) == 1 and result['state'] == 'failed'
    assert result['error']['cause'] == 'CONFIGURATION_INVALID'


def test_the_refusal_also_covers_a_retry_record_of_a_seeded_backend(env):  # noqa: F811
    client = PlannerClientDouble(failure('SCHEMA_INVALID'), (literal_reply(), record(
        'OK', provider='openai_compatible', warnings=[DROPPED])))
    result = run(seeded(env), client)
    assert len(client.calls) == 2 and result['state'] == 'failed' and result['error']['cause'] == 'CONFIGURATION_INVALID'


def test_an_anthropic_retry_is_sent_once_unseeded_and_recorded_as_fresh(env):  # noqa: F811
    first = dropping('anthropic')
    second = (literal_reply(), record('OK', provider='anthropic', warnings=[DROPPED]))
    client = PlannerClientDouble(first, second)
    result = run(seeded(env), client)
    assert result['state'] == 'ready' and len(client.calls) == 2                   # the configured retry, no more
    assert 'seed' not in client.calls[0] and 'seed' not in client.calls[1]
    assert [call['attempt']['seed'] for call in result['calls']] == [None, None]
    assert all(call['attempt']['seed_field'] == 'none_in_protocol' for call in result['calls'])
    assert result['calls'][1]['attempt']['code'] is None


def test_an_unseeded_anthropic_role_records_the_marker_too(env):  # noqa: F811
    env.config['vlm']['roles']['planner']['sampling']['seed'] = 'none'
    client = PlannerClientDouble((None, record('SCHEMA_INVALID', provider='anthropic')),
                                 (literal_reply(), record('OK', provider='anthropic')))
    result = run(env, client)
    assert result['state'] == 'ready' and all('seed' not in call for call in client.calls)
    assert [call['attempt']['seed_field'] for call in result['calls']] == ['none_in_protocol'] * 2


def test_seeded_records_keep_their_shape(env):  # noqa: F811
    client = PlannerClientDouble(failure('SCHEMA_INVALID'), literal_reply())
    result = run(seeded(env), client)
    assert all('seed_field' not in call['attempt'] for call in result['calls'])


class AnthropicSequence:
    """Labelled HTTP double: Anthropic-shaped replies in order; keeps every posted body."""

    ledger = object()

    def __init__(self, *contents):
        self.contents, self.bodies = list(contents), []

    async def post(self, route, body, headers):
        self.bodies.append(deepcopy(body))
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': content}],
                                         'usage': {}}, request=httpx.Request('POST', 'https://example.invalid/m'))


def anthropic_planner(env):  # noqa: F811
    (env.root / '.secrets').mkdir(exist_ok=True)
    (env.root / '.secrets' / 'fake.key').write_text(KEY, encoding='utf-8')
    local = env.config['vlm']['backends']['main']
    env.config['vlm']['backends']['cloud'] = {
        'provider': 'anthropic', 'engine': {'name': 'anthropic'}, 'api_key_file': '.secrets/fake.key',
        'anthropic_version': '2025-01-01', 'anthropic_workspace_id': 'none', 'allow_images': False,
        'model': {'id': 'claude-haiku-5-5', 'revision': 'none'}, 'endpoints': {'chat': 'messages'},
        'on_budget_exhausted': 'stop', 'image': deepcopy(local['image'])}
    planner = env.config['vlm']['roles']['planner']
    planner['backend'] = 'cloud'
    planner['reasoning'] = {'mode': 'auto', 'effort': 'medium', 'budget_tokens': 'none', 'template_kwargs_key': 'x'}
    planner['sampling'] = {**planner['sampling'], 'seed': BASE_SEED}
    return env


def test_real_client_planner_retry_on_anthropic_posts_the_same_unseeded_request_twice(env):  # noqa: F811
    anthropic_planner(env)
    transport = AnthropicSequence(json.dumps(NOT_VERBATIM), json.dumps(literal_reply()))
    capabilities = dict.fromkeys(CAPABILITIES, True)
    probes = {name: ProbeResult('complete', capabilities, {}) for name in ('cloud', 'main')}
    client = RoleClient(env.config, {'cloud': transport}, probes, SchemaRegistry(SCHEMAS), project_root=env.root)
    result = run(env, BoundModelClient(SimpleNamespace(client=client), 'planner'))
    assert result['state'] == 'ready' and len(transport.bodies) == 2               # the configured retry count
    first, second = transport.bodies
    assert first == second and 'seed' not in first and not {'temperature', 'top_p', 'top_k'} & set(first)
    assert first['output_config']['effort'] == 'medium'
    sent = first['output_config']['format']['schema']
    assert 'maxItems' not in json.dumps(sent) and 'minimum' not in json.dumps(sent)  # stripped
    call = result['calls'][0]
    bounded = bounded_reply_schema(SchemaRegistry(SCHEMAS), GENERATE_REPLY_SCHEMA, [], {})
    assert call['sent_schema_hash'] == _digest(sent) and call['client_schema_hash'] == _digest(bounded)
    assert call['schema_hash'] == SchemaRegistry(SCHEMAS).get(GENERATE_REPLY_SCHEMA)[1]
    assert [c['attempt']['seed'] for c in result['calls']] == [None, None]
    assert all(KEY not in json.dumps(c, default=str) for c in result['calls'])
