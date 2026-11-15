"""Synthetic snapshots and labelled interruption doubles for K0 checkpoint integration."""

import base64
import json
import os

import pytest
from storage_fixtures import DIM, make_store

from prompt_enhancer import k0_scan
from prompt_enhancer.k0 import rows_from_scan
from prompt_enhancer.k0_checkpoint import _REVISION, load_scan_checkpoint, scan_to_checkpoint
from prompt_enhancer.storage import SNAPSHOT_TABLES, SPECS, StorageError
from prompt_enhancer.storage.store import Checkpoints


@pytest.fixture
def setup_tree(project_root, tmp_path):
    root = tmp_path / 'source'
    folder = root / 'folder'
    folder.mkdir(parents=True)
    (folder / 'info.txt').write_text('Kimono\nhand written', encoding='utf-8')
    (folder / 'image.jpg').write_bytes(b'image one')
    store = make_store(project_root, tmp_path / 'kb')
    return store, root, folder


def run(store, root):
    return scan_to_checkpoint(store, root, info_names=['info.txt'], encoding='utf-8',
                              extensions=['.jpg'], separator='|')


def publish_synthetic_snapshot(store, root):
    """Test double: explicit empty K1-K5 tables in a synthetic snapshot."""
    source = k0_scan.scan_source(root, info_names=['info.txt'], encoding='utf-8',
                                extensions=['.jpg'], separator='|', previous={})
    k0_rows = rows_from_scan(source['folders'], '|')
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        for name in SNAPSHOT_TABLES:
            builder.write_table(name, k0_rows.get(name, []), dim=DIM if SPECS[name].vector else None)
        sid = builder.finish(source_scan_summary={'fixture': 'synthetic downstream test doubles'})
        writer.publish(sid)
    return sid


@pytest.mark.parametrize('interrupt_at', [1, 2, 3, 4, 5])
def test_interrupted_checkpoint_resumes_and_converges(setup_tree, monkeypatch, interrupt_at):
    store, root, _ = setup_tree
    original = Checkpoints.commit
    calls = []

    def interrupt_after_commit(self, *args, **kwargs):  # labelled interruption double
        result = original(self, *args, **kwargs)
        calls.append(args[1])
        if len(calls) == interrupt_at:
            raise RuntimeError('simulated interruption')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', interrupt_after_commit)
        with pytest.raises(RuntimeError, match='simulated interruption'):
            run(store, root)
    assert store.current() is None
    resumed = run(store, root)
    assert run(store, root) == resumed
    assert store.current() is None


def test_unchanged_rerun_creates_no_checkpoints(setup_tree, monkeypatch):
    store, root, _ = setup_tree
    first = run(store, root)
    original = Checkpoints.commit
    writes = []
    original_read = k0_scan.os.read
    reads = []

    def record_commit(self, *args, **kwargs):  # labelled observation double
        writes.append(args[1])
        return original(self, *args, **kwargs)

    def record_read(fd, count):  # labelled observation double
        reads.append(fd)
        return original_read(fd, count)

    monkeypatch.setattr(Checkpoints, 'commit', record_commit)
    monkeypatch.setattr(k0_scan.os, 'read', record_read)
    assert run(store, root) == first
    assert load_scan_checkpoint(store, first['key']) == first
    assert writes == []
    assert reads == []
    assert store.current() is None


def test_unreadable_info_keeps_exact_rows_from_current(setup_tree, monkeypatch):
    store, root, _ = setup_tree
    sid = publish_synthetic_snapshot(store, root)
    with store.pin() as snapshot:
        expected = {name: snapshot.read_rows(name) for name in
                    ('folders', 'concepts', 'folder_concepts', 'images', 'folder_images')}
    original = k0_scan._native_open

    def deny_info(path, parent_fd, name, directory):  # labelled access-failure double
        if name == 'info.txt':
            raise PermissionError('simulated denial')
        return original(path, parent_fd, name, directory)

    monkeypatch.setattr(k0_scan, '_native_open', deny_info)
    result = run(store, root)
    assert result['report']['unreadable'] == ['folder/info.txt']
    assert result['rows'] == expected
    assert store.current()[0] == sid


def test_changed_stat_rehashes_and_corrupt_cache_is_rebuilt(setup_tree, monkeypatch):
    store, root, folder = setup_tree
    original = Checkpoints.commit
    recorded = []

    def record(self, *args, **kwargs):  # labelled observation double
        path = original(self, *args, **kwargs)
        recorded.append((args[1], path))
        return path

    monkeypatch.setattr(Checkpoints, 'commit', record)
    first = run(store, root)
    cache_path = next(path for kind, path in recorded if kind == 'k0_file')
    store.path(cache_path).write_bytes(b'corrupt')
    recorded.clear()
    rebuilt = run(store, root)
    assert rebuilt == first
    assert [kind for kind, _ in recorded] == ['k0_file']
    recorded.clear()
    (folder / 'image.jpg').write_bytes(b'a changed image with a different size')
    changed = run(store, root)
    assert changed['key'] != first['key']
    assert [kind for kind, _ in recorded].count('k0_file') == 1
    assert store.current() is None


def test_identical_content_is_checkpointed_once(setup_tree, monkeypatch):
    store, root, folder = setup_tree
    other = root / 'other'
    other.mkdir()
    (other / 'same.jpg').write_bytes((folder / 'image.jpg').read_bytes())
    original = Checkpoints.commit
    kinds = []

    def record(self, *args, **kwargs):  # labelled observation double
        kinds.append(args[1])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Checkpoints, 'commit', record)
    result = run(store, root)
    assert len(result['rows']['images']) == 1
    assert len(result['rows']['folder_images']) == 2
    assert kinds.count('k0_content') == 2  # one info, one shared image
    assert kinds.count('k0_file') == 3


