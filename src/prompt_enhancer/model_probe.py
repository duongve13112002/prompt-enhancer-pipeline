"""Bounded capability evidence supplied by independently verifiable probes."""

import asyncio
import math
import json
import base64
import hashlib
from io import BytesIO
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from .model_resources import BudgetExhausted
from .model_cloud import cloud_request
from .model_mapping import openai_request
from .config import image_media_type
from .model_image import prepare_image
from .model_json import strict_json


# Artifact-bound pins for the identical one-line bundle in LF and CRLF checkouts.
BUNDLE_ID = 'model_capabilities.v0'
BUNDLE_PATH = Path('contracts/probes/model_capabilities.v0.json')
BUNDLE_PINS = frozenset({
    (1501, '99d84e9f0b4ba1c749b3bb51ed6423f5a6a134bea834fe6a1ac8174bee7f4449'),
    (1502, '868bb69d96c9cf8814b385f680308492410fe9f811654d292684cda3a99ea10d'),
})


class ProbeArtifactError(ValueError):
    """The independently pinned challenge artifact cannot be trusted."""


class ProbeHttpError(RuntimeError):
    """A probe request answered with a non-2xx status; `status` is the closed integer, never the body."""

    def __init__(self, status):
        super().__init__(f'probe HTTP {status}')
        self.status = status


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


# Protocol-bound verdict codes; never retain response text in a verdict.
PROBE_CODES = frozenset(('PROBE_TRUNCATED', 'PROBE_REFUSED', 'PROBE_MALFORMED',
                         'PROBE_WRONG_ANSWER', 'PROBE_INCOMPLETE', 'PROBE_FAILED'))
PROBE_SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                'required': ['value'], 'additionalProperties': False}  # artifact-bound challenge shape


@dataclass(frozen=True)
class ProbeVerdict:
    success: bool
    code: str | None = None
    # SHA-256 of the reply text when one was read (decision 188 attempt record); never the text, never compared.
    digest: str | None = field(default=None, compare=False)

    def __post_init__(self):
        if type(self.success) is not bool or (self.success and self.code is not None) or (
                not self.success and (type(self.code) is not str or self.code not in PROBE_CODES)):
            raise ValueError('invalid structural probe verdict')


def probe_verdict(response, provider, expected):
    """Classify only protocol structure, then exact JSON shape and integer answer."""
    digest = None
    try:
        payload = response.json()
        if type(payload) is not dict:
            return ProbeVerdict(False, 'PROBE_MALFORMED')
        if provider == 'openai_compatible':
            choices = payload['choices']
            if type(choices) is not list or not choices or type(choices[0]) is not dict:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            choice = choices[0]
            message = choice['message']
            finish = choice['finish_reason']
            if type(message) is not dict or type(finish) is not str:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            refusal = message.get('refusal')
            if refusal is not None and type(refusal) is not str:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            if refusal or finish == 'content_filter':
                return ProbeVerdict(False, 'PROBE_REFUSED')
            if finish == 'length':
                return ProbeVerdict(False, 'PROBE_TRUNCATED')
            if finish != 'stop':
                return ProbeVerdict(False, 'PROBE_INCOMPLETE')
            text = message['content']
        elif provider == 'anthropic':
            finish = payload['stop_reason']
            if type(finish) is not str:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            if finish == 'refusal':
                return ProbeVerdict(False, 'PROBE_REFUSED')
            if finish == 'max_tokens':
                return ProbeVerdict(False, 'PROBE_TRUNCATED')
            if finish not in ('end_turn', 'stop_sequence'):
                return ProbeVerdict(False, 'PROBE_INCOMPLETE')
            parts = payload['content']
            if type(parts) is not list or any(type(part) is not dict or type(part.get('type')) is not str
                                               for part in parts):
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            text = ''.join(part['text'] for part in parts if part['type'] == 'text')
        elif provider == 'gemini':
            feedback = payload.get('promptFeedback', {})
            if type(feedback) is not dict:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            block_reason = feedback.get('blockReason', '')
            if type(block_reason) is not str:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            if block_reason:
                return ProbeVerdict(False, 'PROBE_REFUSED')
            candidates = payload['candidates']
            if type(candidates) is not list or not candidates or type(candidates[0]) is not dict:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            choice = candidates[0]
            finish = choice['finishReason']
            if type(finish) is not str:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            if finish == 'SAFETY':
                return ProbeVerdict(False, 'PROBE_REFUSED')
            if finish == 'MAX_TOKENS':
                return ProbeVerdict(False, 'PROBE_TRUNCATED')
            if finish != 'STOP':
                return ProbeVerdict(False, 'PROBE_INCOMPLETE')
            content = choice['content']
            if type(content) is not dict:
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            parts = content['parts']
            if type(parts) is not list or any(type(part) is not dict for part in parts):
                return ProbeVerdict(False, 'PROBE_MALFORMED')
            text = ''.join(part['text'] for part in parts if 'text' in part)
        else:
            return ProbeVerdict(False, 'PROBE_MALFORMED')
        if type(text) is not str:
            return ProbeVerdict(False, 'PROBE_MALFORMED')
        digest = hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()
        value = strict_json(text)
        if type(value) is not dict or set(value) != {'value'} or type(value['value']) is not int:
            return ProbeVerdict(False, 'PROBE_MALFORMED', digest)
        return (ProbeVerdict(True, digest=digest) if value['value'] == expected
                else ProbeVerdict(False, 'PROBE_WRONG_ANSWER', digest))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return ProbeVerdict(False, 'PROBE_MALFORMED', digest)


