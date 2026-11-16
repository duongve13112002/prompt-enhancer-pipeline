"""T-006 race fix round 3 (R-007, R-014, R-017, R-025): every store mutation is anchored.

Retest 2 found that only lease and snapshot removals were anchored: every other delete, write, rename and create
re-resolved its name, so an ancestor directory swapped for a junction after the path check made the store delete,
overwrite or create a file outside its root (P1-A, P1-B, P3-A). The regressions below swap a real ancestor for a
real link at the last boundary before the act and assert the outside file survives and the act is refused. The
schedule hook is the only double: on round 2 that boundary is the name-based act (`store._unlink`,
`_fs._temporary`, `store.path` before the lock-file open), on round 3 the anchored operation's own name check.

Links are junctions on Windows (no privilege needed) and symlinks on POSIX. POSIX-only tests run on Linux CI.
"""

import ast
from datetime import datetime, timedelta, timezone
import errno
import importlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
import warnings

import pyarrow as pa
import pytest
from storage_fixtures import DIM, FOLDERS, dataset, make_store, publish
from test_storage_anchored import REVISION, _dir_link, _drop_link, _is_link

import prompt_enhancer
from prompt_enhancer.storage import SPECS, DataStore, StorageError, artifact_key
from prompt_enhancer.storage import store as store_module
from prompt_enhancer.storage.identity import artifact_path

anchored = importlib.import_module('prompt_enhancer.storage._anchored')
_fs = importlib.import_module('prompt_enhancer.storage._fs')
ROUND_3 = hasattr(anchored, 'make_directory')  # the anchored write layer; absent in the round 2 tree
POSIX_ONLY = pytest.mark.skipif(os.name == 'nt', reason='POSIX dir_fd branch: runs on Linux CI')
WINDOWS_ONLY = pytest.mark.skipif(os.name != 'nt', reason='Windows handle behaviour')
SRC = str(Path(prompt_enhancer.__file__).resolve().parents[1])


def _refusal(excinfo):
    assert type(excinfo.value) is StorageError, repr(excinfo.value)


class Swap:
    """Move a store directory aside and put a link to an outside directory in its place, once; undo it."""

    def __init__(self, store_root, relative, outside):
        self.place, self.moved, self.outside = store_root / relative, store_root / 'displaced', outside
        self.done = False

    def __call__(self):
        if not self.done:
            self.done = True
            self.place.rename(self.moved)
            _dir_link(self.place, self.outside)

    def undo(self):
        if self.done and _is_link(self.place):
            _drop_link(self.place)
            self.moved.rename(self.place)


def arm(monkeypatch, store, match, swap, kind):
    """Fire `swap` right after the last name check before the act on a store-relative name `match` accepts.

    Scheduling hook only (the code that acts is unmodified). Round 3 checks inside the anchored operation; round
    2 acted by name after `store.path`, so its boundary is the name-based act itself."""
    root = store.root

    def fire(relative):
        if match(relative):
            swap()

    if ROUND_3:
        real = anchored.real_inside

        def checked(check_root, relative):
            result = real(check_root, relative)
            fire(relative)
            return result
        monkeypatch.setattr(anchored, 'real_inside', checked)
    elif kind == 'delete':
        real = store_module._unlink

        def unlink(path):
            fire(Path(path).relative_to(root).as_posix())
            return real(path)
        monkeypatch.setattr(store_module, '_unlink', unlink)
    elif kind == 'write':
        real = _fs._temporary

        def temporary(path):
            fire(Path(path).relative_to(root).as_posix())
            return real(path)
        monkeypatch.setattr(_fs, '_temporary', temporary)
    else:  # 'create': the lease file is opened by name after `store.path` accepted it
        real = store.path

        def path(relative):
            result = real(relative)
            fire(relative)
            return result
        monkeypatch.setattr(store, 'path', path)


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / 'outside'
    folder.mkdir()
    yield folder
    for path in tmp_path.rglob('*'):  # never leave a link behind; remove links as links
        if _is_link(path):
            _drop_link(path)


def _victim(outside, name, data=b'must survive'):
    victim = outside / name
    victim.write_bytes(data)
    return victim


def _outcome(call):
    try:
        call()
    except StorageError as error:
        return error
    return None


# ----------------------------------------------------------------------------- P1-A: deletes (D1, D4, D5, hold)


def test_d1_registry_sweep_never_deletes_outside(project_root, tmp_path, monkeypatch, outside):
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    stale = 'snapshots-' + '0' * 20 + '-' + '0' * 8 + '-' + 'a' * 8 + '.parquet'
    (store.root / 'registry' / stale).write_bytes(b'stale')
    victim = _victim(outside, stale)
    swap = Swap(store.root, 'registry', outside)
    arm(monkeypatch, store, lambda relative: relative == f'registry/{stale}', swap, 'delete')
    try:
        error = _outcome(lambda: store._sweep_registry(store._registry_name()))
    finally:
        monkeypatch.undo()
        swap.undo()
    assert swap.done and victim.read_bytes() == b'must survive'
    assert type(error) is StorageError


@pytest.mark.parametrize('path', ['release_hold', 'expired_hold_in_gc'])
def test_hold_removal_never_deletes_outside(project_root, tmp_path, monkeypatch, outside, path):
    store = make_store(project_root, tmp_path / 'kb', keep=5, retention_s=10 ** 9)
    sid = publish(store)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1 if path == 'release_hold' else -1)
    store.hold(sid, 'req1', expiry)
    held = next((store.root / 'holds' / sid).iterdir())
    victim = outside / held.name
    shutil.copy(held, victim)
    original = victim.read_bytes()
    swap = Swap(store.root, f'holds/{sid}', outside)
    arm(monkeypatch, store, lambda relative: relative == f'holds/{sid}/{held.name}', swap, 'delete')
    try:
        if path == 'release_hold':
            error = _outcome(lambda: store.release_hold(sid, 'req1'))
        else:
            with store.writer() as writer:
                error = _outcome(writer.gc)
                monkeypatch.undo()
                swap.undo()
    finally:
        monkeypatch.undo()
        swap.undo()
    assert swap.done and victim.read_bytes() == original
    assert type(error) is StorageError


def test_d4_state_rewrite_never_deletes_outside(project_root, tmp_path, monkeypatch, outside):
    """Any segment name: an in-store file of the victim's name that parses as sync_state."""
    store = make_store(project_root, tmp_path / 'kb')
    victim = _victim(outside, 'zz-victim.parquet', b'user data that must survive')
    swap = Swap(store.root, 'registry/sync_state', outside)
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K3')
        for index in range(2):
            checkpoints.commit(artifact_key('card', 'sha256:' + str(index) * 64, REVISION), 'card', b'x',
                               revisions=REVISION)
        segments = sorted((store.root / 'registry' / 'sync_state').iterdir())
        shutil.copy(segments[0], store.root / 'registry' / 'sync_state' / victim.name)
        arm(monkeypatch, store, lambda relative: relative.endswith(victim.name), swap, 'delete')
        try:
            error = _outcome(writer._compact_state)
        finally:
            monkeypatch.undo()
            swap.undo()
    assert swap.done and victim.read_bytes() == b'user data that must survive'
    assert type(error) is StorageError


@pytest.mark.parametrize('kind', ['artifact', 'tmp'])
def test_d5_artifact_gc_never_deletes_outside(project_root, tmp_path, monkeypatch, outside, kind):
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=0)
    with store.writer() as writer:
        key = artifact_key('card', 'sha256:' + '3' * 64, REVISION)
        relative = writer.checkpoints('K3').commit(key, 'card', b'unreferenced', revisions=REVISION)
        if kind == 'tmp':
            relative = relative.rsplit('/', 1)[0] + '/.junk.tmp'
            (store.root / relative).write_bytes(b'junk')
        victim = _victim(outside, relative.rsplit('/', 1)[1])
        swap = Swap(store.root, relative.rsplit('/', 1)[0], outside)
        arm(monkeypatch, store, lambda name: name == relative, swap, 'delete')
        try:
            error = _outcome(writer.gc)
        finally:
            monkeypatch.undo()
            swap.undo()
    assert swap.done and victim.read_bytes() == b'must survive'
    assert type(error) is StorageError


