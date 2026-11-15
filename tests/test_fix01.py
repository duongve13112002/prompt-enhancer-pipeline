"""Regression tests for T-005 review round 1."""

from pathlib import Path
import json

import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config, load_profile, resolve_writer_format
from prompt_enhancer.config import _cached_schema, _schema


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    result = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    result['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}  # test-only: resolves deploy_required (T-006)
    return result


def write_config(tmp_path, config):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return path


@pytest.mark.parametrize(('section', 'key', 'value'), [
    ('storage', 'kb_store', '../../escape'), ('storage', 'kb_store', 'C:/Windows/Temp/kb'),
    ('storage', 'kb_store', '/etc/kb'), ('storage', 'kb_store', ''),
    ('storage', 'data_root', '.secrets'), ('profiles', 'root', '../../elsewhere'),
])
def test_configured_path_refused(config, tmp_path, section, key, value):
    config[section][key] = value
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigurationError, match=section + '.' + key) as error:
        load_config(path, 'kb status')
    assert str(path) in str(error.value)


def test_secret_path_escape_refused(config, tmp_path):
    config['vlm']['backends']['cloud_claude']['api_key_file'] = 'C:/Users/x/k.key'
    with pytest.raises(ConfigurationError, match='api_key_file'):
        load_config(write_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('value', ['', '.secrets', '.secrets/../escape.key', '../escape.key', '.secret/key'])
def test_secret_key_requires_lexical_secret_file(config, tmp_path, value):
    config['vlm']['backends']['cloud_claude']['api_key_file'] = value
    with pytest.raises(ConfigurationError, match='api_key_file'):
        load_config(write_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize(('section', 'key'), [
    ('knowledge', 'root'), ('storage', 'data_root'), ('storage', 'kb_store'), ('profiles', 'root'),
])
@pytest.mark.parametrize('directory', ['.secrets', '.git', '.venv', 'models'])
def test_protected_root_refused(config, tmp_path, section, key, directory):
    config[section][key] = directory + '/nested'
    with pytest.raises(ConfigurationError, match=section + '.' + key):
        load_config(write_config(tmp_path, config), 'kb status')


def test_secret_path_is_never_resolved(config, tmp_path, monkeypatch):
    original = Path.resolve

    def checked(path, *args, **kwargs):
        if '.secrets' in path.parts:
            raise AssertionError('secret path must not be resolved')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', checked)
    config['vlm']['backends']['main']['api_key_file'] = '.secrets/alternate.key'
    result = load_config(write_config(tmp_path, config), 'kb status')
    assert result['vlm']['backends']['main']['api_key_file'] == '.secrets/alternate.key'


def test_link_to_secret_is_refused_without_reading_target(config, tmp_path, project_root, monkeypatch):
    import os

    link = tmp_path / 'simulated-link'
    original = os.readlink

    def fake_readlink(path):
        path = Path(path)
        if '.secrets' in path.parts:
            raise AssertionError('secret target must not be inspected')
        if path == link:
            return str(project_root / '.secrets' / 'key')
        return original(path)

    monkeypatch.setattr(os, 'readlink', fake_readlink)
    config['storage']['kb_store'] = str(link)
    with pytest.raises(ConfigurationError, match='storage.kb_store.*protected'):
        load_config(write_config(tmp_path, config), 'kb status')


def test_link_inspection_failure_is_refused(config, tmp_path, monkeypatch):
    import os

    original = os.readlink

    def denied(path):
        if Path(path).name == 'denied-link':
            raise PermissionError(13, 'link inspection denied')
        return original(path)

    monkeypatch.setattr(os, 'readlink', denied)
    config['storage']['kb_store'] = 'denied-link'
    with pytest.raises(ConfigurationError, match='storage.kb_store.*link inspection denied'):
        load_config(write_config(tmp_path, config), 'kb status')


def test_internal_link_is_resolved(config, tmp_path, project_root, monkeypatch):
    import os

    link = tmp_path / 'simulated-internal-link'
    original = os.readlink
    target = str(project_root / 'safe-target')
    if os.name == 'nt':
        target = '\\\\?\\' + target

    def fake_readlink(path):
        if Path(path) == link:
            return target
        return original(path)

    monkeypatch.setattr(os, 'readlink', fake_readlink)
    config['storage']['kb_store'] = str(link / 'kb')
    assert load_config(write_config(tmp_path, config), 'kb status')['storage']['kb_store'] == str(link / 'kb')


def test_symlink_escape_refused(config, tmp_path, project_root):
    link = tmp_path / 'outside-link'
    try:
        link.symlink_to(project_root.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f'symlink unavailable: {error}')
    config['storage']['kb_store'] = str(link / 'escape')
    with pytest.raises(ConfigurationError, match='storage.kb_store'):
        load_config(write_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize(('key', 'value'), [
    ('overrides_file', '../outside'), ('overrides_file', '/absolute'),
])
def test_kb_relative_filename_refused(config, tmp_path, key, value):
    config['knowledge'][key] = value
    with pytest.raises(ConfigurationError, match=key):
        load_config(write_config(tmp_path, config), 'kb status')


def test_info_filename_refused(config, tmp_path):
    config['knowledge']['info_file']['names'] = ['../outside.txt']
    with pytest.raises(ConfigurationError, match='knowledge.info_file.names'):
        load_config(write_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('field', ['file', 'mmproj_file'])
def test_model_file_escape_refused(config, tmp_path, field):
    config['vlm']['backends']['main']['model'][field] = '../../outside'
    with pytest.raises(ConfigurationError, match=field):
        load_config(write_config(tmp_path, config), 'kb status')


@pytest.mark.parametrize('key', ['default_profile_id', 'exclude'])
def test_profile_directory_names_refused(config, tmp_path, key):
    config['profiles'][key] = '../outside' if key == 'default_profile_id' else ['../outside']
    with pytest.raises(ConfigurationError, match='profiles.' + key):
        load_config(write_config(tmp_path, config), 'kb status')


def test_manifest_profile_directory_name_refused(tmp_path):
    manifest = yaml.safe_load((ROOT / 'configs/profiles/_example/manifest.yaml').read_bytes())
    manifest['profile_id'] = '../outside'
    (tmp_path / 'manifest.yaml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='manifest.yaml.*profile_id'):
        load_profile(tmp_path)


@pytest.mark.parametrize(('suffix', 'key'), [('\npurpose: other\n', 'purpose'), ('\non: 1\n', 'on|True')])
def test_yaml_duplicate_or_nonstring_key(config, tmp_path, suffix, key):
    path = write_config(tmp_path, config)
    path.write_text(path.read_text(encoding='utf-8') + suffix, encoding='utf-8')
    with pytest.raises(ConfigurationError, match=key):
        load_config(path, 'kb status')


def test_yaml_merge_override_is_not_duplicate(config, tmp_path):
    path = write_config(tmp_path, config)
    text = path.read_text(encoding='utf-8')
    text = text.replace('  kb_store: data/kb', '  <<: {kb_store: data/old}\n  kb_store: data/new')
    path.write_text(text, encoding='utf-8')
    assert load_config(path, 'kb status')['storage']['kb_store'] == 'data/new'


@pytest.mark.parametrize('payload', [b'', b'---\n{}\n---\n{}\n', b'!!python/object/apply:os.system [echo]\n'])
def test_invalid_yaml_document_refused(tmp_path, payload):
    path = tmp_path / 'bad.yaml'
    path.write_bytes(payload)
    with pytest.raises(ConfigurationError, match='bad.yaml'):
        load_config(path, 'kb status')


def test_utf8_bom_is_accepted(config, tmp_path):
    path = write_config(tmp_path, config)
    path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
    assert load_config(path, 'kb status')['storage']['kb_store'] == 'data/kb'


@pytest.mark.parametrize('key', ['knowledge', 'writer', 'rag'])
def test_overlay_top_level_forbidden(config, tmp_path, key):
    base = write_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text(yaml.safe_dump({'schema_version': 1, 'overlay_of': 'config.yaml', key: {}}), encoding='utf-8')
    with pytest.raises(ConfigurationError, match=key):
        load_config(base, 'kb status', overlay=overlay)


def test_overlay_requires_base_reference(config, tmp_path):
    base = write_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text('schema_version: 1\nruntime: {}\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='overlay_of'):
        load_config(base, 'kb status', overlay=overlay)


def test_overlay_concurrency_override_and_refusal(config, tmp_path):
    base = write_config(tmp_path, config)
    overlay = tmp_path / 'overlay.yaml'
    body = {'schema_version': 1, 'overlay_of': 'config.yaml', 'vlm': {'backends': {'main': {'max_concurrency': 4}}}}
    overlay.write_text(yaml.safe_dump(body), encoding='utf-8')
    assert load_config(base, 'kb status', overlay=overlay)['vlm']['backends']['main']['max_concurrency'] == 4
    body['vlm']['backends']['main']['max_concurrency'] = -1
    overlay.write_text(yaml.safe_dump(body), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='max_concurrency'):
        load_config(base, 'kb status', overlay=overlay)


def test_reference_error_names_referrer(tmp_path):
    child = tmp_path / 'child.yaml'
    child.write_text('schema_version: 1\nextends: missing.yaml\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='child.yaml.*extends'):
        load_config(child, 'kb status')


def test_writer_schema_error_names_manifest(config):
    role = config['vlm']['roles']['writer']
    with pytest.raises(ConfigurationError, match='manifest.yaml.*output_schema_id'):
        resolve_writer_format(role, {'output_schema_id': 'missing.v0'}, project_root=ROOT)


def test_role_schema_error_has_one_target_path(config, tmp_path, project_root):
    config['vlm']['roles']['planner']['output']['schema_id'] = 'missing.v0'
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigurationError, match='config.yaml.*vlm.roles.planner.output.schema_id') as error:
        load_config(path, 'kb status')
    assert str(error.value).count(str(project_root / 'contracts/schemas/missing.v0.schema.json')) == 1


def test_missing_config_and_manifest_schemas_name_referrer(config, tmp_path):
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigurationError, match='config.yaml.*schema_version.*enhancer_config'):
        load_config(path, 'kb status', project_root=tmp_path)
    manifest = ROOT / 'configs/profiles/_example/manifest.yaml'
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'manifest.yaml').write_bytes(manifest.read_bytes())
    with pytest.raises(ConfigurationError, match='manifest.yaml.*schema_version.*profile_manifest'):
        load_profile(profile, project_root=tmp_path)


def test_schema_cache_invalidates_on_file_change(tmp_path):
    path = tmp_path / 'contracts/schemas/sample.v0.schema.json'
    path.parent.mkdir(parents=True)
    path.write_text('{"type": "string"}', encoding='utf-8')
    _cached_schema.cache_clear()
    first = _schema(tmp_path, 'sample.v0')
    assert _schema(tmp_path, 'sample.v0') is first
    assert _cached_schema.cache_info().hits >= 1
    path.write_text(json.dumps({'type': 'integer', 'description': 'updated'}), encoding='utf-8')
    assert _schema(tmp_path, 'sample.v0')['type'] == 'integer'


def test_escaping_reference_names_referrer(tmp_path):
    child = tmp_path / 'child.yaml'
    child.write_text('schema_version: 1\nextends: ../../outside.yaml\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='child.yaml.*extends'):
        load_config(child, 'kb status')


def test_opening_secret_path_is_refused_before_resolve(project_root, monkeypatch):
    original = Path.resolve

    def checked(path, *args, **kwargs):
        if '.secrets' in path.parts:
            raise AssertionError('secret path must not be resolved')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', checked)
    with pytest.raises(ConfigurationError, match='protected'):
        load_config(project_root / '.secrets' / 'config.yaml', 'kb status')


def test_gc_refuses_snapshot_retention(config, tmp_path):
    config['sync']['gc']['retention'] = '30d'
    config['sync']['snapshots']['keep'] = 'design_required'
    with pytest.raises(ConfigurationError, match='sync.snapshots.keep'):
        load_config(write_config(tmp_path, config), 'kb gc')
