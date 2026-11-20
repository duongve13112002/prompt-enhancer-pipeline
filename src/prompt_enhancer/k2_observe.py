"""Unpublished, single-image K2 observations over named folders and current K1 vectors (R-010/R-017)."""

from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path

import PIL
import pyarrow as pa

from .config import (ConfigurationError, _lexical_path, _resolve_within, _schema, resolve_knowledge_root,
                     same_physical_path, validate_observation_reads)
from .k1_embed import _read_verified
from .k2_naming import _digest, _validate_reply, failure_code, record_probe_failure
from .model_client import (MODEL_CALL_REVISION, ModelCallError, ModelClientRun, run_concurrency,
                           semantic_role_settings)
from .model_resources import CapacityLog, await_owned, flow
from .profile_prompt import load_prompt
from .reply_retry import Attempts, call_until_valid
from .storage import StorageError, artifact_key, concept_id
from .storage.identity import canonical_json, check_artifact_key, is_image_id


# Artifact-bound stage/layout/algorithm identities; no measured operating value is embedded here.
STAGE = 'k2_observation'
LAYOUT = 1
GROUPING = 'cosine-components-v1'
STORED_SCHEMA = 'image_observation.v0'
REPLY_SCHEMA = 'observer_reply.v1'
# declared revision of the code that shapes a K2 observation (KBS-S3-1); keys the artifacts in place of a hash of
# module source. Bump it with any edit to a function its guard pins (tests/test_stage_revisions.py).
K2_OBSERVE_REVISION = 'k2-observe-4'


def group_images(images, vectors, distance, *, revision, dim):
    """Exact single-link components, O(n^2) CPU and O(n*d) memory; no pairwise matrix."""
    if type(distance) not in (int, float) or not math.isfinite(distance) or not 0 <= distance <= 2:
        raise ValueError('near_duplicate_cosine_distance must be finite and in [0, 2]')
    if type(dim) is not int or dim <= 0 or not isinstance(revision, str) or not revision:
        raise ValueError('current K1 dimension and embedder revision are required')
    by_image = {row['image_id']: row for row in images}
    if len(by_image) != len(images) or any(not is_image_id(key) for key in by_image):
        raise ValueError('K1 images must have unique valid image_id values')
    unit = {}
    for row in vectors:
        identity = row['image_id']
        if identity not in by_image or identity in unit:
            raise ValueError('K1 vector references an unknown or repeated image')
        if row['embedder_revision'] != revision or row['dim'] != dim:
            raise ValueError('K1 vectors must share the current revision and dimension')
        values = row['vector']
        if len(values) != dim or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('K1 vector must have finite values and the current dimension')
        scale = max(abs(value) for value in values)
        if scale == 0:
            raise ValueError('K1 vector must be nonzero')
        if by_image[identity]['decode_status'] != 'ok':
            continue
        scaled = [value / scale for value in values]
        norm = math.sqrt(math.fsum(value * value for value in scaled))
        unit[identity] = [value / norm for value in scaled]
    ids = sorted(unit)
    parents = {identity: identity for identity in ids}

    def find(identity):
        while parents[identity] != identity:
            parents[identity] = parents[parents[identity]]
            identity = parents[identity]
        return identity

    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            # Identical normalized vectors have exact zero distance; do not invent an epsilon for roundoff.
            cosine_distance = (0.0 if unit[left] == unit[right] else
                               max(0.0, min(2.0, 1.0 - math.fsum(a * b for a, b in zip(unit[left], unit[right])))))
            if cosine_distance <= distance:
                first, second = sorted((find(left), find(right)))
                parents[second] = first
    components = {}
    for identity in ids:
        components.setdefault(find(identity), []).append(identity)
    return {identity: 'nd-' + _digest({'algorithm': GROUPING, 'revision': revision, 'images': members})
            for members in components.values() for identity in members}


def choose_representatives(image_ids, groups, quota):
    """One least-ID member per group, then sorted remaining IDs until the positive quota is filled."""
    if type(quota) is not int or quota <= 0:
        raise ValueError('representatives_per_folder must be positive')
    ids = sorted(set(image_ids))
    by_group = {}
    for identity in ids:
        by_group.setdefault(groups[identity], []).append(identity)
    first = sorted(members[0] for members in by_group.values())
    first_set = set(first)
    selected = (first + [identity for identity in ids if identity not in first_set])[:quota]
    selected_set = set(selected)
    return selected, [identity for identity in ids if identity not in selected_set]


