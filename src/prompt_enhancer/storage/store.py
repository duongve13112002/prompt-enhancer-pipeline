"""Knowledge store and data store (docs/execution.md:62-74, contracts/README.md commit protocol; R-014, R-017).

Layout under `storage.kb_store`:

    LOCK, WRITER                   OS lock files (store lock: short critical sections; writer lock: one writer)
    CURRENT                        "<snapshot_id> <manifest sha256>\n", replaced atomically under the store lock
    snapshots/<id>/                immutable tables, files and MANIFEST.parquet (built in .building-<id>/)
    registry/REGISTRY              names the authoritative registry file; the others are leftovers of a crash
    registry/snapshots-<n>.<fmt>   the snapshots table
    registry/sync_state/*          sync_state segments, compacted when a writer session ends
    artifacts/<kind>/<xx>/<key>    content-addressed artifacts
    leases/<id>/<uuid>.lease       one per pinned reader, held through an OS lock (written by the serving runtime)
    holds/<id>/<request>.parquet   trace holds with an expiry (written by the serving runtime)

Rules (fix rounds 1 and 2): each check sits where the store trusts the fact, not at one call site.
- one path rule (fix round 4): every path the store opens, writes, renames, replaces or deletes comes from
  `KnowledgeStore.path` at the moment of the act, under the lock that makes the act. It refuses anything outside
  the store root and any link or junction below it, also one that stays inside the store: a link would let one
  name reach another name's file, behind every check made on the name. Paths are kept as store-relative strings
  and resolved when used, never stored resolved. Every traversal (listing, GC, recovery, tree removal) goes
  through `KnowledgeStore.walk`/`_children`, which apply the same rule to each directory before listing it;
- every writer-side helper calls `Writer.require()` first; a Writer is one session (single-use, its own run id),
  so a helper acts only inside the session that made it;
- a decision is acted on under the lock that made it (GC marks `deleting` before it removes anything), and the
  facts behind an action are verified again there: publish and rollback run `verify_snapshot` (every file and
  artifact by size and a fresh SHA-256, tags, references, the embedding space) under the store lock, and
  resume re-hashes a done artifact before skipping it;
- an artifact a live snapshot references is immutable: `commit()` refuses content whose SHA-256 differs.
- one identity rule (identity.py, fix round 3): every name the store admits has a single spelling; an alias
  (other case, final dot or space, NFD, stream, device or 8.3 name, backslash) is refused at admission, never
  rewritten, so a string comparison of admitted names is a comparison of files.
- one serialization rule per Writer (fix round 3): every writer-side operation runs under the Writer's mutex
  from its check to its act (`_writer_operation`).

Lock order, outermost first: Writer mutex (threads of one Writer) -> WRITER file lock (one writer process; taken
once in `__enter__`, under the mutex) -> store LOCK (short sections) -> a lease file lock (a leaf: taken under the
store lock by `pin` and GC, which take nothing after it). Readers (`pin`, `hold`, `release_hold`, `current`,
`snapshots`) take only the store lock. No code holding the store lock calls a writer operation, so no path takes
the mutex after the store lock (`tests/test_storage_review_r3.py` asserts this on every operation).

Every reader takes CURRENT only under the store lock, so our own handles never block its replacement on
Windows (T-006 probe 3); a foreign handle makes os.replace fail as a whole and the old pointer stays.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import functools
import hashlib
import itertools
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import SENTINELS, ConfigurationError, _command_reads, _get, _lexical_path, _resolve_within
from ._fs import (SUFFIX, FileLock, StorageError, atomic_write_bytes, format_of, fsync_dir, read_table_file,
                  real_inside, sha256_file, write_table_file)
from .identity import artifact_path, check_artifact_key, check_artifact_path, check_relative_name, identity_key
from .tables import (MANIFEST_KINDS, MANIFEST_SCHEMA, SNAPSHOT_TABLES, SPECS, TS, Catalog, check_embedding_space,
                     check_references, check_tags)

CURRENT, LOCK, WRITER, MANIFEST = 'CURRENT', 'LOCK', 'WRITER', 'MANIFEST.parquet'  # artifact-bound names
REGISTRY_POINTER = 'registry/REGISTRY'
BUILDING = '.building-'
GC_STAGE = 'gc'  # sync_state stage of "first seen unreferenced" marks (T-006 Q3)
_SNAPSHOT_ID = re.compile(r's[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}')
_REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_REGISTRY_NAME = re.compile(r'snapshots-[0-9]{20}-[0-9]{8}-[0-9a-f]{8}\.(parquet|arrow)')
_RETENTION = re.compile(r'([0-9]+)([smhd])')
_UNIT_S = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}  # protocol-bound: units of the retention grammar
RAG_COMMANDS = ('enhance once', 'enhance serve', 'eval run')
_FORBIDDEN = ('.secrets', '.git', '.venv', 'models')  # same protected roots as config._configured_path
HOLDABLE = ('published', 'retired')  # a trace may hold only a snapshot a reader could have pinned
LIVE = ('validated', 'published', 'retired')  # states whose snapshot may still be published, pinned or rolled back
HOLD_SCHEMA = pa.schema([pa.field('request_id', pa.large_string(), False),
                         pa.field('snapshot_id', pa.large_string(), False),
                         pa.field('expires_at', TS, False)], metadata={'pe.table': 'hold', 'pe.physical_version': '1'})
_UNDECLARED = object()
_SEQUENCE = itertools.count()
_VERIFIED = {}  # (path, size, mtime_ns) -> sha256: a file is hashed once per process
_VERIFIED_LOCK = threading.Lock()


def _now(clock):
    return datetime.fromtimestamp(clock(), timezone.utc)


def _segment_name(suffix, prefix=''):
    return f'{prefix}{time.time_ns():020d}-{next(_SEQUENCE):08d}-{uuid.uuid4().hex[:8]}{suffix}'


def _unlink(path):
    """The one delete of a single file; tests interrupt it to simulate a crash."""
    path.unlink(missing_ok=True)


def new_snapshot_id(clock=time.time):
    stamp = _now(clock).strftime('%Y%m%dT%H%M%S%f')
    return f's{stamp}Z-{uuid.uuid4().hex[:8]}'


def check_snapshot_id(value):
    if not isinstance(value, str) or not _SNAPSHOT_ID.fullmatch(value):
        raise StorageError(f'invalid snapshot id {value!r}')
    return value


def check_request_id(value):
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise StorageError(f'invalid request id {value!r}')
    return value


def parse_retention(value):
    match = _RETENTION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise StorageError(f'sync.gc.retention: expected <n>s|m|h|d, got {value!r}')
    return int(match.group(1)) * _UNIT_S[match.group(2)]


def _read_hold(path):
    """A hold row, parsed from bytes read through a Python handle that is closed on return, so that a delete
    right after it is not blocked by a reader handle still open (Windows; fix round 3, full-suite WinError 32)."""
    try:
        table = pq.read_table(pa.BufferReader(path.read_bytes()))
    except (OSError, pa.ArrowException) as error:
        raise StorageError(f'{path}: cannot read hold: {error}') from error
    check_tags(table.schema, 'hold')
    return table.to_pylist()[0]


def _namespace():
    """The names of one snapshot directory, keyed by identity_key; the manifest's own name is taken."""
    return {identity_key(MANIFEST): (MANIFEST, False)}


