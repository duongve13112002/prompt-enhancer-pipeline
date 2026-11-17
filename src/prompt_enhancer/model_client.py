"""Role-bound model calls with explicit capability evidence."""

import hashlib
import json
import time
import base64
import asyncio
import math
from copy import deepcopy

from jsonschema.exceptions import ValidationError

from .config import (ConfigurationError, NO_MEDIA_TYPE, _sentinel, image_media_type, resolve_backend_engine,
                     resolve_writer_format)
from .model_cloud import cloud_headers, cloud_request
from .model_mapping import map_generation_fields
from .model_image import configure_decode_backstop, prepare_image
from .model_probe import (ProbeResult, probe_backend, probe_local_image_pair, probe_local_thinking_pair,
                         probe_local_multiple_images_pair, multiple_images_plan,
                         require_capabilities, ProbeAllowance, ProbeTransport, ProbeArtifactError,
                         load_probe_bundle, production_probe_cases, prepare_probe_image, BUNDLE_ID,
                         ProbeLimitExhausted)
from .model_resources import BackendConcurrency, BudgetExhausted, SingleRunBudgetLedger
from .model_schemas import cached_registry
from .model_transport import ModelTransport
from pathlib import Path


# Protocol-bound public failure codes; never expose arbitrary exception/model text.
MODEL_CODES = frozenset(('MODEL_CALL_FAILED', 'MODEL_CALL_INVALID', 'CAPABILITY_UNAVAILABLE',
                        'CONFIGURATION_INVALID', 'INPUT_INVALID', 'CLIENT_STATE_INVALID', 'IMAGE_INVALID',
                        'BUDGET_EXHAUSTED', 'HTTP_ERROR', 'INCOMPLETE_OR_REFUSED', 'SCHEMA_INVALID',
                        'PROBE_CASES_MISSING', 'PROBE_ARTIFACT_INVALID', 'PROBE_INCOMPLETE', 'PROBE_FAILED'))


def safe_model_code(code):
    return code if type(code) is str and code in MODEL_CODES else 'MODEL_CALL_FAILED'


def safe_probe_events(events):
    """Only protocol labels and nonnegative counters may reach failure diagnostics."""
    result = []
    roles = ('info_assessor', 'folder_namer', 'observer', 'card_builder', 'alias_generator',
             'planner', 'writer', 'verifier', 'manifest_drafter', 'scenario_generator')
    capabilities = CAPABILITIES | {'image+json_object', 'image+json_schema'}
    for event in events if isinstance(events, (list, tuple)) else ():
        if (not isinstance(event, dict) or type(event.get('role')) is not str or event['role'] not in roles
                or type(event.get('capability')) is not str or event['capability'] not in capabilities
                or type(event.get('status')) is not str
                or event.get('status') not in ('complete', 'incomplete', 'failed', 'negative')
                or type(event.get('sent')) is not int or event['sent'] < 0):
            continue
        item = {key: event[key] for key in ('role', 'capability', 'status', 'sent')}
        challenges = event.get('challenges', [])
        if isinstance(challenges, list):
            item['challenges'] = [
                {'challenge': case['challenge'], 'status': case['status']}
                for case in challenges if isinstance(case, dict)
                and type(case.get('challenge')) is str and case['challenge'] in ('0', '1', 'first', 'second')
                and type(case.get('status')) is str
                and case['status'] in ('complete', 'incomplete', 'failed', 'negative')]
        if type(event.get('reason')) is str and event['reason'] in MODEL_CODES:
            item['reason'] = event['reason']
        result.append(item)
    return result


