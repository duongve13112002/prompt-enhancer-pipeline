"""Regression coverage for portable path-segment identity (T-005 round 2)."""

import os
from pathlib import Path

import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config, load_profile, resolve_writer_format


ROOT = Path(__file__).resolve().parents[1]
BAD_SEGMENTS = ['.. ', 'x.', ' x', 'x ', 'CON', 'nul.txt', 'COM1.log', 'LPT1',
                'COM0', 'LPT0.txt', 'COM¹.dat', 'CONIN$', 'CONOUT$']


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())


def write_config(tmp_path, config):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return path


@pytest.mark.parametrize(('section', 'key'), [
    ('storage', 'data_root'), ('storage', 'kb_store'), ('knowledge', 'root'), ('profiles', 'root'),
])
@pytest.mark.parametrize('value', ['.', './', 'data/..'])
def test_configured_root_cannot_be_project_root(config, tmp_path, project_root, section, key, value):
    config[section][key] = value
    with pytest.raises(ConfigurationError, match=section + '.' + key):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


@pytest.mark.parametrize(('section', 'key'), [
    ('storage', 'data_root'), ('storage', 'kb_store'), ('knowledge', 'root'), ('profiles', 'root'),
])
@pytest.mark.parametrize('bad', BAD_SEGMENTS)
def test_configured_roots_reject_ambiguous_segments(config, tmp_path, project_root, section, key, bad):
    config[section][key] = 'safe/' + bad + '/child'
    with pytest.raises(ConfigurationError, match='invalid path segment'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


@pytest.mark.parametrize('field', ['default_profile_id', 'exclude', 'info_names', 'overrides_file', 'manifest'])
@pytest.mark.parametrize('bad', BAD_SEGMENTS)
def test_filename_users_reject_ambiguous_segments(config, tmp_path, project_root, field, bad):
    if field == 'manifest':
        manifest = yaml.safe_load((ROOT / 'configs/profiles/_example/manifest.yaml').read_bytes())
        manifest['profile_id'] = bad
        (tmp_path / 'manifest.yaml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='invalid path segment'):
            load_profile(tmp_path, project_root=project_root)
        return
    if field == 'info_names':
        config['knowledge']['info_file']['names'] = [bad]
    elif field == 'overrides_file':
        config['knowledge']['overrides_file'] = bad
    else:
        config['profiles'][field] = [bad] if field == 'exclude' else bad
    with pytest.raises(ConfigurationError, match='invalid path segment'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


@pytest.mark.parametrize('field', ['api_key_file', 'file', 'mmproj_file'])
@pytest.mark.parametrize('bad', BAD_SEGMENTS)
def test_secret_and_model_paths_reject_ambiguous_segments(config, tmp_path, project_root, field, bad):
    backend = config['vlm']['backends']['main']
    if field == 'api_key_file':
        backend[field] = '.secrets/' + bad + '/file'
    else:
        backend['model'][field] = 'models/' + bad + '/file'
    with pytest.raises(ConfigurationError, match='invalid path segment'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


@pytest.mark.parametrize('field', ['file', 'mmproj_file'])
@pytest.mark.parametrize('value', ['.', './', 'data/..'])
def test_model_file_cannot_be_project_root(config, tmp_path, project_root, field, value):
    config['vlm']['backends']['main']['model'][field] = value
    with pytest.raises(ConfigurationError, match=field):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


@pytest.mark.parametrize('bad', BAD_SEGMENTS)
def test_schema_id_rejects_ambiguous_segment(config, bad):
    role = config['vlm']['roles']['writer']
    with pytest.raises(ConfigurationError, match='invalid path segment'):
        resolve_writer_format(role, {'output_schema_id': bad}, project_root=ROOT)


@pytest.mark.parametrize('bad', ['.', '..'])
def test_schema_id_cannot_be_navigation(config, bad):
    role = config['vlm']['roles']['writer']
    with pytest.raises(ConfigurationError, match='invalid path segment'):
        resolve_writer_format(role, {'output_schema_id': bad}, project_root=ROOT)


@pytest.mark.parametrize('kind', ['extends', 'overlay_of'])
@pytest.mark.parametrize('bad', BAD_SEGMENTS)
def test_reference_rejects_ambiguous_segment(config, tmp_path, project_root, kind, bad):
    if kind == 'extends':
        path = tmp_path / 'child.yaml'
        path.write_text(yaml.safe_dump({'schema_version': 1, 'extends': bad + '/file.yaml'}), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='invalid path segment'):
            load_config(path, 'kb status', project_root=project_root)
    else:
        base = write_config(tmp_path, config)
        overlay = tmp_path / 'overlay.yaml'
        overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': bad + '/file.yaml'}), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='invalid path segment'):
            load_config(base, 'kb status', overlay=overlay, project_root=project_root)


@pytest.mark.parametrize('kind', ['extends', 'overlay_of'])
def test_reference_cannot_be_project_root(config, tmp_path, project_root, kind):
    relative_root = os.path.relpath(project_root, tmp_path)
    if kind == 'extends':
        path = tmp_path / 'child.yaml'
        path.write_text(yaml.safe_dump({'schema_version': 1, 'extends': relative_root}), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='extends.*project root'):
            load_config(path, 'kb status', project_root=project_root)
    else:
        base = write_config(tmp_path, config)
        overlay = tmp_path / 'overlay.yaml'
        overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': relative_root}), encoding='utf-8')
        with pytest.raises(ConfigurationError, match='overlay_of.*project root'):
            load_config(base, 'kb status', overlay=overlay, project_root=project_root)


def test_inner_dot_values_are_valid(config, tmp_path, project_root):
    config['storage']['kb_store'] = 'data.v2/kb'
    config['knowledge']['overrides_file'] = 'kb_overrides.yaml'
    result = load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)
    assert result['storage']['kb_store'] == 'data.v2/kb'
    assert result['knowledge']['overrides_file'] == 'kb_overrides.yaml'


def test_link_resolving_to_project_root_is_refused(config, tmp_path, project_root, monkeypatch):
    link = tmp_path / 'root-link'
    original = os.readlink

    def labelled_link_double(path):
        if Path(path) == link:
            return str(project_root)
        return original(path)

    monkeypatch.setattr(os, 'readlink', labelled_link_double)
    config['storage']['kb_store'] = str(link)
    with pytest.raises(ConfigurationError, match='storage.kb_store.*project root'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


def test_info_names_checked_when_profiles_is_unread_sentinel(config, tmp_path, project_root):
    config['profiles'] = 'design_required'
    config['knowledge']['info_file']['names'] = ['NUL.txt']
    with pytest.raises(ConfigurationError, match='knowledge.info_file.names'):
        load_config(write_config(tmp_path, config), 'kb status', project_root=project_root)


def test_relative_arguments_use_project_root(config, tmp_path, project_root, monkeypatch):
    folder = project_root / 'relative-input'
    folder.mkdir()
    (folder / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    profile = folder / 'profile'
    profile.mkdir()
    (profile / 'manifest.yaml').write_bytes((ROOT / 'configs/profiles/_example/manifest.yaml').read_bytes())
    (profile / 'system_prompt.txt').write_bytes(b'prompt')
    monkeypatch.chdir(tmp_path)
    assert load_config('relative-input/config.yaml', 'kb status', project_root=project_root)['storage']['kb_store'] == 'data/kb'
    assert load_profile('relative-input/profile', project_root=project_root)[1] == b'prompt'
