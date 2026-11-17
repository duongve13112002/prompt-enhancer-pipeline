"""S2: one planner call parses, links and plans a request (R-001, R-008, R-013, R-026).

The model refers to shortlist entries, variant groups and variants only by position (`planner_reply.v0`); code checks
every position, reference and verbatim span, makes the family and variant identities concrete, picks among family
candidates (decisions 15, 69) and builds `intent_ir.v0` inside `plan_result.v0`. All shortlist, card and prompt text
goes to the model as one JSON data message; the system message is the prompt file's bytes, unchanged.
"""

import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import secrets

from jsonschema import ValidationError

from .candidates import build_candidates, validate_shortlist
from .config import _EXACT_INTEGERS, validate_planner_reads
from .model_client import ModelCallError
from .model_schemas import SchemaRegistry
from .profile_prompt import load_prompt

ROLE = 'planner'  # protocol-bound VLM role name (contracts/vlm-request.md)
FIT_ORDER = ('fits', 'neutral', 'conflicts')  # protocol-bound preference tiers (decisions 15, 69)
EDIT_FIELDS = ('edit_targets', 'delta', 'preserve', 'dependent_effects')  # intent_ir.v0 fields empty for generate
RESULT_SCHEMA = 'plan_result.v0'  # artifact-bound
SEED_BITS = 63  # protocol-bound: seeds stay inside the schema's non-negative 64-bit integer range


class InvalidReply(ValueError):
    """A reply that passed the JSON schema but breaks an invariant the schema cannot express."""


def _plain(record):
    """Call records hold tuples; the stage result is plain JSON."""
    return json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))


def _facet_groups(registry):
    # artifact-bound: the facet-group enum of the stored card, never a local copy of the words
    schema, _ = registry.get('concept_card.v0')
    return frozenset(schema['properties']['facets']['items']['properties']['facet_group']['enum'])


def _variant_groups(snapshot, concept_ids):
    wanted = set(concept_ids)
    return {row['concept_id']: row['variant_groups'] for row in snapshot.read_rows('cards')
            if row['concept_id'] in wanted}


def _data(payload, config, manifest, entries, variants):
    """The one user-data message: every string in it is data (R-026)."""
    shortlist = []
    for position, entry in enumerate(entries):
        groups = [{'group_position': group_position, 'facet': group['facet'],
                   'variants': [{'variant_position': variant_position, 'value': variant['value']}
                                for variant_position, variant in enumerate(group['variants'])]}
                  for group_position, group in enumerate(variants.get(entry['concept_id'], []))]
        shortlist.append({'position': position, 'name': entry['name'], 'kind': entry['kind'],
                          'description': entry['description'], 'variant_groups': groups})
    data = {'task': payload['task'], 'prompt': payload['prompt'], 'quote_semantics': manifest['quote_semantics'],
            'creativity': config['creativity'], 'shortlist': shortlist}
    if payload['task'] == 'edit':
        data['edit_default_preservation'] = config['edit']['default_preservation']
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def _require(condition, message):
    if not condition:
        raise InvalidReply(message)


def check_integers(reply, registry):
    """Reply integers are native ints in their schema domain.

    Draft 2020-12 counts an integral float such as `1.0` as an integer, and positions index Python lists below, so the
    reply is revalidated with exact integers (as S1 does); a failure joins the invalid-reply path (R-013).
    """
    schema, _ = registry.get('planner_reply.v0')
    try:
        _EXACT_INTEGERS(schema).validate(reply)
    except ValidationError as error:
        raise InvalidReply('reply breaks the schema with exact integers at ' + '/'.join(map(str, error.absolute_path)))


