"""Fix round 3: one identity rule and one serialization rule (R-014, R-017, R-025).

Identity: every admission point refuses each alias form of a name (case, final dot or space, NFD, NTFS stream,
device name, 8.3 short name, backslash) instead of letting two spellings reach one file.
Serialization: every mutating Writer operation, paused between its check and its act, keeps a racing publish
or GC from a second thread out until it is done; the lock order never inverts; a lease sharing violation is
"still live", never a crash.
"""

from datetime import datetime, timedelta, timezone
import os
import threading
import unicodedata

import pyarrow as pa
import pytest
from storage_fixtures import DIM, dataset, make_store, publish
from test_storage_review_r1 import REV

from prompt_enhancer.storage import StorageError, _fs, artifact_key
from prompt_enhancer.storage import store as store_module
from prompt_enhancer.storage.identity import check_artifact_path
from prompt_enhancer.storage.tables import MANIFEST_SCHEMA

LATER = datetime.now(timezone.utc) + timedelta(days=1)
PAUSE_S = 0.5  # how long a racing thread must stay blocked; the paused operation itself waits up to 10 s


def aliases(name):
    """Each alias form of a name whose last segment is `stem.ext`, as a file system could map it."""
    head, _, last = name.rpartition('/')
    head = head + '/' if head else ''
    stem, _, ext = last.partition('.')
    return {
        'case': name.upper() if name.upper() != name else name.capitalize(),
        'trailing dot': name + '.',
        'trailing space': name + ' ',
        'nfd': unicodedata.normalize('NFD', name) if unicodedata.normalize('NFD', name) != name
        else head + 'é' + last,
        'stream': name + ':s',
        'device': head + 'nul',
        'device with extension': head + 'CON.txt',
        'short name': head + f'{stem[:6].upper()}~1.{ext[:3].upper()}',
        'backslash': name.replace('/', '\\') if '/' in name else 'x\\' + name,
    }


# ---------------------------------------------------------------- identity: every admission point, every alias


def _committed(writer, tag='m'):
    return writer.checkpoints('K2').commit(artifact_key('blob', tag, REV), 'blob', b'good', revisions=REV)


@pytest.mark.parametrize('form', sorted(aliases('artifacts/blob/ab/x.bin')))
def test_reference_artifacts_refuses_every_alias(project_root, tmp_path, form):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        relative = _committed(writer)
        builder = writer.begin_snapshot()
        with pytest.raises(StorageError):
            builder.reference_artifacts([aliases(relative)[form]])
        assert builder.entries == []
        builder.reference_artifacts([relative])  # the generated spelling is admitted


