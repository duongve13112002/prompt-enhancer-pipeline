"""Bounded Unicode/parser/collision properties; no inference, downloads or language rules."""

from copy import deepcopy
from pathlib import Path
import unicodedata

from hypothesis import given, settings, strategies as st
from jsonschema import Draft202012Validator
import pytest

from prompt_enhancer.config import _schema
from prompt_enhancer.k4_aliases import mark_collisions, parse_alias_reply
from prompt_enhancer.storage import normalise_name

ROOT = Path(__file__).resolve().parents[1]
TEXT = st.text(min_size=1, max_size=30)


@settings(max_examples=60, deadline=None)
@given(st.text(max_size=100))
def test_normalization_matches_nfkc_casefold(value):
    assert normalise_name(value) == unicodedata.normalize('NFKC', value).casefold()


@pytest.mark.parametrize(('left', 'right', 'equal'), [
    ('ÁO', 'A\u0301O', True), ('ＳＡＭＥ', 'same', True), ('Straße', 'STRASSE', True),
    ('áo', 'ao', False), (' name', 'name', False), ('名前', 'namae', False),
])
def test_normalization_keeps_only_structural_equivalence(left, right, equal):
    assert (normalise_name(left) == normalise_name(right)) is equal


@settings(max_examples=45, deadline=None)
@given(st.lists(st.fixed_dictionaries({'text': TEXT, 'language': st.one_of(TEXT, st.none())}), max_size=8))
def test_alias_reply_properties(aliases):
    value = {'aliases': aliases}
    parsed = parse_alias_reply(value, project_root=ROOT)
    assert parsed == value and parsed is not value
    for field in ('concept_id', 'origin', 'status', 'revisions', 'path'):
        invalid = deepcopy(value)
        invalid[field] = 'pipeline-owned'
        with pytest.raises(ValueError):
            parse_alias_reply(invalid, project_root=ROOT)
    if aliases:
        invalid = deepcopy(value)
        invalid['aliases'][0]['concept_id'] = 'model-owned'
        with pytest.raises(ValueError):
            parse_alias_reply(invalid, project_root=ROOT)


@pytest.mark.parametrize('value', [None, [], {'aliases': None}, {'aliases': ['text']},
    {'aliases': [{'text': '', 'language': None}]}, {'aliases': [{'text': 'x'}]},
    {'aliases': [{'text': 'x', 'language': 1}]}, {'aliases': [{'text': '\ud800', 'language': None}]}])
def test_alias_reply_invalid_structure(value):
    with pytest.raises((ValueError, UnicodeError)):
        parse_alias_reply(value, project_root=ROOT)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.lists(TEXT, max_size=7), min_size=1, max_size=5))
def test_collision_groups_match_reference(alias_sets):
    concepts = [{'concept_id': str(index), 'display_name': f'Canonical {index}'}
                for index in range(len(alias_sets))]
    rows = [{'concept_id': str(index), 'alias_text': text} for index, aliases in enumerate(alias_sets) for text in aliases]
    actual, findings = mark_collisions(concepts, rows)
    for row in actual:
        # Independent small reference scans are intentional in the property, never in runtime.
        key = unicodedata.normalize('NFKC', row['alias_text']).casefold()
        expected = {item['concept_id'] for item in concepts
                    if unicodedata.normalize('NFKC', item['display_name']).casefold() == key}
        expected.update(item['concept_id'] for item in rows
                        if unicodedata.normalize('NFKC', item['alias_text']).casefold() == key)
        expected.discard(row['concept_id'])
        assert row['collides_with'] == sorted(expected)
        assert row['status'] == ('pending_review' if expected else 'accepted')
    assert len(findings) == sum(row['status'] == 'pending_review' for row in actual)


def test_alias_grouping_work_is_bounded(monkeypatch):
    import prompt_enhancer.k4_aliases as module
    real = module.normalise_name
    calls = []
    monkeypatch.setattr(module, 'normalise_name', lambda value: calls.append(value) or real(value))
    for n in (16, 32):
        calls.clear()
        concepts = [{'concept_id': str(i), 'display_name': f'Name {i}'} for i in range(n)]
        rows = [{'concept_id': str(i), 'alias_text': f'Alias {i}'} for i in range(n)]
        mark_collisions(concepts, rows)
        assert len(calls) == 3 * n  # C canonical names + A ownership inserts + A lookups.


@pytest.mark.parametrize('origin', ['author', 'generated'])
@pytest.mark.parametrize('missing_revision', [False, True])
def test_author_provenance_contract(origin, missing_revision):
    value = {'concept_id': 'trusted', 'aliases': [{'text': 'alias', 'origin': origin,
             'language': None, 'status': 'accepted', 'collides_with': []}],
             'revisions': {'model_revision': None if missing_revision else 'actual-model-revision',
                           'prompt_id': None if missing_revision else 'actual-prompt-id'}}
    validator = Draft202012Validator(_schema(ROOT, 'alias_set.v0'))
    assert validator.is_valid(value) == ((origin == 'author') == missing_revision)
