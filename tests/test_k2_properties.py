"""Bounded structural properties of K2 parsers and sampling; no model or network."""

from collections import Counter
from itertools import product
from random import Random

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.k0 import trim_info_token
from prompt_enhancer.k2_naming import parse_assessment, parse_naming, parse_overrides, uniform_draw
from k2_fixtures import REPO, assessment, naming

TEXT = st.text(st.characters(blacklist_categories=('Cs',)), max_size=40)


@given(st.integers(min_value=1, max_value=12), st.data())
@settings(max_examples=40)
def test_reply_labels_cover_exact_declared_positions(count, data):
    descriptions = data.draw(st.lists(TEXT, min_size=count, max_size=count))
    order = data.draw(st.permutations(tuple(range(count))))
    parsed = {'names': [str(pos) for pos in range(count)], 'line2': 'Shared text', 'status': 'description_mismatch'}
    values = [{'position': position, 'description': descriptions[position]} for position in order]
    assert parse_assessment(assessment(assignments=values), parsed, project_root=REPO) == descriptions
    for invalid in (values[:-1], values + values[:1], [dict(values[0], position=count)] + values[1:]):
        with pytest.raises(ValueError):
            parse_assessment(assessment(assignments=invalid), parsed, project_root=REPO)


@given(TEXT)
def test_assignments_only_for_usable_nonempty_mismatch(description):
    assignments = [{'position': 0, 'description': description}]
    for usable, status, line in ((False, 'description_mismatch', 'Text'), (True, 'parsed', 'Text'),
                                  (True, 'description_mismatch', '')):
        parsed = {'names': ['One'], 'line2': line, 'status': status}
        assert parse_assessment(assessment(usable), parsed, project_root=REPO) is None
        with pytest.raises(ValueError):
            parse_assessment(assessment(usable, assignments), parsed, project_root=REPO)


@given(st.sampled_from(['folder_id', 'image_id', 'concept_id', 'sampled_image_ids', 'origin', 'model_revision']), TEXT)
def test_pipeline_owned_fields_are_rejected(field, payload):
    parsed = {'names': ['One'], 'line2': 'Text', 'status': 'parsed'}
    with pytest.raises(ValueError):
        parse_assessment(dict(assessment(), **{field: payload}), parsed, project_root=REPO)
    with pytest.raises(ValueError):
        parse_naming(dict(naming(), **{field: payload}), 'folder', None, project_root=REPO, separator='|')


@given(st.lists(TEXT.filter(bool), min_size=1, max_size=8))
def test_naming_has_unique_normalized_names(names):
    from prompt_enhancer.storage import concept_id
    value = naming(names)
    normalized = [trim_info_token(name) for name in names]
    if (any('\n' in name or '\r' in name for name in names)
            or any(not name or '|' in name for name in normalized)
            or len({concept_id(name) for name in normalized}) < len(names)):
        with pytest.raises(ValueError):
            parse_naming(value, 'folder', None, project_root=REPO, separator='|')
    else:
        assert parse_naming(value, 'folder', None, project_root=REPO, separator='|') == 'model'
        assert [item['name'] for item in value['concepts']] == normalized


@given(st.sampled_from(['|', '::', '※']), st.sampled_from(['', ' \t', ' Áo dài ', '\t着物\r', '\u2028猫\u2029',
                                                       'A|B', 'A::B', 'A※B', 'A\nB', 'A\rB', '\n猫']))
def test_parser_name_structure_uses_configured_separator_and_k0_trim(separator, name):
    value = naming([name])
    expected = trim_info_token(name)
    if '\n' in name or '\r' in name or not expected or separator in expected:
        with pytest.raises(ValueError):
            parse_naming(value, 'f', None, project_root=REPO, separator=separator)
    else:
        assert parse_naming(value, 'f', None, project_root=REPO, separator=separator) == 'model'
        assert value['concepts'][0]['name'] == expected


@given(st.sampled_from(['', ' \t', ' Áo dài ', '着物', 'A|B', 'A\nB', 'A\rB', '\u2028猫']), TEXT)
def test_assessment_uses_k0_declared_positions_for_structural_names(name, description):
    from prompt_enhancer.k0 import parse_info
    parsed = parse_info(name + '\nShared', '|')
    needed = parsed['status'] == 'description_mismatch' and bool(parsed['line2'])
    assignments = ([{'position': pos, 'description': description} for pos in range(len(parsed['names']))]
                   if needed else None)
    assert parse_assessment(assessment(assignments=assignments), parsed, project_root=REPO) == (
        [description] * len(parsed['names']) if needed else None)


JSON = st.recursive(st.none() | st.booleans() | st.integers() | TEXT,
                    lambda children: st.lists(children, max_size=4) | st.dictionaries(TEXT, children, max_size=4),
                    max_leaves=12)


@given(JSON)
@settings(max_examples=60)
def test_arbitrary_reply_shapes_fail_structurally(value):
    parsed = {'names': ['One'], 'line2': 'Text', 'status': 'parsed'}
    for parser, args in ((parse_assessment, (parsed,)), (parse_naming, ('folder', None))):
        try:
            parser(value, *args, project_root=REPO, **({'separator': '|'} if parser is parse_naming else {}))
        except ValueError:
            pass
        else:
            assert isinstance(value, dict)


@given(st.lists(TEXT, max_size=7), st.integers(min_value=1, max_value=12), st.integers())
def test_uniform_draw_size_membership_and_uniqueness(values, size, seed):
    drawn = uniform_draw(values, size, Random(seed))
    assert len(drawn) == min(size, len(set(values)))
    assert len(set(drawn)) == len(drawn) and set(drawn) <= set(values)
    assert drawn == uniform_draw(reversed(values), size, Random(seed))


def test_uniform_draw_every_two_element_subset_has_equal_weight():
    class ChoiceDouble(Random):
        """Labelled RNG double enumerating each equiprobable _randbelow choice path."""
        def __init__(self, choices):
            self.choices = iter(choices)

        def _randbelow(self, bound):
            value = next(self.choices)
            assert 0 <= value < bound
            return value

    counts = Counter(tuple(sorted(uniform_draw(range(4), 2, ChoiceDouble(choices))))
                     for choices in product(range(4), range(3)))
    assert len(counts) == 6 and set(counts.values()) == {2}


@given(TEXT.filter(bool), st.lists(TEXT, max_size=4), st.sampled_from(['character', 'clothing', 'place', 'object', 'style', 'other']))
def test_overrides_preserve_curated_strings(name, facts, kind):
    # YAML's own escapes preserve supplementary characters without surrogate-pair ambiguity.
    value = {name: {'concept_type': kind, 'facts': facts, 'aliases': facts, 'suppress': facts}}
    assert parse_overrides(yaml.safe_dump(value, allow_unicode=False).encode(), project_root=REPO) == value


@pytest.mark.parametrize('raw', [b'a: &a {facts: [*a]}', b'a: {unknown: value}', b'a: {facts: [1]}',
                                  b'a: {concept_type: invented}', b'a: {}\na: {}', b'A: {}\na: {}',
                                  b'a: !!python/object:os.system {}', b'[]', b'', b'? [a, b]\n: {}',
                                  b'"\\ud800\\udc00": {}'])
def test_unsafe_or_unknown_override_content_is_refused(raw):
    with pytest.raises(ConfigurationError):
        parse_overrides(raw, project_root=REPO)
