"""Decisions 183 and 188 at the role client: the per-request seed, the budget and the dropped-seed guard (labelled doubles)."""

import asyncio
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from prompt_enhancer.model_client import ModelCallError, RoleClient
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_resources import BudgetExhausted
from prompt_enhancer.model_schemas import SchemaRegistry
from prompt_enhancer.reply_retry import Attempts, call_until_valid

SCHEMAS = Path(__file__).resolve().parents[1] / 'contracts' / 'schemas'


class ScriptedTransport:
    """Replies with the scripted contents in order; a BudgetExhausted entry raises as a spent ledger would."""

    def __init__(self, *contents):
        self.contents, self.bodies = list(contents), []

    async def post(self, route, body, headers):
        self.bodies.append(body)
        content = self.contents[min(len(self.bodies), len(self.contents)) - 1]
        if isinstance(content, Exception):
            raise content
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}], 'usage': {}},
                              request=httpx.Request('POST', 'http://127.0.0.1/chat'))


def make(transport, *, seed_supported=True, seed=5):
    role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'image': 'none',
            'sampling': {'temperature': 0.2, 'seed': seed},
            'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
            'output': {'format': 'json_object', 'schema_id': 'none', 'strict': False, 'validate_response': True}}
    backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'},
               'model': {'id': 'local', 'revision': 'r1'}, 'endpoints': {'chat': 'chat'}}
    config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
    capabilities = {'json_object': True, 'sampling.temperature': True, 'sampling.seed': seed_supported}
    return role, RoleClient(config, {'main': transport}, {'main': ProbeResult('complete', capabilities, {})},
                            SchemaRegistry(SCHEMAS))


def drive(client, attempts, base=5):
    log = Attempts()

    async def call(seed):
        return await client.call('planner', b'system', 'data', seed=seed)

    async def go():
        return await call_until_valid(call, lambda value: value, attempts=attempts, base_seed=base, log=log)
    return go, log


def test_a_retry_changes_only_the_seed_and_the_configured_role_is_untouched():
    transport = ScriptedTransport('not json', '{"ok": true}')
    role, client = make(transport)
    before = deepcopy(client.config)
    go, log = drive(client, 3)
    value, _, _ = asyncio.run(go())
    assert value == {'ok': True} and len(transport.bodies) == 2
    first, second = transport.bodies
    assert first['seed'] == 5 and second['seed'] == 6
    assert {k: v for k, v in first.items() if k != 'seed'} == {k: v for k, v in second.items() if k != 'seed'}
    # artifact keys are made from the configured role (`semantic_role_settings`), which the request copy never touches
    assert role['sampling']['seed'] == 5 and client.config == before
    assert [(e['attempt'], e['seed'], e['code']) for e in log.entries] == [(0, 5, 'SCHEMA_INVALID'), (1, 6, None)]
    assert log.records[0]['effective_body_hash'] != log.records[1]['effective_body_hash']  # the record names what was sent


def test_a_call_without_a_seed_override_is_unchanged():
    transport = ScriptedTransport('{"ok": true}')
    _, client = make(transport)
    asyncio.run(client.call('planner', b'system', 'data'))
    assert transport.bodies[0]['seed'] == 5


@pytest.mark.parametrize('seed', [-1, True, 1.5, '3'])
def test_an_invalid_override_is_refused_before_any_request(seed):
    transport = ScriptedTransport('{"ok": true}')
    _, client = make(transport)
    with pytest.raises(ModelCallError):
        asyncio.run(client.call('planner', b'system', 'data', seed=seed))
    assert transport.bodies == []


def test_retries_are_requests_and_an_exhausted_budget_is_not_retried():
    """Each attempt is one request through the transport that charges the ledger; a spent budget ends the loop."""
    transport = ScriptedTransport('not json', BudgetExhausted('spent'), '{"ok": true}')
    _, client = make(transport)
    go, log = drive(client, 5)
    with pytest.raises(ModelCallError) as caught:
        asyncio.run(go())
    assert caught.value.code == 'BUDGET_EXHAUSTED'
    assert len(transport.bodies) == 2  # the retry was sent (and charged); nothing after the refusal


def test_a_backend_that_drops_the_seed_is_a_configuration_error_not_a_repeat():
    transport = ScriptedTransport('not json', '{"ok": true}')
    _, client = make(transport, seed_supported=False)
    go, log = drive(client, 3)
    with pytest.raises(ModelCallError) as caught:
        asyncio.run(go())
    assert caught.value.code == 'CONFIGURATION_INVALID' and len(transport.bodies) == 1


def test_one_attempt_tolerates_a_dropped_seed_exactly_as_before():
    transport = ScriptedTransport('not json')
    _, client = make(transport, seed_supported=False)
    go, log = drive(client, 1)
    with pytest.raises(ValueError):
        asyncio.run(go())
    assert len(transport.bodies) == 1 and len(log.entries) == 1
