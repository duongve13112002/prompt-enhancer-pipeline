"""Production proof, integrity and accounting contracts using labelled HTTP doubles."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import httpx
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
import yaml

from prompt_enhancer import model_probe as probes, model_client
from prompt_enhancer.config import ConfigurationError, load_config
from prompt_enhancer.k2_naming import failure_code
from prompt_enhancer.kb_sync import require_success
from prompt_enhancer.model_client import (
    MODEL_CODES, ModelCallError, ModelClientRun, compose_model_client, safe_model_code, safe_probe_events,
)
from prompt_enhancer.model_resources import BudgetExhausted, SingleRunBudgetLedger
from prompt_enhancer.model_transport import ModelTransport
from prompt_enhancer.storage import StorageError
from test_kb_sync_production_probes import ProductionServer, loopback_server


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def production_env(k2_env):
    target = k2_env.root / probes.BUNDLE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / probes.BUNDLE_PATH, target)
    k2_env.config['embedding']['query_text_template'] = '{text}'
    k2_env.config['vlm']['backends']['main']['probe'] = {'max_requests': 8, 'timeout_s': 1}
    return k2_env


def bind_double(run, env, double=None, handler=None):
    double = ProductionServer() if double is None else double

    def respond(request):
        return httpx.Response(200, json=double.answer(request.url.path, json.loads(request.content)))

    run.transports['main'] = ModelTransport('main', env.config['vlm']['backends']['main'],
        run.concurrency, run.ledger, transport=httpx.MockTransport(respond if handler is None else handler))
    return double


def synthetic_provider_backend(env, provider, *, budget=5, policy='stop'):
    """Resolve and validate a whole example backend; never load a credential or call it."""
    names = {'openai_compatible': 'main', 'anthropic': 'cloud_claude'}
    examples = yaml.safe_load((REPO / 'configs/enhancer.example.yaml').read_bytes())
    backend = deepcopy(examples['vlm']['backends'][names[provider]])
    local = env.config['vlm']['backends']['main']
    for key in ('http', 'probe', 'image', 'max_concurrency', 'model', 'base_urls'):
        backend[key] = deepcopy(local[key])
    if provider == 'anthropic':
        backend.update(endpoints={'chat': 'messages', 'health': 'health'},
                       anthropic_version='2025-01-01', anthropic_workspace_id='none',
                       call_budget=budget, on_budget_exhausted=policy, allow_images=True)
    else:
        backend.update(engine=deepcopy(local['engine']), endpoints=deepcopy(local['endpoints']))
    profile = deepcopy(env.config)
    profile['vlm']['backends']['probe_fixture'] = backend
    path = env.root / 'synthetic-provider-fixture.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    return load_config(path, 'kb status', project_root=env.root)['vlm']['backends']['probe_fixture']


@pytest.mark.parametrize('limit', [1, 2, 3, 5, 6, 8])
def test_loaded_probe_allowance_and_lazy_role_extension(production_env, limit):
    env = production_env
    env.config['vlm']['backends']['main']['probe']['max_requests'] = limit
    profile = env.root / 'probe-override.yaml'
    profile.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    loaded = load_config(profile, 'kb sync', project_root=env.root)

    async def execute():
        run = ModelClientRun(loaded, env.root)
        double = bind_double(run, env)
        try:
            await run.for_role('info_assessor')
            assert len(double.probe_requests) == min(limit, 2)
            await run.for_role('observer')
            assert len(double.probe_requests) == min(limit, 8)  # Different output/image contexts do not share text proof.
            first = len(double.probe_requests)
            await run.for_role('observer')
            assert len(double.probe_requests) == first
            await run.for_role('folder_namer', require=('multiple_images',))
            assert len(double.probe_requests) == min(limit, 8)
            assert run.client._probe_allowances['main'].sent == len(double.probe_requests)
            assert run.probes['main'].capabilities.get('multiple_images') is None  # Earlier contexts used the cap.
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize('key', ['max_requests', 'timeout_s'])
@pytest.mark.parametrize('invalid', ['missing', 'design_required', 'deploy_required', True, 0, -1, float('inf')])
def test_probe_profile_missing_sentinel_and_invalid_refused(production_env, key, invalid):
    env = production_env
    value = env.config['vlm']['backends']['main']['probe']
    if invalid == 'missing':
        del value[key]
    else:
        value[key] = invalid
    profile = env.root / 'probe-refusal.yaml'
    profile.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='probe'):
        load_config(profile, 'kb sync', project_root=env.root)


@pytest.mark.parametrize(('timeout', 'status'), [(0.005, 'failed'), (0.2, 'complete')])
def test_loaded_probe_timeout_changes_observed_outcome(production_env, timeout, status):
    env = production_env
    env.config['vlm']['backends']['main']['probe']['timeout_s'] = timeout
    profile = env.root / 'probe-timeout.yaml'
    profile.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    loaded = load_config(profile, 'kb sync', project_root=env.root)

    async def respond(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=ProductionServer().answer(request.url.path, json.loads(request.content)))

    async def execute():
        run = ModelClientRun(loaded, env.root)
        bind_double(run, env, handler=respond)
        try:
            await run.for_role('info_assessor')
            assert all(event['status'] == status for event in run.client.probe_events)
            sent = run.client._probe_allowances['main'].sent
            await run.for_role('info_assessor')
            assert run.client._probe_allowances['main'].sent == sent
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize(('failure', 'code'), [('negative', 'PROBE_MALFORMED'),
    ('failed', 'PROBE_FAILED'), ('incomplete', 'PROBE_INCOMPLETE')])
def test_required_proof_failure_reason_and_no_repeat(production_env, failure, code):
    env = production_env
    if failure == 'incomplete':
        env.config['vlm']['backends']['main']['probe']['max_requests'] = 1
    double = ProductionServer()
    seen = []

    def respond(request):
        seen.append(request)
        if failure == 'failed':
            raise httpx.ReadError('LABELLED_PRIVATE_RESPONSE')
        reply = double.answer(request.url.path, json.loads(request.content))
        if failure == 'negative':
            reply['choices'][0]['message']['content'] = '{"value":false}'
        return httpx.Response(200, json=reply)

    async def execute():
        run = ModelClientRun(env.config, env.root)
        bind_double(run, env, handler=respond)
        try:
            client = await run.for_role('info_assessor')
            for _ in range(2):
                with pytest.raises(ModelCallError) as caught:
                    await client.call(b'unchanged system', '{}')
                assert caught.value.code == code
                assert caught.value.probe_evidence
                assert 'LABELLED_PRIVATE_RESPONSE' not in json.dumps(caught.value.probe_evidence)
            assert len(seen) <= 2
        finally:
            await run.close()

    asyncio.run(execute())


def test_composition_without_injected_probes_and_optional_warnings(production_env, monkeypatch):
    env = production_env
    double = ProductionServer()
    original = ModelTransport.__init__

    def init(transport, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(200,
            json=double.answer(request.url.path, json.loads(request.content))))
        original(transport, *args, **kwargs)

    monkeypatch.setattr(ModelTransport, '__init__', init)

    async def execute():
        client = await compose_model_client(env.config, 'info_assessor', project_root=env.root)
        try:
            value, record = await client.call(b'unchanged', '{"folder_name":"synthetic"}')
            assert value['usable'] is True and record['status'] == 'OK'
            assert record['probe_evidence']
            assert ('PARAM_UNSUPPORTED', 'sampling.repetition_penalty') not in record['warnings']
            assert double.requests[-1]['repeat_penalty'] == env.config['vlm']['roles']['info_assessor']['sampling']['repetition_penalty']
            assert 'reasoning.mode' not in client.run.probes['main'].capabilities  # Acceptance is not semantic proof.
            assert double.requests[-1]['temperature'] == env.config['vlm']['roles']['info_assessor']['sampling']['temperature']
            assert double.requests[-1]['chat_template_kwargs'] == {'enable_thinking': False}
        finally:
            await client.close()

    asyncio.run(execute())


@pytest.mark.parametrize('cloud', [False, True])
def test_retries_are_actual_sends_and_blocked_send_does_not_spend_cloud_grant(production_env, cloud):
    env = production_env
    backend = synthetic_provider_backend(env, 'anthropic' if cloud else 'openai_compatible')
    backend['http'].update(max_retries=3, retry_statuses=[503])
    ledger = SingleRunBudgetLedger({'main': 5})
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(503, json={'error': 'labelled transient failure'})

    async def execute():
        run = ModelClientRun(env.config, env.root)
        transport = ModelTransport('main', backend, run.concurrency, ledger,
            transport=httpx.MockTransport(respond))
        allowance = probes.ProbeAllowance(2)
        try:
            with pytest.raises(probes.ProbeLimitExhausted):
                await transport.post_probe('chat', {}, {}, allowance)
            assert len(seen) == allowance.sent == 2
            assert ledger._remaining['main'] == (3 if cloud else 5)
        finally:
            await transport.close()
            await run.close()

    asyncio.run(execute())


def test_retries_and_later_roles_share_production_send_cap(production_env):
    env = production_env
    backend = env.config['vlm']['backends']['main']
    backend['probe']['max_requests'] = 3
    backend['http'].update(max_retries=1, retry_statuses=[503])
    seen = []
    double = ProductionServer()

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=double.answer(request.url.path, json.loads(request.content)))

    async def execute():
        run = ModelClientRun(env.config, env.root)
        bind_double(run, env, handler=respond)
        try:
            await run.for_role('info_assessor')
            assert run.probes['main'].capabilities['text'] is True
            assert run.probes['main'].capabilities['json_schema'] is True
            await run.for_role('observer')
            await run.for_role('folder_namer', require=('multiple_images',))
            assert len(seen) == 3
            assert run.client._probe_allowances['main'].remaining == 0
            assert run.probes['main'].errors['image'] == 'PROBE_INCOMPLETE'
            assert run.probes['main'].errors['multiple_images'] == 'PROBE_INCOMPLETE'
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize('entry', ['bind', 'call'])
def test_escaping_probe_allowance_remains_incomplete(production_env, monkeypatch, entry):
    """Labelled probe-boundary refusal double; no request or negative evidence exists."""
    env = production_env
    config = cloud_profile(env, budget=5, policy='fallback_to_default_backend')
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: {})

    async def refuse(*args, **kwargs):
        raise probes.ProbeLimitExhausted()

    monkeypatch.setattr(model_client, 'probe_backend', refuse)

    async def execute():
        run = ModelClientRun(config, env.root)
        bind_cloud_double(run, CloudProbeDouble())
        try:
            with pytest.raises(ModelCallError) as caught:
                if entry == 'bind':
                    await run.for_role('card_builder')
                else:
                    await run.client.call('card_builder', b'system', '{"request":"generation"}')
            assert caught.value.code == 'PROBE_INCOMPLETE'
            assert caught.value.probe_evidence[-1]['status'] == 'incomplete'
            assert all(not attempts for attempts in run.client._production_attempted.values())
            assert 'cloud_claude' not in run.probes and 'main' not in run.probes
            assert run.ledger._remaining['cloud_claude'] == 5 and not run.ledger._active
        finally:
            await run.close()

    asyncio.run(execute())


def test_image_plus_schema_proof_is_required_separately(production_env):
    env = production_env
    double = ProductionServer()

    def respond(request):
        body = json.loads(request.content)
        reply = double.answer(request.url.path, body)
        if isinstance(body['messages'][-1]['content'], list) and 'response_format' in body:
            reply['choices'][0]['message']['content'] = '{}'
        return httpx.Response(200, json=reply)

    async def execute():
        run = ModelClientRun(env.config, env.root)
        bind_double(run, env, handler=respond)
        try:
            client = await run.for_role('observer')
            proof = run.probes['main'].capabilities
            assert proof['image'] is True and proof['json_schema'] is True
            assert proof['image+json_schema'] is False
            _, images = probes.load_probe_bundle(env.root)
            with pytest.raises(ModelCallError) as caught:
                await client.call(b'system', '{}', images=[('knowledge_reference', images[0])])
            assert caught.value.code == 'PROBE_MALFORMED'
            assert not double.requests
        finally:
            await run.close()

    asyncio.run(execute())


def test_local_and_ordinary_injected_evidence_is_combined(production_env):
    env = production_env
    bundle, images = probes.load_probe_bundle(env.root)
    backend = env.config['vlm']['backends']['main']
    role = env.config['vlm']['roles']['observer']
    side = model_client.image_max_side(env.config, role['image'], backend)
    image_cases = probes.production_probe_cases(bundle, images, backend, role, env.config['vision'], 'image', side)
    image_plan = tuple((case[0], answer) for case, answer in zip(image_cases.values(), (1, 2)))
    body = probes.production_probe_cases(bundle, images, backend, role, env.config['vision'], 'text', None)['0'][0]
    enabled, disabled = deepcopy(body), deepcopy(body)
    key = role['reasoning']['template_kwargs_key']
    enabled['chat_template_kwargs'], disabled['chat_template_kwargs'] = {key: True}, {key: False}
    ordinary = probes.production_probe_cases(bundle, images, backend, role, env.config['vision'], 'json_schema', None)['0']
    backend['probe']['max_requests'] = 5
    double = ProductionServer()

    def respond(request):
        body = json.loads(request.content)
        reply = double.answer(request.url.path, body)
        reply['thinking_observed'] = body.get('chat_template_kwargs', {}).get(key)
        return httpx.Response(200, json=reply)

    async def execute():
        run = ModelClientRun(env.config, env.root,
            probe_cases={'main': {'json_schema': ordinary}}, local_probe_plans={'main': {
                'image': image_plan, 'thinking': (enabled, disabled, key, lambda response: response.json()['thinking_observed'])}})
        bind_double(run, env, handler=respond)
        try:
            await run.for_role('observer')
            assert all(run.probes['main'].capabilities.get(name) is True
                       for name in ('image', 'reasoning.mode', 'json_schema'))
            assert len(double.probe_requests) == 5
        finally:
            await run.close()

    asyncio.run(execute())


def test_cloud_budget_refusal_makes_no_probe_send(production_env):
    env = production_env
    backend = synthetic_provider_backend(env, 'anthropic', budget=0)

    async def execute():
        run = ModelClientRun(env.config, env.root)
        transport = ModelTransport('main', backend, run.concurrency, SingleRunBudgetLedger({'main': 0}),
            transport=httpx.MockTransport(lambda request: pytest.fail('no grant must make no HTTP request')))
        allowance = probes.ProbeAllowance(2)
        try:
            with pytest.raises(BudgetExhausted):
                await transport.post_probe('chat', {}, {}, allowance)
            assert allowance.sent == 0
        finally:
            await transport.close()
            await run.close()

    asyncio.run(execute())


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(index=st.integers(min_value=0, max_value=1499), mask=st.integers(min_value=1, max_value=255))
def test_bundle_mutated_bytes_refused_before_parsing(tmp_path, monkeypatch, index, mask):
    raw = bytearray((REPO / probes.BUNDLE_PATH).read_bytes())
    raw[index % len(raw)] ^= mask
    target = tmp_path / probes.BUNDLE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    with pytest.raises(probes.ProbeArtifactError):
        probes.load_probe_bundle(tmp_path)


@pytest.mark.parametrize('ending', [b'\n', b'\r\n'])
def test_bundle_valid_identity_size_hash_and_images(tmp_path, ending):
    raw = (REPO / probes.BUNDLE_PATH).read_bytes().rstrip(b'\r\n') + ending
    target = tmp_path / probes.BUNDLE_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    bundle, images = probes.load_probe_bundle(tmp_path)
    assert bundle['id'] == probes.BUNDLE_ID
    assert (len(raw), hashlib.sha256(raw).hexdigest()) in probes.BUNDLE_PINS
    assert [len(image) for image in images] == [item['size_bytes'] for item in bundle['images']]


@pytest.mark.parametrize('kind', ['identity', 'size', 'hash', 'image_size', 'image_hash', 'image_shape'])
def test_independent_bundle_pins_and_embedded_validation(production_env, monkeypatch, kind):
    env = production_env
    target = env.root / probes.BUNDLE_PATH
    raw = target.read_bytes()
    if kind == 'identity':
        monkeypatch.setattr(probes, 'BUNDLE_ID', 'wrong-identity')
    elif kind == 'size':
        monkeypatch.setattr(probes, 'BUNDLE_PINS', {(len(raw) + 1, hashlib.sha256(raw).hexdigest())})
    elif kind == 'hash':
        monkeypatch.setattr(probes, 'BUNDLE_PINS', {(len(raw), '0' * 64)})
    else:
        # Labelled repinned malformed artifact tests the nested validator separately from its outer pin.
        bundle = json.loads(raw)
        field = {'image_size': 'size_bytes', 'image_hash': 'sha256', 'image_shape': 'width'}[kind]
        bundle['images'][0][field] = 'wrong' if field == 'sha256' else 0
        raw = json.dumps(bundle).encode()
        target.write_bytes(raw)
        monkeypatch.setattr(probes, 'BUNDLE_PINS', {(len(raw), hashlib.sha256(raw).hexdigest())})
    with pytest.raises(probes.ProbeArtifactError):
        probes.load_probe_bundle(env.root)


@settings(max_examples=30)
@given(payload=st.one_of(st.none(), st.booleans(), st.integers(), st.text(),
    st.lists(st.integers(), max_size=3), st.dictionaries(st.text(max_size=8), st.integers(), max_size=3)))
def test_probe_reply_strict_structure_property(payload):
    response = httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
        'message': {'content': json.dumps(payload)}}]})
    expected = type(payload) is dict and set(payload) == {'value'} and type(payload['value']) is int and payload['value'] == 7
    assert probes.verify_probe_reply(response, 'openai_compatible', 7) is expected


@pytest.mark.parametrize('value', [8, -1, True, False, 7.0, None, '7'])
def test_wrong_probe_answer_is_never_positive(value):
    response = httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
        'message': {'content': json.dumps({'value': value})}}]})
    assert probes.verify_probe_reply(response, 'openai_compatible', 7) is False


@settings(max_examples=30)
@given(value=st.one_of(st.text(), st.none(), st.integers(), st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=2)))
def test_structural_failure_code_property(value):
    code = safe_model_code(value)
    assert code in MODEL_CODES
    assert failure_code(ModelCallError('PRIVATE_MODEL_TEXT', code=value)) == code
    with pytest.raises(StorageError) as caught:
        require_success('k2_naming', {'report': {'failed': {'item': value}}})
    assert 'PRIVATE_MODEL_TEXT' not in str(caught.value)
    assert safe_probe_events([{'role': value, 'capability': value, 'status': value, 'sent': value}]) == []


def test_bundle_escaping_directory_link_refused_before_read(tmp_path):
    from test_storage_anchored import _dir_link, _drop_link

    project = tmp_path / 'project'
    project.mkdir()
    (project / 'contracts').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    shutil.copyfile(REPO / probes.BUNDLE_PATH, outside / probes.BUNDLE_PATH.name)
    link = project / 'contracts/probes'
    _dir_link(link, outside)
    try:
        with pytest.raises(probes.ProbeArtifactError):
            probes.load_probe_bundle(project)
    finally:
        _drop_link(link)


def test_standalone_stages_use_production_proofs(production_env, monkeypatch):
    from k2_fixtures import folder, scan
    from k2_observe_fixtures import k1_result
    from prompt_enhancer.k2_naming import name_folders
    from prompt_enhancer.k2_observe import observe_images
    from prompt_enhancer.k3_cards import build_cards
    from prompt_enhancer.k4_aliases import build_aliases
    from prompt_enhancer.storage.store import new_snapshot_id

    env = production_env
    folder(env, 'declared', 'Standalone\nSynthetic description', count=2)
    double = ProductionServer()
    original = ModelTransport.__init__

    def init(transport, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(200,
            json=double.answer(request.url.path, json.loads(request.content))))
        original(transport, *args, **kwargs)

    monkeypatch.setattr(ModelTransport, '__init__', init)

    async def execute():
        scanned = scan(env)
        images = k1_result(env)
        named = await name_folders(env.store, env.config, scanned['handoff']['k2'], images=images['images'],
            folder_images=images['folder_images'], knowledge_root=env.source)
        observed = await observe_images(env.store, env.config, named['rows'], images=images['images'],
            folder_images=images['folder_images'], image_embeddings=images['image_embeddings'],
            embedder_revision=images['embedder_revision'], dim=2, knowledge_root=env.source)
        cards = await build_cards(env.store, env.config, named['rows'], observations=observed['rows']['observations'],
            images=observed['rows']['images'], folder_images=images['folder_images'], snapshot_id=new_snapshot_id(),
            overrides=named['effective_overrides'])
        aliases = await build_aliases(env.store, env.config, named['rows'], cards=cards['rows']['cards'],
            overrides=named['effective_overrides'])
        for stage in (named, observed, cards, aliases):
            assert not stage['report']['failed']

    asyncio.run(execute())
    assert len(double.probe_requests) == 12  # 2 assessor + 6 observer + 2 cards + 2 aliases.


def test_probe_failure_evidence_is_checkpointed(production_env, monkeypatch):
    from k2_fixtures import folder, scan
    from prompt_enhancer.k2_naming import name_folders

    env = production_env
    folder(env, 'declared', 'Failure\nSynthetic description')
    original = ModelTransport.__init__

    def init(transport, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(200,
            json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"value":false}'}}]}))
        original(transport, *args, **kwargs)

    monkeypatch.setattr(ModelTransport, '__init__', init)

    async def execute():
        scanned = scan(env)
        result = await name_folders(env.store, env.config, scanned['handoff']['k2'],
            images=scanned['rows']['images'], folder_images=scanned['rows']['folder_images'], knowledge_root=env.source)
        assert set(result['report']['failed'].values()) == {'PROBE_MALFORMED'}

    asyncio.run(execute())
    with env.store.writer() as writer:
        rows = [row for row in writer._state_rows().values() if row['stage'] == 'model_probe_failure']
        assert len(rows) == 1
        document = json.loads(writer.checkpoints('model_probe_failure').load(rows[0]['input_key']))
        assert document['error'] == 'PROBE_MALFORMED'
        assert {item['capability'] for item in document['probes']} == {'text', 'json_schema'}
        assert all(item['status'] == 'negative' for item in document['probes'])


def cloud_profile(env, *, budget, policy):
    """Resolved test-only cloud profile; credentials are supplied by a labelled loader double."""
    config = deepcopy(env.config)
    config['vlm']['backends']['cloud_claude'] = synthetic_provider_backend(env, 'anthropic', budget=budget, policy=policy)
    for name in ('card_builder', 'observer'):
        role = config['vlm']['roles'][name]
        role['backend'] = 'cloud_claude'
        role['output'].update(format='none', schema_id='none', strict=False, validate_response=False)
    return config


class CloudProbeDouble:
    """Labelled OpenAI/Anthropic HTTP double; answers are derived from received data."""

    def __init__(self):
        self.local = ProductionServer()
        self.routes = []

    def answer(self, route, body):
        self.routes.append(route)
        converted = deepcopy(body)
        content = converted['messages'][-1]['content']
        if isinstance(content, list):
            for part in content:
                if part['type'] == 'image':
                    source = part.pop('source')
                    part.update(type='image_url', image_url={
                        'url': 'data:' + source['media_type'] + ';base64,' + source['data']})
        reply = self.local.answer(route, converted)
        if 'system' in body:
            return {'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': reply['choices'][0]['message']['content']}]}
        return reply


def test_production_cloud_probe_budget_falls_back_over_http(production_env, monkeypatch):
    """F1 regression: real HTTP and production probes, no injected client or proof."""
    env = production_env
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: {})  # Labelled credential loader double.
    double = CloudProbeDouble()
    with loopback_server(double) as endpoint:
        config = cloud_profile(env, budget=0, policy='fallback_to_default_backend')
        for name, route in (('main', 'local'), ('cloud_claude', 'cloud')):
            config['vlm']['backends'][name].update(base_urls=[endpoint], endpoints={'chat': route, 'health': 'health'})
        path = env.root / 'cloud-fallback.yaml'
        path.write_text(yaml.safe_dump(config), encoding='utf-8')
        loaded = load_config(path, 'kb status', project_root=env.root)

        async def execute():
            run = ModelClientRun(loaded, env.root)
            try:
                bound = await run.for_role('card_builder')
                value, record = await bound.call(b'system', '{"request":"generation"}')
                assert json.loads(value) == {'request': 'generation'}
                assert record['backend'] == 'main' and record['status'] == 'OK'
                assert ('BUDGET_EXHAUSTED', 'cloud_claude') in record['warnings']
                assert record['previous_attempts'][0]['status'] == 'BUDGET_EXHAUSTED'
                assert run.client._probe_allowances['cloud_claude'].sent == 0
                assert all(not attempts for key, attempts in run.client._production_attempted.items() if key[0] == 'cloud_claude')
                assert 'cloud_claude' not in run.probes
                assert double.routes == ['/local', '/local']
            finally:
                await run.close()

        asyncio.run(execute())


def bind_cloud_double(run, double, handler=None):
    """Replace HTTP only; production proof, budgets and provider mapping remain real."""
    def respond(request):
        return httpx.Response(200, json=double.answer(request.url.path, json.loads(request.content)))

    for name in ('main', 'cloud_claude'):
        run.transports[name] = ModelTransport(name, run.config['vlm']['backends'][name],
            run.concurrency, run.ledger, transport=httpx.MockTransport(handler if name == 'cloud_claude'
                                                                       and handler else respond))


@settings(max_examples=16, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(budget=st.integers(min_value=0, max_value=3), lazy=st.booleans(),
       policy=st.sampled_from(['stop', 'fallback_to_default_backend']))
def test_production_budget_policy_property(production_env, monkeypatch, budget, lazy, policy):
    """Grant exhaustion at binding, lazy extension or generation obeys the loaded policy."""
    env = production_env
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: {})
    config = cloud_profile(env, budget=budget, policy=policy)
    path = env.root / 'policy-property.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=env.root)

    async def execute():
        run = ModelClientRun(loaded, env.root)
        bind_cloud_double(run, CloudProbeDouble())
        try:
            try:
                bound = await run.for_role('card_builder')
                if lazy:
                    _, images = probes.load_probe_bundle(env.root)
                    value, record = await run.client.call('observer', b'system', '{"request":"generation"}',
                        images=[('knowledge_reference', images[0])])
                else:
                    value, record = await bound.call(b'system', '{"request":"generation"}')
            except ModelCallError as error:
                assert policy == 'stop' and budget < (4 if lazy else 2)
                assert error.code == 'BUDGET_EXHAUSTED'
                assert error.code != 'CAPABILITY_UNAVAILABLE'
            else:
                fallback = budget < (4 if lazy else 2)
                assert record['backend'] == ('main' if fallback else 'cloud_claude')
                assert (('BUDGET_EXHAUSTED', 'cloud_claude') in record['warnings']) == fallback
                assert record['status'] == 'OK' and value is not None
            assert all(event['status'] != 'negative' for event in run.client.probe_events)
            assert all(value is not False for proof in run.probes.values() for value in proof.capabilities.values())
            assert run.ledger._active == {}
            assert run.ledger._remaining['cloud_claude'] == max(0, budget - (4 if lazy else 2))
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize('key', ['call_budget', 'on_budget_exhausted'])
@pytest.mark.parametrize('invalid', ['missing', 'ask_user', 'design_required', True, -1, 'invalid'])
def test_production_budget_configuration_refusal(production_env, monkeypatch, key, invalid):
    env = production_env
    config = cloud_profile(env, budget=2, policy='stop')
    cloud = config['vlm']['backends']['cloud_claude']
    if invalid == 'missing':
        del cloud[key]
    else:
        cloud[key] = invalid
    path = env.root / 'budget-refusal.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: pytest.fail('credential load before refusal'))

    async def execute():
        loaded = load_config(path, 'kb status', project_root=env.root)
        run = ModelClientRun(loaded, env.root)
        try:
            await run.for_role('card_builder')
        finally:
            await run.close()

    with pytest.raises((ConfigurationError, ValueError), match=key if invalid != -1 else 'budget'):
        asyncio.run(execute())


@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(fault=st.sampled_from(['budget', 'allowance', 'transport', 'cancel', 'timeout']),
       policy=st.sampled_from(['stop', 'fallback_to_default_backend']))
def test_production_probe_exception_property(production_env, monkeypatch, fault, policy):
    env = production_env
    config = cloud_profile(env, budget=0 if fault == 'budget' else 5, policy=policy)
    cloud = config['vlm']['backends']['cloud_claude']
    # Only the timeout case races a deadline; other faults must reach their intended boundary.
    cloud['probe'].update(max_requests=1 if fault == 'allowance' else 8,
                          timeout_s=0.01 if fault == 'timeout' else 2)
    cloud['http'].update(max_retries=1 if fault == 'allowance' else 0, retry_statuses=[503])
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: {})
    active = [True]
    double = CloudProbeDouble()

    async def respond(request):
        if active[0]:
            if fault == 'allowance':
                return httpx.Response(503)
            if fault == 'transport':
                raise httpx.ReadError('LABELLED_TRANSPORT_FAILURE')
            if fault == 'cancel':
                raise asyncio.CancelledError()
            if fault == 'timeout':
                await asyncio.sleep(1)
        return httpx.Response(200, json=double.answer(request.url.path, json.loads(request.content)))

    async def execute():
        run = ModelClientRun(config, env.root)
        bind_cloud_double(run, double, respond)
        try:
            try:
                bound = await run.for_role('card_builder')
                _, record = await bound.call(b'system', '{"request":"generation"}')
            except asyncio.CancelledError:
                assert fault == 'cancel'
                assert all(not attempts for key, attempts in run.client._production_attempted.items() if key[0] == 'cloud_claude')
                active[0] = False
                # Cancellation spent its attempted send, but did not poison the next bind.
                await run.for_role('card_builder')
                assert run.probes['cloud_claude'].capabilities['text'] is True
            except ModelCallError as error:
                expected = {'budget': 'BUDGET_EXHAUSTED', 'allowance': 'PROBE_INCOMPLETE',
                            'transport': 'PROBE_FAILED', 'timeout': 'PROBE_FAILED'}
                assert error.code == expected[fault]
                if fault == 'budget':
                    assert policy == 'stop' and all(not attempts for key, attempts in run.client._production_attempted.items()
                                                  if key[0] == 'cloud_claude')
            else:
                assert fault == 'budget' and policy == 'fallback_to_default_backend'
                assert record['backend'] == 'main'
                assert ('BUDGET_EXHAUSTED', 'cloud_claude') in record['warnings']
            assert all(event['status'] != 'negative' for event in run.client.probe_events)
            assert all(value is not False for proof in run.probes.values() for value in proof.capabilities.values())
            assert not run.ledger._active
            assert run.ledger._remaining['cloud_claude'] == (0 if fault == 'budget' else 3 if fault == 'cancel' else 4)
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize('failure', [probes.ProbeLimitExhausted, asyncio.CancelledError, RuntimeError])
def test_unsent_probe_reservation_released_for_every_escape(production_env, failure):
    env = production_env
    config = cloud_profile(env, budget=5, policy='stop')

    class RefusedAllowance:
        def take(self):
            raise failure()

    async def execute():
        run = ModelClientRun(config, env.root)
        transport = ModelTransport('cloud_claude', config['vlm']['backends']['cloud_claude'],
            run.concurrency, run.ledger, transport=httpx.MockTransport(lambda request: pytest.fail('unsent request')))
        try:
            with pytest.raises(failure):
                await transport.post_probe('chat', {}, {}, RefusedAllowance())
            assert run.ledger._remaining['cloud_claude'] == 5
            assert not run.ledger._active
        finally:
            await transport.close()
            await run.close()

    asyncio.run(execute())


def test_cloud_headers_loaded_once_per_backend_per_run(production_env, monkeypatch):
    env = production_env
    config = cloud_profile(env, budget=10, policy='stop')
    loads = []

    def headers(provider, backend, root):
        loads.append(provider)  # Labelled loader double; no filesystem or credential logging.
        return {'Authorization': 'Bearer synthetic-test-only'}

    monkeypatch.setattr(model_client, 'cloud_headers', headers)

    async def execute():
        for index in range(2):
            run = ModelClientRun(config, env.root)
            bind_cloud_double(run, CloudProbeDouble())
            try:
                first = await run.for_role('card_builder')
                await run.for_role('card_builder')
                assert loads == ['anthropic'] * (index + 1)
                for _ in range(2):
                    _, record = await first.call(b'system', '{"request":"generation"}')
                    assert record['status'] == 'OK'
                    assert 'Authorization' not in json.dumps(record)
                observer = await run.for_role('observer')
                _, images = probes.load_probe_bundle(env.root)
                _, record = await observer.call(b'system', '{"request":"generation"}',
                    images=[('knowledge_reference', images[0])])
                assert record['status'] == 'OK'
                assert loads == ['anthropic'] * (index + 1)
                assert run.client._cloud_headers
            finally:
                await run.close()
            assert not run.client._cloud_headers

    asyncio.run(execute())


def test_cancelled_ordered_probe_does_not_poison_extension(production_env):
    env = production_env
    sends = []
    double = ProductionServer()

    def respond(request):
        sends.append(request)
        if len(sends) == 7:  # Six ordinary proofs precede the first ordered challenge.
            raise asyncio.CancelledError()
        return httpx.Response(200, json=double.answer(request.url.path, json.loads(request.content)))

    async def execute():
        run = ModelClientRun(env.config, env.root)
        bind_double(run, env, handler=respond)
        try:
            with pytest.raises(asyncio.CancelledError):
                await run.for_role('folder_namer', require=('multiple_images',))
            assert 'main' not in run.client._multiple_attempted
            assert run.client._probe_allowances['main'].sent == 7
            await run.for_role('folder_namer', require=('multiple_images',))
            assert len(sends) == 8  # Cancelled sends stay spent; no allowance reset.
            assert run.probes['main'].errors['multiple_images'] == 'PROBE_INCOMPLETE'
            assert run.probes['main'].capabilities.get('multiple_images') is None
        finally:
            await run.close()

    asyncio.run(execute())


# INT-F1 (decision 144): every probe image obeys the side generation would use. The oracle below restates the
# rule independently of `image_max_side`: the smaller configured side, `none` unlimited, never upscaled, aspect kept.
def expected_shape(source, *limits):
    limit = min((value for value in limits if value != 'none'), default=None)
    long = max(source)
    if limit is None or long <= limit:
        return source
    return (max(1, source[0] * limit // long), max(1, source[1] * limit // long))


def received_images(double):
    """(longest-first request order) every image a probe request really carried: lists of decoded sizes."""
    from base64 import b64decode
    from io import BytesIO
    from PIL import Image

    found = []
    for body in double.probe_requests:
        content = body['messages'][-1]['content']
        sizes = []
        for part in content if isinstance(content, list) else ():
            if part['type'] == 'image_url':
                with Image.open(BytesIO(b64decode(part['image_url']['url'].split(',', 1)[1]))) as image:
                    sizes.append(image.size)
        if sizes:
            found.append(sizes)
    return found


def check_probe_shapes(env, double, role_side, vision_side, backend_side):
    """Single, image+schema (bundle fixtures, in challenge order) and ordered-pair requests (96x64) are all sized."""
    from prompt_enhancer.model_probe import load_probe_bundle

    bundle, _ = load_probe_bundle(env.root)
    sources = [(item['width'], item['height']) for item in bundle['images']]
    found = received_images(double)
    assert [len(sizes) for sizes in found] == [1, 1, 1, 1, 2, 2]  # image x2, image+schema x2, ordered pair x2
    limits = (vision_side if role_side == 'from_vision' else role_side, backend_side)
    for index, sizes in enumerate(found):
        for source in ([sources[index % 2]] if len(sizes) == 1 else [(96, 64)] * 2):
            assert sizes.pop(0) == expected_shape(source, *limits), (index, role_side, vision_side, backend_side)


SIDES = st.one_of(st.just('none'), st.integers(min_value=1, max_value=48), st.integers(min_value=97, max_value=200))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(role_side=st.one_of(st.just('from_vision'), SIDES), vision_side=SIDES, backend_side=SIDES)
def test_probe_images_obey_the_generation_side_rule(production_env, role_side, vision_side, backend_side):
    """Property: the smaller of role (`from_vision` resolved) and backend side, `none` unlimited, for every probe."""
    env = production_env
    config = deepcopy(env.config)
    config['vlm']['roles']['folder_namer']['image']['max_side'] = role_side
    config['vision']['max_image_side'] = vision_side
    config['vlm']['backends']['main']['image']['max_side'] = backend_side
    assert config['vlm']['roles']['folder_namer']['output']['format'] == 'json_schema'

    async def execute():
        run = ModelClientRun(config, env.root)
        double = bind_double(run, env)
        try:
            await run.for_role('folder_namer', require=('multiple_images',))
            check_probe_shapes(env, double, role_side, vision_side, backend_side)
            assert model_client.image_max_side(config, config['vlm']['roles']['folder_namer']['image'],
                                               config['vlm']['backends']['main']) == (
                min((v for v in ((vision_side if role_side == 'from_vision' else role_side), backend_side)
                     if v != 'none'), default='none'))
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize(('role_side', 'vision_side', 'backend_side'), [
    ('from_vision', 'none', 32),   # backend cap alone
    ('from_vision', 'none', 48),
    (20, 'none', 32),              # role side below the backend side
    ('from_vision', 24, 32),       # inherited vision side below the backend side
    ('none', 'none', 'none'),      # no limit anywhere: the 96x64 fixtures go out unchanged
])
def test_loaded_profile_probe_images_over_loopback_obey_the_side_rule(production_env, role_side, vision_side,
                                                                      backend_side):
    """The reproducer of INT-F1 promoted: a loaded profile, real HTTP transport, images decoded as received."""
    env = production_env
    config = env.config
    config['vlm']['roles']['folder_namer']['image']['max_side'] = role_side
    config['vision']['max_image_side'] = vision_side
    config['vlm']['backends']['main']['image']['max_side'] = backend_side
    double = ProductionServer()
    with loopback_server(double) as url:
        config['vlm']['backends']['main']['base_urls'] = [url]
        profile = env.root / 'image-side.yaml'
        profile.write_text(yaml.safe_dump(config), encoding='utf-8')
        loaded = load_config(profile, 'kb sync', project_root=env.root)

        async def execute():
            run = ModelClientRun(loaded, env.root)
            try:
                await run.for_role('folder_namer', require=('multiple_images',))
            finally:
                await run.close()

        asyncio.run(execute())
    check_probe_shapes(env, double, role_side, vision_side, backend_side)


def test_invalid_backend_side_is_refused_without_a_probe_mark(production_env):
    """Generation refuses a non-limit side; a probe does too, before it marks the capability as attempted."""
    env = production_env
    config = deepcopy(env.config)
    config['vlm']['backends']['main']['image']['max_side'] = 0

    async def execute():
        run = ModelClientRun(config, env.root)
        double = bind_double(run, env)
        try:
            with pytest.raises(ConfigurationError, match='max_side'):
                await run.for_role('folder_namer')
            assert received_images(double) == []
            assert not run.client._production_attempted  # Invalid context is refused before any mark/send.
        finally:
            await run.close()

    asyncio.run(execute())


def test_fallback_backend_probe_and_generation_images_use_its_own_side(production_env, monkeypatch):
    """A cloud role that may not send images falls back to the local backend; that backend's side governs."""
    from base64 import b64decode
    from io import BytesIO
    from PIL import Image

    env = production_env
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: {})  # Labelled credential loader double.
    config = cloud_profile(env, budget=5, policy='stop')
    config['vlm']['backends']['cloud_claude']['allow_images'] = False
    config['vlm']['backends']['cloud_claude']['image']['max_side'] = 'none'  # The cloud side must not be used.
    config['vlm']['backends']['main']['image']['max_side'] = 32
    source = BytesIO()
    Image.new('RGB', (200, 120), 'green').save(source, format='PNG')

    async def execute():
        run = ModelClientRun(config, env.root)
        double = CloudProbeDouble()
        bind_cloud_double(run, double)
        try:
            bound = await run.for_role('observer')
            await bound.call(b'system', '{}', images=[('knowledge_reference', source.getvalue())])
            received = received_images(double.local)
            assert len(received) == 3  # image probe pair on the fallback, then the generation image
            probe_limit = config['vlm']['roles']['observer']['image']['max_side']
            for sizes in received[:2]:
                assert sizes == [expected_shape((96, 64), probe_limit if probe_limit != 'from_vision'
                                                else config['vision']['max_image_side'], 32)]
            assert received[2] == [expected_shape((200, 120), 32)]
            assert max(received[0][0]) <= 32
        finally:
            await run.close()

    asyncio.run(execute())


def test_ordered_pair_that_cannot_be_prepared_is_reported_not_faked(production_env):
    """A decode bound below the 96x64 pair is never raised: the pair is recorded failed, proof stays absent."""
    env = production_env

    async def execute():
        run = ModelClientRun(env.config, env.root)
        double = bind_double(run, env)
        try:
            await run.for_role('folder_namer')
            sent = len(double.probe_requests)
            run.config['vlm']['backends']['main']['image']['max_decode_pixels'] = 1000  # Below 96x64 = 6144.
            with pytest.raises(ModelCallError, match='CAPABILITY_UNAVAILABLE'):
                await run.for_role('folder_namer', require=('multiple_images',))
            assert len(double.probe_requests) == sent + 2  # New context proves text/format before image preparation refuses.
            assert run.probes['main'].capabilities.get('image') is None  # Never borrow the old decode policy's proof.
            event = run.client.probe_events[-1]
            assert (event['capability'], event['status'], event['reason']) == (
                'image', 'failed', 'INPUT_INVALID')
            assert all(e['capability'] != 'multiple_images' for e in run.client.probe_events)
        finally:
            await run.close()

    asyncio.run(execute())