def parse_observation(value, *, image_id, concepts, context_key, model_revision, prompt_id,
                      facet_schema_version, project_root):
    """Validate untrusted model positions, then construct and validate the code-owned stored document."""
    _validate_reply(project_root, REPLY_SCHEMA, value)
    try:
        canonical_json(value).encode('utf-8')  # refuse NaN/Infinity and unencodable strings before any commit
    except (StorageError, UnicodeError) as error:
        raise ValueError('SCHEMA_INVALID: observer_reply.v1: invalid JSON') from None
    check_artifact_key(context_key)
    try:
        finite_quality = math.isfinite(value['quality'])
    except OverflowError:
        finite_quality = False
    if not finite_quality:
        raise ValueError('quality must be finite')
    seen, observations = set(), []
    for item in value['observations']:
        position = item['concept_position']
        if type(position) is not int or not 0 <= position < len(concepts):
            raise ValueError('concept_position is outside the supplied positions')
        identity = concepts[position]['concept_id']
        facet = item['facet']
        if not facet.strip() or '.' in facet:
            raise ValueError('SCHEMA_INVALID: facet must be a nonblank name without dots')
        qualified = item['facet_group'] + '.' + facet
        key = (identity, qualified)
        if key in seen:
            raise ValueError('duplicate facet for one concept')
        seen.add(key)
        observations.append({**{k: deepcopy(v) for k, v in item.items() if k != 'concept_position'},
                             'concept_id': identity, 'facet': qualified})
    document = {'context_key': context_key, 'image_id': image_id, 'view': value['view'],
                'quality': value['quality'], 'origin': {'origin': 'generated', 'model_revision': model_revision,
                'prompt_id': prompt_id, 'facet_schema_version': facet_schema_version}, 'observations': observations}
    _validate_reply(project_root, STORED_SCHEMA, document)
    return document


def observation_revisions(config, prompt, root):
    role = config['vlm']['roles']['observer']
    return {'model': _digest(semantic_role_settings(config, 'observer')),
            'prompt': _digest({'id': role['prompt_id'], 'bytes': hashlib.sha256(prompt).hexdigest()}),
            'schema': _digest({'layout': LAYOUT, 'reply': _schema(root, REPLY_SCHEMA),
                              'stored': _schema(root, STORED_SCHEMA),
                              'configured': _schema(root, role['output']['schema_id']),
                              'facet_schema_version': config['observe']['facet_schema_version'],
                              'processor': PIL.__version__, 'stage_revision': K2_OBSERVE_REVISION,
                              'model_call': MODEL_CALL_REVISION})}


def _member(row):
    """One declared concept of a folder; the stored bundle keeps this layout in `concepts`."""
    return {'name': row['name_as_written'], 'concept_id': row['concept_id']}


def _declared(members):
    """A folder's declared concepts in name order; their positions are what the observer sees and answers with."""
    return sorted(members, key=lambda row: row['name'])


def _observation_payload(declared, config, root):
    """The user text of one observer call: positioned concept names and the facet groups the stored schema allows."""
    return {'concept_names': [{'concept_position': index, 'name': item['name']} for index, item in enumerate(declared)],
            'facet_schema': {'version': config['observe']['facet_schema_version'],
                             'groups': _schema(root, STORED_SCHEMA)['properties']['observations']['items']
                                       ['properties']['facet_group']['enum']}}


def _observation_bundle(revisions, declared, reply, document, attempts):
    """The stored layout of one completed image-call bundle; `attempts` lists every request made for it (decision 188)."""
    return {'version': LAYOUT, 'revisions': revisions, 'concepts': declared, 'reply': reply, 'document': document,
            'attempts': attempts.entries}


def _failure_row(writer, key, attempts, revisions, error=None):
    """The k2_call_failure checkpoint of one image: every attempt so far (index, seed, closed code, reply digest) and its
    call records; rewritten after each refused attempt. A snapshot never references it, so garbage collection removes it
    after `sync.gc.retention`."""
    code = failure_code(error) if error is not None else next(
        (entry['code'] for entry in reversed(attempts.entries) if entry['code']), None)
    writer.checkpoints('k2_call_failure').commit(
        key, 'k2_call_failure', canonical_json({'error': code, 'attempts': attempts.entries}).encode('utf-8'),
        revisions=revisions, call_records=attempts.records)


def observation_key(image_id, names, revisions):
    return artifact_key(STAGE, _digest({'image_id': image_id, 'concept_names': sorted(set(names))}), revisions)


def _rows(document):
    return [{'context_key': document['context_key'], 'image_id': document['image_id'],
             'view': document['view'], 'quality': document['quality'], **document['origin'], **item}
            for item in document['observations']]


