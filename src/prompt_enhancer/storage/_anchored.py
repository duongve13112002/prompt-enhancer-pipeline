"""The anchored file layer of both stores: every create, write, rename, lock-file open and delete below a store root
acts on objects verified inside that root at the moment of the act, never on whatever a name resolves to later
(T-006 race fix round 2, N2; round 3, P1-A, P1-B, P3-A). Store code has no other way to change a file.

Each operation checks the name by the path rule first (`real_inside`), then opens the store root (the resolved,
validated root) and verifies it, then opens, or creates, every component relative to the handle or descriptor of
its verified parent, without following a link, and keeps the chain open until the act ends. A link, junction or
other reparse point anywhere on the way is refused; where no anchored primitive exists the operation is refused,
never done by name.

Windows: `CreateFileW` for the root, `NtCreateFile` relative to the parent handle with `FILE_OPEN_REPARSE_POINT`
for each component, attribute and `GetFinalPathNameByHandleW` checks on every handle, then the act on a handle:
delete-on-handle, rename-on-handle into the verified parent (`FileRenameInfo` with `RootDirectory`; without
replace for `publish_new_bytes`), or a file object over the verified handle. A store root is created by
`make_root`, relative to the project root; nothing here creates a directory by name. Directories on the way are
held without `FILE_SHARE_DELETE`; measured on NTFS, none of them can be renamed meanwhile (each has an open
descendant, which NTFS refuses to move; the share mode is a second guard). The durability of a completed rename
relies on NTFS journaling its metadata (Windows cannot fsync a directory: T-006 probe 10); an assumption, not a
measured guarantee.
POSIX: `os.open(..., O_DIRECTORY | O_NOFOLLOW, dir_fd=parent)` for each directory, then `mkdir`, `open` with
`O_NOFOLLOW`, `rename`, `unlink` or `rmdir` with `dir_fd`, and `fsync` on the parent's descriptor after a rename.
"""

import errno
import hashlib
import os
from pathlib import Path
import stat
import uuid

import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from ..config import _strip_extended_prefix
from ._fs import (HASH_CHUNK, PARQUET_COMPRESSION, PARQUET_VERSION, SUFFIX, FileLock, StorageError, check_segment,
                  real_inside)

if os.name == 'nt':
    import msvcrt


def _checked(root, relative):
    """The path rule on the name, then its segments; the root must already be resolved."""
    real_inside(root, relative)
    return Path(root), [check_segment(part) for part in relative.split('/')]


def remove_entry(root, relative, *, directory=False):
    """Remove the file (or, with `directory`, the empty directory) `relative` below the resolved store root.

    Raises FileNotFoundError when the entry (or a directory on its way) is absent inside a verified directory,
    StorageError on any link, junction, other spelling or type mismatch, and OSError for other operating-system
    failures (a foreign handle, a non-empty directory)."""
    root, parts = _checked(root, relative)
    with _directory(root, parts[:-1], create=False) as parent:
        parent.remove(parts[-1], directory)


def atomic_write_bytes(root, relative, data, check=None):
    """Write `data` to a new temporary file in the verified parent of `relative`, fsync, then rename it over
    `relative` within that parent: a reader sees the old or the new file, never half of one. Missing directories
    are created on the way. `check(size, sha256)` may refuse the content before anything is written."""
    data = bytes(data)
    size, digest = len(data), hashlib.sha256(data).hexdigest()
    if check is not None:
        check(size, digest)
    _write(root, relative, lambda handle: handle.write(data), None)
    return size, digest


def publish_new_bytes(root, relative, data):
    """Publish `data` at `relative` only where no entry exists (FileExistsError otherwise), never replacing one
    (T-012 F2). The fsynced temporary is committed without replace: Windows renames it on its handle with
    ReplaceIfExists = 0; POSIX links it with `linkat`, which fails on an existing name. Where `linkat` fails
    otherwise (no hard links), the name is created exclusively, without following a link, in the same verified
    parent: still no replace, but a reader may see the file while it is written."""
    data = bytes(data)
    return _write(root, relative, lambda handle: handle.write(data), None,
                  commit=lambda parent, handle, temporary, name: _publish(parent, handle, temporary, name, data))


