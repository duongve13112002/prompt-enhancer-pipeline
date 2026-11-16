"""Unpublished K4 aliases: model text stays data; code owns identity and collision status (R-026)."""

import asyncio
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from .config import ConfigurationError, _lexical_path, _resolve_within, _schema, validate_alias_reads
from .k2_naming import _digest, _validate_reply, failure_code
from .k1_embed import MODEL_TOOL_MODULES, tool_revision
from .model_client import ModelCallError, ModelClientRun, semantic_role_settings
from .profile_prompt import load_prompt
from .storage import StorageError, artifact_key, concept_id, normalise_name
from .storage.identity import canonical_json


# Artifact/protocol-bound checkpoint layout and schemas, independent of deployment settings.
LAYOUT = 1
REPLY_SCHEMA = 'alias_generator_reply.v0'
STORED_SCHEMA = 'alias_set.v0'


def parse_alias_reply(value, *, project_root):
    """Validate even injected replies; no lexical rule interprets model text."""
    _validate_reply(project_root, REPLY_SCHEMA, value)
    canonical_json(value).encode('utf-8')
    return deepcopy(value)


def mark_collisions(concepts, rows):
    """Group once by NFKC/casefold, including canonical names; retain raw spellings."""
    owners = {}
    for concept in concepts:
        owners.setdefault(normalise_name(concept['display_name']), set()).add(concept['concept_id'])
    for row in rows:
        owners.setdefault(normalise_name(row['alias_text']), set()).add(row['concept_id'])
    result, findings = [], []
    for original in rows:
        row = deepcopy(original)
        peers = sorted(owners[normalise_name(row['alias_text'])] - {row['concept_id']})
        row['collides_with'] = peers
        row['status'] = 'pending_review' if peers else 'accepted'
        result.append(row)
        if peers:
            findings.append({'code': 'ALIAS_COLLISION', 'concept_id': row['concept_id'],
                             'alias_text': row['alias_text'], 'collides_with': peers})
    return result, findings


def _record_revisions(record, config):
    role = config['vlm']['roles']['alias_generator']
    if (not isinstance(record, dict) or record.get('status') != 'OK'
            or record.get('role') != 'alias_generator' or record.get('prompt_id') != role['prompt_id']):
        raise ValueError('alias generator returned an invalid call record')
    backend = record.get('backend')
    if backend not in {role['backend'], config['vlm']['default_backend']}:
        raise ValueError('alias call record has an unexpected backend')
    revision = record.get('model_revision')
    if (not isinstance(revision, str) or not revision
            or revision != config['vlm']['backends'][backend]['model']['revision']):
        raise ValueError('alias call record has an invalid model revision')
    return {'model_revision': revision, 'prompt_id': record['prompt_id']}


def _inputs(store, config, handoff, cards, overrides):
    """Validate current tables and index memberships once before opening a writer."""
    root = store.catalog.root
    forbidden = {'.secrets', 'models', '.git', '.venv'}

    def resolved(key):
        section, field = key.split('.')
        return _resolve_within(_lexical_path(config[section][field], root, root, key),
                               Path(root), forbidden, Path(root), key)

    source, target, data = (resolved(key) for key in ('knowledge.root', 'storage.kb_store', 'storage.data_root'))
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
    for destination in (target, data):
        if source == destination or source in destination.parents or destination in source.parents:
            raise ConfigurationError('storage path overlaps knowledge.root')
    for table in ('folders', 'concepts', 'folder_concepts'):
        store.catalog.build(table, handoff[table])
    store.catalog.build('cards', cards)
    _validate_reply(root, 'kb_overrides.v0', overrides['data'])
    canonical_json(overrides).encode('utf-8')
    concepts = {}
    for row in handoff['concepts']:
        if concept_id(row['display_name']) != row['concept_id']:
            raise ValueError('alias canonical name disagrees with its concept identity')
        concepts[row['concept_id']] = row
    folders = {row['folder_id'] for row in handoff['folders']}
    descriptions = {identity: set() for identity in concepts}
    for member in handoff['folder_concepts']:
        if member['concept_id'] not in concepts or member['folder_id'] not in folders:
            raise ValueError('alias membership references an unknown concept or folder')
        if member['description'] is not None:
            descriptions[member['concept_id']].add(member['description'])
    summaries = {}
    for card in cards:
        if card['concept_id'] not in concepts:
            raise ValueError('alias card references an unknown concept')
        if concept_id(card['name']) != card['concept_id']:
            raise ValueError('alias card name disagrees with its concept identity')
        summaries[card['concept_id']] = card['summary']
    if config['aliases']['generated'] and summaries.keys() != concepts.keys():
        raise ValueError('alias generation requires exactly one current card for every concept')
    effective = {}
    for name, override in overrides['data'].items():
        if any(not text for text in override.get('aliases', [])):
            raise ValueError('author alias text must be nonempty')
        identity = concept_id(name)
        if identity in effective:
            raise ValueError('alias overrides duplicate a normalized concept name')
        effective[identity] = override
    return concepts, descriptions, summaries, effective


