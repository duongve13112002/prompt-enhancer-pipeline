"""A store file that vanishes while its path is resolved (T-006 follow-up, R-014).

Windows `ntpath.realpath` keeps the `\\\\?\\` prefix for a file that exists at its first probe and is gone at the
second (stripped: it is only a spelling of the resolved object), and returns NTFS's `<drive>\\$Extend\\$Deleted\\...`
name for a file unlinked while it is resolved. A name cannot prove which object that was, so the deletion name is a
plain refusal (T-006 race fix round 2). Leases, the store files readers delete, are removed only under the store
lock, where GC lists them, so that race cannot happen to them (tests in test_storage_anchored.py).
"""

import errno
import ntpath
import os
from pathlib import Path
import subprocess
import threading
import time

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest
from storage_fixtures import make_store, publish

from prompt_enhancer.config import _is_deleted_name, _strip_extended_prefix
from prompt_enhancer.storage import StorageError
from prompt_enhancer.storage import store as store_module
from prompt_enhancer.storage._fs import inside, real_inside

WINDOWS_ONLY = pytest.mark.skipif(os.name != 'nt', reason='the realpath spellings and junctions are Windows behaviour')
LEASE = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.lease'
FAST = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

_SEGMENT = st.text(alphabet=st.characters(whitelist_categories=('Ll', 'Lu', 'Nd'), max_codepoint=0x024F),
                   min_size=1, max_size=8)


def _junction(link, target):
    """A real NTFS junction (no symlink privilege needed); removed by the tests with rmdir, as a link."""
    done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert done.returncode == 0, done.stderr.decode(errors='replace')


def _deleted_name(root, name='000B0000003D2A984E07D461'):
    return os.path.splitdrive(root)[0] + '\\$Extend\\$Deleted\\' + name


def _refusal(excinfo):
    """A link, containment or spelling refusal: exactly StorageError."""
    assert type(excinfo.value) is StorageError, repr(excinfo.value)


@WINDOWS_ONLY
@FAST
@given(st.lists(_SEGMENT, min_size=1, max_size=4), st.sampled_from('CDE'))
def test_extended_prefix_property_drive_paths(parts, drive):
    plain = f'{drive}:\\' + '\\'.join(parts)
    assert _strip_extended_prefix('\\\\?\\' + plain) == plain
    assert _strip_extended_prefix(plain) == plain  # idempotent, and a plain spelling is untouched
    assert _strip_extended_prefix(_strip_extended_prefix('\\\\?\\' + plain)) == plain


@WINDOWS_ONLY
@FAST
@given(st.lists(_SEGMENT, min_size=2, max_size=4))
def test_extended_prefix_property_unc_paths(parts):
    plain = '\\\\' + '\\'.join(parts)
    assert _strip_extended_prefix('\\\\?\\UNC\\' + '\\'.join(parts)) == plain
    assert _strip_extended_prefix(plain) == plain


def test_extended_prefix_is_a_noop_without_a_prefix():
    plain = os.path.join(os.path.abspath(os.sep), 'a', 'b')
    assert _strip_extended_prefix(plain) == plain
    assert _strip_extended_prefix(Path(plain)) == plain


@WINDOWS_ONLY
@FAST
@given(st.lists(_SEGMENT, min_size=1, max_size=3))
def test_deleted_name_is_recognised_only_below_extend_deleted(parts):
    assert _is_deleted_name('D:\\$Extend\\$Deleted\\' + '\\'.join(parts))
    assert not _is_deleted_name('D:\\' + '\\'.join(parts) + '\\$Extend\\$Deleted\\x')
    assert not _is_deleted_name('D:\\$Extend\\' + '\\'.join(parts))


def _lease_dir(root):
    directory = root / 'leases' / 'sid'
    directory.mkdir(parents=True)
    return directory


