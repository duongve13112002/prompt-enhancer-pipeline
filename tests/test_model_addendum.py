"""P1b message contracts with labelled in-memory transports; no live providers."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

from hypothesis import example, given, settings, strategies as st
import pytest

from prompt_enhancer.config import SENTINELS
from prompt_enhancer.model_client import BoundModelClient, ModelCallError, RoleClient
from prompt_enhancer.model_cloud import cloud_request
from prompt_enhancer.model_mapping import effective_system_hash, system_messages
from prompt_enhancer.model_probe import ProbeResult
from test_model_client import DoubleTransport


SEPARATE = {'addendum_layout': 'separate_system_message'}  # test-only layouts (T017-FIX)
JOINED = {'addendum_layout': 'joined_system_message', 'addendum_separator': '\n\n'}
TEXT = st.text(alphabet=st.characters(blacklist_categories=('Cs',)))


def local_client(content='draft', *, structured=False, layout=SEPARATE):
    role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'image': 'none',
            'sampling': {}, 'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
            'output': {'format': 'json_object' if structured else 'none', 'schema_id': 'none',
                       'strict': False, 'validate_response': True}}
    backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test'},
               'model': {'id': 'synthetic', 'revision': 'pinned-test'}, 'endpoints': {'chat': 'chat'}, **layout}
    config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
    transport = DoubleTransport(content)
    client = RoleClient(config, {'main': transport}, {'main': ProbeResult('complete', {'json_object': True}, {})}, None)
    return client, transport


def sent_system_texts(body):
    return [message['content'] for message in body['messages'] if message['role'] == 'system']


@settings(max_examples=25, deadline=None)
@given(TEXT, TEXT)
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


SEPARATOR = TEXT.filter(lambda text: bool(text) and text != 'none' and text not in SENTINELS)  # usable joined values


@settings(max_examples=60, deadline=None)
@given(TEXT, TEXT.filter(bool), SEPARATOR)
@example('', 'A', '\n\n')
@example('admin', 'addendum', '\r\n---\r\n')
@example('admin', 'addendum', ' ')
@example('admin', 'addendum', '﻿')
def test_joined_layout_property_admin_prefix_then_separator_then_addendum(system, addendum, separator):
    """Property (T017-FIX F1): one leading system message; its UTF-8 bytes start with the admin bytes, then the
    configured separator, and end with the addendum; the user message is untouched."""
    client, transport = local_client(layout={'addendum_layout': 'joined_system_message',
                                             'addendum_separator': separator})
    _, record = asyncio.run(client.call('planner', system.encode(), 'user data', addendum=addendum.encode()))
    messages = transport.body['messages']
    assert [message['role'] for message in messages] == ['system', 'user']
    sent = messages[0]['content'].encode()
    assert sent.startswith(system.encode()) and sent.endswith(addendum.encode())
    assert sent == system.encode() + separator.encode() + addendum.encode()
    assert messages[1] == {'role': 'user', 'content': 'user data'}
    assert record['status'] == 'OK'


@settings(max_examples=25, deadline=None)
@given(TEXT)
def test_both_layouts_without_addendum_send_the_admin_message_alone(system):
    """Property: with no addendum the layout is not read; both layouts send byte-identical bodies."""
    bodies = []
    for layout in (SEPARATE, JOINED, {}):
        client, transport = local_client(layout=layout)
        asyncio.run(client.call('planner', system.encode(), 'data'))
        bodies.append(transport.body)
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]['messages'] == [{'role': 'system', 'content': system}, {'role': 'user', 'content': 'data'}]


ADMIN_FILE_BYTES = '﻿Admin rule one.\r\nNo trailing newline: é ✓'.encode()  # synthetic admin file bytes


@pytest.mark.parametrize('layout', [SEPARATE, JOINED], ids=['separate', 'joined'])
def test_admin_file_bytes_are_first_and_unchanged_in_either_layout(tmp_path, layout):
    """Pre-mortem: admin bytes changed, reordered or not first. Compared byte for byte against the admin file."""
    admin = tmp_path / 'system.txt'
    admin.write_bytes(ADMIN_FILE_BYTES)
    client, transport = local_client(layout=layout)
    asyncio.run(client.call('planner', admin.read_bytes(), 'data', addendum=b'ADDENDUM'))
    first = transport.body['messages'][0]
    assert first['role'] == 'system'
    assert first['content'].encode()[:len(ADMIN_FILE_BYTES)] == admin.read_bytes()
    texts = sent_system_texts(transport.body)
    if layout is SEPARATE:
        assert texts == [ADMIN_FILE_BYTES.decode(), 'ADDENDUM']
    else:
        assert texts == [ADMIN_FILE_BYTES.decode() + '\n\nADDENDUM']


@pytest.mark.parametrize('layout', [SEPARATE, JOINED, {}], ids=['separate', 'joined', 'none'])
def test_trace_hash_is_computed_over_the_system_texts_actually_sent(layout):
    """Pre-mortem: the record's effective_system_hash equals a hash recomputed from the posted body."""
    client, transport = local_client(layout=layout)
    addendum = None if not layout else b'extra'
    _, record = asyncio.run(client.call('planner', b'admin', 'data', addendum=addendum))
    texts = sent_system_texts(transport.body)
    expected = 'sha256:' + hashlib.sha256(json.dumps(texts, ensure_ascii=False, separators=(',', ':'))
                                          .encode('utf-8')).hexdigest()
    assert record['effective_system_hash'] == expected
    assert effective_system_hash('openai_compatible', transport.body) == expected


