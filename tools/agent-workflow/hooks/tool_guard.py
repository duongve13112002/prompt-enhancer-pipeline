"""PreToolUse guard for shell tools (Bash, PowerShell, Monitor): P-01, P-03, P-05, P-12, P-13, P-15.

Checks (each with a mode in guard_config.json: block, warn or off):
- tool_broken: a tool flagged by the harness-error streak (tool_monitor.py) is blocked until reset.
- write_guard: shell writes only under the configured allowlist (today `.tmp/`, `runs/`) of the innermost work tree.
- git_guard: `--git-mode pm` blocks force pushes and pushing aliases; `worker` allows read-only git only;
  `worker-merge --worktree D` also allows `merge` and `add` inside that worktree.
- push_guard (`--git-mode pm`): `git push` is blocked unless `<marker_dir>/<full HEAD id of the pushing repository>`
  exists (written by prepush_mark.py after the lint is clean); `--dry-run`, an undeterminable repository and the
  check's own errors pass.
- inline_interp: no multi-line or long `python -c`/`node -e`, no interpreter reading its program from stdin.
- pytest_slot: pytest runs only through a configured launcher (test_slot.py).
- read_router (warn): file reads, Select-String and recursive listings that the Read/Grep/Glob tools do better.

Failure policy: a tool other than the shell tools is never blocked; unreadable event JSON passes; a broken config
blocks only the shell tools (so Edit can repair it); a check that crashes blocks only when the command carries that
check's trigger (a write verb, `git`, an interpreter, `pytest`), else warns and logs to `guard-crash.log`.
Exit 2 blocks with the reason on stderr; warnings go to `additionalContext`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import traceback
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_config as gc
import process_guard as pg
import shell_targets as st
import tool_health as th

SHELL_TOOLS = {
    "Bash",
    "PowerShell",
    "Monitor",
}  # protocol-bound: tools whose input is a shell `command`
NEVER_BLOCKED = {
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
}  # safety-bound: the repair path always stays open
GIT_MODES = ("pm", "worker", "worker-merge")
LOG = pg.STATE_DIR / "tool-guard.log"
CRASH_LOG = pg.STATE_DIR / "guard-crash.log"

GIT_TRIGGER = re.compile(r"(?i)git")
HEAD_MOVERS = {
    *("commit", "merge", "pull", "rebase", "reset", "cherry-pick", "am", "revert"),
    *("checkout", "switch", "stash", "bisect", "update-ref", "symbolic-ref"),
    *("filter-branch", "fast-import"),
}  # git subcommands that can move HEAD (detection coverage, safety-bound)
# Built-in subcommands that cannot move HEAD. Every other name may be a user alias (`git co` = `git checkout`) or an
# external `git-<name>` program, so it counts as possibly moving HEAD (fail closed, safety-bound).
NON_MOVING_GIT = {
    *("add", "annotate", "apply", "archive", "blame", "branch", "bundle", "cat-file", "check-attr"),
    *(
        "check-ignore",
        "check-ref-format",
        "cherry",
        "clean",
        "clone",
        "commit-graph",
        "commit-tree",
    ),
    *("config", "count-objects", "credential", "describe", "diff", "diff-files", "diff-index"),
    *("diff-tree", "difftool", "fetch", "for-each-ref", "format-patch", "fsck", "gc", "grep"),
    *("hash-object", "help", "init", "log", "ls-files", "ls-remote", "ls-tree", "maintenance"),
    *("merge-base", "mktree", "mv", "name-rev", "notes", "pack-objects", "prune", "push"),
    *("range-diff", "reflog", "remote", "repack", "restore", "rev-list", "rev-parse", "rm"),
    *("shortlog", "show", "show-ref", "sparse-checkout", "status", "submodule", "tag"),
    *("update-index", "var", "verify-commit", "verify-tag", "version", "whatchanged"),
    *("worktree", "write-tree", "stage", "show-branch", "merge-tree", "check-mailmap", "column"),
    *("interpret-trailers", "patch-id", "stripspace", "pack-refs", "for-each-repo", "bugreport"),
    *("diagnose", "index-pack", "verify-pack", "show-index", "request-pull", "fast-export"),
    *("replace", "refs", "hook", "send-email", "rerere", "update-server-info"),
}
HEAD_MOVING_FLAGS = {
    *("-b", "-B", "--orphan", "-d", "--detach", "--hard", "--soft"),
    *("--mixed", "--keep", "--merge"),
}  # options that make `checkout`/`reset` move HEAD even beside `--`
PUSH_VALUE_OPTIONS = (
    "--push-option",
    "--receive-pack",
    "--exec",
    "--repo",
)  # git-push(1) options taking a value
COMMIT_ID = re.compile(
    r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
)  # protocol-bound: SHA-1 or SHA-256 object id
PYTEST_TRIGGER = re.compile(r"(?i)py\.?test")
PYTEST_HEADS = {"pytest", "py.test"}
# Programs that run another command named by their arguments (`uv run pytest`): detection coverage, safety-bound.
RUNNERS = {
    "uv",
    "uvx",
    "poetry",
    "pipenv",
    "hatch",
    "pdm",
    "rye",
    "pipx",
    "conda",
    "mamba",
    "micromamba",
    "nox",
}
LINE_BREAKS = (
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\N{LINE SEPARATOR}",
    "\N{PARAGRAPH SEPARATOR}",
)  # str.splitlines set
GIT_WRITE_WORDS = re.compile(
    r"(?i)\b(commit|push|reset|checkout|switch|restore|merge|rebase|stash|cherry-pick|revert|am|apply|clean|rm|mv"
    r"|add|tag|branch|update-ref|update-index|symbolic-ref|commit-tree|hash-object|write-tree|read-tree|config"
    r"|worktree|notes|replace|fast-import|filter-branch|gc|prune|pull|fetch|clone|init|remote|submodule)\b"
)
GIT_ENV = re.compile(
    r"(?i)(?<![\w$.])GIT_[A-Z0-9_]+\s*=|\benv:\s*GIT_|SetEnvironmentVariable\s*\(\s*['\"]GIT_"
    r"|\b(?:export|set|setx)\s+GIT_"
)
ALIAS_HEADS = {"alias", "set-alias", "sal", "new-alias", "nal", "doskey", "function", "filter"}

# git global options (git(1)): print-only or harmless, value-taking, and repository-redirecting.
GIT_SAFE_GLOBALS = {
    *(
        "--no-pager",
        "-P",
        "-p",
        "--paginate",
        "--no-optional-locks",
        "--literal-pathspecs",
        "--glob-pathspecs",
    ),
    *(
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-advice",
    ),
    *("--version", "--help", "-h", "--exec-path", "--html-path", "--man-path", "--info-path"),
}
GIT_REDIRECTING = (
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--exec-path",
    "--super-prefix",
    "--bare",
)
GIT_VALUE_GLOBALS = {
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--super-prefix",
    "--list-cmds",
    "--attr-source",
}
MERGE_STRATEGIES = {
    "ort",
    "recursive",
    "resolve",
    "octopus",
    "ours",
    "subtree",
}  # git-merge(1) built-ins
CONFIG_READ_FLAGS = {
    *(
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--get-color",
        "--get-colorbool",
        "--list",
        "-l",
    ),
    *(
        "--show-origin",
        "--show-scope",
        "--name-only",
        "-z",
        "--null",
        "--global",
        "--system",
        "--local",
    ),
    *("--worktree", "--includes", "--no-includes", "--bool", "--int", "--bool-or-int", "--path"),
    *("--expiry-date", "--no-type", "--all", "--regexp", "--url"),
}
CONFIG_VALUE_FLAGS = {"--file", "-f", "--blob", "--type", "--default", "--value"}
CONFIG_WRITE_SUBS = {"set", "unset", "rename-section", "remove-section", "edit"}
LIST_FLAGS = {
    *(
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-l",
        "--list",
        "-v",
        "-vv",
        "--verbose",
        "--show-current",
        "--color",
    ),
    *(
        "--no-color",
        "--column",
        "--no-column",
        "-i",
        "--ignore-case",
        "--abbrev",
        "--no-abbrev",
        "--omit-empty",
    ),
    *("-q", "--quiet", "--verify"),
}
LIST_VALUE_FLAGS = {
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--points-at",
    "--sort",
    "--format",
}
REFLOG_WRITES = {"expire", "delete", "drop"}

READ_HEADS = {"get-content", "gc", "cat", "type"}
READ_FILTERS = {
    *(
        "select-object",
        "select",
        "select-string",
        "sls",
        "where-object",
        "where",
        "?",
        "head",
        "tail",
        "grep",
    ),
    *(
        "format-table",
        "ft",
        "format-list",
        "fl",
        "format-wide",
        "fw",
        "out-string",
        "more",
        "less",
        "sort",
    ),
    *("out-host", "oh", "findstr", "sort-object"),
}
LISTING_HEADS = {"get-childitem", "gci", "dir", "ls", "childitem"}
DOTNET_READ = re.compile(r"\[\s*(?:System\.)?IO\.File\s*\]\s*::\s*ReadAll(?:Text|Lines)\b", re.I)
DOTNET_READ_CAPTURED = re.compile(
    r"(?:\$[\w:]+\s*=|\(|\|)\s*\[\s*(?:System\.)?IO\.File\s*\]\s*::\s*ReadAll(?:Text|Lines)\b", re.I
)


class Context:
    def __init__(
        self, event: dict[str, Any], cfg: dict[str, Any], args: argparse.Namespace, root: Path
    ) -> None:
        self.cfg, self.args, self.root = cfg, args, root
        self.tool = str(event.get("tool_name"))
        tool_input = event.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        self.command = command if isinstance(command, str) else ""
        self.session = event.get("session_id") if isinstance(event.get("session_id"), str) else None
        cwd = event.get("cwd")
        self.cwd = Path(cwd if isinstance(cwd, str) and cwd else os.getcwd())
        self.trigger_text = st.trigger_text(self.command)
        self._walk: st.Walk | None = None
        self._writes: st.Writes | None = None
        self._opaque: list[tuple[str, str]] | None = None

    def walk(self) -> st.Walk:
        if self._walk is None:
            self._walk = st.walk(self.command, self.tool, self.cfg["pytest_slot"]["launchers"])
        return self._walk

    def writes(self) -> st.Writes:
        if self._writes is None:
            self._writes = st.write_targets(self.walk())
        return self._writes

    def opaque(self) -> list[tuple[str, str]]:
        if self._opaque is None:
            self._opaque = st.opaque_constructs(self.walk())
        return self._opaque

    def bases(self) -> list[Path] | None:
        return st.cwd_candidates(self.cwd, self.writes().cwd_changes, st.shell_of(self.tool))


Result = tuple[list[str], list[str]]  # (block reasons, warnings)


def check_tool_broken(ctx: Context) -> list[str]:
    tb = ctx.cfg["tool_broken"]
    directory = th.state_dir(ctx.cfg, ctx.root)
    out = []
    for tool in dict.fromkeys([ctx.tool, tb["dependent_tools"].get(ctx.tool, ctx.tool)]):
        entry = th.flagged(directory, ctx.session, tool)
        if entry:
            out.append(th.message(tool, entry, directory, ctx.root))
    return out


def check_write(ctx: Context) -> list[str]:
    walked = ctx.walk()
    writes = ctx.writes()
    reasons = list(writes.problems)
    triggered = bool(
        st.WRITE_TRIGGER.search(ctx.trigger_text) or st.BUILT_STRING.search(ctx.command)
    )
    if walked.too_deep and st.WRITE_TRIGGER.search(ctx.trigger_text):
        reasons.append("the command nests too deeply to inspect its writes")
    if triggered:
        # A computed command name or opaque code next to a write verb or a run-time built string
        # (`$(printf '\x72m') f`, `& ('Set-'+'Content') f`) may be that writer.
        reasons += [f"{what}; the guard cannot tell what it writes" for _kind, what in ctx.opaque()]
    for cmd in walked.cmds:
        if cmd.head is not None and cmd.name in ALIAS_HEADS and triggered:
            reasons.append(f"`{cmd.name}` defines a command in a command that writes")
    if not writes.targets:
        return reasons
    bases = ctx.bases()
    policy = st.PathPolicy(
        ctx.root, ctx.cfg["write_guard"]["allowed_dirs"], ctx.cfg["git_timeout_seconds"]
    )
    for target in writes.targets:
        if bases is None:
            reasons.append(
                f"{target.verb} target `{target.word}` follows a `cd` to a run-time directory"
            )
            continue
        reason = policy.check(target, bases)
        if reason:
            reasons.append(reason)
    if reasons:
        allowed = ", ".join(f"`{d}/`" for d in ctx.cfg["write_guard"]["allowed_dirs"])
        reasons.append(
            f"Shell commands may write only below {allowed} of the project or worktree root (no tracked files); "
            "create or change other files with the Write or Edit tool"
        )
    return reasons


def _inside(path: Path, top: Path) -> bool:
    here = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(top))
    return here == root or here.startswith(root.rstrip("\\/") + os.sep)


def _resolve_all(word: str, bases: list[Path]) -> list[Path] | None:
    if not word or any(ch in word for ch in "$`%{}@(") or word.startswith("~"):
        return None
    path = Path(word)
    return [path] if path.is_absolute() else [b / word for b in bases]


def _merge_worktree(ctx: Context) -> tuple[Path | None, str]:
    """The merge job's worktree, or None with the reason merge/add are refused."""
    if ctx.args.git_mode != "worker-merge":
        return None, "only a merge job (`cli_worker.py start --git-guard merge`) may run it"
    worktree = Path(ctx.args.worktree)
    worktrees = ctx.root / ctx.cfg["git_guard"]["worktrees_dir"]
    real = Path(os.path.realpath(worktree))
    if not _inside(real, worktrees) or os.path.normcase(str(real)) == os.path.normcase(
        os.path.realpath(worktrees)
    ):
        return None, f"the merge worktree {worktree} is not below {worktrees}"
    return real, ""


