"""K1 part 1: image embeddings through a labelled HTTP double, resume, keys and response parsing (T-008)."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import functools
from io import BytesIO
import json
import math
import os
import random
import warnings

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
import httpx
from PIL import Image, ImageFile
import pytest

from prompt_enhancer import k1_embed
from prompt_enhancer.k1_embed import (EmbeddingError, REUSED_REFERENCED, SOURCE_CHANGED, embed_images,
                                      embedder_settings, image_key, parse_embeddings)
from prompt_enhancer.storage.store import Checkpoints
from prompt_enhancer.storage.tables import SNAPSHOT_TABLES, SPECS
from k1_fixtures import (EmbeddingServer, bigtiff_with_strip_offset, data_uri_image, image_bytes, k0_handoff,
                         k1_profile, make_source)
from storage_fixtures import make_store


@pytest.fixture
def kb(project_root, tmp_path):
    source = make_source(tmp_path / 'source', {
        'a.png': image_bytes(color=(10, 20, 30)), 'b.png': image_bytes(size=(5, 9), color=(40, 50, 60)),
        'c.png': image_bytes(mode='RGBA', color=(1, 2, 3, 128)), 'broken.png': b'not an image'})
    store = make_store(project_root, tmp_path / 'kb')
    return store, source, k0_handoff(store, source)


def run(store, config, handoff, source, server):
    return asyncio.run(embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                    transport=server.transport()))


def ids(handoff):
    return sorted(row['image_id'] for row in handoff['images'])


def test_embeds_every_image_once_with_metadata_and_documented_body(kb, model_test_profile):
    store, source, handoff = kb
    server = EmbeddingServer()
    result = run(store, k1_profile(model_test_profile), handoff, source, server)
    assert result['report']['embedded'] == 3
    assert list(result['report']['decode_failed'].values()) == ['undecodable']
    assert sorted(server.input_counts()) == [1, 2]  # batch_size 2 from the conftest profile; batches run concurrently
    url, body = server.requests[0]
    assert url == 'http://127.0.0.1:8080/v1/embeddings'
    assert body['model'] == 'synthetic-embedder' and body['encoding_format'] == 'float'
    part = body['input'][0]['content'][0]  # image_instruction none: the image part alone (llama.cpp A1)
    header, image = data_uri_image(part)
    assert header == 'data:image/jpeg;base64' and image.format == 'JPEG'  # vision.format and vision.transport
    rows = {row['image_id']: row for row in result['images']}
    assert sorted(row['decode_status'] for row in rows.values()) == ['ok', 'ok', 'ok', 'undecodable']
    sizes = sorted((row['width'], row['height'], row['format']) for row in rows.values() if row['format'])
    assert sizes == [(5, 9, 'PNG'), (6, 4, 'PNG'), (6, 4, 'PNG')]
    table = result['image_embeddings']
    assert table.num_rows == 3 and table.schema.field('vector').type.list_size == 4
    assert set(table.column('embedder_revision').to_pylist()) == {result['embedder_revision']}


def test_rerun_makes_no_call_and_returns_the_same_table(kb, model_test_profile):
    store, source, handoff = kb
    config = k1_profile(model_test_profile)
    first = run(store, config, handoff, source, EmbeddingServer())
    server = EmbeddingServer()
    second = run(store, config, handoff, source, server)
    assert server.requests == [] and second['report']['skipped_done'] == 4
    assert second['image_embeddings'].equals(first['image_embeddings'])
    assert second['images'] == first['images']


def test_interrupted_run_resumes_at_the_first_unfinished_image(kb, model_test_profile, monkeypatch, tmp_path,
                                                               project_root):
    store, source, handoff = kb
    config = k1_profile(model_test_profile)
    original = Checkpoints.commit
    calls = []

    def interrupt(self, *args, **kwargs):  # labelled interruption double: crash after the second artifact
        result = original(self, *args, **kwargs)
        calls.append(args[0])
        if len(calls) == 2:
            raise RuntimeError('simulated interruption')
        return result

    monkeypatch.setattr(Checkpoints, 'commit', interrupt)
    with pytest.raises(RuntimeError, match='simulated'):
        run(store, config, handoff, source, EmbeddingServer())
    monkeypatch.setattr(Checkpoints, 'commit', original)
    server = EmbeddingServer()
    resumed = run(store, config, handoff, source, server)
    report = resumed['report']
    assert report['skipped_done'] == 2 and report['embedded'] + len(report['decode_failed']) == 2
    assert sum(server.input_counts()) == report['embedded']  # only unfinished images reach the server
    fresh = make_store(project_root, tmp_path / 'fresh')
    clean = run(fresh, config, k0_handoff(fresh, source), source, EmbeddingServer())
    assert resumed['image_embeddings'].equals(clean['image_embeddings'])


def test_embedder_change_reembeds_every_image(kb, model_test_profile):
    store, source, handoff = kb
    first = run(store, k1_profile(model_test_profile), handoff, source, EmbeddingServer())
    changed = k1_profile(model_test_profile, revision='other')
    changed['vlm']['backends']['embedder']['model']['revision'] = 'other'
    server = EmbeddingServer()
    second = run(store, changed, handoff, source, server)
    assert second['report']['embedded'] == 3 and sum(server.input_counts()) == 3
    assert second['embedder_revision'] != first['embedder_revision']


def test_image_setting_change_reembeds_in_the_same_space(kb, model_test_profile):
    store, source, handoff = kb
    first = run(store, k1_profile(model_test_profile), handoff, source, EmbeddingServer())
    changed = k1_profile(model_test_profile)
    changed['vision']['max_image_side'] = 3  # non-default override: the shared resize reaches the request
    server = EmbeddingServer()
    second = run(store, changed, handoff, source, server)
    assert second['report']['embedded'] == 3
    assert second['embedder_revision'] == first['embedder_revision']
    _, image = data_uri_image(server.requests[0][1]['input'][0]['content'][0])
    assert max(image.size) == 3


def test_changed_source_fails_then_recovers(kb, model_test_profile):
    store, source, handoff = kb
    original = (source / 'folder' / 'a.png').read_bytes()
    (source / 'folder' / 'a.png').write_bytes(image_bytes(color=(99, 99, 99)))
    config = k1_profile(model_test_profile)
    result = run(store, config, handoff, source, EmbeddingServer())
    assert list(result['report']['failed'].values()) == [SOURCE_CHANGED]
    failed = next(iter(result['report']['failed']))
    assert result['image_embeddings'].num_rows == 2
    assert next(row for row in result['images'] if row['image_id'] == failed)['decode_status'] is None
    (source / 'folder' / 'a.png').write_bytes(original)
    server = EmbeddingServer()
    again = run(store, config, handoff, source, server)
    assert again['report']['embedded'] == 1 and again['report']['failed'] == {}


def test_linked_source_file_is_never_read(kb, model_test_profile, tmp_path):
    store, source, handoff = kb
    target = source / 'folder' / 'a.png'
    outside = tmp_path / 'outside.png'
    outside.write_bytes(target.read_bytes())  # same bytes, so only the link rule can refuse it
    target.unlink()
    try:
        os.symlink(outside, target)
    except OSError:
        pytest.skip('this platform or account cannot create a symbolic link')
    result = run(store, k1_profile(model_test_profile), handoff, source, EmbeddingServer())
    assert list(result['report']['failed'].values()) == [SOURCE_CHANGED]


def test_http_error_fails_items_without_done_rows(kb, model_test_profile):
    store, source, handoff = kb
    config = k1_profile(model_test_profile)
    result = run(store, config, handoff, source, EmbeddingServer(status=500))
    assert set(result['report']['failed'].values()) == {'HTTP_ERROR'} and len(result['report']['failed']) == 3
    assert result['image_embeddings'].num_rows == 0
    again = run(store, config, handoff, source, EmbeddingServer())
    assert again['report']['embedded'] == 3


@pytest.mark.parametrize(('change', 'status', 'expected'), [
    ({'alpha_policy': {'mode': 'refuse'}}, 'alpha_refused', [('PNG', 6, 4)]),  # c.png
    ({'max_decode_pixels': 30}, 'exceeds_max_decode_pixels', [('PNG', 5, 9)]),  # b.png: 45 pixels
])
def test_policy_refusals_are_recorded_with_file_metadata(kb, model_test_profile, change, status, expected):
    store, source, handoff = kb
    config = k1_profile(model_test_profile)
    if 'alpha_policy' in change:
        config['vision']['alpha_policy'] = change['alpha_policy']
    else:
        config['vlm']['backends']['embedder']['image']['max_decode_pixels'] = change['max_decode_pixels']
    result = run(store, config, handoff, source, EmbeddingServer())
    refused = [(row['format'], row['width'], row['height']) for row in result['images'] if row['decode_status'] == status]
    assert refused == expected


# ----------------------------------------------------------------------------- properties: stored image metadata

def k1_settings(model_test_profile, **changes):
    return replace(embedder_settings(k1_profile(model_test_profile)), **changes)


TAGGED_FORMATS = ('PNG', 'JPEG', 'WEBP', 'TIFF', 'AVIF')  # formats whose orientation tag Pillow writes and reads back
ORIENTATIONS = [None, 1, 2, 3, 4, 5, 6, 7, 8]


@hsettings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(1, 24), st.integers(1, 24), st.sampled_from([*TAGGED_FORMATS, 'BMP', 'GIF']),
       st.sampled_from(ORIENTATIONS))
def test_decode_records_the_stored_files_format_and_size(model_test_profile, width, height, fmt, orientation):
    """K1 stores the file's own format and pixel size before any orientation (Q4), whatever the format and tag.

    The oracle is the generated pixel grid. TIFF is included with its own orientation tag: Pillow's TIFF opener
    reports an already-swapped size for orientations 5 to 8 (retest 1, RT2)."""
    tagged = orientation if fmt in TAGGED_FORMATS else None
    data = image_bytes(size=(width, height), fmt=fmt, orientation=tagged)
    metadata, prepared = k1_embed._decode(data, k1_settings(model_test_profile))
    assert {key: metadata[key] for key in ('format', 'width', 'height')} == {
        'format': fmt, 'width': width, 'height': height}
    if prepared is None:  # GIF opens as a palette image, which the shared JPEG encoder refuses (T-009 behaviour)
        assert (fmt, metadata['decode_status']) == ('GIF', 'encode_failed')
        return
    assert metadata['decode_status'] == 'ok'
    with Image.open(BytesIO(prepared)) as image:
        assert image.size == ((height, width) if tagged in (5, 6, 7, 8) else (width, height))


@hsettings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(2, 24), st.integers(2, 24), st.sampled_from(['exceeds', 'alpha', 'encode']),
       st.sampled_from(['PNG', 'TIFF']), st.sampled_from(ORIENTATIONS))
def test_refused_images_keep_the_stored_files_metadata(model_test_profile, width, height, refusal, fmt, orientation):
    size = (width, height)
    if refusal == 'exceeds':
        data, changes = image_bytes(size=size, fmt=fmt, orientation=orientation), {'max_decode_pixels': width * height - 1}
        status = 'exceeds_max_decode_pixels'
    elif refusal == 'alpha':
        data = image_bytes(size=size, mode='RGBA', color=(1, 2, 3, 128), fmt=fmt, orientation=orientation)
        vision = dict(embedder_settings(k1_profile(model_test_profile)).vision, alpha_policy={'mode': 'refuse'})
        changes, status = {'vision': vision}, 'alpha_refused'
    else:
        data, changes = image_bytes(size=size, mode='I;16', color=7, fmt=fmt, orientation=orientation), {}
        status = 'encode_failed'
    metadata, prepared = k1_embed._decode(data, k1_settings(model_test_profile, **changes))
    assert prepared is None
    assert metadata == {'format': fmt, 'width': width, 'height': height, 'decode_status': status}


@pytest.mark.parametrize('orientation', range(1, 9))
def test_tiff_stores_its_own_size_tags(model_test_profile, orientation):
    """Retest 1 probe `test_tiff_stored_dimensions`: K1's size is TIFF's ImageWidth/ImageLength, not Pillow's size."""
    data = image_bytes(size=(9, 7), fmt='TIFF', orientation=orientation)
    with Image.open(BytesIO(data)) as image:
        assert (image.tag_v2[256], image.tag_v2[257], image.tag_v2[274]) == (9, 7, orientation)
    metadata, prepared = k1_embed._decode(data, k1_settings(model_test_profile))
    assert metadata == {'format': 'TIFF', 'width': 9, 'height': 7, 'decode_status': 'ok'}
    with Image.open(BytesIO(prepared)) as image:
        assert image.size == ((7, 9) if orientation in (5, 6, 7, 8) else (9, 7))


