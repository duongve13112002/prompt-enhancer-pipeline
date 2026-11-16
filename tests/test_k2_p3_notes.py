"""Accepted P1 P3 notes closed with labelled sub-probes and synthetic storage manifests."""

import asyncio
import hashlib

from hypothesis import given, settings, strategies as st
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from test_k2_fix_round2 import probe_run
from test_storage_references import referenced_snapshot
from prompt_enhancer.model_client import BoundModelClient, CAPABILITIES, validate_require
from prompt_enhancer.storage import StorageError
from prompt_enhancer.storage.identity import check_artifact_path


@pytest.mark.parametrize('entry', ['run', 'bound', 'start', 'ensure'])
@pytest.mark.parametrize('invalid', ['multiple_images_extra', 'xmultiple_images', ('bogus',), None,
                                   ['multiple_images'], (1,), ('image', 'unknown')])
def test_require_exact_known_names_before_any_activity(k2_env, monkeypatch, entry, invalid):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    async def execute():
        try:
            with pytest.raises(ValueError, match='require'):
                if entry == 'run':
                    await run.for_role('folder_namer', require=invalid)
                elif entry == 'bound':
                    await BoundModelClient(run, 'observer').for_role('folder_namer', require=invalid)
                elif entry == 'start':
                    await run.client.start_backend('main', require=invalid)
                else:
                    await run.client.ensure_role_capabilities('main', 'folder_namer', require=invalid)
            assert not seen and run.client._probe_remaining == {}
        finally:
            await run.close()
    asyncio.run(execute())


@given(st.sets(st.sampled_from(sorted(CAPABILITIES))), st.text(min_size=1).filter(lambda v: v not in CAPABILITIES))
@settings(max_examples=35, deadline=None)
def test_require_validator_property(known, unknown):
    assert validate_require(tuple(sorted(known))) == tuple(sorted(known))
    assert validate_require(frozenset(known)) == known
    with pytest.raises(ValueError, match='unknown capability'):
        validate_require(tuple(known) + (unknown,))
    with pytest.raises(ValueError, match='tuple or frozenset'):
        validate_require('multiple_images' + unknown)


def test_repeated_start_extends_cached_backend(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    async def execute():
        try:
            first = await run.client.start_backend('main', role_name='folder_namer')
            assert first.capabilities.get('multiple_images') is None
            result = await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))
            assert result.capabilities['multiple_images'] is True
            await run.client.start_backend('main', role_name='folder_namer', require=('multiple_images',))
        finally:
            await run.close()
    asyncio.run(execute())
    assert seen == [('image', 2), ('thinking', 2), ('multiple', 2)]
    assert run.client._probe_remaining['main'] == 14


def test_backend_only_plan_is_explicit_proof_demand(k2_env, monkeypatch):
    run, seen = probe_run(k2_env, monkeypatch, limit=20)
    run.client.local_probe_plans['main']['multiple_images'] = ('one', 'two')
    async def execute():
        try:
            result = await run.client.start_backend('main')
            assert result.capabilities['multiple_images'] is True
        finally:
            await run.close()
    asyncio.run(execute())
    assert seen == [('image', 2), ('thinking', 2), ('multiple', 2)]


@pytest.mark.parametrize('suffix', ['parquet', 'arrow'])
def test_draw_suffix_refused_at_manifest_boundary(project_root, tmp_path, suffix):
    from storage_fixtures import make_store
    store = make_store(project_root, tmp_path / 'store')
    sid, relative = referenced_snapshot(store, 'suffix')
    path = relative.removesuffix('.bin') + '.' + suffix
    with pytest.raises(StorageError, match='k2_draw requires the .bin suffix'):
        check_artifact_path(path)
    directory = store._snapshot_dir(sid)
    from prompt_enhancer.storage.store import MANIFEST
    manifest_path = store.path(directory + '/' + MANIFEST)
    data = manifest_path.read_bytes()
    table = pq.read_table(pa.BufferReader(data))
    entries = table.to_pylist()
    for entry in entries:
        if entry['path'] == relative:
            entry['path'] = path
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(entries, schema=table.schema), sink)
    manifest_path.write_bytes(sink.getvalue().to_pybytes())
    with pytest.raises(StorageError, match=r'entry \d+:.*k2_draw requires'):
        store._manifest(directory, hashlib.sha256(manifest_path.read_bytes()).hexdigest())


def test_storage_context_key_keeps_shared_observations_distinct(project_root):
    from storage_fixtures import dataset
    from prompt_enhancer.storage import Catalog
    catalog = Catalog(project_root)
    row = dataset()['observations'][0]
    other = dict(row, context_key='b' * 64, value='another context')
    assert catalog.build('observations', [row, other]).num_rows == 2
    with pytest.raises(StorageError, match='duplicate key'):
        catalog.build('observations', [row, row])
    with pytest.raises(StorageError, match='context_key'):
        catalog.build('observations', [dict(row, context_key='invalid')])