async def build_aliases(store, config, handoff, *, cards, overrides, client=None,
                        probe_cases=None, local_probe_plans=None, writer=None):
    """Return aliases, collision lint and artifact references; never scan the KB or publish.

    handoff is the effective naming output; cards are current K3 rows. An injected client is caller-owned.
    """
    root = store.catalog.root
    validate_alias_reads(config, project_root=root)
    concepts, descriptions, summaries, effective = _inputs(store, config, handoff, cards, overrides)
    report = {'generated': 0, 'reused': 0, 'failed': {}, 'lint_findings': [],
              'unmatched_overrides': sorted(name for name in overrides['data'] if concept_id(name) not in concepts)}
    role = config['vlm']['roles']['alias_generator']
    generate = config['aliases']['generated']
    prompt = load_prompt(root, role['prompt_id']) if generate else None
    # Hashing absence for author-only checkpoint identity is not stored model provenance.
    revisions = {'model': _digest(semantic_role_settings(config, 'alias_generator') if generate else None),
                 'prompt': _digest({'id': role['prompt_id'], 'sha256': hashlib.sha256(prompt).hexdigest()}
                                   if generate else None),
                 'schema': _digest({'layout': LAYOUT, 'reply': _schema(root, REPLY_SCHEMA),
                                   'configured': _schema(root, role['output']['schema_id']),
                                   'tool': tool_revision('k4_aliases', 'k2_naming', *MODEL_TOOL_MODULES)})}
    candidates, artifacts = {}, []
    owned_run = None
    try:
        with nullcontext(writer) if writer is not None else store.writer() as writer:
            generated = writer.checkpoints('k4_generation')

            async def evaluate(concept):
                nonlocal owned_run
                identity = concept['concept_id']
                payload = {'concept_name': concept['display_name'], 'descriptions': sorted(descriptions[identity]),
                           'card_summary': summaries[identity], 'languages': deepcopy(config['aliases']['languages'])}
                content = {'concept_id': identity, 'name_hash': _digest(payload['concept_name']),
                           'description_hash': _digest(payload['descriptions']),
                           'summary_hash': _digest(payload['card_summary']), 'languages_hash': _digest(payload['languages'])}
                key = artifact_key('k4_generation', _digest(content), revisions)
                record = None
                try:
                    cached = generated.done(key) or generated.reusable(key)
                    if cached:
                        try:
                            bundle = json.loads(generated.load(key))
                            if (bundle['version'] != LAYOUT or bundle['revisions'] != revisions
                                    or bundle['payload'] != payload or bundle['key'] != key
                                    or cached['call_records'] != [bundle['record']]):
                                raise ValueError('completed alias bundle does not match input or call record')
                            reply = parse_alias_reply(bundle['reply'], project_root=root)
                            record = bundle['record']
                            provenance = _record_revisions(record, config)
                        except (ValueError, KeyError, TypeError, UnicodeError) as error:
                            raise StorageError('k4_generation: CACHE_INVALID: invalid completed bundle') from None
                        artifact = cached['output_key']
                        report['reused'] += 1
                    else:
                        if client is None:
                            if owned_run is None:
                                owned_run = ModelClientRun(config, root, probe_cases=probe_cases or {},
                                                           local_probe_plans=local_probe_plans or {})
                            bound = await owned_run.for_role('alias_generator')
                        else:
                            bound = await client.for_role('alias_generator')
                        value, record = await bound.call(prompt, canonical_json(payload))
                        if value is None:
                            raise ValueError(record['status'])
                        reply = parse_alias_reply(value, project_root=root)
                        provenance = _record_revisions(record, config)
                        bundle = {'version': LAYOUT, 'revisions': revisions, 'payload': payload,
                                  'key': key, 'reply': reply, 'record': record}
                        artifact = generated.commit(key, 'k4_generation', canonical_json(bundle).encode('utf-8'),
                                                    revisions=revisions, call_records=[record])
                        report['generated'] += 1
                    candidates[identity] = (reply['aliases'], provenance)
                    artifacts.append(artifact)
                except (ValueError, TypeError, UnicodeError, ModelCallError) as error:
                    if record is not None:
                        writer.checkpoints('k4_call_failure').commit(
                            key, 'k4_call_failure', canonical_json({'error': failure_code(error)}).encode('utf-8'),
                            revisions=revisions, call_records=[record])
                    generated.fail(key, failure_code(error), revisions=revisions)
                    report['failed'][identity] = failure_code(error)

            ordered = [concepts[key] for key in sorted(concepts)]
            if generate:
                workers = config['vlm']['backends'][role['backend']]['max_concurrency']
                for start in range(0, len(ordered), workers):
                    pending = [asyncio.create_task(evaluate(concept)) for concept in ordered[start:start + workers]]
                    try:
                        await asyncio.gather(*pending)
                    except BaseException:
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        raise
            rows = []
            for concept in ordered:
                identity = concept['concept_id']
                merged = {}
                aliases, provenance = candidates.get(identity, ([], None))
                for item in aliases:
                    # Same raw spelling can occur repeatedly in a model reply; deterministic first candidate wins.
                    merged.setdefault(item['text'], {'concept_id': identity, 'alias_text': item['text'],
                                      'origin': 'generated', 'language': item['language'], 'revisions': provenance})
                for text in effective.get(identity, {}).get('aliases', []):
                    merged[text] = {'concept_id': identity, 'alias_text': text, 'origin': 'author', 'language': None,
                                    'revisions': {'model_revision': None, 'prompt_id': None}}
                rows.extend(merged[text] for text in sorted(merged))
            rows, findings = mark_collisions(ordered, rows)
            report['lint_findings'] = findings
            report['lint_findings'].extend({'code': 'UNMATCHED_OVERRIDE', 'message': name, 'refs': []}
                                           for name in report['unmatched_overrides'])
            report['lint_findings'].extend(deepcopy(overrides.get('lint_findings', [])))
            report['lint_findings'].extend({'code': 'UNMATCHED_FOLDER_OVERRIDE', 'message': path, 'refs': []}
                                           for path in overrides.get('unmatched_folders', []))
            for row in rows:
                item = {'text': row['alias_text'], **{field: row[field] for field in
                        ('origin', 'language', 'status', 'collides_with')}}
                _validate_reply(root, STORED_SCHEMA,
                                {'concept_id': row['concept_id'], 'aliases': [item], 'revisions': row['revisions']})
            store.catalog.build('aliases', rows)
            document_revisions = dict(revisions, schema=_digest({'layout': LAYOUT, 'stored': _schema(root, STORED_SCHEMA),
                                                                 'tool': revisions['schema']}))
            document_key = artifact_key('k4_aliases', _digest(rows), document_revisions)
            documents = writer.checkpoints('k4_aliases')
            cached = documents.done(document_key) or documents.reusable(document_key)
            if cached:
                if json.loads(documents.load(document_key)) != rows:
                    raise StorageError('k4_aliases: completed rows disagree with current aliases')
                artifacts.append(cached['output_key'])
            else:
                artifacts.append(documents.commit(document_key, 'k4_aliases', canonical_json(rows).encode('utf-8'),
                                                  revisions=document_revisions))
    finally:
        if owned_run is not None:
            await owned_run.close()
    report['lint_findings'].sort(key=canonical_json)
    return {'rows': {'aliases': rows}, 'report': report, 'artifacts': sorted(set(artifacts))}
