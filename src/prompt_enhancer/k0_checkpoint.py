"""K0 scan artifacts and snapshot reconciliation through the T-006 store."""

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import RLock

from .k0 import rows_from_scan
from .k0_scan import decode_info, scan_source, validate_scan
from .storage import StorageError, artifact_key

# artifact-bound: declared revision of the code that shapes a K0 scan artifact (KBS-S3-1); it replaces a hash of the
# source of four modules. Bump it with any edit to a function its guard pins (tests/test_stage_revisions.py).
K0_SCAN_REVISION = 'k0-scan-v6'
_REVISION = {'model': 'none', 'prompt': 'none', 'schema': K0_SCAN_REVISION}  # artifact-bound
_VERSION = 2  # artifact-bound: JSON layout version
_TABLES = ('folders', 'concepts', 'folder_concepts', 'images', 'folder_images')  # contract-bound


def _json_value(value):
    if isinstance(value, datetime):
        return {'$datetime': value.isoformat()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _restore(value):
    if isinstance(value, list):
        return [_restore(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {'$datetime'}:
            return datetime.fromisoformat(value['$datetime'])
        return {key: _restore(item) for key, item in value.items()}
    return value


def _bytes(value):
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _key(kind, value):
    return artifact_key(kind, hashlib.sha256(_bytes(value)).hexdigest(), _REVISION)


class _FileCache:
    """Reuse only checksum-verified, versioned records for matching path and stat."""

    def __init__(self, checkpoints, content, root):
        self.checkpoints = checkpoints
        self.content = content
        self.root = os.path.abspath(root)
        self._lock = RLock()

    def _identity(self, path, info, encoding):
        relative = Path(path).relative_to(self.root).as_posix()
        return {'version': _VERSION, 'root': self.root, 'path': relative,
                'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'encoding': encoding}

    def get(self, path, info, encoding):
        with self._lock:
            return self._get(path, info, encoding)

    def _get(self, path, info, encoding):
        identity = self._identity(path, info, encoding)
        key = _key('k0_file', identity)
        if self.checkpoints.done(key) is None:
            return None
        try:
            document = _restore(json.loads(self.checkpoints.load(key)))
            if document['identity'] != identity or document['version'] != _VERSION:
                return None
            content_key = document['content_key']
            if self.content.done(content_key) is None:
                return None
            content = _restore(json.loads(self.content.load(content_key)))
            if (content['version'] != _VERSION or content['encoding'] != encoding or
                    not _valid_id(content['hash']) or
                    content_key != _key('k0_content', {'hash': content['hash'], 'encoding': encoding})):
                return None
            if encoding is None:
                if set(content) != {'version', 'encoding', 'hash', 'size_bytes'} or content['size_bytes'] != info.st_size:
                    return None
                return {'image_id': content['hash'], 'size_bytes': content['size_bytes'],
                        'modified_at': datetime.fromtimestamp(info.st_mtime, timezone.utc)}
            if set(content) != {'version', 'encoding', 'hash', 'raw'} or not isinstance(content['raw'], str):
                return None
            raw = base64.b64decode(content['raw'], validate=True)
            if len(raw) != info.st_size or 'sha256:' + hashlib.sha256(raw).hexdigest() != content['hash']:
                return None
            return {'hash': content['hash'], 'text': decode_info(raw, encoding)}
        except (StorageError, ValueError, TypeError, KeyError, UnicodeError):
            return None  # corrupt or obsolete cache: safely re-read source and rebuild

    def put(self, path, info, encoding, record, *, raw):
        with self._lock:
            self._put(path, info, encoding, record, raw=raw)

    def _put(self, path, info, encoding, record, *, raw):
        identity = self._identity(path, info, encoding)
        content_hash = record['hash'] if encoding is not None else record['image_id']
        content_key = _key('k0_content', {'hash': content_hash, 'encoding': encoding})
        content = {'version': _VERSION, 'encoding': encoding, 'hash': content_hash}
        content['raw' if encoding is not None else 'size_bytes'] = (
            base64.b64encode(raw).decode('ascii') if encoding is not None else record['size_bytes'])
        content_bytes = _bytes(content)
        if not _matching(self.content, content_key, content_bytes):
            self.content.commit(content_key, 'k0_content', content_bytes, revisions=_REVISION)
        payload = {'version': _VERSION, 'identity': identity, 'content_key': content_key}
        key = _key('k0_file', identity)
        if not _matching(self.checkpoints, key, _bytes(payload)):
            self.checkpoints.commit(key, 'k0_file', _bytes(payload), revisions=_REVISION)


def _matching(checkpoints, key, expected):
    if checkpoints.done(key) is None:
        state = checkpoints.state(key)
        if state is None or state['state'] != 'done' or not state['output_key']:
            return False
    try:
        return checkpoints.load(key) == expected
    except StorageError:
        return False


def _valid_id(value):
    return (isinstance(value, str) and len(value) == 71 and value.startswith('sha256:') and
            all(char in '0123456789abcdef' for char in value[7:]))


def _previous(store):
    pinned = store.pin()
    if pinned is None:
        return {name: [] for name in _TABLES}
    with pinned:  # a failed release never replaces this result or exception (T-006 race round 3, P3-B)
        return {name: pinned.read_rows(name) for name in _TABLES}


def _scan_previous(rows):
    image_rows = {row['image_id']: row for row in rows['images']}
    result = {row['folder_id']: {'info': None, 'images': {}} for row in rows['folders']}
    for membership in rows['folder_images']:
        image = image_rows[membership['image_id']]
        result[membership['folder_id']]['images'][membership['relative_path']] = {
            'image_id': membership['image_id'], 'size_bytes': image['size_bytes'],
            'modified_at': membership['modified_at']}
    return result


def _merge_previous(rows, previous, report):
    protected = set(report['protected_info']) | set(report['protected_folders'])
    old_folders = {row['folder_id']: row for row in previous['folders']}
    rows['folders'] = [deepcopy(old_folders[row['folder_id']])
                       if row['folder_id'] in protected and row['folder_id'] in old_folders else row
                       for row in rows['folders']]
    rows['folder_concepts'] = [row for row in rows['folder_concepts'] if row['folder_id'] not in protected]
    rows['folder_concepts'].extend(deepcopy(row) for row in previous['folder_concepts']
                                   if row['folder_id'] in protected and row['folder_id'] in old_folders)
    wanted = {row['concept_id'] for row in rows['folder_concepts']}
    concepts = {row['concept_id']: row for row in rows['concepts'] if row['concept_id'] in wanted}
    concepts.update({row['concept_id']: deepcopy(row) for row in previous['concepts'] if row['concept_id'] in wanted})
    rows['concepts'] = [concepts[key] for key in sorted(concepts)]
    old_images = {row['image_id']: row for row in previous['images']}
    rows['images'] = [deepcopy(old_images.get(row['image_id'], row)) for row in rows['images']]
    rows['folder_concepts'].sort(key=lambda row: (row['folder_id'], row['position'], row['concept_id']))
    return rows


def scan_to_checkpoint(store, root, *, info_names, encoding, extensions, separator):
    """Scan against CURRENT, commit verified per-file and final artifacts, never publish."""
    validate_scan(info_names, encoding, extensions, separator, 1)
    root = Path(root).resolve()
    store_root = Path(store.root).resolve()
    if root == store_root or root in store_root.parents or store_root in root.parents:
        raise ValueError('storage.kb_store overlaps knowledge.root')
    with store.writer() as writer:
        previous = _previous(store)
        cache = _FileCache(writer.checkpoints('k0_file'), writer.checkpoints('k0_content'), root)
        scanned = scan_source(root, info_names=info_names, encoding=encoding, extensions=extensions,
                              separator=separator, previous=_scan_previous(previous), cache=cache)
        rows = _merge_previous(rows_from_scan(scanned['folders'], separator), previous, scanned['report'])
        result = {'version': _VERSION, 'rows': rows, 'report': scanned['report'],
                  'settings': {'info_names': list(info_names), 'encoding': encoding,
                               'extensions': list(extensions), 'separator': separator}}
        checkpoints = writer.checkpoints('k0_scan')
        key = _key('k0_scan', result)
        if not _matching(checkpoints, key, _bytes(result)):
            checkpoints.commit(key, 'k0_scan', _bytes(result), revisions=_REVISION)
        return {'key': key, **result}


def load_scan_checkpoint(store, key):
    """Load an intact versioned scan result without moving CURRENT."""
    with store.writer() as writer:
        raw = writer.checkpoints('k0_scan').load(key)
    try:
        result = _restore(json.loads(raw))
        if (not isinstance(result, dict) or set(result) != {'version', 'rows', 'report', 'settings'} or
                result['version'] != _VERSION or not isinstance(result['report'], dict) or
                not isinstance(result['rows'], dict) or set(result['rows']) != set(_TABLES) or
                any(not isinstance(result['rows'][name], list) for name in _TABLES) or
                _key('k0_scan', result) != key):
            raise ValueError('invalid scan result')
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise StorageError('invalid K0 scan checkpoint') from error
    return {'key': key, **result}
