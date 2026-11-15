"""Structural K0 parsing and change planning; source text is always data."""

from collections import Counter
import os
import re

from .storage import concept_id, normalise_name


def resolve_workers(value):
    """Resolve the configured CPU limit without inventing a fallback value."""
    if value == 'auto':
        affinity = getattr(os, 'sched_getaffinity', None)
        try:
            value = len(affinity(0)) if affinity is not None else os.cpu_count()
        except OSError:
            value = os.cpu_count()
    if type(value) is not int or value <= 0:
        raise ValueError('runtime.cpu_workers must resolve to a positive integer')
    return value


def guard_removals(plan, previous_count, threshold, *, force=False):
    """Return the removed fraction, refusing a plan beyond the configured limit."""
    if type(previous_count) is not int or previous_count < 0:
        raise ValueError('previous folder count must be nonnegative')
    if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
        raise ValueError('guard fraction must be between zero and one')
    fraction = len(plan['removed']) / previous_count if previous_count else 0.0
    if fraction > threshold and not force:
        raise ValueError(f'mass deletion guard: removed fraction {fraction} exceeds {threshold}')
    return fraction


def parse_info(text, separator):
    """Return names and positional descriptions, or a pending/duplicate status."""
    if not isinstance(separator, str) or not separator:
        raise ValueError('concept separator must be nonempty')
    lines = re.split(r'\r\n|\r|\n', text)
    def trim(value):
        # Unicode line separators are source data, even at token boundaries.
        data = '\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029'
        start, end = 0, len(value)
        while start < end and value[start].isspace() and value[start] not in data:
            start += 1
        while end > start and value[end - 1].isspace() and value[end - 1] not in data:
            end -= 1
        return value[start:end]
    names = [trim(part) for part in lines[0].split(separator)]
    if any(not name for name in names):
        return {'names': [], 'descriptions': [], 'line2': '', 'status': 'pending_naming'}
    normalized = [normalise_name(name) for name in names]
    if len(set(normalized)) != len(normalized):
        return {'names': names, 'descriptions': [], 'line2': '', 'status': 'duplicate_names'}
    line2 = lines[1] if len(lines) > 1 else ''
    descriptions = [trim(part) for part in line2.split(separator)]
    mismatch = len(descriptions) != len(names)
    return {'names': names, 'concept_ids': [concept_id(name) for name in names],
            'descriptions': [] if mismatch else descriptions, 'line2': line2,
            'status': 'description_mismatch' if mismatch else 'parsed'}


def diff_folders(previous, current):
    """Match only unique complete signatures; ambiguous candidates stay removed/added."""
    removed = set(previous) - set(current)
    added = set(current) - set(previous)
    before = Counter(previous[name] for name in removed)
    after = Counter(current[name] for name in added)
    unique_added = {current[name]: name for name in added if after[current[name]] == 1}
    moved = []
    for old in sorted(removed):
        signature = previous[old]
        if before[signature] == after[signature] == 1:
            new = unique_added[signature]
            moved.append((old, new))
    removed.difference_update(old for old, _ in moved)
    added.difference_update(new for _, new in moved)
    ambiguous = sorted(name for name in removed if previous[name] in after)
    return {'added': sorted(added), 'removed': sorted(removed), 'renamed': moved,
            'changed': sorted(name for name in set(previous) & set(current) if previous[name] != current[name]),
            'ambiguous': ambiguous}


