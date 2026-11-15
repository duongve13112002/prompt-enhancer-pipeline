"""T-006 race fix round 2 (decision 123, R-014, R-017): a lease is removed under the store lock; lease and snapshot
entries are removed only through an object verified inside the store root at the moment of removal; GC's keep-set
fails closed on a missing or unreadable LIVE manifest; a snapshot is collected only when verified absent.

Links are junctions on Windows (no privilege needed) and symlinks on POSIX. POSIX-only tests run on Linux CI.
"""

from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest
from storage_fixtures import DIM, dataset, make_store, publish

from prompt_enhancer import config
from prompt_enhancer.storage import StorageError, artifact_key
from prompt_enhancer.storage import store as store_module
from prompt_enhancer.storage.tables import SPECS

anchored = importlib.import_module('prompt_enhancer.storage._anchored')
remove_entry = anchored.remove_entry

WINDOWS_ONLY = pytest.mark.skipif(os.name != 'nt', reason='Windows realpath spelling or handle behaviour')
POSIX_ONLY = pytest.mark.skipif(os.name == 'nt', reason='POSIX dir_fd branch: runs on Linux CI')
REVISION = {'model': 'probe', 'prompt': 'probe', 'schema': 'probe'}


def _dir_link(link, target):
    """A directory link without privilege: a junction on Windows, a symlink on POSIX."""
    if os.name == 'nt':
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True,
                              creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert done.returncode == 0, done.stderr.decode(errors='replace')
    else:
        os.symlink(target, link, target_is_directory=True)


def _drop_link(link):
    """Remove a directory link as a link, never through it."""
    if os.name == 'nt':
        os.rmdir(link)
    else:
        os.unlink(link)


def _is_link(path):
    return path.is_junction() if os.name == 'nt' else path.is_symlink()


def _refusal(excinfo):
    """A link, containment, spelling or type refusal: exactly StorageError."""
    assert type(excinfo.value) is StorageError, repr(excinfo.value)


def _tree(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    root = root.resolve()  # the store rule: root is resolved before any path is checked against it
    (root / 'leases' / 'sid').mkdir(parents=True)
    lease = root / 'leases' / 'sid' / 'x.lease'
    lease.write_bytes(b'')
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'x.lease'
    victim.write_bytes(b'must survive')
    return root, lease, victim


# ----------------------------------------------------------------------------- the anchored primitive


def test_remove_entry_removes_a_file_then_its_empty_directory(tmp_path):
    root, lease, _ = _tree(tmp_path)
    remove_entry(root, 'leases/sid/x.lease')
    assert not lease.exists()
    remove_entry(root, 'leases/sid', directory=True)
    assert not (root / 'leases' / 'sid').exists()


def test_remove_entry_reports_absence_as_file_not_found(tmp_path):
    root, _, _ = _tree(tmp_path)
    with pytest.raises(FileNotFoundError):
        remove_entry(root, 'leases/sid/gone.lease')
    with pytest.raises(FileNotFoundError):
        remove_entry(root, 'leases/absent/x.lease')  # a directory on the way is missing


def test_remove_entry_refuses_a_type_mismatch_and_a_non_empty_directory(tmp_path):
    root, lease, _ = _tree(tmp_path)
    with pytest.raises(StorageError, match='expected a'):
        remove_entry(root, 'leases/sid', directory=False)
    with pytest.raises(StorageError, match='expected a'):
        remove_entry(root, 'leases/sid/x.lease', directory=True)
    with pytest.raises(OSError):
        remove_entry(root, 'leases/sid', directory=True)
    assert lease.exists()


def test_remove_entry_refuses_a_linked_ancestor_and_keeps_the_outside_file(tmp_path):
    root, lease, victim = _tree(tmp_path)
    directory = root / 'leases' / 'sid'
    directory.rename(root / 'displaced')
    _dir_link(directory, victim.parent)
    try:
        with pytest.raises(StorageError) as excinfo:
            remove_entry(root, 'leases/sid/x.lease')
        _refusal(excinfo)
    finally:
        _drop_link(directory)
    assert victim.read_bytes() == b'must survive'


def test_remove_entry_refuses_a_swap_after_the_name_check(tmp_path, monkeypatch):
    """N2 at the primitive: the ancestor becomes a link after the path rule passed, before the removal."""
    root, lease, victim = _tree(tmp_path)
    directory = root / 'leases' / 'sid'
    real_check = anchored.real_inside

    def check_then_swap(check_root, relative):  # scheduling hook only: the real check, then a real link
        result = real_check(check_root, relative)
        directory.rename(root / 'displaced')
        _dir_link(directory, victim.parent)
        return result

    monkeypatch.setattr(anchored, 'real_inside', check_then_swap)
    try:
        with pytest.raises(StorageError) as excinfo:
            remove_entry(root, 'leases/sid/x.lease')
        _refusal(excinfo)
    finally:
        _drop_link(directory)
    assert victim.read_bytes() == b'must survive'
    assert (root / 'displaced' / 'x.lease').exists()


def test_remove_entry_refuses_a_linked_leaf_without_following_it(tmp_path, monkeypatch):
    root, _, victim = _tree(tmp_path)
    link = root / 'leases' / 'linked'
    _dir_link(link, victim.parent)
    # Labelled double: skip the name check so the anchored chain itself must refuse the link.
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)
    try:
        with pytest.raises(StorageError) as excinfo:
            remove_entry(root, 'leases/linked', directory=True)
        _refusal(excinfo)
        assert _is_link(link)
    finally:
        _drop_link(link)
    assert victim.read_bytes() == b'must survive'


