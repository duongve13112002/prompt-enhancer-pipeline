"""KBS-F2 request contracts; every server is a labelled CPU double, never inference."""

import asyncio
import base64
from copy import deepcopy
import hashlib
import json

import pytest
import yaml
import httpx
from hypothesis import given, settings, strategies as st, HealthCheck

from prompt_enhancer.config import ConfigurationError, load_config
from prompt_enhancer.model_client import ModelCallError, ModelClientRun, safe_probe_events, _digest
from prompt_enhancer.model_mapping import openai_request
from prompt_enhancer import model_probe as probes
from test_kb_sync_production_probes import ProductionServer, loopback_server
from test_model_production_probes import production_env, bind_double, synthetic_provider_backend  # noqa: F401 - shared fixture


class CapturedShapeDouble(ProductionServer):
    """Synthetic replay of EKS-1's missing-control request and length envelope."""

    def __init__(self, controls, force_truncation=False):
        super().__init__()
        self.controls = controls
        self.force_truncation = force_truncation
        self.seen = []

    def answer(self, route, body):
        self.seen.append(deepcopy(body))
        if self.force_truncation or any(body.get(key) != value for key, value in self.controls.items()):
            return {'choices': [{'finish_reason': 'length', 'message': {
                'content': '', 'reasoning_content': 'LABELLED_PRIVATE_REASONING'}}],
                'usage': {'completion_tokens': 256}}
        return super().answer(route, body)


@pytest.mark.parametrize('force_truncation', [False, True])
def test_captured_run_shapes_with_role_controls_over_loopback(production_env, force_truncation):
    env = production_env
    role = env.config['vlm']['roles']['info_assessor']
    role['max_tokens'] = 256
    role['reasoning'].update(mode='off', template_kwargs_key='configured_switch')
    role['sampling'].update(temperature=0.37, top_p=0.73, seed=19)
    controls = {'max_tokens': 256, 'temperature': 0.37, 'top_p': 0.73, 'seed': 19,
                'chat_template_kwargs': {'configured_switch': False}}
    double = CapturedShapeDouble(controls, force_truncation)
    with loopback_server(double) as endpoint:
        env.config['vlm']['backends']['main']['base_urls'] = [endpoint]
        profile = env.root / 'captured-shapes.yaml'
        profile.write_text(yaml.safe_dump(env.config), encoding='utf-8')
        config = load_config(profile, 'kb sync', project_root=env.root)

        async def execute():
            run = ModelClientRun(config, env.root)
            try:
                bound = await run.for_role('info_assessor')
                if force_truncation:
                    with pytest.raises(ModelCallError) as caught:
                        await bound.call(b'synthetic unchanged system', '{"folder_name":"synthetic"}')
                    assert caught.value.code == 'PROBE_TRUNCATED'
                    assert not double.requests
                    assert 'LABELLED_PRIVATE_REASONING' not in json.dumps(caught.value.probe_evidence)
                else:
                    _, record = await bound.call(b'synthetic unchanged system', '{"folder_name":"synthetic"}')
                    assert record['status'] == 'OK'
                    assert not any(name.startswith('sampling.') or name.startswith('reasoning.')
                                   for name, value in run.probes[next(iter(run.probes))].capabilities.items()
                                   if value is True)
                assert all(all(body.get(key) == value for key, value in controls.items()) for body in double.seen)
            finally:
                await run.close()

        asyncio.run(execute())


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mode=st.sampled_from(['on', 'off', 'auto']), effort=st.sampled_from(['none', 'high', 'backend_default']),
       maximum=st.integers(1, 4096), temperature=st.floats(0, 2, allow_nan=False),
       top_p=st.floats(0, 1, allow_nan=False), top_k=st.integers(1, 100),
       seed=st.one_of(st.just('none'), st.integers(0, 1000)), key=st.sampled_from(['switch_a', 'switch_b']))
def test_probe_controls_differential_against_role_builder(production_env, mode, effort, maximum,
                                                         temperature, top_p, top_k, seed, key):
    env = production_env
    backend = env.config['vlm']['backends']['main']
    role = deepcopy(env.config['vlm']['roles']['folder_namer'])
    role['max_tokens'] = maximum
    role['sampling'].update(temperature=temperature, top_p=top_p, top_k=top_k, seed=seed,
                            stop=['synthetic-stop'])
    role['reasoning'].update(mode=mode, effort=effort, template_kwargs_key=key)
    bundle, images = probes.load_probe_bundle(env.root)
    expected, warnings = openai_request(backend, role, 'system', 'user', schema=probes.PROBE_SCHEMA, mapped=True)
    controls = set(expected) - {'messages', 'response_format'}
    for capability in ('text', 'json_schema', 'image', 'image+json_schema'):
        dropped = []
        cases = probes.production_probe_cases(bundle, images, backend, role, env.config['vision'],
                                              capability, 48, warnings=dropped)
        assert dropped == warnings
        for body, _ in cases.values():
            assert {name: body[name] for name in controls} == {name: expected[name] for name in controls}
            assert set(body) - {'messages', 'response_format'} == controls


