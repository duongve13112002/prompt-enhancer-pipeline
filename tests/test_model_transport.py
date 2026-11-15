"""In-memory HTTP double; no network or provider calls."""

import asyncio

import httpx
import pytest

from prompt_enhancer.model_resources import BackendConcurrency, BudgetExhausted, SingleRunBudgetLedger
from prompt_enhancer.model_transport import ModelTransport, endpoint_url


@pytest.mark.parametrize('base,route', [('http://example.com', 'chat'), ('http://127.0.0.1', '../x'),
                                        ('https://example.com', '//evil/x'), ('https://example.com', '%2e%2e/x')])
def test_refuse_unsafe_urls(base, route):
    with pytest.raises(ValueError):
        endpoint_url(base, route)


def test_retry_reserves_budget_per_attempt():
    async def run():
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(503 if len(seen) == 1 else 200, json={'ok': True})

        settings = {'provider': 'anthropic', 'base_urls': ['https://example.com/api'],
                    'http': dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                                 max_retries=1, retry_statuses=[503], backoff_initial_s=0, backoff_max_s=0)}
        ledger = SingleRunBudgetLedger({'cloud': 2})
        client = ModelTransport('cloud', settings, BackendConcurrency({'cloud': 1}), ledger,
                                transport=httpx.MockTransport(handler))
        try:
            response = await client.post('messages', {}, {})
            assert response.status_code == 200
            assert seen == ['/api/messages', '/api/messages']
            with pytest.raises(BudgetExhausted):
                await client.post('messages', {}, {})
        finally:
            await client.close()

    asyncio.run(run())


def test_configured_timeouts_and_bounded_backoff(monkeypatch):
    async def run():
        delays, attempts = [], []

        async def sleep(delay):
            delays.append(delay)

        def handler(request):
            attempts.append(request.extensions['timeout'])
            return httpx.Response(503)

        monkeypatch.setattr('prompt_enhancer.model_transport.asyncio.sleep', sleep)
        settings = {'provider': 'openai_compatible', 'base_urls': ['http://127.0.0.1'],
                    'http': dict(connect_timeout_s=2, read_timeout_s=3, write_timeout_s=4, pool_timeout_s=5,
                                 max_retries=3, retry_statuses=[503], backoff_initial_s=0.5, backoff_max_s=0.75)}
        client = ModelTransport('main', settings, BackendConcurrency({'main': 1}), transport=httpx.MockTransport(handler))
        try:
            response = await client.post('chat', {}, {})
            assert response.status_code == 503 and len(attempts) == 4
            assert attempts[0] == {'connect': 2, 'read': 3, 'write': 4, 'pool': 5}
            assert delays == [0.5, 0.75, 0.75]
        finally:
            await client.close()

    asyncio.run(run())


def test_transport_timeout_and_cancellation_release_concurrency():
    async def run():
        entered = asyncio.Event()

        async def handler(request):
            if request.url.path == '/timeout':
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            entered.set()
            await asyncio.Event().wait()

        settings = {'provider': 'openai_compatible', 'base_urls': ['http://127.0.0.1'],
                    'http': dict(connect_timeout_s=2, read_timeout_s=3, write_timeout_s=4, pool_timeout_s=5,
                                 max_retries=0, retry_statuses=[], backoff_initial_s=0, backoff_max_s=0)}
        guard = BackendConcurrency({'main': 1})
        client = ModelTransport('main', settings, guard, transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(httpx.ReadTimeout):
                await client.post('timeout', {}, {})
            task = asyncio.create_task(client.post('wait', {}, {}))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(1):
                async with guard.for_backend('main'):
                    pass
        finally:
            await client.close()

    asyncio.run(run())
