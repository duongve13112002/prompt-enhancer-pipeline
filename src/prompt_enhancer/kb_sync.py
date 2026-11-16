"""Preflighted K0-to-K5 sync with one writer and publication last (R-014/R-017)."""

from pathlib import Path

from .config import COMMAND_READS, _get, _schema, validate_sync_config
from .k0 import guard_removals
from .k0_checkpoint import _REVISION, _VERSION, _bytes, _key, _matching
from .k0_sync import _previous_for_plan, sync
from .k1_embed import (MODEL_TOOL_MODULES, ConceptText, embed_concept_texts, embed_images, image_key, text_key,
                       embedder_settings, tool_revision)
from .k2_naming import _digest, _read_overrides, name_folders
from .k2_observe import observe_images
from .k3_cards import build_cards
from .k4_aliases import build_aliases
from .k5_index import ServingIndex, _tools, build_index
from .profile_prompt import load_prompt
from .storage import StorageError, open_knowledge_store
from .storage.tables import SNAPSHOT_TABLES


# Artifact-bound completion identity in the existing scan-summary metadata.
LAYOUT = 'kb-sync-v1'
ROLES = ('info_assessor', 'folder_namer', 'observer', 'card_builder', 'alias_generator')


def require_success(stage, result):
    """Partial rows cannot substitute for successful completion of a model stage."""
    failed = result['report']['failed']
    if failed:
        raise StorageError(f'{stage}: STAGE_FAILED: count={len(failed)}')


def concept_texts(rows):
    """Exact nonempty current strings only; pending aliases never enter vector lookup."""
    values = {(row['concept_id'], 'name', row['display_name']) for row in rows['concepts']}
    values.update((row['concept_id'], 'description', row['description']) for row in rows['folder_concepts'])
    values.update((row['concept_id'], 'alias', row['alias_text']) for row in rows['aliases']
                  if row['status'] == 'accepted')
    values.update((row['concept_id'], 'card_summary', row['summary']) for row in rows['cards'])
    return [ConceptText(*value) for value in sorted(value for value in values if value[2])]


def sync_identity(config, scanned, overrides, store):
    """Source identity excludes downstream metadata copied into subsequent K0 rows."""
    source = {
        'folders': [{key: row[key] for key in ('folder_id', 'info_file_hash', 'declared_names',
                                              'declared_descriptions', 'error')} for row in scanned['rows']['folders']],
        'images': [{key: row[key] for key in ('image_id', 'size_bytes')} for row in scanned['rows']['images']],
        'memberships': [{key: row[key] for key in ('folder_id', 'relative_path', 'image_id')}
                        for row in scanned['rows']['folder_images']],
        'overrides': overrides,
    }
    reads = {key: _get(config, key, 'configuration') for key in COMMAND_READS['kb sync']}
    names = {config['vlm']['default_backend'], config['embedding']['backend']}
    names.update(config['vlm']['roles'][role]['backend'] for role in ROLES)
    reads['backends'] = {name: config['vlm']['backends'][name] for name in sorted(names)}
    root = store.catalog.root
    prompts = {role: load_prompt(root, config['vlm']['roles'][role]['prompt_id']).hex() for role in ROLES}
    schemas = {name: _schema(root, name) for name in (
        'info_assessment.v0', 'folder_naming.v0', 'observer_reply.v0', 'image_observation.v0',
        'card_builder_reply.v0', 'concept_card.v0', 'alias_generator_reply.v0', 'alias_set.v0', 'kb_overrides.v0')}
    schemas.update({config['vlm']['roles'][role]['output']['schema_id']:
                    _schema(root, config['vlm']['roles'][role]['output']['schema_id']) for role in ROLES})
    return _digest({'layout': LAYOUT, 'source': source, 'config': reads, 'prompts': prompts, 'schemas': schemas,
                    'tools': _tools(store.catalog), 'code': tool_revision(
                        'kb_sync', 'k0', 'k0_scan', 'k0_checkpoint', 'k0_sync', 'k1_embed', 'k2_naming',
                        'k2_observe', 'k3_cards', 'k4_aliases', *MODEL_TOOL_MODULES)})


def _checkpoint(writer, scanned, knowledge):
    report = {key: value for key, value in scanned['report'].items() if key not in ('workers', 'guard', 'configured_root')}
    artifact = {'version': _VERSION, 'rows': scanned['rows'], 'report': report,
                'settings': {'info_names': list(knowledge['info_file']['names']),
                             'encoding': knowledge['info_file']['encoding'],
                             'extensions': list(knowledge['image_extensions']),
                             'separator': knowledge['concept_separator']}}
    checkpoints = writer.checkpoints('k0_scan')
    key = _key('k0_scan', artifact)
    if not _matching(checkpoints, key, _bytes(artifact)):
        checkpoints.commit(key, 'k0_scan', _bytes(artifact), revisions=_REVISION)
    return checkpoints.state(key)['output_key']


