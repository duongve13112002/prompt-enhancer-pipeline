"""Role-bound model calls with explicit capability evidence."""

import hashlib
import json
import time
import base64
from copy import deepcopy

from jsonschema.exceptions import ValidationError

from .config import ConfigurationError, _sentinel, resolve_backend_engine, resolve_writer_format
from .model_cloud import cloud_headers, cloud_request
from .model_mapping import map_generation_fields
from .model_image import configure_decode_backstop, prepare_image
from .model_probe import (ProbeResult, probe_backend, probe_local_image_pair, probe_local_thinking_pair,
                          require_capabilities)
from .model_resources import BackendConcurrency, BudgetExhausted, SingleRunBudgetLedger
from .model_schemas import SchemaRegistry
from .model_transport import ModelTransport
from pathlib import Path


class ModelCallError(RuntimeError):
    """A request could not be completed safely."""


class ModelClientRun:
    """Own the shared budgets, concurrency and transports for one run."""

    def __init__(self, config, project_root, *, probe_cases, local_probe_plans):
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
        self.schemas = SchemaRegistry(self.project_root / 'contracts' / 'schemas')
        self.client = RoleClient(self.config, self.transports, self.probes, self.schemas,
                                 project_root=self.project_root, probe_cases=probe_cases,
                                 local_probe_plans=local_probe_plans)

    async def for_role(self, role_name):
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
        default = self.config['vlm']['default_backend']
        _sentinel(default, self.project_root, 'vlm.default_backend', {})
        for name in {role['backend'], default}:
            backend = self.config['vlm']['backends'][name]
            _sentinel(backend, self.project_root, f'vlm.backends.{name}', {})
            if name not in self.transports:
                self.transports[name] = ModelTransport(name, backend, self.concurrency, self.ledger)
        if role['backend'] not in self.probes:
            try:
                await self.client.start_backend(role['backend'])
            except BudgetExhausted:
                if self.config['vlm']['backends'][role['backend']].get('on_budget_exhausted') != 'fallback_to_default_backend':
                    raise
        return BoundModelClient(self, role_name)

    async def close(self):
        for transport in self.transports.values():
            await transport.close()


class BoundModelClient:
    """A role bound to a run; other roles share its run's resources."""

    def __init__(self, run, role_name):
        self.run = run
        self.role_name = role_name

    async def call(self, system_prompt, user_text, *, manifest=None, images=()):
        return await self.run.client.call(self.role_name, system_prompt, user_text, manifest=manifest, images=images)

    async def for_role(self, role_name):
        return await self.run.for_role(role_name)

    async def close(self):
        await self.run.close()


async def compose_model_client(config, role_name, *, project_root, probe_cases, local_probe_plans):
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
        self.probe_cases = probe_cases or {}
        self.local_probe_plans = local_probe_plans or {}

    async def start_backend(self, backend_name):
        backend = self.config['vlm']['backends'][backend_name]
        resolve_backend_engine(backend, f'vlm.backends.{backend_name}')
        if backend_name in self.local_probe_plans:
            if backend['provider'] != 'openai_compatible':
                raise ModelCallError('local probe plan requires an OpenAI-compatible backend')
            plan = self.local_probe_plans[backend_name]
            limits, transport = backend['probe'], self.transports[backend_name]
            endpoint = backend['endpoints']['chat']
            image = await probe_local_image_pair(transport, endpoint, *plan['image'],
                                                 min(2, limits['max_requests']), limits['timeout_s'])
            remaining = limits['max_requests'] - min(2, limits['max_requests'])
            if remaining:
                thinking = await probe_local_thinking_pair(transport, endpoint, *plan['thinking'],
                                                            min(2, remaining), limits['timeout_s'])
            else:
                thinking = ProbeResult('incomplete', {}, {})
            status = ('failed' if 'failed' in (image.status, thinking.status) else
                      'incomplete' if 'incomplete' in (image.status, thinking.status) else 'complete')
            result = ProbeResult(status, {**image.capabilities, **thinking.capabilities},
                                 {**image.errors, **thinking.errors})
            self.probes[backend_name] = result
            return result
        if backend_name not in self.probe_cases:
            raise ModelCallError('backend has no verifiable probe cases')
        headers = {}
        if backend['provider'] != 'openai_compatible':
            if getattr(self.transports[backend_name], 'ledger', None) is None:
                raise ModelCallError('cloud backend requires a budget ledger')
            if self.project_root is None:
                raise ModelCallError('cloud backend requires a project root')
            headers = cloud_headers(backend['provider'], backend, self.project_root)
        limits = backend['probe']
        result = await probe_backend(self.transports[backend_name], backend['endpoints']['chat'],
                                     self.probe_cases[backend_name], limits['max_requests'], limits['timeout_s'],
                                     headers=headers)
        self.probes[backend_name] = result
        return result

    async def call(self, role_name, system_prompt, user_text, *, manifest=None, images=()):
        try:
            return await self._call(role_name, system_prompt, user_text, manifest=manifest, images=images)
        except ModelCallError as error:
            if error.__cause__ is not None:
                raise
            raise ModelCallError(f'MODEL_CALL_INVALID: {error}') from error
        except BudgetExhausted as error:
            raise ModelCallError('BUDGET_EXHAUSTED: model request budget exhausted') from error
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
                await self.start_backend(backend_name)
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
                headers = cloud_headers(provider, backend, self.project_root)
            if backend_name not in self.probes:
                try:
                    await self.start_backend(backend_name)
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
                    await self.start_backend(backend_name)
                    continue
            output = role['output']
            if output['format'] != 'none' and output['validate_response'] is not True:
                raise ModelCallError('structured output requires response validation')
            required = [] if output['format'] == 'none' else [output['format']]
            if images:
                required.append('image')
                if len(images) > 1:
                    required.append('multiple_images')
            try:
                require_capabilities(self.probes[backend_name], required)
            except ValueError as error:
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
                max_side = image_settings.get('max_side', 'from_vision')
                max_side = vision['max_image_side'] if max_side == 'from_vision' else max_side
                fmt = image_settings['format']
                fmt = vision['format'] if fmt == 'from_vision' else fmt
                quality = image_settings['quality']
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
                    cloud_images.append((f'image/{fmt}', prepared))
                    content.append({'type': 'image_url', 'image_url': {'url': f'data:image/{fmt};base64,{encoded}'}})  # O
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
            request_hash = _digest({'body_hash': body_hash, 'role': role_name, 'prompt_id': role['prompt_id'],
                                    'image_roles': [image_role for image_role, _ in images],
                                    'image': image_config, 'vision': vision_config, 'sampling': role['sampling'],
                                    'reasoning': role['reasoning'], 'output': configured_output,
                                    'resolved_output': output, 'engine': engine,
                                    'model_revision': backend['model']['revision']})
            record = {'role': role_name, 'prompt_id': role['prompt_id'], 'backend': backend_name,
                      'provider': provider, 'engine': engine, 'model_id': backend['model']['id'],
                      'model_revision': backend['model']['revision'], 'schema_id': schema_id,
                      'schema_hash': schema_hash, 'effective_body_hash': body_hash, 'request_hash': request_hash,
                      'image_roles': [image_role for image_role, _ in images],
                      'warnings': warnings, 'previous_attempts': attempts}
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
                await self.start_backend(backend_name)
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
