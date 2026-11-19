"""CI visibility window of the orchestrator (T-022, docs/ci.md, R-025, decisions 26, 77, 112).

`ci_window.py run --repo <main working tree>` opens one window:
1. Preconditions, all failing closed (exit 2, the repository untouched): a clean tree; HEAD is
   `refs/heads/<push_branch>` and equals that branch on the remote (`git ls-remote`, which writes nothing); every
   commit a remote ref names exists locally; no path under a forbidden prefix or with a forbidden suffix in the history
   of any remote ref (exact allowlisted paths aside); every `uses:` of the workflow at that commit is pinned to a full
   commit SHA; the configured secret scanner, hash-checked before and after it runs and its version read from the
   binary itself, passes over that same history, with the baseline read from the scanned commit.
2. Record the visibility. 3. While still private, read the remote refs and local HEAD again and refuse (exit 2) on any
   difference from what was scanned; refuse unless the wiki, discussions and Pages are off, no release, issue or pull
   request exists, and every retained workflow run and artifact is from a run in `run_ledger`; check for a recorded
   signal after these reads; then switch to public, only when it was private. An already-public start is left
   as found and warned about loudly (S35). An internal repository is refused.
4. Compare the remote refs again, dispatch the workflow on the branch, tie the run to the commit (run id from the
   printed URL, else exactly one new `gh run list` match; then `headSha` and `event`), record its id in `run_ledger`,
   and poll it with a time limit. The ledger is read and written under its lock; every trusted local file is opened
   through `trusted_state`, which holds the folder chain from the project root and checks the opened handle, so no
   link below the root is followed.
5. Signals (SIGINT, SIGTERM, SIGHUP, SIGQUIT, SIGBREAK, whichever exist) are only recorded from before the first
   change until after the report: no handler raises inside the window, and a recorded signal stops it at the next step
   boundary. The `finally` switches back to private (`restore_attempts` tries) and reads the visibility again. A failed
   return exits 3 with a loud `REPOSITORY MAY BE PUBLIC` line. After a verified return, a timed-out or interrupted run
   is cancelled, best effort. A hard kill runs no code (docs/ci.md, known limits).
6. Write a JSON report under `report_dir` and print its path.
`ci_window.py findings --repo <tree> [--commit <sha> ...]` lists what the secret scanner finds without the secrets: it
runs the scanner with --redact and its JSON report into `findings_raw_dir`, keeps only RuleID, File, StartLine, Commit
and Date in a report under `findings_dir`, deletes the raw report on every path, and prints a table of those fields;
exit 0 none, 1 findings, 2 refused. Nothing of the raw report or the scanner's own output is printed.
Exit codes of `run`: 0 green and back as found; 1 red, cancelled, timed out or an error inside the window (returned as found);
2 refused before anything changed; 3 the final visibility is not the one found at the start.
Settings: tools/agent-workflow/ci_window_config.json, every key required (AGENTS.md section 5); the remote, branch,
forbidden paths and git timeout come from guard_config.json. Run by the orchestrator only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import secrets
import sys
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "hooks"))
import guard_config as gc

sys.path.insert(0, str(HERE))
import prepush_check as pc
import trusted_state as ts  # every read and write of trusted local state (fix round 4)

DEFAULT_CONFIG = HERE / "ci_window_config.json"
SCHEMA_VERSION = 1  # protocol-bound: the layout this module reads
SENTINEL = "pin_required"  # the AGENTS.md section 5 sentinel for an unpinned artifact
WORKFLOWS_DIR = ".github/workflows"  # protocol-bound: where GitHub Actions reads workflow files
WINDOWS_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (protocol-bound; footer rule 7)
VISIBILITIES = ("public", "private", "internal")  # protocol-bound: `gh repo edit --visibility` choices
OBJECT_ID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # protocol-bound: SHA-1 or SHA-256 object ids
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")  # protocol-bound: GitHub owner and repository names
RUN_URL = re.compile(r"/actions/runs/(\d+)(?:\D|$)")  # protocol-bound: the run URL `gh workflow run` prints
GH_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")  # protocol-bound: the status `gh` prints for a failed API call
NOT_FOUND = 404  # protocol-bound: HTTP status of a run the API does not know yet right after dispatch
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)")  # protocol-bound: workflow `uses:` syntax
PINNED = re.compile(r"@[0-9a-f]{40}$")
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_EXPOSED = 0, 1, 2, 3

INT_RANGES = {
    "run_timeout_seconds": (60, 21600),
    "poll_interval_seconds": (1, 600),
    "dispatch_lookup_timeout_seconds": (5, 600),
    "gh_timeout_seconds": (5, 600),
    "restore_attempts": (1, 5),
    "ledger_lock_timeout_seconds": (1, 120),  # the wait for another window's ledger transaction (fix round 3)
}
LIST_KEYS = ("gh_argv", "extra_forbidden_prefixes", "allowed_tracked_paths")
DIR_KEYS = ("report_dir", "findings_dir", "findings_raw_dir")
PATH_KEYS = (*DIR_KEYS, "run_ledger")  # run_ledger is a file: the run ids this tool dispatched (fix round 2)
KEYS = {"schema_version", "workflow_file", "secret_scanner", "scanner_baseline", *PATH_KEYS, *INT_RANGES, *LIST_KEYS}


class ConfigError(ValueError):
    """The window settings are missing or invalid; the message names the field."""


class Refusal(Exception):
    """A precondition failed; nothing was changed."""


class WindowError(Exception):
    """Something failed after the window may have opened; the return to the found visibility still runs.

    `http_status` is the HTTP status `gh` printed for a failed API call (None when it printed none)."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class Cancelled(Exception):
    """A recorded signal, raised by `Deferred.check` at a step boundary of the window, never by a signal handler."""


Escape = ts.Escape  # a ValueError: a link below the project root, or an object outside it


# --- settings ---------------------------------------------------------------------------------------------------


def _int(data: dict[str, Any], key: str) -> int:
    low, high = INT_RANGES[key]
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{key} must be an integer from {low} to {high}, got {value!r}")
    return value


def _strings(data: dict[str, Any], key: str, allow_empty: bool) -> list[str]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{key} must be a list of non-empty strings, got {value!r}")
    if not value and not allow_empty:
        raise ConfigError(f"{key} must not be empty")
    return list(value)


SCANNER_KEYS = {"executable", "version", "version_args", "sha256", "args"}
SCANNER_TOKENS = ("{revs}", "{repo}")


def _scanner_entry(value: Any, where: str, root: Path) -> str | dict[str, Any]:
    if value == SENTINEL:
        return SENTINEL
    if not isinstance(value, dict) or set(value) != SCANNER_KEYS:
        raise ConfigError(f"{where} must be {SENTINEL!r} or an object with exactly {', '.join(sorted(SCANNER_KEYS))}")
    exe = (root / _relative(value["executable"], f"{where}.executable")).resolve()
    try:
        exe.relative_to(root.resolve())  # after resolving links (AGENTS.md section 6)
    except ValueError:
        raise ConfigError(f"{where}.executable resolves outside the project root: {exe}") from None
    version = value["version"]
    if not isinstance(version, str) or not version or not version.isprintable() or version != version.strip():
        raise ConfigError(f"{where}.version must be a non-empty printable string without outer spaces")
    digest = value["sha256"]
    if not isinstance(digest, str) or not SHA256.match(digest.lower()):
        raise ConfigError(f"{where}.sha256 must be 64 hex characters")
    args = _strings(value, "args", allow_empty=False)
    if not any("{revs}" in a for a in args):  # a whole argument or inside one, as in `--log-opts={revs}`
        raise ConfigError(f"{where}.args must hold the token {{revs}}, replaced by the exposed commits")
    try:
        version_args = _strings(value, "version_args", allow_empty=False)
    except ConfigError as exc:
        raise ConfigError(f"{where}.{exc}") from None
    if any(t in a for a in version_args for t in SCANNER_TOKENS):
        raise ConfigError(f"{where}.version_args must not hold {' or '.join(SCANNER_TOKENS)}: it only asks the version")
    return {"path": str(exe), "version": version, "version_args": version_args, "sha256": digest.lower(),
            "args": args}  # fmt: skip