def _admit(names, relative):
    """Admit a caller-chosen snapshot path into its directory's namespace (the identity rule, fix round 3).

    Every prefix is a directory and the last part a file; a prefix may repeat only with the same spelling as a
    directory, and a file never repeats. So 'INDEX/x' after 'index/y', or 'Folders.parquet' after
    'folders.parquet', is refused instead of landing on the same file of a case-insensitive file system."""
    parts = check_relative_name(relative).split('/')
    for depth in range(1, len(parts) + 1):
        spelled, is_dir = '/'.join(parts[:depth]), depth < len(parts)
        known = names.get(identity_key(spelled))
        if known is not None and (known != (spelled, True) or not is_dir):
            raise StorageError(f'{relative!r} names the same file or folder as {known[0]!r}')
        names[identity_key(spelled)] = (spelled, is_dir)
    return relative


def _verify(path, size, sha256, *, fresh=False):
    """Size, then SHA-256. A reader may reuse this process's hash of an unchanged (path, size, mtime) file; an
    acting point (publish, rollback, finish, resume) passes fresh=True and hashes the bytes again."""
    try:
        stat = path.stat()
    except OSError as error:
        raise StorageError(f'{path}: listed in the manifest but missing: {error}') from error
    if stat.st_size != size:
        raise StorageError(f'{path}: size {stat.st_size} differs from the manifest ({size})')
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _VERIFIED_LOCK:
        known = None if fresh else _VERIFIED.get(key)
    if known is None:
        known = sha256_file(path)
        with _VERIFIED_LOCK:
            _VERIFIED[key] = known
    if known != sha256:
        raise StorageError(f'{path}: checksum differs from the manifest')


# ----------------------------------------------------------------------------- factories


def _resolved_path(config, key, root):
    value = _get(config, key, 'config')
    if isinstance(value, str) and value in SENTINELS:
        raise StorageError(f'{key}: unresolved sentinel {value}')
    try:
        candidate = _lexical_path(value, root, 'config', key)
        resolved = _resolve_within(candidate, root, {os.path.normcase(name) for name in _FORBIDDEN}, 'config', key)
    except ConfigurationError as error:
        raise StorageError(str(error)) from error
    if resolved == root:
        raise StorageError(f'{key}: must not be the project root')
    return resolved


def _positive_finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _settings(config, command, project_root):
    """Read only keys in the command's read set; each is refused when it holds a sentinel or a bad value."""
    from ..config import COMMAND_READS
    if command not in COMMAND_READS:
        raise StorageError(f'unknown command {command!r}')
    reads = _command_reads(command, config)

    def value(key, check):
        if not any(key == read or key.startswith(read + '.') for read in reads):
            return _UNDECLARED
        try:
            result = _get(config, key, 'config')
        except ConfigurationError as error:
            raise StorageError(str(error)) from error
        if isinstance(result, str) and result in SENTINELS:
            raise StorageError(f'{key}: unresolved sentinel {result}')
        if not check(result):
            raise StorageError(f'{key}: invalid value {result!r}')
        return result

    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    settings = {
        'timeout_s': value('storage.lock.timeout_s', _positive_finite),  # finite: a bounded wait (F9)
        'poll_s': value('storage.lock.poll_s', _positive_finite),
        'table_format': value('storage.table_format', lambda x: x in SUFFIX),
        'keep': value('sync.snapshots.keep', lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0),
        'retention_s': value('sync.gc.retention', lambda x: isinstance(x, str) and bool(_RETENTION.fullmatch(x))),
        'resume': value('sync.resume', lambda x: isinstance(x, bool)),
    }
    if settings['retention_s'] is not _UNDECLARED:
        settings['retention_s'] = parse_retention(settings['retention_s'])
    if (settings['poll_s'] is not _UNDECLARED and settings['timeout_s'] is not _UNDECLARED
            and settings['poll_s'] > settings['timeout_s']):
        raise StorageError('storage.lock.poll_s must not exceed storage.lock.timeout_s')
    return reads, root, settings


def open_knowledge_store(config, command, *, project_root=None):
    """Build the knowledge store for a command; with rag.enabled false the request path gets none (R-009)."""
    if command in RAG_COMMANDS and not (isinstance(config.get('rag'), dict) and config['rag'].get('enabled') is True):
        raise StorageError('rag.enabled is false: no knowledge store is constructed (R-009)')
    reads, root, settings = _settings(config, command, project_root)
    if not any(read == 'storage.kb_store' for read in reads) or settings['timeout_s'] is _UNDECLARED:
        raise StorageError(f'command {command!r} does not read storage.kb_store and storage.lock')
    return KnowledgeStore(_resolved_path(config, 'storage.kb_store', root), project_root=root, **settings)


