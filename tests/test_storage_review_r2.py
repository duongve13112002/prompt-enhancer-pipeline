"""Fix round 2: the store re-checks a fact where it acts on it (R-014, R-017, R-025).

R2 and its class: publish, rollback and resume verify again; referenced artifacts are immutable.
R1 and its class: every traversal refuses a link before it lists anything behind it.
R3 and its class: a Writer is one session; its helpers never act in another.
"""

import os
from pathlib import Path
import sys

import pytest
from storage_fixtures import DIM, data_rows, dataset, make_store, publish
from test_storage_review_r1 import HELPER_CALLS, REV, dir_link, remove_link

from prompt_enhancer.storage import DataStore, StorageError, artifact_key

# One audit hook for the module (hooks cannot be removed): it records directory listings under OUTSIDE.
OUTSIDE, LISTED = [], []


def _audit(event, args):
    if OUTSIDE and event in ('os.scandir', 'os.listdir') and args and isinstance(args[0], (str, bytes)):
        if Path(os.fsdecode(args[0])).resolve().is_relative_to(OUTSIDE[0]):
            LISTED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def tamper(path, data):
    """Rewrite a file in place with same-size bytes and restore its mtime, so only a fresh hash can tell."""
    before = path.stat()
    assert len(data) == before.st_size
    path.write_bytes(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def flip(path):
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tamper(path, bytes(data))


def build(writer, tag='a', artifact=b'good', key_tag='source'):
    """A finished snapshot referencing one blob artifact; returns (snapshot id, artifact path, input key)."""
    key = artifact_key('blob', key_tag, REV)
    relative = writer.checkpoints('K2').commit(key, 'blob', artifact, revisions=REV)
    builder = writer.begin_snapshot()
    for name, rows in dataset(tag).items():
        builder.write_table(name, rows, dim=DIM if 'embeddings' in name else None)
    builder.reference_artifacts([relative])
    return builder.finish(), relative, key


# ---------------------------------------------------------------- R2: acting points verify again


def test_r2_commit_cannot_change_an_artifact_a_validated_snapshot_references(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        sid, relative, key = build(writer)
        checkpoints = writer.checkpoints('K2')
        with pytest.raises(StorageError, match='referenced by a snapshot'):
            checkpoints.commit(key, 'blob', b'evil', revisions=REV)
        assert (store.root / relative).read_bytes() == b'good'
        checkpoints.commit(key, 'blob', b'good', revisions=REV)  # the recorded bytes may be written again
        writer.publish(sid)
    assert store.current()[0] == sid


def test_r2_commit_cannot_change_an_artifact_of_a_published_snapshot_in_a_later_session(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        sid, relative, key = build(writer)
        writer.publish(sid)
    with store.writer() as writer:  # a new session learns the references from the manifests
        with pytest.raises(StorageError, match='referenced by a snapshot'):
            writer.checkpoints('K2').commit(key, 'blob', b'evil', revisions=REV)
    assert (store.root / relative).read_bytes() == b'good'


def test_r2_commit_cannot_change_an_artifact_an_unfinished_build_references(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        key = artifact_key('blob', 'source', REV)
        relative = checkpoints.commit(key, 'blob', b'good', revisions=REV)
        checkpoints.commit(key, 'blob', b'better', revisions=REV)  # nothing references it yet
        writer.begin_snapshot().reference_artifacts([relative])
        with pytest.raises(StorageError, match='referenced by a snapshot'):
            checkpoints.commit(key, 'blob', b'evil!!', revisions=REV)


def test_r2_a_collected_snapshot_no_longer_freezes_its_artifacts(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=86400)
    with store.writer() as writer:
        old, relative, key = build(writer, 'old')
        writer.publish(old)
    publish(store, 'new')
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        checkpoints.commit('c' * 64, 'blob', b'other', revisions=REV)  # the reference cache now holds `old`
        assert old in writer.gc().collected
        checkpoints.commit(key, 'blob', b'evil', revisions=REV)
    assert (store.root / relative).read_bytes() == b'evil'


def test_r2_a_referenced_table_artifact_is_immutable(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    rows = dataset('a')['observations']
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        key = artifact_key('observations', 'image', REV)
        relative = checkpoints.commit(key, 'observations', rows, revisions=REV, table='observations')
        writer.begin_snapshot().reference_artifacts([relative])
        before = (store.root / relative).read_bytes()
        with pytest.raises(StorageError, match='referenced by a snapshot'):
            checkpoints.commit(key, 'observations', rows[:1], revisions=REV, table='observations')
        assert (store.root / relative).read_bytes() == before
        assert not [p for p in (store.root / relative).parent.iterdir() if p.name.endswith('.tmp')]


@pytest.mark.parametrize('target', ['artifact', 'table', 'manifest'])
def test_r2_publish_verifies_every_byte_under_the_lock(project_root, tmp_path, target):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        sid, relative, _ = build(writer)  # finish hashed every file: the per-process cache is warm
        directory = store.root / 'snapshots' / sid
        flip({'artifact': store.root / relative, 'table': directory / 'folders.parquet',
              'manifest': directory / 'MANIFEST.parquet'}[target])
        with pytest.raises(StorageError, match='checksum'):
            writer.publish(sid)
    assert store.current() is None and store.pin() is None


def test_r2_rollback_verifies_with_a_fresh_hash(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=2)
    with store.writer() as writer:
        old, relative, _ = build(writer, 'old')
        writer.publish(old)
    new = publish(store, 'new')
    flip(store.root / relative)
    with store.writer() as writer:
        with pytest.raises(StorageError, match='checksum'):
            writer.rollback(old)
    assert store.current()[0] == new


def test_r2_resume_rehashes_a_done_artifact(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    key = artifact_key('blob', 'source', REV)
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        relative = checkpoints.commit(key, 'blob', b'good', revisions=REV)
        assert checkpoints.done(key) is not None  # hashed once: the cache is warm
        tamper(store.root / relative, b'evil')
        assert checkpoints.done(key) is None  # redone, not skipped


def test_r2_pin_refuses_a_tampered_manifest(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store, 'a')
    flip(store.root / 'snapshots' / sid / 'MANIFEST.parquet')
    with pytest.raises(StorageError, match='manifest checksum differs'):
        store.pin()


# ---------------------------------------------------------------- R1: no traversal enters a link


def _gc(store):
    with store.writer() as writer:
        writer.gc()


def _checkpoints(store):
    with store.writer() as writer:
        writer.checkpoints('K2')


def _open_writer(store):
    with store.writer():
        pass


# (link placed at, needs a published snapshot, operation that traverses there)
TRAVERSALS = [
    ('artifacts/linked', False, _gc),
    ('artifacts/blob/linked', False, _gc),
    ('leases/s20261115T120000000000Z-0123abcd', True, _gc),
    ('holds/s20261115T120000000000Z-0123abcd', True, _gc),
    ('registry/sync_state', True, _checkpoints),
    ('registry/linked', True, _open_writer),
    ('snapshots/linked', True, _open_writer),
    ('snapshots/.building-s20261115T120000000000Z-0123abcd/linked', True, _open_writer),
]


@pytest.mark.parametrize('relative, published, operation', TRAVERSALS, ids=[case[0] for case in TRAVERSALS])
@pytest.mark.parametrize('populated', [False, True])
def test_r1_every_traversal_refuses_a_link_out_before_listing_it(project_root, tmp_path, relative, published,
                                                                 operation, populated):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    if published:
        publish(store, 'a')
    outside = tmp_path / 'outside'
    outside.mkdir()
    if populated:
        (outside / 'victim').write_bytes(b'keep')
    link = store.root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    dir_link(link, outside)
    LISTED.clear()
    OUTSIDE[:] = [outside.resolve()]
    try:
        with pytest.raises(StorageError, match='outside'):
            operation(store)
    finally:
        OUTSIDE.clear()
        remove_link(link)
    assert LISTED == []
    assert [p.name for p in outside.iterdir()] == (['victim'] if populated else [])


def test_r1_gc_refuses_a_link_back_into_the_store(project_root, tmp_path):
    """A link inside the store would show snapshot files as orphan artifacts; GC refuses it instead."""
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store, 'a')
    link = store.root / 'artifacts' / 'loop'
    link.parent.mkdir(parents=True)
    dir_link(link, store.root / 'snapshots' / sid)
    try:
        with pytest.raises(StorageError, match='link'):
            _gc(store)
    finally:
        remove_link(link)
    with store.pin() as pinned:
        assert pinned.read_file('index/names.txt') == b'index a'


def test_r1_gc_skips_a_snapshot_with_a_link_inside_and_retries(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'victim').write_bytes(b'keep')
    link = store.root / 'snapshots' / old / 'linked'
    dir_link(link, outside)
    LISTED.clear()
    OUTSIDE[:] = [outside.resolve()]
    try:
        with store.writer() as writer:
            report = writer.gc()
    finally:
        OUTSIDE.clear()
    assert [sid for sid, _ in report.skipped] == [old] and 'outside' in report.skipped[0][1]
    assert LISTED == [] and (outside / 'victim').read_bytes() == b'keep'
    assert {row['snapshot_id']: row['state'] for row in store.snapshots()}[old] == 'deleting'
    remove_link(link)
    with store.writer() as writer:
        assert writer.gc().collected == [old]


def test_r1_data_store_listing_refuses_a_link(project_root, tmp_path):
    data = DataStore(tmp_path / 'data', project_root=project_root, table_format='parquet')
    data.append('request_traces', data_rows()['request_traces'])
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'data' / 'traces' / 'request_traces' / 'linked'
    dir_link(link, outside)
    try:
        with pytest.raises(StorageError, match='outside'):
            data.read('request_traces')
    finally:
        remove_link(link)


# ---------------------------------------------------------------- R3: one Writer, one session


def test_r3_a_writer_cannot_be_entered_again(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', resume=False)
    writer = store.writer()
    with writer:
        checkpoints = writer.checkpoints('K2')
        checkpoints.commit('a' * 64, 'blob', b'good', revisions=REV)
        builder = writer.begin_snapshot()
    with pytest.raises(StorageError, match='single-use'):
        with writer:
            pass
    with pytest.raises(StorageError, match='outlived its writer'):
        builder.add_file('after.txt', b'x')
    assert not (builder.directory / 'after.txt').exists()
    with store.writer() as fresh:  # a new session: new run id, so resume false redoes earlier output
        assert fresh.run_id != writer.run_id
        assert fresh.checkpoints('K2').done('a' * 64) is None


@pytest.mark.parametrize('name', sorted(HELPER_CALLS))
def test_r3_every_helper_refuses_after_its_writer_is_entered_again(project_root, tmp_path, name):
    store = make_store(project_root, tmp_path / 'kb')
    writer = store.writer()
    with writer:
        checkpoints = writer.checkpoints('K2')
        builder = writer.begin_snapshot()
        builder.write_table('folders', [])
    with pytest.raises(StorageError, match='single-use'):
        writer.__enter__()
    with pytest.raises(StorageError, match='outlived its writer'):
        HELPER_CALLS[name](writer, checkpoints, builder)
    with store.writer():  # the refused re-entry left no lock behind
        pass
