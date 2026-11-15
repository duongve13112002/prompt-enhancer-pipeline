"""PM disk cleanup: archive small reports, then delete bulky temporary trees.

Usage:
  python purge_tmp.py archive-remove <worktree_dir>...   archive reports, then delete whole worktree dir
  python purge_tmp.py purge-children <dir> <glob>...      delete matching child dirs of <dir>
  python purge_tmp.py purge-older <dir> <hours>            delete children of <dir> older than <hours>
  python purge_tmp.py sweep                                delete unowned children of `.tmp` older than the configured age
  python purge_tmp.py prune-archive                        delete children of `.tmp/pm-archive` past the configured retention

Every command accepts `--dry-run`: it prints what would be deleted and deletes nothing. `sweep` and `prune-archive` read
`cleanup_config.json` (a missing or invalid key is refused). `sweep` keeps what a busy CLI worker (`cli-workers.json`),
a task in `## IN-FLIGHT` of `.tmp/pm-state/pm-continuation.md` or an unfinished job of `.tmp/pm-state/agents.json` owns;
an unreadable ownership source refuses every deletion (fail closed). `sweep` never acts on `.tmp/pm-archive/`, which only
`prune-archive` trims.

Never deletes a protected tree: ledgers, evidence, credentials, models and environments (PROTECTED_NAMES at any depth
below a target, links excluded) and any path that is, contains or lies inside the `cwd` of a busy CLI worker; each
refusal is reported. One waiver: `archive-remove` of a worker worktree (a direct child of `.tmp/worktrees` whose `.git`
is a file) may delete that worktree's own `.tmp/pm-state` (test-slot counters, guard log and caches written by tools run
inside it), after archiving its reports; every other protected name inside it still refuses. An unreadable worker registry refuses every deletion. Never reads file contents of any path under
a `.secrets` directory: only the entry names of a nested `.secrets` are counted (never printed) to tell the checked-out
placeholder `README.md` from real credentials. Links (symlinks, junctions) are never followed; deleting one removes
only the link.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import math
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

PROJECT = Path(__file__).resolve().parents[2]
ARCHIVE = PROJECT / ".tmp" / "pm-archive"
REPORT_SUFFIXES = {".md", ".json", ".log", ".txt", ".patch"}
MAX_REPORT_BYTES = 2 * 1024 * 1024
# Safety invariant (AGENTS.md section 6): ledgers, evidence, credentials, models and environments are never deleted.
PROTECTED_NAMES = frozenset(
    {"pm-state", "pm-archive", "models", ".venv", "runs", ".secrets", "cli-jobs"}
)
# (parent name, name): Gemini job results; periodic audit reports (S22), kept outside the state directory since T-024;
# the AgyRunner job folder, whose ACL only scripts/agy-runner/setup.ps1 (run by the user as administrator) can recreate;
# hash-pinned tool binaries the user approved downloading (gitleaks, decision 113), which a sweep must never age out.
PROTECTED_PAIRS = (("agy-work", "_results"), (".tmp", "audit"), (".tmp", "agy-jobs"), (".tmp", "tools"))
# Every checkout holds `.secrets/README.md` and `models/README.md` (tracked placeholders): such a nested directory
# contains nothing to protect. Only a nested directory qualifies, never a target named on the command line.
PLACEHOLDER_DIRS = frozenset({".secrets", "models"})
PLACEHOLDER_FILES = frozenset({"README.md"})
CLEANUP_CONFIG = Path(__file__).resolve().parent / "cleanup_config.json"
# Artifact-bound: `.tmp` directories that only hold other trees. `sweep` judges their children, never the container.
CONTAINERS = frozenset({"worktrees", "agy-work"})
# Containers that carry an explicit runner ACL from scripts/agy-runner/setup.ps1 (the runner's only write grant), so
# the container itself, or anything holding it, is never deleted; its children (Gemini workspaces) stay purgeable.
KEPT_CONTAINERS = (Path(".tmp") / "agy-work",)
INFLIGHT_HEADING = "## IN-FLIGHT"
CLEANUP_KEYS = ("sweep_age_hours", "archive_retention_hours", "scan_timeout_seconds")


class CleanupConfig(NamedTuple):
    sweep_age_hours: float
    archive_retention_hours: float
    scan_timeout_seconds: float


class CleanupConfigError(ValueError):
    """`cleanup_config.json` is missing, unreadable or has a missing or invalid key."""


def load_cleanup_config(path: Path | None = None) -> CleanupConfig:
    """Validated settings; every key is required and a positive finite number (no source default, AGENTS.md section 5)."""
    path = CLEANUP_CONFIG if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise CleanupConfigError(f"cannot read {path}: {error!r}") from None
    if not isinstance(data, dict):
        raise CleanupConfigError(f"{path} must hold a JSON object")
    values: dict[str, float] = {}
    for key in CLEANUP_KEYS:
        if key not in data:
            raise CleanupConfigError(f"{path}: missing key {key}")
        value = data[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise CleanupConfigError(
                f"{path}: {key} must be a positive finite number, got {value!r}"
            )
        values[key] = float(value)
    unknown = sorted(set(data) - set(CLEANUP_KEYS))
    if unknown:
        raise CleanupConfigError(f"{path}: unknown key(s) {', '.join(unknown)}")
    return CleanupConfig(**values)


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
    """The path exactly as given (made absolute, never resolved: `Path.resolve` would hide a linked ancestor from the
    gate), inside `<project>/.tmp`. A `..` component is refused: collapsing it lexically differs from what the OS does
    behind a link."""
    if ".." in path.parts:
        raise SystemExit(f"refusing path with a parent reference: {path}")
    absolute = Path(os.path.abspath(path))
    if PROJECT not in absolute.parents or absolute == PROJECT:
        raise SystemExit(f"refusing path outside project tmp: {absolute}")
    if allow_tmp_root and absolute == PROJECT / ".tmp":
        return absolute
    if (PROJECT / ".tmp") not in absolute.parents:
        raise SystemExit(f"refusing path outside .tmp: {absolute}")
    return absolute


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


def _manifest_roots(target: Path) -> list[Path]:
    """Every root whose candidate manifests can bind a file under `target`: the project root, each ancestor worktree
    (a directory with `.git`) between the project and the target, the target itself when it is a worktree, and every
    worktree below it when it is a directory that holds worktrees (links are never entered)."""
    roots = [PROJECT]
    for ancestor in reversed(target.parents):
        if PROJECT in ancestor.parents and (ancestor / ".git").exists():
            roots.append(ancestor)
    if _is_link(target) or not target.is_dir():
        return roots
    for root, dirs, files in _walk(target):
        if ".git" in dirs or ".git" in files:
            roots.append(Path(root))
        dirs[:] = [d for d in dirs if d != ".git"]
    return roots


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


def _size_bounded(path: Path, deadline: float) -> tuple[int, bool]:
    """(bytes, complete): like `_size` but stops at the monotonic `deadline`; links are never followed."""
    total = 0
    for root, dirs, files in _walk(path):
        if time.monotonic() >= deadline:
            return total, False
        dirs[:] = [d for d in dirs if d != ".secrets"]
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total, True


def _state_dir() -> Path:
    """The PM state directory by the hooks' rule (cli_worker.state_root, hooks/git_ledger.ledger_dir): the
    AGENT_WORKFLOW_STATE_DIR override, else `<project>/.tmp/pm-state`, read at call time."""
    return Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR") or PROJECT / ".tmp" / "pm-state")


def _worker_registry() -> Path:
    """Same location as cli_worker.REGISTRY, so ownership follows a state-directory override."""
    return _state_dir() / "cli-workers.json"


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


def _protected_ancestor(target: Path, exempt: Path | None = None) -> Path | None:
    """The target itself or the first directory between it and the project root that is protected: anything inside a
    protected tree is protected too (purging `.tmp/pm-state` children would delete the ledgers). `exempt` is the one
    directory whose protected name is waived as a parent (`prune-archive` acts on children of `.tmp/pm-archive`)."""
    for path in (target, *target.parents):
        if path == PROJECT or PROJECT not in path.parents:
            return None
        if exempt is not None and path == exempt:
            return None
        if _protected_name(str(path.parent), path.name):
            return path
    return None


def _kept_container(target: Path) -> Path | None:
    """The kept container that deleting `target` would remove: the target itself or one inside it. Case-folded like
    `_protected_name`, so `.TMP/Agy-Work` is the same directory on NTFS (fail closed on POSIX)."""
    folded = str(target).casefold()
    for relative in KEPT_CONTAINERS:
        container = PROJECT / relative
        if folded in {str(p).casefold() for p in (container, *container.parents)}:
            return container
    return None


def _worker_state_dir(target: Path) -> Path | None:
    """`<target>/.tmp/pm-state` when `target` is a worker worktree (a direct child of `.tmp/worktrees` whose `.git` is a
    file): the worktree's own test-slot counters, guard log and caches, written by tools run inside it, not the
    project's ledgers (those live in `<project>/.tmp/pm-state`). None for any other target."""
    if target.parent != PROJECT / ".tmp" / "worktrees" or not (target / ".git").is_file():
        return None
    return target / ".tmp" / "pm-state"


