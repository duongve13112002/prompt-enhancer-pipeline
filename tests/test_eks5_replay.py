"""Labelled recorded model replies, replayed on CPU without model calls."""

import json
from pathlib import Path
from copy import deepcopy

import pytest
from hypothesis import given, settings, strategies as st

from prompt_enhancer.k2_observe import parse_observation
from prompt_enhancer.storage import concept_id, image_id


def parse(value, root):
    return parse_observation(value, image_id=image_id(b'replay'),
        concepts=[{'concept_id': concept_id('A')}, {'concept_id': concept_id('B')}],
        context_key='a' * 64, model_revision='replay', prompt_id='observer_v1',
        facet_schema_version='image_observation.v0', project_root=root)


@pytest.mark.parametrize('name', [' ', '\t', '\u3000'])
def test_blank_facet_names_are_refused(project_root, name):
    reply = {'view': 'v', 'quality': 1, 'observations': [{'concept_position': 0, 'facet_group': 'style',
        'facet': name, 'value': 'v', 'visibility': 'clear', 'region': None}]}
    with pytest.raises(ValueError, match='SCHEMA_INVALID: facet'):
        parse(reply, project_root)


@pytest.mark.parametrize('index', range(3))
def test_recorded_reply_is_accepted_as_unqualified_name(project_root, index):
    replies = json.loads((Path(__file__).parent / 'fixtures/eks5_smoke_replies.json').read_text(encoding='utf-8'))
    reply = replies[index]
    result = parse(reply, project_root)
    for source, stored in zip(reply['observations'], result['observations']):
        assert stored['facet'] == source['facet_group'] + '.' + source['facet']


@given(st.sampled_from(['identity', 'apparel', 'object', 'place', 'style', 'incidental', 'text_in_image']),
       st.text(max_size=20).filter(lambda s: all(not 0xD800 <= ord(c) <= 0xDFFF for c in s)))
@settings(max_examples=100, deadline=None)
def test_facet_name_qualification_or_refusal(project_root, group, name):
    reply = {'view': 'v', 'quality': 1, 'observations': [{'concept_position': 0, 'facet_group': group,
        'facet': name, 'value': 'v', 'visibility': 'clear', 'region': None}]}
    if not name.strip() or '.' in name:
        with pytest.raises(ValueError):
            parse(reply, project_root)
    else:
        assert parse(reply, project_root)['observations'][0]['facet'] == group + '.' + name


def test_prefix_related_groups_are_kept_distinct(project_root, monkeypatch):
    import prompt_enhancer.k2_naming as naming
    original = naming._schema
    def schema(root, schema_id):
        value = deepcopy(original(root, schema_id))
        if schema_id in ('observer_reply.v1', 'image_observation.v0'):
            value['properties']['observations']['items']['properties']['facet_group']['enum'] = ['组', '组.long']
        return value
    monkeypatch.setattr(naming, '_schema', schema)
    reply = {'view': 'v', 'quality': 1, 'observations': [
        {'concept_position': 0, 'facet_group': group, 'facet': '名称', 'value': 'v',
         'visibility': 'clear', 'region': None} for group in ('组', '组.long')]}
    assert [item['facet'] for item in parse(reply, project_root)['observations']] == ['组.名称', '组.long.名称']
