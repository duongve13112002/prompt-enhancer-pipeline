"""Attachment copies of one delivered response, under `storage.data_root` (T-016, PM answer Q3).

Layout (artifact-bound): `attachments/<trace_id>/<image-hash-hex>` plus `attachments/<trace_id>/index.json`. The envelope
names an image by its `sha256:<hex>` id; the file name is the hex alone, because Windows names cannot hold a colon. The
index is written first: it lists every copy (hash and size) and the expiry, so a crash leaves a set the expiry pass can
age and remove, and a reader checks each file against the index. All writes and removals are anchored below the data
root (`storage/_anchored.py`); a link anywhere on the way is refused, never followed.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re

from .model_json import strict_json
from .storage import StorageError
from .storage._anchored import make_root, publish_new_bytes, remove_entry
from .storage._fs import check_segment, real_inside
from .storage.store import check_request_id

ATTACHMENT_DIR = 'attachments'  # artifact-bound directory under storage.data_root
INDEX_NAME = 'index.json'  # artifact-bound index file of one trace's copies
_IMAGE_ID = re.compile(r'sha256:([0-9a-f]{64})')  # protocol-bound image id form (storage/identity.py)


def attachment_path(trace_id, image_id):
    """Data-root-relative path of one copy; refuses an id that is not the content-hash form."""
    check_request_id(trace_id)
    match = _IMAGE_ID.fullmatch(image_id) if isinstance(image_id, str) else None
    if match is None:
        raise StorageError('invalid attachment image id')
    return f'{ATTACHMENT_DIR}/{trace_id}/{match.group(1)}'


def publish_attachments(store, trace_id, images, expires_at):
    """Write the index, then every copy; on any failure remove what this call wrote and raise.

    `images` maps image id to bytes; each copy must hash to its id, so a published file is exactly the original
    bytes. Returns the data-root-relative paths. A trace id that already has a directory is refused, never merged."""
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise StorageError('expires_at must be a timezone-aware datetime')
    entries = []
    for image_id, data in sorted(images.items()):
        path = attachment_path(trace_id, image_id)
        if hashlib.sha256(data).hexdigest() != image_id[7:]:
            raise StorageError('attachment bytes differ from their image id')
        entries.append((path, {'image_id': image_id, 'file': image_id[7:], 'size_bytes': len(data)}, data))
    index = json.dumps({'trace_id': trace_id, 'expires_at': expires_at.isoformat(),
                        'images': [entry for _, entry, _ in entries]}, ensure_ascii=True).encode('ascii')
    if not os.path.lexists(store.root):
        make_root(store.catalog.root, store.root)
    base = f'{ATTACHMENT_DIR}/{trace_id}'
    written = []
    try:
        publish_new_bytes(store.root, f'{base}/{INDEX_NAME}', index)
        written.append(f'{base}/{INDEX_NAME}')
        for path, _, data in entries:
            publish_new_bytes(store.root, path, data)
            written.append(path)
    except BaseException:
        _discard(store, base, written)
        raise
    return [path for path, _, _ in entries]


def _discard(store, base, written):
    """Best effort rollback of one failed publication; a leftover is aged out by its index at expiry."""
    for relative in reversed(written):
        try:
            remove_entry(store.root, relative)
        except (OSError, StorageError):
            pass
    try:
        remove_entry(store.root, base, directory=True)
    except (OSError, StorageError):
        pass


def _expired(store, relative, now):
    """True when the directory's index names an expiry that has passed; None when there is no readable index."""
    try:
        data = real_inside(store.root, f'{relative}/{INDEX_NAME}').read_bytes()
    except FileNotFoundError:
        return None
    index = strict_json(data.decode('utf-8'))
    expires_at = datetime.fromisoformat(index['expires_at'])
    if expires_at.tzinfo is None:
        raise ValueError('index expiry has no time zone')
    return expires_at <= now


def expire_attachments(store, now=None):
    """Remove every trace directory whose index has expired; return (removed, failed).

    A directory without an index is removed only when empty (a publication may be in progress). A link, an unreadable
    index or a refused removal counts as failed and is left in place: nothing outside the data root is ever touched."""
    now = now or datetime.now(timezone.utc)
    removed = failed = 0
    try:
        base = real_inside(store.root, ATTACHMENT_DIR)
    except StorageError:
        return 0, 1
    if not base.is_dir():
        return 0, 0
    try:
        names = sorted(os.listdir(base))
    except OSError:  # an unreadable listing is housekeeping that failed, like every other expiry fault: counted, not fatal
        return removed, failed + 1
    for name in names:
        relative = f'{ATTACHMENT_DIR}/{name}'
        try:
            check_segment(name)
            directory = real_inside(store.root, relative)
            expired = _expired(store, relative, now)
            if expired is None:
                if not os.listdir(directory):
                    remove_entry(store.root, relative, directory=True)
                    removed += 1
                continue
            if not expired:
                continue
            members = sorted(os.listdir(directory), key=lambda member: member == INDEX_NAME)  # the index goes last
            for member in members:
                remove_entry(store.root, f'{relative}/{check_segment(member)}')
            remove_entry(store.root, relative, directory=True)
            removed += 1
        except (StorageError, OSError, ValueError, KeyError, TypeError):
            failed += 1
    return removed, failed