class ModelCallError(RuntimeError):
    """A request could not be completed safely."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = safe_model_code(code if code is not None else str(message).partition(':')[0])
        self.probe_evidence = []


class ProbeCasesMissing(ModelCallError):
    """The backend was composed without verifiable probe cases (the production gap KBS-F1 closes); a type of its
    own so a test can name exactly this condition instead of every ModelCallError."""


# Protocol-bound capability names consumed by the client and generation mapper.
CAPABILITIES = frozenset(('text', 'image', 'multiple_images', 'json_object', 'json_schema',
                          'reasoning.mode', 'reasoning.effort', 'reasoning.budget_tokens',
                          *(f'sampling.{name}' for name in ('temperature', 'top_p', 'top_k', 'min_p',
                            'frequency_penalty', 'presence_penalty', 'repetition_penalty', 'seed', 'stop'))))


# Protocol-bound (T-014): the image roles a call may carry for each `vlm.roles.<name>.image.source` value; the
# schema enum (vlm_role.v0) names the same keys. An image role outside its set is refused before any request.
IMAGE_SOURCE_ROLES = {'source_image': ('source_image',), 'user_reference': ('user_reference',),
                      'knowledge_reference': ('knowledge_reference',), 'edit_source_only': ('source_image',),
                      'edit_source_and_live_look': ('source_image', 'knowledge_reference')}
NO_LIMIT = 'none'  # protocol-bound profile value: no longest-side limit (decision 84)


def image_max_side(config, role_image, backend):
    """Decision 144: the smaller of the role's side (`from_vision` resolved) and the backend's; `none` is no limit."""
    side = role_image.get('max_side', 'from_vision')
    if side == 'from_vision':
        side = config['vision']['max_image_side']
    limits = [value for value in (side, backend['image']['max_side']) if value != NO_LIMIT]
    if any(type(value) is not int or value < 1 for value in limits):
        raise ConfigurationError('image max_side: expected a positive integer or none')
    return min(limits) if limits else NO_LIMIT


def validate_require(require):
    """Caller demands are exact known names, never substring matches or ignored typos."""
    if not isinstance(require, (tuple, frozenset)) or any(type(name) is not str for name in require):
        raise ValueError('require must be a tuple or frozenset of capability names')
    unknown = set(require) - CAPABILITIES
    if unknown:
        raise ValueError('require: unknown capability names: ' + ', '.join(sorted(unknown)))
    return require


def semantic_role_settings(config, role_name):
    """Offline reply identity excludes transport/admission settings; request trace hashes stay separate."""
    role = config['vlm']['roles'][role_name]
    backends = config['vlm']['backends']
    selected = backends[role['backend']]
    image = role['image']

    def identity(backend):
        found = {'provider': backend['provider'], 'engine': resolve_backend_engine(backend, 'vlm.backends'),
                 'model': deepcopy(backend['model']), 'extra_body': deepcopy(backend.get('extra_body', {}))}
        # Decision 144: a backend cap changes the pixels sent, so it is reply identity; `none` sends what was sent
        # before the key existed, so it adds nothing: the model-settings component of offline checkpoint keys is
        # unchanged (full keys still change with tool source revisions, R-017).
        if isinstance(image, dict) and backend['image']['max_side'] != NO_LIMIT:
            found['image_max_side'] = backend['image']['max_side']
        return found

    result = {'identity': 'semantic_reply_v1',  # artifact-bound: never interpret old whole-profile keys here
              'inference': {key: deepcopy(role[key]) for key in ('max_tokens', 'sampling', 'reasoning', 'output')},
              'backend': identity(selected)}
    if selected['provider'] != 'openai_compatible':
        route = {'on_budget_exhausted': selected['on_budget_exhausted']}
        if isinstance(image, dict):
            route['allow_images'] = selected['allow_images']
        result['routing'] = route
        if (selected['on_budget_exhausted'] == 'fallback_to_default_backend'
                or (isinstance(image, dict) and not selected['allow_images'])):
            result['fallback'] = identity(backends[config['vlm']['default_backend']])
    if isinstance(image, dict):
        vision = config['vision']
        side = image.get('max_side', 'from_vision')
        alpha = vision['alpha_policy']
        result['image'] = {'max_side': vision['max_image_side'] if side == 'from_vision' else side,
                           'format': vision['format'] if image['format'] == 'from_vision' else image['format'],
                           'quality': image['quality'], 'alpha_policy': deepcopy(alpha)}
    return result


class ModelClientRun:
    """Own the shared budgets, concurrency and transports for one run."""

    def __init__(self, config, project_root, *, probe_cases=None, local_probe_plans=None):
        self.config = deepcopy(config)
        self.project_root = Path(project_root).resolve()
        backends = self.config['vlm']['backends']
        configure_decode_backstop(backend['image']['max_decode_pixels'] for backend in backends.values()
                                  if isinstance(backend, dict) and isinstance(backend.get('image'), dict)
                                  and type(backend['image']['max_decode_pixels']) is int)
        self.concurrency = BackendConcurrency({name: backend['max_concurrency'] for name, backend in backends.items()
                                               if isinstance(backend, dict)
                                               and type(backend['max_concurrency']) is int})
        self.ledger = SingleRunBudgetLedger({name: backend['call_budget'] for name, backend in backends.items()
                                            if isinstance(backend, dict) and type(backend.get('call_budget')) is int})
        self.transports = {}
        self.probes = {}
        self.schemas = cached_registry(self.project_root / 'contracts' / 'schemas')  # checked once per unchanged set
        self.client = RoleClient(self.config, self.transports, self.probes, self.schemas,
                                 project_root=self.project_root, probe_cases=probe_cases,
                                 local_probe_plans=local_probe_plans)

    async def for_role(self, role_name, *, require=()):
        validate_require(require)
        role = self.config['vlm']['roles'][role_name]
        _sentinel(role, self.project_root, f'vlm.roles.{role_name}', {})
        image = role['image']
        if isinstance(image, dict):
            vision = self.config['vision']
            if not isinstance(vision, dict):
                _sentinel(vision, self.project_root, 'vision', {})
            if image.get('max_side', 'from_vision') == 'from_vision':
                _sentinel(vision['max_image_side'], self.project_root, 'vision.max_image_side', {})
            if image['format'] == 'from_vision':
                _sentinel(vision['format'], self.project_root, 'vision.format', {})
            # A missing source is refused like an unknown one (vlm_role.v0 requires it), before any probe is spent.
            if image.get('source') not in IMAGE_SOURCE_ROLES:
                raise ConfigurationError(f'vlm.roles.{role_name}.image.source: missing or unknown image source')
        default =self.config['vlm']['default_backend']
        _sentinel(default, self.project_root, 'vlm.default_backend', {})
        for name in {role['backend'], default}:
            backend = self.config['vlm']['backends'][name]
            _sentinel(backend, self.project_root, f'vlm.backends.{name}', {})
            if name not in self.transports:
                self.transports[name] = ModelTransport(name, backend, self.concurrency, self.ledger)
        try:
            await self.client.ensure_role_capabilities(role['backend'], role_name, require=require)
        except BudgetExhausted:
            if self.config['vlm']['backends'][role['backend']].get('on_budget_exhausted') != 'fallback_to_default_backend':
                error = ModelCallError('BUDGET_EXHAUSTED: probe grant exhausted')
                error.probe_evidence = safe_probe_events(self.client.probe_events)
                raise error from None
        except ProbeLimitExhausted as cause:
            error = ModelCallError('PROBE_INCOMPLETE: probe allowance exhausted')
            error.probe_evidence = safe_probe_events(self.client.probe_events)
            raise error from cause
        except ModelCallError as error:
            error.probe_evidence = safe_probe_events(self.client.probe_events)
            raise
        return BoundModelClient(self, role_name)

    async def close(self):
        try:
            for transport in self.transports.values():
                await transport.close()
        finally:
            self.client._cloud_headers.clear()


class BoundModelClient:
    """A role bound to a run; other roles share its run's resources."""

    def __init__(self, run, role_name):
        self.run = run
        self.role_name = role_name

    async def call(self, system_prompt, user_text, *, manifest=None, images=()):
        return await self.run.client.call(self.role_name, system_prompt, user_text, manifest=manifest, images=images)

    async def for_role(self, role_name, *, require=()):
        validate_require(require)
        return await self.run.for_role(role_name, require=require)

    async def close(self):
        await self.run.close()


