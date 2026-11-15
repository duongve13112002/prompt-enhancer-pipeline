"""Bounded capability evidence supplied by independently verifiable probes."""

import asyncio
import math
import json
from copy import deepcopy
from dataclasses import dataclass

from .model_resources import BudgetExhausted


@dataclass(frozen=True)
class ProbeResult:
    status: str
    capabilities: dict
    errors: dict


async def probe_capabilities(cases, max_requests, timeout_s):
    """Run bounded probe callables; a callable must verify its own response.

    Each case maps a capability name to an async callable returning a strict
    boolean. Missing evidence remains unknown, never inferred from HTTP status.
    """
    if (type(max_requests) is not int or max_requests < 1 or type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError('invalid probe limits')
    capabilities, errors = {}, {}
    for index, (name, case) in enumerate(cases.items()):
        if index >= max_requests:
            break
        try:
            result = await asyncio.wait_for(case(), timeout=timeout_s)
            if type(result) is not bool:
                raise ValueError('probe did not return verified boolean evidence')
            capabilities[name] = result
        except BudgetExhausted:
            raise
        except Exception as error:
            errors[name] = type(error).__name__
    status = 'failed' if errors else ('incomplete' if len(capabilities) < len(cases) else 'complete')
    return ProbeResult(status, capabilities, errors)


def require_capabilities(result, required):
    missing = [name for name in required if result.capabilities.get(name) is not True]
    if missing:
        raise ValueError('unproven required capabilities: ' + ', '.join(sorted(missing)))


async def probe_backend(transport, endpoint, cases, max_requests, timeout_s, *, headers=None):
    """Probe via the backend transport using caller-supplied verifiable cases.

    A case is a body and a verifier; a verifier must inspect the reply and
    return a bool. Transport owns concurrency and any budget reservation.
    """
    async def run_case(body, verifier):
        response = await transport.post(endpoint, body, headers or {})
        if not response.is_success:
            raise RuntimeError(f'probe HTTP {response.status_code}')
        return verifier(response)

    calls = {name: (lambda body=body, verifier=verifier: run_case(body, verifier))
             for name, (body, verifier) in cases.items()}
    return await probe_capabilities(calls, max_requests, timeout_s)


async def probe_local_image_pair(transport, endpoint, first, second, max_requests, timeout_s):
    """Check two image challenges whose only request difference is image data.

    Each challenge is (OpenAI-compatible request body, expected integer). The
    model must return JSON containing that integer in ``value``. Fixtures and
    their expected values are caller-supplied, never user knowledge-base data.
    """
    bodies = [deepcopy(first[0]), deepcopy(second[0])]
    expected = (first[1], second[1])
    if any(type(value) is not int for value in expected) or expected[0] == expected[1]:
        raise ValueError('image challenges require distinct integer answers')
    urls = []
    for body in bodies:
        parts = [part for message in body['messages'] if isinstance(message['content'], list)
                 for part in message['content'] if part.get('type') == 'image_url']
        if len(parts) != 1:
            raise ValueError('each image challenge must contain exactly one image')
        urls.append(parts[0]['image_url']['url'])
        parts[0]['image_url']['url'] = '<image>'
    if urls[0] == urls[1] or bodies[0] != bodies[1]:
        raise ValueError('image challenges must differ only in their image')

    def verifier(expected_value):
        def verify(response):
            try:
                choice = response.json()['choices'][0]
                if choice['finish_reason'] != 'stop' or choice['message'].get('refusal'):
                    return False
                result = json.loads(choice['message']['content'])
                return type(result.get('value')) is int and result['value'] == expected_value
            except (KeyError, IndexError, TypeError, ValueError):
                return False
        return verify

    cases = {'first': (first[0], verifier(expected[0])), 'second': (second[0], verifier(expected[1]))}
    result = await probe_backend(transport, endpoint, cases, max_requests, timeout_s)
    if result.status != 'complete':
        return ProbeResult(result.status, {}, result.errors)
    return ProbeResult('complete', {'image': all(result.capabilities.values())}, result.errors)


async def probe_local_thinking_pair(transport, endpoint, enabled, disabled, template_key,
                                    observe_thinking, max_requests, timeout_s):
    """Probe two requests differing only in the configured thinking switch.

    The caller supplies an observer backed by the configured engine's documented
    response evidence. It must return True, False or None when inconclusive.
    """
    on_body, off_body = deepcopy(enabled), deepcopy(disabled)
    if (on_body.get('chat_template_kwargs', {}).get(template_key) is not True
            or off_body.get('chat_template_kwargs', {}).get(template_key) is not False):
        raise ValueError('thinking challenges require on and off settings')
    on_body['chat_template_kwargs'][template_key] = False
    if on_body != off_body:
        raise ValueError('thinking challenges must differ only by the switch')

    def verify(expected):
        def check(response):
            observed = observe_thinking(response)
            if type(observed) is not bool:
                raise ValueError('thinking evidence is inconclusive')
            return observed is expected
        return check

    result = await probe_backend(transport, endpoint, {'on': (enabled, verify(True)),
                                'off': (disabled, verify(False))}, max_requests, timeout_s)
    if result.status != 'complete':
        return ProbeResult(result.status, {}, result.errors)
    return ProbeResult('complete', {'reasoning.mode': all(result.capabilities.values())}, result.errors)
