"""Role client exercises a labelled in-memory HTTP double."""

import asyncio
import base64
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
import pytest

from prompt_enhancer.model_client import ModelCallError, ProbeCasesMissing, RoleClient
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.model_resources import BudgetExhausted
from prompt_enhancer.model_schemas import SchemaRegistry


class DoubleTransport:
    def __init__(self, content):
        self.content = content
        self.body = None
        self.finish_reason = 'stop'
        self.refusal = None

    async def post(self, route, body, headers):
        self.body = body
        return httpx.Response(200, json={'choices': [{'finish_reason': self.finish_reason,
                                                       'message': {'content': self.content, 'refusal': self.refusal}}], 'usage': {}},
                              request=httpx.Request('POST', 'http://127.0.0.1/chat'))


def test_role_client_records_effective_body_and_validates_json():
    async def run():
        role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'image': 'none',
                'sampling': {'temperature': 0.2, 'top_k': 5},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'json_object', 'schema_id': 'none', 'strict': False, 'validate_response': True}}
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'}, 'model': {'id': 'local', 'revision': 'r1'},
                   'endpoints': {'chat': 'chat'}, 'extra_body': {'temperature': 0.8}}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
        double = DoubleTransport('{"ok": true}')
        schema = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
        client = RoleClient(config, {'main': double}, {'main': ProbeResult('complete', {'json_object': True, 'sampling.temperature': True}, {})}, schema)
        result, record = await client.call('planner', b'unchanged', 'data')
        assert result == {'ok': True}
        assert double.body['messages'][0]['content'] == 'unchanged'
        assert double.body['temperature'] == 0.8
        assert len(record['effective_body_hash']) == 64
        assert 'unchanged' not in str(record)
        assert ('PARAM_UNSUPPORTED', 'sampling.top_k') in record['warnings']
        double.content = 'not json'
        result, record = await client.call('planner', b'unchanged', 'data')
        assert result is None and record['status'] == 'SCHEMA_INVALID'
        double.finish_reason = 'length'
        result, record = await client.call('planner', b'unchanged', 'data')
        assert result is None and record['status'] == 'INCOMPLETE_OR_REFUSED' and record['finish_reason'] == 'length'
        double.finish_reason = 'stop'
        double.refusal = 'private refusal text'
        result, record = await client.call('planner', b'unchanged', 'data')
        assert result is None and record['status'] == 'INCOMPLETE_OR_REFUSED'
        assert 'private refusal text' not in str(record)

    asyncio.run(run())


def test_request_hash_includes_dropped_settings_and_engine():
    async def run():
        role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'image': 'none',
                'sampling': {'top_k': 5},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'v1'},
                   'model': {'id': 'local', 'revision': 'r1'}, 'endpoints': {'chat': 'chat'}}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
        client = RoleClient(config, {'main': DoubleTransport('ok')}, {'main': ProbeResult('complete', {}, {})}, None)
        _, first = await client.call('planner', b'system', 'data')
        assert first['engine'] == {'name': 'llama_cpp', 'version': 'v1'}
        role['sampling']['top_k'] = 6
        _, dropped = await client.call('planner', b'system', 'data')
        assert dropped['effective_body_hash'] == first['effective_body_hash']
        assert dropped['request_hash'] != first['request_hash']
        backend['engine']['name'] = 'vllm'
        _, changed_name = await client.call('planner', b'system', 'data')
        assert changed_name['request_hash'] != dropped['request_hash']
        backend['engine']['version'] = 'v2'
        _, changed_version = await client.call('planner', b'system', 'data')
        assert changed_version['request_hash'] != changed_name['request_hash']
        backend['model']['revision'] = 'r2'
        _, changed_revision = await client.call('planner', b'system', 'data')
        assert changed_revision['request_hash'] != changed_version['request_hash']
        role['output']['strict'] = True
        _, changed_output = await client.call('planner', b'system', 'data')
        assert changed_output['effective_body_hash'] == changed_revision['effective_body_hash']
        assert changed_output['request_hash'] != changed_revision['request_hash']

    asyncio.run(run())


def test_structured_output_without_validation_is_refused():
    async def run():
        role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'sampling': {}, 'image': 'none',
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'json_object', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'}, 'model': {'id': 'local', 'revision': 'r'},
                   'endpoints': {'chat': 'chat'}}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
        client = RoleClient(config, {'main': DoubleTransport('{}')},
                            {'main': ProbeResult('complete', {'json_object': True}, {})}, None)
        with pytest.raises(ModelCallError, match='structured output requires response validation'):
            await client.call('planner', b'system', 'data')

    asyncio.run(run())


