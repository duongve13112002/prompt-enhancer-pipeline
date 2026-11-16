"""Completed checkpoints survive unsuccessful attempts; all stores and clocks are synthetic."""

from itertools import count

from hypothesis import given, settings, strategies as st
import pytest

from prompt_enhancer.storage import artifact_key, StorageError
from prompt_enhancer.storage import store as module
from storage_fixtures import make_store


REV = {'model': 'test', 'prompt': 'test', 'schema': 'test'}
KEY = artifact_key('reply', 'content', REV)


def transition(checkpoints, operation, key=KEY):
    if operation == 'fail':
        checkpoints.fail(key, 'labelled unsuccessful attempt', revisions=REV)
    else:
        checkpoints.queue(key, revisions=REV)


@pytest.mark.parametrize('operation', ['fail', 'queue'])
@pytest.mark.parametrize('other_run', [False, True])
@pytest.mark.parametrize('stale_helper', [False, True])
@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
def test_done_transition_reads_durable_state_across_helpers_and_runs(
        project_root, tmp_path, operation, other_run, stale_helper, fmt):
    store = make_store(project_root, tmp_path / 'store', table_format=fmt, resume=False)
    with store.writer() as writer:
        writer.run_id = 'initial-test-run'
        stale = module.Checkpoints(writer, 'reply')  # An independent helper stays stale despite factory memoization.
        writer.checkpoints('reply').commit(KEY, 'reply', b'valid reply', revisions=REV,
                                         call_records=[{'status': 'OK'}])
        before = writer.checkpoints('reply').state(KEY)
        if not other_run:
            selected = stale if stale_helper else writer.checkpoints('reply')
            segments = writer._state_segments()
            transition(selected, operation)
            assert selected.state(KEY) == before
            assert writer._state_rows()[('reply', KEY)] == before
            assert writer._state_segments() == segments
    if other_run:
        with store.writer() as writer:
            writer.run_id = 'next-test-run'
            # Construct an independent handle when testing staleness; the factory memo is session-local.
            selected = module.Checkpoints(writer, 'reply') if stale_helper else writer.checkpoints('reply')
            if stale_helper:
                selected._rows.pop(KEY)  # Labelled stale view; durable committed state remains intact.
            assert selected.done(KEY) is None and selected.reusable(KEY) is None
            segments = writer._state_segments()
            transition(selected, operation)
            assert selected.state(KEY) == before
            assert writer._state_rows()[('reply', KEY)] == before
            assert writer._state_segments() == segments
    with store.writer() as writer:
        assert writer.checkpoints('reply').load(KEY) == b'valid reply'


@pytest.mark.parametrize('operation', ['fail', 'queue'])
@pytest.mark.parametrize('initial', [None, 'queued', 'failed'])
def test_nonterminal_transition_still_records_unfinished_work(project_root, tmp_path, operation, initial):
    store = make_store(project_root, tmp_path / 'store')
    with store.writer() as writer:
        checkpoints = writer.checkpoints('reply')
        if initial:
            transition(checkpoints, 'queue' if initial == 'queued' else 'fail')
        transition(checkpoints, operation)
        row = checkpoints.state(KEY)
        assert row['state'] == ('failed' if operation == 'fail' else 'queued')
        assert row['error'] == ('labelled unsuccessful attempt' if operation == 'fail' else None)
        assert checkpoints.done(KEY) is None


@pytest.mark.parametrize('operation', ['fail', 'queue'])
def test_preservation_is_key_and_stage_local_and_does_not_hide_corruption(project_root, tmp_path, operation):
    store = make_store(project_root, tmp_path / 'store')
    other = artifact_key('reply', 'other content', REV)
    with store.writer() as writer:
        checkpoints = writer.checkpoints('reply')
        artifact = checkpoints.commit(KEY, 'reply', b'valid reply', revisions=REV)
        store.path(artifact).write_bytes(b'corrupt')
        assert checkpoints.done(KEY) is None
        transition(checkpoints, operation)
        assert checkpoints.state(KEY)['state'] == 'done'
        assert checkpoints.done(KEY) is None
        with pytest.raises(StorageError):
            checkpoints.load(KEY)
        transition(checkpoints, operation, other)
        transition(writer.checkpoints('different-stage'), operation)
        expected = 'failed' if operation == 'fail' else 'queued'
        assert checkpoints.state(other)['state'] == expected
        assert writer.checkpoints('different-stage').state(KEY)['state'] == expected


@given(operations=st.lists(st.sampled_from(['commit', 'fail', 'queue']), min_size=1, max_size=10))
@settings(max_examples=25, deadline=None)
def test_done_is_preserved_by_every_nonterminal_sequence(operations, tmp_path_factory, project_root):
    store = make_store(project_root, tmp_path_factory.mktemp('checkpoint-property'))
    expected = None
    with store.writer() as writer:
        for operation in operations:
            checkpoints = writer.checkpoints('reply')
            if operation == 'commit':
                checkpoints.commit(KEY, 'reply', b'valid reply', revisions=REV)
                expected = 'done'
            else:
                transition(checkpoints, operation)
                if expected != 'done':
                    expected = 'failed' if operation == 'fail' else 'queued'
            assert checkpoints.state(KEY)['state'] == expected
            assert writer._state_rows()[('reply', KEY)]['state'] == expected


def test_equal_timestamp_segments_keep_write_order(project_root, tmp_path, monkeypatch):
    store = make_store(project_root, tmp_path / 'store')
    monkeypatch.setattr(module.time, 'time_ns', lambda: 1_800_000_000_000_000_000)
    monkeypatch.setattr(module, '_SEQUENCE', count())
    with store.writer() as writer:
        checkpoints = writer.checkpoints('reply')
        checkpoints.queue(KEY, revisions=REV)
        checkpoints.commit(KEY, 'reply', b'valid reply', revisions=REV)
        assert writer._state_rows()[('reply', KEY)]['state'] == 'done'
        checkpoints.fail(KEY, 'labelled failure', revisions=REV)
        assert writer._state_rows()[('reply', KEY)]['state'] == 'done'
