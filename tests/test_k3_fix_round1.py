"""S31 regressions using tiny synthetic snapshots and labelled transport/failure doubles."""

from copy import deepcopy
import json
import hashlib
from pathlib import Path
from random import getstate

import PIL
from hypothesis import example, given, settings, strategies as st
import pytest

from k2_fixtures import folder, invoke
from k3_fixtures import CardServer, checkpoint_rows, inputs, invoke_cards, reply_for
from prompt_enhancer.k2_naming import apply_name_overrides, _digest, DRAW_REVISION
from prompt_enhancer.k3_cards import assemble_card, card_inputs, parse_card_reply
from prompt_enhancer.kb_audit import audit
from prompt_enhancer.storage import artifact_key, concept_id, SPECS, StorageError
from prompt_enhancer.storage.store import Checkpoints, PinnedSnapshot
from prompt_enhancer.storage.identity import canonical_json
from prompt_enhancer.profile_prompt import load_prompt
from prompt_enhancer.config import _schema, ConfigurationError
from storage_fixtures import dataset, DIM, make_store


def test_grouping_ignores_unrelated_overrides_and_rebuilds_current_evidence(k2_env):
    data = inputs(k2_env, ('A', 'B'))
    server = CardServer()
    first = invoke_cards(k2_env, data, server)
    for change in ('hash', 'unrelated', 'aliases', 'kind', 'context', 'folder_provenance', 'report'):
        if change == 'hash':
            data['overrides']['file_hash'] = 'comment-only'
        elif change == 'unrelated':
            data['overrides']['data']['Other'] = {'facts': ['Unrelated']}
            data['overrides']['unmatched_folders'] = ['absent']
        elif change == 'aliases':
            data['overrides']['data']['Concept'] = {'aliases': ['Alias']}
        elif change == 'kind':
            data['overrides']['data']['Concept']['concept_type'] = 'object'
        elif change == 'context':
            for row in data['observations']:
                row['context_key'] = _digest('changed-provenance')
        elif change == 'folder_provenance':
            for row in data['handoff']['folders'] + data['handoff']['folder_concepts'] + data['folder_images']:
                row['folder_id'] = 'renamed'
        else:
            k2_env.config['cards']['lint_report'] = False
        current = invoke_cards(k2_env, data, server)
        assert current['rows']['cards'] and len(server.requests) == 1, change
    assert current['rows']['cards'][0]['kind']['value'] == 'object'
    assert first['rows']['cards'][0]['curated_statements'][0]['folder_id'] == 'f'
    assert current['rows']['cards'][0]['curated_statements'][0]['folder_id'] == 'renamed'
    assert current['rows']['cards'][0]['revisions']['snapshot_id'] == data['snapshot_id']
    before = current['artifacts']
    k2_env.config['cards']['lint_report'] = True
    assert invoke_cards(k2_env, data, server)['artifacts'] == before
    assert len(server.requests) == 1


def test_variation_outside_support_or_invisible_or_suppressed_cannot_prove_stability(k2_env):
    data = inputs(k2_env, ('A', 'A', 'A', 'A'))
    for index, row in enumerate(data['observations']):
        row['view'] = 'front'
        if index > 1:
            row['visibility'] = 'occluded'
            row['value'] = 'unseen'
    original = deepcopy(data['observations'])
    def trait():
        card = invoke_cards(k2_env, data)['rows']['cards'][0]
        return next(item for item in card['facets'] if item['facet'] == 'identity.hair')['stability']
    data['observations'].extend(dict(row, facet='place.background', facet_group='place',
                                    visibility='clear', value=str(index))
                                for index, row in enumerate(original[2:]))
    assert trait() == 'stable_low_diversity'
    data['observations'] = original + [dict(row, facet='place.background', facet_group='place',
        value=str(index)) for index, row in enumerate(original[:2])]
    assert trait() == 'stable'
    data['overrides']['data']['Concept'] = {'suppress': ['place.background']}
    assert trait() == 'stable_low_diversity'
    data['overrides']['data']['Concept'] = {}
    for row in data['observations'][4:]:
        row.update(visibility='occluded', value='unseen')
    assert trait() == 'stable_low_diversity'