# ----------------------------------------------------------------------------- P1-B: overwrites (retest 2 W1, W2, W3)


def test_w1_registry_pointer_never_overwrites_outside(project_root, tmp_path, monkeypatch, outside):
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    victim = _victim(outside, 'REGISTRY')
    swap = Swap(store.root, 'registry', outside)
    with store.writer() as writer:
        arm(monkeypatch, store, lambda relative: relative == 'registry/REGISTRY', swap, 'write')
        try:
            error = _outcome(writer.begin_snapshot)
        finally:
            monkeypatch.undo()
            swap.undo()
    assert swap.done and victim.read_bytes() == b'must survive'
    assert sorted(path.name for path in outside.iterdir()) == ['REGISTRY']  # nothing else written there
    assert type(error) is StorageError


def test_w2_hold_never_overwrites_outside(project_root, tmp_path, monkeypatch, outside):
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store)
    name = f'{store_module.identity_key("req2")}.parquet'
    victim = _victim(outside, name)
    (store.root / 'holds' / sid).mkdir(parents=True, exist_ok=True)
    swap = Swap(store.root, f'holds/{sid}', outside)
    arm(monkeypatch, store, lambda relative: relative == f'holds/{sid}/{name}', swap, 'write')
    try:
        error = _outcome(lambda: store.hold(sid, 'req2', datetime.now(timezone.utc) + timedelta(hours=1)))
    finally:
        monkeypatch.undo()
        swap.undo()
    assert swap.done and victim.read_bytes() == b'must survive'
    assert sorted(path.name for path in outside.iterdir()) == [name]
    assert type(error) is StorageError


def test_w3_artifact_never_overwrites_outside(project_root, tmp_path, monkeypatch, outside):
    store = make_store(project_root, tmp_path / 'kb')
    key = artifact_key('card', 'sha256:' + '4' * 64, REVISION)
    relative = artifact_path('card', key, '.bin')
    (store.root / relative).parent.mkdir(parents=True)
    victim = _victim(outside, relative.rsplit('/', 1)[1])
    swap = Swap(store.root, relative.rsplit('/', 1)[0], outside)
    with store.writer() as writer:
        arm(monkeypatch, store, lambda name: name == relative, swap, 'write')
        try:
            error = _outcome(lambda: writer.checkpoints('K3').commit(key, 'card', b'attacker-visible bytes',
                                                                     revisions=REVISION))
        finally:
            monkeypatch.undo()
            swap.undo()
    assert swap.done and victim.read_bytes() == b'must survive'
    assert sorted(path.name for path in outside.iterdir()) == [victim.name]
    assert type(error) is StorageError


# ----------------------------------------------------------------------------- P3-A: creates (retest 2 C1)


def test_c1_lease_is_never_created_outside(project_root, tmp_path, monkeypatch, outside):
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store)
    (store.root / 'leases' / sid).mkdir(parents=True)
    swap = Swap(store.root, f'leases/{sid}', outside)
    arm(monkeypatch, store, lambda relative: relative.startswith(f'leases/{sid}/') and relative.endswith('.lease'),
        swap, 'create')
    pinned = None
    try:
        try:
            pinned = store.pin()
            error = None
        except StorageError as refused:
            error = refused
    finally:
        monkeypatch.undo()
        swap.undo()
        if pinned is not None:
            pinned._lease.release()
    assert swap.done and list(outside.iterdir()) == []
    assert type(error) is StorageError


def test_gc_lease_probe_never_recreates_a_vanished_lease(project_root, tmp_path, monkeypatch):
    """The probe opens a lease without creating it: a lease (and its directory) removed after the listing stays
    removed and protects nothing (round 2's probe reopened it with 'a+b', recreating the directory)."""
    store = make_store(project_root, tmp_path / 'kb', keep=5, retention_s=10 ** 9)
    sid = publish(store)
    pinned = store.pin()
    pinned._lease.release()
    lease = pinned._lease.path
    real_children = store._children

    def vanish_after_listing(relative):  # scheduling hook only: a foreign removal right after the listing
        listed = real_children(relative)
        if relative == f'leases/{sid}':
            lease.unlink()
            lease.parent.rmdir()
        return listed

    monkeypatch.setattr(store, '_children', vanish_after_listing)
    with store.writer() as writer:
        assert writer._live_leases() == set()
    assert not lease.parent.exists()


# ----------------------------------------------------------------------------- the anchored primitives


def _root(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    return root.resolve()


def test_atomic_write_creates_replaces_and_leaves_no_temporary(tmp_path):
    root = _root(tmp_path)
    assert anchored.atomic_write_bytes(root, 'a/b/c.bin', b'one')[0] == 3
    anchored.atomic_write_bytes(root, 'a/b/c.bin', b'two')
    assert (root / 'a' / 'b' / 'c.bin').read_bytes() == b'two'
    assert sorted(path.name for path in (root / 'a' / 'b').iterdir()) == ['c.bin']


@pytest.mark.parametrize('stage', ['check', 'produce'])
def test_a_refused_write_changes_nothing_and_leaves_no_temporary(tmp_path, stage):
    root = _root(tmp_path)
    anchored.atomic_write_bytes(root, 'd/x.parquet', b'old')

    def refuse(size, sha):
        raise StorageError('refused by check')
    table = pa.table({'a': [1]})
    with pytest.raises(StorageError, match='refused by check'):
        if stage == 'check':
            anchored.write_table_file(root, 'd/x.parquet', table, 'parquet', check=refuse)
        else:
            anchored.atomic_write_bytes(root, 'd/x.parquet', b'new', check=refuse)
    assert (root / 'd' / 'x.parquet').read_bytes() == b'old'
    assert [path.name for path in (root / 'd').iterdir()] == ['x.parquet']


def test_table_write_reports_the_committed_bytes(tmp_path):
    root = _root(tmp_path)
    size, digest = anchored.write_table_file(root, 't/x.arrow', pa.table({'a': [1, 2]}), 'arrow')
    data = (root / 't' / 'x.arrow').read_bytes()
    assert size == len(data) and digest == __import__('hashlib').sha256(data).hexdigest()


@pytest.mark.parametrize('operation', ['write', 'mkdir', 'lock', 'rename'])
def test_every_primitive_refuses_a_linked_ancestor_and_changes_nothing_outside(tmp_path, outside, operation):
    root = _root(tmp_path)
    (root / 'inner').mkdir()
    (root / 'inner' / 'src').mkdir()
    (root / 'inner').rename(root / 'displaced')
    _dir_link(root / 'inner', outside)
    victim = _victim(outside, 'x.bin')
    (outside / 'src').mkdir()
    before = sorted(path.name for path in outside.iterdir())
    with pytest.raises(StorageError) as excinfo:
        if operation == 'write':
            anchored.atomic_write_bytes(root, 'inner/x.bin', b'overwritten')
        elif operation == 'mkdir':
            anchored.make_directory(root, 'inner/new')
        elif operation == 'lock':
            anchored.open_lock_file(root, 'inner/new.lock', create=True)
        else:
            anchored.rename_entry(root, 'inner/src', 'inner/dst', directory=True)
    _refusal(excinfo)
    assert victim.read_bytes() == b'must survive' and sorted(path.name for path in outside.iterdir()) == before


@pytest.mark.parametrize('operation', ['write', 'write-deep', 'mkdir', 'lock', 'rename', 'remove'])
def test_every_primitive_refuses_a_swap_after_its_name_check(tmp_path, monkeypatch, outside, operation):
    """The ancestor becomes a link after the path rule accepted the name, before the act (the anchoring)."""
    root = _root(tmp_path)
    (root / 'inner' / 'src').mkdir(parents=True)
    (root / 'inner' / 'x.bin').write_bytes(b'inside')
    victim = _victim(outside, 'x.bin')
    (outside / 'src').mkdir()
    swap = Swap(root, 'inner', outside)
    real = anchored.real_inside

    def check_then_swap(check_root, relative):  # scheduling hook only
        result = real(check_root, relative)
        swap()
        return result
    monkeypatch.setattr(anchored, 'real_inside', check_then_swap)
    before = sorted(path.name for path in outside.iterdir())
    try:
        with pytest.raises(StorageError) as excinfo:
            if operation == 'write':
                anchored.atomic_write_bytes(root, 'inner/x.bin', b'overwritten')
            elif operation == 'write-deep':  # a missing directory below the swapped one is never created outside
                anchored.atomic_write_bytes(root, 'inner/deep/x.bin', b'overwritten')
            elif operation == 'mkdir':
                anchored.make_directory(root, 'inner/new')
            elif operation == 'lock':
                anchored.open_lock_file(root, 'inner/new.lock', create=True)
            elif operation == 'rename':
                anchored.rename_entry(root, 'inner/src', 'inner/dst', directory=True)
            else:
                anchored.remove_entry(root, 'inner/x.bin')
        _refusal(excinfo)
    finally:
        monkeypatch.undo()
        swap.undo()
    assert victim.read_bytes() == b'must survive' and sorted(path.name for path in outside.iterdir()) == before
    assert (root / 'inner' / 'x.bin').read_bytes() == b'inside'


@pytest.mark.parametrize('operation', ['write', 'lock'])
def test_the_chain_itself_refuses_a_link_without_the_name_check(tmp_path, monkeypatch, outside, operation):
    """With the path rule skipped (labelled double), the handle checks alone refuse a linked ancestor."""
    root = _root(tmp_path)
    _dir_link(root / 'inner', outside)
    victim = _victim(outside, 'x.bin')
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)
    with pytest.raises(StorageError) as excinfo:
        if operation == 'write':
            anchored.atomic_write_bytes(root, 'inner/x.bin', b'overwritten')
        else:
            anchored.open_lock_file(root, 'inner/x.bin', create=False)
    _refusal(excinfo)
    assert victim.read_bytes() == b'must survive'


