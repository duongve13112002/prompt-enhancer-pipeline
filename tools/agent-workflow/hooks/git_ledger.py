"""Orchestrator Git write ledger and the worker turn snapshot (T-024 round 5, classes J and K; final round).

A before/after comparison of shared Git state shows that the state changed, not who changed it: refs are shared by
all worktrees (git-worktree(1): "refs are shared across all worktrees, except `refs/bisect`, `refs/worktree` and
`refs/rewritten`"), so the orchestrator's integration in the main tree moves refs that a running worker's turn also
sees. Attribution:

- `tool_guard.py --git-mode pm`, after it allows a command holding an orchestrator write form, calls `begin()`: the
  repository state before the command goes to `<state>/git-ledger/pending/<key>.json`, the key derived from the
  session and the tool call, created exclusively and atomically.
- this script, the orchestrator's PostToolUse and PostToolUseFailure hook, verifies it owns that record, takes the
  state again and appends the transitions the command caused (key, old, new) with the command's start and end time
  to `ledger.jsonl`, under a lock, one complete line per entry.
- `cli_worker.py` brackets each worker turn with `snapshot()` and flags every transition that no ledger entry inside
  the turn window explains (`unexplained()`).

The compared state (class K, final round F09-F13): every ref of the common Git directory with its object name and
symref target, plus every loose ref `git for-each-ref` leaves out (a dangling symref is kept by its target, any
other unreadable ref fails the reading); the root refs of every Git directory (`HEAD`, `ORIG_HEAD`, `FETCH_HEAD`,
`MERGE_HEAD`, `BISECT_*`, ... read as files, because `for-each-ref --include-root-refs` lists neither the pseudorefs
nor other worktrees); every registered worktree's HEAD (`git worktree list --porcelain`) under a canonical path key;
and every reflog of the common directory and of each worktree by entry count and SHA-256 of its bytes (`logs/`
read structurally; this needs the `files` ref storage format, any other format is reported as unreadable, so the
turn is flagged rather than unchecked). The worker snapshot adds the ledger's own fingerprint (F27 detection): the
ledger is append-only, so a prefix that changed during the turn is flagged. One deadline, `git_timeout_seconds`,
bounds the whole reading; exceeding it is a reported problem, never an omission.

Workers cannot write the ledger with their shell (tool_guard worker mode refuses targets in the state directory)
nor with Edit/Write (cli_worker deny rules); a forged entry still does not explain a change of the worker's own linked
worktree, a change outside the entry's time window, an entry carrying the worker's session, an entry whose end
precedes its start, or a transition whose old and new values do not chain.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_config as gc

LEDGER_NAME = "ledger.jsonl"  # artifact-bound: written by this hook, read by cli_worker.py
LOCK_NAME = "ledger.lock"  # artifact-bound: serialises appends to LEDGER_NAME across hook processes
PENDING_NAME = "pending"  # artifact-bound: begin() -> main() hand-over directory
LEDGER_KEY = f"ledger {LEDGER_NAME}"  # artifact-bound: the worker snapshot's ledger fingerprint key
FILES_FORMAT = "files"  # protocol-bound: git-rev-parse(1) --show-ref-format value whose reflogs are files
SYMREF_PREFIX = b"ref: "  # protocol-bound: gitglossary(7) symref, "of the format 'ref: refs/some/thing'"
# protocol-bound: git-worktree(1), the ref namespaces each worktree keeps for itself (R1 covers other worktrees')
PER_WORKTREE_REFS = ("refs/bisect/", "refs/worktree/", "refs/rewritten/")
# protocol-bound: the file-name form of Git's root refs and pseudorefs at the top of a Git directory (HEAD,
# ORIG_HEAD, FETCH_HEAD, MERGE_HEAD, CHERRY_PICK_HEAD, REVERT_HEAD, BISECT_*, AUTO_MERGE); a name with a dot (a
# `.lock`) or a lower-case letter is no root ref
ROOT_REF_NAME = re.compile(r"[A-Z][A-Z0-9_-]*")
# protocol-bound: the harness's documented foreground maximum for a Bash/PowerShell command ("timeout is in
# milliseconds: default 120000, max 600000"), used when the tool input states no timeout of its own
COMMAND_TIMEOUT_MAX_MS = 600_000
CHUNK = 1 << 16  # safety-bound: read size between two deadline checks
POLL_SECONDS = 0.05  # safety-bound: poll interval of the settle wait and of the append lock
# platform-bound: Windows refuses to delete or replace a file another handle holds open (a reader of a pending record
# holds it for the length of one small read; Python opens without FILE_SHARE_DELETE), so those calls wait and retry
RETRY_SHARING = os.name == "nt"
SHARING_ATTEMPTS = 40  # safety-bound: the same bound as cli_worker.REPLACE_ATTEMPTS (40 x POLL_SECONDS = 2 s)


T = TypeVar("T")


class SnapshotProblem(Exception):
    """The repository state cannot be read completely; the turn is flagged with this text."""


def ledger_dir(root: Path) -> Path:
    """`<state>/git-ledger`, with the state directory of the other hooks (AGENT_WORKFLOW_STATE_DIR, else
    `<root>/.tmp/pm-state`)."""
    state = os.environ.get("AGENT_WORKFLOW_STATE_DIR") or str(Path(root) / ".tmp" / "pm-state")
    return Path(state) / "git-ledger"


def _remaining(deadline: float, timeout: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise SnapshotProblem(
            f"reading the repository took longer than {timeout} s (git_timeout_seconds); it is judged unreadable"
        )
    return left


def _path_key(path: str) -> str:
    """One spelling per worktree directory (F13): links resolved, the on-disk case and `/` separators, so the keys of
    `git worktree list` and `git rev-parse --show-toplevel` compare equal however Git spelled the path."""
    return Path(os.path.realpath(path)).as_posix()


def _open_file(path: Path) -> int:
    """Open for reading without following a final link (lstat first: Windows has no O_NOFOLLOW) and without
    blocking on a FIFO; the caller checks with fstat that the opened file is a regular one."""
    if stat.S_ISLNK(os.lstat(path).st_mode) or _is_junction(path):
        raise SnapshotProblem(f"{path.name} is a link")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _is_junction(path: Path) -> bool:
    return bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def _fingerprint(path: Path, deadline: float, timeout: float) -> tuple[int, int, str, int, str]:
    """Stream a regular file up to its size at opening, with the deadline checked between chunks (F12): (bytes,
    newlines, SHA-256 of all bytes, bytes of the complete lines, SHA-256 of the complete lines)."""
    fd = _open_file(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotProblem(f"{path.name} is not a regular file")
        whole, lines, newlines, done, cut = hashlib.sha256(), hashlib.sha256(), 0, 0, 0
        while done < info.st_size:
            _remaining(deadline, timeout)
            chunk = os.read(fd, min(CHUNK, info.st_size - done))
            if not chunk:
                break  # the file shrank while it was read: the next reading sees the rest
            last = chunk.rfind(b"\n")
            if last < 0:
                whole.update(chunk)
            else:  # `lines` is the hash state right after the last newline read so far
                whole.update(chunk[: last + 1])
                lines, cut = whole.copy(), done + last + 1
                whole.update(chunk[last + 1:])
                newlines += chunk.count(b"\n")
            done += len(chunk)
        return done, newlines, whole.hexdigest(), cut, lines.hexdigest()
    finally:
        os.close(fd)


def _read(path: Path, deadline: float, timeout: float) -> bytes:
    """A small regular file's bytes, bounded by its size at opening and by the deadline."""
    fd = _open_file(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotProblem(f"{path.name} is not a regular file")
        parts: list[bytes] = []
        left = info.st_size
        while left > 0:
            _remaining(deadline, timeout)
            chunk = os.read(fd, min(CHUNK, left))
            if not chunk:
                break
            parts.append(chunk)
            left -= len(chunk)
        return b"".join(parts)
    finally:
        os.close(fd)


def _linked(entry: os.DirEntry[str]) -> bool:
    return entry.is_symlink() or bool(getattr(entry, "is_junction", lambda: False)())


def _walk(top: Path, deadline: float, timeout: float) -> list[tuple[str, Path]]:
    """Every regular file below `top` as (path relative to `top`, path); a link or a special file is a problem, so
    nothing can hide behind one; `.lock` files (a ref update in progress) are left out."""
    found: list[tuple[str, Path]] = []
    if top.is_symlink() or _is_junction(top):
        raise SnapshotProblem(f"{top.parent.name}/{top.name} is a link")
    if not top.is_dir():
        return found
    stack = [top]
    while stack:
        _remaining(deadline, timeout)
        folder = stack.pop()
        with os.scandir(folder) as listing:
            for entry in listing:
                path = Path(entry.path)
                if _linked(entry):
                    raise SnapshotProblem(f"{path.relative_to(top).as_posix()} below {top.name} is a link")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.name.endswith(".lock"):
                    continue
                elif entry.is_file(follow_symlinks=False):
                    found.append((path.relative_to(top).as_posix(), path))
                else:
                    raise SnapshotProblem(f"{path.relative_to(top).as_posix()} below {top.name} is no regular file")
    return sorted(found)


def _omitted_refs(base: Path, listed: dict[str, str], deadline: float, timeout: float) -> dict[str, str]:
    """F10: the shared refs `for-each-ref` left out. Git drops a ref with unreadable content ("warning: ignoring
    broken ref") and a dangling symref silently; a dangling symref is kept by its target, anything else fails."""
    found: dict[str, str] = {}
    for rel, path in _walk(base / "refs", deadline, timeout):
        name = f"refs/{rel}"
        if f"ref {name}" in listed or name.startswith(PER_WORKTREE_REFS):
            continue
        data = _read(path, deadline, timeout)
        target = data[len(SYMREF_PREFIX):].strip() if data.startswith(SYMREF_PREFIX) else b""
        if not target or b"\n" in target or b" " in target:
            raise SnapshotProblem(f"ref {name} could not be read (git for-each-ref left it out)")
        found[f"ref {name}"] = f"-> {target.decode('utf-8', 'surrogateescape')} (unresolved)"
    packed = base / "packed-refs"
    if packed.is_file():
        for line in _read(packed, deadline, timeout).splitlines():
            if not line or line.startswith((b"#", b"^")):
                continue  # gitrepository-layout(5) packed-refs: header and peeled lines
            _, _, raw = line.partition(b" ")
            name = raw.decode("utf-8", "surrogateescape").strip()
            if not name:
                raise SnapshotProblem("packed-refs holds a line that names no ref")
            if f"ref {name}" not in listed and f"ref {name}" not in found and not name.startswith(PER_WORKTREE_REFS):
                raise SnapshotProblem(f"packed ref {name} could not be read (git for-each-ref left it out)")
    return found


def _git_state(cwd: str | Path, timeout: float) -> dict[str, Any] | None:
    """The compared Git state at `cwd` without the ledger key (begin() and finish() record Git transitions only)."""
    deadline = time.monotonic() + timeout

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, timeout=_remaining(deadline, timeout), check=False
        )

    try:
        info = git(
            "rev-parse", "--show-ref-format", "--path-format=absolute", "--git-common-dir", "--git-dir",
            "--show-toplevel",
        )
        if info.returncode != 0:
            return None
        lines = info.stdout.decode("utf-8", "surrogateescape").splitlines()
        if len(lines) != 4:
            return {"problem": f"git rev-parse printed {len(lines)} lines, not 4"}
        ref_format, common, gitdir, top = lines
        if ref_format != FILES_FORMAT:
            return {"problem": f"the ref storage format `{ref_format}` keeps no reflog files to compare"}
        listed = git("for-each-ref", "--format=%(refname) %(objectname) %(symref)")
        trees = git("worktree", "list", "--porcelain", "-z")
        if listed.returncode != 0 or trees.returncode != 0:
            return {"problem": "git for-each-ref or git worktree list failed"}
        state: dict[str, str] = {}
        for line in listed.stdout.decode("utf-8", "surrogateescape").splitlines():
            name, _, rest = line.partition(" ")  # "<refname> <objectname> <symref>"; a ref name holds no space
            obj, _, target = rest.partition(" ")
            state[f"ref {name}"] = f"{obj} -> {target}" if target else obj
        base = Path(common)
        state.update(_omitted_refs(base, state, deadline, timeout))
        record: dict[str, str] = {}
        # git-worktree(1) --porcelain -z: "<label> <value>" fields, NUL-terminated, a record ends with an empty one.
        for field in [*trees.stdout.decode("utf-8", "surrogateescape").split("\0"), ""]:
            if field:
                label, _, value = field.partition(" ")
                record[label] = value
            elif "worktree" in record:
                where = "bare" if "bare" in record else record.get("branch", "detached")
                state[f"head {_path_key(record['worktree'])}"] = f"{where} {record.get('HEAD', '')}".strip()
                record = {}
        worktrees = base / "worktrees"
        gitdirs = [("", base)]
        if worktrees.is_symlink() or _is_junction(worktrees):
            raise SnapshotProblem("the worktrees folder of the Git directory is a link")
        if worktrees.is_dir():
            with os.scandir(worktrees) as listing:
                for entry in sorted(listing, key=lambda e: e.name):
                    if _linked(entry):
                        raise SnapshotProblem(f"worktrees/{entry.name} is a link")
                    if entry.is_dir(follow_symlinks=False):
                        gitdirs.append((f"worktrees/{entry.name}/", Path(entry.path)))
        for rel, folder in gitdirs:
            with os.scandir(folder) as listing:
                names = sorted((e for e in listing if ROOT_REF_NAME.fullmatch(e.name)), key=lambda e: e.name)
            for entry in names:
                if _linked(entry) or not (entry.is_file(follow_symlinks=False) or entry.is_dir(follow_symlinks=False)):
                    raise SnapshotProblem(f"root ref {rel}{entry.name} is a link or no regular file")
                if entry.is_file(follow_symlinks=False):
                    size, _, digest, _, _ = _fingerprint(Path(entry.path), deadline, timeout)
                    state[f"root {rel}{entry.name}"] = f"{size} {digest}"
            for name, path in _walk(folder / "logs", deadline, timeout):
                _, newlines, digest, _, _ = _fingerprint(path, deadline, timeout)
                state[f"reflog {rel}logs/{name}"] = f"{newlines} {digest}"
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"problem": f"the repository could not be read ({type(exc).__name__})"}
    except SnapshotProblem as exc:
        return {"problem": str(exc)}
    main = os.path.normcase(os.path.realpath(gitdir)) == os.path.normcase(os.path.realpath(common))
    own_rel = Path(gitdir).resolve().relative_to(base.resolve()).as_posix() if not main else ""
    return {"state": state, "main": main, "own": [f"head {_path_key(top)}", f"reflog {own_rel}/", f"root {own_rel}/"]}


