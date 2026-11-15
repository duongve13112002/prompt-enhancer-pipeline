"""Snapshot build, publish, pin, interruption, resume, rollback and garbage collection (R-014, R-017)."""

from datetime import datetime, timedelta, timezone
import os

import pytest
from storage_fixtures import T0, build_snapshot, make_store, publish, tag_of

from prompt_enhancer.storage import StorageError, artifact_key
from prompt_enhancer.storage import store as store_module

REV = {'model': 'obs-r1', 'prompt': 'observer_v0', 'schema': 'f1'}


class Clock:
    """Test double: a settable clock for retention and hold expiry."""

    def __init__(self):
        self.now = T0.timestamp()

    def __call__(self):
        return self.now


@pytest.fixture
def store(project_root, tmp_path):
    return make_store(project_root, tmp_path / 'kb')


def test_publish_pin_and_read(store):
    assert store.pin() is None  # nothing published: the caller warns SNAPSHOT_MISSING
    sid = publish(store, 'a')
    assert (store.root / 'CURRENT').read_bytes().decode().startswith(sid + ' ')
    with store.pin() as pinned:
        assert pinned.snapshot_id == sid and tag_of(pinned) == {'a'}
        with pytest.raises(StorageError, match='not listed in the manifest'):
            pinned.read_file('unlisted.txt')
    with pytest.raises(StorageError, match='released'):
        pinned.read_rows('images')
    assert [row['state'] for row in store.snapshots()] == ['published']


def test_interrupt_after_data_keeps_old_snapshot_and_is_redone(store):
    old = publish(store, 'a')
    with pytest.raises(RuntimeError, match='crash'):
        with store.writer() as writer:
            builder = writer.begin_snapshot(parent=old)
            builder.write_table('folders', [])
            raise RuntimeError('crash')  # stands in for a killed sync after some data
    with store.pin() as pinned:
        assert pinned.snapshot_id == old and tag_of(pinned) == {'a'}
    new = publish(store, 'b')  # the next session removes the .building directory and redoes the build
    states = {row['snapshot_id']: row['state'] for row in store.snapshots()}
    assert states[old] == 'retired' and states[new] == 'published' and list(states.values()).count('failed') == 1
    assert not [p for p in (store.root / 'snapshots').iterdir() if p.name.startswith('.building-')]


def test_interrupt_after_manifest_is_never_read(store, monkeypatch):
    old = publish(store, 'a')
    with pytest.raises(OSError, match='crash'):
        with store.writer() as writer:
            monkeypatch.setattr(store_module.os, 'rename', lambda *a: (_ for _ in ()).throw(OSError('crash')))
            build_snapshot(writer, 'b')
    monkeypatch.undo()
    building = [p for p in (store.root / 'snapshots').iterdir() if p.name.startswith('.building-')]
    assert len(building) == 1 and (building[0] / 'MANIFEST.parquet').exists()
    with store.pin() as pinned:
        assert pinned.snapshot_id == old
    with store.writer():
        pass
    assert not (store.root / 'snapshots' / building[0].name).exists()


def test_interrupt_before_current_then_resume_is_idempotent(store, monkeypatch):
    old = publish(store, 'a')
    with store.writer() as writer:
        new = build_snapshot(writer, 'b')
        monkeypatch.setattr(store_module, 'atomic_write_bytes', lambda *a: (_ for _ in ()).throw(OSError('crash')))
        with pytest.raises(OSError, match='crash'):
            writer.publish(new)
    monkeypatch.undo()
    with store.pin() as pinned:
        assert pinned.snapshot_id == old
    for _ in range(2):  # resuming twice publishes once and changes nothing more
        with store.writer() as writer:
            if store.current()[0] != new:
                writer.publish(new)
    states = {row['snapshot_id']: row['state'] for row in store.snapshots()}
    assert states == {old: 'retired', new: 'published'}


def test_checkpoints_resume_redo_and_idempotence(store):
    keys = [artifact_key('observation', f'sha256:{n:064x}', REV) for n in range(3)]
    with pytest.raises(RuntimeError):
        with store.writer() as writer:
            checkpoints = writer.checkpoints('K2')
            checkpoints.commit(keys[0], 'observation', b'zero', revisions=REV)
            # A crash after the artifact file and before its row: write the file only.
            partial = store.root / f'artifacts/observation/{keys[1][:2]}/{keys[1]}.bin'
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b'half')
            raise RuntimeError('crash')
    written = []
    for _ in range(2):
        with store.writer() as writer:
            checkpoints = writer.checkpoints('K2')
            for key in keys:
                if checkpoints.done(key) is None:
                    written.append(key)
                    checkpoints.commit(key, 'observation', key.encode(), revisions=REV,
                                       call_records=[{'role': 'observer', 'latency_ms': 1.0}])
    assert written == keys[1:]  # only the unfinished artifacts are redone, and only once
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        assert checkpoints.load(keys[1]) == keys[1].encode()
        assert checkpoints.state(keys[2])['call_records'] == [{'role': 'observer', 'latency_ms': 1.0}]
    assert len(list((store.root / 'registry' / 'sync_state').iterdir())) == 1  # compacted


