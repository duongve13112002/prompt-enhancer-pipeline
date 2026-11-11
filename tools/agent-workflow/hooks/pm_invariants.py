"""PostToolUse hook for the PM session: prints one short line per broken PM invariant, nothing otherwise (decision 503).

Rules (each line names the action):
(a) work is running (a busy CLI worker, a running CI watcher, an agy job without `.done`) but THIS project's
    supervisor is not alive: "start the supervisor" (alive = a live heartbeat record, see `record_alive`, or a process
    whose command line names a configured marker AND this project, see `names_project`);
(b) HEAD has commits no remote has (ahead of its upstream, else not on any remote branch), no CI watcher runs and the
    oldest of them is older than the configured age: "push and run CI" (a CI watcher is a live `ci-watch-<run>.json`
    record or a process whose command line names a configured marker and this project);
(c) a worker result file (`.tmp/cli-jobs/<name>/NNN.result.md`) is newer than the PM's last Read of it: "read <path>";
(d) work is running and `.tmp/pm-state/pm-continuation.md` is older than the configured age: "update the
    continuation note".
The checks run at most once per `check_interval_minutes` (state `pm-invariants.json` in the state directory). Every
Read of a `*.result.md` file is recorded on each call, as the modification time of a marker file under
`pm-invariants-reads/`, so parallel hook processes cannot lose a record.

Values: `pm_invariants_config.json` next to this file; a missing or invalid value prints one line
"pm_invariants config invalid: <reason>" and nothing else runs. Fail-open policy: the hook never blocks, always exits
0 and prints nothing on its own errors; a rule whose input cannot be read (no process list, no remote) is skipped,
never reported. A process on this machine counts only when it is this project's: other projects run the same script
names. Output is PostToolUse `additionalContext`. Standard library only (runs with `python -B -I`).

Usage (hook command args): pm_invariants.py [--config FILE] [--root DIR] [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "pm_invariants_config.json"
SCHEMA_VERSION = (
    2  # protocol-bound: the layout this module reads (2: supervisor_heartbeat_file added)
)
WORKER_ENV_FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"  # set by cli_worker.py for workers: the PM-only hook stays silent
# Artifact-bound locations written by cli_worker.py, pm-supervisor.ps1, the agy runner and the PM.
CLI_REGISTRY = "cli-workers.json"
AGENT_REGISTRY = "agents.json"
CONTINUATION_NOTE = "pm-continuation.md"
CLI_JOBS = Path(".tmp") / "cli-jobs"
AGY_RESULTS = Path(".tmp") / "agy-work" / "_results"
RESULT_SUFFIX = ".result.md"
CI_WATCH_GLOB = (
    "ci-watch-*.json"  # written by .tmp/pm-scripts/ci_wait_private.ps1, one file per watched run
)
STATE_FILE = "pm-invariants.json"
READS_DIR = "pm-invariants-reads"
WINDOWS_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (protocol-bound)

Processes = Callable[[int], "list[str] | None"]


class ConfigError(ValueError):
    """The hook configuration is missing or invalid; the message names the field."""


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a positive integer, got {value!r}")
    return value


def _markers(value: Any, where: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise ConfigError(f"{where} must be a non-empty list of non-empty strings, got {value!r}")
    return list(value)


INT_FIELDS = (
    "check_interval_minutes",
    "unpushed_age_minutes",
    "note_age_minutes",
    "result_lookback_minutes",
    "max_paths_listed",
    "git_timeout_seconds",
    "process_list_timeout_seconds",
)
LIST_FIELDS = ("supervisor_markers", "ci_watcher_markers")
STR_FIELDS = ("supervisor_heartbeat_file",)


def _file_name(value: Any, where: str) -> str:
    """A plain file name: the heartbeat is read from the state directory only, never from another place."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or value in {".", ".."}
        or any(c in value for c in "/\\:\0")
    ):
        raise ConfigError(f"{where} must be a plain file name without a path, got {value!r}")
    return value


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path.name}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be an object")
    expected = {"schema_version", *INT_FIELDS, *LIST_FIELDS, *STR_FIELDS}
    if sorted(set(data) - expected):
        raise ConfigError(
            f"{path.name} has unknown keys: {', '.join(sorted(set(data) - expected))}"
        )
    if sorted(expected - set(data)):
        raise ConfigError(
            f"{path.name} is missing required keys: {', '.join(sorted(expected - set(data)))}"
        )
    if data["schema_version"] != SCHEMA_VERSION or isinstance(data["schema_version"], bool):
        raise ConfigError(
            f"schema_version must be {SCHEMA_VERSION}, got {data['schema_version']!r}"
        )
    cfg: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for name in INT_FIELDS:
        cfg[name] = _positive_int(data[name], name)
    for name in LIST_FIELDS:
        cfg[name] = _markers(data[name], name)
    for name in STR_FIELDS:
        cfg[name] = _file_name(data[name], name)
    return cfg