def test_tiff_metadata_persists_with_its_own_size(project_root, tmp_path, model_test_profile):
    """Retest 1 probe `test_tiff_metadata_persists`: the entry point stores and resumes the TIFF's own size."""
    source = make_source(tmp_path / 'source', {'turned.tif': image_bytes(size=(9, 7), fmt='TIFF', orientation=6)})
    store = make_store(project_root, tmp_path / 'kb')
    handoff = k0_handoff(store, source, extensions=['.tif'])
    config = k1_profile(model_test_profile)
    result = run(store, config, handoff, source, EmbeddingServer())
    (row,) = result['images']
    assert (row['format'], row['width'], row['height'], row['decode_status']) == ('TIFF', 9, 7, 'ok')
    with store.writer() as writer:
        artifact = json.loads(writer.checkpoints(k1_embed.IMAGE_STAGE).load(
            image_key(embedder_settings(config), row['image_id'])))
    assert (artifact['width'], artifact['height']) == (9, 7)
    server = EmbeddingServer()
    again = run(store, config, handoff, source, server)
    assert again['report']['skipped_done'] == 1 and server.requests == [] and again['images'] == result['images']


def test_metadata_rule_keys_image_artifacts_only(model_test_profile, monkeypatch):
    """A change of the stored-metadata rule re-keys every image artifact (old TIFF rows are never reused)."""
    settings = embedder_settings(k1_profile(model_test_profile))
    image, text = image_key(settings, 'sha256:' + '0' * 64), k1_embed.text_key(settings, 'Kimono')
    monkeypatch.setattr(k1_embed, 'IMAGE_METADATA_RULE', k1_embed.IMAGE_METADATA_RULE + 1)
    assert image_key(settings, 'sha256:' + '0' * 64) != image and k1_embed.text_key(settings, 'Kimono') == text


