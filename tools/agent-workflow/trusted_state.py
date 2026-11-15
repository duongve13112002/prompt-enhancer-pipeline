"""Trusted local state I/O bound to the opened object (T-022 fix round 4, retest 3 R3F1, docs/ci.md).

A `Folder` is a directory below a validated root, opened one component at a time from the root and held open while
it is used, so no component can be swapped for a link between the check and the use:
- Windows: each component is opened with `CreateFileW(FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, FILE_SHARE_READ,
  BACKUP_SEMANTICS | OPEN_REPARSE_POINT)`. While that handle is held, the folder cannot be renamed, removed or opened
  for writing, so it cannot become a junction in place (experiment in the T-022 task file, fix round 4). Each opened
  handle, folder or file, must carry no reparse point, and `GetFinalPathNameByHandleW` must name it as a direct
  child of its held parent, under the root.
- POSIX: each component is opened with `O_DIRECTORY | O_NOFOLLOW` relative to its parent's descriptor (`dir_fd`), and
  files with `O_NOFOLLOW` relative to the held folder.
Reads and writes go through the checked handle only; a write is in place (a held Windows folder refuses
`os.replace` into it). `Folder.owned` makes a fresh folder for an external writer, and `remove_owned` deletes only the
entries inside it and then that same folder, checked by identity, and returns what it could not remove.
Raises `Escape` (a ValueError) for a link or an object outside the root; other failures are OSError.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import git_ledger as gl  # the project's OS file lock (`_lock`, `_unlock`)

WINDOWS = os.name == "nt"


class Escape(ValueError):
    """A path this tool is about to use is not under the root, or a component below the root is a link."""


# --- final path spellings (pure; property-tested on every platform) -----------------------------------------------

UNC_PREFIX, LOCAL_PREFIX = "\\\\?\\UNC\\", "\\\\?\\"  # protocol-bound: GetFinalPathNameByHandleW spellings


def final_parts(text: str) -> tuple[str, ...]:
    """The components of a Windows final path, with the `\\\\?\\` and `\\\\?\\UNC\\` prefixes removed, so the long
    and the short spelling compare equal. Case and short names are kept as they are: both sides of a comparison come
    from the same API on the same volume, so any difference refuses (the conservative side)."""
    t = text.replace("/", "\\")
    if t.startswith(UNC_PREFIX):
        head, t = ("\\\\",), t[len(UNC_PREFIX):]
    elif t.startswith(LOCAL_PREFIX):
        head, t = (), t[len(LOCAL_PREFIX):]
    elif t.startswith("\\\\"):
        head, t = ("\\\\",), t[2:]
    else:
        head = ()
    return head + tuple(p for p in t.split("\\") if p)


def is_child(final: str, parent_final: str) -> bool:
    """True when `final` names a direct child of `parent_final`."""
    child, parent = final_parts(final), final_parts(parent_final)
    return len(child) == len(parent) + 1 and child[:-1] == parent


def is_inside(final: str, root_final: str) -> bool:
    child, root = final_parts(final), final_parts(root_final)
    return len(child) >= len(root) and child[: len(root)] == root


# --- Windows handles --------------------------------------------------------------------------------------------

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    _k32.CreateFileW.restype = wt.HANDLE
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
    _k32.GetFinalPathNameByHandleW.restype = wt.DWORD
    _k32.SetFileInformationByHandle.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
    _k32.SetFileInformationByHandle.restype = wt.BOOL
    _INVALID = wt.HANDLE(-1).value

# Protocol-bound Win32 values (winnt.h, fileapi.h, minwinbase.h).
FILE_READ_DATA = FILE_LIST_DIRECTORY = 0x1
FILE_WRITE_DATA = 0x2
FILE_READ_ATTRIBUTES = 0x80
DELETE = 0x10000
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
SHARE_READ, SHARE_WRITE, SHARE_DELETE = 0x1, 0x2, 0x4
CREATE_NEW, OPEN_EXISTING, OPEN_ALWAYS = 1, 3, 4
BACKUP_SEMANTICS, OPEN_REPARSE_POINT = 0x02000000, 0x00200000
FILE_DISPOSITION_INFO = 4  # FILE_INFO_BY_HANDLE_CLASS FileDispositionInfo
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _win_fd(path: Path, access: int, share: int, disposition: int, flags: int, mode: int) -> int:
    """`CreateFileW`, then a C runtime descriptor that owns the handle (closed by `os.close`)."""
    handle = _k32.CreateFileW(str(path), access, share, None, disposition, flags, None)
    if handle == _INVALID or handle is None:
        code = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(code).strip(), str(path), code)
    try:
        return msvcrt.open_osfhandle(handle, mode)
    except OSError:
        _k32.CloseHandle(handle)
        raise


def _final_path(fd: int) -> str:
    """`GetFinalPathNameByHandleW` of an open descriptor: the object actually opened."""
    buf = ctypes.create_unicode_buffer(32768)
    n = _k32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
    if not 0 < n < len(buf):
        code = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(code).strip(), None, code)
    return buf.value


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


# --- folders and files ------------------------------------------------------------------------------------------

POSIX_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
POSIX_FILE = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
FILE_MODES = {  # mode -> (Windows access, share, disposition, POSIX flags, fdopen mode)
    "r": (GENERIC_READ, SHARE_READ | SHARE_WRITE, OPEN_EXISTING, os.O_RDONLY, "rb"),
    "x": (GENERIC_WRITE, SHARE_READ, CREATE_NEW, os.O_WRONLY | os.O_CREAT | os.O_EXCL, "wb"),
    "u": (GENERIC_READ | GENERIC_WRITE, SHARE_READ | SHARE_WRITE, OPEN_ALWAYS, os.O_RDWR | os.O_CREAT, "r+b"),
}


class Folder:
    """A held directory below `root` (see the module docstring); use as a context manager."""

    def __init__(self, root: Path, path: Path, create: bool = False) -> None:
        self.root = root.resolve()
        try:
            rel = PurePath(path).relative_to(self.root)
        except ValueError:
            raise Escape(f"{path} is not under the project root {self.root}") from None
        self.path = self.root.joinpath(*rel.parts)
        self._fds: list[int] = []
        self.final = ""
        try:
            self._open(self.root, None)
            self.root_final = self.final
            current = self.root
            for part in rel.parts:
                current = current / part
                self._open(current, part, create)
        except BaseException:
            self.close()
            raise

    @classmethod
    def _child(cls, parent: Folder, name: str) -> Folder:
        child = cls.__new__(cls)
        child.root, child.root_final, child.path, child._fds = parent.root, parent.root_final, parent.path / name, []
        child.final = parent.final
        try:
            child._open(child.path, name, parent_fd=parent.fd)
        except BaseException:
            child.close()
            raise
        return child

    def _open(self, path: Path, part: str | None, create: bool = False, parent_fd: int | None = None) -> None:
        """Open and hold one component; `part` None is the root itself."""
        parent = parent_fd if parent_fd is not None else (self._fds[-1] if self._fds else None)
        if WINDOWS:
            if create and part is not None:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(path)  # inside the held parent, which cannot be swapped
            fd = _win_fd(path, FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, SHARE_READ, OPEN_EXISTING,
                         BACKUP_SEMANTICS | OPEN_REPARSE_POINT, os.O_RDONLY)  # fmt: skip
            self._fds.append(fd)
            st = os.fstat(fd)
            if st.st_file_attributes & REPARSE or not stat.S_ISDIR(st.st_mode):
                raise Escape(f"{path} is a link or no folder; this tool follows no link below the project root")
            final = _final_path(fd)
            if part is not None and not (is_child(final, self.final) and is_inside(final, self.root_final)):
                raise Escape(f"{path} was opened as {final}, outside its folder {self.final}")
            self.final = final
            return
        if create and part is not None:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o777, dir_fd=parent)
        try:
            fd = os.open(path if part is None else part, POSIX_DIR, dir_fd=None if part is None else parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Escape(f"{path} is a link or no folder; this tool follows no link below the project root") from None
            raise
        self._fds.append(fd)

    @property
    def fd(self) -> int:
        return self._fds[-1]

    def identity(self) -> tuple[int, int]:
        return _identity(os.fstat(self.fd))

    def close(self) -> None:
        while self._fds:
            os.close(self._fds.pop())

    def __enter__(self) -> Folder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open_file(self, name: str, mode: str) -> BinaryIO:
        """`name` in this folder, opened `r` (existing), `x` (new) or `u` (read and write, created when missing),
        checked on the opened handle: a regular file, no link, a direct child of this folder."""
        if PurePath(name).name != name or name in (".", ".."):
            raise Escape(f"{name!r} is not a plain file name")
        access, share, disposition, flags, fmode = FILE_MODES[mode]
        if WINDOWS:
            fd = _win_fd(self.path / name, access, share, disposition, OPEN_REPARSE_POINT,
                         os.O_RDONLY if mode == "r" else 0)  # fmt: skip
        else:
            try:
                fd = os.open(name, flags | POSIX_FILE, 0o666, dir_fd=self.fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise Escape(f"{self.path / name} is a link; this tool follows no link below the project root") from None
                raise
        try:
            st = os.fstat(fd)
            if getattr(st, "st_file_attributes", 0) & REPARSE or not stat.S_ISREG(st.st_mode):
                raise Escape(f"{self.path / name} is a link or no regular file")
            if WINDOWS:
                final = _final_path(fd)
                if not (is_child(final, self.final) and is_inside(final, self.root_final)):
                    raise Escape(f"{self.path / name} was opened as {final}, outside its folder {self.final}")
            return os.fdopen(fd, fmode)
        except BaseException:
            os.close(fd)
            raise

    def read_bytes(self, name: str) -> bytes | None:
        """The bytes of `name`, or None when it does not exist."""
        try:
            handle = self.open_file(name, "r")
        except FileNotFoundError:
            return None
        with handle:
            return handle.read()

    def write_new(self, name: str, data: bytes) -> None:
        """A new file `name` (refused when it exists), written and synced through its checked handle."""
        with self.open_file(name, "x") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    @contextlib.contextmanager
    def locked(self, name: str, timeout: float) -> Iterator[None]:
        """Hold the project's OS file lock (`git_ledger._lock`, shared with usage_snapshot.locked of T-026) on the
        lock file `name` in this folder, opened through a checked handle."""
        with self.open_file(name, "u") as handle:
            try:
                gl._lock(handle, time.monotonic() + timeout)
            except TimeoutError:
                raise TimeoutError(f"the lock {self.path / name} stayed busy for {timeout:g} s") from None
            try:
                yield
            finally:
                gl._unlock(handle)

    def owned(self, name: str) -> Folder:
        """A new folder `name` in this one (refused when it exists), held, for one writer of this invocation."""
        if PurePath(name).name != name:
            raise Escape(f"{name!r} is not a plain folder name")
        if WINDOWS:
            os.mkdir(self.path / name)
        else:
            os.mkdir(name, 0o700, dir_fd=self.fd)
        return Folder._child(self, name)

    def remove_owned(self, child: Folder) -> list[str]:
        """Delete the entries of `child` (a folder from `owned`), then `child` itself, only if the folder at its name is
        still the same object. Closes `child`. Returns what was left, empty when all is gone."""
        left: list[str] = []
        name = child.path.name
        try:
            identity = child.identity()
            entries = os.listdir(child.path if WINDOWS else child.fd)
        except OSError as exc:
            child.close()
            return [f"{child.path}: cannot list ({exc})"]
        for entry in entries:
            try:
                if WINDOWS:
                    os.unlink(child.path / entry)  # inside the held folder
                else:
                    os.unlink(entry, dir_fd=child.fd)
            except OSError as exc:
                left.append(f"{child.path / entry} ({type(exc).__name__})")
        if WINDOWS:
            child.close()  # the delete handle below needs DELETE access, which the held handle does not share
            left += self._delete_dir(name, identity)
        else:
            try:
                st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if _identity(st) == identity:
                    os.rmdir(name, dir_fd=self.fd)
                if os.fstat(child.fd).st_nlink != 0:
                    left.append(f"{child.path}: the owned folder is still linked (moved or replaced)")
            except OSError as exc:
                left.append(f"{child.path} ({type(exc).__name__})")
            finally:
                child.close()
        return left

    def _delete_dir(self, name: str, identity: tuple[int, int]) -> list[str]:
        """Windows: open `name` for DELETE without following a link, check it is the recorded folder, and mark that
        handle for deletion, so only the object this tool created is removed."""
        path = self.path / name
        try:
            fd = _win_fd(path, DELETE | FILE_READ_ATTRIBUTES, SHARE_READ | SHARE_WRITE | SHARE_DELETE, OPEN_EXISTING,
                         BACKUP_SEMANTICS | OPEN_REPARSE_POINT, os.O_RDONLY)  # fmt: skip
        except OSError as exc:
            return [f"{path} ({type(exc).__name__})"]
        try:
            st = os.fstat(fd)
            if st.st_file_attributes & REPARSE or _identity(st) != identity:
                return [f"{path}: not the folder this tool created (moved or replaced)"]
            flag = ctypes.c_ubyte(1)
            if not _k32.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), FILE_DISPOSITION_INFO,
                                                   ctypes.byref(flag), 1):  # fmt: skip
                return [f"{path}: delete refused (Windows error {ctypes.get_last_error()})"]
            return []
        finally:
            os.close(fd)


def rewrite(handle: BinaryIO, data: bytes) -> None:
    """Replace the whole content of an open `u` handle in place, then sync it."""
    handle.seek(0)
    handle.truncate()
    handle.write(data)
    handle.flush()
    os.fsync(handle.fileno())