CONTEXT_CHANGES = [
    ('role.max_tokens', 129), ('role.reasoning.mode', 'on'), ('role.reasoning.effort', 'high'),
    ('role.reasoning.budget_tokens', 7), ('role.reasoning.template_kwargs_key', 'another_switch'),
    ('role.sampling.temperature', 0.7), ('role.sampling.top_p', 0.7), ('role.sampling.top_k', 17),
    ('role.sampling.min_p', 0.2), ('role.sampling.frequency_penalty', 0.2),
    ('role.sampling.presence_penalty', 0.2), ('role.sampling.repetition_penalty', 1.2),
    ('role.sampling.seed', 11), ('role.sampling.stop', ['stop']),
    ('role.output.format', 'json_object'), ('role.output.schema_id', 'info_assessment.v0'),
    ('role.output.strict', False), ('role.output.transport', 'engine_native'),
    ('role.output.validate_response', False), ('role.image.source', 'user_reference'),
    ('role.image.max_side', 17), ('role.image.format', 'png'), ('role.image.quality', 27),
    ('role.image.transport', 'synthetic-transport'), ('vision.max_image_side', 17),
    ('vision.format', 'png'), ('vision.alpha_policy.background', '#123456'),
    ('backend.image.max_side', 17), ('backend.image.max_decode_pixels', 19000),
    ('backend.extra_body', {'marker': 'override'}), ('backend.model.id', 'different-model'),
    ('backend.model.revision', 'different-revision'), ('backend.engine.version', 'different-version'),
    ('backend.engine.name', 'vllm'), ('backend.endpoints.chat', 'another-chat'),
    ('backend.base_urls', ['http://127.0.0.1:9321']), ('backend.provider', 'anthropic'),
]


def change(config, role_name, dotted, value):
    group, *parts = dotted.split('.')
    target = (config['vlm']['roles'][role_name] if group == 'role' else
              config['vlm']['backends']['main'] if group == 'backend' else config['vision'])
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