def verify_probe_reply(response, provider, expected):
    """Compatibility boolean adapter for explicitly supplied verification seams."""
    return probe_verdict(response, provider, expected).success


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


def probe_image_hashes(body, provider):
    """Artifact provenance of final prepared payloads, including any engine-extra override."""
    hashes = []
    messages = body.get('messages', [])
    for message in messages if isinstance(messages, list) else ():
        content = message.get('content') if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else ():
            if not isinstance(part, dict):
                continue
            encoded = None
            if provider == 'openai_compatible' and part.get('type') == 'image_url':
                image = part.get('image_url')
                url = image.get('url') if isinstance(image, dict) else None
                if isinstance(url, str) and url.startswith('data:') and ';base64,' in url:
                    encoded = url.partition(';base64,')[2]
            elif provider == 'anthropic' and part.get('type') == 'image':
                source = part.get('source')
                if isinstance(source, dict) and source.get('type') == 'base64':
                    encoded = source.get('data')
            if not isinstance(encoded, str):
                continue
            try:
                hashes.append(hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest())
            except ValueError:
                # An invalid overridden payload cannot gain provenance from the original fixture.
                continue
    return hashes


def probe_image_challenges(project_root):
    """How many image challenges the probe bundle holds, read without decoding them, or None when it cannot be read
    (the profile loader then leaves the request arithmetic to the runtime)."""
    try:
        with (Path(project_root) / BUNDLE_PATH).open('rb') as stream:
            count = len(json.loads(stream.read(max(size for size, _ in BUNDLE_PINS) + 1))['images'])
        return count if count > 0 else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


# Safety-bound (CLOUD-2): `max_tokens` of a probe request whose reply is never read. The grammar-size refusal of the real
# planner schema was seen at 16 (experiment, claude-haiku-5-5, 2026-11-20), and a larger value only costs output tokens.
PROBE_COMPILE_MAX_TOKENS = 16


def _compile_max_tokens(role):
    """The small limit of a probe whose reply is not read; the role's own when thinking is on, because the documented
    thinking budget must stay below `max_tokens`."""
    return role['max_tokens'] if role['reasoning']['mode'] == 'on' else min(role['max_tokens'], PROBE_COMPILE_MAX_TOKENS)


def accepted_verdict(response):
    """A reply that arrived as a JSON object; the request was accepted, whatever the `stop_reason` (CLOUD-2)."""
    try:
        payload = response.json()
    except ValueError:
        return ProbeVerdict(False, 'PROBE_MALFORMED')
    return ProbeVerdict(True) if type(payload) is dict else ProbeVerdict(False, 'PROBE_MALFORMED')