def test_null_kind_is_explicit_with_lint_and_human_kind_wins(k2_env):
    data = inputs(k2_env)
    server = CardServer(lambda payload, body: dict(reply_for(payload), kind=None))
    result = invoke_cards(k2_env, data, server)
    card = result['rows']['cards'][0]
    assert card['kind'] == {'value': None, 'origin': 'generated'}
    assert any(item['code'] == 'KIND_NOT_GIVEN' for item in card['lint_findings'])
    assert result['rows']['concepts'][0]['kind'] is None
    assert result['rows']['concepts'][0]['kind_origin'] is None
    data['overrides']['data']['Concept'] = {'concept_type': 'object'}
    result = invoke_cards(k2_env, data, server)
    assert result['rows']['cards'][0]['kind'] == {'value': 'object', 'origin': 'override'}
    assert not any(item['code'] == 'KIND_NOT_GIVEN' for item in result['rows']['cards'][0]['lint_findings'])
    assert len(server.requests) == 1


@given(st.text(min_size=1, max_size=40))
@example('VISIBILITY_GAP')
@example('LOW_DIVERSITY')
@example('UNMATCHED_OVERRIDE')
@example('SHADOWED_NAME_OVERRIDE')
@example('model:LOW_DIVERSITY')
@settings(max_examples=35)
def test_model_lint_namespace_is_code_owned(code):
    rows, payload = card_inputs([], [], 'Concept')
    reply = dict(reply_for(payload), lint_findings=[{'code': code, 'message': 'Data', 'facet_positions': [],
                                                  'observation_positions': [], 'curated_positions': []}])
    card = assemble_card({'concept_id': 'concept', 'display_name': 'Concept'}, rows, payload, reply, [], {}, [],
                         {'variant_groups': True, 'min_visible_images': 2, 'low_diversity_max_group_ratio': .5}, {})
    assert card['lint_findings'][0]['code'] == 'model:' + code


@pytest.mark.parametrize('selected_folder', ['a', 'z'])
@pytest.mark.parametrize('shadow', [False, True])
def test_same_identity_folder_spelling_keeps_canonical_and_traces_shadow(k2_env, selected_folder, shadow):
    for name in ('a', 'z'):
        folder(k2_env, name, 'Alice\nHuman text')
    rows = invoke(k2_env)['rows']
    effective, events, _ = apply_name_overrides(rows, {'file_hash': None, 'data': {
        'concepts': {'Alice': {'name': 'Zed'}} if shadow else {},
        'folders': [{'folder_id': selected_folder, 'name': 'ALICE'}]}},
        separator='|')
    canonical = next(row for row in rows['concepts'] if row['concept_id'] == concept_id('Alice'))
    assert canonical['display_name'] == 'Alice'
    members = {row['folder_id']: row for row in rows['folder_concepts']}
    assert members[selected_folder]['name_as_written'] == 'ALICE'
    assert members['z' if selected_folder == 'a' else 'a']['name_as_written'] == ('Zed' if shadow else 'Alice')
    codes = {item['code'] for item in effective.get('lint_findings', [])}
    assert 'FOLDER_NAME_SPELLING_DIFFERENCE' in codes
    assert ('SHADOWED_NAME_OVERRIDE' in codes) == shadow
    assert events and all(row['declared_names'] == ['Alice'] for row in rows['folders'])


@pytest.mark.parametrize('site', ['commit_then_raise', 'assemble', 'document', 'reuse'])
def test_caught_post_commit_failure_keeps_done_reply_and_retry_reuses(k2_env, monkeypatch, site):
    import prompt_enhancer.k3_cards as module
    data = inputs(k2_env)
    server = CardServer()
    original_commit = Checkpoints.commit
    if site == 'reuse':
        invoke_cards(k2_env, data, server)
    def damaged_commit(self, *args, **kwargs):
        if site == 'document' and self.stage == 'k3_card':
            raise ValueError('labelled document failure')
        result = original_commit(self, *args, **kwargs)
        if site == 'commit_then_raise' and self.stage == 'k3_grouping':
            raise ValueError('labelled caught failure after done')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'commit', damaged_commit)
        if site in ('assemble', 'reuse'):
            patch.setattr(module, 'assemble_card', lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError('labelled assembly failure')))
        failed = invoke_cards(k2_env, data, server)
    assert failed['report']['failed']
    states = checkpoint_rows(k2_env, 'k3_grouping')
    assert len(states) == 1 and states[0]['state'] == 'done'
    resumed = invoke_cards(k2_env, data, server)
    assert resumed['rows']['cards'] and len(server.requests) == 1


