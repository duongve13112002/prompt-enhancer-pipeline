"""Round 2 regressions: synthetic sources and labelled capability doubles, no inference."""

import asyncio
from copy import deepcopy
import json
from random import Random

import pytest
from hypothesis import given, settings, strategies as st

from k2_fixtures import NamingServer, folder, image_bytes, invoke, naming, scan
from test_k2_fix_round1 import snapshot
from prompt_enhancer.k2_naming import DRAW_REVISION, _key, _save
from prompt_enhancer.model_probe import ProbeResult
from prompt_enhancer.storage import StorageError
from prompt_enhancer.storage.store import Checkpoints, KnowledgeStore


def probe_run(env, monkeypatch, *, limit=6, answer=True, local=True):
    """Labelled sub-probe doubles count their granted requests without HTTP."""
    import prompt_enhancer.model_client as module

    run = NamingServer().run(env)
    run.probes.clear()
    roles = run.config['vlm']['roles']
    roles['planner'] = deepcopy(roles['info_assessor'])
    roles['image_alias'] = deepcopy(roles['folder_namer'])
    run.config['vlm']['backends']['main']['probe']['max_requests'] = limit
    seen = []

    async def image(*args):
        seen.append(('image', args[-2]))
        return ProbeResult('complete', {'image': True, 'json_schema': True}, {})

    async def thinking(*args):
        seen.append(('thinking', args[-2]))
        return ProbeResult('complete', {'reasoning.mode': True}, {})

    async def multiple(*args):
        allowance = args[-2]
        seen.append(('multiple', allowance))
        return (ProbeResult('complete', {'multiple_images': answer}, {}) if allowance == 2
                else ProbeResult('incomplete', {}, {}))

    monkeypatch.setattr(module, 'probe_local_image_pair', image)
    monkeypatch.setattr(module, 'probe_local_thinking_pair', thinking)
    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', multiple)
    if local:
        run.client.local_probe_plans = {'main': {'image': ('one', 'two'),
            'thinking': ('on', 'off', 'key', 'observer')}}
    else:
        async def cases(*args, **kwargs):
            seen.append(('cases', min(1, args[3])))
            return ProbeResult('complete', {'image': True, 'json_schema': True}, {})

        monkeypatch.setattr(module, 'probe_backend', cases)
        run.client.probe_cases = {'main': {'json_schema': ({}, None)}}
    return run, seen


@pytest.mark.parametrize('local', [True, False])
@pytest.mark.parametrize('first', ['planner', 'info_assessor', 'folder_namer'])
def test_multi_image_proof_is_independent_of_bind_order(k2_env, monkeypatch, first, local):
    run, seen = probe_run(k2_env, monkeypatch, local=local)

    async def execute():
        try:
            await run.for_role(first, require=('multiple_images',) if first == 'folder_namer' else ())
            await run.for_role('folder_namer', require=('multiple_images',))
            await run.for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('multiple', 2)) == 1
    assert run.probes['main'].capabilities['multiple_images'] is True
    assert run.probes['main'].capabilities['json_schema'] is True
    assert sum(count for _, count in seen) <= 6