@settings(max_examples=len(CONTEXT_CHANGES), suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(item=st.sampled_from(CONTEXT_CHANGES))
def test_each_request_setting_partitions_context(production_env, item):
    env = production_env
    run = ModelClientRun(env.config, env.root)
    role = run.config['vlm']['roles']['folder_namer']
    initial = run.client._proof_key('main', role)
    if item[0] == 'backend.provider':
        run.config['vlm']['backends']['main'] = synthetic_provider_backend(env, item[1])
    else:
        change(run.config, 'folder_namer', *item)
    assert run.client._proof_key('main', role) != initial, item


def test_identical_contexts_reuse_under_concurrent_role_binding(production_env):
    env = production_env
    env.config['vlm']['roles']['card_builder'] = deepcopy(env.config['vlm']['roles']['info_assessor'])

    async def execute():
        run = ModelClientRun(env.config, env.root)
        double = bind_double(run, env)
        try:
            await asyncio.gather(run.for_role('info_assessor'), run.for_role('card_builder'))
            assert len(double.probe_requests) == 2
            assert len(run.client._production_proofs) == 1
            assert run.client._probe_allowances['main'].sent == 2
        finally:
            await run.close()

    asyncio.run(execute())


def test_ordered_pair_uses_role_builder_and_extra_body(production_env):
    env = production_env
    backend = env.config['vlm']['backends']['main']
    backend['extra_body'] = {'temperature': 0.87, 'marker': 'synthetic-extra'}
    role = env.config['vlm']['roles']['folder_namer']
    role['reasoning']['budget_tokens'] = 31
    role['sampling']['repetition_penalty'] = 1.2

    async def execute():
        run = ModelClientRun(env.config, env.root)
        double = bind_double(run, env)
        try:
            bound = await run.for_role('folder_namer', require=('multiple_images',))
            expected, _ = openai_request(backend, role, None, 'user', schema=probes.PROBE_SCHEMA, mapped=True)
            for body in double.probe_requests[-2:]:
                assert {key: value for key, value in body.items() if key not in ('messages', 'response_format')} == {
                    key: value for key, value in expected.items() if key not in ('messages', 'response_format')}
                assert body['response_format']['json_schema']['strict'] is True
                assert body['response_format']['json_schema']['schema'] == probes.PROBE_SCHEMA
            _, images = probes.load_probe_bundle(env.root)
            _, record = await bound.call(b'system', '{"folder_name":"synthetic"}',
                                         images=[('knowledge_reference', data) for data in images])
            ordinary, ordered = record['probe_evidence'][0], record['probe_evidence'][-1]
            assert ordered['capability'] == 'multiple_images'
            assert ordered['context_hash'] == ordinary['context_hash'] == run.client._proof_key('main', role)[1]
            assert ordered['warnings'] == ordinary['warnings'] == [('PARAM_UNSUPPORTED', 'reasoning.budget_tokens')]
            assert all(body['repeat_penalty'] == 1.2 for body in double.probe_requests[-2:])
        finally:
            await run.close()

    asyncio.run(execute())


def test_prepared_image_provenance_matches_final_sent_payloads(production_env):
    env = production_env
    env.config['vlm']['backends']['main']['image']['max_side'] = 48

    async def execute():
        run = ModelClientRun(env.config, env.root)
        double = bind_double(run, env)
        try:
            await run.for_role('folder_namer', require=('multiple_images',))
            sent = {_digest(body): body for body in double.probe_requests}
            for event in run.probes['main'].evidence:
                expected = {}
                for name, digest in zip(event['prepared_image_hashes'], event['body_hashes']):
                    body = sent[digest]
                    content = body['messages'][-1]['content']
                    parts = content if isinstance(content, list) else []
                    expected[name] = [hashlib.sha256(base64.b64decode(part['image_url']['url'].split(',', 1)[1])).hexdigest()
                                      for part in parts if part['type'] == 'image_url']
                assert event['prepared_image_hashes'] == expected
                assert bool(any(expected.values())) is (event['capability'].startswith('image') or
                                                        event['capability'] == 'multiple_images')
            ordered = run.probes['main'].evidence[-1]['prepared_image_hashes']
            assert ordered['first'] == list(reversed(ordered['second']))
        finally:
            await run.close()

    asyncio.run(execute())


def test_resolved_writer_manifests_have_separate_proofs(production_env):
    """Production composition with a labelled writer transport double, no semantic-control proof."""
    from k2_fixtures import assessment

    env = production_env
    writer = deepcopy(env.config['vlm']['roles']['info_assessor'])
    writer['output'].update(format='from_profile_manifest', schema_id='from_profile_manifest')
    env.config['vlm']['roles']['writer'] = writer
    double = ProductionServer()

    def respond(request):
        body = json.loads(request.content)
        if body['messages'][-1]['content'] == 'synthetic writer generation':
            schema = body.get('response_format', {}).get('json_schema', {}).get('name')
            value = assessment() if schema == 'info_assessment.v0' else {'results': []}
            reply = {'choices': [{'finish_reason': 'stop', 'message': {
                'content': 'synthetic prose' if schema is None else json.dumps(value)}}]}
        else:
            reply = double.answer(request.url.path, body)
        return httpx.Response(200, json=reply)

    async def execute():
        run = ModelClientRun(env.config, env.root)
        bind_double(run, env, handler=respond)
        try:
            client = await run.for_role('writer')
            assert not double.probe_requests  # Manifest output has not been resolved yet.
            contexts = set()
            for schema in ('none', 'verifier_result.v0', 'info_assessment.v0'):
                for _ in range(2):
                    _, record = await client.call(b'exact admin system', 'synthetic writer generation',
                                                  manifest={'output_schema_id': schema})
                    assert record['status'] == 'OK'
                    contexts.add(record['probe_evidence'][0]['context_hash'])
            assert len(contexts) == len(run.client._production_proofs) == 3
            assert len(double.probe_requests) == 5
        finally:
            await run.close()

    asyncio.run(execute())


def test_concurrent_different_contexts_keep_their_own_call_evidence(production_env):
    env = production_env
    other = deepcopy(env.config['vlm']['roles']['info_assessor'])
    other['max_tokens'] += 1
    env.config['vlm']['roles']['card_builder'] = other

    async def execute():
        run = ModelClientRun(env.config, env.root)
        double = bind_double(run, env)
        try:
            clients = await asyncio.gather(run.for_role('info_assessor'), run.for_role('card_builder'))
            records = await asyncio.gather(*(client.call(b'system', '{"folder_name":"synthetic"}')
                                             for client in clients))
            assert len(double.probe_requests) == 4
            for role, (_, record) in zip(('info_assessor', 'card_builder'), records):
                assert record['status'] == 'OK'
                expected = run.client._proof_key('main', run.config['vlm']['roles'][role])[1]
                assert all(event['context_hash'] == expected for event in record['probe_evidence'])
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize('negative', [False, True])
@pytest.mark.parametrize('item', CONTEXT_CHANGES[:14])
def test_positive_and_negative_proofs_never_cross_contexts(production_env, negative, item):
    env = production_env
    env.config['vlm']['backends']['main']['probe']['max_requests'] = 4
    double = ProductionServer()

    def respond(request):
        reply = double.answer(request.url.path, json.loads(request.content))
        if negative:
            reply['choices'][0]['message']['content'] = '{"value":8}'
        return httpx.Response(200, json=reply)

    async def execute():
        run = ModelClientRun(env.config, env.root)
        bind_double(run, env, handler=respond)
        try:
            await run.for_role('info_assessor')
            first = next(iter(run.client._production_proofs.values()))
            await run.for_role('info_assessor')
            assert len(double.probe_requests) == 2
            change(run.config, 'info_assessor', *item)
            await run.for_role('info_assessor')
            assert len(double.probe_requests) == 4
            assert len(run.client._production_proofs) == 2
            assert first.capabilities['text'] is (not negative)
            assert run.client._probe_allowances['main'].remaining == 0
            change(run.config, 'info_assessor', 'role.max_tokens', 333)
            bound = await run.for_role('info_assessor')
            with pytest.raises(ModelCallError) as caught:
                await bound.call(b'system', '{}')
            assert caught.value.code == 'PROBE_INCOMPLETE'
            assert len(double.probe_requests) == 4
        finally:
            await run.close()

    asyncio.run(execute())


HOSTILE = st.recursive(st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=50)),
                       lambda children: st.one_of(st.lists(children, max_size=4),
                                                 st.dictionaries(st.text(max_size=12), children, max_size=4)), max_leaves=12)