@WINDOWS_ONLY
def test_remove_entry_checks_the_spelling_on_the_handle(tmp_path, monkeypatch):
    root, lease, _ = _tree(tmp_path)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double, as above
    with pytest.raises(StorageError, match='another spelling') as excinfo:
        remove_entry(root, 'Leases/sid/x.lease')  # NTFS opens it case-insensitively; the final path differs
    _refusal(excinfo)
    assert lease.exists()


@WINDOWS_ONLY
def test_directories_held_during_removal_cannot_be_renamed(tmp_path, monkeypatch):
    """Native evidence for the anchoring: no directory on the held chain can be renamed while the entry is
    removed (NTFS refuses to move a directory with an open descendant; the share mode is a second guard, so
    mutant M5, which shares delete, is not distinguishable here)."""
    root, lease, _ = _tree(tmp_path)
    seen = []
    real_delete = anchored._delete_on_handle

    def try_rename_first(api, handle):  # scheduling hook only: attempt the swap while the chain is held
        for directory in (root / 'leases' / 'sid', root / 'leases'):
            try:
                directory.rename(directory.with_name(directory.name + '-moved'))
                seen.append('renamed')
            except OSError as error:
                seen.append(type(error).__name__)
        return real_delete(api, handle)

    monkeypatch.setattr(anchored, '_delete_on_handle', try_rename_first)
    remove_entry(root, 'leases/sid/x.lease')
    assert seen == ['PermissionError', 'PermissionError'], seen
    assert not lease.exists()


@POSIX_ONLY
def test_remove_entry_refuses_without_a_dir_fd_primitive(tmp_path, monkeypatch):
    root, lease, _ = _tree(tmp_path)
    monkeypatch.setattr(os, 'supports_dir_fd', set())  # labelled double: a platform without dir_fd support
    with pytest.raises(StorageError, match='refused'):
        remove_entry(root, 'leases/sid/x.lease')
    assert lease.exists()


@POSIX_ONLY
def test_remove_entry_removes_a_symlink_leaf_never_its_target(tmp_path, monkeypatch):
    root, _, victim = _tree(tmp_path)
    link = root / 'leases' / 'sid' / 'linked.lease'
    os.symlink(victim, link)
    monkeypatch.setattr(anchored, 'real_inside', lambda check_root, relative: None)  # labelled double
    with pytest.raises(StorageError, match='is a link'):
        remove_entry(root, 'leases/sid/linked.lease')
    assert link.is_symlink() and victim.read_bytes() == b'must survive'


# ----------------------------------------------------------------------------- release under the store lock


def _pinned(project_root, tmp_path, **settings_):
    store = make_store(project_root, tmp_path / 'kb', **settings_)
    publish(store)
    pinned = store.pin()
    return store, pinned, pinned._lease.path


