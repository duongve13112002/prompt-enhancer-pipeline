"""Decision 165: a failed K4 alias key publishes without that concept's generated aliases and is retried next sync.

Real stages over labelled HTTP doubles (`SyncServer`); no network, no inference."""

import json

import pytest
from hypothesis import given, settings, strategies as st

from k2_fixtures import folder
from kb_sync_fixtures import SyncServer, invoke
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
    assert summary(k2_env, result['snapshot_id'])['k4_failed'] == {'count': 1, 'codes': {'SCHEMA_INVALID': 1}}
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


def test_a_key_that_keeps_failing_is_recorded_again_and_called_once_per_run(k2_env):
    server = setup(k2_env)
    first = invoke(k2_env, server)
    server.roles.clear()
    second = invoke(k2_env, server)
    assert second['status'] == 'published' and second['snapshot_id'] != first['snapshot_id']
    assert server.roles == ['alias_generator']  # no loop inside one run, and the done key is not repeated
    assert second['alias_failures']['concept_ids'] == [NAME]
    assert summary(k2_env, second['snapshot_id'])['k4_failed']['count'] == 1


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

