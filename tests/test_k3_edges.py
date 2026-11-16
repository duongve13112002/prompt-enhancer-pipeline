"""Cache provenance, semantic grouping and failure isolation; scripted transport evidence only."""

import asyncio
import json

import pytest

from k3_fixtures import CardServer, inputs, invoke_cards, reply_for
from prompt_enhancer.k2_naming import _digest
from prompt_enhancer.k3_cards import build_cards
from prompt_enhancer.storage import StorageError, concept_id
from prompt_enhancer.storage.identity import canonical_json, normalise_name


def test_semantic_model_grouping_and_kind_absence(k2_env):
    data = inputs(k2_env, ('long black hair', 'black hair, long', '黒い長い髪'))
    def grouped(payload, body):
        reply = reply_for(payload)
        reply['kind'] = None
        reply['facets'][0]['groups'] = [{'value': 'scripted semantic equivalence',
                                        'observation_positions': list(range(3))}]
        return reply
    card = invoke_cards(k2_env, data, CardServer(grouped))['rows']['cards'][0]
    assert card['facets'][0]['stability'] == 'stable'
    assert card['facets'][0]['values'][0]['support_count'] == 3
    assert card['kind'] == {'value': None, 'origin': 'generated'}
    assert any(item['code'] == 'KIND_NOT_GIVEN' for item in card['lint_findings'])


def test_no_variation_requires_low_diversity_and_other_facet_can_prove_variation(k2_env):
    data = inputs(k2_env, ('A', 'A', 'A'))
    for row in data['observations']:
        row['view'] = 'front'
    low = invoke_cards(k2_env, data)['rows']['cards'][0]
    assert low['facets'][0]['stability'] == 'stable_low_diversity'
    data['observations'].extend(dict(row, facet_group='place', facet='place.background', value=str(index))
                                for index, row in enumerate(list(data['observations'])))
    proven = invoke_cards(k2_env, data)['rows']['cards'][0]
    assert next(row for row in proven['facets'] if row['facet'] == 'identity.hair')['stability'] == 'stable'


def test_conflicting_contexts_of_one_image_do_not_prove_variation(k2_env):
    data = inputs(k2_env, ('A', 'A', 'A'))
    for row in data['observations']:
        row['view'] = 'front'
    first = data['observations'][0]
    data['observations'].extend(dict(first, context_key=_digest({'context': value}),
                                    facet_group='place', facet='place.background', value=value)
                                for value in ('one', 'two'))
    result = invoke_cards(k2_env, data)
    trait = next(row for row in result['rows']['cards'][0]['facets'] if row['facet'] == 'identity.hair')
    assert trait['stability'] == 'stable_low_diversity'


def test_bad_concept_does_not_discard_good_card_and_unknown_override_linted(k2_env):
    data = inputs(k2_env)
    cid = concept_id('Other')
    data['handoff']['concepts'].append({'concept_id': cid, 'display_name': 'Other', 'normalised_name': normalise_name('Other'),
                                      'kind': None, 'kind_origin': None})
    data['handoff']['folder_concepts'].append(dict(data['handoff']['folder_concepts'][0], concept_id=cid,
                                                  position=1, name_as_written='Other'))
    data['observations'].extend(dict(row, concept_id=cid) for row in list(data['observations']))
    data['overrides']['data']['Unknown'] = {'facts': ['Not created']}
    def scripted(payload, body):
        return {'forged': True} if payload['concept_name'] == 'Other' else reply_for(payload)
    result = invoke_cards(k2_env, data, CardServer(scripted))
    assert len(result['rows']['cards']) == 1 and cid in result['report']['failed']
    assert any(row['code'] == 'UNMATCHED_OVERRIDE' for row in result['report']['lint_findings'])