def test_file_offset_beyond_the_c_range_is_undecodable(model_test_profile):
    """Retest 1 probe `test_file_offset_numeric_boundary`: a file-controlled offset of 2**64-1."""
    metadata, prepared = k1_embed._decode(bigtiff_with_strip_offset(2 ** 64 - 1), k1_settings(model_test_profile))
    assert prepared is None
    assert metadata == {'format': None, 'width': None, 'height': None, 'decode_status': 'undecodable'}


def test_file_offset_failure_is_checkpointed_and_isolated(project_root, tmp_path, model_test_profile):
    """Retest 1 probe `test_file_offset_checkpoint`: a durable no-vector record; the other image still embeds."""
    source = make_source(tmp_path / 'source', {'malformed.tif': bigtiff_with_strip_offset(2 ** 64 - 1),
                                               'good.tif': image_bytes(fmt='TIFF')})
    store = make_store(project_root, tmp_path / 'kb')
    handoff = k0_handoff(store, source, extensions=['.tif'])
    config = k1_profile(model_test_profile)
    result = run(store, config, handoff, source, EmbeddingServer())
    assert list(result['report']['decode_failed'].values()) == ['undecodable'] and result['report']['embedded'] == 1
    (bad,) = result['report']['decode_failed']
    with store.writer() as writer:
        assert writer.checkpoints(k1_embed.IMAGE_STAGE).state(image_key(embedder_settings(config), bad))['state'] == 'done'
    server = EmbeddingServer()
    assert run(store, config, handoff, source, server)['report']['skipped_done'] == 2 and server.requests == []


