"""Decision 165: a failed K4 alias key publishes without that concept's generated aliases and is retried next sync.

Real stages over labelled HTTP doubles (`SyncServer`); no network, no inference."""

import json

import pytest
from hypothesis import given, settings, strategies as st

from k2_fixtures import folder
from kb_sync_fixtures import SyncServer, invoke
from prompt_enhancer.k2_naming import _digest
from prompt_enhancer.kb_sync import _CLOSED_CODES, alias_failures, require_per_concept
from prompt_enhancer.storage import StorageError, concept_id
from prompt_enhancer.storage.identity import is_concept_id

MARKER = 'MODEL_TEXT_MUST_NEVER_ENTER_DIAGNOSTICS'
NAME, OTHER = concept_id('Name'), concept_id('Other')


def setup(env):
    folder(env, 'f', 'Name\ndescription')
    folder(env, 'peer', 'Other\npeer description')
    (env.source / 'kb_overrides.yaml').write_text('Name:\n  aliases: [author]\n', encoding='utf-8')
    server = SyncServer()
    server.fail_role, server.fail_concept = 'alias_generator', 'Name'
    return server


def rows(env, sid):
    with env.store.pin() as pin:
        assert pin.snapshot_id == sid
        aliases = {}
        for row in pin.read_rows('aliases'):
            aliases.setdefault(row['concept_id'], []).append((row['origin'], row['alias_text']))
        texts = [(row['concept_id'], row['text_kind']) for row in pin.read_rows('concept_text_embeddings')]
        return {key: sorted(value) for key, value in aliases.items()}, texts


def summary(env, sid):
    return env.store._registry()[sid]['source_scan_summary']


def test_a_failed_alias_publishes_without_its_generated_rows_and_records_the_failure(k2_env):
    server = setup(k2_env)
    result = invoke(k2_env, server)
    assert result['status'] == 'published'
    assert result['alias_failures'] == {'count': 1, 'codes': {'SCHEMA_INVALID': 1}, 'concept_ids': [NAME]}
    aliases, texts = rows(k2_env, result['snapshot_id'])
    # Author data survives a model failure; the other concept keeps its generated alias; nothing generated for Name.
    assert aliases[NAME] == [('author', 'author')]
    assert aliases[OTHER] == [('generated', 'Other alias')]
    assert texts.count((NAME, 'alias')) == 1 and texts.count((OTHER, 'alias')) == 1
    # Count, closed codes and a digest of the sorted failed ids; the ids themselves are not stored (F5, P3-SWEEP-2).
    assert summary(k2_env, result['snapshot_id'])['k4_failed'] == {
        'count': 1, 'codes': {'SCHEMA_INVALID': 1}, 'concept_ids_hash': _digest([NAME])}
    assert NAME not in json.dumps(summary(k2_env, result['snapshot_id']))
    assert result['stages']['k4']['failed'] == {NAME: 'SCHEMA_INVALID'}
    assert MARKER not in json.dumps(result, default=str) and MARKER not in json.dumps(
        k2_env.store._registry(), default=str)


def test_the_next_sync_retries_only_the_failed_key_and_publishes_when_it_succeeds(k2_env):
    server = setup(k2_env)
    first = invoke(k2_env, server)
    server.fail_role = None
    server.roles.clear()
    second = invoke(k2_env, server)
    assert second['status'] == 'published' and second['snapshot_id'] != first['snapshot_id']
    assert server.roles == ['alias_generator']  # the done key of Other is not called again (R-017)
    assert 'alias_failures' not in second and 'k4_failed' not in summary(k2_env, second['snapshot_id'])
    aliases, _ = rows(k2_env, second['snapshot_id'])
    assert aliases[NAME] == [('author', 'author'), ('generated', 'Name alias')]
    server.roles.clear()
    assert invoke(k2_env, server)['status'] == 'unchanged' and server.roles == []


def live_snapshots(env):
    """The snapshots a reader could pin or roll back to (the store's LIVE states), not an abandoned build."""
    return [item['snapshot_id'] for item in env.store.snapshots() if item['state'] in ('validated', 'published', 'retired')]