def test_audit_excludes_every_non_model_source_and_populations_have_independent_rng(project_root, tmp_path):
    store = make_store(project_root, tmp_path / 'audit-store')
    rows = dataset('audit')
    for row in rows['folders']:
        row['name_source'] = 'model'
    for index, source in enumerate(('info_file', 'folder_name', 'override', 'folder_override', None)):
        rows['folders'].append(dict(rows['folders'][0], folder_id='excluded-' + str(index), name_source=source))
    with store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, data in rows.items():
            builder.write_table(name, data, dim=DIM if SPECS[name].vector else None)
        writer.publish(builder.finish())
    state = getstate()
    all_rows = audit(store, folder_count=100, card_count=100, seed=4)
    assert len(all_rows['folders']) == 3
    assert all(row['name_source'] == 'model' for row in all_rows['folders'])
    assert len({row['folder_id'] for row in all_rows['folders']}) == len(all_rows['folders'])
    assert len({row['concept_id'] for row in all_rows['cards']}) == len(all_rows['cards'])
    for seed in (0, 1, 4, -17):
        base = audit(store, folder_count=0, card_count=2, seed=seed)
        for count in (1, 2, 3, 100):
            other = audit(store, folder_count=count, card_count=2, seed=seed)
            assert other['cards'] == base['cards']
            assert other['folders'] == audit(store, folder_count=count, card_count=0, seed=seed)['folders']
    assert getstate() == state


@given(st.lists(st.sampled_from(('A', 'B', 'C')), min_size=2, max_size=10))
@settings(max_examples=25)
def test_position_payload_ignores_provenance_order(values):
    rows = [{'context_key': str(index), 'image_id': str(index), 'facet_group': 'identity', 'facet': 'trait',
             'value': value, 'visibility': 'clear', 'view': 'front'} for index, value in enumerate(values)]
    before = card_inputs(rows, [], 'Concept')[1]
    after = card_inputs([dict(row, context_key=str(len(rows) - index)) for index, row in enumerate(rows)], [], 'Concept')[1]
    assert before == after


@pytest.mark.parametrize('source', [None, 'Different'])
def test_occupied_folder_target_refusal_is_named(k2_env, source):
    folder(k2_env, 'existing', 'Known', count=0)
    folder(k2_env, 'selected', source, count=0)
    rows = invoke(k2_env)['rows']
    with pytest.raises(ConfigurationError, match='FOLDER_TARGET_OCCUPIED'):
        apply_name_overrides(rows, {'file_hash': None, 'data': {'concepts': {},
            'folders': [{'folder_id': 'selected', 'name': 'KNOWN'}]}}, separator='|')