def load_cli_worker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "cli_worker_identity_src", HERE.parent / "cli_worker.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load cli_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def launcher_gone(entry: dict[str, Any]) -> bool:
    """Whether the launcher recorded in a busy cli_worker entry is provably gone: no process with its PID, or another
    one (a reused PID) by creation time. The identity logic is cli_worker's (`entry_process_state`), loaded from the
    sibling file, never duplicated; if it cannot be loaded the launcher counts as not gone (the hook stays quiet)."""
    try:
        module = load_cli_worker()
        return bool(module.entry_process_state(entry, "launcher")[1] == module.GONE)
    except Exception:
        return False


def record_alive(find: Callable[[], list[Path]]) -> bool:
    """Whether one of the identity records `find()` lists belongs to a process that still runs. A record has `pid` and
    `created` (the process creation time, epoch seconds). It counts only while a process with that PID exists AND was
    created at the recorded time (cli_worker's `process_state`): a killed process or a reused PID leaves a stale file
    that is ignored, never deleted here. A damaged file (also one that makes the reader raise: deep nesting, a huge
    number) counts as no record without hiding a live one beside it; a failure to load the identity code or to list
    the records counts as no record at all."""
    try:
        module = load_cli_worker()
        paths = find()
    except Exception:
        return False
    for path in paths:
        try:
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            pid, created = module.as_pid(data.get("pid")), module.as_time(data.get("created"))
            if (
                pid is not None
                and created is not None
                and module.process_state(pid, created) == module.ALIVE
            ):
                return True
        except Exception:
            continue
    return False


def ci_watch_alive(state: Path) -> bool:
    """Whether a CI watcher recorded its own identity and still runs: `ci_wait_private.ps1` writes
    `ci-watch-<run id>.json` at start and removes it at the end. A watcher the PM starts through the Claude tool has
    no marker in any command line, so the record is the signal."""
    return record_alive(lambda: sorted(state.glob(CI_WATCH_GLOB)))


def supervisor_alive(state: Path, file_name: str) -> bool:
    """Whether THIS project's supervisor is alive: `pm-supervisor.ps1` writes its heartbeat record (`pid`, `created`)
    to `<state>/<file_name>` at start and removes it at the end. The supervisor is started with a relative path, so
    its command line does not name the project; another project's supervisor has the same name and its own state
    directory, which is why the record, not a machine-wide command-line match, is the signal."""
    return record_alive(lambda: [state / file_name])


class Identity(NamedTuple):
    """How a command line names this project: its root path (both slash forms) or its GitHub `owner/repo`."""

    patterns: tuple[re.Pattern[str], ...]


REMOTE_URL = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^/@]+@)?[^/:]+(?::\d+)?/|[\w.-]+@[\w.-]+:)"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)  # protocol-bound: the URL shapes git accepts for a remote; a local path names no owner/repo


def project_identity(root: Path, timeout: int) -> Identity:
    flags = re.I if sys.platform == "win32" else 0
    forms: set[str] = set()
    candidates = [root]
    with contextlib.suppress(OSError, RuntimeError):
        candidates.append(root.resolve())
    for path in candidates:
        text = str(path)
        forms.update({text, text.replace("\\", "/"), text.replace("/", "\\")})
    patterns = [
        re.compile(rf"(?<![\w.~-]){re.escape(form)}(?=$|[\\/\s\"'])", flags)
        for form in sorted(forms)
    ]
    url = git_lines(root, ["remote", "get-url", "origin"], timeout)
    match = REMOTE_URL.match(url[0]) if url else None
    if match:
        slug = f"{match['owner']}/{match['repo']}"
        patterns.append(re.compile(rf"(?<![\w.-]){re.escape(slug)}(?![\w.-])", re.I))
    return Identity(tuple(patterns))


def names_project(line: str, identity: Identity) -> bool:
    return any(p.search(line) for p in identity.patterns)


def _run_lines(argv: list[str], timeout: int) -> list[str] | None:
    flags = WINDOWS_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False, creationflags=flags
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]