async def observe_images(store, config, handoff, *, images, folder_images, image_embeddings,
                         embedder_revision, dim, knowledge_root, client=None, probe_cases=None, local_probe_plans=None,
                         writer=None):
    """Consume P1 naming rows and K1 part-1 tables; return rows/report/artifacts, never publish.

    An injected BoundModelClient is caller-owned. A lazy run created here is always closed. A completed
    image-call bundle, including an empty reply, is the one checkpoint for its exact concept-name set.
    Injected run doubles expose `.concurrency: BackendConcurrency` and async `for_role`; bound doubles expose `.run`.
    """
    root = store.catalog.root
    validate_observation_reads(config, project_root=root)
    forbidden = {os.path.normcase(name) for name in ('.secrets', 'models', '.git', '.venv')}

    def resolved(key, value):
        # Every path the stage writes is strict (decision 174); the read-only knowledge root is not resolved here.
        return _resolve_within(_lexical_path(value, root, root, key), root, forbidden, root, key)

    source = resolve_knowledge_root(config, root)  # the one physical root (decision 175); read only (R-014)
    if not same_physical_path(knowledge_root, source):
        raise ConfigurationError('knowledge.root: stage argument does not match configured root')
    target = resolved('storage.kb_store', config['storage']['kb_store'])
    if Path(store.root).resolve() != target:
        raise ConfigurationError('storage.kb_store: stage store does not match configured root')
    for target in (target, resolved('storage.data_root', config['storage']['data_root'])):
        if target == source or target in source.parents or source in target.parents:
            raise ConfigurationError('storage path overlaps knowledge.root')
    for table in ('folders', 'concepts', 'folder_concepts'):
        store.catalog.build(table, handoff[table])
    store.catalog.build('images', images)
    store.catalog.build('folder_images', folder_images)
    if isinstance(image_embeddings, pa.Table):
        store.catalog.validate('image_embeddings', image_embeddings)
        vector_rows = image_embeddings.to_pylist()
    else:
        vector_rows = store.catalog.build('image_embeddings', image_embeddings, dim).to_pylist()
    groups = group_images(images, vector_rows, config['observe']['near_duplicate_cosine_distance'],
                          revision=embedder_revision, dim=dim)
    updated_images = [dict(row, near_duplicate_group=groups.get(row['image_id'])) for row in images]
    folders = {row['folder_id']: row for row in handoff['folders']}
    concepts = {row['concept_id']: row for row in handoff['concepts']}
    members, locations = {}, {}
    for row in handoff['folder_concepts']:
        if row['folder_id'] not in folders or row['concept_id'] not in concepts:
            raise ValueError('folder concept references an unknown folder or concept')
        if concept_id(row['name_as_written']) != row['concept_id']:
            raise ValueError('folder concept name disagrees with its concept identity')
        members.setdefault(row['folder_id'], []).append(_member(row))
    all_images = {row['image_id'] for row in images}
    for row in folder_images:
        if row['folder_id'] not in folders or row['image_id'] not in all_images:
            raise ValueError('image membership references an unknown folder or image')
        locations.setdefault(row['folder_id'], {}).setdefault(row['image_id'], []).append(
            (row['folder_id'], row['relative_path']))
    role = config['vlm']['roles']['observer']
    prompt = load_prompt(root, role['prompt_id'])
    revisions = observation_revisions(config, prompt, root)
    attempts_allowed = config['observe']['reply_attempts']
    report = {'observed': 0, 'skipped_done': 0, 'reused_referenced': [], 'queued': [], 'failed': {},
              'skipped_folders': [], 'ineligible_images': sorted(all_images - set(groups)), 'representatives': {},
              'attempts': {'allowed': attempts_allowed, 'used': {}}}
    tasks = {}
    for folder_id, folder in sorted(folders.items()):
        declared = _declared(members.get(folder_id, []))
        if folder['error'] or folder['name_source'] is None or not declared:
            report['skipped_folders'].append(folder_id)
            continue
        selected, rest = choose_representatives(set(locations.get(folder_id, {})) & set(groups), groups,
                                                config['observe']['representatives_per_folder'])
        report['representatives'][folder_id] = selected
        names = [item['name'] for item in declared]
        for identity in selected + rest:
            key = observation_key(identity, names, revisions)
            task = tasks.setdefault(key, {'image_id': identity, 'concepts': declared, 'selected': False, 'locations': []})
            task['selected'] |= identity in selected
            task['locations'].extend(locations[folder_id][identity])
    observations, artifacts, owned_run = [], [], None
    capacity = CapacityLog()
    if client is not None:
        capacity.watch(run_concurrency(client))
    try:
        with nullcontext(writer) if writer is not None else store.writer() as writer:
            checkpoints = writer.checkpoints(STAGE)

            async def evaluate(key, task):
                nonlocal owned_run
                identity, declared = task['image_id'], task['concepts']
                done = checkpoints.done(key)
                reusable = None if done else checkpoints.reusable(key)
                if done or reusable:
                    try:
                        bundle = json.loads(checkpoints.load(key))
                        if (bundle['version'] != LAYOUT or bundle['revisions'] != revisions
                                or bundle['concepts'] != declared or bundle['document']['image_id'] != identity
                                or bundle['document']['context_key'] != key):
                            raise ValueError('observation artifact does not match its cache key')
                        document = parse_observation(bundle['reply'], image_id=identity, concepts=declared,
                            context_key=key, model_revision=bundle['document']['origin']['model_revision'],
                            prompt_id=role['prompt_id'], facet_schema_version=config['observe']['facet_schema_version'],
                            project_root=root)
                        if document != bundle['document']:
                            raise ValueError('stored observation document disagrees with its reply')
                    except (ValueError, KeyError, TypeError, UnicodeError) as error:
                        raise StorageError(f'{STAGE}: CACHE_INVALID: invalid completed bundle {key}') from None
                    observations.extend(_rows(document))
                    artifacts.append(checkpoints.state(key)['output_key'])
                    report['skipped_done'] += 1
                    if reusable:
                        report['reused_referenced'].append(key)
                    return
                if not task['selected']:
                    if checkpoints.state(key) is None or checkpoints.state(key)['state'] != 'queued':
                        checkpoints.queue(key, revisions=revisions)
                    report['queued'].append(key)
                    return
                attempts = Attempts()
                try:
                    data = _read_verified(source, task['locations'], identity)
                    if data is None:
                        raise ValueError('SOURCE_CHANGED: image no longer matches K1 content')
                    if client is None:
                        if owned_run is None:
                            owned_run = ModelClientRun(config, root, probe_cases=probe_cases,
                                                       local_probe_plans=local_probe_plans)
                            capacity.watch(owned_run.concurrency)
                        bound = await owned_run.for_role('observer')
                    else:
                        bound = await client.for_role('observer')
                    payload = _observation_payload(declared, config, root)
                    text = canonical_json(payload)
                    images = [('knowledge_reference', data)]

                    def call(seed):  # attempt 0 is the profile's request; a retry changes only the seed
                        return (bound.call(prompt, text, images=images) if seed is None
                                else bound.call(prompt, text, images=images, seed=seed))

                    def parse(reply):
                        return parse_observation(reply, image_id=identity, concepts=declared, context_key=key,
                            model_revision=attempts.records[-1]['model_revision'], prompt_id=role['prompt_id'],
                            facet_schema_version=config['observe']['facet_schema_version'], project_root=root)

                    document, value, record = await call_until_valid(
                        call, parse, attempts=attempts_allowed, base_seed=role['sampling']['seed'], log=attempts,
                        persist=lambda log: _failure_row(writer, key, log, revisions))
                except (ValueError, TypeError, OverflowError, UnicodeError, ModelCallError) as error:
                    record_probe_failure(writer, key, error, revisions)
                    if attempts.records:
                        _failure_row(writer, key, attempts, revisions, error)
                        report['attempts']['used'][key] = len(attempts.entries)
                    checkpoints.fail(key, failure_code(error), revisions=revisions)
                    report['failed'][key] = failure_code(error)
                    return
                # The call, the source read and the reply are per-image failures; turning a validated reply into
                # rows is this stage's own work, so an error there fails the stage (P3-SWEEP-2).
                rows = _rows(document)
                store.catalog.build('observations', rows)
                bundle = _observation_bundle(revisions, declared, value, document, attempts)
                artifact = checkpoints.commit(key, STAGE, canonical_json(bundle).encode('utf-8'),
                                               revisions=revisions, call_records=attempts.records)
                report['attempts']['used'][key] = len(attempts.entries)
                artifacts.append(artifact)
                observations.extend(rows)
                report['observed'] += 1
            # Per-item flow (decision 149): the configured backend concurrency bounds both requests in flight and
            # prepared image bodies; a fatal error drains the items in flight so every reply received is checkpointed.
            workers = config['vlm']['backends'][role['backend']]['max_concurrency']
            await flow(sorted(tasks.items()), lambda item: evaluate(*item), workers)
    finally:
        try:
            report['capacity'] = capacity.close()
        finally:
            if owned_run is not None:
                await await_owned(owned_run.close())
    report['failed'] = dict(sorted(report['failed'].items()))
    # Completion order varies with timing; the serial order is the sorted key order.
    report['queued'].sort()
    report['reused_referenced'].sort()
    observations.sort(key=lambda row: (row['context_key'], row['concept_id'], row['facet']))
    store.catalog.build('observations', observations)
    return {'rows': {'images': updated_images, 'observations': observations}, 'report': report,
            'artifacts': sorted(set(artifacts))}
