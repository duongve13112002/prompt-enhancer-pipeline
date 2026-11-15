"""In-memory HTTP double; no network or provider calls."""

import asyncio
from fractions import Fraction
import sys

import httpx
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
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


# T-008 fix round 5 (N1): capped backoff without the unbounded product 2**attempt.
FLOAT_MAX = sys.float_info.max  # test-only oracle: the largest finite binary64 value
LARGEST_TIMER = 2 ** 1024 - 2 ** 970 - 1  # test-only oracle: the largest int float() converts (T-008 timer_probe)


def retry_delays(max_retries, initial, cap, monkeypatch=None):
    """One post against a labelled HTTP double that always answers 503: the response, the sleeps, the requests.
    With `monkeypatch`, asyncio.sleep is replaced by a labelled recording double, so a large delay is not awaited."""
    delays, attempts = [], []

    async def recording_sleep(delay):
        delays.append(delay)

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    async def run():
        settings = {'provider': 'openai_compatible', 'base_urls': ['http://127.0.0.1'],
                    'http': dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                                 max_retries=max_retries, retry_statuses=[503], backoff_initial_s=initial,
                                 backoff_max_s=cap)}
        client = ModelTransport('main', settings, BackendConcurrency({'main': 1}),
                                transport=httpx.MockTransport(handler))
        try:
            return await client.post('chat', {}, {})
        finally:
            await client.close()

    if monkeypatch is not None:
        monkeypatch.setattr('prompt_enhancer.model_transport.asyncio.sleep', recording_sleep)
    return asyncio.run(run()), delays, attempts


def expected_delays(initial, cap, count):
    """Exact oracle: min(initial * 2**k, cap) in rational arithmetic, so the oracle itself cannot overflow."""
    return [Fraction(cap) if Fraction(initial) * 2 ** k > Fraction(cap) else Fraction(initial) * 2 ** k
            for k in range(count)]


@pytest.mark.parametrize(('initial', 'cap'), [(0.0, 0.0), (0, 0), (5e-324, 1e-9)])
def test_a_long_retry_sequence_with_zero_or_tiny_backoff_finishes_with_real_sleep(initial, cap):
    """Retest 4 (N1): 1025 retries with float zero backoffs raised OverflowError at attempt 1024."""
    response, _, attempts = retry_delays(1025, initial, cap)
    assert response.status_code == 503 and len(attempts) == 1026


@pytest.mark.parametrize(('initial', 'cap'), [(0.0, 0.0), (5e-324, 5e-324), (1.0, FLOAT_MAX), (FLOAT_MAX, FLOAT_MAX),
                                              (0.5, 0.75), (1, LARGEST_TIMER), (1.0, LARGEST_TIMER), (3, 10 ** 300)])
@pytest.mark.parametrize('max_retries', [1024, 1025, 1100])
def test_capped_backoff_past_the_float_exponent_limit(monkeypatch, max_retries, initial, cap):
    response, delays, attempts = retry_delays(max_retries, initial, cap, monkeypatch)
    assert response.status_code == 503 and len(attempts) == max_retries + 1
    assert [Fraction(delay) for delay in delays] == expected_delays(initial, cap, max_retries)


TIMER = st.one_of(st.floats(min_value=0, max_value=FLOAT_MAX, allow_nan=False, allow_infinity=False),
                  st.integers(min_value=0, max_value=LARGEST_TIMER))


@hsettings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(max_retries=st.integers(min_value=0, max_value=1100), pair=st.tuples(TIMER, TIMER))
def test_backoff_delays_are_the_capped_doubling_for_any_valid_pair(monkeypatch, max_retries, pair):
    initial, cap = sorted(pair)  # the loader keeps backoff_initial_s <= backoff_max_s
    response, delays, attempts = retry_delays(max_retries, initial, cap, monkeypatch)
    assert response.status_code == 503 and len(attempts) == max_retries + 1
    assert [Fraction(delay) for delay in delays] == expected_delays(initial, cap, max_retries)
