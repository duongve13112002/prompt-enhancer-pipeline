"""Embedding server wire format and pooling arithmetic (T-018): example, edge and property tests, CPU only."""

import base64
from io import BytesIO
import json
import math
import struct

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
from PIL import Image
import pytest

from prompt_enhancer.embed_server.pooling import l2_normalize, last_token_index, mean_from_sums
from prompt_enhancer.embed_server.protocol import (EmbedItem, EngineError, Limits, RequestError, build_response,
                                                   error_body, parse_request)
from prompt_enhancer.k1_embed import Embedder, embedder_settings, parse_embeddings
from embed_server_fixtures import server_profile
from k1_fixtures import image_bytes

LIMITS = Limits(model_id='synthetic-embedder', max_items=3, max_image_bytes=5000, max_decode_pixels=1000,
                accepts_instruction=True)
NO_INSTRUCTION = Limits(model_id='synthetic-embedder', max_items=3, max_image_bytes=5000, max_decode_pixels=1000,
                        accepts_instruction=False)
FAST = hsettings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def uri(data, media='image/png'):
    return f'data:{media};base64,' + base64.b64encode(data).decode('ascii')


def body(**fields):
    request = {'model': 'synthetic-embedder', 'encoding_format': 'float', 'input': ['a text']}
    request.update(fields)
    return json.dumps(request).encode('utf-8')


def refused(raw, limits=LIMITS):
    with pytest.raises(RequestError) as caught:
        parse_request(raw, limits)
    assert 400 <= caught.value.status < 500 and caught.value.code
    return caught.value


@pytest.fixture
def k1_embedder(model_test_profile):
    return Embedder(embedder_settings(server_profile(model_test_profile)))


# --- every `input` shape K1 can send -----------------------------------------------------------------------------

def test_k1_text_bodies_round_trip(k1_embedder):
    [request] = k1_embedder.text_bodies(['first', 'second text'])
    items = parse_request(json.dumps(request).encode(), LIMITS)
    assert [item.text for item in items] == ['first', 'second text'] and all(item.image is None for item in items)


def test_k1_image_bodies_round_trip_with_and_without_instruction(model_test_profile):
    images = [image_bytes((6, 4), (1, 2, 3)), image_bytes((3, 5), (9, 9, 9))]
    for instruction, expected in (('none', None), ('Represent the input.', 'Represent the input.')):
        profile = server_profile(model_test_profile)
        profile['embedding']['request']['image_instruction'] = instruction
        [request] = Embedder(embedder_settings(profile)).image_bodies(images)
        items = parse_request(json.dumps(request).encode(), LIMITS)
        assert [(item.image.size, item.instruction) for item in items] == [((6, 4), expected), ((3, 5), expected)]
        assert items[0].image.getpixel((0, 0)) == (1, 2, 3) and items[0].image.mode == 'RGB'


@given(st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=3),
       st.lists(st.tuples(st.integers(1, 12), st.integers(1, 12), st.tuples(*[st.integers(0, 255)] * 3)),
                min_size=1, max_size=3))
@FAST
def test_property_any_k1_batch_is_accepted_in_order(texts, pictures):
    request ={'model': 'synthetic-embedder', 'encoding_format': 'float', 'input': texts}
    assert [item.text for item in parse_request(json.dumps(request).encode(), LIMITS)] == texts
    parts = [{'content': [{'type': 'image_url', 'image_url': {'url': uri(image_bytes((w, h), color))}}]}
             for w, h, color in pictures]
    items = parse_request(json.dumps(dict(request, input=parts)).encode(), LIMITS)
    assert [item.image.size for item in items] == [(w, h) for w, h, _ in pictures]


# --- refusals -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('raw', [b'', b'not json', b'\xff\xfe', b'[]', b'null', b'"text"', b'1', b'{' * 100000,
                                 b'[' * 100000])
def test_malformed_bodies_are_a_400_with_a_code(raw):
    assert refused(raw).status == 400


@pytest.mark.parametrize('fields', [
    {'input': 'bare string'}, {'input': []}, {'input': None}, {'input': 5}, {'input': ['']}, {'input': [None]},
    {'input': [5]}, {'input': [['nested']]}, {'input': ['a'] * 4},  # more than max_items
    {'model': 'other'}, {'model': None}, {'encoding_format': 'base64'}, {'encoding_format': 5},
    {'dimensions': 4}, {'user': 'x'}, {'input': [{'content': 'text'}]}, {'input': [{'content': []}]},
    {'input': [{'content': [{'type': 'text', 'text': 'no image'}]}]},
    {'input': [{'content': [], 'extra': 1}]}, {'input': [{}]}])