def _scanner(value: Any, root: Path) -> dict[str, str | dict[str, Any]]:
    """One entry per `sys.platform` value (the binary is per platform); each is the sentinel or a pinned binary."""
    if not isinstance(value, dict) or not value or not all(isinstance(k, str) and NAME.match(k) for k in value):
        raise ConfigError("secret_scanner must be a non-empty object keyed by sys.platform (for example win32, linux)")
    return {k: _scanner_entry(v, f"secret_scanner.{k}", root) for k, v in value.items()}


def _relative(text: Any, key: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    # Both path flavours, so `/abs` on Windows (rooted, no drive) and `C:/x` on Linux are refused alike.
    posix, windows = PurePosixPath(text), PureWindowsPath(text)
    if posix.anchor or windows.anchor or ".." in posix.parts or ".." in windows.parts:
        raise ConfigError(f"{key} must be a relative path inside the project, got {text!r}")
    return text


def validate(data: Any, root: Path = gc.ROOT) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError("ci_window config must be a JSON object")
    data = {k: v for k, v in data.items() if k != "_comment"}
    unknown, missing = sorted(set(data) - KEYS), sorted(KEYS - set(data))
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"missing required keys: {', '.join(missing)}")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:  # not True, not 1.0
        raise ConfigError(f"schema_version must be the integer {SCHEMA_VERSION}, got {data['schema_version']!r}")
    cfg: dict[str, Any] = {k: _int(data, k) for k in INT_RANGES}
    if cfg["poll_interval_seconds"] >= cfg["run_timeout_seconds"]:
        raise ConfigError("poll_interval_seconds must be below run_timeout_seconds")
    cfg["gh_argv"] = _strings(data, "gh_argv", allow_empty=False)
    for key in ("extra_forbidden_prefixes", "allowed_tracked_paths"):
        cfg[key] = [_relative(v, key) for v in _strings(data, key, allow_empty=True)]
    workflow = data["workflow_file"]
    if not isinstance(workflow, str) or not NAME.match(workflow) or not workflow.endswith((".yml", ".yaml")):
        raise ConfigError(f"workflow_file must be a .yml or .yaml file name under {WORKFLOWS_DIR}, got {workflow!r}")
    cfg["workflow_file"] = workflow
    for key in PATH_KEYS:
        cfg[key] = _relative(data[key], key)
    # The paths stay unresolved below the resolved root, so `inside` can check every component again at each use
    # (fix round 3, R2F1): this load-time check only fails early.
    cfg["root"] = root.resolve()
    cfg["paths"] = {k: cfg["root"] / cfg[k] for k in PATH_KEYS}
    if any(not p.resolve().is_relative_to(cfg["root"]) for p in cfg["paths"].values()):
        raise ConfigError(f"{', '.join(PATH_KEYS)} must stay inside the project root after resolving links")
    base = data["scanner_baseline"]
    if not isinstance(base, dict) or set(base) != {"path", "sha256"}:
        raise ConfigError("scanner_baseline must be an object with exactly path and sha256")
    if not isinstance(base["sha256"], str) or not SHA256.match(base["sha256"].lower()):
        raise ConfigError("scanner_baseline.sha256 must be 64 hex characters")
    # The path is relative to the scanned tree: the baseline is tracked there and reviewed with its history.
    cfg["scanner_baseline"] = {"path": _relative(base["path"], "scanner_baseline.path"),
                               "sha256": base["sha256"].lower()}  # fmt: skip
    cfg["secret_scanner"] = _scanner(data["secret_scanner"], root)
    return cfg


def load(path: Path, root: Path = gc.ROOT) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    try:
        return validate(data, root)
    except ConfigError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None


def write_new(root: Path, path: Path, text: str) -> None:
    """A new file at `path` (a report), created and written through a checked handle in its held folder
    (trusted_state): no link below `root` is followed, and nothing is renamed."""
    with ts.Folder(root, path.parent, create=True) as folder:
        folder.write_new(path.name, text.encode("utf-8"))


# --- child processes --------------------------------------------------------------------------------------------


def _flags() -> int:
    return WINDOWS_NO_WINDOW if sys.platform == "win32" else 0


# Every child runs in its own session (`start_new_session`, POSIX setsid; ignored on Windows, where CREATE_NO_WINDOW
# already detaches it from our console), so a terminal Ctrl+C or hangup reaches only this process, which records it,
# and never kills a `gh` call of the return to private.
CHILD = {"capture_output": True, "check": False, "start_new_session": True}


def _git(repo: Path, timeout: int, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], timeout=timeout, creationflags=_flags(), **CHILD)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"git {args[0]}: {type(exc).__name__}") from None


def _ok(proc: subprocess.CompletedProcess[bytes], what: str) -> str:
    if proc.returncode != 0:
        raise Refusal(f"{what} failed (exit {proc.returncode}): {pc._text(proc.stderr)}")
    return proc.stdout.decode("utf-8", "surrogateescape")


Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def subprocess_runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=_flags(), **CHILD
    )


class Gh:
    """Every `gh` call of the window, aimed at one `HOST/OWNER/REPO`; any unparsable answer raises WindowError."""

    def __init__(self, argv: list[str], repo: str, timeout: int, runner: Runner = subprocess_runner) -> None:
        self.argv, self.repo, self.timeout, self.runner = argv, repo, timeout, runner

    def call(self, *args: str) -> str:
        try:
            proc = self.runner([*self.argv, *args], self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WindowError(f"gh {' '.join(args[:2])}: {type(exc).__name__}") from None
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            status = GH_HTTP_STATUS.search(err)
            raise WindowError(f"gh {' '.join(args[:2])} exit {proc.returncode}: {err[:300]}",
                              int(status.group(1)) if status else None)  # fmt: skip
        return proc.stdout or ""

    def _json(self, *args: str) -> Any:
        out = self.call(*args)
        try:
            return json.loads(out)
        except ValueError:
            raise WindowError(f"gh {' '.join(args[:2])} printed no JSON: {out.strip()[:200]!r}") from None

    def rest(self, tail: str) -> Any:
        """A read-only REST GET of `repos/<owner>/<repo><tail>` on this repository's host (`gh api` defaults to GET)."""
        host, owner, name = self.repo.split("/")
        return self._json("api", "--hostname", host, f"repos/{owner}/{name}{tail}")

    def visibility(self) -> str:
        data = self._json("repo", "view", self.repo, "--json", "visibility")
        value = data.get("visibility") if isinstance(data, dict) else None
        if not isinstance(value, str) or value.casefold() not in VISIBILITIES:
            raise WindowError(f"gh repo view gave no known visibility: {data!r}")
        return value.casefold()

    def set_visibility(self, value: str) -> None:
        self.call(
            "repo", "edit", self.repo, "--visibility", value, "--accept-visibility-change-consequences"
        )

    def run_ids(self, workflow: str, sha: str) -> set[int]:
        data = self._json(
            "run", "list", "-R", self.repo, "--workflow", workflow, "--commit", sha,
            "--event", "workflow_dispatch", "--json", "databaseId",
        )  # fmt: skip
        if not isinstance(data, list) or not all(
            isinstance(r, dict) and type(r.get("databaseId")) is int for r in data
        ):
            raise WindowError(f"gh run list gave no run ids: {str(data)[:200]}")
        return {r["databaseId"] for r in data}

    def dispatch(self, workflow: str, branch: str) -> str:
        return self.call("workflow", "run", workflow, "--ref", branch, "-R", self.repo)

    def run_view(self, run_id: int) -> dict[str, Any]:
        data = self._json(
            "run", "view", str(run_id), "-R", self.repo, "--json", "headSha,event,status,conclusion,url"
        )
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("headSha", "event", "status")):
            raise WindowError(f"gh run view gave no run state: {str(data)[:200]}")
        return data

    def cancel(self, run_id: int) -> None:
        self.call("run", "cancel", str(run_id), "-R", self.repo)