def process_command_lines(timeout: int) -> list[str] | None:
    """Command line of every running process, or None when the platform gives no list. Windows: CIM through
    PowerShell (`wmic` is gone from current Windows); Linux: /proc; others: `ps`."""
    if sys.platform == "win32":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            return None
        return _run_lines(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }",
            ],
            timeout,
        )
    proc_dir = Path("/proc")
    if (proc_dir / "self").exists():
        lines = []
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            with contextlib.suppress(OSError):
                raw = (entry / "cmdline").read_bytes()
                if raw:
                    lines.append(raw.replace(b"\0", b" ").decode("utf-8", "replace").strip())
        return lines
    return _run_lines(["ps", "-eo", "args="], timeout)


def marker_running(lines: list[str], markers: list[str], identity: Identity) -> bool:
    """Whether a command line names one of the markers as a whole file name (not `x-pm-supervisor.ps1.bak`) AND
    names this project. The process list is machine-wide: a marker alone also matches another project's process."""
    words = [r"\s+".join(re.escape(w) for w in m.split()) for m in markers]
    patterns = [re.compile(rf"(?<![\w.-]){w}(?![\w.-])", re.I) for w in words]
    return any(
        names_project(line, identity) and any(p.search(line) for p in patterns) for line in lines
    )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def norm(path: str | Path, base: Path | None = None) -> str:
    p = Path(path)
    if not p.is_absolute() and base is not None:
        p = base / p
    return os.path.normcase(os.path.normpath(p))


def read_marker(state: Path, path: str) -> Path:
    return state / READS_DIR / hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]


