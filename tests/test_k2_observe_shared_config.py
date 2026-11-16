"""Shared configuration read-set coverage at the observer boundary; synthetic transports only."""

from copy import deepcopy

import pytest

from k2_fixtures import folder
from k2_observe_fixtures import ObserverServer, invoke_observer
from test_k2_config import set_key
from test_k2_observe_config import refused_stage
from prompt_enhancer.config import ConfigurationError


B = 'vlm.backends.main.'
OVERRIDES = {
    B + 'engine.name': 'vllm', B + 'engine.version': 'other-version',
    B + 'model.id': 'other-model', B + 'model.revision': 'other-revision',
    B + 'model.source': 'test-repository', B + 'model.file': 'test-model.bin',
    B + 'model.mmproj_file': 'test-projector.bin', B + 'base_urls': ['http://127.0.0.1:8765'],
    B + 'endpoints.chat': 'other-chat', B + 'endpoints.health': 'other-health',
    B + 'max_concurrency': 2,
    B + 'http.connect_timeout_s': 2, B + 'http.read_timeout_s': 3,
    B + 'http.write_timeout_s': 4, B + 'http.pool_timeout_s': 5,
    B + 'http.max_retries': 1, B + 'http.backoff_initial_s': 0.1,
    B + 'http.backoff_max_s': 0.5, B + 'http.retry_statuses': [503],
    B + 'probe.max_requests': 3, B + 'probe.timeout_s': 2,
    B + 'image.max_decode_pixels': 10_000_000,
    'vision.max_image_side': 640, 'vision.format': 'png', 'vision.quality': 40,
    'vision.alpha_policy.mode': 'refuse', 'vision.alpha_policy.background': '#123456',
}
REFUSE_KEYS = [*OVERRIDES, B + 'provider', B + 'api_key_file', B + 'unsupported_param_policy',
               'vision.transport', 'knowledge.root', 'storage.kb_store', 'storage.data_root', 'vlm.default_backend']


@pytest.mark.parametrize(('key', 'value'), OVERRIDES.items())
def test_shared_observer_override_changes_only_semantic_cache_identity(k2_env, key, value):
    folder(k2_env, 'f', 'A')
    server = ObserverServer()
    first = invoke_observer(k2_env, server)
    set_key(k2_env.config, key, value)
    if key == 'vision.alpha_policy.mode':
        del k2_env.config['vision']['alpha_policy']['background']
    if key == B + 'http.backoff_initial_s':
        k2_env.config['vlm']['backends']['main']['http']['backoff_max_s'] = 0.5
    changed = invoke_observer(k2_env, server)
    assert not changed['report']['failed']
    semantic = key.startswith((B + 'engine.', B + 'model.')) or key.startswith('vision.alpha_policy') or key == 'vision.max_image_side'
    assert (changed['rows']['observations'][0]['context_key'] != first['rows']['observations'][0]['context_key']) == semantic
    assert len(server.requests) == (2 if semantic else 1)
    if key == B + 'model.id':
        assert server.requests[-1]['model'] == value
    elif key == B + 'model.revision':
        assert changed['rows']['observations'][0]['model_revision'] == value


@pytest.mark.parametrize('key', REFUSE_KEYS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'wrong_type'])
def test_shared_observer_preflight_refuses_each_leaf(k2_env, key, failure):
    config = deepcopy(k2_env.config)
    set_key(config, key, 'design_required' if failure == 'sentinel' else {'unexpected': True}, remove=failure == 'missing')
    with pytest.raises(ConfigurationError):
        refused_stage(k2_env, config)


def test_default_backend_and_image_inheritance_are_explicit(k2_env):
    folder(k2_env, 'f', 'A')
    server = ObserverServer()
    first = invoke_observer(k2_env, server)
    k2_env.config['vlm']['backends']['alternate'] = deepcopy(k2_env.config['vlm']['backends']['main'])
    k2_env.config['vlm']['default_backend'] = 'alternate'
    changed = invoke_observer(k2_env, server)
    assert changed['rows']['observations'][0]['context_key'] == first['rows']['observations'][0]['context_key']
    assert len(server.requests) == 1