def test_refused_shapes(fields):
    assert refused(body(**fields)).status == 400


def test_a_missing_model_is_refused():
    raw = json.dumps({'input': ['a']}).encode()
    assert refused(raw).code == 'unknown_model'


def test_encoding_format_may_be_left_out():
    raw = json.dumps({'model': 'synthetic-embedder', 'input': ['a']}).encode()
    assert [item.text for item in parse_request(raw, LIMITS)] == ['a']


def part(url):
    return {'type': 'image_url', 'image_url': {'url': url}}


@pytest.mark.parametrize('url', [
    'http://example.invalid/a.png', 'https://example.invalid/a.png', 'file:///etc/passwd', 'ftp://x/y',
    '/local/path.png', 'data:image/png,AAAA', 'data:text/plain;base64,AAAA', 'data:image/;base64,AAAA',
    'data:;base64,AAAA', 'data:image/png;charset=x;base64,AAAA', 'DATA:image/png;base64,AAAA', '', 5, None])
def test_only_data_uris_of_an_image_type_are_accepted(url):
    assert refused(body(input=[{'content': [part(url)]}])).code in {
        'unsupported_image_url', 'unsupported_media_type', 'invalid_image'}


def test_no_network_is_opened_for_a_remote_url(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError('the parser opened a socket')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    assert refused(body(input=[{'content': [part('http://127.0.0.1:1/a.png')]}])).code == 'unsupported_image_url'


@pytest.mark.parametrize('payload', ['@@@@', 'AAA', 'AAAA====', 'A A A A', 'AAAA\n'])
def test_invalid_base64_is_refused(payload):
    assert refused(body(input=[{'content': [part('data:image/png;base64,' + payload)]}])).code in {
        'invalid_image', 'undecodable_image'}


def test_bytes_that_are_not_an_image_are_refused():
    assert refused(body(input=[{'content': [part(uri(b'plain bytes, no image'))]}])).code == 'undecodable_image'
    assert refused(body(input=[{'content': [part(uri(image_bytes()[:30]))]}])).code == 'undecodable_image'


EPS = b'%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\n%%EndComments\n{} loop\nshowpage\n'  # labelled: never rendered


class Dispatch:
    """Records every way Pillow could start a process or create a file while decoding (labelled spy)."""

    def __init__(self, monkeypatch):
        import subprocess
        import tempfile
        from PIL import EpsImagePlugin

        self.calls = []
        for owner, name in ((EpsImagePlugin, 'Ghostscript'), (subprocess, 'Popen'), (subprocess, 'run'),
                            (subprocess, 'check_call'), (tempfile, 'mkstemp'), (tempfile, 'NamedTemporaryFile'),
                            (tempfile, 'TemporaryFile')):
            monkeypatch.setattr(owner, name, self._spy(name))

    def _spy(self, name):
        def spy(*args, **kwargs):
            self.calls.append(name)
            raise RuntimeError('labelled spy: nothing was started')
        return spy


def test_the_spy_sees_the_postscript_dispatch_when_the_format_is_not_restricted(monkeypatch):
    """Positive control: without `formats=` Pillow dispatches the PostScript interpreter for these very bytes."""
    spy = Dispatch(monkeypatch)
    with pytest.raises(RuntimeError):
        Image.open(BytesIO(EPS)).load()
    assert spy.calls and spy.calls[0] in {'Ghostscript', 'mkstemp', 'NamedTemporaryFile', 'TemporaryFile'}


@pytest.mark.parametrize('media', ['image/png', 'application/postscript', 'image/x-eps', 'image/jpeg'])
def test_postscript_bytes_never_dispatch_an_interpreter_under_any_media_type(monkeypatch, media):
    spy = Dispatch(monkeypatch)
    error = refused(body(input=[{'content': [part(uri(EPS, media))]}]))
    assert error.code in {'undecodable_image', 'unsupported_media_type'} and spy.calls == []


def test_decoding_an_allowed_format_starts_no_process_and_creates_no_file(monkeypatch):
    spy = Dispatch(monkeypatch)
    for fmt, media in (('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp'), ('GIF', 'image/gif'),
                       ('BMP', 'image/bmp'), ('TIFF', 'image/tiff')):
        item = parse_request(body(input=[{'content': [part(uri(image_bytes(fmt=fmt), media))]}]), LIMITS)[0]
        assert item.image.size == (6, 4), fmt
    assert spy.calls == []


@pytest.mark.parametrize('fmt, media', [('PNG', 'image/jpeg'), ('JPEG', 'image/png'), ('GIF', 'image/webp'),
                                        ('PNG', 'image/gif')])
def test_the_detected_format_must_match_the_declared_media_type(fmt, media):
    assert refused(body(input=[{'content': [part(uri(image_bytes(fmt=fmt), media))]}])).code == 'unsupported_media_type'


def test_formats_outside_the_allowed_set_are_refused_even_with_a_matching_media_type():
    from prompt_enhancer.embed_server.protocol import ALLOWED_FORMATS

    buffer = BytesIO()
    Image.new('RGB', (4, 3)).save(buffer, 'PPM')  # a raster format that is not in the allowed set
    assert 'PPM' not in ALLOWED_FORMATS
    assert refused(body(input=[{'content': [part(uri(buffer.getvalue(), 'image/x-portable-anymap'))]}])).code == (
        'undecodable_image')


def test_every_allowed_format_is_in_process_and_registered_with_a_media_type():
    """Audit of the installed Pillow: an allowed format needs a media type, and EPS (the one decoder that renders
    through an external interpreter on load) is not allowed."""
    from prompt_enhancer.embed_server.protocol import ALLOWED_FORMATS

    Image.init()
    assert all(isinstance(Image.MIME.get(name), str) and Image.MIME[name].startswith('image/')
               for name in ALLOWED_FORMATS)
    assert {'EPS', 'WMF', 'PDF', 'MPEG'}.isdisjoint(ALLOWED_FORMATS)


def test_an_empty_payload_is_refused():
    assert refused(body(input=[{'content': [part('data:image/png;base64,')]}])).code == 'invalid_image'


def test_image_byte_and_pixel_limits():
    big = image_bytes((40, 40), mode='RGB')  # 1600 pixels > 1000
    assert refused(body(input=[{'content': [part(uri(big))]}])).code == 'image_too_large'
    noisy = BytesIO()
    Image.effect_noise((60, 60), 80).save(noisy, 'PNG')
    tight = Limits('synthetic-embedder', 3, len(noisy.getvalue()) - 1, 10_000, True)
    assert refused(body(input=[{'content': [part(uri(noisy.getvalue()))]}]), tight).code == 'image_too_large'
    exact = Limits('synthetic-embedder', 3, len(noisy.getvalue()), 10_000, True)
    assert parse_request(body(input=[{'content': [part(uri(noisy.getvalue()))]}]), exact)[0].image.size == (60, 60)


def test_one_image_and_one_leading_instruction_only():
    ok = image_bytes()
    text = {'type': 'text', 'text': 'be brief'}
    assert parse_request(body(input=[{'content': [text, part(uri(ok))]}]), LIMITS)[0].instruction == 'be brief'
    for content in ([part(uri(ok)), text], [text, text, part(uri(ok))], [part(uri(ok)), part(uri(ok))],
                    [{'type': 'text', 'text': ''}, part(uri(ok))], [{'type': 'audio'}], [{'type': 'text'}],
                    [{'type': 'text', 'text': 'x', 'extra': 1}, part(uri(ok))],
                    [{'type': 'image_url', 'image_url': {'url': uri(ok), 'detail': 'high'}}]):
        assert refused(body(input=[{'content': content}])).status == 400


def test_a_family_without_an_instruction_refuses_one():
    ok = image_bytes()
    raw = body(input=[{'content': [{'type': 'text', 'text': 'be brief'}, part(uri(ok))]}])
    assert refused(raw, NO_INSTRUCTION).code == 'instruction_unsupported'
    assert parse_request(body(input=[{'content': [part(uri(ok))]}]), NO_INSTRUCTION)[0].instruction is None


def test_alpha_and_palette_images_become_rgb():
    for mode in ('RGBA', 'L', 'P'):
        buffer = BytesIO()
        Image.new(mode, (4, 3)).save(buffer, 'PNG')
        item = parse_request(body(input=[{'content': [part(uri(buffer.getvalue()))]}]), LIMITS)[0]
        assert item.image.mode == 'RGB'


@given(st.binary(max_size=300))
@FAST
def test_property_arbitrary_bytes_never_escape_as_another_exception(raw):
    try:
        parse_request(raw, LIMITS)
    except RequestError:
        pass


JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=12),
                    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3),
                    max_leaves=12)


