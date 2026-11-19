"""S0 startup gates and typed request overrides; all settings are test-only."""

from copy import deepcopy

import pytest
import yaml

from intake_fixtures import configuration
from prompt_enhancer.config import ConfigurationError, _command_reads, effective_request_config, load_config


@pytest.fixture
def intake_config(model_test_profile, tmp_path):
    return configuration(model_test_profile, tmp_path)


def load(config, tmp_path, project_root, command='enhance once'):
    path = tmp_path / 'intake.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    return load_config(path, command, project_root=project_root)


def leaf(config, key):
    parts = key.split('.')
    for part in parts[:-1]:
        config = config[part]
    return config, parts[-1]


@pytest.mark.parametrize('key,good,bad', [
    ('vision.max_decode_pixels', 200, [0, True, 2.0, 'deploy_required']),
    ('runtime.intake_deadline_s', 12.5, [0, False, float('inf'), float('nan'), 10**400, 'fit_from_bench']),
])
def test_intake_values_override_and_refusal(intake_config, tmp_path, project_root, key, good, bad):
    parent, name = leaf(intake_config, key)
    parent[name] = good
    assert leaf(load(intake_config, tmp_path, project_root), key)[0][name] == good
    for value in bad:
        parent[name] = value
        with pytest.raises(ConfigurationError, match=key):
            load(intake_config, tmp_path, project_root)
    del parent[name]
    with pytest.raises(ConfigurationError, match=key):
        load(intake_config, tmp_path, project_root)


@pytest.mark.parametrize('allowed', [['missing.key'], ['vision'], ['rag.enabled', 'rag.enabled'],
                                   ['request_overrides.allowed_keys'], 'rag.enabled'])
def test_override_policy_refused(intake_config, tmp_path, project_root, allowed):
    intake_config['request_overrides']['allowed_keys'] = allowed
    with pytest.raises(ConfigurationError, match='request_overrides.allowed_keys'):
        load(intake_config, tmp_path, project_root)


def test_override_permission_and_types(intake_config, project_root):
    original = deepcopy(intake_config)
    with pytest.raises(ConfigurationError, match='request configuration validation failed'):
        effective_request_config(intake_config, {'vision.max_image_side': 3}, project_root=project_root)
    intake_config['request_overrides']['allowed_keys'] = ['vision.max_image_side', 'rag.enabled']
    effective = effective_request_config(intake_config, {'vision.max_image_side': 3, 'rag.enabled': True},
                                         project_root=project_root)
    assert effective['vision']['max_image_side'] == 3 and effective['rag']['enabled'] is True
    assert intake_config['vision'] == original['vision'] and intake_config['rag'] == original['rag']
    for override in ({'rag.enabled': 'false'}, {'vision.max_image_side': True},
                     {'vision.max_image_side': 2.0}, {'vision.max_image_side': 'fit_from_bench'},
                     {'unknown.key': 2}, {'request_overrides.allowed_keys': []}):
        with pytest.raises(ConfigurationError):
            effective_request_config(intake_config, override, project_root=project_root)


def test_override_revalidates_effective_config(intake_config, project_root):
    intake_config['request_overrides']['allowed_keys'] = ['profiles.root', 'runtime.intake_deadline_s']
    for overrides in ({'profiles.root': '../outside'}, {'runtime.intake_deadline_s': float('nan')}):
        with pytest.raises(ConfigurationError):
            effective_request_config(intake_config, overrides, project_root=project_root)


def test_request_schema_load_failure_is_structural(tmp_path):
    # Review reproduction: this project root deliberately has no contracts directory.
    root = tmp_path / 'private-schema-location'
    with pytest.raises(ConfigurationError) as caught:
        effective_request_config({}, {}, project_root=root)
    assert str(caught.value) == 'configuration: invalid base configuration'


