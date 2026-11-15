"""Review regressions using synthetic trees and labelled fault doubles."""

from hypothesis import given, strategies as st
import pytest

from prompt_enhancer.k0 import parse_info
from prompt_enhancer import k0_scan
from prompt_enhancer import __main__ as cli
from prompt_enhancer.storage.store import PinnedSnapshot, Writer
from prompt_enhancer.storage import _fs
from prompt_enhancer.storage._fs import FileLock
from test_k0_config import config, load
from test_k0_sync import fixture, run, tree
from test_k0_checkpoint import publish_synthetic_snapshot


@pytest.mark.parametrize('source,target,refused', [
    ('source', 'source', True), ('source', 'source/store', True),
    ('source/nested', 'source', True), ('source', 'other', False),
])
@pytest.mark.parametrize('key', ['kb_store', 'data_root'])
def test_config_path_overlap(project_root, config, source, target, refused, key):
    config['knowledge']['root'] = source
    config['storage'][key] = target
    if refused:
        with pytest.raises(ValueError, match='overlaps knowledge.root'):
            load(project_root, config)
    else:
        assert load(project_root, config)['storage'][key] == target


@given(st.text(alphabet=st.sampled_from(list('abc') + ['\x0b', '\x0c', '\x1c', '\x1d', '\x1e',
                                                      '\x85', '\u2028', '\u2029']), min_size=1))
def test_unicode_separators_are_name_data(name):
    assert parse_info(name + '\ntext', '|')['names'] == [name]


@pytest.mark.parametrize('ending', ['\n', '\r', '\r\n'])
def test_line_endings_and_bom(fixture, ending):
    store, root, folder = fixture
    (folder / 'info.txt').write_bytes(('\ufeffName' + ending + 'description').encode('utf-8'))
    first = run(store, root)
    second = run(store, root)
    assert first['rows']['folders'][0]['declared_names'] == ['Name']
    assert first['rows'] == second['rows']


@pytest.mark.parametrize('bad', [-1, 2, float('nan'), True, 'bad'])
def test_invalid_threshold_before_store_access(fixture, monkeypatch, bad):
    store, root, _ = fixture
    def fail():  # labelled I/O spy
        raise AssertionError('writer opened')
    monkeypatch.setattr(store, 'writer', fail)
    with pytest.raises(ValueError):
        run(store, root, threshold=bad)


def test_checkpoint_excludes_operation_options(fixture):
    store, root, _ = fixture
    first = run(store, root, workers=1, threshold=0.1)
    second = run(store, root, workers=2, threshold=0.9, force=True)
    assert first['checkpoint'] == second['checkpoint']


def test_checkpoint_reused_for_linked_root_spelling(fixture):
    import os
    store, root, _ = fixture
    link = root.parent / 'checkpoint-root-link'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(root), str(link))
    else:
        try:
            link.symlink_to(root, target_is_directory=True)
        except OSError as error:
            pytest.skip(f'synthetic symlink creation unavailable: {error}')
    try:
        direct = run(store, root)
        linked = run(store, link)
        assert direct['checkpoint'] == linked['checkpoint']
        assert direct['report']['configured_root'] != linked['report']['configured_root']
        assert direct['report']['resolved_root'] == linked['report']['resolved_root']
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()


def test_lock_acquisition_interrupt_closes_handle(tmp_path, monkeypatch):
    import os
    lock = FileLock(tmp_path / 'interrupt.lock', 1, 0.01)
    opened = []
    real_open = open
    def observe(*args, **kwargs):  # labelled handle-observation double
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle
    def interrupt(*args):  # labelled OS-lock interrupt double
        raise KeyboardInterrupt
    monkeypatch.setattr(_fs, 'open', observe, raising=False)
    monkeypatch.setattr(_fs.msvcrt if os.name == 'nt' else _fs.fcntl,
                        'locking' if os.name == 'nt' else 'flock', interrupt)
    with pytest.raises(KeyboardInterrupt):
        lock.try_acquire()
    assert opened and opened[0].closed
    assert not lock.held


def test_writer_acquisition_interrupt_releases_retained_writer(fixture, monkeypatch):
    store, _, _ = fixture
    writer = store.writer()
    def interrupt(self):  # labelled recovery-interrupt double
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(Writer, '_recover', interrupt)
        with pytest.raises(KeyboardInterrupt):
            writer.__enter__()
    try:
        assert not writer._lock.held
        with store.writer():
            pass
    finally:
        writer._lock.release()


@pytest.mark.parametrize('filename', ['.jpg', '.JPG', 'notes.txt'])
def test_ignored_file_is_reported(fixture, filename):
    store, root, folder = fixture
    (folder / filename).write_bytes(b'ignored')
    assert f'folder/{filename}' in run(store, root, plan=True)['report']['ignored']


