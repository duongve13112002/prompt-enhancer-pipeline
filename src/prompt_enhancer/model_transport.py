"""Bounded HTTP transport for configured model backends."""

import asyncio
from urllib.parse import urlsplit, urlunsplit
import ipaddress

import httpx


def endpoint_url(base, endpoint):
    """Join an explicit relative route to a secure or loopback base URL."""
    parsed = urlsplit(base)
    route = urlsplit(endpoint)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or route.scheme or route.netloc or route.query or route.fragment
            or not endpoint or endpoint.startswith('/') or any(p in ('.', '..') for p in route.path.split('/'))
            or '\\' in endpoint or '%' in endpoint):
        raise ValueError('invalid backend base URL or relative endpoint')
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == 'localhost'
    if parsed.scheme != 'https' and not loopback:
        raise ValueError('non-loopback backend requires HTTPS')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/') + '/' + route.path, '', ''))


class ModelTransport:
    def __init__(self, backend, settings, concurrency, ledger=None, *, transport=None):
        self.backend = backend
        self.settings = settings
        self.concurrency = concurrency
        self.ledger = ledger
        if settings['provider'] != 'openai_compatible' and ledger is None:
            raise ValueError('cloud backend requires a budget ledger')
        http = settings['http']
        timeout = httpx.Timeout(connect=http['connect_timeout_s'], read=http['read_timeout_s'],
                                write=http['write_timeout_s'], pool=http['pool_timeout_s'])
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False,
                                        trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def post(self, route, body, headers):
        http = self.settings['http']
        url = endpoint_url(self.settings['base_urls'][0], route)
        async with self.concurrency.for_backend(self.backend):
            for attempt in range(http['max_retries'] + 1):
                token = None
                if self.settings['provider'] != 'openai_compatible':
                    token = await self.ledger.reserve(self.backend)
                try:
                    if token is not None:
                        # A cancellation at this boundary must not return a grant
                        # while an attempt might already be sent.
                        await asyncio.shield(self.ledger.commit(token))
                    response = await self.client.post(url, json=body, headers=headers)
                except httpx.TransportError:
                    if attempt == http['max_retries']:
                        raise
                else:
                    if response.status_code not in http['retry_statuses'] or attempt == http['max_retries']:
                        return response
                await asyncio.sleep(min(http['backoff_initial_s'] * (2 ** attempt), http['backoff_max_s']))
