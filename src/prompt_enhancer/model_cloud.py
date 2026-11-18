"""Cloud request construction using documented provider fields."""

from copy import deepcopy
import base64

from .model_mapping import map_cloud_reasoning, map_generation_fields
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
        headers = {'Authorization': 'Bearer ' + key, 'anthropic-version': version}  # A: API overview
        if workspace != 'none':
            headers['anthropic-workspace-id'] = workspace
        return headers
    if provider == 'gemini':
        # G: API key documentation, REST example.
        return {'x-goog-api-key': key}
    raise ValueError('unsupported cloud provider')


def cloud_request(provider, backend, role, system_prompt, user_text, *, schema=None, images=(), addendum=None):
    """Build text-only fragments; unverified required output capabilities fail closed."""
    if provider == 'anthropic' and role['reasoning']['mode'] == 'on':
        budget = role['reasoning']['budget_tokens']
        # A: documented minimum and required relationship to max_tokens.
        if type(budget) is not int or budget < 1024 or budget >= role['max_tokens']:
            raise ValueError('anthropic thinking budget must be at least 1024 and less than max_tokens')
    fields, warnings = map_generation_fields(provider, role)
    reasoning, dropped = map_cloud_reasoning(provider, role['reasoning'])
    warnings.extend(dropped)
    output = role['output']
    if output['format'] != 'none':
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
