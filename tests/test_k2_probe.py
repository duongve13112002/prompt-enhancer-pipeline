"""Multi-image capability plans verified by labelled transport doubles, never live evidence."""

import asyncio
import base64
from io import BytesIO
import json

import httpx
import pytest
from PIL import Image

from prompt_enhancer.model_probe import multiple_images_plan, probe_local_multiple_images_pair, ProbeResult
from k2_fixtures import NamingServer


def challenge(first='data:image/png;base64,first', second='data:image/png;base64,second'):
    return {'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Return value from both images in order.'},
        {'type': 'image_url', 'image_url': {'url': first}},
        {'type': 'image_url', 'image_url': {'url': second}}]}]}


class ProbeTransport:
    """Labelled challenge double whose response depends on both ordered image inputs."""
    def __init__(self, *, ignore_order=False, refuse=False):
        self.requests = []
        self.ignore_order = ignore_order
        self.refuse = refuse

    async def post(self, endpoint, body, headers):
        self.requests.append(body)
        image_parts = body['messages'][0]['content'][1:]
        first, second = [part['image_url']['url'].split(',')[1] for part in image_parts]
        value = 12 if self.ignore_order or (first, second) == ('first', 'second') else 21
        refusal = 'synthetic refusal' if self.refuse else None
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps({'value': value}), 'refusal': refusal}}]})

    async def close(self):
        pass


@pytest.mark.parametrize(('ignore_order', 'refuse', 'limit', 'expected'), [
    (False, False, 2, True), (True, False, 2, False), (False, True, 2, False), (False, False, 1, None)])
def test_multi_image_probe_requires_both_images_and_order(ignore_order, refuse, limit, expected):
    transport = ProbeTransport(ignore_order=ignore_order, refuse=refuse)
    result = asyncio.run(probe_local_multiple_images_pair(transport, 'chat', (challenge(), 12),
                        (challenge('data:image/png;base64,second', 'data:image/png;base64,first'), 21), limit, 1))
    assert result.capabilities.get('multiple_images') is expected
    assert len(transport.requests) == limit
    assert [case['challenge'] for case in result.evidence] == ['first', 'second']
    statuses = [case['status'] for case in result.evidence]
    assert statuses == (['complete', 'incomplete'] if limit == 1 else
                        ['negative', 'negative'] if refuse else
                        ['complete', 'negative'] if ignore_order else ['complete', 'complete'])
    if refuse:
        assert result.errors == {'multiple_images': 'PROBE_REFUSED'}
        assert [case['reason'] for case in result.evidence] == ['PROBE_REFUSED', 'PROBE_REFUSED']


def test_malformed_multi_image_plan_is_refused_without_requests():
    transport = ProbeTransport()
    with pytest.raises(ValueError):
        asyncio.run(probe_local_multiple_images_pair(transport, 'chat', (challenge(), 12),
                                                     (challenge(), 21), 2, 1))
    assert not transport.requests


def test_production_multi_image_plan_is_built_without_caller_challenges(k2_env, monkeypatch):
    import prompt_enhancer.model_client as module
    run = NamingServer().run(k2_env)
    run.config['vlm']['backends']['main']['probe']['max_requests'] = 6
    run.client.local_probe_plans = {'main': {'image': ('one', 'two'), 'thinking': ('on', 'off', 'key', 'observer')}}
    seen = []

    async def image(*args):
        return ProbeResult('complete', {'image': True, 'json_schema': True}, {})

    async def thinking(*args):
        return ProbeResult('complete', {'reasoning.mode': True}, {})

    async def multiple(transport, endpoint, first, second, limit, timeout):
        seen.append((first, second, limit, timeout))
        assert first[1] != second[1]
        assert first[0]['model'] == run.config['vlm']['backends']['main']['model']['id']
        assert first[0]['max_tokens'] == run.config['vlm']['roles']['folder_namer']['max_tokens']
        return ProbeResult('complete', {'multiple_images': True}, {})

    monkeypatch.setattr(module, 'probe_local_image_pair', image)
    monkeypatch.setattr(module, 'probe_local_thinking_pair', thinking)
    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', multiple)

    async def execute():
        try:
            return await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    result = asyncio.run(execute())
    assert len(seen) == 1 and seen[0][2:] == (2, 1)
    assert result.capabilities.get('multiple_images') is True


def test_bound_naming_role_adds_production_pair_once(k2_env, monkeypatch):
    import prompt_enhancer.model_client as module
    run = NamingServer().run(k2_env)
    run.probes.clear()
    run.config['vlm']['backends']['main']['probe']['max_requests'] = 6
    run.client.local_probe_plans = {'main': {'image': ('one', 'two'), 'thinking': ('on', 'off', 'key', 'observer')}}
    seen = []

    async def ordinary(*args):
        return ProbeResult('complete', {'image': True, 'json_schema': True}, {})

    async def multiple(*args):
        seen.append(True)
        return ProbeResult('complete', {'multiple_images': True}, {})

    monkeypatch.setattr(module, 'probe_local_image_pair', ordinary)
    monkeypatch.setattr(module, 'probe_local_thinking_pair', ordinary)
    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', multiple)

    async def execute():
        try:
            await run.for_role('info_assessor')
            await run.for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen == [True]


