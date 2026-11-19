"""The approved-profile gate's view of a configuration: the merged `extends` chain, refused outside the project."""

from pathlib import Path

import pytest
import yaml

from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.run_settings import load_runtime_settings

REPO = Path(__file__).resolve().parents[1]


def test_the_dev_example_resolves_to_its_merged_chain():
    merged = load_runtime_settings('configs/enhancer.dev.example.yaml', REPO)
    own = yaml.safe_load((REPO / 'configs/enhancer.dev.example.yaml').read_text(encoding='utf-8'))
    base = yaml.safe_load((REPO / 'configs/enhancer.example.yaml').read_text(encoding='utf-8'))
    # A value the dev file states wins; every section only the base states is kept (no default is invented).
    assert merged['knowledge']['root'] == own['knowledge']['root']
    for key in set(base) - set(own) - {'extends'}:
        assert merged[key] == base[key], key
    assert 'extends' not in merged  # the chain is merged away: the gate compares values, not file links


def test_a_change_in_an_extended_file_shows_in_the_child(tmp_path):
    root = tmp_path / 'project'
    (root / 'configs').mkdir(parents=True)
    (root / 'configs' / 'base.yaml').write_text('schema_version: 2\nonly_base: {n: 1}\nshared: {v: base}\n',
                                                encoding='utf-8')
    (root / 'configs' / 'child.yaml').write_text('schema_version: 2\nextends: base.yaml\nshared: {v: child}\n',
                                                 encoding='utf-8')
    first = load_runtime_settings('configs/child.yaml', root)
    assert first == {'schema_version': 2, 'only_base': {'n': 1}, 'shared': {'v': 'child'}}
    (root / 'configs' / 'base.yaml').write_text('schema_version: 2\nonly_base: {n: 2}\nshared: {v: base}\n',
                                                encoding='utf-8')
    assert load_runtime_settings('configs/child.yaml', root)['only_base'] == {'n': 2}


def test_a_changed_value_changes_the_resolved_settings(tmp_path):
    root = tmp_path / 'project'
    (root / 'configs').mkdir(parents=True)
    (root / 'configs' / 'a.yaml').write_text('schema_version: 2\nruntime:\n  gpus: [0]\n', encoding='utf-8')
    first = load_runtime_settings('configs/a.yaml', root)
    (root / 'configs' / 'a.yaml').write_text('schema_version: 2\nruntime:\n  gpus: [1]\n', encoding='utf-8')
    assert load_runtime_settings('configs/a.yaml', root) != first


@pytest.mark.parametrize('profile', ['../outside.yaml', '.secrets/x.yaml'])
def test_a_path_outside_the_project_or_under_secrets_is_refused(tmp_path, profile):
    root = tmp_path / 'project'
    root.mkdir()
    (tmp_path / 'outside.yaml').write_text('schema_version: 2\n', encoding='utf-8')
    with pytest.raises(ConfigurationError):
        load_runtime_settings(profile, root)