def test_release_removes_its_lease_only_under_the_store_lock(project_root, tmp_path):
    store, pinned, lease = _pinned(project_root, tmp_path)
    lock = store.store_lock().acquire()
    thread = threading.Thread(target=pinned.release, daemon=True)
    try:
        thread.start()
        time.sleep(0.5)  # release has dropped its lease lock and waits for the store lock
        assert thread.is_alive() and lease.exists() and not pinned._lease.held
    finally:
        lock.release()
    thread.join(timeout=10)
    assert not thread.is_alive() and not lease.exists()


def test_release_lock_timeout_leaves_the_stale_lease_for_gc(project_root, tmp_path):
    """Non-default storage.lock.timeout_s/poll_s observed in release(): the wait is bounded by them."""
    store, pinned, lease = _pinned(project_root, tmp_path, timeout_s=0.3, poll_s=0.05)
    lock = store.store_lock().acquire()
    started = time.monotonic()
    try:
        with pytest.raises(StorageError, match='left for GC as a stale lease.*timeout_s=0.3'):
            pinned.release()
    finally:
        lock.release()
    assert 0.25 <= time.monotonic() - started < 5
    assert lease.exists() and not pinned._lease.held  # not removed without the lock, and no longer pinned
    with store.writer() as writer:
        report = writer.gc()
    assert report.leased == [] and not lease.exists()


def test_release_after_gc_removed_the_stale_lease_is_silent(project_root, tmp_path):
    """The original race, now serialised: GC removed the unlocked lease first; release reports nothing."""
    store, pinned, lease = _pinned(project_root, tmp_path)
    pinned._lease.release()
    with store.writer() as writer:
        writer.gc()
    assert not lease.exists()
    pinned.release()


def test_gc_skips_a_lease_that_vanished_after_listing(project_root, tmp_path):
    store, pinned, held = _pinned(project_root, tmp_path)
    stale = held.with_name('b' * 32 + '.lease')
    stale.write_bytes(b'')
    original = store.path

    def vanish_first(relative):  # scheduling hook only: a foreign delete between listing and resolution
        if relative.endswith(stale.name):
            stale.unlink(missing_ok=True)
        return original(relative)

    store.path = vanish_first
    try:
        with store.writer() as writer:
            assert writer._live_leases() == {pinned.snapshot_id}
    finally:
        store.path = original
    assert not stale.exists()
    pinned.release()


# ----------------------------------------------------------------------------- retest 1 regressions


@WINDOWS_ONLY
@pytest.mark.parametrize('consumer', ['release', 'listing'])
@pytest.mark.parametrize('schedule', ['leaf_after_missing', 'ancestor_after_check', 'after_proof'])
def test_late_junction_is_never_tolerated(project_root, tmp_path, monkeypatch, consumer, schedule):
    """Retest 1 R1, three schedules times two consumers (release, GC lease listing). The schedule hooks fire in
    the round-1 lstat chain; round 2 has no such chain, so the deletion name is refused before any hook."""
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    pinned = store.pin()
    pinned._lease.release()  # stale unlocked lease, so no live-reader loss is claimed
    leaf = pinned._lease.path
    directory = leaf.parent
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = leaf if schedule != 'ancestor_after_check' else directory
    active = injected = False
    realpath, lstat = os.path.realpath, os.lstat
    proof = getattr(config, '_proven_vanished', None)

    def swap():
        nonlocal injected
        if schedule == 'ancestor_after_check':
            directory.rename(store.root / 'displaced-leases')
        _dir_link(link, outside)
        injected = True

    def pending(path, *args, **kwargs):  # labelled OS-result double: genuine unlink, then NTFS deletion name
        nonlocal active
        if Path(path) == leaf:
            leaf.unlink(missing_ok=True)
            active = True
            return leaf.drive + r'\$Extend\$Deleted\retest1_native_shape'
        return realpath(path, *args, **kwargs)

    def scheduled_lstat(path, *args, **kwargs):
        try:
            info = lstat(path, *args, **kwargs)
        except FileNotFoundError:
            if active and not injected and schedule == 'leaf_after_missing' and Path(path) == leaf:
                swap()
            raise
        if active and not injected and schedule == 'ancestor_after_check' and Path(path) == directory:
            swap()
        return info

    def scheduled_proof(root, target):
        result = proof(root, target)
        if active and result and not injected and schedule == 'after_proof':
            swap()
        return result

    monkeypatch.setattr(os.path, 'realpath', pending)
    monkeypatch.setattr(os, 'lstat', scheduled_lstat)
    monkeypatch.setattr(config, '_proven_vanished', scheduled_proof, raising=False)
    try:
        with pytest.raises(StorageError) as excinfo:
            if consumer == 'release':
                pinned.release()
            else:
                with store.writer() as writer:
                    writer._live_leases()
        _refusal(excinfo)
        assert active, 'the deletion-name double never ran: the test no longer reaches the resolution'
        if injected:
            assert _is_link(link)  # refused, not followed or removed
    finally:
        monkeypatch.undo()
        if _is_link(link):
            _drop_link(link)


