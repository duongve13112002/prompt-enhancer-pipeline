"""Fix-round regressions, synthetic source trees and labelled transport doubles only."""

import asyncio
from copy import deepcopy
import json
from random import Random

import pytest

from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.k2_naming import DRAW_REVISION, _key, _load, name_folders, parse_naming
from prompt_enhancer.storage import SPECS, StorageError
from prompt_enhancer.storage.store import Checkpoints
from k2_fixtures import NamingServer, assessment, folder, image_bytes, invoke, naming, scan, source_tree
from storage_fixtures import dataset


def snapshot(env, result, parent=None):
    with env.store.writer() as writer:
        builder = writer.begin_snapshot(parent=parent)
        for name, rows in dataset().items():
            builder.write_table(name, rows, dim=4 if SPECS[name].vector else None)
        builder.reference_artifacts(result['artifacts'])
        sid = builder.finish()
        writer.publish(sid)
    return sid


@pytest.mark.parametrize('name', ['\n\t', ' ', 'A|B'])
def test_structurally_invalid_name_fails_only_its_folder(k2_env, name):
    folder(k2_env, 'bad')
    folder(k2_env, 'good')
    result = invoke(k2_env, NamingServer({('folder_namer', 'bad'): naming([name])}))
    assert set(result['report']['failed']) == {'bad'}
    assert [row['folder_id'] for row in result['rows']['folder_concepts']] == ['good']


def test_padded_model_name_uses_declared_identity_everywhere(k2_env):
    folder(k2_env, 'a', 'Kimono\nHuman')
    folder(k2_env, 'b')
    result = invoke(k2_env, NamingServer({('folder_namer', 'b'): naming([' \tKimono \t '])}))
    assert not result['report']['failed']
    assert len(result['rows']['concepts']) == 1
    assert {row['name_as_written'] for row in result['rows']['folder_concepts']} == {'Kimono'}
    assert result['report']['model_named_folders'][0]['concepts'][0]['name'] == 'Kimono'


def test_gc_preserves_below_threshold_naming_baseline(k2_env):
    path = folder(k2_env, 'f', count=4)
    server = NamingServer()
    first = invoke(k2_env, server, rng=Random(1))
    snapshot(k2_env, first)
    with k2_env.store.writer() as writer:
        removed = writer.gc().artifacts_removed
    assert any('/k2_draw_head/' in path for path in removed)
    (path / 'new.png').write_bytes(image_bytes((200, 100, 20)))
    second = invoke(k2_env, server, rng=Random(99))
    assert len(server.requests) == 1
    assert second['rows'] == first['rows']
    assert second['report']['model_named_folders'][0]['sampled_image_ids'] == first['report']['model_named_folders'][0]['sampled_image_ids']
    assert second['report']['redraws'][0]['reason'] == 'baseline_recovered'
    with k2_env.store.writer() as writer:
        events = [row for row in writer._state_rows().values() if row['stage'] == 'k2_redraw']
        current = [row for row in events if json.loads(writer.checkpoints('k2_redraw').load(row['input_key']))['reason'] == 'baseline_recovered']
        assert current and all(row['output_key'] in second['artifacts'] for row in current)


def test_missing_baseline_has_distinct_durable_cause(k2_env):
    folder(k2_env, 'f')
    result = invoke(k2_env)
    assert result['report']['redraws'][0]['reason'] == 'baseline_missing'
    assert any('/k2_redraw/' in path for path in result['artifacts'])


def test_redraw_event_survives_resume(k2_env, monkeypatch):
    path = folder(k2_env, 'f', count=2)
    server = NamingServer()
    invoke(k2_env, server)
    k2_env.config['knowledge']['naming']['rename_on_change'] = 0.25
    (path / 'new.png').write_bytes(image_bytes((200, 100, 20)))
    commit = Checkpoints.commit

    class Interrupted(BaseException):
        """Labelled process-interruption double after durable head update."""

    def interrupted(self, *args, **kwargs):
        result = commit(self, *args, **kwargs)
        if self.stage == 'k2_draw_head':
            raise Interrupted()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', interrupted)
        with pytest.raises(Interrupted):
            invoke(k2_env, server)
    result = invoke(k2_env, server)
    assert result['report']['redraws'][0]['reason'] == 'image_set_change'
    again = invoke(k2_env, server)
    assert again['report']['redraws'] == result['report']['redraws']
    assert len(server.requests) == 2


