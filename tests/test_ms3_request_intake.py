"""MS-3 (decision 179; R-001, R-004, R-009, R-015, R-026): `request.v0` and `request.v1` at the parser and at S0, the
source cap, the structural refusals, the intake result for several images and the pipeline boundary until MS-4 and
MS-5. Synthetic tiny images; the only doubles are the labelled loopback model server, the drafter and the runner."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
from jsonschema import Draft202012Validator
import pytest
from referencing import Registry, Resource

from intake_fixtures import activate, configuration
from ms3_fixtures import activate_manifest_v2, configure_cap, images, png_b64, run_pipeline
from prompt_enhancer.cli_input import Refusal, parse_request
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.intake import intake, refuse_multi_source
import prompt_enhancer.intake as module
from prompt_enhancer.request_shape import request_version, source_images, structure_refusal
from prompt_enhancer.writer import prepare_write
from prompt_enhancer.planner import plan

ROOT = Path(__file__).resolve().parents[1]
PROMPT = 'SECRET-PROMPT-ΩΩ keep the cat'  # a marker no refusal text may repeat (R-026)
BAD = 'SECRETB64!!'  # not base64, a marker likewise


def edit(**fields):
    return {'prompt': PROMPT, 'task': 'edit', **fields}


def v1(count):
    return edit(source_images=images(count))


def body(request):
    return json.dumps(request).encode()


# ---- the parser: request file and HTTP body -------------------------------------------------------------------------

def test_v1_is_accepted_unchanged_in_caller_order():
    request = v1(3)
    assert parse_request(body(request), ROOT) == request
    assert source_images(request) == request['source_images']


@pytest.mark.parametrize('request_,message', [
    (edit(source_image=images(1)[0], source_images=images(1)),
     'request: source_image and source_images are both set; send one of them'),
    (edit(source_image=None, source_images=images(1)),
     'request: source_image and source_images are both set; send one of them'),
    (edit(source_images=[]), 'request.source_images: required by task edit, with at least one image'),
    ({'prompt': PROMPT, 'task': 'generate', 'source_images': images(1)},
     'request.source_images: not accepted by task generate'),
    (edit(source_images=images(1) + [BAD, 'AA==']), 'request.source_images[1]: not valid base64'),
    (edit(source_images=[BAD]), 'request.source_images[0]: not valid base64'),
    (edit(source_images=[images(1)[0], 7]), 'request.source_images.1: violates type'),
    (edit(source_images='x'), 'request.source_images: violates type'),
])
def test_v1_refusals_name_the_field_and_the_index_and_hold_no_value(request_, message):
    with pytest.raises(Refusal) as error:
        parse_request(body(request_), ROOT)
    assert str(error.value) == message
    assert BAD not in str(error.value) and 'SECRET' not in str(error.value)


def test_the_version_is_told_by_the_field_present():
    assert request_version({'source_images': []}) == 'request.v1'
    assert [request_version(item) for item in (None, [], {}, {'source_image': None}, {'source_image': 'x'})] == \
        ['request.v0'] * 5
    assert structure_refusal(None) is None and structure_refusal({'task': 'edit'}) is None  # the schema refuses these
    assert source_images({'source_image': None}) == [] and source_images({'source_image': 'x'}) == ['x']


def test_v0_text_is_unchanged():
    with pytest.raises(Refusal, match='^request.source_image: not valid base64$'):
        parse_request(body(edit(source_image=BAD)), ROOT)


VALID = st.binary(min_size=1, max_size=12).map(lambda data: base64.b64encode(data).decode())


@settings(max_examples=250, deadline=None)
@given(task=st.sampled_from(['generate', 'edit']), shape=st.sampled_from(['none', 'v0', 'v0null', 'v1', 'both']),
       items=st.lists(st.tuples(VALID, st.booleans()), max_size=4), prompt=st.text(max_size=20))
def test_parser_accepts_exactly_what_the_schema_and_rules_allow(task, shape, items, prompt):
    """Oracle written from the contract, not from the code: both fields refused; a generate takes no image; an edit takes
    one non-null valid string (v0) or a non-empty list of valid strings (v1); accepted output keeps the request and its
    image order."""
    strings = [value if good else value + '!' for value, good in items]
    request = {'prompt': prompt, 'task': task}
    if shape == 'v0':
        request['source_image'] = strings[0] if strings else None
    elif shape == 'v0null':
        request['source_image'] = None
    elif shape == 'v1':
        request['source_images'] = strings
    elif shape == 'both':
        request.update(source_image=strings[0] if strings else None, source_images=strings)
    goods = [good for _, good in items]
    if shape == 'both':
        expected = False
    elif shape == 'v1':
        expected = task == 'edit' and bool(strings) and all(goods)
    elif shape == 'v0' and strings:  # one string in `source_image`
        expected = task == 'edit' and goods[0]
    else:  # no image field, or `source_image: null`
        expected = task == 'generate'
    try:
        parsed = parse_request(body(request), ROOT)
    except Refusal:
        assert not expected
        return
    assert expected and parsed == request
    if shape == 'v1':
        assert source_images(parsed) == strings  # order is the caller's order


# ---- S0: ready results -----------------------------------------------------------------------------------------------

@pytest.fixture
def validator():
    schemas = {}
    for name in ('request.v0', 'request.v1', 'intake_result.v1', 'intake_result.v2', 'response_envelope.v1'):
        schemas[name] = json.loads((ROOT / 'contracts/schemas' / f'{name}.schema.json').read_bytes())
    registry = Registry().with_resources((name, Resource.from_contents(value)) for name, value in schemas.items())
    return lambda name, instance: Draft202012Validator(schemas[name], registry=registry).validate(instance)


def make_env(model_test_profile, tmp_path, project_root, *, cap=1, manifest_cap=None, mutate=None):
    config = configure_cap(configuration(model_test_profile, tmp_path), cap)
    if mutate is not None:
        mutate(config)
    if manifest_cap is None:
        activate(config, project_root)
    else:
        activate_manifest_v2(config, project_root, manifest_cap)
    return config


def raw(value):
    return base64.b64decode(value)


def test_several_images_build_intake_result_v2_in_request_order(model_test_profile, tmp_path, project_root, validator):
    config = make_env(model_test_profile, tmp_path, project_root, cap=3, manifest_cap=3)
    request = v1(3)
    with intake(request, config, project_root=project_root) as result:
        payload = result.payload
        assert payload['outcome'] == 'ready' and payload['request_version'] == 'request.v1'
        assert 'source_image' not in payload  # no stage can take image 1 for all of them
        assert [item['index'] for item in payload['source_images']] == [1, 2, 3]
        assert [item['image_id'] for item in payload['source_images']] == [
            'sha256:' + hashlib.sha256(raw(value)).hexdigest() for value in request['source_images']]
        assert all(item['role'] == 'source_image' for item in payload['source_images'])
        validator('intake_result.v2', payload)
        validator('request.v1', request)


def test_a_one_item_list_keeps_the_one_image_payload(model_test_profile, tmp_path, project_root, validator):
    config = make_env(model_test_profile, tmp_path, project_root, cap=3, manifest_cap=3)
    one = images(1)[0]
    with intake(edit(source_images=[one]), config, project_root=project_root) as listed, \
            intake(edit(source_image=one), config, project_root=project_root) as single:
        assert 'source_images' not in listed.payload and 'request_version' not in listed.payload
        assert listed.payload['source_image'] == single.payload['source_image']
        validator('intake_result.v1', listed.payload)


@pytest.mark.parametrize('cap,manifest_cap,count,key', [
    (3, 3, 3, None), (2, 5, 2, None), (5, 2, 2, None), (3, 3, 4, 'edit.max_source_images'),
    (2, 5, 3, 'edit.max_source_images'), (5, 2, 3, 'renderer.max_source_images'),
])
def test_the_cap_is_the_smaller_of_config_and_manifest(model_test_profile, tmp_path, project_root, monkeypatch,
                                                         cap, manifest_cap, count, key):
    """Accepted at the cap, refused at cap + 1 naming the limiting setting, with nothing decoded (R-015)."""
    config = make_env(model_test_profile, tmp_path, project_root, cap=cap, manifest_cap=manifest_cap)
    decoded, prepared = [], []
    monkeypatch.setattr(module, '_image', lambda *args: decoded.append(args) or b'')
    real = module.prepare_vision_image_with_info
    monkeypatch.setattr(module, 'prepare_vision_image_with_info', lambda *args: prepared.append(args) or real(*args))
    request = v1(count)
    if key is None:
        monkeypatch.undo()
        with intake(request, config, project_root=project_root) as result:
            assert result.payload['outcome'] == 'ready' and len(result.payload['source_images']) == count
        return
    result = intake(request, config, project_root=project_root)
    error = result.payload['error']
    assert result.payload['outcome'] == 'refused' and error['code'] == 'SOURCE_IMAGES_TOO_MANY'
    assert error['message'] == (f'intake refused (SOURCE_IMAGES_TOO_MANY): source_images: {count} images, '
                                f'at most {min(cap, manifest_cap)} allowed by {key}')
    assert decoded == [] and prepared == []  # refused on the count, before any base64 or Pillow work


def test_a_version_1_manifest_means_one_image(model_test_profile, tmp_path, project_root, monkeypatch):
    config = make_env(model_test_profile, tmp_path, project_root, cap=1)  # activate(): a schema_version 1 manifest
    prepared = []
    monkeypatch.setattr(module, 'prepare_vision_image_with_info', lambda *args: prepared.append(args))
    payload = intake(v1(2), config, project_root=project_root).payload
    assert payload['error']['code'] == 'SOURCE_IMAGES_TOO_MANY' and prepared == []
    assert payload['error']['message'].endswith('2 images, at most 1 allowed by edit.max_source_images')
    monkeypatch.undo()
    with intake(v1(1), config, project_root=project_root) as one:  # the cap itself is accepted
        assert one.payload['outcome'] == 'ready'


def test_source_cap_of_a_version_1_manifest_and_a_missing_limit(model_test_profile, tmp_path, project_root):
    """The helper directly: a version-1 manifest caps at 1 whatever the configuration says (a hand-built view), and a
    missing or sentinel limit is refused, never defaulted (R-015)."""
    view = SimpleNamespace(manifest={'schema_version': 1})
    assert module.source_cap({'edit': {'max_source_images': 4}}, view) == (1, 'the version-1 profile manifest')
    assert module.source_cap({'edit': {'max_source_images': 1}}, view) == (1, 'edit.max_source_images')
    for bad in (None, 'deploy_required', 0, True, 2.0):
        with pytest.raises(ConfigurationError, match='edit.max_source_images'):
            module.source_cap({'edit': {'max_source_images': bad}}, view)
    wide = SimpleNamespace(manifest={'schema_version': 2, 'renderer': {'max_source_images': None}})
    with pytest.raises(ConfigurationError, match='renderer.max_source_images'):
        module.source_cap({'edit': {'max_source_images': 2}}, wide)


# ---- S0: refusals -----------------------------------------------------------------------------------------------------

def test_duplicate_bytes_are_refused_before_any_pillow_call(model_test_profile, tmp_path, project_root, monkeypatch):
    config = make_env(model_test_profile, tmp_path, project_root, cap=4, manifest_cap=4)
    prepared = []
    monkeypatch.setattr(module, 'prepare_vision_image_with_info', lambda *args: prepared.append(args))
    one, two = images(2)
    payload = intake(edit(source_images=[one, two, one]), config, project_root=project_root).payload
    assert payload['error'] == {'code': 'SOURCE_IMAGE_DUPLICATE',
                                'message': 'intake refused (SOURCE_IMAGE_DUPLICATE): source_images[2] repeats source_images[0]'}
    assert prepared == [] and payload['outcome'] == 'refused'


def test_two_spellings_of_the_same_bytes_are_a_duplicate(model_test_profile, tmp_path, project_root):
    """`QQ==` and `QR==` decode to the same byte: the rule compares bytes, not the strings."""
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    assert raw('QQ==') == raw('QR==')
    payload = intake(edit(source_images=['QQ==', 'QR==']), config, project_root=project_root).payload
    assert payload['error']['code'] == 'SOURCE_IMAGE_DUPLICATE'


@pytest.mark.parametrize('items,code,text', [
    ([png_b64('red'), BAD], 'IMAGE_UNDECODABLE', 'source_images[1] is not valid base64'),
    ([png_b64('red'), 'YmFkIGJ5dGVz'], 'IMAGE_UNDECODABLE', 'source_images[1] is not a decodable image'),
    ([BAD], 'IMAGE_UNDECODABLE', 'source_images[0] is not valid base64'),
])
def test_a_bad_image_names_its_index_only(model_test_profile, tmp_path, project_root, items, code, text):
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    payload = intake(edit(source_images=items), config, project_root=project_root).payload
    assert payload['error'] == {'code': code, 'message': f'intake refused ({code}): {text}'}
    assert payload['source_image'] is None and 'source_images' not in payload


def test_an_image_over_the_decode_bound_is_rejected_with_its_index(model_test_profile, tmp_path, project_root):
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    big = png_b64('red', 200, 200)  # 40000 pixels against max_decode_pixels 10000 of the S0 fixture
    payload = intake(edit(source_images=[png_b64('blue'), big]), config, project_root=project_root).payload
    assert payload['error']['code'] in ('IMAGE_REJECTED', 'IMAGE_UNDECODABLE')
    assert payload['error']['message'].endswith('source_images[1] was rejected by the vision limits') or \
        payload['error']['message'].endswith('source_images[1] is not a decodable image')


@pytest.mark.parametrize('request_,code,message', [
    (edit(source_images=[]), 'SOURCE_IMAGE_REQUIRED',
     'intake refused (SOURCE_IMAGE_REQUIRED): request.source_images: required by task edit, with at least one image'),
    ({'prompt': PROMPT, 'task': 'generate', 'source_images': [png_b64('red')]}, 'SOURCE_IMAGE_UNEXPECTED',
     'intake refused (SOURCE_IMAGE_UNEXPECTED): request.source_images: not accepted by task generate'),
    (edit(source_image=png_b64('red'), source_images=[png_b64('red')]), 'SOURCE_IMAGES_CONFLICT',
     'intake refused (SOURCE_IMAGES_CONFLICT): request: source_image and source_images are both set; send one of them'),
])
def test_structural_refusals_of_a_v1_request(model_test_profile, tmp_path, project_root, request_, code, message):
    config = make_env(model_test_profile, tmp_path, project_root)
    payload = intake(request_, config, project_root=project_root).payload
    assert payload['outcome'] == 'refused' and payload['error'] == {'code': code, 'message': message}
    assert payload['envelope']['status'] == 'failed' and payload['envelope']['source_binding'] is None


@pytest.mark.parametrize('request_', [
    edit(source_images=[png_b64('red'), BAD]), edit(source_images=[png_b64('red'), png_b64('red')]),
    edit(source_images=[png_b64('red'), png_b64('green'), png_b64('blue')]), edit(source_images=[BAD]),
    edit(source_image=BAD, source_images=[BAD]), {'prompt': PROMPT, 'task': 'generate', 'source_images': [BAD]},
    edit(source_images=[]), edit(source_images=[png_b64('red'), 'YmFkIGJ5dGVz']),
])
def test_no_refusal_carries_a_request_value(model_test_profile, tmp_path, project_root, request_):
    """R-026: neither the prompt, an image string nor an exception text reaches the error or the envelope."""
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    payload = intake(request_, config, project_root=project_root).payload
    assert payload['outcome'] == 'refused'
    shown = json.dumps({'error': payload['error'], 'envelope': payload['envelope']}, ensure_ascii=False)
    assert 'SECRET' not in shown and BAD not in shown and 'YmFkIGJ5dGVz' not in shown
    for item in request_.get('source_images', []) + [request_.get('source_image') or '-']:
        assert item not in shown


def test_the_deadline_is_checked_between_images(model_test_profile, tmp_path, project_root, monkeypatch):
    config = make_env(model_test_profile, tmp_path, project_root, cap=3, manifest_cap=3)
    clock, calls = [0.0], []
    real = module.prepare_vision_image_with_info

    def slow(*args):
        calls.append(1)
        clock[0] += 1000  # one image takes longer than runtime.intake_deadline_s (60)
        return real(*args)

    monkeypatch.setattr(module, 'prepare_vision_image_with_info', slow)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    payload = intake(v1(3), config, project_root=project_root).payload
    assert payload['error']['code'] == 'INTAKE_DEADLINE' and payload['error']['message'] == \
        'intake refused (INTAKE_DEADLINE at image start)'
    assert len(calls) == 1  # image 2 and 3 were never decoded


def test_retrieval_off_with_several_images_builds_no_knowledge_gateway(model_test_profile, tmp_path, project_root,
                                                                         monkeypatch):
    """R-009: no knowledge access of any kind when retrieval is off, whatever the number of images."""
    config = make_env(model_test_profile, tmp_path, project_root, cap=3, manifest_cap=3)

    def forbidden(*args, **kwargs):
        raise AssertionError('retrieval off must not construct a knowledge gateway')

    monkeypatch.setattr(module, 'KnowledgeGateway', forbidden)
    with intake(v1(3), config, project_root=project_root) as result:
        assert result.payload['outcome'] == 'ready' and result.knowledge is None
        assert result.payload['retrieval'] == {'state': 'disabled', 'snapshot_id': None}


# ---- the boundary until MS-4 and MS-5 --------------------------------------------------------------------------------

def test_refuse_multi_source_turns_a_ready_result_into_one_documented_refusal(model_test_profile, tmp_path,
                                                                              project_root, validator):
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    with intake(v1(2), config, project_root=project_root) as result:
        assert refuse_multi_source(result) is True
        payload = result.payload
        assert payload['outcome'] == 'refused'
        assert payload['error'] == {'code': 'MULTI_SOURCE_UNAVAILABLE', 'message':
                                    'intake refused (MULTI_SOURCE_UNAVAILABLE): several source images are not yet supported '
                                    'by this build; send one source image'}
        assert payload['envelope']['status'] == 'failed' and payload['envelope']['output'] is None
        assert payload['envelope']['attachments'] == [] and payload['envelope']['source_binding'] is None
        validator('intake_result.v2', payload)


@pytest.mark.parametrize('request_', [edit(source_image=png_b64('red')), edit(source_images=[png_b64('red')])])
def test_refuse_multi_source_leaves_one_image_alone(model_test_profile, tmp_path, project_root, request_):
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    with intake(request_, config, project_root=project_root) as result:
        before = json.dumps(result.payload, sort_keys=True)
        assert refuse_multi_source(result) is False
        assert json.dumps(result.payload, sort_keys=True) == before and result.payload['outcome'] == 'ready'


def test_no_stage_can_take_one_image_of_several(model_test_profile, tmp_path, project_root):
    """The v2 payload has no `source_image`, so the planner and the writer (one image until MS-4/5) fail instead of
    sending image 1 for all of them; the model double records that no call is made."""
    config = make_env(model_test_profile, tmp_path, project_root, cap=2, manifest_cap=2)
    calls = []

    class ClientDouble:
        role_name = 'planner'

        async def call(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError('a model call was made')

    with intake(v1(2), config, project_root=project_root) as result:
        with pytest.raises(KeyError):
            asyncio.run(plan(result, None, ClientDouble(), project_root=project_root))
        with pytest.raises(KeyError):
            prepare_write(result, {}, None, project_root=project_root)
    assert calls == []


def test_the_pipeline_refuses_several_images_before_any_model_call(model_test_profile, tmp_path, project_root):
    outcome = run_pipeline(model_test_profile, tmp_path, project_root, v1(3), cap=3, manifest_cap=3)
    envelope = outcome['envelope']
    assert envelope['status'] == 'failed' and envelope['output'] is None and envelope['attachments'] == []
    assert envelope['source_binding'] is None and envelope['retrieval']['state'] == 'disabled'
    assert outcome['body']['intake_error']['code'] == 'MULTI_SOURCE_UNAVAILABLE'
    assert outcome['calls'] == [] and outcome['seen'] == 0  # no planner, writer, verifier or probe request at all


def test_the_pipeline_refuses_a_list_over_the_cap(model_test_profile, tmp_path, project_root):
    outcome = run_pipeline(model_test_profile, tmp_path, project_root, v1(4), cap=3, manifest_cap=3)
    assert outcome['envelope']['status'] == 'failed' and outcome['seen'] == 0
    assert outcome['body']['intake_error']['code'] == 'SOURCE_IMAGES_TOO_MANY'