def make_root(project_root, root):
    """Create the store root `root` (and missing parents) relative to the verified, resolved `project_root`, so
    even the root is never created by name (T-006-int, retest 3 P3-B)."""
    project_root, root = Path(project_root), Path(root)
    if root == project_root or not root.is_relative_to(project_root):
        raise StorageError(f'{root}: a store root lies strictly inside the project root {project_root}')
    project_root, parts = _checked(project_root, '/'.join(root.relative_to(project_root).parts))
    with _directory(project_root, parts, create=True):
        pass


def write_table_file(root, relative, table, fmt, check=None):
    """Write one table durably, as `atomic_write_bytes`; return (size_bytes, sha256) of the committed file, read
    back through the temporary file's own handle. `check(size, sha256)` may refuse the bytes before the rename."""
    if fmt not in SUFFIX:
        raise StorageError(f'{root}: {relative}: unknown table format {fmt!r}')

    def produce(handle):
        if fmt == 'parquet':
            pq.write_table(table, handle, version=PARQUET_VERSION, compression=PARQUET_COMPRESSION)
        else:
            with ipc.new_file(handle, table.schema) as writer:
                writer.write_table(table)
    return _write(root, relative, produce, check)


def make_directory(root, relative):
    """Create the new directory `relative` (FileExistsError if it exists), and any missing parent."""
    root, parts = _checked(root, relative)
    with _directory(root, parts[:-1], create=True) as parent:
        parent.mkdir(parts[-1])


def rename_entry(root, source, target, *, directory):
    """Rename `source` to `target`, two names in one store directory, never over an existing entry
    (FileExistsError). The source is opened and verified relative to the verified parent and renamed there."""
    root, source_parts = _checked(root, source)
    _, target_parts = _checked(root, target)
    if source_parts[:-1] != target_parts[:-1]:
        raise StorageError(f'{root}: {source} -> {target}: a rename stays inside one directory')
    with _directory(root, source_parts[:-1], create=False) as parent:
        parent.rename(source_parts[-1], target_parts[-1], directory)


def open_lock_file(root, relative, *, create):
    """A binary read-write file object for the lock file `relative` (created, with missing parents, when
    `create`); FileNotFoundError when it is absent and not created. A sharing violation or a delete-pending file
    raises PermissionError, which `FileLock.try_acquire` reads as "not acquired"."""
    root, parts = _checked(root, relative)
    with _directory(root, parts[:-1], create=create) as parent:
        return parent.open_lock(parts[-1], create)


def lock_file(root, relative, timeout_s, poll_s, *, create=True):
    """The `FileLock` of a store lock file, opened only through `open_lock_file` (looked up when it opens)."""
    root = Path(root)
    return FileLock(root.joinpath(*relative.split('/')), timeout_s, poll_s,
                    opener=lambda: open_lock_file(root, relative, create=create))


def _write(root, relative, produce, check, commit=None):
    commit = commit or _commit
    root, parts = _checked(root, relative)
    name = parts[-1]
    temporary = f'.{name}.{uuid.uuid4().hex}.tmp'  # artifact-bound form: GC removes a leftover `.*.tmp`
    with _directory(root, parts[:-1], create=True) as parent:
        handle = parent.create_temporary(temporary)
        try:
            produce(handle)
            handle.flush()
            os.fsync(handle.fileno())
            size, digest = _measure(handle)
            if check is not None:
                check(size, digest)
            commit(parent, handle, temporary, name)
        except BaseException:
            parent.discard(handle, temporary)
            raise
        handle.close()
    return size, digest