def test_release_never_unlinks_through_a_swap_after_the_check(project_root, tmp_path, monkeypatch):
    """Retest 1 N2: the lease directory becomes a link to an outside folder after the path check."""
    store = make_store(project_root, tmp_path / 'kb')
    publish(store)
    pinned = store.pin()
    pinned._lease.release()
    directory = pinned._lease.path.parent
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / pinned._lease.path.name
    victim.write_bytes(b'must survive')
    relative = f'leases/{pinned.snapshot_id}/{pinned._lease.path.name}'
    swapped = []

    def swap_once():
        if not swapped:
            directory.rename(store.root / 'displaced-leases')
            _dir_link(directory, outside)
            swapped.append(True)

    original_path, original_check = store.path, anchored.real_inside

    def path_then_swap(name):  # scheduling hook (round 1 checks through store.path)
        result = original_path(name)
        if name == relative:
            swap_once()
        return result

    def check_then_swap(root, name):  # scheduling hook (round 2 checks inside the anchored removal)
        result = original_check(root, name)
        if name == relative:
            swap_once()
        return result

    store.path = path_then_swap
    monkeypatch.setattr(anchored, 'real_inside', check_then_swap)
    try:
        with pytest.raises(StorageError) as excinfo:
            pinned.release()
        _refusal(excinfo)
        assert swapped
    finally:
        store.path = original_path
        if _is_link(directory):
            _drop_link(directory)
    assert victim.read_bytes() == b'must survive'


def _snapshot_with_artifact(store):
    with store.writer() as writer:
        key = artifact_key('card', 'sha256:' + '2' * 64, REVISION)
        relative = writer.checkpoints('K3').commit(key, 'card', b'protected', revisions=REVISION)
        builder = writer.begin_snapshot()
        for name, rows in dataset('a').items():
            builder.write_table(name, rows, dim=DIM if SPECS[name].vector else None)
        builder.reference_artifacts([relative])
        sid = builder.finish()
        writer.publish(sid)
    return sid, relative


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_gc_refuses_when_a_live_manifest_cannot_be_read(project_root, tmp_path, damage):
    """Retest 1 N1: a leased snapshot's references are unknown, so GC deletes nothing."""
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=0)
    sid, relative = _snapshot_with_artifact(store)
    pinned = store.pin()
    manifest = store.root / 'snapshots' / sid / store_module.MANIFEST
    if damage == 'missing':
        manifest.unlink()
    else:
        manifest.write_bytes(b'not a parquet file')
    try:
        with store.writer() as writer:
            with pytest.raises(StorageError, match=f'snapshot {sid} is published but its manifest cannot be read'):
                writer.gc()
        assert (store.root / relative).read_bytes() == b'protected'
    finally:
        pinned.release()