@pytest.mark.parametrize('damage', ['reply', 'key', 'payload', 'record'])
def test_checksumming_wrong_bundle_does_not_make_it_trusted(k2_env, damage):
    data = inputs(k2_env)
    server = CardServer()
    invoke_cards(k2_env, data, server)
    with k2_env.store.writer() as writer:
        checkpoints = writer.checkpoints('k3_grouping')
        state = next(iter(checkpoints._rows.values()))
        key = state['input_key']
        bundle = json.loads(checkpoints.load(key))
        if damage == 'reply':
            bundle['reply']['facets'][0]['groups'] = []
        elif damage == 'key':
            bundle['key'] = _digest('wrong-key')
        elif damage == 'payload':
            bundle['payload']['concept_name'] = 'Wrong'
        else:
            bundle['record']['model_revision'] = 'forged'
        revisions = {'model': state['model_revision'], 'prompt': state['prompt_revision'], 'schema': state['schema_revision']}
        checkpoints.commit(key, 'k3_grouping', canonical_json(bundle).encode(), revisions=revisions,
                           call_records=state['call_records'])
    with pytest.raises(StorageError, match='completed bundle'):
        invoke_cards(k2_env, data, server)
    assert len(server.requests) == 1


def test_current_provenance_reuses_grouping_and_curated_text_invalidates(k2_env):
    data = inputs(k2_env)
    server = CardServer()
    invoke_cards(k2_env, data, server)
    data['observations'][0]['context_key'] = _digest('new-current-context')
    invoke_cards(k2_env, data, server)
    data['overrides']['file_hash'] = 'new-override-hash'
    invoke_cards(k2_env, data, server)
    assert len(server.requests) == 1
    data['handoff']['folder_concepts'][0]['description'] = 'Changed human statement'
    invoke_cards(k2_env, data, server)
    assert len(server.requests) == 2


def test_store_configuration_overrides_are_consumed_or_refused(k2_env):
    from prompt_enhancer.config import ConfigurationError
    from prompt_enhancer.storage import open_knowledge_store
    data = inputs(k2_env)
    server = CardServer()
    k2_env.config['storage']['kb_store'] += '-alternate'
    k2_env.config['storage']['table_format'] = 'arrow'
    k2_env.config['storage']['lock'] = {'timeout_s': 2.0, 'poll_s': 0.02}
    k2_env.config['sync']['resume'] = False
    k2_env.store = open_knowledge_store(k2_env.config, 'kb sync', project_root=k2_env.root)
    first = invoke_cards(k2_env, data, server)
    assert first['rows']['cards'] and all(k2_env.store.path(path).exists() for path in first['artifacts'])
    invoke_cards(k2_env, data, server)
    assert len(server.requests) == 2  # resume=false and no snapshot references: model work is redone.
    assert k2_env.store.timeout_s == 2.0 and k2_env.store.poll_s == 0.02
    assert k2_env.store.table_format == 'arrow'
    k2_env.config['storage']['lock']['timeout_s'] = 3.0
    with pytest.raises(ConfigurationError, match='does not match'):
        invoke_cards(k2_env, data, server)


def test_card_stage_concurrency_obeys_selected_backend_limit(k2_env):
    data = inputs(k2_env)
    cid = concept_id('Other')
    data['handoff']['concepts'].append({'concept_id': cid, 'display_name': 'Other', 'normalised_name': 'other',
                                      'kind': None, 'kind_origin': None})
    data['handoff']['folder_concepts'].append(dict(data['handoff']['folder_concepts'][0], concept_id=cid,
                                                  position=1, name_as_written='Other'))
    data['observations'].extend(dict(row, concept_id=cid) for row in list(data['observations']))
    class CapacityDouble:
        """Labelled async call double; scheduler overlap is observed without sockets or inference."""
        def __init__(self):
            self.active = self.peak = 0
        async def for_role(self, name):
            assert name == 'card_builder'
            return self
        async def call(self, prompt, text):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return reply_for(json.loads(text)), {'model_revision': 'capacity-double', 'status': 'OK'}
    for limit in (1, 2):
        k2_env.config['vlm']['backends']['main']['max_concurrency'] = limit
        # Capacity alone reuses replies; submit fresh semantic work for each scheduler measurement.
        for row in data['observations']:
            row['value'] += ':' + str(limit)
        client = CapacityDouble()
        result = asyncio.run(build_cards(k2_env.store, k2_env.config, client=client, **data))
        assert not result['report']['failed'] and client.active == 0 and client.peak == limit
