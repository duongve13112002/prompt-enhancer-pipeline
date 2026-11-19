"""Whole-flow CPU contract tests over real stages and labelled HTTP doubles."""

from pathlib import Path
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from k2_fixtures import environment, folder, source_tree
from kb_sync_fixtures import SyncServer, inventory, invoke
from link_fixtures import drop_link, is_directory_link, make_link
from prompt_enhancer import kb_sync as module
from prompt_enhancer.k5_index import ServingIndex
from prompt_enhancer.storage import StorageError, concept_id
from prompt_enhancer.storage.store import SnapshotBuilder, Writer
from prompt_enhancer.storage.tables import SNAPSHOT_TABLES


def test_full_sync_uses_one_writer_and_target_snapshot_id(k2_env, monkeypatch):
    folder(k2_env, 'f', '猫|Áo\nfirst|second')
    folder(k2_env, 'shared', '猫\nanother')
    before = source_tree(k2_env.source)
    opened = []
    original = Writer.__enter__

    def enter(writer):
        opened.append(writer)
        return original(writer)

    monkeypatch.setattr(Writer, '__enter__', enter)
    result = invoke(k2_env, SyncServer())
    assert result['status'] == 'published' and len(opened) == 1
    with k2_env.store.pin() as pin:
        index = ServingIndex(pin, k2_env.config)
        assert index.exact('猫') == [concept_id('猫')]
        assert all(card['revisions']['snapshot_id'] == result['snapshot_id'] for card in pin.read_rows('cards'))
        assert {row['text_kind'] for row in pin.read_rows('concept_text_embeddings')} == {
            'name', 'description', 'alias', 'card_summary'}
        assert {row['concept_id'] for row in pin.read_rows('concepts')} == {concept_id('猫'), concept_id('Áo')}
    assert source_tree(k2_env.source) == before


def test_a_sync_reads_the_knowledge_base_through_a_link_in_every_stage(k2_env):
    """Fix round 1 (F1, F2; decision 174): `knowledge.root` may be a link. K0 to K4 each resolve it again, and one that
    still refused it would fail after the paid stages before it, so the whole flow runs. The link is checked to be one."""
    env = k2_env
    folder(env, 'f', '猫|Áo\nfirst|second')
    link = env.source.parent / 'kb-link'
    make_link(link, env.source)
    try:
        assert is_directory_link(link)
        env.config['knowledge']['root'] = link.relative_to(env.root).as_posix()
        server = SyncServer()
        result = invoke(env, server)
        assert result['status'] == 'published'
        assert {'observer', 'card_builder', 'alias_generator'} <= set(server.roles)  # K2 observe, K3 and K4 all ran
        with env.store.pin() as pin:
            assert {row['concept_id'] for row in pin.read_rows('concepts')} == {concept_id('猫'), concept_id('Áo')}
    finally:
        drop_link(link)


@settings(max_examples=3, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.sampled_from(['猫', 'Áo', 'Name']))
def test_unchanged_sync_has_no_calls_or_new_files(project_root, tmp_path, model_test_profile, name):
    path = Path(tempfile.mkdtemp(dir=tmp_path))
    env = environment(project_root, path, model_test_profile)
    folder(env, 'f', name + '\ndescription')
    server = SyncServer()
    initial = invoke(env, server)
    before = inventory(env.store.root)
    calls = (len(server.requests), len(server.embedder.requests))
    repeated = invoke(env, server)
    assert repeated['status'] == 'unchanged' and repeated['snapshot_id'] == initial['snapshot_id']
    assert inventory(env.store.root) == before
    assert (len(server.requests), len(server.embedder.requests)) == calls


def test_full_sync_plan_is_read_only(k2_env):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    before = inventory(k2_env.store.root)
    result = invoke(k2_env, server, plan=True)
    assert result['status'] == 'planned' and result['model_calls']['conditional']
    assert inventory(k2_env.store.root) == before
    assert not server.requests and not server.embedder.requests
    invoke(k2_env, server)
    before = inventory(k2_env.store.root)
    result = invoke(k2_env, server, plan=True)
    assert inventory(k2_env.store.root) == before and result['status'] == 'planned'


# A failed K4 alias key no longer blocks the publish (decision 165): tests/test_kb_sync_alias_failure.py.
@pytest.mark.parametrize('stage', ['k1_image', 'k2_naming', 'k2_observation', 'k3', 'k1_text'])
def test_every_failed_stage_blocks_publication(k2_env, monkeypatch, stage):
    folder(k2_env, 'f', 'Name\ndescription')
    function = {'k1_image': 'embed_images', 'k2_naming': 'name_folders', 'k2_observation': 'observe_images',
                'k3': 'build_cards', 'k1_text': 'embed_concept_texts'}[stage]
    original = getattr(module, function)

    async def fail(*args, **kwargs):
        result = await original(*args, **kwargs)
        result['report']['failed']['labelled-item'] = 'SCHEMA_INVALID'
        return result

    monkeypatch.setattr(module, function, fail)
    monkeypatch.setattr(SnapshotBuilder, 'finish', lambda *a, **k: pytest.fail('finish on failure'))
    with pytest.raises(StorageError, match=f'^{stage}: STAGE_FAILED: count=1; codes=SCHEMA_INVALID=1$'):
        invoke(k2_env, SyncServer())
    assert k2_env.store.current() is None