def test_trace_hash_distinguishes_the_two_layouts():
    hashes = set()
    for layout in (SEPARATE, JOINED):
        client, _ = local_client(layout=layout)
        _, record = asyncio.run(client.call('planner', b'admin', 'data', addendum=b'extra'))
        hashes.add(record['effective_system_hash'])
    assert len(hashes) == 2


@pytest.mark.parametrize('layout, expected', [
    ({}, 'addendum_layout'),
    ({'addendum_layout': 'deploy_required'}, 'addendum_layout'),
    ({'addendum_layout': 'joined_system_message'}, 'addendum_separator'),
    ({'addendum_layout': 'joined_system_message', 'addendum_separator': ''}, 'addendum_separator'),
    ({'addendum_layout': 'joined_system_message', 'addendum_separator': 'deploy_required'}, 'addendum_separator'),
    ({'addendum_layout': 'joined_system_message', 'addendum_separator': 'none'}, 'addendum_separator'),
], ids=['missing', 'sentinel', 'no-separator', 'empty-separator', 'sentinel-separator', 'none-separator'])
def test_unresolved_layout_with_an_addendum_refuses_before_http(layout, expected):
    """Pre-mortem: a backend with no usable layout is never sent silently; the request builder refuses."""
    client, transport = local_client(layout=layout)
    with pytest.raises(ModelCallError, match='^CONFIGURATION_INVALID') as caught:
        asyncio.run(client.call('planner', b'admin', 'data', addendum=b'extra'))
    assert expected in str(caught.value.__cause__)
    assert transport.body is None


def test_none_separator_is_never_concatenated_under_the_separate_layout():
    """Decision 193: `none` is read only as "no separator"; the separate layout sends the admin text and the addendum
    as two messages, and the reserved word appears in neither."""
    client, transport = local_client(layout={'addendum_layout': 'separate_system_message', 'addendum_separator': 'none'})
    asyncio.run(client.call('planner', b'admin', 'data', addendum=b'extra'))
    assert sent_system_texts(transport.body) == ['admin', 'extra']


def test_joined_layout_mapping_needs_an_admin_message():
    with pytest.raises(ValueError):
        system_messages(JOINED, None, 'extra')
    assert system_messages(SEPARATE, None, 'extra') == [{'role': 'system', 'content': 'extra'}]


def test_budget_fallback_backend_uses_its_own_layout():
    """Pre-mortem: a fallback backend builds its request with its own layout, not the first backend's."""
    from prompt_enhancer.model_resources import BudgetExhausted

    class Exhausted:
        body = None

        async def post(self, route, body, headers):
            self.body = body
            raise BudgetExhausted('main')

    client, _ = local_client()
    config = client.config
    config['vlm']['backends']['main']['on_budget_exhausted'] = 'fallback_to_default_backend'
    config['vlm']['backends']['spare'] = dict(deepcopy(config['vlm']['backends']['main']), **JOINED)
    config['vlm']['backends']['spare'].pop('on_budget_exhausted')
    config['vlm']['backends']['spare']['probe'] = {'max_requests': 1, 'timeout_s': 1}  # test-only
    config['vlm']['default_backend'] = 'spare'
    first, spare = Exhausted(), DoubleTransport('draft')
    client.transports.update(main=first, spare=spare)
    client.probes.pop('spare', None)
    client.probe_cases['spare'] = {'text': ({'probe': True}, lambda response: True)}  # labelled double probe
    output, record = asyncio.run(client.call('planner', b'admin', 'data', addendum=b'extra'))
    assert output == 'draft' and record['backend'] == 'spare'
    assert sent_system_texts(first.body) == ['admin', 'extra']
    assert sent_system_texts(spare.body) == ['admin\n\nextra']
    assert record['effective_system_hash'] == effective_system_hash('openai_compatible', spare.body)


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
    expected = json.dumps(['﻿admin\r\n', 'extra\r\n'], ensure_ascii=False, separators=(',', ':')).encode()
    assert effective_system_hash(provider, body) == 'sha256:' + hashlib.sha256(expected).hexdigest()


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
