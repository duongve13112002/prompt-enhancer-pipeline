import asyncio

import pytest
import httpx

from prompt_enhancer.model_probe import (probe_backend, probe_capabilities, probe_local_image_pair,
                                         probe_local_thinking_pair, require_capabilities)


def test_probe_requires_evidence_and_distinguishes_incomplete_and_failed():
    async def run():
        async def supported():
            return True

        async def no_proof():
            return 200

        partial = await probe_capabilities({'image': supported, 'schema': supported}, 1, 1)
        assert partial.status == 'incomplete'
        with pytest.raises(ValueError, match='schema'):
            require_capabilities(partial, ['schema'])
        failed = await probe_capabilities({'image': no_proof}, 1, 1)
        assert failed.status == 'failed'
        assert failed.capabilities == {}
        complete = await probe_capabilities({'image': supported}, 1, 1)
        assert complete.status == 'complete'
        require_capabilities(complete, ['image'])

    asyncio.run(run())


def test_backend_probe_uses_transport_and_verifies_response():
    async def run():
        class Double:
            async def post(self, endpoint, body, headers):
                assert endpoint == 'chat'
                return httpx.Response(200, json={'seen': body['value']})

        result = await probe_backend(Double(), 'chat', {'sampling': ({'value': 3},
                                     lambda response: response.json()['seen'] == 3)}, 1, 1)
        assert result.status == 'complete' and result.capabilities['sampling'] is True
        result = await probe_backend(Double(), 'chat', {'sampling': ({'value': 3}, lambda response: False)}, 1, 1)
        assert result.capabilities['sampling'] is False

    asyncio.run(run())


def test_image_probe_requires_distinct_image_dependent_answers():
    async def run():
        class ImageDouble:
            def __init__(self, ignore=False):
                self.ignore = ignore

            async def post(self, endpoint, body, headers):
                url = body['messages'][0]['content'][1]['image_url']['url']
                value = 1 if self.ignore or url == 'data:image/png;base64,AA==' else 2
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                               'message': {'content': '{"value": %d}' % value}}]})

        def body(url):
            return {'messages': [{'content': [{'type': 'text', 'text': 'Read the image'},
                                               {'type': 'image_url', 'image_url': {'url': url}}]}]}

        first = (body('data:image/png;base64,AA=='), 1)
        second = (body('data:image/png;base64,AQ=='), 2)
        result = await probe_local_image_pair(ImageDouble(), 'chat', first, second, 2, 1)
        assert result.status == 'complete' and result.capabilities['image'] is True
        result = await probe_local_image_pair(ImageDouble(ignore=True), 'chat', first, second, 2, 1)
        assert result.capabilities['image'] is False
        result = await probe_local_image_pair(ImageDouble(), 'chat', first, second, 1, 1)
        assert result.status == 'incomplete' and 'image' not in result.capabilities

    asyncio.run(run())


def test_thinking_probe_requires_observable_switch_effect():
    async def run():
        class ThinkingDouble:
            def __init__(self, ignore=False):
                self.ignore = ignore

            async def post(self, endpoint, body, headers):
                value = body['chat_template_kwargs']['configured_switch'] and not self.ignore
                return httpx.Response(200, json={'observed_thinking': value})

        enabled = {'messages': [{'role': 'user', 'content': 'probe'}],
                   'chat_template_kwargs': {'configured_switch': True}}
        disabled = {'messages': [{'role': 'user', 'content': 'probe'}],
                    'chat_template_kwargs': {'configured_switch': False}}
        observe = lambda response: response.json()['observed_thinking']
        result = await probe_local_thinking_pair(ThinkingDouble(), 'chat', enabled, disabled,
                                                 'configured_switch', observe, 2, 1)
        assert result.status == 'complete' and result.capabilities['reasoning.mode'] is True
        result = await probe_local_thinking_pair(ThinkingDouble(ignore=True), 'chat', enabled, disabled,
                                                 'configured_switch', observe, 2, 1)
        assert result.capabilities['reasoning.mode'] is False
        result = await probe_local_thinking_pair(ThinkingDouble(), 'chat', enabled, disabled,
                                                 'configured_switch', lambda response: None, 2, 1)
        assert result.status == 'failed' and 'reasoning.mode' not in result.capabilities

    asyncio.run(run())


def test_probe_timeout_is_failure():
    async def run():
        async def never_returns():
            await asyncio.Event().wait()
        result = await probe_capabilities({'image': never_returns}, 1, 0.01)
        assert result.status == 'failed' and result.errors['image'] == 'TimeoutError'
    asyncio.run(run())