def _long_prefix(arg: str, full: str, minimum: int) -> bool:
    """git accepts any unambiguous prefix of a long option (`--outp` for `--output`)."""
    name = arg.split("=", 1)[0]
    return len(name) >= minimum and full.startswith(name)


def _list_only(args: list[str], positional_flags: set[str]) -> bool:
    """`git branch`/`git tag` listing forms: listing options only; names only with a listing flag."""
    positional = False
    listing = False
    k = 0
    while k < len(args):
        a = args[k]
        name = a.split("=", 1)[0]
        if name in LIST_VALUE_FLAGS:
            if "=" not in a and k + 1 < len(args) and not args[k + 1].startswith("-"):
                k += 1
        elif a in positional_flags:
            listing = True
        elif (
            a in LIST_FLAGS
            or re.fullmatch(r"-n\d*", a)
            or name in ("--color", "--column", "--abbrev")
        ):
            pass
        elif a.startswith("-"):
            return False
        else:
            positional = True
        k += 1
    return listing or not positional


def _config_read_only(args: list[str]) -> bool:
    positional: list[str] = []
    read = False
    k = 0
    while k < len(args):
        a = args[k]
        name = a.split("=", 1)[0]
        if name in CONFIG_VALUE_FLAGS:
            k += 1 if "=" in a else 2
            continue
        if a.startswith("-"):
            if name not in CONFIG_READ_FLAGS:
                return False
            read = read or name.startswith("--get") or name in ("--list", "-l")
        else:
            positional.append(a)
        k += 1
    if positional and positional[0] in ("get", "list"):
        return True
    if positional and positional[0] in CONFIG_WRITE_SUBS:
        return False
    return read or len(positional) == 1


