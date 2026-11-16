"""Preflight domains, current-only outputs and consumed overrides with labelled doubles."""

import json

import pytest

from k2_fixtures import folder
from kb_sync_fixtures import SyncServer, invoke
from prompt_enhancer import kb_sync as module
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.k5_index import ServingIndex
from prompt_enhancer.storage import StorageError, concept_id


@pytest.mark.parametrize(('key', 'value'), [
    ('storage.lock.timeout_s', float('inf')), ('storage.lock.poll_s', 6),
    ('storage.lock.timeout_s', 10 ** 400), ('sync.snapshots.keep', 1.0),
    ('sync.gc.retention', 'invalid'), ('runtime.cpu_workers', True),
    ('sync.mass_deletion_guard.max_removed_fraction', float('nan')),
    ('index.engine', 'unsupported'), ('embedding.dimension', 2.0),
])
def test_invalid_sync_domains_refused_before_store(k2_env, monkeypatch, key, value):
    from test_kb_sync_gates import set_leaf
    set_leaf(k2_env.config, key, value)
    monkeypatch.setattr(module, 'open_knowledge_store', lambda *a, **k: pytest.fail('store before refusal'))
    with pytest.raises(ConfigurationError):
        invoke(k2_env, SyncServer())


@pytest.mark.parametrize('change', ['info', 'override', 'move', 'remove', 'policy', 'index', 'prompt', 'schema',
                                   'configured_schema', 'query_limit', 'tool'])
def test_sync_identity_invalidation(k2_env, monkeypatch, change):
    folder(k2_env, 'f', 'Name\ndescription')
    folder(k2_env, 'g', 'Other\nother description')
    server = SyncServer()
    if change == 'configured_schema':
        path = k2_env.root / 'contracts/schemas/alternate_schema.schema.json'
        original_schema = json.loads((k2_env.root / 'contracts/schemas/alias_generator_reply.v0.schema.json').read_bytes())
        original_schema['$id'] = 'alternate_schema'
        path.write_text(json.dumps(original_schema), encoding='utf-8')
        k2_env.config['vlm']['roles']['alias_generator']['output']['schema_id'] = 'alternate_schema'
    original = invoke(k2_env, server)['snapshot_id']
    if change == 'info':
        (k2_env.source / 'f/info.txt').write_text('Name\nchanged', encoding='utf-8')
    elif change == 'override':
        (k2_env.source / 'kb_overrides.yaml').write_text('Name:\n  aliases: [author]\n', encoding='utf-8')
    elif change == 'move':
        (k2_env.source / 'f').rename(k2_env.source / 'moved')
    elif change == 'remove':
        for path in (k2_env.source / 'f').iterdir():
            path.unlink()
        (k2_env.source / 'f').rmdir()
    elif change == 'policy':
        k2_env.config['cards']['min_visible_images'] = 3
    elif change == 'index':
        k2_env.config['index']['build']['block_rows'] = 1
    elif change == 'prompt':
        path = k2_env.root / 'prompts' / (k2_env.config['vlm']['roles']['alias_generator']['prompt_id'] + '.txt')
        path.write_bytes(path.read_bytes() + b'\nLabelled test revision.\n')
    elif change in ('schema', 'configured_schema'):
        name = 'alternate_schema' if change == 'configured_schema' else 'alias_generator_reply.v0'
        path = k2_env.root / 'contracts/schemas' / (name + '.schema.json')
        value = json.loads(path.read_bytes())
        value['$comment'] = value.get('$comment', '') + ' Labelled schema revision'
        path.write_text(json.dumps(value), encoding='utf-8')
    elif change == 'query_limit':
        k2_env.config['embedding']['query_text_limit'] = 256
    else:
        revision = module.tool_revision
        monkeypatch.setattr(module, 'tool_revision', lambda *names: revision(*names) + 'test-revision')
    changed = invoke(k2_env, server)
    assert changed['status'] == 'published' and changed['snapshot_id'] != original
    assert invoke(k2_env, server)['status'] == 'unchanged'
    with k2_env.store.pin() as pin:
        index = ServingIndex(pin, k2_env.config)
        assert index.exact('Name') == ([] if change == 'remove' else [concept_id('Name')])
        if change == 'remove':
            assert all(row['concept_id'] != concept_id('Name') for row in pin.read_rows('cards'))