@pytest.mark.parametrize('stage', ['k2_assessment', 'k2_naming', 'k2_observation', 'k3_grouping'])
def test_referenced_old_format_key_is_never_read_or_rewritten(k2_env, monkeypatch, stage):
    from k2_fixtures import NamingServer, scan
    from k2_observe_fixtures import ObserverServer, invoke_observer, k1_result
    from prompt_enhancer.k3_cards import _curated
    env = k2_env
    role_name = {'k2_assessment': 'info_assessor', 'k2_naming': 'folder_namer',
                 'k2_observation': 'observer', 'k3_grouping': 'card_builder'}[stage]
    if stage == 'k3_grouping':
        data = inputs(env, ('A', 'B'))
    else:
        folder(env, 'f', None if stage == 'k2_naming' else 'Concept\nHuman text')
        data = k1_result(env) if stage == 'k2_observation' else scan(env)
    config, root = env.config, env.root
    role = config['vlm']['roles'][role_name]
    backends = config['vlm']['backends']
    prompt = load_prompt(root, role['prompt_id'])
    model = _digest({'role': role, 'backends': {name: backends[name]
                     for name in {role['backend'], config['vlm']['default_backend']}}})
    reply_schema = {'k2_assessment': 'info_assessment.v0', 'k2_naming': 'folder_naming.v0',
                    'k2_observation': 'observer_reply.v0', 'k3_grouping': 'card_builder_reply.v0'}[stage]
    schema = {'layout': 1, 'configured': _schema(root, role['output']['schema_id'])}
    prompt_revision = {'id': role['prompt_id']}
    if stage in ('k2_assessment', 'k2_naming'):
        schema.update(contract=_schema(root, reply_schema), vision=config['vision'], pillow=PIL.__version__)
        prompt_revision['sha256'] = hashlib.sha256(prompt).hexdigest()
        context = {'folder_id': 'f', 'folder_name': 'f', 'info_file_text': 'Concept\nHuman text',
                   'names': [{'position': 0, 'name': 'Concept'}], 'assignment_required': False}
        value = context
        if stage == 'k2_naming':
            images = sorted({row['image_id'] for row in data['rows']['images']})
            context = {'folder_id': 'f', 'folder_name': 'f', 'info_file_text': None, 'names': [],
                       'sampled_image_ids': images}
            population = {'folder_id': 'f', 'sample_size': config['knowledge']['naming']['sample_size'],
                          'selection': config['knowledge']['naming']['selection'], 'candidate_image_ids': images}
            draw = artifact_key('k2_draw', _digest(population), DRAW_REVISION)
            value = {'context': context, 'draw_key': draw}
    elif stage == 'k2_observation':
        schema.update(reply=_schema(root, reply_schema), stored=_schema(root, 'image_observation.v0'),
                      facet_schema_version=config['observe']['facet_schema_version'], vision=config['vision'],
                      processor=PIL.__version__)
        prompt_revision['bytes'] = hashlib.sha256(prompt).hexdigest()
        value = {'image_id': data['images'][0]['image_id'], 'concept_names': ['Concept']}
    else:
        schema['reply'] = _schema(root, reply_schema)
        prompt_revision['sha256'] = hashlib.sha256(prompt).hexdigest()
        concept = data['handoff']['concepts'][0]
        curated = _curated(concept, data['handoff'], data['overrides']['data'].get('Concept', {}))
        rows, payload = card_inputs(data['observations'], curated, concept['display_name'])
        legacy_rows = sorted(rows, key=canonical_json)
        payload['observations'] = [dict(payload['observations'][rows.index(row)], observation_position=index)
                                   for index, row in enumerate(legacy_rows)]
        payload['suppressed_facets'] = []
        value = {'rows': legacy_rows, 'curated': curated, 'concept': concept,
                 'overrides': data['overrides'], 'payload': payload}
    old_revisions = {'model': model, 'prompt': _digest(prompt_revision), 'schema': _digest(schema)}
    old_key = artifact_key(stage, _digest(value), old_revisions)
    opaque = b'old-format artifact must stay opaque and immutable'
    with env.store.writer() as writer:
        artifact = writer.checkpoints(stage).commit(old_key, stage, opaque, revisions=old_revisions)
        builder = writer.begin_snapshot()
        for name, rows in dataset('old').items():
            builder.write_table(name, rows, dim=DIM if SPECS[name].vector else None)
        builder.reference_artifacts([artifact])
        writer.publish(builder.finish())
    load = Checkpoints.load
    def guarded(self, key):
        assert not (self.stage == stage and key == old_key), 'old-format key was read'
        return load(self, key)
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoints, 'load', guarded)
        if stage == 'k3_grouping':
            result = invoke_cards(env, data, CardServer())
            assert result['rows']['cards']
        elif stage == 'k2_observation':
            result = invoke_observer(env, ObserverServer(), inputs=data)
            assert result['rows']['observations']
        else:
            result = invoke(env, NamingServer(), result=data)
            assert result['rows']['folder_concepts']
    assert (env.store.root / artifact).read_bytes() == opaque


