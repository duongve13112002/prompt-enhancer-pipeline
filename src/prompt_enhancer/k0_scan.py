"""Read-only, no-follow scan of direct knowledge-base concept folders."""

from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import errno
import hashlib
import os
from pathlib import Path
import stat
import codecs

from .k0 import parse_info

_CHUNK_BYTES = 1024 * 1024  # safety-bound: limit the memory used to hash an image
_REPARSE = 0x400  # protocol-bound: Windows FILE_ATTRIBUTE_REPARSE_POINT


class ScanError(OSError):
    """A complete, safe source listing could not be obtained."""


class LinkRefused(ScanError):
    """A source entry is a link or reparse point."""


class CacheError(RuntimeError):
    """Checkpoint I/O failed; this is not an unreadable source file."""


class MissingFileID(ScanError):
    """The filesystem cannot provide the identities needed for safe traversal."""


def decode_info(raw, encoding):
    return raw.decode(encoding).removeprefix('\ufeff')


def validate_scan(info_names, encoding, extensions, separator, workers):
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be a positive integer')
    if not isinstance(info_names, (list, tuple)) or not info_names or any(
            not isinstance(n, str) or not n or n in ('.', '..') or any(c in n for c in '/\\:') for n in info_names):
        raise ValueError('knowledge.info_file.names: expected nonempty portable filenames')
    try:
        codecs.lookup(encoding)
    except (LookupError, TypeError) as error:
        raise ValueError('knowledge.info_file.encoding: unknown codec') from error
    if not isinstance(extensions, (list, tuple)) or not extensions or any(
            not isinstance(e, str) or len(e) < 2 or not e.startswith('.') or any(c in e for c in '/\\:') for e in extensions):
        raise ValueError('knowledge.image_extensions: invalid extensions')
    if not isinstance(separator, str) or not separator:
        raise ValueError('knowledge.concept_separator: expected nonempty string')


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & _REPARSE)


