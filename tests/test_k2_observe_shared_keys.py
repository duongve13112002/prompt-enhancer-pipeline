"""Shared-key regressions with synthetic images and a labelled in-process observer double."""

import json

import pytest

from k2_fixtures import folder, image_bytes
from k2_observe_fixtures import ObserverServer, invoke_observer, k1_result, states
from prompt_enhancer.storage import SPECS, StorageError, concept_id, image_id
from prompt_enhancer.storage.identity import canonical_json


def test_shared_key_selected_in_one_folder_not_other(k2_env):
    # Reviewer's labelled regression: the later folder must preserve earlier selection.
    payloads = [image_bytes((i * 20, 30, 50)) for i in range(6)]
    ids = sorted(range(6), key=lambda i: image_id(payloads[i]))
    y, x = payloads[ids[0]], payloads[ids[-1]]
    folder(k2_env, 'f1', 'A', count=0)
    (k2_env.source / 'f1/0.png').write_bytes(x)
    folder(k2_env, 'g', 'A', count=0)
    (k2_env.source / 'g/0.png').write_bytes(x)
    (k2_env.source / 'g/1.png').write_bytes(y)
    server = ObserverServer()
    result = invoke_observer(k2_env, server)
    assert result['report']['representatives'] == {'f1': [image_id(x)], 'g': [image_id(y)]}
    assert result['report']['observed'] == len(server.requests) == 2
    assert {row['image_id'] for row in result['rows']['observations']} == {image_id(x), image_id(y)}
    assert result['report']['queued'] == []


@pytest.mark.parametrize('earlier_selected', [True, False])
def test_shared_selection_queue_and_checkpoints_merge_once(k2_env, earlier_selected):
    payloads = sorted((image_bytes((i * 20, 30, 50)) for i in range(6)), key=image_id)
    y, x, z = payloads[0], payloads[1], payloads[-1]
    narrow, broad = [x, z], [y, x, z]
    for name, values in zip(('f1', 'g'), (narrow, broad) if earlier_selected else (broad, narrow)):
        path = folder(k2_env, name, 'A', count=0)
        for index, payload in enumerate(values):
            (path / f'{index}.png').write_bytes(payload)
    server = ObserverServer()
    first = invoke_observer(k2_env, server)
    assert first['report']['observed'] == len(server.requests) == 2
    assert len(first['report']['queued']) == 1
    assert len(first['artifacts']) == len(set(first['artifacts'])) == 2
    rows = first['rows']['observations']
    assert len(rows) == len({row['context_key'] for row in rows}) == 2
    checkpoints = states(k2_env)
    assert len(checkpoints) == len({row['input_key'] for row in checkpoints}) == 3
    assert sorted(row['state'] for row in checkpoints) == ['done', 'done', 'queued']
    assert {row['input_key'] for row in checkpoints if row['state'] == 'queued'} == set(first['report']['queued'])
    replay = invoke_observer(k2_env, server)
    assert replay['rows'] == first['rows'] and replay['artifacts'] == first['artifacts']
    assert replay['report']['queued'] == first['report']['queued']
    assert replay['report']['skipped_done'] == 2 and len(server.requests) == 2
    assert states(k2_env) == checkpoints


def test_shared_key_uses_later_matching_source_location(k2_env):
    for name in ('f1', 'g'):
        folder(k2_env, name, 'A')
    inputs = k1_result(k2_env)
    # Only the synthetic source changes; K1 still identifies the shared original bytes.
    (k2_env.source / 'f1/0.png').write_bytes(image_bytes((255, 0, 0)))
    server = ObserverServer()
    result = invoke_observer(k2_env, server, inputs=inputs)
    assert result['report']['observed'] == len(server.requests) == 1
    assert not result['report']['failed'] and len(states(k2_env)) == 1


@pytest.mark.parametrize('referenced', [False, True])
def test_completed_bundle_with_other_concepts_is_refused(k2_env, referenced):
    from storage_fixtures import dataset

    folder(k2_env, 'f', 'A')
    server = ObserverServer()
    result = invoke_observer(k2_env, server)
    key = result['rows']['observations'][0]['context_key']
    with k2_env.store.writer() as writer:
        checkpoints = writer.checkpoints('k2_observation')
        bundle = json.loads(checkpoints.load(key))
        record = checkpoints.state(key)
        # Commit a checksummed synthetic artifact so only the declared-concepts guard can reject it.
        bundle['concepts'] = [{'name': 'Other', 'concept_id': concept_id('Other')}]
        artifact = checkpoints.commit(key, 'k2_observation', canonical_json(bundle).encode('utf-8'),
            revisions=bundle['revisions'], call_records=record['call_records'])
        assert checkpoints.done(key)
        if referenced:
            builder = writer.begin_snapshot()
            for name, rows in dataset().items():
                builder.write_table(name, rows, dim=4 if SPECS[name].vector else None)
            builder.reference_artifacts([artifact])
            writer.publish(builder.finish())
    if referenced:
        k2_env.store._resume = False
        with k2_env.store.writer() as writer:
            checkpoints = writer.checkpoints('k2_observation')
            assert checkpoints.done(key) is None and checkpoints.reusable(key)
    with pytest.raises(StorageError, match='CACHE_INVALID: invalid completed bundle'):
        invoke_observer(k2_env, server)
    assert len(server.requests) == 1