def test_a_key_that_keeps_failing_is_retried_but_publishes_no_duplicate_snapshot(k2_env):
    """P3-1 (PM decision pending user review): the retried key fails again and nothing else changed, so the sync
    reports the current snapshot as unchanged, lists the failure, and the live snapshots stay as they are."""
    server = setup(k2_env)
    first = invoke(k2_env, server)
    current, live = k2_env.store.current(), live_snapshots(k2_env)
    server.roles.clear()
    second = invoke(k2_env, server)
    assert second['status'] == 'unchanged' and second['publish_skipped'] == 'identical'
    assert second['snapshot_id'] == first['snapshot_id']
    assert server.roles == ['alias_generator']  # the failed key is still retried once; the done key is not repeated
    assert second['alias_failures'] == {'count': 1, 'codes': {'SCHEMA_INVALID': 1}, 'concept_ids': [NAME]}
    assert k2_env.store.current() == current and live_snapshots(k2_env) == live
    assert summary(k2_env, first['snapshot_id'])['k4_failed']['count'] == 1
    # The only trace of the skipped run is its abandoned build, which the next writer session marks failed (GC collects it).
    states = sorted(item['state'] for item in k2_env.store.snapshots())
    assert states == ['building', 'published']
    server.fail_role = None
    assert invoke(k2_env, server)['status'] == 'published'
    assert sorted(item['state'] for item in k2_env.store.snapshots()) == ['failed', 'published', 'retired']


def test_the_retry_still_publishes_after_a_skipped_identical_publish(k2_env):
    server = setup(k2_env)
    first = invoke(k2_env, server)
    assert invoke(k2_env, server)['status'] == 'unchanged'
    server.fail_role = None
    server.roles.clear()
    third = invoke(k2_env, server)
    assert third['status'] == 'published' and third['snapshot_id'] != first['snapshot_id']
    assert server.roles == ['alias_generator'] and 'alias_failures' not in third
    assert rows(k2_env, third['snapshot_id'])[0][NAME] == [('author', 'author'), ('generated', 'Name alias')]
    fourth = invoke(k2_env, server)  # the ordinary unchanged branch, not the identical-publish skip
    assert fourth['status'] == 'unchanged' and 'publish_skipped' not in fourth and 'alias_failures' not in fourth


def test_the_identity_check_needs_the_same_failures_and_the_same_alias_rows(k2_env):
    """The skip decision itself: any difference in identity, recorded failures or alias content publishes."""
    from prompt_enhancer.kb_sync import _publishes_the_same_snapshot
    first = invoke(k2_env, setup(k2_env))
    current, recorded = k2_env.store.current(), summary(k2_env, first['snapshot_id'])
    with k2_env.store.pin() as pin:
        stored = pin.read_rows('aliases')
    failures = first['alias_failures']

    def same(**changes):
        arguments = dict(recorded=recorded, identity=recorded['sync_identity'], failures=failures, aliases=stored)
        arguments.update(changes)
        return _publishes_the_same_snapshot(k2_env.store, current, **arguments)

    assert same()
    assert not same(identity='sha256:other')
    assert not same(recorded={key: value for key, value in recorded.items() if key != 'k4_failed'})
    assert not same(failures=dict(failures, count=2))
    assert not same(failures=dict(failures, codes={'HTTP_ERROR': 1}))
    # Same count, same codes, other concept (F5): a different failed concept is a different snapshot.
    assert not same(failures=dict(failures, concept_ids=[OTHER]))
    # A record from before the digest existed is never taken for identical; the next sync publishes once.
    assert not same(recorded=dict(recorded, k4_failed={'count': 1, 'codes': {'SCHEMA_INVALID': 1}}))
    assert not same(recorded=dict(recorded, k4_failed=dict(recorded['k4_failed'], concept_ids_hash=_digest([OTHER]))))
    assert not same(aliases=stored + [dict(stored[0], alias_text='an extra generated alias')])
    assert not same(aliases=stored[1:])
    assert not _publishes_the_same_snapshot(k2_env.store, None, recorded, recorded['sync_identity'], failures, stored)


def test_a_pinned_reader_serves_a_snapshot_that_recorded_failed_keys(k2_env):
    """P3-2: a reader that pins a snapshot with `k4_failed` reads author aliases and no generated alias of the failed
    concept, through the K5 index too, and keeps that view when a later sync publishes the repaired snapshot."""
    from prompt_enhancer.k5_index import ServingIndex
    server = setup(k2_env)
    first = invoke(k2_env, server)
    with k2_env.store.pin() as pin:
        assert pin.snapshot_id == first['snapshot_id']
        index = ServingIndex(pin, k2_env.config)
        assert index.exact('author') == [NAME] and index.exact('Name alias') == []
        assert index.exact('Other alias') == [OTHER]
        server.fail_role = None
        second = invoke(k2_env, server)
        assert second['status'] == 'published' and second['snapshot_id'] != pin.snapshot_id
        assert not [row for row in pin.read_rows('aliases') if row['concept_id'] == NAME and row['origin'] == 'generated']
        assert ServingIndex(pin, k2_env.config).exact('Name alias') == []  # the pinned view did not move
    with k2_env.store.pin() as pin:
        assert pin.snapshot_id == second['snapshot_id']
        assert ServingIndex(pin, k2_env.config).exact('Name alias') == [NAME]


