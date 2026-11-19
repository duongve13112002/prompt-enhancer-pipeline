"""The documented commands through the real entry point (T-016 P1, R-015, R-017, R-024, R-025).

Labelled doubles: the loopback model server (observer/embedder/planner/drafter replies) and the synthetic profile bundle.
The parser, dispatcher, configuration loader, knowledge store, profile logic and pipeline are production code. Exit
statuses: 0 done, 1 operational failure, 2 refusal made before any side effect (kb sync and kb audit keep exit 1)."""
import json
from pathlib import Path

import pytest

from cli_fixtures import (DraftingServer, TwoViewSync, finish_setup, model_server, pipeline_config,
                          published_knowledge, run_cli, text, write_yaml)
from k2_fixtures import folder
from kb_sync_fixtures import invoke
from profile_fixtures import SECTIONED, bundle, install_prompts, set_manifest, write_golden
from prompt_enhancer.profiles import confirm_profile, resolve_profile
from prompt_enhancer.storage import normalise_name


def kb(project_root, tmp_path, config, *args):
    path = write_yaml(project_root, tmp_path, config)
    return run_cli(['kb', args[0], '--config', path, '--project-root', project_root, *args[1:]], project_root)


def listing(root):
    return sorted(str(path.relative_to(root)) for path in Path(root).rglob('*')) if Path(root).exists() else None


def test_kb_status_reports_stored_facts_and_does_not_check_the_live_source(k2_env, tmp_path, project_root):
    before = kb(project_root, tmp_path, k2_env.config, 'status')
    out, err = text(before)
    assert before.returncode == 0, err
    empty = json.loads(out)
    assert empty['current'] is None and empty['tables'] is None and empty['live_source_changes'] == 'not_checked'
    published_knowledge(k2_env)
    done = kb(project_root, tmp_path, k2_env.config, 'status')
    out, err = text(done)
    assert done.returncode == 0, err
    status = json.loads(out)
    (row,) = [item for item in status['snapshots'] if item['state'] == 'published']
    assert status['current']['snapshot_id'] == row['snapshot_id'] and status['store_present'] is True
    assert status['tables'] == {'folders': 1, 'concepts': 1, 'images': 2, 'cards': 1, 'aliases': 1}
    assert status['folders_with_error'] == 0 and status['live_source_changes'] == 'not_checked'
    assert out.count('\n') == 1  # one JSON line, nothing else on stdout


def test_kb_show_finds_a_concept_by_name_or_id_and_refuses_an_unknown_one(k2_env, tmp_path, project_root):
    published_knowledge(k2_env)
    by_name = json.loads(text(kb(project_root, tmp_path, k2_env.config, 'show', 'Name'))[0])
    identity = by_name['concept']['concept_id']
    assert by_name['concept']['normalised_name'] == normalise_name('Name') and by_name['card'] is not None
    assert [facet['facet'] for facet in by_name['card']['facets']] == ['apparel.color']
    assert by_name['aliases'] and len(by_name['folders']) == 1
    by_id = json.loads(text(kb(project_root, tmp_path, k2_env.config, 'show', identity))[0])
    assert by_id == by_name
    done = kb(project_root, tmp_path, k2_env.config, 'show', 'No such concept')
    out, err = text(done)
    assert done.returncode == 2 and out == '' and err.startswith('refused: kb show: unknown concept')


def test_kb_show_refuses_when_nothing_is_published(k2_env, tmp_path, project_root):
    done = kb(project_root, tmp_path, k2_env.config, 'show', 'Name')
    assert done.returncode == 2 and text(done)[0] == '' and 'no published snapshot' in text(done)[1]


def test_kb_rollback_moves_the_pointer_only_to_a_retired_snapshot_and_gc_runs(k2_env, tmp_path, project_root):
    published_knowledge(k2_env)
    folder(k2_env, 'f', 'Name\ndescription', count=3)  # a third image: the second sync publishes a new snapshot
    invoke(k2_env, TwoViewSync())
    rows = {row['snapshot_id']: row['state'] for row in k2_env.store.snapshots()}
    (current,) = [key for key, state in rows.items() if state == 'published']
    (retired,) = [key for key, state in rows.items() if state == 'retired']
    store_before = listing(k2_env.store.root)
    for bad, why in ((current, 'published'), ('s20200101T000000000000Z-00000000', 'unknown'), ('not-an-id', 'not a snapshot id')):
        done = kb(project_root, tmp_path, k2_env.config, 'rollback', bad)
        out, err = text(done)
        assert done.returncode == 2 and out == '' and err.startswith('refused: kb rollback:') and why in err, (bad, err)
    assert listing(k2_env.store.root) == store_before  # a refusal changes nothing
    done = kb(project_root, tmp_path, k2_env.config, 'rollback', retired)
    assert done.returncode == 0, text(done)[1]
    assert json.loads(text(done)[0]) == {'command': 'kb rollback', 'current': retired}
    assert {row['snapshot_id']: row['state'] for row in k2_env.store.snapshots()}[retired] == 'published'
    done = kb(project_root, tmp_path, k2_env.config, 'gc')
    out, err = text(done)
    assert done.returncode == 0, err
    assert json.loads(out)['command'] == 'kb gc'


