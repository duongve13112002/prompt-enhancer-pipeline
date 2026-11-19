"""S3: assemble the Knowledge Pack from the pinned snapshot's cards, as data, with no model call (R-008, R-009, R-026).

For each concept S2 linked with a role, the facets the plan needs are read from the card of S0's one pinned snapshot.
Every considered fact is kept or left out with exactly one reason, in the precedence of `REASONS`: the role's scope
(`assembly.scope_matrix`, R-012), the user's negations and fixed values (R-001), the K3 stability label, the variant
choice (decision 16) and the fact budgets. For `edit` with `edit.attach_kb_reference_images`, images are chosen by
greedy coverage of the kept facts. Card and observation strings are copied, never rendered into instructions.
"""

from collections import Counter
import hashlib
import json
from pathlib import Path
import secrets

from .config import validate_assembly_reads
from .model_schemas import cached_registry
from .planner import SEED_BITS, _seed, pick

RESULT_SCHEMA = 'assembly_result.v0'  # artifact-bound
PLAN_SCHEMA = 'plan_result.v0'  # artifact-bound: the S2 result S3 consumes
# protocol-bound: left-out reasons in precedence order, the first that applies wins (docs/stages/S3-knowledge.md)
REASONS = ('OUT_OF_SCOPE', 'NEGATED_BY_USER', 'USER_LOCKED', 'UNSTABLE', 'VARIANT_NOT_CHOSEN', 'BUDGET')
# protocol-bound: K3 labels used as facts. stable_low_diversity stays UNSTABLE until E9 names the facets a role
# treats as intrinsic (T-014 PM answer 2); single_observation is never a fact; varies goes through the variant choice.
USABLE = frozenset(('curated', 'stable'))
VARIES = 'varies'  # protocol-bound K3 label
VISIBILITY_ORDER = ('clear', 'partial')  # protocol-bound: preferred first; other visibilities show nothing usable
DECODED = 'ok'  # protocol-bound images.decode_status of an image that can be sent
CHANNEL = 'attach'  # protocol-bound delivery channel for edit with attached knowledge images (decision 14)
OUTFIT_ROLE = 'subject_identity'  # protocol-bound meaning of assembly.default_outfit: the role that wears it
OUTFIT_GROUP = 'apparel'  # and the facet group include admits
ERRORS = {'SNAPSHOT_MISSING': 'S3: no pinned snapshot', 'KNOWLEDGE_READ_FAILED': 'S3: a pinned table read failed'}


def _digest(value):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def fact_id(snapshot_id, link_index, concept_id, facet, value, *provenance):
    """Stable per snapshot and link, so one concept linked to two entities gives two bound facts (PM answer 4)."""
    return 'fact:' + _digest([snapshot_id, link_index, concept_id, facet, value, *provenance])


def effective_scope(config, role):
    """Facet groups a role may use: its scope_matrix row, plus apparel for a subject under default_outfit include."""
    assembly = config['assembly']
    if role is None:
        return frozenset()
    scope = set(assembly['scope_matrix'][role])
    if role == OUTFIT_ROLE and assembly['default_outfit'] == 'include':
        scope.add(OUTFIT_GROUP)
    return frozenset(scope)


def _group_of(facet):
    return facet.partition('.')[0]  # facet names are '<facet group>.<facet>' (contracts/README.md, Enums)


def _empty_pack(request_id, snapshot_id):
    return {'request_id': request_id, 'snapshot_id': snapshot_id, 'facts': [], 'images': [], 'left_out': [],
            'selections': []}