def production_probe_cases(bundle, image_bytes, backend, role, vision, capability, max_side, *, warnings=None,
                           seed=None, real_schema=None):
    """Build only the demanded challenge; deployment choices come from validated config.

    `max_side` is the effective image side generation would use for this role and backend (never re-derived here).
    `seed` replaces `sampling.seed` in every body (a probe retry, decision 188); nothing else changes.
    `real_schema` (CLOUD-2) is the role's registered reply schema, for an Anthropic role with `json_schema` output:
    on `output.transport: tool_forced` the json_schema probe is one forced-tool request carrying it, and on
    `response_format` the `json_schema` capability gains a `schema` challenge that posts it as the sent schema. Both
    pass on an accepted request, so an API refusal of the real schema shows at start, not at the first real call.
    """
    provider = backend['provider']
    image_case = capability == 'image' or capability.startswith('image+')
    fmt = capability.partition('+')[2] if '+' in capability else capability
    fmt = fmt if fmt in ('json_object', 'json_schema') else 'none'
    if image_case and provider != 'openai_compatible' and not backend['allow_images']:
        raise ValueError('image probe prohibited by backend')
    tool_forced = fmt == 'json_schema' and role['output'].get('transport') == 'tool_forced'
    if tool_forced and (provider != 'anthropic' or real_schema is None):
        raise ValueError('a tool_forced probe needs an anthropic backend and the real schema')
    challenges = zip(image_bytes, [item['answer'] for item in bundle['images']]) if image_case else (
        (None, bundle['text_answer']),)
    if tool_forced:
        challenges = list(challenges)[:1]  # one request: the real tool schema, with the image when the capability has one
    result = {}
    for index, (data, answer) in enumerate(challenges):
        prompt = bundle['image'] if image_case else bundle['text']
        images = []
        if data is not None:
            images = [prepare_probe_image(data, max_side, backend, role, vision)]
        probe_role = deepcopy(role)
        probe_role['output'] = dict(role['output'], format=fmt, schema_id=BUNDLE_ID)
        if role['output'].get('transport') == 'tool_forced' and not tool_forced:
            probe_role['output']['transport'] = 'response_format'  # the text probe of a tool_forced role is plain text
        if seed is not None:
            probe_role['sampling']['seed'] = seed
        if tool_forced:
            probe_role['max_tokens'] = _compile_max_tokens(role)
        if provider == 'openai_compatible':
            body, dropped = openai_request(backend, probe_role, None, prompt, schema=PROBE_SCHEMA,
                                           images=images, mapped=True)
        else:
            body, dropped = cloud_request(provider, backend, probe_role, '', prompt,
                                          schema=real_schema if tool_forced else PROBE_SCHEMA, images=images)
        if warnings is not None:
            warnings.extend(item for item in dropped if item not in warnings)
        verdict = accepted_verdict if tool_forced else (
            lambda response, answer=answer: probe_verdict(response, provider, answer))
        result[str(index)] = (body, verdict)
    if provider == 'anthropic' and capability == 'json_schema' and real_schema is not None and not tool_forced:
        probe_role = deepcopy(role)
        probe_role['max_tokens'] = _compile_max_tokens(role)
        if seed is not None:
            probe_role['sampling']['seed'] = seed
        body, dropped = cloud_request(provider, backend, probe_role, '', bundle['text'], schema=real_schema)
        if warnings is not None:
            warnings.extend(item for item in dropped if item not in warnings)
        result['schema'] = (body, accepted_verdict)
    return result


@dataclass(frozen=True)
class ProbeResult:
    status: str
    capabilities: dict
    errors: dict
    evidence: tuple = ()
    digests: dict = field(default_factory=dict)  # case name -> reply digest of the last verdict that carried one
    http_status: dict = field(default_factory=dict)  # case name -> HTTP status of a refused request (CLOUD-2)


def summarize_probe_group(result, names, capability):
    """Normalize case evidence and decide the outcome of one required capability."""
    cases = []
    for name in names:
        reason = result.errors.get(name)
        if reason == 'PROBE_INCOMPLETE' or (reason is None and name not in result.capabilities):
            status, reason = 'incomplete', 'PROBE_INCOMPLETE'
        elif reason == 'PROBE_FAILED' or name not in result.capabilities:
            status, reason = 'failed', 'PROBE_FAILED'
        else:
            status = 'complete' if result.capabilities[name] else 'negative'
        case = {'challenge': name, 'status': status}
        if status == 'failed' and name == 'schema' and name in result.http_status:
            # CLOUD-2: only the real-schema challenge names its HTTP status (a closed integer); the events of every
            # other challenge keep their shape, which existing tests pin.
            case['http_status'] = result.http_status[name]
        if reason in PROBE_CODES:
            case['reason'] = reason
        cases.append(case)

    statuses = {case['status'] for case in cases}
    if 'failed' in statuses:
        return ProbeResult('failed', {}, {capability: 'PROBE_FAILED'}, tuple(cases))
    if not cases or 'incomplete' in statuses:
        return ProbeResult('incomplete', {}, {capability: 'PROBE_INCOMPLETE'}, tuple(cases))
    reason = next((case['reason'] for case in cases if case['status'] == 'negative' and 'reason' in case), None)
    return ProbeResult('complete', {capability: 'negative' not in statuses},
                       {capability: reason} if reason else {}, tuple(cases))