@pytest.mark.parametrize('failure', ['denied', 'type', 'path', 'policy', 'container', 'object', 'base'])
def test_override_diagnostics_are_structural(intake_config, project_root, failure):
    intake_config['request_overrides']['allowed_keys'] = ['profiles.root', 'vision.max_image_side']
    overrides = {
        'denied': {'SECRET PROMPT TEXT': 'private/value'},
        'type': {'vision.max_image_side': 'SECRET PROMPT TEXT'},
        'path': {'profiles.root': '../private/value'},
        'policy': {}, 'container': {}, 'object': None, 'base': {},
    }[failure]
    if failure == 'policy':
        intake_config['request_overrides']['allowed_keys'] = ['SECRET PROMPT TEXT']
    elif failure == 'container':
        intake_config['request_overrides']['allowed_keys'] = ['vision']
    elif failure == 'base':
        intake_config['vision']['quality'] = 'SECRET PROMPT TEXT'
    with pytest.raises(ConfigurationError) as caught:
        effective_request_config(intake_config, overrides, project_root=project_root)
    message = str(caught.value)
    if failure in ('base', 'policy', 'container'):
        assert 'configuration:' in message
        assert ('vision.quality' if failure == 'base' else 'request_overrides.allowed_keys') in message
        assert 'SECRET' not in message and 'private/' not in message
    else:
        assert message == 'request configuration validation failed (ConfigurationError)'


@pytest.mark.parametrize('command', ['enhance once', 'enhance serve', 'eval run'])
@pytest.mark.parametrize('key,good,bad', [
    ('index.engine', 'numpy_exact', 'design_required'),
    ('index.metric', 'inner_product', 'design_required'),
    ('index.build.block_rows', 3, 'fit_from_bench'),
    ('index.lexical.ngram_size', 3, 'fit_from_gt'),
])
def test_enhance_index_preflight(intake_config, tmp_path, project_root, command, key, good, bad):
    # Other stages' preflight precedes index: use once to exercise the actual loader for every index leaf.
    intake_config['rag']['enabled'] = True
    assert 'index' in _command_reads(command, intake_config)
    parent, name = leaf(intake_config, key)
    parent[name] = good
    assert leaf(load(intake_config, tmp_path, project_root), key)[0][name] == good
    parent[name] = bad
    with pytest.raises(ConfigurationError, match=key):
        load(intake_config, tmp_path, project_root)
    intake_config['rag']['enabled'] = False
    assert 'index' not in _command_reads(command, intake_config)
    assert leaf(load(intake_config, tmp_path, project_root), key)[0][name] == bad
    intake_config['rag']['enabled'] = True
    del parent[name]
    with pytest.raises(ConfigurationError, match=key):
        load(intake_config, tmp_path, project_root)


def test_rag_off_skips_knowledge_path_resolution(intake_config, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.config as module

    original = module._configured_path

    retrieval_off = [True]

    def guard(value, root, path, key, **kwargs):
        assert not retrieval_off[0] or key not in ('knowledge.root', 'storage.kb_store')
        return original(value, root, path, key, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('retrieval off resolved the knowledge root (R-009)')

    monkeypatch.setattr(module, '_configured_path', guard)
    # decision 175: the root now goes through one resolver and its physical step (labelled spies). The root must be a
    # real value here: a sentinel is skipped before the resolver whatever `knowledge_access` says.
    intake_config['knowledge']['root'] = str(tmp_path / 'no-external-knowledge-base')
    intake_config['rag']['enabled'] = False
    monkeypatch.setattr(module, 'resolve_knowledge_root', forbidden)
    monkeypatch.setattr(module, '_physical', forbidden)
    load(intake_config, tmp_path, project_root)
    # control: the spy sees the call once retrieval is on (the load may stop at the root's own refusal or pass)
    calls = []

    def counting(*args, **kwargs):
        calls.append(args)
        return tmp_path

    retrieval_off[0] = False
    monkeypatch.setattr(module, 'resolve_knowledge_root', counting)
    intake_config['rag']['enabled'] = True
    try:
        load(intake_config, tmp_path, project_root)
    except ConfigurationError:
        pass  # other retrieval keys may be unresolved in this fixture; only the call matters here
    assert calls, 'with retrieval on the loader resolves the knowledge root'


def test_policy_is_required(intake_config, tmp_path, project_root):
    del intake_config['request_overrides']
    with pytest.raises(ConfigurationError, match='request_overrides'):
        load(intake_config, tmp_path, project_root)