def _find_protected(
    target: Path, exempt: Path | None = None, worker_state: bool = False
) -> Path | None:
    """The first protected directory that deleting `target` would remove or touch: the target, an ancestor or a
    nested one. `worker_state` (only `archive-remove`, which archives the reports first) waives the protected name
    of a worker worktree's own `.tmp/pm-state`; every other protected name inside it still refuses."""
    ancestor = _protected_ancestor(target, exempt)
    if ancestor is not None:
        return ancestor
    kept = _kept_container(target)
    if kept is not None:
        return kept
    waived = _worker_state_dir(target) if worker_state else None
    for root, dirs, _files in _walk(target):
        for name in dirs:
            if not _protected_name(root, name):
                continue
            nested = os.path.join(root, name)
            if name.casefold() in PLACEHOLDER_DIRS and _is_placeholder(nested):
                continue
            if waived is not None and os.path.normcase(nested) == os.path.normcase(str(waived)):
                if not _is_link(nested):
                    continue
            return Path(nested)
    return None


def _path_problem(target: Path) -> str | None:
    """Containment: the path, exactly as given, lies inside `<project>/.tmp` with no `..` component and no ancestor
    between the project root and the target that is a link or junction (the target itself may be one: it is removed as
    a link). Only after that is the parent resolved, as a last containment check. No race is needed to cross a link."""
    if ".." in Path(target).parts:
        return "parent reference in the path"
    tmp = PROJECT / ".tmp"
    path = Path(os.path.abspath(target))
    if tmp not in path.parents:
        return f"outside {tmp}"
    for ancestor in path.parents:
        if ancestor == PROJECT:
            break
        if _is_link(ancestor):
            return f"link on the path: {ancestor}"
    if tmp.resolve() not in path.parent.resolve().parents and path.parent.resolve() != tmp.resolve():
        return f"resolves outside {tmp}"
    return None


