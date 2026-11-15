"""Loaded configuration exercised against a bounded loopback HTTP double."""

import asyncio
import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from threading import Event, Lock, Thread
import time

import pytest
import yaml
from PIL import Image

from prompt_enhancer.config import ConfigurationError, load_config
from prompt_enhancer.model_client import ModelCallError, compose_model_client
from prompt_enhancer.model_image import prepare_vision_image
from prompt_enhancer.model_resources import BudgetExhausted


ROOT = Path(__file__).resolve().parents[1]


def test_loaded_vision_quality_changes_shared_encoding(tmp_path, project_root):
    profile = yaml.safe_load((ROOT / 'configs' / 'enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    profile['vision'].update(max_image_side='none', format='jpeg', quality=10)
    path = tmp_path / 'profile.yaml'
    image = BytesIO()
    Image.effect_noise((32, 32), 80).convert('RGB').save(image, format='PNG')
    results = []
    for quality in (10, 90):
        profile['vision']['quality'] = quality
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        vision = load_config(path, 'kb status', project_root=project_root)['vision']
        results.append(prepare_vision_image(image.getvalue(), vision, 2048))
    assert results[0] != results[1]
    profile['vision']['quality'] = 101
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize('field', ['max_image_side', 'format', 'block'])
def test_composition_refuses_unresolved_inherited_vision(tmp_path, project_root, field):
    profile = yaml.safe_load((ROOT / 'configs' / 'enhancer.example.yaml').read_bytes())
    profile['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only
    role = profile['vlm']['roles']['manifest_drafter']
    role.update(max_tokens=100, image=dict(source='source_image', max_side='from_vision',
                                          format='from_vision', quality=80, transport='data_uri'),
                sampling=dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0, presence_penalty=0.0,
                              frequency_penalty=0.0, repetition_penalty=1.0, seed='none', stop='none'),
                reasoning=dict(mode='auto', effort='none', budget_tokens='none', template_kwargs_key='switch'),
                output=dict(format='none', schema_id='none', strict=False,
                            transport='response_format', validate_response=False))
    profile['vision']['format'] = 'png'
    if field == 'block':
        profile['vision'] = 'deploy_required'
    else:
        profile['vision'][field] = 'deploy_required'
    path = tmp_path / 'profile.yaml'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)

    async def run():
        with pytest.raises(ConfigurationError, match='vision' if field == 'block' else f'vision.{field}'):
            await compose_model_client(loaded, 'manifest_drafter', project_root=project_root,
                                       probe_cases={}, local_probe_plans={})

    asyncio.run(run())


@pytest.mark.parametrize(('key', 'value', 'observation'), [
    ('engine.name', 'vllm', 'engine_name'),
    ('engine.version', 'version-override', 'engine_version'),
    ('model.id', 'model-override', 'model'),
    ('model.revision', 'revision-override', 'revision'),
    ('endpoints.chat', 'alternate-route', 'route'),
    ('base_urls', 'prefix', 'base_url'),
    ('http.connect_timeout_s', 4, 'connect_timeout'),
    ('http.read_timeout_s', 3, 'timeout'),
    ('http.write_timeout_s', 5, 'write_timeout'),
    ('http.pool_timeout_s', 6, 'pool_timeout'),
    ('http.max_retries', 2, 'retry_count'),
    ('http.retry_statuses', [503], 'retry_statuses'),
    ('http.backoff_initial_s', 0.4, 'backoff_initial'),
    ('http.backoff_max_s', 0.5, 'backoff_max'),
    ('probe.max_requests', 3, 'probe_count'),
    ('probe.timeout_s', 0.05, 'probe_timeout'),
    ('max_concurrency', 3, 'concurrency'),
    ('image.max_decode_pixels', 1500, 'backstop'),
    ('extra_body', {'marker': 'extra-override'}, 'extra'),
    ('role.max_tokens', 150, 'max_tokens'),
    ('role.prompt_id', 'prompt-override', 'prompt_id'),
    ('role.sampling.temperature', 0.7, 'temperature'),
    ('role.sampling.top_p', 0.6, 'top_p'),
    ('role.sampling.top_k', 12, 'top_k'),
    ('role.sampling.min_p', 0.2, 'min_p'),
    ('role.sampling.presence_penalty', 0.3, 'presence_penalty'),
    ('role.sampling.frequency_penalty', 0.4, 'frequency_penalty'),
    ('role.sampling.seed', 9, 'seed'),
    ('role.sampling.stop', ['END'], 'stop'),
    ('role.sampling.repetition_penalty', 1.2, 'repetition_warning'),
    ('role.reasoning.mode', 'on', 'reasoning_mode'),
    ('role.reasoning.effort', 'high', 'reasoning_effort'),
    ('role.reasoning.budget_tokens', 128, 'budget_warning'),
    ('role.reasoning.template_kwargs_key', 'custom_switch', 'reasoning_key'),
    ('role.output.validate_response', False, 'validation_false'),
    ('role.output.validate_response', True, 'validation_true'),
    ('role.output.format', 'json_object', 'output_format'),
    ('role.output.schema_id', 'verifier_result.v0', 'output_schema'),
    ('role.output.strict', True, 'output_strict'),
    ('role.output.schema_id', 'from_profile_manifest', 'writer_manifest'),
    ('role.image.max_side', 4, 'image_max_side'),
    ('vision.max_image_side', 4, 'vision_max_side'),
    ('role.image.format', 'jpeg', 'image_format'),
    ('vision.format', 'jpeg', 'vision_format'),
    ('role.image.quality', 20, 'image_quality'),
    ('image.max_decode_pixels', 63, 'decode_refusal'),
])
def test_loaded_profile_composes_role_client(tmp_path, project_root, monkeypatch, key, value, observation):
    seen = []
    delays = []
    activity = {'active': 0, 'peak': 0}
    activity_lock = Lock()
    three_entered = Event()
    retry_cases = {'retry_count', 'retry_statuses', 'backoff_initial', 'backoff_max'}
    if observation == 'backstop':
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, body))
            if observation == 'probe_timeout' and body.get('probe'):
                time.sleep(0.15)
            concurrent_call = observation == 'concurrency' and not body.get('probe')
            if concurrent_call:
                with activity_lock:
                    activity['active'] += 1
                    activity['peak'] = max(activity['peak'], activity['active'])
                    if activity['active'] == 3:
                        three_entered.set()
                three_entered.wait(timeout=2)
                time.sleep(0.02)
            failures = sum(not item[1].get('probe') for item in seen)
            failure_limit = 2 if observation in ('retry_count', 'backoff_max') else 1
            if observation in retry_cases and not body.get('probe') and failures <= failure_limit:
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            structured = observation in ('output_format', 'output_schema', 'output_strict', 'writer_manifest') and not body.get('probe')
            response = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': '{"results": []}' if structured else 'ok'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if concurrent_call:
                    with activity_lock:
                        activity['active'] -= 1

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = yaml.safe_load((ROOT / 'configs' / 'enhancer.example.yaml').read_bytes())
        backend = profile['vlm']['backends']['main']
        backend['engine'] = {'name': 'llama_cpp', 'version': 'test-version'}
        backend.update(base_urls=[f'http://127.0.0.1:{server.server_port}'],
                       endpoints={'chat': 'chat', 'health': 'health'}, max_concurrency=2,
                       http=dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                                 max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[]),
                       probe=dict(max_requests=20, timeout_s=1), image=dict(max_decode_pixels=1000), extra_body={})
        backend['model'].update(id='model-base', revision='revision-base')
        if observation in retry_cases:
            backend['http'].update(max_retries=1, retry_statuses=[503], backoff_initial_s=0.2, backoff_max_s=2)
            if observation == 'retry_statuses':
                backend['http']['retry_statuses'] = []
            if observation == 'backoff_max':
                backend['http'].update(max_retries=2, backoff_initial_s=0.4)

            async def capture_delay(delay):
                delays.append(delay)

            monkeypatch.setattr('prompt_enhancer.model_transport.asyncio.sleep', capture_delay)
        role = profile['vlm']['roles']['manifest_drafter']
        role.update(max_tokens=100, image='none',
                    sampling=dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0, presence_penalty=0.0,
                                  frequency_penalty=0.0, repetition_penalty=1.0, seed='none', stop='none'),
                    reasoning=dict(mode='auto', effort='none', budget_tokens='none', template_kwargs_key='switch'),
                    output=dict(format='none', schema_id='none', strict=False,
                                transport='response_format', validate_response=False))
        if observation == 'reasoning_key':
            role['reasoning']['mode'] = 'on'
        if observation.startswith('validation_'):
            role['output']['schema_id'] = 'intent_ir.v0'
        if observation in ('output_format', 'output_schema', 'output_strict'):
            role['output']['validate_response'] = True
        if observation in ('output_schema', 'output_strict'):
            role['output'].update(format='json_schema', schema_id=('intent_ir.v0' if observation == 'output_schema'
                                                                    else 'verifier_result.v0'))
        if observation == 'writer_manifest':
            role['output'].update(format='from_profile_manifest', validate_response=True)
        image_observations = {'image_max_side', 'vision_max_side', 'image_format', 'vision_format', 'image_quality', 'decode_refusal'}
        source = BytesIO()
        if observation in image_observations:
            Image.effect_noise((8, 8), 40).convert('RGB').save(source, format='PNG')
            role['image'] = dict(source='source_image', max_side='from_vision', format='from_vision',
                                 quality=70, transport='data_uri')
            profile['vision'].update(max_image_side='none', format='png', quality=75)
            if observation == 'image_quality':
                role['image']['format'] = 'jpeg'
        parts = key.split('.')
        node = role if parts[0] == 'role' else profile['vision'] if parts[0] == 'vision' else backend
        if parts[0] in ('role', 'vision'):
            parts = parts[1:]
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = ([f'http://127.0.0.1:{server.server_port}/{value}']
                           if observation == 'base_url' else deepcopy(value))
        role_name = 'writer' if observation == 'writer_manifest' else 'manifest_drafter'
        if role_name == 'writer':
            profile['vlm']['roles']['writer'] = deepcopy(role)
        path = tmp_path / 'profile.yaml'
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        loaded = load_config(path, 'profile register', project_root=project_root)

        async def run():
            verify = lambda response: response.json()['choices'][0]['message']['content'] == 'ok'
            capabilities = ['text', 'reasoning.mode', 'reasoning.effort', 'json_object', 'json_schema', 'image']
            capabilities += [f'sampling.{name}' for name in ('temperature', 'top_p', 'top_k', 'min_p',
                                                               'presence_penalty', 'frequency_penalty', 'seed', 'stop')]
            cases = {'main': {name: ({'probe': True}, verify) for name in capabilities}}
            client = await compose_model_client(loaded, role_name, project_root=project_root,
                                                probe_cases=cases, local_probe_plans={})
            try:
                images = [('source_image', source.getvalue())] if observation in image_observations else ()
                if observation == 'decode_refusal':
                    with pytest.raises(ModelCallError, match='IMAGE_INVALID'):
                        await client.call(b'system', 'data', images=images)
                    return
                manifest = {'output_schema_id': 'verifier_result.v0'} if observation == 'writer_manifest' else None
                if observation == 'concurrency':
                    replies = await asyncio.gather(*(client.call(b'system', 'data') for _ in range(4)))
                    result, record = replies[-1]
                else:
                    result, record = await client.call(b'system', 'data', images=images, manifest=manifest)
                if observation == 'validation_true':
                    assert result is None and record['status'] == 'SCHEMA_INVALID'
                elif observation in ('output_format', 'output_schema', 'output_strict', 'writer_manifest'):
                    assert result == {'results': []} and record['status'] == 'OK'
                else:
                    assert result == 'ok' and record['status'] == 'OK'
                body = seen[-1][1]
                actual = {'model': body['model'], 'revision': record['model_revision'], 'route': seen[-1][0],
                          'engine_name': record['engine']['name'], 'engine_version': record['engine']['version'],
                          'timeout': client.run.transports['main'].client.timeout.read,
                          'connect_timeout': client.run.transports['main'].client.timeout.connect,
                          'write_timeout': client.run.transports['main'].client.timeout.write,
                          'pool_timeout': client.run.transports['main'].client.timeout.pool,
                          'concurrency': activity['peak'],
                          'backstop': Image.MAX_IMAGE_PIXELS,
                          'prompt_id': record['prompt_id'], 'max_tokens': body['max_tokens'],
                          'temperature': body.get('temperature'),
                          **{name: body.get(name) for name in ('top_p', 'top_k', 'min_p', 'presence_penalty',
                                                               'frequency_penalty', 'seed', 'stop')},
                          'reasoning_mode': 'on' if body.get('chat_template_kwargs', {}).get('switch') else 'off',
                          'reasoning_effort': body.get('reasoning_effort'),
                          'reasoning_key': next(iter(body.get('chat_template_kwargs', {})), None),
                          'repetition_warning': ('PARAM_UNSUPPORTED', 'sampling.repetition_penalty') in record['warnings'],
                          'budget_warning': ('PARAM_UNSUPPORTED', 'reasoning.budget_tokens') in record['warnings'],
                          'validation_false': record['status'] == 'SCHEMA_INVALID',
                          'validation_true': record['status'] == 'SCHEMA_INVALID',
                          'output_format': body.get('response_format', {}).get('type'),
                          'output_schema': body.get('response_format', {}).get('json_schema', {}).get('name'),
                          'output_strict': body.get('response_format', {}).get('json_schema', {}).get('strict'),
                          'writer_manifest': body.get('response_format', {}).get('json_schema', {}).get('name'),
                          'base_url': seen[-1][0],
                          'retry_count': sum(not entry[1].get('probe') for entry in seen),
                          'retry_statuses': sum(not entry[1].get('probe') for entry in seen),
                          'backoff_initial': delays[0] if delays else None,
                          'backoff_max': delays[-1] if delays else None,
                          'probe_count': sum(bool(entry[1].get('probe')) for entry in seen),
                          'probe_timeout': client.run.probes['main'].errors.get('text'),
                          'extra': body.get('marker')}
                if images:
                    encoded = body['messages'][1]['content'][1]['image_url']['url'].split(',', 1)[1]
                    with Image.open(BytesIO(base64.b64decode(encoded))) as prepared:
                        actual.update(image_max_side=max(prepared.size), vision_max_side=max(prepared.size),
                                      image_format=prepared.format.lower(), vision_format=prepared.format.lower())
                        if observation == 'image_quality':
                            reference = BytesIO()
                            with Image.open(BytesIO(source.getvalue())) as original:
                                original.save(reference, format='JPEG', quality=value)
                            with Image.open(BytesIO(reference.getvalue())) as expected_image:
                                actual['image_quality'] = prepared.quantization == expected_image.quantization
                expected = '/' + value if observation == 'route' else value['marker'] if observation == 'extra' else value
                if observation == 'base_url':
                    expected = f'/{value}/chat'
                elif observation == 'retry_count':
                    expected = value + 1
                elif observation == 'retry_statuses':
                    expected = 2
                elif observation == 'probe_timeout':
                    expected = 'TimeoutError'
                elif observation == 'writer_manifest':
                    expected = 'verifier_result.v0'
                if observation in ('repetition_warning', 'budget_warning'):
                    expected = True
                if observation == 'image_quality':
                    expected = True
                assert actual[observation] == expected
            finally:
                await client.close()

        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('policy', ['stop', 'fallback_to_default_backend', 'image_local', 'image_cloud'])
