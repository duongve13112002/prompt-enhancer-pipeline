"""Unpublished K3 cards: models supply meaning; code owns evidence, counts and labels (R-010)."""

from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from .config import ConfigurationError, _lexical_path, _resolve_within, _schema, validate_card_reads
from .k2_naming import _digest, _validate_reply, failure_code, record_probe_failure
from .model_client import (MODEL_CALL_REVISION, ModelCallError, ModelClientRun, run_concurrency,
                           semantic_role_settings)
from .model_resources import CapacityLog, await_owned, flow
from .profile_prompt import load_prompt
from .storage import StorageError, artifact_key, concept_id
from .storage.identity import canonical_json
from .storage.store import check_snapshot_id
from .storage.tables import SPECS


# Artifact/protocol-bound schema and checkpoint identities, not fitted operating values.
REPLY_SCHEMA = 'card_builder_reply.v0'
STORED_SCHEMA = 'concept_card.v0'
LAYOUT = 1
# declared revision of the code that shapes a K3 grouping bundle or card (KBS-S3-1); keys the artifacts in place of a
# hash of module source. Bump it with any edit to a function its guard pins (tests/test_stage_revisions.py).
K3_CARD_REVISION = 'k3-card-2'
VISIBLE = frozenset(('clear', 'partial'))


def card_inputs(observations, curated, name):
    """Canonical positions expose no pipeline IDs; exact duplicate evidence cannot multiply support."""
    # Provenance cannot reorder model-visible positions; equal semantic rows use provenance only to break ties.
    def order(row):
        return (canonical_json({key: row[key] for key in ('facet_group', 'facet', 'value', 'visibility', 'view')}),
                canonical_json(row))

    rows = sorted({canonical_json(row): row for row in observations}.values(), key=order)
    facets = sorted({(row['facet_group'], row['facet']) for row in rows})
    positions = {facet: index for index, facet in enumerate(facets)}
    payload = {'concept_name': name,
               'facets': [{'facet_position': index, 'facet_group': group, 'facet': facet}
                          for index, (group, facet) in enumerate(facets)],
               'observations': [{'observation_position': index,
                                 'facet_position': positions[(row['facet_group'], row['facet'])],
                                 **{field: row[field] for field in ('value', 'visibility', 'view')}}
                                for index, row in enumerate(rows)],
               'curated_statements': [{'curated_position': index, 'text': item['text'], 'origin': item['origin']}
                                      for index, item in enumerate(curated)]}
    return rows, payload


def _partition(groups, expected):
    actual = [position for group in groups for position in group['observation_positions']]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError('value/view groups must partition exactly the supplied positions')


def parse_card_reply(value, payload, *, project_root):
    """Validate structure and exact evidence domains, even for an injected transport double."""
    _validate_reply(project_root, REPLY_SCHEMA, value)
    canonical_json(value).encode('utf-8')
    facets, observations, curated = (payload[key] for key in ('facets', 'observations', 'curated_statements'))
    positions = [item['facet_position'] for item in value['facets']]
    if sorted(positions) != list(range(len(facets))):
        raise ValueError('reply must cover every facet position exactly once')
    for facet in value['facets']:
        expected = {row['observation_position'] for row in observations
                    if row['facet_position'] == facet['facet_position'] and row['visibility'] in VISIBLE}
        _partition(facet['groups'], expected)
    _partition(value['view_groups'], range(len(observations)))
    for finding in value['lint_findings']:
        for field, count in (('facet_positions', len(facets)), ('observation_positions', len(observations)),
                             ('curated_positions', len(curated))):
            if any(position >= count for position in finding[field]):
                raise ValueError(f'lint {field} references an unknown position')
    return value


