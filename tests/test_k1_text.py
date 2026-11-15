"""K1 part 2: the concept-text interface with synthetic ConceptText fixtures and a labelled HTTP double (T-008)."""

import asyncio

import pytest

from prompt_enhancer.k1_embed import (TEXT_STAGE, ConceptText, embed_concept_texts, embed_images, embedder_settings,
                                      text_hash, text_key)
from prompt_enhancer.storage.identity import concept_id
from prompt_enhancer.storage.tables import check_embedding_space
from k1_fixtures import EmbeddingServer, image_bytes, k0_handoff, k1_profile, make_source
from storage_fixtures import make_store

KIMONO, DONUT = concept_id('Kimono'), concept_id('Donut')
ITEMS = [ConceptText(KIMONO, 'name', 'Kimono'), ConceptText(KIMONO, 'alias', 'yukata'),
         ConceptText(KIMONO, 'alias', 'áo kimono'), ConceptText(DONUT, 'alias', 'yukata'),
         ConceptText(DONUT, 'description', 'a ring-shaped pastry')]


def run(store, config, items, server):
    return asyncio.run(embed_concept_texts(store, config, items, transport=server.transport()))


@pytest.fixture
def store(project_root, tmp_path):
    return make_store(project_root, tmp_path / 'kb')


def test_aliases_of_one_concept_keep_separate_rows_and_shared_text_is_embedded_once(store, model_test_profile):
    server = EmbeddingServer()
    result = run(store, k1_profile(model_test_profile), ITEMS, server)
    rows = result['concept_text_embeddings'].to_pylist()
    assert len(rows) == 5 and sum(server.input_counts()) == 4  # 'yukata' is one text for two concepts
    kimono_aliases = [row for row in rows if row['concept_id'] == KIMONO and row['text_kind'] == 'alias']
    assert len({row['text_hash'] for row in kimono_aliases}) == 2
    sent = sorted(text for _, body in server.requests for text in body['input'])
    assert 'áo kimono' in sent  # multilingual text verbatim


def test_rerun_makes_no_call(store, model_test_profile):
    config = k1_profile(model_test_profile)
    first = run(store, config, ITEMS, EmbeddingServer())
    server = EmbeddingServer()
    second = run(store, config, ITEMS, server)
    assert server.requests == [] and second['report']['skipped_done'] == 4
    assert second['concept_text_embeddings'].equals(first['concept_text_embeddings'])


def test_text_template_override_reaches_the_request_and_the_key(store, model_test_profile):
    run(store, k1_profile(model_test_profile), ITEMS[:1], EmbeddingServer())
    server = EmbeddingServer()
    result = run(store, k1_profile(model_test_profile, request={'text_template': 'query: {text}'}), ITEMS[:1], server)
    assert server.requests[0][1]['input'] == ['query: Kimono'] and result['report']['embedded'] == 1


def test_vllm_text_body_has_no_image_instruction(store, model_test_profile):
    config = k1_profile(model_test_profile, batch_size=1, request={
        'shape': 'vllm_chat_messages', 'image_instruction': 'image only', 'extra_body': {}})
    config['vlm']['backends']['embedder']['engine'] = {'name': 'vllm', 'version': 'test-version'}
    server = EmbeddingServer()
    run(store, config, ITEMS[:1], server)
    assert server.requests[0][1]['messages'] == [{'role': 'user', 'content': [{'type': 'text', 'text': 'Kimono'}]}]


def test_server_refusal_fails_the_item_without_truncation(store, model_test_profile):
    result = run(store, k1_profile(model_test_profile), ITEMS[:1], EmbeddingServer(status=400))
    assert list(result['report']['failed'].values()) == ['HTTP_ERROR']
    assert result['concept_text_embeddings'].num_rows == 0


@pytest.mark.parametrize('mode', ['native', 'client_prefix_l2'])
def test_unrepresentable_response_fails_the_text_with_a_failed_checkpoint(store, model_test_profile, mode):
    config = k1_profile(model_test_profile, dimension_mode=mode)
    server = EmbeddingServer(payload={'data': [{'index': 0, 'embedding': [10 ** 400, 0, 0, 0]}]})
    result = run(store, config, ITEMS[:1], server)
    (failed,) = result['report']['failed'].items()
    assert failed == (text_hash('Kimono'), 'RESPONSE_INVALID') and result['concept_text_embeddings'].num_rows == 0
    with store.writer() as writer:
        state = writer.checkpoints(TEXT_STAGE).state(text_key(embedder_settings(config), 'Kimono'))
    assert state['state'] == 'failed' and state['error'] == 'RESPONSE_INVALID'


@pytest.mark.parametrize('item', [
    ConceptText('kimono', 'name', 'Kimono'), ConceptText(KIMONO, 'title', 'Kimono'),
    ConceptText(KIMONO, 'name', ''), ('not', 'a', 'ConceptText')])
def test_invalid_items_are_refused(store, model_test_profile, item):
    with pytest.raises(ValueError, match='invalid concept text'):
        run(store, k1_profile(model_test_profile), [item], EmbeddingServer())


def test_text_and_image_rows_share_one_embedding_space(project_root, tmp_path, model_test_profile):
    source = make_source(tmp_path / 'source', {'a.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'kb')
    config = k1_profile(model_test_profile)
    images = asyncio.run(embed_images(store, config, k0_handoff(store, source), knowledge_root=source.resolve(),
                                      transport=EmbeddingServer().transport()))
    texts = run(store, config, ITEMS, EmbeddingServer())
    assert images['embedder_revision'] == texts['embedder_revision']
    check_embedding_space({'image_embeddings': images['image_embeddings'],
                           'concept_text_embeddings': texts['concept_text_embeddings']})
