"""Documented provider parameter spellings from T-009 provider-docs.md."""

from copy import deepcopy


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


def map_generation_fields(provider, role, capabilities=None):
    """Map only documented fields; return body fragment and warning names."""
    mapping = dict(SAMPLING[provider])
    capabilities = capabilities or {}
    if provider == 'openai_compatible':
        # L and V agree on these spellings; send only after a verified probe.
        for name in ('top_k', 'min_p'):
            if capabilities.get('sampling.' + name) is True:
                mapping[name] = name
    fields, warnings = {}, []
    maximum = {'openai_compatible': 'max_tokens', 'anthropic': 'max_tokens', 'gemini': 'maxOutputTokens'}
    fields[maximum[provider]] = role['max_tokens']  # O, A, G
    for name, value in role['sampling'].items():
        if value == 'none':
            continue
        if name not in mapping or (provider == 'openai_compatible'
                                   and capabilities.get('sampling.' + name) is not True):
            warnings.append(('PARAM_UNSUPPORTED', 'sampling.' + name))
        else:
            fields[mapping[name]] = deepcopy(value)
    # The surrounding request shape is not established by the cited excerpts.
    return fields, warnings


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