def stability_label(values, *, groups, variation, minimum, ratio):
    """Invariance policy v0, over distinct supporting images; minority evidence always survives."""
    images = {identity for value in values for identity in value['image_ids']}
    if not images:
        return None, 'no visible evidence'
    if len(values) > 1:
        return 'varies', None
    if len(images) == 1:
        return 'single_observation', None
    if len(images) < minimum:
        return 'stable_low_diversity', 'insufficient visible images'
    if not variation:
        return 'stable_low_diversity', 'no independent variation on other facets or views'
    independent = {groups[identity] for identity in images}
    if len(independent) < 2 or len(independent) / len(images) <= ratio:
        return 'stable_low_diversity', 'near-duplicate diversity ratio is at or below the configured maximum'
    return 'stable', None


def _has_variation(groups, rows, support):
    """Only unambiguous per-image assignments can establish variation across images."""
    assignments = {}
    for index, group in enumerate(groups):
        for position in group['observation_positions']:
            row = rows[position]
            if row['image_id'] in support and row['visibility'] in VISIBLE:
                assignments.setdefault(row['image_id'], set()).add(index)
    return len({next(iter(values)) for values in assignments.values() if len(values) == 1}) > 1


def assemble_card(concept, rows, payload, reply, curated, override, images, cards, revisions):
    """Map validated positions to trusted identities and derive all stored evidence fields."""
    by_facet = {item['facet_position']: item['groups'] for item in reply['facets']}
    group_map = {row['image_id']: row['near_duplicate_group'] for row in images}
    facets, variants, findings = [], [], []
    suppress = set(override.get('suppress', []))

    def evidence_ref(position):
        row = rows[position]
        return 'observation:' + _digest(row)

    for item in payload['facets']:
        position, facet = item['facet_position'], item['facet']
        if facet in suppress:
            continue
        groups = by_facet[position]
        values = [{'value': group['value'], 'image_ids': sorted({rows[index]['image_id']
                  for index in group['observation_positions']})} for group in groups]
        values.sort(key=canonical_json)
        for value in values:
            value['support_count'] = len(value['image_ids'])
        support = {identity for value in values for identity in value['image_ids']}
        # Other-facet variation counts only evidence from the images supporting this facet.
        variation = any(_has_variation(other_groups, rows, support)
                        for other_position, other_groups in by_facet.items()
                        if payload['facets'][other_position]['facet'] not in suppress)
        variation = variation or _has_variation(reply['view_groups'], rows, support)
        label, reason = stability_label(values, groups=group_map, variation=variation,
                                        minimum=cards['min_visible_images'],
                                        ratio=cards['low_diversity_max_group_ratio'])
        if label is None:
            findings.append({'code': 'VISIBILITY_GAP', 'message': facet + ': ' + reason, 'refs': [facet]})
            continue
        facets.append({'facet_group': item['facet_group'], 'facet': facet, 'seen_count': len(support),
                       'values': values, 'stability': label, 'origin': 'generated'})
        if reason:
            findings.append({'code': 'LOW_DIVERSITY', 'message': facet + ': ' + reason, 'refs': [facet]})
        if label == 'varies' and cards['variant_groups']:
            variants.append({'group_id': 'group:' + _digest({'concept': concept['concept_id'], 'facet': facet}),
                             'facet': facet, 'variants': [
                                 {'variant_id': 'variant:' + _digest({'facet': facet, 'value': value}),
                                  'value': value['value'], 'image_ids': value['image_ids']} for value in values]})
    for item in reply['lint_findings']:
        refs = [payload['facets'][index]['facet'] for index in item['facet_positions']]
        refs.extend(evidence_ref(index) for index in item['observation_positions'])
        refs.extend('curated:' + _digest(curated[index]) for index in item['curated_positions'])
        findings.append({'code': 'model:' + item['code'], 'message': item['message'], 'refs': sorted(set(refs))})
    kind = override.get('concept_type', reply['kind'])
    if kind is None:
        findings.append({'code': 'KIND_NOT_GIVEN', 'message': 'No supported concept kind was given', 'refs': []})
    origin = 'override' if 'concept_type' in override else 'generated'
    return {'concept_id': concept['concept_id'], 'name': concept['display_name'],
            'kind': {'value': kind, 'origin': origin}, 'summary': reply['summary'],
            'facets': facets, 'variant_groups': variants, 'curated_statements': deepcopy(curated),
            'lint_findings': findings, 'revisions': revisions}