def open_data_store(config, command, *, project_root=None):
    """Traces and evaluation tables under storage.data_root; never touches the knowledge store."""
    reads, root, settings = _settings(config, command, project_root)
    if 'storage.data_root' not in reads or settings['table_format'] is _UNDECLARED:
        raise StorageError(f'command {command!r} does not read storage.data_root and storage.table_format')
    return DataStore(_resolved_path(config, 'storage.data_root', root), project_root=root,
                     table_format=settings['table_format'])


# ----------------------------------------------------------------------------- knowledge store


class KnowledgeStore:
    def __init__(self, root, *, project_root, timeout_s, poll_s, table_format=_UNDECLARED, keep=_UNDECLARED,
                 retention_s=_UNDECLARED, resume=_UNDECLARED, clock=time.time):
        if not (_positive_finite(timeout_s) and _positive_finite(poll_s)):
            raise StorageError(f'storage.lock values must be finite and positive: {timeout_s!r}, {poll_s!r}')
        self.root = Path(root).resolve()
        self.catalog = Catalog(project_root)
        self.timeout_s, self.poll_s = timeout_s, poll_s
        self._table_format, self._keep, self._retention_s, self._resume = table_format, keep, retention_s, resume
        self.clock = clock

    def path(self, relative):
        """The one path rule: inside the store root, and no link or junction on the way (F1, F2, R1, round 4).
        Single-path operations and traversals both take every path from here, when they act on it."""
        return real_inside(self.root, relative)

    def _setting(self, name, key):
        value = getattr(self, name)
        if value is _UNDECLARED:
            raise StorageError(f'{key} is not in the read set of this command')
        return value

    @property
    def table_format(self):
        return self._setting('_table_format', 'storage.table_format')

    def store_lock(self):
        return FileLock(self.path(LOCK), self.timeout_s, self.poll_s)

    def _children(self, relative):
        """Entries of a store directory; the directory is checked before it is listed, each entry after."""
        directory = self.path(relative)
        if not directory.is_dir():
            return []
        return [(name, self.path(f'{relative}/{name}')) for name in sorted(os.listdir(directory))]

    def walk(self, relative):
        """Every file below a store directory as (relative, path), in sorted order: the one store traversal.
        Each directory is checked by `path` before it is listed, so a link is refused, never entered (R1)."""
        found, pending = [], [relative]
        while pending:
            current = pending.pop()
            subdirectories = []
            for name, path in self._children(current):
                if path.is_dir():
                    subdirectories.append(f'{current}/{name}')
                else:
                    found.append((f'{current}/{name}', path))
            pending.extend(reversed(subdirectories))
        return found

    def _remove_tree(self, relative):
        """Delete a store directory after `walk` has checked that no link lies below it."""
        self.walk(relative)
        shutil.rmtree(self.path(relative))

    # -- CURRENT and registry (callers hold the store lock)

    def _read_current(self):
        path = self.path(CURRENT)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        parts = data.decode('ascii', 'replace').split(' ')
        if len(parts) != 2 or not parts[1].endswith('\n') or not re.fullmatch(r'[0-9a-f]{64}', parts[1][:-1]):
            raise StorageError(f'{path}: corrupt pointer {data[:80]!r}')
        return check_snapshot_id(parts[0]), parts[1][:-1]

    def _registry_name(self):
        try:
            name = self.path(REGISTRY_POINTER).read_bytes().decode('ascii', 'replace').strip()
        except FileNotFoundError:
            return None
        if not _REGISTRY_NAME.fullmatch(name):
            raise StorageError(f'{self.path(REGISTRY_POINTER)}: corrupt registry pointer {name[:80]!r}')
        return name

    def _registry(self):
        name = self._registry_name()
        if name is None:
            return {}
        table = read_table_file(self.path(f'registry/{name}'))
        self.catalog.validate('snapshots', table)
        return {row['snapshot_id']: row for row in self.catalog.rows('snapshots', table)}

    def _write_registry(self, rows):
        """New uniquely named file, then the pointer, then sweep: a crash at any step leaves one authority (F5)."""
        fmt = self.table_format
        table = self.catalog.build('snapshots', sorted(rows.values(), key=lambda row: row['created_at']))
        name = _segment_name(SUFFIX[fmt], 'snapshots-')
        write_table_file(self.path(f'registry/{name}'), table, fmt)
        atomic_write_bytes(self.path(REGISTRY_POINTER), f'{name}\n'.encode('ascii'))
        self._sweep_registry(name)

    def _sweep_registry(self, keep):
        """Delete registry leftovers. Only exact generated names qualify: their alphabet has one case, so a name
        that differs from `keep` as a string is a different file; another spelling of `keep` is left alone."""
        for name, path in self._children('registry'):
            if _REGISTRY_NAME.fullmatch(name) and name != keep and path.is_file():
                _unlink(path)

    def _reconcile(self, rows):
        """CURRENT is the truth: its snapshot is published, any other published one is retired."""
        current = self._read_current()
        changed = False
        for sid, row in rows.items():
            state = row['state']
            if current and sid == current[0] and state != 'published':
                row.update(state='published', published_at=row['published_at'] or _now(self.clock))
                changed = True
            elif state == 'published' and (not current or sid != current[0]):
                row['state'] = 'retired'
                changed = True
        return changed

    def snapshots(self):
        """The snapshots table as rows (kb status)."""
        if not self.root.exists():
            return []
        with self.store_lock():
            return list(self._registry().values())

    def current(self):
        if not self.root.exists():
            return None
        with self.store_lock():
            return self._read_current()

    def _manifest(self, directory, expected_sha=None):
        """The manifest rows of a snapshot directory (store-relative); with expected_sha, the bytes that are
        hashed are the bytes that are parsed."""
        path = self.path(f'{directory}/{MANIFEST}')
        try:
            data = path.read_bytes()
        except OSError as error:
            raise StorageError(f'{path}: manifest missing: {error}') from error
        if expected_sha is not None and hashlib.sha256(data).hexdigest() != expected_sha:
            raise StorageError(f'{path}: manifest checksum differs from CURRENT')
        try:
            table = pq.read_table(pa.BufferReader(data))
        except (OSError, pa.ArrowException) as error:
            raise StorageError(f'{path}: cannot read manifest: {error}') from error
        if not table.schema.equals(MANIFEST_SCHEMA, check_metadata=False):
            raise StorageError(f'{path}: malformed manifest schema')
        check_tags(table.schema, 'manifest')
        rows = table.to_pylist()
        names, artifacts = _namespace(), set()
        for row in rows:
            if row['kind'] not in MANIFEST_KINDS:
                raise StorageError(f'{path}: unknown manifest entry kind {row["kind"]!r}')
            if row['size_bytes'] is None or row['sha256'] is None:
                raise StorageError(f'{path}: entry {row["path"]!r} lacks size or checksum')
            # Manifest keys follow the identity rule too: one spelling, and no two entries one file (round 3).
            if row['kind'] == 'artifact':
                if check_artifact_path(row['path']) in artifacts:
                    raise StorageError(f'{path}: artifact {row["path"]!r} is listed twice')
                artifacts.add(row['path'])
            else:
                _admit(names, row['path'])
        return rows

    def _entry_path(self, directory, row):
        return self.path(row['path'] if row['kind'] == 'artifact' else f'{directory}/{row["path"]}')

    def _verify_dir(self, directory, expected_sha=None, *, full):
        """Not full (pin): the manifest hash and the size of each snapshot file. Full (finish, publish, rollback):
        every file and referenced artifact by size and a fresh SHA-256, then tags and schemas, completeness,
        references and the embedding space (F4, F6, fix round 2 R2)."""
        rows = self._manifest(directory, expected_sha)
        tables = {}
        for row in rows:
            if row['kind'] == 'artifact' and not full:
                continue  # readers never open artifacts; a pin stays O(snapshot files)
            path = self._entry_path(directory, row)
            if full:
                _verify(path, row['size_bytes'], row['sha256'], fresh=True)
                if row['kind'] == 'table':
                    tables[row['table']] = read_table_file(path)
                    self.catalog.validate(row['table'], tables[row['table']])
            else:
                try:
                    size = path.stat().st_size
                except OSError as error:
                    raise StorageError(f'{path}: listed in the manifest but missing: {error}') from error
                if size != row['size_bytes']:
                    raise StorageError(f'{path}: size {size} differs from the manifest ({row["size_bytes"]})')
        if full:
            missing = [name for name in SNAPSHOT_TABLES if name not in tables]
            if missing:
                raise StorageError(f'snapshot {directory}: missing tables {missing}')
            check_references(tables)
            check_embedding_space(tables)
        return rows

    def _snapshot_dir(self, sid):
        """The store-relative directory of a snapshot; `path` resolves it wherever it is used."""
        return f'snapshots/{check_snapshot_id(sid)}'

    def verify_snapshot(self, sid, manifest_sha256):
        """Everything a published snapshot promises, checked on disk now; publish and rollback call it under the
        store lock right before they move CURRENT."""
        return self._verify_dir(self._snapshot_dir(sid), manifest_sha256, full=True)

    # -- readers

    def pin(self):
        """Pin the published snapshot until release; None when nothing is published (SNAPSHOT_MISSING)."""
        if not self.root.exists():
            return None
        with self.store_lock():
            current = self._read_current()
            if current is None:
                return None
            sid, sha = current
            manifest = self._verify_dir(self._snapshot_dir(sid), sha, full=False)
            lease = FileLock(self.path(f'leases/{sid}/{uuid.uuid4().hex}.lease'), self.timeout_s, self.poll_s)
            lease.acquire()  # a new file a scanner holds briefly is waited for, up to storage.lock.timeout_s
        return PinnedSnapshot(self, sid, manifest, lease)

    def hold(self, snapshot_id, request_id, expires_at):
        """Keep a snapshot for a trace (its attachments) until expires_at, even past keep (S6-envelope.md:20).

        Admission and GC's `deleting` mark both run under the store lock, so an admitted hold always protects
        its snapshot (F3)."""
        check_snapshot_id(snapshot_id)
        check_request_id(request_id)
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise StorageError('expires_at must be a timezone-aware datetime')
        with self.store_lock():
            row = self._registry().get(snapshot_id)
            if row is None or row['state'] not in HOLDABLE:
                raise StorageError(f'snapshot {snapshot_id} cannot be held: {row and row["state"]}')
            path = self._hold_path(snapshot_id, request_id)
            table = pa.Table.from_pylist([{'request_id': request_id, 'snapshot_id': snapshot_id,
                                           'expires_at': expires_at}], schema=HOLD_SCHEMA)
            write_table_file(path, table, 'parquet')

    def release_hold(self, snapshot_id, request_id):
        check_snapshot_id(snapshot_id)
        check_request_id(request_id)  # F1: a path is not a request id
        with self.store_lock():
            path = self._hold_path(snapshot_id, request_id)
            if path.exists():
                _unlink(path)

    def _hold_path(self, snapshot_id, request_id):
        """The hold file of a request, named by its identity key (a caller-chosen name, fix round 3): a request
        id that differs only in case from the one that holds the file is refused, so it can neither replace
        nor release another request's hold. Device names ('nul', 'CON' fit the request-id pattern) are refused
        by the segment rule `path` applies to every name. Callers hold the store lock."""
        path = self.path(f'holds/{snapshot_id}/{identity_key(request_id)}.parquet')
        if path.exists():
            owner = _read_hold(path)['request_id']
            if owner != request_id:
                raise StorageError(f'request id {request_id!r} collides with the hold of {owner!r}')
        return path

    def writer(self):
        return Writer(self)