def test_multi_image_requirement_is_explicit_for_any_image_role(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch)

    async def execute():
        try:
            await run.for_role('image_alias', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert ('multiple', 2) in seen


@pytest.mark.parametrize('role,sample', [('info_assessor', 2), ('folder_namer', 1)])
def test_text_only_and_single_sample_roles_do_not_probe_multiple_images(k2_env, monkeypatch, role, sample):
    run, seen = probe_run(k2_env, monkeypatch)
    run.config['knowledge']['naming']['sample_size'] = sample
    if role == 'info_assessor':
        run.config['vlm']['roles']['folder_namer']['max_tokens'] = 'deploy_required'

    async def execute():
        try:
            await run.for_role(role, require=('multiple_images',) if sample > 1 else ())
        finally:
            await run.close()

    asyncio.run(execute())
    assert not [item for item in seen if item[0] == 'multiple']


@pytest.mark.parametrize('limit', [1, 4, 5, 6, 7])
@pytest.mark.parametrize('local', [True, False])
def test_lazy_proof_preserves_remaining_request_limit(k2_env, monkeypatch, limit, local):
    run, seen = probe_run(k2_env, monkeypatch, limit=limit, local=local)

    async def execute():
        try:
            await run.for_role('planner')
            await run.for_role('folder_namer', require=('multiple_images',))
            await run.for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert sum(count for _, count in seen) <= limit
    assert run.probes['main'].capabilities.get('multiple_images') is (True if limit >= (6 if local else 3) else None)
    assert len([item for item in seen if item[0] == 'multiple']) <= 1


@pytest.mark.parametrize('first', ['planner', 'folder_namer'])
def test_known_negative_multiple_image_proof_is_not_retried(k2_env, monkeypatch, first):
    run, seen = probe_run(k2_env, monkeypatch, answer=False, limit=8)

    async def execute():
        try:
            await run.for_role(first, require=('multiple_images',) if first == 'folder_namer' else ())
            for role in ['folder_namer', 'image_alias', 'folder_namer']:
                await run.for_role(role, require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('multiple', 2)) == 1
    assert run.probes['main'].capabilities['multiple_images'] is False


def test_multi_image_proof_is_not_attached_to_another_backend(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch)
    run.config['vlm']['roles']['folder_namer']['backend'] = 'embedder'

    async def execute():
        try:
            await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert not [item for item in seen if item[0] == 'multiple']
    assert 'multiple_images' not in run.probes['main'].capabilities


def test_lazy_proof_preserves_errors_and_serializes_concurrent_binds(k2_env, monkeypatch):
    import prompt_enhancer.model_client as module
    run, seen = probe_run(k2_env, monkeypatch)
    multiple = module.probe_local_multiple_images_pair

    async def yielding(*args):
        await asyncio.sleep(0)
        return await multiple(*args)

    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', yielding)

    async def execute():
        try:
            await run.for_role('planner')
            base = run.probes['main']
            run.probes['main'] = ProbeResult(base.status, base.capabilities, {'prior': 'RecordedError'})
            await asyncio.gather(run.for_role('folder_namer', require=('multiple_images',)),
                                 run.for_role('image_alias', require=('multiple_images',)))
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('multiple', 2)) == 1
    assert run.probes['main'].errors == {'prior': 'RecordedError'}
    assert run.probes['main'].capabilities['reasoning.mode'] is True


def test_lazy_proof_uses_configured_deadline_without_retrying_unknown_result(k2_env, monkeypatch):
    import prompt_enhancer.model_client as module
    from prompt_enhancer.model_probe import probe_local_multiple_images_pair
    run, _ = probe_run(k2_env, monkeypatch)
    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', probe_local_multiple_images_pair)
    run.config['vlm']['backends']['main']['probe']['timeout_s'] = 0.01
    calls = []
    original = run.transports['main']

    class HangingTransport:
        """Labelled timeout double; no sockets or services."""
        async def post(self, *args):
            calls.append(True)
            await asyncio.sleep(1)

        async def close(self):
            pass

    run.transports['main'] = HangingTransport()

    async def execute():
        await original.close()
        try:
            await run.for_role('planner')
            await run.for_role('folder_namer', require=('multiple_images',))
            await run.for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert len(calls) == 2
    assert run.probes['main'].status == 'failed'
    assert set(run.probes['main'].errors.values()) == {'TimeoutError'}
    assert run.probes['main'].capabilities.get('multiple_images') is None


@pytest.mark.parametrize('name', ['A\nB', 'A\rB', '\nA', 'A\r'])
def test_generated_cr_lf_name_fails_only_its_folder(k2_env, name):
    folder(k2_env, 'bad')
    folder(k2_env, 'good')
    result = invoke(k2_env, NamingServer({('folder_namer', 'bad'): naming([name])}))
    assert set(result['report']['failed']) == {'bad'}
    assert [row['folder_id'] for row in result['rows']['folder_concepts']] == ['good']