def test_snapshot_is_collected_only_when_its_tree_is_verified_absent(project_root, tmp_path, monkeypatch):
    """Retest 1 N4: a removal that reports "not found" while the tree is still there does not mark collected."""
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')

    def absent_but_kept(*args, **kwargs):  # labelled double: the removal claims absence and deletes nothing
        raise FileNotFoundError(2, 'reported absent')

    if hasattr(store_module, 'remove_entry'):
        monkeypatch.setattr(store_module, 'remove_entry', absent_but_kept)
    else:  # round 1 removed the tree with rmtree
        monkeypatch.setattr(store_module.shutil, 'rmtree', absent_but_kept)
    with store.writer() as writer:
        report = writer.gc()
    assert report.collected == [] and [sid for sid, _ in report.skipped] == [old]
    assert {row['snapshot_id']: row['state'] for row in store.snapshots()}[old] == 'deleting'
    assert (store.root / 'snapshots' / old).is_dir()
    monkeypatch.undo()
    with store.writer() as writer:
        assert writer.gc().collected == [old]
    assert not (store.root / 'snapshots' / old).exists()


def test_gc_removes_every_lease_and_snapshot_entry_through_the_anchored_primitive(project_root, tmp_path,
                                                                                  monkeypatch):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    stale = store.root / 'leases' / old / ('c' * 32 + '.lease')
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b'')  # an unlocked lease: stale, so GC removes it and the snapshot is collectable
    publish(store, 'new')
    snapshot = store.root / 'snapshots' / old
    entries = {path.relative_to(store.root).as_posix() for path in snapshot.rglob('*')} | {f'snapshots/{old}'}
    real, calls = store_module.remove_entry, []

    def recording(root, relative, **kwargs):  # spy only: records, then removes for real
        calls.append(relative)
        return real(root, relative, **kwargs)

    monkeypatch.setattr(store_module, 'remove_entry', recording)
    with store.writer() as writer:
        assert writer.gc().collected == [old]
    assert f'leases/{old}/{stale.name}' in calls and entries <= set(calls)
    assert not stale.exists() and not snapshot.exists()