@WINDOWS_ONLY
def test_file_vanishing_at_the_second_probe_is_not_outside_the_root(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    lease = _lease_dir(root) / LEASE
    lease.write_bytes(b'')
    original = ntpath._getfinalpathname
    calls = []

    def vanishing(path):
        if os.fspath(path).endswith(LEASE):
            calls.append(path)
            if len(calls) == 2:  # the unprefixed re-probe: the file is gone, winerror 2 differs from the first
                lease.unlink()
        return original(path)

    monkeypatch.setattr(ntpath, '_getfinalpathname', vanishing)
    assert real_inside(root, f'leases/sid/{LEASE}') == lease
    assert len(calls) >= 2, 'the patch never reached the second probe: the test no longer models the race'


@WINDOWS_ONLY
@pytest.mark.parametrize('gone', [True, False])
def test_deleted_name_is_a_plain_refusal(tmp_path, monkeypatch, gone):
    root = tmp_path.resolve()
    lease = _lease_dir(root) / LEASE
    lease.write_bytes(b'')
    real = os.path.realpath

    def pending(path, *args, **kwargs):  # labelled OS-result double: NTFS's delete-pending name (native probe)
        if os.fspath(path).endswith(LEASE):
            if gone:
                lease.unlink(missing_ok=True)
            return _deleted_name(root)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, 'realpath', pending)
    with pytest.raises(StorageError, match='changed while it was resolved') as excinfo:
        real_inside(root, f'leases/sid/{LEASE}')
    _refusal(excinfo)


@WINDOWS_ONLY
@pytest.mark.parametrize('extended', [False, True])
def test_junction_swapped_in_after_readlink_is_refused(tmp_path, monkeypatch, extended):
    """Review F1 regression: a junction to a `$Deleted` spelling installed after readlink said "not a link"."""
    root = tmp_path.resolve()
    candidate = root / 'candidate'
    candidate.mkdir()
    deleted = _deleted_name(root, 't006_review_absent')
    if extended:
        deleted = '\\\\?\\' + deleted
    original = os.readlink
    armed = True

    def swap_after_nonlink_check(path, *args, **kwargs):
        # Scheduling hook only: the real readlink error, then a real junction before realpath runs.
        nonlocal armed
        try:
            return original(path, *args, **kwargs)
        except OSError as error:
            if Path(path) == candidate and armed:
                assert error.errno == errno.EINVAL
                armed = False
                candidate.rmdir()
                _junction(candidate, deleted)
            raise

    monkeypatch.setattr(os, 'readlink', swap_after_nonlink_check)
    try:
        with pytest.raises(StorageError) as excinfo:
            real_inside(root, 'candidate')
        _refusal(excinfo)
        assert not armed and candidate.is_junction(), 'the hook never swapped: the test no longer models F1'
    finally:
        if candidate.is_junction():
            candidate.rmdir()  # the link only


@WINDOWS_ONLY
def test_threaded_create_delete_never_reads_as_outside_the_root(tmp_path):
    root = tmp_path.resolve()
    lease = _lease_dir(root) / LEASE
    stop = threading.Event()
    errors = []

    def churn():
        while not stop.is_set():
            try:
                lease.write_bytes(b'')
                lease.unlink()
            except OSError:
                pass  # the reader holds it for a moment: the churn only needs to race, not to succeed

    thread = threading.Thread(target=churn, daemon=True)
    thread.start()
    deadline = time.monotonic() + 4
    try:
        while time.monotonic() < deadline:
            try:
                real_inside(root, f'leases/sid/{LEASE}')
            except StorageError as error:
                errors.append(str(error))
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive(), 'the churn thread did not stop within 10 s'
    # Fail-closed outcomes only, never "outside the root": a deletion name, or a delete-pending file readlink
    # cannot open. Store files that callers delete are removed under the store lock, so they never race here.
    allowed = ('cannot inspect link', 'changed while it was resolved')
    assert all(any(text in e for text in allowed) for e in errors), errors[:3]


def test_a_link_is_still_refused(tmp_path):
    root = tmp_path.resolve()
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (root / 'leases').mkdir()
    link = root / 'leases' / 'sid'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('no symlink right on this machine')
    with pytest.raises(StorageError, match='outside project root'):
        inside(root, f'leases/sid/{LEASE}')
    inner = root / 'real'
    inner.mkdir()
    link_in = root / 'leases' / 'inner'
    link_in.symlink_to(inner, target_is_directory=True)
    with pytest.raises(StorageError, match='link or another spelling'):  # a link that stays inside is refused too
        real_inside(root, f'leases/inner/{LEASE}')