def _considered(ir, plan_result, cards, config, snapshot_id, needs):
    """Every considered fact with its reason (None means kept so far), plus the random variant choices."""
    resolution = config['resolution']
    base = (secrets.randbits(SEED_BITS) if resolution['random_seed'] == 'per_request'
            else resolution['random_seed'])
    entities = {entity['id']: entity for entity in ir['entities']}
    scene_locks = {item['facet'] for item in ir['scene']['attributes'] if item['source'] == 'user'}
    negated = {item['bound_entity'] for item in ir['constraint_ledger']['constraints']
               if item['type'] == 'negation' and item['bound_entity'] is not None}
    ratings = {(item['link_index'], item['variant_group']): item for item in plan_result['variant_ratings']}
    facts, selections = [], []

    for index, link in enumerate(ir['concept_links']):
        concept = link['concept_id']
        wanted = needs.get(concept)
        card = cards.get(concept)
        if link['status'] not in ('resolved', 'family') or concept is None or not wanted or card is None:
            continue
        scope = effective_scope(config, link['role'])
        entity = link['entity_id']
        # A link without an entity takes the user's scene values as its locks (PM answer 5, R-001).
        locks = scene_locks if entity is None else {
            item['facet'] for item in entities[entity]['attributes'] if item['source'] == 'user'}
        variants = {group['facet']: group for group in card['variant_groups']}

        def candidate(facet, value, stability, origin, image_ids, support, reason, *provenance):
            facts.append({'id': fact_id(snapshot_id, index, concept, facet, value, *provenance),
                          'link_index': index, 'concept_id': concept, 'facet': facet, 'value': value,
                          'stability': stability, 'origin': origin, 'bound_entity': entity,
                          'image_ids': list(image_ids), 'support': support,
                          'need_rank': wanted.index(facet) if facet in wanted else len(wanted), 'reason': reason})

        for item in card['facets']:
            facet = item['facet']
            if facet not in wanted:
                continue
            if item['facet_group'] not in scope or _group_of(facet) != item['facet_group']:
                reason = 'OUT_OF_SCOPE'
            elif entity is not None and entity in negated:
                reason = 'NEGATED_BY_USER'
            elif facet in locks:
                reason = 'USER_LOCKED'
            elif item['stability'] != VARIES and item['stability'] not in USABLE:
                reason = 'UNSTABLE'
            else:
                reason = None
            if item['stability'] != VARIES:
                for value in item['values']:
                    candidate(facet, value['value'], item['stability'], item['origin'], value['image_ids'],
                              value['support_count'], reason)
                continue
            group = variants.get(facet)
            if group is None:
                # cards.variant_groups false: the planner never saw these values, so none is chosen (PM answer 2).
                for value in item['values']:
                    candidate(facet, value['value'], VARIES, item['origin'], value['image_ids'],
                              value['support_count'], reason or 'VARIANT_NOT_CHOSEN')
                continue
            chosen = None
            if reason is None:
                rated = ratings.get((index, group['group_id']))
                if rated is not None and rated['named_variant_id'] is not None:
                    chosen = rated['named_variant_id']  # the variant the user named wins (decision 16)
                elif resolution['on_variant_unspecified'] == 'context_then_random':
                    options = (rated['ratings'] if rated is not None and rated['ratings'] else
                               [{'id': variant['variant_id'], 'context_fit': 'neutral'}
                                for variant in group['variants']])
                    seed = _seed(base, f'variant:{index}:{group["group_id"]}')
                    chosen = pick(options, seed)
                    if resolution['trace_choice']:
                        selections.append({'concept_id': concept, 'variant_group': group['group_id'],
                                           'candidates': [dict(option) for option in options],
                                           'chosen_id': chosen, 'seed': seed})
            for variant in group['variants']:
                kept = reason if reason is not None else (None if variant['variant_id'] == chosen
                                                          else 'VARIANT_NOT_CHOSEN')
                # K3 writes a variant's images as its value's supporting images, so their count is its support.
                candidate(facet, variant['value'], VARIES, item['origin'], variant['image_ids'],
                          len(variant['image_ids']), kept)
        for statement in card['curated_statements']:
            # Unfaceted text cannot be scope-filtered structurally, so it never reaches the writer (P1 Step 0).
            candidate(None, statement['text'], 'curated', statement['origin'], (), 0, 'OUT_OF_SCOPE',
                      statement['folder_id'])
    return facts, selections


def _dedupe(facts):
    """One considered fact per id; a kept copy wins over a left-out duplicate of the same text."""
    chosen = {}
    for fact in facts:
        if fact['id'] not in chosen or (chosen[fact['id']]['reason'] is not None and fact['reason'] is None):
            chosen[fact['id']] = fact
    return [fact for fact in facts if chosen[fact['id']] is fact]


def _apply_budget(facts, config):
    """need_then_support: link order, the plan's facet order, support descending, then value code points."""
    assembly = config['assembly']
    kept = sorted((fact for fact in facts if fact['reason'] is None),
                  key=lambda fact: (fact['link_index'], fact['need_rank'], -fact['support'], fact['value']))
    per_concept, total = Counter(), 0
    for fact in kept:
        if per_concept[fact['concept_id']] >= assembly['max_facts_per_concept'] or \
                total >= assembly['max_facts_per_request']:
            fact['reason'] = 'BUDGET'
        else:
            per_concept[fact['concept_id']] += 1
            total += 1
    return [fact for fact in kept if fact['reason'] is None]