@pytest.mark.parametrize('kind', ['empty', 'non_image', 'corrupt', 'truncated', 'RGBA', 'LA', 'PA', 'palette',
                                  'P', 'I', 'I;16', 'I;16B', 'I;16L', 'F', 'LAB',
                                  'CMYK_png', 'I;16L_png', 'F_png', 'LAB_png', 'YCbCr_png',
                                  'RGBA_png', 'LA_png', 'PA_png', 'palette_png',
                                  'RGBA_webp', 'LA_webp', 'PA_webp', 'palette_webp'])
def test_image_failure_is_coded_with_original_cause(kind):
    async def run():
        source_kind = kind.removesuffix('_png').removesuffix('_webp')
        target_format = 'png' if kind.endswith('_png') else 'webp' if kind.endswith('_webp') else 'jpeg'
        role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30, 'sampling': {},
                'image': {'source': 'source_image', 'max_side': 'none', 'format': target_format, 'quality': 90},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'v'},
                   'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'},
                   'image': {'max_decode_pixels': 1000, 'max_side': 'none'}}
        config = {'vision': {'max_image_side': 'none', 'format': 'jpeg', 'alpha_policy': {'mode': 'refuse'}},
                  'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
        client = RoleClient(config, {'main': DoubleTransport('ok')},
                            {'main': ProbeResult('complete', {'image': True}, {})}, None)
        source = BytesIO()
        if source_kind in ('RGBA', 'LA', 'PA', 'palette', 'P', 'I', 'I;16', 'I;16B', 'I;16L', 'F', 'LAB', 'CMYK', 'YCbCr'):
            image = Image.new('P' if source_kind == 'palette' else source_kind, (8, 8))
            if source_kind == 'palette':
                image.info['transparency'] = 0
            image.save(source, 'IM' if source_kind in ('I;16L', 'YCbCr') else 'TIFF' if source_kind in ('PA', 'I', 'I;16B', 'F', 'LAB', 'CMYK') else 'PNG')
            data = source.getvalue()
        elif kind == 'truncated':
            Image.new('RGB', (8, 8)).save(source, 'PNG')
            data = source.getvalue()[:40]
        else:
            data = {'empty': b'', 'non_image': b'not an image', 'corrupt': b'\x89PNG\r\n\x1a\ncorrupt'}[kind]
        with pytest.raises(ModelCallError, match='^IMAGE_INVALID:') as caught:
            await client.call('planner', b'system', 'data', images=[('source_image', data)])
        assert isinstance(caught.value.__cause__, (ValueError, OSError))

    asyncio.run(run())


@pytest.mark.parametrize(('error', 'code'), [
    (BudgetExhausted('synthetic'), 'BUDGET_EXHAUSTED'),
    (ConfigurationError('synthetic'), 'CONFIGURATION_INVALID'),
    (ValueError('synthetic'), 'INPUT_INVALID'),
    (KeyError('synthetic'), 'CLIENT_STATE_INVALID'),
    (ModelCallError('synthetic'), 'MODEL_CALL_INVALID'),
])
def test_call_wraps_escaping_exception_with_cause(monkeypatch, error, code):
    client = RoleClient({}, {}, {}, None)

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(client, '_call', fail)
    with pytest.raises(ModelCallError, match='^' + code + ':') as caught:
        asyncio.run(client.call('role', b'system', 'text'))
    assert caught.value.__cause__ is error


def test_call_cancellation_propagates(monkeypatch):
    client = RoleClient({}, {}, {}, None)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(client, '_call', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.call('role', b'system', 'text'))


@pytest.mark.parametrize('inherit', [False, True])
def test_image_requires_verified_capability_and_is_resized(inherit):
    async def run():
        role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30,
                'sampling': {'temperature': 0.2}, 'image': {'source': 'source_image', 'max_side': 4, 'format': 'png', 'quality': 90},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'}, 'model': {'id': 'local', 'revision': 'r'},
                   'endpoints': {'chat': 'chat'}, 'image': {'max_decode_pixels': 1000, 'max_side': 'none'}}
        if inherit:
            del role['image']['max_side']
        config = {'vision': {'max_image_side': 4 if inherit else 'none', 'format': 'png', 'quality': 90, 'alpha_policy': {'mode': 'refuse'}},
                  'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
        double = DoubleTransport('done')
        schema = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
        client = RoleClient(config, {'main': double}, {'main': ProbeResult('complete', {'image': True, 'sampling.temperature': True}, {})}, schema)
        source = BytesIO()
        Image.new('RGB', (8, 4)).save(source, format='PNG')
        result, record = await client.call('planner', b'system', 'data', images=[('source_image', source.getvalue())])
        assert result == 'done' and record['status'] == 'OK'
        assert record['image_roles'] == ['source_image']
        assert 'base64' not in str(record)
        url = double.body['messages'][1]['content'][1]['image_url']['url']
        assert url.startswith('data:image/png;base64,')
        with Image.open(BytesIO(base64.b64decode(url.split(',', 1)[1]))) as prepared:
            assert prepared.size == (4, 2)

    asyncio.run(run())


# T-008 fix round 5 (R4F1): the oracle is Pillow's initialized registry, never a list of format or media type words.
Image.init()
SENT = [name for name in sorted(Image.SAVE) if str(Image.MIME.get(name, '')).startswith('image/')]
NOT_SENT = [name for name in sorted(Image.SAVE) if name not in SENT]


def image_call(fmt, data):
    """One local image call with role image format `fmt`; the body the labelled transport double received."""
    async def run():
        role = {'backend': 'main', 'prompt_id': 'test', 'max_tokens': 30,
                'sampling': {'temperature': 0.2}, 'image': {'source': 'source_image', 'max_side': 'none', 'format': fmt, 'quality': 90},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'},
                   'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'},
                   'image': {'max_decode_pixels': 1000, 'max_side': 'none'}}
        config = {'vision': {'max_image_side': 'none', 'format': 'png', 'quality': 90,
                             'alpha_policy': {'mode': 'refuse'}},
                  'vlm': {'roles': {'planner': role}, 'backends': {'main': backend}, 'default_backend': 'main'}}
        double = DoubleTransport('done')
        schema = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
        probe = ProbeResult('complete', {'image': True, 'sampling.temperature': True}, {})
        client = RoleClient(config, {'main': double}, {'main': probe}, schema)
        await client.call('planner', b'system', 'data', images=[('source_image', data)])
        return double.body

    return asyncio.run(run())