def run_concurrency(client):
    """The run-owned `BackendConcurrency` of a supplied run or bound client (stage capacity records, decision 149)."""
    return (client.run if isinstance(client, BoundModelClient) else client).concurrency


async def compose_model_client(config, role_name, *, project_root, probe_cases=None, local_probe_plans=None):
    """Build a role client and its run-owned resources from loaded configuration."""
    run = ModelClientRun(config, project_root, probe_cases=probe_cases, local_probe_plans=local_probe_plans)
    try:
        return await run.for_role(role_name)
    except BaseException:
        await run.close()
        raise


def _digest(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def _usage(payload, provider):
    usage = payload.get('usageMetadata' if provider == 'gemini' else 'usage')
    if not isinstance(usage, dict):
        return None
    fields = {'openai_compatible': ('prompt_tokens', 'completion_tokens', 'total_tokens'),  # O
              'anthropic': ('input_tokens', 'output_tokens'),  # A
              'gemini': ('promptTokenCount', 'candidatesTokenCount')}[provider]  # G
    return {key: usage[key] for key in fields if type(usage.get(key)) is int and usage[key] >= 0}


class RoleClient:
    """Use preconfigured transports and verified probes for a single run."""

    def __init__(self, config, transports, probes, schemas, *, project_root=None, probe_cases=None,
                 local_probe_plans=None):
        self.config = config
        self.transports = transports
        self.probes = probes
        self.schemas = schemas
        self.project_root = project_root
        self._production = probe_cases is None and local_probe_plans is None and not probes
        self._bundle = None
        self._production_attempted = {}
        self.probe_events = []
        self._probe_allowances = {}
        self.probe_cases = probe_cases or {}
        self.local_probe_plans = local_probe_plans or {}
        self._probe_remaining = {}
        self._probe_locks = {}
        self._multiple_attempted = set()
        self._cloud_headers = {}

    def _headers(self, backend_name):
        backend = self.config['vlm']['backends'][backend_name]
        if backend['provider'] == 'openai_compatible':
            return {}
        if backend_name not in self._cloud_headers:
            self._cloud_headers[backend_name] = cloud_headers(backend['provider'], backend, self.project_root)
        return self._cloud_headers[backend_name]

    def _multiple_images_pair(self, backend_name, role_name, *, require=()):
        validate_require(require)
        if 'multiple_images' not in require or backend_name in self._multiple_attempted:
            return None
        backend = self.config['vlm']['backends'][backend_name]
        role = self.config['vlm'].get('roles', {}).get(role_name)
        if (role is not None and role['backend'] == backend_name
                and backend['provider'] == 'openai_compatible' and isinstance(role['image'], dict)
                and role['image'].get('source') in ('source_image', 'user_reference', 'knowledge_reference')):
            # Same side rule as generation (decision 144), so the pair is proved on the pixels a real call sends.
            side = image_max_side(self.config, role['image'], backend)
            vision = self.config['vision']
            try:
                return multiple_images_plan(backend['model']['id'], role['max_tokens'],
                                            lambda data: prepare_probe_image(data, side, backend, role, vision))
            except (ValueError, TypeError, KeyError, OSError) as error:
                # An unpreparable fixture (e.g. a decode bound below its size) leaves the proof absent, never faked.
                self._multiple_attempted.add(backend_name)
                self.probe_events.append({'role': role_name, 'capability': 'multiple_images', 'status': 'failed',
                    'sent': self._probe_transport(backend_name).allowance.sent,
                    'reason': 'CLIENT_STATE_INVALID' if isinstance(error, (TypeError, KeyError)) else 'INPUT_INVALID'})
                cached = self.probes.get(backend_name)
                if cached is not None:
                    self.probes[backend_name] = ProbeResult('failed', cached.capabilities,
                        {**cached.errors, 'multiple_images': 'PROBE_FAILED'}, cached.evidence)
        return None

    def _take_probe_requests(self, backend_name, wanted):
        # Reserve before awaiting: errors and later role binds cannot reset the backend allowance.
        allowance = self._probe_allowances.get(backend_name)
        if self._production:
            return min(wanted, allowance.remaining)
        count = min(wanted, self._probe_remaining.get(backend_name, 0),
                    allowance.remaining if allowance is not None else wanted)
        self._probe_remaining[backend_name] = self._probe_remaining.get(backend_name, 0) - count
        return count

    def _probe_transport(self, backend_name):
        backend = self.config['vlm']['backends'][backend_name]
        allowance = self._probe_allowances.setdefault(backend_name, ProbeAllowance(backend['probe']['max_requests']))
        return ProbeTransport(self.transports[backend_name], allowance)

    async def _production_probe(self, backend_name, role_name, *, require=(), role=None):
        backend = self.config['vlm']['backends'][backend_name]
        limits = backend['probe']
        _sentinel(limits, self.project_root, f'vlm.backends.{backend_name}.probe', {})
        if (type(limits['max_requests']) is not int or limits['max_requests'] < 1
                or type(limits['timeout_s']) not in (int, float)
                or not math.isfinite(limits['timeout_s']) or limits['timeout_s'] <= 0):
            raise ModelCallError('CONFIGURATION_INVALID: invalid probe limits')
        if role is None:
            role = self.config['vlm']['roles'].get(role_name)
        if role is None or self.project_root is None:
            raise ModelCallError('PROBE_CASES_MISSING: production probes need a configured role and project')
        if self._bundle is None:
            try:
                self._bundle, self._images = load_probe_bundle(self.project_root)
            except ProbeArtifactError as error:
                raise ModelCallError('PROBE_ARTIFACT_INVALID: probe artifact is invalid') from error
        bundle = self._bundle
        capabilities = ['text']
        output = role['output']['format']
        if output in ('json_object', 'json_schema'):
            capabilities.append(output)
        image = isinstance(role['image'], dict)
        if image and backend['provider'] != 'openai_compatible' and not backend['allow_images']:
            image = False  # The actual image call must bind/probe its local fallback.
        if image:
            capabilities.append('image')
            if output in ('json_object', 'json_schema'):
                capabilities.append('image+' + output)
        previous = self.probes.get(backend_name, ProbeResult('complete', {}, {}))
        attempted = self._production_attempted.setdefault(backend_name, {})
        transport = self._probe_transport(backend_name)
        self._probe_remaining.setdefault(backend_name, limits['max_requests'])
        for capability in capabilities:
            if capability in attempted or previous.capabilities.get(capability) is not None:
                continue
            # The generation rule (decision 144); an invalid side is refused as generation refuses it, before any mark.
            side = image_max_side(self.config, role['image'], backend) if capability.startswith('image') else None
            # Mark before await so failed/incomplete proof is never repeatedly spent.
            attempted[capability] = 'incomplete'
            try:
                cases = production_probe_cases(bundle, self._bundle_images(), backend, role,
                                               self.config['vision'], capability, side)
            except (ValueError, TypeError, KeyError) as error:
                self.probe_events.append({'role': role_name, 'capability': capability,
                    'status': 'failed', 'sent': transport.allowance.sent,
                    'reason': 'CLIENT_STATE_INVALID' if isinstance(error, (TypeError, KeyError)) else 'INPUT_INVALID'})
                raise ModelCallError('CAPABILITY_UNAVAILABLE: probe request mapping is unproved') from error
            count = self._take_probe_requests(backend_name, len(cases))
            try:
                result = (await probe_backend(transport, backend['endpoints']['chat'], cases, count,
                                              limits['timeout_s'], headers=self._headers(backend_name))
                          if count else ProbeResult('incomplete', {}, {}))
            except BaseException as error:
                # No evidence was recorded: refusals and cancellation must remain retryable.
                attempted.pop(capability, None)
                if isinstance(error, (BudgetExhausted, ProbeLimitExhausted)):
                    self.probe_events.append({'role': role_name, 'capability': capability,
                        'status': 'incomplete', 'sent': transport.allowance.sent,
                        'reason': 'BUDGET_EXHAUSTED' if isinstance(error, BudgetExhausted) else 'PROBE_INCOMPLETE'})
                raise
            status = result.status
            if status == 'complete' and not all(result.capabilities.values()):
                attempted[capability] = 'negative'
            else:
                attempted[capability] = status
            proof = ({capability: all(result.capabilities.values())} if status == 'complete' else {})
            evidence = {'bundle_id': BUNDLE_ID, 'capability': capability, 'role': role_name,
                        'backend': backend_name, 'provider': backend['provider'],
                        'model_id': backend['model']['id'], 'model_revision': backend['model']['revision'],
                        'engine': resolve_backend_engine(backend, 'vlm.backends.' + backend_name),
                        'status': attempted[capability], 'sent': transport.allowance.sent,
                        'body_hashes': [_digest(body) for body, _ in cases.values()],
                        'image_hashes': [item['sha256'] for item in bundle['images']] if capability.startswith('image') else []}
            previous = ProbeResult(status, {**previous.capabilities, **proof},
                                   {**previous.errors, **({capability: 'PROBE_' + status.upper()} if status != 'complete' else {})},
                                   (*previous.evidence, evidence))
            self.probes[backend_name] = previous
            self.probe_events.append({'role': role_name, 'capability': capability,
                'status': attempted[capability], 'sent': transport.allowance.sent,
                'challenges': [{'challenge': name,
                    'status': ('complete' if result.capabilities[name] else 'negative')
                        if name in result.capabilities else
                        'failed' if name in result.errors and result.errors[name] != 'PROBE_INCOMPLETE' else 'incomplete'}
                    for name in cases]})
        self.probes.setdefault(backend_name, previous)
        return await self._extend_multiple_images(backend_name, self._multiple_images_pair(backend_name, role_name, require=require))

    def _bundle_images(self):
        return self._images

    async def _extend_multiple_images(self, backend_name, pair):
        result = self.probes[backend_name]
        if (pair is None or result.capabilities.get('multiple_images') is not None
                or backend_name in self._multiple_attempted):
            return result
        self._multiple_attempted.add(backend_name)
        count = self._take_probe_requests(backend_name, 2)
        if not count:
            result = ProbeResult('incomplete', result.capabilities,
                                 {**result.errors, 'multiple_images': 'PROBE_INCOMPLETE'}, result.evidence)
            self.probes[backend_name] = result
            self.probe_events.append({'role': 'folder_namer', 'capability': 'multiple_images',
                'status': 'incomplete', 'sent': self._probe_transport(backend_name).allowance.sent,
                'challenges': [{'challenge': name, 'status': 'incomplete'} for name in ('first', 'second')]})
            return result
        backend = self.config['vlm']['backends'][backend_name]
        try:
            multiple = await probe_local_multiple_images_pair(self._probe_transport(backend_name), backend['endpoints']['chat'],
                                                             *pair, count, backend['probe']['timeout_s'])
        except BaseException:
            self._multiple_attempted.discard(backend_name)
            raise
        status = ('failed' if 'failed' in (result.status, multiple.status) else
                  'incomplete' if 'incomplete' in (result.status, multiple.status) else 'complete')
        result = ProbeResult(status, {**result.capabilities, **multiple.capabilities},
                             {**result.errors, **multiple.errors,
                              **({'multiple_images': 'PROBE_' + multiple.status.upper()}
                                 if multiple.status != 'complete' else {})}, result.evidence)
        self.probes[backend_name] = result
        proof_status = (multiple.status if multiple.status != 'complete' else
                        'complete' if multiple.capabilities.get('multiple_images') is True else 'negative')
        event = {'role': 'folder_namer', 'capability': 'multiple_images', 'status': proof_status,
                 'sent': self._probe_transport(backend_name).allowance.sent,
                 'challenges': list(multiple.evidence)}
        self.probe_events.append(event)
        if self._production:
            result = ProbeResult(result.status, result.capabilities, result.errors,
                (*result.evidence, {**event, 'body_hashes': [_digest(item[0]) for item in pair]}))
            self.probes[backend_name] = result
        return result

    async def start_backend(self, backend_name, *, role_name=None, require=()):
        validate_require(require)
        async with self._probe_locks.setdefault(backend_name, asyncio.Lock()):
            return await self._start_backend(backend_name, role_name=role_name, require=require)

    async def ensure_role_capabilities(self, backend_name, role_name, *, require=()):
        validate_require(require)
        async with self._probe_locks.setdefault(backend_name, asyncio.Lock()):
            if self._production:
                return await self._production_probe(backend_name, role_name, require=require)
            if backend_name not in self.probes:
                return await self._start_backend(backend_name, role_name=role_name, require=require)
            return await self._extend_multiple_images(
                backend_name, self._multiple_images_pair(backend_name, role_name, require=require))

    async def _start_backend(self, backend_name, *, role_name, require=()):
        backend = self.config['vlm']['backends'][backend_name]
        resolve_backend_engine(backend, f'vlm.backends.{backend_name}')
        if self._production:
            return await self._production_probe(backend_name, role_name, require=require)
        production_pair = self._multiple_images_pair(backend_name, role_name, require=require)
        cached = self.probes.get(backend_name)
        if cached is not None and backend_name in self._probe_remaining:
            return await self._extend_multiple_images(backend_name, production_pair)
        self._probe_remaining.setdefault(backend_name, backend['probe']['max_requests'])
        if backend_name in self.local_probe_plans:
            if backend['provider'] != 'openai_compatible':
                raise ModelCallError('local probe plan requires an OpenAI-compatible backend')
            plan = self.local_probe_plans[backend_name]
            limits, transport = backend['probe'], self._probe_transport(backend_name)
            endpoint = backend['endpoints']['chat']
            count = self._take_probe_requests(backend_name, 2)
            image = (await probe_local_image_pair(transport, endpoint, *plan['image'], count, limits['timeout_s'])
                     if count else ProbeResult('incomplete', {}, {}))
            count = self._take_probe_requests(backend_name, 2)
            if count:
                thinking = await probe_local_thinking_pair(transport, endpoint, *plan['thinking'],
                                                            count, limits['timeout_s'])
            else:
                thinking = ProbeResult('incomplete', {}, {})
            results = [image, thinking]
            status = ('failed' if any(item.status == 'failed' for item in results) else
                      'incomplete' if any(item.status == 'incomplete' for item in results) else 'complete')
            result = ProbeResult(status, {key: value for item in results for key, value in item.capabilities.items()},
                                 {key: value for item in results for key, value in item.errors.items()})
            self.probes[backend_name] = result
            if backend_name in self.probe_cases:
                cases = self.probe_cases[backend_name]
                count = self._take_probe_requests(backend_name, len(cases))
                ordinary = (await probe_backend(transport, endpoint, cases, count, limits['timeout_s'])
                            if count else ProbeResult('incomplete', {}, {}))
                result = ProbeResult(
                    'failed' if 'failed' in (result.status, ordinary.status) else
                    'incomplete' if 'incomplete' in (result.status, ordinary.status) else 'complete',
                    {**result.capabilities, **ordinary.capabilities}, {**result.errors, **ordinary.errors})
                self.probes[backend_name] = result
            pair = (plan.get('multiple_images', production_pair)
                    if role_name is None or production_pair is not None else None)
            return await self._extend_multiple_images(backend_name, pair)
        if backend_name not in self.probe_cases:
            raise ProbeCasesMissing('PROBE_CASES_MISSING: backend has no verifiable probe cases')
        headers = {}
        if backend['provider'] != 'openai_compatible':
            if getattr(self.transports[backend_name], 'ledger', None) is None:
                raise ModelCallError('cloud backend requires a budget ledger')
            if self.project_root is None:
                raise ModelCallError('cloud backend requires a project root')
            headers = self._headers(backend_name)
        limits = backend['probe']
        count = self._take_probe_requests(backend_name, len(self.probe_cases[backend_name]))
        result = (await probe_backend(self._probe_transport(backend_name), backend['endpoints']['chat'],
                                      self.probe_cases[backend_name], count, limits['timeout_s'], headers=headers)
                  if count else ProbeResult('incomplete', {}, {}))
        self.probes[backend_name] = result
        return await self._extend_multiple_images(backend_name, production_pair)

    async def call(self, role_name, system_prompt, user_text, *, manifest=None, images=()):
        try:
            return await self._call(role_name, system_prompt, user_text, manifest=manifest, images=images)
        except ModelCallError as error:
            error.probe_evidence = safe_probe_events(self.probe_events)
            if error.__cause__ is not None or error.code != 'MODEL_CALL_FAILED':
                raise
            raise ModelCallError(f'MODEL_CALL_INVALID: {error}') from error
        except BudgetExhausted as error:
            failure = ModelCallError('BUDGET_EXHAUSTED: model request budget exhausted')
            failure.probe_evidence = safe_probe_events(self.probe_events)
            raise failure from error
        except ProbeLimitExhausted as error:
            failure = ModelCallError('PROBE_INCOMPLETE: probe allowance exhausted')
            failure.probe_evidence = safe_probe_events(self.probe_events)
            raise failure from error
        except ConfigurationError as error:
            raise ModelCallError('CONFIGURATION_INVALID: model configuration is invalid') from error
        except (ValueError, TypeError) as error:
            raise ModelCallError('INPUT_INVALID: model input is invalid') from error
        except (KeyError, IndexError) as error:
            raise ModelCallError('CLIENT_STATE_INVALID: model client state is incomplete') from error
        except Exception as error:
            raise ModelCallError('MODEL_CALL_FAILED: model call could not be completed') from error

    async def _call(self, role_name, system_prompt, user_text, *, manifest=None, images=()):
        role = deepcopy(self.config['vlm']['roles'][role_name])
        configured_output = deepcopy(role['output'])
        if role_name == 'writer':
            if manifest is None:
                raise ModelCallError('writer requires a profile manifest')
            role = resolve_writer_format(role, manifest, project_root=self.project_root)
        backend_name = role['backend']
        if images and isinstance(role['image'], dict):
            permitted = IMAGE_SOURCE_ROLES.get(role['image']['source'], ())
            for image_role, _ in images:
                if image_role not in permitted:
                    # Before any probe or request: a role receives only the images its configured source names.
                    raise ModelCallError(f'image role {image_role!r} is not permitted by '
                                         f'vlm.roles.{role_name}.image.source')
        attempts = []
        fallback_warnings = []
        while True:
            warnings = list(fallback_warnings)
            backend = self.config['vlm']['backends'][backend_name]
            provider = backend['provider']
            headers = {}
            if images and role['image'] == 'none':
                raise ModelCallError('text-only role cannot receive images')
            if images and provider != 'openai_compatible' and not backend['allow_images']:
                fallback = self.config['vlm']['default_backend']
                if fallback == backend_name:
                    raise ModelCallError('image fallback is not local')
                backend_name = fallback
                await self.start_backend(backend_name, role_name=role_name)
                continue
            engine = resolve_backend_engine(backend, f'vlm.backends.{backend_name}')
            if provider != 'openai_compatible':
                transport = self.transports.get(backend_name)
                if transport is None or getattr(transport, 'ledger', None) is None:
                    raise ModelCallError('cloud backend requires a budget ledger')
                if self.project_root is None:
                    raise ModelCallError('cloud backend requires a project root')
                if images and provider == 'gemini':
                    raise ModelCallError('gemini: image request mapping unproven')
                headers = self._headers(backend_name)
            try:
                if backend_name not in self.probes:
                    await self.start_backend(backend_name, role_name=role_name)
                if self._production:
                    async with self._probe_locks.setdefault(backend_name, asyncio.Lock()):
                        await self._production_probe(backend_name, role_name, role=role,
                                                     require=('multiple_images',) if len(images) > 1 else ())
            except BudgetExhausted:
                if backend.get('on_budget_exhausted') != 'fallback_to_default_backend':
                    raise
                fallback = self.config['vlm']['default_backend']
                if fallback == backend_name:
                    raise ModelCallError('budget fallback is the exhausted backend')
                attempts.append({'backend': backend_name, 'warnings': warnings,
                                 'status': 'BUDGET_EXHAUSTED'})
                fallback_warnings.append(('BUDGET_EXHAUSTED', backend_name))
                backend_name = fallback
                await self.start_backend(backend_name, role_name=role_name)
                continue
            output = role['output']
            if output['format'] != 'none' and output['validate_response'] is not True:
                raise ModelCallError('structured output requires response validation')
            required = [] if output['format'] == 'none' else [output['format']]
            if self._production:
                required.append('text')
            if images:
                required.append('image')
                if self._production and output['format'] != 'none':
                    required.append('image+' + output['format'])
                if len(images) > 1:
                    required.append('multiple_images')
                    if (backend_name == role['backend']
                            and self.probes[backend_name].capabilities.get('multiple_images') is None):
                        await self.ensure_role_capabilities(backend_name, role_name, require=('multiple_images',))
            try:
                require_capabilities(self.probes[backend_name], required)
            except ValueError as error:
                if self._production:
                    missing = [cap for cap in required if self.probes[backend_name].capabilities.get(cap) is not True]
                    codes = [self.probes[backend_name].errors.get(cap) for cap in missing]
                    code = ('PROBE_INCOMPLETE' if 'PROBE_INCOMPLETE' in codes else
                            'PROBE_FAILED' if 'PROBE_FAILED' in codes else 'CAPABILITY_UNAVAILABLE')
                    raise ModelCallError(code + ': required capability is unproved') from error
                raise ModelCallError('CAPABILITY_UNAVAILABLE: required capability is unproven') from error
            capabilities = self.probes[backend_name].capabilities
            body, dropped = map_generation_fields(provider, role, capabilities)
            if provider == 'openai_compatible':
                warnings.extend(dropped)
            body['model'] = backend['model']['id']
            content = user_text
            cloud_images = []
            if images:
                image_settings = role['image']
                vision = self.config['vision']
                max_side = image_max_side(self.config, image_settings, backend)  # per backend, after any fallback
                fmt = image_settings['format']
                fmt = vision['format'] if fmt == 'from_vision' else fmt
                quality = image_settings['quality']
                # The registered type of the encoded bytes, not the encoder name (T-008 fix round 5, R4F1); the
                # loader refuses a format without one, so this guards only a profile that skipped it.
                media_type = image_media_type(fmt)
                if media_type is None:
                    raise ConfigurationError(f'vlm.roles.{role_name}.image.format: {NO_MEDIA_TYPE.format(fmt)}')
                content = [{'type': 'text', 'text': user_text}]
                for image_role, image_data in images:
                    if image_role not in ('source_image', 'user_reference', 'knowledge_reference'):
                        raise ModelCallError('invalid image role')
                    try:
                        prepared = prepare_image(image_data, max_side, backend['image']['max_decode_pixels'],
                                                 alpha_policy=vision['alpha_policy'], output_format=fmt, quality=quality)
                    except Exception as error:
                        raise ModelCallError('IMAGE_INVALID: image preparation failed') from error
                    encoded = base64.b64encode(prepared).decode('ascii')
                    cloud_images.append((media_type, prepared))
                    content.append({'type': 'image_url', 'image_url': {'url': f'data:{media_type};base64,{encoded}'}})  # O
            body['messages'] = [{'role': 'system', 'content': system_prompt.decode('utf-8')},
                                {'role': 'user', 'content': content}]
            reasoning = role['reasoning']
            if provider == 'openai_compatible' and reasoning['mode'] in ('on', 'off'):
                if capabilities.get('reasoning.mode') is True:
                    body['chat_template_kwargs'] = {reasoning['template_kwargs_key']: reasoning['mode'] == 'on'}  # L,V
                else:
                    warnings.append(('PARAM_UNSUPPORTED', 'reasoning.mode'))
            if provider == 'openai_compatible' and reasoning['effort'] not in ('none', 'backend_default', None):
                if capabilities.get('reasoning.effort') is True:
                    body['reasoning_effort'] = reasoning['effort']  # O,L
                else:
                    warnings.append(('PARAM_UNSUPPORTED', 'reasoning.effort'))
            if provider == 'openai_compatible' and reasoning['budget_tokens'] not in ('none', None):
                warnings.append(('PARAM_UNSUPPORTED', 'reasoning.budget_tokens'))
            schema_id = output['schema_id']
            schema_hash = None
            cloud_schema = None
            if output['validate_response'] and schema_id != 'none':
                _, schema_hash = self.schemas.get(schema_id)
            if provider == 'anthropic' and output['format'] == 'json_schema':
                cloud_schema, schema_hash = self.schemas.get(schema_id)
            if provider == 'openai_compatible' and output['format'] == 'json_object':
                body['response_format'] = {'type': 'json_object'}  # O
            elif provider == 'openai_compatible' and output['format'] == 'json_schema':
                schema, schema_hash = self.schemas.get(schema_id)
                body['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': schema_id, 'schema': schema, 'strict': output['strict']}}  # O
            if provider == 'openai_compatible':
                body.update(deepcopy(backend.get('extra_body', {})))
            else:
                body, cloud_warnings = cloud_request(provider, backend, role, system_prompt.decode('utf-8'), user_text,
                                                     schema=cloud_schema, images=cloud_images)
                warnings.extend(cloud_warnings)
            body_hash = _digest(body)
            image_config = role['image']
            vision_config = None
            if isinstance(image_config, dict):
                vision = self.config['vision']
                vision_config = {key: vision[key] for key in ('max_image_side', 'format', 'alpha_policy')
                                 if ((key == 'max_image_side' and image_config.get('max_side', 'from_vision') == 'from_vision')
                                     or (key == 'format' and image_config['format'] == 'from_vision') or key == 'alpha_policy')}
            # Decision 144: the side actually used and the backend cap, so requests differing only in the cap differ.
            side_config = None
            if isinstance(image_config, dict):
                side_config = {'used': image_max_side(self.config, image_config, backend),
                               'backend': backend['image']['max_side']}
            request_hash = _digest({'body_hash': body_hash, 'role': role_name, 'prompt_id': role['prompt_id'],
                                    'image_roles': [image_role for image_role, _ in images],
                                    'image': image_config, 'vision': vision_config, 'image_max_side': side_config,
                                    'sampling': role['sampling'],
                                    'reasoning': role['reasoning'], 'output': configured_output,
                                    'resolved_output': output, 'engine': engine,
                                    'model_revision': backend['model']['revision']})
            record = {'role': role_name, 'prompt_id': role['prompt_id'], 'backend': backend_name,
                      'provider': provider, 'engine': engine, 'model_id': backend['model']['id'],
                      'model_revision': backend['model']['revision'], 'schema_id': schema_id,
                      'schema_hash': schema_hash, 'effective_body_hash': body_hash, 'request_hash': request_hash,
                      'image_roles': [image_role for image_role, _ in images],
                      'warnings': warnings, 'previous_attempts': attempts}
            if self._production:
                record['probe_evidence'] = list(self.probes[backend_name].evidence)
            started = time.monotonic()
            try:
                response = await self.transports[backend_name].post(backend['endpoints']['chat'], body, headers)
            except BudgetExhausted:
                if backend.get('on_budget_exhausted') != 'fallback_to_default_backend':
                    raise
                fallback = self.config['vlm']['default_backend']
                if fallback == backend_name:
                    raise ModelCallError('budget fallback is the exhausted backend')
                attempts.append({'backend': backend_name, 'warnings': warnings,
                                 'status': 'BUDGET_EXHAUSTED'})
                fallback_warnings.append(('BUDGET_EXHAUSTED', backend_name))
                backend_name = fallback
                await self.start_backend(backend_name, role_name=role_name)
                continue
            except Exception as error:
                record['status'] = 'HTTP_ERROR'
                record['error_type'] = type(error).__name__
                return None, record
            finally:
                record['latency_s'] = time.monotonic() - started
            if not response.is_success:
                record['status'] = 'HTTP_ERROR'
                record['http_status'] = response.status_code
                return None, record
            try:
                payload = response.json()
                if provider == 'openai_compatible':
                    choice = payload['choices'][0]  # O: Chat response
                    message = choice['message']
                    reason, text, refused = choice['finish_reason'], message['content'], message.get('refusal')
                    accepted = ('stop',)
                elif provider == 'anthropic':
                    reason = payload['stop_reason']  # A: provider-docs response
                    text = ''.join(part['text'] for part in payload['content'] if part['type'] == 'text')
                    refused = reason == 'refusal'
                    accepted = ('end_turn', 'stop_sequence')
                else:
                    choice = payload['candidates'][0]  # G: generateContent response
                    reason = choice['finishReason']
                    text = ''.join(part['text'] for part in choice['content']['parts'] if 'text' in part)
                    refused = reason == 'SAFETY'
                    accepted = ('STOP',)
                if reason not in accepted or refused:
                    record['status'] = 'INCOMPLETE_OR_REFUSED'
                    record['finish_reason'] = reason if reason in ('length', 'content_filter', 'max_tokens',
                                                                    'MAX_TOKENS', 'SAFETY') else 'other'
                    record['usage'] = _usage(payload, provider)
                    return None, record
                if not isinstance(text, str) or not text:
                    raise ValueError('response content is empty or not text')
                value = text
                if output['format'] != 'none':
                    value = json.loads(text)
                if output['validate_response'] and schema_id != 'none':
                    self.schemas.validate(schema_id, value)
                record['usage'] = _usage(payload, provider)
                record['status'] = 'OK'
                return value, record
            except (KeyError, IndexError, TypeError, ValueError, ValidationError) as error:
                record['status'] = 'SCHEMA_INVALID'
                record['warnings'].append(('SCHEMA_INVALID', type(error).__name__))
                return None, record