def _authorize(
    target: Path,
    own: Ownership,
    *,
    check_inflight: bool,
    exempt: Path | None,
    manifests: set[Path],
    deep: bool,
    worker_state: bool = False,
) -> tuple[str, str] | None:
    """(status, reason) when `target` must not be deleted, else None. Private: `Gate` supplies every input.

    Checks in order: containment and no link on any ancestor; protected names (target, ancestors, and with `deep` the
    trees nested inside); ownership (unreadable or malformed state fails closed; a name in `## IN-FLIGHT` when
    `check_inflight`; an unfinished Antigravity job; a busy worker cwd); manifest-bound files. Status is `refused`,
    `unknown` (ownership unreadable) or `owned`. A link target is judged by name only: removing it touches nothing behind
    it. `exempt` waives the protected name of that one parent directory."""
    problem = _path_problem(target)
    if problem:
        return "refused", problem
    target = Path(os.path.abspath(target))
    ancestor = _protected_ancestor(target, exempt)
    if ancestor is not None:
        return "refused", f"protected tree {ancestor}"
    if own.problem:
        return "unknown", own.problem
    is_link = _is_link(target)
    if not is_link and deep:
        nested = _find_protected(target, exempt, worker_state)
        if nested is not None:
            return "refused", f"protected tree {nested}"
    owner = _owner(target, own, check_inflight, is_link)
    if owner:
        return "owned", owner
    if deep and not is_link and _holds_protected(target, manifests):
        return "refused", "holds a manifest-bound file"
    return None