async def sync_knowledge(config, *, project_root, plan=False, force=False, client=None, transport=None):
    """Execute with optional caller-owned semantic client and labelled-test embedding transport.

    Normal callers supply neither seam; each stage lazily owns and closes its real configured HTTP client.
    The unchanged branch precedes builder/checkpoint/client creation. The writer prevents concurrent sync/GC.
    """
    validate_sync_config(config, project_root=project_root)
    if not config['rag']['enabled']:
        return {'status': 'skipped', 'reason': 'retrieval_disabled'}
    project = Path(project_root).resolve()
    knowledge = config['knowledge']
    source = project / knowledge['root']
    store = open_knowledge_store(config, 'kb sync', project_root=project)

    def scan():
        return sync(store, source, info_names=knowledge['info_file']['names'],
                    encoding=knowledge['info_file']['encoding'], extensions=knowledge['image_extensions'],
                    separator=knowledge['concept_separator'], workers=config['runtime']['cpu_workers'],
                    threshold=config['sync']['mass_deletion_guard']['max_removed_fraction'], plan=True, force=force)

    if plan:
        result = scan()
        result['status'] = 'planned'
        result['model_calls'] = {'conditional': True, 'reason': 'depends_on_model_replies',
                                 'roles': list(ROLES), 'image_inputs': len(result['rows']['images'])}
        return result
    with store.writer() as writer:
        scanned = scan()
        guard = scanned['report']['guard']
        guard_removals(scanned['plan']['folders'], guard['previous'], guard['threshold'], force=force)
        # Scan/source failures cannot become a partially refreshed serving snapshot.
        if any(scanned['report'][key] for key in ('unreadable', 'protected_folders', 'protected_info')):
            raise StorageError('k0: SOURCE_READ_FAILED')
        overrides = _read_overrides(knowledge, source.resolve(), project)
        identity = sync_identity(config, scanned, overrides, store)
        current = store.current()
        if current is not None:
            summary = store._registry()[current[0]]['source_scan_summary']
            if summary.get('sync_identity') == identity and summary.get('sync_layout') == LAYOUT:
                store.verify_snapshot(*current)
                _previous_for_plan(store, inspect=lambda pin: ServingIndex(pin, config))
                return {'status': 'unchanged', 'snapshot_id': current[0], 'report': scanned['report']}
        artifacts = {_checkpoint(writer, scanned, knowledge)}
        builder = writer.begin_snapshot(parent=current[0] if current else None)
        reports = {}

        def accept(stage, result):
            require_success(stage, result)
            reports[stage] = result['report']
            artifacts.update(result.get('artifacts', []))
            return result

        image = accept('k1_image', await embed_images(store, config, scanned['handoff']['k1'],
                       knowledge_root=source, transport=transport, writer=writer))
        rows = dict(scanned['rows'], images=image['images'], image_embeddings=image['image_embeddings'])
        named = accept('k2_naming', await name_folders(store, config, scanned['handoff']['k2'],
                       images=rows['images'], folder_images=rows['folder_images'], knowledge_root=source,
                       client=client, writer=writer))
        if named['overrides'] != overrides:
            raise StorageError('k2_naming: SOURCE_CHANGED: overrides')
        rows.update(named['rows'])
        handoff = {name: rows[name] for name in ('folders', 'concepts', 'folder_concepts')}
        observed = accept('k2_observation', await observe_images(store, config, handoff, images=rows['images'],
                          folder_images=rows['folder_images'], image_embeddings=rows['image_embeddings'],
                          embedder_revision=image['embedder_revision'], dim=image['dim'], knowledge_root=source,
                          client=client, writer=writer))
        rows.update(observed['rows'])
        cards = accept('k3', await build_cards(store, config, handoff, observations=rows['observations'],
                       images=rows['images'], folder_images=rows['folder_images'], snapshot_id=builder.snapshot_id,
                       overrides=named['effective_overrides'], client=client, writer=writer))
        rows.update(cards['rows'])
        handoff['concepts'] = rows['concepts']
        aliases = accept('k4', await build_aliases(store, config, handoff, cards=rows['cards'],
                         overrides=named['effective_overrides'], client=client, writer=writer))
        rows.update(aliases['rows'])
        items = concept_texts(rows)
        text = accept('k1_text', await embed_concept_texts(store, config, items, transport=transport, writer=writer))
        rows['concept_text_embeddings'] = text['concept_text_embeddings']
        settings = embedder_settings(config)
        for stage, keys in (('k1_image', [image_key(settings, row['image_id']) for row in rows['images']]),
                            ('k1_text', [text_key(settings, item.text) for item in items])):
            artifacts.update(writer.checkpoints(stage).state(key)['output_key'] for key in set(keys))
        for name in SNAPSHOT_TABLES:
            builder.write_table(name, rows[name], dim=image['dim'])
        builder.reference_artifacts(sorted(artifacts))
        index = build_index(builder, config)
        if index.get('validated') is not True:
            raise StorageError('k5: VALIDATION_REQUIRED')
        snapshot_id = builder.finish(source_scan_summary={'sync_layout': LAYOUT, 'sync_identity': identity})
        writer.publish(snapshot_id)
        return {'status': 'published', 'snapshot_id': snapshot_id, 'report': scanned['report'],
                'stages': reports, 'index': index}
