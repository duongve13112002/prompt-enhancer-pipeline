"""Properties for K2 numeric grouping and untrusted observation parsing; synthetic inputs only."""

from copy import deepcopy

from hypothesis import given, settings, strategies as st
import pytest

from k2_observe_fixtures import observation
from prompt_enhancer.k2_observe import group_images, parse_observation
from prompt_enhancer.storage import concept_id, image_id


vectors = st.lists(st.tuples(st.integers(-10, 10), st.integers(-10, 10)).filter(lambda v: any(v)),
                  min_size=1, max_size=8)


def test_zero_distance_groups_identical_nonunit_vectors():
    ids = [image_id(b'first'), image_id(b'second')]
    images = [{'image_id': identity, 'decode_status': 'ok'} for identity in ids]
    rows = [{'image_id': identity, 'embedder_revision': 'r', 'dim': 2, 'vector': [1, 2]} for identity in ids]
    groups = group_images(images, rows, 0, revision='r', dim=2)
    assert len(set(groups.values())) == 1


@given(vectors, st.integers(1, 30), st.floats(min_value=0, max_value=2, allow_nan=False))
@settings(max_examples=45, deadline=None)
def test_grouping_partition_is_permutation_scale_and_threshold_invariant(values, scale, distance):
    ids = [image_id(str(index).encode()) for index in range(len(values))]
    images = [{'image_id': identity, 'decode_status': 'ok'} for identity in ids]
    rows = [{'image_id': identity, 'vector': list(vector), 'embedder_revision': 'r', 'dim': 2}
            for identity, vector in zip(ids, values)]
    grouped = group_images(images, rows, distance, revision='r', dim=2)
    assert grouped == group_images(images[::-1], rows[::-1], distance, revision='r', dim=2)
    scaled = [dict(row, vector=[v * scale for v in row['vector']]) for row in rows]
    assert grouped == group_images(images, scaled, distance, revision='r', dim=2)
    wider = group_images(images, rows, min(2, distance + 0.1), revision='r', dim=2)
    for left in ids:
        for right in ids:
            if grouped[left] == grouped[right]:
                assert wider[left] == wider[right]


@given(st.one_of(st.integers(max_value=-1), st.integers(min_value=1), st.booleans(), st.text(), st.none()))
@settings(max_examples=40, deadline=None)
def test_parser_refuses_every_out_of_set_or_noninteger_position(project_root, position):
    value = observation()
    value['observations'][0]['concept_position'] = position
    with pytest.raises(ValueError):
        parse_observation(value, image_id=image_id(b'fixture'), concepts=[{'concept_id': concept_id('A')}],
            context_key='a' * 64, model_revision='r', prompt_id='p', facet_schema_version='image_observation.v0',
            project_root=project_root)


@given(st.text().filter(lambda s: all(not 0xD800 <= ord(c) <= 0xDFFF for c in s)))
@settings(max_examples=35, deadline=None)
def test_parser_planted_text_is_only_literal_data_and_concepts_remain_distinct(project_root, planted):
    value = observation((0, 1), text=planted, facet='text_in_image.verbatim')
    concepts = [{'concept_id': concept_id('A')}, {'concept_id': concept_id('B')}]
    result = parse_observation(value, image_id=image_id(b'fixture'), concepts=concepts,
        context_key='a' * 64, model_revision='r', prompt_id='p', facet_schema_version='image_observation.v0',
        project_root=project_root)
    assert [item['concept_id'] for item in result['observations']] == [concept_id('A'), concept_id('B')]
    assert all(item['value'] == planted for item in result['observations'])
    forged = deepcopy(value)
    forged['origin'] = {'model_revision': planted}
    with pytest.raises(ValueError):
        parse_observation(forged, image_id=image_id(b'fixture'), concepts=concepts,
            context_key='a' * 64, model_revision='r', prompt_id='p', facet_schema_version='image_observation.v0',
            project_root=project_root)