def wrong_refusal_envelope(provider, wrong_type):
    if provider == 'openai_compatible':
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"value":7}', 'refusal': wrong_type}}]}
    return {'promptFeedback': {'blockReason': wrong_type},
            'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"value":7}'}]}}]}


@given(payload=HOSTILE, provider=st.sampled_from(['openai_compatible', 'anthropic', 'gemini']),
       wrong_type=st.one_of(st.booleans(), st.integers(), st.lists(HOSTILE, max_size=2),
                            st.dictionaries(st.text(max_size=8), HOSTILE, max_size=2)))
@settings(max_examples=70)
def test_hostile_reply_and_diagnostics_are_closed(payload, provider, wrong_type):
    verdict = probes.probe_verdict(httpx.Response(200, json=payload), provider, 7)
    assert type(verdict.success) is bool
    assert verdict.code is None if verdict.success else verdict.code in probes.PROBE_CODES
    event = {'role': 'observer', 'capability': 'image', 'status': 'negative', 'sent': 1,
             'reason': payload, 'challenges': [{'challenge': '0', 'status': 'negative', 'reason': payload}]}
    safe = safe_probe_events([event])[0]
    for result in (safe, safe['challenges'][0]):
        assert 'reason' not in result or result['reason'] in probes.PROBE_CODES
    for provider in ('openai_compatible', 'gemini'):
        malformed = wrong_refusal_envelope(provider, wrong_type)
        assert probes.probe_verdict(httpx.Response(200, json=malformed), provider, 7).code == 'PROBE_MALFORMED'


@pytest.mark.parametrize(('provider', 'payload', 'code'), [
    ('openai_compatible', wrong_refusal_envelope('openai_compatible', {'private': 'LABELLED'}), 'PROBE_MALFORMED'),
    ('gemini', wrong_refusal_envelope('gemini', {'private': 'LABELLED'}), 'PROBE_MALFORMED'),
    ('gemini', wrong_refusal_envelope('gemini', None), 'PROBE_MALFORMED'),
    ('openai_compatible', wrong_refusal_envelope('openai_compatible', None), None),
    ('openai_compatible', {'choices': [{'finish_reason': 'content_filter', 'message': {'content': None}}]},
     'PROBE_REFUSED'),
    ('gemini', {'promptFeedback': {'blockReason': 'SAFETY'}}, 'PROBE_REFUSED'),
    ('anthropic', {'stop_reason': 'end_turn', 'content': [{'type': False}, {'type': 'text', 'text': '{"value":7}'}]},
     'PROBE_MALFORMED'),
])
def test_probe_refusal_envelope_types_and_finish_categories(provider, payload, code):
    verdict = probes.probe_verdict(httpx.Response(200, json=payload), provider, 7)
    assert verdict.code == code
    assert verdict.success is (code is None)