class Gate:
    """The one authorization every selecting or deleting path goes through (S31).

    A caller states only its policy (`check_inflight`, `exempt`, `deep`, `strict`, `deadline`). The gate loads every
    input itself (ownership of workers, tasks in flight and Antigravity jobs; candidate manifests) and judges the path
    exactly as given, so a caller can neither hand it a pre-resolved path nor withhold an input. `remove` re-checks and
    then deletes, so no command deletes anything it did not just authorize. `strict` makes unreadable ownership refuse
    the whole command (SystemExit) instead of a per-path verdict."""

    def __init__(
        self,
        *,
        check_inflight: bool,
        exempt: Path | None = None,
        deep: bool = True,
        strict: bool = False,
        deadline: float | None = None,
        worker_state: bool = False,
    ) -> None:
        self.check_inflight = check_inflight
        self.exempt = exempt
        self.deep = deep
        self.worker_state = worker_state
        self.own = load_ownership(deadline)
        if strict and self.own.problem:
            problem = self.own.problem
            raise SystemExit(problem if problem.startswith("refusing") else f"refusing: {problem}")
        self._manifests: dict[Path, set[Path]] = {}

    def _manifests_for(self, target: Path) -> set[Path]:
        """Files bound by the manifests of every root that can bind `target` (see `_manifest_roots`)."""
        if not self.deep:
            return set()
        found: set[Path] = set()
        for root in _manifest_roots(target):
            if root not in self._manifests:
                self._manifests[root] = _protected_paths(root)
            found |= self._manifests[root]
        return found

    def check(self, target: Path) -> tuple[str, str] | None:
        problem = _path_problem(target)
        if problem:
            return "refused", problem
        absolute = Path(os.path.abspath(target))
        return _authorize(
            absolute,
            self.own,
            check_inflight=self.check_inflight,
            exempt=self.exempt,
            manifests=self._manifests_for(absolute),
            deep=self.deep,
            worker_state=self.worker_state,
        )

    def remove(self, target: Path) -> bool:
        """Authorize again right before the delete, then delete (a link is removed as a link). True only when gone."""
        if self.check(target) is not None:
            return False
        return _remove(os.path.abspath(target))


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


def _refuse_protected_parent(parent: Path) -> None:
    ancestor = _protected_ancestor(parent)
    if ancestor is not None:
        raise SystemExit(f"refusing: {parent} is inside protected tree {ancestor}")


def _refuse_linked_directory(parent: Path) -> None:
    """A directory argument reached through a link is refused before it is enumerated."""
    problem = _path_problem(parent / "_")
    if problem:
        raise SystemExit(f"refusing: {parent}: {problem}")


def archive_remove(paths: list[str], dry_run: bool = False) -> int:
    """Archive reports, then delete each worktree; returns how many targets were refused. A dry run changes nothing."""
    # A worker worktree's own `.tmp/pm-state` may go: `_archive_reports` copies its reports before the delete.
    gate = Gate(check_inflight=False, strict=True, worker_state=True)
    refused = 0
    for raw in paths:
        worktree = _inside_project(Path(raw))
        verdict = gate.check(worktree)
        if verdict:
            print(f"{worktree.name}: refused, {verdict[1]}")
            refused += 1
            continue
        is_link = _is_link(worktree)
        before = 0 if is_link else _size(worktree)
        if dry_run:
            print(
                f"{worktree.name}: would archive reports and remove, ~{before / 2**30:.2f} GB (dry run)"
            )
            continue
        archived = 0 if is_link else _archive_reports(worktree)
        # Same removal path as the purge commands: a junction is removed as a link, never followed.
        if gate.remove(worktree):
            state = "removed (link only)" if is_link else "removed"
        else:
            state = "left partially"
            refused += 1
        print(f"{worktree.name}: archived {archived} reports, {state}, ~{before / 2**30:.2f} GB")
    return refused