def png_bytes():
    source = BytesIO()
    Image.new('RGB', (8, 4)).save(source, format='PNG')
    return source.getvalue()


def test_jpeg2000_is_sent_with_its_registered_media_type():
    """Retest 4 (R4F1) on the T-009 builder: the header named the encoder, image/jpeg2000; Pillow registers
    image/jp2 (PIL/Jpeg2KImagePlugin.py:442)."""
    url = image_call('jpeg2000', png_bytes())['messages'][1]['content'][1]['image_url']['url']
    with Image.open(BytesIO(base64.b64decode(url.split(',', 1)[1]))) as prepared:
        assert prepared.format == 'JPEG2000'
        assert url.startswith(f'data:{Image.MIME[prepared.format]};base64,') and url.startswith('data:image/jp2;')


@pytest.mark.parametrize('encoder', SENT)
def test_every_installed_encoder_sends_its_registered_media_type(monkeypatch, encoder):
    # Labelled double of the image preparation: the header, not the encoding, is under test for every encoder.
    monkeypatch.setattr('prompt_enhancer.model_client.prepare_image', lambda *args, **kwargs: b'labelled bytes')
    url = image_call(encoder.lower(), png_bytes())['messages'][1]['content'][1]['image_url']['url']
    assert url.split(',', 1)[0] == f'data:{Image.MIME[encoder]};base64'


@pytest.mark.parametrize('encoder', NOT_SENT)
def test_an_encoder_without_a_registered_image_media_type_is_refused_before_the_request(encoder):
    with pytest.raises(ModelCallError, match='^CONFIGURATION_INVALID:') as caught:
        image_call(encoder.lower(), png_bytes())
    assert 'registers no image/ media type' in str(caught.value.__cause__)


def test_client_runs_configured_probe_through_transport():
    async def run():
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'}, 'endpoints': {'chat': 'chat'},
                   'probe': {'max_requests': 1, 'timeout_s': 1, 'image_attempts': 1}}
        config = {'vlm': {'backends': {'main': backend}}}
        double = DoubleTransport('evidence')
        schema = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
        cases = {'main': {'text': ({'test': True},
                                   lambda response: response.json()['choices'][0]['message']['content'] == 'evidence')}}
        client = RoleClient(config, {'main': double}, {}, schema, probe_cases=cases)
        result = await client.start_backend('main')
        assert result.status == 'complete' and result.capabilities['text']
        assert double.body == {'test': True}

    asyncio.run(run())