def gc(env):
    with env.store.writer() as writer:
        return writer.gc()


def test_headless_recovery_requires_matching_sample_size(k2_env):
    folder(k2_env, 'f', count=4)
    server = NamingServer()
    first = invoke(k2_env, server)
    snapshot(k2_env, first)
    gc(k2_env)
    k2_env.config['knowledge']['naming']['sample_size'] = 3
    second = invoke(k2_env, server)
    assert len(server.requests) == 2
    assert len(second['report']['model_named_folders'][0]['sampled_image_ids']) == 3
    assert [event['reason'] for event in second['report']['redraws']] == ['baseline_missing']


@pytest.mark.parametrize('field,value', [('selection', 'incompatible-test-only'), ('folder_id', 'another')])
def test_recovery_rejects_incompatible_namespace(k2_env, field, value):
    folder(k2_env, 'f', count=4)
    server = NamingServer()
    first = invoke(k2_env, server)
    with k2_env.store.writer() as writer:
        draws = writer.checkpoints('k2_draw')
        original = first['report']['redraws'][0]['draw_key']
        draw = json.loads(draws.load(original))
        draw.pop('version')
        draw[field] = value
        key = _key('k2_draw', draw, DRAW_REVISION)
        path = _save(draws, key, draw, DRAW_REVISION)
    snapshot(k2_env, {'artifacts': [path]})
    gc(k2_env)
    second = invoke(k2_env, server)
    assert [event['reason'] for event in second['report']['redraws']] == ['baseline_missing']
    assert len(server.requests) == 2


@pytest.mark.parametrize('field,value', [('output_sha256', '0' * 64), ('output_size', 0)])
def test_recovery_refuses_manifest_checkpoint_disagreement(k2_env, monkeypatch, field, value):
    folder(k2_env, 'f', count=3)
    first = invoke(k2_env)
    snapshot(k2_env, first)
    gc(k2_env)
    state = Checkpoints.state

    def disagree(self, key):
        row = state(self, key)
        return dict(row, **{field: value}) if self.stage == 'k2_draw' and row is not None else row

    monkeypatch.setattr(Checkpoints, 'state', disagree)
    with pytest.raises(StorageError, match='checkpoint disagrees with referenced manifest'):
        invoke(k2_env)