def diff_rows(previous, current):
    """Compare K0 row sets by their contract keys, preserving image content identity."""
    def keyed(rows, *columns):
        result = {}
        for row in rows:
            key = tuple(row[column] for column in columns)
            if key in result:
                raise ValueError(f'duplicate row key {key!r}')
            result[key] = row
        return result

    def changes(old, new):
        old_keys, new_keys = set(old), set(new)
        return {'added': sorted(new_keys - old_keys), 'removed': sorted(old_keys - new_keys),
                'changed': sorted(key for key in old_keys & new_keys if old[key] != new[key])}

    old_folders = keyed(previous['folders'], 'folder_id')
    new_folders = keyed(current['folders'], 'folder_id')
    old_members = keyed(previous['folder_images'], 'folder_id', 'relative_path')
    new_members = keyed(current['folder_images'], 'folder_id', 'relative_path')

    def signatures(folders, members):
        result = {key[0]: [row['info_file_hash'], []] for key, row in folders.items()}
        for (folder_id, relative_path), row in members.items():
            if folder_id not in result:
                raise ValueError(f'image references missing folder {folder_id!r}')
            result[folder_id][1].append((relative_path, row['image_id']))
        return {key: (value[0], tuple(sorted(value[1]))) for key, value in result.items()}

    folder_plan = diff_folders(signatures(old_folders, old_members), signatures(new_folders, new_members))
    old_images = keyed(previous['images'], 'image_id')
    new_images = keyed(current['images'], 'image_id')
    old_concepts = keyed(previous['concepts'], 'concept_id')
    new_concepts = keyed(current['concepts'], 'concept_id')
    member_plan = changes(old_members, new_members)
    old_by_id, new_by_id = {}, {}
    for key in member_plan['removed']:
        old_by_id.setdefault(old_members[key]['image_id'], []).append(key)
    for key in member_plan['added']:
        new_by_id.setdefault(new_members[key]['image_id'], []).append(key)
    moves = []
    for image_id in sorted(set(old_by_id) & set(new_by_id)):
        if len(old_by_id[image_id]) == len(new_by_id[image_id]) == 1:
            moves.append((old_by_id[image_id][0], new_by_id[image_id][0], image_id))
    member_plan['moved'] = moves
    moved_old = {move[0] for move in moves}
    moved_new = {move[1] for move in moves}
    member_plan['removed'] = [key for key in member_plan['removed'] if key not in moved_old]
    member_plan['added'] = [key for key in member_plan['added'] if key not in moved_new]
    return {'folders': folder_plan, 'images': changes(old_images, new_images),
            'folder_images': member_plan, 'concepts': changes(old_concepts, new_concepts)}


def rows_from_scan(folders, separator):
    """Build K0 table rows from verified scan records, in stable folder/path order.

    Each folder supplies ``info`` (None or a hash/text mapping) and ``images``
    (relative path to image_id, size_bytes, modified_at). The scanner owns error
    reconciliation before calling this function.
    """
    result = {'folders': [], 'concepts': [], 'folder_concepts': [], 'images': [], 'folder_images': []}
    concepts, images = {}, {}
    for folder_id in sorted(folders):
        source = folders[folder_id]
        info = source['info']
        parsed = parse_info(info['text'], separator) if info is not None else None
        status = parsed['status'] if parsed is not None else 'pending_naming'
        usable = status in ('parsed', 'description_mismatch')
        folder = {'folder_id': folder_id, 'info_file_hash': info['hash'] if info is not None else None,
                  'declared_names': parsed['names'] if usable else [],
                  'declared_descriptions': parsed['line2'] if parsed is not None else None,
                  'name_source': 'info_file' if usable else None,
                  'error': '' if usable else status}
        result['folders'].append(folder)
        if usable:
            for position, (name, identity) in enumerate(zip(parsed['names'], parsed['concept_ids'])):
                concepts.setdefault(identity, {'concept_id': identity, 'normalised_name': normalise_name(name),
                                               'display_name': name, 'kind': None, 'kind_origin': None})
                description = parsed['descriptions'][position] if status == 'parsed' else None
                result['folder_concepts'].append({'folder_id': folder_id, 'concept_id': identity,
                                                  'position': position, 'name_as_written': name,
                                                  'description': description,
                                                  'description_origin': 'info_file' if description is not None else None,
                                                  'description_revision': None})
        for path, image in sorted(source['images'].items()):
            identity = image['image_id']
            row = {'image_id': identity, 'size_bytes': image['size_bytes'], 'format': None, 'width': None,
                   'height': None, 'decode_status': None, 'near_duplicate_group': None}
            if identity in images and images[identity]['size_bytes'] != row['size_bytes']:
                raise ValueError(f'inconsistent size for image {identity!r}')
            images.setdefault(identity, row)
            result['folder_images'].append({'folder_id': folder_id, 'relative_path': path, 'image_id': identity,
                                            'modified_at': image['modified_at']})
    result['concepts'] = [concepts[key] for key in sorted(concepts)]
    result['images'] = [images[key] for key in sorted(images)]
    return result