def test_backend_without_probe_cases_raises_its_own_type_and_sends_nothing():
    """The KBS-F1 production gap is named by ProbeCasesMissing, a ModelCallError; no request is made."""
    async def run():
        backend = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'},
                   'endpoints': {'chat': 'chat'}, 'probe': {'max_requests': 1, 'timeout_s': 1, 'image_attempts': 1}}
        double = DoubleTransport('evidence')
        schema = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
        client = RoleClient({'vlm': {'backends': {'main': backend}}}, {'main': double}, {}, schema, probe_cases={})
        with pytest.raises(ProbeCasesMissing) as caught:
            await client.start_backend('main')
        assert isinstance(caught.value, ModelCallError) and double.body is None

    asyncio.run(run())


def test_local_probe_plan_runs_both_challenge_pairs():
    async def run():
        class ProbeDouble:
            async def post(self, endpoint, body, headers):
                if 'chat_template_kwargs' in body:
                    return httpx.Response(200, json={'thinking': body['chat_template_kwargs']['switch']})
                url = body['messages'][0]['content'][0]['image_url']['url']
                value = 1 if url == 'first' else 2
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                               'message': {'content': '{"value": %d}' % value}}]})

        def image_body(url):
            return {'messages': [{'content': [{'type': 'image_url', 'image_url': {'url': url}}]}]}

        enabled = {'messages': [], 'chat_template_kwargs': {'switch': True}}
        disabled = {'messages': [], 'chat_template_kwargs': {'switch': False}}
        plan = {'image': ((image_body('first'), 1), (image_body('second'), 2)),
                'thinking': (enabled, disabled, 'switch', lambda response: response.json()['thinking'])}
        config = {'vlm': {'backends': {'main': {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'},
                  'endpoints': {'chat': 'chat'}, 'probe': {'max_requests': 4, 'timeout_s': 1, 'image_attempts': 1}}}}}
        client = RoleClient(config, {'main': ProbeDouble()}, {}, None, local_probe_plans={'main': plan})
        result = await client.start_backend('main')
        assert result.status == 'complete' and result.capabilities == {'image': True, 'reasoning.mode': True}

    asyncio.run(run())


def test_cloud_image_forbidden_falls_back_and_reprobes_local():
    async def run():
        role = {'backend': 'cloud', 'prompt_id': 'test', 'max_tokens': 30, 'sampling': {},
                'image': {'source': 'source_image', 'max_side': 4, 'format': 'png', 'quality': 90},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        local ={'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'}, 'model': {'id': 'local', 'revision': 'r'},
                 'endpoints': {'chat': 'chat'}, 'probe': {'max_requests': 1, 'timeout_s': 1, 'image_attempts': 1},
                 'image': {'max_decode_pixels': 1000, 'max_side': 'none'}}
        config = {'vision': {'max_image_side': 'none', 'format': 'png', 'quality': 90, 'alpha_policy': {'mode': 'refuse'}},
                  'vlm': {'roles': {'planner': role}, 'backends': {'main': local,
                          'cloud': {'provider': 'anthropic', 'allow_images': False}}, 'default_backend': 'main'}}
        double = DoubleTransport('verified')
        schema = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
        cases = {'main': {'image': ({'probe': True},
                                   lambda response: response.json()['choices'][0]['message']['content'] == 'verified')}}
        client = RoleClient(config, {'main': double}, {'main': ProbeResult('failed', {}, {})}, schema, probe_cases=cases)
        source = BytesIO()
        Image.new('RGB', (8, 4)).save(source, format='PNG')
        result, record = await client.call('planner', b'system', 'data', images=[('source_image', source.getvalue())])
        assert result == 'verified' and record['backend'] == 'main'
        assert client.probes['main'].capabilities['image'] is True

    asyncio.run(run())