def test_a_store_root_swapped_for_a_link_is_refused_by_its_own_open(tmp_path, monkeypatch, outside):
    """Fix round 2 (mutants N5, N6 on the renamed root helpers): the root itself is a link once the path rule has
    passed. Its own open refuses it as a link, never through a later check of the path the handle reached. Windows
    opens the junction itself and reads its reparse attribute; POSIX opens the last component with `O_NOFOLLOW`.
    Nothing is written behind it."""
    root = tmp_path / 'store'
    _dir_link(root, outside)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double: the race
    message = 'is a link or junction: refused' if os.name == 'nt' else 'is a link or not a directory: refused'
    for call in (lambda: anchored.atomic_write_bytes(root, 'x.bin', b'x'),
                 lambda: anchored.make_directory(root, 'd'),
                 lambda: anchored.open_lock_file(root, 'x.lock', create=True)):
        with pytest.raises(StorageError, match=message):
            call()
    assert list(outside.iterdir()) == []


@WINDOWS_ONLY
def test_the_chain_checks_the_spelling_on_the_handle(tmp_path, monkeypatch):
    root = _root(tmp_path)
    anchored.atomic_write_bytes(root, 'registry/REGISTRY', b'one')
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double
    with pytest.raises(StorageError, match='another spelling'):
        anchored.atomic_write_bytes(root, 'Registry/REGISTRY', b'two')  # NTFS opens it case-insensitively
    assert (root / 'registry' / 'REGISTRY').read_bytes() == b'one'