@given(JSON)
@FAST
def test_property_arbitrary_json_never_escapes_as_another_exception(value):
    try:
        parse_request(json.dumps(value).encode(), LIMITS)
    except RequestError:
        pass


@given(st.dictionaries(st.sampled_from(['model', 'encoding_format', 'input', 'dimensions']), JSON, max_size=4))
@FAST
def test_property_request_shaped_json_never_escapes(value):
    value.setdefault('model', 'synthetic-embedder')
    try:
        parse_request(json.dumps(value).encode(), LIMITS)
    except RequestError:
        pass


@given(st.text(max_size=80))
@FAST
def test_property_arbitrary_data_uri_text_is_refused_or_decoded(url):
    try:
        item = parse_request(body(input=[{'content': [part(url)]}]), LIMITS)[0]
    except RequestError:
        return
    assert item.image is not None and item.image.mode == 'RGB'


# --- response ----------------------------------------------------------------------------------------------------

def test_response_shape_and_k1_parse_round_trip():
    vectors = [[0.5, -0.25, 0.125, 1.0], [0.0, 1.0, 2.0, -3.0]]
    payload = json.loads(build_response('synthetic-embedder', vectors, 7))
    assert payload['object'] == 'list' and payload['model'] == 'synthetic-embedder'
    assert payload['usage'] == {'prompt_tokens': 7, 'total_tokens': 7}
    assert [item['index'] for item in payload['data']] == [0, 1]
    assert parse_embeddings(payload, 2, 4, 'native') == vectors  # exactly representable in float32


