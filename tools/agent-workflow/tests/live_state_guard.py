"""Test guard for the live PM state directory (S42-statedir, decision 72).

The tool tests must never read or write the live `<project>/.tmp/pm-state` (nor an ambient AGENT_WORKFLOW_STATE_DIR
the run was started with): that directory holds the orchestrator's ledgers and a leaked read changes a verdict
(`git_history_changed`). Two layers, with the coverage each really has:

* In-process access guard: a Python audit hook (`sys.addaudithook`, documented in the `sys` module reference) that
  refuses a path-taking file event (open, mkdir, remove, rename, scandir, listdir, rmtree, copy, sqlite3.connect ...)
  whose path, after every link, junction and `..` is resolved (`os.path.realpath`), lies inside a protected tree,
  and records it, so the running test fails at teardown even when the code under test swallows the error. The
  candidate, the protected roots and the exemption share one comparison key (`_key`): resolved, an extended-length
  `\\\\?\\`, `\\\\?\\UNC\\` or device `\\\\.\\` prefix removed on Windows, case normalized, evaluated on each call. A
  path the operating system cannot resolve is refused and recorded, never passed. A loopback share spelling of a
  local drive (`\\\\?\\UNC\\localhost\\D$`) is not collapsed to the drive. It sees only this process and only the
  listed events with a path argument (not a file-descriptor or `dir_fd`-relative access, not native I/O). Children
  are not covered: tests route their environment instead (the autouse `pm_state_dir` fixture sets
  AGENT_WORKFLOW_STATE_DIR, which every child inherits). `os.stat` is not an audited event, so a bare metadata probe
  passes.
* Before/after metadata snapshot of the live directory (names, sizes, mtimes; never file contents): it sees a
  write by any process but not a read, and other sessions may legitimately change the directory, so it is reported
  in the terminal summary and does not fail the run by itself. It lists a link or junction below the root with its
  own metadata and never descends into it (the root itself may be a link: it is the tree being watched). The
  `test-slots` name is matched with the platform's case rule (`os.path.normcase`).

Scope: this protects the tool tests against accidental reads and writes of live PM state in a stable directory
layout. It is not a security boundary against a concurrent actor: a link created or swapped while a check or a
snapshot scan is running is not covered (a path is classified, then used; there is no handle-based enforcement).

The one exemption is `<project>/.tmp/pm-state/test-slots`: the machine-wide test limiter (`test_slot.py`) owns it
while it runs this very session (decision 128, answer 1). It is the real `test-slots` directory under the resolved
root: a path that only reaches it, or leaves it, through a link is judged by its target. Nothing else under the live
directory is exempt.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE_ENV = "AGENT_WORKFLOW_STATE_DIR"  # protocol-bound: shared with every hook
SLOTS_NAME = "test-slots"  # protocol-bound: the limiter's directory, the only exempt subtree

# audit event -> argument positions that hold a path (CPython audit event table, docs.python.org/3/library/audit_events)
PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "open": (0,),
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.rename": (0, 1),
    "os.listdir": (0,),
    "os.scandir": (0,),
    "os.chmod": (0,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "sqlite3.connect": (0,),
}


class LiveStateAccess(PermissionError):
    """A test (or the code it runs) reached into the live PM state directory."""


class Unresolvable(Exception):
    """A path could not be turned into its comparison key; the guard treats that as a refusal, never as a pass."""


# Windows namespace prefixes (protocol-bound, documented: learn.microsoft.com "Naming Files, Paths, and Namespaces").
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"
_DEVICE = "\\\\.\\"


def _plain_spelling(text: str) -> str:
    """The ordinary spelling of a Windows path: `\\\\?\\C:\\x` and `\\\\.\\C:\\x` become `C:\\x`, `\\\\?\\UNC\\s\\h\\x`
    becomes `\\\\s\\h\\x`. Pure text, no I/O. Other spellings pass through unchanged."""
    if text[: len(_EXTENDED_UNC)].upper() == _EXTENDED_UNC.upper():
        return "\\\\" + text[len(_EXTENDED_UNC) :]
    if text.startswith((_EXTENDED, _DEVICE)):
        return text[len(_EXTENDED) :]
    return text


def _key(path: str | Path) -> str:
    """The one comparison form, used for candidates, protected roots and the exemption alike: links, junctions and
    `..` resolved (a missing tail is kept, `realpath` is non-strict), an extended-length or device prefix removed on
    Windows, case normalized. Raises `Unresolvable` when the operating system refuses to resolve the path: a guess at
    the spelling would be a different answer than the target's.

    On Windows a path without the exact `\\\\?\\` prefix first goes through `os.path.abspath`, which calls
    `GetFullPathNameW`: the normalization the file APIs apply (separators, `.` and `..`, trailing periods and spaces,
    the current directory). `realpath` alone keeps the not yet existing tail verbatim, so `pm-state.` and `pm-state `
    would stay apart from `pm-state` although `mkdir` creates the same directory for all three. A `\\\\?\\` path skips
    the `abspath` step by design; `realpath` still normalizes and resolves it as before, and its prefix is removed.
    Both steps start with the lexical `normpath`, which drops a trailing separator, so a distinct `\\\\?\\` name with a
    trailing space can share the key of the protected name: a conservative false refusal, accepted."""
    try:
        text = os.fsdecode(path)
        if os.name == "nt" and not text.startswith(_EXTENDED):
            text = os.path.abspath(text)
        resolved = os.path.realpath(text)
    except (OSError, ValueError) as err:
        raise Unresolvable(f"{path!s}: {err}") from err
    if os.name == "nt":
        resolved = _plain_spelling(resolved)
    return os.path.normcase(resolved)


def _under(key: str, base: str) -> bool:
    return key == base or key.startswith(base + os.sep)


def live_roots(project: Path = ROOT, ambient: str | None = None) -> tuple[Path, ...]:
    """The protected trees: the project's default state directory and, when set, the ambient override."""
    roots = [project / ".tmp" / "pm-state"]
    if ambient:
        roots.append(Path(ambient))
    return tuple(roots)


