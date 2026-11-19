"""K2 integration with labelled transport doubles; no model/network/GPU."""

import json
from random import Random

import pytest

from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.storage import concept_id, image_id
from prompt_enhancer.storage.store import Checkpoints
from k2_fixtures import FixedDraw, NamingServer, assessment, folder, invoke, naming, scan, source_tree


def test_separator_override_changes_namer_request_and_checkpoint(k2_env):
    env = k2_env
    folder(env, 'f')
    server = NamingServer()
    assert not invoke(env, server)['report']['failed']
    with env.store.writer() as writer:
        first = {key for key, row in writer._state_rows().items() if row['stage'] == 'k2_naming'}
    env.config['knowledge']['concept_separator'] = '::'
    assert not invoke(env, server)['report']['failed']
    with env.store.writer() as writer:
        second = {key for key, row in writer._state_rows().items() if row['stage'] == 'k2_naming'}
    assert json.loads(server.requests[-1]['messages'][1]['content'][0]['text'])['concept_separator'] == '::'
    assert second - first


def test_six_folder_kinds_end_to_end_with_transport_double(k2_env):
    env = k2_env
    folder(env, 'no-info', count=3)
    folder(env, 'unclear', 'Old\nCurated original', count=2)
    folder(env, 'usable', 'Usable\nHuman description')
    folder(env, 'mismatch', 'One|Two\nOne paragraph for both')
    folder(env, 'empty', count=0)
    folder(env, 'override', 'Override\nHuman statement')
    (env.source / 'kb_overrides.yaml').write_text('Override:\n  concept_type: clothing\n  aliases: [Alias]\n'
        '  facts: [Curated fact]\n  suppress: [apparel.test]\nUndeclared:\n  concept_type: object\n', encoding='utf-8')
    server = NamingServer({('info_assessor', 'unclear'): assessment(False),
                           ('folder_namer', 'unclear'): naming(['New'], info_fits=False, consistent=False),
                           ('folder_namer', 'no-info'): naming(['no-info'], folder_fits=True),
                           ('info_assessor', 'mismatch'): assessment(assignments=[
                               {'position': 1, 'description': 'Assigned two'},
                               {'position': 0, 'description': 'Assigned one'}])})
    before = source_tree(env.source)
    result = invoke(env, server)
    assert source_tree(env.source) == before
    assert len(server.requests) == 6  # four assessments and two naming requests
    image_requests = [request for request in server.requests if isinstance(request['messages'][1]['content'], list)]
    assert all(json.loads(request['messages'][1]['content'][0]['text'])['concept_separator'] ==
               env.config['knowledge']['concept_separator'] for request in image_requests)
    folders = {row['folder_id']: row for row in result['rows']['folders']}
    assert folders['no-info']['name_source'] == 'folder_name'
    assert folders['unclear']['name_source'] == 'model' and folders['unclear']['declared_names'] == ['Old']
    assert folders['mismatch']['declared_descriptions'] == 'One paragraph for both'
    members = {row['concept_id']: row for row in result['rows']['folder_concepts']}
    assert members[concept_id('One')]['description'] == 'Assigned one'
    assert members[concept_id('One')]['description_origin'] == 'generated'
    assert members[concept_id('New')]['description_revision'] == 'test'
    assert members[concept_id('Usable')]['description'] == 'Human description'
    concepts = {row['concept_id']: row for row in result['rows']['concepts']}
    assert concepts[concept_id('Override')]['kind_origin'] == 'override'
    assert concepts[concept_id('Override')]['kind'] == 'clothing'
    assert concept_id('Undeclared') not in concepts
    assert result['report']['unmatched_overrides'] == ['Undeclared']
    assert result['report']['skipped_folders'] == ['empty']
    assert result['report']['description_assignments'] == ['mismatch']
    assert result['report']['inconsistent_folders'] == ['unclear']
    assert not result['report']['failed'] and result['report']['override_renaming_supported']
    assert set(result['rows']) == {'folders', 'concepts', 'folder_concepts'}
    assert not (env.store.root / 'CURRENT').exists()
    with env.store.writer() as writer:
        assert all(row['stage'].startswith('k0_') or row['stage'].startswith('k2_')
                   for row in writer._state_rows().values())


class Interrupted(BaseException):
    """Labelled simulated process interruption, deliberately not an ordinary per-folder failure."""


