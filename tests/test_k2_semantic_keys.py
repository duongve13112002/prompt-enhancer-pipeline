"""Each removed transport/unused setting preserves P1/P2 successful-reply identity."""

from copy import deepcopy
from pathlib import Path

import pytest

from prompt_enhancer.k2_naming import _role_revisions, _naming_key
from prompt_enhancer.k2_observe import observation_revisions
from prompt_enhancer.model_client import semantic_role_settings
from test_k2_config import set_key, ROLE_OVERRIDES
from test_k2_observe_shared_config import B, OVERRIDES

ROOT = Path(__file__).resolve().parents[1]
OPERATIONAL = {key: value for key, value in OVERRIDES.items()
               if key.startswith(B) and not key.startswith((B + 'engine.', B + 'model.'))}
SEMANTIC = {key: value for key, value in OVERRIDES.items()
            if key.startswith((B + 'engine.', B + 'model.'))}
SEMANTIC[B + 'extra_body'] = {'test_extension': 'different'}
OPERATIONAL[B + 'api_key_file'] = '.secrets/test.api-key'
ROLES = ['info_assessor', 'folder_namer', 'observer', 'card_builder']


def revisions(config, role):
    if role == 'card_builder':
        return semantic_role_settings(config, role)
    return (observation_revisions(config, b'prompt', ROOT) if role == 'observer' else
            _role_revisions(config, role, b'prompt', ROOT))


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize(('key', 'value'), OPERATIONAL.items())
def test_each_operational_leaf_is_not_reply_identity(model_test_profile, role, key, value):
    before = revisions(model_test_profile, role)
    changed = deepcopy(model_test_profile)
    set_key(changed, key, value)
    assert revisions(changed, role) == before


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize(('key', 'value'), SEMANTIC.items())
def test_each_model_selection_leaf_is_reply_identity(model_test_profile, role, key, value):
    before = revisions(model_test_profile, role)
    changed = deepcopy(model_test_profile)
    set_key(changed, key, value)
    assert revisions(changed, role) != before


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize(('field', 'value'), [(key, value) for key, value in ROLE_OVERRIDES.items()
                                           if key not in ('backend', 'prompt_id', 'output.schema_id')])
def test_each_retained_inference_leaf_invalidates(model_test_profile, role, field, value):
    before = revisions(model_test_profile, role)
    changed = deepcopy(model_test_profile)
    set_key(changed, 'vlm.roles.' + role + '.' + field, value)
    assert revisions(changed, role) != before


@pytest.mark.parametrize('role', ROLES)
def test_unreachable_backend_and_equivalent_backend_alias_do_not_invalidate(model_test_profile, role):
    config = deepcopy(model_test_profile)
    before = revisions(config, role)
    config['vlm']['backends']['alternate'] = deepcopy(config['vlm']['backends']['main'])
    config['vlm']['backends']['unused'] = {'not': 'a selected backend'}
    config['vlm']['default_backend'] = 'alternate'
    assert revisions(config, role) == before
    config['vlm']['roles'][role]['backend'] = 'alternate'
    assert revisions(config, role) == before


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('key', ['vision.max_image_side', 'vision.format', 'vision.quality', 'vision.alpha_policy.background'])
def test_unused_image_inheritance_does_not_invalidate(model_test_profile, role, key):
    config = deepcopy(model_test_profile)
    if isinstance(config['vlm']['roles'][role]['image'], dict):
        config['vlm']['roles'][role]['image'].update(max_side=9, format='png')
    before = revisions(config, role)
    changes = {'vision.max_image_side': 37, 'vision.format': 'webp', 'vision.quality': 13,
               'vision.alpha_policy.background': '#123456'}
    set_key(config, key, changes[key])
    if isinstance(config['vlm']['roles'][role]['image'], dict) and key.startswith('vision.alpha_policy'):
        assert revisions(config, role) != before
    else:
        assert revisions(config, role) == before


@pytest.mark.parametrize('role', ['folder_namer', 'observer'])
@pytest.mark.parametrize(('key', 'value'), [('image.max_side', 7), ('image.format', 'png'), ('image.quality', 31),
                                         ('vision.max_image_side', 39), ('vision.format', 'webp'),
                                         ('vision.alpha_policy.background', '#123456')])
