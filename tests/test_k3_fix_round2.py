"""P1/P2/K3 completion preservation with labelled transports, failures and synthetic stores."""

from copy import deepcopy
import hashlib
from random import Random

import pytest

from k2_fixtures import NamingServer, folder, invoke, naming, scan
from k2_observe_fixtures import ObserverServer, invoke_observer, k1_result
from k3_fixtures import CardServer, inputs, invoke_cards, reply_for
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.storage import SPECS
from prompt_enhancer.storage.store import Checkpoints
from storage_fixtures import dataset, DIM


def state(env, stage):
    with env.store.writer() as writer:
        return {key: deepcopy(row) for (current_stage, key), row in writer._state_rows().items()
                if current_stage == stage}


def reference(env, artifacts):
    with env.store.writer() as writer:
        builder = writer.begin_snapshot()
        for table, rows in dataset('preservation').items():
            builder.write_table(table, rows, dim=DIM if SPECS[table].vector else None)
        builder.reference_artifacts(artifacts)
        writer.publish(builder.finish())


def resume(env, enabled):
    env.store._resume = env.config['sync']['resume'] = enabled


@pytest.mark.parametrize('initial', [None, 'queued', 'failed'])
def test_k3_invalid_reply_records_exact_failed_grouping_key_and_error(k2_env, monkeypatch, initial):
    data = inputs(k2_env)
    keys = []
    original = Checkpoints.fail

    def record_failure(self, key, error, **kwargs):
        if self.stage == 'k3_grouping':
            keys.append(key)
        return original(self, key, error, **kwargs)

    monkeypatch.setattr(Checkpoints, 'fail', record_failure)  # Labelled transparent failure-call observer.
    server = CardServer(lambda payload, body: dict(reply_for(payload), facets=[]))
    if initial is not None:
        # Obtain the real semantic key, then seed a never-done row before the observed failing attempt.
        invoke_cards(k2_env, data, server)
        assert len(keys) == 1
        with k2_env.store.writer() as writer:
            helper = writer.checkpoints('k3_grouping')
            if initial == 'queued':
                helper.queue(keys[0])
            else:
                helper.fail(keys[0], 'labelled earlier failure')
        keys.clear()
    failed = invoke_cards(k2_env, data, server)
    assert len(keys) == 1
    rows = state(k2_env, 'k3_grouping')
    assert rows.keys() == {keys[0]}
    row = rows[keys[0]]
    identity = data['handoff']['concepts'][0]['concept_id']
    assert row['state'] == 'failed'
    assert row['error'] == failed['report']['failed'][identity] == 'SCHEMA_INVALID'
    assert row['output_key'] is None and not failed['rows']['cards']


def test_separator_refusal_keeps_raw_reply_and_revert_reuses_without_call(k2_env):
    folder(k2_env, 'f')
    raw = scan(k2_env)
    server = NamingServer({('folder_namer', 'f'): naming(['A+B'])})
    invoke(k2_env, server, result=raw, rng=Random(1))
    before = state(k2_env, 'k2_naming')
    assert len(before) == 1
    # EKS-5: the separator is request data and part of the naming key, so a new separator asks the namer once.
    k2_env.config['knowledge']['concept_separator'] = '+'
    refused = invoke(k2_env, server, result=raw, rng=Random(1))
    assert refused['report']['failed']['f'] == 'SCHEMA_INVALID'
    assert len(server.requests) == 2
    after = state(k2_env, 'k2_naming')
    assert {key: after[key] for key in before} == before
    k2_env.config['knowledge']['concept_separator'] = '|'
    assert not invoke(k2_env, server, result=raw, rng=Random(1))['report']['failed']
    assert len(server.requests) == 2


@pytest.mark.parametrize('invalid', [None, '', 'design_required', 7])
def test_invalid_separator_configuration_refuses_before_reuse_or_call(k2_env, invalid):
    folder(k2_env, 'f')
    raw, server = scan(k2_env), NamingServer()
    invoke(k2_env, server, result=raw, rng=Random(1))
    before = state(k2_env, 'k2_naming')
    if invalid is None:
        del k2_env.config['knowledge']['concept_separator']
    else:
        k2_env.config['knowledge']['concept_separator'] = invalid
    with pytest.raises(ConfigurationError, match='concept_separator'):
        invoke(k2_env, server, result=raw, rng=Random(1))
    assert state(k2_env, 'k2_naming') == before and len(server.requests) == 1