def snapshot(cwd: str | Path, timeout: int) -> dict[str, Any] | None:
    """The compared state of the repository at `cwd` as {"state": {key: value}, "main": bool, "own": [...]};
    None when `cwd` is no Git work tree; {"problem": text} when git, the reflogs or the ledger cannot be read
    within `timeout` seconds. The state includes the fingerprint of the orchestrator ledger (LEDGER_KEY)."""
    deadline = time.monotonic() + timeout
    got = _git_state(cwd, timeout)
    if got is None or "problem" in got:
        return got
    path = ledger_dir(gc.ROOT) / LEDGER_NAME
    try:
        _, _, _, cut, digest = _fingerprint(path, deadline, timeout)
    except FileNotFoundError:
        cut, digest = 0, hashlib.sha256().hexdigest()
    except (OSError, SnapshotProblem) as exc:
        return {"problem": f"the orchestrator ledger could not be read ({exc})"}
    got["state"][LEDGER_KEY] = f"{cut} {digest}"
    return got


def transitions(before: dict[str, str], after: dict[str, str]) -> list[list[str | None]]:
    """[key, old, new] for every key whose value differs; None stands for absent."""
    return [
        [key, before.get(key), after.get(key)]
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


def describe(key: str, old: str | None, new: str | None) -> str:
    kind, _, name = key.partition(" ")
    if kind == "reflog":
        was, now = ((v or "0").split(" ")[0] for v in (old, new))
        if was == now and old and new:
            return f"reflog {name}: rewritten with {now} entries (same count, other content)"
        return f"reflog {name}: {was} -> {now} entries"
    if kind == "root":
        return f"root ref {name}: {old or 'absent'} -> {new or 'absent'}"
    label = f"HEAD of {name}" if kind == "head" else name
    return f"{label}: {old or 'absent'} -> {new or 'absent'}"


def _record_key(session: str, call: str) -> str:
    """F19: the pending record's file name from the session and the tool call, so neither two sessions reusing an id
    nor two ids that differ only in characters a file name cannot hold share a record."""
    return hashlib.sha256(json.dumps([session, call]).encode("utf-8")).hexdigest()[:40]


def _command_seconds(value: Any) -> float:
    """The command's own timeout from the tool input (milliseconds), else the documented maximum."""
    number = _real(value)
    if number is not None and number > 0:
        return number / 1000
    return COMMAND_TIMEOUT_MAX_MS / 1000


def _real(value: Any) -> float | None:
    """The one reader of a decoded number (a time, a deadline, a timeout): a finite float, or None for a boolean, a
    non-number, NaN, an infinity or an integer too large for a float (`float()` raises OverflowError on it, and so
    does `math.isfinite`), so an unrepresentable value makes its record unusable instead of raising (CX5)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _number(value: Any) -> bool:
    return _real(value) is not None


def _pending_record(raw: bytes) -> dict[str, Any] | str:
    """A pending record, schema-checked (F18); the reason as text when it is not one."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:  # ValueError covers over-long integers too
        return f"not JSON ({type(exc).__name__})"
    if not isinstance(record, dict):
        return "not a JSON object"
    for name in ("session", "tool_use_id", "cwd", "command"):
        if not isinstance(record.get(name), str):
            return f"`{name}` is not text"
    if not (_number(record.get("start")) and _number(record.get("deadline"))):
        return "`start` or `deadline` is not a number"
    if record["deadline"] < record["start"]:
        return "`deadline` precedes `start`"
    before = record.get("before")
    if not isinstance(before, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in before.items()):
        return "`before` is not a map of text"
    if "incomplete" in record and not isinstance(record["incomplete"], str):
        return "`incomplete` is not text"
    return record


def _sharing_retry(action: Callable[[], T]) -> T:
    """Run `action`; where `RETRY_SHARING` holds, a PermissionError (a reader's handle on the file, gone within one
    small read) is waited out and the call repeated, so a delete or replace never loses the race against a poll of
    the pending folder. The last failure is raised: a handle held for seconds is a real fault, not a race."""
    for attempt in range(SHARING_ATTEMPTS if RETRY_SHARING else 1):
        try:
            return action()
        except PermissionError:
            if attempt == SHARING_ATTEMPTS - 1 or not RETRY_SHARING:
                raise
            time.sleep(POLL_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


def _write_new(path: Path, data: bytes, exclusive: bool) -> bool:
    """Write through a temporary file in the same folder, then publish it: exclusively with a hard link (False when
    `path` exists already) or by replacing; readers never see a partial record (F18)."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if not exclusive:
            _sharing_retry(lambda: os.replace(temp, path))
            return True
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        return True
    finally:
        # the temp name is a second link of the published record once linked, so a reader of `path` holds it too
        _sharing_retry(lambda: temp.unlink(missing_ok=True))


def begin(root: Path, event: dict[str, Any], cwd: Path, timeout: int) -> None:
    """pm mode, after the guard allowed an orchestrator write form: keep the state before the command."""
    call, session = event.get("tool_use_id"), event.get("session_id")
    if not (isinstance(call, str) and call and isinstance(session, str) and session):
        return  # nothing pairs the PostToolUse event with this one: the change stays unexplained (fail closed)
    start = time.time()
    before = _git_state(cwd, timeout)
    if before is None or "problem" in before:
        return
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = tool_input.get("command")
    record = {
        "start": start,
        # past this the command and its PostToolUse hook (bounded by the same git timeout) have ended (F20)
        "deadline": start + _command_seconds(tool_input.get("timeout")) + timeout,
        "session": session,
        "cwd": str(cwd),
        "tool_use_id": call,
        "command": command[:300] if isinstance(command, str) else "",
        "before": before["state"],
    }
    pending = ledger_dir(root) / PENDING_NAME
    pending.mkdir(parents=True, exist_ok=True)
    if not _write_new(pending / f"{_record_key(session, call)}.json", json.dumps(record).encode("utf-8"), True):
        sys.stderr.write(f"git_ledger: a pending record for {call} exists already; the first one is kept\n")


def _lock(handle: Any, deadline: float) -> None:
    """An exclusive OS lock on the lock file (released by the kernel when a killed hook's process ends)."""
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("the ledger lock stayed busy") from None
            time.sleep(POLL_SECONDS)


def _append(folder: Path, line: bytes, timeout: float) -> None:
    """Append one entry line under the lock (F18): concurrent hooks never interleave, and a line left torn by a killed
    hook is closed first, so it fails parsing alone instead of swallowing this entry."""
    with (folder / LOCK_NAME).open("a+b") as handle:
        _lock(handle, time.monotonic() + timeout)
        try:
            with (folder / LEDGER_NAME).open("a+b") as out:
                size = out.seek(0, os.SEEK_END)
                torn = False
                if size:
                    out.seek(size - 1)
                    torn = out.read(1) != b"\n"
                out.write((b"\n" if torn else b"") + line)
                out.flush()
                os.fsync(out.fileno())
        finally:
            _unlock(handle)


def _unlock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def finish(root: Path, event: dict[str, Any], timeout: int) -> bool:
    """PostToolUse: append the transitions of the command `begin()` saw; True when an entry was written."""
    call, session = event.get("tool_use_id"), event.get("session_id")
    if not (isinstance(call, str) and call and isinstance(session, str) and session):
        return False
    folder = ledger_dir(root)
    path = folder / PENDING_NAME / f"{_record_key(session, call)}.json"
    try:
        raw = _sharing_retry(path.read_bytes)
    except FileNotFoundError:
        return False
    record = _pending_record(raw)
    if isinstance(record, str):
        return False  # left in place: pending() names it as incomplete in the turns it overlaps
    if record["session"] != session or record["tool_use_id"] != call or "incomplete" in record:
        return False  # not this call's record (F19)
    after = _git_state(record["cwd"], timeout)
    if after is None or "problem" in after:
        reason = (after or {}).get("problem", "no Git work tree")
        mark = {**record, "incomplete": f"the state after the command could not be read: {reason}"}
        _write_new(path, json.dumps(mark).encode("utf-8"), False)
        return False
    changed = transitions(record.pop("before"), after["state"])
    record.pop("deadline")
    if changed:
        line = json.dumps({**record, "end": time.time(), "transitions": changed}) + "\n"
        _append(folder, line.encode("utf-8"), timeout)
    _sharing_retry(lambda: path.unlink(missing_ok=True))  # a settle poll may be reading the record this instant
    return bool(changed)


def entry_ok(entry: Any) -> bool:
    """A usable ledger entry (F18, F21): finite times with `end >= start` (an entry whose clock ran backwards
    explains nothing), a known session type and well-formed [key, old, new] text transitions."""
    if not isinstance(entry, dict):
        return False
    start, end, steps = entry.get("start"), entry.get("end"), entry.get("transitions")
    if not (_number(start) and _number(end) and end >= start and isinstance(steps, list)):
        return False
    if not isinstance(entry.get("session"), (str, type(None))):
        return False
    return all(
        isinstance(s, list) and len(s) == 3 and isinstance(s[0], str)
        and all(isinstance(v, (str, type(None))) for v in s[1:])
        for s in steps
    )


class Ledger(list):  # type: ignore[type-arg]
    """The usable entries, plus what the integrity check needs: `prefixes` maps the end offset of every complete line
    (and 0) to the SHA-256 of the bytes before it, `malformed` holds the start offsets of complete lines that are no
    usable entry, `incomplete` the (start, description) of orchestrator commands that will not settle (F20)."""

    def __init__(self) -> None:
        super().__init__()
        self.prefixes: dict[int, str] = {0: hashlib.sha256().hexdigest()}
        self.malformed: list[int] = []
        self.incomplete: list[tuple[float, str]] = []


def _scan_pending(root: Path) -> list[tuple[float, str, str | None]]:
    """(start, name, reason) of every pending record; reason None while the command may still write its entry. A
    record that cannot be read, that finish() marked, or that outlived its command's timeout is incomplete."""
    found: list[tuple[float, str, str | None]] = []
    folder = ledger_dir(root) / PENDING_NAME
    if not folder.is_dir():
        return found
    now = time.time()
    for path in sorted(folder.glob("*.json")):
        try:
            raw, mtime = _sharing_retry(path.read_bytes), path.stat().st_mtime
        except FileNotFoundError:
            continue  # finished meanwhile
        except OSError as exc:
            found.append((now, path.name, f"unreadable record ({type(exc).__name__})"))
            continue
        record = _pending_record(raw)
        if isinstance(record, str):
            found.append((mtime, path.name, f"malformed record: {record}"))
        elif "incomplete" in record:
            found.append((record["start"], record["tool_use_id"], record["incomplete"]))
        elif now > record["deadline"]:
            seconds = round(record["deadline"] - record["start"])
            found.append((record["start"], record["tool_use_id"], f"no entry within its {seconds} s bound"))
        else:
            found.append((record["start"], record["tool_use_id"], None))
    return found


def entries(root: Path) -> Ledger:
    """The ledger's usable entries; a malformed, undecodable or clock-reversed line explains nothing and is listed
    in `malformed`; a trailing line without its newline is an append in progress and is not read yet."""
    found = Ledger()
    found.incomplete = [(start, f"{name} ({why})") for start, name, why in _scan_pending(root) if why]
    try:
        data = (ledger_dir(root) / LEDGER_NAME).read_bytes()
    except FileNotFoundError:
        return found
    except OSError:
        found.prefixes = {}  # nothing can be verified: the integrity check flags the turn
        return found
    digest, offset = hashlib.sha256(), 0
    while (end := data.find(b"\n", offset)) >= 0:  # split on LF only, as the writer frames lines
        line = data[offset : end + 1]
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            entry = None
        if entry_ok(entry):
            found.append(entry)
        elif line.strip():
            found.malformed.append(offset)
        digest.update(line)
        offset += len(line)
        found.prefixes[offset] = digest.hexdigest()
    return found


def pending(root: Path, since: float, until: float) -> list[str]:
    """The orchestrator write commands that started inside [since, until], have not written their entry yet and may
    still do so (incomplete ones are named by `incomplete()` and in `unexplained()`)."""
    return [name for start, name, why in _scan_pending(root) if why is None and since <= start <= until]


def incomplete(root: Path, since: float, until: float) -> list[str]:
    """The orchestrator write commands that started inside [since, until] and will not write their entry (F20)."""
    return [f"{name} ({why})" for start, name, why in _scan_pending(root) if why and since <= start <= until]


def settle(root: Path, since: float, until: float, timeout: float) -> list[str]:
    """Wait, at most `timeout` seconds, for orchestrator commands that started inside [since, until] to write their
    entry, so a write still running when the turn ends is not taken for the worker's; returns those still running
    (empty when all settled). An incomplete record ends the wait at once: no entry is coming (F20)."""
    deadline = time.monotonic() + timeout
    while True:
        running = pending(root, since, until)
        if not running or time.monotonic() >= deadline:
            return running
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def overlapping(ledger: list[dict[str, Any]], read_start: float, read_end: float) -> list[str]:
    """Entries of commands that were still running while the state was read in [read_start, read_end]: that reading
    may hold an intermediate state their recorded transitions do not chain to (round 6, class M)."""
    return [
        str(e.get("tool_use_id") or "unnamed")
        for e in ledger
        if entry_ok(e) and e["end"] >= read_start and e["start"] <= read_end
    ]


def _chains(key: str, old: str | None, new: str | None, usable: list[dict[str, Any]]) -> bool:
    current = old
    for entry in usable:
        for step in entry["transitions"]:
            if step[0] == key and step[1] == current:
                current = step[2]
    return current == new


def _ledger_integrity(before: str | None, after: str | None, ledger: list[dict[str, Any]]) -> list[str]:
    """F27 detection: the ledger is append-only, so the bytes it held when the turn began, and when it ended, are a
    prefix of the ledger read now; every line appended since the turn began is a usable entry."""
    try:
        (n0, d0), (n1, d1) = ((int(v.split(" ")[0]), v.split(" ")[1]) for v in (before or "", after or ""))
    except (IndexError, ValueError):
        return ["the orchestrator ledger fingerprint is missing on one side of the turn"]
    prefixes: dict[int, str] | None = getattr(ledger, "prefixes", None)
    if not prefixes:
        return ["the orchestrator ledger could not be read to verify it"]
    problems = []
    if n1 < n0:
        problems.append(f"the orchestrator ledger shrank during the turn ({n0} -> {n1} bytes)")
    elif prefixes.get(n0) != d0:
        problems.append(f"the first {n0} bytes of the orchestrator ledger changed during the turn")
    if n1 >= n0 and prefixes.get(n1) != d1:
        problems.append("the orchestrator ledger read after the turn does not extend the one read at its end")
    late = [offset for offset in getattr(ledger, "malformed", []) if offset >= n0]
    if late:
        problems.append(f"{len(late)} line(s) appended to the orchestrator ledger during the turn are no usable entry")
    return problems


def unexplained(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    window: tuple[float, float],
    ledger: list[dict[str, Any]],
    session: str | None,
    until: float | None = None,
) -> list[str]:
    """The turn's changes that no orchestrator ledger entry explains. An entry explains a key's change when its
    transitions chain from the value before the turn to the value after it, the entry's command overlaps the turn
    window, extended to `until` (the end of the after-reading, which follows the turn), without containing the turn
    window itself (a command that spans the whole turn may have run that turn), and it is not the worker's session.
    A change of the worker's own linked worktree (its HEAD, root refs and reflogs) is never explained: the
    orchestrator does not move a running worker's HEAD. A ledger whose earlier bytes changed is flagged; an
    orchestrator command that began in the window and will not write its entry is named before the changes."""
    if before is None and after is None:
        return []  # no repository: nothing to compare
    if before is None or after is None:
        return [f"the repository could not be read {'before' if before is None else 'after'} the turn"]
    for snap in (before, after):
        if "problem" in snap:
            return [snap["problem"]]
    t0, t1 = window
    read_end = t1 if until is None else max(t1, until)
    usable = sorted(
        (
            e
            for e in ledger
            if entry_ok(e)
            and e["end"] >= t0
            and e["start"] <= read_end
            and not (e["start"] <= t0 and e["end"] >= t1)
            and (session is None or e.get("session") != session)
        ),
        key=lambda e: (e["start"], e["end"]),
    )
    old_state, new_state = before["state"], after["state"]
    problems = []
    if LEDGER_KEY in old_state or LEDGER_KEY in new_state:
        problems = _ledger_integrity(old_state.get(LEDGER_KEY), new_state.get(LEDGER_KEY), ledger)
    own_head, *own_prefixes = before["own"]
    changes = []
    for key, old, new in transitions(old_state, new_state):
        if key == LEDGER_KEY:
            continue
        own = not before["main"] and (key == own_head or any(key.startswith(p) for p in own_prefixes))
        if own or not _chains(key, old, new, usable):
            changes.append(describe(key, old, new))
    if changes:
        stalled = [text for start, text in getattr(ledger, "incomplete", []) if t0 <= start <= read_end]
        if stalled:
            changes.insert(
                0,
                f"orchestrator Git write(s) begun during the turn will not record their changes: {', '.join(stalled)}",
            )
    return problems + changes


def main(argv: list[str] | None = None, root: Path = gc.ROOT) -> int:
    """PostToolUse / PostToolUseFailure hook of the orchestrator; never blocks and never fails on a bad record."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if isinstance(event, dict):
            finish(root, event, gc.load(gc.resolve_path(None))["git_timeout_seconds"])
    except Exception as exc:  # noqa: BLE001 - a hook that raises would surface as a tool failure; report and go on
        sys.stderr.write(f"git_ledger: no entry written ({type(exc).__name__}: {exc})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