def test_every_concept_failing_publishes_author_aliases_only(k2_env):
    """P3-2: with no generated alias at all the snapshot is still valid, serves the author aliases and records every key."""
    from prompt_enhancer.k5_index import ServingIndex
    server = setup(k2_env)
    server.fail_concept = None
    result = invoke(k2_env, server)
    assert result['status'] == 'published'
    assert result['alias_failures'] == {'count': 2, 'codes': {'SCHEMA_INVALID': 2}, 'concept_ids': sorted([NAME, OTHER])}
    assert summary(k2_env, result['snapshot_id'])['k4_failed'] == {
        'count': 2, 'codes': {'SCHEMA_INVALID': 2}, 'concept_ids_hash': _digest(sorted([NAME, OTHER]))}
    aliases, texts = rows(k2_env, result['snapshot_id'])
    assert aliases == {NAME: [('author', 'author')]}  # no generated row for any concept
    assert texts.count((NAME, 'alias')) == 1 and (OTHER, 'alias') not in texts
    with k2_env.store.pin() as pin:
        assert ServingIndex(pin, k2_env.config).exact('author') == [NAME]


def test_every_concept_failing_reaches_the_command_with_exit_0_then_reports_the_skipped_publish(
        k2_env, monkeypatch, capsys):
    from prompt_enhancer import __main__ as cli
    server = setup(k2_env)
    server.fail_concept = None
    monkeypatch.setattr(cli, 'load_config', lambda *a, **k: k2_env.config)
    monkeypatch.setattr(cli, 'sync_config', lambda *a, **k: invoke(k2_env, server))
    printed = []
    for _ in range(2):
        assert cli.main(['kb', 'sync', '--config', 'labelled-double.yaml']) == 0
        captured = capsys.readouterr()
        assert MARKER not in captured.out + captured.err
        printed.append(json.loads(captured.out))
    assert [item['status'] for item in printed] == ['published', 'unchanged']
    assert printed[1]['publish_skipped'] == 'identical' and printed[1]['alias_failures']['count'] == 2
    assert printed[1]['snapshot_id'] == printed[0]['snapshot_id']


def test_a_stale_generated_alias_of_the_previous_snapshot_does_not_leak(k2_env):
    server = setup(k2_env)
    server.fail_role = None
    old = invoke(k2_env, server)
    assert ('generated', 'Name alias') in rows(k2_env, old['snapshot_id'])[0][NAME]
    (k2_env.source / 'f/info.txt').write_text('Name\nchanged description', encoding='utf-8')  # new K4 key
    server.fail_role = 'alias_generator'
    new = invoke(k2_env, server)
    assert new['status'] == 'published' and new['alias_failures']['concept_ids'] == [NAME]
    assert rows(k2_env, new['snapshot_id'])[0][NAME] == [('author', 'author')]


def test_a_k3_failure_still_blocks_the_publish(k2_env):
    server = setup(k2_env)
    server.fail_role, server.fail_concept = 'card_builder', None
    with pytest.raises(StorageError, match='^k3: STAGE_FAILED'):
        invoke(k2_env, server)
    assert k2_env.store.current() is None


def test_the_k4_failure_reaches_the_command_with_exit_0_and_no_model_text(k2_env, monkeypatch, capsys):
    from prompt_enhancer import __main__ as cli
    server = setup(k2_env)
    monkeypatch.setattr(cli, 'load_config', lambda *a, **k: k2_env.config)
    monkeypatch.setattr(cli, 'sync_config', lambda *a, **k: invoke(k2_env, server))
    assert cli.main(['kb', 'sync', '--config', 'labelled-double.yaml']) == 0
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    assert printed['status'] == 'published' and printed['alias_failures']['concept_ids'] == [NAME]
    assert MARKER not in captured.out + captured.err


def test_a_failed_alias_leaves_the_status_summary_printable(k2_env):
    result = invoke(k2_env, setup(k2_env))
    row = next(item for item in k2_env.store.snapshots() if item['snapshot_id'] == result['snapshot_id'])
    assert json.loads(json.dumps(row['source_scan_summary']))['k4_failed']['count'] == 1


@pytest.mark.parametrize('code', ['BUDGET_EXHAUSTED', 'CONFIGURATION_INVALID', 'CAPABILITY_UNAVAILABLE',
                                  'CLIENT_STATE_INVALID', 'PROBE_CASES_MISSING', 'PROBE_ARTIFACT_INVALID'])
