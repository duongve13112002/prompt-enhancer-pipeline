"""K1 embedding-space identity of the embedding server's output settings (T-018 fix round 1, review R2).

A checkpoint is reused only when everything that shapes its vectors is unchanged (R-017). The server's family, dtype,
attention kernel, token and pixel limits and default instruction shape the vectors, so they enter the identity of the
`embed_server` engine; other engines keep the identity they had.
"""

import asyncio
from copy import deepcopy
from dataclasses import fields

import pytest

from prompt_enhancer import ConfigurationError
from prompt_enhancer.embed_server.families import output_fingerprint
from prompt_enhancer.embed_server.settings import ServerSettings
from prompt_enhancer.k1_embed import (_digest, embed_images, embedder_settings, image_key, image_revisions, text_key,
                                      text_revisions)
from embed_server_fixtures import server_profile
from k1_fixtures import EmbeddingServer, image_bytes, k0_handoff, make_source
from storage_fixtures import make_store

SERVER = 'vlm.backends.embedder.server'
IMAGE_ID = 'sha256:' + 'a' * 64

# Every ServerSettings field is classified here: a new setting fails `test_every_setting_is_classified` until its
# author decides whether it changes the vectors. The reasons for the excluded ones are in `output_fingerprint`.
FINGERPRINTED = {'family', 'dtype', 'attn_implementation', 'max_length', 'min_pixels', 'max_pixels', 'max_soft_tokens',
                 'default_instruction'}
ALREADY_IN_K1 = {'model_id', 'revision', 'dimension', 'dimension_mode', 'batch_size', 'max_decode_pixels',
                 'backend_name'}  # K1's identity or schema revision already holds these
EXCLUDED = {'forward_batch', 'idle_exit_s', 'max_request_bytes', 'max_image_bytes', 'max_pending_requests',
            'max_connections', 'max_discard_bytes', 'read_timeout_s', 'host', 'port', 'embeddings_path', 'health_path', 'gpu', 'vram_budget_gb', 'project_root'}


def set_in(profile, dotted, value):
    node = profile
    parts = dotted.split('.')
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return profile


def test_every_setting_is_classified():
    names = {field.name for field in fields(ServerSettings)}
    assert names == FINGERPRINTED | ALREADY_IN_K1 | EXCLUDED
    assert not FINGERPRINTED & EXCLUDED and not ALREADY_IN_K1 & (FINGERPRINTED | EXCLUDED)


@pytest.fixture
def qwen(model_test_profile):
    return server_profile(model_test_profile, family='qwen3_vl_embedding')


@pytest.fixture
def gemma(model_test_profile):
    return server_profile(model_test_profile)


FINGERPRINT_CHANGES = [
    (SERVER + '.dtype', 'float32'), (SERVER + '.attn_implementation', 'eager'), (SERVER + '.max_length', 7),
    (SERVER + '.image.min_pixels', 100), (SERVER + '.image.max_pixels', 5000),
    (SERVER + '.default_instruction', 'A different instruction.'),
]


@pytest.mark.parametrize('key, value', FINGERPRINT_CHANGES, ids=[key.rpartition('.')[2] for key, _ in FINGERPRINT_CHANGES])
def test_an_output_setting_changes_the_image_and_text_identity_together(qwen, key, value):
    before = embedder_settings(qwen)
    after = embedder_settings(set_in(deepcopy(qwen), key, value))
    assert before.revision != after.revision
    assert image_revisions(before)['model'] == before.revision and text_revisions(before)['model'] == before.revision
    assert image_revisions(after)['model'] == after.revision and text_revisions(after)['model'] == after.revision
    assert image_key(before, IMAGE_ID) != image_key(after, IMAGE_ID)
    assert text_key(before, 'concept') != text_key(after, 'concept')  # one space for both kinds of row


def test_the_gemma_image_budget_changes_the_identity(gemma):
    before = embedder_settings(gemma)
    after = embedder_settings(set_in(deepcopy(gemma), SERVER + '.image.max_soft_tokens', 70))
    assert before.revision != after.revision


def test_the_family_changes_the_identity(gemma, qwen):
    assert embedder_settings(gemma).revision != embedder_settings(qwen).revision


EXCLUDED_CHANGES = [
    (SERVER + '.forward_batch', 1), (SERVER + '.idle_exit_s', 9), (SERVER + '.max_request_bytes', 123456),
    (SERVER + '.max_image_bytes', 4000), (SERVER + '.max_pending_requests', 2), (SERVER + '.read_timeout_s', 9),
    (SERVER + '.max_connections', 99), (SERVER + '.max_discard_bytes', 9999999),
    ('vlm.backends.embedder.base_urls', ['http://127.0.0.1:9999']),
    ('vlm.backends.embedder.endpoints', {'embeddings': 'v1/embeddings', 'health': 'ping'}),
    ('runtime.gpus', [1]), ('runtime.vram_budget_gb', 3),
]


