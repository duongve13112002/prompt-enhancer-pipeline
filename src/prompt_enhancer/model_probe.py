"""Bounded capability evidence supplied by independently verifiable probes."""

import asyncio
import math
import json
import base64
import hashlib
from io import BytesIO
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from .model_resources import BudgetExhausted
from .model_cloud import cloud_request
from .config import image_media_type
from .model_image import prepare_image


# Artifact-bound pins for the identical one-line bundle in LF and CRLF checkouts.
BUNDLE_ID = 'model_capabilities.v0'
BUNDLE_PATH = Path('contracts/probes/model_capabilities.v0.json')
BUNDLE_PINS = frozenset({
    (1501, '99d84e9f0b4ba1c749b3bb51ed6423f5a6a134bea834fe6a1ac8174bee7f4449'),
    (1502, '868bb69d96c9cf8814b385f680308492410fe9f811654d292684cda3a99ea10d'),
})


class ProbeArtifactError(ValueError):
    """The independently pinned challenge artifact cannot be trusted."""


class ProbeLimitExhausted(RuntimeError):
    """The backend's run-scoped allowance has no send left."""


class ProbeAllowance:
    def __init__(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError('invalid probe allowance')
        self.remaining = limit
        self.sent = 0

    def take(self):
        # No await between the check and debit, including concurrent retry paths.
        if self.remaining == 0:
            raise ProbeLimitExhausted()
        self.remaining -= 1
        self.sent += 1


class ProbeTransport:
    """Charge real transport retries; simple labelled doubles have one send per post."""

    def __init__(self, transport, allowance):
        self.transport = transport
        self.allowance = allowance

    async def post(self, endpoint, body, headers):
        method = getattr(self.transport, 'post_probe', None)
        if method is not None:
            return await method(endpoint, body, headers, self.allowance)
        self.allowance.take()
        return await self.transport.post(endpoint, body, headers)


def load_probe_bundle(project_root):
    """Verify raw bytes against independent pins before parsing or decoding anything."""
    try:
        root = Path(project_root).resolve(strict=True)
        path = (root / BUNDLE_PATH).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError('probe artifact escaped project')
        if any(part.lower() in ('.secrets', 'models', '.git', '.venv') for part in path.relative_to(root).parts):
            raise ValueError('probe artifact resolved into protected storage')
        with path.open('rb') as stream:
            raw = stream.read(max(size for size, _ in BUNDLE_PINS) + 1)
        if (len(raw), hashlib.sha256(raw).hexdigest()) not in BUNDLE_PINS:
            raise ValueError('probe artifact pin mismatch')
        bundle = json.loads(raw)
        if bundle['id'] != BUNDLE_ID:
            raise ValueError('probe identity mismatch')
        images = []
        for fixture in bundle['images']:
            data = base64.b64decode(fixture['data'], validate=True)
            if (len(data) != fixture['size_bytes'] or hashlib.sha256(data).hexdigest() != fixture['sha256']):
                raise ValueError('probe image pin mismatch')
            with Image.open(BytesIO(data)) as image:
                if image.format != fixture['format'] or image.size != (fixture['width'], fixture['height']):
                    raise ValueError('probe image shape mismatch')
                image.verify()
            images.append(data)
        return bundle, tuple(images)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ProbeArtifactError('PROBE_ARTIFACT_INVALID') from error


def verify_probe_reply(response, provider, expected):
    """Check completion, refusal, exact JSON shape, strict integer type and answer."""
    try:
        payload = response.json()
        if provider == 'openai_compatible':
            choice = payload['choices'][0]
            message = choice['message']
            if choice['finish_reason'] != 'stop' or message.get('refusal'):
                return False
            text = message['content']
        elif provider == 'anthropic':
            if payload['stop_reason'] not in ('end_turn', 'stop_sequence'):
                return False
            text = ''.join(part['text'] for part in payload['content'] if part['type'] == 'text')
        else:
            choice = payload['candidates'][0]
            if choice['finishReason'] != 'STOP':
                return False
            text = ''.join(part['text'] for part in choice['content']['parts'] if 'text' in part)
        value = json.loads(text)
        return (type(value) is dict and set(value) == {'value'}
                and type(value['value']) is int and value['value'] == expected)
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def prepare_probe_image(data, max_side, backend, role, vision):
    """The one preparation every probe image gets, as generation prepares a real one.

    `max_side` is the caller's resolved side (`model_client.image_max_side`, decision 144); format, quality, alpha
    policy and decode bound come from the role, vision and backend settings. Returns (media type, bytes).
    """
    policy = role['image']
    encoding = vision['format'] if policy['format'] == 'from_vision' else policy['format']
    media_type = image_media_type(encoding)
    if media_type is None:
        raise ValueError('probe image format has no registered media type')
    prepared = prepare_image(data, max_side, backend['image']['max_decode_pixels'],
                             alpha_policy=vision['alpha_policy'], output_format=encoding, quality=policy['quality'])
    return media_type, prepared


def production_probe_cases(bundle, image_bytes, backend, role, vision, capability, max_side):
    """Build only the demanded challenge; deployment choices come from validated config.

    `max_side` is the effective image side generation would use for this role and backend (never re-derived here).
    """
    provider = backend['provider']
    image_case = capability == 'image' or capability.startswith('image+')
    fmt = capability.partition('+')[2] if '+' in capability else capability
    fmt = fmt if fmt in ('json_object', 'json_schema') else 'none'
    if image_case and provider != 'openai_compatible' and not backend['allow_images']:
        raise ValueError('image probe prohibited by backend')
    schema = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
              'required': ['value'], 'additionalProperties': False}
    challenges = zip(image_bytes, [item['answer'] for item in bundle['images']]) if image_case else (
        (None, bundle['text_answer']),)
    result = {}
    for index, (data, answer) in enumerate(challenges):
        prompt = bundle['image'] if image_case else bundle['text']
        images = []
        if data is not None:
            images = [prepare_probe_image(data, max_side, backend, role, vision)]
        if provider == 'openai_compatible':
            content = prompt if not images else [
                {'type': 'text', 'text': prompt}, *[
                {'type': 'image_url', 'image_url': {'url': 'data:' + mime + ';base64,'
                                                  + base64.b64encode(raw).decode('ascii')}} for mime, raw in images]]
            body = {'model': backend['model']['id'], 'max_tokens': role['max_tokens'],
                    'messages': [{'role': 'user', 'content': content}]}
            if fmt != 'none':
                body['response_format'] = {'type': fmt}
                if fmt == 'json_schema':
                    body['response_format']['json_schema'] = {
                        'name': BUNDLE_ID, 'schema': schema, 'strict': role['output']['strict']}
            body.update(deepcopy(backend.get('extra_body', {})))
        else:
            probe_role = deepcopy(role)
            probe_role['output'] = dict(role['output'], format=fmt)
            # Optional controls without observable proof stay unknown; the call records their warnings.
            body, _ = cloud_request(provider, backend, probe_role, '', prompt, schema=schema, images=images)
        result[str(index)] = (body, lambda response, answer=answer: verify_probe_reply(response, provider, answer))
    return result