class PinnedSnapshot:
    """A reader's view of one published snapshot; it trusts only files the manifest lists."""

    def __init__(self, store, snapshot_id, manifest, lease):
        self.store, self.snapshot_id, self._lease = store, snapshot_id, lease
        self._entries = {(row['kind'], row['table'] if row['kind'] == 'table' else row['path']): row
                         for row in manifest if row['kind'] != 'artifact'}
        self._directory = store._snapshot_dir(snapshot_id)  # store-relative; resolved at each read

    def _entry(self, kind, name):
        if not self._lease.held:
            raise StorageError(f'snapshot {self.snapshot_id}: pin already released')
        row = self._entries.get((kind, name))
        if row is None:
            raise StorageError(f'snapshot {self.snapshot_id}: {name!r} is not listed in the manifest')
        path = self.store.path(f'{self._directory}/{row["path"]}')
        _verify(path, row['size_bytes'], row['sha256'])
        return path

    def read_table(self, name):
        table = read_table_file(self._entry('table', name))
        self.store.catalog.validate(name, table)
        return table

    def read_rows(self, name):
        return self.store.catalog.rows(name, self.read_table(name))

    def read_file(self, relative):
        return self._entry('file', relative).read_bytes()

    def release(self):
        self._lease.release()
        try:  # the path rule again, now: a link put on the way since the pin is refused, not deleted through
            _unlink(self.store.path(f'leases/{self.snapshot_id}/{self._lease.path.name}'))
        except OSError:
            pass  # GC may hold it open while checking liveness; it removes stale leases itself.

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


