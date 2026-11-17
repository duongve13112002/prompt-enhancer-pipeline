"""Round-3 regressions; synthetic inputs and labelled sub-probe/checkpoint doubles only."""

import asyncio
import ast
from copy import deepcopy
import inspect
import json
from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest

from k2_fixtures import NamingServer, folder, image_bytes, invoke, scan
from test_k2_fix_round1 import snapshot
from test_k2_fix_round2 import probe_run
from test_storage_references import referenced_snapshot
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.k2_naming import _recovery_index
from prompt_enhancer.model_client import BoundModelClient
from prompt_enhancer.model_probe import ProbeResult


@pytest.mark.parametrize('sample', [1, 3, 'design_required', None])
def test_unrequested_observer_never_reads_naming_or_probes_multiple(k2_env, monkeypatch, sample):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    run.config['vlm']['roles']['observer'] = deepcopy(run.config['vlm']['roles']['folder_namer'])
    if sample is None:
        del run.config['knowledge']
    else:
        run.config['knowledge']['naming']['sample_size'] = sample

    async def execute():
        try:
            await run.for_role('observer')
        finally:
            await run.close()

    asyncio.run(execute())
    assert not [item for item in seen if item[0] == 'multiple']
    assert run.probes['main'].capabilities['json_schema'] is True


def test_generic_model_modules_have_no_knowledge_configuration_key():
    source = Path(__file__).resolve().parents[1] / 'src' / 'prompt_enhancer'
    for name in ('model_client', 'model_probe', 'model_mapping', 'model_transport', 'model_image'):
        tree = ast.parse((source / (name + '.py')).read_text(encoding='utf-8'))
        assert not [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and (node.value == 'knowledge' or node.value.startswith('knowledge.'))], name


def test_requirement_is_keyword_only_on_run_and_bound_clients():
    from prompt_enhancer.model_client import ModelClientRun, RoleClient
    for method in (ModelClientRun.for_role, BoundModelClient.for_role, RoleClient.ensure_role_capabilities):
        parameter = inspect.signature(method).parameters['require']
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default == ()


def test_actual_multi_image_call_states_demand_after_plain_bind(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)

    async def execute():
        try:
            bound = await run.for_role('folder_namer')
            assert not [item for item in seen if item[0] == 'multiple']
            value, record = await bound.call(b'Test-only system prompt', json.dumps({'folder_name': 'f'}),
                images=[('knowledge_reference', image_bytes()), ('knowledge_reference', image_bytes((1, 2, 3)))])
            assert value is not None and record['status'] == 'OK'
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('multiple', 2)) == 1


def test_bound_client_forwards_explicit_demand(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)

    async def execute():
        try:
            await BoundModelClient(run, 'info_assessor').for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('multiple', 2)) == 1


@pytest.mark.parametrize('outcome', ['negative', 'incomplete'])
def test_explicit_demand_is_single_attempt_with_spare_allowance(k2_env, monkeypatch, outcome):
    import prompt_enhancer.model_client as module
    run, seen = probe_run(k2_env, monkeypatch, limit=20)

    async def multiple(*args):
        await asyncio.sleep(0)
        seen.append(('multiple', args[-2]))
        return (ProbeResult('complete', {'multiple_images': False}, {}) if outcome == 'negative'
                else ProbeResult('incomplete', {}, {}))

    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', multiple)

    async def execute():
        try:
            for _ in range(5):
                await run.for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('multiple', 2)) == 1
    assert run.client._probe_remaining['main'] == 14
    assert run.probes['main'].capabilities.get('multiple_images') is (False if outcome == 'negative' else None)


def test_concurrent_explicit_binds_share_yielding_base_and_extension(k2_env, monkeypatch):
    import prompt_enhancer.model_client as module
    run, seen = probe_run(k2_env, monkeypatch, limit=20)

    async def base(*args):
        await asyncio.sleep(0)
        seen.append(('base', args[-2]))
        return ProbeResult('complete', {'image': True, 'json_schema': True}, {})

    async def multiple(*args):
        await asyncio.sleep(0)
        seen.append(('multiple', args[-2]))
        return ProbeResult('complete', {'multiple_images': True}, {})

    monkeypatch.setattr(module, 'probe_local_image_pair', base)
    monkeypatch.setattr(module, 'probe_local_thinking_pair', base)
    monkeypatch.setattr(module, 'probe_local_multiple_images_pair', multiple)

    async def execute():
        try:
            await asyncio.wait_for(asyncio.gather(
                run.for_role('folder_namer', require=('multiple_images',)),
                run.for_role('info_assessor'),
                run.for_role('image_alias', require=('multiple_images',))), timeout=5)
        finally:
            await run.close()

    asyncio.run(execute())
    assert seen.count(('base', 2)) == 2
    assert seen.count(('multiple', 2)) == 1
    assert run.client._probe_remaining['main'] == 14