def purge_children(directory: str, patterns: list[str], dry_run: bool = False) -> int:
    """Delete matching children of <directory>; returns how many matches were refused or kept."""
    parent = _inside_project(Path(directory), allow_tmp_root=True)
    _refuse_protected_parent(parent)
    _refuse_linked_directory(parent)
    gate = Gate(check_inflight=False, strict=True)
    freed = kept = 0
    for child in sorted(parent.iterdir()):
        if child.name == ".secrets" or not (child.is_dir() or _is_link(child)):
            continue
        if not any(fnmatch.fnmatch(child.name, pattern) for pattern in patterns):
            continue
        verdict = gate.check(child)
        if verdict:
            print(f"  refused {child.name}: {verdict[1]}")
            kept += 1
            continue
        is_link = _is_link(child)
        size = 0 if is_link else _size(child)
        if dry_run:
            print(f"  would remove {child.name} ({size / 2**30:.2f} GB) (dry run)")
            continue
        if not gate.remove(child):
            print(f"  kept {child.name} ({'link' if is_link else 'left partially'})")
            kept += 1
            continue
        freed += size
        print(f"  removed {child.name} ({size / 2**30:.2f} GB){' (link only)' if is_link else ''}")
    print(f"{parent}: freed ~{freed / 2**30:.2f} GB")
    return kept


def purge_older(directory: str, hours: float, dry_run: bool = False) -> int:
    """Delete every child of <directory> not modified within the last <hours>; returns how many old ones were kept."""
    parent = _inside_project(Path(directory))
    _refuse_protected_parent(parent)
    _refuse_linked_directory(parent)
    cutoff = time.time() - hours * 3600
    gate = Gate(check_inflight=False, strict=True)
    removed = kept = refused = 0
    with os.scandir(parent) as entries:
        for entry in entries:
            if entry.name == ".secrets":
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    kept += 1
                    continue
            except OSError:
                continue
            verdict = gate.check(Path(entry.path))
            if verdict:
                print(f"  refused {entry.name}: {verdict[1]}")
                refused += 1
            elif dry_run:
                print(f"  would remove {entry.name} (dry run)")
            elif gate.remove(Path(entry.path)):
                removed += 1
            else:
                print(f"  kept {entry.name} (could not remove: link or left partially)")
                refused += 1
    print(
        f"{parent}: removed {removed} entries, kept {kept} recent entries, "
        f"kept {refused} old entries (refused or not removable)"
    )
    return refused


class Ownership:
    """Who owns what under `.tmp`; `problem` is set when a source is unreadable (fail closed)."""

    def __init__(self) -> None:
        self.busy: list[Path] = []
        self.inflight = ""
        self.agy_active = False
        self.problem: str | None = None
        self.timed_out = False


def _read_inflight(note: Path) -> str:
    """Text of the `## IN-FLIGHT` section; an absent note owns nothing (as hooks/precompact_snapshot.py)."""
    if not note.exists():
        return ""
    text = note.read_text(encoding="utf-8-sig", errors="replace")
    body: list[str] = []
    inside = False
    for line in text.split("\n"):
        if line.startswith("## "):
            inside = line.startswith(INFLIGHT_HEADING)
        elif inside:
            body.append(line)
    return "\n".join(body)


AGENT_KINDS = ("gemini", "codex", "claude")  # protocol-bound: the kinds pm-supervisor.ps1 registers


def _read_agent_jobs(registry: Path, own: Ownership) -> None:
    """Strictly validated `agents.json` (an array of `{kind, id, ...}`). An Antigravity (`gemini`) job is finished only
    when its exact marker `_results/<id>.done` exists (scripts/agy-runner/agy-job.ps1 writes it last); an unfinished one
    owns `.tmp/agy-work` whole. Other jobs own their `cwd` (required for `codex`). A record that is not an object, has an
    unknown kind, an unusable id or a bad `cwd` raises ValueError: the caller then refuses every deletion."""
    if not registry.exists():
        return
    jobs = json.loads(registry.read_text(encoding="utf-8-sig"))
    if not isinstance(jobs, list):
        raise ValueError("not a JSON array of job objects")
    results = PROJECT / ".tmp" / "agy-work" / "_results"
    for number, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"record {number} is not an object")
        kind, ident, cwd = job.get("kind"), job.get("id"), job.get("cwd")
        if kind not in AGENT_KINDS:
            raise ValueError(f"record {number} has an unknown or missing kind")
        if (
            not isinstance(ident, str)
            or not ident
            or ident in (".", "..")
            or any(c in ident for c in "/\\")
        ):
            raise ValueError(f"record {number} has no usable id")
        if cwd is not None and (not isinstance(cwd, str) or not cwd):
            raise ValueError(f"record {number} has an unusable cwd")
        if kind == "codex" and cwd is None:
            raise ValueError(f"record {number} (codex) has no cwd")
        if kind == "gemini":
            if not (results / f"{ident}.done").is_file():
                own.agy_active = True
        elif cwd is not None:
            own.busy.append(Path(cwd).resolve())


