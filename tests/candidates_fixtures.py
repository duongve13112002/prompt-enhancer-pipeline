"""Labelled synthetic S1 ownership and encoder doubles over real tiny K5 snapshots."""

from types import SimpleNamespace

import pytest

from intake_fixtures import configuration
from k5_fixtures import published
from prompt_enhancer.candidates import query_embedder_settings
from prompt_enhancer.intake import IntakeResult
from storage_fixtures import dataset


class QueryEncoderDouble:
    """Labelled double: no HTTP/model; records raw query strings and returns explicit test vectors."""

    def __init__(self, config):
        self.settings = query_embedder_settings(config)
        self.calls = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def candidate_env(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    config['rag']['enabled'] = True
    config['embedding']['query_text_limit'] = 1000  # labelled test-only
    config['candidates']['max_prompt_windows'] = 100000
    rows = dataset(revision=query_embedder_settings(config).revision)
    env = published(project_root, tmp_path, rows=rows, settings=config)
    config['storage']['kb_store'] = str(env.store.root)
    snapshot = env.store.pin()
    intake = IntakeResult({'outcome': 'ready', 'prompt': '', 'effective_config': config,
                           'task': 'generate', 'config_hash': 'test-only-hash',
                           'retrieval': {'state': 'pinned', 'snapshot_id': snapshot.snapshot_id}},
                          knowledge=SimpleNamespace(snapshot=snapshot, close=snapshot.release))
    try:
        yield SimpleNamespace(config=config, intake=intake, store=env.store, rows=rows)
    finally:
        intake.close()


def source_only(config, name):
    for key, source in config['candidates']['sources'].items():
        source['enabled'] = key == name