@pytest.mark.parametrize('data', [b'not an image', image_bytes(size=(9, 7))[:40]])
def test_undecodable_images_store_no_metadata(model_test_profile, data):
    metadata, prepared = k1_embed._decode(data, k1_settings(model_test_profile))
    assert prepared is None
    assert metadata == {'format': None, 'width': None, 'height': None, 'decode_status': 'undecodable'}


def test_decompression_bomb_stores_no_metadata(model_test_profile, monkeypatch):
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1)  # Pillow refuses at open, before any size is read
    metadata, prepared = k1_embed._decode(image_bytes(size=(3, 3)), k1_settings(model_test_profile))
    assert prepared is None
    assert metadata == {'format': None, 'width': None, 'height': None, 'decode_status': 'exceeds_max_decode_pixels'}


def test_size_is_recorded_before_exif_orientation(project_root, tmp_path, model_test_profile):
    source = make_source(tmp_path / 'source', {'turned.jpg': image_bytes(fmt='JPEG', orientation=6)})
    store = make_store(project_root, tmp_path / 'kb')
    result = run(store, k1_profile(model_test_profile), k0_handoff(store, source), source, EmbeddingServer())
    (row,) = result['images']
    assert (row['format'], row['width'], row['height']) == ('JPEG', 6, 4)


def test_batch_size_override(project_root, tmp_path, model_test_profile):
    files = {f'{index}.png': image_bytes(color=(index, 0, 0)) for index in range(5)}
    source = make_source(tmp_path / 'source', files)
    store = make_store(project_root, tmp_path / 'kb')
    server = EmbeddingServer()
    config = k1_profile(model_test_profile, batch_size=3)
    config['vlm']['backends']['embedder']['max_concurrency'] = 2  # a window of 6 holds both batches
    result = run(store, config, k0_handoff(store, source), source, server)
    assert sorted(server.input_counts()) == [2, 3] and result['report']['embedded'] == 5


def test_invalid_handoff_image_id_is_refused(kb, model_test_profile):
    store, source, handoff = kb
    broken = dict(handoff, images=[dict(handoff['images'][0], image_id='sha256:not-hex')])
    with pytest.raises(ValueError, match='invalid image_id'):
        run(store, k1_profile(model_test_profile), broken, source, EmbeddingServer())


def test_resume_false_recomputes_an_unreferenced_done_artifact(kb, model_test_profile, project_root):
    store, source, handoff = kb
    config = k1_profile(model_test_profile)
    run(store, config, handoff, source, EmbeddingServer())  # done rows of an earlier run, never published
    server = EmbeddingServer()
    result = run(make_store(project_root, store.root, resume=False), config, handoff, source, server)
    assert result['report']['embedded'] == 3 and result['report']['reused_referenced'] == []


def vllm_profile(model_test_profile, **embedding):
    config = k1_profile(model_test_profile, batch_size=1, dimension_mode='request_field', request={
        'shape': 'vllm_chat_messages', 'image_instruction': "Represent the user's input.",
        'extra_body': {'continue_final_message': True, 'add_special_tokens': True}}, **embedding)
    config['vlm']['backends']['embedder']['engine'] = {'name': 'vllm', 'version': 'test-version'}
    return config


def test_vllm_chat_messages_body(kb, model_test_profile):
    store, source, handoff = kb
    server = EmbeddingServer()
    run(store, vllm_profile(model_test_profile), handoff, source, server)
    assert server.input_counts() == [1, 1, 1]  # one conversation per request (A2)
    body = server.requests[0][1]
    assert body['continue_final_message'] is True and body['add_special_tokens'] is True
    assert body['dimensions'] == 4  # dimension_mode request_field
    system, user, assistant = body['messages']
    assert system == {'role': 'system', 'content': [{'type': 'text', 'text': "Represent the user's input."}]}
    assert user['content'][0]['type'] == 'image_url' and user['content'][1] == {'type': 'text', 'text': ''}
    assert assistant == {'role': 'assistant', 'content': [{'type': 'text', 'text': ''}]}


def test_instruction_override_reaches_llama_body(kb, model_test_profile):
    store, source, handoff = kb
    server = EmbeddingServer()
    run(store, k1_profile(model_test_profile, request={'image_instruction': 'describe'}), handoff, source, server)
    content = server.requests[0][1]['input'][0]['content']
    assert content[0] == {'type': 'text', 'text': 'describe'} and content[1]['type'] == 'image_url'


def test_client_prefix_l2_keeps_leading_values_normalised(kb, model_test_profile):
    store, source, handoff = kb
    server = EmbeddingServer(length=8)
    result = run(store, k1_profile(model_test_profile, dimension_mode='client_prefix_l2'), handoff, source, server)
    vector = result['image_embeddings'].column('vector').to_pylist()[0]
    assert len(vector) == 4 and math.isclose(math.fsum(value * value for value in vector), 1.0, rel_tol=1e-6)


def test_dimension_override_builds_the_wider_table(kb, model_test_profile):
    store, source, handoff = kb
    result = run(store, k1_profile(model_test_profile, dimension=256), handoff, source, EmbeddingServer(length=256))
    assert result['image_embeddings'].schema.field('vector').type.list_size == 256


