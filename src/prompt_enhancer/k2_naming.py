"""Unpublished K2 naming over K0's handoff; source text and model replies stay data (R-007/R-026)."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from random import SystemRandom

import PIL

from .config import (ConfigurationError, _EXACT_INTEGERS, _StrictSafeLoader, _lexical_path, _load_yaml,
                     _resolve_within, _schema, validate_naming_reads)
from .k0 import parse_info, trim_info_token
from .k0_scan import decode_info, read_source_file
from .k1_embed import _read_verified
from .model_client import ModelCallError, ModelClientRun
from .profile_prompt import load_prompt
from .storage import StorageError, artifact_key, concept_id, normalise_name
from .storage.identity import canonical_json, check_folder_id, is_image_id


# Artifact-bound: checkpoint layout/stages, independent of deployment choices.
LAYOUT = 1
DRAW_REVISION = {'model': 'none', 'prompt': 'none', 'schema': 'k2-draw-v1'}
TABLES = ('folders', 'concepts', 'folder_concepts')
ROLE_SCHEMAS = {'info_assessor': 'info_assessment.v0', 'folder_namer': 'folder_naming.v0'}


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _key(stage, value, revisions):
    return artifact_key(stage, _digest(value), revisions)


def _validate_reply(root, schema_id, value):
    error = next(_EXACT_INTEGERS(_schema(root, schema_id)).iter_errors(value), None)
    if error is not None:
        raise ValueError(f'{schema_id}: {error.message}')


def parse_assessment(value, parsed, *, project_root):
    """Validate the model shape and exact position coverage; return descriptions in declared order."""
    _validate_reply(project_root, 'info_assessment.v0', value)
    assigned = value['assigned_descriptions']
    needed = value['usable'] and parsed['status'] == 'description_mismatch' and bool(parsed['line2'])
    if not needed:
        if assigned is not None:
            raise ValueError('assigned_descriptions must be null without a usable nonempty mismatch')
        return None
    if assigned is None:
        raise ValueError('assigned_descriptions must cover every declared position')
    positions = [item['position'] for item in assigned]
    if sorted(positions) != list(range(len(parsed['names']))):
        raise ValueError('assigned_descriptions must cover every declared position exactly once')
    return [item['description'] for item in sorted(assigned, key=lambda item: item['position'])]


def parse_naming(value, folder_name, parsed, *, project_root, separator):
    """Validate names structurally; the model alone judges whether hints fit the images."""
    _validate_reply(project_root, 'folder_naming.v0', value)
    concepts = value['concepts']
    for item in concepts:
        # Safety-bound: K0 names occupy one CR/LF-delimited line, including before trimming.
        if '\n' in item['name'] or '\r' in item['name']:
            raise ValueError('generated name must contain no CR or LF')
        name = trim_info_token(item['name'])
        if not name or separator in name:
            raise ValueError('generated name must be nonblank and contain no concept separator')
        item['name'] = name
    names = [item['name'] for item in concepts]
    if len({concept_id(name) for name in names}) != len(names):
        raise ValueError('duplicate normalized generated concept names')
    if not value['images_consistent'] and len(concepts) > 1:
        raise ValueError('inconsistent images cannot declare several co-occurring concepts')
    declared = parsed['names'] if parsed is not None and parsed['status'] in ('parsed', 'description_mismatch') else []
    if not declared and value['info_file_fits'] is not None:
        raise ValueError('info_file_fits must be null without parsed names')
    if declared and value['info_file_fits'] is None:
        raise ValueError('info_file_fits must judge the declared names')
    source = 'model'
    if declared and value['info_file_fits']:
        if names != declared:
            raise ValueError('fitting info names must be kept exactly in declared order')
        source = 'info_file'
    elif value['folder_name_fits']:
        if names != [folder_name]:
            raise ValueError('fitting folder name must be used exactly as given')
        source = 'folder_name'
    return source


def parse_overrides(raw, *, project_root):
    """Read only the four approved curated keys, without executing YAML or guessing a fact's facet."""
    value = _load_yaml(raw, _StrictSafeLoader, 'knowledge.overrides_file')
    try:
        _validate_reply(project_root, 'kb_overrides.v0', value)
        canonical_json(value).encode('utf-8')  # YAML may accept surrogate escapes that cannot be stored as UTF-8.
    except (ValueError, RecursionError) as error:
        raise ConfigurationError(f'knowledge.overrides_file: {error}') from error
    normalized = [normalise_name(name) for name in value]
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError('knowledge.overrides_file: normalized concept-name collision')
    return value