@pytest.mark.parametrize('key, value', EXCLUDED_CHANGES, ids=[key.rpartition('.')[2] for key, _ in EXCLUDED_CHANGES])
def test_transport_placement_and_batching_values_keep_the_identity(qwen, key, value):
    before = embedder_settings(qwen)
    after = embedder_settings(set_in(deepcopy(qwen), key, value))
    assert before.revision == after.revision and image_key(before, IMAGE_ID) == image_key(after, IMAGE_ID)
    assert text_key(before, 'concept') == text_key(after, 'concept')


def test_the_fingerprint_names_only_output_settings(qwen):
    fingerprint = output_fingerprint(qwen['vlm']['backends']['embedder']['server'])
    assert set(fingerprint) == {'family', 'model_type', 'pooling', 'dtype', 'attn_implementation', 'max_length',
                                'image', 'default_instruction'}
    assert fingerprint['pooling'] == 'last_token' and fingerprint['model_type'] == 'qwen3_vl'


def test_other_engines_keep_exactly_their_earlier_identity(model_test_profile):
    """The revision of a non-`embed_server` engine is the digest of the identity K1 built before the server existed."""
    profile = deepcopy(model_test_profile)
    assert profile['vlm']['backends']['embedder']['engine']['name'] == 'llama_cpp'
    settings = embedder_settings(profile)
    embedding, backend = profile['embedding'], profile['vlm']['backends']['embedder']
    earlier = {'engine': {'name': 'llama_cpp', 'version': 'test-version'},
               'model': {'id': backend['model']['id'], 'revision': backend['model']['revision']},
               'chosen': embedding['chosen'], 'revision': embedding['revision'],
               'shape': embedding['request']['shape'], 'dimension': embedding['dimension'],
               'dimension_mode': embedding['dimension_mode']}
    assert settings.revision == 'emb-' + _digest(earlier)
    assert 'server' not in earlier


def test_an_embed_server_identity_is_the_earlier_one_plus_the_fingerprint(gemma):
    settings = embedder_settings(gemma)
    embedding, backend = gemma['embedding'], gemma['vlm']['backends']['embedder']
    identity = {'engine': {'name': 'embed_server', 'version': 'test-version'},
                'model': {'id': backend['model']['id'], 'revision': backend['model']['revision']},
                'chosen': embedding['chosen'], 'revision': embedding['revision'],
                'shape': embedding['request']['shape'], 'dimension': embedding['dimension'],
                'dimension_mode': embedding['dimension_mode'], 'server': output_fingerprint(backend['server'])}
    assert settings.revision == 'emb-' + _digest(identity)


def test_a_sentinel_in_an_identity_setting_is_refused_by_k1(qwen):
    for key in ('dtype', 'attn_implementation', 'max_length', 'default_instruction', 'family'):
        broken = set_in(deepcopy(qwen), f'{SERVER}.{key}', 'deploy_required')
        with pytest.raises(ConfigurationError, match=key):
            embedder_settings(broken)
    broken = deepcopy(qwen)
    del broken['vlm']['backends']['embedder']['server']
    with pytest.raises(ConfigurationError, match='server'):
        embedder_settings(broken)


def test_the_k1_image_format_must_be_one_the_server_decodes(gemma, model_test_profile):
    embedder_settings(gemma)  # png is decoded by the server
    for name in ('jpeg', 'webp', 'gif', 'bmp', 'tiff'):
        embedder_settings(set_in(deepcopy(gemma), 'vision.format', name))
    with pytest.raises(ConfigurationError, match='decodes only'):
        embedder_settings(set_in(deepcopy(gemma), 'vision.format', 'ppm'))
    embedder_settings(set_in(deepcopy(model_test_profile), 'vision.format', 'ppm'))  # another engine: not restricted


# --- an override reaches the checkpoint decision: a fresh HTTP call ----------------------------------------------

@pytest.fixture
def kb(project_root, tmp_path):
    source = make_source(tmp_path / 'source', {'a.png': image_bytes(color=(10, 20, 30)),
                                               'b.png': image_bytes(size=(5, 9), color=(40, 50, 60))})
    store = make_store(project_root, tmp_path / 'kb')
    return store, source, k0_handoff(store, source)


def run(store, config, handoff, source, server):
    return asyncio.run(embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                    transport=server.transport()))


def test_an_output_override_makes_k1_call_the_server_again_and_a_transport_one_does_not(kb, qwen):
    store, source, handoff = kb
    first = run(store, qwen, handoff, source, EmbeddingServer())
    assert first['report']['embedded'] == 2

    same = EmbeddingServer()
    assert run(store, qwen, handoff, source, same)['report']['embedded'] == 0 and same.requests == []

    transport_only = set_in(deepcopy(qwen), SERVER + '.read_timeout_s', 9)
    quiet = EmbeddingServer()
    assert run(store, transport_only, handoff, source, quiet)['report']['embedded'] == 0 and quiet.requests == []

    changed = set_in(deepcopy(qwen), SERVER + '.default_instruction', 'Another retrieval instruction.')
    fresh = EmbeddingServer()
    again = run(store, changed, handoff, source, fresh)
    assert again['report']['embedded'] == 2 and fresh.requests  # the stored vectors were not reused
    assert again['embedder_revision'] != first['embedder_revision']