def test_demand_property_uses_own_backend_and_declared_image_source(k2_env, monkeypatch):
    @given(source=st.sampled_from(['knowledge_reference', 'source_image', 'user_reference', None]),
           image=st.booleans(), own_backend=st.booleans(), demand=st.booleans(),
           sample=st.sampled_from([1, 3, 'design_required']))
    @settings(max_examples=48, deadline=None)
    def check(source, image, own_backend, demand, sample):
        run, _ = probe_run(k2_env, monkeypatch, limit=20)
        role = run.config['vlm']['roles']['folder_namer']
        role['backend'] = 'main' if own_backend else 'embedder'
        role['image'] = dict(role['image'], source=source) if image else 'none'
        run.config['knowledge']['naming']['sample_size'] = sample
        # Pure construction check; no transport opened by the pair builder.
        try:
            pair = run.client._multiple_images_pair('main', 'folder_namer',
                                                   require=('multiple_images',) if demand else ())
            assert (pair is not None) == (demand and own_backend and image and source is not None)
        finally:
            asyncio.run(run.close())

    check()


def test_missing_source_does_not_spend_explicit_probe_allowance(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    del run.config['vlm']['roles']['folder_namer']['image']['source']

    async def execute():
        try:
            # vlm_role.v0 requires image.source: a missing one is refused before any probe (R-015).
            with pytest.raises(ConfigurationError, match=r'vlm\.roles\.folder_namer\.image\.source'):
                await run.for_role('folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert not seen


def test_explicit_demand_never_attaches_pair_to_fallback(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    run.config['vlm']['roles']['folder_namer']['backend'] = 'embedder'

    async def execute():
        try:
            await run.client.ensure_role_capabilities('main', 'folder_namer', require=('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert not [item for item in seen if item[0] == 'multiple']


def test_actual_fallback_does_not_forward_requirement(k2_env, monkeypatch):
    from prompt_enhancer.model_client import ModelCallError
    from prompt_enhancer.model_resources import BudgetExhausted
    run = NamingServer().run(k2_env)
    run.config['vlm']['roles']['folder_namer']['backend'] = 'alternate'
    backend = deepcopy(run.config['vlm']['backends']['main'])
    backend['on_budget_exhausted'] = 'fallback_to_default_backend'
    run.config['vlm']['backends']['alternate'] = backend
    run.probes['alternate'] = run.probes['main']
    run.probes['main'] = ProbeResult('complete', {'image': True, 'json_schema': True}, {})
    run.client._probe_remaining['main'] = 0  # labelled already-started fallback evidence, no fresh allowance

    class ExhaustedTransport:
        """Labelled exhausted primary backend, no HTTP."""
        async def post(self, *args):
            raise BudgetExhausted('alternate')

        async def close(self):
            pass

    run.transports['alternate'] = ExhaustedTransport()

    async def forbidden(*args, **kwargs):
        pytest.fail('a primary role requirement was forwarded to the fallback backend')

    monkeypatch.setattr(run.client, 'ensure_role_capabilities', forbidden)

    async def execute():
        try:
            with pytest.raises(ModelCallError, match='CAPABILITY_UNAVAILABLE'):
                await run.client.call('folder_namer', b'Test-only prompt', json.dumps({'folder_name': 'f'}),
                    images=[('knowledge_reference', image_bytes()), ('knowledge_reference', image_bytes((1, 2, 3)))])
        finally:
            await run.close()

    asyncio.run(execute())


@pytest.mark.parametrize('own_backend', [True, False])
def test_supplied_local_pair_cannot_bypass_role_demand_or_backend(k2_env, monkeypatch, own_backend):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    run.client.local_probe_plans['main']['multiple_images'] = ('first', 'second')
    if not own_backend:
        run.config['vlm']['roles']['folder_namer']['backend'] = 'embedder'

    async def execute():
        try:
            await run.client.start_backend('main', role_name='folder_namer',
                                           require=() if own_backend else ('multiple_images',))
        finally:
            await run.close()

    asyncio.run(execute())
    assert not [item for item in seen if item[0] == 'multiple']


@pytest.mark.parametrize('sample,count,expected', [
    (1, 3, ()), (3, 1, ()), (2, 3, ('multiple_images',)), (3, 3, ('multiple_images',))])
def test_naming_caller_states_actual_image_demand(k2_env, monkeypatch, sample, count, expected):
    folder(k2_env, 'f', count=count)
    k2_env.config['knowledge']['naming']['sample_size'] = sample
    calls = []
    original = BoundModelClient.for_role

    async def record(self, role, **kwargs):
        calls.append((role, kwargs.get('require', ())))
        return await original(self, role, **kwargs)

    monkeypatch.setattr(BoundModelClient, 'for_role', record)
    result = invoke(k2_env)
    assert not result['report']['failed']
    assert calls == [('folder_namer', expected)]


@pytest.mark.parametrize('value', ['design_required', 'fit_from_pilot', 0, -1, True, 1.0, None, '2'])
def test_invalid_sample_is_keyed_configuration_refusal_before_binding(k2_env, value):
    k2_env.config['knowledge']['naming']['sample_size'] = value
    server = NamingServer()
    with pytest.raises(ConfigurationError, match=r'knowledge\.naming\.sample_size'):
        invoke(k2_env, server)
    assert not server.requests


def test_stage_owned_run_receives_demand_and_is_closed(k2_env, monkeypatch):
    import prompt_enhancer.k2_naming as module
    folder(k2_env, 'f', count=3)
    result = scan(k2_env)
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    monkeypatch.setattr(module, 'ModelClientRun', lambda *args, **kwargs: run)
    closed = []
    original = run.close

    async def close():
        closed.append(True)
        await original()

    monkeypatch.setattr(run, 'close', close)
    named = asyncio.run(module.name_folders(k2_env.store, k2_env.config, result['handoff']['k2'],
        images=result['rows']['images'], folder_images=result['rows']['folder_images'], knowledge_root=k2_env.source))
    assert not named['report']['failed']
    assert seen.count(('multiple', 2)) == 1
    assert closed == [True]


def test_recovery_skips_manifest_draw_with_removed_checkpoint(k2_env):
    folder(k2_env, 'f', count=3)
    result = invoke(k2_env)
    snapshot(k2_env, result)
    key = result['report']['redraws'][0]['draw_key']
    with k2_env.store.writer() as writer:
        draws = writer.checkpoints('k2_draw')
        assert draws.reusable(key) is not None
        # Labelled checkpoint-loss double: remove the indexed checkpoint, retaining the real live manifest/blob.
        del draws._rows[key]
        assert draws.reusable(key) is None
        assert _recovery_index(writer, draws) == {}


def test_malformed_manifest_paths_are_precise_refusals(k2_env, monkeypatch):
    from prompt_enhancer.storage import StorageError
    sid, _ = referenced_snapshot(k2_env.store, 'malformed')
    manifest = k2_env.store._manifest
    entries = manifest(k2_env.store._snapshot_dir(sid))
    artifact = next(i for i, entry in enumerate(entries) if entry['kind'] == 'artifact')

    @given(st.one_of(st.sampled_from(['', 'bad', 'artifacts', 'artifacts/k2_draw',
                                    'elsewhere/k2_draw/aa/bad.bin', '/absolute', None, 42]),
                    st.text(alphabet=st.characters(blacklist_characters='/'), max_size=40)))
    @settings(max_examples=20, deadline=None)
    def check(value):
        altered = deepcopy(entries)
        altered[artifact]['path'] = value
        # Labelled manifest-reader double isolates path validation after the checksum boundary.
        monkeypatch.setattr(k2_env.store, '_manifest', lambda *args: altered)
        with pytest.raises(StorageError) as raised:
            k2_env.store.referenced_artifacts('k2_draw')
        assert sid in str(raised.value)
        assert f'entry {artifact}' in str(raised.value)
        assert repr(value) in str(raised.value)

    check()


@pytest.mark.parametrize('value', ['bad', 'artifacts/k2_draw', 'elsewhere/k2_draw/aa/bad.bin'])
def test_checksummed_malformed_manifest_names_snapshot_and_entry(k2_env, value):
    import pyarrow as pa
    from prompt_enhancer.storage import StorageError
    from prompt_enhancer.storage.tables import MANIFEST_SCHEMA
    sid, _ = referenced_snapshot(k2_env.store, 'malformed-on-disk')
    directory = k2_env.store._snapshot_dir(sid)
    entries = k2_env.store._manifest(directory)
    index = next(i for i, entry in enumerate(entries) if entry['kind'] == 'artifact')
    entries[index]['path'] = value
    # Labelled corrupt-manifest fixture: keep checksums consistent to reach structural validation on real bytes.
    with k2_env.store.store_lock():
        _, digest = k2_env.store._write_table(directory + '/MANIFEST.parquet',
                                             pa.Table.from_pylist(entries, schema=MANIFEST_SCHEMA), 'parquet')
        rows = k2_env.store._registry()
        rows[sid]['manifest_sha256'] = digest
        k2_env.store._write_registry(rows)
    with pytest.raises(StorageError) as raised:
        k2_env.store.referenced_artifacts('k2_draw')
    assert sid in str(raised.value)
    assert f'entry {index}' in str(raised.value)
    assert repr(value) in str(raised.value)