def test_cache_write_failure_is_not_reported_as_source_failure(setup_tree, monkeypatch):
    store, root, _ = setup_tree

    def fail(*args, **kwargs):  # labelled storage-failure double
        raise OSError('simulated storage error')

    monkeypatch.setattr(Checkpoints, 'commit', fail)
    with pytest.raises(k0_scan.CacheError, match='checkpoint cache write failed'):
        run(store, root)
    assert store.current() is None


def test_unreadable_image_keeps_snapshot_rows(setup_tree, monkeypatch):
    store, root, _ = setup_tree
    sid = publish_synthetic_snapshot(store, root)
    with store.pin() as snapshot:
        expected = {name: snapshot.read_rows(name) for name in
                    ('folders', 'concepts', 'folder_concepts', 'images', 'folder_images')}
    original = k0_scan._native_open

    def deny_image(path, parent_fd, name, directory):  # labelled access-failure double
        if name == 'image.jpg':
            raise PermissionError('simulated denial')
        return original(path, parent_fd, name, directory)

    monkeypatch.setattr(k0_scan, '_native_open', deny_image)
    result = run(store, root)
    assert result['rows'] == expected
    assert result['report']['unreadable'] == ['folder/image.jpg']
    assert store.current()[0] == sid


def test_corrupt_content_checkpoint_is_rebuilt(setup_tree, monkeypatch):
    store, root, _ = setup_tree
    original = Checkpoints.commit
    paths = []

    def record(self, *args, **kwargs):  # labelled observation double
        result = original(self, *args, **kwargs)
        paths.append((args[1], result))
        return result

    monkeypatch.setattr(Checkpoints, 'commit', record)
    first = run(store, root)
    content_path = next(path for kind, path in paths if kind == 'k0_content')
    store.path(content_path).write_bytes(b'broken')
    paths.clear()
    assert run(store, root) == first
    assert [kind for kind, _ in paths] == ['k0_content']


def test_mtime_change_rehashes_without_duplicating_content(setup_tree, monkeypatch):
    store, root, folder = setup_tree
    first = run(store, root)
    image = folder / 'image.jpg'
    before = image.stat()
    os.utime(image, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    original = Checkpoints.commit
    kinds = []

    def record(self, *args, **kwargs):  # labelled observation double
        kinds.append(args[1])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Checkpoints, 'commit', record)
    changed = run(store, root)
    assert changed['rows']['images'] == first['rows']['images']
    assert kinds.count('k0_file') == 1
    assert 'k0_content' not in kinds


def test_semantically_corrupt_cached_info_is_rebuilt(setup_tree, monkeypatch):
    store, root, _ = setup_tree
    original = Checkpoints.commit
    content_keys = []

    def record(self, *args, **kwargs):  # labelled observation double
        result = original(self, *args, **kwargs)
        if args[1] == 'k0_content':
            content_keys.append(args[0])
        return result

    monkeypatch.setattr(Checkpoints, 'commit', record)
    first = run(store, root)
    with store.writer() as writer:
        checkpoints = writer.checkpoints('k0_content')
        for key in list(content_keys):
            data = json.loads(checkpoints.load(key))
            if data['encoding'] == 'utf-8':
                data['raw'] = base64.b64encode(b'wrong').decode('ascii')
                checkpoints.commit(key, 'k0_content', json.dumps(data).encode(), revisions=_REVISION)
                break
        else:
            pytest.fail('info content checkpoint was not written')
    assert run(store, root) == first


def test_resume_false_recomputes_in_new_session(project_root, tmp_path, monkeypatch):
    root = tmp_path / 'source'
    folder = root / 'folder'
    folder.mkdir(parents=True)
    (folder / 'image.jpg').write_bytes(b'image')
    store = make_store(project_root, tmp_path / 'kb', resume=False)
    first = run(store, root)
    original = Checkpoints.commit
    writes = []
    original_read = k0_scan.os.read
    reads = []

    def record(self, *args, **kwargs):  # labelled observation double
        writes.append(args[1])
        return original(self, *args, **kwargs)

    def record_read(fd, count):  # labelled observation double
        reads.append(fd)
        return original_read(fd, count)

    monkeypatch.setattr(Checkpoints, 'commit', record)
    monkeypatch.setattr(k0_scan.os, 'read', record_read)
    assert run(store, root) == first
    assert reads  # T-006 deliberately refuses cross-session resume when disabled.
    assert writes == []  # Recomputed identical bytes do not need another checkpoint.


def test_corrupt_scan_result_refused_then_rebuilt(setup_tree, monkeypatch):
    store, root, _ = setup_tree
    original = Checkpoints.commit
    scan_paths = []

    def record(self, *args, **kwargs):  # labelled observation double
        path = original(self, *args, **kwargs)
        if args[1] == 'k0_scan':
            scan_paths.append(path)
        return path

    monkeypatch.setattr(Checkpoints, 'commit', record)
    first = run(store, root)
    store.path(scan_paths[0]).write_bytes(b'broken')
    with pytest.raises(StorageError):
        load_scan_checkpoint(store, first['key'])
    assert run(store, root) == first
    assert load_scan_checkpoint(store, first['key']) == first
