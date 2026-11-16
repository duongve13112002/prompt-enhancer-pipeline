"""Decision 125: concept-name selector, immutable raw naming cache and structural refusals."""

from pathlib import Path
import json
import unicodedata

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from k2_fixtures import NamingServer, folder, invoke, scan, source_tree
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.k2_naming import apply_name_overrides, parse_overrides
from prompt_enhancer.storage import concept_id

ROOT = Path(__file__).resolve().parents[1]


def test_name_wins_changes_hash_and_removal_reuses_raw_result(k2_env):
    folder(k2_env, 'f')
    path = k2_env.source / 'kb_overrides.yaml'
    server = NamingServer()
    raw = invoke(k2_env, server)
    previous_hash = raw['overrides']['file_hash']
    for target in ('Corrected', 'Changed'):
        path.write_text(yaml.safe_dump({'Generated': {'name': target, 'concept_type': 'object', 'facts': ['Human']}}), encoding='utf-8')
        result = invoke(k2_env, server)
        assert result['rows']['concepts'][0]['concept_id'] == concept_id(target)
        assert result['rows']['concepts'][0]['kind'] == 'object'
        assert result['rows']['folders'][0]['name_source'] == 'override'
        assert result['report']['renamed_concepts'][0]['origin'] == 'override'
        assert result['overrides']['file_hash'] != previous_hash
        previous_hash = result['overrides']['file_hash']
        assert target in result['effective_overrides']['data']
        assert result['rows']['folders'][0]['declared_names'] == []
    path.unlink()
    restored = invoke(k2_env, server)
    assert restored['rows'] == raw['rows'] and len(server.requests) == 1


def test_name_applies_across_folders_preserving_human_text(k2_env):
    folder(k2_env, 'one', 'Old | Other\nFirst | Second')
    folder(k2_env, 'two', 'Old\nHuman text')
    (k2_env.source / 'kb_overrides.yaml').write_text('Old: {name: Corrected}\nUnknown: {name: Unused}', encoding='utf-8')
    before = source_tree(k2_env.source)
    result = invoke(k2_env)
    renamed = [row for row in result['rows']['folder_concepts'] if row['concept_id'] == concept_id('Corrected')]
    assert len(renamed) == 2 and {row['description'] for row in renamed} == {'First', 'Human text'}
    assert result['report']['unmatched_overrides'] == ['Unknown']
    assert [row['declared_names'] for row in result['rows']['folders']] == [['Old', 'Other'], ['Old']]
    assert source_tree(k2_env.source) == before


@pytest.mark.parametrize('selector', ['concept', 'folder'])
def test_name_change_flows_through_observation_context_and_card(k2_env, selector):
    from k2_observe_fixtures import ObserverServer, invoke_observer, k1_result
    from k3_fixtures import CardServer, invoke_cards
    from prompt_enhancer.storage.store import new_snapshot_id
    folder(k2_env, 'f', count=2)
    current = k1_result(k2_env)
    naming_server, observer_server, card_server = NamingServer(), ObserverServer(), CardServer()
    contexts = []
    for target in ('Corrected', 'Changed'):
        curation = {'facts': ['Human fact'], 'concept_type': 'object'}
        document = ({'Generated': dict(curation, name=target)} if selector == 'concept' else
                    {'concepts': {'Generated': curation}, 'folders': [{'folder_id': 'f', 'name': target}]})
        (k2_env.source / 'kb_overrides.yaml').write_text(yaml.safe_dump(document), encoding='utf-8')
        named = invoke(k2_env, naming_server)
        observed = invoke_observer(k2_env, observer_server, inputs=dict(current, handoff=named['rows']))
        assert all(row['concept_id'] == concept_id(target) for row in observed['rows']['observations'])
        contexts.append(observed['rows']['observations'][0]['context_key'])
        data = {'handoff': named['rows'], 'observations': observed['rows']['observations'],
                'images': observed['rows']['images'], 'folder_images': current['folder_images'],
                'snapshot_id': new_snapshot_id(), 'overrides': named['effective_overrides']}
        result = invoke_cards(k2_env, data, card_server)
        assert not result['report']['failed']
        card = result['rows']['cards'][0]
        assert card['concept_id'] == concept_id(target) and card['kind']['origin'] == 'override'
        assert any(row['text'] == 'Human fact' for row in card['curated_statements'])
    assert contexts[0] != contexts[1]
    assert len(naming_server.requests) == 1 and len(observer_server.requests) == len(card_server.requests) == 2