def uniform_draw(image_ids, sample_size, rng):
    """Uniform sampling of distinct content ids; the persisted order is reused on resume."""
    if type(sample_size) is not int or sample_size <= 0:
        raise ValueError('sample_size must be positive')
    candidates = sorted(set(image_ids))
    return rng.sample(candidates, min(sample_size, len(candidates)))


def _load(checkpoints, key):
    if checkpoints.done(key) is None and checkpoints.reusable(key) is None:
        return None
    try:
        value = json.loads(checkpoints.load(key))
    except (ValueError, UnicodeError) as error:
        raise StorageError(f'{checkpoints.stage}: invalid JSON artifact') from error
    if not isinstance(value, dict) or value.get('version') != LAYOUT:
        raise StorageError(f'{checkpoints.stage}: unknown artifact layout')
    return value


def _save(checkpoints, key, value, revisions, call_records=()):
    return checkpoints.commit(key, checkpoints.stage,
                              canonical_json(dict(value, version=LAYOUT)).encode('utf-8'),
                              revisions=revisions, call_records=call_records)


def _role_revisions(config, name, prompt, root):
    role = config['vlm']['roles'][name]
    backends = config['vlm']['backends']
    identities = {backend: backends[backend] for backend in {role['backend'], config['vlm']['default_backend']}}
    return {'model': _digest({'role': role, 'backends': identities}),
            'prompt': _digest({'id': role['prompt_id'], 'sha256': hashlib.sha256(prompt).hexdigest()}),
            'schema': _digest({'layout': LAYOUT, 'contract': _schema(root, ROLE_SCHEMAS[name]),
                               'configured': _schema(root, role['output']['schema_id']),
                               'vision': config['vision'], 'pillow': PIL.__version__})}


def _read_info(folder, knowledge, root):
    expected = folder['info_file_hash']
    if expected is None:
        return None, None
    for filename in knowledge['info_file']['names']:
        try:
            raw = read_source_file(root, folder['folder_id'].split('/') + [filename])
        except FileNotFoundError:
            continue
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('SOURCE_CHANGED: info file no longer matches the K0 hash')
        text = decode_info(raw, knowledge['info_file']['encoding'])
        return text, parse_info(text, knowledge['concept_separator'])
    raise ValueError('SOURCE_CHANGED: scanned info file is missing')


def _read_overrides(knowledge, source, root):
    filename = knowledge['overrides_file']
    if filename == 'none':
        return {'file_hash': None, 'data': {}}
    try:
        raw = read_source_file(source, [filename])
    except FileNotFoundError:
        return {'file_hash': None, 'data': {}}
    except OSError as error:
        raise ConfigurationError('knowledge.overrides_file: cannot safely read file') from error
    return {'file_hash': 'sha256:' + hashlib.sha256(raw).hexdigest(),
            'data': parse_overrides(raw, project_root=root)}


def _recovery_index(writer, draws):
    """Read live references once; preserve snapshot groups and verify each referenced draw."""
    index, loaded = {}, {}
    for snapshot in writer.store.referenced_artifacts('k2_draw'):
        folders = {}
        for entry in snapshot['artifacts']:
            key = entry['path'].rsplit('/', 1)[-1].removesuffix('.bin')
            if draws.reusable(key) is None:
                continue
            state = draws.state(key)
            if state['output_sha256'] != entry['sha256'] or state['output_size'] != entry['size_bytes']:
                raise StorageError('k2_draw: checkpoint disagrees with referenced manifest')
            if key not in loaded:
                loaded[key] = _load(draws, key)
            draw = loaded[key]
            if draw is not None:
                folders.setdefault(draw['folder_id'], []).append((key, draw))
        for folder_id, candidates in folders.items():
            index.setdefault(folder_id, []).append(candidates)
    return index


