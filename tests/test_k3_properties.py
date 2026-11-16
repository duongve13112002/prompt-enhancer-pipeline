"""Bounded structural properties of card support, labels and model reply partitions."""

from copy import deepcopy
from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest

from k3_fixtures import reply_for
from prompt_enhancer.k3_cards import assemble_card, card_inputs, parse_card_reply, stability_label

ROOT = Path(__file__).resolve().parents[1]


@given(st.lists(st.integers(0, 3), min_size=1, max_size=12), st.booleans(), st.integers(1, 8))
@settings(max_examples=50)
def test_support_and_labels_ignore_order_and_duplicate_image_memberships(labels, variation, minimum):
    def values_for(order, duplicate):
        values = {}
        for index in order:
            values.setdefault(labels[index], []).extend([str(index)] * duplicate)
        return [{'value': str(key), 'image_ids': list(set(ids))} for key, ids in values.items()]
    groups = {str(index): str(index % 3) for index in range(len(labels))}
    first = stability_label(values_for(range(len(labels)), 1), groups=groups, variation=variation, minimum=minimum, ratio=0.5)
    second = stability_label(values_for(reversed(range(len(labels))), 3), groups=groups, variation=variation, minimum=minimum, ratio=0.5)
    assert first == second
    if len(set(labels)) > 1:
        assert first[0] == 'varies'
    elif len(labels) == 1:
        assert first[0] == 'single_observation'
    elif first[0] == 'stable':
        assert variation and len(labels) >= minimum and len(set(groups.values())) > 1


@given(st.lists(st.text(st.characters(blacklist_categories=('Cs',)), max_size=16), min_size=1, max_size=8))
@settings(max_examples=35)
def test_reply_partition_exact_and_owned_fields_refused(values):
    rows = [{'facet_group': 'identity', 'facet': 'identity.trait', 'value': value,
             'visibility': 'clear', 'view': 'front', 'image_id': str(index)} for index, value in enumerate(values)]
    current, payload = card_inputs(rows, [], 'Concept')
    reply = reply_for(payload)
    assert parse_card_reply(reply, payload, project_root=ROOT) == reply
    for damage in ('omit', 'repeat', 'outside', 'owned'):
        changed = deepcopy(reply)
        groups = changed['facets'][0]['groups']
        if damage == 'omit':
            groups.pop()
        elif damage == 'repeat':
            groups.append(deepcopy(groups[0]))
        elif damage == 'outside':
            groups[0]['observation_positions'][0] = len(current)
        else:
            changed['stability'] = 'stable'
        with pytest.raises(ValueError):
            parse_card_reply(changed, payload, project_root=ROOT)


@given(st.integers(2, 20), st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
def test_near_duplicate_folder_never_stable(count, ratio):
    values = [{'value': 'observed', 'image_ids': [str(i) for i in range(count)]}]
    groups = {str(i): 'one-group' for i in range(count)}
    assert stability_label(values, groups=groups, variation=True, minimum=2, ratio=ratio)[0] == 'stable_low_diversity'


def test_ratio_and_support_boundary_and_zero_visible():
    values = [{'value': 'A', 'image_ids': ['a', 'b', 'c', 'd']}]
    groups = {'a': 'one', 'b': 'one', 'c': 'two', 'd': 'two'}
    assert stability_label(values, groups=groups, variation=True, minimum=4, ratio=0.5)[0] == 'stable_low_diversity'
    assert stability_label(values, groups=groups, variation=True, minimum=4, ratio=0.49)[0] == 'stable'
    assert stability_label(values, groups=groups, variation=False, minimum=4, ratio=0.49)[0] == 'stable_low_diversity'
    assert stability_label([], groups={}, variation=False, minimum=4, ratio=0.49)[0] is None


@given(st.lists(st.integers(0, 3), min_size=1, max_size=10), st.integers(1, 3))
@settings(max_examples=35)
def test_assembled_support_counts_match_image_sets(labels, copies):
    rows = [{'facet_group': 'identity', 'facet': 'identity.trait', 'value': str(value),
             'visibility': 'partial', 'view': 'front', 'image_id': str(index), 'context_key': str(copy)}
            for index, value in enumerate(labels) for copy in range(copies)]
    current, payload = card_inputs(rows, [], 'Concept')
    reply = parse_card_reply(reply_for(payload), payload, project_root=ROOT)
    images = [{'image_id': str(index), 'near_duplicate_group': str(index)} for index in range(len(labels))]
    card = assemble_card({'concept_id': 'concept', 'display_name': 'Concept'}, current, payload, reply, [], {},
                         images, {'min_visible_images': 2, 'low_diversity_max_group_ratio': 0.5,
                                  'variant_groups': True}, {})
    facet = card['facets'][0]
    assert facet['seen_count'] == len(labels)
    assert {item['value']: item['support_count'] for item in facet['values']} == {
        str(value): labels.count(value) for value in set(labels)}
    if len(set(labels)) > 1:
        assert facet['stability'] == 'varies' and len(card['variant_groups'][0]['variants']) == len(set(labels))