def test_visible_view_variation_ignores_occluded_conflicting_views(k2_env):
    data = inputs(k2_env, ('A', 'A'))
    for index, row in enumerate(data['observations']):
        row['view'] = ('left', 'right')[index]
    data['observations'].extend(dict(row, facet='place.background', facet_group='place', view='front',
                                    visibility='occluded', value='unknown')
                                for row in list(data['observations']))
    trait = next(item for item in invoke_cards(k2_env, data)['rows']['cards'][0]['facets']
                 if item['facet'] == 'identity.hair')
    assert trait['stability'] == 'stable'


def test_ambiguous_other_facet_assignments_do_not_prove_variation(k2_env):
    data = inputs(k2_env, ('A', 'A'))
    for row in data['observations']:
        row['view'] = 'front'
    first, second = data['observations']
    data['observations'].extend([
        dict(first, facet='place.background', facet_group='place', value='A'),
        dict(first, facet='place.background', facet_group='place', value='B', context_key=_digest('alternative')),
        dict(second, facet='place.background', facet_group='place', value='B')])
    trait = next(item for item in invoke_cards(k2_env, data)['rows']['cards'][0]['facets']
                 if item['facet'] == 'identity.hair')
    assert trait['stability'] == 'stable_low_diversity'


def test_naming_lints_are_checkpointed_propagated_and_not_model_input(k2_env):
    folder(k2_env, 'f', 'Alice\nHuman')
    (k2_env.source / 'kb_overrides.yaml').write_text(
        'concepts: {Alice: {name: Global}}\nfolders: [{folder_id: f, name: ALICE}]', encoding='utf-8')
    named = invoke(k2_env)
    assert {item['code'] for item in named['report']['lint_findings']} == {
        'SHADOWED_NAME_OVERRIDE', 'FOLDER_NAME_SPELLING_DIFFERENCE'}
    applied = next(path for path in named['artifacts'] if '/k2_names_applied/' in path)
    assert json.loads((k2_env.store.root / applied).read_bytes())['lint_findings'] == named['report']['lint_findings']
    data = inputs(k2_env)
    data['overrides'] = named['effective_overrides']
    server = CardServer()
    result = invoke_cards(k2_env, data, server)
    assert any(item['code'] == 'SHADOWED_NAME_OVERRIDE' for item in result['report']['lint_findings'])
    payload = json.loads(server.requests[0]['messages'][1]['content'])
    assert 'SHADOWED_NAME_OVERRIDE' not in canonical_json(payload)
    assert 'folder_id' not in canonical_json(payload)
    k2_env.config['cards']['lint_report'] = False
    assert invoke_cards(k2_env, data, server)['report']['lint_findings'] == []
    assert len(server.requests) == 1


@pytest.mark.parametrize('current', [False, True])
def test_audit_missing_lock_distinguishes_absent_snapshot_without_writing(project_root, tmp_path, current):
    store = make_store(project_root, tmp_path / 'bare-store')
    store.root.mkdir()
    if current:
        (store.root / 'CURRENT').write_text('not opened', encoding='utf-8')
        with pytest.raises(StorageError, match='without the store lock'):
            audit(store, folder_count=1, card_count=1, seed=0)
    else:
        assert audit(store, folder_count=1, card_count=1, seed=0)['status'] == 'SNAPSHOT_MISSING'
    assert sorted(path.name for path in store.root.iterdir()) == (['CURRENT'] if current else [])


def test_audit_zero_reads_and_permutation_are_population_local(project_root, tmp_path, monkeypatch):
    from test_kb_audit import published
    store, _ = published(project_root, tmp_path)
    first = audit(store, folder_count=2, card_count=2, seed=7)
    original, reads = PinnedSnapshot.read_rows, []
    def reversed_rows(self, name):
        reads.append(name)
        return list(reversed(original(self, name)))
    monkeypatch.setattr(PinnedSnapshot, 'read_rows', reversed_rows)
    assert audit(store, folder_count=2, card_count=2, seed=7) == first
    assert all(member['folder_id'] == row['folder_id'] for row in first['folders'] for member in row['concepts'])
    for folders, cards, expected in [(0, 0, []), (0, 1, ['cards']), (1, 0, ['folders', 'folder_concepts'])]:
        reads.clear()
        audit(store, folder_count=folders, card_count=cards, seed=7)
        assert reads == expected


