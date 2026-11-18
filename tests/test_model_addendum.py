"""P1b message contracts with labelled in-memory transports; no live providers."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from prompt_enhancer.model_client import BoundModelClient, ModelCallError, RoleClient
from prompt_enhancer.model_cloud import cloud_request
from prompt_enhancer.model_probe import ProbeResult
from test_model_client import DoubleTransport


def local_client(content='draft', *, structured=False):
    role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'image': 'none',
            'sampling': {}, 'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
            'output': {'format': 'json_object' if structured else 'none', 'schema_id': 'none',
                       'strict': False, 'validate_response': True}}
    backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test'},
               'model': {'id': 'synthetic', 'revision': 'pinned-test'}, 'endpoints': {'chat': 'chat'}}
    config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
    transport = DoubleTransport(content)
    client = RoleClient(config, {'main': transport}, {'main': ProbeResult('complete', {'json_object': True}, {})}, None)
    return client, transport


@settings(max_examples=25, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',))),
       st.text(alphabet=st.characters(blacklist_categories=('Cs',))))
def test_addendum_is_separate_message_after_exact_admin_bytes(system, addendum):
    client, transport = local_client()
    bound = BoundModelClient(SimpleNamespace(client=client), 'planner')
    async def run():
        _, first = await bound.call(system.encode(), 'user data', addendum=addendum.encode())
        messages = transport.body['messages']
        assert messages == [{'role': 'system', 'content': system}, {'role': 'system', 'content': addendum},
                            {'role': 'user', 'content': 'user data'}]
        assert messages[0]['content'].encode() == system.encode()
        _, second = await bound.call(system.encode(), 'user data', addendum=(addendum + '!').encode())
        assert first['request_hash'] != second['request_hash']
    asyncio.run(run())


def test_absent_addendum_keeps_existing_two_message_contract():
    client, transport = local_client()
    asyncio.run(client.call('planner', b'admin\r\n', 'data'))
    assert transport.body['messages'] == [{'role': 'system', 'content': 'admin\r\n'},
                                           {'role': 'user', 'content': 'data'}]


def test_bound_default_keeps_legacy_client_signature():
    class LegacyDouble:
        async def call(self, role, system, user, *, manifest=None, images=()):
            return 'draft', {'status': 'OK'}
    bound = BoundModelClient(SimpleNamespace(client=LegacyDouble()), 'writer')
    assert asyncio.run(bound.call(b'admin', 'data'))[0] == 'draft'


@pytest.mark.parametrize('provider', ['anthropic', 'gemini'])
def test_native_cloud_system_parts_keep_admin_and_addendum_separate(provider):
    role = {'max_tokens': 20, 'sampling': {}, 'reasoning': {'mode': 'auto', 'effort': 'none',
                                                        'budget_tokens': 'none'},
            'output': {'format': 'none', 'strict': True}}
    backend = {'model': {'id': 'synthetic'}}
    original = deepcopy(role)
    body, _ = cloud_request(provider, backend, role, '\ufeffadmin\r\n', 'data', addendum='extra\r\n')
    if provider == 'anthropic':
        assert body['system'] == [{'type': 'text', 'text': '\ufeffadmin\r\n'},
                                  {'type': 'text', 'text': 'extra\r\n'}]
        assert body['messages'] == [{'role': 'user', 'content': 'data'}]
    else:
        assert body['systemInstruction']['parts'] == [{'text': '\ufeffadmin\r\n'}, {'text': 'extra\r\n'}]
        assert body['contents'] == [{'role': 'user', 'parts': [{'text': 'data'}]}]
    assert role == original


@pytest.mark.parametrize('raw', ['{"a":1,"a":2}', '{"a":NaN}', '[Infinity]', '1e999'])
def test_structured_decoder_refuses_ambiguous_or_nonfinite_json(raw):
    client, _ = local_client(raw, structured=True)
    output, record = asyncio.run(client.call('planner', b'admin', 'data'))
    assert output is None and record['status'] == 'SCHEMA_INVALID'


@pytest.mark.parametrize('addendum', ['text', 1, {}, b'\xff'])
def test_invalid_addendum_refuses_before_http(addendum):
    client, transport = local_client()
    with pytest.raises(ModelCallError):
        asyncio.run(client.call('planner', b'admin', 'data', addendum=addendum))
    assert transport.body is None


@pytest.mark.parametrize('field', ['messages', 'system', 'systemInstruction', 'contents', 'model'])
def test_addendum_identity_cannot_be_overridden_by_engine_extras(field):
    client, transport = local_client()
    client.config['vlm']['backends']['main']['extra_body'] = {field: 'replacement'}
    with pytest.raises(ModelCallError):
        asyncio.run(client.call('planner', b'admin', 'data', addendum=b'addition'))
    assert transport.body is None


@pytest.mark.parametrize('provider', ['anthropic', 'gemini'])
def test_native_addendum_rejects_engine_system_override(provider):
    role = {'max_tokens': 20, 'sampling': {}, 'reasoning': {'mode': 'auto', 'effort': 'none',
                                                        'budget_tokens': 'none'},
            'output': {'format': 'none', 'strict': True}}
    with pytest.raises(ValueError):
        cloud_request(provider, {'model': {'id': 'synthetic'}, 'extra_body': {'system': 'replacement'}},
                      role, 'admin', 'user', addendum='addition')