def load_ownership(deadline: float | None = None) -> Ownership:
    """Read the three ownership sources; `timed_out` is set when the monotonic `deadline` passes between sources."""
    own = Ownership()
    state = _state_dir()

    def late() -> bool:
        own.timed_out = deadline is not None and time.monotonic() >= deadline
        return own.timed_out

    try:
        own.busy = _busy_cwds()
    except SystemExit as error:
        own.problem = str(error.code)
        return own
    if late():
        return own
    try:
        own.inflight = _read_inflight(state / "pm-continuation.md")
        if late():
            return own
        _read_agent_jobs(state / "agents.json", own)
    except (OSError, ValueError) as error:
        own.problem = f"cannot read ownership state in {state}: {error!r}"
    late()
    return own


def _mentions(text: str, name: str) -> bool:
    """`name` appears in `text` as a whole path component or token (a structural match, no natural-language rule)."""
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", text, re.IGNORECASE) is not None


def _owner(path: Path, own: Ownership, check_inflight: bool, is_link: bool) -> str | None:
    if check_inflight and _mentions(own.inflight, path.name):
        return "named in ## IN-FLIGHT"
    if own.agy_active and any(p.name.casefold() == "agy-work" for p in (path, *path.parents)):
        return "unfinished Antigravity job"
    if not is_link:  # `_busy_conflict` resolves the path; a link is removed itself and nothing behind it is touched
        cwd = _busy_conflict(path, own.busy)
        if cwd is not None:
            return f"busy worker cwd {cwd}"
    return None


def _sweep_children(tmp: Path) -> list[Path]:
    """Direct children of `.tmp`, with the children of a container directory in place of the container."""
    found: list[Path] = []
    with os.scandir(tmp) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            path = Path(entry.path)
            if entry.name.casefold() in CONTAINERS and not _is_link(path) and path.is_dir():
                found.extend(sorted(path.iterdir()))
            else:
                found.append(path)
    return found


class SweepItem(NamedTuple):
    path: Path
    status: str  # orphan | recent | owned | protected | unknown | refused
    detail: str = ""


def plan_sweep_bounded(
    age_hours: float,
    *,
    refusals: bool = True,
    now: float | None = None,
    deadline: float | None = None,
    gate: Gate | None = None,
) -> tuple[list[SweepItem], bool]:
    """(items, complete). Classify the children of `.tmp` through the `Gate`; `orphan` = unowned and older than
    `age_hours`. With `refusals` off the nested protected-tree walk and the manifest scan are skipped (cheap read-only
    count for the SessionStart line). `complete` is False when the monotonic `deadline` passed before the end."""
    tmp = PROJECT / ".tmp"
    if not tmp.is_dir():
        return [], True
    cutoff = (time.time() if now is None else now) - age_hours * 3600
    gate = gate or Gate(check_inflight=True, deep=refusals, deadline=deadline)
    if gate.own.timed_out:
        return [], False
    problem = _path_problem(tmp / "x")
    if problem:  # `.tmp` itself reached through a link: nothing under it is selected
        return [SweepItem(tmp, "refused", problem)], True
    items: list[SweepItem] = []
    for path in _sweep_children(tmp):
        if deadline is not None and time.monotonic() >= deadline:
            return items, False
        if path.name.casefold() == ".secrets" or _protected_name(str(path.parent), path.name):
            items.append(SweepItem(path, "protected", "protected name"))
            continue
        try:
            old = os.lstat(path).st_mtime < cutoff
        except OSError:
            continue
        if not old:
            items.append(SweepItem(path, "recent"))
            continue
        verdict = gate.check(path)
        items.append(SweepItem(path, *verdict) if verdict else SweepItem(path, "orphan"))
    return items, True


def plan_sweep(
    age_hours: float, *, refusals: bool = True, now: float | None = None
) -> list[SweepItem]:
    return plan_sweep_bounded(age_hours, refusals=refusals, now=now)[0]