def test_cloud_requires_ledger_and_uses_synthetic_key(tmp_path):
    async def run():
        secret_dir = tmp_path / '.secrets'
        secret_dir.mkdir()
        (secret_dir / 'fake.key').write_text('synthetic-key', encoding='utf-8')
        role = {'backend': 'cloud', 'prompt_id': 'test', 'max_tokens': 10, 'sampling': {}, 'image': 'none',
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'cloud': {'provider': 'anthropic', 'engine': {'name': 'anthropic'},
                  'api_key_file': '.secrets/fake.key', 'anthropic_version': '2025-01-01', 'anthropic_workspace_id': 'none',
                  'model': {'id': 'fake', 'revision': 'r'}, 'endpoints': {'chat': 'messages'}}}, 'default_backend': 'main'}}

        class CloudDouble:
            ledger = None
            async def post(self, route, body, headers):
                self.headers = headers
                return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}]},
                                      request=httpx.Request('POST', 'https://example.org'))

        double = CloudDouble()
        client = RoleClient(config, {'cloud': double}, {'cloud': ProbeResult('complete', {}, {})}, None,
                            project_root=tmp_path)
        with pytest.raises(ModelCallError, match='ledger'):
            await client.call('planner', b'system', 'data')
        double.ledger = object()
        result, record = await client.call('planner', b'system', 'data')
        assert result == 'ok' and double.headers['Authorization'] == 'Bearer synthetic-key'
        assert double.headers['anthropic-version'] == '2025-01-01'
        assert record['engine'] == {'name': 'anthropic', 'version': '2025-01-01'}
        assert 'synthetic-key' not in str(record)

    asyncio.run(run())


def test_cloud_image_states_the_registered_media_type(tmp_path):
    """T-008 fix round 5 (R4F1): the Anthropic image `media_type` is Pillow's registered type, not the encoder name.
    A labelled transport double; the key is synthetic, under the test's own tmp_path."""
    async def run():
        (tmp_path / '.secrets').mkdir()
        (tmp_path / '.secrets' / 'fake.key').write_text('synthetic-key', encoding='utf-8')
        role = {'backend': 'cloud', 'prompt_id': 'test', 'max_tokens': 10, 'sampling': {},
                'image': {'source': 'source_image', 'max_side': 'none', 'format': 'jpeg2000', 'quality': 90},
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'anthropic', 'engine': {'name': 'anthropic'}, 'api_key_file': '.secrets/fake.key',
                   'anthropic_version': '2025-01-01', 'anthropic_workspace_id': 'none', 'allow_images': True,
                   'model': {'id': 'fake', 'revision': 'r'}, 'endpoints': {'chat': 'messages'},
                   'image': {'max_decode_pixels': 1000, 'max_side': 'none'}}
        config = {'vision': {'max_image_side': 'none', 'format': 'png', 'quality': 90,
                             'alpha_policy': {'mode': 'refuse'}},
                  'vlm': {'roles': {'planner': role}, 'backends': {'cloud': backend}, 'default_backend': 'cloud'}}

        class CloudDouble:
            ledger = object()

            async def post(self, route, body, headers):
                self.body = body
                return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}]},
                                      request=httpx.Request('POST', 'https://example.org'))

        double = CloudDouble()
        client = RoleClient(config, {'cloud': double}, {'cloud': ProbeResult('complete', {'image': True}, {})}, None,
                            project_root=tmp_path)
        await client.call('planner', b'system', 'data', images=[('source_image', png_bytes())])
        source = double.body['messages'][0]['content'][1]['source']
        with Image.open(BytesIO(base64.b64decode(source['data']))) as prepared:
            assert source['media_type'] == Image.MIME[prepared.format] == 'image/jp2'

    asyncio.run(run())


def test_cloud_budget_exhaustion_reprobes_and_falls_back(tmp_path):
    async def run():
        directory = tmp_path / '.secrets'
        directory.mkdir()
        (directory / 'fake.key').write_text('synthetic-key', encoding='utf-8')
        role = {'backend': 'cloud', 'prompt_id': 'test', 'max_tokens': 10, 'sampling': {}, 'image': 'none',
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        local = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'test-version'}, 'model': {'id': 'local', 'revision': 'r'},
                 'endpoints': {'chat': 'chat'}, 'probe': {'max_requests': 1, 'timeout_s': 1, 'image_attempts': 1}}
        cloud = {'provider': 'gemini', 'engine': {'name': 'gemini'}, 'api_key_file': '.secrets/fake.key', 'model': {'id': 'fake', 'revision': 'r'},
                 'endpoints': {'chat': 'v1beta/models/fake:generateContent'}, 'on_budget_exhausted': 'fallback_to_default_backend'}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': local, 'cloud': cloud}, 'default_backend': 'main'}}

        class Exhausted:
            ledger = object()
            async def post(self, route, body, headers):
                assert headers['x-goog-api-key'] == 'synthetic-key'
                raise BudgetExhausted('cloud')

        double = DoubleTransport('verified')
        cases = {'main': {'text': ({'probe': True}, lambda response: response.json()['choices'][0]['message']['content'] == 'verified')}}
        client = RoleClient(config, {'cloud': Exhausted(), 'main': double},
                            {'cloud': ProbeResult('complete', {}, {})}, None, project_root=tmp_path, probe_cases=cases)
        result, record = await client.call('planner', b'system', 'data')
        assert result == 'verified' and record['backend'] == 'main'
        assert ('BUDGET_EXHAUSTED', 'cloud') in record['warnings']
        assert client.probes['main'].capabilities['text'] is True

    asyncio.run(run())


