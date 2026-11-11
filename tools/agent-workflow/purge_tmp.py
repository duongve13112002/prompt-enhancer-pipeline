"""PM disk cleanup: archive small reports, then delete bulky temporary trees.

Usage:
  python purge_tmp.py archive-remove <worktree_dir>...   archive reports, then delete whole worktree dir
  python purge_tmp.py purge-children <dir> <glob>...      delete matching child dirs of <dir>
  python purge_tmp.py purge-older <dir> <hours>            delete children of <dir> older than <hours>

Never deletes a protected tree: ledgers, evidence, credentials, models and environments (PROTECTED_NAMES at any depth
below a target, links excluded) and any path that is, contains or lies inside the `cwd` of a busy CLI worker; each
refusal is reported. An unreadable worker registry refuses every deletion. Never reads file contents of any path under
a `.secrets` directory: only the entry names of a nested `.secrets` are counted (never printed) to tell the checked-out
placeholder `README.md` from real credentials. Links (symlinks, junctions) are never followed; deleting one removes
only the link.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
ARCHIVE = PROJECT / ".tmp" / "pm-archive"
REPORT_SUFFIXES = {".md", ".json", ".log", ".txt", ".patch"}
MAX_REPORT_BYTES = 2 * 1024 * 1024
# Safety invariant (AGENTS.md section 6): ledgers, evidence, credentials, models and environments are never deleted.
PROTECTED_NAMES = frozenset(
    {"pm-state", "pm-archive", "models", ".venv", "runs", ".secrets", "cli-jobs"}
)
PROTECTED_PAIRS = (("agy-work", "_results"),)  # (parent name, name): Gemini job results
# Every checkout holds `.secrets/README.md` and `models/README.md` (tracked placeholders): such a nested directory
# contains nothing to protect. Only a nested directory qualifies, never a target named on the command line.
PLACEHOLDER_DIRS = frozenset({".secrets", "models"})
PLACEHOLDER_FILES = frozenset({"README.md"})


def _is_link(path: str | os.PathLike[str]) -> bool:
    """Symlink or Windows reparse point (junction); `os.walk` and `Path.is_symlink` miss junctions on Python 3.12."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _walk(top: str | os.PathLike[str]) -> Iterator[tuple[str, list[str], list[str]]]:
    """`os.walk` that never descends into a link (junction included)."""
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(root, d))]
        yield root, dirs, files


def _inside_project(path: Path, allow_tmp_root: bool = False) -> Path:
    resolved = path.resolve()
    if PROJECT not in resolved.parents or resolved == PROJECT:
        raise SystemExit(f"refusing path outside project tmp: {resolved}")
    if allow_tmp_root and resolved == PROJECT / ".tmp":
        return resolved
    if (PROJECT / ".tmp") not in resolved.parents:
        raise SystemExit(f"refusing path outside .tmp: {resolved}")
    return resolved


def _protected_paths(path: Path) -> set[Path]:
    """Return files listed in any candidate manifest of the enclosing worktree or project.

    Frozen candidate manifests may bind small evidence files under `.tmp/`; deleting
    them breaks the freeze, so purge commands skip any tree that contains one.
    """
    root = path
    while root != PROJECT and not (root / ".git").exists():
        root = root.parent
    protected: set[Path] = set()
    for pattern in (".tmp/review/candidate-manifest*.json", ".tmp/*/candidate-manifest*.json"):
        for manifest in root.glob(pattern):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entries = data.get("files", []) if isinstance(data, dict) else []
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    protected.add((root / entry["path"]).resolve())
    return protected


def _holds_protected(candidate: Path, protected: set[Path]) -> bool:
    resolved = candidate.resolve()
    return any(item == resolved or resolved in item.parents for item in protected)


def _on_error(func, path, _exc):  # type: ignore[no-untyped-def]
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as error:
        print(f"  could not remove {path}: {error.__class__.__name__}")


def _size(path: Path) -> int:
    total = 0
    for root, dirs, files in _walk(path):
        dirs[:] = [d for d in dirs if d != ".secrets"]
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def _worker_registry() -> Path:
    """Same location as cli_worker.REGISTRY (`<root>/.tmp/pm-state`), derived from this checkout's root."""
    return PROJECT / ".tmp" / "pm-state" / "cli-workers.json"