@pytest.mark.parametrize('command', [('rollback', 's20200101T000000000000Z-00000000'), ('gc',)])
def test_kb_writer_commands_refuse_a_missing_store_without_creating_it(k2_env, tmp_path, project_root, command):
    config = json.loads(json.dumps(k2_env.config))
    config['storage']['kb_store'] = (tmp_path / 'absent-store').relative_to(project_root).as_posix()
    done = kb(project_root, tmp_path, config, *command)
    out, err = text(done)
    assert done.returncode == 2 and out == '' and 'no knowledge store' in err
    assert not (tmp_path / 'absent-store').exists()


@pytest.mark.parametrize('argv, owner', [
    (['eval', 'build'], 'T-003'),
    (['eval', 'fit'], 'T-030'),
    (['kb', 'sync', '--only', 'folder'], 'kb sync --only is not available'),
])
def test_commands_this_build_does_not_serve_are_refused_precisely_before_any_side_effect(
        tmp_path, project_root, argv, owner):
    before = listing(tmp_path)
    done = run_cli([*argv[:2], '--config', tmp_path / 'absent.yaml', '--project-root', project_root, *argv[2:]],
                   project_root)
    out, err = text(done)
    assert done.returncode == 2 and out == '' and err.startswith('refused: ') and owner in err
    assert listing(tmp_path) == before


def test_new_commands_refuse_where_kb_sync_and_kb_audit_keep_their_exit_one(k2_env, tmp_path, project_root):
    config = json.loads(json.dumps(k2_env.config))
    config['storage']['kb_store'] = 'deploy_required'
    path = write_yaml(project_root, tmp_path, config)
    audit = ['--folder-count', '1', '--card-count', '1', '--seed', '1']
    legacy = {'sync': run_cli(['kb', 'sync', '--config', path, '--project-root', project_root], project_root),
              'audit': run_cli(['kb', 'audit', '--config', path, '--project-root', project_root, *audit], project_root)}
    new = run_cli(['kb', 'status', '--config', path, '--project-root', project_root], project_root)
    assert [done.returncode for done in legacy.values()] == [1, 1]  # unchanged since before T-016
    assert new.returncode == 2 and text(new)[0] == '' and 'storage.kb_store' in text(new)[1]


def prepared_profile(model_test_profile, tmp_path, project_root, port):
    config = pipeline_config(model_test_profile, tmp_path, project_root, port)
    install_prompts(project_root)
    return config, bundle(tmp_path, 'p1', SECTIONED)


def run_profile(project_root, tmp_path, config, action):
    path = write_yaml(project_root, tmp_path, config)
    return run_cli(['profile', action, '--config', path, '--profile-id', 'p1', '--project-root', project_root],
                   project_root)


def test_profile_register_drafts_once_and_check_activates_through_the_pipeline(model_test_profile, tmp_path, project_root):
    with model_server(DraftingServer) as (handler, port):
        config, folder_path = prepared_profile(model_test_profile, tmp_path, project_root, port)
        done = run_profile(project_root, tmp_path, config, 'register')
        out, err = text(done)
        assert done.returncode == 0, err
        registered = json.loads(out)
        assert registered['command'] == 'profile register' and registered['status'] == 'draft'
        assert (folder_path / 'manifest.yaml').is_file()
        again = run_profile(project_root, tmp_path, config, 'register')  # the draft exists: the admin removes it to redraft
        assert again.returncode == 2 and text(again)[0] == '' and 'manifest.yaml' in text(again)[1]
        calls = len(handler.seen)
        confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
        write_golden(folder_path, 'g1', ['c1'], expect=('success', 'degraded'))  # the double writer is not the profile's
        set_manifest(folder_path, golden_tests=['g1'])
        handler.writer_system = handler.verifier_system = None  # no scripted writer/verifier: replies fall to the probe
        done = run_profile(project_root, tmp_path, config, 'check')
        out, err = text(done)
        assert done.returncode == 0, err
        checked = json.loads(out)
        assert checked['command'] == 'profile check' and checked['activated'] is True and out.count('\n') == 1
        assert len(handler.seen) > calls  # the golden case ran through the model client
        assert resolve_profile(config, project_root=project_root).profile_id == 'p1'  # and the profile is active


def test_profile_check_failure_is_exit_one_and_activates_nothing(model_test_profile, tmp_path, project_root):
    with model_server(DraftingServer) as (handler, port):
        config, folder_path = prepared_profile(model_test_profile, tmp_path, project_root, port)
        assert run_profile(project_root, tmp_path, config, 'register').returncode == 0
        confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
        write_golden(folder_path, 'g1', ['c1'], expect=('failed',))  # the pipeline cannot deliver `failed` here
        set_manifest(folder_path, golden_tests=['g1'])
        handler.writer_system = handler.verifier_system = None
        done = run_profile(project_root, tmp_path, config, 'check')
        out, err = text(done)
    assert done.returncode == 1 and out == '' and err.startswith('failed: ')
    with pytest.raises(Exception):
        resolve_profile(config, project_root=project_root)  # not activated