def test_response_of_another_length_fails_the_items(kb, model_test_profile):
    store, source, handoff = kb
    result = run(store, k1_profile(model_test_profile), handoff, source, EmbeddingServer(length=5))
    assert set(result['report']['failed'].values()) == {'DIMENSION_MISMATCH'}


def one_image(project_root, tmp_path):
    source = make_source(tmp_path / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'kb')
    return store, source, k0_handoff(store, source)


def checkpoint_state(store, config, image_id):
    with store.writer() as writer:
        return writer.checkpoints(k1_embed.IMAGE_STAGE).state(image_key(embedder_settings(config), image_id))


@pytest.mark.parametrize(('values', 'mode'), [
    ([10 ** 400, 0, 0, 0], 'native'),  # JSON integer beyond the float range (review F1 probe)
    ([10 ** 400, 0, 0, 0], 'client_prefix_l2'),
    ([1e300, 0.0, 0.0, 0.0], 'native'),  # a float outside float32
])
def test_unrepresentable_response_fails_the_item_with_a_failed_checkpoint(project_root, tmp_path,
                                                                          model_test_profile, values, mode):
    store, source, handoff = one_image(project_root, tmp_path)
    config = k1_profile(model_test_profile, dimension_mode=mode)
    server = EmbeddingServer(payload={'data': [{'index': 0, 'embedding': values}]})
    result = run(store, config, handoff, source, server)
    (image_id,) = ids(handoff)
    assert result['report']['failed'] == {image_id: 'RESPONSE_INVALID'} and result['report']['embedded'] == 0
    state = checkpoint_state(store, config, image_id)
    assert state['state'] == 'failed' and state['error'] == 'RESPONSE_INVALID'
    assert result['image_embeddings'].num_rows == 0


def test_undecodable_json_response_fails_the_item_with_a_failed_checkpoint(project_root, tmp_path,
                                                                           model_test_profile):
    store, source, handoff = one_image(project_root, tmp_path)
    config = k1_profile(model_test_profile)
    server = EmbeddingServer()
    server.handler = lambda request: httpx.Response(200, content=b'[' * 100_000)  # nesting beyond the decoder
    result = run(store, config, handoff, source, server)
    (image_id,) = ids(handoff)
    assert result['report']['failed'] == {image_id: 'RESPONSE_INVALID'}
    assert checkpoint_state(store, config, image_id)['state'] == 'failed'


@pytest.mark.parametrize('values', [[1e154] * 4, [1.7e308] * 4, [-1.7e308, 1.7e308, 0.0, 0.0], [5e-324, 0, 0, 0]])
def test_client_prefix_l2_normalises_finite_extremes(project_root, tmp_path, model_test_profile, values):
    store, source, handoff = one_image(project_root, tmp_path)
    config = k1_profile(model_test_profile, dimension_mode='client_prefix_l2')
    server = EmbeddingServer(payload={'data': [{'index': 0, 'embedding': values + [1.0]}]})  # prefix of 4
    result = run(store, config, handoff, source, server)
    assert result['report']['embedded'] == 1
    (vector,) = result['image_embeddings'].column('vector').to_pylist()
    largest = max(abs(value) for value in values)
    scaled = [value / largest for value in values]
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    assert all(math.isclose(got, value / norm, abs_tol=1e-7) for got, value in zip(vector, scaled))


def test_backend_and_route_override_reach_the_request(kb, model_test_profile):
    store, source, handoff = kb
    config = k1_profile(model_test_profile, backend='emb_b')
    backend = deepcopy(config['vlm']['backends']['embedder'])
    backend['base_urls'] = ['http://127.0.0.1:9100']
    backend['endpoints'] = {'embeddings': 'alt/embed', 'health': 'health'}
    config['vlm']['backends']['emb_b'] = backend
    server = EmbeddingServer()
    run(store, config, handoff, source, server)
    assert {url for url, _ in server.requests} == {'http://127.0.0.1:9100/alt/embed'}


def publish_with_k1(store, source, handoff, result, settings):
    """Publish a snapshot whose manifest references every K1 artifact, as the kb sync caller will."""
    k2 = k0_handoff(store, source, 'k2')
    with store.writer() as writer:
        checkpoints = writer.checkpoints(k1_embed.IMAGE_STAGE)
        paths = [checkpoints.state(image_key(settings, row['image_id']))['output_key'] for row in handoff['images']]
        builder = writer.begin_snapshot()
        tables = dict(k2, images=result['images'], folder_images=handoff['folder_images'],
                      image_embeddings=result['image_embeddings'])
        for name in SNAPSHOT_TABLES:
            builder.write_table(name, tables.get(name, []), dim=4 if SPECS[name].vector else None)
        builder.reference_artifacts(paths)
        writer.publish(builder.finish(source_scan_summary={'fixture': 'K1 reuse test'}))


def test_resume_false_reuses_referenced_artifacts_only(kb, model_test_profile, project_root):
    store, source, handoff = kb
    config = k1_profile(model_test_profile)
    first = run(store, config, handoff, source, EmbeddingServer())
    publish_with_k1(store, source, handoff, first, embedder_settings(config))
    no_resume = make_store(project_root, store.root, resume=False)
    server = EmbeddingServer(length=4)
    server.vector = lambda item: [0.5, 0.5, 0.5, 0.5]  # different bytes: a recompute would be refused
    result = run(no_resume, config, handoff, source, server)
    assert server.requests == [] and len(result['report']['reused_referenced']) == 4
    assert result['image_embeddings'].equals(first['image_embeddings'])
    other = k1_profile(model_test_profile)
    other['vision']['quality'] = 70  # new keys, nothing referenced: recomputed even without resume
    server = EmbeddingServer()
    assert run(no_resume, other, handoff, source, server)['report']['embedded'] == 3 and server.requests