def _readonly_args(sub: str, args: list[str]) -> str | None:
    """Reason when a read-only subcommand's arguments make it write; None when they do not."""
    for a in args:
        if _long_prefix(a, "--output", 4):
            return f"`git {sub} {a}` writes a file"
    if sub == "merge-tree" and any(_long_prefix(a, "--write-tree", 4) for a in args):
        return "`git merge-tree --write-tree` writes objects"
    if sub == "grep" and any(
        (a.startswith("-O") and not a.startswith("--"))
        or _long_prefix(a, "--open-files-in-pager", 4)
        for a in args
    ):
        return "`git grep -O` runs a pager program on the files"
    positional = [a for a in args if not a.startswith("-")]
    if sub == "config" and not _config_read_only(args):
        return "`git config` may only read (`--get`, `--list`, `get`, `list`, or one key)"
    if sub == "worktree" and positional[:1] != ["list"]:
        return "`git worktree` may only `list`"
    if sub == "stash" and positional[:1] not in (["list"], ["show"]):
        return "`git stash` may only `list` or `show`"
    if sub == "branch" and not _list_only(args, {"-l", "--list"}):
        return "`git branch` may only list branches"
    if sub == "tag" and not _list_only(args, {"-l", "--list", "-v", "--verify"}):
        return "`git tag` may only list or verify tags"
    if sub == "remote":
        if positional[:1] in (["get-url"], ["show"]):
            return None
        if positional or any(a not in ("-v", "--verbose") for a in args):
            return "`git remote` may only list (`-v`), `get-url` or `show`"
    if sub == "reflog" and positional[:1] and positional[0] in REFLOG_WRITES:
        return f"`git reflog {positional[0]}` rewrites the reflog"
    return None


