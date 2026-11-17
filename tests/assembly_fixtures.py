"""Synthetic S3 inputs (test-only): cards, Intent IRs, plan results and a labelled snapshot double."""

from copy import deepcopy
from types import SimpleNamespace

from intake_fixtures import configuration
from prompt_enhancer.intake import IntakeResult
from prompt_enhancer.storage import concept_id, image_id

KIMONO, DONUT = concept_id('Kimono'), concept_id('Donut')
IMG = [image_id(f'assembly image {index}'.encode()) for index in range(6)]  # test-only image identities
REVISIONS = {'model_revision': 'card-r1', 'prompt_id': 'card_v0', 'facet_schema_version': 'f1',
             'stability_policy_version': 'p1', 'snapshot_id': 'test-only'}


def facet(name, values, stability='stable', group=None):
    """A card facet; `values` maps value text to its supporting image ids."""
    group = group or name.partition('.')[0]
    return {'facet_group': group, 'facet': name, 'seen_count': len({i for ids in values.values() for i in ids}),
            'values': [{'value': value, 'support_count': len(ids), 'image_ids': list(ids)}
                       for value, ids in values.items()],
            'stability': stability, 'origin': 'generated'}


def variant_group(concept, name, values):
    """A K3-style variant group over the values of a `varies` facet (identities are test-only strings)."""
    return {'group_id': f'group:{concept[-6:]}:{name}', 'facet': name,
            'variants': [{'variant_id': f'variant:{name}:{value}', 'value': value, 'image_ids': list(ids)}
                         for value, ids in values.items()]}


def card(concept, name, facets=(), variants=(), curated=()):
    return {'concept_id': concept, 'name': name, 'kind': {'value': 'clothing', 'origin': 'generated'},
            'facets': list(facets), 'variant_groups': list(variants),
            'curated_statements': [{'text': text, 'origin': 'info_file', 'folder_id': name} for text in curated],
            'lint_findings': [], 'revisions': dict(REVISIONS), 'summary': f'card {name}'}


def entity(entity_id, mention, attributes=()):
    return {'id': entity_id, 'kind': 'person', 'count': 1, 'mention': mention,
            'attributes': [{'facet': name, 'value': value, 'source': source} for name, value, source in attributes],
            'action': None, 'relations': [], 'source': 'user'}


def link(concept, role, entity_id=None, status='resolved', mention='kimono'):
    return {'mention': mention, 'entity_id': entity_id, 'status': status, 'role': role,
            'concept_id': concept if status != 'no_match' else None,
            'candidates': [] if concept is None else [{'concept_id': concept, 'context_fit': 'fits'}],
            'needs_lookup': False}


def ir(links=(), needs=(), entities=(), negations=(), scene=(), task='generate'):
    """`needs` is (concept, [facets]); `negations` the entity ids a negation constraint binds."""
    return {'task': task, 'edit_kind': 'local_edit' if task == 'edit' else None, 'entities': list(entities),
            'scene': {'location': None, 'medium': None,
                      'attributes': [{'facet': name, 'value': value, 'source': source}
                                     for name, value, source in scene]},
            'concept_links': list(links), 'rejected_candidates': [],
            'knowledge_needs': [{'concept_id': concept, 'facets': list(facets)} for concept, facets in needs],
            'constraint_ledger': {'constraints': [
                {'id': f'n{index}', 'type': 'negation', 'text': 'no item', 'span': 'no', 'bound_entity': bound}
                for index, bound in enumerate(negations)]},
            'literal_texts': [], 'passthrough_tokens': [], 'edit_targets': [], 'delta': [], 'preserve': [],
            'dependent_effects': [], 'unresolved': [], 'needs_clarification': False,
            'clarification_candidates': []}


def plan(intent, ratings=(), state='ready'):
    return {'state': state, 'intent_ir': intent, 'selections': [], 'variant_ratings': list(ratings),
            'warnings': [], 'calls': [], 'error': None}


def rating(link_index, group, named=None, fits=()):
    """Ratings for every variant of `group`: listed ids `fits`, the rest `conflicts`; none when `named` is set."""
    rated = [] if named is not None else [
        {'id': variant['variant_id'], 'context_fit': 'fits' if variant['variant_id'] in fits else 'conflicts'}
        for variant in group['variants']]
    return {'link_index': link_index, 'concept_id': 'concept:x', 'variant_group': group['group_id'],
            'named_variant_id': named, 'ratings': rated}


class SnapshotDouble:
    """Labelled double of a pinned snapshot: serves given rows by table name and records each read."""

    def __init__(self, tables, snapshot_id='snapshot-test'):
        self.tables, self.snapshot_id, self.reads = tables, snapshot_id, []

    def read_rows(self, name):
        self.reads.append(name)
        value = self.tables[name]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def release(self):
        pass


def tables(cards=(), images=(), observations=(), folder_images=(), folder_concepts=()):
    return {'cards': list(cards), 'images': list(images), 'observations': list(observations),
            'folder_images': list(folder_images), 'folder_concepts': list(folder_concepts)}


def image_row(image, group=None, decode='ok'):
    return {'image_id': image, 'size_bytes': 1, 'format': 'png', 'width': 2, 'height': 2,
            'decode_status': decode, 'near_duplicate_group': group}


def observation(image, concept, name, value, visibility='clear', quality=0.5):
    return {'context_key': f'ctx-{image}-{name}', 'image_id': image, 'concept_id': concept, 'facet': name,
            'model_revision': 'obs-r1', 'prompt_id': 'observer_v0', 'facet_schema_version': 'f1',
            'facet_group': name.partition('.')[0], 'value': value, 'visibility': visibility, 'region': None,
            'view': 'front', 'quality': quality, 'origin': 'generated'}


def assembly_config(model_test_profile, tmp_path):
    config = configuration(model_test_profile, tmp_path)
    config['rag']['enabled'] = True
    config['assembly'].update(max_facts_per_concept=10, max_facts_per_request=20, max_images_per_concept=2)
    config['edit']['max_kb_reference_images'] = 3  # labelled test-only limits
    return config


def intake_view(config, snapshot, *, task='generate', outcome='ready'):
    """A ready intake result holding `snapshot` as its pin (test-only, no store)."""
    payload = {'outcome': outcome, 'request_id': 'request-test', 'prompt': 'kimono', 'task': task,
               'effective_config': config, 'config_hash': 'test-only-hash',
               'retrieval': ({'state': 'pinned', 'snapshot_id': snapshot.snapshot_id} if snapshot is not None
                             else {'state': 'failed', 'snapshot_id': None})}
    knowledge = None if snapshot is None else SimpleNamespace(snapshot=snapshot, close=snapshot.release)
    return IntakeResult(payload, knowledge=knowledge)
