"""Writer checkpoint view invariants with synthetic stores and labelled counting/failure doubles."""

from copy import deepcopy
import shutil

from hypothesis import given, settings, strategies as st
import pytest

from prompt_enhancer.storage import artifact_key, StorageError
from prompt_enhancer.storage import store as module
from storage_fixtures import make_store, T0


REV = {'model': 'test', 'prompt': 'test', 'schema': 'test'}
KEY = artifact_key('reply', 'content', REV)


@pytest.mark.parametrize('n', [50, 200])
@pytest.mark.parametrize('seeded', [0, 3])
@pytest.mark.parametrize('operation', ['queue', 'fail', 'commit'])
@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
@pytest.mark.parametrize('resume', [False, True])
def test_checkpoint_loop_reads_each_seed_segment_at_most_once(
        project_root, tmp_path, monkeypatch, n, seeded, operation, fmt, resume):
    store = make_store(project_root, tmp_path / 'store', table_format=fmt, resume=resume)
    if seeded:
        with pytest.raises(RuntimeError, match='labelled interrupted session'):
            with store.writer() as writer:
                for i in range(seeded):
                    writer._append_state([writer._row('seed', artifact_key('seed', str(i), REV), 'queued')])
                raise RuntimeError('labelled interrupted session')  # Keep the real segments un-compacted.
    with store.writer() as writer:
        counts = {'reads': 0, 'listings': 0, 'helpers': 0}
        read, segments, init = module.read_table_file, writer._state_segments, module.Checkpoints.__init__

        def counted_read(path, *args, **kwargs):
            if path.parent.name == 'sync_state':
                counts['reads'] += 1
            return read(path, *args, **kwargs)  # Labelled transparent counting double.

        def counted_segments():
            counts['listings'] += 1
            return segments()

        def counted_init(self, *args, **kwargs):
            counts['helpers'] += 1
            return init(self, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(module, 'read_table_file', counted_read)
            patch.setattr(writer, '_state_segments', counted_segments)
            patch.setattr(module.Checkpoints, '__init__', counted_init)
            helper = writer.checkpoints('reply')
            for i in range(n):
                selected = writer.checkpoints('reply') if i % 2 else helper
                key = artifact_key('reply', str(i), REV)
                if operation == 'commit':
                    selected.commit(key, 'reply', b'synthetic', revisions=REV)
                elif operation == 'fail':
                    selected.fail(key, 'labelled failure', revisions=REV)
                else:
                    selected.queue(key, revisions=REV)
            assert counts['reads'] <= seeded, counts
            assert counts['listings'] <= 1, counts
            assert counts['helpers'] == 1, counts
        assert writer._latest_rows() == writer._state_rows()


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('retention', [0, 10])
@given(operations=st.lists(st.tuples(st.sampled_from(['commit', 'fail', 'queue', 'compact', 'gc']),
                                   st.sampled_from(['reply', 'other']), st.integers(0, 2)),
                         min_size=1, max_size=15))
@settings(max_examples=25, deadline=None, database=None)
def test_checkpoint_view_matches_fresh_segments_after_sequences(
        project_root, tmp_path_factory, operations, fmt, resume, retention):
    root = tmp_path_factory.mktemp('checkpoint-view-property')
    # Labelled fixed clock keeps positive-retention marks independent of test runtime.
    store = make_store(project_root, root, table_format=fmt, resume=resume, retention_s=retention,
                       clock=lambda: T0.timestamp())
    expected = {}
    try:
        with store.writer() as writer:
            for operation, stage, item in operations:
                key = artifact_key(stage, str(item), REV)
                if operation == 'compact':
                    writer._compact_state()
                elif operation == 'gc':
                    writer.gc()
                    if retention == 0:
                        expected = {identity: state for identity, state in expected.items() if state != 'done'}
                else:
                    helper = writer.checkpoints(stage)
                    if operation == 'commit':
                        helper.commit(key, stage, b'synthetic', revisions=REV, call_records=[{'status': 'OK'}])
                        expected[(stage, key)] = 'done'
                    else:
                        if operation == 'fail':
                            helper.fail(key, 'labelled failure', revisions=REV)
                        else:
                            helper.queue(key, revisions=REV)
                        if expected.get((stage, key)) != 'done':
                            expected[(stage, key)] = 'failed' if operation == 'fail' else 'queued'
                cached, fresh = writer._latest_rows(), writer._state_rows()  # Fresh disk oracle bypasses the view.
                assert cached == fresh and cached is not fresh
                assert {identity: row['state'] for identity, row in fresh.items()
                        if identity[0] != module.GC_STAGE} == expected
                for row in fresh.values():
                    assert row['error'] == ('labelled failure' if row['state'] == 'failed' else None)
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
@pytest.mark.parametrize('published', [False, True])
def test_append_failure_never_exposes_an_unpublished_row(project_root, tmp_path, monkeypatch, fmt, published):
    store = make_store(project_root, tmp_path / 'store', table_format=fmt)
    with store.writer() as writer:
        helper = writer.checkpoints('reply')
        helper.queue(KEY)
        before = deepcopy(writer._latest_rows())
        write = store._write_table

        def broken(relative, *args, **kwargs):
            if relative.startswith('registry/sync_state/'):
                # Labelled failure double observes the boundary before any publication, too.
                assert writer._latest_rows() == before == writer._state_rows()
                if published:
                    write(relative, *args, **kwargs)
                raise OSError('labelled append failure')
            return write(relative, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(store, '_write_table', broken)
            with pytest.raises(OSError, match='labelled append failure'):
                helper.fail(KEY, 'labelled failure')
        assert writer._latest_rows() == writer._state_rows()
        assert writer._latest_rows()[('reply', KEY)]['state'] == ('failed' if published else 'queued')
        if not published:
            assert writer._latest_rows() == before


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
@pytest.mark.parametrize('failure', ['empty', 'append', 'discard'])
def test_rewrite_invalidates_view_and_factory_even_on_failure(project_root, tmp_path, monkeypatch, fmt, failure):
    store = make_store(project_root, tmp_path / 'store', table_format=fmt)
    with store.writer() as writer:
        helper = writer.checkpoints('reply')
        helper.queue(KEY)
        removed = artifact_key('reply', 'removed', REV)
        helper.queue(removed)
        assert writer._latest_rows() == writer._state_rows()
        rows = {('reply', KEY): writer._latest_rows()[('reply', KEY)]} if failure != 'empty' else {}

        def broken(*args, **kwargs):
            raise OSError('labelled rewrite failure')

        with monkeypatch.context() as patch:
            if failure == 'empty':
                writer._rewrite_state(rows)
            else:
                patch.setattr(store, '_write_table' if failure == 'append' else '_discard', broken)
                with pytest.raises(OSError, match='labelled rewrite failure'):
                    writer._rewrite_state(rows)
        assert writer._latest is None and not writer._checkpoints
        assert writer._latest_rows() == writer._state_rows()
        assert writer.checkpoints('reply') is not helper
        if failure == 'empty':
            assert writer._latest_rows() == {}


@pytest.mark.parametrize('boundary', ['compact', 'gc', 'close', 'exceptional_close'])
def test_view_and_helper_memo_lifecycle(project_root, tmp_path, boundary):
    store = make_store(project_root, tmp_path / 'store')
    writer = store.writer()
    try:
        with writer:
            helper = writer.checkpoints('reply')
            helper.queue(KEY)
            assert writer.checkpoints('reply') is helper
            if boundary == 'compact':
                writer._compact_state()  # Invalidate even when there is only one segment.
            elif boundary == 'gc':
                writer.gc()
            elif boundary == 'exceptional_close':
                raise RuntimeError('labelled exceptional close')
            if boundary in ('compact', 'gc'):
                assert writer._latest is None and not writer._checkpoints
                assert writer.checkpoints('reply') is not helper
    except RuntimeError:
        assert boundary == 'exceptional_close'
    assert writer._latest is None and not writer._checkpoints
    with pytest.raises(StorageError, match='session is not open'):
        helper.queue(KEY)


def test_gc_uses_a_working_copy_and_drops_collected_done_keys(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'store', retention_s=0)
    with store.writer() as writer:
        helper = writer.checkpoints('reply')
        removed = helper.commit(KEY, 'reply', b'synthetic', revisions=REV)
        keep = artifact_key('reply', 'keep', REV)
        retained = helper.commit(keep, 'reply', b'synthetic', revisions=REV)
        builder = writer.begin_snapshot()
        builder.reference_artifacts([retained])
        original = writer._rewrite_state

        def checked(rows):
            # Labelled assertion double: GC must not edit its authoritative view before publication.
            assert ('reply', KEY) in writer._latest_rows()
            assert ('reply', KEY) not in rows and rows is not writer._latest_rows()
            return original(rows)

        with monkeypatch.context() as patch:
            patch.setattr(writer, '_rewrite_state', checked)
            writer.gc()
        assert writer._latest_rows() == writer._state_rows()
        assert ('reply', KEY) not in writer._latest_rows()
        assert writer.checkpoints('reply').state(KEY) is None
        assert not store.path(removed).exists() and store.path(retained).exists()
        assert writer.checkpoints('reply').reusable(keep) is not None


def test_view_includes_retained_gc_marks_after_compaction(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'store', retention_s=10, clock=lambda: T0.timestamp())
    with store.writer() as writer:
        relative = writer.checkpoints('reply').commit(KEY, 'reply', b'synthetic', revisions=REV)
        writer.gc()
        writer._compact_state()
        fresh = writer._state_rows()
        assert writer._latest_rows() == fresh
        assert fresh[(module.GC_STAGE, relative)]['state'] == 'queued'
        assert fresh[('reply', KEY)]['state'] == 'done'
        assert store.path(relative).exists()


def test_out_of_order_append_rebuilds_using_segment_precedence(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'store')
    ticks = iter([200, 100])
    with store.writer() as writer:
        helper = writer.checkpoints('reply')
        monkeypatch.setattr(module.time, 'time_ns', lambda: next(ticks))  # Labelled backwards-clock double.
        helper.queue(KEY)
        helper.fail(KEY, 'older filename')
        assert writer._latest_rows() == writer._state_rows()
        assert writer._latest_rows()[('reply', KEY)]['state'] == 'queued'
        monkeypatch.undo()  # Normal writer-exit compaction needs an unrestricted clock.