# ----------------------------------------------------------------------------- writer side


@dataclass
class GcReport:
    collected: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    leased: list = field(default_factory=list)
    held: list = field(default_factory=list)
    session: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    artifacts_removed: list = field(default_factory=list)
    artifacts_marked: list = field(default_factory=list)


def _writer_operation(method):
    """The serialization rule (fix round 3, R2-B): a writer-side method runs under its Writer's mutex, inside the
    live session, from its first check to its last act, so no other thread of the same Writer can undo a fact
    in between. The mutex is reentrant: an operation may call another (finish calls write_manifest)."""
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        writer = self if isinstance(self, Writer) else self.writer
        with writer._mutex:
            writer.require()
            return method(self, *args, **kwargs)
    return run


class Writer:
    """One writer session (kb sync, kb gc, kb rollback): holds the WRITER lock throughout.

    A Writer is single-use, so the session its helpers were made in is the only one they can ever act in, and
    each session has its own run id, which `sync.resume: false` relies on (fix round 2, R3). Its operations and
    those of its helpers are serialized by one mutex (`_writer_operation`)."""

    def __init__(self, store):
        self.store = store
        self.run_id = None  # set when the session opens
        self._entered = False
        self._mutex = threading.RLock()
        self._lock = None  # the WRITER file lock, built when the session opens so its path is checked then
        self._built = set()  # validated snapshots of this session, not yet published: GC skips them (G4)
        self._builders = []
        self._references = None  # cache of _referenced(); reset whenever a reference is added or dropped

    def __enter__(self):
        with self._mutex:
            if self._entered:
                raise StorageError('a Writer is single-use: open a new session with store.writer()')
            self._entered = True
            self._lock = FileLock(self.store.path(WRITER), self.store.timeout_s, self.store.poll_s).acquire()
            try:
                self._recover()
            except BaseException:
                self._lock.release()
                raise
            self.run_id = uuid.uuid4().hex
            return self

    def __exit__(self, exc_type, *exc):
        with self._mutex:
            try:
                if exc_type is None:
                    self._compact_state()
            finally:
                if self._lock is not None:
                    self._lock.release()

    def require(self):
        """Every writer-side action runs inside this live session (F7); a closed Writer never reopens (R3)."""
        if self._lock is None or not self._lock.held:
            raise StorageError('writer session is not open: a helper outlived its writer')

    def _recover(self):
        """An interrupted build left a .building directory: it was never readable, so remove and redo it."""
        for name, _ in self.store._children('snapshots'):
            if name.startswith(BUILDING):
                self.store._remove_tree(f'snapshots/{name}')
        with self.store.store_lock():
            rows = self.store._registry()
            changed = self.store._reconcile(rows)
            for row in rows.values():
                if row['state'] == 'building':
                    row['state'] = 'failed'
                    changed = True
            if changed:
                self.store._write_registry(rows)
            elif self.store._registry_name() is not None:
                self.store._sweep_registry(self.store._registry_name())  # leftovers of a crash (F5)

    # -- sync_state

    def _state_segments(self):
        return [path for name, path in self.store._children('registry/sync_state')
                if not name.startswith('.') and path.is_file()]

    def _state_rows(self):
        """Latest row per (stage, input_key); segment names sort in write order."""
        latest = {}
        for path in self._state_segments():
            table = read_table_file(path)
            self.store.catalog.validate('sync_state', table)
            for row in self.store.catalog.rows('sync_state', table):
                # Paths in checkpoints follow the identity rule like every other admitted name (round 3).
                if row['output_key'] is not None:
                    check_artifact_path(row['output_key'])
                if row['stage'] == GC_STAGE:
                    check_artifact_path(row['input_key'])
                latest[(row['stage'], row['input_key'])] = row
        return latest

    def _append_state(self, rows):
        self.require()
        fmt = self.store.table_format
        table = self.store.catalog.build('sync_state', rows)
        write_table_file(self.store.path(f'registry/sync_state/{_segment_name(SUFFIX[fmt])}'), table, fmt)

    def _rewrite_state(self, rows):
        old = self._state_segments()
        if rows:
            self._append_state(list(rows.values()))
        for path in old:
            _unlink(path)

    def _compact_state(self):
        if len(self._state_segments()) > 1:
            self._rewrite_state(self._state_rows())

    @_writer_operation
    def checkpoints(self, stage):
        return Checkpoints(self, stage)

    # -- snapshots

    @_writer_operation
    def begin_snapshot(self, *, parent=None):
        builder = SnapshotBuilder(self, parent)
        self._builders.append(builder)
        return builder

    @_writer_operation
    def publish(self, snapshot_id):
        """Move CURRENT to a validated snapshot: CURRENT first, then the registry (reconciled on recovery)."""
        check_snapshot_id(snapshot_id)
        with self.store.store_lock():
            rows = self.store._registry()
            row = rows.get(snapshot_id)
            if row is None or row['state'] != 'validated':
                raise StorageError(f'snapshot {snapshot_id} is not validated: {row and row["state"]}')
            self._move_current(rows, snapshot_id)
        self._built.discard(snapshot_id)

    @_writer_operation
    def rollback(self, snapshot_id):
        """Move CURRENT back to a retained snapshot; a collected or damaged target is refused."""
        check_snapshot_id(snapshot_id)
        with self.store.store_lock():
            rows = self.store._registry()
            row = rows.get(snapshot_id)
            if row is None or row['state'] != 'retired':
                raise StorageError(f'snapshot {snapshot_id} is not a retained snapshot: {row and row["state"]}')
            if not self.store.path(self.store._snapshot_dir(snapshot_id)).is_dir():
                raise StorageError(f'snapshot {snapshot_id} was collected')
            self._move_current(rows, snapshot_id)

    def _move_current(self, rows, snapshot_id):
        """The one place CURRENT moves; the caller holds the store lock, under which the snapshot is verified in
        full right before the move, so nothing changed since finish can be published (F4, R2)."""
        sha = rows[snapshot_id]['manifest_sha256']
        self.store.verify_snapshot(snapshot_id, sha)
        atomic_write_bytes(self.store.path(CURRENT), f'{snapshot_id} {sha}\n'.encode('ascii'))
        self.store._reconcile(rows)
        rows[snapshot_id]['published_at'] = _now(self.store.clock)
        self.store._write_registry(rows)

    # -- garbage collection

    def _live_leases(self):
        live = set()
        for sid, _ in self.store._children('leases'):
            check_snapshot_id(sid)  # only the exact spelling protects that snapshot; anything else is refused
            for name, path in self.store._children(f'leases/{sid}'):
                if not name.endswith('.lease'):
                    continue
                probe = FileLock(path, self.store.timeout_s, self.store.poll_s)
                if probe.try_acquire():  # no process holds it: its reader ended or crashed
                    probe.release()
                    try:
                        _unlink(path)
                    except OSError:
                        pass
                else:
                    live.add(sid)
        return live

    def _live_holds(self, now):
        live = set()
        for sid, _ in self.store._children('holds'):
            for name, path in self.store._children(f'holds/{sid}'):
                if not name.endswith('.parquet'):
                    continue
                row = _read_hold(path)
                if row['expires_at'] > now:
                    live.add(row['snapshot_id'])
                else:
                    _unlink(path)
        return live

    @_writer_operation
    def gc(self):
        """Remove snapshots and artifacts nothing needs (docs/execution.md:70, model-storage.md:29).

        Eligibility and the `deleting` mark are decided under one store-lock section; a hold or rollback that
        comes later sees `deleting` and is refused (F3). Validated snapshots of this session are skipped (G4).
        The Writer mutex is held throughout, so no reference can be added between deciding and deleting."""
        keep = self.store._setting('_keep', 'sync.snapshots.keep')
        retention = self.store._setting('_retention_s', 'sync.gc.retention')
        now = _now(self.store.clock)
        report = GcReport()
        # Listed before any change: a link under artifacts/ refuses the whole GC before it marks anything (R1).
        # Only this writer adds artifacts, so the list stays complete until the artifact pass.
        artifact_files = self.store.walk('artifacts')
        self._referenced()  # likewise read before any change: a link at a live snapshot refuses the whole GC
        with self.store.store_lock():
            rows = self.store._registry()
            self.store._reconcile(rows)
            report.leased = sorted(self._live_leases())
            report.held = sorted(self._live_holds(now))
            report.session = sorted(self._built)
            retired = sorted((row for row in rows.values() if row['state'] == 'retired'),
                             key=lambda row: row['published_at'] or row['created_at'], reverse=True)
            report.kept = [row['snapshot_id'] for row in retired[:keep]]
            protected = set(report.kept) | set(report.leased) | set(report.held) | self._built
            candidates = [sid for sid, row in rows.items()
                          if row['state'] == 'deleting'
                          or (row['state'] in ('retired', 'validated', 'failed') and sid not in protected)]
            for sid in candidates:
                rows[sid]['state'] = 'deleting'
            self.store._write_registry(rows)
        removed = []
        for sid in candidates:
            # Only CURRENT gains leases, and holds and rollback refuse `deleting`, so this runs outside the lock.
            try:
                self.store._remove_tree(self.store._snapshot_dir(sid))
                removed.append(sid)
            except FileNotFoundError:
                removed.append(sid)
            except (OSError, StorageError) as error:  # a foreign handle (T-006 probe 5a) or a link: retried next GC
                report.skipped.append((sid, str(error)))
        with self.store.store_lock():
            rows = self.store._registry()
            for sid in removed:
                rows[sid]['state'] = 'collected'
            self.store._write_registry(rows)
        report.collected = removed
        self._references = None  # collected snapshots no longer protect their artifacts
        self._gc_artifacts(artifact_files, now, retention, report)
        return report

    def _referenced(self):
        """Artifact path -> SHA-256 values recorded for it by live snapshots (LIVE states) and by this session's
        unfinished builds. GC keeps these paths; commit() refuses to change their bytes (R2)."""
        if self._references is None:
            with self.store.store_lock():
                rows = self.store._registry()
            entries = [entry for builder in self._builders for entry in builder.entries]
            for sid, row in rows.items():
                directory = self.store._snapshot_dir(sid)
                if row['state'] in LIVE and self.store.path(f'{directory}/{MANIFEST}').exists():
                    entries.extend(self.store._manifest(directory))
            references = {}
            for entry in entries:
                if entry['kind'] == 'artifact':
                    references.setdefault(entry['path'], set()).add(entry['sha256'])
            self._references = references
        return self._references

    def _gc_artifacts(self, artifact_files, now, retention, report):
        referenced = self._referenced()
        state = self._state_rows()
        present = set()
        for relative, _ in artifact_files:  # listed when GC began: each delete takes its path again, now
            name = relative.rsplit('/', 1)[1]
            if name.startswith('.') and name.endswith('.tmp'):
                _unlink(self.store.path(relative))  # a write interrupted before its replace (writer lock held)
                continue
            try:  # a name on disk that is not the generated form may be another spelling of a referenced one
                check_artifact_path(relative)
            except StorageError as error:
                report.skipped.append((relative, f'never marked or deleted: {error}'))
                continue
            present.add(relative)
            mark = state.get((GC_STAGE, relative))
            if relative in referenced:
                state.pop((GC_STAGE, relative), None)
                continue
            if mark is None:
                mark = self._row(GC_STAGE, relative, 'queued', recorded_at=now)
                state[(GC_STAGE, relative)] = mark
                report.artifacts_marked.append(relative)
            if (now - mark['recorded_at']).total_seconds() >= retention:
                _unlink(self.store.path(relative))
                present.discard(relative)
                report.artifacts_removed.append(relative)
        for key, row in list(state.items()):
            gone_mark = key[0] == GC_STAGE and key[1] not in present
            gone_output = key[0] != GC_STAGE and row['output_key'] and row['output_key'].startswith('artifacts/') \
                and row['output_key'] not in present
            if gone_mark or gone_output:
                del state[key]
        self._rewrite_state(state)

    def _row(self, stage, input_key, state, *, output=None, revisions=None, call_records=(), error=None,
             recorded_at=None):
        revisions = revisions or {}
        return {'stage': stage, 'input_key': input_key, 'output_key': output and output[0],
                'output_size': output and output[1], 'output_sha256': output and output[2], 'state': state,
                'model_revision': revisions.get('model'), 'prompt_revision': revisions.get('prompt'),
                'schema_revision': revisions.get('schema'), 'call_records': list(call_records), 'error': error,
                'run_id': self.run_id, 'recorded_at': recorded_at or _now(self.store.clock)}