def test_fallback_warnings_belong_to_each_attempt(tmp_path):
    async def run():
        directory = tmp_path / '.secrets'
        directory.mkdir()
        (directory / 'fake.key').write_text('synthetic', encoding='utf-8')
        role = {'backend': 'cloud', 'prompt_id': 'test', 'max_tokens': 10,
                'sampling': {'min_p': 0.2}, 'image': 'none',
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        local = {'provider': 'openai_compatible', 'engine': {'name': 'llama_cpp', 'version': 'v'},
                 'model': {'id': 'local', 'revision': 'r'}, 'endpoints': {'chat': 'chat'},
                 'probe': {'max_requests': 1, 'timeout_s': 1, 'image_attempts': 1}}
        cloud = {'provider': 'gemini', 'engine': {'name': 'gemini'}, 'api_key_file': '.secrets/fake.key',
                 'model': {'id': 'cloud', 'revision': 'r'}, 'endpoints': {'chat': 'v1beta/models/x:generateContent'},
                 'on_budget_exhausted': 'fallback_to_default_backend'}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'main': local, 'cloud': cloud},
                          'default_backend': 'main'}}

        class Exhausted:
            ledger = object()
            async def post(self, *args):
                raise BudgetExhausted('cloud')

        double = DoubleTransport('verified')
        cases = {'main': {'sampling.min_p': ({'probe': True}, lambda response: True)}}
        client = RoleClient(config, {'cloud': Exhausted(), 'main': double},
                            {'cloud': ProbeResult('complete', {}, {})}, None,
                            project_root=tmp_path, probe_cases=cases)
        _, record = await client.call('planner', b'system', 'data')
        warning = ('PARAM_UNSUPPORTED', 'sampling.min_p')
        assert warning not in record['warnings']
        assert warning in record['previous_attempts'][0]['warnings']
        assert record['previous_attempts'][0]['status'] == 'BUDGET_EXHAUSTED'

    asyncio.run(run())


def test_gemini_text_response_and_usage_with_synthetic_key(tmp_path):
    async def run():
        directory = tmp_path / '.secrets'
        directory.mkdir()
        (directory / 'fake.key').write_text('synthetic-key', encoding='utf-8')
        role = {'backend': 'cloud', 'prompt_id': 'test', 'max_tokens': 10, 'sampling': {'top_p': 0.7}, 'image': 'none',
                'reasoning': {'mode': 'auto', 'effort': 'none', 'budget_tokens': 'none'},
                'output': {'format': 'none', 'schema_id': 'none', 'strict': False, 'validate_response': False}}
        backend = {'provider': 'gemini', 'engine': {'name': 'gemini'}, 'api_key_file': '.secrets/fake.key', 'model': {'id': 'fake', 'revision': 'r'},
                   'endpoints': {'chat': 'v1beta/models/fake:generateContent'}}
        config = {'vlm': {'roles': {'planner': role}, 'backends': {'cloud': backend}, 'default_backend': 'main'}}

        class GeminiDouble:
            ledger = object()
            async def post(self, route, body, headers):
                assert headers['x-goog-api-key'] == 'synthetic-key'
                assert body['generationConfig']['topP'] == 0.7
                return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                    'parts': [{'text': 'answer'}]}}], 'usageMetadata': {'promptTokenCount': 4, 'candidatesTokenCount': 2}},
                    request=httpx.Request('POST', 'https://example.org'))

        client = RoleClient(config, {'cloud': GeminiDouble()}, {'cloud': ProbeResult('complete', {}, {})}, None,
                            project_root=tmp_path)
        result, record = await client.call('planner', b'system', 'data')
        assert result == 'answer' and record['usage']['promptTokenCount'] == 4
        assert record['engine'] == {'name': 'gemini', 'version': 'v1beta'}
        assert 'synthetic-key' not in str(record)

    asyncio.run(run())
