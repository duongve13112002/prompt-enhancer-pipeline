"""Every new P3 lifecycle setting has override and refusal evidence."""
from copy import deepcopy

import pytest

from intake_fixtures import configuration
from prompt_enhancer.config import ConfigurationError, validate_pipeline_reads

KEYS = ['request_deadline_s', 'trace.retention_s', 'trace.queue_limit', 'trace.workers', 'trace.drain_timeout_s',
        'monitoring.queue_limit', 'monitoring.workers', 'monitoring.drain_timeout_s']


def runtime():
    return {'request_deadline_s': 5, 'trace': {'retention_s': 20, 'queue_limit': 2, 'workers': 1,
            'drain_timeout_s': 2}, 'monitoring': {'queue_limit': 2, 'workers': 1, 'drain_timeout_s': 2}}


@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('bad', [None, 'deploy_required', 0, -1, True, float('inf')])
def test_each_pipeline_key_refuses(model_test_profile, tmp_path, project_root, key, bad):
    config = configuration(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    config['validation']['verifier']['sampled_fraction_for_rest'] = 1
    parts = key.split('.')
    parent = config['runtime'] if len(parts) == 1 else config['runtime'][parts[0]]
    if bad is None:
        parent.pop(parts[-1])
    else:
        parent[parts[-1]] = bad
    with pytest.raises(ConfigurationError, match='runtime'):
        validate_pipeline_reads(config, project_root=project_root)


@pytest.mark.parametrize('key', KEYS)
def test_pipeline_override_reaches_lifecycle_settings(model_test_profile, tmp_path, project_root, key):
    config = configuration(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    config['validation']['verifier']['sampled_fraction_for_rest'] = 1
    parts = key.split('.')
    parent = config['runtime'] if len(parts) == 1 else config['runtime'][parts[0]]
    parent[parts[-1]] = 3
    checked = validate_pipeline_reads(config, project_root=project_root)
    assert (checked if len(parts) == 1 else checked[parts[0]])[parts[-1]] == 3


def test_disabled_monitoring_settings_unread(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    config['runtime'].update(runtime())
    config['runtime'].pop('monitoring')
    assert 'monitoring' not in validate_pipeline_reads(config, project_root=project_root)


@pytest.mark.parametrize('duration', ['request_deadline_s', 'trace.retention_s', 'trace.drain_timeout_s',
                                      'monitoring.drain_timeout_s'])
def test_unrepresentable_duration(model_test_profile, tmp_path, project_root, duration):
    config = configuration(model_test_profile, tmp_path)
    config['validation']['verifier']['sampled_fraction_for_rest'] = 1
    parent = config['runtime']
    head, _, leaf = duration.rpartition('.')
    if head:
        parent = parent[head]
    parent[leaf] = 1e20 if leaf == 'retention_s' else 10**400
    with pytest.raises(ConfigurationError, match=duration):
        validate_pipeline_reads(config, project_root=project_root)


def test_attachment_configuration_accepts_true(model_test_profile, tmp_path, project_root):
    import yaml
    from prompt_enhancer.config import effective_request_config, load_config
    from prompt_enhancer.pipeline import Pipeline
    config = configuration(model_test_profile, tmp_path)
    config['rag']['enabled'] = True
    config['edit']['attach_kb_reference_images'] = True

    assert Pipeline(config, project_root=project_root).config['edit']['attach_kb_reference_images'] is True
    target = tmp_path / 'no-retention.yaml'
    target.write_text(yaml.safe_dump(config), encoding='utf-8')
    assert load_config(target, 'enhance once', project_root=project_root)['edit']['attach_kb_reference_images'] is True
    config['edit']['attach_kb_reference_images'] = False
    config['request_overrides']['allowed_keys'] = ['edit.attach_kb_reference_images']
    assert effective_request_config(config, {'edit.attach_kb_reference_images': True}, project_root=project_root)['edit']['attach_kb_reference_images'] is True