@pytest.mark.parametrize('old_override_spelling', ['a', 'Ａ'])
def test_raw_k0_spelling_restored_after_same_identity_name_override(k2_env, old_override_spelling):
    folder(k2_env, 'f', 'A\nHuman text')
    raw = scan(k2_env)
    # K0 _merge_previous retains concept metadata by normalized ID; source members are freshly parsed.
    raw['handoff']['k2']['concepts'][0]['display_name'] = old_override_spelling
    result = invoke(k2_env, result=raw)
    assert result['rows']['concepts'][0]['display_name'] == 'A'
    assert result['rows']['folders'][0]['declared_names'] == ['A']
    assert result['rows']['folder_concepts'][0]['description'] == 'Human text'


@pytest.mark.parametrize('text', ['A: {name: B}\nB: {name: C}', 'A: {name: B}\nB: {name: A}',
                                  'A: {name: ""}', 'A: {name: " A"}', 'A: {name: "A\\nB"}',
                                  'A: {name: 1}', 'A: {name: [B]}'])
def test_override_name_structure_and_chains_refused(text):
    with pytest.raises(ConfigurationError):
        parse_overrides(text.encode(), project_root=ROOT)


@pytest.mark.parametrize('text', ['A: {name: B}', 'A: {name: Same}\nB: {name: same}', 'A: {name: "B|C"}'])
def test_rename_never_merges_or_creates_multiple_names(k2_env, text):
    folder(k2_env, 'f', 'A | B')
    (k2_env.source / 'kb_overrides.yaml').write_text(text, encoding='utf-8')
    with pytest.raises(ConfigurationError):
        invoke(k2_env)


@given(st.text(st.characters(blacklist_categories=('Cs', 'Cc', 'Zl', 'Zp')), min_size=1, max_size=25))
@settings(max_examples=40)
def test_override_name_round_trip_or_structural_refusal(name):
    from prompt_enhancer.k0 import trim_info_token
    raw = yaml.safe_dump({'Source': {'name': name}}, allow_unicode=False).encode()
    if not trim_info_token(name) or trim_info_token(name) != name:
        with pytest.raises(ConfigurationError):
            parse_overrides(raw, project_root=ROOT)
    else:
        assert parse_overrides(raw, project_root=ROOT)['Source']['name'] == name


def test_folder_override_precedence_scope_curation_hash_and_raw_rebuild(k2_env):
    folder(k2_env, 'one', 'Old\nHuman text')
    folder(k2_env, 'two', 'Old\nOther human text')
    path = k2_env.source / 'kb_overrides.yaml'
    raw = invoke(k2_env)
    hashes, applied = [], []
    for name in ('Local', 'Changed'):
        path.write_text(yaml.safe_dump({'concepts': {'Old': {'name': 'Global', 'facts': ['Curated'],
                                                           'concept_type': 'object'}},
                                       'folders': [{'folder_id': 'one', 'name': name}]}), encoding='utf-8')
        before = source_tree(k2_env.source)
        result = invoke(k2_env)
        assert source_tree(k2_env.source) == before
        members = {row['folder_id']: row for row in result['rows']['folder_concepts']}
        assert members['one']['concept_id'] == concept_id(name)
        assert members['two']['concept_id'] == concept_id('Global')
        assert members['one']['description'] == 'Human text'
        assert members['two']['description'] == 'Other human text'
        assert all(row['declared_names'] == ['Old'] for row in result['rows']['folders'])
        assert [row['name_source'] for row in result['rows']['folders']] == ['folder_override', 'override']
        assert all(row['kind'] == 'object' for row in result['rows']['concepts'])
        assert result['effective_overrides']['data'][name]['facts'] == ['Curated']
        event = next(row for row in result['report']['renamed_concepts'] if row['folder_id'] == 'one')
        assert event['origin'] == 'folder_override' and event['source_concept_id'] == concept_id('Old')
        hashes.append(result['overrides']['file_hash'])
        checkpoint = next(key for key in result['artifacts'] if '/k2_names_applied/' in key)
        stored = json.loads((k2_env.store.root / checkpoint).read_bytes())
        assert event in stored['renames']
        assert stored['rows']['folders'][0]['name_source'] == 'folder_override'
        applied.append(checkpoint)
    assert len(set(hashes)) == len(set(applied)) == 2
    path.write_text('concepts: {Old: {name: Global}}\nfolders: []', encoding='utf-8')
    assert {row['concept_id'] for row in invoke(k2_env)['rows']['folder_concepts']} == {concept_id('Global')}
    path.unlink()
    assert invoke(k2_env)['rows'] == raw['rows']