def _push_force(args: list[str]) -> str | None:
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            if any(
                _long_prefix(a, full, 4) for full in ("--force", "--force-with-lease", "--mirror")
            ):
                return f"`git push {a}` rewrites remote history"
        elif a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                if ch == "f":
                    return f"`git push {a}` forces the push"
                if ch == "o":
                    break  # `-o <option>` takes the rest of the cluster
    for a in args:  # refspecs may also follow `--`
        if a.startswith("+"):
            return f"`git push {a}`: a `+` refspec forces the update"
    return None


def _git_alias(ctx: Context, name: str) -> tuple[str | None, str | None]:
    """(alias text, error) from `git config --get alias.<name>` in the session's directory."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ctx.cwd), "config", "--get", f"alias.{name}"],
            capture_output=True,
            timeout=ctx.cfg["git_timeout_seconds"],
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if proc.returncode == 1:
        return None, None  # no such alias
    if proc.returncode != 0:
        return None, proc.stderr.decode("utf-8", "replace").strip()[
            :120
        ] or f"exit {proc.returncode}"
    return proc.stdout.decode("utf-8", "replace").strip(), None


def _git_command(cmd: st.Cmd, ctx: Context) -> str | None:
    """Reason when this git invocation is not allowed in the session's git mode."""
    mode = ctx.args.git_mode
    gg = ctx.cfg["git_guard"]
    worker = mode != "pm"
    args = [str(a) for a in cmd.args]
    bases = ctx.bases() or [ctx.cwd]
    c_dirs: list[str] = []
    k = 0
    sub: str | None = None
    while k < len(args):
        a = args[k]
        if a == "-c" or (a.startswith("-c") and not a.startswith("--") and len(a) > 2):
            value = args[k + 1] if a == "-c" and k + 1 < len(args) else a[2:]
            key = value.split("=", 1)[0].lower()
            if key.startswith("alias."):
                return f"`git -c {value}` defines an alias"
            if worker and key not in {c.lower() for c in gg["allowed_config_keys"]}:
                return f"`git -c {key}` is not on the allowed config keys"
            if not worker and ("push" in key or "mirror" in key):
                return f"`git -c {key}` changes how push works"
            k += 2 if a == "-c" else 1
            continue
        if a == "-C":
            if k + 1 >= len(args):
                return "`git -C` without a directory"
            c_dirs.append(args[k + 1])
            k += 2
            continue
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            if name in GIT_SAFE_GLOBALS and not (name == "--exec-path" and "=" in a):
                k += 1
                continue
            if worker:
                if any(_long_prefix(name, full, 5) for full in GIT_REDIRECTING):
                    return f"`git {a}` redirects the repository or git's programs"
                return f"git global option `{a}` is not on the allowlist"
            k += 2 if name in GIT_VALUE_GLOBALS and "=" not in a else 1
            continue
        sub = a
        k += 1
        break
    rest = args[k:]
    if sub is None:
        return None  # `git --version`, `git -C x` alone
    if any(ch in sub for ch in "$`%(){}@") or not sub:
        return f"git subcommand `{sub}` is computed at run time"
    for c_dir in c_dirs:
        paths = _resolve_all(c_dir, bases)
        if paths is None:
            if worker:
                return f"`git -C {c_dir}` is a run-time directory"
            continue
        if worker and not all(_inside(p, ctx.root) for p in paths):
            return f"`git -C {c_dir}` leaves the project root"
    if not worker:
        if sub == "push":
            runtime = [a for a in rest if any(ch in a for ch in "$`%") or a.startswith("@")]
            if runtime:
                return f"`git push {runtime[0]}`: a run-time push argument may force the push"
            return _push_force(rest)
        if sub == "config":
            return _config_alias(rest)
        if sub not in gg["readonly_subcommands"] and sub not in gg["merge_subcommands"]:
            alias, error = _git_alias(ctx, sub)
            if error:
                return f"cannot resolve whether `git {sub}` is an alias ({error})"
            if alias and (alias.startswith("!") or re.search(r"\bpush\b", alias)):
                return f"`git {sub}` is an alias that runs `{alias[:80]}`"
        return None
    if sub in gg["readonly_subcommands"]:
        return _readonly_args(sub, rest)
    if sub in gg["merge_subcommands"]:
        worktree, why = _merge_worktree(ctx)
        if worktree is None:
            return f"`git {sub}` is not read-only: {why}"
        dirs = [p for c in c_dirs for p in (_resolve_all(c, bases) or [])] or bases
        if not all(_inside(d, worktree) for d in dirs):
            return f"`git {sub}` must run inside the merge worktree {worktree}"
        return _merge_args(sub, rest, dirs, worktree)
    return f"`git {sub}` is not on the worker read-only allowlist"


def _config_alias(args: list[str]) -> str | None:
    """PM `git config alias.<x> <value>`: an alias that runs a shell or a push could force-push later."""
    positional = [a for a in args if not a.startswith("-")]
    if positional[:1] == ["set"]:
        positional = positional[1:]
    for k, key in enumerate(positional):
        if key.lower().startswith("alias.") and k + 1 < len(positional):
            value = positional[k + 1]
            if value.startswith("!") or re.search(r"(?i)\bpush\b", value):
                return f"`git config {key}` defines an alias that runs `{value[:80]}`"
    return None