def _measure(handle):
    """Size and SHA-256 of what the temporary file holds, read back through its own handle."""
    handle.seek(0)
    digest, size = hashlib.sha256(), 0
    while chunk := handle.read(HASH_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _commit(parent, handle, temporary, name):
    """The one replace of a store file: the temporary file, renamed within its verified parent (tests pause it)."""
    parent.commit(handle, temporary, name)


def _publish(parent, handle, temporary, name, data):
    """The one no-replace publication: the temporary, committed without replacing `name` (tests pause it)."""
    parent.publish(handle, temporary, name, data)


def _directory(root, parts, *, create):
    # The root is never created here: a store root is made by `make_root` from the project root, and the
    # project root always exists. A missing root is the open's own FileNotFoundError.
    return (_WindowsDirectory if os.name == 'nt' else _PosixDirectory)(root, parts, create)


# ----------------------------------------------------------------------------- POSIX


def _posix_available():
    return (hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
            and all(function in os.supports_dir_fd
                    for function in (os.open, os.stat, os.unlink, os.rmdir, os.mkdir, os.rename, os.link))
            and os.stat in os.supports_follow_symlinks and os.link in os.supports_follow_symlinks)


def _open_directory(name, flags, dir_fd, shown):
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        # ELOOP (EMLINK on FreeBSD): a link where a directory is expected; ENOTDIR: not a directory
        if error.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise StorageError(f'{shown}: is a link or not a directory: refused') from error
        raise


class _PosixDirectory:
    """A verified directory below the root, held open by descriptor with every directory above it."""

    def __init__(self, root, parts, create):
        if not _posix_available():
            raise StorageError(f'{root}: no directory-relative no-follow file operations on this platform: refused')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        self.descriptors = []
        try:
            self.descriptors.append(_open_directory(str(root), flags, None, root))
            for index, part in enumerate(parts):
                shown = root.joinpath(*parts[:index + 1])
                if create:
                    try:
                        os.mkdir(part, 0o777, dir_fd=self.descriptors[-1])  # mkdirat never follows a final link
                    except FileExistsError:
                        pass
                self.descriptors.append(_open_directory(part, flags, self.descriptors[-1], shown))
        except BaseException:
            self.close()
            raise
        self.path, self.fd = root.joinpath(*parts), self.descriptors[-1]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        while self.descriptors:
            os.close(self.descriptors.pop())

    def _lstat(self, name, directory):
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise StorageError(f'{self.path / name}: is a link: refused, not changed')
        if stat.S_ISDIR(info.st_mode) != directory:
            raise StorageError(f'{self.path / name}: expected a {"directory" if directory else "file"}')

    def remove(self, name, directory):
        self._lstat(name, directory)
        # unlink and rmdir never follow a final link, so an entry swapped since the stat is not followed either
        (os.rmdir if directory else os.unlink)(name, dir_fd=self.fd)

    def mkdir(self, name):
        os.mkdir(name, 0o777, dir_fd=self.fd)

    def create_temporary(self, name):
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        return _file_object(os.open(name, flags, 0o666, dir_fd=self.fd))

    def commit(self, handle, temporary, name):
        os.rename(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)  # renameat: never follows `name`
        os.fsync(self.fd)

    def publish(self, handle, temporary, name, data):
        try:  # linkat without AT_SYMLINK_FOLLOW: EEXIST on any existing name, never a replace
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
        except FileExistsError:
            raise
        except OSError:  # no hard links here (T-012's fallback breadth): exclusive no-follow create instead
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
            with _file_object(os.open(name, flags, 0o666, dir_fd=self.fd)) as published:
                published.write(data)
                published.flush()
                os.fsync(published.fileno())
        os.unlink(temporary, dir_fd=self.fd)
        os.fsync(self.fd)

    def discard(self, handle, temporary):
        try:
            handle.close()
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def rename(self, source, target, directory):
        self._lstat(source, directory)
        try:
            os.stat(target, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, 'store entry exists', str(self.path / target))
        # residual: POSIX rename(2) replaces an empty directory created in between; only this writer creates here
        os.rename(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)

    def open_lock(self, name, create):
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | (os.O_CREAT if create else 0)
        try:
            descriptor = os.open(name, flags, 0o666, dir_fd=self.fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                raise StorageError(f'{self.path / name}: is a link: refused') from error
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise StorageError(f'{self.path / name}: expected a file')
        return _file_object(descriptor)


def _file_object(descriptor):
    try:
        return open(descriptor, 'r+b')
    except BaseException:
        os.close(descriptor)
        raise


# ----------------------------------------------------------------------------- Windows

# protocol-bound Windows constants (winnt.h, winbase.h, ntifs.h, ntstatus.h, winerror.h)
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_LIST_DIRECTORY = 0x0001
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
_SHARE_READ = 0x1
_SHARE_READ_WRITE = 0x1 | 0x2  # no FILE_SHARE_DELETE: no other handle may rename or delete a held entry
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FLAG_BACKUP_SEMANTICS = 0x02000000
_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_OPEN, _FILE_CREATE, _FILE_OPEN_IF = 1, 2, 3  # NtCreateFile CreateDisposition
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_ATTRIBUTE_DIRECTORY = 0x10
_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_RENAME_INFORMATION = 10  # FILE_INFORMATION_CLASS FileRenameInformation (NtSetInformationFile)
_FILE_DISPOSITION_INFO = 4
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_DISPOSITION_INFO_EX = 21
_DISPOSITION_DELETE_POSIX = 0x1 | 0x2  # FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
_EX_UNSUPPORTED = (1, 50, 87)  # ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED, ERROR_INVALID_PARAMETER
_STATUS_NOT_FOUND = (0xC0000034, 0xC000003A)  # STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND
_STATUS_COLLISION = 0xC0000035  # STATUS_OBJECT_NAME_COLLISION
_STATUS_WRONG_TYPE = (0xC0000103, 0xC00000BA)  # STATUS_NOT_A_DIRECTORY, STATUS_FILE_IS_A_DIRECTORY
_MAX_PATH_CHARS = 32768  # the longest extended-length path Windows returns
_DIRECTORY_ACCESS = _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE


class _Api:
    """Private ctypes bindings, so declared argument types never leak into the shared `ctypes.windll`."""

    def __init__(self):
        import ctypes

        self.ctypes = ctypes
        handle = ctypes.c_void_p

        class UnicodeString(ctypes.Structure):
            _fields_ = [('Length', ctypes.c_ushort), ('MaximumLength', ctypes.c_ushort),
                        ('Buffer', ctypes.c_wchar_p)]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [('Length', ctypes.c_ulong), ('RootDirectory', handle),
                        ('ObjectName', ctypes.POINTER(UnicodeString)), ('Attributes', ctypes.c_ulong),
                        ('SecurityDescriptor', ctypes.c_void_p), ('SecurityQualityOfService', ctypes.c_void_p)]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_size_t)]

        self.UnicodeString, self.ObjectAttributes, self.IoStatusBlock = UnicodeString, ObjectAttributes, IoStatusBlock
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        ntdll = ctypes.WinDLL('ntdll')
        self.create = kernel.CreateFileW
        self.create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_uint32, handle]
        self.create.restype = handle
        self.query = kernel.GetFileInformationByHandleEx
        self.query.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.query.restype = ctypes.c_int
        self.final = kernel.GetFinalPathNameByHandleW
        self.final.argtypes = [handle, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self.final.restype = ctypes.c_uint32
        self.set_info = kernel.SetFileInformationByHandle
        self.set_info.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.set_info.restype = ctypes.c_int
        self.close = kernel.CloseHandle
        self.close.argtypes = [handle]
        self.close.restype = ctypes.c_int
        self.create_relative = ntdll.NtCreateFile
        self.create_relative.argtypes = [ctypes.POINTER(handle), ctypes.c_uint32, ctypes.POINTER(ObjectAttributes),
                                         ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32]
        self.create_relative.restype = ctypes.c_long
        self.set_information = ntdll.NtSetInformationFile
        self.set_information.argtypes = [handle, ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_int]
        self.set_information.restype = ctypes.c_long
        self.status_to_error = ntdll.RtlNtStatusToDosError
        self.status_to_error.argtypes = [ctypes.c_long]
        self.status_to_error.restype = ctypes.c_uint32
        self.invalid = ctypes.c_void_p(-1).value

    def last_error(self):
        return self.ctypes.WinError(self.ctypes.get_last_error())


_API = None


def _api():
    global _API
    if _API is None:
        _API = _Api()
    return _API


def _open_root(api, root):
    handle = api.create(str(root), _DIRECTORY_ACCESS, _SHARE_READ_WRITE, None, _OPEN_EXISTING,
                        _FLAG_BACKUP_SEMANTICS | _FLAG_OPEN_REPARSE_POINT, None)
    if handle is None or handle == api.invalid:
        raise api.last_error()
    return handle


def _open_child(api, parent, name, shown, *, access, share, disposition, directory):
    """`name` opened (or created) relative to the verified `parent` handle, never following a reparse point."""
    ctypes = api.ctypes
    text = ctypes.create_unicode_buffer(name)
    size = len(name.encode('utf-16-le'))
    object_name = api.UnicodeString(size, size + 2, ctypes.cast(text, ctypes.c_wchar_p))
    attributes = api.ObjectAttributes(ctypes.sizeof(api.ObjectAttributes), parent, ctypes.pointer(object_name),
                                      _OBJ_CASE_INSENSITIVE, None, None)
    status_block = api.IoStatusBlock()
    handle = ctypes.c_void_p()
    kind = _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
    status = api.create_relative(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(status_block),
                                 None, _FILE_ATTRIBUTE_NORMAL, share, disposition,
                                 kind | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT, None, 0)
    status &= 0xFFFFFFFF
    if status in _STATUS_NOT_FOUND:
        raise FileNotFoundError(errno.ENOENT, 'no such store entry', str(shown))
    if status == _STATUS_COLLISION:
        raise FileExistsError(errno.EEXIST, 'store entry exists', str(shown))
    if status in _STATUS_WRONG_TYPE:
        raise StorageError(f'{shown}: expected a {"directory" if directory else "file"}')
    if status:
        raise api.ctypes.WinError(api.status_to_error(status))
    return handle.value


def _verify(api, handle, shown, directory):
    """The object behind the handle is no reparse point, has the expected type and lives at `shown` now."""
    ctypes = api.ctypes
    info = (ctypes.c_uint32 * 2)()  # FILE_ATTRIBUTE_TAG_INFO: FileAttributes, ReparseTag
    if not api.query(handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)):
        raise api.last_error()
    if info[0] & _ATTRIBUTE_REPARSE_POINT:
        raise StorageError(f'{shown}: is a link or junction: refused, not changed')
    if bool(info[0] & _ATTRIBUTE_DIRECTORY) != directory:
        raise StorageError(f'{shown}: expected a {"directory" if directory else "file"}')
    buffer = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
    length = api.final(handle, buffer, _MAX_PATH_CHARS, 0)  # 0: FILE_NAME_NORMALIZED | VOLUME_NAME_DOS
    if not length or length >= _MAX_PATH_CHARS:
        raise api.last_error()
    final = _strip_extended_prefix(buffer.value)
    if final != str(shown):  # exact: another spelling (case, 8.3) is refused like in `real_inside`
        raise StorageError(f'{shown}: is a link or another spelling of a stored name (now {final})')