@WINDOWS_ONLY
def test_a_junction_is_still_refused(tmp_path):
    root = tmp_path.resolve()
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (root / 'leases').mkdir()
    junction = root / 'leases' / 'sid'
    _junction(junction, outside)
    try:
        with pytest.raises(StorageError) as excinfo:
            real_inside(root, f'leases/sid/{LEASE}')
        _refusal(excinfo)
    finally:
        junction.rmdir()


def _pinned_store(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    return store, store.pin()


def test_release_raises_a_refusal_even_when_the_lease_is_gone(project_root, tmp_path, monkeypatch):
    store, pinned = _pinned_store(project_root, tmp_path)
    lease = store.root / 'leases' / pinned.snapshot_id / pinned._lease.path.name

    def refusing(root, relative, **kwargs):  # labelled double: the removal refuses after the file went away
        lease.unlink(missing_ok=True)
        raise StorageError('resolved outside project root')

    monkeypatch.setattr(store_module, 'remove_entry', refusing)
    with pytest.raises(StorageError, match='outside project root'):
        pinned.release()


@WINDOWS_ONLY
def test_release_refuses_an_ancestor_junction(project_root, tmp_path):
    """Review F2 regression (release): the lease directory replaced by a junction to an empty outside folder."""
    store, pinned = _pinned_store(project_root, tmp_path)
    pinned._lease.release()
    directory = store.root / 'leases' / pinned.snapshot_id
    directory.rename(store.root / 'displaced-leases')
    outside = tmp_path / 'outside'
    outside.mkdir()
    _junction(directory, outside)
    try:
        with pytest.raises(StorageError) as excinfo:
            pinned.release()
        _refusal(excinfo)
    finally:
        directory.rmdir()


def _stale_lease(store, sid):
    path = store.root / 'leases' / sid / LEASE
    path.write_bytes(b'')
    return path


def test_live_leases_still_fails_closed_on_other_refusals(project_root, tmp_path):
    store, pinned = _pinned_store(project_root, tmp_path)
    stale = _stale_lease(store, pinned.snapshot_id)
    real_path = store.path

    def refusing(relative):
        if relative.endswith(LEASE):
            stale.unlink(missing_ok=True)  # gone, but the refusal stays a refusal
            raise StorageError('resolved outside project root')
        return real_path(relative)

    store.path = refusing
    with store.writer() as writer:
        with pytest.raises(StorageError, match='outside project root'):
            writer._live_leases()
    del store.path
    pinned.release()


@WINDOWS_ONLY
def test_gc_refuses_an_ancestor_junction_swapped_in_after_listing(project_root, tmp_path):
    """Review F2 regression (GC): the lease directory becomes a junction after `_children` listed it."""
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, '1')
    pinned = store.pin()
    pinned._lease.release()  # deliberately stale: the refusal, not live-reader protection, is under test
    publish(store, '2')
    directory = store.root / 'leases' / old
    outside = tmp_path / 'outside'
    outside.mkdir()
    lease_relative = f'leases/{old}/{pinned._lease.path.name}'
    original = store.path
    armed = True

    def swap_before_entry_resolution(relative):
        # Scheduling hook only: replace the listed directory by a real junction before the real path rule runs.
        nonlocal armed
        if relative == lease_relative and armed:
            armed = False
            directory.rename(store.root / 'displaced-leases')
            _junction(directory, outside)
        return original(relative)

    store.path = swap_before_entry_resolution
    try:
        with store.writer() as writer:
            with pytest.raises(StorageError) as excinfo:
                writer.gc()
        _refusal(excinfo)
        assert not armed, 'the hook never swapped: the test no longer models F2'
        store.path = original
        assert (store.root / 'snapshots' / old).is_dir()  # GC aborted before marking or deleting
        assert {row['snapshot_id']: row['state'] for row in store.snapshots()}[old] == 'retired'
    finally:
        store.path = original
        if directory.is_junction():
            directory.rmdir()