class Checkpoints:
    """Resumable progress of one stage: artifact first, sync_state row last (docs/execution.md:66)."""

    def __init__(self, writer, stage):
        if not isinstance(stage, str) or not stage or stage == GC_STAGE:
            raise StorageError(f'invalid stage {stage!r}')
        self.writer, self.stage = writer, stage
        self._rows = {key[1]: row for key, row in writer._state_rows().items() if key[0] == stage}

    @_writer_operation
    def state(self, input_key):
        return self._rows.get(check_artifact_key(input_key))

    @_writer_operation
    def done(self, input_key):
        """The done row only if its artifact is intact by size and SHA-256 (F8); with sync.resume false only rows
        of this session count. A caller acting on the answer is protected later too: reference_artifacts records
        the bytes, and finish and publish verify them again (fix round 3: the done() gap)."""
        row = self._rows.get(check_artifact_key(input_key))
        if row is None or row['state'] != 'done':
            return None
        resume = self.writer.store._setting('_resume', 'sync.resume')
        if not resume and row['run_id'] != self.writer.run_id:
            return None
        if row['output_key']:
            path = self.writer.store.path(row['output_key'])  # a link on the way is refused, not "redone"
            try:  # skipping work acts on the row: re-hash the artifact now, never trust a cached hash (R2 class)
                _verify(path, row['output_size'], row['output_sha256'], fresh=True)
            except StorageError:
                return None  # missing or corrupt output is redone, never skipped
        return row

    @_writer_operation
    def commit(self, input_key, kind, data, *, revisions, table=None, dim=None, call_records=()):
        """Write the artifact atomically, then its done row; a crash between leaves no row and it is redone.

        An artifact a live snapshot or an unfinished build references is immutable: content whose SHA-256 differs
        from the recorded one is refused before it replaces the file (R2). Writing the recorded bytes again (to
        repair a damaged file) is allowed. The check and the replace run under the Writer mutex (R2-B), and the
        path has its one generated spelling and no link on the way (`store.path`), so the check compares the very
        file the write replaces (R2-A; round 4, F1)."""
        store = self.writer.store

        def immutable(size, sha):
            recorded = self.writer._referenced().get(relative)
            if recorded and sha not in recorded:
                raise StorageError(f'{relative}: referenced by a snapshot, so its bytes cannot change')
        if table is not None:
            if table not in SPECS:
                raise StorageError(f'unknown table {table!r}')
            fmt = 'arrow' if SPECS[table].vector else store.table_format
            if not isinstance(data, pa.Table):
                data = store.catalog.build(table, data, dim)
            store.catalog.validate(table, data)
            relative = artifact_path(kind, input_key, SUFFIX[fmt])
            size, sha = write_table_file(store.path(relative), data, fmt, check=immutable)
        else:
            if not isinstance(data, (bytes, bytearray)):
                raise StorageError('a blob artifact must be bytes')
            relative = artifact_path(kind, input_key, '.bin')
            size, sha = atomic_write_bytes(store.path(relative), bytes(data), check=immutable)
        row = self.writer._row(self.stage, input_key, 'done', output=(relative, size, sha), revisions=revisions,
                               call_records=call_records)
        self.writer._append_state([row])
        self._rows[input_key] = row
        return relative

    @_writer_operation
    def queue(self, input_key, *, revisions=None):
        row = self.writer._row(self.stage, check_artifact_key(input_key), 'queued', revisions=revisions)
        self.writer._append_state([row])
        self._rows[input_key] = row

    @_writer_operation
    def fail(self, input_key, error, *, revisions=None):
        row = self.writer._row(self.stage, check_artifact_key(input_key), 'failed', revisions=revisions,
                               error=str(error))
        self.writer._append_state([row])
        self._rows[input_key] = row

    @_writer_operation
    def load(self, input_key):
        """Read a done artifact, refusing a size or checksum mismatch."""
        row = self._rows.get(check_artifact_key(input_key))
        if row is None or row['state'] != 'done' or not row['output_key']:
            raise StorageError(f'{self.stage}: no done artifact for {input_key}')
        path = self.writer.store.path(row['output_key'])
        _verify(path, row['output_size'], row['output_sha256'])
        if path.suffix == '.bin':
            return path.read_bytes()
        return read_table_file(path)