def _delete_on_handle(api, handle):
    ctypes = api.ctypes
    flags = ctypes.c_uint32(_DISPOSITION_DELETE_POSIX)
    if api.set_info(handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)):
        return
    error = ctypes.get_last_error()
    if error not in _EX_UNSUPPORTED:
        raise ctypes.WinError(error)
    legacy = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile: still on the verified handle, deleted at close
    if not api.set_info(handle, _FILE_DISPOSITION_INFO, ctypes.byref(legacy), ctypes.sizeof(legacy)):
        raise api.last_error()


def _rename_on_handle(api, handle, parent, name, *, replace):
    """Rename the object behind `handle` to `name` inside the verified, held `parent` directory.

    `NtSetInformationFile(FileRenameInformation)` with `RootDirectory`: measured on this host, kernel32's
    `SetFileInformationByHandle(FileRenameInfo)` refuses a non-NULL RootDirectory (ERROR_INVALID_PARAMETER),
    while the NT call accepts it (`.tmp/T-006-race/fix3/probe_rename.log`). Classic semantics, as MoveFileExW."""
    ctypes = api.ctypes
    units = len(name.encode('utf-16-le')) // 2

    class RenameInformation(ctypes.Structure):  # FILE_RENAME_INFORMATION; BOOLEAN ReplaceIfExists, padded
        _fields_ = [('ReplaceIfExists', ctypes.c_uint32), ('RootDirectory', ctypes.c_void_p),
                    ('FileNameLength', ctypes.c_uint32), ('FileName', ctypes.c_wchar * (units + 1))]

    info = RenameInformation(1 if replace else 0, parent, units * 2, name)
    status_block = api.IoStatusBlock()
    status = api.set_information(handle, ctypes.byref(status_block), ctypes.byref(info), ctypes.sizeof(info),
                                 _FILE_RENAME_INFORMATION) & 0xFFFFFFFF
    if status == _STATUS_COLLISION:
        raise FileExistsError(errno.EEXIST, 'store entry exists', name)
    if status:
        raise ctypes.WinError(api.status_to_error(status))