@pytest.mark.parametrize('missing', [False, True])
def test_changed_set_recovery_keeps_both_causes(k2_env, missing):
    path = folder(k2_env, 'f', count=4)
    server = NamingServer()
    first = invoke(k2_env, server, rng=Random(1))
    snapshot(k2_env, first)
    gc(k2_env)
    if missing:
        sampled = first['report']['model_named_folders'][0]['sampled_image_ids'][0]
        import hashlib
        next(p for p in path.glob('*.png') if 'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest() == sampled).unlink()
    else:
        for index in range(3):
            (path / f'new{index}.png').write_bytes(image_bytes((100, index, 20)))
    second = invoke(k2_env, server)
    expected = ['baseline_recovered', 'sample_member_missing' if missing else 'image_set_change']
    assert [event['reason'] for event in second['report']['redraws']] == expected
    assert invoke(k2_env, server)['report']['redraws'] == second['report']['redraws']


@pytest.mark.parametrize('shade', [0, 1, 2])
def test_ambiguous_snapshot_draws_fresh_with_durable_cause(k2_env, shade):
    path = folder(k2_env, 'f', count=4)
    server = NamingServer()
    first = invoke(k2_env, server, rng=Random(1))
    parent = snapshot(k2_env, first)
    for index in range(3):
        (path / f'new{index}.png').write_bytes(image_bytes((200, index + shade * 7, 20)))
    latest = invoke(k2_env, server, rng=Random(3))
    snapshot(k2_env, {'artifacts': sorted(set(first['artifacts'] + latest['artifacts']))}, parent)
    gc(k2_env)
    third = invoke(k2_env, server, rng=Random(99))
    assert [event['reason'] for event in third['report']['redraws']] == ['baseline_ambiguous']
    old_keys = {event['draw_key'] for result in (first, latest) for event in result['report']['redraws']}
    assert third['report']['redraws'][0]['draw_key'] not in old_keys
    assert len(server.requests) == 3
    assert invoke(k2_env, server)['report']['redraws'] == third['report']['redraws']


def test_headless_recovery_scans_live_manifests_once_for_all_folders(k2_env, monkeypatch):
    for name in 'abcd':
        folder(k2_env, name, count=3)
    server = NamingServer()
    first = invoke(k2_env, server)
    snapshot(k2_env, first)
    gc(k2_env)
    scanned = scan(k2_env)
    calls = []
    manifest = KnowledgeStore._manifest

    def count(self, *args, **kwargs):
        calls.append(args[0])
        return manifest(self, *args, **kwargs)

    monkeypatch.setattr(KnowledgeStore, '_manifest', count)
    second = invoke(k2_env, server, result=scanned)
    # One recovery scan plus the writer's existing immutable-reference scan, independent of folder count.
    assert len(calls) <= 2
    assert len(server.requests) == 4
    assert second['rows'] == first['rows']


def test_k2_recovery_uses_public_storage_reader_once(k2_env, monkeypatch):
    for name in 'ab':
        folder(k2_env, name, count=3)
    server = NamingServer()
    first = invoke(k2_env, server)
    snapshot(k2_env, first)
    gc(k2_env)
    reader = KnowledgeStore.referenced_artifacts
    calls = []

    def read(self, kind):
        calls.append(kind)
        return reader(self, kind)

    monkeypatch.setattr(KnowledgeStore, 'referenced_artifacts', read)
    invoke(k2_env, server)
    assert calls == ['k2_draw']
    import inspect
    import prompt_enhancer.k2_naming as module
    source = inspect.getsource(module)
    assert all(f'.{name}(' not in source for name in ['_registry', '_manifest', '_snapshot_dir'])


def test_recovery_refuses_unknown_referenced_layout(k2_env):
    folder(k2_env, 'f')
    first = invoke(k2_env)
    with k2_env.store.writer() as writer:
        draws = writer.checkpoints('k2_draw')
        key = first['report']['redraws'][0]['draw_key']
        path = draws.commit(key, 'k2_draw', b'{"version":999}', revisions=DRAW_REVISION)
    snapshot(k2_env, {'artifacts': [path]})
    gc(k2_env)
    with pytest.raises(StorageError, match='unknown artifact layout'):
        invoke(k2_env)


@given(st.lists(st.lists(st.tuples(st.integers(1, 4), st.sampled_from(['random', 'other'])),
                         max_size=4), min_size=1, max_size=5), st.integers(1, 4))
@settings(max_examples=50)
def test_recovery_choice_over_multiple_snapshots_and_draws(snapshots, sample_size):
    from prompt_enhancer.k2_naming import _referenced_draw

    groups = [[(f'{i}-{j}', {'folder_id': 'f', 'sample_size': size, 'selection': selection})
               for j, (size, selection) in enumerate(group)] for i, group in enumerate(snapshots)]
    namespace = {'folder_id': 'f', 'sample_size': sample_size, 'selection': 'random'}
    key, draw, ambiguous = _referenced_draw(None, namespace, None, {'index': {'f': groups}})
    expected = next(([f'{i}-{j}' for j, pair in enumerate(group) if pair == (sample_size, 'random')]
                     for i, group in enumerate(snapshots) if (sample_size, 'random') in group), [])
    if len(expected) > 1:
        assert key is None and draw is None and set(ambiguous) == set(expected)
    elif expected:
        assert key == expected[0] and draw == namespace and not ambiguous
    else:
        assert key is None and draw is None and not ambiguous