def test_lock_file_open_and_create(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(FileNotFoundError):
        anchored.open_lock_file(root, 'leases/sid/x.lease', create=False)
    assert not (root / 'leases').exists()  # opening without create creates no directory either
    handle = anchored.open_lock_file(root, 'leases/sid/x.lease', create=True)
    handle.close()
    assert (root / 'leases' / 'sid' / 'x.lease').is_file()
    lock = anchored.lock_file(root, 'leases/sid/x.lease', 1, 0.01, create=False)
    other = anchored.lock_file(root, 'leases/sid/x.lease', 1, 0.01, create=False)
    assert lock.try_acquire() and not other.try_acquire()  # the OS lock works on the anchored handle
    lock.release()


def test_lock_file_refuses_a_directory_link_leaf(tmp_path, outside):
    root = _root(tmp_path)
    (root / 'leases').mkdir()
    _dir_link(root / 'leases' / 'x.lease', outside)
    with pytest.raises(StorageError):
        anchored.open_lock_file(root, 'leases/x.lease', create=True)
    assert list(outside.iterdir()) == []


def test_make_directory_and_rename_never_replace(tmp_path):
    root = _root(tmp_path)
    anchored.make_directory(root, 'snapshots/.building-a')
    with pytest.raises(FileExistsError):
        anchored.make_directory(root, 'snapshots/.building-a')
    anchored.make_directory(root, 'snapshots/b')
    with pytest.raises(FileExistsError):
        anchored.rename_entry(root, 'snapshots/.building-a', 'snapshots/b', directory=True)
    anchored.rename_entry(root, 'snapshots/.building-a', 'snapshots/a', directory=True)
    assert sorted(path.name for path in (root / 'snapshots').iterdir()) == ['a', 'b']
    with pytest.raises(StorageError, match='one directory'):
        anchored.rename_entry(root, 'snapshots/a', 'other/a', directory=True)


def test_data_store_append_never_writes_outside(project_root, tmp_path, monkeypatch, outside):
    data = DataStore(tmp_path / 'data', project_root=project_root, table_format='parquet')
    from storage_fixtures import data_rows
    data.append('request_traces', data_rows()['request_traces'])
    swap = Swap(data.root, 'traces', outside)
    real = anchored.real_inside

    def check_then_swap(check_root, relative):  # scheduling hook only
        result = real(check_root, relative)
        if relative.startswith('traces/'):
            swap()
        return result
    monkeypatch.setattr(anchored, 'real_inside', check_then_swap)
    try:
        with pytest.raises(StorageError) as excinfo:
            data.append('request_traces', data_rows()['request_traces'])
        _refusal(excinfo)
    finally:
        monkeypatch.undo()
        swap.undo()
    assert list(outside.iterdir()) == []


@POSIX_ONLY
def test_posix_writes_refuse_without_a_dir_fd_primitive(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(os, 'supports_dir_fd', set())  # labelled double: a platform without dir_fd support
    for call in (lambda: anchored.atomic_write_bytes(root, 'a/x.bin', b'x'),
                 lambda: anchored.make_directory(root, 'a/b'),
                 lambda: anchored.open_lock_file(root, 'a/x.lock', create=True)):
        with pytest.raises(StorageError, match='refused'):
            call()
    assert list(root.iterdir()) == []


# The primitives `_PosixDirectory` calls, stated here and not read from the module under test (review P3-1).
_POSIX_DIR_FD = ('open', 'stat', 'unlink', 'rmdir', 'mkdir', 'rename', 'link')
_POSIX_NO_FOLLOW = ('stat', 'link')


def _fake_posix(monkeypatch, dir_fd=_POSIX_DIR_FD, no_follow=_POSIX_NO_FOLLOW):
    """Labelled double: a POSIX platform whose capability sets hold the native functions named."""
    for flag in ('O_NOFOLLOW', 'O_DIRECTORY'):
        monkeypatch.setattr(os, flag, getattr(os, flag, 0), raising=False)
    monkeypatch.setattr(os, 'supports_dir_fd', {getattr(os, name) for name in dir_fd})
    monkeypatch.setattr(os, 'supports_follow_symlinks', {getattr(os, name) for name in no_follow})


def test_the_capability_check_is_not_fooled_by_a_replaced_function(monkeypatch):
    """S42-ci3 L1 and review P3-3: the check judges the platform sets by name, so a double over any primitive,
    installed after the sets were built, keeps the store open (CI window 3 refused at start)."""
    _fake_posix(monkeypatch)
    for name in sorted(set(_POSIX_DIR_FD)):
        monkeypatch.setattr(os, name, lambda *args, **kwargs: None)  # labelled double, as in test_profiles.py
    assert anchored._posix_available()


@pytest.mark.parametrize('dir_fd, no_follow', [
    *[(tuple(n for n in _POSIX_DIR_FD if n != missing), _POSIX_NO_FOLLOW) for missing in _POSIX_DIR_FD],
    *[(_POSIX_DIR_FD, tuple(n for n in _POSIX_NO_FOLLOW if n != missing)) for missing in _POSIX_NO_FOLLOW],
])
def test_the_capability_check_refuses_without_any_one_primitive(monkeypatch, dir_fd, no_follow):
    """Review P3-1: every primitive the class calls is required; dropping one from the platform sets refuses."""
    _fake_posix(monkeypatch, dir_fd, no_follow)
    assert not anchored._posix_available()


def test_the_capability_check_holds_for_a_double_installed_before_import_and_after_reload_of_os(tmp_path):
    """Review P3-3: a module imported while `os.link` is wrapped, and `importlib.reload(os)`, which rebuilds the
    sets. Run in a child interpreter, so reloading `os` never touches this test process."""
    script = tmp_path / 'probe.py'
    script.write_text(
        'import importlib, os, sys\n'
        f'names = {_POSIX_DIR_FD!r}\n'
        'def fake():  # labelled double: a POSIX platform, sets built from the native functions\n'
        '    os.O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0); os.O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)\n'
        '    os.supports_dir_fd = {getattr(os, n) for n in names}\n'
        '    os.supports_follow_symlinks = {os.stat, os.link}\n'
        'fake()\n'
        'os.link = lambda *a, **k: None  # labelled double installed before the import\n'
        'from prompt_enhancer.storage import _anchored\n'
        'before = _anchored._posix_available()\n'
        'importlib.reload(os); fake()\n'
        'os.link = lambda *a, **k: None\n'
        'print(before, _anchored._posix_available())\n', encoding='utf-8')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run([sys.executable, '-X', 'faulthandler', str(script)], capture_output=True, text=True,
                            timeout=120, creationflags=flags, env={**os.environ, 'PYTHONPATH': SRC})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['True', 'True']


@POSIX_ONLY
def test_posix_lock_file_refuses_a_symlink_leaf(tmp_path, monkeypatch, outside):
    """A symlink leaf is refused by both layers, in their documented order (module docstring of `_anchored`):
    the path rule `real_inside` first, then the `O_NOFOLLOW` open, which alone holds against a link swapped in
    after the path rule (S42-ci3 L2: CI window 3 met the path rule's refusal, which names its own reason)."""
    root = _root(tmp_path)
    (root / 'leases').mkdir()
    victim = _victim(outside, 'x.lease')
    inner = _victim(root / 'leases', 'inner.lease', b'inner')
    os.symlink(victim, root / 'leases' / 'x.lease')
    os.symlink(inner, root / 'leases' / 'y.lease')
    with pytest.raises(StorageError, match='resolved outside project root'):
        anchored.open_lock_file(root, 'leases/x.lease', create=True)
    with pytest.raises(StorageError, match='is a link or another spelling'):  # a link that stays inside root
        anchored.open_lock_file(root, 'leases/y.lease', create=True)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double: the race
    for name in ('x.lease', 'y.lease'):
        with pytest.raises(StorageError, match='is a link: refused'):
            anchored.open_lock_file(root, f'leases/{name}', create=True)
    assert victim.read_bytes() == b'must survive' and inner.read_bytes() == b'inner'
    assert (root / 'leases' / 'x.lease').is_symlink() and (root / 'leases' / 'y.lease').is_symlink()


@POSIX_ONLY
def test_posix_root_reached_through_a_swapped_ancestor_is_refused(tmp_path, monkeypatch):
    """S42-ci3 fix round 1, class audit S1: `O_NOFOLLOW` on the root's full path covers its last component only. A
    directory above the root swapped for a link after the path rule must refuse every write, not move the store."""
    real = tmp_path / 'real'
    (real / 'store').mkdir(parents=True)
    os.symlink(real, tmp_path / 'hop', target_is_directory=True)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double: the race
    for call in (lambda root: anchored.atomic_write_bytes(root, 'x.bin', b'x'),
                 lambda root: anchored.make_directory(root, 'd')):
        with pytest.raises(StorageError, match='is a link or not a directory: refused'):
            call(tmp_path / 'hop' / 'store')
    assert list((real / 'store').iterdir()) == []
    anchored.atomic_write_bytes(real / 'store', 'x.bin', b'x')  # the same root by its resolved name still works
    assert (real / 'store' / 'x.bin').read_bytes() == b'x'


@POSIX_ONLY
def test_posix_write_replaces_a_symlink_leaf_as_a_name(tmp_path, monkeypatch, outside):
    root = _root(tmp_path)
    (root / 'registry').mkdir()
    victim = _victim(outside, 'REGISTRY')
    os.symlink(victim, root / 'registry' / 'REGISTRY')
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double
    anchored.atomic_write_bytes(root, 'registry/REGISTRY', b'new')
    assert victim.read_bytes() == b'must survive'
    assert not (root / 'registry' / 'REGISTRY').is_symlink()
    assert (root / 'registry' / 'REGISTRY').read_bytes() == b'new'


@POSIX_ONLY
def test_posix_rename_refuses_a_symlink_source(tmp_path, monkeypatch, outside):
    root = _root(tmp_path)
    (root / 'snapshots').mkdir()
    os.symlink(outside, root / 'snapshots' / '.building-a', target_is_directory=True)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double
    with pytest.raises(StorageError, match='is a link'):
        anchored.rename_entry(root, 'snapshots/.building-a', 'snapshots/a', directory=True)
    assert (root / 'snapshots' / '.building-a').is_symlink()


# ----------------------------------------------------------------------------- the POSIX class on every platform


@pytest.mark.parametrize('flag', ['O_NOFOLLOW', 'O_DIRECTORY'])
def test_the_capability_check_refuses_without_either_open_flag(monkeypatch, flag):
    """Review mutant E: every primitive is present, but the platform lacks one of the two open flags the class
    passes on every open; the store refuses at start instead of opening without it."""
    _fake_posix(monkeypatch)
    assert anchored._posix_available()
    monkeypatch.delattr(os, flag, raising=False)
    assert not anchored._posix_available()


class PosixTree:
    """Labelled double: the `os` of a POSIX host as `_anchored` uses it, over an in-memory tree, so the POSIX class
    runs on every platform. S42-ci3 fix round 2, P0-1: no Windows test reached `_PosixDirectory`, so a helper
    shadowed by its Windows namesake failed every POSIX store write unseen.

    It gives the kernel's documented answers the class relies on (open(2), mkdir(2), linkat(2); from the man
    pages, not re-run on Linux: an assumption the Linux CI checks). An open of a link without `O_NOFOLLOW`
    would follow it, which the double refuses as a test failure. `O_DIRECTORY` on a non-directory, a link
    included, is ENOTDIR. `O_NOFOLLOW` without `O_PATH` on a link is ELOOP. `O_EXCL`, mkdir and linkat on an
    existing name are EEXIST. The double also refuses what the class must never do: open a full path, as
    anything but the anchor `/`, or a name with a separator relative to a descriptor. File bytes live in real
    scratch files, so the real `open(descriptor)` of `_file_object` works. The flag values are Linux x86-64
    (asm-generic/fcntl.h); test-only."""

    name = 'posix'
    O_RDONLY, O_WRONLY, O_RDWR, O_ACCMODE = 0, 0o1, 0o2, 0o3
    O_CREAT, O_EXCL, O_NONBLOCK = 0o100, 0o200, 0o4000
    O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC, O_PATH = 0o200000, 0o400000, 0o2000000, 0o10000000
    _MODES = {'dir': stat.S_IFDIR, 'file': stat.S_IFREG, 'link': stat.S_IFLNK, 'fifo': stat.S_IFIFO}

    def __init__(self, scratch, directories=(), links=(), fifos=(), files=(), hard_links=True):
        self.scratch, self.hard_links = scratch, hard_links
        self.entries = {PurePosixPath('/'): ('dir', None)}
        for directory in directories:
            path = PurePosixPath(directory)
            for item in (*reversed(path.parents), path):
                self.entries.setdefault(item, ('dir', None))
        for kind, names in (('link', links), ('fifo', fifos)):
            for item in names:
                self.entries[PurePosixPath(item)] = (kind, None)
        for item in files:
            self.entries[PurePosixPath(item)] = ('file', self._new_file())
        self.descriptors, self.next, self.opens, self.synced = {}, 1 << 20, [], []
        self.supports_dir_fd = {getattr(os, name) for name in _POSIX_DIR_FD}  # real functions: their names count
        self.supports_follow_symlinks = {getattr(os, name) for name in _POSIX_NO_FOLLOW}

    def _new_file(self):
        real = self.scratch / f'file-{len(list(self.scratch.iterdir()))}'
        real.write_bytes(b'')
        return real

    def _path(self, name, dir_fd):
        if dir_fd is None:
            assert name == '/', f'opened by a full path, not from the anchor: {name!r}'
            return PurePosixPath('/')
        assert '/' not in name and name not in ('', '.', '..'), f'not one component: {name!r}'
        return self.descriptors[dir_fd] / name

    def _kind(self, path):
        return self.entries.get(path, (None, None))[0]

    def _descriptor(self, path):
        self.next += 1
        self.descriptors[self.next] = path
        return self.next

    def kinds(self):
        return {path.as_posix(): kind for path, (kind, _) in self.entries.items()}

    def read(self, path):
        return self.entries[PurePosixPath(path)][1].read_bytes()

    def open(self, name, flags, mode=0o777, *, dir_fd=None):
        path = self._path(name, dir_fd)
        self.opens.append((name, flags, dir_fd))
        kind = self._kind(path)
        if kind is None:
            if not flags & self.O_CREAT:
                raise FileNotFoundError(errno.ENOENT, 'No such file or directory', str(path))
            self.entries[path], kind = ('file', self._new_file()), 'file'
        elif flags & self.O_CREAT and flags & self.O_EXCL:
            raise FileExistsError(errno.EEXIST, 'File exists', str(path))
        if kind == 'link':
            assert flags & self.O_NOFOLLOW, f'{path}: a link opened without O_NOFOLLOW would be followed'
            if flags & self.O_DIRECTORY:
                raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(path))
            if not flags & self.O_PATH:
                raise OSError(errno.ELOOP, 'Too many levels of symbolic links', str(path))
            return self._descriptor(path)
        if flags & self.O_DIRECTORY and kind != 'dir':
            raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(path))
        if kind == 'dir' and flags & self.O_ACCMODE:
            raise IsADirectoryError(errno.EISDIR, 'Is a directory', str(path))
        if kind in ('dir', 'fifo'):
            return self._descriptor(path)
        access = {self.O_RDONLY: os.O_RDONLY, self.O_WRONLY: os.O_WRONLY, self.O_RDWR: os.O_RDWR}
        return os.open(self.entries[path][1], access[flags & self.O_ACCMODE] | getattr(os, 'O_BINARY', 0))

    def close(self, descriptor):
        if self.descriptors.pop(descriptor, None) is None:
            os.close(descriptor)  # a real scratch file

    def fstat(self, descriptor):
        if descriptor in self.descriptors:
            return SimpleNamespace(st_mode=self._MODES[self._kind(self.descriptors[descriptor])] | 0o755)
        return os.fstat(descriptor)

    def fsync(self, descriptor):
        if descriptor in self.descriptors:
            self.synced.append(self.descriptors[descriptor].as_posix())
        else:
            os.fsync(descriptor)

    def stat(self, name, *, dir_fd, follow_symlinks):
        assert follow_symlinks is False
        path = self._path(name, dir_fd)
        if self._kind(path) is None:
            raise FileNotFoundError(errno.ENOENT, 'No such file or directory', str(path))
        return SimpleNamespace(st_mode=self._MODES[self._kind(path)] | 0o644)

    def mkdir(self, name, mode=0o777, *, dir_fd):
        path = self._path(name, dir_fd)
        if self._kind(path) is not None:
            raise FileExistsError(errno.EEXIST, 'File exists', str(path))
        self.entries[path] = ('dir', None)

    def rmdir(self, name, *, dir_fd):
        path = self._path(name, dir_fd)
        if self._kind(path) != 'dir':
            raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(path))
        if any(path in item.parents for item in self.entries):
            raise OSError(errno.ENOTEMPTY, 'Directory not empty', str(path))
        del self.entries[path]

    def unlink(self, name, *, dir_fd):
        path = self._path(name, dir_fd)
        if self._kind(path) is None:
            raise FileNotFoundError(errno.ENOENT, 'No such file or directory', str(path))
        if self._kind(path) == 'dir':
            raise IsADirectoryError(errno.EISDIR, 'Is a directory', str(path))
        del self.entries[path]  # a link goes as a name; the real scratch file stays until pytest removes it

    def rename(self, source, target, *, src_dir_fd, dst_dir_fd):
        source, target = self._path(source, src_dir_fd), self._path(target, dst_dir_fd)
        if self._kind(source) is None:
            raise FileNotFoundError(errno.ENOENT, 'No such file or directory', str(source))
        for item in sorted(self.entries):
            if item == source or source in item.parents:
                self.entries[target / item.relative_to(source)] = self.entries.pop(item)

    def link(self, source, target, *, src_dir_fd, dst_dir_fd, follow_symlinks):
        assert follow_symlinks is False
        source, target = self._path(source, src_dir_fd), self._path(target, dst_dir_fd)
        if not self.hard_links:
            raise PermissionError(errno.EPERM, 'Operation not permitted', str(target))
        if self._kind(target) is not None:
            raise FileExistsError(errno.EEXIST, 'File exists', str(target))
        self.entries[target] = self.entries[source]