def _grouping_bundle(revisions, payload, key, reply, record):
    """The stored layout of one completed grouping call."""
    return {'version': LAYOUT, 'revisions': revisions, 'payload': payload, 'key': key, 'reply': reply,
            'record': record}


def _card_revisions(record, role, config, snapshot_id):
    """The provenance block stored in every card."""
    return {'model_revision': record['model_revision'], 'prompt_id': role['prompt_id'],
            'facet_schema_version': config['observe']['facet_schema_version'],
            'stability_policy_version': config['cards']['stability_policy']['version'],
            'snapshot_id': snapshot_id}


def _curated(concept, handoff, override):
    statements = []
    folders = {row['folder_id']: row for row in handoff['folders']}
    for member in sorted(handoff['folder_concepts'], key=lambda row: (row['folder_id'], row['position'])):
        if member['concept_id'] != concept['concept_id']:
            continue
        text = (member['description'] if member['description_origin'] == 'info_file'
                else folders[member['folder_id']]['declared_descriptions'])
        if text:
            statement = {'text': text, 'origin': 'info_file', 'folder_id': member['folder_id']}
            if statement not in statements:
                statements.append(statement)
    statements.extend({'text': text, 'origin': 'override', 'folder_id': None} for text in override.get('facts', []))
    return statements