def test_unclear_no_images_keeps_declared_names(k2_env):
    folder(k2_env, 'u', 'Old\nCurated original', count=0)
    server = NamingServer({('info_assessor', 'u'): assessment(False)})
    result = invoke(k2_env, server)
    assert len(server.requests) == 1
    assert result['rows']['folders'][0]['name_source'] == 'info_file'
    assert result['rows']['folder_concepts'][0]['name_as_written'] == 'Old'
    assert result['rows']['folder_concepts'][0]['description'] == 'Curated original'
    assert result['report']['retained_declared_folders'] == ['u']


def test_model_payload_has_positional_labels_without_pipeline_ids(k2_env):
    folder(k2_env, 'f', 'Old\nText', count=2)
    server = NamingServer({('info_assessor', 'f'): assessment(False),
                           ('folder_namer', 'f'): naming(['New'], info_fits=False)})
    result = invoke(k2_env, server)
    assert not result['report']['failed']
    assert len(server.requests) == 2
    for body in server.requests:
        content = body['messages'][1]['content']
        value = json.loads(content[0]['text'] if isinstance(content, list) else content)
        assert 'folder_id' not in value and 'sampled_image_ids' not in value
        if isinstance(content, list):
            assert value['image_labels'] == [0, 1]
            assert len(content) == 3


@pytest.mark.parametrize(('info', 'reply'), [
    ('One|Two\nA|B', naming(['Two', 'One'], info_fits=True)),
    ('One\nA', naming(['Other'], info_fits=True)),
    (None, naming(['Other'], folder_fits=True)),
])
def test_fitting_reply_preserves_exact_names_and_order(k2_env, info, reply):
    folder(k2_env, 'f', info)
    server = NamingServer({('info_assessor', 'f'): assessment(False), ('folder_namer', 'f'): reply})
    result = invoke(k2_env, server)
    assert 'f' in result['report']['failed']
    assert result['rows']['folders'][0]['name_source'] != ('info_file' if info is None else 'model')
    with k2_env.store.writer() as writer:
        assert not [row for row in writer._state_rows().values() if row['stage'] == 'k2_naming' and row['state'] == 'done']


def test_changed_sample_size_gets_new_draw(k2_env):
    folder(k2_env, 'f', count=4)
    server = NamingServer()
    invoke(k2_env, server)
    k2_env.config['knowledge']['naming']['sample_size'] = 3
    result = invoke(k2_env, server)
    assert len(result['report']['model_named_folders'][0]['sampled_image_ids']) == 3
    assert len(server.requests) == 2


def test_knowledge_root_argument_mismatch_refused_before_activity(k2_env):
    scanned = scan(k2_env)
    before = source_tree(k2_env.source)
    with pytest.raises(ConfigurationError, match='stage argument'):
        asyncio.run(name_folders(k2_env.store, k2_env.config, scanned['handoff']['k2'],
                                images=[], folder_images=[], knowledge_root=k2_env.root))
    assert source_tree(k2_env.source) == before


@pytest.mark.parametrize('target', ['store', 'data'])
@pytest.mark.parametrize('relation', ['equal', 'inside', 'parent'])
def test_storage_overlap_refused_before_activity(k2_env, target, relation):
    scanned = scan(k2_env)
    path = {'equal': k2_env.source, 'inside': k2_env.source / 'inner', 'parent': k2_env.source.parent}[relation]
    before = source_tree(k2_env.source)
    if target == 'store':
        k2_env.store.root = path
    else:
        k2_env.config['storage']['data_root'] = path.relative_to(k2_env.root).as_posix()
    with pytest.raises(ConfigurationError, match='overlaps'):
        asyncio.run(name_folders(k2_env.store, k2_env.config, scanned['handoff']['k2'],
                                images=[], folder_images=[], knowledge_root=k2_env.source))
    assert source_tree(k2_env.source) == before


def test_latest_referenced_compatible_draw_uses_stored_snapshot_order(k2_env):
    path = folder(k2_env, 'f', count=4)
    server = NamingServer()
    first = invoke(k2_env, server, rng=Random(1))
    parent = snapshot(k2_env, first)
    for index in range(3):
        (path / f'new{index}.png').write_bytes(image_bytes((200, index, 20)))
    latest = invoke(k2_env, server, rng=Random(3))
    snapshot(k2_env, latest, parent)
    with k2_env.store.writer() as writer:
        writer.gc()
    (path / 'extra.png').write_bytes(image_bytes((40, 220, 90)))
    resumed = invoke(k2_env, server, rng=Random(99))
    assert len(server.requests) == 2  # The older 4-image baseline would require a third call.
    assert resumed['report']['model_named_folders'][0]['sampled_image_ids'] == latest['report']['model_named_folders'][0]['sampled_image_ids']


@pytest.mark.parametrize(('parsed', 'fits'), [(None, True), ({'names': [], 'status': 'pending_naming'}, False),
                                          ({'names': ['One'], 'status': 'parsed'}, None)])