def _merge_args(sub: str, args: list[str], dirs: list[Path], worktree: Path) -> str | None:
    if sub == "merge":
        # A merge job stages a merge and stops; the PM commits it. Plain `git merge` would commit or fast-forward
        # the branch, so it needs `--no-commit --no-ff` (or `--squash`); `--abort` undoes a staged merge.
        if "--abort" not in args and not (
            {"--no-commit", "--no-ff"} <= set(args) or "--squash" in args
        ):
            return "`git merge` in a merge job needs `--no-commit --no-ff` (or `--squash`); the PM commits"
        if {"--continue", "--commit", "--ff-only"} & set(args):
            return "`git merge --continue/--commit/--ff-only` commits or moves the branch; the PM commits"
        for k, a in enumerate(args):
            value = None
            if a in ("-s", "--strategy"):
                value = args[k + 1] if k + 1 < len(args) else ""
            elif a.startswith("--strategy="):
                value = a.split("=", 1)[1]
            elif a.startswith("-s") and not a.startswith("--") and len(a) > 2:
                value = a[2:]
            if value is not None and value not in MERGE_STRATEGIES:
                return f"`git merge` strategy `{value}` is not a built-in strategy"
            if a in ("-e", "--edit"):
                return "`git merge --edit` opens an editor"
        return None
    options_done = False
    for a in args:
        if not options_done and a == "--":
            options_done = True
            continue
        if not options_done and a.startswith("-"):
            name = a.split("=", 1)[0]
            if name in ("-i", "--interactive", "-p", "--patch", "-e", "--edit") or _long_prefix(
                name, "--pathspec-from-file", 6
            ):
                return f"`git add {a}` is interactive or reads paths from a file"
            if re.fullmatch(r"-[A-Za-z]+", a) and set(a[1:]) & set("ipe"):
                return f"`git add {a}` is interactive"
            continue
        if a.startswith(":"):
            return f"`git add {a}`: pathspec magic is not allowed"
        paths = _resolve_all(a, dirs)
        if paths is None or not all(_inside(p, worktree) for p in paths):
            return f"`git add {a}` is outside the merge worktree"
    return None


def check_git(ctx: Context) -> list[str]:
    walked = ctx.walk()
    mode = ctx.args.git_mode
    worker = mode != "pm"
    reasons: list[str] = []
    if walked.too_deep and GIT_TRIGGER.search(ctx.trigger_text):
        reasons.append("the command nests too deeply to inspect its git calls")
    if worker:
        for _raw, masked, _shell in walked.texts:
            if GIT_ENV.search(masked):
                reasons.append("workers may not set GIT_* environment variables")
                break
    mentions_git = bool(GIT_TRIGGER.search(ctx.trigger_text))
    dangerous = (
        GIT_WRITE_WORDS.search(ctx.trigger_text)
        if worker
        else re.search(r"(?i)\bpush\b", ctx.trigger_text)
    )
    # A computed name or opaque code can run git (`& (gcm git) commit`, `& ('g'+'it') push -f`). A computed
    # command name may spell git without the word (`$(echo g)it commit`, review N5): a git write word is enough.
    names_opaque = [what for kind, what in ctx.opaque() if kind == "name"]
    if names_opaque and dangerous:
        reasons.append(f"{names_opaque[0]}, in a command with a git write word")
    elif ctx.opaque() and ((mentions_git and dangerous) or st.BUILT_STRING.search(ctx.command)):
        what = ctx.opaque()[0][1]
        reasons.append(
            f"{what}, in a command that may run git (`git` with a write word or a built string)"
        )
    for cmd in walked.cmds:
        name = cmd.name
        gits = [c for c in st.resolved_cmds(cmd, walked) or [] if c.name == "git"]
        if cmd.head is not None and name in ALIAS_HEADS and mentions_git:
            reasons.append(f"`{name}` defines a command in a command that mentions git")
        elif cmd.head is not None and (name == "git" or gits):
            for call in [cmd] if name == "git" else gits:
                reason = _git_command(call, ctx)
                if reason:
                    reasons.append(reason)
        elif worker:
            code = _interpreter_code(cmd, ctx) or ""
            if GIT_TRIGGER.search(code) and GIT_WRITE_WORDS.search(code):
                reasons.append(f"`{name}` inline code runs a git subcommand that is not read-only")
    if reasons:
        hint = (
            "The PM pushes without force; use `git push` plain."
            if not worker
            else "Workers run read-only git only (status, diff, log, show, ...); the PM commits and merges."
        )
        reasons.append(hint)
    return list(dict.fromkeys(reasons))


def _git_call(cmd: st.Cmd) -> tuple[list[str], str, list[str], bool, bool] | None:
    """(`-C` directories, subcommand, its arguments, repository redirected, alias defined on the command line) of a
    git call; None without a subcommand."""
    args = [str(a) for a in cmd.args]
    program = GIT_PROGRAM.match(cmd.name)
    if program:
        return [], program.group(1).lower(), args, False, False
    c_dirs: list[str] = []
    redirected = False
    alias_cfg = False
    k = 0
    while k < len(args):
        a = args[k]
        if a == "-c":
            alias_cfg = alias_cfg or (
                k + 1 < len(args) and args[k + 1].lower().startswith("alias.")
            )
            k += 2
        elif a.startswith("-c") and not a.startswith("--") and len(a) > 2:
            alias_cfg = alias_cfg or a[2:].lower().startswith("alias.")
            k += 1
        elif a == "-C":
            if k + 1 >= len(args):
                return None
            c_dirs.append(args[k + 1])
            k += 2
        elif a.startswith("-"):
            name = a.split("=", 1)[0]
            redirected = redirected or any(_long_prefix(name, full, 5) for full in GIT_REDIRECTING)
            alias_cfg = alias_cfg or "alias." in a.lower()  # `--config-env=alias.x=VAR`
            k += 2 if name in GIT_VALUE_GLOBALS and "=" not in a else 1
        else:
            return c_dirs, a, args[k + 1 :], redirected, alias_cfg
    return None


def _push_dry_run(args: list[str]) -> bool:
    skip = False  # the previous option takes this word as its value
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            break
        if a.startswith("--"):
            if _long_prefix(a, "--dry-run", 4):
                return True
            skip = "=" not in a and any(_long_prefix(a, full, 4) for full in PUSH_VALUE_OPTIONS)
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch == "n":
                    return True
                if ch == "o":
                    skip = (
                        k == len(a) - 1
                    )  # `-o <option>`: the value is the next word, or the rest of the cluster
                    break
    return False