async def build_cards(store, config, handoff, *, observations, images, folder_images, snapshot_id,
                      overrides, client=None, probe_cases=None, local_probe_plans=None, writer=None):
    """Return current cards/concept kinds, lint and artifact references; never publish or read source files.

    handoff must be the effective P1 naming rows, overrides its effective_overrides, and observations the
    current P2 output. The caller owns the target snapshot ID and any injected model client.
    Injected run doubles expose `.concurrency: BackendConcurrency` and async `for_role`; bound doubles expose `.run`.
    """
    root = store.catalog.root
    validate_card_reads(config, project_root=root)
    check_snapshot_id(snapshot_id)
    forbidden = {'.secrets', 'models', '.git', '.venv'}

    def resolved(key):
        return _resolve_within(_lexical_path(config[key.split('.')[0]][key.split('.')[1]], root, root, key),
                               Path(root), forbidden, Path(root), key, links=key == 'knowledge.root')  # read only (R-014)

    source, target = resolved('knowledge.root'), resolved('storage.kb_store')
    if Path(store.root).resolve() != target:
        raise ConfigurationError('storage.kb_store: stage store does not match configured root')
    for key, actual, configured in (
        ('storage.lock.timeout_s', store.timeout_s, config['storage']['lock']['timeout_s']),
        ('storage.lock.poll_s', store.poll_s, config['storage']['lock']['poll_s']),
        ('storage.table_format', store._table_format, config['storage']['table_format']),
        ('sync.resume', store._resume, config['sync']['resume']),
    ):
        if actual != configured:
            raise ConfigurationError(f'{key}: stage store does not match configured value')
    for destination in (target, resolved('storage.data_root')):
        if source == destination or source in destination.parents or destination in source.parents:
            raise ConfigurationError('storage path overlaps knowledge.root')
    for table in ('folders', 'concepts', 'folder_concepts'):
        store.catalog.build(table, handoff[table])
    store.catalog.build('images', images)
    store.catalog.build('folder_images', folder_images)
    _validate_reply(root, 'kb_overrides.v0', overrides['data'])
    canonical_json(overrides).encode('utf-8')
    # Exact duplicate rows are harmless; different rows with the same primary key remain invalid.
    current = sorted({canonical_json(row): row for row in observations}.values(), key=canonical_json)
    store.catalog.build('observations', current)
    concepts = {row['concept_id']: deepcopy(row) for row in handoff['concepts']}
    by_image = {row['image_id']: row for row in images}
    members = {}
    folders = {row['folder_id'] for row in handoff['folders']}
    for member in handoff['folder_concepts']:
        if member['concept_id'] not in concepts or member['folder_id'] not in folders:
            raise ValueError('card membership references an unknown concept or folder')
        members.setdefault(member['folder_id'], set()).add(member['concept_id'])
    eligible = set()
    for member in folder_images:
        if member['folder_id'] not in folders or member['image_id'] not in by_image:
            raise ValueError('card image membership references an unknown image or folder')
        eligible.update((member['image_id'], identity) for identity in members.get(member['folder_id'], ()))
    for row in current:
        if (row['image_id'], row['concept_id']) not in eligible:
            raise ValueError('observation references an image outside the current concept membership')
        if row['facet_schema_version'] != config['observe']['facet_schema_version']:
            raise ValueError('observations mix incompatible facet schema revisions')
        if row['visibility'] in VISIBLE and not by_image[row['image_id']]['near_duplicate_group']:
            raise ValueError('visible evidence requires a current near_duplicate_group')
    role = config['vlm']['roles']['card_builder']
    prompt = load_prompt(root, role['prompt_id'])
    backends = config['vlm']['backends']
    revisions = {'model': _digest(semantic_role_settings(config, 'card_builder')),
                 'prompt': _digest({'id': role['prompt_id'], 'sha256': hashlib.sha256(prompt).hexdigest()}),
                 'schema': _digest({'layout': LAYOUT, 'reply': _schema(root, REPLY_SCHEMA),
                                   'configured': _schema(root, role['output']['schema_id']),
                                   'stage_revision': K3_CARD_REVISION, 'model_call': MODEL_CALL_REVISION})}
    effective = {concept_id(name): value for name, value in overrides['data'].items()}
    unknown = [name for name in overrides['data'] if concept_id(name) not in concepts]
    report = {'built': 0, 'reused_grouping': 0, 'failed': {}, 'lint_findings': [], 'unmatched_overrides': sorted(unknown)}
    artifacts, result = [], []
    owned_run = None
    by_concept = {}  # grouped once, in `current` order, instead of a filter per concept
    for row in current:
        by_concept.setdefault(row['concept_id'], []).append(row)
    capacity = CapacityLog()
    if client is not None:
        capacity.watch(run_concurrency(client))
    try:
        with nullcontext(writer) if writer is not None else store.writer() as writer:
            grouping = writer.checkpoints('k3_grouping')
            documents = writer.checkpoints('k3_card')

            async def evaluate(concept):
                nonlocal owned_run
                identity = concept['concept_id']
                override = effective.get(identity, {})
                curated = _curated(concept, handoff, override)
                rows, payload = card_inputs(by_concept.get(identity, []), curated, concept['display_name'])
                payload['suppressed_facets'] = override.get('suppress', [])
                key = artifact_key('k3_grouping', _digest({'identity': 'card_payload_v1', 'payload': payload}), revisions)
                record = None
                try:
                    if grouping.done(key) or grouping.reusable(key):
                        try:
                            bundle = json.loads(grouping.load(key))
                            if (bundle['version'] != LAYOUT or bundle['revisions'] != revisions
                                    or bundle['payload'] != payload or bundle['key'] != key
                                    or grouping.state(key)['call_records'] != [bundle['record']]):
                                raise ValueError('completed bundle does not match current input or call record')
                            reply = parse_card_reply(bundle['reply'], payload, project_root=root)
                            record = bundle['record']
                        except (ValueError, KeyError, TypeError, UnicodeError) as error:
                            raise StorageError('k3_grouping: CACHE_INVALID: invalid completed bundle') from None
                        report['reused_grouping'] += 1
                    else:
                        if client is None:
                            if owned_run is None:
                                owned_run = ModelClientRun(config, root, probe_cases=probe_cases,
                                                           local_probe_plans=local_probe_plans)
                                capacity.watch(owned_run.concurrency)
                            bound = await owned_run.for_role('card_builder')
                        else:
                            bound = await client.for_role('card_builder')
                        value, record = await bound.call(prompt, canonical_json(payload))
                        if value is None:
                            raise ValueError(record['status'])
                        reply = parse_card_reply(value, payload, project_root=root)
                        bundle = _grouping_bundle(revisions, payload, key, reply, record)
                        grouping.commit(key, 'k3_grouping', canonical_json(bundle).encode('utf-8'),
                                        revisions=revisions, call_records=[record])
                    artifact = grouping.state(key)['output_key']
                    card_revisions = _card_revisions(record, role, config, snapshot_id)
                    card = assemble_card(concept, rows, payload, reply, curated, override, images,
                                         config['cards'], card_revisions)
                    _validate_reply(root, STORED_SCHEMA, card)
                    store.catalog.build('cards', [card])
                    document_revisions = dict(revisions, schema=_digest({'contract': _schema(root, STORED_SCHEMA),
                                                                         'layout': LAYOUT,
                                                                         'tool': revisions['schema']}))
                    document_key = artifact_key('k3_card', _digest(card), document_revisions)
                    if documents.done(document_key) or documents.reusable(document_key):
                        if json.loads(documents.load(document_key)) != card:
                            raise StorageError('k3_card: completed document disagrees with derived evidence')
                    else:
                        documents.commit(document_key, 'k3_card', canonical_json(card).encode('utf-8'),
                                         revisions=document_revisions)
                    artifacts.extend((artifact, documents.state(document_key)['output_key']))
                    concept.update(kind=card['kind']['value'],
                                   kind_origin=card['kind']['origin'] if card['kind']['value'] is not None else None)
                    result.append(card)
                    report['built'] += 1
                    if config['cards']['lint_report']:
                        report['lint_findings'].extend(dict(finding, concept_id=identity) for finding in card['lint_findings'])
                except (ValueError, TypeError, UnicodeError, ModelCallError) as error:
                    record_probe_failure(writer, key, error, revisions)
                    if record is not None:
                        writer.checkpoints('k3_call_failure').commit(
                            key, 'k3_call_failure', canonical_json({'error': failure_code(error)}).encode('utf-8'),
                            revisions=revisions, call_records=[record])
                    grouping.fail(key, failure_code(error), revisions=revisions)
                    report['failed'][identity] = failure_code(error)

            ordered = [concepts[key] for key in sorted(concepts)]
            # Per-item flow (decision 149); a fatal error drains the cards in flight before it is raised.
            await flow(ordered, evaluate, backends[role['backend']]['max_concurrency'])
    finally:
        try:
            report['capacity'] = capacity.close()
        finally:
            if owned_run is not None:
                await await_owned(owned_run.close())
    report['failed'] = dict(sorted(report['failed'].items()))
    report['lint_findings'].extend({'code': 'UNMATCHED_OVERRIDE', 'message': name, 'refs': []}
                                   for name in unknown if config['cards']['lint_report'])
    report['lint_findings'].extend({'code': 'UNMATCHED_FOLDER_OVERRIDE', 'message': path, 'refs': []}
                                   for path in overrides.get('unmatched_folders', []) if config['cards']['lint_report'])
    if config['cards']['lint_report']:
        report['lint_findings'].extend(deepcopy(overrides.get('lint_findings', [])))
    report['lint_findings'].sort(key=canonical_json)
    return {'rows': {'cards': sorted(result, key=lambda card: card['concept_id']),
                     'concepts': [concepts[key] for key in sorted(concepts)]},
            'report': report, 'artifacts': sorted(set(artifacts))}