def _busy_cwds() -> list[Path]:
    """Working directories of busy CLI workers. A missing registry means no worker was ever started; an unreadable or
    malformed one, or a busy worker without a usable `cwd`, refuses every deletion (fail closed)."""
    registry = _worker_registry()
    if not registry.exists():
        return []
    try:
        data = json.loads(registry.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"refusing: cannot read worker registry {registry}: {error!r}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"refusing: worker registry {registry} is not a JSON object")
    workers = data.get("workers", data)
    if not isinstance(workers, dict):
        raise SystemExit(f"refusing: worker registry {registry} has no worker table")
    busy: list[Path] = []
    for name, entry in workers.items():
        if not isinstance(entry, dict) or entry.get("status") != "busy":
            continue
        cwd = entry.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise SystemExit(f"refusing: busy worker {name!r} has no usable cwd in {registry}")
        busy.append(Path(cwd).resolve())
    return busy


def _busy_conflict(target: Path, busy: list[Path]) -> Path | None:
    """A busy worker cwd that is the target, lies inside it or contains it."""
    resolved = target.resolve()
    for cwd in busy:
        if cwd == resolved or resolved in cwd.parents or cwd in resolved.parents:
            return cwd
    return None


def _is_placeholder(path: str) -> bool:
    """Only the tracked placeholder files, as regular files: nothing to protect (names only, nothing is opened)."""
    try:
        with os.scandir(path) as entries:
            return all(
                e.name in PLACEHOLDER_FILES and e.is_file(follow_symlinks=False) for e in entries
            )
    except OSError:
        return False


def _protected_name(parent: str, name: str) -> bool:
    # Case-folded: NTFS is case-insensitive, so `PM-State` or `.Secrets` is the same directory (fail closed on POSIX).
    folded = name.casefold()
    return (
        folded in PROTECTED_NAMES
        or (os.path.basename(parent).casefold(), folded) in PROTECTED_PAIRS
    )


def _protected_ancestor(target: Path) -> Path | None:
    """The target itself or the first directory between it and the project root that is protected: anything inside a
    protected tree is protected too (purging `.tmp/pm-state` children would delete the ledgers)."""
    for path in (target, *target.parents):
        if path == PROJECT or PROJECT not in path.parents:
            return None
        if _protected_name(str(path.parent), path.name):
            return path
    return None


def _find_protected(target: Path) -> Path | None:
    """The first protected directory that deleting `target` would remove or touch: the target, an ancestor or a
    nested one."""
    ancestor = _protected_ancestor(target)
    if ancestor is not None:
        return ancestor
    for root, dirs, _files in _walk(target):
        for name in dirs:
            if not _protected_name(root, name):
                continue
            nested = os.path.join(root, name)
            if name.casefold() in PLACEHOLDER_DIRS and _is_placeholder(nested):
                continue
            return Path(nested)
    return None


def _refusal(target: Path, busy: list[Path]) -> str | None:
    """Why `target` must not be deleted, or None. A link outside protected trees is never refused: removing it touches
    nothing behind it."""
    ancestor = _protected_ancestor(target)
    if ancestor is not None:
        return f"protected tree {ancestor}"
    if _is_link(target):
        return None
    protected = _find_protected(target)
    if protected is not None:
        return f"protected tree {protected}"
    cwd = _busy_conflict(target, busy)
    if cwd is not None:
        return f"busy worker cwd {cwd}"
    return None


def _remove_link(path: str) -> bool:
    """Remove a link itself (never its target); True only when it is really gone (Windows junctions need rmdir)."""
    if os.name == "nt":
        with contextlib.suppress(OSError):
            os.rmdir(path)
    with contextlib.suppress(OSError):
        os.unlink(path)
    return not os.path.lexists(path)


def _remove(path: str) -> bool:
    """Delete a link, tree or file; True only when nothing is left."""
    if _is_link(path):
        return _remove_link(path)
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_on_error)
    else:
        with contextlib.suppress(OSError):
            os.remove(path)
    return not os.path.lexists(path)


def _archive_reports(worktree: Path) -> int:
    tmp = worktree / ".tmp"
    count = 0
    if not tmp.is_dir() or _is_link(tmp):
        return 0
    target_root = ARCHIVE / worktree.name
    for root, dirs, files in _walk(tmp):
        # Skip credentials, mutant copies and basetemps; reports live at shallow levels.
        dirs[:] = [
            d
            for d in dirs
            if d != ".secrets"
            and "mutant" not in d
            and not d.startswith(("mypy", "pytest", "tests"))
        ]
        for name in files:
            source = Path(root) / name
            if source.suffix.lower() not in REPORT_SUFFIXES or _is_link(source):
                continue
            try:
                if source.stat().st_size > MAX_REPORT_BYTES:
                    continue
            except OSError:
                continue
            destination = target_root / source.relative_to(tmp)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            count += 1
    return count