def test_partial_removal_stays_deleting_and_is_retried(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    real, calls = store_module.remove_entry, []

    def second_fails(root, relative, **kwargs):  # labelled double: a foreign handle blocks the second file
        calls.append(relative)
        if len(calls) == 2:
            raise PermissionError(13, 'busy')
        return real(root, relative, **kwargs)

    monkeypatch.setattr(store_module, 'remove_entry', second_fails)
    with store.writer() as writer:
        report = writer.gc()
    assert report.collected == [] and report.skipped[0][0] == old
    assert {row['snapshot_id']: row['state'] for row in store.snapshots()}[old] == 'deleting'
    monkeypatch.undo()
    with store.writer() as writer:
        assert writer.gc().collected == [old]
    assert not (store.root / 'snapshots' / old).exists()


# ----------------------------------------------------------------------------- property: GC keep-set


_STATES = st.sampled_from(['intact', 'missing', 'corrupt'])
_REFS = st.frozensets(st.sampled_from([0, 1]))
_THREE = 3


def _damage(store, sid, state):
    manifest = store.root / 'snapshots' / sid / store_module.MANIFEST
    if state == 'missing':
        manifest.unlink()
    elif state == 'corrupt':
        manifest.write_bytes(b'\x00' * 16)


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(refs=st.tuples(*[_REFS] * _THREE), manifests=st.tuples(*[_STATES] * _THREE),
       published=st.tuples(*[st.booleans()] * _THREE), leased=st.tuples(*[st.booleans()] * _THREE),
       holds=st.tuples(*[st.sampled_from(['none', 'live', 'expired'])] * _THREE), keep=st.sampled_from([0, 1]))
def test_gc_keep_set_property(project_root, tmp_path, refs, manifests, published, leased, holds, keep):
    """Round 3 (P2-A, P3-C): three snapshots with generated row state (published in order, so the last
    published one is CURRENT and the others retired, or validated and never published), references, manifest
    state, leases, live or expired holds, and keep. The keep-set is CURRENT, the newest `keep` retired, and
    every leased or live-held snapshot. GC refuses, marking, collecting and deleting nothing, exactly when a
    manifest in the keep-set cannot be read; otherwise it collects every other snapshot, reports those with a
    damaged manifest, and exactly the artifacts the keep-set references survive (retention 0)."""
    store = make_store(project_root, tmp_path / uuid.uuid4().hex, keep=keep, retention_s=0)
    pins, order = [], []
    try:
        with store.writer() as writer:
            checkpoints = writer.checkpoints('K3')
            artifacts = [checkpoints.commit(artifact_key('card', 'sha256:' + digit * 64, REVISION), 'card',
                                            digit.encode(), revisions=REVISION) for digit in '23']
            sids = []
            for index, chosen in enumerate(refs):
                builder = writer.begin_snapshot()
                for name, rows in dataset(str(index)).items():
                    builder.write_table(name, rows, dim=DIM if SPECS[name].vector else None)
                builder.reference_artifacts(sorted(artifacts[i] for i in chosen))
                sids.append(builder.finish())
                if published[index]:
                    writer.publish(sids[-1])
                    order.append(index)
                    if leased[index]:
                        pins.append(store.pin())  # a reader that still reads it after it is retired
        now = datetime.now(timezone.utc)
        for index in order:
            if holds[index] != 'none':
                expiry = now + timedelta(hours=1 if holds[index] == 'live' else -1)
                store.hold(sids[index], f'r{index}', expiry)
        for sid, state in zip(sids, manifests):
            _damage(store, sid, state)
        current = order[-1] if order else None
        kept = list(reversed(order[:-1]))[:keep]
        needed = ({current} - {None}) | set(kept) | {i for i in order if leased[i] or holds[i] == 'live'}
        others = set(range(_THREE)) - needed
        refused = any(manifests[i] != 'intact' for i in needed)
        before = {row['snapshot_id']: row['state'] for row in store.snapshots()}
        with store.writer() as writer:
            if refused:
                with pytest.raises(StorageError, match='manifest cannot be read.*Restore .*MANIFEST.parquet'):
                    writer.gc()
            else:
                report = writer.gc()
                assert {sid for sid, _ in report.damaged} == {sids[i] for i in others if manifests[i] != 'intact'}
        states = {row['snapshot_id']: row['state'] for row in store.snapshots()}
        if refused:
            assert states == before and all((store.root / artifact).exists() for artifact in artifacts)
            assert all((store.root / 'snapshots' / sid).is_dir() for sid in sids)
        else:
            assert {i for i in range(_THREE) if states[sids[i]] == 'collected'} == others
            assert all(not (store.root / 'snapshots' / sids[i]).exists() for i in others)
            survivors = {artifacts[a] for i in needed for a in refs[i]}
            assert {a for a in artifacts if (store.root / a).exists()} == survivors
    finally:
        for pinned in pins:
            pinned.release()


def test_gc_collects_an_unneeded_snapshot_whose_manifest_is_damaged(project_root, tmp_path):
    """P2-A: a retired snapshot outside the keep-set never stops GC or commit; it is reported and collected."""
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=0)
    old, relative = _snapshot_with_artifact(store)
    publish(store, 'new')
    _damage(store, old, 'missing')
    key = artifact_key('card', 'sha256:' + '2' * 64, REVISION)
    with store.writer() as writer:
        with pytest.raises(StorageError, match=f'snapshot {old} is retired .*run kb gc'):
            writer.checkpoints('K3').commit(key, 'card', b'changed', revisions=REVISION)  # before GC: refused
        report = writer.gc()
    assert report.collected == [old] and [sid for sid, _ in report.damaged] == [old]
    assert 'manifest missing' in report.damaged[0][1]
    assert not (store.root / relative).exists()  # only the collected snapshot referenced it
    with store.writer() as writer:  # the refusal ended with the snapshot
        writer.checkpoints('K3').commit(key, 'card', b'changed', revisions=REVISION)


def test_gc_refusal_for_a_needed_damaged_manifest_names_the_file_and_the_action(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=0)
    old = publish(store, 'old')
    sid, relative = _snapshot_with_artifact(store)
    _damage(store, sid, 'corrupt')
    with store.writer() as writer:
        with pytest.raises(StorageError) as excinfo:
            writer.gc()
    message = str(excinfo.value)
    for part in (f'snapshot {sid} is published', f'snapshots/{sid}/MANIFEST.parquet', 'Restore', 'kb gc',
                 'nothing deleted'):
        assert part in message, message
    assert (store.root / relative).read_bytes() == b'protected'
    assert {row['snapshot_id']: row['state'] for row in store.snapshots()}[old] == 'retired'  # nothing marked