def test_sync_stage_order_and_text_inputs(k2_env, monkeypatch):
    folder(k2_env, 'f', 'Name|Other\nfirst|')
    (k2_env.source / 'kb_overrides.yaml').write_text('Name:\n  aliases: [accepted, Other]\n', encoding='utf-8')
    order, captured = [], []
    for name in ('embed_images', 'name_folders', 'observe_images', 'build_cards', 'build_aliases', 'embed_concept_texts'):
        original = getattr(module, name)

        async def wrapped(*args, _original=original, _name=name, **kwargs):
            order.append(_name)
            if _name == 'embed_concept_texts':
                captured.extend(args[2])
            return await _original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)
    invoke(k2_env, SyncServer())
    assert order == ['embed_images', 'name_folders', 'observe_images', 'build_cards', 'build_aliases', 'embed_concept_texts']
    assert ('alias', 'accepted') in {(item.text_kind, item.text) for item in captured}
    assert ('alias', 'Other') not in {(item.text_kind, item.text) for item in captured}
    values = [(item.concept_id, item.text_kind, item.text) for item in captured]
    assert values == sorted(set(values)) and all(value[2] for value in values)


def test_k5_validation_required(k2_env, monkeypatch):
    folder(k2_env, 'f', 'Name\ndescription')
    monkeypatch.setattr(module, 'build_index', lambda *args: {'validated': False})
    with pytest.raises(StorageError, match='k5: VALIDATION_REQUIRED'):
        invoke(k2_env, SyncServer())
    assert k2_env.store.current() is None


@pytest.mark.parametrize('kind', ['empty', 'declared-broken', 'nameless-broken'])
def test_empty_and_undecodable_sources_are_explicit_skips(k2_env, kind):
    if kind != 'empty':
        folder(k2_env, 'f', 'Name\ndescription' if kind == 'declared-broken' else None,
               payload=b'labelled undecodable source')
    result = invoke(k2_env, SyncServer())
    assert result['status'] == 'published'
    assert bool(result['stages']['k1_image']['decode_failed']) == (kind != 'empty')
    with k2_env.store.pin() as pin:
        assert len(pin.read_rows('concepts')) == (1 if kind == 'declared-broken' else 0)
        ServingIndex(pin, k2_env.config)


def logical(env):
    with env.store.pin() as pin:
        rows = {name: pin.read_rows(name) for name in SNAPSHOT_TABLES}
        for card in rows['cards']:
            card['revisions']['snapshot_id'] = 'normalized'
        index = ServingIndex(pin, env.config)
        answers = {row['display_name']: index.exact(row['display_name']) for row in rows['concepts']}
        return rows, answers


@pytest.mark.parametrize('boundary', ['_checkpoint', 'embed_images', 'name_folders', 'observe_images', 'build_cards',
                                    'build_aliases', 'embed_concept_texts', 'build_index', 'finish', 'publish'])
@settings(max_examples=2, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.sampled_from(['猫', 'Name']))
def test_resume_at_every_stage_boundary(project_root, tmp_path, model_test_profile, monkeypatch, boundary, name):
    base = Path(tempfile.mkdtemp(dir=tmp_path))
    reference_path, resumed_path = base / 'reference', base / 'resumed'
    reference_path.mkdir()
    resumed_path.mkdir()
    reference = environment(project_root, reference_path, model_test_profile)
    resumed = environment(project_root, resumed_path, model_test_profile)
    for env in (reference, resumed):
        folder(env, 'f', name + '\ndescription')
    # Only assigned run metadata is normalized; source timestamps must actually be equal.
    import os
    source = reference.source / 'f/0.png'
    stat = source.stat()
    os.utime(resumed.source / 'f/0.png', ns=(stat.st_atime_ns, stat.st_mtime_ns))
    invoke(reference, SyncServer())
    server = SyncServer()
    owner = SnapshotBuilder if boundary == 'finish' else Writer if boundary == 'publish' else module
    original = getattr(owner, boundary)
    with monkeypatch.context() as patch:
        if boundary in ('_checkpoint', 'build_index', 'finish', 'publish'):
            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('labelled interruption after ' + boundary)
        else:
            async def interrupted(*args, **kwargs):
                await original(*args, **kwargs)
                raise RuntimeError('labelled interruption after ' + boundary)
        patch.setattr(owner, boundary, interrupted)
        with pytest.raises(RuntimeError, match='labelled interruption'):
            invoke(resumed, server)
    invoke(resumed, server)
    assert logical(resumed) == logical(reference)
    assert len(server.roles) == len(set(server.roles))  # completed item replies were never called again


def test_resume_preserves_saved_naming_draw(k2_env, monkeypatch):
    folder(k2_env, 'f', count=2)
    server = SyncServer()
    original = module.name_folders
    with monkeypatch.context() as patch:
        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError('labelled stop after saved draw')
        patch.setattr(module, 'name_folders', interrupted)
        with pytest.raises(RuntimeError, match='saved draw'):
            invoke(k2_env, server)
    with k2_env.store.writer() as writer:
        draws = {key: writer.checkpoints('k2_draw').load(key[1]) for key, row in writer._state_rows().items()
                 if row['stage'] == 'k2_draw'}
    assert draws
    invoke(k2_env, server)
    with k2_env.store.writer() as writer:
        assert {key: writer.checkpoints('k2_draw').load(key[1]) for key, row in writer._state_rows().items()
                if row['stage'] == 'k2_draw'} == draws
    assert server.roles.count('folder_namer') == 1


def test_interruption_before_pointer_movement_preserves_current(k2_env, monkeypatch):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    current = k2_env.store.current()
    (k2_env.source / 'f/info.txt').write_text('Name\nchanged', encoding='utf-8')
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise RuntimeError('labelled stop before pointer')
        patch.setattr(Writer, 'publish', interrupted)
        with pytest.raises(RuntimeError, match='before pointer'):
            invoke(k2_env, server)
    assert k2_env.store.current() == current
    calls = len(server.requests)
    assert invoke(k2_env, server)['status'] == 'published'
    assert len(server.requests) == calls