@pytest.mark.parametrize('info', [None, 'A | a\nOriginal unusable text'])
def test_folder_override_names_empty_folder_and_removal_restores_raw(k2_env, info):
    folder(k2_env, 'empty', info, count=0)
    path = k2_env.source / 'kb_overrides.yaml'
    server = NamingServer()
    raw = invoke(k2_env, server)
    path.write_text('concepts: {}\nfolders: [{folder_id: empty, name: Human}]', encoding='utf-8')
    named = invoke(k2_env, server)
    assert named['rows']['folder_concepts'] == [{'folder_id': 'empty', 'concept_id': concept_id('Human'),
        'position': 0, 'name_as_written': 'Human', 'description': None,
        'description_origin': None, 'description_revision': None}]
    assert named['rows']['folders'][0]['name_source'] == 'folder_override'
    assert named['rows']['folders'][0]['error'] == ''
    path.unlink()
    assert invoke(k2_env, server)['rows'] == raw['rows'] and not server.requests


def test_folder_override_generated_name_uses_raw_cache(k2_env):
    folder(k2_env, 'f')
    server = NamingServer()
    raw = invoke(k2_env, server)
    path = k2_env.source / 'kb_overrides.yaml'
    for name in ('Local', 'Changed'):
        path.write_text(yaml.safe_dump({'concepts': {}, 'folders': [{'folder_id': 'f', 'name': name}]}), encoding='utf-8')
        result = invoke(k2_env, server)
        assert result['rows']['concepts'][0]['concept_id'] == concept_id(name)
        assert result['report']['renamed_concepts'][0]['origin'] == 'folder_override'
    path.unlink()
    assert invoke(k2_env, server)['rows'] == raw['rows'] and len(server.requests) == 1


def test_unknown_folder_selector_is_lint_only_through_cards(k2_env):
    from k3_fixtures import CardServer, invoke_cards, inputs
    folder(k2_env, 'f', 'Known')
    (k2_env.source / 'kb_overrides.yaml').write_text(
        'concepts: {}\nfolders: [{folder_id: absent, name: Unused}]', encoding='utf-8')
    named = invoke(k2_env)
    assert {row['display_name'] for row in named['rows']['concepts']} == {'Known'}
    assert named['report']['lint_findings'] == [{'code': 'UNMATCHED_FOLDER_OVERRIDE', 'message': 'absent', 'refs': []}]
    data = inputs(k2_env)
    data['overrides'] = named['effective_overrides']
    cards = invoke_cards(k2_env, data, CardServer())
    assert any(row['code'] == 'UNMATCHED_FOLDER_OVERRIDE' and row['message'] == 'absent'
               for row in cards['report']['lint_findings'])


@pytest.mark.parametrize('entries', [
    [{'folder_id': 'f', 'name': 'One'}, {'folder_id': 'f', 'name': 'Two'}],
    [{'folder_id': 'f', 'name': 'One'}, {'folder_id': 'f', 'name': 'Ｏｎｅ'}],
    [{'folder_id': 'f'}], [{'folder_id': 'f', 'name': 1}],
    [{'name': 'Local'}], [{'folder_id': 1, 'name': 'Local'}],
    [{'folder_id': 'f', 'name': ''}], [{'folder_id': 'f', 'name': '\t'}],
    [{'folder_id': 'f', 'name': ' A'}], [{'folder_id': 'f', 'name': 'A\nB'}],
    [{'folder_id': 'f', 'name': 'A', 'facts': ['Not a folder option']}],
])
def test_folder_selector_structure_and_conflicts_refused(entries):
    with pytest.raises(ConfigurationError):
        parse_overrides(yaml.safe_dump({'concepts': {}, 'folders': entries}).encode(), project_root=ROOT)


@pytest.mark.parametrize('names,entries', [
    (['A | B'], [{'folder_id': 'f0', 'name': 'Local'}]),
    (['A', 'B'], [{'folder_id': 'f0', 'name': 'Ｂ'}]),
    (['A', 'B'], [{'folder_id': 'f0', 'name': 'Local'}, {'folder_id': 'f1', 'name': 'ｌｏｃａｌ'}]),
    ([None, None], [{'folder_id': 'f0', 'name': 'Local'}, {'folder_id': 'f1', 'name': 'LOCAL'}]),
    (['A'], [{'folder_id': 'f0', 'name': 'B|C'}]),
])
def test_folder_selector_ambiguity_collisions_and_separator_refused(k2_env, names, entries):
    for index, text in enumerate(names):
        folder(k2_env, f'f{index}', text, count=0)
    (k2_env.source / 'kb_overrides.yaml').write_text(
        yaml.safe_dump({'concepts': {}, 'folders': entries}), encoding='utf-8')
    with pytest.raises(ConfigurationError):
        invoke(k2_env)


