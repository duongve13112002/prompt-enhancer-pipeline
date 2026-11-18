"""Documented provider parameter spellings from T-009 provider-docs.md."""

from copy import deepcopy
import base64


# Source columns O and G. Missing A sampling fields are deliberately absent.
SAMPLING = {
    'openai_compatible': {'temperature': 'temperature', 'top_p': 'top_p',
                          'presence_penalty': 'presence_penalty', 'frequency_penalty': 'frequency_penalty',
                          'seed': 'seed', 'stop': 'stop'},
    'anthropic': {'stop': 'stop_sequences'},
    'gemini': {'temperature': 'temperature', 'top_p': 'topP', 'top_k': 'topK',
               'presence_penalty': 'presencePenalty', 'frequency_penalty': 'frequencyPenalty',
               'seed': 'seed', 'stop': 'stopSequences'},
}

# Protocol-bound engine/version enums, from vLLM ChatCompletionRequest at v0.11.0 (N057).
LOCAL_REASONING_EFFORT = {('vllm', '0.11.0'): frozenset(('low', 'medium', 'high'))}


def map_generation_fields(provider, role, capabilities=None, *, mapped=False, engine=None):
    """Map only documented fields; return body fragment and warning names."""
    mapping = dict(SAMPLING[provider])
    capabilities = capabilities or {}
    if provider == 'openai_compatible':
        # L and V agree on these spellings; send only after a verified probe.
        for name in ('top_k', 'min_p'):
            if capabilities.get('sampling.' + name) is True or (mapped and engine in ('llama_cpp', 'vllm')):
                mapping[name] = name
    fields, warnings = {}, []
    maximum = {'openai_compatible': 'max_tokens', 'anthropic': 'max_tokens', 'gemini': 'maxOutputTokens'}
    fields[maximum[provider]] = role['max_tokens']  # O, A, G
    for name, value in role['sampling'].items():
        if value == 'none':
            continue
        if name not in mapping or (provider == 'openai_compatible' and not mapped
                                   and capabilities.get('sampling.' + name) is not True):
            warnings.append(('PARAM_UNSUPPORTED', 'sampling.' + name))
        else:
            fields[mapping[name]] = deepcopy(value)
    # The surrounding request shape is not established by the cited excerpts.
    return fields, warnings


def openai_request(backend, role, system_prompt, user_text, *, schema=None, images=(),
                   capabilities=None, mapped=False, addendum=None):
    """One body builder for challenges and real calls; acceptance is not semantic proof.

    `mapped` is the production construction policy, not a positive capability flag.
    Explicit injected evidence retains its old warning/drop behavior.
    """
    capabilities = capabilities or {}
    engine_config = backend.get('engine', {})
    engine = engine_config.get('name')
    body, warnings = map_generation_fields('openai_compatible', role, capabilities,
                                          mapped=mapped, engine=engine)
    reasoning = role['reasoning']
    if reasoning['mode'] in ('on', 'off'):
        if capabilities.get('reasoning.mode') is True or (mapped and engine in ('llama_cpp', 'vllm')):
            body['chat_template_kwargs'] = {reasoning['template_kwargs_key']: reasoning['mode'] == 'on'}
        else:
            warnings.append(('PARAM_UNSUPPORTED', 'reasoning.mode'))
    effort = reasoning['effort']
    if effort not in ('none', 'backend_default', None):
        supported = (effort in LOCAL_REASONING_EFFORT.get((engine, engine_config.get('version')), ()) if mapped
                     else capabilities.get('reasoning.effort') is True)
        if supported:
            body['reasoning_effort'] = effort
        else:
            warnings.append(('PARAM_UNSUPPORTED', 'reasoning.effort'))
    if reasoning['budget_tokens'] not in ('none', None):
        warnings.append(('PARAM_UNSUPPORTED', 'reasoning.budget_tokens'))
    content = user_text
    if images:
        content = [{'type': 'text', 'text': user_text}, *[
            {'type': 'image_url', 'image_url': {'url': 'data:' + mime + ';base64,'
                                              + base64.b64encode(data).decode('ascii')}}
            for mime, data in images]]
    body['model'] = backend['model']['id']
    body['messages'] = ([] if system_prompt is None else [{'role': 'system', 'content': system_prompt}])
    if addendum is not None:
        # T-015: the S4 addendum follows the admin's verbatim system prompt as its own system message (R-004).
        body['messages'].append({'role': 'system', 'content': addendum})
    body['messages'].append({'role': 'user', 'content': content})
    output = role['output']
    if output['format'] == 'json_object':
        body['response_format'] = {'type': 'json_object'}
    elif output['format'] == 'json_schema':
        body['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': output['schema_id'], 'schema': schema, 'strict': output['strict']}}
    body.update(deepcopy(backend.get('extra_body', {})))
    return body, warnings


def map_cloud_reasoning(provider, reasoning):
    """Map documented cloud reasoning fragments, without constructing a request."""
    if provider not in ('anthropic', 'gemini'):
        raise ValueError('expected cloud provider')
    fields, warnings = {}, []
    mode, effort, budget = reasoning['mode'], reasoning['effort'], reasoning['budget_tokens']
    if provider == 'anthropic':  # A: thinking.enabled, thinking.budget_tokens, output_config.effort
        if mode == 'on' and budget not in ('none', None):
            fields['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
        elif mode != 'auto':
            warnings.append(('PARAM_UNSUPPORTED', 'reasoning.mode'))
        if effort not in ('none', 'backend_default', None):
            fields['output_config'] = {'effort': effort}
        if budget not in ('none', None) and mode != 'on':
            warnings.append(('PARAM_UNSUPPORTED', 'reasoning.budget_tokens'))
    else:  # G: thinkingConfig.thinkingLevel and thinkingBudget; switch undocumented
        if mode != 'auto':
            warnings.append(('PARAM_UNSUPPORTED', 'reasoning.mode'))
        thinking = {}
        if effort not in ('none', 'backend_default', None):
            thinking['thinkingLevel'] = effort
        if budget not in ('none', None):
            thinking['thinkingBudget'] = budget
        if thinking:
            fields['thinkingConfig'] = thinking
    return fields, warnings