# ----------------------------------------------------------------------------- properties: parsing and keys

finite = st.floats(min_value=-1e3, max_value=1e3, allow_nan=False, allow_infinity=False)


@hsettings(max_examples=60, deadline=None)
@given(st.lists(st.lists(finite, min_size=3, max_size=3), min_size=1, max_size=6), st.randoms())
def test_parse_returns_vectors_in_index_order(vectors, random):
    data = [{'index': index, 'embedding': vector} for index, vector in enumerate(vectors)]
    random.shuffle(data)
    parsed = parse_embeddings({'data': data}, len(vectors), 3, 'native')
    assert len(parsed) == len(vectors)
    for got, sent in zip(parsed, vectors):
        assert all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-30) for a, b in zip(got, sent))


@hsettings(max_examples=60, deadline=None)
@given(st.lists(st.lists(finite, min_size=3, max_size=3), min_size=1, max_size=5),
       st.sampled_from(['drop', 'duplicate', 'nan', 'bool', 'string', 'short', 'index_type', 'no_data']))
def test_parse_refuses_malformed_responses(vectors, defect):
    data = [{'index': index, 'embedding': list(vector)} for index, vector in enumerate(vectors)]
    if defect == 'drop':
        data.pop()
    elif defect == 'duplicate':
        data[-1]['index'] = 0 if len(data) > 1 else 1
    elif defect == 'nan':
        data[0]['embedding'][0] = float('nan')
    elif defect == 'bool':
        data[0]['embedding'][0] = True
    elif defect == 'string':
        data[0]['embedding'][0] = '1.0'
    elif defect == 'short':
        data[0]['embedding'].pop()
    elif defect == 'index_type':
        data[0]['index'] = 0.0
    payload = {} if defect == 'no_data' else {'data': data}
    with pytest.raises(EmbeddingError):
        parse_embeddings(payload, len(vectors), 3, 'native')


def test_parse_refuses_values_outside_float32():
    with pytest.raises(EmbeddingError, match='RESPONSE_INVALID'):
        parse_embeddings({'data': [{'index': 0, 'embedding': [1e300, 0.0]}]}, 1, 2, 'native')


# test-only oracle constants from IEEE 754 binary32/binary64 (round to nearest, ties to even): the smallest
# magnitudes that round to infinity. They check the parser; it never uses them.
FLOAT32_OVERFLOW = 2 ** 128 - 2 ** 103
FLOAT64_OVERFLOW = 2 ** 1024 - 2 ** 970
EXTREMES = [FLOAT32_OVERFLOW, FLOAT32_OVERFLOW - 1, -FLOAT32_OVERFLOW, 3.4028234663852886e38, 1.7976931348623157e308,
            -1.7976931348623157e308, FLOAT64_OVERFLOW, FLOAT64_OVERFLOW - 1, -FLOAT64_OVERFLOW, 10 ** 400, 5e-324,
            -5e-324, 2.2250738585072014e-308, 1e-45, 1.4e-45, 0, 0.0, -0.0]
numbers = st.one_of(st.integers(), st.integers(min_value=-2 ** 1100, max_value=2 ** 1100),
                    st.floats(allow_nan=False, allow_infinity=False),
                    st.floats(min_value=-1e-300, max_value=1e-300, allow_nan=False), st.sampled_from(EXTREMES))


def as_float(value):
    try:
        return float(value)
    except OverflowError:
        return math.inf


@hsettings(max_examples=300, deadline=None)
@given(st.lists(numbers, min_size=1, max_size=6))
def test_parse_native_over_the_whole_numeric_range(values):
    payload = {'data': [{'index': 0, 'embedding': values}]}
    if not all(abs(as_float(value)) < FLOAT32_OVERFLOW for value in values):
        with pytest.raises(EmbeddingError, match='RESPONSE_INVALID'):
            parse_embeddings(payload, 1, len(values), 'native')
        return
    (vector,) = parse_embeddings(payload, 1, len(values), 'native')
    for got, sent in zip(vector, values):  # float32 rounding: half an ulp, or half the smallest subnormal
        assert math.isfinite(got) and abs(got - float(sent)) <= abs(float(sent)) * 2 ** -24 + 2 ** -150


@hsettings(max_examples=300, deadline=None)
@given(st.lists(numbers, min_size=3, max_size=6))
def test_parse_client_prefix_l2_over_the_whole_numeric_range(values):
    payload = {'data': [{'index': 0, 'embedding': values}]}
    prefix = [as_float(value) for value in values[:3]]
    if any(as_float(value) == math.inf for value in values) or not any(prefix):
        with pytest.raises(EmbeddingError, match='RESPONSE_INVALID'):
            parse_embeddings(payload, 1, 3, 'client_prefix_l2')
        return
    (vector,) = parse_embeddings(payload, 1, 3, 'client_prefix_l2')
    largest = max(abs(value) for value in prefix)
    scaled = [value / largest for value in prefix]
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    assert all(math.isclose(got, value / norm, abs_tol=1e-6) for got, value in zip(vector, scaled))
    assert math.isclose(math.fsum(value * value for value in vector), 1.0, rel_tol=1e-5)