def test_resume_false_redoes_done_artifacts(project_root, tmp_path):
    key = artifact_key('observation', 'sha256:' + '0' * 64, REV)
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        writer.checkpoints('K2').commit(key, 'observation', b'x', revisions=REV)
    fresh = make_store(project_root, tmp_path / 'kb', resume=False)
    with fresh.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        assert checkpoints.done(key) is None
        checkpoints.commit(key, 'observation', b'x', revisions=REV)
        assert checkpoints.done(key) is not None
    with store.writer() as writer:
        assert writer.checkpoints('K2').done(key) is not None


def test_queue_and_fail_states(store):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        checkpoints.queue('a' * 64, revisions=REV)
        checkpoints.fail('b' * 64, 'cannot decode')
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        assert checkpoints.state('a' * 64)['state'] == 'queued' and checkpoints.done('a' * 64) is None
        assert checkpoints.state('b' * 64)['error'] == 'cannot decode'
        with pytest.raises(StorageError, match='no done artifact'):
            checkpoints.load('b' * 64)


def test_checksum_size_and_pointer_corruption_refused(store):
    sid = publish(store, 'a')
    images = store.root / 'snapshots' / sid / 'images.parquet'
    data = bytearray(images.read_bytes())
    data[len(data) // 2] ^= 0xFF
    images.write_bytes(bytes(data))  # same size, other bytes
    store_module._VERIFIED.clear()
    with store.pin() as pinned:
        with pytest.raises(StorageError, match='checksum'):
            pinned.read_table('images')
    with store.pin() as pinned:
        images.write_bytes(bytes(data) + b'x')  # grows after the pin: refused by size, before any hashing
        with pytest.raises(StorageError, match='size .* differs from the manifest'):
            pinned.read_table('images')
    with pytest.raises(StorageError, match='size'):
        store.pin()
    manifest = store.root / 'snapshots' / sid / 'MANIFEST.parquet'
    manifest.write_bytes(manifest.read_bytes() + b'x')
    with pytest.raises(StorageError, match='manifest checksum'):
        store.pin()
    (store.root / 'CURRENT').write_bytes(b'garbage')
    with pytest.raises(StorageError, match='corrupt pointer'):
        store.pin()


def test_artifact_checksum_refused(store):
    key = artifact_key('observation', 'sha256:' + '1' * 64, REV)
    with store.writer() as writer:
        relative = writer.checkpoints('K2').commit(key, 'observation', b'good', revisions=REV)
    (store.root / relative).write_bytes(b'evil')
    store_module._VERIFIED.clear()
    with store.writer() as writer:
        with pytest.raises(StorageError, match='checksum'):
            writer.checkpoints('K2').load(key)


def test_rollback_and_collected_target_refused(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    first = publish(store, 'a')
    second = publish(store, 'b')
    with store.writer() as writer:
        writer.rollback(first)
    with store.pin() as pinned:
        assert pinned.snapshot_id == first and tag_of(pinned) == {'a'}
    with store.writer() as writer:
        with pytest.raises(StorageError, match='not a retained snapshot'):
            writer.rollback(first)  # already current
        report = writer.gc()
        assert report.collected == [second]
        with pytest.raises(StorageError, match='not a retained snapshot: collected'):
            writer.rollback(second)
        with pytest.raises(StorageError, match='invalid snapshot id'):
            writer.rollback('../etc')


def test_rollback_to_damaged_snapshot_refused(store):
    first = publish(store, 'a')
    publish(store, 'b')
    (store.root / 'snapshots' / first / 'cards.parquet').write_bytes(b'damaged')
    store_module._VERIFIED.clear()
    with store.writer() as writer:
        with pytest.raises(StorageError):
            writer.rollback(first)


def test_gc_keep_leases_and_holds(project_root, tmp_path):
    clock = Clock()
    store = make_store(project_root, tmp_path / 'kb', keep=1, clock=clock)
    ids = []
    for tag in 'abcde':
        clock.now += 60
        ids.append(publish(store, tag))
    a, b, c, d, e = ids
    pinned = store.pin()  # a reader on e, which becomes retired below
    store.hold(b, 'request-1', datetime.fromtimestamp(clock.now, timezone.utc) + timedelta(hours=1))
    clock.now += 60
    with store.writer() as writer:
        writer.rollback(d)  # retired by recency: e (newest, also leased), c, b (held), a
        report = writer.gc()
    assert report.leased == [e] and report.held == [b] and report.kept == [e]
    assert report.collected == [a, c]
    states = {row['snapshot_id']: row['state'] for row in store.snapshots()}
    assert states == {a: 'collected', b: 'retired', c: 'collected', d: 'published', e: 'retired'}
    assert tag_of(pinned) == {'e'}  # the pinned reader keeps a consistent view through GC
    pinned.release()
    clock.now += 7200  # the hold expires and the lease is gone
    with store.writer() as writer:
        report = writer.gc()
    assert report.kept == [e] and report.collected == [b] and not report.leased and not report.held


def test_hold_refused_for_collected_or_unknown(store):
    with pytest.raises(StorageError, match='cannot be held'):
        store.hold('s20261115T120000000000Z-0123abcd', 'r1', T0)
    sid = publish(store, 'a')
    with pytest.raises(StorageError, match='invalid request id'):
        store.hold(sid, '../x', T0)
    with pytest.raises(StorageError, match='timezone-aware'):
        store.hold(sid, 'r1', T0.replace(tzinfo=None))


def test_gc_artifact_retention(project_root, tmp_path):
    clock = Clock()
    store = make_store(project_root, tmp_path / 'kb', retention_s=86400, clock=clock)
    kept_key = artifact_key('card', 'sha256:' + '2' * 64, REV)
    orphan_key = artifact_key('card', 'sha256:' + '3' * 64, REV)
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K3')
        kept = checkpoints.commit(kept_key, 'card', b'kept', revisions=REV)
        orphan = checkpoints.commit(orphan_key, 'card', b'orphan', revisions=REV)
        leftover = store.root / 'artifacts' / 'card' / '.x.bin.0.tmp'
        leftover.write_bytes(b'partial')
        builder = writer.begin_snapshot()
        for name, rows in __import__('storage_fixtures').dataset('a').items():
            builder.write_table(name, rows, dim=4 if 'embeddings' in name else None)
        builder.reference_artifacts([kept])
        writer.publish(builder.finish())
        first = writer.gc()
    assert first.artifacts_marked == [orphan] and not first.artifacts_removed and not leftover.exists()
    clock.now += 86399
    with store.writer() as writer:
        assert not writer.gc().artifacts_removed
    clock.now += 1
    with store.writer() as writer:
        assert writer.gc().artifacts_removed == [orphan]
        checkpoints = writer.checkpoints('K3')
        assert checkpoints.done(orphan_key) is None and checkpoints.done(kept_key) is not None
    assert (store.root / kept).exists() and not (store.root / orphan).exists()


def test_second_writer_refused(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    other = make_store(project_root, tmp_path / 'kb', timeout_s=0.2, poll_s=0.05)
    with store.writer():
        with pytest.raises(StorageError, match='lock not acquired'):
            with other.writer():
                pass


def test_unknown_snapshot_and_missing_tables_refused(store):
    with store.writer() as writer:
        with pytest.raises(StorageError, match='not validated'):
            writer.publish('s20261115T120000000000Z-0123abcd')
        builder = writer.begin_snapshot()
        builder.write_table('folders', [])
        with pytest.raises(StorageError, match='missing tables'):
            builder.finish()
        with pytest.raises(StorageError, match='already written'):
            builder.write_table('folders', [])
        with pytest.raises(StorageError, match='not a snapshot table'):
            builder.write_table('request_traces', [])
        with pytest.raises(StorageError, match='invalid relative path'):
            builder.add_file('../escape', b'x')


def test_finish_refuses_dangling_reference(store):
    from storage_fixtures import dataset
    from prompt_enhancer.storage import image_id
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        rows = dataset('a')
        rows['folder_images'].append(dict(rows['folder_images'][0], relative_path='z.png', image_id=image_id(b'?')))
        for name, value in rows.items():
            builder.write_table(name, value, dim=4 if 'embeddings' in name else None)
        with pytest.raises(StorageError, match='dangling reference'):
            builder.finish()
    assert store.pin() is None and os.path.isdir(store.root / 'snapshots')