def _native_open(path, parent_fd, name, directory):
    """Open one component without following its final link; callers hold its parent."""
    if os.name != 'nt':
        if (not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK') or
                not hasattr(os, 'O_DIRECTORY') or os.open not in os.supports_dir_fd):
            raise ScanError('safe no-follow directory-relative open is unavailable')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise LinkRefused(f'{path}: link refused') from error
            raise

    import ctypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    # GENERIC_READ, share read/write but not delete, OPEN_EXISTING, OPEN_REPARSE_POINT.
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = create(str(path), 0x80000000, 3, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = (ctypes.c_uint32 * 2)()
        if not query(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes[0] & _REPARSE:
            raise LinkRefused(f'{path}: reparse point refused')
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    return fd


def _lstat(path, parent_fd, name):
    if os.name == 'nt':
        return os.lstat(path)
    if os.stat not in os.supports_dir_fd:
        raise ScanError('safe directory-relative stat is unavailable')
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


@contextmanager
def _opened(path, parent_fd, name, directory):
    before = _lstat(path, parent_fd, name)
    if _is_link(before):
        raise LinkRefused(f'{path}: link refused')
    fd = _native_open(path, parent_fd, name, directory)
    try:
        after = os.fstat(fd)
        if not before.st_ino or not after.st_ino:
            raise MissingFileID(f'{path}: filesystem does not provide file-ID support')
        if ((before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)) !=
                (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) or _is_link(after)):
            raise ScanError(f'{path}: entry changed during open')
        if directory and not stat.S_ISDIR(after.st_mode):
            raise ScanError(f'{path}: expected a directory')
        if not directory and not stat.S_ISREG(after.st_mode):
            raise ScanError(f'{path}: expected a regular file')
        yield fd
    finally:
        os.close(fd)


def _names(path, fd):
    if os.name == 'nt':
        return sorted(os.listdir(path))
    if os.listdir not in os.supports_fd:
        raise ScanError('safe directory-handle listing is unavailable')
    return sorted(os.listdir(fd))


def _read(path, parent_fd, name, *, text_encoding, cache=None):
    with _opened(path, parent_fd, name, False) as fd:
        initial = os.fstat(fd)
        if cache is not None:
            try:
                cached = cache.get(path, initial, text_encoding)
            except OSError as error:
                raise CacheError('checkpoint cache read failed') from error
            if cached is not None:
                final = os.fstat(fd)
                if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                    raise ScanError(f'{path}: changed while reading cache')
                return cached
        digest = hashlib.sha256()
        chunks = [] if text_encoding is not None else None
        while True:
            block = os.read(fd, _CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        final = os.fstat(fd)
        if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise ScanError(f'{path}: changed while reading')
        identity = 'sha256:' + digest.hexdigest()
        raw = b''.join(chunks) if chunks is not None else None
        if raw is not None:
            record = {'hash': identity, 'text': decode_info(raw, text_encoding)}
        else:
            record = {'image_id': identity, 'size_bytes': final.st_size,
                      'modified_at': datetime.fromtimestamp(final.st_mtime, timezone.utc)}
        if cache is not None:
            try:
                cache.put(path, final, text_encoding, record, raw=raw)
            except OSError as error:
                raise CacheError('checkpoint cache write failed') from error
        return record


def read_source_file(root, parts):
    """Read one regular file at `root`/`parts` read-only, refusing a link on every component (R-014, K1 input).

    `root` is the already resolved knowledge root; `parts` are the relative components K0 recorded.
    """
    parts = tuple(parts)
    if not parts or any(not part or part in ('.', '..') or '/' in part or '\\' in part or '\x00' in part
                        for part in parts):
        raise ScanError(f'invalid relative source path {parts!r}')
    root = Path(root)
    with ExitStack() as stack:
        anchor = Path(root.anchor)
        parent = stack.enter_context(_opened(anchor, None, str(anchor), True))
        current = anchor
        for part in root.parts[1:] + parts[:-1]:
            current = current / part
            parent = stack.enter_context(_opened(current, parent, part, True))
        path = current / parts[-1]
        with _opened(path, parent, parts[-1], False) as fd:
            initial = os.fstat(fd)
            chunks = []
            while True:
                block = os.read(fd, _CHUNK_BYTES)
                if not block:
                    break
                chunks.append(block)
            final = os.fstat(fd)
            if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                raise ScanError(f'{path}: changed while reading')
    return b''.join(chunks)


def scan_source(root, *, info_names, encoding, extensions, separator, previous, cache=None, workers=1,
                resolved_root=None):
    """Scan a source tree, preserving supplied previous records on file read failure.

    ``previous`` has the same folder mapping shape as the returned ``folders``.
    Without a supplied cache every selected file is hashed.
    Directory errors raise before a result is returned. Links are never opened for data.
    """
    validate_scan(info_names, encoding, extensions, separator, workers)
    configured_root = Path(os.path.abspath(root))
    root = Path(resolved_root) if resolved_root is not None else configured_root.resolve()
    report = {'unreadable': [], 'links': [], 'nested': [], 'duplicate_names': [],
              'description_mismatch': [], 'protected_folders': [], 'protected_info': [],
              'ignored': [], 'configured_root': str(configured_root), 'resolved_root': str(root)}
    folders = {}
    extension_set = {value.casefold() for value in extensions}
    with ExitStack() as stack:
        anchor = Path(root.anchor)
        parent = stack.enter_context(_opened(anchor, None, str(anchor), True))
        current = anchor
        for part in root.parts[1:]:
            current = current / part
            parent = stack.enter_context(_opened(current, parent, part, True))
        try:
            children = _names(root, parent)
        except OSError as error:
            raise ScanError(f'{root}: cannot list source directory') from error
        for name in children:
            path = root / name
            try:
                metadata = _lstat(path, parent, name)
            except OSError as error:
                raise ScanError(f'{path}: cannot classify source entry') from error
            if _is_link(metadata):
                report['links'].append(name)
                report['protected_folders'].append(name)
                if name in previous:
                    folders[name] = deepcopy(previous[name])
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                if stat.S_ISREG(metadata.st_mode):
                    report['ignored'].append(name)
                continue
            try:
                with _opened(path, parent, name, True) as folder_fd, ThreadPoolExecutor(max_workers=workers) as pool:
                    entries = _names(path, folder_fd)
                    previous_folder = previous.get(name, {'info': None, 'images': {}})
                    folder = {'info': None, 'images': {}}
                    selected = next((item for item in info_names if item in entries), None)
                    pending = deque()

                    def collect():
                        future, entry, relative, is_info = pending.popleft()
                        try:
                            record = future.result()
                        except (OSError, UnicodeError) as error:
                            if isinstance(error, MissingFileID):
                                raise
                            report['unreadable'].append(relative)
                            if isinstance(error, LinkRefused):
                                report['links'].append(relative)
                            if is_info:
                                report['protected_info'].append(name)
                                folder['info'] = deepcopy(previous_folder['info'])
                            elif entry in previous_folder['images']:
                                folder['images'][entry] = deepcopy(previous_folder['images'][entry])
                            return
                        if is_info:
                            folder['info'] = record
                            status = parse_info(record['text'], separator)['status']
                            if status == 'duplicate_names':
                                report['duplicate_names'].append(name)
                            elif status == 'description_mismatch':
                                report['description_mismatch'].append(name)
                        else:
                            folder['images'][entry] = record

                    for entry in entries:
                        child = path / entry
                        relative = f'{name}/{entry}'
                        is_info = entry == selected
                        is_image = Path(entry).suffix.casefold() in extension_set and not is_info
                        try:
                            info = _lstat(child, folder_fd, entry)
                        except OSError:
                            if is_info or is_image or entry in previous_folder['images']:
                                report['unreadable'].append(relative)
                            if is_info:
                                report['protected_info'].append(name)
                                folder['info'] = deepcopy(previous_folder['info'])
                            elif entry in previous_folder['images']:
                                folder['images'][entry] = deepcopy(previous_folder['images'][entry])
                            continue
                        if _is_link(info):
                            report['links'].append(relative)
                            if is_info:
                                report['protected_info'].append(name)
                                folder['info'] = deepcopy(previous_folder['info'])
                            elif entry in previous_folder['images']:
                                folder['images'][entry] = deepcopy(previous_folder['images'][entry])
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            report['nested'].append(relative)
                            if is_info:
                                report['unreadable'].append(relative)
                                report['protected_info'].append(name)
                                folder['info'] = deepcopy(previous_folder['info'])
                            elif entry in previous_folder['images']:
                                report['unreadable'].append(relative)
                                folder['images'][entry] = deepcopy(previous_folder['images'][entry])
                            continue
                        if not (is_info or is_image):
                            if stat.S_ISREG(info.st_mode):
                                report['ignored'].append(relative)
                            continue
                        future = pool.submit(_read, child, folder_fd, entry,
                                             text_encoding=encoding if is_info else None, cache=cache)
                        pending.append((future, entry, relative, is_info))
                        if len(pending) >= workers:
                            collect()
                    while pending:
                        collect()
                    folders[name] = folder
            except MissingFileID:
                raise
            except OSError as error:
                raise ScanError(f'{path}: cannot safely list concept folder') from error
    return {'folders': folders, 'report': report}