def test_zero_file_id_has_precise_failure(fixture, monkeypatch):
    store, root, folder = fixture
    original = k0_scan._lstat
    def missing_id(path, parent, name):  # labelled filesystem double
        value = original(path, parent, name)
        if path == folder / 'image.jpg':
            class StatDouble:
                st_ino = 0
                def __getattr__(self, attr):
                    return getattr(value, attr)
            return StatDouble()
        return value
    monkeypatch.setattr(k0_scan, '_lstat', missing_id)
    with pytest.raises(k0_scan.MissingFileID, match='file-ID support'):
        run(store, root, plan=True)


@pytest.mark.parametrize('force,expected', [(False, 1), (True, 0)])
def test_cli_plan_guard_exit(fixture, monkeypatch, capsys, force, expected):
    store, root, folder = fixture
    publish_synthetic_snapshot(store, root)
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    monkeypatch.setattr(cli, 'load_config', lambda *args, **kwargs: {})
    monkeypatch.setattr(cli, 'sync_config', lambda config, **kwargs: run(store, root, plan=True, force=force))
    args = ['kb', 'sync', '--config', 'synthetic', '--plan'] + (['--force'] if force else [])
    assert cli.main(args) == expected
    assert '"exceeded": true' in capsys.readouterr().out


@pytest.mark.parametrize('phase', ['snapshot', 'writer'])
def test_interrupt_during_ownership_setup_releases_lock(fixture, monkeypatch, phase):
    store, root, _ = fixture
    publish_synthetic_snapshot(store, root)
    if phase == 'snapshot':
        def interrupt(*args, **kwargs):  # labelled interrupt double
            raise KeyboardInterrupt
        monkeypatch.setattr(PinnedSnapshot, '__init__', interrupt)
        with pytest.raises(KeyboardInterrupt):
            store.pin()
        assert not list(store.root.rglob('*.lease'))
    else:
        def interrupt(*args, **kwargs):  # labelled interrupt double
            raise KeyboardInterrupt
        with monkeypatch.context() as patch:
            patch.setattr(Writer, '_recover', interrupt)
            with pytest.raises(KeyboardInterrupt):
                with store.writer():
                    pass
        with store.writer():
            pass


@pytest.mark.parametrize('key,value', [
    ('info_names', []), ('encoding', 'no-such-codec'), ('extensions', []),
    ('separator', ''), ('workers', 0), ('threshold', 2),
])
def test_invalid_scan_setting_before_io(fixture, monkeypatch, key, value):
    store, root, _ = fixture
    def fail(*args, **kwargs):  # labelled operational I/O spy
        raise AssertionError('operational I/O occurred')
    monkeypatch.setattr(store, 'writer', fail)
    monkeypatch.setattr(k0_scan, '_opened', fail)
    with pytest.raises(ValueError):
        run(store, root, **{key: value})


def test_linked_root_is_resolved_and_reported(fixture):
    import os
    store, root, _ = fixture
    link = root.parent / 'root-link'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(root), str(link))
    else:
        link.symlink_to(root, target_is_directory=True)
    try:
        result = run(store, link, plan=True)
        assert result['plan']['folders']['added'] == ['folder']
        assert result['report']['configured_root'] == str(link.absolute())
        assert result['report']['resolved_root'] == str(root.resolve())
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.parametrize('key', ['kb_store', 'data_root'])
@pytest.mark.parametrize('direction', ['storage_inside_source', 'source_inside_storage'])
def test_linked_storage_overlap_refused(project_root, tmp_path, config, key, direction):
    import os
    storage = tmp_path / 'storage-root'
    source = tmp_path / 'source'
    link = tmp_path / 'overlap-link'
    config['knowledge']['root'] = source.relative_to(project_root).as_posix()
    if direction == 'storage_inside_source':
        source.mkdir()
        target = source / 'inside'
        target.mkdir()
        config['storage'][key] = link.relative_to(project_root).as_posix()
    else:
        storage.mkdir()
        source = storage / 'inside'
        source.mkdir()
        target = source
        config['knowledge']['root'] = link.relative_to(project_root).as_posix()
        config['storage'][key] = storage.relative_to(project_root).as_posix()
    marker = source / 'evidence.bin'
    marker.write_bytes(b'synthetic knowledge-base bytes')
    before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f'synthetic symlink creation unavailable: {error}')
    try:
        with pytest.raises(ValueError, match=rf'storage\.{key} overlaps knowledge\.root'):
            load(project_root, config)
        after = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        assert after == before
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.parametrize('lock_held', [True, False])
def test_gc_lease_lock_liveness_double(fixture, monkeypatch, lock_held):
    store, root, _ = fixture
    snapshot = publish_synthetic_snapshot(store, root)
    path = store.root / 'leases' / snapshot / ('f' * 32 + '.lease')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')  # labelled legacy lease double; no process identity is recorded
    real = FileLock.try_acquire
    def probe(self):  # labelled live/dead lock-state double
        return not lock_held if self.path.suffix == '.lease' else real(self)
    monkeypatch.setattr(FileLock, 'try_acquire', probe)
    with store.writer() as writer:
        report = writer.gc()
    assert path.exists() is lock_held
    assert (snapshot in report.leased) is lock_held