GIT_PROGRAM = re.compile(r"(?i)^git-([a-z0-9][a-z0-9-]*)(?:\.exe)?$")  # `git-co` = `git co`
MAYBE_EMPTY = re.compile(
    r"[$`]|@[({]|@$|%[^%\s]+%"
)  # a word the shell may expand to nothing (variable, substitution)


def _moves_head(sub: str, rest: list[str], alias_cfg: bool = False) -> bool:
    """Whether this git call can move HEAD. Path-only `checkout`/`reset` (`-- <paths>`) restore files only; a `--`
    with no path after it (`git checkout main --`, `git reset HEAD~1 --`) still switches or resets, and so does one
    whose only words may expand to nothing (`-- $FILES`). A subcommand that is not a known built-in may be an alias
    for a mover, as may any call made with `-c alias.*`; `symbolic-ref` only moves HEAD when it sets a value."""
    if sub in NON_MOVING_GIT and not alias_cfg:
        return False
    if sub == "symbolic-ref":
        return (
            sum(1 for a in rest if not a.startswith("-")) >= 2 or "--delete" in rest or "-d" in rest
        )
    if sub in ("checkout", "reset") and "--" in rest:
        cut = rest.index("--")
        if any(w and not MAYBE_EMPTY.search(w) for w in rest[cut + 1 :]):
            return any(a in HEAD_MOVING_FLAGS for a in rest[:cut])
    return True


def _head_sha(repo: Path, timeout: int) -> str | None:
    """Full HEAD commit id of the repository at `repo`, or None when git cannot tell."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = proc.stdout.decode("ascii", "replace").strip().lower()
    return sha if proc.returncode == 0 and COMMIT_ID.fullmatch(sha) else None


def _push_reasons(ctx: Context) -> list[str]:
    """Why each `git push` of the command is refused: the marker of its repository's HEAD is provably missing."""
    cfg = ctx.cfg["push_guard"]
    bases = ctx.bases()
    if bases is None:
        return []
    markers = ctx.root / cfg["marker_dir"]
    walked = ctx.walk()
    reasons: list[str] = []
    moved: str | None = None  # the first earlier call that moves HEAD
    for cmd in walked.cmds:
        if cmd.head is None:
            continue
        gits = (
            [cmd]
            if cmd.name == "git" or GIT_PROGRAM.match(cmd.name)
            else [
                c
                for c in st.resolved_cmds(cmd, walked) or []
                if c.name == "git" or GIT_PROGRAM.match(c.name)
            ]
        )
        for call in gits:
            parsed = _git_call(call)
            if parsed is None:
                continue
            c_dirs, sub, rest, redirected, alias_cfg = parsed
            if _moves_head(sub, rest, alias_cfg):
                moved = moved or f"git {sub}" + ("" if sub in HEAD_MOVERS else " (maybe an alias)")
                continue
            if sub != "push" or redirected or _push_dry_run(rest):
                continue
            if moved:
                # The lint cannot have marked a commit that does not exist yet.
                reasons.append(
                    f"`{moved}` moves HEAD in the same command, so no pre-push marker can exist for the pushed commit"
                )
                continue
            dirs: list[Path] | None = bases
            for c_dir in c_dirs:
                dirs = _resolve_all(c_dir, dirs) if dirs is not None else None
            if dirs is None:
                continue  # a run-time directory: the repository is not provable
            heads = [h for h in (_head_sha(d, ctx.cfg["git_timeout_seconds"]) for d in dirs) if h]
            # The parser does not keep `cd` scoping (`cd X && ...`, `(cd X; ...)`, `Set-Location X`, `pushd X`): the
            # start directory stays a candidate next to every target. Any of them may be the pushing repository, so
            # each one with a HEAD needs its marker; a marked start directory must not vouch for a `cd` target.
            unmarked = list(dict.fromkeys(h for h in heads if not (markers / h).is_file()))
            for sha in unmarked:
                hint = (
                    " (after a directory change, push with `git -C <dir> push`)"
                    if ctx.writes().cwd_changes
                    else ""
                )
                reasons.append(
                    f"the pre-push marker {markers / sha} for HEAD {sha} is missing{hint}"
                )
    return list(dict.fromkeys(reasons))


def check_push(ctx: Context) -> list[str]:
    """PM mode: `git push` needs the marker the prepush lint writes for the pushed HEAD. Fails open on its own errors."""
    if ctx.args.git_mode != "pm" or not re.search(r"(?i)\bpush\b", ctx.trigger_text):
        return []
    try:
        reasons = _push_reasons(ctx)
    except Exception:
        _crash_log("push_guard", ctx)
        return []
    if reasons:
        reasons.append(
            f"Run {ctx.cfg['push_guard']['lint_command']} on the commit to push, then push"
        )
    return reasons


def _code_letters(flags: list[str]) -> str:
    return "".join(f[1] for f in flags if len(f) == 2 and f[0] == "-" and f[1] != "-")


def _interp_family(name: str, ctx: Context) -> str | None:
    if st.is_python(name) or name.startswith("$"):
        return "python"  # `& $PY -c ...`: an interpreter held in a variable is read like python
    return name if name in ctx.cfg["inline_interp"]["code_flags"] else None


def _interpreter_code(cmd: st.Cmd, ctx: Context) -> str | None:
    """The inline program text of an interpreter call (`-c`, `-e`, `--eval=`), or None."""
    family = _interp_family(cmd.name, ctx)
    if family is None:
        return None
    flags = ctx.cfg["inline_interp"]["code_flags"].get(family, [])
    args = [str(a) for a in cmd.args]
    if family == "python":
        code, _module, _stdin = st.python_code(args, _code_letters(flags))
        return code
    longs = [f for f in flags if f.startswith("--")]
    letters = _code_letters(flags)
    for k, a in enumerate(args):
        if a == "--":
            break
        for long in longs:
            if a == long:
                return args[k + 1] if k + 1 < len(args) else ""
            if a.startswith(long + "="):
                return a[len(long) + 1 :]
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for j, ch in enumerate(a[1:]):
                if ch in letters:
                    rest = a[j + 2 :]
                    return rest or (args[k + 1] if k + 1 < len(args) else "")
        elif not a.startswith("-"):
            return None  # a script path: the rest are its arguments
    return None