class SnapshotBuilder:
    """Builds a snapshot beside the current one: data first, manifest last, validate, then rename."""

    def __init__(self, writer, parent):
        self.writer, self.store = writer, writer.store
        self.snapshot_id = new_snapshot_id(self.store.clock)
        self.relative = f'snapshots/{BUILDING}{self.snapshot_id}'  # resolved by `store.path` at every use
        self.directory.mkdir(parents=True)
        self.entries = []
        self._formats = {}
        self._names = _namespace()
        if parent is not None:
            check_snapshot_id(parent)
        with self.store.store_lock():
            rows = self.store._registry()
            rows[self.snapshot_id] = {'snapshot_id': self.snapshot_id, 'parent_snapshot_id': parent,
                                      'state': 'building', 'created_at': _now(self.store.clock),
                                      'published_at': None, 'manifest_sha256': None, 'source_scan_summary': {}}
            self.store._write_registry(rows)

    @property
    def directory(self):
        return self.store.path(self.relative)

    def _target(self, relative):
        """Admit a snapshot path and return where to write it: the identity rule against every earlier entry,
        the path rule, then the disk itself. The build directory is new and only this builder writes in it, so a
        target that already exists can only be an earlier entry under another spelling the identity key did not
        foresee: refused as well."""
        _admit(self._names, relative)
        target = self.store.path(f'{self.relative}/{relative}')
        if os.path.lexists(target):
            raise StorageError(f'{relative!r} is already part of this snapshot')
        return target

    @_writer_operation
    def write_table(self, name, data, *, dim=None):
        if name not in SNAPSHOT_TABLES:
            raise StorageError(f'{name!r} is not a snapshot table')
        if name in self._formats:
            raise StorageError(f'table {name!r} was already written')
        fmt = 'arrow' if SPECS[name].vector else self.store.table_format  # embeddings are Arrow (README.md:26)
        if not isinstance(data, pa.Table):
            data = self.store.catalog.build(name, data, dim)
        self.store.catalog.validate(name, data)
        relative = f'{name}{SUFFIX[fmt]}'
        size, sha = write_table_file(self._target(relative), data, fmt)
        self._formats[name] = fmt
        self.entries.append({'kind': 'table', 'path': relative, 'table': name, 'format': fmt, 'size_bytes': size,
                             'sha256': sha, 'rows': data.num_rows})

    @_writer_operation
    def add_file(self, relative, data):
        size, sha = atomic_write_bytes(self._target(relative), bytes(data))
        self.entries.append({'kind': 'file', 'path': relative, 'table': None, 'format': None, 'size_bytes': size,
                             'sha256': sha, 'rows': None})

    @_writer_operation
    def reference_artifacts(self, paths):
        """Record each artifact with its size and SHA-256 now; finish and publish verify them again (F4). Only
        the exact generated spelling is admitted (R2-A), and the file on disk must carry that spelling."""
        for relative in paths:
            check_artifact_path(relative)
            if any(entry['path'] == relative for entry in self.entries):
                raise StorageError(f'{relative!r} is already referenced by this snapshot')
            path = self.store.path(relative)
            if not path.is_file():
                raise StorageError(f'{relative!r} is not a stored artifact')
            self.entries.append({'kind': 'artifact', 'path': relative, 'table': None, 'format': None,
                                 'size_bytes': path.stat().st_size, 'sha256': sha256_file(path), 'rows': None})
            self.writer._references = None  # from now on commit() may not change this artifact

    @_writer_operation
    def write_manifest(self):
        """The manifest is written last; a directory without it is never trusted."""
        rows = [{**entry, 'physical_version': 1} for entry in self.entries]
        table = pa.Table.from_pylist(rows, schema=MANIFEST_SCHEMA)
        return write_table_file(self.store.path(f'{self.relative}/{MANIFEST}'), table, 'parquet')[1]

    @_writer_operation
    def finish(self, *, source_scan_summary=None):
        """Write the manifest, validate everything on disk, then rename to snapshots/<id> (state validated).
        Verification, rename and registry update run under the Writer mutex, so no operation of this Writer
        changes a file in between; publish verifies everything again before CURRENT moves."""
        missing = [name for name in SNAPSHOT_TABLES if name not in self._formats]
        if missing:
            raise StorageError(f'snapshot {self.snapshot_id}: missing tables {missing}')
        sha = self.write_manifest()
        self.store._verify_dir(self.relative, sha, full=True)
        final = self.store.path(self.store._snapshot_dir(self.snapshot_id))
        os.rename(self.directory, final)  # a new name, never over an existing directory (T-006 probe 7)
        fsync_dir(final.parent)
        with self.store.store_lock():
            rows = self.store._registry()
            rows[self.snapshot_id].update(state='validated', manifest_sha256=sha,
                                          source_scan_summary=source_scan_summary or {})
            self.store._write_registry(rows)
        self.writer._built.add(self.snapshot_id)
        self.writer._builders.remove(self)  # its artifact references live on in the manifest of a LIVE snapshot
        return self.snapshot_id


