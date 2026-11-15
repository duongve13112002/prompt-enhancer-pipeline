"""Fix round 1: one test per review finding (F1-F10, G4) plus class tests (R-014, R-015, R-017, R-025)."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
import pyarrow.parquet as pq
import pytest
import yaml
from storage_fixtures import DIM, T0, build_snapshot, data_rows, dataset, make_store, publish

from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer.storage import Catalog, DataStore, StorageError, artifact_key, open_knowledge_store
from prompt_enhancer.storage import store as store_module
from prompt_enhancer.storage.tables import MANIFEST_SCHEMA

REV = {'model': 'm', 'prompt': 'p', 'schema': 's'}
LATER = T0 + timedelta(days=1)
EXAMPLE = Path(__file__).resolve().parents[1] / 'configs' / 'enhancer.example.yaml'


def dir_link(link, target):
    """A directory link that leaves the store: a junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def remove_link(link):
    if os.name == 'nt':
        os.rmdir(link)  # removes the junction itself, not its target
    else:
        os.unlink(link)


def files_under(path):
    return [p for p in Path(path).rglob('*') if p.is_file()]


# ---------------------------------------------------------------- F1, F2 and the containment class


def test_f1_release_hold_refuses_a_path(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store, 'a')
    victim = tmp_path / 'victim.parquet'
    victim.write_bytes(b'keep')
    for request in (victim.as_posix().removesuffix('.parquet'), '../../victim', 'a/b', ''):
        with pytest.raises(StorageError, match='invalid request id'):
            store.release_hold(sid, request)
    assert victim.read_bytes() == b'keep'


def _commit(store):
    with store.writer() as writer:
        writer.checkpoints('K2').commit('b' * 64, 'blob', b'escaped', revisions=REV)


def _pin(store):
    store.pin()


def _hold(store):
    store.hold(store.current()[0], 'r1', LATER)


def _begin(store):
    with store.writer() as writer:
        writer.begin_snapshot()


# Every store directory a write can land in, with the operation that writes there (class sweep).
WRITE_PATHS = [
    ('artifacts', False, _commit),
    ('registry/sync_state', False, _commit),
    ('registry', False, _begin),
    ('snapshots', False, _begin),
    ('leases', True, _pin),
    ('holds', True, _hold),
]


@pytest.mark.parametrize('relative, published, operation', WRITE_PATHS, ids=[case[0] for case in WRITE_PATHS])
def test_f2_every_write_path_refuses_a_link_out_of_the_store(project_root, tmp_path, relative, published, operation):
    store = make_store(project_root, tmp_path / 'kb')
    if published:
        publish(store, 'a')
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = store.root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    dir_link(link, outside)
    try:
        with pytest.raises(StorageError, match='outside'):
            operation(store)
    finally:
        remove_link(link)
    assert files_under(outside) == []