def _select_images(kept, tables, config):
    """Greedy coverage of the kept (link, facet) pairs over the facts' own supporting images."""
    images = {row['image_id']: row for row in tables['images']}
    concepts_per_folder = Counter(row['folder_id'] for row in tables['folder_concepts'])
    single = {row['image_id'] for row in tables['folder_images'] if concepts_per_folder[row['folder_id']] == 1}
    seen = {}
    for row in tables['observations']:
        if row['visibility'] in VISIBILITY_ORDER:
            seen.setdefault((row['image_id'], row['concept_id'], row['facet']), []).append(row)
    # (link index, image id) -> the kept facts this image supports (the card's own per-value image ids, so an excluded
    # value never rides along) and, per facet, the visible observation rows that rank it. Rows only rank; they never
    # reach the note, because a raw observation value is not mapped to a card value (R-015).
    offers = {}
    for fact in kept:
        for image in fact['image_ids']:
            rows = seen.get((image, fact['concept_id'], fact['facet']))
            if rows and image in images and images[image]['decode_status'] == DECODED:
                offer = offers.setdefault((fact['link_index'], image), {'fact': fact, 'facts': [], 'facets': {}})
                offer['facts'].append(fact)
                offer['facets'][fact['facet']] = rows
    uncovered = {(fact['link_index'], fact['facet']) for fact in kept}
    per_concept, used_images, used_groups, chosen = Counter(), set(), set(), []
    per_cap = config['assembly']['max_images_per_concept']
    total_cap = config['edit']['max_kb_reference_images']

    def rank(key):
        index, image = key
        offer = offers[key]
        rows = [row for facet_rows in offer['facets'].values() for row in facet_rows]
        gain = sum((index, facet) in uncovered for facet in offer['facets'])
        visibility = min(VISIBILITY_ORDER.index(row['visibility']) for row in rows)
        quality = max(row['quality'] for row in rows)
        return (-gain, visibility, -quality, image not in single, image, index)

    while uncovered and len(chosen) < total_cap:
        open_offers = [key for key, offer in offers.items()
                       if key[1] not in used_images
                       and (images[key[1]]['near_duplicate_group'] is None
                            or images[key[1]]['near_duplicate_group'] not in used_groups)
                       and per_concept[offer['fact']['concept_id']] < per_cap
                       and any((key[0], facet) in uncovered for facet in offer['facets'])]
        if not open_offers:
            break
        key = min(open_offers, key=rank)
        offer = offers[key]
        fact = offer['fact']
        group = images[key[1]]['near_duplicate_group']
        used_images.add(key[1])
        if group is not None:
            used_groups.add(group)
        per_concept[fact['concept_id']] += 1
        uncovered -= {(key[0], facet) for facet in offer['facets']}
        pairs = sorted({(item['facet'], item['value']) for item in offer['facts']})
        chosen.append({'image_id': key[1], 'concept_id': fact['concept_id'], 'channel': CHANNEL,
                       'bound_entity': fact['bound_entity'], 'facets': sorted(offer['facets']),
                       # Kept fact values as JSON data, never a sentence built around them (R-026).
                       'note': json.dumps([{'facet': facet, 'value': value} for facet, value in pairs],
                                          ensure_ascii=False)})
    return chosen


def attached_note(image, facts):
    """Decode an `attach` image's note into facet and value pairs, checked against the kept facts it supports.

    The one rule for S3 validation and for the S4 brief, so the writer never renders a value S3 would refuse.
    """
    supported = {(fact['facet'], fact['value']) for fact in facts
                 if image['image_id'] in fact['image_ids'] and fact['concept_id'] == image['concept_id']
                 and fact['bound_entity'] == image['bound_entity']}
    if image['channel'] != CHANNEL or not supported:
        raise ValueError('S3: an image does not support a kept fact')
    try:
        noted = json.loads(image['note'])
        pairs = {(item['facet'], item['value']) for item in noted}
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError('S3: an image note is not a list of facet and value pairs') from error
    # The note and the facet list carry only values of kept facts this image supports (decision 16).
    if not pairs <= supported or sorted({facet for facet, _ in pairs}) != image['facets']:
        raise ValueError('S3: an image note names a value that is not a kept fact it supports')
    return [{'facet': item['facet'], 'value': item['value']} for item in noted]


def validate_assembly(result, ir, config, task, snapshot_id, request_id, registry):
    """Schema, then the pack invariants the schema cannot express; a failure is a defect, never repaired."""
    registry.validate(RESULT_SCHEMA, result)
    state, pack = result['retrieval_state'], result['pack']
    if (state == 'failed') != (pack is None) or (state == 'failed') != (result['error'] is not None):
        raise ValueError('S3: a failed result has an error and no pack, any other has a pack and no error')
    if pack is None:
        return result
    if pack['request_id'] != request_id or pack['snapshot_id'] != snapshot_id:
        raise ValueError('S3: the pack names another request or snapshot')
    ids = [fact['id'] for fact in pack['facts']] + [item['fact_id'] for item in pack['left_out']]
    if len(ids) != len(set(ids)) or None in ids:
        raise ValueError('S3: every considered fact is kept or left out exactly once')
    links = ir['concept_links']
    for fact in pack['facts']:
        if not any(link['concept_id'] == fact['concept_id'] and link['entity_id'] == fact['bound_entity']
                   and _group_of(fact['facet']) in effective_scope(config, link['role']) for link in links):
            raise ValueError('S3: a fact is outside the scope of the role that receives it')
    images = pack['images']
    attach = task == 'edit' and config['edit']['attach_kb_reference_images'] is True
    if images and not attach:
        raise ValueError('S3: knowledge images are attached only for edit with attach_kb_reference_images')
    if attach:
        per_concept = Counter(image['concept_id'] for image in images)
        if (len(images) > config['edit']['max_kb_reference_images']
                or any(count > config['assembly']['max_images_per_concept'] for count in per_concept.values())
                or len({image['image_id'] for image in images}) != len(images)):
            raise ValueError('S3: the image selection exceeds its limits')
        for image in images:
            attached_note(image, pack['facts'])
    content = bool(pack['facts'] or images)
    if (state == 'retrieved') != content or (state == 'skipped_no_knowledge_needed' and pack['left_out']):
        raise ValueError('S3: the retrieval state does not match the pack')
    return result