@pytest.mark.parametrize('boundary', ['k2_draw', 'k2_draw_head', 'k2_assessment', 'k2_naming'])
def test_resume_reuses_committed_draw_and_completed_calls(k2_env, monkeypatch, boundary):
    env = k2_env
    folder(env, 'folder', 'Old\nOriginal', count=4)
    server = NamingServer({('info_assessor', 'folder'): assessment(False),
                           ('folder_namer', 'folder'): naming(['New'], info_fits=False)})
    scanned = scan(env)
    commit = Checkpoints.commit
    saved = []

    def interrupt(self, *args, **kwargs):
        result = commit(self, *args, **kwargs)
        if self.stage == 'k2_draw':
            saved[:] = json.loads(self.load(args[0]))['sampled_image_ids']
        if self.stage == boundary:
            raise Interrupted()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', interrupt)
        with pytest.raises(Interrupted):
            invoke(env, server, result=scanned, rng=Random(1))
    calls_before = len(server.requests)
    resumed = invoke(env, server, result=scanned, rng=Random(999))
    assert not resumed['report']['failed']
    assert len(server.requests) == calls_before + (0 if boundary == 'k2_naming' else 1)
    sampled = resumed['report']['model_named_folders'][0]['sampled_image_ids']
    if saved:
        assert sampled == saved
    assert len(sampled) == 2
    calls_before = len(server.requests)
    assert invoke(env, server, result=scanned)['rows'] == resumed['rows']
    assert len(server.requests) == calls_before


def test_duplicate_names_remain_refused_without_calls(k2_env):
    folder(k2_env, 'duplicate', 'Same|same\nFirst|Second')
    server = NamingServer()
    result = invoke(k2_env, server)
    assert result['report']['refused_folders'] == ['duplicate']
    assert not server.requests and result['rows']['folders'][0]['error'] == 'duplicate_names'


def test_assessment_disabled_mismatch_refuses_all_calls(k2_env):
    folder(k2_env, 'a-first', count=1)
    folder(k2_env, 'mismatch', 'One|Two\nSingle paragraph')
    k2_env.config['knowledge']['naming']['assess_info_file'] = False
    server = NamingServer()
    with pytest.raises(ConfigurationError, match='assess_info_file.*mismatch'):
        invoke(k2_env, server)
    assert not server.requests


def test_no_image_usable_file_is_assessed_and_kept(k2_env):
    folder(k2_env, 'declared', 'Declared\n', count=0)
    server = NamingServer()
    result = invoke(k2_env, server)
    assert len(server.requests) == 1 and result['rows']['folders'][0]['name_source'] == 'info_file'
    assert result['rows']['concepts'][0]['concept_id'] == concept_id('Declared')


def test_unclear_info_names_that_fit_keep_human_descriptions(k2_env):
    folder(k2_env, 'f', 'Keep\nHuman text')
    server = NamingServer({('info_assessor', 'f'): assessment(False),
                           ('folder_namer', 'f'): naming(['Keep'], info_fits=True)})
    result = invoke(k2_env, server)
    assert result['rows']['folders'][0]['name_source'] == 'info_file'
    member = result['rows']['folder_concepts'][0]
    assert member['description'] == 'Human text' and member['description_origin'] == 'info_file'


def test_override_hash_changes_without_repeating_naming_calls(k2_env):
    folder(k2_env, 'f')
    overrides = k2_env.source / 'kb_overrides.yaml'
    overrides.write_text('Generated: {concept_type: object}', encoding='utf-8')
    server = NamingServer()
    first = invoke(k2_env, server)
    overrides.write_text('Generated: {concept_type: style}', encoding='utf-8')
    second = invoke(k2_env, server)
    assert len(server.requests) == 1
    assert first['overrides']['file_hash'] != second['overrides']['file_hash']
    assert second['rows']['concepts'][0]['kind'] == 'style'


@pytest.mark.parametrize('reply', [naming(['Same', 'same']), dict(naming(), folder_id='forged'),
                                  dict(naming(), sampled_image_ids=['forged']), dict(naming(), concepts=[]),
                                  naming(['A', 'B'], consistent=False)])
def test_invalid_naming_reply_commits_no_name_result(k2_env, reply):
    folder(k2_env, 'f')
    result = invoke(k2_env, NamingServer({('folder_namer', 'f'): reply}))
    assert 'f' in result['report']['failed'] and not result['rows']['concepts']
    with k2_env.store.writer() as writer:
        assert not [row for row in writer._state_rows().values()
                    if row['stage'] == 'k2_naming' and row['state'] == 'done']


def test_missing_sample_member_redraws_whole_sample(k2_env):
    env = k2_env
    folder(env, 'f', count=4)
    server = NamingServer()
    first_scan = scan(env)
    first = invoke(env, server, result=first_scan, rng=Random(1))
    selected = first['report']['model_named_folders'][0]['sampled_image_ids']
    removed = next(row for row in first_scan['rows']['folder_images'] if row['image_id'] == selected[0])
    (env.source / removed['folder_id'] / removed['relative_path']).unlink()
    second = invoke(env, server, rng=Random(9))
    draw = second['report']['model_named_folders'][0]['sampled_image_ids']
    assert selected[0] not in draw and len(draw) == 2
    assert second['report']['redraws'][0]['reason'] == 'sample_member_missing'
    assert len(server.requests) == 2