def test_a_run_level_alias_code_still_blocks_the_publish(code):
    # Review P2-1: a code that is the same for every concept (setup, gate, spend) is not "a concept whose alias
    # generation fails" (decision 165), so it fails K4 as before; a per-concept code alone publishes.
    with pytest.raises(StorageError, match='^k4: STAGE_FAILED'):
        require_per_concept({'report': {'failed': {NAME: 'SCHEMA_INVALID', OTHER: code}}})
    require_per_concept({'report': {'failed': {NAME: 'SCHEMA_INVALID', OTHER: 'INCOMPLETE_OR_REFUSED'}}})
    require_per_concept({'report': {'failed': {}}})


@pytest.mark.parametrize('code', ['NOT_A_KNOWN_CODE', 'schema_invalid', '', None, 7, ('SCHEMA_INVALID',)])
def test_an_unrecognised_alias_code_blocks_the_publish(code):
    # Decision 171 review (Gemini) of the P2-1 fix: `_closed_code` maps an unknown or non-string code to
    # MODEL_CALL_FAILED, which is tolerated; checking that mapped value would publish on an unrecognised failure.
    with pytest.raises(StorageError, match='^k4: STAGE_FAILED'):
        require_per_concept({'report': {'failed': {NAME: 'SCHEMA_INVALID', OTHER: code}}})


@settings(max_examples=60, deadline=None)
@given(failed=st.dictionaries(
    st.one_of(st.text(min_size=1).map(concept_id), st.text(min_size=1)),
    st.one_of(st.sampled_from(sorted(_CLOSED_CODES)), st.text(min_size=1).map(lambda text: MARKER + text)), max_size=8))
def test_the_recorded_failure_holds_only_concept_ids_and_closed_codes(failed):
    record = alias_failures(failed)
    assert record['count'] == len(failed)
    assert set(record['concept_ids']) <= set(failed) and all(is_concept_id(key) for key in record['concept_ids'])
    assert set(record['codes']) <= _CLOSED_CODES | {'MODEL_CALL_FAILED'} and sum(record['codes'].values()) == len(failed)
    assert MARKER not in json.dumps(record)
    assert record['concept_ids'] == sorted(record['concept_ids'])


def test_a_skipped_identical_publish_copies_no_retained_image_and_the_real_publish_does(k2_env, monkeypatch):
    """P3-SWEEP-2 item 3: the copy (read, hash, write of every image) runs after the identity check, so a sync that
    repeats the current snapshot reads no source image for it. A real publish still retains every image it lists."""
    import prompt_enhancer.kb_sync as module
    copies, reads = [], []
    original_retain, original_read = module.retain_images, module.read_source_file
    monkeypatch.setattr(module, 'retain_images', lambda *args, **kwargs: copies.append(1) or original_retain(*args, **kwargs))
    monkeypatch.setattr(module, 'read_source_file', lambda *args, **kwargs: reads.append(1) or original_read(*args, **kwargs))
    server = setup(k2_env)
    first = invoke(k2_env, server)
    assert first['status'] == 'published' and copies == [1] and reads
    with k2_env.store.pin() as pin:
        listed = [row['image_id'] for row in pin.read_rows('images')]
        assert listed and all(pin.read_image(image_id) for image_id in listed)  # every listed image has its copy
    copies.clear()
    reads.clear()
    second = invoke(k2_env, server)
    assert second['status'] == 'unchanged' and second['publish_skipped'] == 'identical'
    assert copies == [] and reads == []
    server.fail_role = None
    third = invoke(k2_env, server)
    assert third['status'] == 'published' and copies == [1] and reads


def test_a_scan_that_breaks_the_retention_limits_still_fails_before_any_model_call(k2_env):
    """The cheap identity, size and limit check stays ahead of the model stages; only the copy moved."""
    server = setup(k2_env)
    k2_env.config['sync']['retained_images'] = dict(k2_env.config['sync']['retained_images'], max_image_bytes=1)
    with pytest.raises(StorageError, match='retained images: byte limit exceeded'):
        invoke(k2_env, server)
    assert server.roles == [] and server.embedder.requests == []
    assert k2_env.store.current() is None


@pytest.mark.parametrize('site', ['_record_revisions', '_generation_bundle'])
def test_a_k4_defect_fails_the_sync_instead_of_publishing_without_aliases(k2_env, monkeypatch, site):
    """P3-SWEEP-2 item 1: an internal K4 error used to become SCHEMA_INVALID, a tolerated code, so the sync published
    a snapshot without any generated alias. It now fails the stage and nothing is published."""
    import prompt_enhancer.k4_aliases as module

    def defect(*args, **kwargs):
        raise ValueError('labelled internal defect')
    monkeypatch.setattr(module, site, defect)
    with pytest.raises(ValueError, match='labelled internal defect'):
        invoke(k2_env, setup(k2_env))
    assert k2_env.store.current() is None
