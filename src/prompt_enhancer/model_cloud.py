"""Cloud request construction using documented provider fields."""

from copy import deepcopy
import base64

from .model_mapping import anthropic_reasoning_refusal, map_cloud_reasoning, map_generation_fields
from .model_secrets import load_api_key
from .config import SENTINELS


def cloud_headers(provider, backend, project_root):
    """Load credentials at call time; callers must not record these headers."""
    key = load_api_key(project_root, backend['api_key_file'])
    if provider == 'anthropic':
        version = backend['anthropic_version']
        if version == 'deploy_required':
            raise ValueError('anthropic_version is unresolved')
        workspace = backend['anthropic_workspace_id']
        if workspace in SENTINELS:
            raise ValueError('anthropic_workspace_id is unresolved')
        # A: an API key goes in `x-api-key`; `Authorization: Bearer` is for OAuth tokens (Claude API skill docs, cache
        # dated 2026-11-03). Protocol-bound. No OAuth mode exists here.
        headers = {'x-api-key': key, 'anthropic-version': version}
        if workspace != 'none':
            headers['anthropic-workspace-id'] = workspace
        return headers
    if provider == 'gemini':
        # G: API key documentation, REST example.
        return {'x-goog-api-key': key}
    raise ValueError('unsupported cloud provider')


# Protocol-bound. JSON Schema keywords the Anthropic structured output does not support (Claude API skill docs,
# structured-outputs page, cache dated 2026-11-03): numerical constraints, string length constraints and array
# constraints. Removing them only loosens the request; the client validates the reply against the full schema.
ANTHROPIC_UNSUPPORTED_KEYWORDS = frozenset((
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength',
    'minItems', 'maxItems', 'uniqueItems'))
# Keywords whose subschemas are only ever "and"-ed or applied positively: a keyword removed below them can only widen
# the set of accepted instances. Anything else that holds a subschema (`not`, `if`, `oneOf`, ...) could turn a removal
# into a narrowing, so a schema using it is refused instead of changed.
_SCHEMA_MAPS = ('properties', '$defs', 'definitions')
_SCHEMA_LISTS = ('anyOf', 'allOf')
_REFUSED_APPLICATORS = frozenset((
    'not', 'if', 'then', 'else', 'oneOf', 'dependentSchemas', 'contains', 'minContains', 'maxContains',
    'propertyNames', 'patternProperties', 'unevaluatedProperties', 'unevaluatedItems', 'prefixItems'))


def anthropic_output_schema(schema):
    """The copy of `schema` sent as `output_config.format.schema`: the same schema with the unsupported keywords removed.

    It never adds or changes a keyword. A schema Anthropic cannot take as it is raises `ValueError` naming the path,
    and nothing is repaired: an object without `additionalProperties: false`, a `$ref` outside the schema's own
    `$defs`/`definitions` or a recursive one, and the applicators in `_REFUSED_APPLICATORS`.
    """
    sent = _strip_schema(schema, '$')
    _refuse_recursion(sent)
    return sent


def _strip_schema(node, path):
    if not isinstance(node, dict):
        raise ValueError(f'{path}: a schema object is required')
    result = {}
    for key, value in node.items():
        if key in ANTHROPIC_UNSUPPORTED_KEYWORDS:
            continue
        if key in _REFUSED_APPLICATORS:
            raise ValueError(f'{path}: {key} cannot be sent to Anthropic structured output')
        if key in _SCHEMA_MAPS:
            if not isinstance(value, dict):
                raise ValueError(f'{path}/{key}: a map of schemas is required')
            result[key] = {name: _strip_schema(item, f'{path}/{key}/{name}') for name, item in value.items()}
        elif key in _SCHEMA_LISTS:
            if not isinstance(value, list):
                raise ValueError(f'{path}/{key}: a list of schemas is required')
            result[key] = [_strip_schema(item, f'{path}/{key}[{index}]') for index, item in enumerate(value)]
        elif key == 'items':
            result[key] = _strip_schema(value, f'{path}/items')
        elif key == 'additionalProperties':
            if value is not False:
                raise ValueError(f'{path}: additionalProperties other than false cannot be sent to Anthropic')
            result[key] = False
        elif key == '$ref':
            if type(value) is not str or not value.startswith(('#/$defs/', '#/definitions/')):
                raise ValueError(f"{path}: $ref must point into the schema's own $defs")
            result[key] = value
        else:
            result[key] = deepcopy(value)
    kind = node.get('type')
    if (kind == 'object' or (isinstance(kind, list) and 'object' in kind) or 'properties' in node) \
            and node.get('additionalProperties') is not False:
        raise ValueError(f'{path}: an object needs additionalProperties: false for Anthropic structured output')
    return result