def check_reply(reply, prompt, task, entries, variants, facet_groups):
    """Invariants of a schema-valid reply; any failure makes the whole reply invalid (never repaired)."""
    def unique(values, what):
        values = list(values)
        _require(len(values) == len(set(values)), f'duplicate {what}')
        return set(values)

    def verbatim(text, what):
        _require(text in prompt, f'{what} is not verbatim prompt text')

    entities = unique((entity['id'] for entity in reply['entities']), 'entity id')

    def entity(value, what):
        _require(value is None or value in entities, f'{what} names an unknown entity')

    for item in reply['entities']:
        if item['source'] == 'user':
            verbatim(item['mention'], 'entity mention')  # an entity read from the source image is not prompt text
        for relation in item['relations']:
            entity(relation['object_entity_id'], 'relation')
    if task == 'generate':
        _require(reply['edit_kind'] is None and not any(reply[field] for field in EDIT_FIELDS),
                 'generate has edit fields')
    else:
        _require(reply['edit_kind'] is not None, 'edit requires edit_kind')
    targets = unique((target['id'] for target in reply['edit_targets']), 'edit target id')
    for target in reply['edit_targets']:
        entity(target['entity_id'], 'edit target')
    unique((operation['id'] for operation in reply['delta']), 'delta id')
    for item in (*reply['delta'], *reply['dependent_effects']):
        _require(item['target_id'] in targets, 'unknown edit target')
    unique((constraint['id'] for constraint in reply['constraints']), 'constraint id')
    for constraint in reply['constraints']:
        verbatim(constraint['span'], 'constraint span')
        entity(constraint['bound_entity'], 'constraint')
    unique((literal['id'] for literal in reply['literal_texts']), 'literal id')
    for literal in reply['literal_texts']:
        verbatim(literal['span'], 'literal span')
        _require(literal['text'] in literal['span'], 'literal text is not inside its span')
    for token in reply['passthrough_tokens']:
        verbatim(token['span'], 'passthrough span')
        _require(token['token'] in token['span'], 'passthrough token is not inside its span')

    linked = set()
    for link in reply['links']:
        verbatim(link['mention'], 'mention')
        entity(link['entity_id'], 'link')
        positions = unique((candidate['position'] for candidate in link['candidates']), 'candidate position')
        _require(all(position < len(entries) for position in positions), 'candidate position out of range')
        own = link['candidate_position']
        if link['status'] == 'resolved':
            _require(own is not None and own < len(entries), 'resolved link needs a shortlist position')
            _require(link['role'] is not None, 'resolved link needs a role')
            usable = {own}
        elif link['status'] == 'family':
            _require(own is None and positions, 'family link needs candidates and no single position')
            _require(link['role'] is not None, 'family link needs a role')
            usable = positions
        else:
            _require(own is None and not link['variants'], 'no_match link names a concept')
            usable = set()
        linked |= usable
        unique(((item['candidate_position'], item['group_position']) for item in link['variants']), 'variant group')
        for item in link['variants']:
            _require(item['candidate_position'] in usable, 'variant of an unlinked candidate')
            groups = variants.get(entries[item['candidate_position']]['concept_id'], [])
            _require(item['group_position'] < len(groups), 'variant group out of range')
            count = len(groups[item['group_position']]['variants'])
            named = item['named_variant_position']
            _require(named is None or named < count, 'named variant out of range')
            rated = [rating['variant_position'] for rating in item['ratings']]
            # Ratings are absent (no preference) or rate every variant exactly once; a named variant needs none.
            _require((not rated and named is not None) or sorted(rated) == list(range(count)),
                     'variant ratings must cover the group exactly once')
    for need in reply['knowledge_needs']:
        _require(need['candidate_position'] in linked, 'knowledge need for an unlinked candidate')
        for facet in need['facets']:
            group, separator, name = facet.partition('.')
            _require(separator and name and group in facet_groups, 'facet is not <facet group>.<facet>')
    return reply


def _seed(base, index):
    digest = hashlib.sha256(f'{base}:{index}'.encode('ascii')).digest()
    return int.from_bytes(digest[:8], 'big') >> (64 - SEED_BITS)


def pick(candidates, seed):
    """Keep `fits`, else `neutral`, else all; a uniform pick that never empties the set (decisions 15, 69)."""
    for fit in FIT_ORDER:
        tier = sorted(item['id'] for item in candidates if item['context_fit'] == fit)
        if tier:
            return random.Random(seed).choice(tier)
    raise ValueError('a family needs at least one candidate')