KEYED = [
    ('vision.max_image_side', 512), ('vision.format', 'png'), ('vision.quality', 70),
    ('vision.alpha_policy', {'mode': 'refuse'}), ('vlm.backends.embedder.image.max_decode_pixels', 999),
    ('embedding.request.image_instruction', 'other'), ('embedding.dimension', 8),
    ('embedding.dimension_mode', 'client_prefix_l2'), ('vlm.backends.embedder.model.id', 'other-model'),
    ('vlm.backends.embedder.engine.version', 'other-version'),
]
UNKEYED = [('embedding.batch_size', 1), ('vlm.backends.embedder.max_concurrency', 4),
           ('vlm.backends.embedder.http.read_timeout_s', 9), ('embedding.query_text_limit', 7)]


def put(config, key, value):
    parts = key.split('.')
    for part in parts[:-1]:
        config = config[part]
    config[parts[-1]] = value


@hsettings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(min_size=1, max_size=64))  # the fixture is only read (deep-copied), so sharing it is safe
def test_key_is_a_stable_function_of_content_and_settings(model_test_profile, content):
    import hashlib
    image_id = 'sha256:' + hashlib.sha256(content).hexdigest()
    config = k1_profile(model_test_profile)
    base = image_key(embedder_settings(config), image_id)
    assert base == image_key(embedder_settings(deepcopy(config)), image_id)
    for key, value in KEYED:
        changed = deepcopy(config)
        put(changed, key, value)
        assert image_key(embedder_settings(changed), image_id) != base, key
    for key, value in UNKEYED:
        changed = deepcopy(config)
        put(changed, key, value)
        assert image_key(embedder_settings(changed), image_id) == base, key


def test_revision_excludes_instruction_and_image_settings(model_test_profile):
    config = k1_profile(model_test_profile)
    base = embedder_settings(config).revision
    changed = k1_profile(model_test_profile, request={'image_instruction': 'other', 'text_template': 'q: {text}'})
    changed['vision']['max_image_side'] = 10
    assert embedder_settings(changed).revision == base
    assert base.startswith('emb-')


# Fix round 3 (R2F1): one decode boundary. Every format the installed Pillow can write, cut short and damaged; each
# input must end as a per-image outcome, never as an exception that stops the stage.
DECODE_STATUSES = {'ok', 'undecodable', 'exceeds_max_decode_pixels', 'alpha_refused', 'encode_failed'}
EXIF_FORMATS = ('PNG', 'JPEG', 'WEBP', 'TIFF', 'AVIF', 'MPO')  # formats whose encoder takes an `exif` block
MALFORMED_EXIF = [b'Exif\0\0', b'Exif\0\0II*\0' + b'\xff' * 16, b'Exif\0\0MM\0*' + b'\xff' * 32,
                  b'Exif\0\0II*\0\x08\0\0\0\xff\xff', b'II*\0\x08\0\0\0\x01\0\x12\x01\x03\0\xff\xff\xff\xff\0\0\0\0',
                  b'MM\0*\0\0\0\x08\0\x01\x87\x69\0\x04\0\0\0\x01\x7f\xff\xff\xff']
FLATTEN = {'mode': 'flatten', 'background': '#FFFFFF'}  # flatten also runs the RGBA conversion inside the boundary


@functools.lru_cache(maxsize=1)
def writable_seeds():
    """A 9x7 image per format the installed Pillow writes and per mode it accepts, EXIF-tagged where it can be."""
    Image.init()
    seeds = []
    for fmt in sorted(Image.SAVE):
        for mode in ('RGB', 'RGBA', 'L', '1', 'P'):
            buffer = BytesIO()
            kwargs = {}
            if fmt in EXIF_FORMATS:
                kwargs['exif'] = Image.Exif()
                kwargs['exif'][274] = 6
            try:
                Image.new(mode, (9, 7)).save(buffer, fmt, **kwargs)
            except Exception:  # noqa: BLE001 - test-only seed selection: this encoder does not write this mode
                continue
            if buffer.tell():
                seeds.append((fmt, mode, buffer.getvalue()))
    return tuple(seeds)


def flatten_settings(model_test_profile):
    settings = k1_settings(model_test_profile)
    return replace(settings, vision=dict(settings.vision, alpha_policy=FLATTEN))


def decode_outcome(data, settings):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # Pillow warns about truncated EXIF; the outcome is what matters here
        metadata, prepared = k1_embed._decode(data, settings)
    assert metadata['decode_status'] in DECODE_STATUSES
    assert (prepared is None) == (metadata['decode_status'] != 'ok')
    if metadata['decode_status'] == 'undecodable':
        assert metadata == {'format': None, 'width': None, 'height': None, 'decode_status': 'undecodable'}
    return metadata['decode_status']


def test_the_sweep_covers_the_formats_the_review_names():
    formats = {fmt for fmt, _, _ in writable_seeds()}
    assert {'QOI', 'AVIF', 'DDS', 'BLP', 'PNG', 'JPEG', 'WEBP', 'TIFF', 'GIF', 'BMP'} <= formats