GROUP_REPLIES = {
    'pass': None,
    'malformed': {'finish_reason': 'stop', 'message': {'content': 'LABELLED_NON_JSON_REPLY'}},
    'truncated': {'finish_reason': 'length', 'message': {'content': ''}},
    'transport': None,
}
GROUP_RESULTS = {
    'pass': ('complete', None), 'malformed': ('negative', 'PROBE_MALFORMED'),
    'truncated': ('negative', 'PROBE_TRUNCATED'), 'transport': ('failed', 'PROBE_FAILED'),
}


async def probe_unpreparable_ordered_pair(env, limit, eager, messages):
    """Answer the ordinary challenges from their pinned contract, independently of the overridden body."""
    profile = deepcopy(env.config)
    backend = profile['vlm']['backends']['main']
    backend['probe']['max_requests'] = limit
    backend['extra_body'] = {'messages': messages, 'temperature': 0.87}
    path = env.root / 'invalid-ordered-pair.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    config = load_config(path, 'kb sync', project_root=env.root)
    bundle, images = probes.load_probe_bundle(env.root)
    text = [bundle['text_answer']]
    counts = [item['answer'] for item in bundle['images']]
    expected = text + text + counts + counts
    seen = []

    def respond(request):
        index = len(seen)
        seen.append(json.loads(request.content))
        assert index < len(expected), 'invalid pair must make no ordered or generation send'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'value': expected[index]})}}]})

    run = ModelClientRun(config, env.root)
    bind_double(run, env, handler=respond)
    try:
        with pytest.raises((ModelCallError, ValueError)) as caught:
            required = ('multiple_images',) if eager else ()
            bound = await run.for_role('folder_namer', require=required)
            await bound.call(b'system', '{}', images=[('knowledge_reference', data) for data in images])
        error = caught.value
        allowance = run.client._probe_allowances['main']
        ordered = [event for event in safe_probe_events(run.client.probe_events)
                   if event['capability'] == 'multiple_images']
        attached = getattr(error, 'probe_evidence', [])
        if eager or limit == 8:
            print(f'ordered override cap={limit} eager={eager}: type={type(error).__name__} '
                  f'code={getattr(error, "code", None)} sent={allowance.sent} remaining={allowance.remaining} '
                  f'ordered_events={len(ordered)} attached={len(attached)}')
        assert isinstance(error, ModelCallError) and error.code == 'PROBE_FAILED'
        assert len(seen) == allowance.sent == 6 and allowance.remaining == limit - 6
        assert all(body['messages'] == messages and body['temperature'] == 0.87 for body in seen)
        proof = run.probes['main']
        assert proof.errors['multiple_images'] == 'PROBE_FAILED'
        assert proof.capabilities == dict.fromkeys(('text', 'json_schema', 'image', 'image+json_schema'), True)
        cases = [{'challenge': name, 'status': 'failed', 'reason': 'PROBE_FAILED'} for name in ('first', 'second')]
        event = {'role': 'folder_namer', 'capability': 'multiple_images', 'status': 'failed',
                 'sent': 6, 'reason': 'PROBE_FAILED', 'challenges': cases}
        assert ordered == [event] and event in attached
        evidence = proof.evidence[-1]
        assert all(evidence[key] == value for key, value in event.items())
        assert evidence['context_hash'] == proof.evidence[0]['context_hash']
        assert not evidence['body_hashes'] and not evidence['prepared_image_hashes']
        assert 'LABELLED_CONFIGURED_OVERRIDE' not in json.dumps(attached)
        # Repeated demand uses the recorded failed preparation and never spends a pair send.
        await run.for_role('folder_namer', require=('multiple_images',))
        assert run.probes['main'] is proof and allowance.sent == 6
        assert sum(event['capability'] == 'multiple_images' for event in run.client.probe_events) == 1
    finally:
        await run.close()


@pytest.mark.parametrize(('limit', 'eager'), [(8, True), (8, False), (6, True)])
def test_loaded_ordered_shape_override_reports_probe_failure(production_env, limit, eager):
    messages = [{'role': 'user', 'content': 'LABELLED_CONFIGURED_OVERRIDE'}]
    asyncio.run(probe_unpreparable_ordered_pair(production_env, limit, eager, messages))