_POSIX_ROOT = PurePosixPath('/srv/data/store')


def _posix_tree(monkeypatch, tmp_path, **layout):
    """Install `PosixTree` as the `os` of `_anchored`, with a POSIX path flavour, and bypass the path rule
    (`real_inside` resolves on the host): labelled doubles, so only the anchored POSIX layer is under test."""
    scratch = tmp_path / 'posix-files'
    scratch.mkdir()
    tree = PosixTree(scratch, **layout)
    monkeypatch.setattr(anchored, 'os', tree)
    monkeypatch.setattr(anchored, 'Path', PurePosixPath)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)
    return tree


def test_every_store_write_runs_through_the_posix_class_on_any_platform(tmp_path, monkeypatch):
    """S42-ci3 fix round 2, P0-1: the fix-round-1 tree failed here with AttributeError, because the Windows
    `_open_root(api, root)` replaced the POSIX `_open_root(root, flags)`. Every write acts on descriptors opened
    from `/` one component at a time, ancestors with `O_PATH`, and leaves no descriptor open."""
    tree = _posix_tree(monkeypatch, tmp_path, directories=[_POSIX_ROOT])
    anchored.make_directory(_POSIX_ROOT, 'a')
    above = PosixTree.O_PATH | PosixTree.O_DIRECTORY | PosixTree.O_NOFOLLOW | PosixTree.O_CLOEXEC
    held = PosixTree.O_RDONLY | PosixTree.O_DIRECTORY | PosixTree.O_NOFOLLOW | PosixTree.O_CLOEXEC
    assert [(name, flags) for name, flags, _ in tree.opens] == [('/', above), ('srv', above), ('data', above),
                                                                  ('store', held)]
    assert tree.opens[0][2] is None and all(dir_fd is not None for _, _, dir_fd in tree.opens[1:])
    anchored.atomic_write_bytes(_POSIX_ROOT, 'a/b/x.bin', b'one')  # `b` is made on the way
    anchored.atomic_write_bytes(_POSIX_ROOT, 'a/b/x.bin', b'two')  # renamed over the first
    anchored.publish_new_bytes(_POSIX_ROOT, 'a/b/p.bin', b'published')
    with pytest.raises(FileExistsError):
        anchored.publish_new_bytes(_POSIX_ROOT, 'a/b/p.bin', b'never')
    anchored.rename_entry(_POSIX_ROOT, 'a/b/x.bin', 'a/b/y.bin', directory=False)
    with anchored.open_lock_file(_POSIX_ROOT, 'a/l.lock', create=True) as lock:
        lock.write(b'lock')
    anchored.make_directory(_POSIX_ROOT, 'a/gone')
    anchored.remove_entry(_POSIX_ROOT, 'a/gone', directory=True)
    store = {path[len('/srv/data/store/'):]: kind for path, kind in tree.kinds().items()
             if path.startswith('/srv/data/store/')}
    assert store == {'a': 'dir', 'a/b': 'dir', 'a/b/y.bin': 'file', 'a/b/p.bin': 'file', 'a/l.lock': 'file'}
    assert tree.read(_POSIX_ROOT / 'a/b/y.bin') == b'two' and tree.read(_POSIX_ROOT / 'a/b/p.bin') == b'published'
    assert tree.read(_POSIX_ROOT / 'a/l.lock') == b'lock'
    assert '/srv/data/store/a/b' in tree.synced  # the parent is fsynced after a rename
    assert all(flags & PosixTree.O_NOFOLLOW for _, flags, _ in tree.opens)
    assert tree.descriptors == {}