@pytest.mark.parametrize('field', ['facet_positions', 'observation_positions', 'curated_positions'])
def test_each_lint_reference_domain_is_checked(field):
    rows = [{'facet_group': 'identity', 'facet': 'trait', 'value': 'A', 'visibility': 'clear', 'view': 'front'}]
    _, payload = card_inputs(rows, [{'text': 'Human', 'origin': 'info_file'}], 'Concept')
    finding = {'code': 'Data', 'message': 'Data', 'facet_positions': [], 'observation_positions': [], 'curated_positions': []}
    finding[field] = [1]
    reply = dict(reply_for(payload), lint_findings=[finding])
    with pytest.raises(ValueError, match='references an unknown position'):
        parse_card_reply(reply, payload, project_root=Path(__file__).resolve().parents[1])


def test_reused_card_document_must_equal_current_derivation(k2_env):
    data, server = inputs(k2_env), CardServer()
    invoke_cards(k2_env, data, server)
    with k2_env.store.writer() as writer:
        docs = writer.checkpoints('k3_card')
        row = next(iter(docs._rows.values()))
        card = json.loads(docs.load(row['input_key']))
        card['summary'] = 'Checksummed but wrong derived document'
        docs.commit(row['input_key'], 'k3_card', canonical_json(card).encode(), revisions={
            'model': row['model_revision'], 'prompt': row['prompt_revision'], 'schema': row['schema_revision']})
    with pytest.raises(StorageError, match='completed document disagrees'):
        invoke_cards(k2_env, data, server)
    assert len(server.requests) == 1


def test_referenced_reply_survives_caught_failure_with_resume_disabled(k2_env, monkeypatch):
    import prompt_enhancer.k3_cards as module
    from prompt_enhancer.storage.store import Writer

    sessions = []
    enter = Writer.__enter__

    def deterministic_session(self):
        result = enter(self)
        self.run_id = 'labelled-test-session-' + str(len(sessions))
        sessions.append(self.run_id)
        return result

    monkeypatch.setattr(Writer, '__enter__', deterministic_session)
    data, server = inputs(k2_env), CardServer()
    built = invoke_cards(k2_env, data, server)
    artifact = next(path for path in built['artifacts'] if '/k3_grouping/' in path)
    key = artifact.rsplit('/', 1)[-1].removesuffix('.bin')
    before = (k2_env.store.root / artifact).read_bytes()

    def capture(phase, referenced):
        with k2_env.store.writer() as writer:
            rows = {row['input_key']: row for row in writer._state_rows().values()
                    if row['stage'] == 'k3_grouping'}
            trace = {'phase': phase, 'key': key, 'rows': rows, 'session': writer.run_id,
                     'referenced': artifact in writer._referenced(),
                     'sha256': hashlib.sha256(k2_env.store.path(artifact).read_bytes()).hexdigest(),
                     'segments': [name for name, _ in writer._state_segments()]}
            print(json.dumps(trace, default=str, sort_keys=True, ensure_ascii=False))
            assert set(rows) == {key}, trace
            assert rows[key]['state'] == 'done' and rows[key]['run_id'] != writer.run_id, trace
            assert trace['referenced'] is referenced, trace
            assert trace['sha256'] == hashlib.sha256(before).hexdigest(), trace
            return rows[key]

    initial = capture('built', False)
    with k2_env.store.writer() as writer:
        builder = writer.begin_snapshot()
        for name, rows in dataset('reference').items():
            builder.write_table(name, rows, dim=DIM if SPECS[name].vector else None)
        builder.reference_artifacts([artifact])
        writer.publish(builder.finish())
    assert capture('referenced', True) == initial
    k2_env.store._resume = False
    k2_env.config['sync']['resume'] = False
    with monkeypatch.context() as patch:
        patch.setattr(module, 'assemble_card', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('labelled retry')))
        failed = invoke_cards(k2_env, data, server)
        assert list(failed['report']['failed'].values()) == ['SCHEMA_INVALID'], failed['report']
    assert capture('after_failure', True) == initial
    assert (k2_env.store.root / artifact).read_bytes() == before
    assert invoke_cards(k2_env, data, server)['rows']['cards'] and len(server.requests) == 1
    assert capture('resumed', True) == initial
