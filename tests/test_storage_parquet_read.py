"""S42-ci7: in-memory Parquet reads of manifests and holds keep no Python-owned buffer on an Arrow worker thread."""

from datetime import datetime, timezone

from hypothesis import HealthCheck, given, settings, strategies as st
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from storage_fixtures import make_store, publish

from prompt_enhancer.storage import _fs
from prompt_enhancer.storage.store import MANIFEST, _read_hold

FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
EXPIRES = datetime(2030, 1, 1, tzinfo=timezone.utc)


def old_read(data):
    """The read S42-ci7 replaced (test-only reference): the dataset scanner over a Python-owned buffer."""
    return pq.read_table(pa.BufferReader(data))


def assert_same(old, new):
    assert new.schema.equals(old.schema, check_metadata=True)
    # A file with no metadata at all reads as `{}` through the dataset layer and as None through ParquetFile; both
    # mean "no tags", and every manifest and hold carries tags, so the two are compared as "or {}".
    assert (new.schema.metadata or {}) == (old.schema.metadata or {})
    assert new.equals(old, check_metadata=True)
    assert new.to_pylist() == old.to_pylist()


def to_bytes(table):
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def published(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'kb', keep=0)
    sid = publish(store, 'parquet-read')
    store.hold(sid, 'r-1', EXPIRES)
    manifest = store.path(f'{store._snapshot_dir(sid)}/{MANIFEST}')
    hold = store.path(store._hold_path(sid, 'r-1'))
    return store, sid, manifest, hold


def test_manifest_and_hold_read_equal_the_old_read(published):
    _, _, manifest, hold = published
    for path in (manifest, hold):
        data = path.read_bytes()
        assert_same(old_read(data), _fs.read_parquet_bytes(data))


def column_strategy():
    return st.one_of(
        st.tuples(st.just(pa.int64()), st.lists(st.one_of(st.none(), st.integers(-2**40, 2**40)), max_size=6)),
        st.tuples(st.just(pa.large_string()), st.lists(st.one_of(st.none(), st.text(max_size=8)), max_size=6)),
        st.tuples(st.just(pa.float64()), st.lists(st.one_of(st.none(), st.floats(allow_nan=False)), max_size=6)),
        st.tuples(st.just(pa.timestamp('us', tz='+01:00')),  # a fixed offset: a named zone needs tzdata on Windows
                  st.lists(st.one_of(st.none(), st.integers(-2**50, 2**50)), max_size=6)),
        st.tuples(st.just(pa.list_(pa.large_string())),
                  st.lists(st.one_of(st.none(), st.lists(st.text(max_size=4), max_size=3)), max_size=6)))


@FAST
@given(st.lists(column_strategy(), min_size=1, max_size=4),
       st.dictionaries(st.text(min_size=1, max_size=6), st.text(max_size=6), max_size=3))
def test_read_round_trips_random_tables_with_schema_and_metadata(columns, metadata):
    size = min(len(values) for _, values in columns)
    fields = [pa.field(f'c{index}', kind, True) for index, (kind, _) in enumerate(columns)]
    table = pa.Table.from_arrays([pa.array(values[:size], type=kind) for kind, values in columns],
                                 schema=pa.schema(fields, metadata=metadata))
    data = to_bytes(table)
    new = _fs.read_parquet_bytes(data)
    assert_same(old_read(data), new)
    assert new.to_pylist() == table.to_pylist()


class Recorder:
    """Test double: records how `_fs` opens and reads Parquet bytes, then calls the real pyarrow."""

    def __init__(self, monkeypatch):
        self.opened, self.reads, self.sources = [], [], []
        real_file, real_reader = pq.ParquetFile, pa.BufferReader
        recorder = self

        class SpyFile(real_file):
            def __init__(self, source, **kwargs):
                recorder.opened.append(kwargs)
                super().__init__(source, **kwargs)

            def read(self, *args, **kwargs):
                recorder.reads.append(kwargs)
                return super().read(*args, **kwargs)

        def spy_reader(obj):
            recorder.sources.append(obj)
            return real_reader(obj)

        def refuse_read_table(*args, **kwargs):
            raise AssertionError('pq.read_table (dataset scanner on the CPU pool) used for an in-memory read')

        monkeypatch.setattr(_fs.pq, 'ParquetFile', SpyFile)
        monkeypatch.setattr(_fs.pa, 'BufferReader', spy_reader)
        monkeypatch.setattr(_fs.pq, 'read_table', refuse_read_table)


def test_manifest_is_read_from_disk_once_so_hashed_bytes_are_the_parsed_bytes(published, monkeypatch):
    store, _, manifest, _ = published
    reads = []
    real = type(manifest).read_bytes

    def counting(self):
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(type(manifest), 'read_bytes', counting)
    store.pin().release()
    assert reads.count(MANIFEST) == 1


def test_in_memory_read_uses_no_scanner_no_threads_and_an_arrow_owned_buffer(published, monkeypatch):
    store, _, _, hold = published
    recorder = Recorder(monkeypatch)
    pinned = store.pin()  # reads the manifest
    pinned.release()
    _read_hold(hold)  # reads the hold
    assert len(recorder.reads) == 2 and len(recorder.opened) == 2  # manifest, then hold
    assert all(call == {'use_threads': False} for call in recorder.reads)
    assert all(call == {'pre_buffer': False} for call in recorder.opened)
    # The reader's source is an Arrow-allocated buffer, never the `bytes` object nor a Python-owned wrapper of it.
    assert all(type(source) is pa.Buffer for source in recorder.sources)
    assert not any(isinstance(source, (bytes, bytearray, memoryview)) for source in recorder.sources)


def test_the_reader_source_is_a_copy_not_a_wrapper_of_the_input_bytes(published, monkeypatch):
    """S42-ci7 review P3-1: `pa.py_buffer(data)` is also a `pa.Buffer`, but it wraps the Python `bytes`; only a
    separate memory address shows that the reader holds an Arrow-allocated copy."""
    _, _, manifest, _ = published
    data = manifest.read_bytes()
    recorder = Recorder(monkeypatch)
    _fs.read_parquet_bytes(data)
    (source,) = recorder.sources
    assert source.address != pa.py_buffer(data).address
    assert source.to_pybytes() == data