def test_naming_fit_flag_requires_declared_names(k2_env, parsed, fits):
    with pytest.raises(ValueError, match='info_file_fits'):
        parse_naming(naming(info_fits=fits), 'f', parsed, project_root=k2_env.root, separator='|')


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"version": 999}'])
def test_corrupt_checkpoint_json_or_layout_refused(k2_env, raw):
    with k2_env.store.writer() as writer:
        checkpoints = writer.checkpoints('k2_assessment')
        key = _key('k2_assessment', {'test': 'corrupt'}, DRAW_REVISION)
        checkpoints.commit(key, 'k2_assessment', raw, revisions=DRAW_REVISION)
        with pytest.raises(StorageError, match='invalid JSON|unknown artifact layout'):
            _load(checkpoints, key)


@pytest.mark.parametrize('alternate', [False, True])
def test_scanned_info_missing_or_found_under_alternate_filename(k2_env, alternate):
    path = folder(k2_env, 'f', 'One\nOriginal')
    scanned = scan(k2_env)
    if alternate:
        (path / 'alternate.txt').write_bytes((path / 'info.txt').read_bytes())
        k2_env.config['knowledge']['info_file']['names'] = ['missing.txt', 'alternate.txt']
    (path / 'info.txt').unlink()
    server = NamingServer()
    result = invoke(k2_env, server, result=scanned)
    assert len(server.requests) == (1 if alternate else 0)
    assert bool(result['report']['failed']) is not alternate


@pytest.mark.parametrize('kind', ['folder', 'unknown_image', 'invalid_image'])
def test_invalid_image_membership_refused(k2_env, kind):
    folder(k2_env, 'f')
    scanned = scan(k2_env)
    membership = scanned['rows']['folder_images'][0]
    if kind == 'folder':
        membership['folder_id'] = 'unknown'
    elif kind == 'unknown_image':
        membership['image_id'] = 'sha256:' + '0' * 64
    else:
        membership['image_id'] = scanned['rows']['images'][0]['image_id'] = 'invalid'
    with pytest.raises(ValueError, match='membership references'):
        invoke(k2_env, result=scanned)


def test_completed_naming_does_not_read_images(k2_env, monkeypatch):
    import prompt_enhancer.k2_naming as module
    folder(k2_env, 'f')
    server = NamingServer()
    scanned = scan(k2_env)
    first = invoke(k2_env, server, result=scanned)

    def forbidden(*args):
        pytest.fail('completed naming must not reread image bytes')

    monkeypatch.setattr(module, '_read_verified', forbidden)
    assert invoke(k2_env, server, result=scanned)['rows'] == first['rows']
    assert len(server.requests) == 1


def test_malformed_cached_reply_refuses_without_new_call(k2_env):
    folder(k2_env, 'f')
    server = NamingServer()
    invoke(k2_env, server)
    with k2_env.store.writer() as writer:
        row = next(row for row in writer._state_rows().values() if row['stage'] == 'k2_naming')
        checkpoints = writer.checkpoints('k2_naming')
        cached = json.loads(checkpoints.load(row['input_key']))
        cached['reply']['concepts'] = []
        checkpoints.commit(row['input_key'], 'k2_naming', json.dumps(cached).encode(), revisions=DRAW_REVISION)
    assert 'f' in invoke(k2_env, server)['report']['failed']
    assert len(server.requests) == 1


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('owned', [False, True])
def test_owned_run_closes_and_supplied_client_stays_caller_owned(k2_env, monkeypatch, fail, owned):
    import prompt_enhancer.k2_naming as module
    from prompt_enhancer.model_client import BoundModelClient
    folder(k2_env, 'f')
    server = NamingServer({('folder_namer', 'f'): dict(naming(), concepts=[])} if fail else {})
    run = server.run(k2_env)
    closed = []
    close = run.close

    async def tracked_close():
        closed.append(True)
        await close()

    monkeypatch.setattr(run, 'close', tracked_close)
    monkeypatch.setattr(module, 'ModelClientRun', lambda *args, **kwargs: run)
    scanned = scan(k2_env)

    async def execute():
        try:
            result = await name_folders(k2_env.store, k2_env.config, scanned['handoff']['k2'],
                                       images=scanned['rows']['images'], folder_images=scanned['rows']['folder_images'],
                                       knowledge_root=k2_env.source, client=None if owned else BoundModelClient(run, 'info_assessor'))
            assert bool(result['report']['failed']) is fail
            assert len(closed) == int(owned)
        finally:
            if not closed:
                await run.close()

    asyncio.run(execute())
    assert closed == [True]