def sweep(dry_run: bool = False, config: CleanupConfig | None = None) -> int:
    """Delete (or list, in a dry run) the orphans; returns how many were refused, unknown or left."""
    config = config or load_cleanup_config()
    gate = Gate(check_inflight=True)
    items = plan_sweep_bounded(config.sweep_age_hours, gate=gate)[0]
    tmp = PROJECT / ".tmp"
    problems = 0
    freed = 0
    for item in items:
        name = item.path.relative_to(tmp).as_posix()
        if item.status == "orphan":
            is_link = _is_link(item.path)
            size = 0 if is_link else _size(item.path)
            note = " (link only)" if is_link else ""
            if dry_run:
                print(f"  would delete {name} (~{size / 2**20:.1f} MB){note}")
            elif gate.remove(item.path):
                freed += size
                print(f"  deleted {name} (~{size / 2**20:.1f} MB){note}")
            else:
                print(f"  kept {name} (could not remove: link or left partially)")
                problems += 1
        elif item.status == "unknown":
            print(f"  ownership unknown, not deleted: {name}: {item.detail}")
            problems += 1
        elif item.status == "refused":
            print(f"  refused {name}: {item.detail}")
            problems += 1
        elif item.status == "owned":
            print(f"  kept {name}: owned ({item.detail})")
    orphans = sum(1 for i in items if i.status == "orphan")
    verb = "would delete" if dry_run else "deleted"
    freed_note = "" if dry_run else f", freed ~{freed / 2**20:.1f} MB"
    print(f"{tmp}: {verb} {orphans} orphan(s) older than {config.sweep_age_hours:g} h{freed_note}")
    return problems


def prune_archive(dry_run: bool = False, config: CleanupConfig | None = None) -> int:
    """Delete direct children of `.tmp/pm-archive` older than the configured retention; the archive itself stays.
    Every child passes the `Gate` like a `sweep` candidate (containment, protected trees nested in it, ownership,
    busy worker); the only waiver is the protected name of `pm-archive` as their parent. A link child is removed as a
    link. Returns how many children were refused, unknown or could not be removed."""
    config = config or load_cleanup_config()
    problem = _path_problem(ARCHIVE)
    if problem:
        print(f"{ARCHIVE}: refused, {problem}")
        return 1
    if not ARCHIVE.is_dir() or _is_link(ARCHIVE):
        print(f"{ARCHIVE}: no archive directory, nothing to prune")
        return 0
    cutoff = time.time() - config.archive_retention_hours * 3600
    gate = Gate(check_inflight=True, exempt=ARCHIVE)
    removed = left = 0
    for child in sorted(ARCHIVE.iterdir()):
        try:
            if os.lstat(child).st_mtime >= cutoff:
                continue
        except OSError:
            continue
        verdict = gate.check(child)
        if verdict:
            label = "ownership unknown, not deleted" if verdict[0] == "unknown" else verdict[0]
            print(f"  {label}: archive entry {child.name}: {verdict[1]}")
            left += 1
        elif dry_run:
            print(f"  would delete archive entry {child.name} (dry run)")
            removed += 1
        elif gate.remove(child):
            print(f"  deleted archive entry {child.name}")
            removed += 1
        else:
            print(f"  kept archive entry {child.name} (could not remove)")
            left += 1
    verb = "would delete" if dry_run else "deleted"
    print(f"{ARCHIVE}: {verb} {removed} entries older than {config.archive_retention_hours:g} h")
    return left


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = __doc__ or ""
    if not args:
        raise SystemExit(usage)
    command, *rest = args
    dry_run = "--dry-run" in rest
    rest = [a for a in rest if a != "--dry-run"]
    if command in ("sweep", "prune-archive") and not rest:
        try:
            return 1 if (sweep if command == "sweep" else prune_archive)(dry_run) else 0
        except CleanupConfigError as error:
            raise SystemExit(f"refusing: {error}") from None
    if command == "archive-remove" and rest:
        return 1 if archive_remove(rest, dry_run) else 0
    if command == "purge-children" and len(rest) >= 2:
        purge_children(rest[0], rest[1:], dry_run)
    elif command == "purge-older" and len(rest) == 2:
        try:
            hours = float(rest[1])
        except ValueError:
            raise SystemExit(f"hours must be a number: {rest[1]!r}") from None
        purge_older(rest[0], hours, dry_run)
    else:
        raise SystemExit(f"unknown command or missing arguments: {command}\n{usage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