class _WindowsDirectory:
    """A verified directory below the root, held open by handle with every directory above it."""

    def __init__(self, root, parts, create):
        self.api, self.handles = _api(), []
        try:
            self.handles.append(_open_root(self.api, root))
            _verify(self.api, self.handles[-1], root, True)
            for index, part in enumerate(parts):
                shown = root.joinpath(*parts[:index + 1])
                self.handles.append(_open_child(self.api, self.handles[-1], part, shown, access=_DIRECTORY_ACCESS,
                                                share=_SHARE_READ_WRITE, directory=True,
                                                disposition=_FILE_OPEN_IF if create else _FILE_OPEN))
                _verify(self.api, self.handles[-1], shown, True)
        except BaseException:
            self.close()
            raise
        self.path, self.handle = root.joinpath(*parts), self.handles[-1]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        while self.handles:
            self.api.close(self.handles.pop())

    def _child(self, name, *, access, share, disposition, directory, verify=True):
        handle = _open_child(self.api, self.handle, name, self.path / name, access=access, share=share,
                             disposition=disposition, directory=directory)
        try:
            if verify:
                _verify(self.api, handle, self.path / name, directory)
        except BaseException:
            self.api.close(handle)
            raise
        return handle

    def _file_object(self, handle):
        try:
            descriptor = msvcrt.open_osfhandle(handle, 0)  # binary; the descriptor now owns the handle
        except BaseException:
            self.api.close(handle)
            raise
        return _file_object(descriptor)

    def remove(self, name, directory):
        handle = self._child(name, access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE, share=_SHARE_ALL,
                             disposition=_FILE_OPEN, directory=directory)
        try:
            _delete_on_handle(self.api, handle)
        finally:
            self.api.close(handle)

    def mkdir(self, name):
        self.api.close(self._child(name, access=_DIRECTORY_ACCESS, share=_SHARE_READ_WRITE, disposition=_FILE_CREATE,
                                   directory=True, verify=False))

    def create_temporary(self, name):
        # a new file in the verified parent cannot be a reparse point: FILE_CREATE fails on any existing name
        return self._file_object(self._child(name, access=_FILE_GENERIC_READ | _FILE_GENERIC_WRITE | _DELETE,
                                             share=_SHARE_READ, disposition=_FILE_CREATE, directory=False,
                                             verify=False))

    def commit(self, handle, temporary, name):
        _rename_on_handle(self.api, msvcrt.get_osfhandle(handle.fileno()), self.handle, name, replace=True)

    def publish(self, handle, temporary, name, data):
        # ReplaceIfExists = 0: STATUS_OBJECT_NAME_COLLISION (FileExistsError) on any existing name, atomically;
        # every Windows file system renames, so no fallback is needed
        _rename_on_handle(self.api, msvcrt.get_osfhandle(handle.fileno()), self.handle, name, replace=False)

    def discard(self, handle, temporary):
        try:
            _delete_on_handle(self.api, msvcrt.get_osfhandle(handle.fileno()))
        finally:
            handle.close()

    def rename(self, source, target, directory):
        handle = self._child(source, access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE, share=_SHARE_ALL,
                             disposition=_FILE_OPEN, directory=directory)
        try:
            _rename_on_handle(self.api, handle, self.handle, target, replace=False)
        finally:
            self.api.close(handle)

    def open_lock(self, name, create):
        # the share mode of CPython's open(): a held lock file cannot be deleted or renamed by anyone
        return self._file_object(self._child(name, access=_FILE_GENERIC_READ | _FILE_GENERIC_WRITE,
                                             share=_SHARE_READ_WRITE, directory=False,
                                             disposition=_FILE_OPEN_IF if create else _FILE_OPEN))