def test_current_corruption_cannot_pass_unchanged(k2_env):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    current = k2_env.store.current()
    path = k2_env.store.root / 'snapshots' / current[0] / 'index/exact.arrow'
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    calls = len(server.requests)
    with pytest.raises(StorageError, match='checksum'):
        invoke(k2_env, server)
    assert len(server.requests) == calls and k2_env.store.current() == current


def test_sync_read_overrides_are_observed(k2_env, monkeypatch):
    k2_env.config['storage'].update(table_format='arrow', lock={'timeout_s': 7, 'poll_s': 0.02})
    k2_env.config['sync'].update(resume=False, snapshots={'keep': 2}, gc={'retention': '2d'})
    k2_env.config['runtime']['cpu_workers'] = 1
    k2_env.config['knowledge'].update(concept_separator=';', image_extensions=['.png'])
    k2_env.config['knowledge']['info_file'].update(names=['custom.txt'], encoding='latin-1')
    path = folder(k2_env, 'f', count=1)
    (path / 'custom.txt').write_bytes('Name;Other\nfirst;second'.encode('latin-1'))
    observed = []
    open_store = module.open_knowledge_store

    def opening(*args, **kwargs):
        store = open_store(*args, **kwargs)
        observed.append((store._table_format, store.timeout_s, store.poll_s, store._resume, store._keep,
                         store._retention_s))
        return store

    monkeypatch.setattr(module, 'open_knowledge_store', opening)
    result = invoke(k2_env, SyncServer())
    assert observed == [('arrow', 7, 0.02, False, 2, 172800)]
    assert result['report']['workers'] == 1
    with k2_env.store.pin() as pin:
        assert {row['display_name'] for row in pin.read_rows('concepts')} == {'Name', 'Other'}
        assert all(entry['format'] == 'arrow' for (kind, _), entry in pin._entries.items() if kind == 'table')


def test_alias_tool_revision_invalidates_its_items_only(k2_env, monkeypatch):
    from prompt_enhancer import k4_aliases
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    server.roles.clear()
    embedding_calls = len(server.embedder.requests)
    revision = module.tool_revision
    monkeypatch.setattr(module, 'tool_revision', lambda *names: revision(*names) + 'test-change')
    monkeypatch.setattr(k4_aliases, 'tool_revision', lambda *names: revision(*names) + 'test-change')
    invoke(k2_env, server)
    assert server.roles == ['alias_generator']
    assert len(server.embedder.requests) == embedding_calls


def test_transport_change_reuses_semantic_items(k2_env):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    count = len(server.requests)
    k2_env.config['vlm']['backends']['main']['http']['read_timeout_s'] = 2
    result = invoke(k2_env, server)
    assert result['status'] == 'published' and len(server.requests) == count


def test_override_change_between_identity_and_naming_blocks_publish(k2_env, monkeypatch):
    folder(k2_env, 'f', 'Name\ndescription')
    original = module.name_folders

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        result['overrides'] = {'data': {}, 'file_hash': 'labelled intervening source change'}
        return result

    monkeypatch.setattr(module, 'name_folders', changed)
    with pytest.raises(StorageError, match='k2_naming: SOURCE_CHANGED: overrides'):
        invoke(k2_env, SyncServer())
    assert k2_env.store.current() is None


def test_coordinator_guard_plan_and_force(k2_env):
    folder(k2_env, 'f', 'Name\ndescription')
    folder(k2_env, 'g', 'Other\nother description')
    server = SyncServer()
    invoke(k2_env, server)
    current = k2_env.store.current()
    for path in (k2_env.source / 'g').iterdir():
        path.unlink()
    (k2_env.source / 'g').rmdir()
    k2_env.config['sync']['mass_deletion_guard']['max_removed_fraction'] = 0
    calls = len(server.requests)
    plan = invoke(k2_env, server, plan=True)
    assert plan['report']['guard']['exceeded'] and len(server.requests) == calls
    with pytest.raises(ValueError, match='mass deletion guard'):
        invoke(k2_env, server)
    assert k2_env.store.current() == current
    result = invoke(k2_env, server, force=True)
    assert result['status'] == 'published' and result['report']['guard']['forced']
    with k2_env.store.pin() as pin:
        assert ServingIndex(pin, k2_env.config).exact('Other') == []
