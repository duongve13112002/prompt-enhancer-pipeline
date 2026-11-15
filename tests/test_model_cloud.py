"""Synthetic credentials and cloud request fragments; no network calls."""

import pytest
import asyncio
import httpx

from prompt_enhancer.model_cloud import cloud_headers, cloud_request
from prompt_enhancer.model_resources import BackendConcurrency, SingleRunBudgetLedger
from prompt_enhancer.model_transport import ModelTransport


def test_cloud_headers_use_synthetic_credentials(tmp_path):
    root = tmp_path / '.secrets'
    root.mkdir()
    (root / 'fake.key').write_text('fake-token', encoding='utf-8')
    backend = {'api_key_file': '.secrets/fake.key', 'anthropic_version': '2025-01-01', 'anthropic_workspace_id': 'none'}
    assert cloud_headers('anthropic', backend, tmp_path) == {
        'Authorization': 'Bearer fake-token', 'anthropic-version': '2025-01-01'}
    assert cloud_headers('gemini', backend, tmp_path) == {'x-goog-api-key': 'fake-token'}
    backend['anthropic_version'] = 'deploy_required'
    with pytest.raises(ValueError):
        cloud_headers('anthropic', backend, tmp_path)
    backend['anthropic_version'] = '2025-01-01'
    backend['anthropic_workspace_id'] = 'deploy_required'
    with pytest.raises(ValueError, match='anthropic_workspace_id'):
        cloud_headers('anthropic', backend, tmp_path)


@pytest.mark.parametrize(('provider', 'workspace'), [('anthropic', 'none'), ('anthropic', 'workspace-123'), ('gemini', 'none')])
def test_exact_cloud_request_headers_redacted(provider, workspace, tmp_path):
    async def run():
        directory = tmp_path / '.secrets'
        directory.mkdir()
        (directory / 'fake.key').write_text('synthetic-key', encoding='utf-8')
        backend = {'provider': provider, 'api_key_file': '.secrets/fake.key',
                   'anthropic_version': '2025-01-01', 'anthropic_workspace_id': workspace, 'base_urls': ['https://example.invalid'],
                   'http': dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1,
                                pool_timeout_s=1, max_retries=0, retry_statuses=[],
                                backoff_initial_s=0, backoff_max_s=0)}
        observed = []

        def handler(request):
            names = ('authorization', 'x-api-key', 'x-goog-api-key', 'anthropic-version', 'anthropic-workspace-id', 'content-type')
            selected = {name: request.headers[name] for name in names if name in request.headers}
            expected = ({'authorization': 'Bearer synthetic-key', 'anthropic-version': '2025-01-01',
                         'content-type': 'application/json'} if provider == 'anthropic' else
                        {'x-goog-api-key': 'synthetic-key', 'content-type': 'application/json'})
            if provider == 'anthropic' and workspace != 'none':
                expected['anthropic-workspace-id'] = 'workspace-123'
            observed.append(selected == expected)
            return httpx.Response(200)

        transport = ModelTransport('cloud', backend, BackendConcurrency({'cloud': 1}),
                                   SingleRunBudgetLedger({'cloud': 1}), transport=httpx.MockTransport(handler))
        try:
            headers = cloud_headers(provider, backend, tmp_path)
            route = 'messages' if provider == 'anthropic' else 'v1beta/models/test:generateContent'
            await transport.post(route, {}, headers)
        finally:
            await transport.close()
        assert observed == [True], 'request headers differ (credential redacted)'

    asyncio.run(run())


def test_cloud_text_request_and_unverified_structured_output():
    role = {'max_tokens': 20, 'sampling': {'temperature': 0.3, 'stop': 'none'},
            'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
            'output': {'format': 'none', 'strict': True}}
    backend = {'model': {'id': 'fake'}}
    body, warnings = cloud_request('anthropic', backend, role, 'system', 'user')
    assert body['max_tokens'] == 20 and body['system'] == 'system'
    assert ('PARAM_UNSUPPORTED', 'sampling.temperature') in warnings
    body, warnings = cloud_request('gemini', backend, role, 'system', 'user')
    assert body['generationConfig']['temperature'] == 0.3 and not warnings
    body, _ = cloud_request('anthropic', backend, role, 'system', 'user', images=[('image/png', b'fake')])
    assert body['messages'][0]['content'][1]['source']['data'] == 'ZmFrZQ=='
    with pytest.raises(ValueError, match='image field members'):
        cloud_request('gemini', backend, role, 'system', 'user', images=[('image/png', b'fake')])
    role['output']['format'] = 'json_schema'
    with pytest.raises(ValueError, match='unverified|not yet verified'):
        cloud_request('gemini', backend, role, 'system', 'user')


@pytest.mark.parametrize('budget', [0, 1023, 2048, 'none'])
def test_anthropic_invalid_thinking_budget_refused(budget):
    role = {'max_tokens': 2048, 'sampling': {}, 'reasoning': {'mode': 'on', 'effort': 'none',
            'budget_tokens': budget}, 'output': {'format': 'none', 'strict': False}}
    with pytest.raises(ValueError, match='thinking budget'):
        cloud_request('anthropic', {'model': {'id': 'fake'}}, role, 'system', 'user')


def test_anthropic_valid_thinking_budget_is_sent():
    role = {'max_tokens': 2048, 'sampling': {}, 'reasoning': {'mode': 'on', 'effort': 'none',
            'budget_tokens': 1024}, 'output': {'format': 'none', 'strict': False}}
    body, _ = cloud_request('anthropic', {'model': {'id': 'fake'}}, role, 'system', 'user')
    assert body['thinking'] == {'type': 'enabled', 'budget_tokens': 1024}