def build_plan(reply, task, entries, variants, config, warnings):
    """intent_ir.v0 plus selections, variant ratings and the stage state from a checked reply."""
    resolution = config['resolution']
    base = (secrets.randbits(SEED_BITS) if resolution['random_seed'] == 'per_request'
            else resolution['random_seed'])
    links, selections, ratings, final = [], [], [], {}
    ask_family = []
    for index, link in enumerate(reply['links']):
        rated = [{'concept_id': entries[item['position']]['concept_id'], 'context_fit': item['context_fit']}
                 for item in link['candidates']]
        status, concept = link['status'], None
        if status == 'resolved':
            concept = entries[link['candidate_position']]['concept_id']  # ratings never change it (decision 69)
        elif status == 'family':
            mode = resolution['on_family_match']
            options = [{'id': item['concept_id'], 'context_fit': item['context_fit']} for item in rated]
            if mode == 'context_then_random':
                seed = _seed(base, index)
                concept = pick(options, seed)
                if resolution['trace_choice']:
                    selections.append({'link_index': index, 'candidates': options, 'chosen_id': concept,
                                       'seed': seed})
            elif mode == 'omit':
                status = 'no_match'
            else:
                ask_family.append([entries[item['position']]['name'] for item in link['candidates']])
        if concept is not None:
            final[index] = concept
        links.append({'mention': link['mention'], 'entity_id': link['entity_id'], 'status': status,
                      'role': link['role'], 'concept_id': concept, 'candidates': rated,
                      'needs_lookup': link['needs_lookup']})
        for item in link['variants']:
            entry = entries[item['candidate_position']]
            if entry['concept_id'] != concept:
                continue  # rated a family candidate code did not pick
            group = variants[entry['concept_id']][item['group_position']]
            named = item['named_variant_position']
            ratings.append({'link_index': index, 'concept_id': concept, 'variant_group': group['group_id'],
                            'named_variant_id': None if named is None else group['variants'][named]['variant_id'],
                            'ratings': [{'id': group['variants'][rating['variant_position']]['variant_id'],
                                         'context_fit': rating['context_fit']} for rating in item['ratings']]})
    chosen = set(final.values())
    needs = {}
    for need in reply['knowledge_needs']:
        concept = entries[need['candidate_position']]['concept_id']
        if concept in chosen:
            facets = needs.setdefault(concept, [])
            facets.extend(facet for facet in need['facets'] if facet not in facets)
    clarification = list(reply['clarification_candidates'])
    for names in ask_family:
        clarification.extend(name for name in names if name not in clarification)
    ir = {'task': task, 'edit_kind': reply['edit_kind'], 'entities': reply['entities'], 'scene': reply['scene'],
          'concept_links': links, 'rejected_candidates': [],
          'knowledge_needs': [{'concept_id': concept, 'facets': facets} for concept, facets in needs.items()],
          'constraint_ledger': {'constraints': reply['constraints']}, 'literal_texts': reply['literal_texts'],
          'passthrough_tokens': reply['passthrough_tokens'],
          **{field: reply[field] for field in EDIT_FIELDS}, 'unresolved': reply['unresolved'],
          'needs_clarification': reply['needs_clarification'] or bool(ask_family),
          'clarification_candidates': clarification}
    state = 'ready'
    if ask_family or (reply['needs_clarification'] and config['clarification']['mode'] == 'ask'):
        state = 'needs_clarification'
    elif reply['needs_clarification']:
        state = 'degraded'  # best_effort: the most plausible reading is used (decision 68)
        warnings.append('CLARIFICATION_ASSUMED')
    return ir, selections, ratings, state