def test_the_posix_publication_falls_back_without_hard_links_on_any_platform(tmp_path, monkeypatch):
    """`publish` where linkat fails (EPERM: no hard links): an exclusive no-follow create, still no replace."""
    tree = _posix_tree(monkeypatch, tmp_path, directories=[_POSIX_ROOT], hard_links=False)
    anchored.publish_new_bytes(_POSIX_ROOT, 'p.bin', b'published')
    with pytest.raises(FileExistsError):
        anchored.publish_new_bytes(_POSIX_ROOT, 'p.bin', b'never')
    assert tree.read(_POSIX_ROOT / 'p.bin') == b'published'
    assert [path for path in tree.kinds() if path.endswith('.tmp')] == [] and tree.descriptors == {}


@pytest.mark.parametrize('where, link, root, relative', [
    ('above the root', '/srv/hop', '/srv/hop/store', 'x.bin'),
    ('the root', '/srv/store', '/srv/store', 'x.bin'),
    ('below the root', '/srv/store/sub', '/srv/store', 'sub/x.bin'),
])
def test_the_posix_class_refuses_a_link_on_the_way_on_any_platform(tmp_path, monkeypatch, where, link, root,
                                                                   relative):
    """A link anywhere from `/` down to the act's parent refuses the act and changes nothing (class audit S1 on
    every platform; the POSIX-only test above checks the same against a real kernel)."""
    tree = _posix_tree(monkeypatch, tmp_path, directories=['/srv/store'], links=[link])
    before = tree.kinds()
    for call in (lambda: anchored.atomic_write_bytes(PurePosixPath(root), relative, b'x'),
                 lambda: anchored.make_directory(PurePosixPath(root), relative.replace('.bin', '')),
                 lambda: anchored.open_lock_file(PurePosixPath(root), relative, create=True)):
        with pytest.raises(StorageError, match='is a link or not a directory: refused'):
            call()
    assert tree.kinds() == before and tree.descriptors == {}


def test_the_posix_class_refuses_links_and_wrong_types_at_the_leaf_on_any_platform(tmp_path, monkeypatch):
    """The leaf arms: a link is never removed, renamed or opened as a lock, a type mismatch is refused, a rename
    never replaces, a lock file that is no regular file is refused (first reported untested in round 0), and a
    root that is not absolute is refused before any open."""
    tree = _posix_tree(monkeypatch, tmp_path, directories=[_POSIX_ROOT / 'd'], links=[_POSIX_ROOT / 'ln'],
                       fifos=[_POSIX_ROOT / 'fifo'], files=[_POSIX_ROOT / 'f', _POSIX_ROOT / 'g'])
    before = tree.kinds()
    refusals = [
        (lambda: anchored.remove_entry(_POSIX_ROOT, 'ln'), 'is a link: refused, not changed'),
        (lambda: anchored.remove_entry(_POSIX_ROOT, 'd'), 'expected a file'),
        (lambda: anchored.remove_entry(_POSIX_ROOT, 'f', directory=True), 'expected a directory'),
        (lambda: anchored.rename_entry(_POSIX_ROOT, 'ln', 'new', directory=False), 'is a link: refused'),
        (lambda: anchored.open_lock_file(_POSIX_ROOT, 'ln', create=True), 'is a link: refused'),
        (lambda: anchored.open_lock_file(_POSIX_ROOT, 'fifo', create=False), 'expected a file'),
    ]
    for call, message in refusals:
        with pytest.raises(StorageError, match=message):
            call()
    with pytest.raises(FileExistsError):
        anchored.rename_entry(_POSIX_ROOT, 'f', 'g', directory=False)
    with pytest.raises(StorageError, match='absolute, resolved path'):
        anchored._open_posix_root(PurePosixPath('srv/data/store'), PosixTree.O_RDONLY)
    assert tree.kinds() == before and tree.descriptors == {}


# ----------------------------------------------------------------------------- structural: no name-based mutation


_PACKAGE = Path(prompt_enhancer.__file__).parent
# Every module of the package except the anchored layer itself, found by glob so a new module is checked too
# (T-006-int; retest 3 P3-D: a fixed list of eight left the rest unchecked).
_MODULES = sorted(path.relative_to(_PACKAGE).as_posix() for path in _PACKAGE.rglob('*.py')
                  if path.relative_to(_PACKAGE).as_posix() != 'storage/_anchored.py')
# k0_scan.py: the read-only no-follow scanner opens with GENERIC_READ and OPEN_EXISTING (k0_scan.py:92-93) and
# `os.open` without write flags; the write-flag rule below still applies to it.
_NATIVE_ALLOWED = {'k0_scan.py'}
_OS_MUTATIONS = {'replace', 'rename', 'renames', 'remove', 'unlink', 'rmdir', 'removedirs', 'mkdir', 'makedirs',
                 'link', 'symlink', 'truncate', 'ftruncate', 'chmod', 'lchmod', 'chown', 'lchown', 'utime', 'mkfifo',
                 'mknod', 'write', 'fdopen', 'system', 'startfile', 'posix_spawn', 'spawnv', 'execv'}
_WRITE_FLAGS = {'O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_TRUNC', 'O_APPEND', 'O_EXCL', 'O_TMPFILE'}
_PATH_MUTATIONS = {'unlink', 'rmdir', 'mkdir', 'rename', 'touch', 'write_bytes', 'write_text', 'symlink_to',
                   'hardlink_to', 'chmod', 'lchmod'}
_ARROW_WRITES = {'write_table', 'new_file', 'new_stream', 'ParquetWriter', 'write_feather', 'write_to_dataset',
                 'write_dataset', 'output_stream', 'create_memory_map'}
_FORBIDDEN_MODULES = {'shutil', 'tempfile', 'subprocess'}
_NATIVE_MODULES = {'ctypes', '_winapi'}
_OPENERS = {('io', 'open'), ('codecs', 'open'), ('os', 'fdopen')}  # mode is the second argument, as in open()
_REMOVED = {'_unlink', '_temporary', '_commit', 'fsync_dir', 'FileLock'}


def _write_mode(mode):
    """True unless `mode` is a literal read-only mode (absent means the read default)."""
    return mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                     and not set(mode.value) & set('wax+'))


