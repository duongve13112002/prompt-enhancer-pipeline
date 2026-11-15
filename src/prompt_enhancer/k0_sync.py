"""Unpublished K0 sync and the handoff to later knowledge stages."""

from pathlib import Path

from .k0 import diff_rows, guard_removals, resolve_workers, rows_from_scan
from .k0_checkpoint import (_FileCache, _REVISION, _TABLES, _VERSION, _bytes, _key,
                            _matching, _merge_previous, _scan_previous)
from .k0_scan import scan_source, validate_scan
from .storage import StorageError
from .storage.store import keep_outcome


def _previous_for_plan(store):
    """Pin while reading; remove only newly created empty lease directories."""
    if not store.root.exists():
        return {name: [] for name in _TABLES}
    if not store.path('LOCK').exists():
        # A valid publisher creates LOCK before CURRENT and never removes it.
        if store.path('CURRENT').exists():
            raise StorageError('CURRENT exists without the store lock file')
        return {name: [] for name in _TABLES}
    lease_root = store.path('leases')
    existed = lease_root.exists()
    old_dirs = {path for _, path in store._children('leases')} if existed else set()
    pinned = store.pin()
    if pinned is None:
        return {name: [] for name in _TABLES}
    new_dir = (lease_root / pinned.snapshot_id) not in old_dirs

    def remove_new_directories():
        with store.store_lock():
            # anchored removal: a lease directory swapped for a link since any check is refused, not followed
            if new_dir:
                try:
                    store._remove(f'leases/{pinned.snapshot_id}', directory=True)
                except OSError:
                    pass  # another reader may hold a lease
            if not existed:
                try:
                    store._remove('leases', directory=True)
                except OSError:
                    pass

    outcome = None
    try:
        with pinned:  # a failed release never replaces this result or exception (P3-B)
            return {name: pinned.read_rows(name) for name in _TABLES}
    except BaseException as error:
        outcome = error
        raise
    finally:
        keep_outcome(remove_new_directories, outcome)


def sync(store, root, *, info_names, encoding, extensions, separator, workers, threshold,
         plan=False, force=False, _resolved_root=None):
    """Return a plan and K1/K2 rows; never publish an incomplete snapshot."""
    workers = resolve_workers(workers)
    validate_scan(info_names, encoding, extensions, separator, workers)
    guard_removals({'removed': []}, 0, threshold, force=True)
    resolved_root = Path(_resolved_root) if _resolved_root is not None else Path(root).resolve()
    store_root = Path(store.root).resolve()
    if resolved_root == store_root or resolved_root in store_root.parents or store_root in resolved_root.parents:
        raise ValueError('storage.kb_store overlaps knowledge.root')

    def run(previous, cache):
        scanned = scan_source(root, info_names=info_names, encoding=encoding, extensions=extensions,
                              separator=separator, previous=_scan_previous(previous), cache=cache,
                              workers=workers, resolved_root=resolved_root)
        rows = _merge_previous(rows_from_scan(scanned['folders'], separator), previous, scanned['report'])
        diff = diff_rows(previous, rows)
        removed = len(diff['folders']['removed'])
        total = len(previous['folders'])
        fraction = guard_removals(diff['folders'], total, threshold, force=True)
        old_members = {(row['folder_id'], row['relative_path']): row['image_id']
                       for row in previous['folder_images']}
        new_members = {(row['folder_id'], row['relative_path']): row['image_id']
                       for row in rows['folder_images']}
        old_unmatched = {old_members[key] for key in diff['folder_images']['removed']}
        new_unmatched = {new_members[key] for key in diff['folder_images']['added']}
        report = dict(scanned['report'], workers=workers,
                      refused_folders=sorted(set(scanned['report']['duplicate_names']) |
                                             set(scanned['report']['protected_folders'])),
                      ambiguous_matches={'folders': diff['folders']['ambiguous'],
                                         'images': sorted(old_unmatched & new_unmatched)},
                      pending_folders=[row['folder_id'] for row in rows['folders']
                                       if row['error'] == 'pending_naming'],
                      model_named_folders=[row['folder_id'] for row in rows['folders']
                                           if row['name_source'] == 'model'],
                      guard={'removed': removed, 'previous': total, 'fraction': fraction,
                             'threshold': threshold, 'exceeded': fraction > threshold, 'forced': force})
        return {'rows': rows, 'plan': diff, 'report': report,
                'handoff': {'k1': {'images': rows['images'], 'folder_images': rows['folder_images']},
                            'k2': {'folders': rows['folders'], 'concepts': rows['concepts'],
                                   'folder_concepts': rows['folder_concepts']}}}

    if plan:
        return run(_previous_for_plan(store), None)
    with store.writer() as writer:
        previous = _previous_for_plan(store)
        cache = _FileCache(writer.checkpoints('k0_file'), writer.checkpoints('k0_content'), resolved_root)
        result = run(previous, cache)
        guard_removals(result['plan']['folders'], len(previous['folders']), threshold, force=force)
        report = {key: value for key, value in result['report'].items()
                  if key not in ('workers', 'guard', 'configured_root')}
        artifact = {'version': _VERSION, 'rows': result['rows'], 'report': report,
                    'settings': {'info_names': list(info_names), 'encoding': encoding,
                                 'extensions': list(extensions), 'separator': separator}}
        checkpoints = writer.checkpoints('k0_scan')
        key = _key('k0_scan', artifact)
        if not _matching(checkpoints, key, _bytes(artifact)):
            checkpoints.commit(key, 'k0_scan', _bytes(artifact), revisions=_REVISION)
        result['checkpoint'] = key
        return result


def sync_config(config, *, project_root, plan=False, force=False):
    """Run K0 with a configuration already validated for kb sync."""
    from .storage import open_knowledge_store

    knowledge = config['knowledge']
    from .config import _lexical_path, _resolve_within, ConfigurationError
    import os
    project = Path(project_root).resolve()
    resolved = {}
    for key, value in (('knowledge.root', knowledge['root']), ('storage.kb_store', config['storage']['kb_store']),
                       ('storage.data_root', config['storage']['data_root'])):
        resolved[key] = _resolve_within(_lexical_path(value, project, project, key), project,
                                        {os.path.normcase('.secrets'), os.path.normcase('models')}, project, key)
    source = resolved['knowledge.root']
    for key in ('storage.kb_store', 'storage.data_root'):
        target = resolved[key]
        if target == source or target in source.parents or source in target.parents:
            raise ConfigurationError(f'{key} overlaps knowledge.root')
    store = open_knowledge_store(config, 'kb sync', project_root=project_root)
    root = Path(project_root) / knowledge['root']
    return sync(store, root, info_names=knowledge['info_file']['names'],
                encoding=knowledge['info_file']['encoding'], extensions=knowledge['image_extensions'],
                separator=knowledge['concept_separator'], workers=config['runtime']['cpu_workers'],
                threshold=config['sync']['mass_deletion_guard']['max_removed_fraction'], plan=plan, force=force,
                _resolved_root=source)