async def probe_capabilities(cases, max_requests, timeout_s):
    """Run bounded probe callables; a callable must verify its own response.

    Each case maps a capability name to an async callable returning a strict
    boolean. Missing evidence remains unknown, never inferred from HTTP status.
    """
    if (type(max_requests) is not int or max_requests < 1 or type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError('invalid probe limits')
    capabilities, errors, digests = {}, {}, {}
    failed, incomplete = False, False
    statuses = {}
    for index, (name, case) in enumerate(cases.items()):
        if index >= max_requests:
            break
        try:
            result = await asyncio.wait_for(case(), timeout=timeout_s)
            if isinstance(result, ProbeVerdict):
                if result.digest:
                    digests[name] = result.digest
                if result.code == 'PROBE_INCOMPLETE':
                    incomplete = True
                else:
                    capabilities[name] = result.success
                if result.code:
                    errors[name] = result.code
                continue
            if type(result) is not bool:
                raise ValueError('probe did not return verified boolean evidence')
            capabilities[name] = result
        except BudgetExhausted:
            raise
        except ProbeLimitExhausted:
            return ProbeResult('incomplete', capabilities, {**errors, name: 'PROBE_INCOMPLETE'}, digests=digests)
        except Exception as error:
            failed = True
            errors[name] = 'RuntimeError' if isinstance(error, ProbeHttpError) else type(error).__name__
            if isinstance(error, ProbeHttpError):
                statuses[name] = error.status
    status = 'failed' if failed else ('incomplete' if incomplete or len(capabilities) < len(cases) else 'complete')
    return ProbeResult(status, capabilities, errors, digests=digests, http_status=statuses)


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
            raise ProbeHttpError(response.status_code)
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
            return probe_verdict(response, 'openai_compatible', expected_value)
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


def multiple_images_plan(model_id, max_tokens, prepare, *, build=None):
    """Production challenge: count one/two circles and combine both counts in their input order.

    `prepare` maps the raw fixture PNG to (media type, bytes) exactly as generation prepares a real image
    (`prepare_probe_image`), so the proof is made on the pixels the model will receive.
    """
    # Protocol-bound challenge geometry/answers, independent of deployment quality settings.
    urls = []
    prepared_images = []
    for count in (1, 2):
        with Image.new('RGB', (96, 64), 'white') as image:
            draw = ImageDraw.Draw(image)
            for index in range(count):
                draw.ellipse((8 + 44 * index, 16, 36 + 44 * index, 44), fill='black')
            output = BytesIO()
            image.save(output, format='PNG')
        media_type, prepared = prepare(output.getvalue())
        prepared_images.append((media_type, prepared))
        urls.append('data:' + media_type + ';base64,' + base64.b64encode(prepared).decode('ascii'))
    prompt = ('Count the black circles in each image. Return only JSON {"value": N}, '
              'where N is ten times the count in the first image plus the count in the second image.')

    def body(images):
        return {'model': model_id, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            *[{'type': 'image_url', 'image_url': {'url': url}} for url in images]]}]}

    if build is not None:
        return (build(prompt, prepared_images), 12), (build(prompt, list(reversed(prepared_images))), 21)
    return (body(urls), 12), (body(list(reversed(urls))), 21)


def multiple_images_probe_cases(first, second):
    """Validate the final ordered bodies before constructing response verifiers.

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
            return probe_verdict(response, 'openai_compatible', answer)
        return verify

    return {'first': (first[0], verifier(expected[0])), 'second': (second[0], verifier(expected[1]))}


async def probe_local_multiple_images_pair(transport, endpoint, first, second, max_requests, timeout_s):
    """Run caller-supplied ordered challenges after validating their controlled difference."""
    cases = multiple_images_probe_cases(first, second)
    result = await probe_backend(transport, endpoint, cases, max_requests, timeout_s)
    return summarize_probe_group(result, cases, 'multiple_images')