@pytest.mark.parametrize('length', [13, 14, 18])
def test_truncated_qoi_is_undecodable(model_test_profile, length):
    """Retest 2 probe (R2F1): Pillow's QOI decoder indexes past the end of a short file (QoiImagePlugin.py:63)."""
    data = image_bytes(size=(9, 7), fmt='QOI')[:length]
    assert k1_embed._decode(data, k1_settings(model_test_profile))[0]['decode_status'] == 'undecodable'


def test_truncated_qoi_is_checkpointed_and_isolated(project_root, tmp_path, model_test_profile):
    """Retest 2 probe `test_qoi_failure_checkpoint`, with a good image beside it and a resume."""
    source = make_source(tmp_path / 'source', {'truncated.qoi': image_bytes(size=(9, 7), fmt='QOI')[:14],
                                               'good.qoi': image_bytes(size=(9, 7), fmt='QOI')})
    store = make_store(project_root, tmp_path / 'kb')
    handoff = k0_handoff(store, source, extensions=['.qoi'])
    config = k1_profile(model_test_profile)
    result = run(store, config, handoff, source, EmbeddingServer())
    assert list(result['report']['decode_failed'].values()) == ['undecodable'] and result['report']['embedded'] == 1
    (bad,) = result['report']['decode_failed']
    with store.writer() as writer:
        checkpoints = writer.checkpoints(k1_embed.IMAGE_STAGE)
        key = image_key(embedder_settings(config), bad)
        assert checkpoints.state(key)['state'] == 'done' and json.loads(checkpoints.load(key))['vector'] is None
    server = EmbeddingServer()
    assert run(store, config, handoff, source, server)['report']['skipped_done'] == 2 and server.requests == []


def test_every_writable_format_cut_or_damaged_is_a_per_image_outcome(model_test_profile):
    """Deterministic sweep: every prefix up to 64 bytes, 16 seeded prefixes and 24 seeded 1-4 byte mutations."""
    settings = flatten_settings(model_test_profile)
    rng = random.Random(20261115)
    escaped = []
    for fmt, mode, data in writable_seeds():
        cases = [data] + [data[:length] for length in range(min(len(data), 64))]
        cases += [data[:rng.randrange(len(data))] for _ in range(16)]
        for _ in range(24):
            damaged = bytearray(data)
            for _ in range(rng.randint(1, 4)):
                damaged[rng.randrange(len(damaged))] = rng.choice([0, 0xFF, rng.randrange(256)])
            cases.append(bytes(damaged))
        for case in cases:
            try:
                decode_outcome(case, settings)
            except Exception as error:  # noqa: BLE001 - collect every escape so the failure lists them all
                escaped.append((fmt, mode, len(case), type(error).__name__))
    assert escaped == []


@pytest.mark.parametrize('fmt', EXIF_FORMATS)
def test_malformed_exif_is_a_per_image_outcome(model_test_profile, fmt):
    if fmt not in {seed[0] for seed in writable_seeds()}:
        pytest.skip(f'the installed Pillow does not write {fmt}')
    settings = flatten_settings(model_test_profile)
    for block in MALFORMED_EXIF:
        buffer = BytesIO()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')  # the encoder itself warns that the block is corrupt
                Image.new('RGB', (9, 7)).save(buffer, fmt, exif=block)
        except Exception:  # noqa: BLE001 - test-only: this encoder refuses the block, so there is no file to read
            continue
        decode_outcome(buffer.getvalue(), settings)


@hsettings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.data())
def test_decode_property_any_cut_and_damage_of_any_writable_format(model_test_profile, data):
    fmt, mode, raw = data.draw(st.sampled_from(writable_seeds()), label='seed')
    cut = data.draw(st.integers(0, len(raw)), label='cut')
    edits = data.draw(st.lists(st.tuples(st.integers(0, len(raw) - 1), st.integers(0, 255)), max_size=4))
    damaged = bytearray(raw)
    for position, value in edits:
        damaged[position] = value
    decode_outcome(bytes(damaged[:cut]), flatten_settings(model_test_profile))


def test_the_boundary_holds_no_setting_or_encoding_fault(model_test_profile, monkeypatch):
    """Only the file's bytes are inside: a setting that bypassed preflight and an encoder fault still fail."""
    settings, data = k1_settings(model_test_profile), image_bytes()
    with pytest.raises(OverflowError):  # retest 2 `test_overflow_catch_is_file_only`: Pillow's encoder, C int
        k1_embed._decode(data, replace(settings, vision=dict(settings.vision, quality=2 ** 64)))

    def failing_save(*args, **kwargs):  # labelled double: an encoder defect that is not a file problem
        raise ValueError('labelled double: encoder defect')

    monkeypatch.setattr(Image.Image, 'save', failing_save)  # after the fixture image is written
    with pytest.raises(ValueError, match='labelled double'):
        k1_embed._decode(data, settings)


@pytest.mark.parametrize('error', [MemoryError, KeyboardInterrupt])
def test_memory_exhaustion_and_interrupts_pass_through_the_boundary(model_test_profile, monkeypatch, error):
    def failing_load(self):  # labelled double: the process, not the file, failed while decoding
        raise error('labelled double')

    data = image_bytes()
    monkeypatch.setattr(ImageFile.ImageFile, 'load', failing_load)  # the load every file-backed opener inherits
    with pytest.raises(error, match='labelled double'):
        k1_embed._decode(data, k1_settings(model_test_profile))