# --- preconditions ----------------------------------------------------------------------------------------------


def repo_slug(url: str) -> str:
    """`HOST/OWNER/REPO` from an https, ssh or scp-like remote URL; anything else is refused."""
    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        host, path = parts.hostname or "", parts.path
        if parts.scheme not in ("https", "ssh"):
            raise Refusal(f"remote URL scheme {parts.scheme!r} is not https or ssh")
    else:
        match = re.match(r"^[^@/\s]+@([^:/\s]+):(.+)$", text)
        if not match:
            raise Refusal(f"cannot read owner and repository from the remote URL {text!r}")
        host, path = match.group(1), match.group(2)
    segments = path.strip("/").removesuffix(".git").split("/")
    if not host or len(segments) != 2 or not all(NAME.match(s) for s in segments):
        raise Refusal(f"cannot read owner and repository from the remote URL {text!r}")
    return f"{host.lower()}/{segments[0]}/{segments[1]}"


def remote_refs(repo: Path, timeout: int, remote: str) -> dict[str, str]:
    """ref -> object id of every ref on the remote, through `git ls-remote` (no local write)."""
    out = _ok(_git(repo, timeout, "ls-remote", remote), f"git ls-remote {remote}")
    refs: dict[str, str] = {}
    for line in out.splitlines():
        oid, tab, ref = line.partition("\t")
        if not tab or not OBJECT_ID.match(oid) or not ref.strip():
            raise Refusal(f"git ls-remote printed a line it cannot parse: {line[:120]!r}")
        refs[ref] = oid
    if not refs:
        raise Refusal(f"the remote {remote} has no refs; push first")
    return refs


def snapshot_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Every ref added, moved or removed between two `remote_refs` maps; empty exactly when they are equal."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    moved = sorted(r for r in set(before) & set(after) if before[r] != after[r])
    parts = [(label, refs) for label, refs in (("added", added), ("moved", moved), ("removed", removed)) if refs]
    return "; ".join(f"{label} {', '.join(refs)}" for label, refs in parts)


def local_head(repo: Path, timeout: int, branch: str) -> str:
    ref = _ok(_git(repo, timeout, "symbolic-ref", "-q", "HEAD"), "git symbolic-ref HEAD").strip()
    if ref != f"refs/heads/{branch}":
        raise Refusal(f"HEAD is {ref or 'detached'}, not refs/heads/{branch}")
    sha = _ok(_git(repo, timeout, "rev-parse", "--verify", "HEAD^{commit}"), "git rev-parse HEAD").strip().lower()
    if not OBJECT_ID.match(sha):
        raise Refusal(f"git rev-parse printed no object id: {sha[:80]!r}")
    return sha


def check_clean(repo: Path, timeout: int) -> None:
    out = _ok(_git(repo, timeout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), "git status")
    if out:
        names = [entry[3:] for entry in out.split("\0") if len(entry) > 3]
        raise Refusal(f"the working tree is not clean: {', '.join(names[:5]) or out[:80]!r}")


def exposed_paths(repo: Path, timeout: int, oids: list[str]) -> list[str]:
    """Every path any commit reachable from `oids` adds or changes (merges against each parent)."""
    out = _ok(
        _git(repo, timeout, "log", "--no-renames", "-m", "--format=", "--name-only", "--diff-filter=ACMRT", "-z",
             *oids, "--"),
        "git log over the exposed history",
    )  # fmt: skip
    return list(dict.fromkeys(n for n in out.split("\0") if n))


def check_workflow_pins(repo: Path, timeout: int, sha: str, workflow: str) -> None:
    text = _ok(_git(repo, timeout, "show", f"{sha}:{WORKFLOWS_DIR}/{workflow}"), f"{WORKFLOWS_DIR}/{workflow}")
    uses = [m.group(1) for m in map(USES.match, text.splitlines()) if m]
    if not uses:
        raise Refusal(f"{WORKFLOWS_DIR}/{workflow} names no action")
    loose = [u for u in uses if not u.startswith("./") and not PINNED.search(u)]
    if loose:
        raise Refusal(f"actions not pinned to a full commit SHA in {workflow}: {', '.join(loose)}")


# Protocol-bound to the approved scanner (gitleaks 8.30.1 `--help`): its config is read from "(target path)/.gitleaks.toml"
# and the env vars below when no --config is given, and `--gitleaks-ignore-path` defaults to "." (a .gitleaksignore
# there). Shipping our own --config would mean writing rules, so instead any of these refuses: a repository file or an
# environment variable cannot weaken the scan silently.
SCANNER_OVERRIDE_FILES = (".gitleaks.toml", ".gitleaksignore")
SCANNER_OVERRIDE_ENV = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")
REPORT_ARGS = ("--redact", "--report-format", "json", "--report-path")  # protocol-bound: gitleaks report flags
FINDING_FIELDS = (("RuleID", str), ("File", str), ("StartLine", int), ("Commit", str), ("Date", str))


def check_digest(exe: Path, pinned: str) -> None:
    """The scanner's bytes against the pin; run before and after the scanner, so what ran is what was pinned."""
    try:
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    except OSError as exc:
        raise Refusal(f"secret scanner {exe} cannot be read: {exc.strerror or exc}") from None
    if digest != pinned:
        raise Refusal(f"secret scanner {exe} has SHA-256 {digest}, not the configured {pinned}")


def parse_version(out: bytes) -> str:
    """The one line a version command prints, one line end allowed; anything else is refused (structure only)."""
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("the secret scanner's version output is not UTF-8") from None
    line = text.removesuffix("\n").removesuffix("\r")
    if not line or not line.isprintable() or line != line.strip():
        raise Refusal(f"the secret scanner's version output is not one version line ({len(out)} bytes)")
    return line


def scanner_version(exe: Path, version_args: list[str], timeout: int) -> str:
    """The version the binary itself reports; its stderr is never shown."""
    try:
        proc = subprocess.run([str(exe), *version_args], timeout=timeout, creationflags=_flags(), **CHILD)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"secret scanner version command: {type(exc).__name__}") from None
    if proc.returncode != 0:
        raise Refusal(f"secret scanner version command exit {proc.returncode}")
    return parse_version(proc.stdout)