def test_rename_share_below_equal_and_above_threshold(k2_env):
    env = k2_env
    path = folder(env, 'f', count=2)
    server = NamingServer()
    first = invoke(env, server, rng=Random(1))
    original = first['report']['model_named_folders'][0]['sampled_image_ids']
    from k2_fixtures import image_bytes
    payload = image_bytes((200, 100, 20))
    (path / 'new.png').write_bytes(payload)
    unchanged = invoke(env, server, rng=Random(9))  # one added / two baseline = threshold; do not rename
    assert unchanged['report']['model_named_folders'][0]['sampled_image_ids'] == original
    assert len(server.requests) == 1
    env.config['knowledge']['naming']['rename_on_change'] = 0.25
    selected = [image_id(payload), original[0]]
    result = invoke(env, server, rng=FixedDraw(selected))
    assert result['report']['model_named_folders'][0]['sampled_image_ids'] == selected != original
    assert server.requests[-1]['messages'][1]['content'] != server.requests[0]['messages'][1]['content']
    assert len(server.requests) == 2 and result['report']['redraws'][0]['reason'] == 'image_set_change'


@pytest.mark.parametrize('capabilities', [{'image': True, 'json_schema': True},
                                         {'image': True, 'json_schema': True, 'multiple_images': False}])
def test_namer_refuses_unproved_multiple_images(k2_env, capabilities):
    folder(k2_env, 'f', count=2)
    server = NamingServer(capabilities=capabilities)
    result = invoke(k2_env, server)
    assert 'CAPABILITY_UNAVAILABLE' in result['report']['failed']['f']
    assert not server.requests and not result['rows']['concepts']


def test_failed_decode_status_is_skipped_null_status_prepared(k2_env):
    folder(k2_env, 'f', count=2)
    scanned = scan(k2_env)
    scanned['rows']['images'][0]['decode_status'] = 'undecodable'
    server = NamingServer()
    result = invoke(k2_env, server, result=scanned)
    assert len(result['report']['model_named_folders'][0]['sampled_image_ids']) == 1
    assert len(server.requests[0]['messages'][1]['content']) == 2


def test_invalid_image_fails_folder_and_continues(k2_env):
    folder(k2_env, 'a-bad', payload=b'not an image')
    folder(k2_env, 'b-good')
    result = invoke(k2_env)
    assert 'IMAGE_INVALID' in result['report']['failed']['a-bad']
    assert result['report']['model_named_folders'][0]['folder_id'] == 'b-good'


def test_source_changes_after_scan_fail_without_model_call(k2_env):
    path = folder(k2_env, 'f', 'Name\nDescription')
    scanned = scan(k2_env)
    (path / 'info.txt').write_text('Changed\nData', encoding='utf-8')
    server = NamingServer()
    result = invoke(k2_env, server, result=scanned)
    assert 'SOURCE_CHANGED' in result['report']['failed']['f'] and not server.requests


def test_later_info_file_supersedes_generated_names(k2_env):
    path = folder(k2_env, 'f')
    invoke(k2_env)
    (path / 'info.txt').write_text('Human name\nHuman description', encoding='utf-8')
    server = NamingServer()
    result = invoke(k2_env, server)
    assert len(server.requests) == 1 and result['rows']['folders'][0]['name_source'] == 'info_file'
    assert result['rows']['concepts'][0]['display_name'] == 'Human name'


def test_removed_override_does_not_keep_stale_kind(k2_env):
    env = k2_env
    folder(env, 'f')
    override = env.source / 'kb_overrides.yaml'
    override.write_text('Generated: {concept_type: object}', encoding='utf-8')
    first = invoke(env)
    override.unlink()
    scanned = scan(env)
    scanned['handoff']['k2'] = first['rows']
    result = invoke(env, result=scanned)
    assert result['rows']['concepts'][0]['kind'] is None
    assert result['rows']['concepts'][0]['kind_origin'] is None


def test_a_bad_naming_reply_is_still_a_per_folder_failure(k2_env):
    env = k2_env
    folder(env, 'bad')
    folder(env, 'good')
    server = NamingServer({('folder_namer', 'bad'): {'concepts': 'labelled invalid reply'}})
    result = invoke(env, server)
    assert set(result['report']['failed']) == {'bad'} and result['report']['failed']['bad'] == 'SCHEMA_INVALID'
