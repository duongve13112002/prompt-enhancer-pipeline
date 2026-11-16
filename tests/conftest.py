"""Give temporary test inputs an explicit, self-contained project root."""

from copy import deepcopy
from functools import partial
from pathlib import Path
import shutil

import pytest
import yaml

from prompt_enhancer import load_config, load_profile


@pytest.fixture
def model_test_profile():
    """Example profile with synthetic model settings for command-loader tests."""
    profile = yaml.safe_load((Path(__file__).resolve().parents[1] / 'configs/enhancer.example.yaml').read_bytes())
    backend = profile['vlm']['backends']['main']
    backend.update(engine={'name': 'llama_cpp', 'version': 'test-version'},
                   base_urls=['http://127.0.0.1:8080'], endpoints={'chat': 'chat', 'health': 'health'},
                   max_concurrency=1, http=dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1,
                   pool_timeout_s=1, max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[]),
                   probe={'max_requests': 1, 'timeout_s': 1}, image={'max_decode_pixels': 1000})
    backend['model'].update(id='synthetic', revision='test')
    for name in ('observer', 'info_assessor', 'folder_namer', 'card_builder', 'alias_generator'):
        role = profile['vlm']['roles'][name]
        role.update(max_tokens=128,
                    sampling=dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0,
                                  frequency_penalty=0.0, presence_penalty=0.0,
                                  repetition_penalty=1.0, seed='none', stop='none'),
                    reasoning=dict(mode='off', effort='none', budget_tokens='none',
                                   template_kwargs_key='enable_thinking'))
        if isinstance(role['image'], dict):
            role['image'].update(source='source_image', quality=80)
    profile['vlm']['roles']['folder_namer']['image']['source'] = 'knowledge_reference'
    profile['vlm']['roles']['observer']['image']['source'] = 'knowledge_reference'
    profile['observe']['near_duplicate_cosine_distance'] = 0.01  # labelled test-only grouping threshold
    profile['vision']['quality'] = 80
    # T-008: kb sync reads the embedder; a synthetic local embedding backend with every K1 field resolved.
    embedder = deepcopy(backend)
    embedder['endpoints'] = {'embeddings': 'v1/embeddings', 'health': 'health'}
    embedder['model'] = dict(backend['model'], id='synthetic-embedder', revision='test')
    profile['vlm']['backends']['embedder'] = embedder
    profile['embedding'].update(chosen='embeddinggemma-2', revision='test', backend='embedder', dimension=4,
                                dimension_mode='native', batch_size=2,
                                request={'shape': 'openai_input_content', 'image_instruction': 'none',
                                         'extra_body': {}, 'text_template': '{text}'})
    return profile


@pytest.fixture(scope='session')
def project_root(tmp_path_factory):
    root = tmp_path_factory.getbasetemp()
    schemas = Path(__file__).resolve().parents[1] / 'contracts' / 'schemas'
    shutil.copytree(schemas, root / 'contracts' / 'schemas')
    return root


@pytest.fixture
def k2_env(project_root, tmp_path, model_test_profile):
    from k2_fixtures import environment

    return environment(project_root, tmp_path, model_test_profile)


@pytest.fixture(autouse=True)
def bind_temporary_project(request, project_root, monkeypatch):
    if request.module.__name__ in {'test_loader', 'test_field_contracts', 'test_fix01'}:
        monkeypatch.setattr(request.module, 'load_config', partial(load_config, project_root=project_root))
        monkeypatch.setattr(request.module, 'load_profile', partial(load_profile, project_root=project_root))
