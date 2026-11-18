from prompt_enhancer.model_mapping import map_cloud_reasoning, map_generation_fields


def test_repetition_penalty_local_mappings_and_unknown_backend():
    role = {'max_tokens': 64, 'sampling': {'repetition_penalty': 1.37}}
    for engine, field in [('llama_cpp', 'repeat_penalty'), ('vllm', 'repetition_penalty')]:
        body, warnings = map_generation_fields('openai_compatible', role, mapped=True, engine=engine)
        assert body[field] == 1.37 and warnings == []
    body, warnings = map_generation_fields('openai_compatible', role, mapped=True, engine='unknown')
    assert 'repetition_penalty' not in body and warnings == [('PARAM_UNSUPPORTED', 'sampling.repetition_penalty')]
    body, warnings = map_generation_fields('openai_compatible', role, mapped=False, engine='llama_cpp')
    assert 'repeat_penalty' not in body and warnings == [('PARAM_UNSUPPORTED', 'sampling.repetition_penalty')]
    # Injected capability evidence without an engine mapping keeps the old warning and drop behaviour.
    evidence = {'sampling.repetition_penalty': True}
    body, warnings = map_generation_fields('openai_compatible', role, evidence, mapped=False, engine='llama_cpp')
    assert body == {'max_tokens': 64} and warnings == [('PARAM_UNSUPPORTED', 'sampling.repetition_penalty')]


def test_undocumented_fields_warn_and_none_is_omitted():
    role = {'max_tokens': 64, 'sampling': {'temperature': 0.4, 'stop': 'none', 'top_k': 8}}
    body, warnings = map_generation_fields('anthropic', role)
    assert body == {'max_tokens': 64}
    assert warnings == [('PARAM_UNSUPPORTED', 'sampling.temperature'), ('PARAM_UNSUPPORTED', 'sampling.top_k')]


def test_gemini_documented_fields():
    role = {'max_tokens': 64, 'sampling': {'top_p': 0.8, 'seed': 5}}
    body, warnings = map_generation_fields('gemini', role)
    assert body == {'maxOutputTokens': 64, 'topP': 0.8, 'seed': 5}
    assert warnings == []


def test_local_engine_fields_require_probe():
    role = {'max_tokens': 32, 'sampling': {'top_k': 7, 'min_p': 0.1}}
    body, warnings = map_generation_fields('openai_compatible', role, {'sampling.top_k': True})
    assert body['top_k'] == 7 and 'min_p' not in body
    assert warnings == [('PARAM_UNSUPPORTED', 'sampling.min_p')]


def test_documented_cloud_reasoning_fragments_and_unsupported_switch():
    role = {'mode': 'on', 'effort': 'high', 'budget_tokens': 2048}
    fields, warnings = map_cloud_reasoning('anthropic', role)
    assert fields == {'thinking': {'type': 'enabled', 'budget_tokens': 2048}, 'output_config': {'effort': 'high'}}
    assert warnings == []
    fields, warnings = map_cloud_reasoning('gemini', role)
    assert fields == {'thinkingConfig': {'thinkingLevel': 'high', 'thinkingBudget': 2048}}
    assert warnings == [('PARAM_UNSUPPORTED', 'reasoning.mode')]