def test_explicit_sections_do_not_reserve_concept_names_and_duplicates_are_idempotent(k2_env):
    legacy = {'folders': {'name': 'F'}, 'concepts': {'facts': ['Human']}}
    assert parse_overrides(yaml.safe_dump(legacy).encode(), project_root=ROOT) == legacy
    folder(k2_env, 'f', 'A')
    entry = {'folder_id': 'f', 'name': 'Local'}
    (k2_env.source / 'kb_overrides.yaml').write_text(
        yaml.safe_dump({'concepts': {}, 'folders': [entry, entry]}), encoding='utf-8')
    result = invoke(k2_env)
    assert len(result['rows']['concepts']) == len(result['rows']['folder_concepts']) == 1


@pytest.mark.parametrize('document', [
    {'folders': []}, {'concepts': {}, 'folders': [], 'extra': {}},
    {'concepts': {}, 'folders': [{'folder_id': 'f', 'name': 'Local'}], 'unknown': True},
    {'concepts': {'A': {}, 'Ａ': {}}, 'folders': []},
    {'concepts': {'A': {'name': 'B'}, 'B': {'name': 'C'}}, 'folders': []},
])
def test_explicit_selector_schema_and_concept_rules_are_not_bypassed(document):
    with pytest.raises(ConfigurationError):
        parse_overrides(yaml.safe_dump(document).encode(), project_root=ROOT)


def test_folder_selector_ambiguous_curation_is_refused(k2_env):
    folder(k2_env, 'f', 'A')
    document = {'concepts': {'A': {'facts': ['Source']}, 'Local': {'facts': ['Target']}},
                'folders': [{'folder_id': 'f', 'name': 'Local'}]}
    (k2_env.source / 'kb_overrides.yaml').write_text(yaml.safe_dump(document), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='ambiguous curated selectors'):
        invoke(k2_env)


def test_folder_selector_preserves_source_errors():
    rows = {'folders': [{'folder_id': 'f', 'name_source': None, 'error': 'SOURCE_CHANGED'}],
            'concepts': [], 'folder_concepts': []}
    apply_name_overrides(rows, {'data': {'concepts': {}, 'folders': [{'folder_id': 'f', 'name': 'Human'}]},
                                'file_hash': None}, separator='|')
    assert rows['folders'][0]['error'] == 'SOURCE_CHANGED'


@given(st.lists(st.text(st.characters(whitelist_categories=('Lu', 'Ll', 'Lo', 'Nd')), min_size=1, max_size=8),
                min_size=1, max_size=3))
@settings(max_examples=35)
def test_folder_selector_path_spellings_are_exact_and_noncanonical_paths_refused(parts):
    path = '/'.join(parts)
    doc = {'concepts': {}, 'folders': [{'folder_id': path, 'name': 'Human'}]}
    parsed = parse_overrides(yaml.safe_dump(doc).encode(), project_root=ROOT)
    assert parsed == doc
    rows = {'folders': [{'folder_id': path, 'name_source': None, 'error': 'pending_naming'}],
            'concepts': [], 'folder_concepts': []}
    applied, events, unmatched = apply_name_overrides(rows, {'data': parsed, 'file_hash': None}, separator='|')
    assert rows['folder_concepts'][0]['folder_id'] == path
    assert not applied['unmatched_folders'] and not unmatched and len(events) == 1
    for bad in ('/' + path, path + '/', './' + path, '../' + path, path + '//x', path + '\\x', path + '\x00'):
        doc['folders'][0]['folder_id'] = bad
        with pytest.raises(ConfigurationError):
            parse_overrides(yaml.safe_dump(doc).encode(), project_root=ROOT)
    alternates = {path.swapcase(), unicodedata.normalize('NFC', path), unicodedata.normalize('NFD', path)} - {path}
    for alternate in sorted(alternates):
        doc['folders'][0]['folder_id'] = alternate
        other = parse_overrides(yaml.safe_dump(doc).encode(), project_root=ROOT)
        unchanged = {'folders': [{'folder_id': path, 'name_source': None, 'error': 'pending_naming'}],
                     'concepts': [], 'folder_concepts': []}
        result, events, _ = apply_name_overrides(unchanged, {'data': other, 'file_hash': None}, separator='|')
        assert result['unmatched_folders'] == [alternate] and not events and not unchanged['concepts']