def _mutations(tree, module=''):
    """Every by-name file mutation form in a module: calls, aliases, imports, `getattr` and native bindings."""
    aliases, found = {}, []  # local name -> module it binds (import os as o; import pyarrow.parquet as pq)

    def flag(node, text):
        found.append(f'{node.lineno}: {text}')

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.partition('.')[0]] = alias.name if alias.asname else \
                    alias.name.partition('.')[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if node.module in ('os', 'posix', 'nt') and (alias.name in _OS_MUTATIONS or alias.name == '*'):
                    flag(node, f'from {node.module} import {alias.name}')
                elif (node.module, alias.name) in _OPENERS:
                    aliases[alias.asname or alias.name] = 'builtins.open'
                elif node.module.partition('.')[0] == 'pyarrow' and alias.name in _ARROW_WRITES:
                    flag(node, f'from {node.module} import {alias.name}')
                else:
                    aliases[alias.asname or alias.name] = f'{node.module}.{alias.name}'
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or '']
            for name in names:
                top = name.partition('.')[0]
                if top in _FORBIDDEN_MODULES:
                    flag(node, f'imports {top}')
                elif top in _NATIVE_MODULES and module not in _NATIVE_ALLOWED:
                    flag(node, f'imports {top} (native file calls)')
        if isinstance(node, (ast.FunctionDef, ast.Name, ast.Attribute)):
            name = node.name if isinstance(node, ast.FunctionDef) else getattr(node, 'id', getattr(node, 'attr', ''))
            if name in _REMOVED and not (name == 'FileLock' and module == 'storage/_fs.py'):
                flag(node, name)
            if name in _WRITE_FLAGS:
                flag(node, f'write flag {name}')
            if name in ('windll', 'WinDLL', 'oledll', 'OleDLL') and module not in _NATIVE_ALLOWED:
                flag(node, f'native library {name}')
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            target = aliases.get(function.id, function.id)
            if function.id in ('__import__', 'import_module') or target == 'importlib.import_module':
                flag(node, f'{function.id}() (dynamic import)')
            elif target in ('open', 'builtins.open'):
                mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords
                                                                     if k.arg == 'mode'), None)
                if _write_mode(mode):
                    flag(node, f'{function.id}() for writing or with a mode that is not a read-only literal')
            elif function.id == 'getattr' and node.args:
                owner = node.args[0]
                attribute = node.args[1] if len(node.args) > 1 else None
                owner_module = aliases.get(owner.id) if isinstance(owner, ast.Name) else None
                literal = attribute.value if isinstance(attribute, ast.Constant) else None
                if owner_module in ('os', 'posix', 'nt', 'shutil', 'io') and (
                        not isinstance(literal, str) or literal in _OS_MUTATIONS or literal == 'open'):
                    flag(node, f'getattr({owner.id}, ...) of a mutation or a computed name')
                elif isinstance(literal, str) and literal in _PATH_MUTATIONS | _OS_MUTATIONS:
                    flag(node, f'getattr(..., {literal!r})')
            continue
        if not isinstance(function, ast.Attribute):
            continue
        owner = aliases.get(function.value.id) if isinstance(function.value, ast.Name) else None
        top = (owner or '').partition('.')[0]
        if function.attr == 'import_module':
            flag(node, 'import_module() (dynamic import)')
        elif top in ('os', 'posix', 'nt') and function.attr in _OS_MUTATIONS:
            flag(node, f'os.{function.attr}')
        elif (top, function.attr) in _OPENERS or (owner, function.attr) in _OPENERS:
            if _write_mode(node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == 'mode'), None)):
                flag(node, f'{function.value.id}.{function.attr}() for writing')
        elif top == 'pyarrow' and function.attr in _ARROW_WRITES:
            flag(node, f'{function.value.id}.{function.attr}')
        elif top == 'pyarrow' and function.attr == 'OSFile' and _write_mode(
                node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == 'mode'),
                                                             ast.Constant('r'))):
            flag(node, f'{function.value.id}.OSFile() for writing')
        elif function.attr == 'open':
            # Path.open(mode, ...): the mode is the first argument; a literal write mode or a mode= keyword that
            # is not a read-only literal is flagged (Image.open(fp) passes a file, not a mode)
            first = node.args[0] if node.args else None
            keyword = next((k.value for k in node.keywords if k.arg == 'mode'), None)
            if (isinstance(first, ast.Constant) and _write_mode(first)) or _write_mode(keyword):
                flag(node, '.open() for writing')
        elif function.attr in _PATH_MUTATIONS or (function.attr == 'replace' and len(node.args) == 1):
            flag(node, f'.{function.attr}()')
    return found


def test_the_module_list_covers_the_package():
    """The glob finds every module, so the check below cannot pass by finding none."""
    assert {'profiles.py', 'config.py', 'k0_scan.py', 'storage/store.py', 'storage/_fs.py', '__main__.py'} <= \
        set(_MODULES)
    assert 'storage/_anchored.py' not in _MODULES and len(_MODULES) >= 20


@pytest.mark.parametrize('module', _MODULES)
def test_no_module_mutates_a_path_by_name(module):
    """Every create, write, rename and delete of a file in `src/prompt_enhancer` goes through `_anchored`; no other
    module holds a name-based mutation that a later caller could reach (structural: by AST call and name check)."""
    path = _PACKAGE / module
    assert _mutations(ast.parse(path.read_text(encoding='utf-8')), module) == []


_FORMS = {
    'import shutil': 'import shutil\n',
    'shutil alias': 'import shutil as s\ns.rmtree(p)\n',
    'from shutil': 'from shutil import rmtree\n',
    'tempfile': 'import tempfile\n',
    'from tempfile': 'from tempfile import NamedTemporaryFile\n',
    'subprocess': 'import subprocess\n',
    'os.replace': 'import os\nos.replace(a, b)\n',
    'os.rename': 'import os\nos.rename(a, b)\n',
    'os alias': 'import os as o\no.replace(a, b)\n',
    'os.makedirs': 'import os\nos.makedirs(p)\n',
    'os.open write flags': 'import os\nos.open(p, os.O_WRONLY | os.O_CREAT)\n',
    'from os import replace': 'from os import replace\n',
    'from os import alias': 'from os import unlink as u\n',
    'from os import *': 'from os import *\n',
    'getattr(os, literal)': 'import os\ngetattr(os, "unlink")(p)\n',
    'getattr(os, computed)': 'import os\ngetattr(os, name)(p)\n',
    'getattr(path, literal)': 'getattr(p, "write_bytes")(b"")\n',
    'open wb': 'open(p, "wb")\n',
    'open computed mode': 'open(p, mode)\n',
    'open mode keyword': 'open(p, mode="a")\n',
    'io.open': 'import io\nio.open(p, "w")\n',
    'from io import open': 'from io import open\nopen(p, "w")\n',
    'from io import open alias': 'from io import open as o\no(p, "x")\n',
    'codecs.open': 'import codecs\ncodecs.open(p, "w", "utf-8")\n',
    'os.fdopen': 'import os\nos.fdopen(fd, "wb")\n',
    'Path.open wb': 'p.open("wb")\n',
    'Path.open mode keyword': 'p.open(mode="r+")\n',
    'Path.unlink': 'p.unlink()\n',
    'Path.mkdir': 'p.mkdir()\n',
    'Path.replace': 'p.replace(q)\n',
    'Path.write_bytes': 'p.write_bytes(b"")\n',
    'Path.touch': 'p.touch()\n',
    'ctypes': 'import ctypes\n',
    'from ctypes': 'from ctypes import windll\n',
    'ctypes alias call': 'import ctypes as c\nc.WinDLL("kernel32").DeleteFileW(p)\n',
    '_winapi': 'import _winapi\n',
    'pyarrow write_table': 'import pyarrow.parquet as pq\npq.write_table(t, p)\n',
    'pyarrow alias': 'import pyarrow.parquet as parquet\nparquet.write_table(t, p)\n',
    'from pyarrow import': 'from pyarrow.parquet import write_table\n',
    'pyarrow OSFile wb': 'import pyarrow as pa\npa.OSFile(p, "wb")\n',
    'dynamic import': '__import__("shutil")\n',
    'import_module': 'from importlib import import_module\nimport_module("shutil")\n',
    'importlib.import_module': 'import importlib\nimportlib.import_module("shutil")\n',
    'removed helper': '_temporary(p)\n',
    'FileLock by name': 'FileLock(p, 1, 0.1)\n',
}