def scanner_argv(scanners: dict[str, Any], repo: Path, oids: list[str], timeout: int,
                 platform: str = sys.platform) -> tuple[list[str], dict[str, Any]]:  # fmt: skip
    """The hash- and version-checked scanner command for `oids`, or Refusal (no entry, sentinel, override, wrong
    binary, wrong version)."""
    scanner = scanners.get(platform)
    if scanner is None or scanner == SENTINEL:
        state = "has no entry" if scanner is None else "is pin_required"
        raise Refusal(
            f"secret_scanner.{platform} {state}: place the approved gitleaks release binary for this platform under a "
            "Git-ignored project folder (decision 113, docs/ci.md), check it against the release checksums, then set "
            f"secret_scanner.{platform} to its executable, version and sha256 in "
            "tools/agent-workflow/ci_window_config.json"
        )
    present = [n for n in SCANNER_OVERRIDE_FILES if (repo / n).exists()]
    if present:
        raise Refusal(f"{', '.join(present)} in {repo} would change the scanner's rules or ignores; remove it")
    env = [n for n in SCANNER_OVERRIDE_ENV if os.environ.get(n) is not None]
    if env:
        raise Refusal(f"environment variable {', '.join(env)} would change the scanner's rules; unset it")
    exe = Path(scanner["path"])
    check_digest(exe, scanner["sha256"])
    reported = scanner_version(exe, scanner["version_args"], timeout)
    if reported != scanner["version"]:
        raise Refusal(f"secret scanner {exe} reports version {reported!r}, not the configured {scanner['version']!r}")
    revs = " ".join(oids)
    argv = [str(exe)] + [a.replace("{revs}", revs).replace("{repo}", str(repo)) for a in scanner["args"]]
    return argv, scanner


def fingerprint(row: dict[str, Any]) -> str:
    """gitleaks' own finding id, `commit:file:rule:line` (README v8.30.1: "Fingerprint: <commit>:<file>:<rule>:<line>")."""
    return f"{row['Commit'].lower()}:{row['File']}:{row['RuleID']}:{row['StartLine']}"


BASELINE_FIELDS = (("RuleID", str), ("File", str), ("StartLine", int), ("Commit", str), ("Fingerprint", str),
                   ("Reason", str), ("Decision", str))  # fmt: skip


def load_baseline(repo: Path, spec: dict[str, str], timeout: int, rev: str = "HEAD") -> tuple[Path, set[str]]:
    """The hash-pinned baseline and the fingerprints it suppresses. The pin is the SHA-256 of the committed blob
    `<rev>:<path>` (Git-normalised, so a CRLF checkout does not change it); any change, such as a new entry, changes
    it and refuses until the pin is changed in review. The window passes the scanned commit's object id as `rev`, so
    a later move of HEAD cannot change what was read. The working file must parse identical to that blob, so an
    uncommitted edit cannot widen the baseline either."""
    path = repo.resolve() / spec["path"]
    try:
        with ts.Folder(repo, path.parent) as folder:  # read through a checked handle (fix round 4)
            working = folder.read_bytes(path.name)
    except Escape as exc:
        raise Refusal(f"scanner baseline {spec['path']}: {exc}") from None
    except OSError as exc:
        raise Refusal(f"scanner baseline {path} cannot be read: {exc.strerror or exc}") from None
    if working is None:
        raise Refusal(f"scanner baseline {path} cannot be read: it does not exist")
    proc = _git(repo, timeout, "cat-file", "blob", f"{rev}:{PurePosixPath(spec['path']).as_posix()}")
    if proc.returncode != 0:
        raise Refusal(f"scanner baseline {spec['path']} is not committed at {rev}")
    digest = hashlib.sha256(proc.stdout).hexdigest()
    if digest != spec["sha256"]:
        raise Refusal(f"scanner baseline {spec['path']} has SHA-256 {digest}, not the pinned {spec['sha256']}")
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
        same = json.loads(working.decode("utf-8-sig")) == data
    except ValueError:
        raise Refusal(f"scanner baseline {spec['path']} is not JSON") from None
    if not same:
        raise Refusal(f"scanner baseline {spec['path']} in the working tree differs from {rev}; commit or restore it")
    if not isinstance(data, list):
        raise Refusal(f"scanner baseline {spec['path']} is not a JSON list")
    prints: set[str] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict) or set(item) != {f for f, _ in BASELINE_FIELDS}:
            raise Refusal(f"baseline entry {i} must have exactly {', '.join(f for f, _ in BASELINE_FIELDS)}")
        if any(type(item[f]) is not kind or (kind is str and not item[f].strip()) for f, kind in BASELINE_FIELDS):
            raise Refusal(f"baseline entry {i} has an empty or wrong-typed field")
        if not OBJECT_ID.match(item["Commit"]) or item["Fingerprint"] != fingerprint(item):
            raise Refusal(f"baseline entry {i} has no full commit id or a Fingerprint that does not match its fields")
        prints.add(item["Fingerprint"])
    return path, prints