@pytest.mark.parametrize('stage', ['k2_assessment', 'k2_naming'])
@pytest.mark.parametrize('site', ['commit_then_raise', 'fresh_application', 'reuse_application', 'failed_recompute'])
def test_p1_reported_failure_cannot_erase_completed_reply(k2_env, monkeypatch, stage, site):
    import prompt_enhancer.k2_naming as module

    folder(k2_env, 'f', 'Declared\nHuman' if stage == 'k2_assessment' else None)
    raw = scan(k2_env)
    server = NamingServer()
    if site in ('reuse_application', 'failed_recompute'):
        invoke(k2_env, server, result=raw, rng=Random(1))
    before = state(k2_env, stage)
    if site == 'failed_recompute':
        resume(k2_env, False)
        server.responses[('info_assessor' if stage == 'k2_assessment' else 'folder_namer', 'f')] = {}

    commit = Checkpoints.commit
    parse = module.parse_assessment
    parses = []

    def interrupted(self, *args, **kwargs):
        result = commit(self, *args, **kwargs)
        if self.stage == stage:
            raise ValueError('labelled post-commit failure')
        return result

    def bad_application(*args, **kwargs):
        if stage == 'k2_assessment':
            parses.append(1)
            if len(parses) == 1:
                return parse(*args, **kwargs)
        raise ValueError('labelled application failure')

    with monkeypatch.context() as patch:
        if site == 'commit_then_raise':
            patch.setattr(Checkpoints, 'commit', interrupted)
        elif site in ('fresh_application', 'reuse_application'):
            patch.setattr(module, 'parse_assessment' if stage == 'k2_assessment' else '_replace_members', bad_application)
        failed = invoke(k2_env, server, result=raw, rng=Random(1))
    assert failed['report']['failed'].keys() == {'f'}
    after = state(k2_env, stage)
    assert len(after) == 1 and all(row['state'] == 'done' for row in after.values()), after
    if before:
        assert after == before
    resume(k2_env, True)
    server.responses.clear()
    count = len(server.requests)
    assert not invoke(k2_env, server, result=raw, rng=Random(1))['report']['failed']
    assert len(server.requests) == count


@pytest.mark.parametrize('failure', ['reply', 'source'])
def test_p2_failed_recompute_keeps_prior_done_and_reports_error(k2_env, failure):
    folder(k2_env, 'f', 'A')
    current = k1_result(k2_env)
    server = ObserverServer()
    first = invoke_observer(k2_env, server, inputs=current)
    before = state(k2_env, 'k2_observation')
    original = (k2_env.source / 'f/0.png').read_bytes()
    resume(k2_env, False)
    if failure == 'reply':
        server.reply = {}
    else:
        (k2_env.source / 'f/0.png').write_bytes(b'changed after scan')
    failed = invoke_observer(k2_env, server, inputs=current)
    assert failed['report']['failed'] and not failed['rows']['observations']
    assert state(k2_env, 'k2_observation') == before
    (k2_env.source / 'f/0.png').write_bytes(original)
    server.reply = None
    resume(k2_env, True)
    count = len(server.requests)
    assert invoke_observer(k2_env, server, inputs=current)['rows'] == first['rows']
    assert len(server.requests) == count


def test_p2_deferring_completed_image_does_not_queue_over_done(k2_env):
    folder(k2_env, 'f', 'A', count=2)
    current = k1_result(k2_env)
    k2_env.config['observe']['representatives_per_folder'] = 2
    server = ObserverServer()
    invoke_observer(k2_env, server, inputs=current)
    before = state(k2_env, 'k2_observation')
    resume(k2_env, False)
    k2_env.config['observe']['representatives_per_folder'] = 1
    server.reply = {}  # Labelled failed selected recompute; the other key is deferred.
    failed = invoke_observer(k2_env, server, inputs=current)
    assert len(failed['report']['queued']) == len(failed['report']['failed']) == 1
    assert state(k2_env, 'k2_observation') == before
    resume(k2_env, True)
    server.reply = None
    count = len(server.requests)
    replay = invoke_observer(k2_env, server, inputs=current)
    assert replay['report']['skipped_done'] == 2 and len(server.requests) == count


@pytest.mark.parametrize('site', ['invalid_reply', 'assemble', 'commit_then_raise'])
@pytest.mark.parametrize('referenced', [False, True])
def test_k3_resume_off_failure_preserves_exact_done_reply(k2_env, monkeypatch, site, referenced):
    import prompt_enhancer.k3_cards as module

    data, server = inputs(k2_env), CardServer()
    built = invoke_cards(k2_env, data, server)
    before = state(k2_env, 'k3_grouping')
    artifact = next(path for path in built['artifacts'] if '/k3_grouping/' in path)
    digest = hashlib.sha256(k2_env.store.path(artifact).read_bytes()).hexdigest()
    if referenced:
        reference(k2_env, [artifact])
    resume(k2_env, False)
    original = Checkpoints.commit

    def interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.stage == 'k3_grouping':
            raise ValueError('labelled post-commit recompute failure')
        return result

    with monkeypatch.context() as patch:
        if site == 'invalid_reply' and not referenced:
            server.reply = lambda payload, body: dict(reply_for(payload), facets=[])
        elif site == 'commit_then_raise' and not referenced:
            patch.setattr(Checkpoints, 'commit', interrupted)
        else:
            patch.setattr(module, 'assemble_card', lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError('labelled failed derivation')))
        failed = invoke_cards(k2_env, data, server)
    assert failed['report']['failed'] and not failed['rows']['cards']
    after = state(k2_env, 'k3_grouping')
    assert after.keys() == before.keys() and all(row['state'] == 'done' for row in after.values()), after
    if referenced or site == 'invalid_reply':
        assert after == before
    if referenced or site == 'invalid_reply':
        assert hashlib.sha256(k2_env.store.path(artifact).read_bytes()).hexdigest() == digest
    resume(k2_env, True)
    server.reply = None
    count = len(server.requests)
    assert invoke_cards(k2_env, data, server)['rows']['cards'] and len(server.requests) == count