def _refuse_recursion(schema):
    """Refuse a `$ref` cycle among the schema's definitions (a recursive schema is unsupported)."""
    def pointer(ref):
        node = schema
        for key in ref[2:].split('/'):
            if not isinstance(node, dict) or key not in node:
                raise ValueError(f'$ref {ref}: no such definition')
            node = node[key]
        return node

    def refs(node):
        for key, value in node.items():
            if key == '$ref':
                yield value
            elif key in _SCHEMA_MAPS:
                for item in value.values():
                    yield from refs(item)
            elif key in _SCHEMA_LISTS:
                for item in value:
                    yield from refs(item)
            elif key == 'items':
                yield from refs(value)

    def visit(node, active):
        for ref in refs(node):
            if ref in active:
                raise ValueError(f'$ref {ref}: a recursive schema cannot be sent to Anthropic')
            visit(pointer(ref), active | {ref})

    visit(schema, frozenset())


def cloud_request(provider, backend, role, system_prompt, user_text, *, schema=None, images=(), addendum=None):
    """Build text-only fragments; unverified required output capabilities fail closed."""
    if provider == 'anthropic' and role['reasoning']['mode'] == 'on':
        budget = role['reasoning']['budget_tokens']
        # A: documented minimum and required relationship to max_tokens.
        if type(budget) is not int or budget < 1024 or budget >= role['max_tokens']:
            raise ValueError('anthropic thinking budget must be at least 1024 and less than max_tokens')
    if provider == 'anthropic':
        problem = anthropic_reasoning_refusal(backend['model']['id'], role['reasoning'])
        if problem:
            raise ValueError(problem)
    fields, warnings = map_generation_fields(provider, role)
    reasoning, dropped = map_cloud_reasoning(provider, role['reasoning'])
    warnings.extend(dropped)
    output = role['output']
    sent_schema = None
    if provider == 'anthropic' and output['format'] == 'json_schema':
        if schema is None:
            raise ValueError('json_schema output needs a schema')
        if 'output_config' in backend.get('extra_body', {}):
            raise ValueError('engine extras cannot replace output_config of a structured request')
        sent_schema = anthropic_output_schema(schema)
    elif output['format'] != 'none':
        raise ValueError('cloud structured output mapping is not yet verified')
    if addendum is not None and (not isinstance(addendum, str) or set(backend.get('extra_body', {})) & {
            'messages', 'system', 'systemInstruction', 'contents', 'model'}):
        raise ValueError('invalid or overridden admin addendum')
    if provider == 'anthropic':
        content = user_text
        if images:
            content = [{'type': 'text', 'text': user_text}]
            for mime, data in images:
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
                               'data': base64.b64encode(data).decode('ascii')}})  # A: image source
        body = {'model': backend['model']['id'], 'system': system_prompt,
                'messages': [{'role': 'user', 'content': content}], **fields, **reasoning}  # A
        if addendum is not None:
            body['system'] = [{'type': 'text', 'text': system_prompt}, {'type': 'text', 'text': addendum}]
        if sent_schema is not None:  # A: output_config.format; merged with output_config.effort, never replacing it
            body['output_config'] = {**body.get('output_config', {}),
                                     'format': {'type': 'json_schema', 'schema': sent_schema}}
    elif provider == 'gemini':
        if images:
            raise ValueError('Gemini image field members are not verified')
        fields.update(reasoning)
        body = {'systemInstruction': {'parts': [{'text': system_prompt}]},
                'contents': [{'role': 'user', 'parts': [{'text': user_text}]}],
                'generationConfig': fields}  # G
        if addendum is not None:
            body['systemInstruction']['parts'].append({'text': addendum})
    else:
        raise ValueError('unsupported cloud provider')
    body.update(deepcopy(backend.get('extra_body', {})))
    return body, warnings