# ----------------------------------------------------------------------------- data store


_DATA_DIRS = {'request_traces': 'traces', 'eval_scenarios': 'eval', 'eval_labels': 'eval', 'eval_results': 'eval'}


class DataStore:
    """Append-only segments for traces and evaluation tables under storage.data_root."""

    def __init__(self, root, *, project_root, table_format):
        self.root = Path(root).resolve()
        self.catalog = Catalog(project_root)
        self.table_format = table_format

    def _relative(self, name):
        if name not in _DATA_DIRS:
            raise StorageError(f'{name!r} is not a data-store table')
        return f'{_DATA_DIRS[name]}/{name}'

    def append(self, name, rows):
        table = self.catalog.build(name, rows)
        # the knowledge store's path rule: no link below the root, not even one that stays inside it
        path = real_inside(self.root, f'{self._relative(name)}/{_segment_name(SUFFIX[self.table_format])}')
        write_table_file(path, table, self.table_format)
        return path

    def read(self, name):
        relative = self._relative(name)
        directory = real_inside(self.root, relative)  # checked before it is listed, like every store traversal
        parts = []
        if directory.is_dir():
            for entry in sorted(os.listdir(directory)):
                path = real_inside(self.root, f'{relative}/{entry}')
                if path.is_file() and not entry.startswith('.'):
                    format_of(path)
                    parts.append(read_table_file(path))
        table = pa.concat_tables(parts) if parts else self.catalog.build(name, [])
        self.catalog.validate(name, table)  # a key repeated across segments is refused here
        return table

    def read_rows(self, name):
        return self.catalog.rows(name, self.read(name))
