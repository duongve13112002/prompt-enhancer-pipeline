"""Give temporary test inputs an explicit, self-contained project root."""

from copy import deepcopy
from functools import partial
from pathlib import Path
import shutil
import sys

import pytest
import yaml
from hypothesis import settings

# CI shard selection (decision 168): inert unless PE_SHARD and PE_SHARDS are set; see tools/agent-workflow/ci_shard.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'agent-workflow'))
from ci_shard import pytest_collection_modifyitems, pytest_report_collectionfinish  # noqa: E402,F401 - pytest hooks

# Shared machines cannot provide a stable per-example wall-clock deadline.
settings.register_profile('pipeline_cpu', deadline=None)
settings.load_profile('pipeline_cpu')

from prompt_enhancer import load_config, load_profile


@pytest.fixture
def model_test_profile():
    """Example profile with synthetic model settings for command-loader tests."""
    profile = yaml.safe_load((Path(__file__).resolve().parents[1] / 'configs/enhancer.example.yaml').read_bytes())
    profile['sync']['retained_images'] = {'max_image_bytes': 1048576, 'max_total_bytes': 10485760}  # test-only
    backend = profile['vlm']['backends']['main']
    backend.update(engine={'name': 'llama_cpp', 'version': 'test-version'},
                   base_urls=['http://127.0.0.1:8080'], endpoints={'chat': 'chat', 'health': 'health'},
                   max_concurrency=1, http=dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1,
                   pool_timeout_s=1, max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[]),
                   probe={'max_requests': 1, 'timeout_s': 1, 'image_attempts': 1}, image={'max_decode_pixels': 1000, 'max_side': 'none', 'max_images_per_call': 1},
                   addendum_layout='separate_system_message')  # test-only layout (T017-FIX)
    backend.pop('addendum_separator', None)
    profile['edit']['max_source_images'] = 1  # test-only (decision 179): the multi-source cap of the synthetic profile
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
    profile['cards']['low_diversity_max_group_ratio'] = 0.5  # labelled test-only independence threshold
    profile['aliases']['languages'] = ['test-language']  # labelled test-only, not a dev language decision
    profile['vision']['quality'] = 80
    # T-008: kb sync reads the embedder; a synthetic local embedding backend with every K1 field resolved.
    embedder = deepcopy(backend)
    embedder['endpoints'] = {'embeddings': 'v1/embeddings', 'health': 'health'}
    embedder.pop('addendum_layout', None)  # a chat-less backend refuses the addendum keys (T017-FIX)
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
    # The production capability-probe bundle (KBS-F1): production entry points load it from the project root.
    shutil.copytree(schemas.parent / 'probes', root / 'contracts' / 'probes')
    # MS-2: a request command reads the multi_source prompt files when it loads the configuration, so every test that
    # loads one from this root finds the pipeline-owned prompts (the same copy profile_fixtures.install_prompts makes).
    (root / 'prompts').mkdir(exist_ok=True)
    for prompt in (schemas.parents[1] / 'prompts').glob('*.txt'):
        shutil.copyfile(prompt, root / 'prompts' / prompt.name)
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
