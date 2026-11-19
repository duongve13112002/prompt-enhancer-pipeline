"""The run sidecar reader and the run lock of the data store (T-016 P4 fix round 1: R-015, R-017).

`iter_sidecar_lines` is what `eval import` reads a pack with, line by line under a byte limit from configuration;
`run_lock` is the exclusive lock one `eval run` (or one `eval import`) holds on a run. Evidence class: unit tests of
production code on a temporary data root (no labelled double)."""
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from prompt_enhancer import k0_scan
from prompt_enhancer.storage import StorageError
from prompt_enhancer.storage.store import DataStore

RUN = 'eval/runs/0123456789abcdef'
PROPERTY = settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
LINE = st.binary(max_size=40).filter(lambda value: b'\n' not in value)


def store(tmp_path, project_root, **lock):
    return DataStore(tmp_path / 'data', project_root=project_root, table_format='parquet', **lock)


@PROPERTY
@given(lines=st.lists(LINE, max_size=12), final_newline=st.booleans(), chunk=st.integers(1, 17))
def test_the_lines_come_back_exactly_whatever_the_block_size(tmp_path_factory, project_root, monkeypatch, lines, final_newline,
                                                              chunk):
    data = store(tmp_path_factory.mktemp('lines'), project_root)
    body = b'\n'.join(lines) + (b'\n' if final_newline and lines else b'')
    data.write_sidecar(f'{RUN}/pack.jsonl', body)
    monkeypatch.setattr(k0_scan, '_CHUNK_BYTES', chunk)  # many blocks, so a line spans several of them
    expected = body.split(b'\n')
    if expected and expected[-1] == b'':
        expected.pop()  # the newline after the last line is not another line
    assert list(data.iter_sidecar_lines(f'{RUN}/pack.jsonl', max_bytes=len(body) + 5)) == expected


def test_the_byte_limit_is_exact_and_a_missing_file_is_reported(tmp_path, project_root):
    data = store(tmp_path, project_root)
    body = b'one\ntwo\nthree\n'
    data.write_sidecar(f'{RUN}/pack.jsonl', body)
    assert list(data.iter_sidecar_lines(f'{RUN}/pack.jsonl', len(body))) == [b'one', b'two', b'three']  # exactly at the limit
    with pytest.raises(StorageError, match='SourceTooLarge'):
        list(data.iter_sidecar_lines(f'{RUN}/pack.jsonl', len(body) - 1))
    with pytest.raises(FileNotFoundError):
        list(data.iter_sidecar_lines(f'{RUN}/absent.jsonl', 100))
    with pytest.raises(StorageError, match='not a run sidecar path'):
        list(data.iter_sidecar_lines('eval/runs/x', 100))  # only the run tree is readable


def test_stopping_early_releases_the_file(tmp_path, project_root):
    data = store(tmp_path, project_root)
    data.write_sidecar(f'{RUN}/pack.jsonl', b'a\nb\nc\n')
    lines = data.iter_sidecar_lines(f'{RUN}/pack.jsonl', 100)
    assert next(lines) == b'a'
    lines.close()
    data.write_sidecar(f'{RUN}/pack.jsonl', b'replaced\n')  # a file still open would refuse the rename on Windows
    assert list(data.iter_sidecar_lines(f'{RUN}/pack.jsonl', 100)) == [b'replaced']


def test_the_run_lock_needs_the_validated_lock_setting_and_conflicts_with_itself(tmp_path, project_root):
    with pytest.raises(StorageError, match='storage.lock is not in the read set'):
        store(tmp_path, project_root).run_lock('0123456789abcdef', create=True)
    data = store(tmp_path, project_root, timeout_s=1, poll_s=0.01)
    first, second = data.run_lock('0123456789abcdef', create=True), data.run_lock('0123456789abcdef', create=True)
    assert first.try_acquire() and not second.try_acquire()  # a second holder is not acquired, and does not wait
    first.release()
    assert second.try_acquire()
    second.release()
    with pytest.raises(StorageError, match='not a run sidecar path'):
        data.run_lock('../escape', create=True)