def _link_location(path: Path) -> Path | None:
    """For a link (junction included) named on the command line: its own location, contained in `.tmp`, so that it is
    removed as a link. `Path.resolve` would follow it and make the command act on the tree behind it."""
    absolute = Path(os.path.abspath(path))
    if not _is_link(absolute):
        return None
    return _inside_project(absolute.parent, allow_tmp_root=True) / absolute.name


def _refuse_protected_parent(parent: Path) -> None:
    ancestor = _protected_ancestor(parent)
    if ancestor is not None:
        raise SystemExit(f"refusing: {parent} is inside protected tree {ancestor}")


def archive_remove(paths: list[str]) -> int:
    """Archive reports, then delete each worktree; returns how many targets were refused."""
    busy = _busy_cwds()
    refused = 0
    for raw in paths:
        worktree = _link_location(Path(raw)) or _inside_project(Path(raw))
        reason = _refusal(worktree, busy)
        if reason:
            print(f"{worktree.name}: refused, {reason}")
            refused += 1
            continue
        is_link = _is_link(worktree)
        before = 0 if is_link else _size(worktree)
        archived = 0 if is_link else _archive_reports(worktree)
        # Same removal path as the purge commands: a junction is removed as a link, never followed.
        if _remove(str(worktree)):
            state = "removed (link only)" if is_link else "removed"
        else:
            state = "left partially"
            refused += 1
        print(f"{worktree.name}: archived {archived} reports, {state}, ~{before / 2**30:.2f} GB")
    return refused


def purge_children(directory: str, patterns: list[str]) -> int:
    """Delete matching children of <directory>; returns how many matches were refused or kept."""
    parent = _inside_project(Path(directory), allow_tmp_root=True)
    _refuse_protected_parent(parent)
    busy = _busy_cwds()
    protected = _protected_paths(parent)
    freed = kept = 0
    for child in sorted(parent.iterdir()):
        if child.name == ".secrets" or not (child.is_dir() or _is_link(child)):
            continue
        if not any(fnmatch.fnmatch(child.name, pattern) for pattern in patterns):
            continue
        if _holds_protected(child, protected):
            print(f"  kept {child.name} (holds a manifest-bound file)")
            kept += 1
            continue
        reason = _refusal(child, busy)
        if reason:
            print(f"  refused {child.name}: {reason}")
            kept += 1
            continue
        is_link = _is_link(child)
        size = 0 if is_link else _size(child)
        if not _remove(str(child)):
            print(f"  kept {child.name} ({'link' if is_link else 'left partially'})")
            kept += 1
            continue
        freed += size
        print(f"  removed {child.name} ({size / 2**30:.2f} GB){' (link only)' if is_link else ''}")
    print(f"{parent}: freed ~{freed / 2**30:.2f} GB")
    return kept


def purge_older(directory: str, hours: float) -> int:
    """Delete every child of <directory> not modified within the last <hours>; returns how many old ones were kept."""
    parent = _inside_project(Path(directory))
    _refuse_protected_parent(parent)
    cutoff = time.time() - hours * 3600
    busy = _busy_cwds()
    protected = _protected_paths(parent)
    removed = kept = refused = 0
    with os.scandir(parent) as entries:
        for entry in entries:
            if entry.name == ".secrets" or _holds_protected(Path(entry.path), protected):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    kept += 1
                    continue
            except OSError:
                continue
            reason = _refusal(Path(entry.path), busy)
            if reason:
                print(f"  refused {entry.name}: {reason}")
                refused += 1
            elif _remove(entry.path):
                removed += 1
            else:
                print(f"  kept {entry.name} (could not remove: link or left partially)")
                refused += 1
    print(
        f"{parent}: removed {removed} entries, kept {kept} recent entries, "
        f"kept {refused} old entries (refused or not removable)"
    )
    return refused


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = __doc__ or ""
    if not args:
        raise SystemExit(usage)
    command, *rest = args
    if command == "archive-remove" and rest:
        return 1 if archive_remove(rest) else 0
    if command == "purge-children" and len(rest) >= 2:
        purge_children(rest[0], rest[1:])
    elif command == "purge-older" and len(rest) == 2:
        try:
            hours = float(rest[1])
        except ValueError:
            raise SystemExit(f"hours must be a number: {rest[1]!r}") from None
        purge_older(rest[0], hours)
    else:
        raise SystemExit(f"unknown command or missing arguments: {command}\n{usage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