_ROOTS: tuple[Path, ...] = live_roots(ROOT, os.environ.get(STATE_ENV))
_violations: list[str] = []
_lock = threading.Lock()
_paused = 0
_installed = False


def protected_keys(roots: tuple[Path, ...] | None = None) -> tuple[str, ...]:
    """Canonical keys of the protected roots, resolved now (a root may be, or become, a link)."""
    return tuple(dict.fromkeys(_key(p) for p in (_ROOTS if roots is None else roots)))


def exempt_keys(roots: tuple[Path, ...] | None = None) -> tuple[str, ...]:
    """The limiter's `test-slots` directory under the resolved first root. It is not resolved again, so a link named
    `test-slots` inside the root cannot widen the exemption to its target."""
    first = (_ROOTS if roots is None else roots)[:1]
    return tuple(os.path.join(_key(p), os.path.normcase(SLOTS_NAME)) for p in first)


def is_protected(path: str | Path, keys: tuple[str, ...] | None = None, exempt: tuple[str, ...] | None = None) -> bool:
    """True when the resolved `path` is inside a protected tree and outside the limiter's `test-slots` subtree."""
    key = _key(path)
    keys = protected_keys() if keys is None else keys
    exempt = exempt_keys() if exempt is None else exempt
    return any(_under(key, base) for base in keys) and not any(_under(key, base) for base in exempt)


def violations() -> list[str]:
    return list(_violations)


def _hook(event: str, args: tuple) -> None:
    positions = PATH_EVENTS.get(event)
    if positions is None or _paused:
        return
    for pos in positions:
        if pos >= len(args):
            continue
        raw = args[pos]
        if isinstance(raw, int) or raw is None:
            continue
        try:
            text = os.fsdecode(raw)
        except (TypeError, ValueError):
            continue
        try:
            hit = bool(text) and is_protected(text)
            reason = ""
        except Unresolvable as err:  # fail closed: an access that cannot be classified is refused and recorded
            hit, reason = True, f" (cannot be classified: {err})"
        if hit:
            message = f"{event} {text}{reason}"
            with _lock:
                _violations.append(message)
            raise LiveStateAccess(
                f"test touched the live PM state directory: {message} (only .tmp/pm-state/{SLOTS_NAME} is exempt, "
                "for the test limiter); use the per-test state directory from the pm_state_dir fixture"
            )


def install() -> None:
    """Install the audit hook once per process (an audit hook cannot be removed; `pause` mutes it)."""
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


class pause:  # noqa: N801 - used as a context manager
    """Mute the access guard while the guard's own code lists the live directory (metadata only)."""

    def __enter__(self) -> None:
        global _paused
        _paused += 1

    def __exit__(self, *exc: object) -> None:
        global _paused
        _paused -= 1


def _is_reparse_point(st: os.stat_result) -> bool:
    """A Windows junction, directory symlink or other reparse point. `DirEntry.is_dir(follow_symlinks=False)` is
    true for a junction there, so the attribute bit of the lstat result decides (a POSIX symlink is tested by mode)."""
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st.st_mode)


def snapshot(base: Path | None = None) -> dict[str, tuple[int, int, int]]:
    """`relative path -> (mode, size, mtime_ns)` for the live state tree, `test-slots` excluded. Uses scandir and
    lstat only: no file is opened, no content read, no link or junction below `base` descended into (its own
    metadata is kept)."""
    base = _ROOTS[0] if base is None else base
    found: dict[str, tuple[int, int, int]] = {}
    stack = [base]
    with pause():
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = list(listing)
            except OSError:
                continue
            for entry in entries:
                rel = Path(entry.path).relative_to(base).as_posix()
                if folder == base and os.path.normcase(entry.name) == os.path.normcase(SLOTS_NAME):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                found[rel] = (st.st_mode, st.st_size, st.st_mtime_ns)
                if entry.is_dir(follow_symlinks=False) and not _is_reparse_point(st):
                    stack.append(Path(entry.path))
    return found


def snapshot_all() -> dict[str, dict[str, tuple[int, int, int]]]:
    """One metadata snapshot per protected root (the project default and an ambient override)."""
    return {str(root): snapshot(root) for root in _ROOTS}


def changes(before: dict[str, tuple[int, int, int]], after: dict[str, tuple[int, int, int]]) -> list[str]:
    """Sorted `added|removed|changed <relative path>` lines between two snapshots."""
    out = [f"added {k}" for k in after if k not in before]
    out += [f"removed {k}" for k in before if k not in after]
    out += [f"changed {k}" for k in before if k in after and before[k] != after[k]]
    return sorted(out)