def _report_rows(argv: list[str], repo: Path, timeout: int, raw_dir: Path,
                 root: Path) -> tuple[int, list[dict[str, Any]]]:  # fmt: skip
    """Run the scanner with --redact and its JSON report into a folder this call creates under `raw_dir`; only
    FINDING_FIELDS leave this function. Fix round 4 (R3F1): the folder chain from `root` is held for the whole scan
    (trusted_state), the report is read through a checked handle, and the cleanup deletes the entries of that same
    folder and then the folder, checked by identity. Anything it cannot remove refuses, named. Nothing of the raw file
    or of the scanner's output is shown."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        parent = ts.Folder(root, raw_dir, create=True)
    except (Escape, OSError) as exc:
        raise Refusal(f"the raw scanner report cannot be placed: {exc}") from None
    with parent:
        try:
            owned = parent.owned(f"scan-{stamp}-{os.getpid()}-{secrets.token_hex(4)}")
        except (Escape, OSError) as exc:
            raise Refusal(f"the raw scanner report cannot be placed: {exc}") from None
        try:
            result = _scan_into(owned, argv, repo, timeout)
        except Exception as exc:
            left = parent.remove_owned(owned)
            if left:
                raise Refusal(f"{exc}; and the raw scanner report could not be deleted, delete it before anything "
                              f"else: {'; '.join(left)}") from None  # fmt: skip
            raise
        except BaseException:
            parent.remove_owned(owned)
            raise
        left = parent.remove_owned(owned)
    if left:
        raise Refusal(f"the raw scanner report could not be deleted; delete it before anything else: {'; '.join(left)}")
    return result


RAW_NAME = "report.json"  # artifact-bound: the scanner's report file inside the owned folder


def _scan_into(owned: ts.Folder, argv: list[str], repo: Path, timeout: int) -> tuple[int, list[dict[str, Any]]]:
    try:
        proc = subprocess.run(
            [*argv, *REPORT_ARGS, str(owned.path / RAW_NAME)], timeout=timeout, cwd=repo, creationflags=_flags(),
            **CHILD,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"secret scanner: {type(exc).__name__}") from None
    try:
        raw = owned.read_bytes(RAW_NAME)
    except Escape as exc:
        raise Refusal(f"the raw scanner report: {exc}") from None
    except OSError:
        raw = None
    try:
        if raw is None:
            raise ValueError
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Refusal(f"secret scanner exit {proc.returncode} wrote no readable JSON report") from None
    del raw
    if not isinstance(data, list):
        raise Refusal("secret scanner report is not a JSON list")
    return proc.returncode, [_finding(item, i) for i, item in enumerate(data)]


def scan(scanners: dict[str, Any], baseline: dict[str, str], repo: Path, oids: list[str], timeout: int,
         raw_dir: Path, platform: str = sys.platform, rev: str = "HEAD", *, root: Path) -> dict[str, Any]:  # fmt: skip
    """One full run of the hash-checked scanner over `oids`; a finding is suppressed only when its fingerprint
    (`commit:file:rule:line`) is an entry of the pinned baseline, matched by the tool itself.
    Not gitleaks' `--baseline-path` (T-022 round 4, experiment): with entries holding the fingerprint fields
    (RuleID, File, StartLine, Commit, Fingerprint), the real 8.30.1 suppressed neither reviewed finding, and its README
    says only "A baseline can be any gitleaks report", not which fields it compares. Matching on more fields would
    copy commit author data and other raw-report fields into a tracked file."""
    argv, scanner = scanner_argv(scanners, repo, oids, timeout, platform)
    _, prints = load_baseline(repo, baseline, timeout, rev)
    code, full = _report_rows(argv, repo, timeout, raw_dir, root)
    check_digest(Path(scanner["path"]), scanner["sha256"])  # the bytes that ran are still the pinned ones
    if code != (1 if full else 0):
        raise Refusal(f"secret scanner exit {code} does not match its report of {len(full)} finding(s)")
    found = {fingerprint(r) for r in full}
    new = [r for r in full if fingerprint(r) not in prints]
    return {"version": scanner["version"], "all": full, "new": new, "stale": sorted(prints - found),
            "baseline": len(prints)}  # fmt: skip


def check_secrets(scanners: dict[str, Any], baseline: dict[str, str], repo: Path, oids: list[str], timeout: int,
                  raw_dir: Path, platform: str = sys.platform, rev: str = "HEAD", *,
                  root: Path) -> tuple[str, list[str]]:  # fmt: skip
    """The window's gate: refuses on any finding outside the baseline. A stale baseline entry only warns: findings are
    tied to a commit, and pushed history is never rewritten (AGENTS.md section 11), so an entry that matches nothing
    can hide nothing; refusing would block CI for a reason that exposes no secret."""
    res = scan(scanners, baseline, repo, oids, timeout, raw_dir, platform, rev, root=root)
    if res["new"]:
        listed = "; ".join(fingerprint(r) for r in res["new"][:10])
        raise Refusal(
            f"secret scanner {res['version']}: {len(res['new'])} finding(s) outside the baseline: {listed}; run "
            "`ci_window.py findings` for the table"
        )
    warnings = [
        f"scanner baseline entry no longer found (review and remove it): {fp}" for fp in res["stale"]
    ]
    text = (f"secret scanner {res['version']} (version and hash read from the binary) clean over "
            f"{len(oids)} commit(s), {len(res['all'])} finding(s) all in the baseline of {res['baseline']}")  # fmt: skip
    return text, warnings


def _finding(item: Any, index: int) -> dict[str, Any]:
    """Only the five kept fields; an error names the field, never the item's text."""
    if not isinstance(item, dict):
        raise Refusal(f"scanner report finding {index} is not an object")
    row = {}
    for field, kind in FINDING_FIELDS:
        value = item.get(field)
        if type(value) is not kind or (kind is str and not value):
            raise Refusal(f"scanner report finding {index} has no valid {field}")
        row[field] = value
    if not OBJECT_ID.match(row["Commit"].lower()):
        raise Refusal(f"scanner report finding {index} has no full commit id")
    return row


def findings_report(scanners: dict[str, Any], baseline: dict[str, str], repo: Path, oids: list[str], timeout: int,
                    raw_dir: Path, out_dir: Path, platform: str = sys.platform, *,
                    root: Path) -> tuple[Path, dict[str, Any]]:  # fmt: skip
    """`scan`, then a kept report under `out_dir` with only FINDING_FIELDS per finding, which findings are outside the
    baseline, and which baseline entries no longer occur."""
    res = scan(scanners, baseline, repo, oids, timeout, raw_dir, platform, root=root)
    new = {fingerprint(r) for r in res["new"]}
    body = {
        "scanner": res["version"], "commits": oids, "count": len(res["all"]),
        "findings": [{**r, "InBaseline": fingerprint(r) not in new} for r in res["all"]],
        "outside_baseline": sorted(new), "stale_baseline_entries": res["stale"],
    }  # fmt: skip
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    kept = out_dir / f"findings-{stamp}-{os.getpid()}.json"
    write_new(root, kept, json.dumps(body, indent=2) + "\n")
    return kept, body


