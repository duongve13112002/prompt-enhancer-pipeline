"""Fix round 4: one path rule for every single-path operation (R-014, R-017, R-025).

F1 (fourth Codex review): an internal junction artifacts/alias -> artifacts/blob let commit() replace the bytes of
a published artifact, because the immutability check compared the spelled name while the write followed the link.
The class: every operation that opens, writes, renames, replaces or deletes a store path refuses a link or
junction on that path, whether it points outside the store or stays inside it, and changes no byte.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from storage_fixtures import DIM, build_snapshot, data_rows, dataset, make_store, publish
from test_storage_review_r1 import REV, dir_link, remove_link
from test_storage_review_r2 import build

from prompt_enhancer.storage import DataStore, PinnedSnapshot, StorageError, artifact_key
from prompt_enhancer.storage import store as store_module
from prompt_enhancer.storage._fs import FileLock

LATER = datetime.now(timezone.utc) + timedelta(days=1)
OTHER = artifact_key('blob', 'another input', REV)
BUILDING = 'snapshots/.building-s20261115T120000000000Z-0123abcd'  # what an interrupted build leaves behind
LOCK_FILES = ('LOCK', 'WRITER')  # held through OS locks: compared by presence, their bytes are never written


def is_link(path):
    try:
        os.readlink(path)  # true for a symlink and for a Windows junction, which lstat does not report as a link
    except OSError:
        return False
    return True


def tree_bytes(*roots):
    """Every file below the roots with its bytes; a link is recorded as one and never entered."""
    found, pending = {}, [Path(root) for root in roots]
    while pending:
        for entry in sorted(pending.pop().iterdir()):
            if is_link(entry):
                found[str(entry)] = 'link'
            elif entry.is_dir():
                pending.append(entry)
            elif entry.name in LOCK_FILES or entry.suffix == '.lease':
                found[str(entry)] = 'lock file'
            else:
                found[str(entry)] = entry.read_bytes()
    return found


class Links:
    """Turns a store directory into a link and remembers every byte, so a refusal can be shown to change none.

    The link points at a real directory outside the store, or inside it (`elsewhere/`, where the round 1 to 3
    containment check accepted it). A directory that already exists is moved there first, so its content stays
    reachable through the link and the operation would succeed if it followed the link."""

    def __init__(self, root, kind, outside):
        self.root, self.kind = Path(root), kind
        self.base = self.root / 'elsewhere' if kind == 'inside' else Path(outside)
        self.message = 'is a link' if kind == 'inside' else 'outside'
        self.made, self.before, self.checked = [], None, 0

    def __call__(self, relative):
        place, real = self.root / relative, self.base / f'real-{len(self.made)}'
        self.base.mkdir(parents=True, exist_ok=True)
        moved = place.is_dir()
        if moved:
            os.rename(place, real)
        else:
            real.mkdir()
            place.parent.mkdir(parents=True, exist_ok=True)
        dir_link(place, real)
        self.made.append((place, real, moved))
        self.remember()
        return real

    def remember(self):
        self.before = tree_bytes(self.root, self.base)

    @contextmanager
    def refused(self):
        """The operation inside is refused for the link, no byte changes, and the link is then taken away."""
        with pytest.raises(StorageError, match=self.message):
            yield
        assert self.made, 'the case placed no link'
        assert tree_bytes(self.root, self.base) == self.before
        self.checked += 1
        self.restore()

    def restore(self):
        while self.made:
            place, real, moved = self.made.pop()
            remove_link(place)
            if moved:
                os.rename(real, place)


@pytest.fixture(params=['inside', 'outside'])
def kind(request):
    return request.param


@pytest.fixture
def linked(project_root, tmp_path, kind):
    store = make_store(project_root, tmp_path / 'kb', keep=1, retention_s=0)
    link = Links(store.root, kind, tmp_path / 'outside')
    yield store, link
    link.restore()  # a failed case must not leave a link for the temporary-directory cleanup to follow


def build_directory(builder):
    return f'snapshots/.building-{builder.snapshot_id}'


def seed(store):
    """A retained snapshot that references a blob artifact, and the published one after it."""
    with store.writer() as writer:
        old, relative, key = build(writer, 'old')
        writer.publish(old)
    return SimpleNamespace(old=old, new=publish(store, 'new'), relative=relative, key=key)


# ---------------------------------------------------------------- F1 as reported


@pytest.mark.parametrize('arm', ['blob', 'table'])
def test_f1_commit_through_an_internal_junction_cannot_change_a_published_artifact(project_root, tmp_path, arm):
    store = make_store(project_root, tmp_path / 'kb')
    kind_name, table = ('blob', None) if arm == 'blob' else ('observations', 'observations')
    good = b'good' if arm == 'blob' else dataset('a')['observations']
    evil = b'evil' if arm == 'blob' else dataset('a')['observations'][:1]
    key = artifact_key(kind_name, 'link', REV)
    alias = store.root / 'artifacts' / 'alias'
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        relative = checkpoints.commit(key, kind_name, good, revisions=REV, table=table)
        dir_link(alias, store.root / 'artifacts' / kind_name)  # before the snapshot is built
        try:
            builder = writer.begin_snapshot()
            for name, rows in dataset('a').items():
                builder.write_table(name, rows, dim=DIM if 'embeddings' in name else None)
            builder.reference_artifacts([relative])
            writer.publish(builder.finish())
            before = tree_bytes(store.root)
            with pytest.raises(StorageError, match='is a link'):
                checkpoints.commit(key, 'alias', evil, revisions=REV, table=table)
            assert tree_bytes(store.root) == before
            store.verify_snapshot(*store.current())
        finally:
            remove_link(alias)


# ---------------------------------------------------------------- the class: every single-path operation

CASES = {}


def case(function):
    CASES[function.__name__.replace('_', ' ')] = function
    return function


@case
def commit_blob_arm(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link('artifacts/blob')
        with link.refused():
            checkpoints.commit(OTHER, 'blob', b'x', revisions=REV)


@case
def commit_blob_arm_at_the_key_prefix(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link(f'artifacts/blob/{OTHER[:2]}')
        with link.refused():
            checkpoints.commit(OTHER, 'blob', b'x', revisions=REV)


@case
def commit_blob_arm_at_the_artifact_root(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link('artifacts')
        with link.refused():
            checkpoints.commit(OTHER, 'blob', b'x', revisions=REV)


@case
def commit_table_arm(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link('artifacts/observations')
        with link.refused():
            checkpoints.commit(artifact_key('observations', 'image', REV), 'observations',
                               dataset('a')['observations'], revisions=REV, table='observations')


@case
def checkpoint_done(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        assert checkpoints.done(s.key) is not None
        link('artifacts/blob')
        with link.refused():  # refused, not answered "redo it": the redo would write through the link
            checkpoints.done(s.key)


@case
def checkpoint_load(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link('artifacts/blob')
        with link.refused():
            checkpoints.load(s.key)


@case
def checkpoint_queue(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link('registry/sync_state')
        with link.refused():
            checkpoints.queue('a' * 64)


@case
def checkpoint_fail(store, s, link):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        link('registry/sync_state')
        with link.refused():
            checkpoints.fail('a' * 64, 'undecodable')


@case
def builder_reference_artifacts(store, s, link):
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        link('artifacts/blob')
        with link.refused():
            builder.reference_artifacts([s.relative])


@case
def builder_add_file(store, s, link):
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        builder.add_file('index/one.txt', b'one')
        link(f'{build_directory(builder)}/index')
        with link.refused():
            builder.add_file('index/two.txt', b'two')


@case
def builder_write_table(store, s, link):
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        link(build_directory(builder))
        with link.refused():
            builder.write_table('folders', dataset('c')['folders'])


@case
def builder_finish_writes_the_manifest(store, s, link):
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, rows in dataset('c').items():
            builder.write_table(name, rows, dim=DIM if 'embeddings' in name else None)
        link(build_directory(builder))
        with link.refused():
            builder.finish()


@case
def begin_snapshot(store, s, link):
    with store.writer() as writer:
        link('snapshots')
        with link.refused():
            writer.begin_snapshot()


@case
def publish_moves_current(store, s, link):
    with store.writer() as writer:
        sid = build_snapshot(writer, 'c')
        link(f'snapshots/{sid}')
        with link.refused():
            writer.publish(sid)


@case
def publish_reads_the_registry_pointer(store, s, link):
    with store.writer() as writer:
        sid = build_snapshot(writer, 'c')
        link('registry')
        with link.refused():
            writer.publish(sid)


@case
def rollback(store, s, link):
    with store.writer() as writer:
        link(f'snapshots/{s.old}')
        with link.refused():
            writer.rollback(s.old)


@case
def recovery_of_the_registry(store, s, link):
    link('registry')
    with link.refused():
        with store.writer():
            pass


@case
def recovery_of_an_interrupted_build(store, s, link):
    (store.root / BUILDING).mkdir()
    (store.root / BUILDING / 'folders.parquet').write_bytes(b'partial')
    link(BUILDING)
    with link.refused():
        with store.writer():
            pass


@case
def recovery_lists_snapshots(store, s, link):
    link('snapshots')
    with link.refused():
        with store.writer():
            pass


@case
def gc_lists_artifacts(store, s, link):
    with store.writer() as writer:
        link('artifacts/blob')
        with link.refused():
            writer.gc()


@case
def gc_reads_a_live_snapshot(store, s, link):
    with store.writer() as writer:
        link(f'snapshots/{s.old}')
        with link.refused():  # before any mark: the registry is not rewritten
            writer.gc()


@case
def gc_lists_leases(store, s, link):
    """The link is the last component and its target is empty: the rule refuses the link itself, not its content."""
    with store.writer() as writer:
        link(f'leases/{s.new}')
        with link.refused():
            writer.gc()


@case
def gc_lists_holds(store, s, link):
    with store.writer() as writer:
        link(f'holds/{s.new}')
        with link.refused():
            writer.gc()


@case
def hold(store, s, link):
    store.hold(s.new, 'req-1', LATER)
    link(f'holds/{s.new}')
    with link.refused():
        store.hold(s.new, 'req-2', LATER)


@case
def hold_at_the_holds_root(store, s, link):
    link('holds')
    with link.refused():
        store.hold(s.new, 'req-1', LATER)


@case
def release_hold(store, s, link):
    store.hold(s.new, 'req-1', LATER)
    link(f'holds/{s.new}')
    with link.refused():
        store.release_hold(s.new, 'req-1')


@case
def pin_creates_a_lease(store, s, link):
    link(f'leases/{s.new}')
    with link.refused():
        store.pin()


@case
def pin_at_the_leases_root(store, s, link):
    link('leases')
    with link.refused():
        store.pin()


@case
def pin_reads_the_manifest(store, s, link):
    link(f'snapshots/{s.new}')
    with link.refused():
        store.pin()


@case
def pinned_read(store, s, link):
    with store.pin() as pinned:
        link(f'snapshots/{s.new}/index')
        with link.refused():
            pinned.read_file('index/names.txt')


@case
def lease_release(store, s, link):
    """The link appears after the pin: release removes its lease file only through the path rule."""
    real = link(f'leases/{s.new}')
    lease = FileLock(real / 'reader.lease', store.timeout_s, store.poll_s).acquire()
    link.remember()
    with link.refused():
        PinnedSnapshot(store, s.new, [], lease).release()
    assert not lease.held  # the OS lock is released all the same


@pytest.mark.parametrize('name', sorted(CASES))
def test_every_single_path_operation_refuses_a_link_and_changes_no_byte(linked, name):
    store, link = linked
    s = seed(store)
    CASES[name](store, s, link)
    assert link.checked == 1
    assert store.current()[0] == s.new  # the link gone, the store serves what it served before
    with store.pin() as pinned:
        assert pinned.read_file('index/names.txt') == b'index new'


def test_gc_takes_the_path_again_when_it_deletes(linked, monkeypatch):
    """GC lists artifacts when it begins and deletes them later: a link that appears in between is refused."""
    store, link = linked
    seed(store)
    with store.writer() as writer:
        orphan = writer.checkpoints('K2').commit(OTHER, 'blob', b'orphan', revisions=REV)
    artifact_pass = store_module.Writer._gc_artifacts

    def late_link(self, *args):
        link('artifacts/blob')
        return artifact_pass(self, *args)

    monkeypatch.setattr(store_module.Writer, '_gc_artifacts', late_link)
    with store.writer() as writer:
        with link.refused():
            writer.gc()
    assert (store.root / orphan).read_bytes() == b'orphan'
    monkeypatch.undo()
    with store.writer() as writer:  # without the link the orphan is collected (retention 0)
        assert writer.gc().artifacts_removed == [orphan]


def test_a_link_to_an_empty_sibling_is_refused_as_the_last_component(project_root, tmp_path):
    """F1's shape with nothing behind it: the link and its target share a parent and the target is empty, so only
    comparing the last component itself can tell (a mutant that compared parents survived the cases above)."""
    store = make_store(project_root, tmp_path / 'kb')
    s = seed(store)
    (store.root / 'leases' / s.old).mkdir(parents=True)
    alias = store.root / 'leases' / s.new
    dir_link(alias, store.root / 'leases' / s.old)
    try:
        before = tree_bytes(store.root)
        with pytest.raises(StorageError, match='is a link'):
            with store.writer() as writer:
                writer.gc()
        with pytest.raises(StorageError, match='is a link'):
            store.pin()
        assert tree_bytes(store.root) == before
    finally:
        remove_link(alias)


FILE_LINKS = {
    'CURRENT': lambda store, s: store.current(),
    'LOCK': lambda store, s: store.current(),
    'WRITER': lambda store, s: store.writer().__enter__(),
    'registry/REGISTRY': lambda store, s: store.snapshots(),
    'artifact': lambda store, s: _commit_again(store, s),
    'snapshot file': lambda store, s: store.pin(),
}


def _commit_again(store, s):
    with store.writer() as writer:
        writer.checkpoints('K2').commit(s.key, 'blob', b'good', revisions=REV)


def test_a_file_that_is_a_symlink_is_refused(project_root, tmp_path):
    """The same rule for the last component: a store file replaced by a symlink to its own bytes, kept inside or
    outside the store. File symlinks need a privilege on Windows, so this runs where they can be made (Linux CI)."""
    for kind_name in ('inside', 'outside'):
        store = make_store(project_root, tmp_path / kind_name / 'kb')
        s = seed(store)
        base = store.root / 'elsewhere' if kind_name == 'inside' else tmp_path / kind_name / 'outside'
        base.mkdir()
        places = {**{name: name for name in FILE_LINKS}, 'artifact': s.relative,
                  'snapshot file': f'snapshots/{s.new}/folders.parquet'}
        for name, operation in FILE_LINKS.items():
            place, real = store.root / places[name], base / 'real'
            os.rename(place, real)
            try:
                os.symlink(real, place)
            except OSError as error:
                os.rename(real, place)
                pytest.skip(f'file symlinks cannot be created here: {error}')
            before = tree_bytes(store.root, base)
            try:
                with pytest.raises(StorageError, match='is a link' if kind_name == 'inside' else 'outside'):
                    operation(store, s)
                assert tree_bytes(store.root, base) == before, name
            finally:
                os.unlink(place)
                os.rename(real, place)


@pytest.mark.parametrize('place', ['traces', 'traces/request_traces'])
def test_data_store_append_refuses_a_link(project_root, tmp_path, kind, place):
    data = DataStore(tmp_path / 'data', project_root=project_root, table_format='parquet')
    data.append('request_traces', data_rows()['request_traces'])
    link = Links(data.root, kind, tmp_path / 'outside')
    try:
        link(place)
        with link.refused():
            data.append('request_traces', data_rows()['request_traces'])
    finally:
        link.restore()
    assert len(data.read_rows('request_traces')) == 1