async def plan(intake_result, shortlist, client, *, project_root, embedder=None):
    """Return plan_result.v0 for one request; never pins, closes the pin or builds a model client.

    `shortlist` is S1's `candidate_shortlist.v0` with retrieval on and None with retrieval off (R-009). `client` is
    the caller's planner-bound model client. A schema- or invariant-invalid reply is retried once, then the result
    is `degraded` without a plan; a failed call is `failed`. Cancellation propagates.
    """
    payload = intake_result.payload
    config = payload.get('effective_config')
    if not isinstance(config, dict) or payload.get('outcome') not in ('ready', 'degraded'):
        raise ValueError('S2 requires a successful intake')
    prompt, task = payload.get('prompt'), payload.get('task')
    if not isinstance(prompt, str) or task not in ('generate', 'edit'):
        raise ValueError('S2 requires the original prompt and task')
    if getattr(client, 'role_name', None) != ROLE:
        raise ValueError('S2 requires a planner-bound model client')
    project_root = Path(project_root).resolve()
    validate_planner_reads(config, project_root=project_root)
    rag = config['rag']['enabled']
    if not rag and shortlist is not None:
        raise ValueError('S2: retrieval is off, so there is no shortlist')
    if rag and shortlist is None:
        raise ValueError('S2 requires the S1 shortlist with retrieval on')
    snapshot = intake_result.snapshot if rag else None
    warnings, calls = [], []
    entries, variants = [], {}
    # Grounding is what S1 and the card read supplied. A failed S1 (decision 143) or a failed card read continues
    # without it and the second-chance lookup never regains it; the plan says so with RETRIEVAL_FAILED.
    grounded = rag
    if rag:
        validate_shortlist(shortlist, prompt, project_root=project_root)
        if shortlist['state'] != 'ready':
            grounded = False
            warnings.append('RETRIEVAL_FAILED')
        elif shortlist['candidates']:
            if snapshot is None:
                raise ValueError('S2: the shortlist needs the intake snapshot')
            entries = list(shortlist['candidates'])
            try:
                variants = _variant_groups(snapshot, [entry['concept_id'] for entry in entries])
            except Exception:
                entries, variants = [], {}
                grounded = False
                warnings.append('RETRIEVAL_FAILED')
    registry = SchemaRegistry(project_root / 'contracts' / 'schemas')
    groups = _facet_groups(registry)
    role = config['vlm']['roles'][ROLE]
    system = load_prompt(project_root, role['prompt_id'])
    images = ()
    if task == 'edit' and role['image'] != 'none':
        images = (('source_image', base64.b64decode(payload['source_image']['data'], validate=True)),)
    manifest = intake_result.profile.manifest

    async def ask(shortlist_entries):
        text = _data(payload, config, manifest, shortlist_entries, variants)
        for _ in range(2):  # one retry, and only for an invalid reply (docs/stages/S2-planner.md)
            try:
                value, record = await client.call(system, text, images=images)
            except ModelCallError:
                return None, 'PLANNER_CALL_FAILED'
            calls.append(_plain(record))
            if value is None:
                if record['status'] != 'SCHEMA_INVALID':
                    return None, 'PLANNER_CALL_FAILED'
                _warn('SCHEMA_INVALID')
                continue
            try:
                check_integers(value, registry)
                return check_reply(value, prompt, task, shortlist_entries, variants, groups), None
            except InvalidReply:
                calls[-1]['reply_check'] = 'INVARIANT_INVALID'
                _warn('SCHEMA_INVALID')
        return None, 'PLANNER_REPLY_INVALID'

    def _warn(code):
        if code not in warnings:
            warnings.append(code)

    def result(state, ir=None, selections=(), ratings=(), error=None):
        value = {'state': state, 'intent_ir': ir, 'selections': list(selections), 'variant_ratings': list(ratings),
                 'warnings': warnings, 'calls': calls,
                 'error': None if error is None else {'code': error, 'message': {
                     'PLANNER_REPLY_INVALID': 'S2: the planner reply was invalid after one retry',
                     'PLANNER_CALL_FAILED': 'S2: the planner call failed'}[error]}}
        registry.validate(RESULT_SCHEMA, value)
        return value

    reply, error = await ask(entries)
    if reply is None:
        return result('failed' if error == 'PLANNER_CALL_FAILED' else 'degraded', error=error)
    for _ in range(config['planner']['second_chance_rounds']):
        mentions = sorted({link['mention'] for link in reply['links']
                           if link['needs_lookup'] and link['status'] == 'no_match'})
        if not grounded or snapshot is None or not mentions:
            break
        known, added = {entry['concept_id'] for entry in entries}, []
        for mention in mentions:
            # One extra S1 lookup per mention, over the same pin and sources (S2 second-chance lookup).
            found = await build_candidates(replace(intake_result, payload=dict(payload, prompt=mention)),
                                           embedder=embedder)
            if found['state'] != 'ready':
                _warn('RETRIEVAL_FAILED')
            for entry in found['candidates']:
                if entry['concept_id'] not in known:
                    known.add(entry['concept_id'])
                    added.append(entry)
        if not added:
            break
        try:
            variants.update(_variant_groups(snapshot, [entry['concept_id'] for entry in added]))
        except Exception:
            _warn('RETRIEVAL_FAILED')
            break
        again, _ = await ask(entries + added)
        if again is None:
            _warn('SECOND_CHANCE_FAILED')  # the earlier plan stands
            break
        entries, reply = entries + added, again
    ir, selections, ratings, state = build_plan(reply, task, entries, variants, config, warnings)
    return result(state, ir, selections, ratings)