def test_cloud_budget_is_shared_by_roles_from_same_run(tmp_path, project_root, policy):
    seen = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.headers.get('Authorization'), self.headers.get('anthropic-version')))
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            payload = ({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}]}
                       if self.path == '/cloud' else
                       {'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]})
            response = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = yaml.safe_load((ROOT / 'configs' / 'enhancer.example.yaml').read_bytes())
        local = profile['vlm']['backends']['main']
        local['engine'] = {'name': 'llama_cpp', 'version': 'test-version'}
        local.update(base_urls=[f'http://127.0.0.1:{server.server_port}'],
                     endpoints={'chat': 'chat', 'health': 'health'}, max_concurrency=1,
                     http=dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                               max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[]),
                     probe=dict(max_requests=1, timeout_s=1), image=dict(max_decode_pixels=1000))
        local['model'].update(id='local', revision='revision')
        cloud = deepcopy(local)
        cloud.update(provider='anthropic', engine={'name': 'anthropic'}, api_key_file='.secrets/synthetic.key', anthropic_version='2025-01-01', anthropic_workspace_id='none',
                     endpoints={'chat': 'cloud', 'health': 'health'},
                     call_budget=3, on_budget_exhausted=('stop' if policy.startswith('image_') else policy),
                     allow_images=policy == 'image_cloud')
        profile['vlm']['backends']['cloud_claude'] = cloud
        role = profile['vlm']['roles']['manifest_drafter']
        role.update(backend='cloud_claude', max_tokens=100, image='none',
                    sampling=dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0, presence_penalty=0.0,
                                  frequency_penalty=0.0, repetition_penalty=1.0, seed='none', stop='none'),
                    reasoning=dict(mode='auto', effort='none', budget_tokens='none', template_kwargs_key='switch'),
                    output=dict(format='none', schema_id='none', strict=False,
                                transport='response_format', validate_response=False))
        source = BytesIO()
        if policy.startswith('image_'):
            role['image'] = dict(source='source_image', max_side='none', format='png', quality=80, transport='data_uri')
            profile['vision'].update(max_image_side='none', format='png', quality=80)
            Image.new('RGB', (8, 8)).save(source, format='PNG')
        directory = project_root / '.secrets'
        directory.mkdir(exist_ok=True)
        (directory / 'synthetic.key').write_text('synthetic', encoding='utf-8')
        path = tmp_path / 'profile.yaml'
        path.write_text(yaml.safe_dump(profile), encoding='utf-8')
        loaded = load_config(path, 'profile register', project_root=project_root)

        async def run():
            cloud_capability = 'image' if policy == 'image_cloud' else 'text'
            cases = {'cloud_claude': {cloud_capability: ({'probe': True}, lambda response: response.json()['stop_reason'] == 'end_turn')},
                     'main': {'image': ({'probe': True}, lambda response: response.json()['choices'][0]['finish_reason'] == 'stop')}}
            first = await compose_model_client(loaded, 'manifest_drafter', project_root=project_root,
                                               probe_cases=cases, local_probe_plans={})
            try:
                second = await first.for_role('manifest_drafter')
                assert first.run is second.run
                if policy.startswith('image_'):
                    result, record = await second.call(b'system', 'data', images=[('source_image', source.getvalue())])
                    assert result == 'ok'
                    if policy == 'image_local':
                        assert record['backend'] == 'main'
                        assert seen == [('Bearer synthetic', '2025-01-01'), (None, None), (None, None)]
                    else:
                        assert record['backend'] == 'cloud_claude'
                        assert requests[-1]['messages'][0]['content'][1]['type'] == 'image'
                        assert seen == [('Bearer synthetic', '2025-01-01')] * 2
                    return
                assert (await first.call(b'system', 'one'))[0] == 'ok'
                assert (await second.call(b'system', 'two'))[0] == 'ok'
                if policy == 'stop':
                    with pytest.raises(ModelCallError, match='BUDGET_EXHAUSTED'):
                        await second.call(b'system', 'three')
                    assert seen == [('Bearer synthetic', '2025-01-01')] * 3
                else:
                    result, record = await second.call(b'system', 'three')
                    assert result == 'ok' and record['backend'] == 'main'
                    assert ('BUDGET_EXHAUSTED', 'cloud_claude') in record['warnings']
                    assert seen == [('Bearer synthetic', '2025-01-01')] * 3 + [(None, None), (None, None)]
            finally:
                await first.close()

        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