def _referenced_draw(writer, namespace, draws, recovery):
    """Newest snapshot by stored created_at; ambiguity within a snapshot fails closed."""
    if 'index' not in recovery:
        recovery['index'] = _recovery_index(writer, draws)
    for candidates in recovery['index'].get(namespace['folder_id'], []):
        compatible = {key: draw for key, draw in candidates
                      if all(draw.get(field) == value for field, value in namespace.items())}
        if len(compatible) > 1:
            return None, None, sorted(compatible)
        if compatible:
            key, draw = next(iter(compatible.items()))
            return key, draw, []
    return None, None, []


def _redraw(writer, event, report, artifacts):
    checkpoints = writer.checkpoints('k2_redraw')
    key = _key('k2_redraw', event, DRAW_REVISION)
    if _load(checkpoints, key) is None:
        _save(checkpoints, key, event, DRAW_REVISION)
    artifacts.append(checkpoints.state(key)['output_key'])
    report['redraws'].append(event)
    return key


def _draw(writer, folder_id, candidates, settings, rng, report, artifacts, recovery):
    heads, draws = writer.checkpoints('k2_draw_head'), writer.checkpoints('k2_draw')
    namespace = {'folder_id': folder_id, 'sample_size': settings['sample_size'], 'selection': settings['selection']}
    head_key = _key('k2_draw_head', namespace, DRAW_REVISION)
    head = _load(heads, head_key)
    previous_key = head['draw_key'] if head is not None else None
    previous = _load(draws, previous_key) if previous_key is not None else None
    recovered = False
    ambiguous = []
    if previous is None:
        previous_key, previous, ambiguous = _referenced_draw(writer, namespace, draws, recovery)
        recovered = previous is not None
    reason = 'baseline_recovered' if recovered else 'baseline_ambiguous' if ambiguous else 'baseline_missing'
    if previous is not None:
        old, current = set(previous['candidate_image_ids']), set(candidates)
        missing = not set(previous['sampled_image_ids']) <= current
        changed = len(old ^ current) > settings['rename_on_change'] * len(old)
        if not missing and not changed:
            if recovered:
                event = dict(namespace, reason=reason, baseline_key=previous_key, draw_key=previous_key,
                             sampled_image_ids=previous['sampled_image_ids'])
                event_keys = [_redraw(writer, event, report, artifacts)]
                _save(heads, head_key, {'draw_key': previous_key, 'event_keys': event_keys}, DRAW_REVISION)
            else:
                for event_key in head.get('event_keys', []):
                    event = _load(writer.checkpoints('k2_redraw'), event_key)
                    if event is not None:
                        event.pop('version')
                        _redraw(writer, event, report, artifacts)
            return previous_key, previous
        reason = 'sample_member_missing' if missing else 'image_set_change'
    population = dict(namespace, candidate_image_ids=sorted(candidates))
    if ambiguous:
        # Artifact-bound context: draw fresh without overwriting a referenced population draw; replay is stable.
        population['ambiguous_baseline_keys'] = ambiguous
    key = _key('k2_draw', population, DRAW_REVISION)
    # Recover even if interrupted after the draw commit but before its head was updated.
    draw = _load(draws, key)
    if draw is None:
        draw = dict(population, sampled_image_ids=uniform_draw(candidates, settings['sample_size'], rng))
        _save(draws, key, draw, DRAW_REVISION)
    # Only this private head is replaceable. Snapshots may reference content-addressed draws/results, never heads.
    causes = ['baseline_recovered', reason] if recovered else [reason]
    event_keys = [_redraw(writer, dict(namespace, reason=cause, baseline_key=previous_key, draw_key=key,
                                     sampled_image_ids=draw['sampled_image_ids']), report, artifacts)
                  for cause in causes]
    _save(heads, head_key, {'draw_key': key, 'event_keys': event_keys}, DRAW_REVISION)
    return key, draw