async def probe_group_outcomes(env, ordered, outcomes, limit, *, call_error=None):
    """Send a generated pair of outcomes through the actual production binding and allowance."""
    config = deepcopy(env.config)
    backend = config['vlm']['backends']['main']
    before_group = 6 if ordered else 2
    backend['probe']['max_requests'] = before_group + limit
    backend['http']['max_retries'] = 0
    role, capability = ('folder_namer', 'multiple_images') if ordered else ('observer', 'image')
    double = ProductionServer()
    received = []

    def respond(request):
        index = len(received)
        body = json.loads(request.content)
        content = body['messages'][-1]['content']
        count = sum(part['type'] == 'image_url' for part in content) if isinstance(content, list) else 0
        received.append(count)
        if before_group <= index < before_group + limit:
            outcome = outcomes[index - before_group]
            if outcome == 'transport':
                return httpx.Response(503, text='LABELLED_HTTP_ERROR')
            if GROUP_REPLIES[outcome] is not None:
                return httpx.Response(200, json={'choices': [GROUP_REPLIES[outcome]]})
        return httpx.Response(200, json=double.answer(request.url.path, body))

    run = ModelClientRun(config, env.root)
    bind_double(run, env, handler=respond)
    try:
        bound = await run.for_role(role, require=('multiple_images',) if ordered else ())
        assert received == ([0, 0, 1, 1, 1, 1] if ordered else [0, 0]) + [2 if ordered else 1] * limit
        allowance = run.client._probe_allowances['main']
        assert allowance.sent == len(received) and allowance.remaining == 0
        if call_error is not None:
            _, images = probes.load_probe_bundle(env.root)
            used = images if ordered else images[:1]
            with pytest.raises(ModelCallError) as caught:
                await bound.call(b'system', '{}', images=[('knowledge_reference', data) for data in used])
            assert caught.value.code == call_error
            assert 'LABELLED_' not in json.dumps(caught.value.probe_evidence)
        result = run.probes['main']
        event = next(event for event in safe_probe_events(run.client.probe_events) if event['capability'] == capability)
        evidence = next(item for item in result.evidence if item['capability'] == capability)
        return result, event, evidence, len(received)
    finally:
        await run.close()


@pytest.mark.parametrize('ordered', [False, True])
@pytest.mark.parametrize(('outcomes', 'limit', 'code'), [
    (('transport', 'malformed'), 2, 'PROBE_FAILED'), (('truncated', 'pass'), 1, 'PROBE_INCOMPLETE'),
])
def test_probe_pair_review_failure_scenarios(production_env, ordered, outcomes, limit, code):
    result, event, _, sends = asyncio.run(probe_group_outcomes(production_env, ordered, outcomes, limit, call_error=code))
    assert result.errors[event['capability']] == event['reason'] == code
    print(f'{event["capability"]} {outcomes} cap={sends}: {code}, cases={event["challenges"]}')


def test_ordinary_proof_keeps_each_typed_reason(production_env):
    _, event, evidence, _ = asyncio.run(probe_group_outcomes(production_env, False, ('malformed', 'truncated'), 2))
    assert [case['reason'] for case in event['challenges']] == ['PROBE_MALFORMED', 'PROBE_TRUNCATED']
    assert evidence['challenges'] == event['challenges']
    assert 'LABELLED_NON_JSON_REPLY' not in json.dumps(evidence)


@given(outcomes=st.tuples(*(st.sampled_from(tuple(GROUP_RESULTS)) for _ in range(2))), limit=st.integers(0, 2),
       invalid_pair=st.sampled_from([None, [], {'wrong': 'container'},
                                     [{'role': 'user', 'content': [12]}]]))
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_probe_pair_outcome_property(production_env, outcomes, limit, invalid_pair):
    if invalid_pair is not None:
        asyncio.run(probe_unpreparable_ordered_pair(production_env, 6 + limit, False, invalid_pair))
    seen = outcomes[:limit]
    cases = [GROUP_RESULTS[outcome] for outcome in seen] + [('incomplete', 'PROBE_INCOMPLETE')] * (2 - limit)
    if 'transport' in seen:
        expected = ('failed', 'PROBE_FAILED')
    elif limit < 2:
        expected = ('incomplete', 'PROBE_INCOMPLETE')
    else:
        expected = next((case for case in cases if case[0] == 'negative'), ('complete', None))
    for ordered in (False, True):
        proof, event, evidence, _ = asyncio.run(probe_group_outcomes(production_env, ordered, outcomes, limit))
        capability = event['capability']
        assert (event['status'], event.get('reason')) == expected
        assert proof.errors.get(capability) == expected[1]
        assert proof.capabilities.get(capability) is {'complete': True, 'negative': False}.get(expected[0])
        assert [(case['status'], case.get('reason')) for case in event['challenges']] == cases
        assert evidence['challenges'] == event['challenges']


@given(content=HOSTILE, provider=st.sampled_from(['openai_compatible', 'anthropic', 'gemini']))
@settings(max_examples=40)
def test_image_provenance_parser_refuses_hostile_payloads(content, provider):
    values = probes.probe_image_hashes({'messages': [{'content': content}]}, provider)
    assert all(type(value) is str and len(value) == 64 and set(value) <= set('0123456789abcdef')
               for value in values)


