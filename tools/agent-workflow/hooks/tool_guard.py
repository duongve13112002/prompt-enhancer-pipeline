"""PreToolUse guard for shell tools (Bash, PowerShell, Monitor): P-01, P-03, P-05, P-12, P-13, P-15.

Checks (each with a mode in guard_config.json: block, warn or off):
- tool_broken: a tool flagged by the harness-error streak (tool_monitor.py) is blocked until reset.
- write_guard: shell writes only under the configured allowlist (today `.tmp/`, `runs/`) of the innermost work tree.
- git_guard (decision 77, T-024 round 2): both modes allow the read-only allowlist; `--git-mode pm` (the
  orchestrator) adds an allowlist of plain write forms (`WRITE_FORMS`: add, commit -m/-F, push <remote> <branch>,
  revert --no-edit, pull --ff-only, fetch, reset/restore of paths, apply, worktree add/remove/prune, branch -d) from
  the main working tree, and `git add`/`git apply` and the patch save (`diff --cached --binary --output=<patch_dir
  file>`) in a worker worktree. Everything else is refused, so no alias, option spelling, launcher or environment
  opens a history write. Round 3: a git call whose words the shell expands at run time is refused in both modes
  (class E); a write form has nothing before `git`; git under a wrapper, given to another program or named in inline
  interpreter code is refused in both modes (class F); the main tree and worker worktrees are proven by the Git
  directories git resolves, not by the toplevel (class G).
- push_guard (`--git-mode pm`): `git push` is blocked unless `<marker_dir>/<full HEAD id of the pushing repository>`
  exists (written by prepush_mark.py when prepush_check.py is clean) and HEAD is `refs/heads/<push_branch>`, so the
  branch tip sent is the checked HEAD (class H); `--dry-run` passes; an undeterminable repository, HEAD or branch and
  the check's own errors refuse the push (T-024 final, PD2).
- write_guard in worker mode (T-024 final): the whole state directory (`git_ledger.ledger_dir(root).parent`) is
  protected (PD1); an argument of a program the guard does not model may not resolve into or above it (F06); archive
  options that keep absolute member names are refused (F07); links are judged at both ends and a write through a
  place another part of the command may turn into a link is refused (F27). git_guard in worker mode refuses read-only
  options and `-c` settings that run configured helper programs (F08).
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
import fnmatch
import json
import os
import re
import subprocess
import sys
import traceback
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_ledger as gl
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
GIT_MODES = ("pm", "worker")
LOG = pg.STATE_DIR / "tool-guard.log"
CRASH_LOG = pg.STATE_DIR / "guard-crash.log"

GIT_TRIGGER = re.compile(r"(?i)git")
PUSH_TRIGGER = re.compile(r"(?i)\bpush\b")
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
# Orchestrator (`--git-mode pm`) writes, decision 77 and T-024 round 2: an allowlist of plain forms, like the worker's
# read-only allowlist. A git call that is neither read-only nor one of these forms is refused (safety-bound).
MAIN_TREE_FORMS = (
    *("add", "commit", "push", "revert", "pull", "fetch"),
    *("reset", "restore", "apply", "worktree", "branch"),
)
WORKER_TREE_FORMS = ("add", "apply")  # in a worker worktree the orchestrator only stages or applies a patch
# Subcommands that change history or refs: an opaque command (`& $g rebase`, a name built at run time) carrying one
# of them is refused in pm mode.
PM_HISTORY_WORDS = re.compile(
    r"(?i)\b(?:push|commit|merge|rebase|reset|cherry-pick|revert|am|pull|fetch|tag|branch|update-ref|symbolic-ref"
    r"|filter-branch|filter-repo|fast-import|replace|reflog|notes|checkout|switch|worktree)\b"
)
GIT_ENV_RAW = re.compile(r"(?i)\benv:\s*GIT_")  # `Set-Item 'env:GIT_DIR'`: the variable name may sit in quotes
GIT_WORD = re.compile(r"(?i)(?<![\w-])git(?!\w)")  # `git`, `git.exe`, `/usr/bin/git`, `git-commit`; not `digit`
# Programs that run another command line given as their arguments; with `git` in those arguments they are refused in
# both modes, because the guard does not read the inner line through them (review R2, safety-bound).
LAUNCHERS = {
    *("bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "pwsh", "powershell", "wsl", "xargs"),
    *("start-process", "saps", "start", "invoke-expression", "iex"),
}
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
# git-reflog(1): "show" is the default subcommand; "list" and "exists" only read; "expire", "delete", "drop" and
# "write" change reflogs. Only the reading forms pass (T-024 round 3, item 6; safety-bound).
REFLOG_READS = {"show", "list", "exists"}
# Words that start a command without changing the argv of the command after them (shell grammar, protocol-bound).
SHELL_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!"}
# Programs that only read or print their arguments: a `git` word given to one of them is data, not a program it
# runs (T-024 round 3, class F). Any other program given a `git` word may run it and is refused (safety-bound).
READ_ONLY_PROGRAMS = {
    *("echo", "printf", "write-output", "write-host", "which", "where", "get-command", "gcm", "type"),
    *("grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls"),
}
# A revision word of the `revert` and `worktree add` forms: a ref name or object id with optional `~<n>`/`^<n>`
# parent suffixes (gitrevisions(7)); no range, reflog selector, `:path` or option (T-024 round 3, item 8).
REVISION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*(?:[~^]\d*)*")
# Characters through which a shell changes a word after the guard read it (T-024 round 3, class E). Bash: parameter,
# command and arithmetic expansion (`$`, backtick) also inside double quotes; outside quotes pathname (`*?[`) and brace
# expansion, and tilde expansion at the start of a word. PowerShell: variables and subexpressions (`$`, backtick
# escapes) also in double-quoted and here-strings; outside quotes splatting (`@name`), arrays (`,`), script blocks and
# subexpressions; Windows PowerShell 5.1 passes embedded `"` to native programs unescaped. cmd: `%`, `!`, `^`, `"`.
RUN_TIME_ANY = {"bash": "$`", "ps": '$`"', "cmd": '%!^"'}
RUN_TIME_UNQUOTED = {"bash": "*?[{", "ps": "@(){},;", "cmd": ""}

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
# F06: programs that only read or print their arguments; any other program may write below a path it is given.
PROBE_EXEMPT = READ_ONLY_PROGRAMS | READ_HEADS | LISTING_HEADS
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
        self.ledger = False  # pm mode: an orchestrator write form was allowed, so git_ledger.begin() records it
        self._walk: st.Walk | None = None
        self._writes: st.Writes | None = None
        self._opaque: list[tuple[str, str]] | None = None
        self.root_identity: GitIdentity | bool | None = None  # the project's own repository; False: git cannot tell

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
    worker = ctx.args.git_mode != "pm"
    if not writes.targets and not (worker and writes.probes):
        return reasons
    bases = ctx.bases()
    # T-024 round 5 and final (PD1): a worker's shell never writes the orchestrator's state directory (the Git
    # ledger that excuses ref changes, push markers, patches, the worker registry), derived as the ledger's parent.
    protected = (gl.ledger_dir(ctx.root).parent,) if worker else ()
    policy = st.PathPolicy(
        ctx.root, ctx.cfg["write_guard"]["allowed_dirs"], ctx.cfg["git_timeout_seconds"], protected
    )
    if worker:
        reasons += _probe_reasons(writes, policy, bases)
    # F27: (command, place, strictly below) where a command may leave a link. The walk does not keep the run order
    # of substitutions, so a link from any other command of the line counts, before or after the write.
    regions = (
        [(t.group, top, strict) for t in writes.targets for top, strict in policy.link_regions(t, bases)]
        if worker and bases is not None
        else []
    )
    for target in writes.targets:
        if bases is None:
            reasons.append(
                f"{target.verb} target `{target.word}` follows a `cd` to a run-time directory"
            )
            continue
        reason = policy.check(target, bases)
        if not reason and regions:
            others = [(top, strict) for group, top, strict in regions if group != target.group]
            reason = policy.through_link(target, bases, others)
        if reason:
            reasons.append(reason)
    if reasons:
        allowed = ", ".join(f"`{d}/`" for d in ctx.cfg["write_guard"]["allowed_dirs"])
        reasons.append(
            f"Shell commands may write only below {allowed} of the project or worktree root (no tracked files); "
            "create or change other files with the Write or Edit tool"
        )
    return reasons


def _probe_reasons(writes: st.Writes, policy: st.PathPolicy, bases: list[Path] | None) -> list[str]:
    """F06 (worker mode): a program the guard does not model may write below any path it is given, so no argument
    of a command (other than the command's own modelled targets, judged by the path policy) may resolve into, onto
    or above the protected state directory. Programs that only read or print their arguments are exempt."""
    reasons: list[str] = []
    for name, probe in writes.probes:
        if name in PROBE_EXEMPT or _names_git(name):  # git: the git guard judges every git argument
            continue
        if bases is None:
            if not st._is_absolute(probe.word):
                reasons.append(
                    f"`{name}` follows a `cd` to a run-time directory, so the guard cannot place its argument "
                    f"`{probe.word[:60]}`"
                )
                break
            reason = policy.check_protected(probe, [])
        else:
            reason = policy.check_protected(probe, bases)
        if reason:
            reasons.append(reason)
    return list(dict.fromkeys(reasons))


def _inside(path: Path, top: Path) -> bool:
    here = st.fs_key(path)  # Win32 aliases folded (`.tmp/pm-state ./x`, streams, `\\?\`)
    root = st.fs_key(top)
    return here == root or here.startswith(root.rstrip("\\/") + os.sep)


def _resolve_all(word: str, bases: list[Path]) -> list[Path] | None:
    if not word or any(ch in word for ch in "$`%{}@(") or word.startswith("~"):
        return None
    path = Path(word)
    return [path] if path.is_absolute() else [b / word for b in bases]


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
    # git-merge-tree(1): "This command has a modern --write-tree mode and a deprecated --trivial-merge mode": the
    # default writes objects and runs merge drivers; only the trivial mode reads.
    if sub == "merge-tree" and "--trivial-merge" not in args:
        return "`git merge-tree` without `--trivial-merge` writes objects (its default `--write-tree` mode)"
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
    if sub == "reflog" and positional[:1] and positional[0] not in REFLOG_READS:
        return f"`git reflog {positional[0]}`: only `git reflog [show|list|exists] [<options>] [<ref>]` reads"
    return None


# F08 (worker mode): options of read-only calls that run a program named by Git configuration or attributes.
# git-diff(1) "--ext-diff: Allow an external diff helper to be executed", "--textconv: Allow (or disallow) external
# text conversion filters to be run"; git-log(1) "--show-signature: ... passing the signature to gpg --verify";
# git-cat-file(1) "--filters: Show the content as converted by the filters configured" (smudge filters).
HELPER_OPTIONS = ("--ext-diff", "--textconv", "--show-signature", "--filters")
HELPER_NOT_PREFIX = {"--text", "--filter"}  # real options that only look like abbreviations of a helper option
# Format placeholders that verify signatures (pretty-formats.adoc:307-321 `%GG`, `%G?`, `%GS`, ...; ref-filter
# `%(signature...)`). Review CX3: pretty-formats.adoc:381-391 "If you add a `+` (plus sign) after %", "`-` (minus sign)",
# "`' '` (space) after % of a placeholder": the modifier sits between `%` and the placeholder. Scanned left to right,
# so `%%` (a literal percent) is consumed first and `%%GG` stays readable.
SIGNATURE_FORMAT = re.compile(r"%%|%[-+ ]?G|%[-+ ]?\(signature")  # case-sensitive: `%gs` is the reflog subject
# git-help(1): "-w/--web: A web browser will be used", "-m/--man", "-i/--info": viewers named by `man.<tool>.cmd`,
# `browser.<tool>.cmd`, `help.browser`. git-tag(1) "-v, --verify: Verify the cryptographic signature" (gpg.program).
HELP_VIEWERS = (("--web", "w"), ("--man", "m"), ("--info", "i"))
# `-c` keys whose value is a program, or that make git run one (git-config(1)); matched lower-cased, `*` is one
# subsection. The config allowlist refuses them already; worker mode refuses them even when allowlisted.
HELPER_CONFIG = (
    *("core.fsmonitor", "core.pager", "core.editor", "core.sshcommand", "core.askpass", "core.hookspath"),
    *("core.gitproxy", "diff.external", "diff.tool", "merge.tool", "sequence.editor", "help.browser"),
    *("web.browser", "diff.*.command", "diff.*.textconv", "merge.*.driver", "filter.*", "pager.*", "credential.*"),
    *("gpg.*", "difftool.*", "mergetool.*", "browser.*", "man.*", "remote.*", "url.*", "protocol.*"),
)
GIT_FALSE = {"false", "no", "off", "0", ""}  # git-config(1) boolean false spellings


def _helper_reason(inv: GitInvocation) -> str | None:
    """Why a read-only worker call may run a configured helper program (F08); None when it cannot."""
    for setting in inv.configs:
        key, sep, value = setting.partition("=")
        key = key.lower()
        if any(fnmatch.fnmatchcase(key, p) for p in HELPER_CONFIG):
            return f"`git -c {key}` names a program git runs"
        if key == "log.showsignature" and (not sep or value.strip().lower() not in GIT_FALSE):
            return "`git -c log.showsignature` runs the configured gpg program"
    args = inv.rest[: inv.rest.index("--")] if "--" in inv.rest else inv.rest
    for k, a in enumerate(args):
        name = a.split("=", 1)[0]
        if len(name) >= 3 and name.startswith("--") and name not in HELPER_NOT_PREFIX:
            hit = next((full for full in HELPER_OPTIONS if full.startswith(name)), None)
            if hit:
                return f"`git {inv.sub} {a}` runs a configured helper program ({hit})"
        # Experiment abbrev.out: `git for-each-ref --f=...` and `git branch --form=...` are read as `--format`.
        if name.startswith("--") and len(name) >= 3 and ("--format".startswith(name) or "--pretty".startswith(name)):
            value = a.split("=", 1)[1] if "=" in a else (args[k + 1] if k + 1 < len(args) else "")
            if any(m.group(0) != "%%" for m in SIGNATURE_FORMAT.finditer(value)):
                return f"`git {inv.sub}` format `{value[:40]}` verifies signatures with the configured gpg program"
    values = {k + 1 for k, a in enumerate(args) if a in LIST_VALUE_FLAGS}  # `--sort -v:refname` is a value
    shorts = "".join(a[1:] for k, a in enumerate(args) if re.match(r"-[^-]", a) and k not in values)
    if inv.sub == "help":
        for full, letter in HELP_VIEWERS:
            if letter in shorts or any(len(x) >= 3 and full.startswith(x.split("=")[0]) for x in args):
                return f"`git help {full}` starts a viewer program named by the configuration"
    # Experiment abbrev.out: `git tag --v` is `--verify`.
    if inv.sub == "tag" and ("v" in shorts or any(len(x) >= 3 and "--verify".startswith(x.split("=")[0]) for x in args)):
        return "`git tag --verify` runs the configured gpg program"
    positional = [a for a in args if not a.startswith("-")]
    if inv.sub == "remote" and positional[:1] == ["show"] and "-n" not in args:
        # git-remote(1) `show`: "With -n option, the remote heads are not queried first with git ls-remote".
        return "`git remote show` queries the remote (ssh, credential helper); add `-n`"
    return None


def _git_env_set(ctx: Context) -> bool:
    """The command sets a `GIT_*` variable (`X=y` prefix, `export`, `$env:`, `Set-Item env:`, SetEnvironmentVariable)."""
    return any(
        GIT_ENV.search(masked) or GIT_ENV_RAW.search(raw) for raw, masked, _shell in ctx.walk().texts
    )


class GitInvocation(NamedTuple):
    """One git call as git reads it (T-024 review class A); both modes decide from this only."""

    sub: str | None  # the subcommand, lower-cased (`git-commit`, `git COMMIT` give `commit`); None without one
    verbatim: str | None  # the subcommand as typed: a write form matches it exactly
    rest: list[str]  # its arguments
    dirs: list[Path] | None  # candidate repositories (start directory, `cd` targets, chained `-C`); None at run time
    refusal: str | None  # why the read-only rules refuse its global options or subcommand spelling
    not_plain: str | None  # why it is not a plain write call; None for literal `git`/`git.exe`, only `-C <path>`
    configs: tuple[str, ...] = ()  # the `-c <key>[=<value>]` settings, as given


def _run_time_word(word: pg.Word, shell: str) -> bool:
    """The shell may turn this word into other or more argv entries than its text (class E): the guard then does not
    know what git receives. Single and double quotes are not told apart, so `$` fails closed in both."""
    text = str(word)
    if any(ch in text for ch in RUN_TIME_ANY.get(shell, "")):
        return True
    if shell == "ps" and text == "--%":  # about_Parsing: the stop-parsing token passes the rest of the line raw
        return True
    unquoted = not word.literal  # some part of the word stood outside quotes
    return unquoted and (
        any(ch in text for ch in RUN_TIME_UNQUOTED.get(shell, ""))
        or (shell == "bash" and text.startswith("~"))
    )


def _argv_unknown(cmd: st.Cmd) -> str | None:
    """Why the argv git receives is not the words the guard read: a run-time word or a process substitution."""
    for w in cmd.args:
        if _run_time_word(w, cmd.shell):
            return f"its argument `{str(w)[:40]}` is expanded by the shell at run time"
    for r in cmd.redirects:
        if r.op == "<" and r.target is not None and str(r.target).startswith("("):
            return "a process substitution adds an argument at run time"
    return None


def _git_invocation(cmd: st.Cmd, ctx: Context, resolved: bool) -> GitInvocation:
    """Normalise a git call: executable spelling, global options, repository selection. `resolved`: the program came
    from a variable binding (`$g = 'git'; & $g ...`), so it is not a literal `git` word."""
    gg = ctx.cfg["git_guard"]
    args = [str(a) for a in cmd.args]
    head = str(cmd.words[cmd.head]) if cmd.head is not None else ""
    base = re.split(r"[\\/]", head)[-1]
    prefix = [str(w) for w in cmd.words[: cmd.head or 0] if str(w).lower() not in SHELL_KEYWORDS]
    not_plain: str | None = None
    if resolved or st.RUN_TIME_CHARS.search(head):
        not_plain = "its program is named at run time"
    elif head not in ("git", "git.exe"):
        not_plain = f"its program is `{head[:60]}`, not the word `git`"
    elif prefix:  # class E: an assignment or wrapper (`X=y`, `env`, `timeout 9`) runs before git
        not_plain = f"`{prefix[0][:40]}` comes before `git`"
    elif _git_env_set(ctx):
        not_plain = "the command sets a `GIT_*` variable"
    # Class E (review R9): both modes validate options from the words; a word the shell expands may become any
    # options, so the read-only rules and the write forms refuse it alike.
    argv_unknown = _argv_unknown(cmd)
    allowed_keys = {c.lower() for c in gg["allowed_config_keys"]}
    c_dirs: list[str] = []
    configs: list[str] = []
    refusal: str | None = None
    sub: str | None = None
    k = 0
    program = GIT_PROGRAM.match(cmd.name)
    if program:  # git(1) PATH: `git-<command>` is the program `git <command>` runs; it takes no global options
        sub = program.group(1)
    while sub is None and k < len(args):
        a = args[k]
        if a == "-c" or (a.startswith("-c") and not a.startswith("--") and len(a) > 2):
            value = args[k + 1] if a == "-c" and k + 1 < len(args) else a[2:]
            configs.append(value)
            key = value.split("=", 1)[0].lower()
            not_plain = not_plain or f"it sets `-c {key}`"
            if key.startswith("alias."):
                refusal = refusal or f"`git -c {value}` defines an alias"
            elif key not in allowed_keys:
                refusal = refusal or f"`git -c {key}` is not on the allowed config keys"
            k += 2 if a == "-c" else 1
            continue
        if a == "-C":
            if k + 1 >= len(args):
                return GitInvocation(None, None, [], None, "`git -C` without a directory", "no directory")
            c_dirs.append(args[k + 1])
            k += 2
            continue
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            not_plain = not_plain or f"it uses the global option `{a}`"
            if not (name in GIT_SAFE_GLOBALS and not (name == "--exec-path" and "=" in a)):
                moves = any(_long_prefix(name, full, 5) for full in GIT_REDIRECTING)
                refusal = refusal or (
                    f"`git {a}` redirects the repository or git's programs"
                    if moves
                    else f"git global option `{a}` is not on the allowlist"
                )
            k += 2 if name in GIT_VALUE_GLOBALS and "=" not in a else 1
            continue
        sub = a
        k += 1
    rest = args[k:]
    # git(1) `-C`: "each subsequent non-absolute -C <path> is interpreted relative to the preceding -C <path>".
    dirs = ctx.bases()
    for c_dir in c_dirs:
        dirs = _resolve_all(c_dir, dirs) if dirs is not None else None
        if dirs is None:
            refusal = refusal or f"`git -C {c_dir}` is a run-time directory"
        elif not all(_inside(p, ctx.root) for p in dirs):
            refusal = refusal or f"`git -C {c_dir}` leaves the project root"
    if sub is not None and (not sub or any(ch in sub for ch in "$`%(){}@")):
        refusal = f"git subcommand `{sub}` is computed at run time"
    if argv_unknown:
        refusal = refusal or f"`git {sub or ''}`: {argv_unknown}; write the literal words"
    return GitInvocation(sub.lower() if sub else None, sub, rest, dirs, refusal, not_plain, tuple(configs))


def _git_command(cmd: st.Cmd, ctx: Context, resolved: bool = False) -> str | None:
    """Reason when this git invocation is not allowed. Both modes: the read-only allowlist. pm mode adds the
    orchestrator's write forms (`_write_form`); everything else is refused, so no new spelling opens a write."""
    inv = _git_invocation(cmd, ctx, resolved)
    if inv.refusal or inv.sub is None:
        return inv.refusal
    if ctx.args.git_mode == "pm" and inv.verbatim == "diff" and any(_long_prefix(a, "--output", 4) for a in inv.rest):
        return _patch_save(ctx, inv)
    reason = None
    if inv.sub in ctx.cfg["git_guard"]["readonly_subcommands"]:
        reason = _readonly_args(inv.sub, inv.rest)
        if reason is None and ctx.args.git_mode != "pm":
            reason = _helper_reason(inv)
        if reason is None:
            return None
    if ctx.args.git_mode != "pm":
        return reason or f"`git {inv.sub}` is not on the worker read-only allowlist"
    return _write_form(ctx, inv)


PATCH_SAVE = ["--cached", "--binary"]  # PM_POLICY "Integration": `git -C <worktree> diff --cached --binary`


def _patch_save(ctx: Context, inv: GitInvocation) -> str | None:
    """pm mode: `git -C <worker worktree> diff --cached --binary --output=<file>` saves a worker's staged patch
    (AGENTS.md section 11) when `<file>` resolves under `git_guard.patch_dir` of the main tree. git-diff(1)
    `--output=<file>`: "Output to a specific file instead of stdout"; git runs in the `-C` directory, so a relative
    `<file>` is taken from there. Every other `--output` stays refused (item 7)."""
    what = "`git diff --output` writes a file"
    if inv.not_plain or inv.dirs is None:
        return f"{what}; its plain form only ({inv.not_plain or 'run-time directory'})"
    if len(inv.rest) != 3 or inv.rest[:2] != PATCH_SAVE or not inv.rest[2].startswith("--output="):
        return f"{what}; only `git -C <worker worktree> diff --cached --binary --output=<file>`"
    if any(_tree_kind(ctx, d) != "worker" for d in inv.dirs):
        return f"{what}; only in a worker worktree under {ctx.cfg['git_guard']['worktrees_dir']}"
    target = inv.rest[2].split("=", 1)[1]
    patches = ctx.root / ctx.cfg["git_guard"]["patch_dir"]
    paths = _resolve_all(target, inv.dirs) if target else None
    if not paths or not all(_inside(p, patches) and _real(p) != _real(patches) for p in paths):
        return f"{what}; `{target[:60]}` is not a file under {ctx.cfg['git_guard']['patch_dir']} of the main tree"
    return None


def _first_word(args: list[str]) -> str | None:
    """The first word that is not an option (skipping git's `-c`/`-C` values): the subcommand if the program is git."""
    k = 0
    while k < len(args):
        if args[k] in ("-c", "-C"):
            k += 2
        elif args[k].startswith("-"):
            k += 1
        else:
            return args[k].lower()
    return None


class GitIdentity(NamedTuple):
    """Which repository git resolves for a directory (git-rev-parse(1)), each path resolved and case-folded."""

    toplevel: str  # `--show-toplevel`: the root of the working tree (`core.worktree` can point it anywhere)
    git_dir: str  # `--absolute-git-dir`: this working tree's own Git directory
    common_dir: str  # `--git-common-dir`: the repository shared by all its worktrees


def _real(path: str | Path) -> str:
    return st.fs_key(path)


def _git_identity(repo: Path, timeout: int) -> GitIdentity | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--show-toplevel"]
            + ["--absolute-git-dir", "--git-common-dir"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    if proc.returncode != 0 or len(lines) != 3 or not all(lines):
        return None
    return GitIdentity(*(_real(line) for line in lines))


def _tree_kind(ctx: Context, d: Path) -> str | None:
    """"main" when git resolves for `d` the project's own repository (toplevel, Git directory and common directory all
    equal those of the project root), "worker" when it resolves a linked worktree of that repository whose toplevel lies
    below the configured worktrees dir and whose Git directory names it back, else None (another repository, a
    `core.worktree` pointing here, or git cannot tell: fail closed; class G, review R10)."""
    timeout = ctx.cfg["git_timeout_seconds"]
    if not _inside(d, ctx.root):
        return None
    if ctx.root_identity is None:
        ctx.root_identity = _git_identity(ctx.root, timeout) or False
    own = ctx.root_identity
    here = _git_identity(d, timeout)
    if not own or here is None:
        return None
    worktrees = ctx.root / ctx.cfg["git_guard"]["worktrees_dir"]
    if not _inside(d, worktrees):
        return "main" if here == own and here.toplevel == _real(ctx.root) else None
    linked = (
        here.common_dir == own.common_dir
        and _real(Path(here.git_dir).parent) == _real(Path(own.common_dir) / "worktrees")
        and _inside(Path(here.toplevel), worktrees)
        and here.toplevel != _real(worktrees)
        and _inside(d, Path(here.toplevel))
    )
    if not linked:
        return None
    try:  # gitrepository-layout(5) `worktrees/<id>/gitdir`: the path of the worktree's `.git` file
        back = (Path(here.git_dir) / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return "worker" if _real(back) == _real(Path(here.toplevel) / ".git") else None


def _paths_only(words: list[str]) -> bool:
    return bool(words) and all(w and not w.startswith("-") for w in words)


def _plain_names(words: list[str]) -> bool:
    """Remote and branch words without option, refspec (`a:b`, `+a`) or range syntax."""
    return all(w and not w.startswith("-") and not any(ch in w for ch in ":+^~*") for w in words)


def _revision(word: str) -> bool:
    """A ref name or object id with optional parent suffixes (`HEAD~1`, `abc123^2`); no `a..b` range."""
    return bool(REVISION.fullmatch(word)) and ".." not in word


def _form_add(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    paths: list[str] = []
    all_flag = False
    options_done = False
    for a in rest:
        if not options_done and a == "--":
            options_done = True
        elif not options_done and a == "-A":
            all_flag = True
        elif not options_done and a.startswith("-"):
            return f"option `{a}` (only `-A` and paths)"
        else:
            paths.append(a)
    return None if paths or all_flag else "no paths and no `-A`"


def _form_commit(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    sources: list[str] = []
    k = 0
    while k < len(rest):
        a = rest[k]
        if a == "-q":
            k += 1
        elif a in ("-m", "-F"):  # each takes the next word as its value, whatever it looks like (review R8)
            if k + 1 >= len(rest):
                return f"`{a}` without its value"
            sources.append(a)
            k += 2
        else:
            return f"`{a}` (only `-q`, `-m <message>`, `-F <file>`)"
    # git-commit(1): "If multiple -m options are given, their values are concatenated as separate paragraphs"; "The
    # -m option is mutually exclusive with -c, -C, and -F". Without either git opens an editor (item 9).
    if not sources:
        return "no message: give `-m <message>` or `-F <file>`, so no editor opens"
    if "-F" in sources and len(sources) > 1:
        return "one message source: `-m <message>`... or a single `-F <file>`"
    return None


def _form_push(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    gg = ctx.cfg["git_guard"]
    want = [gg["push_remote"], gg["push_branch"]]
    return None if rest == want else f"only `git push {' '.join(want)}` (git_guard.push_remote, push_branch)"


def _form_revert(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    ok = len(rest) == 2 and rest[0] == "--no-edit" and _revision(rest[1])
    return None if ok else "only `git revert --no-edit <commit>` (a ref or id, optionally with `~<n>`/`^<n>`)"


def _form_pull(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    ok = rest[:1] == ["--ff-only"] and len(rest) <= 3 and _plain_names(rest[1:])
    return None if ok else "only `git pull --ff-only [<remote> [<branch>]]`, no other option or refspec"


def _form_fetch(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    return None if len(rest) <= 1 and _plain_names(rest) else "only `git fetch [<remote>]`, no option or refspec"


def _form_reset(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    tail = rest[1:] if rest[:1] == ["HEAD"] else rest
    ok = rest == [] or (tail[:1] == ["--"] and _paths_only(tail[1:]))
    return None if ok else "only bare `git reset` or `git reset [HEAD] -- <paths>`"


def _form_restore(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    tail = rest[1:]
    if tail[:1] == ["--"]:
        tail = tail[1:]
    ok = rest[:1] == ["--staged"] and _paths_only(tail)
    return None if ok else "only `git restore --staged [--] <paths>`"


def _form_apply(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    tail = rest[1:] if rest[:1] == ["--check"] else rest
    return None if len(tail) == 1 and _paths_only(tail) else "only `git apply [--check] <patch>`"


def _worker_path(word: str, ctx: Context, dirs: list[Path]) -> bool:
    top = ctx.root / ctx.cfg["git_guard"]["worktrees_dir"]
    paths = _resolve_all(word, dirs) if not word.startswith("-") else None
    return bool(paths) and all(
        _inside(p, top) and st.fs_key(p) != st.fs_key(top)
        for p in paths or []
    )


def _form_worktree(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    ok = (
        rest == ["prune"]
        or (len(rest) == 2 and rest[0] == "remove" and _worker_path(rest[1], ctx, dirs))
        or (
            len(rest) == 4
            and rest[:2] == ["add", "--detach"]
            and _worker_path(rest[2], ctx, dirs)
            and _revision(rest[3])
        )
    )
    return (
        None
        if ok
        else "only `git worktree add --detach <worker path> <rev>`, `worktree remove <worker path>`, `worktree prune`"
    )


def _form_branch(rest: list[str], ctx: Context, dirs: list[Path]) -> str | None:
    ok = len(rest) == 2 and rest[0] == "-d" and _plain_names(rest[1:])
    return None if ok else "only `git branch -d <name>` (git refuses an unmerged branch)"


WRITE_FORMS: dict[str, Callable[[list[str], Context, list[Path]], str | None]] = {
    "add": _form_add,
    "commit": _form_commit,
    "push": _form_push,
    "revert": _form_revert,
    "pull": _form_pull,
    "fetch": _form_fetch,
    "reset": _form_reset,
    "restore": _form_restore,
    "apply": _form_apply,
    "worktree": _form_worktree,
    "branch": _form_branch,
}


def _write_form(ctx: Context, inv: GitInvocation) -> str | None:
    """pm mode: None when the call is one of the orchestrator's write forms (AGENTS.md section 11, ORCHESTRATION
    section 4), else why not. A form is matched on the subcommand as typed (aliases are never expanded for a write),
    in its plain spelling, in a proven repository, with its own option table and arity."""
    sub = inv.verbatim or ""
    if sub not in WRITE_FORMS:
        return (
            f"`git {inv.sub}` is neither read-only nor an orchestrator write form ({', '.join(MAIN_TREE_FORMS)}); "
            "aliases and other subcommands are never run as a write"
        )
    if inv.not_plain:
        return f"`git {sub}` writes only in its plain form, but {inv.not_plain}"
    if inv.dirs is None:
        return f"`git {sub}`: its repository is computed at run time, so it is not provably the main working tree"
    kinds = {_tree_kind(ctx, d) for d in inv.dirs}
    if None in kinds:
        return (
            f"`git {sub}`: its repository is neither the main working tree nor a worker worktree under "
            f"{ctx.cfg['git_guard']['worktrees_dir']}"
        )
    if "worker" in kinds and sub not in WORKER_TREE_FORMS:
        return (
            f"`git {sub}` inside a worker worktree: there the orchestrator only stages and applies "
            "(`git add`, `git apply`); workers' branches are never committed"
        )
    problem = WRITE_FORMS[sub](inv.rest, ctx, inv.dirs)
    if problem:
        return f"`git {sub} {' '.join(inv.rest)[:80]}` is not an allowed form: {problem}"
    ctx.ledger = True  # T-024 round 5, class J: the ref transitions of an allowed write go to the ledger
    return None


BASH_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=", re.S)  # protocol-bound: a bash `NAME=value` prefix word


def _real_head(cmd: st.Cmd) -> st.Cmd:
    """The command with every leading bash `NAME=value` word and wrapper (`env`, `timeout 9`, `command`, ...) skipped,
    repeated until neither is left: the parser keeps a quoted prefix (`X="y" env git ...`) as the command word, which
    hid both the wrapper and the program after it (review R2 reopened). Skipped wrappers join `cmd.wrappers`."""
    if cmd.head is None or cmd.shell != "bash":
        return cmd
    head, wrappers = cmd.head, list(cmd.wrappers)
    while True:
        start = head
        while start < len(cmd.words) - 1 and BASH_ASSIGNMENT.match(str(cmd.words[start])):
            start += 1
        offset, _ = pg._command_head(cmd.words[start:])
        if offset is None:
            break
        wrappers += [pg._head(w) for w in cmd.words[start : start + offset] if pg._head(w) in pg.WRAPPERS]
        if start + offset == head:
            break
        head = start + offset
    return cmd if head == cmd.head else cmd._replace(head=head, wrappers=wrappers)


# T-024 round 4, class F: one program-name normaliser for every rule. Windows resolves a bare name through PATHEXT
# (`.COM;.EXE;.BAT;.CMD;...` on this machine) and PowerShell also runs `.ps1`; versioned names (`python3.12`,
# `ruby3.4`, `node22`) and Debian's `nodejs` are the same interpreter (protocol-bound: OS and package naming).
STEM_SUFFIX = re.compile(r"(?i)\.(?:exe|com|bat|cmd|ps1)$")
STEM_VERSION = re.compile(r"(?<=[a-z])[-_]?\d[\d.]*$")
STEM_ALIASES = {"nodejs": "node"}
# A command that assigns one of these selects which file a bare program name runs (git(1) PATH; Windows PATHEXT), so
# no git call may share its command line (round 4, R13; protocol-bound: OS program lookup).
PROGRAM_SELECTORS = re.compile(
    r"(?i)(?<![\w$.])(?:PATH|PATHEXT)\s*\+?="
    r"|\b(?:export|set|setx|declare|typeset|local)\s+(?:-\w+\s+)*(?:PATH|PATHEXT)\b"
)
# Forms that name the variable inside quotes: `Set-Item 'env:PATH' x`, `[Environment]::SetEnvironmentVariable('PATH'`.
PROGRAM_SELECTORS_RAW = re.compile(
    r"(?i)\b(?:set-item|si|new-item|ni|remove-item|ri|clear-item|cli)\s+(?:-\w+\s+)*['\"]?env:\s*(?:PATH|PATHEXT)\b"
    r"|SetEnvironmentVariable\s*\(\s*['\"](?:PATH|PATHEXT)['\"]"
)


def program_stem(word: str) -> str:
    """The program a word names, compared by stem: basename, lower case, executable and version suffix removed."""
    base = STEM_SUFFIX.sub("", re.split(r"[\\/]", word)[-1].lower())
    base = STEM_VERSION.sub("", base) if not GIT_PROGRAM.match(base) else base
    return STEM_ALIASES.get(base, base)


def _names_git(word: str) -> bool:
    """A word that names the git program: `git`, `git.exe`, `git.cmd`, ... (any directory), or a bare `git-<command>`."""
    stem = program_stem(word)
    bare = not re.search(r"[\\/]", word)
    return stem == "git" or (bare and bool(GIT_PROGRAM.match(stem)))


def _given_a_program(args: list[str]) -> str | None:
    """Class F, by argument role: the first argument that names git, or names a shell or launcher whose later
    arguments name git (`uv run git.cmd ...`, `chrt -b 0 sh -c "git ..."`)."""
    for k, a in enumerate(args):
        if _names_git(a):
            return a
        if program_stem(a) in LAUNCHERS and any(GIT_WORD.search(b) for b in args[k + 1 :]):
            return a
    return None


def _selects_programs(ctx: Context) -> str | None:
    """The command assigns PATH or PATHEXT (bash prefix or statement, `export`, PowerShell `$env:`)."""
    for raw, masked, _shell in ctx.walk().texts:
        # The masked text hides quoted words (`-m "PATH=x"` stays a message); `Set-Item 'env:PATH'` names the
        # variable inside quotes, so that one form is read from the raw text, as `GIT_ENV_RAW` does.
        hit = PROGRAM_SELECTORS.search(masked) or PROGRAM_SELECTORS_RAW.search(raw)
        if hit:
            return hit.group(0).strip()
    return None


def check_git(ctx: Context) -> list[str]:
    walked = ctx.walk()
    mode = ctx.args.git_mode
    worker = mode != "pm"
    reasons: list[str] = []
    if walked.too_deep and GIT_TRIGGER.search(ctx.trigger_text):
        reasons.append("the command nests too deeply to inspect its git calls")
    if worker and _git_env_set(ctx):
        reasons.append("workers may not set GIT_* environment variables")
    mentions_git = bool(GIT_TRIGGER.search(ctx.trigger_text))
    history_words = GIT_WRITE_WORDS if worker else PM_HISTORY_WORDS
    dangerous = history_words.search(ctx.trigger_text)
    if not worker and mentions_git and dangerous and _git_env_set(ctx):
        # Text level, so a call the parser misreads still fails closed: a write runs with no GIT_* variable.
        reasons.append(
            f"the command sets a `GIT_*` variable next to `{dangerous.group(0)}`; git writes run without them"
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
    selector = _selects_programs(ctx)
    any_git = any(c.head is not None and _names_git(_real_head(c).name) for c in walked.cmds)
    if selector and (any_git or GIT_WORD.search(ctx.trigger_text)):
        # Round 4, R13: a command-local PATH or PATHEXT change can make `git` run a shim; reads included.
        reasons.append(
            f"the command sets `{selector[:40]}` next to a git call, so `git` may run another program; set no "
            "PATH or PATHEXT in a git command"
        )
    for cmd in walked.cmds:
        cmd = _real_head(cmd)
        name = cmd.name
        args = [str(a) for a in cmd.args]
        is_git = _names_git(name)  # `git`, `git.exe`, `git.cmd`, `git-commit` (item 9, round 4 item 6)
        gits = [c for c in st.resolved_cmds(cmd, walked) or [] if _names_git(c.name)]
        head_text = str(cmd.words[cmd.head]) if cmd.head is not None else ""
        # Round 4, class F: wrappers before the program, or the code-carrying arguments it was read from.
        through = sorted(set(cmd.wrappers) - SHELL_KEYWORDS) or [v for v in cmd.via if v not in SHELL_KEYWORDS]
        # Class F, both modes: the code an interpreter runs inline is opaque, so naming git there is refused
        # (review R12); a script run by path is reviewed repository code (accepted residual).
        code = _interpreter_code(cmd, ctx) if cmd.head is not None else None
        if code is not None and GIT_TRIGGER.search(code):  # any `git` text: code can glue the name (`'gi'+'t'`)
            reasons.append(f"`{name}` inline code names git; the guard cannot read what it runs (review R12)")
        if cmd.head is not None and (
            (program_stem(name) in LAUNCHERS and any(GIT_WORD.search(a) for a in args))
            or ((is_git or gits) and through)
        ):
            via = f"`{through[0]}`" if (is_git or gits) and through else f"`{name}`"
            reasons.append(f"{via} runs git through another program or shell (review R2); run git directly")
        elif (
            cmd.head is not None
            and not (is_git or gits)
            and not cmd.launcher  # a configured launcher (`test_slot.py -- git ...`): its command is walked itself
            and name not in READ_ONLY_PROGRAMS
            and _interp_family(name, ctx) is None
            and _given_a_program(args)
        ):
            # Class F: `uv run git.cmd`, `watch git`, `chrt -b 0 sh -c "git ..."`: a program given git, or a shell
            # that runs git, as an argument may run it with argv the guard never sees.
            reasons.append(
                f"`{name}` is given `{_given_a_program(args)}` as an argument and may run git; run git directly"
            )
        elif cmd.head is not None and name in ALIAS_HEADS and mentions_git:
            reasons.append(f"`{name}` defines a command in a command that mentions git")
        elif cmd.head is not None and (is_git or gits):
            for call in [cmd] if is_git else gits:
                reason = _git_command(call, ctx, resolved=call is not cmd)
                if reason:
                    reasons.append(reason)
        elif st.RUN_TIME_CHARS.search(head_text) and (
            history_words.fullmatch(_first_word(args) or "") or any(GIT_WORD.search(a) for a in args)
        ):
            # `& $g rebase`: a program named by a variable the command does not set may be git (review F3).
            reasons.append(f"`{head_text[:40]} {' '.join(args)[:40]}`: the program is named at run time")
    if reasons:
        hint = (
            "The orchestrator writes with plain git forms only (add, commit -m, push <remote> <branch>, revert "
            "--no-edit, pull --ff-only, fetch, reset/restore of paths, apply, worktree add/remove/prune, branch -d) "
            "from the main working tree; it never rewrites history or deletes refs (AGENTS.md section 11, "
            "decision 77)."
            if not worker
            else "Workers run read-only git only (status, diff, log, show, ...); the orchestrator commits and "
            "pushes (AGENTS.md section 11, decision 77)."
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


class PushSource(NamedTuple):
    """One reading of what `git push <remote> <branch>` sends (round 4, class H)."""

    on_branch: bool  # HEAD is the symbolic ref `refs/heads/<branch>`
    head: str  # HEAD's commit id ("" when unborn)
    tip: str  # the commit id of `refs/heads/<branch>` ("" when the branch does not exist)


def _push_source(repo: Path, branch: str, timeout: int) -> PushSource | None:
    """HEAD and the configured branch from one git call (review R11 reopened: two calls let the ref move between
    them): the branch's `for-each-ref` row carries `%(objectname)` and `%(HEAD)` ("*" when HEAD is that branch), so
    the commit HEAD names and the tip the push sends come from the same reading. None when git cannot tell; the
    caller then refuses the push (T-024 final, PD2: the push check fails closed)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "--format=%(HEAD) %(objectname)", branch],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.decode("ascii", "replace").strip().lower()
    if not line:
        return PushSource(False, "", "")
    mark, _, sha = line.rpartition(" ")
    if not COMMIT_ID.fullmatch(sha):
        return None
    on = mark.strip() == "*"
    return PushSource(on, sha if on else "", sha)


def _push_reasons(ctx: Context) -> list[str]:
    """Why each `git push` of the command is refused. T-024 final (F22, PD2): a push passes only when the marker of
    its repository's HEAD is proved present; a repository, HEAD or branch the guard cannot read refuses it."""
    cfg = ctx.cfg["push_guard"]
    bases = ctx.bases()
    markers = ctx.root / cfg["marker_dir"]
    walked = ctx.walk()
    reasons: list[str] = []
    moved: str | None = None  # the first earlier call that moves HEAD
    for cmd in walked.cmds:
        if cmd.head is None:
            continue
        gits = (
            [cmd]
            if _names_git(cmd.name)
            else [c for c in st.resolved_cmds(cmd, walked) or [] if _names_git(c.name)]
        )
        for call in gits:
            parsed = _git_call(call)
            if parsed is None:
                continue
            c_dirs, sub, rest, redirected, alias_cfg = parsed
            if _moves_head(sub, rest, alias_cfg):
                moved = moved or f"git {sub}" + ("" if sub in HEAD_MOVERS else " (maybe an alias)")
                continue
            if sub != "push" or _push_dry_run(rest):
                continue
            if redirected:
                reasons.append("the push names its repository with `--git-dir`/`--work-tree`; the guard cannot prove it")
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
                reasons.append("the push runs in a directory chosen at run time, so its repository is not provable")
                continue
            # Class H (review R11): `git push <push_remote> <push_branch>` sends the tip of that branch, while the
            # marker names the checked HEAD. They are one revision only when HEAD is that branch; a detached HEAD or
            # another branch is refused. Both come from one reading per repository, taken again after the decision:
            # a ref that moved while the guard looked refuses (round 4). git failing keeps the fail-open policy.
            branch = f"refs/heads/{ctx.cfg['git_guard']['push_branch']}"
            timeout = ctx.cfg["git_timeout_seconds"]
            heads: list[str] = []
            for d in dirs:
                first = _push_source(d, branch, timeout)
                if first is None:
                    reasons.append(f"git cannot read HEAD and `{branch}` of `{d}`, so the pushed commit is not provable")
                    continue
                if not first.on_branch:
                    reasons.append(
                        f"HEAD of `{d}` is not `{branch}`: the push sends the tip of `{branch}`, which is not the "
                        f"checked HEAD; check out `{branch}`, run the check, then push"
                    )
                elif first.tip:
                    heads.append(first.tip)
                else:
                    reasons.append(f"`{branch}` of `{d}` has no commit, so there is no checked commit to push")
                if _push_source(d, branch, timeout) != first:
                    reasons.append(
                        f"HEAD or `{branch}` of `{d}` changed while the guard checked it; push again"
                    )
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
    """PM mode: `git push` needs the marker the prepush lint writes for the pushed HEAD. T-024 final (F22, PD2): the
    check fails closed, so its own crash refuses a command that may push; commands without the word `push` never
    reach it, so read and repair commands (`git status`, `git log`) stay open."""
    if ctx.args.git_mode != "pm" or not PUSH_TRIGGER.search(ctx.trigger_text):
        return []
    try:
        reasons = _push_reasons(ctx)
    except Exception as exc:
        _crash_log("push_guard", ctx)
        reasons = [f"the push check failed ({type(exc).__name__}), so the pushed commit is not proved checked"]
    if reasons:
        reasons.append(
            f"Run {ctx.cfg['push_guard']['check_command']} on the commit to push, then push"
        )
    return reasons


def _code_letters(flags: list[str]) -> str:
    return "".join(f[1] for f in flags if len(f) == 2 and f[0] == "-" and f[1] != "-")


def _interp_family(name: str, ctx: Context) -> str | None:
    if st.is_python(name) or name.startswith("$"):
        return "python"  # `& $PY -c ...`: an interpreter held in a variable is read like python
    stem = program_stem(name)  # round 4, item 2: `nodejs`, `ruby3.4`, `node.exe` are the configured families
    if stem == "python":
        return "python"
    return stem if stem in ctx.cfg["inline_interp"]["code_flags"] else None


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
        ("push_guard", check_push, PUSH_TRIGGER),  # F22: a crash fails closed for a command that may push
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
    # Decision 77 removed the merge mode (`worker-merge --worktree D`): workers never merge. A hook line that still
    # names it fails to parse, so that worker's shell tools stay blocked (fail closed) until it is restarted.
    parser.add_argument("--git-mode", choices=GIT_MODES, required=True)
    return parser.parse_args(argv)


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
    if args.git_mode == "pm" and ctx.ledger:
        try:
            gl.begin(root, event, ctx.cwd, cfg["git_timeout_seconds"])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # The command still runs; its ref changes stay unexplained, so a concurrent worker turn is flagged.
            _log(ctx.tool, ctx.command, [f"git ledger not recorded: {type(exc).__name__}"])
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
