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

from .k0_scan import SourceTooLarge, read_source_file
from .model_json import strict_json
from .storage import StorageError
from .storage._anchored import make_root, publish_new_bytes, remove_entry
from .storage._fs import check_segment, real_inside
from .storage.store import check_request_id

ATTACHMENT_DIR = 'attachments'  # artifact-bound directory under storage.data_root
INDEX_NAME = 'index.json'  # artifact-bound index file of one trace's copies
MAX_INDEX_BYTES = 1 << 20  # safety-bound: an index lists a few images; a larger file is damaged, not read into memory
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


def _expiry(store, relative):
    """The expiry the directory's index names; None when there is no index. The read is bounded and never follows a
    link, like `read_attachment`: an oversized index raises `SourceTooLarge` (an OSError), which the sweep counts as failed."""
    try:
        data = _read_once(store, f'{relative}/{INDEX_NAME}', MAX_INDEX_BYTES)
    except FileNotFoundError:
        return None
    index = strict_json(data.decode('utf-8'))
    expires_at = datetime.fromisoformat(index['expires_at'])
    if expires_at.tzinfo is None:
        raise ValueError('index expiry has no time zone')
    return expires_at


class AttachmentNotFound(LookupError):
    """No live published copy answers the request: an unknown trace, an image its index does not list, or an expiry."""


def read_attachment(store, trace_id, image_id, now=None):
    """The bytes of one published copy, reached only through its trace's index (T-016 Q3: the response's published
    index, never a path the caller names). An id of the wrong form, an absent or expired set and an image the index does
    not list raise `AttachmentNotFound` before any file but the index is opened; an index or copy that cannot be read, or
    that no longer matches its recorded size or hash, is a `StorageError` (the copy is damaged, not absent)."""
    now = now or datetime.now(timezone.utc)
    try:
        relative = attachment_path(trace_id, image_id)
    except StorageError:
        raise AttachmentNotFound('not a published attachment') from None
    base = f'{ATTACHMENT_DIR}/{trace_id}'
    try:
        index = strict_json(_read_once(store, f'{base}/{INDEX_NAME}', MAX_INDEX_BYTES).decode('utf-8'))
    except (FileNotFoundError, NotADirectoryError):
        raise AttachmentNotFound('not a published attachment') from None
    except (OSError, ValueError) as error:
        raise StorageError(f'attachment index unreadable ({type(error).__name__})') from None
    try:
        expires_at = datetime.fromisoformat(index['expires_at'])
        listed = {entry['image_id']: entry for entry in index['images']}
        if expires_at.tzinfo is None:
            raise ValueError('index expiry has no time zone')
    except (KeyError, TypeError, ValueError):
        raise StorageError('attachment index malformed') from None
    if expires_at <= now or image_id not in listed:
        raise AttachmentNotFound('not a published attachment')
    entry = listed[image_id]
    size = entry.get('size_bytes') if isinstance(entry, dict) else None
    if type(size) is not int or size < 0:
        raise StorageError('attachment index malformed')
    try:
        data = _read_once(store, relative, size)
    except FileNotFoundError:
        raise AttachmentNotFound('not a published attachment') from None  # removed by an expiry between the two reads
    except SourceTooLarge:
        raise StorageError('attachment copy differs from its index') from None
    except OSError as error:  # a link, a special file or a read that failed: damaged, not absent
        raise StorageError(f'attachment copy unreadable ({type(error).__name__})') from None
    if len(data) != size:
        raise StorageError('attachment copy differs from its index')
    if hashlib.sha256(data).hexdigest() != image_id[7:]:
        raise StorageError('attachment copy differs from its image id')
    return data


def _read_once(store, relative, limit):
    """One open of `relative` under the data root without following a link on any component, then `fstat` and a read of
    at most `limit` bytes on that handle (`k0_scan.read_source_file`): the file that is read is the file that was checked."""
    return read_source_file(store.root, relative.split('/'), max_bytes=limit)


def expire_attachments(store, now=None):
    """Remove every trace directory whose index has expired; return (removed, failed).

    A directory without an index is removed only when empty (a publication may be in progress). A link, an unreadable
    index or a refused removal counts as failed and is left in place: nothing outside the data root is ever touched."""
    removed, failed, _ = sweep_attachments(store, now)
    return removed, failed


def sweep_attachments(store, now=None):
    """`expire_attachments` that also returns the earliest expiry among the sets it left in place (None when no live set
    remains), so a long-lived caller can rescan only once that time has passed instead of on every request."""
    now = now or datetime.now(timezone.utc)
    removed = failed = 0
    earliest = None
    try:
        base = real_inside(store.root, ATTACHMENT_DIR)
    except StorageError:
        return 0, 1, None
    if not base.is_dir():
        return 0, 0, None
    try:
        names = sorted(os.listdir(base))
    except OSError:  # an unreadable listing is housekeeping that failed, like every other expiry fault: counted, not fatal
        return removed, failed + 1, None
    for name in names:
        relative = f'{ATTACHMENT_DIR}/{name}'
        try:
            check_segment(name)
            directory = real_inside(store.root, relative)
            expires_at = _expiry(store, relative)
            if expires_at is None:
                if not os.listdir(directory):
                    remove_entry(store.root, relative, directory=True)
                    removed += 1
                continue
            if expires_at > now:
                earliest = expires_at if earliest is None else min(earliest, expires_at)
                continue
            members = sorted(os.listdir(directory), key=lambda member: member == INDEX_NAME)  # the index goes last
            for member in members:
                remove_entry(store.root, f'{relative}/{check_segment(member)}')
            remove_entry(store.root, relative, directory=True)
            removed += 1
        except (StorageError, OSError, ValueError, KeyError, TypeError):
            failed += 1
    return removed, failed, earliest