def _reads_program_from_stdin(cmd: st.Cmd, ctx: Context) -> bool:
    family = _interp_family(cmd.name, ctx)
    args = [str(a) for a in cmd.args]
    if family == "python":
        code, module, stdin = st.python_code(
            args, _code_letters(ctx.cfg["inline_interp"]["code_flags"].get("python", []))
        )
        if code is not None or module is not None:
            return False
        if "-" in args:
            return True
        return stdin and (cmd.piped_in or cmd.stdin_text or any(r.op == "<" for r in cmd.redirects))
    if _interpreter_code(cmd, ctx) is not None:
        return False
    positional = [a for a in args if not a.startswith("-") or a == "-"]
    if positional[:1] == ["-"]:
        return True
    return not positional and (
        cmd.piped_in or cmd.stdin_text or any(r.op == "<" for r in cmd.redirects)
    )


def check_inline(ctx: Context) -> list[str]:
    limit = ctx.cfg["inline_interp"]["max_inline_chars"]
    reasons: list[str] = []
    walked = ctx.walk()
    for cmd in walked.cmds:
        if cmd.head is None or _interp_family(cmd.name, ctx) is None:
            continue
        code = _interpreter_code(cmd, ctx)
        if code is not None:
            if any(b in code for b in LINE_BREAKS):
                reasons.append(f"`{cmd.name}` inline code spans several lines")
            elif len(code) > limit:
                reasons.append(
                    f"`{cmd.name}` inline code has {len(code)} characters (limit {limit})"
                )
        elif _reads_program_from_stdin(cmd, ctx):
            reasons.append(
                f"`{cmd.name}` reads its program from stdin (pipe, heredoc, here-string or `-`)"
            )
    triggered = _interp_trigger(ctx).search(ctx.trigger_text)
    if walked.too_deep and triggered:
        reasons.append("the command nests too deeply to inspect its interpreter calls")
    if triggered or st.BUILT_STRING.search(ctx.command):
        reasons += [
            f"{what}, next to an interpreter call" for kind, what in ctx.opaque() if kind == "name"
        ]
    if reasons:
        reasons.append(
            "Put the code in a script under <worktree>/.tmp/<task>/ (Write tool) and run `python -B -I <file>`"
        )
    return reasons


def _runs_pytest(cmd: st.Cmd, ctx: Context) -> bool:
    if cmd.head is None or cmd.launcher:
        return False
    name = cmd.name
    args = [str(a) for a in cmd.args]
    if name in PYTEST_HEADS or set(st.head_names(cmd, ctx.walk()) or []) & PYTEST_HEADS:
        return True
    for k, a in enumerate(args):
        if a == "-m" and k + 1 < len(args) and args[k + 1] in PYTEST_HEADS:
            return True
        if a.startswith("-m") and a[2:] in PYTEST_HEADS:
            return True
    if st.is_python(name):
        code, module, _ = st.python_code(args)
        if module in PYTEST_HEADS or (code and re.search(r"\bpytest\b", code)):
            return True
    if name in RUNNERS or name == "tox":
        return any(pg._head(a) in PYTEST_HEADS for a in args) or name == "tox"
    return False


def check_pytest(ctx: Context) -> list[str]:
    exempt = set(ctx.cfg["pytest_slot"]["exempt_flags"])
    walked = ctx.walk()
    reasons = []
    for cmd in walked.cmds:
        if cmd.launched or not _runs_pytest(cmd, ctx):
            continue
        if any(str(a) in exempt for a in cmd.args):
            continue
        reasons.append(
            f"`{' '.join(str(w) for w in cmd.words[cmd.head or 0 :])[:120]}` runs pytest outside the test slot"
        )
    triggered = PYTEST_TRIGGER.search(ctx.trigger_text)
    if walked.too_deep and triggered:
        reasons.append("the command nests too deeply to inspect its test runs")
    if triggered or st.BUILT_STRING.search(ctx.command):
        reasons += [
            f"{what}, next to a pytest call" for kind, what in ctx.opaque() if kind == "name"
        ]
    if reasons:
        launcher = ctx.cfg["pytest_slot"]["launchers"][0]
        reasons.append(
            f"Run tests through the slot limiter: `<venv python> -B tools/agent-workflow/{launcher} -- <python> -m pytest ...`"
        )
    return reasons


def check_read(ctx: Context) -> list[str]:
    notes: list[str] = []
    walked = ctx.walk()
    for cmd in walked.cmds:
        if cmd.head is None:
            continue
        name = cmd.name
        args = [str(a) for a in cmd.args]
        files = [a for a in args if not a.startswith("-")]
        if (
            name in READ_HEADS
            and files
            and not cmd.captured
            and not any(r.op.startswith(">") for r in cmd.redirects)
        ):
            if not cmd.piped_out or all(
                r in READ_FILTERS or r.startswith("format-") for r in cmd.readers
            ):
                notes.append(
                    f"`{name} {files[0]}` displays a file: use the Read tool (offset/limit) or Grep"
                )
        elif name in ("select-string", "sls") and not cmd.piped_in:
            notes.append("Select-String over files: use the Grep tool")
        elif name in LISTING_HEADS and _recursive(name, args, cmd.shell):
            notes.append(f"`{name}` recursive listing: use the Glob tool")
    for _raw, masked, shell in walked.texts:
        if shell == "ps" and DOTNET_READ.search(masked) and not DOTNET_READ_CAPTURED.search(masked):
            notes.append("[IO.File]::ReadAll* displays a file: use the Read tool")
            break
    return [f"[read-router] {n}" for n in dict.fromkeys(notes)]