def synthetic_answer(body, *, ignore_order=False, ignore_second=False):
    """Labelled pixel-reading double; only the two synthetic center positions are relevant."""
    counts = []
    for part in body['messages'][0]['content'][1:]:
        with Image.open(BytesIO(base64.b64decode(part['image_url']['url'].split(',')[1]))) as image:
            counts.append(sum(image.getpixel((x, 30)) == (0, 0, 0) for x in (22, 66)))
    if ignore_order:
        counts.sort()
    if ignore_second:
        counts[1] = counts[0]
    return counts[0] * 10 + counts[1]


@pytest.mark.parametrize(('ignore_order', 'ignore_second', 'limit', 'expected'), [
    (False, False, 2, True), (True, False, 2, False), (False, True, 2, False), (False, False, 1, None)])
def test_production_pair_requires_both_synthetic_images_and_order(ignore_order, ignore_second, limit, expected):
    class SyntheticTransport:
        """Labelled synthetic-image transport double."""
        async def post(self, endpoint, body, headers):
            value = synthetic_answer(body, ignore_order=ignore_order, ignore_second=ignore_second)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'value': value})}}]})

    result = asyncio.run(probe_local_multiple_images_pair(SyntheticTransport(), 'chat',
                        *multiple_images_plan('configured-model', 80, lambda data: ('image/png', data)), limit, 1))
    assert result.capabilities.get('multiple_images') is expected


@pytest.mark.parametrize('limit', [1, 2, 3])
def test_production_plan_shares_probe_case_request_limit(k2_env, limit):
    from prompt_enhancer.model_transport import ModelTransport
    env = k2_env
    env.config['vlm']['backends']['main']['probe']['max_requests'] = limit
    run = NamingServer().run(env)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        value = synthetic_answer(body) if isinstance(body['messages'][0]['content'], list) else 0
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'value': value})}}]})

    original = run.transports['main']
    run.transports['main'] = ModelTransport('main', run.config['vlm']['backends']['main'], run.concurrency,
                                           run.ledger, transport=httpx.MockTransport(handler))
    run.client.probe_cases = {'main': {'json_schema': ({'messages': [{'role': 'user', 'content': 'Base challenge'}]}, lambda response: True)}}

    async def execute():
        await original.close()
        try:
            return await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    result = asyncio.run(execute())
    assert len(requests) == limit
    assert result.capabilities.get('multiple_images') is (True if limit == 3 else None)


def test_production_plan_propagates_transport_budget_exhaustion(k2_env):
    from prompt_enhancer.model_resources import BudgetExhausted
    env = k2_env
    backend = env.config['vlm']['backends']['main']
    backend['probe'] = {'max_requests': 3, 'timeout_s': 1, 'image_attempts': 1}
    run = NamingServer().run(env)
    original = run.transports['main']
    requests = []

    class BudgetedTransport:
        """Labelled one-request grant double; real local transports use probe request limits."""
        async def post(self, endpoint, body, headers):
            if requests:
                raise BudgetExhausted('main')
            requests.append(body)
            return httpx.Response(200, json={})

        async def close(self):
            pass

    run.transports['main'] = BudgetedTransport()
    run.client.probe_cases = {'main': {'image': ({'messages': []}, lambda response: True)}}

    async def execute():
        await original.close()
        try:
            with pytest.raises(BudgetExhausted):
                await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert len(requests) == 1


@pytest.mark.parametrize('budget', [4, 5, 6])
def test_role_client_local_plan_includes_bounded_multiple_image_proof(k2_env, monkeypatch, budget):
    import prompt_enhancer.model_client as module

    env = k2_env
    run = NamingServer().run(env)
    env.config['vlm']['backends']['main']['probe']['max_requests'] = budget
    run.config['vlm']['backends']['main']['probe']['max_requests'] = budget
    transport = ProbeTransport()
    original = run.transports['main']
    run.transports['main'] = transport
    run.client.local_probe_plans = {'main': {'image': ('one', 'two'), 'thinking': ('on', 'off', 'key', 'observer'),
        'multiple_images': ((challenge(), 12), (challenge('data:image/png;base64,second', 'data:image/png;base64,first'), 21))}}

    async def image(*args):
        return ProbeResult('complete', {'image': True, 'json_schema': True}, {})

    async def thinking(*args):
        return ProbeResult('complete', {'reasoning.mode': True}, {})

    monkeypatch.setattr(module, 'probe_local_image_pair', image)
    monkeypatch.setattr(module, 'probe_local_thinking_pair', thinking)
    async def execute():
        await original.close()
        try:
            return await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))

        finally:
            await run.close()

    result = asyncio.run(execute())
    assert len(transport.requests) == budget - 4
    assert result.capabilities.get('multiple_images') is (True if budget == 6 else None)
