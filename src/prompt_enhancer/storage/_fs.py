"""Durable file writes, checksums, containment and OS file locks for the storage layer."""

import hashlib
import os
from pathlib import Path
import re
import time
import unicodedata
import uuid

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from ..config import ConfigurationError, _resolve_within

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class StorageError(RuntimeError):
    """A storage operation was refused or found inconsistent data."""


# artifact-bound: these options change bytes, not meaning; each file records its own format in the manifest.
PARQUET_VERSION = '2.6'
PARQUET_COMPRESSION = 'zstd'
SUFFIX = {'parquet': '.parquet', 'arrow': '.arrow'}
HASH_CHUNK = 1 << 20  # safety-bound read size for streaming SHA-256


def format_of(path):
    for name, suffix in SUFFIX.items():
        if str(path).endswith(suffix):
            return name
    raise StorageError(f'{path}: unknown table file format')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_dir(path):
    """Persist a rename on POSIX.

    Windows cannot open a directory with os.open (measured: PermissionError, T-006 probe 10), so there the
    durability of a completed os.replace/os.rename relies on NTFS journaling its metadata. This is an
    assumption, not a measured guarantee (T-006 Completion report).
    """
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temporary(path):
    return path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')


def _commit(tmp, path):
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def atomic_write_bytes(path, data, check=None):
    """Write to a temporary sibling, fsync, then replace: a reader sees the old or the new file, never half.

    `check(size, sha256)` may refuse the content before anything is written."""
    path = Path(path)
    size, digest = len(data), hashlib.sha256(data).hexdigest()
    if check is not None:
        check(size, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temporary(path)
    with open(tmp, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _commit(tmp, path)
    return size, digest


def write_table_file(path, table, fmt, check=None):
    """Write one table durably; return (size_bytes, sha256) of the committed file.

    `check(size, sha256)` may refuse the written bytes before they replace the target."""
    path = Path(path)
    if fmt not in SUFFIX:
        raise StorageError(f'{path}: unknown table format {fmt!r}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temporary(path)
    try:
        # PyArrow leaves a Python file object open (T-006 probe 1-2), so flush and fsync here.
        with open(tmp, 'wb') as handle:
            if fmt == 'parquet':
                pq.write_table(table, handle, version=PARQUET_VERSION, compression=PARQUET_COMPRESSION)
            else:
                with ipc.new_file(handle, table.schema) as writer:
                    writer.write_table(table)
            handle.flush()
            os.fsync(handle.fileno())
        size, digest = tmp.stat().st_size, sha256_file(tmp)
        if check is not None:
            check(size, digest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _commit(tmp, path)
    return size, digest


def read_table_file(path):
    """Read a whole table into memory and close the file, so no handle blocks a later delete on Windows."""
    fmt = format_of(path)
    try:
        if fmt == 'parquet':
            return pq.read_table(path, memory_map=False)
        with pa.OSFile(str(path), 'rb') as source:
            return ipc.open_file(source).read_all()
    except (OSError, pa.ArrowException) as error:
        raise StorageError(f'{path}: cannot read table: {error}') from error


# protocol-bound: names Windows reserves for devices in every folder, with or without an extension
_DEVICE = re.compile(r'(con|prn|aux|nul|conin\$|conout\$|(com|lpt)[0-9¹²³])(\..*)?', re.IGNORECASE)
_SHORT_NAME = re.compile(r'~[0-9]')  # the 8.3 form NAME~1.EXT: another spelling of a long name
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*\\/')  # protocol-bound: characters a Windows file name cannot hold


def check_segment(segment):
    """One path segment in a spelling no supported file system folds onto another (fix round 3, R2-A).

    Refused, never rewritten: '', '.', '..'; control characters and Windows' forbidden characters (':' also opens
    an NTFS stream, '\\' is a separator there); a final dot or space (Windows drops it, so 'a.' names 'a');
    device names ('nul', 'CON.txt'); the 8.3 short form '~<digit>'; and any text that is not NFC (macOS folds
    NFD onto NFC). Case is not decided here: callers apply the identity rule of their admission point."""
    if not isinstance(segment, str) or segment in ('', '.', '..'):
        raise StorageError(f'invalid path segment {segment!r}')
    if any(ord(char) < 32 or char in _WINDOWS_FORBIDDEN for char in segment) or segment[-1] in ' .':
        raise StorageError(f'invalid path segment {segment!r}')
    if _DEVICE.fullmatch(segment) or _SHORT_NAME.search(segment):
        raise StorageError(f'path segment {segment!r} is a device name or a short-name alias')
    if unicodedata.normalize('NFC', segment) != segment:
        raise StorageError(f'path segment {segment!r} is not in Unicode NFC')
    return segment


def inside(root, relative):
    """Resolve a relative POSIX path under root after links; refuse anything that leaves root."""
    if not isinstance(relative, str) or not relative or relative.startswith('/'):
        raise StorageError(f'{root}: invalid relative path {relative!r}')
    # Every segment passes the one segment rule; ':' would also let 'a/c:' join to a drive outside root.
    try:
        parts = [check_segment(part) for part in relative.split('/')]
    except StorageError as error:
        raise StorageError(f'{root}: invalid relative path {relative!r}: {error}') from error
    root = Path(root)
    try:
        return _resolve_within(root.joinpath(*parts), root, set(), root, relative)
    except ConfigurationError as error:
        raise StorageError(str(error)) from error


def real_inside(root, relative):
    """`inside`, and the resolved final path is exactly the spelled one (root must already be resolved).

    The one path rule of both stores (fix round 4): every operation that opens, writes, renames, replaces or
    deletes a path takes it from here at the moment it acts, so no component below root is a link or junction.
    A link is refused, never followed, also when it stays inside root: there it would let one name write another
    name's file, which no check on the spelled name can see (round 4, F1).
    A traversal checks each directory with this before it lists it, so it never enters a link: one that leaves
    the store is refused before anything outside is listed, and one back into the store cannot make a file
    appear twice or under a name it does not have (fix round 2, R1). The comparison is exact, with no case
    folding: realpath returns each existing component as the file system stores it, so a spelling that only
    aliases a file (other case, a dropped final dot) is refused too (fix round 3). Root is resolved, so its own
    8.3 or case spelling cannot cause a false refusal; segments cannot hold '~<digit>' (check_segment)."""
    path = inside(root, relative)
    if str(path) != str(Path(root).joinpath(*relative.split('/'))):
        raise StorageError(f'{root}: {relative}: is a link or another spelling of a stored name')
    return path


class FileLock:
    """Exclusive OS lock on one byte of a lock file; polled until a deadline.

    Windows uses msvcrt.locking (two handles conflict, even in one process: T-006 probe 9). POSIX uses
    fcntl.flock, which also conflicts between two open file descriptions of one process; that path is
    exercised only by the Linux CI run.
    """

    def __init__(self, path, timeout_s, poll_s):
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._handle = None

    def try_acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(self.path, 'a+b')
        except PermissionError:
            # A sharing violation or a delete-pending file (Windows): not acquired. GC reads that as a live
            # lease, acquire() polls until its timeout; neither crashes nor declares the holder dead (round 3).
            return False
        try:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def acquire(self):
        deadline = time.monotonic() + self.timeout_s
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise StorageError(f'{self.path}: lock not acquired within storage.lock.timeout_s={self.timeout_s}')
            time.sleep(self.poll_s)
        return self

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @property
    def held(self):
        return self._handle is not None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
