"""Pinned audit output, deterministic samples and filesystem isolation on synthetic snapshots."""

from copy import deepcopy
import json

import pytest
import yaml

from prompt_enhancer import __main__ as cli
from prompt_enhancer.kb_audit import audit
from prompt_enhancer.storage import SPECS
from prompt_enhancer.storage.store import PinnedSnapshot
from prompt_enhancer.storage.store import StorageWarning
from storage_fixtures import DIM, dataset, make_store


def published(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'audit-store')
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        rows = dataset('audit')
        for row in rows['folders']:
            row['name_source'] = 'model'
        for name, data in rows.items():
            builder.write_table(name, data, dim=DIM if SPECS[name].vector else None)
        sid = builder.finish()
        writer.publish(sid)
    return store, sid


def tree(path):
    # Windows leases hold a byte-range lock; their names/stat and continued pin use prove preservation.
    files = {item.relative_to(path).as_posix(): (item.stat().st_size, item.stat().st_mtime_ns,
             None if item.suffix == '.lease' else item.read_bytes()) for item in path.rglob('*') if item.is_file()}
    return {'files': files, 'dirs': sorted(item.relative_to(path).as_posix()
            for item in path.rglob('*') if item.is_dir())}


def test_audit_printable_sample_caps_counts_and_restores_store(project_root, tmp_path):
    store, sid = published(project_root, tmp_path)
    before = tree(store.root)
    first = audit(store, folder_count=100, card_count=100, seed=4)
    assert first['status'] == 'OK' and first['snapshot_id'] == sid
    assert first['actual'] == {'folders': 3, 'cards': 4}
    assert first['requested'] == {'folders': 100, 'cards': 100}
    assert first['folders'][0]['concepts'] and first['cards'][0]['summary']
    assert tree(store.root) == before
    assert audit(store, folder_count=100, card_count=100, seed=4) == first
    assert audit(store, folder_count=100, card_count=100, seed=5)['cards'] != first['cards']
    zero = audit(store, folder_count=0, card_count=0, seed=4)
    assert zero['folders'] == zero['cards'] == []


@pytest.mark.parametrize(('key', 'value'), [('folder_count', -1), ('card_count', -1), ('seed', True),
                                           ('folder_count', True), ('card_count', 1.0), ('seed', '4')])
def test_invalid_audit_arguments_refused_without_store(project_root, tmp_path, key, value):
    store = make_store(project_root, tmp_path / 'absent')
    args = {'folder_count': 1, 'card_count': 1, 'seed': 1}
    args[key] = value
    with pytest.raises(ValueError, match=key):
        audit(store, **args)
    assert not store.root.exists()


def test_missing_store_stays_missing(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'absent')
    assert audit(store, folder_count=1, card_count=1, seed=2)['status'] == 'SNAPSHOT_MISSING'
    assert not store.root.exists()


def test_audit_cleanup_failure_is_reported(project_root, tmp_path, monkeypatch):
    store, _ = published(project_root, tmp_path)
    original = store._remove
    def blocked(relative, **kwargs):
        if kwargs.get('directory') and relative.startswith('leases/'):
            raise PermissionError('labelled cleanup refusal')
        return original(relative, **kwargs)
    monkeypatch.setattr(store, '_remove', blocked)
    with pytest.warns(StorageWarning, match='labelled cleanup refusal'):
        result = audit(store, folder_count=1, card_count=1, seed=1)
    assert result['status'] == 'OK'


def test_audit_error_releases_pin_and_preserves_other_reader(project_root, tmp_path, monkeypatch):
    store, _ = published(project_root, tmp_path)
    with store.pin() as other:
        before = tree(store.root)
        with monkeypatch.context() as patch:
            patch.setattr(PinnedSnapshot, 'read_rows', lambda *args: (_ for _ in ()).throw(ValueError('labelled failure')))
            with pytest.raises(ValueError, match='labelled failure'):
                audit(store, folder_count=1, card_count=1, seed=1)
        assert tree(store.root) == before
        assert other.read_rows('cards')


def test_audit_remains_pinned_when_current_changes(project_root, tmp_path, monkeypatch):
    store, sid = published(project_root, tmp_path)
    original = PinnedSnapshot.read_rows
    changed = False
    def read(self, name):
        nonlocal changed
        if not changed:
            changed = True
            with store.writer() as writer:
                builder = writer.begin_snapshot()
                for table, rows in dataset('changed').items():
                    builder.write_table(table, rows, dim=DIM if SPECS[table].vector else None)
                writer.publish(builder.finish())
        return original(self, name)
    monkeypatch.setattr(PinnedSnapshot, 'read_rows', read)
    result = audit(store, folder_count=1, card_count=1, seed=4)
    assert result['snapshot_id'] == sid and result['cards'][0]['summary'] == 'card audit'


def test_cli_audit_prints_sample_without_source_or_models(k2_env, monkeypatch, capsys):
    store, sid = published(k2_env.root, k2_env.source.parent)
    config = deepcopy(k2_env.config)
    config['storage']['kb_store'] = store.root.relative_to(k2_env.root).as_posix()
    config['knowledge']['root'] = 'not-present'
    config['cards']['min_visible_images'] = 'fit_from_gt'
    config['vlm']['roles']['card_builder']['max_tokens'] = 'design_required'
    path = k2_env.root / 'audit-profile.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setattr(cli, '__file__', str(k2_env.root / 'src/prompt_enhancer/__main__.py'))
    def forbidden(*args, **kwargs):
        raise AssertionError('audit must not compose a model or read knowledge files')
    monkeypatch.setattr('prompt_enhancer.model_client.ModelClientRun.__init__', forbidden)
    monkeypatch.setattr('prompt_enhancer.k0_scan.read_source_file', forbidden)
    assert cli.main(['kb', 'audit', '--config', str(path), '--folder-count', '1', '--card-count', '1', '--seed', '7']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['snapshot_id'] == sid and result['actual'] == {'folders': 1, 'cards': 1}
    with pytest.raises(SystemExit):
        cli.main(['kb', 'audit', '--config', str(path)])


def test_the_snapshot_pin_helper_has_a_public_name_and_no_module_imports_a_private_name_from_kb_audit(
        project_root, tmp_path):
    """P3-SWEEP-2 item 8: `cli_commands` used the private `_audit_snapshot`; callers take the public name."""
    import ast
    from pathlib import Path

    import prompt_enhancer
    from prompt_enhancer import kb_audit

    assert not hasattr(kb_audit, '_audit_snapshot')
    store, sid = published(project_root, tmp_path)
    with kb_audit.audit_snapshot(store) as pinned:
        assert pinned.snapshot_id == sid
    private = []
    for path in sorted(Path(prompt_enhancer.__file__).parent.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[-1] == 'kb_audit':
                private += [f'{path.name}:{alias.name}' for alias in node.names if alias.name.startswith('_')]
    assert private == []
