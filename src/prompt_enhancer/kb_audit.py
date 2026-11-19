"""Seeded inspection of one pinned published snapshot; no model or knowledge-source access."""

from contextlib import contextmanager
import errno
from random import Random

from .storage import StorageError, open_knowledge_store
from .storage.store import keep_outcome
from .storage.identity import canonical_json


@contextmanager
def audit_snapshot(store):
    """Release the pin and remove only new empty lease directories, preserving concurrent readers."""
    if not store.root.exists():
        yield None
        return
    if not store.path('LOCK').exists():
        if store.path('CURRENT').exists():
            raise StorageError('CURRENT exists without the store lock file')
        yield None
        return
    lease_root = store.path('leases')
    existed = lease_root.exists()
    old_dirs = {name for name, _ in store._children('leases')} if existed else set()
    pinned = store.pin()
    if pinned is None:
        yield None
        return
    outcome = None

    def cleanup():
        with store.store_lock():
            if pinned.snapshot_id not in old_dirs:
                try:
                    store._remove(f'leases/{pinned.snapshot_id}', directory=True)
                except OSError as error:
                    if error.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                        raise  # keep_outcome reports real cleanup failures; peers may own nonempty directories.
            if not existed:
                try:
                    store._remove('leases', directory=True)
                except OSError as error:
                    if error.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                        raise

    try:
        with pinned:
            yield pinned
    except BaseException as error:
        outcome = error
        raise
    finally:
        keep_outcome(cleanup, outcome)


def audit(store, *, folder_count, card_count, seed):
    """Sample sorted populations without replacement using required, validated caller choices."""
    for key, value in (('folder_count', folder_count), ('card_count', card_count), ('seed', seed)):
        if type(value) is not int or (key != 'seed' and value < 0):
            raise ValueError(f'{key}: expected ' + ('an integer' if key == 'seed' else 'a non-negative integer'))
    result = {'status': 'SNAPSHOT_MISSING', 'snapshot_id': None, 'seed': seed,
              'requested': {'folders': folder_count, 'cards': card_count},
              'actual': {'folders': 0, 'cards': 0}, 'folders': [], 'cards': []}
    # Protocol-bound population labels keep each sample independent of the other's requested count.
    folder_rng = Random(canonical_json({'seed': seed, 'population': 'folders'}))
    card_rng = Random(canonical_json({'seed': seed, 'population': 'cards'}))
    with audit_snapshot(store) as pinned:
        if pinned is None:
            return result
        result.update(status='OK', snapshot_id=pinned.snapshot_id)
        if folder_count:
            population = sorted((row for row in pinned.read_rows('folders') if row['name_source'] == 'model'),
                                key=lambda row: row['folder_id'])
            selected = folder_rng.sample(population, min(folder_count, len(population)))
            names = pinned.read_rows('folder_concepts')
            result['folders'] = [dict(row, concepts=sorted(
                (member for member in names if member['folder_id'] == row['folder_id']),
                key=lambda member: member['position'])) for row in selected]
        if card_count:
            population = sorted(pinned.read_rows('cards'), key=lambda row: row['concept_id'])
            result['cards'] = card_rng.sample(population, min(card_count, len(population)))
        result['actual'] = {'folders': len(result['folders']), 'cards': len(result['cards'])}
    return result


def audit_config(config, *, project_root, folder_count, card_count, seed):
    store = open_knowledge_store(config, 'kb audit', project_root=project_root)
    return audit(store, folder_count=folder_count, card_count=card_count, seed=seed)