def _recursive(name: str, args: list[str], shell: str) -> bool:
    for a in args:
        low = a.lower()
        if (shell == "cmd" or name == "dir") and low in ("/s", "-s"):
            return True
        if name == "ls" and shell == "bash":
            if re.fullmatch(r"-[A-Za-z]*R[A-Za-z]*", a) or a == "--recursive":
                return True
        elif (
            low.startswith("-") and len(low) >= 2 and "-recurse".startswith(low.split(":")[0])
        ) or low.startswith("-depth"):
            return True
    return False


def _interp_trigger(ctx: Context) -> re.Pattern[str]:
    names = [
        "python[\\d.]*w?",
        "pypy[\\d.]*",
        "py",
        *map(re.escape, ctx.cfg["inline_interp"]["code_flags"]),
    ]
    return re.compile(r"(?i)(?<![\w-])(?:" + "|".join(names) + r")(?:\.exe)?(?![\w-])")


Check = Callable[[Context], list[str]]


def checks(ctx: Context) -> list[tuple[str, Check, re.Pattern[str] | None]]:
    """(config section, check, trigger). The trigger decides whether a crash of the check blocks."""
    return [
        ("tool_broken", check_tool_broken, None),
        ("write_guard", check_write, st.WRITE_TRIGGER),
        ("git_guard", check_git, GIT_TRIGGER),
        (
            "push_guard",
            check_push,
            None,
        ),  # no trigger: a crash inside it must never block (it catches its own)
        ("inline_interp", check_inline, _interp_trigger(ctx)),
        ("pytest_slot", check_pytest, PYTEST_TRIGGER),
        ("read_router", check_read, None),
    ]


def evaluate(ctx: Context) -> Result:
    blocks: list[str] = []
    warns: list[str] = []
    invisible = [ch for ch in ctx.command if unicodedata.category(ch) == "Cf"]
    guarded = [
        (section, trigger)
        for section, _check, trigger in checks(ctx)
        if trigger is not None
        and ctx.cfg[section]["mode"] != "off"
        and trigger.search(ctx.trigger_text)
    ]
    if invisible and guarded:
        (blocks if any(ctx.cfg[s]["mode"] == "block" for s, _ in guarded) else warns).append(
            "the command holds invisible format characters "
            f"({', '.join(sorted({f'U+{ord(c):04X}' for c in invisible}))}) next to a guarded command"
        )
    if len(ctx.command) > ctx.cfg["max_command_chars"] and guarded:
        (blocks if any(ctx.cfg[s]["mode"] == "block" for s, _ in guarded) else warns).append(
            f"the command has {len(ctx.command)} characters (inspection limit {ctx.cfg['max_command_chars']}); "
            "put it in a script file"
        )
        return blocks, warns
    for section, check, trigger in checks(ctx):
        mode = ctx.cfg[section]["mode"]
        if mode == "off":
            continue
        try:
            found = check(ctx)
        except Exception:
            _crash_log(section, ctx)
            if mode == "block" and trigger is not None and trigger.search(ctx.trigger_text):
                blocks.append(
                    f"the {section} check crashed on this command; it fails closed for commands it guards"
                )
            else:
                warns.append(
                    f"[tool-guard] the {section} check crashed; the command was not checked by it"
                )
            continue
        if found:
            (blocks if mode == "block" else warns).extend(f"[{section}] {r}" for r in found)
    return blocks, warns


def _crash_log(section: str, ctx: Context) -> None:
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": dt.datetime.now().isoformat(timespec="seconds"),
                        "hook": "tool_guard",
                        "check": section,
                        "tool": ctx.tool,
                        "command": ctx.command[:300],
                        "traceback": traceback.format_exc()[-2000:],
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def _log(tool: str, command: str, reasons: list[str]) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": dt.datetime.now().isoformat(timespec="seconds"),
                        "tool": tool,
                        "reasons": reasons[:5],
                        "command": command[:300],
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PreToolUse shell guard", exit_on_error=False)
    parser.add_argument(
        "--config", help="guard config (default tools/agent-workflow/guard_config.json)"
    )
    parser.add_argument("--git-mode", choices=GIT_MODES, required=True)
    parser.add_argument("--worktree", help="worker-merge: the merge job's worktree")
    args = parser.parse_args(argv)
    if (args.git_mode == "worker-merge") != bool(args.worktree):
        raise ValueError("--worktree is required with --git-mode worker-merge and only there")
    return args


def main(argv: list[str] | None = None, root: Path = gc.ROOT) -> int:
    try:
        event = json.loads(pg.read_event())
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("tool_name") not in SHELL_TOOLS:
        return 0  # never block Read, Edit, Write, Grep, Glob or any other tool
    if event.get("tool_name") in NEVER_BLOCKED:
        return 0
    try:
        args = parse_args(argv)
        cfg = gc.load(gc.resolve_path(args.config))
    except (gc.ConfigError, ValueError, argparse.ArgumentError, SystemExit) as exc:
        sys.stderr.write(
            f"BLOCKED by tool_guard: the guard configuration is invalid ({exc}). Shell tools stay blocked until it "
            "is fixed; repair the config or the hook arguments with the Edit tool.\n"
        )
        return 2
    ctx = Context(event, cfg, args, root)
    if not ctx.command:
        return 0
    blocks, warns = evaluate(ctx)
    if blocks:
        _log(ctx.tool, ctx.command, blocks)
        sys.stderr.write("BLOCKED by tool_guard: " + "\n".join(dict.fromkeys(blocks)) + "\n")
        return 2
    if warns:
        context = "\n".join(dict.fromkeys(warns))
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": context,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