@dataclass(frozen=True)
class ProbeResult:
    status: str
    capabilities: dict
    errors: dict
    evidence: tuple = ()


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
        except ProbeLimitExhausted:
            return ProbeResult('incomplete', capabilities, {**errors, name: 'PROBE_INCOMPLETE'})
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
            return verify_probe_reply(response, 'openai_compatible', expected_value)
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


def multiple_images_plan(model_id, max_tokens, prepare):
    """Production challenge: count one/two circles and combine both counts in their input order.

    `prepare` maps the raw fixture PNG to (media type, bytes) exactly as generation prepares a real image
    (`prepare_probe_image`), so the proof is made on the pixels the model will receive.
    """
    # Protocol-bound challenge geometry/answers, independent of deployment quality settings.
    urls = []
    for count in (1, 2):
        with Image.new('RGB', (96, 64), 'white') as image:
            draw = ImageDraw.Draw(image)
            for index in range(count):
                draw.ellipse((8 + 44 * index, 16, 36 + 44 * index, 44), fill='black')
            output = BytesIO()
            image.save(output, format='PNG')
        media_type, prepared = prepare(output.getvalue())
        urls.append('data:' + media_type + ';base64,' + base64.b64encode(prepared).decode('ascii'))
    prompt = ('Count the black circles in each image. Return only JSON {"value": N}, '
              'where N is ten times the count in the first image plus the count in the second image.')

    def body(images):
        return {'model': model_id, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            *[{'type': 'image_url', 'image_url': {'url': url}} for url in images]]}]}

    return (body(urls), 12), (body(list(reversed(urls))), 21)


async def probe_local_multiple_images_pair(transport, endpoint, first, second, max_requests, timeout_s):
    """Prove ordered two-image input with two independently checked, image-dependent answers.

    Challenges are caller-supplied `(body, expected_integer)` pairs as for the single-image probe. Each body
    contains exactly two distinct images and the second swaps their order without changing text or settings.
    """
    bodies = [deepcopy(first[0]), deepcopy(second[0])]
    expected = (first[1], second[1])
    if any(type(value) is not int for value in expected) or expected[0] == expected[1]:
        raise ValueError('multi-image challenges require distinct integer answers')
    pairs = []
    for body in bodies:
        parts = [part for message in body['messages'] if isinstance(message['content'], list)
                 for part in message['content'] if part.get('type') == 'image_url']
        if len(parts) != 2:
            raise ValueError('each multi-image challenge must contain exactly two images')
        pairs.append([part['image_url']['url'] for part in parts])
        for part in parts:
            part['image_url']['url'] = '<image>'
    if (pairs[0][0] == pairs[0][1] or pairs[1] != list(reversed(pairs[0])) or bodies[0] != bodies[1]):
        raise ValueError('multi-image challenges must differ only by swapping two distinct images')

    def verifier(answer):
        def verify(response):
            return verify_probe_reply(response, 'openai_compatible', answer)
        return verify

    cases = {'first': (first[0], verifier(expected[0])), 'second': (second[0], verifier(expected[1]))}
    result = await probe_backend(transport, endpoint, cases, max_requests, timeout_s)
    evidence = tuple({'challenge': name, 'status':
        ('complete' if result.capabilities[name] else 'negative') if name in result.capabilities else
        'failed' if name in result.errors and result.errors[name] != 'PROBE_INCOMPLETE' else 'incomplete'}
        for name in cases)
    if result.status != 'complete':
        return ProbeResult(result.status, {}, result.errors, evidence)
    return ProbeResult('complete', {'multiple_images': all(result.capabilities.values())}, result.errors, evidence)