@given(st.lists(st.lists(st.floats(min_value=-1e3, max_value=1e3, allow_nan=False, width=32), min_size=1, max_size=6)
                .map(lambda row: row + [1.0]), min_size=1, max_size=4))
@FAST
def test_property_response_round_trips_through_k1_parser(rows):
    width = max(map(len, rows))
    rows = [row + [0.5] * (width - len(row)) for row in rows]
    payload = json.loads(build_response('m', rows, 1))
    expected = [list(struct.unpack(f'<{width}f', struct.pack(f'<{width}f', *row))) for row in rows]
    assert parse_embeddings(payload, len(rows), width, 'native') == expected


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_a_non_finite_vector_is_an_engine_error_not_invalid_json(bad):
    with pytest.raises(EngineError):
        build_response('m', [[0.0, bad]], 1)


def test_error_body_is_json_with_code_and_message():
    assert json.loads(error_body('c', 'm')) == {'error': {'code': 'c', 'message': 'm'}}


def test_embed_item_defaults():
    assert EmbedItem(text='t').image is None and EmbedItem(text='t').instruction is None


# --- pooling arithmetic ---------------------------------------------------------------------------------------

def test_mean_from_sums():
    assert mean_from_sums([2.0, 4.0, -6.0], 2) == [1.0, 2.0, -3.0]
    for count in (0, -1, 1.0, True, None):
        with pytest.raises(ValueError):
            mean_from_sums([1.0], count)


def test_last_token_index_for_right_and_left_padding():
    assert last_token_index([1, 1, 1, 0, 0]) == 2
    assert last_token_index([0, 0, 1, 1, 1]) == 4
    assert last_token_index([1]) == 0 and last_token_index([1, 0, 1, 0]) == 2
    with pytest.raises(ValueError):
        last_token_index([0, 0])


def test_l2_normalize_examples_and_refusals():
    assert l2_normalize([3, 4]) == pytest.approx([0.6, 0.8])
    assert l2_normalize([1e-200, 1e-200]) == pytest.approx([2 ** -0.5, 2 ** -0.5])
    assert l2_normalize([1e200, -1e200]) == pytest.approx([2 ** -0.5, -(2 ** -0.5)])
    for bad in ([], [0, 0], [1, math.nan], [math.inf, 1]):
        with pytest.raises(ValueError):
            l2_normalize(bad)


@given(st.lists(st.floats(min_value=-1e30, max_value=1e30, allow_nan=False), min_size=1, max_size=12)
       .filter(lambda values: any(values)))
@FAST
def test_property_l2_normalize_is_unit_length_and_keeps_direction(values):
    unit = l2_normalize(values)
    assert math.isclose(math.fsum(value * value for value in unit), 1.0, rel_tol=1e-9)
    largest = max(range(len(values)), key=lambda index: abs(values[index]))
    assert (unit[largest] > 0) == (values[largest] > 0)


@given(st.lists(st.integers(0, 1), min_size=1, max_size=20).filter(any))
@FAST
def test_property_last_token_is_the_last_one_in_the_mask(mask):
    index = last_token_index(mask)
    assert mask[index] == 1 and not any(mask[index + 1:])


@given(st.lists(st.lists(st.floats(-100, 100, allow_nan=False), min_size=3, max_size=3), min_size=1, max_size=6))
@FAST
def test_property_mean_from_sums_matches_the_plain_mean(tokens):
    sums = [math.fsum(token[axis] for token in tokens) for axis in range(3)]
    mean = mean_from_sums(sums, len(tokens))
    assert mean == pytest.approx([math.fsum(token[axis] for token in tokens) / len(tokens) for axis in range(3)])
