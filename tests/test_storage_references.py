"""Public live-reference reader: synthetic snapshots, no model or knowledge-base access."""

from contextlib import contextmanager
from datetime import timedelta
import hashlib

from hypothesis import given, settings, strategies as st
import pytest

from storage_fixtures import T0, build_snapshot, make_store
from prompt_enhancer.storage import StorageError, artifact_key

REV = {'model': 'none', 'prompt': 'none', 'schema': 'reference-test'}


def referenced_snapshot(store, tag, *, publish=True):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('k2_draw')
        key = artifact_key('k2_draw', tag, REV)
        path = checkpoints.commit(key, 'k2_draw', tag.encode(), revisions=REV)
        other = writer.checkpoints('other').commit(artifact_key('other', tag, REV), 'other', b'other', revisions=REV)
        # build_snapshot has no artifacts; make our own builder to preserve both kinds in the manifest.
        from storage_fixtures import dataset
        from prompt_enhancer.storage import SPECS
        builder = writer.begin_snapshot()
        for name, rows in dataset(tag).items():
            builder.write_table(name, rows, dim=4 if SPECS[name].vector else None)
        builder.reference_artifacts([path, other])
        sid = builder.finish()
        if publish:
            writer.publish(sid)
    return sid, path


def test_referenced_artifacts_are_grouped_newest_first_with_hash_and_size(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'store')
    store.clock = lambda: T0.timestamp()
    older, first = referenced_snapshot(store, 'old')
    store.clock = lambda: (T0 + timedelta(seconds=10)).timestamp()
    newer, second = referenced_snapshot(store, 'new')
    registry = store._registry
    monkeypatch.setattr(store, '_registry', lambda: dict(reversed(list(registry().items()))))
    result = store.referenced_artifacts('k2_draw')
    assert [item['snapshot_id'] for item in result] == [newer, older]
    assert [item['created_at'] for item in result] == [T0 + timedelta(seconds=10), T0]
    for group, path in zip(result, [second, first]):
        assert len(group['artifacts']) == 1
        entry = group['artifacts'][0]
        data = store.path(path).read_bytes()
        assert (entry['path'], entry['sha256'], entry['size_bytes']) == (path, hashlib.sha256(data).hexdigest(), len(data))
    assert all(not group['artifacts'] for group in store.referenced_artifacts('absent_kind'))


def test_referenced_artifact_reader_is_read_only_and_holds_lock_only_for_read(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'store')
    referenced_snapshot(store, 'one')
    before = {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    real_lock, registry, manifest = store.store_lock, store._registry, store._manifest
    locked = []
    calls = []

    @contextmanager
    def lock():
        with real_lock():
            locked.append(True)
            calls.append('lock')
            try:
                yield
            finally:
                locked.pop()

    def read_registry():
        assert locked
        calls.append('registry')
        return registry()

    def read_manifest(*args):
        assert locked
        calls.append('manifest')
        assert args[1] is not None
        return manifest(*args)

    monkeypatch.setattr(store, 'store_lock', lock)
    monkeypatch.setattr(store, '_registry', read_registry)
    monkeypatch.setattr(store, '_manifest', read_manifest)
    assert store.referenced_artifacts('k2_draw')
    assert not locked and calls == ['lock', 'registry', 'manifest']
    assert before == {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob('*') if p.is_file()}


def test_referenced_artifact_reader_skips_nonlive_and_refuses_corrupt_live_manifest(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'store')
    sid, _ = referenced_snapshot(store, 'live')
    with store.writer() as writer:
        failed = build_snapshot(writer, 'failed')
    with store.store_lock():
        rows = store._registry()
        rows[failed]['state'] = 'failed'
        store._write_registry(rows)
    store.path(f'snapshots/{failed}/MANIFEST.parquet').unlink()
    assert [group['snapshot_id'] for group in store.referenced_artifacts('k2_draw')] == [sid]
    live = store.path(f'snapshots/{sid}/MANIFEST.parquet')
    live.write_bytes(live.read_bytes() + b' ')
    with pytest.raises(StorageError, match='manifest checksum'):
        store.referenced_artifacts('k2_draw')


@given(st.sampled_from(['', '../escape', 'UPPER', 'a/b', 'a\\b', None, 3]))
@settings(max_examples=7)
def test_referenced_artifact_reader_refuses_invalid_kind(kind):
    from pathlib import Path
    store = make_store(Path.cwd(), Path.cwd() / '.tmp' / 'unused-reference-reader')
    with pytest.raises(StorageError, match='invalid artifact kind'):
        store.referenced_artifacts(kind)


def test_referenced_artifact_reader_does_not_create_missing_store(project_root, tmp_path):
    path = tmp_path / 'absent'
    store = make_store(project_root, path)
    assert store.referenced_artifacts('k2_draw') == []
    assert not path.exists()