@pytest.mark.parametrize(('reply', 'code'), [
    ({'choices': [{'finish_reason': 'length', 'message': {'content': ''}}]}, 'PROBE_TRUNCATED'),
    ({'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'PRIVATE', 'content': ''}}]}, 'PROBE_REFUSED'),
    ({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"value":true}'}}]}, 'PROBE_MALFORMED'),
    ({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"value":8}'}}]}, 'PROBE_WRONG_ANSWER'),
])
def test_typed_failure_reaches_checkpoint_and_stage_summary(production_env, monkeypatch, reply, code):
    from k2_fixtures import folder, scan
    from prompt_enhancer.k2_naming import name_folders
    from prompt_enhancer.kb_sync import require_success
    from prompt_enhancer.model_transport import ModelTransport
    from prompt_enhancer.storage import StorageError

    env = production_env
    folder(env, 'declared', 'Name\nSynthetic description')
    original = ModelTransport.__init__

    def init(transport, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(200, json=reply))
        original(transport, *args, **kwargs)

    monkeypatch.setattr(ModelTransport, '__init__', init)

    async def execute():
        scanned = scan(env)
        result = await name_folders(env.store, env.config, scanned['handoff']['k2'], images=scanned['rows']['images'],
                                    folder_images=scanned['rows']['folder_images'], knowledge_root=env.source)
        with pytest.raises(StorageError, match='codes=' + code + '=1'):
            require_success('k2_naming', result)

    asyncio.run(execute())
    with env.store.writer() as writer:
        row = next(row for row in writer._state_rows().values() if row['stage'] == 'model_probe_failure')
        document = json.loads(writer.checkpoints('model_probe_failure').load(row['input_key']))
        assert document['error'] == code
        assert all(event['reason'] == code for event in document['probes'])
        assert 'PRIVATE' not in json.dumps(document)


@pytest.mark.parametrize(('path', 'value'), [
    ('role.max_tokens', 385), ('role.reasoning.mode', 'on'),
    ('role.reasoning.template_kwargs_key', 'switch_override'), ('role.reasoning.effort', 'high'),
    ('role.reasoning.budget_tokens', 31), ('role.sampling.temperature', 0.6),
    ('role.sampling.top_p', 0.6), ('role.sampling.top_k', 14), ('role.sampling.min_p', 0.3),
    ('role.sampling.frequency_penalty', 0.3), ('role.sampling.presence_penalty', 0.3),
    ('role.sampling.seed', 13), ('role.sampling.stop', ['end']), ('role.sampling.repetition_penalty', 1.2),
])
def test_loaded_nondefault_control_override_in_probe(production_env, path, value):
    env = production_env
    change(env.config, 'info_assessor', path, value)
    if path == 'role.reasoning.effort':
        env.config['vlm']['backends']['main']['engine'] = {'name': 'vllm', 'version': '0.11.0'}
    profile = env.root / 'control-override.yaml'
    profile.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    loaded = load_config(profile, 'kb sync', project_root=env.root)

    async def execute():
        run = ModelClientRun(loaded, env.root)
        double = bind_double(run, env)
        try:
            await run.for_role('info_assessor')
            expected, warnings = openai_request(loaded['vlm']['backends']['main'],
                loaded['vlm']['roles']['info_assessor'], None, 'synthetic', schema=probes.PROBE_SCHEMA, mapped=True)
            for body in double.probe_requests:
                assert {key: val for key, val in body.items() if key not in ('messages', 'response_format')} == {
                    key: val for key, val in expected.items() if key not in ('messages', 'response_format')}
                # Observe the configured override directly as well as checking the shared builder.
                leaf = path.rsplit('.', 1)[-1]
                if leaf == 'budget_tokens':
                    assert ('PARAM_UNSUPPORTED', path.removeprefix('role.')) in warnings
                elif leaf == 'repetition_penalty':
                    assert body['repeat_penalty'] == value
                elif leaf == 'mode':
                    switch = loaded['vlm']['roles']['info_assessor']['reasoning']['template_kwargs_key']
                    assert body['chat_template_kwargs'][switch] is (value == 'on')
                elif leaf == 'template_kwargs_key':
                    assert body['chat_template_kwargs'] == {value: False}
                else:
                    assert body['reasoning_effort' if leaf == 'effort' else leaf] == value
            assert all(item['warnings'] == warnings for item in run.probes['main'].evidence)
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize(('engine', 'version', 'effort', 'sent_effort'), [
    ('vllm', '0.11.0', 'low', 'low'), ('vllm', '0.11.0', 'medium', 'medium'), ('vllm', '0.11.0', 'high', 'high'),
    ('vllm', '0.11.0', 'max', None), ('vllm', '0.11.0', 'xhigh', None),
    ('vllm', '0.33.0', 'high', None), ('llama_cpp', '0.11.0', 'high', None),
])
def test_loaded_reasoning_effort_uses_documented_local_enum(production_env, engine, version, effort, sent_effort):
    env = production_env
    config = deepcopy(env.config)
    config['vlm']['backends']['main']['engine'] = {'name': engine, 'version': version}
    config['vlm']['roles']['folder_namer']['reasoning']['effort'] = effort
    profile = env.root / 'effort-profile.yaml'
    profile.write_text(yaml.safe_dump(config), encoding='utf-8')
    loaded = load_config(profile, 'kb sync', project_root=env.root)
    assert loaded['vlm']['roles']['folder_namer']['reasoning']['effort'] == effort
    double = ProductionServer()
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if (engine, version) == ('vllm', '0.11.0') and 'reasoning_effort' in body:
            if body['reasoning_effort'] not in ('low', 'medium', 'high'):
                return httpx.Response(400, text='LABELLED_VLLM_ENUM_REFUSAL')
        return httpx.Response(200, json=double.answer(request.url.path, body))

    async def execute():
        run = ModelClientRun(loaded, env.root)
        bind_double(run, env, handler=respond)
        try:
            bound = await run.for_role('folder_namer', require=('multiple_images',))
            _, images = probes.load_probe_bundle(env.root)
            _, record = await bound.call(b'system', '{"folder_name":"synthetic"}',
                                         images=[('knowledge_reference', data) for data in images])
            assert len(seen) == 9 and len(double.probe_requests) == 8 and len(double.requests) == 1
            if sent_effort is None:
                assert all('reasoning_effort' not in body for body in seen)
            else:
                assert all(body['reasoning_effort'] == sent_effort for body in seen)
            warning = ('PARAM_UNSUPPORTED', 'reasoning.effort')
            for item in [record, *record['probe_evidence']]:
                assert (warning in item['warnings']) is (sent_effort is None)
            print(f'{engine} {version} effort={effort}: wire={sent_effort}, warned={warning in record["warnings"]}')
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize(('field', 'value'), [('output', 'engine_native'), ('image', 'alternate_transport')])
def test_loaded_naming_transport_override_refused_before_sends(production_env, monkeypatch, capsys, field, value):
    from prompt_enhancer import __main__ as cli

    env = production_env
    config = deepcopy(env.config)
    config['vlm']['roles']['folder_namer'][field]['transport'] = value
    profile = env.root / 'transport-override.yaml'
    profile.write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setattr(cli, '__file__', str(env.root / 'src/prompt_enhancer/__main__.py'))
    monkeypatch.setattr(cli, 'sync_config', lambda *args, **kwargs: pytest.fail('unexpected sync after loader refusal'))
    assert cli.main(['kb', 'sync', '--config', str(profile)]) == 1
    assert f'vlm.roles.folder_namer.{field}.transport' in capsys.readouterr().err


@pytest.mark.parametrize(('field', 'value'), [('output', 'engine_native'), ('image', 'alternate_transport')])
def test_unimplemented_transport_override_leaves_builder_wire_format(production_env, field, value):
    env = production_env
    backend = env.config['vlm']['backends']['main']
    role = deepcopy(env.config['vlm']['roles']['folder_namer'])
    _, images = probes.load_probe_bundle(env.root)
    opts = {'schema': probes.PROBE_SCHEMA, 'images': [('image/png', images[0])], 'mapped': True}
    expected, _ = openai_request(backend, role, 'system', 'user', **opts)
    role[field]['transport'] = value
    body, _ = openai_request(backend, role, 'system', 'user', **opts)
    assert body == expected
    assert body['response_format']['type'] == 'json_schema'
    assert body['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')


@pytest.mark.parametrize('field', ['max_tokens', 'reasoning', 'sampling', 'reasoning.mode',
    'reasoning.effort', 'reasoning.budget_tokens', 'reasoning.template_kwargs_key', 'sampling.temperature',
    'sampling.top_p', 'sampling.top_k', 'sampling.min_p', 'sampling.frequency_penalty',
    'sampling.presence_penalty', 'sampling.repetition_penalty', 'sampling.seed', 'sampling.stop'])
@pytest.mark.parametrize('invalid', ['missing', 'design_required', {'invalid': True}])
def test_control_configuration_refusal(production_env, field, invalid):
    env = production_env
    target = env.config['vlm']['roles']['info_assessor']
    parts = field.split('.')
    for part in parts[:-1]:
        target = target[part]
    if invalid == 'missing':
        target.pop(parts[-1])
    else:
        target[parts[-1]] = invalid
    profile = env.root / 'control-refusal.yaml'
    profile.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(profile, 'kb sync', project_root=env.root)