def test_f2_factory_and_data_store_refuse_links(project_root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    data = DataStore(tmp_path / 'data', project_root=project_root, table_format='parquet')
    (tmp_path / 'data').mkdir()
    dir_link(tmp_path / 'data' / 'traces', outside)
    try:
        with pytest.raises(StorageError, match='outside'):
            data.append('request_traces', data_rows()['request_traces'])
    finally:
        remove_link(tmp_path / 'data' / 'traces')
    assert files_under(outside) == []


# ---------------------------------------------------------------- F3, G4: decisions acted on under their lock


def test_f3_hold_after_gc_selection_is_refused(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    real, refused = store_module.remove_entry, []

    def remove_after_hold(root, relative, **kwargs):  # scheduling hook: GC's first removal of the old snapshot
        if relative.startswith(f'snapshots/{old}/') and not refused:
            with pytest.raises(StorageError, match='cannot be held: deleting') as error:
                store.hold(old, 'late', LATER)
            refused.append(str(error.value))
        return real(root, relative, **kwargs)
    monkeypatch.setattr(store_module, 'remove_entry', remove_after_hold)
    with store.writer() as writer:
        report = writer.gc()
    assert refused and report.collected == [old]
    assert not (store.root / 'holds' / old).exists()
    with store.writer() as writer:
        with pytest.raises(StorageError, match='not a retained snapshot: collected'):
            writer.rollback(old)


def test_f3_hold_admitted_first_protects(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    store.hold(old, 'early', datetime.now(timezone.utc) + timedelta(days=1))
    with store.writer() as writer:
        report = writer.gc()
    assert report.held == [old] and report.collected == [] and (store.root / 'snapshots' / old).is_dir()


def test_f3_failed_delete_stays_deleting_and_is_retried(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    real_remove = store_module.remove_entry

    def busy(root, relative, **kwargs):  # labelled double: a foreign handle blocks every snapshot-entry removal
        if relative.startswith('snapshots/'):
            raise PermissionError('busy')
        return real_remove(root, relative, **kwargs)
    monkeypatch.setattr(store_module, 'remove_entry', busy)
    with store.writer() as writer:
        assert writer.gc().skipped[0][0] == old
    monkeypatch.undo()
    assert {r['snapshot_id']: r['state'] for r in store.snapshots()}[old] == 'deleting'
    with pytest.raises(StorageError, match='cannot be held: deleting'):
        store.hold(old, 'r1', LATER)
    with store.writer() as writer:
        assert writer.gc().collected == [old]


def test_g4_gc_skips_this_sessions_unpublished_snapshot(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    publish(store, 'a')
    with store.writer() as writer:
        pending = build_snapshot(writer, 'b')
        report = writer.gc()
        assert report.session == [pending] and pending not in report.collected
        writer.publish(pending)
    with store.pin() as pinned:
        assert pinned.snapshot_id == pending
    with store.writer() as writer:
        orphan = build_snapshot(writer, 'c')
    with store.writer() as writer:  # a later session may collect a never-published snapshot
        assert orphan in writer.gc().collected


# ---------------------------------------------------------------- F4, F6, F8, F10: evidence verified before trust


def _build_with_artifact(store, writer, tag='a', mutate=None):
    path = writer.checkpoints('K2').commit(artifact_key('blob', tag, REV), 'blob', b'good', revisions=REV)
    builder = writer.begin_snapshot()
    for name, rows in dataset(tag).items():
        builder.write_table(name, rows, dim=DIM if 'embeddings' in name else None)
    builder.reference_artifacts([path])
    if mutate:
        mutate(store.root / path)
    return builder, path


@pytest.mark.parametrize('mutate, message', [
    (lambda p: p.unlink(), 'missing'),
    (lambda p: p.write_bytes(b'evil'), 'checksum'),
    (lambda p: p.write_bytes(b'longer bytes'), 'size'),
])
def test_f4_finish_verifies_referenced_artifacts(project_root, tmp_path, mutate, message):
    store = make_store(project_root, tmp_path / 'kb')
    store_module._VERIFIED.clear()
    with store.writer() as writer:
        builder, _ = _build_with_artifact(store, writer, mutate=mutate)
        with pytest.raises(StorageError, match=message):
            builder.finish()
    assert store.pin() is None


def test_f4_publish_verifies_referenced_artifacts(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        builder, path = _build_with_artifact(store, writer)
        sid = builder.finish()
        (store.root / path).unlink()
        with pytest.raises(StorageError, match='missing'):
            writer.publish(sid)
    assert store.pin() is None


@pytest.mark.parametrize('change', ['revision', 'dim'])
def test_f6_one_embedding_space_per_snapshot(project_root, tmp_path, change):
    store = make_store(project_root, tmp_path / 'kb')
    rows = dataset('a')
    text_dim = 8 if change == 'dim' else DIM
    for row in rows['concept_text_embeddings']:
        row.update(dim=text_dim, vector=[0.5] * text_dim,
                   embedder_revision='other' if change == 'revision' else row['embedder_revision'])
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, value in rows.items():
            dim = text_dim if name == 'concept_text_embeddings' else DIM if 'embeddings' in name else None
            builder.write_table(name, value, dim=dim)
        with pytest.raises(StorageError, match='mixes embedding spaces'):
            builder.finish()


def test_f8_done_refuses_same_size_corruption(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    key = artifact_key('blob', 'source', REV)
    with store.writer() as writer:
        relative = writer.checkpoints('K2').commit(key, 'blob', b'good', revisions=REV)
    (store.root / relative).write_bytes(b'evil')
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        assert checkpoints.done(key) is None  # redone, not skipped
        checkpoints.commit(key, 'blob', b'good', revisions=REV)
        assert checkpoints.done(key) is not None and checkpoints.load(key) == b'good'


def test_f10_tags_are_required(project_root, tmp_path):
    catalog = Catalog(project_root)
    table = catalog.build('folders', dataset('a')['folders'])
    with pytest.raises(StorageError, match='tagged as table None'):
        catalog.validate('folders', table.replace_schema_metadata(None))
    with pytest.raises(StorageError, match='unsupported physical version None'):
        catalog.validate('folders', table.replace_schema_metadata({'pe.table': 'folders'}))
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store, 'a')
    manifest = store.root / 'snapshots' / sid / 'MANIFEST.parquet'
    pq.write_table(pq.read_table(manifest).replace_schema_metadata(None), manifest)
    # CURRENT's manifest hash would refuse first, so check the tag path directly.
    with pytest.raises(StorageError, match='manifest: file is tagged as table None'):
        store._manifest(store._snapshot_dir(sid))
    assert MANIFEST_SCHEMA.metadata[b'pe.table'] == b'manifest'


# ---------------------------------------------------------------- F7 and the session class


def _stale_helpers(store):
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        builder = writer.begin_snapshot()
        builder.write_table('folders', [])
    return writer, checkpoints, builder


HELPER_CALLS = {
    'checkpoints.commit': lambda w, c, b: c.commit('a' * 64, 'blob', b'x', revisions=REV),
    'checkpoints.queue': lambda w, c, b: c.queue('a' * 64),
    'checkpoints.fail': lambda w, c, b: c.fail('a' * 64, 'e'),
    'checkpoints.done': lambda w, c, b: c.done('a' * 64),
    'checkpoints.load': lambda w, c, b: c.load('a' * 64),
    'checkpoints.state': lambda w, c, b: c.state('a' * 64),
    'builder.write_table': lambda w, c, b: b.write_table('concepts', []),
    'builder.add_file': lambda w, c, b: b.add_file('x.txt', b'x'),
    'builder.reference_artifacts': lambda w, c, b: b.reference_artifacts([]),
    'builder.write_manifest': lambda w, c, b: b.write_manifest(),
    'builder.finish': lambda w, c, b: b.finish(),
    'writer.checkpoints': lambda w, c, b: w.checkpoints('K2'),
    'writer.begin_snapshot': lambda w, c, b: w.begin_snapshot(),
    'writer.publish': lambda w, c, b: w.publish('s20261115T120000000000Z-0123abcd'),
    'writer.rollback': lambda w, c, b: w.rollback('s20261115T120000000000Z-0123abcd'),
    'writer.gc': lambda w, c, b: w.gc(),
}


def test_f7_every_helper_refuses_after_its_writer_closed(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    writer, checkpoints, builder = _stale_helpers(store)
    with store.writer() as other:  # another session owns WRITER now
        for name, call in HELPER_CALLS.items():
            with pytest.raises(StorageError, match='outlived its writer'):
                call(writer, checkpoints, builder)
        assert other.checkpoints('K2').state('a' * 64) is None
    assert not (builder.directory / 'x.txt').exists()


# ---------------------------------------------------------------- F5: crash at every step of the registry write


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=1, max_value=5), st.sampled_from(['parquet', 'arrow']),
       st.sampled_from(['parquet', 'arrow']))
def test_f5_registry_write_recovers_from_a_crash_at_any_step(project_root, tmp_path_factory, step, before, after):
    root = tmp_path_factory.mktemp('registry') / 'kb'
    publish(make_store(project_root, root, table_format=before), 'a')
    store = make_store(project_root, root, table_format=after)
    with store.store_lock():
        rows = store._registry()
        old = {sid: dict(row) for sid, row in rows.items()}
        for row in rows.values():
            row['source_scan_summary'] = {'marker': 'new'}
        calls = {'n': 0}
        originals = {name: getattr(store_module, name)
                     for name in ('write_table_file', 'atomic_write_bytes', 'remove_entry')}

        def crashing(name):
            def call(*args, **kwargs):
                calls['n'] += 1
                if calls['n'] == step:
                    raise OSError(f'crash at step {step}')
                return originals[name](*args, **kwargs)
            return call
        for name in originals:
            setattr(store_module, name, crashing(name))
        try:
            store._write_registry(rows)
            crashed = False
        except OSError:
            crashed = True
        finally:
            for name, original in originals.items():
                setattr(store_module, name, original)
    fresh = make_store(project_root, root, table_format=after)
    with fresh.writer():  # recovery must succeed whatever the step
        pass
    seen = {row['snapshot_id']: row['source_scan_summary'] for row in fresh.snapshots()}
    assert seen == {sid: {'marker': 'new'} for sid in old} or (crashed and seen == {
        sid: row['source_scan_summary'] for sid, row in old.items()})
    leftovers = [p.name for p in (root / 'registry').iterdir() if p.name.startswith('snapshots')]
    assert len(leftovers) == 1 and (root / 'registry' / 'REGISTRY').read_text().strip() == leftovers[0]
    publish(fresh, 'b')  # and the store keeps working


# ---------------------------------------------------------------- F9: finite, positive lock values


@pytest.mark.parametrize('key', ['timeout_s', 'poll_s'])
@pytest.mark.parametrize('bad', [float('inf'), float('-inf'), 0, -1])
def test_f9_loader_refuses_non_finite_or_non_positive(project_root, key, bad):
    config = yaml.safe_load(EXAMPLE.read_bytes())
    config['storage']['kb_store'] = 'data/kb-f9'
    config['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.01, key: bad}
    path = project_root / f'f9-{key}.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    with pytest.raises(ConfigurationError, match=f'storage.lock.{key}'):
        load_config(path, 'kb status', project_root=project_root)


@pytest.mark.parametrize('key', ['timeout_s', 'poll_s'])
@pytest.mark.parametrize('bad', [float('nan'), float('inf'), 0, -0.5, True])
def test_f9_store_refuses_non_finite_or_non_positive(project_root, tmp_path, key, bad):
    config = {'storage': {'kb_store': 'data/kb-f9', 'lock': {'timeout_s': 5, 'poll_s': 0.01, key: bad}}}
    with pytest.raises(StorageError, match=f'storage.lock.{key}: invalid value'):
        open_knowledge_store(config, 'kb status', project_root=project_root)
    with pytest.raises(StorageError, match='finite and positive'):
        make_store(project_root, tmp_path / 'kb', **{key: bad})


def test_f9_nan_passes_the_schema_but_not_the_store(project_root):
    """JSON Schema bounds cannot refuse NaN (every comparison is false), so the store is the check that does."""
    config = yaml.safe_load(EXAMPLE.read_bytes())
    config['storage']['kb_store'] = 'data/kb-nan'
    config['storage']['lock'] = {'timeout_s': float('nan'), 'poll_s': 0.01}
    path = project_root / 'f9-nan.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=project_root)
    with pytest.raises(StorageError, match='timeout_s: invalid value nan'):
        open_knowledge_store(loaded, 'kb status', project_root=project_root)


def test_hold_table_is_tagged(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store, 'a')
    store.hold(sid, 'r1', LATER)
    table = pq.read_table(store.root / 'holds' / sid / 'r1.parquet')
    assert table.schema.metadata[b'pe.table'] == b'hold'
