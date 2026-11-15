"""Round-3 regressions for root-relative names and real filesystem aliases."""

import os
from pathlib import Path
import shutil

import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config, resolve_writer_format


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())


def write_config(root, config):
    path = root / 'config.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return path


def test_project_root_ancestors_are_not_configured_segments(config, tmp_path):
    project = tmp_path / 'r~1' / 'my dir. x' / ' leading' / 'project'
    project.mkdir(parents=True)
    shutil.copytree(ROOT / 'contracts/schemas', project / 'contracts/schemas')
    path = write_config(project, config)
    assert load_config(path, 'kb status', project_root=project)['storage']['kb_store'] == 'data/kb'


def test_literal_tilde_names_are_valid(config, tmp_path, project_root):
    config['vlm']['backends']['main']['model']['file'] = 'model~2.gguf'
    config['profiles']['default_profile_id'] = 'a~1b'
    config['knowledge']['overrides_file'] = 'file~1.yaml'
    result = load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)
    assert result['vlm']['backends']['main']['model']['file'] == 'model~2.gguf'
    assert result['profiles']['default_profile_id'] == 'a~1b'
    assert result['knowledge']['overrides_file'] == 'file~1.yaml'


@pytest.mark.parametrize('protected', ['.secrets', 'models', '.git', '.venv'])
def test_resolved_alias_to_protected_root_is_refused(config, tmp_path, project_root, monkeypatch, protected):
    original = os.path.realpath
    alias = project_root / 'alias~1'
    inspected = []

    def labelled_realpath_double(path, *args, **kwargs):
        if Path(path) == alias:
            inspected.append(path)
            return str(project_root / protected)
        if '.secrets' in Path(path).relative_to(project_root).parts:
            raise AssertionError('secret target must not be inspected')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os.path, 'realpath', labelled_realpath_double)
    config['storage']['kb_store'] = 'alias~1/nested'
    with pytest.raises(ConfigurationError, match='storage.kb_store.*protected'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)
    assert inspected


def test_secret_alias_is_lexically_refused(config, tmp_path, project_root, monkeypatch):
    original = os.path.realpath

    def labelled_realpath_double(path, *args, **kwargs):
        if Path(path).name == 'SECRE~1':
            raise AssertionError('secret-key path must not be resolved')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os.path, 'realpath', labelled_realpath_double)
    config['vlm']['backends']['main']['api_key_file'] = 'SECRE~1/key'
    with pytest.raises(ConfigurationError, match='api_key_file.*expected a file under'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


@pytest.mark.parametrize('protected', ['.secrets', '.git', '.venv'])
def test_model_alias_to_protected_root_is_refused(config, tmp_path, project_root, monkeypatch, protected):
    original = os.path.realpath
    alias = project_root / 'alias~1'

    def labelled_realpath_double(path, *args, **kwargs):
        if Path(path) == alias:
            return str(project_root / protected)
        if '.secrets' in Path(path).relative_to(project_root).parts:
            raise AssertionError('secret target must not be inspected')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os.path, 'realpath', labelled_realpath_double)
    config['vlm']['backends']['main']['model']['file'] = 'alias~1/model.gguf'
    with pytest.raises(ConfigurationError, match='model.file.*protected'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


def test_secret_directory_case_is_exact(config, tmp_path, project_root):
    backend = config['vlm']['backends']['main']
    backend['api_key_file'] = '.secrets/a.key'
    path = write_config(tmp_path, config)
    assert load_config(path, 'kb status', project_root=project_root)['vlm']['backends']['main']['api_key_file'] == '.secrets/a.key'
    backend['api_key_file'] = '.SECRETS/a.key'
    write_config(tmp_path, config)
    with pytest.raises(ConfigurationError, match='api_key_file.*expected a file under'):
        load_config(path, 'kb status', project_root=project_root)


def test_writer_errors_name_explicit_source(config, project_root):
    role = config['vlm']['roles']['writer']
    role['output']['format'] = 'none'
    with pytest.raises(ConfigurationError) as error:
        resolve_writer_format(role, {}, project_root=project_root, role_source='deployment.yaml')
    assert str(error.value) == 'deployment.yaml: writer.output: expected paired from_profile_manifest references'
    role['output']['format'] = 'from_profile_manifest'
    with pytest.raises(ConfigurationError) as error:
        resolve_writer_format(role, {}, project_root=project_root, manifest_source='profile/manifest.yaml')
    assert str(error.value) == 'profile/manifest.yaml: output_schema_id: missing or invalid'
    with pytest.raises(ConfigurationError) as error:
        resolve_writer_format(role, {'output_schema_id': 'bad$id'}, project_root=project_root,
                              manifest_source='profile/manifest.yaml')
    assert str(error.value) == "profile/manifest.yaml: output_schema_id: invalid schema id 'bad$id'"


def test_invalid_role_schema_has_one_source_and_key(config, tmp_path, project_root):
    config['vlm']['roles']['planner']['output']['schema_id'] = 'bad$id'
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigurationError) as error:
        load_config(path, 'kb status', project_root=project_root)
    assert str(error.value) == f"{path}: vlm.roles.planner.output.schema_id: invalid schema id 'bad$id'"