def test_effective_image_processor_leaves_invalidate(model_test_profile, role, key, value):
    config = deepcopy(model_test_profile)
    config['vlm']['roles'][role]['image'].update(max_side='from_vision', format='from_vision')
    before = revisions(config, role)
    set_key(config, key if key.startswith('vision.') else 'vlm.roles.' + role + '.' + key, value)
    assert revisions(config, role) != before


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('route', ['budget', 'image', 'disabled'])
def test_only_applicable_fallback_model_identity_invalidates(model_test_profile, role, route):
    config = deepcopy(model_test_profile)
    config['vlm']['roles'][role]['backend'] = 'cloud_claude'
    backend = config['vlm']['backends']['cloud_claude']
    backend.update(on_budget_exhausted='fallback_to_default_backend' if route == 'budget' else 'stop',
                   allow_images=route != 'image', anthropic_version='2023-06-01')
    before = revisions(config, role)
    config['vlm']['backends']['main']['model']['revision'] = 'changed-fallback'
    applicable = route == 'budget' or (route == 'image' and role in ('folder_namer', 'observer'))
    assert (revisions(config, role) != before) == applicable
    before = revisions(config, role)
    backend['call_budget'] = 9
    backend['http']['read_timeout_s'] = 12
    assert revisions(config, role) == before


def test_naming_key_is_payload_and_ordered_attachments_not_full_folder_path():
    rev = {'model': 'model', 'prompt': 'prompt', 'schema': 'schema'}
    context = {'folder_id': 'one/Leaf', 'folder_name': 'Leaf', 'info_file_text': 'Human', 'names': [],
               'sampled_image_ids': ['first', 'second']}
    first = _naming_key('k2_naming', context, rev)
    assert _naming_key('k2_naming', dict(context, folder_id='two/Leaf'), rev) == first
    for field, value in [('folder_name', 'Other'), ('info_file_text', 'Changed'), ('names', [{'name': 'New', 'position': 0}]),
                         ('sampled_image_ids', ['second', 'first']), ('sampled_image_ids', ['first', 'third'])]:
        assert _naming_key('k2_naming', dict(context, **{field: value}), rev) != first


@pytest.mark.parametrize('role', ROLES)
def test_cloud_api_and_routing_policy_are_semantic_but_route_suffix_is_not(model_test_profile, role):
    config = deepcopy(model_test_profile)
    config['vlm']['roles'][role]['backend'] = 'cloud_claude'
    backend = config['vlm']['backends']['cloud_claude']
    backend['anthropic_version'] = '2023-06-01'
    before = revisions(config, role)
    backend['anthropic_version'] = '2024-01-01'
    assert revisions(config, role) != before
    before = revisions(config, role)
    backend['on_budget_exhausted'] = 'stop'
    assert revisions(config, role) != before
    if role in ('folder_namer', 'observer'):
        before = revisions(config, role)
        backend['allow_images'] = not backend['allow_images']
        assert revisions(config, role) != before
    config['vlm']['roles'][role]['backend'] = 'cloud_gemini'
    backend = config['vlm']['backends']['cloud_gemini']
    backend['endpoints'] = {'chat': 'v1/models/generate', 'health': 'health'}
    before = revisions(config, role)
    backend['endpoints']['chat'] = 'v1/models/other'
    assert revisions(config, role) == before
    backend['endpoints']['chat'] = 'v2/models/other'
    assert revisions(config, role) != before


@pytest.mark.parametrize('role', ['info_assessor', 'folder_namer', 'observer'])
def test_prompt_and_processor_revisions_are_scoped(model_test_profile, role, monkeypatch):
    import PIL
    before = revisions(model_test_profile, role)
    changed_prompt = (observation_revisions(model_test_profile, b'changed', ROOT) if role == 'observer'
                      else _role_revisions(model_test_profile, role, b'changed', ROOT))
    assert changed_prompt != before
    monkeypatch.setattr(PIL, '__version__', 'changed-processor')
    assert (revisions(model_test_profile, role) != before) == (role != 'info_assessor')