def _replace_members(folder, members, concepts, values, source, revision):
    folder['name_source'], folder['error'] = source, ''
    updated = []
    previous = {row['concept_id']: row for row in members}
    for position, item in enumerate(values):
        name = item['name']
        identity = concept_id(name)
        concepts.setdefault(identity, {'concept_id': identity, 'normalised_name': normalise_name(name),
                                       'display_name': name, 'kind': None, 'kind_origin': None})
        old = previous.get(identity)
        curated = source == 'info_file' and old is not None and old['description_origin'] == 'info_file'
        updated.append({'folder_id': folder['folder_id'], 'concept_id': identity, 'position': position,
                        'name_as_written': name, 'description': old['description'] if curated else item['description'],
                        'description_origin': 'info_file' if curated else 'generated',
                        'description_revision': old['description_revision'] if curated else revision})
    return updated


async def name_folders(store, config, handoff, *, images, folder_images, knowledge_root,
                       client=None, rng=None, probe_cases=None, local_probe_plans=None):
    """Name K0's `handoff['k2']`, returning rows/report/artifact references without publishing.

    Image tables come from K0 (null decode status) or K1. A supplied BoundModelClient belongs to the caller;
    otherwise this stage owns and closes its lazy ModelClientRun. Probe cases/plans use T-009's existing API.
    """
    root = store.catalog.root
    validate_naming_reads(config, project_root=root)
    project = Path(root).resolve()
    forbidden = {os.path.normcase(name) for name in ('.secrets', 'models', '.git', '.venv')}
    source = _resolve_within(_lexical_path(config['knowledge']['root'], project, project, 'knowledge.root'),
                            project, forbidden, project, 'knowledge.root')
    if Path(knowledge_root).resolve() != source:
        raise ConfigurationError('knowledge.root: stage argument does not match configured root')
    for target in (Path(store.root).resolve(),
                   _resolve_within(_lexical_path(config['storage']['data_root'], project, project, 'storage.data_root'),
                                   project, forbidden, project, 'storage.data_root')):
        if target == source or target in source.parents or source in target.parents:
            raise ConfigurationError('storage path overlaps knowledge.root')
    rows = {table: deepcopy(handoff[table]) for table in TABLES}
    for table in TABLES:
        store.catalog.build(table, rows[table])
    knowledge, naming = config['knowledge'], config['knowledge']['naming']
    overrides = _read_overrides(knowledge, source, root)  # malformed overrides refuse before all client activity
    inputs = {}
    source_failures = {}
    for folder in rows['folders']:
        identity = check_folder_id(folder['folder_id'])
        if folder['error'] not in ('', 'pending_naming'):
            continue
        try:
            inputs[identity] = _read_info(folder, knowledge, source)
        except (OSError, UnicodeError, ValueError) as error:
            source_failures[identity] = str(error)
            continue
        parsed = inputs[identity][1]
        if (not naming['assess_info_file'] and parsed is not None
                and parsed['status'] == 'description_mismatch'):
            raise ConfigurationError(f'knowledge.naming.assess_info_file: false with description mismatch in {identity}')
    prompts = {name: load_prompt(root, config['vlm']['roles'][name]['prompt_id']) for name in ROLE_SCHEMAS}
    revisions = {name: _role_revisions(config, name, prompts[name], root) for name in ROLE_SCHEMAS}
    by_image = {row['image_id']: row for row in images}
    folder_ids = {folder['folder_id'] for folder in rows['folders']}
    locations = {}
    for row in folder_images:
        if row['folder_id'] not in folder_ids:
            raise ValueError('image membership references an unknown folder')
        if row['image_id'] not in by_image or not is_image_id(row['image_id']):
            raise ValueError('image membership references an unknown image')
        locations.setdefault(row['folder_id'], {}).setdefault(row['image_id'], []).append(
            (row['folder_id'], row['relative_path']))
    concepts = {row['concept_id']: row for row in rows['concepts']}
    members = {}
    for row in rows['folder_concepts']:
        members.setdefault(row['folder_id'], []).append(row)
    report = {'model_named_folders': [], 'assessed_folders': [], 'description_assignments': [],
              'skipped_folders': [], 'refused_folders': [], 'inconsistent_folders': [], 'unclear_folders': [],
              'redraws': [], 'retained_declared_folders': [], 'failed': dict(source_failures), 'unmatched_overrides': [],
              'override_renaming_supported': False, 'skipped_done': 0}
    artifacts = []
    owned_run = None
    random = rng if rng is not None else SystemRandom()

    async def call(name, text, attachments):
        nonlocal owned_run
        required = ('multiple_images',) if len(attachments) > 1 else ()
        if client is not None:
            bound = await client.for_role(name, require=required)
        else:
            if owned_run is None:
                owned_run = ModelClientRun(config, root, probe_cases=probe_cases or {},
                                           local_probe_plans=local_probe_plans or {})
            bound = await owned_run.for_role(name, require=required)
        payload = {key: value for key, value in text.items() if key not in ('folder_id', 'sampled_image_ids')}
        if 'sampled_image_ids' in text:
            payload['image_labels'] = list(range(len(text['sampled_image_ids'])))
        return await bound.call(prompts[name], canonical_json(payload), images=attachments)

    async def evaluate(writer, stage, key, name, text, attachments, validate):
        checkpoints = writer.checkpoints(stage)
        cached = _load(checkpoints, key)
        if cached is not None:
            validate(cached['reply'])
            report['skipped_done'] += 1
            artifacts.append(checkpoints.state(key)['output_key'])
            return cached
        value, record = await call(name, text, attachments)
        if value is None:
            diagnostic = writer.checkpoints('k2_call_failure')
            _save(diagnostic, key, {'status': record['status']}, revisions[name], [record])
            raise ValueError(record['status'])
        try:
            validate(value)
        except ValueError:
            diagnostic = writer.checkpoints('k2_call_failure')
            _save(diagnostic, key, {'status': 'SCHEMA_INVALID'}, revisions[name], [dict(record, status='SCHEMA_INVALID')])
            raise
        result = {'reply': value, 'folder_id': text['folder_id'], 'origin': 'generated',
                  'model_revision': record['model_revision'], 'sampled_image_ids': text.get('sampled_image_ids', [])}
        artifacts.append(_save(checkpoints, key, result, revisions[name], [record]))
        return result

    try:
        with store.writer() as writer:
            recovery = {}
            override_key = _key('k2_overrides', overrides, DRAW_REVISION)
            override_checkpoints = writer.checkpoints('k2_overrides')
            if _load(override_checkpoints, override_key) is None:
                _save(override_checkpoints, override_key, overrides, DRAW_REVISION)
            artifacts.append(override_checkpoints.state(override_key)['output_key'])
            for folder in sorted(rows['folders'], key=lambda row: row['folder_id']):
                identity = folder['folder_id']
                if folder['error'] not in ('', 'pending_naming'):
                    report['refused_folders'].append(identity)
                    continue
                if identity in source_failures:
                    continue
                text, parsed = inputs[identity]
                folder_name = identity.rsplit('/', 1)[-1]
                context = {'folder_id': identity, 'folder_name': folder_name, 'info_file_text': text,
                           'names': [{'position': pos, 'name': name} for pos, name in enumerate(
                               parsed['names'] if parsed is not None else [])]}
                stage, key = 'k2_assessment', None
                try:
                    usable = parsed is not None and parsed['status'] in ('parsed', 'description_mismatch')
                    if usable and naming['assess_info_file']:
                        context['assignment_required'] = parsed['status'] == 'description_mismatch' and bool(parsed['line2'])
                        key = _key(stage, context, revisions['info_assessor'])
                        assessed = await evaluate(writer, stage, key, 'info_assessor', context, (),
                                                  lambda value: parse_assessment(value, parsed, project_root=root))
                        usable = assessed['reply']['usable']
                        report['assessed_folders'].append(identity)
                        descriptions = parse_assessment(assessed['reply'], parsed, project_root=root)
                        if descriptions is not None:
                            for member in members.get(identity, []):
                                member.update(description=descriptions[member['position']], description_origin='generated',
                                              description_revision=assessed['model_revision'])
                            report['description_assignments'].append(identity)
                        if not usable:
                            report['unclear_folders'].append(identity)
                    if usable:
                        folder['name_source'], folder['error'] = 'info_file', ''
                        continue
                    candidates = sorted(image for image in locations.get(identity, {})
                                        if by_image[image]['decode_status'] in (None, 'ok'))
                    if not candidates:
                        report['skipped_folders'].append(identity)
                        if parsed is not None and parsed['status'] in ('parsed', 'description_mismatch'):
                            folder['name_source'], folder['error'] = 'info_file', ''
                            report['retained_declared_folders'].append(identity)
                        else:
                            members[identity] = []
                            folder['name_source'] = None
                        continue
                    draw_key, draw = _draw(writer, identity, candidates, naming, random, report, artifacts, recovery)
                    artifacts.append(writer.checkpoints('k2_draw').state(draw_key)['output_key'])
                    stage = 'k2_naming'
                    context['sampled_image_ids'] = draw['sampled_image_ids']
                    key = _key(stage, {'context': context, 'draw_key': draw_key}, revisions['folder_namer'])
                    checkpoints = writer.checkpoints(stage)
                    # A completed result needs no image read. New calls verify the exact persisted draw, never replace it.
                    attachments = []
                    if checkpoints.done(key) is None and checkpoints.reusable(key) is None:
                        for image in draw['sampled_image_ids']:
                            raw = _read_verified(source, locations[identity][image], image)
                            if raw is None:
                                raise ValueError('SOURCE_CHANGED: sampled image is missing or changed')
                            attachments.append(('knowledge_reference', raw))
                    named = await evaluate(writer, stage, key, 'folder_namer', context, attachments,
                                           lambda value: parse_naming(value, folder_name, parsed, project_root=root,
                                                                      separator=knowledge['concept_separator']))
                    reply = named['reply']
                    name_source = parse_naming(reply, folder_name, parsed, project_root=root,
                                               separator=knowledge['concept_separator'])
                    members[identity] = _replace_members(folder, members.get(identity, []), concepts,
                                                          reply['concepts'], name_source, named['model_revision'])
                    if not reply['images_consistent']:
                        report['inconsistent_folders'].append(identity)
                    if naming['report']:
                        report['model_named_folders'].append({'folder_id': identity, 'name_source': name_source,
                                                             'sampled_image_ids': draw['sampled_image_ids'],
                                                             'origin': 'generated',
                                                             'model_revision': named['model_revision'],
                                                             'concepts': deepcopy(reply['concepts']),
                                                             'reason': reply['reason']})
                except (ModelCallError, ValueError, OSError) as error:
                    report['failed'][identity] = str(error)
                    if key is not None:
                        writer.checkpoints(stage).fail(key, str(error), revisions=revisions[
                            'info_assessor' if stage == 'k2_assessment' else 'folder_namer'])
            rows['folder_concepts'] = sorted([row for values in members.values() for row in values],
                                             key=lambda row: (row['folder_id'], row['position']))
            wanted = {row['concept_id'] for row in rows['folder_concepts']}
            rows['concepts'] = [concepts[key] for key in sorted(wanted)]
            for concept in rows['concepts']:
                if concept['kind_origin'] == 'override':
                    concept.update(kind=None, kind_origin=None)  # a removed override must not survive as cached truth
            for name, data in overrides['data'].items():
                identity = concept_id(name)
                if identity not in wanted:
                    report['unmatched_overrides'].append(name)
                elif 'concept_type' in data:
                    concepts[identity].update(kind=data['concept_type'], kind_origin='override')
            for table in TABLES:
                store.catalog.build(table, rows[table])
    finally:
        if owned_run is not None:
            await owned_run.close()
    return {'rows': rows, 'report': report, 'overrides': overrides, 'artifacts': sorted(set(artifacts))}