def test_reference_artifacts_compares_the_file_on_disk_not_only_the_spelling(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        relative = _committed(writer)
        head, name = relative.rsplit('/', 1)
        (store.root / head / 'notes.txt').write_bytes(b'foreign')
        builder = writer.begin_snapshot()
        with pytest.raises(StorageError, match='generated form'):  # an existing file that is no artifact
            builder.reference_artifacts([f'{head}/notes.txt'])
        os.rename(store.root / relative, store.root / head / name.upper())
        with pytest.raises(StorageError):  # the generated spelling, but the file on disk carries another one
            builder.reference_artifacts([relative])


def test_artifact_path_prefix_must_match_its_key():
    key = artifact_key('blob', 'p', REV)
    other = '00' if key[:2] != '00' else '11'
    check_artifact_path(f'artifacts/blob/{key[:2]}/{key}.bin')
    with pytest.raises(StorageError, match='generated form'):
        check_artifact_path(f'artifacts/blob/{other}/{key}.bin')


def test_add_file_refuses_a_name_already_on_disk(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        (builder.directory / 'stray.txt').write_bytes(b'not ours')
        with pytest.raises(StorageError, match='already part of this snapshot'):
            builder.add_file('stray.txt', b'x')
        assert (builder.directory / 'stray.txt').read_bytes() == b'not ours'


@pytest.mark.parametrize('form', sorted(aliases('blob.x')))
def test_commit_refuses_every_alias_of_kind_and_key(project_root, tmp_path, form):
    store = make_store(project_root, tmp_path / 'kb')
    key = artifact_key('blob', 'k', REV)
    kind = aliases('blob')[form] if form != 'short name' else 'blob~1'
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        with pytest.raises(StorageError):
            checkpoints.commit(key, kind, b'x', revisions=REV)
        with pytest.raises(StorageError):
            checkpoints.commit(aliases(key)[form], 'blob', b'x', revisions=REV)
    assert not (store.root / 'artifacts').exists() or not any((store.root / 'artifacts').rglob('*.bin'))


@pytest.mark.parametrize('form', sorted(aliases('index/café.txt')))
def test_add_file_refuses_every_alias(project_root, tmp_path, form):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        builder.add_file('index/café.txt', b'one')
        with pytest.raises(StorageError):
            builder.add_file(aliases('index/café.txt')[form], b'two')
        assert [e['path'] for e in builder.entries] == ['index/café.txt']
        assert (builder.directory / 'index' / 'café.txt').read_bytes() == b'one'


@pytest.mark.parametrize('first, second', [('index', 'index/x.txt'), ('index/x.txt', 'INDEX/y.txt'),
                                           ('folders.parquet', 'Folders.parquet'), ('a.txt', 'manifest.parquet')])
def test_add_file_refuses_file_and_folder_collisions(project_root, tmp_path, first, second):
    store = make_store(project_root, tmp_path / 'kb')
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        builder.add_file(first, b'one')
        with pytest.raises(StorageError, match='same file or folder'):
            builder.add_file(second, b'two')


def _manifest_row(path, kind='file'):
    return {'kind': kind, 'path': path, 'table': None, 'format': None, 'size_bytes': 1, 'sha256': '0' * 64,
            'rows': None, 'physical_version': 1}


@pytest.mark.parametrize('form', sorted(aliases('index/names.txt')))
def test_manifest_refuses_every_alias(project_root, tmp_path, form):
    store = make_store(project_root, tmp_path / 'kb')
    directory = store.root / 'snap'  # `_manifest` takes a store-relative directory (fix round 4)

    def write(rows):
        _fs.write_table_file(directory / 'MANIFEST.parquet', pa.Table.from_pylist(rows, schema=MANIFEST_SCHEMA),
                             'parquet')
    write([_manifest_row('index/names.txt')])
    assert len(store._manifest('snap')) == 1  # the control: without an alias this manifest is read
    for rows in ([_manifest_row('index/names.txt'), _manifest_row(aliases('index/names.txt')[form])],
                 [_manifest_row(aliases('artifacts/blob/ab/' + 'ab' * 32 + '.bin')[form], 'artifact')]):
        write(rows)
        with pytest.raises(StorageError):
            store._manifest('snap')


def test_manifest_refuses_a_repeated_artifact(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    artifact = 'artifacts/blob/ab/' + 'ab' * 32 + '.bin'
    rows = [_manifest_row(artifact, 'artifact'), _manifest_row(artifact, 'artifact')]
    _fs.write_table_file(store.root / 'snap' / 'MANIFEST.parquet', pa.Table.from_pylist(rows, schema=MANIFEST_SCHEMA),
                         'parquet')
    with pytest.raises(StorageError, match='listed twice'):
        store._manifest('snap')


@pytest.mark.parametrize('alias', ['REQ-A', 'Req-a', 'nul', 'CON', 'com1', 'req-a.', 'req a', 'req:a', 'REQ~1',
                                   'req\\a', 'réq'])
def test_holds_refuse_every_alias(project_root, tmp_path, alias):
    store = make_store(project_root, tmp_path / 'kb')
    sid = publish(store, 'a')
    store.hold(sid, 'req-a', LATER)
    with pytest.raises(StorageError):
        store.hold(sid, alias, LATER - timedelta(hours=23))
    with pytest.raises(StorageError):
        store.release_hold(sid, alias)
    assert [p.name for p in (store.root / 'holds' / sid).iterdir()] == ['req-a.parquet']
    store.release_hold(sid, 'req-a')
    assert list((store.root / 'holds' / sid).iterdir()) == []


@pytest.mark.parametrize('form', sorted(aliases('a' * 64 + '.x')))
@pytest.mark.parametrize('call', ['state', 'done', 'queue', 'fail', 'load'])
def test_checkpoints_refuse_every_alias(project_root, tmp_path, form, call):
    store = make_store(project_root, tmp_path / 'kb')
    key = artifact_key('blob', 'c', REV)
    alias = aliases(key)[form]
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        checkpoints.commit(key, 'blob', b'good', revisions=REV)
        method = getattr(checkpoints, call)
        with pytest.raises(StorageError):
            method(alias, 'error') if call == 'fail' else method(alias)


def test_gc_never_deletes_a_name_that_is_not_the_generated_spelling(project_root, tmp_path):
    """The referenced artifact renamed on disk to another case is the same file on Windows; GC leaves it."""
    store = make_store(project_root, tmp_path / 'kb', keep=0, retention_s=0)
    with store.writer() as writer:
        relative = _committed(writer)
        builder = writer.begin_snapshot()
        for name, rows in dataset('a').items():
            builder.write_table(name, rows, dim=DIM if 'embeddings' in name else None)
        builder.reference_artifacts([relative])
        writer.publish(builder.finish())
    head, name = relative.rsplit('/', 1)
    os.rename(store.root / relative, store.root / head / name.upper())
    (store.root / head / 'notes.txt').write_bytes(b'foreign')
    with store.writer() as writer:
        report = writer.gc()
    skipped = sorted(path for path, _ in report.skipped)
    assert skipped == sorted([f'{head}/{name.upper()}', f'{head}/notes.txt']) and report.artifacts_removed == []
    assert (store.root / head / name.upper()).read_bytes() == b'good'


def test_registry_sweep_keeps_another_spelling_of_the_authoritative_file(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb')
    for attempt in range(8):  # the hex part needs a letter to have another case
        publish(store, f'a{attempt}')
        name = (store.root / 'registry' / 'REGISTRY').read_text().strip()
        renamed = 'snapshots-' + name.removeprefix('snapshots-').upper().replace('.PARQUET', '.parquet')
        if renamed != name:
            break
    assert renamed != name
    os.rename(store.root / 'registry' / name, store.root / 'registry' / renamed)
    try:
        with store.writer():
            pass
    except StorageError:
        pass  # refusing to open is acceptable; deleting the registry is not
    assert (store.root / 'registry' / renamed).exists()


def test_recovery_and_gc_trust_only_exact_snapshot_names(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    sid = publish(store, 'a')
    foreign = store.root / 'snapshots' / ('.BUILDING-' + sid)
    foreign.mkdir()
    with store.writer():
        pass
    assert foreign.is_dir()  # not a name the store generates: never removed by recovery
    (store.root / 'leases' / sid.upper()).mkdir(parents=True)
    with store.writer() as writer:
        with pytest.raises(StorageError, match='invalid snapshot id'):
            writer.gc()


# ---------------------------------------------------------------- serialization: every operation vs publish and gc


class Race:
    """Pause the first file replace or hash of thread 'op' until released (deterministic)."""

    def __init__(self, monkeypatch):
        self.reached, self.go = threading.Event(), threading.Event()
        for owner, name in ((_fs, '_commit'), (store_module, 'sha256_file')):
            original = getattr(owner, name)
            monkeypatch.setattr(owner, name, self._hook(original))

    def _hook(self, original):
        def hooked(*args, **kwargs):
            if threading.current_thread().name == 'op' and not self.reached.is_set():
                self.reached.set()
                assert self.go.wait(10), 'race test: paused operation was never released'
            return original(*args, **kwargs)
        return hooked


def _prepare(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=2, retention_s=86400)
    old = publish(store, 'old')
    publish(store, 'base')
    return store, old


def _builder(writer, tag, relative=None):
    builder = writer.begin_snapshot()
    for name, rows in dataset(tag).items():
        builder.write_table(name, rows, dim=DIM if 'embeddings' in name else None)
    if relative:
        builder.reference_artifacts([relative])
    return builder


OPERATIONS = {
    'commit': lambda c: c['checkpoints'].commit(artifact_key('blob', 'new', REV), 'blob', b'new', revisions=REV),
    'queue': lambda c: c['checkpoints'].queue('c' * 64),
    'fail': lambda c: c['checkpoints'].fail('d' * 64, 'cannot decode'),
    'done': lambda c: c['checkpoints'].done(c['key']),
    'add_file': lambda c: c['open'].add_file('index/extra.txt', b'x'),
    'write_table': lambda c: c['empty'].write_table('folders', dataset('e')['folders']),
    'reference_artifacts': lambda c: c['open'].reference_artifacts([c['artifact']]),
    'finish': lambda c: c['open'].finish(),
    'begin_snapshot': lambda c: c['writer'].begin_snapshot(),
    'publish': lambda c: c['writer'].publish(c['ready']),
    'rollback': lambda c: c['writer'].rollback(c['old']),
    'gc': lambda c: c['writer'].gc(),
}
RACERS = {'publish': lambda c: c['writer'].publish(c['ready']), 'gc': lambda c: c['writer'].gc()}


@pytest.mark.parametrize('racer', sorted(RACERS))
@pytest.mark.parametrize('operation', sorted(OPERATIONS))
def test_every_writer_operation_excludes_a_racing_publish_and_gc(project_root, tmp_path, monkeypatch, operation,
                                                                 racer):
    store, old = _prepare(project_root, tmp_path)
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        key = artifact_key('blob', 'a', REV)
        artifact = checkpoints.commit(key, 'blob', b'good', revisions=REV)
        context = {'writer': writer, 'checkpoints': checkpoints, 'key': key, 'artifact': artifact, 'old': old,
                   'ready': _builder(writer, 'ready', artifact).finish(), 'open': _builder(writer, 'open'),
                   'empty': writer.begin_snapshot()}
        race = Race(monkeypatch)
        errors = {'op': [], 'racer': []}

        def run(name, call):
            try:
                call(context)
            except StorageError as error:
                errors[name].append(str(error))
        op = threading.Thread(target=run, args=('op', OPERATIONS[operation]), name='op')
        op.start()
        assert race.reached.wait(10), f'{operation} never reached a file replace or hash'
        other = threading.Thread(target=run, args=('racer', RACERS[racer]), name='racer')
        other.start()
        other.join(PAUSE_S)
        entered = not other.is_alive()
        race.go.set()
        op.join(20)
        other.join(20)
        assert not op.is_alive() and not other.is_alive()
        assert not entered, f'{racer} completed while {operation} was between its check and its act'
        assert errors['op'] == []
        # A racing publish of a snapshot the operation already published is refused, nothing else.
        assert all('not validated' in error for error in errors['racer'])
    sid, sha = store.current()
    store.verify_snapshot(sid, sha)


def test_lock_order_never_takes_the_writer_mutex_under_the_store_lock(project_root, tmp_path, monkeypatch):
    held, violations = threading.local(), []
    real_store_lock, real_init = store_module.KnowledgeStore.store_lock, store_module.Writer.__init__

    class Tracked:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            self.lock.__enter__()
            held.depth = getattr(held, 'depth', 0) + 1
            return self

        def __exit__(self, *exc):
            held.depth -= 1
            return self.lock.__exit__(*exc)

    class CheckedMutex(Tracked):
        def __enter__(self):
            if getattr(held, 'depth', 0) and not getattr(held, 'mutex', 0):
                violations.append('writer mutex taken under the store lock')
            self.lock.__enter__()
            held.mutex = getattr(held, 'mutex', 0) + 1
            return self

        def __exit__(self, *exc):
            held.mutex -= 1
            return self.lock.__exit__(*exc)

    def init(self, store):
        real_init(self, store)
        self._mutex = CheckedMutex(self._mutex)
    monkeypatch.setattr(store_module.KnowledgeStore, 'store_lock', lambda self: Tracked(real_store_lock(self)))
    monkeypatch.setattr(store_module.Writer, '__init__', init)
    store = make_store(project_root, tmp_path / 'kb', keep=1, retention_s=0)
    old = publish(store, 'old')
    with store.writer() as writer:
        checkpoints = writer.checkpoints('K2')
        artifact = checkpoints.commit(artifact_key('blob', 'o', REV), 'blob', b'x', revisions=REV)
        checkpoints.queue('c' * 64)
        checkpoints.fail('d' * 64, 'e')
        checkpoints.done(artifact_key('blob', 'o', REV))
        builder = _builder(writer, 'new', artifact)
        builder.add_file('index/names.txt', b'n')
        writer.publish(builder.finish())
        writer.gc()
        writer.rollback(old)
    store.hold(old, 'r1', LATER)
    with store.pin():
        pass
    store.release_hold(old, 'r1')
    assert violations == []


# ---------------------------------------------------------------- leases: a sharing violation means "live"


def _stale_lease(store, sid):
    lease = store.root / 'leases' / sid / ('f' * 32 + '.lease')
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_bytes(b'')
    return lease


def test_lease_open_permission_error_counts_as_live(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    lease = _stale_lease(store, old)
    real_open = open

    def refusing_open(path, *args, **kwargs):
        if str(path).endswith('.lease'):
            raise PermissionError(13, 'simulated sharing violation', str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(_fs, 'open', refusing_open, raising=False)
    with store.writer() as writer:
        report = writer.gc()
    assert report.leased == [old] and old not in report.collected and lease.exists()
    store.timeout_s, store.poll_s = 0.2, 0.01
    with pytest.raises(StorageError, match='not acquired'):  # a reader waits, then refuses; it does not crash
        store.pin()


@pytest.mark.skipif(os.name != 'nt', reason='an exclusive share mode exists only on Windows')
def test_lease_held_exclusively_by_another_process_is_live(project_root, tmp_path):
    import _winapi
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    old = publish(store, 'old')
    publish(store, 'new')
    lease = _stale_lease(store, old)
    handle = _winapi.CreateFile(str(lease), _winapi.GENERIC_READ, 0, 0, _winapi.OPEN_EXISTING, 0, 0)
    try:
        with store.writer() as writer:
            report = writer.gc()
    finally:
        _winapi.CloseHandle(handle)
    assert report.leased == [old] and (store.root / 'snapshots' / old).is_dir()
    with store.writer() as writer:  # once the handle is gone the lease is stale and the snapshot collected
        assert old in writer.gc().collected