def record_read(state: Path, path: str) -> None:
    """The marker's modification time is the time of the PM's last Read of `path`."""
    marker = read_marker(state, path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(path, encoding="utf-8")
    os.utime(marker)  # rewriting the same text may keep the old time on some file systems


def busy_cli_workers(state: Path) -> list[str]:
    data = read_json(state / CLI_REGISTRY)
    if not isinstance(data, dict):
        return []
    out = []
    for name, entry in data.items():
        if not isinstance(entry, dict) or entry.get("status") != "busy":
            continue
        # A busy entry whose launcher is gone is a dead launcher (the supervisor reports it), not running work.
        if launcher_gone(entry):
            continue
        out.append(str(name))
    return out


def pending_agy_jobs(root: Path, state: Path) -> list[str]:
    data = read_json(state / AGENT_REGISTRY)
    if not isinstance(data, list):
        return []
    return [
        str(job["id"])
        for job in data
        if isinstance(job, dict)
        and job.get("kind") == "gemini"
        and isinstance(job.get("id"), str)
        and job["id"]
        and not (root / AGY_RESULTS / f"{job['id']}.done").exists()
    ]


def git_lines(root: Path, args: list[str], timeout: int) -> list[str] | None:
    return _run_lines(["git", "-C", str(root), *args], timeout)


def unpushed_age_minutes(root: Path, timeout: int, now: float) -> tuple[int, int] | None:
    """(unpushed commits, age in minutes of the oldest of them). Unpushed = ahead of the upstream; a branch without one
    (never pushed, or its upstream is gone) counts the commits no remote branch has. None without any remote."""
    stamps = git_lines(root, ["log", "@{u}..HEAD", "--format=%ct"], timeout)
    if stamps is None and git_lines(root, ["remote"], timeout):
        stamps = git_lines(root, ["log", "HEAD", "--not", "--remotes", "--format=%ct"], timeout)
    if not stamps:
        return None
    try:
        oldest = min(int(s) for s in stamps)
    except ValueError:
        return None
    return len(stamps), max(0, int((now - oldest) // 60))


def unread_results(
    root: Path, state: Path, now: float, lookback_minutes: int
) -> list[tuple[float, str]]:
    """(modification time, root-relative path) of result files written within the lookback and after the PM's last Read."""
    out: list[tuple[float, str]] = []
    jobs = root / CLI_JOBS
    if not jobs.is_dir():
        return out
    for path in jobs.glob(f"*/*{RESULT_SUFFIX}"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now - mtime > lookback_minutes * 60:
            continue
        marker = read_marker(state, norm(path))
        with contextlib.suppress(OSError):
            if marker.stat().st_mtime >= mtime:
                continue
        out.append((mtime, path.relative_to(root).as_posix()))
    return sorted(out)


def check(
    cfg: dict[str, Any], root: Path, state: Path, now: float, processes: Processes
) -> list[str]:
    """One line per violated rule."""
    lines: list[str] = []
    cached: list[list[str] | None] = []

    def process_lines() -> list[str] | None:
        if not cached:
            cached.append(processes(cfg["process_list_timeout_seconds"]))
        return cached[0]

    identity: list[Identity] = []

    def running(markers: list[str]) -> bool | None:
        found = process_lines()
        if found is None:
            return None
        if not identity:
            identity.append(project_identity(root, cfg["git_timeout_seconds"]))
        return marker_running(found, markers, identity[0])

    def watcher_running() -> bool | None:
        # The watcher's own record first; the command-line marker covers a watcher that wrote none.
        return True if ci_watch_alive(state) else running(cfg["ci_watcher_markers"])

    def supervisor_running() -> bool | None:
        # The heartbeat first; a command line counts only when it names this project (e.g. an absolute-path start).
        return (
            True
            if supervisor_alive(state, cfg["supervisor_heartbeat_file"])
            else running(cfg["supervisor_markers"])
        )

    work = [f"cli worker {n}" for n in busy_cli_workers(state)]
    work += [f"agy job {j}" for j in pending_agy_jobs(root, state)]
    watcher = watcher_running() if not work else None
    if watcher:
        work.append("CI watcher")
    supervisor = supervisor_running() if work else None
    if work and supervisor is False:
        lines.append(
            f"[pm-invariants] Work is running ({', '.join(work[:3])}) but no supervisor process: start the supervisor."
        )
    if work:
        note = state / CONTINUATION_NOTE
        try:
            age = int((now - note.stat().st_mtime) // 60)
        except OSError:
            age = None
        if age is None or age >= cfg["note_age_minutes"]:
            how = "is missing" if age is None else f"is {age} min old"
            lines.append(
                f"[pm-invariants] Work is running and {CONTINUATION_NOTE} {how}: update the continuation note."
            )
    ahead = unpushed_age_minutes(root, cfg["git_timeout_seconds"], now)
    if ahead and ahead[1] >= cfg["unpushed_age_minutes"] and watcher_running() is False:
        lines.append(
            f"[pm-invariants] {ahead[0]} unpushed commit(s), oldest {ahead[1]} min old, no CI watcher: push and run CI."
        )
    unread = unread_results(root, state, now, cfg["result_lookback_minutes"])
    if unread:
        shown = [p for _t, p in unread[-cfg["max_paths_listed"] :]]
        more = len(unread) - len(shown)
        lines.append(
            f"[pm-invariants] Unread worker result(s): read {', '.join(shown)}"
            + (f" (+{more} older)" if more else "")
            + "."
        )
    return lines


def read_event() -> str:
    """Hook JSON from stdin as UTF-8 (`python -I` ignores PYTHONUTF8; see ctx_nudge.read_event)."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read()
    return str(stream.read().decode("utf-8-sig", errors="replace"))


def emit(text: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}
        )
    )


def load_check_time(path: Path) -> float | None:
    data = read_json(path)
    value = data.get("last_check") if isinstance(data, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def save_check_time(path: Path, now: float) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"last_check": now}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def run(
    event: dict[str, Any],
    cfg_path: Path,
    root: Path,
    state: Path,
    now: float,
    processes: Processes = process_command_lines,
) -> str:
    """The hook's output text ('' for none)."""
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        return f"pm_invariants config invalid: {exc}"
    tool_input = event.get("tool_input")
    target = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if (
        event.get("tool_name") == "Read"
        and isinstance(target, str)
        and target.endswith(RESULT_SUFFIX)
    ):
        cwd = event.get("cwd")
        record_read(state, norm(target, Path(cwd) if isinstance(cwd, str) and cwd else root))
    last = load_check_time(state / STATE_FILE)
    if last is not None and 0 <= now - last < cfg["check_interval_minutes"] * 60:
        return ""
    # Stamp first: a slow or hung check must not run again on the very next tool call.
    save_check_time(state / STATE_FILE, now)
    return " ".join(check(cfg, root, state, now, processes))


class QuietParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(
            message
        )  # the default prints usage to stderr and exits; a hook stays silent


def main(argv: list[str] | None = None) -> int:
    try:
        ap = QuietParser()
        ap.add_argument("--config", default=str(DEFAULT_CONFIG))
        ap.add_argument("--root", default=str(HERE.parents[2]))
        ap.add_argument("--state-dir", default=None)
        a = ap.parse_args(argv)
        event = json.loads(read_event())
        if not isinstance(event, dict) or os.environ.get(WORKER_ENV_FLAG) == "1":
            return 0
        root = Path(a.root)
        state = Path(
            a.state_dir or os.environ.get("AGENT_WORKFLOW_STATE_DIR") or root / ".tmp" / "pm-state"
        )
        text = run(event, Path(a.config), root, state, time.time())
        if text:
            emit(text)
    except (Exception, SystemExit):  # `--help` exits too
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