@pytest.mark.parametrize('form', sorted(_FORMS))
def test_the_structural_check_sees_each_name_based_mutation(form):
    """Control: the check above flags each form it forbids (retest 3 P3-D listed the missing ones)."""
    assert _mutations(ast.parse(_FORMS[form]), 'profiles.py'), form


def test_the_structural_check_passes_read_only_forms():
    source = ('import os\nimport pyarrow as pa\nfrom PIL import Image\nopen(p, "rb")\nopen(p)\n'
              'value.replace("a", "b")\np.read_bytes()\npa.OSFile(p, "rb")\nImage.open(BytesIO(data))\n'
              'image.save(result, format=fmt)\ngetattr(os, "sched_getaffinity", None)\n'
              'os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)\np.open()\np.open("rb")\n')
    assert _mutations(ast.parse(source), 'profiles.py') == []


@pytest.mark.parametrize(('original', 'planted'), [
    ('    _write_bundle_file(root, path, data)\n', '    path.write_bytes(data)\n'),
    ('    publish_new_bytes(root, _anchored_name(root, path), data)\n',
     "    with open(path, 'xb') as handle:\n        handle.write(data)\n"),
    ('import json\n', 'import json\nfrom os import replace\n'),
])
def test_a_planted_by_name_write_in_profiles_fails_the_check(original, planted):
    """The check on the real `profiles.py` source with one by-name write planted (in memory) fails."""
    source = (_PACKAGE / 'profiles.py').read_text(encoding='utf-8')
    assert source.count(original) == 1
    assert _mutations(ast.parse(source.replace(original, planted)), 'profiles.py')


# ----------------------------------------------------------------------------- P3-B: a failed release keeps the outcome


def _reader(module):
    if module == 'k0_sync':
        return importlib.import_module('prompt_enhancer.k0_sync')._previous_for_plan
    return importlib.import_module('prompt_enhancer.k0_checkpoint')._previous


def _failing_release(monkeypatch):
    real = store_module.PinnedSnapshot.release

    def release(self):  # labelled double: the real release runs, then the step reports a failure
        real(self)
        raise StorageError('release refused (double)')
    monkeypatch.setattr(store_module.PinnedSnapshot, 'release', release)


@pytest.mark.parametrize('module', ['k0_sync', 'k0_checkpoint'])
def test_a_failed_release_keeps_the_readers_result(project_root, tmp_path, monkeypatch, module):
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    _failing_release(monkeypatch)
    with pytest.warns(store_module.StorageWarning, match='release refused'):
        rows = _reader(module)(store)
    assert set(rows) == {'folders', 'concepts', 'folder_concepts', 'images', 'folder_images'} and rows['images']


@pytest.mark.parametrize('module', ['k0_sync', 'k0_checkpoint'])
def test_a_failed_release_keeps_the_readers_exception(project_root, tmp_path, monkeypatch, module):
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    _failing_release(monkeypatch)

    def read_fails(self, name):  # labelled double: the reader's own failure
        raise ValueError('reader failed')
    monkeypatch.setattr(store_module.PinnedSnapshot, 'read_rows', read_fails)
    with warnings.catch_warnings():
        warnings.simplefilter('error')  # the failure is a note on the exception, not a warning
        with pytest.raises(ValueError, match='reader failed') as excinfo:
            _reader(module)(store)
    assert any('release refused' in note for note in getattr(excinfo.value, '__notes__', []))


def test_a_lock_timeout_after_reading_keeps_the_plan_rows(project_root, tmp_path):
    """No double: the store lock is held elsewhere, so release and the lease-directory clean-up both time out
    (storage.lock.timeout_s=0.3); the rows stand, both failures are warnings, and GC later removes the lease."""
    store = make_store(project_root, tmp_path / 'kb', timeout_s=0.3, poll_s=0.05, keep=0)
    publish(store)
    previous = importlib.import_module('prompt_enhancer.k0_sync')._previous_for_plan
    real_read = store_module.PinnedSnapshot.read_rows
    holder = anchored.lock_file(store.root, 'LOCK', 5, 0.01)

    def read_then_lock(self, name):  # scheduling hook only: another process takes the store lock mid-read
        if not holder.held:
            holder.acquire()
        return real_read(self, name)

    store_module.PinnedSnapshot.read_rows = read_then_lock
    try:
        with pytest.warns(store_module.StorageWarning) as caught:
            rows = previous(store)
    finally:
        store_module.PinnedSnapshot.read_rows = real_read
        holder.release()
    assert rows['images'] and len(caught) == 2
    assert all('timeout_s=0.3' in str(item.message) for item in caught)
    with store.writer() as writer:
        writer.gc()
    assert not any((store.root / 'leases').rglob('*.lease'))


# ----------------------------------------------------------------------------- T-006-int: retest 3 P3-B, P3-C


def test_gc_collects_a_snapshot_with_nested_directories(project_root, tmp_path):
    """P3-C: `_remove_tree` removes directories bottom-up, so a snapshot with a depth-2 tree is collected; removed
    top-down, the first directory is not empty, and the snapshot would stay `deleting` for ever."""
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=0)
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, rows in dataset('old').items():
            builder.write_table(name, rows, dim=DIM if SPECS[name].vector else None)
        builder.add_file('index/names.txt', b'index old')
        builder.add_file('a/b/c/deep.txt', b'deep')
        old = builder.finish(source_scan_summary={'folders': len(FOLDERS)})
        writer.publish(old)
    assert (store.root / 'snapshots' / old / 'a' / 'b' / 'c' / 'deep.txt').is_file()
    publish(store, 'new')
    with store.writer() as writer:
        report = writer.gc()
    assert report.collected == [old] and report.skipped == []
    assert not os.path.lexists(store.root / 'snapshots' / old)


def test_store_root_is_created_relative_to_the_project_root(project_root, tmp_path, monkeypatch, outside):
    """P3-B: the root's parent swapped for a link right before the root is created: the anchored chain from the
    project root refuses it and nothing is created outside (round 3 made the root by name with `os.makedirs`)."""
    (tmp_path / 'parent').mkdir()
    store = make_store(project_root, tmp_path / 'parent' / 'kb')
    swap = Swap(tmp_path, 'parent', outside)
    arm(monkeypatch, store, lambda relative: relative.endswith('/parent/kb'), swap, 'create')
    try:
        with pytest.raises(StorageError) as excinfo:
            store.store_lock().acquire()
        _refusal(excinfo)
        assert swap.done and list(outside.iterdir()) == []
    finally:
        swap.undo()
    store.store_lock().acquire().release()  # without the swap the root is created, below the project root
    assert (tmp_path / 'parent' / 'kb' / 'LOCK').is_file()


def test_the_anchored_layer_never_creates_a_missing_root(tmp_path):
    root = (tmp_path / 'missing').resolve()
    with pytest.raises(FileNotFoundError):
        anchored.atomic_write_bytes(root, 'a/x.bin', b'x')
    with pytest.raises(FileNotFoundError):
        anchored.open_lock_file(root, 'LOCK', create=True)
    assert not os.path.lexists(root)


def test_make_root_refuses_a_root_that_is_not_strictly_inside_the_project(project_root, tmp_path):
    for root in (project_root.resolve(), tmp_path.parent.parent / 'elsewhere'):
        with pytest.raises(StorageError, match='strictly inside the project root'):
            anchored.make_root(project_root.resolve(), root)