def preconditions(repo: Path, cfg: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
    gg, pg, timeout = guard["git_guard"], guard["push_guard"], guard["git_timeout_seconds"]
    remote, branch = gg["push_remote"], gg["push_branch"]
    check_clean(repo, timeout)
    sha = local_head(repo, timeout, branch)
    url = _ok(_git(repo, timeout, "config", "--get", f"remote.{remote}.url"), f"remote {remote} URL").strip()
    slug = repo_slug(url)
    refs = remote_refs(repo, timeout, remote)
    tip = refs.get(f"refs/heads/{branch}")
    if tip != sha:
        raise Refusal(f"local HEAD {sha} is not {remote}/{branch} ({tip or 'absent'}); push it first")
    oids = sorted(set(refs.values()))
    missing = [o for o in oids if _git(repo, timeout, "cat-file", "-e", o).returncode != 0]
    if missing:
        raise Refusal(f"remote refs name commits that are not local, so they cannot be checked: {missing[:5]}")
    allowed = set(cfg["allowed_tracked_paths"])
    prefixes = [*pg["forbidden_path_prefixes"], *cfg["extra_forbidden_prefixes"]]
    paths = exposed_paths(repo, timeout, oids)
    bad = [p for p in pc.forbidden_paths(paths, prefixes, pg["forbidden_suffixes"]) if p not in allowed]
    if bad:
        raise Refusal(f"forbidden paths in the history that would become public: {', '.join(bad[:10])}")
    check_workflow_pins(repo, timeout, sha, cfg["workflow_file"])
    text, warnings = check_secrets(cfg["secret_scanner"], cfg["scanner_baseline"], repo, oids,
                                   pg["check_timeout_seconds"], cfg["paths"]["findings_raw_dir"], rev=sha,
                                   root=cfg["root"])  # fmt: skip
    return {"commit": sha, "repository": slug, "remote": remote, "branch": branch, "refs": refs, "scan": text,
            "warnings": warnings}  # fmt: skip


def recheck(repo: Path, timeout: int, found: dict[str, Any]) -> None:
    """The last check while still private: the remote refs and local HEAD are still exactly what was scanned. A push
    after this read is the race no local check closes (docs/ci.md, single writer)."""
    diff = snapshot_diff(found["refs"], remote_refs(repo, timeout, found["remote"]))
    if diff:
        raise Refusal(f"the remote refs changed since the scan ({diff}); nothing was made public, run the window again")
    head = local_head(repo, timeout, found["branch"])
    if head != found["commit"]:
        raise Refusal(f"local HEAD moved from {found['commit']} to {head} since the scan; nothing was made public")


# --- the non-Git surfaces that become public with the repository (fix round 2, RT1, docs/ci.md) ------------------
# Protocol-bound to the GitHub REST API: the paths, `per_page` (max 100) and the fields below. The gate is a PM decision
# pending user review: the wiki, discussions and Pages are off; no release, issue or pull request exists; every
# retained workflow run and artifact belongs to a run this tool dispatched (the run ledger). Nothing is ever deleted.

PAGE = 100  # protocol-bound: the REST maximum per_page
LEDGER_SCHEMA = 1
REPO_FLAGS = ("has_wiki", "has_discussions", "has_pages")  # each must be false; the tool never changes them
ABSENT = (("/releases?per_page=1", "a release (published or draft)"),
          ("/issues?state=all&per_page=1", "an issue or pull request"))  # fmt: skip


def _rest(gh: Gh, tail: str) -> Any:
    """A REST read whose failure (missing permission, API error, no JSON) refuses: nothing has changed yet."""
    try:
        return gh.rest(tail)
    except WindowError as exc:
        raise Refusal(f"cannot prove what would become public: {exc}") from None


def page_items(data: Any, key: str, what: str) -> tuple[int, list[dict[str, Any]]]:
    """One page of a counted REST list: `total_count` (integer >= 0) and `key`, a list of objects with an integer id."""
    total = data.get("total_count") if isinstance(data, dict) else None
    items = data.get(key) if isinstance(data, dict) else None
    if type(total) is not int or total < 0 or not isinstance(items, list):
        raise Refusal(f"{what}: the response has no valid total_count and {key} list")
    if not all(isinstance(i, dict) and type(i.get("id")) is int for i in items):
        raise Refusal(f"{what}: an item has no integer id")
    return total, items


def counted(gh: Gh, path: str, key: str, what: str) -> list[dict[str, Any]]:
    """Every item of a paged list, proven complete: each page reports the same total_count, no id repeats, and the
    items collected equal that count. A list that moves while it is read, or ends short, refuses."""
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    total: int | None = None
    page = 1
    while total is None or len(items) < total:
        count, batch = page_items(_rest(gh, f"{path}?per_page={PAGE}&page={page}"), key, what)
        if total is not None and count != total:
            raise Refusal(f"{what}: total_count changed from {total} to {count} while reading; run the window again")
        total = count
        for item in batch:
            if item["id"] in seen:
                raise Refusal(f"{what}: id {item['id']} is listed twice; the list moved while reading")
            seen.add(item["id"])
            items.append(item)
        if not batch:
            break
        page += 1
    if len(items) != total:
        raise Refusal(f"{what}: {len(items)} listed but total_count is {total}; a truncated list proves nothing")
    return items


def inventory(gh: Gh, known: set[int]) -> str:
    """The last read while private: refuses unless every non-Git surface is off, empty or the tool's own runs."""
    repo = _rest(gh, "")
    if not isinstance(repo, dict) or any(type(repo.get(f)) is not bool for f in REPO_FLAGS):
        raise Refusal(f"the repository metadata has no boolean {', '.join(REPO_FLAGS)}")
    on = [f for f in REPO_FLAGS if repo[f]]
    if on:
        raise Refusal(f"{', '.join(on)} is on, so that content would become public; turn it off in the repository "
                      "settings (the tool never changes them)")  # fmt: skip
    for tail, label in ABSENT:
        data = _rest(gh, tail)
        if not isinstance(data, list):
            raise Refusal(f"the listing for {label} is not a JSON list")
        if data:
            raise Refusal(f"{label} exists and would become public; the window refuses (the tool never deletes it)")
    runs = counted(gh, "/actions/runs", "workflow_runs", "workflow runs")
    unknown = sorted(r["id"] for r in runs if r["id"] not in known)
    if unknown:
        raise Refusal(f"workflow runs not dispatched by this tool would show their logs: {unknown[:10]}; the window "
                      "refuses while they are retained (the tool never deletes them)")  # fmt: skip
    artifacts = counted(gh, "/actions/artifacts", "artifacts", "artifacts")
    for art in artifacts:
        run = art.get("workflow_run")
        if not isinstance(run, dict) or type(run.get("id")) is not int:
            raise Refusal(f"artifact {art['id']} names no workflow run, so it cannot be proven to be the tool's own")
        if run["id"] not in known:
            raise Refusal(f"artifact {art['id']} belongs to run {run['id']}, which this tool did not dispatch")
    return f"no wiki, discussions, Pages, release, issue or pull request; {len(runs)} run(s) and {len(artifacts)} " \
           "artifact(s), all from runs this tool dispatched"


LEDGER_LOCK_SUFFIX = ".lock"  # artifact-bound: the lock file beside the run ledger, for its readers and writers


def parse_ledger(text: str) -> dict[str, Any]:
    """The ledger document `{"schema_version": 1, "runs": {repository: [run ids]}}`, or Refusal. Integers are exact:
    `True` and `1.0` are not 1 (fix round 3, R2F3), and every run id is an `int`, never a bool, float or text."""
    try:
        data = json.loads(text)
    except ValueError:
        raise Refusal("the run ledger is not JSON; repair it before the next window") from None
    runs = data.get("runs") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != LEDGER_SCHEMA or not isinstance(runs, dict)
            or not all(isinstance(v, list) and all(type(i) is int for i in v) for v in runs.values())):  # fmt: skip
        raise Refusal("the run ledger is not {schema_version: 1, runs: {repository: [run ids]}}; repair it")
    return data


def _ledger_doc(raw: bytes | None) -> dict[str, Any]:
    """The validated ledger from its bytes. No file, or an empty one (created by the lock-holding writer and not yet
    written, or cut off by a crash), is the empty ledger, which fails closed: any retained run refuses."""
    if not raw:
        return {"schema_version": LEDGER_SCHEMA, "runs": {}}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("the run ledger is not UTF-8; repair it before the next window") from None
    return parse_ledger(text)


def load_ledger(root: Path, path: Path, slug: str, timeout: int) -> set[int]:
    """The run ids this tool dispatched for `slug`, read through a checked handle under `<ledger>.lock` (fix rounds 3
    and 4). No folder yet means none, so any retained run refuses."""
    try:
        with ts.Folder(root, path.parent) as folder, folder.locked(path.name + LEDGER_LOCK_SUFFIX, timeout):
            data = _ledger_doc(folder.read_bytes(path.name))
    except FileNotFoundError:
        return set()  # the folder does not exist yet: nothing was ever recorded
    except (Escape, OSError) as exc:
        raise Refusal(f"the run ledger {path} cannot be read: {exc}") from None
    except Refusal as exc:
        raise Refusal(f"{exc} ({path})") from None
    return set(data["runs"].get(slug, []))


