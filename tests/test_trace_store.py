"""Real tiny trace segments, expiry and hash-checked snapshot attachment bytes."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from intake_fixtures import configuration
from prompt_enhancer.pipeline import retain_attachments
from prompt_enhancer.profile_prompt import sha256_tag
from prompt_enhancer.storage import StorageError
from prompt_enhancer.trace_store import TraceRepository
from storage_fixtures import data_rows, dataset, make_store


@pytest.mark.parametrize('retention,expired', [(10, 1), (30, 0)])
def test_trace_retention_override_and_hold_release(model_test_profile, tmp_path, project_root, retention, expired):
    config = configuration(model_test_profile, tmp_path)
    config['storage']['data_root'] = str(tmp_path / 'data')
    released = []
    repo = TraceRepository(config, project_root=project_root, retention_s=retention,
                           release_hold=lambda *args: released.append(args))
    row = data_rows()['request_traces'][0]
    row.update(snapshot_id='s1', body={'attachments_retained': True})
    # Append directly so the synthetic old row is not swept before the test's controlled clock.
    repo.store.append('request_traces', [row])
    assert repo.expire(row['created_at'] + timedelta(seconds=9)) == 0
    assert repo.expire(row['created_at'] + timedelta(seconds=11)) == expired
    assert released == ([('s1', row['request_id'])] if expired else [])
    assert len(repo.store.read_rows('request_traces')) == (0 if expired else 1)


def test_snapshot_image_is_manifest_copy_and_hold_precedes_release(tmp_path, project_root):
    store = make_store(project_root, tmp_path / 'kb')
    raw = b'labelled synthetic stored image bytes'
    image_id = sha256_tag(raw)
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, rows in dataset().items():
            builder.write_table(name, rows, dim=4 if name.endswith('embeddings') else None)
        builder.add_file('images/' + image_id.removeprefix('sha256:'), raw)  # Labelled synthetic retained copy.
        snapshot_id = builder.finish()
        writer.publish(snapshot_id)
    pin = store.pin()
    events = []
    def hold(*args):
        assert pin.read_image(image_id) == raw
        events.append('held')
        return store.hold(*args)
    s0 = SimpleNamespace(snapshot=pin, knowledge=SimpleNamespace(store=SimpleNamespace(hold=hold)),
        payload={'task': 'edit', 'request_id': 'r1'}, profile=SimpleNamespace(manifest={
            'renderer': {'accepts_reference_images': True}}))
    assembled = {'pack': {'snapshot_id': snapshot_id, 'request_id': 'r1', 'images': [
        {'image_id': image_id, 'channel': 'attach'}]}}
    try:
        data = retain_attachments(s0, assembled, data_rows()['request_traces'][0]['created_at'] + timedelta(days=10000))
        assert data == {image_id: raw} and events == ['held']
        with pytest.raises(StorageError, match='not retained'):
            pin.read_image(sha256_tag(b'missing'))
    finally:
        pin.release()
    with pytest.raises(StorageError, match='released'):
        pin.read_image(image_id)


def test_attachment_hold_failure_is_not_available():
    s0 = SimpleNamespace(payload={'task': 'generate'}, profile=SimpleNamespace(manifest={
        'renderer': {'accepts_reference_images': False}}))
    with pytest.raises(StorageError):
        retain_attachments(s0, {'pack': {'images': [{'channel': 'attach'}]}}, None)


def test_trace_history_is_read_once_not_per_append(model_test_profile, tmp_path, project_root, monkeypatch):
    import prompt_enhancer.trace_store as module
    config = configuration(model_test_profile, tmp_path)
    config['storage']['data_root'] = str(tmp_path / 'data')
    calls = []
    original = module.read_table_file
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(module, 'read_table_file', counted)
    repo = TraceRepository(config, project_root=project_root, retention_s=30)
    row = data_rows()['request_traces'][0]
    repo.store.append('request_traces', [row])
    for index in range(8):
        repo.append(dict(row, request_id='r' + str(index)))
    assert len(calls) == 1


def test_unrepresentable_trace_expiry_does_not_append(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    config['storage']['data_root'] = str(tmp_path / 'data')
    repo = TraceRepository(config, project_root=project_root, retention_s=1e20)
    with pytest.raises((ValueError, OverflowError)):
        repo.append(data_rows()['request_traces'][0])
    assert repo.store.read_rows('request_traces') == []


def test_trace_timestamp_near_datetime_upper_bound_does_not_append(model_test_profile, tmp_path, project_root):
    from datetime import datetime, timezone
    config = configuration(model_test_profile, tmp_path)
    config['storage']['data_root'] = str(tmp_path / 'data')
    repo = TraceRepository(config, project_root=project_root, retention_s=10)
    row = dict(data_rows()['request_traces'][0], created_at=datetime.max.replace(tzinfo=timezone.utc))
    with pytest.raises(OverflowError):
        repo.append(row)
    assert repo.store.read_rows('request_traces') == []
