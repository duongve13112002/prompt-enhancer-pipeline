"""Public kb sync loader checks using a completely synthetic resolved profile."""

from copy import deepcopy
import json
import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer import __main__ as cli


@pytest.fixture
def config(model_test_profile):
    config = model_test_profile
    config['knowledge']['root'] = 'source'
    config['knowledge']['naming']['rename_on_change'] = 0.5
    config['sync'].update(mass_deletion_guard={'max_removed_fraction': 0.5},
                          snapshots={'keep': 1}, gc={'retention': '1d'})
    config['embedding'].update(chosen='synthetic', revision='test', query_text_limit=128)
    config['observe'].update(representatives_per_folder=1, facet_schema_version='test')
    config['cards']['min_visible_images'] = 1
    config['index'].update(ann={'method': 'test', 'params': {}}, exact_search_below_count=1)
    config['index']['lexical']['ngram_size'] = 2
    config['storage'].update(kb_store='store', lock={'timeout_s': 5, 'poll_s': 0.01})
    return config


def load(project_root, config):
    path = project_root / 'k0-profile.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    return load_config(path, 'kb sync', project_root=project_root)


CASES = [
    (('knowledge', 'root'), 'other-source', '../outside'),
    (('knowledge', 'info_file', 'names'), ['other.txt'], []),
    (('knowledge', 'info_file', 'encoding'), 'latin-1', 'not-a-real-codec'),
    (('knowledge', 'info_file', 'on_description_count_mismatch'), 'model_assigns', 'other'),
    (('knowledge', 'image_extensions'), ['.png'], []),
    (('knowledge', 'concept_separator'), ';', ''),
    (('sync', 'mass_deletion_guard', 'max_removed_fraction'), 0.8, 1.5),
    (('sync', 'resume'), False, 'invalid'),
    (('runtime', 'cpu_workers'), 2, 0),
    (('storage', 'kb_store'), 'other-store', '../outside'),
    (('storage', 'data_root'), 'other-data', '../outside'),
    (('storage', 'table_format'), 'arrow', 'csv'),
    (('storage', 'lock', 'timeout_s'), 3, 0),
    (('storage', 'lock', 'poll_s'), 0.05, 0),
    (('sync', 'snapshots', 'keep'), 2, -1),
    (('sync', 'gc', 'retention'), '2d', 'invalid'),
]


@pytest.mark.parametrize(('parts', 'override', 'invalid'), CASES, ids=lambda value: str(value))
def test_kb_sync_configuration_override_and_refusal(project_root, config, parts, override, invalid):
    def set_value(document, value):
        parent = document
        for part in parts[:-1]:
            parent = parent[part]
        parent[parts[-1]] = value
    changed = deepcopy(config)
    set_value(changed, override)
    loaded = load(project_root, changed)
    actual = loaded
    for part in parts:
        actual = actual[part]
    assert actual == override
    set_value(changed, invalid)
    with pytest.raises(ConfigurationError):
        load(project_root, changed)
    changed = deepcopy(config)
    parent = changed
    for part in parts[:-1]:
        parent = parent[part]
    del parent[parts[-1]]
    with pytest.raises(ConfigurationError):
        load(project_root, changed)


@pytest.mark.parametrize(('parts', 'override', 'invalid'), CASES)
def test_kb_sync_refuses_sentinel_for_each_read(project_root, config, parts, override, invalid):
    changed = deepcopy(config)
    parent = changed
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = 'deploy_required'
    with pytest.raises(ConfigurationError):
        load(project_root, changed)


def test_cli_plan_uses_the_real_loader(project_root, config, monkeypatch, capsys):
    folder = project_root / 'source' / 'example'
    folder.mkdir(parents=True)
    (folder / 'info.txt').write_text('Name\nDescription', encoding='utf-8')
    path = project_root / 'cli-profile.yaml'
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    monkeypatch.setattr(cli, '__file__', str(project_root / 'src' / 'prompt_enhancer' / '__main__.py'))
    assert cli.main(['kb', 'sync', '--config', str(path), '--plan']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['plan']['folders']['added'] == ['example']
    assert not (project_root / 'store').exists()