def record_run(root: Path, path: Path, slug: str, run_id: int, timeout: int) -> None:
    """Add a dispatched run as soon as it is known: under `<ledger>.lock`, one read, validate, merge and in-place
    write through the same checked handle (trusted_state; a held folder cannot take an `os.replace`). A failure is a
    window error (PM decision): the window returns to private, and the next one refuses on the unknown run until the
    ledger is repaired."""
    try:
        with (ts.Folder(root, path.parent, create=True) as folder,
              folder.locked(path.name + LEDGER_LOCK_SUFFIX, timeout),
              folder.open_file(path.name, "u") as handle):  # fmt: skip
            data = _ledger_doc(handle.read())
            ids = data["runs"].setdefault(slug, [])
            if run_id not in ids:
                ids.append(run_id)
            ts.rewrite(handle, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    except (OSError, ValueError, Refusal) as exc:  # Escape is a ValueError
        raise WindowError(f"run {run_id} could not be recorded in the run ledger {path}: {exc}; the next window "
                          "refuses until the ledger is repaired") from None  # fmt: skip


# --- the window -------------------------------------------------------------------------------------------------


# Protocol-bound (CPython `signal` docs): the catchable requests to stop, each used where this platform defines it
# (Windows: SIGINT, SIGTERM, SIGBREAK; POSIX: SIGINT, SIGTERM, SIGHUP, SIGQUIT). This table is the only platform
# difference of the signal handling.
SIGNALS = ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP", "SIGQUIT")


class Deferred:
    """Record signals, never raise from them. CPython runs a Python handler "at a later point (for example at the next
    bytecode instruction)", and an exception from it "may be raised after any bytecode instruction" (signal docs), so a
    raising handler can cut any line of the return to private short (review F1). Here the handler only appends the
    signal number; the window calls `check` at its step boundaries, where a plain `Cancelled` is raised. Installed
    before the first change and kept until the report is out, there is no point in between where a signal raises."""

    def __init__(self) -> None:
        self.received: list[int] = []
        self._saved: list[tuple[int, Any]] = []

    def _record(self, signum: int, frame: Any) -> None:
        self.received.append(signum)

    def __enter__(self) -> Deferred:
        try:
            for name in SIGNALS:
                if hasattr(signal, name):
                    num = getattr(signal, name)
                    self._saved.append((num, signal.signal(num, self._record)))
        except BaseException:  # nothing is owed yet; put back what was replaced and stop
            self._restore()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._restore()

    def _restore(self) -> None:
        while self._saved:  # SIGINT was installed first, so it is put back last
            num, old = self._saved.pop()
            signal.signal(num, signal.SIG_DFL if old is None else old)  # None: a handler not set from Python

    def names(self) -> list[str]:
        return [signal.Signals(n).name for n in self.received]

    def check(self, step: str) -> None:
        if self.received:
            raise Cancelled(f"signal {', '.join(self.names())} received; stopped before {step}")


def find_run(gh: Gh, cfg: dict[str, Any], sha: str, before: set[int], printed: str, clock: Callable[[], float],
             sleep: Callable[[float], None], cancel: Deferred) -> int:  # fmt: skip
    found = {int(m) for m in RUN_URL.findall(printed)}
    if len(found) == 1:
        return found.pop()
    if found:
        raise WindowError(f"gh workflow run printed several run URLs: {sorted(found)}")
    deadline = clock() + cfg["dispatch_lookup_timeout_seconds"]
    while True:
        cancel.check("the run lookup")
        new = gh.run_ids(cfg["workflow_file"], sha) - before
        if len(new) == 1:
            return new.pop()
        if len(new) > 1:
            raise WindowError(f"several new dispatched runs for {sha}: {sorted(new)}; cannot tell which is ours")
        if clock() >= deadline:
            raise WindowError(f"no dispatched run for {sha} appeared within {cfg['dispatch_lookup_timeout_seconds']} s")
        sleep(min(cfg["poll_interval_seconds"], cfg["dispatch_lookup_timeout_seconds"]))


def watch(gh: Gh, cfg: dict[str, Any], run_id: int, sha: str, clock: Callable[[], float],
          sleep: Callable[[float], None], report: dict[str, Any], cancel: Deferred) -> str:  # fmt: skip
    start = clock()
    deadline = start + cfg["run_timeout_seconds"]
    seen = False
    while True:
        cancel.check("the next poll")
        try:
            view = gh.run_view(run_id)
        except WindowError as exc:
            # Right after dispatch the API may not know the run yet (CI 35: `HTTP 404: Not Found` on the first view).
            # Until the run has been seen once, a 404 is retried within the dispatch lookup window; any other
            # failure, and a 404 after the run was seen or past that window, is raised unchanged.
            if seen or exc.http_status != NOT_FOUND or clock() >= start + cfg["dispatch_lookup_timeout_seconds"]:
                raise
            sleep(min(cfg["poll_interval_seconds"], cfg["dispatch_lookup_timeout_seconds"]))
            continue
        seen = True
        if view["headSha"].lower() != sha or view["event"] != "workflow_dispatch":
            raise WindowError(f"run {run_id} is {view['event']} on {view['headSha']}, not workflow_dispatch on {sha}")
        report["run"].update(status=view["status"], conclusion=view.get("conclusion"), url=view.get("url"))
        if view["status"] == "completed":
            conclusion = view.get("conclusion")
            return conclusion if isinstance(conclusion, str) and conclusion else "unknown"
        if clock() >= deadline:
            return "timeout"
        sleep(cfg["poll_interval_seconds"])


def restore(gh: Gh, target: str, attempts: int, report: dict[str, Any]) -> bool:
    """Back to `target`, then read it again; True only when the read shows `target`. Any failure of an attempt is
    recorded and the next attempt runs, so nothing escapes before every configured attempt was made."""
    for attempt in range(1, attempts + 1):
        try:
            gh.set_visibility(target)
        except Exception as exc:  # noqa: BLE001 - a runner bug must not end the return to private
            report["errors"].append(f"restore attempt {attempt}: {type(exc).__name__} {exc}")
        try:
            final = gh.visibility()
        except Exception as exc:  # noqa: BLE001 - as above
            report["errors"].append(f"verify attempt {attempt}: {type(exc).__name__} {exc}")
            continue
        report["final_visibility"] = final
        if final == target:
            return True
    return False


def open_window(repo: Path, cfg: dict[str, Any], guard: dict[str, Any], runner: Runner = subprocess_runner,
                clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                pre: Callable[..., dict[str, Any]] = preconditions) -> tuple[int, dict[str, Any]]:  # fmt: skip
    report: dict[str, Any] = {
        "tool": "ci_window", "started_at": _now(), "commit": None, "repository": None, "initial_visibility": None,
        "made_public": False, "run": None, "result": None, "final_visibility": None, "errors": [], "warnings": [],
    }  # fmt: skip
    # Before anything can change: from here to the return below, a signal is only recorded (Deferred). A signal while
    # the handlers are being installed may still raise, but then nothing has changed yet.
    with Deferred() as cancel:
        code = _window(repo, cfg, guard, runner, clock, sleep, pre, cancel, report)
        report["signals"] = cancel.names()
        report["finished_at"] = _now()
    return code, report


def _window(repo: Path, cfg: dict[str, Any], guard: dict[str, Any], runner: Runner, clock: Callable[[], float],
            sleep: Callable[[float], None], pre: Callable[..., dict[str, Any]], cancel: Deferred,
            report: dict[str, Any]) -> int:  # fmt: skip
    try:
        found = pre(repo, cfg, guard)
        cancel.check("the visibility read")
    except Refusal as exc:
        report.update(result="refused", errors=[str(exc)])
        return EXIT_REFUSED
    except Cancelled as exc:  # nothing was changed
        report.update(result="interrupted", errors=[str(exc)])
        return EXIT_FAILED
    report.update(commit=found["commit"], repository=found["repository"], preconditions=found)
    report["warnings"].extend(found.get("warnings", []))
    gh = Gh(cfg["gh_argv"], found["repository"], cfg["gh_timeout_seconds"], runner)
    sha, timeout = found["commit"], guard["git_timeout_seconds"]
    opened = False
    code = EXIT_FAILED
    try:
        initial = gh.visibility()
        report["initial_visibility"] = initial
        if initial == "internal":
            raise WindowError("the repository is internal; the window only handles private or public")
        if initial == "public":
            report["warnings"].append(
                "THE REPOSITORY WAS ALREADY PUBLIC at the start and is left public as found; it should be "
                "private (S35): make it private when no run needs it"
            )
        else:
            recheck(repo, timeout, found)  # still private: a changed snapshot refuses here (review F2)
            known = load_ledger(cfg["root"], cfg["paths"]["run_ledger"], found["repository"],
                                cfg["ledger_lock_timeout_seconds"])  # fmt: skip
            report["inventory"] = inventory(gh, known)  # the non-Git surfaces (RT1)
            # After the last private read, not before it (RT2): a signal recorded during any of the reads above stops
            # here with nothing changed. One arriving after this line is still caught by the recorder and the return.
            cancel.check("the switch to public")
            opened = True  # set before the call: a timed-out switch may still have happened
            gh.set_visibility("public")
            report["made_public"] = True
        cancel.check("the dispatch")
        try:
            diff = snapshot_diff(found["refs"], remote_refs(repo, timeout, found["remote"]))
        except Refusal as exc:
            raise WindowError(str(exc)) from None
        if diff:  # a push raced the switch (single-writer rule broken): stop and return at once
            raise WindowError(f"the remote refs moved after the scan, before dispatch: {diff}")
        before = gh.run_ids(cfg["workflow_file"], sha)
        cancel.check("the dispatch")
        printed = gh.dispatch(cfg["workflow_file"], found["branch"])
        run_id = find_run(gh, cfg, sha, before, printed, clock, sleep, cancel)
        report["run"] = {"id": run_id}
        # Before watching, so an interrupted run is recorded too.
        record_run(cfg["root"], cfg["paths"]["run_ledger"], found["repository"], run_id,
                   cfg["ledger_lock_timeout_seconds"])  # fmt: skip
        result = watch(gh, cfg, run_id, sha, clock, sleep, report, cancel)
        report["result"] = result
        code = EXIT_OK if result == "success" else EXIT_FAILED
    except Refusal as exc:  # only `recheck`, before any change
        report.update(result="refused")
        report["errors"].append(str(exc))
        code = EXIT_REFUSED
    except WindowError as exc:
        report.update(result="error")
        report["errors"].append(str(exc))
    except (KeyboardInterrupt, Cancelled) as exc:  # KeyboardInterrupt only from a caller's code, never a signal here
        report.update(result="interrupted")
        report["errors"].append(f"interrupted: {type(exc).__name__} {exc}")
    except Exception as exc:  # noqa: BLE001 - any failure must still reach the return below
        report.update(result="error")
        report["errors"].append(f"unexpected {type(exc).__name__}: {exc}")
    finally:
        if opened and not restore(gh, "private", cfg["restore_attempts"], report):
            code = EXIT_EXPOSED
            report["errors"].append("REPOSITORY MAY BE PUBLIC: the return to private was not confirmed")
        elif not opened and report["initial_visibility"] is not None:
            report["final_visibility"] = report["initial_visibility"]
        # Only after the visibility is settled; a failure is reported and never changes the exit code.
        if code != EXIT_EXPOSED and report["result"] in ("timeout", "interrupted") and report["run"]:
            try:
                gh.cancel(report["run"]["id"])
                report["run"]["cancelled"] = True
            except Exception as exc:  # noqa: BLE001 - best effort
                report["errors"].append(f"cancel after {report['result']}: {type(exc).__name__} {exc}")
    return code


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_report(root: Path, cfg: dict[str, Any], report: dict[str, Any]) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = root.resolve() / cfg["report_dir"] / f"{stamp}-{(report.get('commit') or 'none')[:12]}-{os.getpid()}.json"
    write_new(root, path, json.dumps(report, indent=2) + "\n")  # Escape (a ValueError) on a link
    return path


def findings_main(repo: Path, commits: list[str] | None, cfg: dict[str, Any], guard: dict[str, Any], root: Path) -> int:
    """`findings`: exit 0 with no finding, 1 with findings (table printed, report kept), 2 refused."""
    timeout = guard["git_timeout_seconds"]
    try:
        if commits:
            oids = sorted({c.lower() for c in commits})
            bad = [c for c in oids if not OBJECT_ID.match(c) or _git(repo, timeout, "cat-file", "-e", c).returncode]
            if bad:
                raise Refusal(f"--commit needs full local commit ids: {bad[:5]}")
        else:
            oids = sorted(set(remote_refs(repo, timeout, guard["git_guard"]["push_remote"]).values()))
        # Each folder is opened through trusted_state at its use; Escape (a ValueError) on a link.
        dirs = [root.resolve() / cfg[k] for k in ("findings_raw_dir", "findings_dir")]
        kept, body = findings_report(cfg["secret_scanner"], cfg["scanner_baseline"], repo, oids,
                                     guard["push_guard"]["check_timeout_seconds"], dirs[0], dirs[1],
                                     root=root)  # fmt: skip
    except (Refusal, ValueError, OSError) as exc:
        sys.stderr.write(f"ci_window: findings refused: {exc}\n")
        return EXIT_REFUSED
    print(f"{'RuleID':<28} {'File':<60} {'Line':>6}  {'Commit':<12}  Baseline")
    for r in body["findings"]:
        mark = "yes" if r["InBaseline"] else "NO"
        print(f"{r['RuleID']:<28} {r['File']:<60} {r['StartLine']:>6}  {r['Commit'][:12]}  {mark}")
    for fp in body["stale_baseline_entries"]:
        sys.stderr.write(f"ci_window: WARNING: scanner baseline entry no longer found: {fp}\n")
    print(f"ci_window: {body['count']} finding(s), {len(body['outside_baseline'])} outside the baseline, over "
          f"{len(oids)} commit(s); report {kept}")  # fmt: skip
    return EXIT_FAILED if body["outside_baseline"] else EXIT_OK


def main(argv: list[str] | None = None, root: Path = gc.ROOT, runner: Runner = subprocess_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="open one visibility window for the pushed HEAD")
    run.add_argument("--repo", required=True, help="the main working tree whose pushed HEAD runs")
    fnd = sub.add_parser("findings", help="list what the secret scanner finds, without the secret values")
    fnd.add_argument("--repo", required=True, help="the working tree to scan")
    fnd.add_argument(
        "--commit", action="append", help="scan the history of this full commit id (repeatable) instead of every "
        "remote ref read with git ls-remote",
    )  # fmt: skip
    for p in (run, fnd):
        p.add_argument("--config", help="window settings (default tools/agent-workflow/ci_window_config.json)")
        p.add_argument("--guard-config", help="guard config (default tools/agent-workflow/guard_config.json)")
    args = parser.parse_args(argv)
    try:
        cfg = load(Path(args.config) if args.config else DEFAULT_CONFIG, root)
        guard = gc.load(gc.resolve_path(args.guard_config))
    except (ConfigError, gc.ConfigError) as exc:
        sys.stderr.write(f"ci_window: refused: {exc}\n")
        return EXIT_REFUSED
    if args.command == "findings":
        return findings_main(Path(args.repo), args.commit, cfg, guard, root)
    # An outer recorder, so the window's own one is replaced by a recorder again when it ends, and no signal can cut
    # the report or the exit-3 message short. The report is written first: stderr may be a closed terminal (SIGHUP).
    with Deferred():
        code, report = open_window(Path(args.repo), cfg, guard, runner)
        try:
            path: Path | None = write_report(root, cfg, report)
        except (OSError, ValueError) as exc:
            path, code = None, code or EXIT_FAILED
            _say(f"ci_window: cannot write the report: {exc}\n{json.dumps(report)}\n")
        for warning in report["warnings"]:
            _say(f"ci_window: WARNING: {warning}\n")
        for error in report["errors"]:
            _say(f"ci_window: {'REFUSED' if code == EXIT_REFUSED else 'ERROR'}: {error}\n")
        if path is not None:
            _say(f"ci_window: result {report['result']}, exit {code}, report {path}\n", sys.stdout)
        if code == EXIT_EXPOSED:
            _say("ci_window: REPOSITORY MAY BE PUBLIC. Make it private now: gh repo edit "
                 f"{report['repository']} --visibility private --accept-visibility-change-consequences\n")  # fmt: skip
    return code


def _say(text: str, stream: Any = None) -> None:
    """A line for the operator; a closed terminal (after SIGHUP) must not change the exit code the window decided."""
    try:
        (stream or sys.stderr).write(text)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