def build_assembly(intake_result, plan_result, *, project_root):
    """Return assembly_result.v0 for one request; reads only S0's pin and never closes it.

    S3 is absent with retrieval off and runs only on a plan with an Intent IR (`ready`, or `degraded` with the
    most plausible reading); every other call is a programming error and raises before any read.
    """
    payload = intake_result.payload
    config = payload.get('effective_config')
    if not isinstance(config, dict) or not isinstance(config.get('rag'), dict):
        raise ValueError('S3 requires successful intake configuration')
    if config['rag'].get('enabled') is False:
        raise ValueError('S3 is absent from the retrieval-disabled graph')
    if config['rag'].get('enabled') is not True or payload.get('outcome') not in ('ready', 'degraded'):
        raise ValueError('S3 requires successful retrieval-enabled intake')
    task = payload.get('task')
    if task not in ('generate', 'edit'):
        raise ValueError('S3 requires the request task')
    root = Path(project_root).resolve()
    validate_assembly_reads(config, project_root=root)
    # Shared across calls while the schema files keep their bytes; key and registry come from one read (RES-1 row 10).
    registry = cached_registry(root / 'contracts' / 'schemas')
    if not isinstance(plan_result, dict) or plan_result.get('intent_ir') is None:
        raise ValueError('S3 requires a plan with an Intent IR')
    registry.validate(PLAN_SCHEMA, plan_result)
    if plan_result['state'] not in ('ready', 'degraded'):
        raise ValueError('S3 runs only on a ready or degraded plan')
    ir = plan_result['intent_ir']
    if ir['task'] != task:
        raise ValueError('S3: the plan is for another task')
    request_id = payload.get('request_id')
    snapshot = intake_result.snapshot

    def result(state, pack=None, error=None):
        value = {'retrieval_state': state, 'pack': pack,
                 'error': None if error is None else {'code': error, 'message': ERRORS[error]}}
        return validate_assembly(value, ir, config, task, None if snapshot is None else snapshot.snapshot_id,
                                 request_id, registry)

    if snapshot is None:
        if payload.get('outcome') != 'degraded':
            raise ValueError('S3: intake snapshot is closed or absent')
        return result('failed', error='SNAPSHOT_MISSING')
    if payload.get('retrieval') != {'state': 'pinned', 'snapshot_id': snapshot.snapshot_id}:
        raise ValueError('S3: intake snapshot identity differs')
    pack = _empty_pack(request_id, snapshot.snapshot_id)
    needs = {}
    for need in ir['knowledge_needs']:
        facets = needs.setdefault(need['concept_id'], [])
        facets.extend(facet for facet in need['facets'] if facet not in facets)
    if not any(needs.values()):
        return result('skipped_no_knowledge_needed', pack)
    attach = task == 'edit' and config['edit']['attach_kb_reference_images'] is True
    try:
        cards = {row['concept_id']: row for row in snapshot.read_rows('cards') if row['concept_id'] in needs}
        tables = ({name: snapshot.read_rows(name)
                   for name in ('images', 'observations', 'folder_images', 'folder_concepts')} if attach else {})
    except Exception:
        return result('failed', error='KNOWLEDGE_READ_FAILED')
    facts, selections = _considered(ir, plan_result, cards, config, snapshot.snapshot_id, needs)
    facts = _dedupe(facts)
    kept = _apply_budget(facts, config)
    pack['facts'] = [{key: fact[key] for key in ('id', 'concept_id', 'facet', 'value', 'stability', 'origin',
                                                 'bound_entity', 'image_ids')} for fact in kept]
    pack['left_out'] = [{'concept_id': fact['concept_id'], 'facet': fact['facet'], 'fact_id': fact['id'],
                         'reason': fact['reason']} for fact in facts if fact['reason'] is not None]
    pack['selections'] = selections
    if attach:
        pack['images'] = _select_images(kept, tables, config)
    return result('retrieved' if pack['facts'] or pack['images'] else 'no_match', pack)
