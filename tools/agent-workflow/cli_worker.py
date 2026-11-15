"""PM launcher for Claude Code CLI worker sessions (decision 232). Documentation: tools/agent-workflow/README.md.

Each worker is one persistent CLI conversation. The PM starts it with a brief and keeps
talking to it with `send` (same session id), so the 1-hour prompt cache is reused.
Workers get an explicit tool allowlist without Agent/Task/Workflow (no subagents), MCP
servers per --mcp (default: unchanged; minimal: none), and permission rules passed through
--settings. Workers never commit, push or merge; the orchestrator commits and pushes (decision 77).

Usage (run through the PowerShell tool with run_in_background; --profile <role> is the normal path, decision 379):
  python -B cli_worker.py start --name w1 --profile dev-small --model sonnet --effort high --cwd <worktree> --message-file <brief>
  python -B cli_worker.py send  --name w1 --message-file <msg> [--effort xhigh] [--timeout-minutes 240]
  python -B cli_worker.py status
  python -B cli_worker.py retire --name w1 [--force] [--purge-transcript]
Codex workers (decision 596; README.md "Codex workers") use the same commands with `--backend codex`:
  python -B cli_worker.py start --backend codex --profile codex-dev --codex-config <local codex_profiles copy> --name c1 --cwd <worktree> --message-file <brief>
Model, effort, web mode and timeout come from the validated Codex config; `notify` is not available for them.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
# Native Windows exposes this builtin independently of mutable sys.platform/os.name strings.
NATIVE_WINDOWS = hasattr(sys, "getwindowsversion")
# The PM state directory, by the hooks' rule (hooks/ctx_nudge.py, git_ledger.ledger_dir): AGENT_WORKFLOW_STATE_DIR,
# else <ROOT>/.tmp/pm-state. Everything the launcher later trusts to judge a worker lives here, where neither the
# shell guard nor the native write denies let a worker write (T-024 final round, CX2 and Gemini P3).
STATE_ENV = "AGENT_WORKFLOW_STATE_DIR"  # protocol-bound: shared with every hook


def state_root() -> Path:
    """The state directory now (ROOT and the environment read at call time, so a test's ROOT override follows)."""
    return Path(os.environ.get(STATE_ENV) or ROOT / ".tmp" / "pm-state")


STATE = state_root()
REGISTRY = STATE / "cli-workers.json"
LOCK = REGISTRY.with_suffix(".lock")
JOBS = ROOT / ".tmp" / "cli-jobs"
# Same directory as hooks/ctx_nudge.py DEFAULT_INBOX.
INBOX = STATE / "inbox"
LINT_WAIVERS_NAME = "lint-waivers.jsonl"  # artifact-bound: read by the PM
# Claude Code keeps each session as <projects>/<cwd-slug>/<session_id>.jsonl plus a <session_id>/ directory;
# only `retire --purge-transcript` deletes them.
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Registry critical sections last milliseconds: a lock file older than this belongs to a launcher that died.
STALE_LOCK_SECONDS = 30
# `os.replace` onto a registry that a reader holds open fails on Windows (no delete sharing); readers hold it for milliseconds.
REPLACE_ATTEMPTS = 40
REPLACE_WAIT_SECONDS = 0.05
# Tail of a live log read by `status`: the last assistant event is far smaller than this.
LOG_TAIL_BYTES = 262144
# The normal subagent tool set (decision 233), minus orchestration, scheduling, cross-session and publishing tools.
# NotebookEdit is withheld (T-024 final round, CX6; PM decision pending user review): no primary source or bounded
# live check shows that the Edit/Write deny rules govern it, so it is not offered until one does.
TOOLS = (
    "Bash,Edit,Glob,Grep,Monitor,PowerShell,Read,ReportFindings,Skill,TaskStop,ToolSearch,"
    "WebFetch,WebSearch,Write,ListMcpResourcesTool,ReadMcpResourceTool,ReadMcpResourceDirTool"
)
DENIED_TOOLS = "Agent,Task,Workflow,SendMessage,ListAgents,CronCreate,CronDelete,RemoteTrigger,ScheduleWakeup,PushNotification,Artifact,ArtifactComments,ArtifactData,DesignSync,EnterWorktree,ExitWorktree,NotebookEdit"


def permission_path(path: Path) -> str:
    """Absolute path in Claude Code permission-rule syntax: `//d/dir` on Windows, `//dir` on POSIX."""
    posix = path.as_posix()
    if path.drive:
        return "//" + path.drive[0].lower() + posix[2:]
    return "/" + posix


# Claude Code permission-rule names of the built-in file writers the worker is offered (protocol-bound). The only other
# built-in writer, NotebookEdit, is not offered at all (TOOLS, DENIED_TOOLS), so no unverified rule shape is relied on.
NATIVE_WRITE_TOOLS = ("Edit", "Write")


def _git_link_dirs(root: Path) -> list[Path]:
    """The Git directories outside `root` that `root/.git` names when it is a link file (`gitdir: <path>`, the layout of
    a linked worktree): its private directory and, under `<common>/worktrees/`, the common directory. Read
    structurally, no git process: the rules are built at import."""
    link = root / ".git"
    try:
        text = link.read_text(encoding="utf-8") if link.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return []
    found: list[Path] = []
    for line in text.splitlines():
        if line.startswith("gitdir: "):  # protocol-bound: gitrepository-layout(5) link file
            private = (root / line[len("gitdir: ") :].strip()).resolve()
            found.append(private)
            if private.parent.name == "worktrees":
                found.append(private.parent.parent)
    return found


def protected_write_rules(root: Path, state_dir: Path, tooling: Path, home: Path) -> list[str]:
    """Deny rules for native file writes (T-024 final round, F05 and PD1): Git internals of every checkout under the
    root, the tooling, the policy folders and the whole PM state directory. Hooks write that directory from their own
    Python process, which these rules do not govern. Built from the given and the resolved spelling of each path,
    since the rule matcher's link handling is not documented here (report-D)."""

    def forms(path: Path) -> list[str]:
        return list(dict.fromkeys(permission_path(p) for p in (path, path.resolve())))

    # Relative rules apply under the worker's cwd; the absolute ones also cover the root it reaches with --add-dir.
    # Git internals and `.claude/` (the CLI reads settings, hooks and skills from its working folder, so a worktree's
    # copy governs the worker) are denied in every checkout. The tooling and `.agents/` only in the main tree: the
    # hooks run from there (HOOKS and the rendered absolute paths), so a worktree copy is data a tooling task may edit
    # (PM decision pending user review, T-024 package D QA feedback).
    patterns = ["**/.git", "**/.git/**", "**/.claude/**"]
    for base in forms(root):
        for tail in (".git", ".git/**", ".claude/**"):
            patterns += [f"{base}/{tail}", f"{base}/**/{tail}"]
        patterns += [f"{base}/tools/agent-workflow/**", f"{base}/.agents/**"]
    for folder in (tooling, state_dir, home / ".claude", *_git_link_dirs(root)):
        patterns += [f"{base}/**" for base in forms(folder)]
    unique = list(dict.fromkeys(patterns))
    return [f"{tool}({pattern})" for tool in NATIVE_WRITE_TOOLS for pattern in unique]


HOOKS = Path(__file__).resolve().parent / "hooks"


def git_ledger() -> Any:
    """hooks/git_ledger.py (round 5): the compared state and the orchestrator's write ledger."""
    spec = importlib.util.spec_from_file_location("git_ledger", HOOKS / "git_ledger.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {HOOKS / 'git_ledger.py'}")
    sys.path.insert(0, str(HOOKS))  # git_ledger imports guard_config next to it
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state_dir(root: Path) -> Path:
    """The PM state directory the hooks use: the parent of hooks/git_ledger.py's `ledger_dir` (PD1: derived, so an
    AGENT_WORKFLOW_STATE_DIR override moves the protection with it)."""
    return Path(git_ledger().ledger_dir(root)).parent


_SETTINGS_CACHE: dict[str, dict[str, Any]] = {}


def worker_settings(legacy: bool = False) -> dict[str, Any]:
    """The worker permission settings (`SETTINGS`, or `LEGACY_SETTINGS` with the prefix git denies), built on first
    use and then the same object: the state directory comes from hooks/git_ledger.py, which `status`, `retire` and the
    supervisor's copies never need (T-024 final round)."""
    key = "legacy" if legacy else "guarded"
    if key not in _SETTINGS_CACHE:
        base = {"permissions": {"allow": list(_ALLOW), "deny": [
            *_DENY_HEAD,
            # T-024: Git internals, tooling, policy folders and the PM state (its Git ledger excuses ref changes).
            *protected_write_rules(ROOT, state_dir(ROOT), Path(__file__).resolve().parent, Path.home()),
            *_DENY_TAIL,
            *(LEGACY_GIT_DENIES if legacy else []),
        ]}}
        _SETTINGS_CACHE[key] = base
    return _SETTINGS_CACHE[key]


def __getattr__(name: str) -> Any:
    """`SETTINGS` and `LEGACY_SETTINGS` stay readable as module attributes (PEP 562), built lazily."""
    if name == "SETTINGS":
        return worker_settings()
    if name == "LEGACY_SETTINGS":
        return worker_settings(legacy=True)
    raise AttributeError(name)


_ALLOW = (
    *TOOLS.split(","),
    "mcp__claude_ai_Context7",
    "mcp__claude_ai_Hugging_Face__hub_repo_search",
    "mcp__claude_ai_Hugging_Face__hub_repo_details",
    "mcp__claude_ai_Claude_Docs__read",
    "mcp__claude_ai_Claude_Docs__query",
    "mcp__claude_ai_Claude_Docs__guide",
)
_DENY_HEAD = (
    "Read(**/.secrets/**)",
    "Edit(**/.secrets/**)",
    "Write(**/.secrets/**)",
    "Grep(**/.secrets/**)",
    "Glob(**/.secrets/**)",
    f"Read({permission_path(ROOT)}/.secrets/**)",
)
_DENY_TAIL = (
    # Read-only research connectors only: no Drive, no document writes, no HF filesystem or Spaces.
    "mcp__claude_ai_Google_Drive",
    "mcp__claude_ai_Claude_Docs__create",
    "mcp__claude_ai_Claude_Docs__delete",
    "mcp__claude_ai_Claude_Docs__update",
    "mcp__claude_ai_Claude_Docs__batch",
    "mcp__claude_ai_Claude_Docs__export",
    "mcp__claude_ai_Hugging_Face__hf_fs",
    "mcp__claude_ai_Hugging_Face__dynamic_space",
    "mcp__claude_ai_Hugging_Face__hf_whoami",
    "mcp__plugin_bio-research_biorxiv",
    "mcp__plugin_bio-research_c-trials",
    "mcp__plugin_bio-research_chembl",
    "mcp__plugin_bio-research_consensus",
    "mcp__plugin_bio-research_ot",
    "mcp__plugin_bio-research_pubmed",
    "mcp__plugin_pdf-viewer_pdf",
)


# Prefix git denies for workers without the tool guard (started before it existed, or `--tool-guard off`); they are not a
# boundary (`git -C . commit` passes), which hooks/tool_guard.py's argv allowlist replaces.
LEGACY_GIT_DENIES = [
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "PowerShell(git commit*)",
    "PowerShell(git push*)",
]
GIT_GUARD_MODES = ("readonly",)  # the merge mode was removed by decision 77 (T-024)
MCP_MODES = ("default", "minimal")
# Advisor aliases accepted by `claude --advisor`; the CLI itself rejects a pairing the main model does not allow.
ADVISORS = ("off", "opus", "fable", "sonnet")
# `--mcp minimal` with --strict-mcp-config: the worker sees no MCP server (no tool schemas or server instructions).
MINIMAL_MCP_CONFIG = json.dumps({"mcpServers": {}})
DEFAULT_CTX_NUDGE = "170000"
DEFAULT_LOOP_WINDOW = 5
# Per-worker auto-compact window (user decision 2026-10-10: workers compact near 200K, a nudge at 170K asks for a
# checkpoint first). Claude Code compacts below the configured window: with a 400K window the 13 measured
# auto-compactions fired at 366-384K (mean 371K, offset 16-34K, mean 29K; .tmp/context-policy/data-report.md section 4).
# ASSUMPTION until a live run confirms it: the offset is constant, so 200K + 30K = 230K compacts near 200K.
DEFAULT_AUTOCOMPACT_WINDOW = "230000"
# Documented range of the window (claude --help: "100k-1M tokens"; env-vars and settings docs: 100000-1000000).
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000
AUTOCOMPACT_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
# Per-role window, nudge and cache TTL (decision 379); `start --profile <role>` reads it, an explicit flag overrides.
PROFILES = Path(__file__).resolve().parent / "worker_profiles.json"
PROFILE_REQUIRED = ("autocompact_window", "ctx_nudge", "cache_ttl")
PROFILE_OPTIONAL = ("model", "effort")
CACHE_TTLS = ("5m", "1h")
BACKENDS = ("claude", "codex")
# One JSONL row per worker turn (usage_snapshot.append_ledger); same state directory as the registry.
LEDGER = STATE / "worker-ledger.jsonl"
# Turn limit of a Claude worker when `--timeout-minutes` is not given (a Codex worker takes its role's value).
DEFAULT_TIMEOUT_MINUTES = 300


def sibling(name: str) -> Any:
    """Load a module that sits next to this script (codex_backend, usage_snapshot); the script runs without a package."""
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / f"{name}.py"
        )
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def venv_python() -> str:
    exe = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not exe.is_file():
        raise SystemExit(f"project interpreter missing: {exe}")
    return exe.as_posix()


def validate_ctx_nudge(text: str) -> str:
    """Same grammar as hooks/ctx_nudge.py: `off` or strictly ascending positive token counts."""
    if text.strip().lower() in ("off", "none", ""):
        return "off"
    try:
        values = [int(p) for p in text.split(",")]
    except ValueError:
        raise SystemExit(
            f"--ctx-nudge must be 'off' or comma-separated integers: {text!r}"
        ) from None
    if any(v <= 0 for v in values) or values != sorted(set(values)):
        raise SystemExit(f"--ctx-nudge values must be positive and strictly ascending: {text!r}")
    return ",".join(str(v) for v in values)


def guard_config() -> dict[str, Any]:
    """The validated hooks/guard_config.py configuration (the same file the guard hooks read)."""
    spec = importlib.util.spec_from_file_location("guard_config", HOOKS / "guard_config.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {HOOKS / 'guard_config.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.load()  # type: ignore[no-any-return]
    except module.ConfigError as exc:
        raise SystemExit(f"guard config: {exc}") from None


def validate_task_file(text: str | None, cwd: Path, worktrees: Path) -> str | None:
    """`--task-file`: an existing .md file inside the worker's own checkout (the PreCompact progress block goes there).

    A worktree worker given the root copy of its task file wrote progress into the root (PM mistake #30), so the
    file must sit under `cwd`, and a worker in the root may not point into a worktree.
    """
    if not text:
        return None
    path = Path(text).resolve()
    if not path.is_relative_to(ROOT) or path.suffix.lower() != ".md" or not path.is_file():
        raise SystemExit(
            f"--task-file must be an existing .md file inside the project root: {text!r}"
        )
    if not path.is_relative_to(cwd) or (
        not cwd.is_relative_to(worktrees) and path.is_relative_to(worktrees)
    ):
        raise SystemExit(
            f"--task-file must be inside the worker's checkout {cwd.as_posix()}: {text!r}"
        )
    return path.as_posix()


def validate_autocompact_window(text: str) -> int | None:
    """`off` (None: Claude Code's own window applies) or an integer token count inside the documented range."""
    if text.strip().lower() in ("off", "none", ""):
        return None
    try:
        value = int(text)
    except ValueError:
        raise SystemExit(
            f"--autocompact-window must be 'off' or an integer token count: {text!r}"
        ) from None
    if not AUTOCOMPACT_MIN <= value <= AUTOCOMPACT_MAX:
        raise SystemExit(
            f"--autocompact-window must be between {AUTOCOMPACT_MIN} and {AUTOCOMPACT_MAX}: {text!r}"
        )
    return value


def nudge_window_error(nudge: str, window: int | None) -> str | None:
    """The nudge must fire before compaction: its last threshold stays below the window (either `off` skips the check)."""
    if window is not None and nudge != "off" and int(nudge.split(",")[-1]) >= window:
        return (
            f"--ctx-nudge {nudge} must stay below --autocompact-window {window}: "
            "the nudge would fire only after compaction "
            "(a lower window than the profile's also needs an explicit lower --ctx-nudge)"
        )
    return None


def _profile_field(role: str, field: str, check: Any, value: Any) -> Any:
    try:
        return check(value)
    except SystemExit as exc:
        raise SystemExit(f"worker profile {role!r}, field {field!r}: {exc.code}") from None


def load_worker_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Validate worker_profiles.json (decision 379) and return role -> normalised settings.

    Every role must carry `autocompact_window`, `ctx_nudge` and `cache_ttl`; `model` and `effort` are optional
    (absent: the matching start flag stays required). Unknown keys, missing fields, invalid values and a nudge
    that does not stay below the window are refused with the role and field named.
    """
    path = PROFILES if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"worker profiles {path}: unreadable or not JSON: {exc}") from None
    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"worker profiles {path}: expected a non-empty object of role -> settings")
    profiles: dict[str, dict[str, Any]] = {}
    for role, entry in raw.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"worker profile {role!r}: expected an object")
        unknown = sorted(set(entry) - set(PROFILE_REQUIRED) - set(PROFILE_OPTIONAL))
        if unknown:
            raise SystemExit(f"worker profile {role!r}: unknown key(s) {', '.join(unknown)}")
        for field in PROFILE_REQUIRED:
            if field not in entry:
                raise SystemExit(f"worker profile {role!r}: missing field {field!r}")
        window_raw, nudge_raw = entry["autocompact_window"], entry["ctx_nudge"]
        if isinstance(window_raw, bool) or not isinstance(window_raw, (int, str)):
            raise SystemExit(
                f"worker profile {role!r}, field 'autocompact_window': expected an integer or 'off'"
            )
        if not isinstance(nudge_raw, str):
            raise SystemExit(f"worker profile {role!r}, field 'ctx_nudge': expected a string")
        window = _profile_field(
            role, "autocompact_window", validate_autocompact_window, str(window_raw)
        )
        nudge = _profile_field(role, "ctx_nudge", validate_ctx_nudge, nudge_raw)
        problem = nudge_window_error(nudge, window)
        if problem:
            raise SystemExit(f"worker profile {role!r}, field 'ctx_nudge': {problem}")
        ttl = entry["cache_ttl"]
        if ttl not in CACHE_TTLS:
            raise SystemExit(
                f"worker profile {role!r}, field 'cache_ttl': must be one of {', '.join(CACHE_TTLS)}: {ttl!r}"
            )
        resolved: dict[str, Any] = {
            "autocompact_window": window,
            "ctx_nudge": nudge,
            "cache_ttl": ttl,
        }
        if "model" in entry:
            if not isinstance(entry["model"], str) or not entry["model"].strip():
                raise SystemExit(
                    f"worker profile {role!r}, field 'model': expected a non-empty string"
                )
            resolved["model"] = entry["model"]
        if "effort" in entry and entry["effort"] not in EFFORTS:
            raise SystemExit(
                f"worker profile {role!r}, field 'effort': must be one of {', '.join(EFFORTS)}: {entry['effort']!r}"
            )
        if "effort" in entry:
            resolved["effort"] = entry["effort"]
        profiles[role] = resolved
    return profiles


def resolve_profile(role: str, path: Path | None = None) -> dict[str, Any]:
    profiles = load_worker_profiles(path)
    if role not in profiles:
        raise SystemExit(f"unknown worker profile {role!r}; known: {', '.join(sorted(profiles))}")
    return profiles[role]


def apply_profile(ap: argparse.ArgumentParser, a: argparse.Namespace) -> None:
    """Fill the unset start settings: explicit flag, else the --profile value, else (no profile) the legacy default."""
    # An empty --profile is an unknown role (refused), never "no profile".
    profile = resolve_profile(a.profile) if a.profile is not None else {}
    for attr in ("model", "effort", "cache_ttl", "ctx_nudge", "autocompact_window"):
        if getattr(a, attr) is None and attr in profile:
            value = profile[attr]
            setattr(a, attr, "off" if value is None else str(value))  # window None means `off`
    if a.profile is None:
        a.ctx_nudge = DEFAULT_CTX_NUDGE if a.ctx_nudge is None else a.ctx_nudge
        a.autocompact_window = (
            DEFAULT_AUTOCOMPACT_WINDOW if a.autocompact_window is None else a.autocompact_window
        )
    for attr in ("model", "effort", "cache_ttl"):
        if getattr(a, attr) is None:
            ap.error(f"start needs --{attr.replace('_', '-')} (or a --profile that sets it)")


def _hook(py: str, script: str, *args: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": py,
        "args": ["-B", "-I", (HOOKS / script).as_posix(), *args],
        "timeout": 30,
    }


def hook_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Optional hooks block for workers started with guards (absent for workers started before the guards existed).
    Without the tool guard (older workers, `--tool-guard off`) the prefix git denies stay as the fallback."""
    wants_nudge = cfg.get("ctx_nudge", "off") != "off" or cfg.get("loop_window", 0) > 0
    tool_guard = bool(cfg.get("tool_guard"))
    settings = worker_settings(legacy=not tool_guard)
    if (
        not cfg.get("process_guard")
        and not wants_nudge
        and not tool_guard
        and not cfg.get("task_file")
    ):
        return settings
    py = venv_python()
    hooks: dict[str, Any] = {}
    if cfg.get("process_guard"):
        hooks["PreToolUse"] = [
            {"matcher": "Bash|PowerShell", "hooks": [_hook(py, "process_guard.py")]}
        ]
    if tool_guard:
        # Workers never merge (decision 77): a record that still says `git_guard: merge` gets the read-only guard.
        guard_args = ["--git-mode", "worker"]
        hooks.setdefault("PreToolUse", []).append(
            {
                "matcher": "Bash|PowerShell|Monitor",
                "hooks": [_hook(py, "tool_guard.py", *guard_args)],
            }
        )
        hooks["PostToolUse"] = [{"matcher": "*", "hooks": [_hook(py, "tool_monitor.py")]}]
        hooks["PostToolUseFailure"] = [{"matcher": "*", "hooks": [_hook(py, "tool_monitor.py")]}]
        hooks["SessionStart"] = [{"hooks": [_hook(py, "tool_health.py", "session-start")]}]
    if cfg.get("task_file"):
        hooks["PreCompact"] = [
            {"hooks": [_hook(py, "worker_progress.py", "--task-file", str(cfg["task_file"]))]}
        ]
    if wants_nudge:
        hooks.setdefault("PostToolUse", []).insert(
            0,
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": py,
                        "timeout": 30,
                        "args": [
                            "-B",
                            "-I",
                            (HOOKS / "ctx_nudge.py").as_posix(),
                            "--thresholds",
                            cfg.get("ctx_nudge", "off"),
                            "--loop-window",
                            str(cfg.get("loop_window", 0)),
                            *(
                                ["--autocompact-window", str(cfg["autocompact_window"])]
                                if cfg.get("autocompact_window")
                                else []
                            ),
                        ],
                    }
                ],
            },
        )
    return {**settings, "hooks": hooks} if hooks else settings


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# Which `send` is a complete brief (full lint) is decided by brief_lint.is_brief_message: a `[NEW TASK` line in any
# case or a `Kind:` header. Feedback and retest messages without a `Kind:` header keep the always-rules only.
LINT_WAIVE_HELP = (
    "launch although brief_lint reports findings; the reason is required and logged to "
    f"lint-waivers.jsonl in the state directory ({STATE_ENV}, else .tmp/pm-state)"
)


# Protocol-bound registry marker (T-021): a worker started under brief_lint's `brief-order` rule ("context first, task
# last"). Written by `start` only; a worker without it was started before T-021 and keeps the old order on its sends.
BRIEF_ORDER_KEY = "brief_order"
BRIEF_ORDER = 2


def brief_order_applies(command: str, name: str) -> bool:
    """Whether the `brief-order` rule runs: always for a `start`; for a `send`, when the worker's registry entry carries
    the marker. An entry without the marker is a worker from before T-021 (legacy order); any other marker value is
    refused, never guessed. A `send` to a name with no entry is checked strictly (it is refused later anyway)."""
    if command == "start":
        return True
    entry = load().get(name)
    if not isinstance(entry, dict):
        return True
    if BRIEF_ORDER_KEY not in entry:
        return False
    value = entry[BRIEF_ORDER_KEY]
    if type(value) is int and value == BRIEF_ORDER:
        return True
    raise SystemExit(
        f"worker {name} has an unknown {BRIEF_ORDER_KEY} marker {value!r} (this launcher writes {BRIEF_ORDER}); "
        "refusing to guess its brief layout: retire it and start a new worker"
    )


def lint_gate(command: str, name: str, message: str, waive: str | None) -> None:
    """Refuse to launch on a brief_lint finding (T-MEM); `--lint-waive "<reason>"` logs the override instead.

    A crash of the linter itself is logged and refuses the launch with its reason (fail closed, T-024 final round)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "brief_lint", Path(__file__).resolve().parent / "brief_lint.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("brief_lint.py")
        lint = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("brief_lint", lint)
        spec.loader.exec_module(lint)
        footer = None  # None: unreadable, itself a finding of a full lint
        with contextlib.suppress(OSError):
            footer = (ROOT / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md").read_text(encoding="utf-8-sig")
        # The worker's layout is known before any reader runs, so a send to a worker started before T-021 (no marker)
        # is read by the base readers only, byte for byte. An unknown marker reads strictly and refuses a brief.
        unknown: SystemExit | None = None
        try:
            layout = brief_order_applies(command, name)
        except SystemExit as refusal:
            layout, unknown = True, refusal
        full = command == "start" or lint.is_brief_message(message, footer=footer, order=layout)
        if full and unknown is not None:
            raise unknown
        order = full and layout
        task_text, extra = (
            lint.task_file_findings(message, ROOT, footer=footer, order=order) if full else (None, [])
        )
        findings = lint.lint_message(
            message, full=full, order=order, footer=footer, task_file_text=task_text
        )
        findings = sorted([*findings, *extra], key=lambda f: (f.line, f.rule))
    except Exception as exc:  # fail closed (T-024 final round): an unchecked brief never starts a worker
        log_waiver({"command": command, "worker": name, "lint_error": type(exc).__name__})
        raise SystemExit(
            f"brief lint crashed ({type(exc).__name__}: {exc}); {command} {name} refused: repair "
            "tools/agent-workflow/brief_lint.py (--lint-waive covers findings, not a crash)"
        ) from None
    if not findings:
        return
    lines = [f"  {f.line}: [{f.rule}] {f.message}" for f in findings]
    if not (waive and waive.strip()):
        raise SystemExit(
            f"brief lint refused {command} {name} ({len(findings)} finding(s); fix the brief or pass "
            '--lint-waive "<reason>"):\n' + "\n".join(lines)
        )
    log_waiver(
        {
            "command": command,
            "worker": name,
            "reason": waive.strip(),
            "rules": sorted({f.rule for f in findings}),
        }
    )
    print(
        f"WARNING: brief lint WAIVED for {name} ({waive.strip()}):\n" + "\n".join(lines),
        file=sys.stderr,
    )


def log_waiver(entry: dict[str, Any]) -> None:
    path = state_root() / LINT_WAIVERS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": now(), **entry}, ensure_ascii=False) + "\n")


def pid_alive(pid: int) -> bool:
    """Whether this PID exists. Windows uses a query handle, never a signal-zero console event."""
    if NATIVE_WINDOWS:
        import ctypes
        from ctypes import wintypes

        if pid <= 0 or pid >= 2**32:
            return False
        # A private WinDLL instance: declared types must not leak into the shared ctypes.windll.kernel32.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
        finally:
            kernel32.CloseHandle(handle)
        return code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but belongs to another user
    return True


# Process identity. A PID alone is not an identity: after a reboot or a long run the OS hands the number of a dead
# worker to an unrelated program, and acting on it (refusing `send` forever, or worse, killing it) is wrong. A
# recorded process counts as ours only if a process with that PID exists AND its creation time equals the one
# recorded beside the PID (`launcher_created`, `worker_created`). The tolerance absorbs the one-second granularity of
# Linux `btime` and clock-tick rounding; it is a safety-bound identity limit (a reused PID must also have been created
# within it of the original, i.e. the original must have died that fast), not a deployment choice.
PROCESS_TIME_TOLERANCE_SECONDS = 2.0
ALIVE, GONE, UNKNOWN, NO_PID = "alive", "gone", "unknown", "none"
WINDOWS_EPOCH_OFFSET_100NS = 116_444_736_000_000_000  # 1601-01-01 to 1970-01-01 in FILETIME ticks
STILL_ACTIVE = 259
PROCESS_KEYS = ("launcher_pid", "launcher_created", "worker_pid", "worker_created")


class IdentityUnavailable(Exception):  # noqa: N818 (a condition the caller maps to UNKNOWN, not an error)
    """The process exists but its creation time cannot be read (or this platform offers none)."""


def filetime_to_epoch(ticks: int) -> float:
    return (ticks - WINDOWS_EPOCH_OFFSET_100NS) / 1e7


def parse_stat_start(stat_text: str, btime: int, ticks_per_second: int) -> float | None:
    """Creation time (epoch seconds) from the text of `/proc/<pid>/stat`; None for a zombie (it has exited).

    `comm` (field 2) may contain spaces and parentheses, so fields are counted after the LAST `)`. Field 22
    (`starttime`, clock ticks since boot) is then index 19 of the remainder, whose first item is field 3 (`state`).
    """
    rest = stat_text.rpartition(")")[2].split()
    if len(rest) < 20 or ticks_per_second <= 0:
        raise IdentityUnavailable("unparsable /proc stat text")
    if rest[0] in ("Z", "X"):
        return None
    try:
        return btime + int(rest[19]) / ticks_per_second
    except ValueError:
        raise IdentityUnavailable("unparsable /proc starttime") from None


def posix_process_start(pid: int, proc: Path = Path("/proc")) -> float | None:
    if not (proc / "stat").is_file():  # no procfs (macOS): existence is knowable, identity is not
        if pid_alive(pid):
            raise IdentityUnavailable("no /proc on this platform")
        return None
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return None
    except OSError as exc:
        raise IdentityUnavailable(str(exc)) from exc
    btime = proc_btime(proc)
    if btime is None:
        raise IdentityUnavailable("no btime in /proc/stat")
    ticks = os.sysconf("SC_CLK_TCK")  # type: ignore[attr-defined, unused-ignore]  # POSIX only
    return parse_stat_start(text, btime, ticks)


def proc_btime(proc: Path = Path("/proc")) -> int | None:
    """System boot time (epoch seconds) from `/proc/stat`, None when unreadable."""
    try:
        lines = (proc / "stat").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "btime" and parts[1].isdigit():
            found = int(parts[1])
    return found


def boot_time() -> float | None:
    """When the system booted (epoch seconds), None when this platform does not tell."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - float(kernel32.GetTickCount64()) / 1000
    return proc_btime()


def windows_process_start(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    if pid >= 2**32:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None  # absent, or not ours to open (a process of this user always opens): never ours
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise IdentityUnavailable("GetExitCodeProcess failed")
        if code.value != STILL_ACTIVE:
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise IdentityUnavailable("GetProcessTimes failed")
    finally:
        kernel32.CloseHandle(handle)
    return filetime_to_epoch((created.dwHighDateTime << 32) | created.dwLowDateTime)


def process_start(pid: int) -> float | None:
    """Creation time (epoch seconds) of the RUNNING process with this PID; None when none runs. Raises
    IdentityUnavailable when it runs but its creation time cannot be read."""
    return windows_process_start(pid) if sys.platform == "win32" else posix_process_start(pid)


def own_start(pid: int) -> float | None:
    """Creation time to record beside a PID this program just obtained; None when unreadable (the entry is then
    judged as a legacy one)."""
    try:
        return process_start(pid)
    except IdentityUnavailable:
        return None


def as_time(value: object) -> float | None:
    """A recorded epoch time, or None when damaged (bool, text, NaN, non-positive)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def process_state(
    pid: int, recorded: float | None, legacy: Callable[[float], str] | None = None
) -> str:
    """ALIVE only for a running process whose creation time equals `recorded` (within the tolerance); GONE for no
    process or another one (a reused PID); UNKNOWN when identity cannot be established. Without a recorded time
    (entries written before this check) `legacy` judges the creation time against the entry's own turn start."""
    try:
        started = process_start(pid)
    except IdentityUnavailable:
        return UNKNOWN
    if started is None:
        return GONE
    if recorded is not None:
        return ALIVE if abs(started - recorded) <= PROCESS_TIME_TOLERANCE_SECONDS else GONE
    return legacy(started) if legacy is not None else UNKNOWN


def turn_start_epoch(entry: dict[str, Any]) -> float | None:
    """When the current turn began: `last_used`, written in the same registry update that records `launcher_pid`."""
    try:
        return dt.datetime.fromisoformat(str(entry["last_used"])).timestamp()
    except (KeyError, ValueError, OverflowError, OSError):
        return None


def entry_process_state(entry: dict[str, Any], role: str) -> tuple[int | None, str]:
    """(pid, state) of the `launcher` or `worker` process recorded in a registry entry.

    Legacy entries (a PID with no `<role>_created`) are judged against the turn start, `last_used`:
    - launcher: it wrote `last_used` after it started, so a live process created later than that cannot be it (GONE);
      one created earlier must have been alive at that moment, and PIDs are unique among live processes, so it is it
      (ALIVE; this only makes `send`/`retire` refuse, nothing ever kills it).
    - worker: it was started after `last_used`, so a process created before that cannot be it (GONE); a later one
      cannot be told apart from a reused PID (UNKNOWN).
    No readable `last_used` leaves a legacy entry UNKNOWN, as does a recorded time that is damaged.
    """
    pid = as_pid(entry.get(f"{role}_pid"))
    if pid is None:
        return None, NO_PID
    recorded = entry.get(f"{role}_created")
    if recorded is not None and as_time(recorded) is None:
        return pid, UNKNOWN
    turn = turn_start_epoch(entry)
    legacy: Callable[[float], str] | None = None
    if turn is not None:
        slack = PROCESS_TIME_TOLERANCE_SECONDS
        if role == "launcher":
            legacy = lambda started: ALIVE if started <= turn + slack else GONE  # noqa: E731
        else:
            legacy = lambda started: GONE if started < turn - slack else UNKNOWN  # noqa: E731
    return pid, process_state(pid, as_time(recorded), legacy)


def kill_tree(pid: int, created: float | None) -> bool:
    """Kill the process tree of a worker process this launcher started (by PID, never by name), but only while the
    process with that PID still has the creation time recorded for it. Anything else is not ours and is left alone.
    Returns whether a kill was issued. The check and the kill are not atomic; the window is microseconds."""
    if created is None or process_state(pid, created) != ALIVE:
        return False
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
    return True


def stop_worker(proc: subprocess.Popen[Any], created: float | None) -> None:
    """Stop the `claude` child of this turn: its whole tree when its identity was recorded, else only the child
    itself through the Popen handle (which keeps the PID ours), never a bare PID."""
    if not kill_tree(proc.pid, created) and proc.poll() is None:
        proc.kill()


# T-024 final round, F17 (decision 100): every process a turn starts ends before the final Git snapshot, including
# processes that detach, start a new session or lose their parent. Windows: the worker starts suspended inside a job
# object that kills every member when it is terminated and allows no breakaway. Linux: the launcher is a child
# subreaper, so every orphan of the worker's tree is re-parented to it and stopped. Values below are protocol-bound
# (winnt.h, winbase.h, tlhelp32.h, linux/prctl.h).
CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004
ERROR_INVALID_PARAMETER = 87  # OpenProcess of a PID no process holds
PR_SET_CHILD_SUBREAPER = 36
# Poll interval while waiting for stopped processes to be gone (safety-bound; the wait itself is bounded by the
# validated git_timeout_seconds, the same bound as the snapshot settle).
STOP_POLL_SECONDS = 0.05


class ContainmentError(OSError):
    """The worker could not be placed where every process it starts can be stopped: the turn is refused."""


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)  # a private instance: declared types stay local
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
    ]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenThread.restype = wintypes.HANDLE
    k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.ResumeThread.restype = wintypes.DWORD
    k.ResumeThread.argtypes = [wintypes.HANDLE]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _win_error(what: str) -> ContainmentError:
    import ctypes

    code = ctypes.get_last_error()  # type: ignore[attr-defined, unused-ignore]  # Windows only
    return ContainmentError(f"{what} failed (Windows error {code})")


class WindowsJob:
    """A job object holding one worker's whole process tree (Windows 8 or later nests it inside any job the launcher
    already runs in). No breakaway flag is set, so a child created with CREATE_BREAKAWAY_FROM_JOB fails to start
    instead of leaving the job; `close` terminates every member."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.k = _kernel32()
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise _win_error("CreateJobObjectW")
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(
            self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = _win_error("SetInformationJobObject")
            self.k.CloseHandle(self.handle)
            raise error

    def adopt(self, pid: int) -> None:
        """Put the suspended process `pid` into the job, then let it run. A PID that no process holds has nothing to
        contain; any other failure raises ContainmentError (the caller kills the suspended child)."""
        import ctypes
        from ctypes import wintypes

        process = self.k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:  # type: ignore[attr-defined, unused-ignore]
                return
            raise _win_error("OpenProcess")
        try:
            if not self.k.AssignProcessToJobObject(self.handle, process):
                raise _win_error("AssignProcessToJobObject")
        finally:
            self.k.CloseHandle(process)

        class Entry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        self.k.Thread32First.restype = self.k.Thread32Next.restype = wintypes.BOOL
        self.k.Thread32First.argtypes = self.k.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        snap = self.k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            raise _win_error("CreateToolhelp32Snapshot")
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            more = self.k.Thread32First(snap, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = self.k.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        raise _win_error("OpenThread")
                    try:
                        if self.k.ResumeThread(thread) == 0xFFFFFFFF:
                            raise _win_error("ResumeThread")
                    finally:
                        self.k.CloseHandle(thread)
                more = self.k.Thread32Next(snap, ctypes.byref(entry))
        finally:
            self.k.CloseHandle(snap)

    def active(self) -> int:
        import ctypes
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        info = Accounting()
        if not self.k.QueryInformationJobObject(
            self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise _win_error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def close(self, bound: float) -> list[str]:
        """Terminate every member, wait up to `bound` seconds for them to be gone, release the job; the problems."""
        problems: list[str] = []
        try:
            if not self.k.TerminateJobObject(self.handle, 1):
                problems.append(str(_win_error("TerminateJobObject")))
            deadline = time.monotonic() + bound
            left = self.active()
            while left and time.monotonic() < deadline:
                time.sleep(STOP_POLL_SECONDS)
                left = self.active()
            if left:
                problems.append(f"{left} process(es) of the worker's job still ran {bound} s after it was terminated")
        except ContainmentError as exc:
            problems.append(str(exc))
        finally:
            self.k.CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE: closing the last handle ends what is left
        return problems


def posix_children(parent: int, proc: Path = Path("/proc")) -> list[tuple[int, str, float | None]]:
    """(pid, state, start) of every process whose parent is `parent`, from `/proc/<pid>/stat` (field 4 is the parent,
    counted after the last `)` of `comm`)."""
    btime = proc_btime(proc)
    ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 0  # type: ignore[attr-defined, unused-ignore]
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # ended while listing
        rest = text.rpartition(")")[2].split()
        if len(rest) < 20 or not rest[1].isdigit() or int(rest[1]) != parent:
            continue
        start = btime + int(rest[19]) / ticks if btime is not None and ticks and rest[19].isdigit() else None
        found.append((int(entry.name), rest[0], start))
    return found


class TurnContainment:
    """Where one worker turn's processes live, so that all of them can be stopped after any exit of the turn.

    Windows: a job object (the child is created suspended, adopted, then resumed). Linux: the launcher is made a child
    subreaper (prctl), so a descendant whose parent ends is re-parented to the launcher instead of init; `close` stops
    the launcher's children started since the turn began, reaps them, and repeats for the children they leave. Other
    platforms have neither mechanism and are refused (R-025 supports Windows and Linux)."""

    def __init__(self) -> None:
        self.job: WindowsJob | None = None
        self.since = time.time()
        if NATIVE_WINDOWS:
            self.job = WindowsJob()
        elif sys.platform.startswith("linux"):
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
                raise ContainmentError(f"prctl(PR_SET_CHILD_SUBREAPER) failed (errno {ctypes.get_errno()})")
        else:
            raise ContainmentError(f"no process containment on {sys.platform}: workers run on Windows or Linux")

    def creationflags(self) -> int:
        return CREATE_SUSPENDED if self.job is not None else 0

    def adopt(self, proc: subprocess.Popen[Any]) -> None:
        if self.job is None:
            return
        try:
            self.job.adopt(proc.pid)
        except BaseException:
            with contextlib.suppress(OSError):
                proc.kill()  # still suspended: it has started nothing
            raise

    def close(self, bound: float) -> list[str]:
        """Stop every process of the turn that is still running; the problems (empty when all are gone)."""
        if self.job is not None:
            return self.job.close(bound)
        deadline = time.monotonic() + bound
        slack = PROCESS_TIME_TOLERANCE_SECONDS

        def turn_children() -> list[tuple[int, str]]:
            # The launcher's own children started since the turn began: the worker, and its descendants re-parented
            # here when their parent ended. Ours until reaped, so a PID cannot be reused before the kill.
            return [
                (pid, state)
                for pid, state, start in posix_children(os.getpid())
                if start is None or start >= self.since - slack
            ]

        while True:
            found = turn_children()
            if not found:
                return []
            for pid, state in found:
                if state not in ("Z", "X"):
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
            waiting = {pid for pid, _ in found}
            while waiting and time.monotonic() < deadline:
                for pid in list(waiting):
                    try:
                        if os.waitpid(pid, os.WNOHANG)[0]:  # type: ignore[attr-defined, unused-ignore]  # POSIX
                            waiting.discard(pid)
                    except ChildProcessError:
                        waiting.discard(pid)
                if waiting:
                    time.sleep(STOP_POLL_SECONDS)
            if time.monotonic() >= deadline:
                left = [pid for pid, _ in turn_children()]
                return [f"process(es) {left} of the worker still ran {bound} s after the stop"] if left else []


class RegistryLock:
    """Exclusive registry lock: a file holding `<pid> <token>` of its owner.

    A lock whose owner is alive is never broken (on POSIX `rename` succeeds on an open file, so age alone would
    steal it from a stalled holder). The owner is alive only if its PID runs AND the process has the creation time
    recorded in the lock (`<pid> <token> <created>`): after a reboot an unrelated process may hold the old PID. A lock
    of a dead or reused owner is broken at once; one without a readable owner (a crash between create and write) is
    broken by age. A legacy lock (no creation time) whose PID runs is held only if the file was written after the
    system booted (a lock older than the boot cannot have a surviving owner); with no boot time known it is held.
    """

    token = ""

    def __enter__(self) -> RegistryLock:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        created = own_start(os.getpid())
        self.token = f"{os.getpid()} {uuid.uuid4().hex}" + (
            "" if created is None else f" {created!r}"
        )
        for _ in range(600):
            try:
                self.fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self.break_stale()
                time.sleep(0.1)
                continue
            try:
                os.write(self.fd, self.token.encode("ascii"))
            except BaseException:
                os.close(self.fd)
                LOCK.unlink(missing_ok=True)
                raise
            return self
        raise SystemExit(f"registry lock busy (owner {lock_owner()}): {LOCK}")

    @staticmethod
    def break_stale() -> None:
        """Remove the lock of a dead owner or an ownerless lock older than STALE_LOCK_SECONDS. The file is moved aside
        first and compared with what was judged, so a lock another process created in between is put back."""
        seen = lock_identity()
        if seen is None:
            return
        pid, token, ino, mtime_ns = seen
        if pid is not None:
            if lock_owner_holds(pid, token, mtime_ns):
                return
        elif time.time() - mtime_ns / 1e9 <= STALE_LOCK_SECONDS:
            return
        graveyard = LOCK.with_name(f"{LOCK.name}.stale-{os.getpid()}")
        try:
            os.replace(LOCK, graveyard)
        except OSError:
            return
        try:
            moved = os.stat(graveyard)
            if (moved.st_ino, moved.st_mtime_ns) != (ino, mtime_ns):
                # Not the lock that was judged: a fresh lock was created between the check and the move. Restore it
                # without clobbering a newer one (`link` fails when LOCK exists again).
                with contextlib.suppress(OSError):
                    os.link(graveyard, LOCK)
        finally:
            graveyard.unlink(missing_ok=True)

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)
        # Remove only our own lock: a breaker that misjudged must not make us delete its successor's lock.
        owner = lock_identity()
        if owner is not None and owner[1] == self.token:
            LOCK.unlink(missing_ok=True)


def lock_identity() -> tuple[int | None, str, int, int] | None:
    """(owner pid or None, token, inode, mtime_ns) of the lock file, None when it does not exist."""
    try:
        info = LOCK.stat()
        text = LOCK.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    head = text.split(" ", 1)[0]
    return (
        (int(head) if head.isdigit() and int(head) > 0 else None),
        text,
        info.st_ino,
        info.st_mtime_ns,
    )


def lock_owner_holds(pid: int, token: str, mtime_ns: int) -> bool:
    """Whether the process named in a lock file still holds it: same PID and creation time, or (legacy lock without a
    time, or a damaged one) a running PID and a file not older than the system boot. Unreadable identity holds."""
    parts = token.split()
    recorded = None
    if len(parts) > 2:
        try:
            recorded = as_time(float(parts[2]))
        except ValueError:
            recorded = None
    state = process_state(pid, recorded)
    if state != UNKNOWN or recorded is not None:
        return state != GONE
    booted = boot_time()
    return booted is None or mtime_ns / 1e9 >= booted - PROCESS_TIME_TOLERANCE_SECONDS


def lock_owner() -> str:
    identity = lock_identity()
    return "unknown" if identity is None else f"pid {identity[0]}" if identity[0] else "no pid"


def load() -> dict[str, Any]:
    if REGISTRY.exists():
        data: dict[str, Any] = json.loads(REGISTRY.read_text(encoding="utf-8-sig"))
        return data
    return {}


def save(data: dict[str, Any]) -> None:
    """Replace the registry atomically: hooks, `status` and the supervisor read it without the lock and must never see
    a truncated file."""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_name(f"{REGISTRY.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, REGISTRY)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(REPLACE_WAIT_SECONDS)


def child_env(ttl: str, autocompact_window: int | None = None) -> dict[str, str]:
    # The desktop host sets ANTHROPIC_BASE_URL for its own session; the CLI must use its own login.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC")}
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Keep the cached prefix byte-identical across --resume turns (cache_check.py, 2026-10-10):
    # explicit TTL, no startup git snapshot (the worktree changes between turns), no auto
    # memory in the context. CLAUDE.md stays (workers need the rules; it does not change).
    # A cache write costs more than plain input (1h about 2x, 5m about 1.25x; a read about
    # 0.1x), so 1h pays only when a later turn or a long tool run reuses the prefix after
    # more than 5 minutes (user, 2026-10-10). Short one-shot jobs use the 5m default.
    if ttl == "1h":
        env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = "1h"
    env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    # Per-process only: the environment variable takes precedence over the user's autoCompactWindow setting (and over
    # an ambient value), and nothing is written to ~/.claude. Unset (None) leaves Claude Code's own window untouched.
    if autocompact_window is not None:
        env[AUTOCOMPACT_ENV] = str(autocompact_window)
    return env


def model_usage_summary(result_event: dict[str, Any]) -> dict[str, Any]:
    """Per-model tokens and list cost of one turn from the `result` event's `modelUsage`; advisor calls appear
    under the advisor's model (uncached input), so a pilot can compare them with the main model's share."""
    usage = result_event.get("modelUsage")
    if not isinstance(usage, dict):
        return {}
    keys = (
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "costUSD",
    )
    return {
        str(model): {k: v.get(k) for k in keys} for model, v in usage.items() if isinstance(v, dict)
    }


def write_prompt(path: Path, message: str) -> None:
    """The turn's message as the child's stdin file: UTF-8 bytes as given (text mode would turn `\\n` into `\\r\\n`
    on Windows), unlimited by the command line."""
    path.write_bytes(message.encode("utf-8"))


def build_command(worker: dict[str, Any], resume: bool) -> list[str]:
    """The `claude -p` command line of one worker turn. The message goes on stdin, never argv: a Windows command line
    holds at most 32767 characters, so a long brief on argv fails to launch (WinError 206)."""
    cmd = [
        "claude",
        "-p",
        "--model",
        worker["model"],
        "--effort",
        worker["effort"],
        "--tools",
        TOOLS,
        "--disallowedTools",
        DENIED_TOOLS,
        "--settings",
        json.dumps(
            hook_settings(
                {
                    **(worker.get("hooks") or {}),
                    "autocompact_window": worker.get("autocompact_window"),
                    "cwd": worker.get("cwd"),
                }
            )
        ),
        "--permission-mode",
        "dontAsk",
        "--add-dir",
        str(ROOT),
        "--output-format",
        "stream-json",
        "--verbose",
        "--exclude-dynamic-system-prompt-sections",
    ]
    if worker.get("mcp", "default") == "minimal":
        cmd += ["--strict-mcp-config", "--mcp-config", MINIMAL_MCP_CONFIG]
    if worker.get("advisor"):
        # Claude Code advisor tool (decision 266 pilot): the main model consults this model when it decides to.
        cmd += ["--advisor", worker["advisor"]]
    cmd += ["--resume", worker["session_id"]] if resume else ["--session-id", worker["session_id"]]
    return cmd


def context_of(usage: dict[str, Any]) -> int | None:
    """Context size of one API call: uncached input plus cache reads plus cache writes; None for malformed counts."""
    try:
        return sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
    except (TypeError, ValueError, OverflowError):
        return None


def stream_events(text: str) -> Iterator[dict[str, Any]]:
    """JSON objects of a stream log, oldest first. Splits on LF only (`str.splitlines` also breaks a line at U+2028,
    U+0085 and form feed, which a JSON line may hold raw) and skips damaged or non-object lines."""
    for line in text.split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def assistant_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    message = event.get("message")
    if event.get("type") != "assistant" or not isinstance(message, dict):
        return None
    usage = message.get("usage")
    return usage if isinstance(usage, dict) else None


def as_pid(value: object) -> int | None:
    """A recorded PID, or None when absent or damaged."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        pid = int(value)
    except ValueError:
        return None
    return pid if pid > 0 else None


def is_link(path: Path) -> bool:
    return path.is_symlink() or os.path.isjunction(path)


def remove_entry(
    path: Path, action: Callable[[], None], removed: list[Path], failed: list[str]
) -> bool:
    """Run one removal; an OSError (e.g. a read-only file) is recorded in `failed` and the purge goes on.
    Permissions and read-only flags are never changed."""
    try:
        action()
    except OSError as exc:
        failed.append(f"{path}: {exc.__class__.__name__}: {exc}")
        return False
    removed.append(path)
    return True


def remove_tree_no_follow(path: Path, removed: list[Path], failed: list[str]) -> bool:
    """Delete a directory tree; a nested link or junction is removed itself, its target is never entered.
    Failures are collected in `failed`; a directory with a failed child is kept. True when `path` is gone."""
    try:
        with os.scandir(path) as it:
            children = [Path(e.path) for e in it]
    except OSError as exc:
        failed.append(f"{path}: {exc.__class__.__name__}: {exc}")
        return False
    clean = True
    for child in children:
        if is_link(child):
            # a junction is dropped as a directory entry, its target stays
            ok = remove_entry(
                child, child.unlink if child.is_symlink() else child.rmdir, removed, failed
            )
        elif child.is_dir():
            ok = remove_tree_no_follow(child, removed, failed)
        else:
            ok = remove_entry(child, child.unlink, removed, failed)
        clean = clean and ok
    return clean and remove_entry(path, path.rmdir, removed, failed)


def project_slug(path: Path) -> str:
    """Claude Code's project folder name for a working directory: every non-alphanumeric character becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def protected_project_slugs() -> set[str]:
    """Folders of the main repository (the PM session lives there); a worktree of it maps back to its main root."""
    roots = [ROOT]
    if ROOT.parent.name == "worktrees" and ROOT.parent.parent.name == ".tmp":
        roots.append(ROOT.parents[2])
    return {project_slug(r).lower() for r in roots}


def purge_transcript(
    session_id: str, projects: Path | None = None, failed: list[str] | None = None
) -> list[Path]:
    """Delete the Claude transcript `<session_id>.jsonl` of a retired worker, its `<session_id>/` directory
    (tool results, subagents) and then its project folder when that is empty (decision 501).

    Fail closed: the id must be a canonical lowercase UUID; only a regular file or a real directory (no link)
    directly inside one project folder of `projects` is removed, and links inside the directory are never
    followed. The project folder of the main repository and `memory/` stay. Turn logs under `.tmp/cli-jobs`
    (cache_check.py input) stay. Returns every removed path; an entry that cannot be removed does not stop the
    purge and is appended to `failed` (the caller reports it).
    """
    failed = failed if failed is not None else []
    try:
        canonical = str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        canonical = ""
    if not session_id or canonical != session_id:
        raise SystemExit(f"session id {session_id!r} is not a canonical UUID; nothing deleted")
    base = (projects if projects is not None else CLAUDE_PROJECTS).resolve()
    removed: list[Path] = []
    protected = protected_project_slugs()
    for folder in sorted(p for p in base.glob("*") if p.is_dir() and not is_link(p)):
        if folder.resolve().parent != base:
            continue
        before = len(removed)
        log = folder / f"{canonical}.jsonl"
        if log.is_file() and not is_link(log):
            remove_entry(log.resolve(), log.unlink, removed, failed)
        session_dir = folder / canonical
        if session_dir.is_dir() and not is_link(session_dir):
            remove_tree_no_follow(session_dir, removed, failed)
        if (
            len(removed) > before
            and folder.name.lower() not in protected
            and folder.name != "memory"
            and not any(folder.iterdir())
        ):
            remove_entry(folder, folder.rmdir, removed, failed)
    return removed


def live_processes(entry: dict[str, Any]) -> list[str]:
    """Recorded processes of a worker entry that still run: its launcher and its `claude` child (which outlives a
    launcher that was killed without its process tree)."""
    return recorded_processes(entry, ALIVE)


def recorded_processes(entry: dict[str, Any], state: str) -> list[str]:
    found = []
    for role in ("launcher", "worker"):
        pid, got = entry_process_state(entry, role)
        if pid is not None and got == state:
            found.append(f"{role}_pid {pid}")
    return found


def unverifiable_message(name: str, found: list[str]) -> str:
    """The refusal for recorded PIDs that cannot be matched to a process (legacy entry, PID possibly reused)."""
    return (
        f"worker {name}: {', '.join(found)} was recorded without a creation time, so it cannot be told from a reused "
        f"PID; check that it is not the worker (Get-Process -Id <pid> / ps -p <pid>), then rerun `send` with "
        f"--pid-checked or run `retire --name {name} --force`"
    )


def next_log_number(job_dir: Path, turns: int) -> int:
    """First unused log number: a worker name started again after `retire` must not overwrite its predecessor's logs."""
    used = [int(p.stem) for p in job_dir.glob("[0-9][0-9][0-9].jsonl")]
    return max(turns, max(used, default=0)) + 1


def live_log(log: Path) -> dict[str, Any]:
    """Size, idle time and current context of a running worker, read from the log file itself.

    Directory listings of a file another process is writing show size 0 and a stale time on Windows; an open handle
    (`fstat`, `seek`) is exact. Reads a bounded tail only.
    """
    try:
        with log.open("rb") as fh:
            size = fh.seek(0, 2)
            mtime = os.fstat(fh.fileno()).st_mtime
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    tokens = None
    # The first line of the tail may be cut: it fails to parse and is skipped.
    for event in reversed(list(stream_events(tail))):
        usage = assistant_usage(event)
        context = context_of(usage) if usage is not None else None
        if context is not None:
            tokens = context
            break
    return {
        "log": log.name,
        "log_bytes": size,
        "log_idle_minutes": round(max(0.0, time.time() - mtime) / 60, 1),
        "live_context_tokens": tokens,
    }


def update_entry(
    name: str, session_id: str, changes: dict[str, Any], drop: tuple[str, ...] = ()
) -> bool:
    """Update a registry entry only while it still belongs to this session (it may have been retired and restarted)."""
    with RegistryLock():
        data = load()
        entry = data.get(name)
        if not isinstance(entry, dict) or entry.get("session_id") != session_id:
            return False
        entry.update(changes)
        for key in drop:
            entry.pop(key, None)
        save(data)
    return True


def mark_error(name: str, status: str, session_id: str, only_busy: bool = False) -> None:
    """End the turn's `busy` state with an error status (only while the entry is still `busy` when `only_busy`). A
    Git history flag stays the status: it is sticky until the PM retires the worker (T-024 F16)."""
    with RegistryLock():
        data = load()
        entry = data.get(name)
        if not isinstance(entry, dict) or entry.get("session_id") != session_id:
            return
        if only_busy and entry.get("status") != "busy":
            return
        if only_busy and entry.get("backend") == "codex":
            try:
                sibling("codex_backend").require_worker(entry)
            except sibling("codex_backend").StaleWorkerError:
                return  # a stale stored row is never rewritten (no silent repair); `send` refuses it anyway
        entry["status"] = GIT_HISTORY_CHANGED if entry.get("git_changes") else status
        for key in PROCESS_KEYS:
            entry.pop(key, None)
        save(data)


# T-024 round 4 (class F, detection at Git itself): the shell guard cannot prove what opaque inline code or a program
# found on a changed PATH runs, so every worker turn is bracketed by a snapshot of its repository's refs and reflogs.
GIT_HISTORY_CHANGED = "git_history_changed"  # protocol-bound: the status the PM's integration refuses
# T-024 F16 and CX2: a turn's starting state and audit identity live in the protected state directory (next to the
# registry, so a REGISTRY override moves them too); the registry entry pins the audit id and the file's SHA-256, so a
# missing, changed or foreign file is itself a finding. Names are artifact-bound.
AUDIT_DIR_NAME = "git-audits"
AUDIT_ID = re.compile(r"[0-9a-f]{32}")  # uuid4().hex


def audit_dir() -> Path:
    return REGISTRY.parent / AUDIT_DIR_NAME


def audit_path(audit_id: str, state: str = "open") -> Path:
    """`<id>.json` while the turn runs, `<id>.closed.json` once its audit is recorded, `<id>.flagged.json` kept as
    evidence after an unaudited turn was flagged."""
    return audit_dir() / (f"{audit_id}.json" if state == "open" else f"{audit_id}.{state}.json")


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audit_finding(entry: dict[str, Any]) -> str | None:
    """Why the entry's last turn has no recorded Git audit, None when it has one (or no turn recorded an audit).
    The audit is proven only by a closed file whose bytes match the hash the registry pinned and whose session is the
    entry's; anything else (still open, missing, changed, another session, damaged pin) is a finding."""
    pin = entry.get("git_audit")
    if pin is None:
        return None
    if not isinstance(pin, dict) or not isinstance(pin.get("id"), str) or not AUDIT_ID.fullmatch(pin["id"]):
        return "the registry's audit record of the last turn is damaged"
    turn = f"turn {pin.get('log')}"
    for state in ("closed", "open"):
        path = audit_path(pin["id"], state)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"the start state of {turn} cannot be read ({type(exc).__name__})"
        if sha256_text(text) != pin.get("sha256"):
            return f"the start state of {turn} was changed after the turn began"
        try:
            owner = json.loads(text).get("session_id")
        except (ValueError, AttributeError):
            owner = None
        if owner != entry.get("session_id"):
            return f"the start state of {turn} belongs to another session"
        if state == "closed":
            return None
        return f"{turn} was never audited (its launcher ended before the audit)"
    return f"the start state of {turn} is missing"


def repository_evidence(cwd: str | Path) -> Path | None:
    """The first `.git` entry (directory, link file or dangling link) at `cwd` or above it, None when there is none:
    git discovers a repository upwards, so only the absence of every such entry proves there is none (T-024 F15)."""
    here = Path(os.path.abspath(cwd))
    for folder in (here, *here.parents):
        if os.path.lexists(folder / ".git"):
            return folder / ".git"
    return None


def git_snapshot(cwd: str | Path, timeout: int) -> dict[str, Any] | None:
    """The compared Git state at `cwd` (round 5, class K; git_ledger.snapshot): every ref with its symref target, the
    root refs of every Git directory, every registered worktree's HEAD, a fingerprint (count and SHA-256) of every
    reflog, and the orchestrator ledger's fingerprint. None only outside Git: where a
    `.git` entry exists but git reads no repository, the result is a problem, never "no repository" (T-024 F15)."""
    snap: dict[str, Any] | None = git_ledger().snapshot(cwd, timeout)
    if snap is None:
        found = repository_evidence(cwd)
        if found is not None:
            return {"problem": f"git reads no repository at {Path(cwd).as_posix()} although {found.as_posix()} exists"}
    return snap


def settled_snapshot(
    cwd: str | Path, since: float, timeout: float
) -> tuple[dict[str, Any] | None, float, float, list[str]]:
    """Read the state while no orchestrator write runs (round 6, class M; F14 for the first reading): read it, wait
    for orchestrator writes begun in [since, end of the reading] to reach the ledger, and read again when one was
    pending or still running during the reading. All waits share `timeout`; past it the last reading is returned with
    the writes that did not settle. Returns (state, reading start, reading end, unsettled write ids)."""
    gl = git_ledger()
    deadline = time.monotonic() + timeout
    while True:
        read_start = time.time()
        snap = git_snapshot(cwd, int(timeout))
        read_end = time.time()
        busy = gl.settle(ROOT, since, read_end, max(0.0, deadline - time.monotonic()))
        busy += gl.overlapping(gl.entries(ROOT), read_start, read_end)
        if not busy or time.monotonic() >= deadline:
            return snap, read_start, read_end, sorted(set(busy))


def git_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    window: tuple[float, float] = (0.0, 0.0),
    session: str | None = None,
    until: float | None = None,
) -> list[str]:
    """The turn's Git changes that the orchestrator's ledger does not explain (round 5, class J)."""
    gl = git_ledger()
    return gl.unexplained(before, after, window, gl.entries(ROOT), session, until)


def turn_git_changes(worker: dict[str, Any], before: dict[str, Any] | None, t0: float, timeout: int) -> list[str]:
    """After a turn (its processes have ended): the changes nobody but the worker explains, judged on a state read
    while no orchestrator write was running (round 6, class M). Each round reads the state, waits for orchestrator
    writes begun since the turn started to reach the ledger, and reads again when a write was pending or still
    running during the reading (it may show an intermediate state no entry chains to). All waits share one bound,
    `git_timeout_seconds`; past it the turn is judged on the last reading and the reason names the unsettled write."""
    t1 = time.time()
    after, _, read_end, busy = settled_snapshot(worker["cwd"], t0, timeout)
    changes = git_changes(before, after, (t0, t1), worker.get("session_id"), read_end)
    if busy and changes:
        changes.insert(
            0,
            f"orchestrator Git write(s) {', '.join(busy)} begun during the turn did not settle within "
            f"{timeout} s (git_timeout_seconds), so the state was judged mid-write",
        )
    return changes


def open_git_audit(name: str, worker: dict[str, Any], log: Path, timeout: int) -> dict[str, Any]:
    """Before the turn's process starts: read the starting state once no orchestrator write runs (F14; writes begun
    at any earlier time count, since one may straddle the start), refuse a repository git cannot read (F15), and
    persist that state in the protected state directory and pin its id and hash on the registry entry (F16, CX2), so
    a launcher that dies before the audit, or a removed or changed file, leaves a finding."""
    snap, read_start, _, busy = settled_snapshot(worker["cwd"], 0.0, timeout)
    if snap is not None and "problem" in snap:
        mark_error(name, "error refused", worker["session_id"])
        raise SystemExit(
            f"worker {name}: the Git state at {worker['cwd']} could not be read before the turn ({snap['problem']}); "
            "the turn was not started (only a directory with no repository runs without a snapshot)"
        )
    audit = {
        "id": uuid.uuid4().hex,
        "t0": read_start,
        "before": snap,
        "unsettled": busy,
        "timeout": timeout,
        "session_id": worker["session_id"],
        "log": log.name,
    }
    text = json.dumps(audit, sort_keys=True)
    audit["sha256"] = sha256_text(text)
    try:
        audit_dir().mkdir(parents=True, exist_ok=True)
        with audit_path(audit["id"]).open("x", encoding="utf-8") as out:
            out.write(text)
    except OSError as exc:
        mark_error(name, "error refused", worker["session_id"])
        raise SystemExit(f"worker {name}: the turn's starting Git state could not be saved ({exc})") from None
    pin = {"id": audit["id"], "log": log.name, "sha256": audit["sha256"]}
    if not update_entry(name, worker["session_id"], {"git_audit": pin}):
        audit_path(audit["id"]).unlink(missing_ok=True)
        raise SystemExit(f"worker {name}: the registry entry changed before the turn; the turn was not started")
    return audit


def close_git_audit(
    name: str,
    worker: dict[str, Any],
    audit: dict[str, Any],
    leftovers: list[str],
    result_file: Path,
    failure: BaseException | None,
) -> list[str]:
    """After every exit of the turn (its processes already stopped, F17): judge the Git changes and persist the flag on
    the registry entry before anything else is written (F16). A failed audit or a process that outlived the stop is
    itself a flag (fail closed). After a failure the entry also leaves `busy` and the result file gets the section."""
    try:
        changes = turn_git_changes(worker, audit["before"], audit["t0"], audit["timeout"])
    except Exception as exc:  # the turn's own exception, if any, still propagates
        changes = [f"the Git audit after the turn failed ({type(exc).__name__}: {exc})"]
    if audit["unsettled"] and changes:
        changes.insert(
            0,
            f"orchestrator Git write(s) {', '.join(audit['unsettled'])} still ran {audit['timeout']} s "
            "(git_timeout_seconds) into the turn start, so the starting state may be mid-write",
        )
    changes += [f"a process of the turn outlived its stop, so Git may change after the audit: {p}" for p in leftovers]
    start = audit_path(audit["id"])
    try:
        intact = sha256_text(start.read_text(encoding="utf-8")) == audit["sha256"]
    except OSError:
        intact = False
    if not intact:  # the judgement above used the in-memory state; the evidence itself was tampered with
        changes.append(f"the start state of turn {audit['log']} was removed or changed during the turn")
    owned = False
    if changes or failure is not None:
        with RegistryLock():
            data = load()
            w = data.get(name)
            owned = isinstance(w, dict) and w.get("session_id") == worker["session_id"]
            if owned:
                if changes:
                    w["git_changes"] = [*w.get("git_changes", []), *changes][-50:]
                    w["status"] = GIT_HISTORY_CHANGED
                if failure is not None:
                    for key in (*PROCESS_KEYS, "current_log", "git_audit"):
                        w.pop(key, None)
                    if w.get("status") == "busy":
                        w["status"] = f"error {type(failure).__name__}"
                save(data)
            elif changes:
                print(f"WARNING: worker {name}: registry entry changed during the turn; Git flag not recorded",
                      file=sys.stderr)
    if changes:
        print(f"WARNING: the turn changed Git refs or reflogs: {'; '.join(changes[:5])}", file=sys.stderr)
        if failure is not None:
            with contextlib.suppress(OSError):
                append_git_section(result_file, changes)
    # The flag, if any, is recorded. After a failure the registry no longer pins the audit: drop the file. Otherwise
    # mark it closed; the turn's final registry update drops the pin and the file (finish_git_audit).
    if failure is not None and owned:
        start.unlink(missing_ok=True)
    elif intact:
        os.replace(start, audit_path(audit["id"], "closed"))
    return changes


def finish_git_audit(w: dict[str, Any]) -> str | None:
    """In the turn's final registry update: drop the audit pin; returns the id whose closed file to delete after
    the save."""
    pin = w.pop("git_audit", None)
    return pin.get("id") if isinstance(pin, dict) and isinstance(pin.get("id"), str) else None


def drop_closed_audit(audit_id: str | None) -> None:
    if audit_id and AUDIT_ID.fullmatch(audit_id):
        audit_path(audit_id, "closed").unlink(missing_ok=True)


def append_git_section(result_file: Path, changes: list[str]) -> None:
    with result_file.open("a", encoding="utf-8") as out:
        out.write(
            "\n\n## git_history_changed (cli_worker turn snapshot, T-024)\n\n" + "".join(f"- {c}\n" for c in changes)
        )


def flag_history_change(w: dict[str, Any], changes: list[str], result_file: Path) -> bool:
    """After the result text: add the turn's Git changes (already persisted on the registry entry by
    close_git_audit) to the result file, and keep the entry's status flagged (sticky until the PM retires the
    worker); True when the entry is flagged."""
    if changes:
        append_git_section(result_file, changes)
    if w.get("git_changes"):
        w["status"] = GIT_HISTORY_CHANGED
        return True
    return False


def status_rows(include_retired: bool) -> list[dict[str, Any]]:
    rows = []
    for name, w in load().items():
        if w.get("status") == "retired" and not include_retired:
            continue
        if w.get("backend") == "codex" and w.get("status") != "retired":
            sibling("codex_backend").require_worker(w)
        row: dict[str, Any] = {
            "name": name,
            "backend": w.get("backend", "claude"),
            "model": w.get("model"),
            "effort": w.get("effort"),
            "advisor": w.get("advisor"),
            "status": w.get("status"),
            "context_tokens": w.get("context_tokens"),
            "window": w.get(
                "autocompact_window", "unset"
            ),  # unset: registered before the flag; null: off
            "turns": w.get("turns", 0),
            "label": w.get("label"),
            "profile": w.get("profile"),
            "last_used": w.get("last_used"),
        }
        if w.get("git_changes"):
            row["git_changes"] = w["git_changes"]  # T-024 round 4: refs or reflogs a turn changed
        if not (w.get("status") == "busy" and entry_process_state(w, "launcher")[1] == ALIVE):
            finding = audit_finding(w)
            if finding:
                row["git_audit_open"] = finding  # T-024 F16/CX2: the last turn has no proven Git audit; do not integrate
        if w.get("status") == "busy":
            # `*_alive` is True/False only when the identity is proven either way; None is "no PID" or "unknown"
            # (see `*_state`, which tells them apart).
            for role in ("launcher", "worker"):
                pid, state = entry_process_state(w, role)
                row[f"{role}_pid"] = pid
                row[f"{role}_state"] = state
                row[f"{role}_alive"] = {ALIVE: True, GONE: False}.get(state)
            row["current_log"] = w.get("current_log")
            if w.get("current_log"):
                row.update(live_log(JOBS / name / w["current_log"]))
        rows.append(row)
    return rows


def format_row(row: dict[str, Any]) -> str:
    idle = ""
    if row["last_used"] and row["status"] == "idle":
        elapsed = dt.datetime.now() - dt.datetime.fromisoformat(row["last_used"])
        idle = f"idle {int(elapsed.total_seconds() // 60)} min"
    window = "off" if row["window"] is None else row["window"]
    context = row["context_tokens"]
    live = ""
    if row["status"] == "busy":
        if row.get("live_context_tokens") is not None:
            context = f"{row['live_context_tokens']}(live)"
        names = {ALIVE: "alive", GONE: "DEAD", UNKNOWN: "unknown", NO_PID: "unknown"}
        live = f" launcher={names[row.get('launcher_state', NO_PID)]}"
        if row.get("worker_state", NO_PID) != NO_PID:
            live += f" worker={names[row['worker_state']]}"
        if "log" in row:
            live = (
                f" log={row['log']} {row['log_bytes'] // 1024}KB "
                f"active {row['log_idle_minutes']} min ago" + live
            )
    return (
        f"{row['name']}: {'codex:' if row.get('backend') == 'codex' else ''}{row['model']}/{row['effort']}"
        f"{'+advisor:' + row['advisor'] if row.get('advisor') else ''} {row['status']} {idle} ctx={context} "
        f"window={window} turns={row['turns']} task={row['label']}"
        f"{' profile=' + row['profile'] if row.get('profile') else ''}{live}"
    )


def launch_turn(
    name: str,
    worker: dict[str, Any],
    cmd: list[str],
    env: dict[str, str],
    log: Path,
    timeout_minutes: float,
    stdin_file: Path | None = None,
    leftovers: list[str] | None = None,
    stop_seconds: float | None = None,
) -> int:
    """Run one worker turn's process, its output in `log`; returns the exit code (-9 after a timeout kill).

    The recorded PID and creation time make `send` refuse while the child runs, even if this launcher is killed;
    a timeout or an interrupted launcher stops the child's whole process tree (sandbox children included). After
    every exit (normal, error, interrupt, timeout) every process the turn started is stopped (F17, TurnContainment),
    waiting at most `stop_seconds` (default: the validated git_timeout_seconds); what still runs is appended to
    `leftovers`, which the turn's Git audit flags."""
    bound = guard_config()["git_timeout_seconds"] if stop_seconds is None else stop_seconds
    containment: TurnContainment | None = None
    try:
        containment = TurnContainment()
        with log.open("w", encoding="utf-8") as out, contextlib.ExitStack() as stack:
            stdin = stack.enter_context(stdin_file.open("rb")) if stdin_file else subprocess.DEVNULL
            proc = subprocess.Popen(
                cmd,
                cwd=worker["cwd"],
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=stdin,
                start_new_session=os.name != "nt",
                creationflags=containment.creationflags(),
            )
            containment.adopt(proc)
            created = own_start(proc.pid)
            recorded: dict[str, Any] = {"worker_pid": proc.pid}
            if created is not None:
                recorded["worker_created"] = created
            update_entry(name, worker["session_id"], recorded)
            try:
                rc = proc.wait(timeout=timeout_minutes * 60)
            except subprocess.TimeoutExpired:
                stop_worker(
                    proc, created
                )  # only this worker's own process, verified by creation time
                rc = -9
            except BaseException:
                stop_worker(
                    proc, created
                )  # interrupted launcher: never leave the worker running unsupervised
                raise
    except BaseException as exc:
        # A launcher that dies here (missing executable, Ctrl-C) must not leave the worker `busy` forever.
        mark_error(name, f"error {type(exc).__name__}", worker["session_id"])
        raise
    finally:
        if containment is not None:
            try:
                problems = containment.close(bound)
            except Exception as exc:  # never mask the turn's own exception; the audit flags the gap
                problems = [f"the worker's processes could not be stopped ({type(exc).__name__}: {exc})"]
            for problem in problems:
                print(f"WARNING: {problem}", file=sys.stderr)
            if leftovers is not None:
                leftovers.extend(problems)
    return rc


def claude_token_totals(model_usage: dict[str, Any]) -> dict[str, int | None]:
    """Ledger counts of a Claude turn from `modelUsage`: input is the whole prompt (uncached + cache reads + writes)."""
    keys = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
    sums = dict.fromkeys(keys, 0)
    seen = False
    for usage in model_usage.values():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                sums[key] += value
                seen = True
    if not seen:
        return dict.fromkeys(
            ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
        )
    return {
        "input_tokens": sums["inputTokens"]
        + sums["cacheReadInputTokens"]
        + sums["cacheCreationInputTokens"],
        "cached_input_tokens": sums["cacheReadInputTokens"],
        "cache_write_input_tokens": sums["cacheCreationInputTokens"],
        "output_tokens": sums["outputTokens"],
    }


def record_ledger(
    worker: dict[str, Any],
    name: str,
    turn: int,
    seconds: float,
    result: str,
    tokens: dict[str, int | None] | None,
    usage_before: dict[str, Any] | None,
    usage_after: dict[str, Any] | None,
    basis: str,
    unreported_turns: int = 0,
) -> None:
    """Append the turn's ledger row and warn on a low cache share from the worker's second turn (not for an
    `aggregate` row: it mixes several turns, so its share says nothing about this one). A ledger failure never hides
    the turn: it is printed."""
    if worker.get("backend") == "codex":
        sibling("codex_backend").require_worker(worker)
    settings = worker.get("_usage")
    if not settings:
        return
    snap = sibling("usage_snapshot")
    counts = tokens or {}
    warning = (
        None
        if basis == "aggregate"
        else snap.cache_warning(
            turn,
            counts.get("input_tokens"),
            counts.get("cached_input_tokens"),
            settings["cache_warn_ratio"],
        )
    )
    row = {
        "time": now(),
        "task": worker.get("label") or None,
        "worker": name,
        "backend": worker.get("backend", "claude"),
        "model": worker.get("model"),
        "effort": worker.get("effort"),
        "turn": turn,
        "seconds": round(seconds, 1),
        **{
            key: counts.get(key)
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
            )
        },
        "usage_basis": basis,
        "unreported_turns_included": unreported_turns,
        "result": result,
        "usage_before": usage_before,
        "usage_after": usage_after,
        "warnings": [warning] if warning else [],
    }
    if warning:
        print(f"WARNING: {name} turn {turn}: {warning}", file=sys.stderr)
    try:
        snap.append_ledger(LEDGER, row)
    except OSError as exc:
        print(f"WARNING: ledger row not written ({exc})", file=sys.stderr)


def clean_scratch(cx: dict[str, Any], cwd: str, drop_directory: bool = False) -> list[str]:
    """Remove the contents of the worker's own scratch directory after a turn (Codex cannot delete what it created
    there). Only a directory this launcher created and marked (`codex_backend.scratch_problem`): an unmarked, linked
    or foreign one is never deleted. The marker stays so the next turn reuses the directory, unless `drop_directory`
    (retire) removes the directory too; returns the entries not removed."""
    problem = sibling("codex_backend").scratch_problem(cx, Path(cwd))
    if problem:
        return [f"{problem}; not removed"]
    scratch = Path(cx["scratch"])
    failed: list[str] = []
    if not scratch.exists() and not is_link(scratch):
        return failed
    marker = sibling("codex_backend").SCRATCH_WORKER_MARKER
    for entry in sorted(scratch.iterdir()):
        if entry.name == marker:
            continue
        if is_link(entry):  # dropped as an entry, its target is never entered
            remove_entry(entry, entry.unlink if entry.is_symlink() else entry.rmdir, [], failed)
        elif entry.is_dir():
            remove_tree_no_follow(entry, [], failed)
        else:
            remove_entry(entry, entry.unlink, [], failed)
    if drop_directory and not failed:
        remove_entry(scratch / marker, (scratch / marker).unlink, [], failed)
        remove_entry(scratch, scratch.rmdir, [], failed)
    return failed


def run_codex_turn(
    name: str,
    worker: dict[str, Any],
    message: str,
    resume: bool,
    timeout_minutes: float,
    log_number: int,
) -> int:
    """One Codex turn: `codex exec` / `exec resume` with the prompt on stdin, outcome from the `--json` events."""
    backend, snap = sibling("codex_backend"), sibling("usage_snapshot")
    backend.require_worker(worker)
    cx = worker["codex"]
    job_dir = JOBS / name
    job_dir.mkdir(parents=True, exist_ok=True)
    turn = worker.get("turns", 0) + 1
    log = job_dir / f"{log_number:03d}.jsonl"
    result_file = job_dir / f"{log_number:03d}.result.md"
    prompt_file = job_dir / f"{log_number:03d}.prompt.md"
    write_prompt(prompt_file, message)
    try:
        policy_file = backend.hook_module().prepare(worker)
        cmd = backend.build_command(worker, resume, result_file)
        env = backend.child_env(worker, os.environ, ROOT)
    except (SystemExit, OSError, ValueError):
        mark_error(
            name, "error refused", worker["session_id"]
        )  # the worker was marked busy: never leave it so
        raise
    home, stale = Path(cx["home"]), worker["_usage"]["stale_minutes"]
    session_file_limit = worker["_usage"]["session_file_limit"]
    before = snap.ledger_usage(snap.codex_snapshot(home, time.time(), stale, session_file_limit))
    git_timeout = guard_config()["git_timeout_seconds"]
    audit = open_git_audit(name, worker, log, git_timeout)
    started = time.monotonic()
    survivors: list[str] = []
    failure: BaseException | None = None
    try:
        rc = launch_turn(
            name, worker, cmd, env, log, timeout_minutes, stdin_file=prompt_file, leftovers=survivors,
            stop_seconds=git_timeout,
        )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        git_changed = close_git_audit(name, worker, audit, survivors, result_file, failure)
    seconds = time.monotonic() - started
    leftovers = clean_scratch(cx, worker["cwd"])
    if leftovers:
        print("WARNING: scratch not fully removed: " + "; ".join(leftovers), file=sys.stderr)
    parsed = backend.parse_log(log.read_text(encoding="utf-8", errors="replace"))
    thread_id = worker.get("thread_id") or parsed["thread_id"]
    problems = []
    if rc != 0:
        problems.append(f"exit code {rc}")
    if not parsed["completed"]:
        problems.append("no turn.completed event")
    problems += [f"event: {text}" for text in parsed["fatal"]]
    if not thread_id:
        problems.append("no thread id in the event stream (not resumable)")
    context = None
    images: list[str] = []
    try:
        context, images = backend.hook_module().read_receipts(worker, policy_file, thread_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Transfers that completed before the failure stay recorded.
        images = list(getattr(exc, "moved", []))
        problems.append(f"host hook receipts/image custody: {exc}")
    ok = not problems
    given = backend.usage_fields(parsed["usage"]) if parsed["usage"] else None
    gap = worker.get("codex_usage_gap", 0)
    gap = gap if isinstance(gap, int) and not isinstance(gap, bool) and gap >= 0 else 0
    delta, basis, folded = backend.attribute_usage(given, worker.get("codex_usage_total"), gap)
    text = ""
    try:
        text = (
            result_file.read_text(encoding="utf-8", errors="replace")
            if result_file.is_file()
            else ""
        )
    except OSError:
        text = ""
    if not text.strip():
        result_file.write_text(
            parsed["result"]
            or f"(no agent message; {'; '.join(problems) or 'empty'}; see {log.name})",
            encoding="utf-8",
        )
    after = snap.ledger_usage(snap.codex_snapshot(home, time.time(), stale, session_file_limit))
    with RegistryLock():
        data = load()
        w = data.get(name)
        if not isinstance(w, dict) or w.get("session_id") != worker["session_id"]:
            print(f"worker {name}: registry entry changed during the turn; result not recorded")
            print(f"result file: {result_file}")
            return 0 if ok else 1
        backend.require_worker(w)
        for key in PROCESS_KEYS:
            w.pop(key, None)
        w.pop("current_log", None)
        w.update(
            turns=turn,
            last_used=now(),
            status="idle" if ok else f"error rc={rc}",
            context_tokens=context,
            generated_images=images,
            thread_id=thread_id,
            minutes_last_turn=round(seconds / 60, 1),
        )
        if flag_history_change(w, git_changed, result_file):
            ok = False
            problems.append(GIT_HISTORY_CHANGED)
        if given is not None:
            w["codex_usage_total"] = given
            w.pop("codex_usage_gap", None)
        else:
            # This turn's tokens will arrive inside a later cumulative total.
            w["codex_usage_gap"] = gap + 1
        audit_done = finish_git_audit(w)
        save(data)
    drop_closed_audit(audit_done)
    record_ledger(
        {**worker, "thread_id": thread_id},
        name,
        turn,
        seconds,
        "ok" if ok else "error: " + "; ".join(problems),
        delta,
        before,
        after,
        basis,
        folded,
    )
    print(
        f"worker {name} turn {turn} rc={rc} {'ok' if ok else 'ERROR ' + '; '.join(problems)} thread={thread_id}"
    )
    print(f"result file: {result_file}")
    return 0 if ok else 1


def run_turn(
    name: str,
    worker: dict[str, Any],
    message: str,
    resume: bool,
    timeout_minutes: float,
    log_number: int,
) -> int:
    if worker.get("backend") == "codex":
        return run_codex_turn(name, worker, message, resume, timeout_minutes, log_number)
    job_dir = JOBS / name
    job_dir.mkdir(parents=True, exist_ok=True)
    turn = worker.get("turns", 0) + 1
    log = job_dir / f"{log_number:03d}.jsonl"
    result_file = job_dir / f"{log_number:03d}.result.md"
    prompt_file = job_dir / f"{log_number:03d}.prompt.md"
    write_prompt(prompt_file, message)
    cmd = build_command(worker, resume)
    git_timeout = guard_config()["git_timeout_seconds"]
    audit = open_git_audit(name, worker, log, git_timeout)
    started = time.monotonic()
    survivors: list[str] = []
    failure: BaseException | None = None
    try:
        rc = launch_turn(
            name,
            worker,
            cmd,
            child_env(worker.get("cache_ttl", "1h"), worker.get("autocompact_window")),
            log,
            timeout_minutes,
            stdin_file=prompt_file,
            leftovers=survivors,
            stop_seconds=git_timeout,
        )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        git_changed = close_git_audit(name, worker, audit, survivors, result_file, failure)
    result, context, cost = None, None, None
    model_usage: dict[str, Any] = {}
    first_read = None
    for m in stream_events(log.read_text(encoding="utf-8", errors="replace")):
        usage = assistant_usage(m)
        if usage is not None:
            if first_read is None:
                first_read = usage.get("cache_read_input_tokens") or 0
            counted = context_of(usage)
            if counted is not None:
                context = counted
        if m.get("type") == "result":
            if isinstance(m.get("result"), str):
                result = m["result"]
            if isinstance(m.get("total_cost_usd"), (int, float)):
                cost = m["total_cost_usd"]
            model_usage = model_usage_summary(m)
    result_file.write_text(
        result or f"(no result event; exit code {rc}; see {log.name})", encoding="utf-8"
    )
    with RegistryLock():
        data = load()
        w = data.get(name)
        if not isinstance(w, dict) or w.get("session_id") != worker["session_id"]:
            # Retired (and maybe restarted under the same name) while this turn ran: the entry is not ours any more.
            print(f"worker {name}: registry entry changed during the turn; result not recorded")
            print(f"result file: {result_file}")
            return 0 if rc == 0 else 1
        if resume and w.get("context_tokens"):
            # Resume cache check: the first call should read about the previous context.
            w["last_resume_cache"] = f"read {first_read} of {w['context_tokens']}"
            print(f"resume cache: first call read {first_read} of previous {w['context_tokens']}")
        for key in PROCESS_KEYS:
            w.pop(key, None)
        w.pop("current_log", None)
        w.update(
            turns=turn,
            last_used=now(),
            status="idle" if rc == 0 else f"error rc={rc}",
            context_tokens=context,
            last_cost_usd=cost,
            last_model_usage=model_usage,
            minutes_last_turn=round((time.monotonic() - started) / 60, 1),
        )
        flagged = flag_history_change(w, git_changed, result_file)
        audit_done = finish_git_audit(w)
        save(data)
    drop_closed_audit(audit_done)
    if flagged:
        rc = rc or 1  # the PM reviews the changed refs before integrating this worktree
    # Claude usage windows are a PM-written file, not collected per turn: before/after stay null (documented).
    record_ledger(
        worker,
        name,
        turn,
        time.monotonic() - started,
        "ok" if rc == 0 else f"error rc={rc}",
        claude_token_totals(model_usage),
        None,
        None,
        "turn",
    )
    print(f"worker {name} turn {turn} rc={rc} context={context} cost={cost}")
    print(f"result file: {result_file}")
    return 0 if rc == 0 else 1


def notify(name: str, message: str) -> int:
    """Queue a message in the inbox that hooks/ctx_nudge.py reads; a later message is appended to an undelivered
    one. Only a busy worker with the PostToolUse hook can receive it; an idle worker takes `send` instead."""
    if not message.strip():
        raise SystemExit("the message is empty")
    with RegistryLock():
        w = load().get(name)
        if not isinstance(w, dict) or w.get("status") == "retired":
            raise SystemExit(f"worker {name} is not registered")
        if w.get("status") != "busy":
            raise SystemExit(f"worker {name} is {w.get('status')}; use send for an idle worker")
        if w.get("backend") == "codex":
            raise SystemExit(
                f"worker {name} is a Codex worker: Codex has no PostToolUse hook, so nothing can deliver a message "
                "during a turn; wait for the turn to end and use send"
            )
        hooks = w.get("hooks") or {}
        if hooks.get("ctx_nudge", "off") == "off" and not hooks.get("loop_window"):
            raise SystemExit(
                f"worker {name} runs without the PostToolUse hook, so nothing can deliver it"
            )
        INBOX.mkdir(parents=True, exist_ok=True)
        target = INBOX / f"{re.sub(r'[^A-Za-z0-9_-]', '_', str(w['session_id']))[:80]}.md"
        try:
            earlier = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            earlier = ""
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(
            f"{earlier.rstrip()}\n\n---\n\n{message.strip()}\n"
            if earlier.strip()
            else f"{message.strip()}\n",
            encoding="utf-8",
        )
        # Atomic: the hook claims the file by rename, so it never reads a half-written message.
        os.replace(tmp, target)
    print(f"queued for {name}; delivered at its next tool call: {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--name", required=True)
    s.add_argument(
        "--backend",
        choices=BACKENDS,
        default="claude",
        help="claude (default) or codex (decision 596: role from codex_profiles.json, README.md 'Codex workers')",
    )
    s.add_argument(
        "--codex-config",
        help="codex backend: a local copy of codex_profiles.json (default: the tracked file, whose placeholders are refused)",
    )
    s.add_argument(
        "--profile",
        help=(
            "worker role from worker_profiles.json (normal path, decision 379): supplies the auto-compact window, "
            "context-nudge thresholds, cache TTL and optionally model and effort; any explicit flag overrides it. "
            "With --backend codex: a role of codex_profiles.json (model, effort, web search, timeout)"
        ),
    )
    # Required unless the profile supplies them (checked after parsing).
    s.add_argument("--model")
    s.add_argument("--effort", choices=EFFORTS)
    s.add_argument("--cwd", required=True)
    s.add_argument("--message-file", required=True)
    s.add_argument("--lint-waive", help=LINT_WAIVE_HELP)
    s.add_argument("--label", default="")
    s.add_argument(
        "--timeout-minutes",
        type=int,
        help=f"turn limit; default {DEFAULT_TIMEOUT_MINUTES} (claude) or the codex role's timeout_minutes",
    )
    # Required so every start is a conscious cost choice: 1h for multi-turn chains or turns
    # with long tool runs, 5m for short one-shot jobs with no follow-up.
    s.add_argument("--cache-ttl", choices=CACHE_TTLS)
    # WORKFLOW-GUARD hooks (see WORKFLOW_GUARD.md); they apply to this worker for its whole life.
    s.add_argument(
        "--process-guard",
        choices=["on", "off"],
        default="on",
        help="block process stops by name/pattern (footer rule 12); default on",
    )
    s.add_argument(
        "--tool-guard",
        choices=["on", "off"],
        default="on",
        help=(
            "shell guards (hooks/tool_guard.py: shell writes, git, inline code, pytest slot) plus tool_monitor and "
            "the Bash probe; off restores the prefix git denies; default on"
        ),
    )
    s.add_argument(
        "--git-guard",
        choices=GIT_GUARD_MODES,
        default="readonly",
        help="readonly: read-only git, the only mode (workers never commit, push or merge, decision 77); "
        "default %(default)s",
    )
    s.add_argument(
        "--task-file",
        help="the worker's task file (.md inside the root): its PreCompact hook writes an AUTO PROGRESS block there",
    )
    s.add_argument(
        "--ctx-nudge",
        help=(
            "context-size nudge thresholds in tokens, comma separated, or 'off'; default: the --profile value, "
            f"else {DEFAULT_CTX_NUDGE} (no profile)"
        ),
    )
    s.add_argument(
        "--autocompact-window",
        help=(
            f"auto-compact window in tokens ({AUTOCOMPACT_MIN}-{AUTOCOMPACT_MAX}) for this worker only, or 'off' to keep "
            "Claude Code's own window; fixed for the worker's life (send never changes it); default: the --profile "
            f"value, else {DEFAULT_AUTOCOMPACT_WINDOW} (no profile)"
        ),
    )
    s.add_argument(
        "--loop-warning",
        type=int,
        default=DEFAULT_LOOP_WINDOW,
        help="warn after N identical consecutive tool calls; 0 disables; default %(default)s",
    )
    s.add_argument(
        "--mcp",
        choices=MCP_MODES,
        default="default",
        help="minimal: --strict-mcp-config with an empty server list (no MCP tools or instructions); default: unchanged",
    )
    s.add_argument(
        "--advisor",
        choices=ADVISORS,
        default="off",
        help="Claude Code advisor model consulted by the worker (decision 266 pilot); default off",
    )
    m = sub.add_parser("send")
    m.add_argument("--name", required=True)
    m.add_argument("--message-file", required=True)
    m.add_argument("--effort", choices=EFFORTS)
    m.add_argument("--label", default="")
    m.add_argument(
        "--timeout-minutes",
        type=int,
        help=f"turn limit; default {DEFAULT_TIMEOUT_MINUTES} (claude) or the worker's stored timeout (codex)",
    )
    m.add_argument("--lint-waive", help=LINT_WAIVE_HELP)
    m.add_argument(
        "--pid-checked",
        action="store_true",
        help="resume although a recorded PID has no creation time: you verified it is not the worker",
    )
    n = sub.add_parser(
        "notify",
        help="queue a PM message for a busy worker; its PostToolUse hook delivers it at the next tool call",
    )
    n.add_argument("--name", required=True)
    n.add_argument("--message-file", required=True)
    st = sub.add_parser("status")
    st.add_argument(
        "--all", action="store_true", help="include retired workers (hidden by default)"
    )
    st.add_argument(
        "--json", action="store_true", help="machine-readable rows (used by pm-supervisor.ps1)"
    )
    r = sub.add_parser("retire")
    r.add_argument("--name", required=True)
    r.add_argument(
        "--force",
        action="store_true",
        help="retire a busy entry that recorded no PID or only a PID that cannot be matched to a process "
        "(cannot be judged); never overrides a process proven to be the worker's",
    )
    r.add_argument(
        "--purge-transcript",
        action="store_true",
        help="also delete the worker's own Claude transcript <session_id>.jsonl, its <session_id>/ directory and "
        "the emptied project folder (decision 501); turn logs stay",
    )
    a = ap.parse_args()
    if a.cmd in ("start", "send") and a.timeout_minutes is not None and a.timeout_minutes <= 0:
        ap.error("--timeout-minutes must be positive")
    codex_start: dict[str, Any] | None = None
    if a.cmd == "start":
        if a.backend == "codex":
            codex_start = codex_start_fields(ap, a)
        else:
            apply_profile(ap, a)

    if a.cmd == "status":
        rows = status_rows(a.all)
        if a.json:
            print(json.dumps(rows))
            return 0
        for row in rows:
            print(format_row(row))
        hidden = 0 if a.all else sum(1 for w in load().values() if w.get("status") == "retired")
        if hidden:
            print(f"({hidden} retired workers hidden; --all lists them)")
        return 0
    if a.cmd == "retire":
        with RegistryLock():
            data = load()
            entry = data.get(a.name)
            if not isinstance(entry, dict):
                raise SystemExit(f"worker {a.name} is not registered")
            running = live_processes(entry)
            if running:
                raise SystemExit(
                    f"worker {a.name} still runs ({', '.join(running)}); stop its process tree first"
                )
            if (
                entry.get("status") == "busy"
                and not as_pid(entry.get("launcher_pid"))
                and not a.force
            ):
                raise SystemExit(
                    f"worker {a.name} is busy and recorded no PID, so it cannot be judged; use --force once you "
                    "verified that nothing runs"
                )
            unverified = recorded_processes(entry, UNKNOWN)
            if unverified and not a.force:
                raise SystemExit(unverifiable_message(a.name, unverified))
            entry["status"] = "retired"
            for key in (*PROCESS_KEYS, "current_log"):
                entry.pop(key, None)
            save(data)
            failed: list[str] = []
            if entry.get("backend") == "codex":
                # Host-side cleanup of the worker's own scratch directory (Codex cannot delete what it created there).
                problem = sibling("codex_backend").stored_codex_problem(entry.get("codex"))
                if problem:
                    print(
                        f"worker {a.name} retired; incompatible stored configuration ({problem}); cleanup skipped"
                    )
                else:
                    failed += clean_scratch(entry["codex"], str(entry["cwd"]), drop_directory=True)
            if a.purge_transcript:
                if entry.get("backend") == "codex":
                    thread = entry.get("thread_id")
                    deleted = (
                        sibling("codex_backend").purge_session(
                            Path(entry["codex"]["home"]), thread, failed
                        )
                        if thread and not problem
                        else []
                    )
                    print(
                        "Codex session purge skipped: incompatible stored configuration"
                        if problem
                        else "\n".join(f"deleted {p}" for p in deleted)
                        or "no Codex session file found"
                    )
                else:
                    deleted = purge_transcript(str(entry.get("session_id", "")), failed=failed)
                    print("\n".join(f"deleted {p}" for p in deleted) or "no transcript found")
            if failed:
                print("\n".join(f"NOT removed {line}" for line in failed))
                print(f"worker {a.name} is retired; {len(failed)} path(s) could not be removed")
                return 1
        return 0

    if a.cmd == "notify":
        return notify(a.name, Path(a.message_file).read_text(encoding="utf-8-sig"))

    message = Path(a.message_file).read_text(encoding="utf-8-sig")
    lint_gate(a.cmd, a.name, message, a.lint_waive)
    usage_settings = turn_usage_settings(a, codex_start)
    unaudited: list[str] = []
    proven: list[str] = []
    with RegistryLock():
        data = load()
        if a.cmd == "start":
            old = data.get(a.name)
            if isinstance(old, dict):
                if old.get("status") != "retired":
                    raise SystemExit(f"worker {a.name} exists; retire it first")
                running = live_processes(old)
                if running:
                    raise SystemExit(
                        f"a process of the retired worker {a.name} still runs ({', '.join(running)}): "
                        "the name cannot be reused until it ends"
                    )
                unverified = recorded_processes(old, UNKNOWN)
                if unverified:
                    raise SystemExit(unverifiable_message(a.name, unverified))
            cwd = Path(a.cwd).resolve()
            if not cwd.is_relative_to(ROOT):
                raise SystemExit("cwd must be inside the project root")
            worktrees = (ROOT / guard_config()["git_guard"]["worktrees_dir"]).resolve()
            task_file = validate_task_file(a.task_file, cwd, worktrees)
            if codex_start is not None:
                # A fresh binding: new registry id, no thread id until the first turn reports one (a retired name
                # restarted never resumes its predecessor's thread).
                data[a.name] = {
                    "backend": "codex",
                    "session_id": str(uuid.uuid4()),
                    "thread_id": None,
                    "model": codex_start["model"],
                    "effort": codex_start["effort"],
                    "cwd": str(cwd),
                    "created": now(),
                    "turns": 0,
                    "profile": a.profile,
                    "timeout_minutes": codex_start["codex"]["timeout_minutes"],
                    "autocompact_window": None,
                    "hooks": {"ctx_nudge": "off", "loop_window": 0, "task_file": task_file},
                    "codex": codex_start["codex"],
                    BRIEF_ORDER_KEY: BRIEF_ORDER,
                }
            else:
                data[a.name] = claude_entry(a, cwd, task_file)
        else:
            w = data.get(a.name)
            if not w or w["status"] == "retired":
                raise SystemExit(f"worker {a.name} is not available")
            if w.get("backend") == "codex":
                sibling("codex_backend").require_worker(w)
            running = live_processes(w)
            if running:
                raise SystemExit(
                    f"worker {a.name} is not available: {', '.join(running)} still runs"
                )
            unverified = recorded_processes(w, UNKNOWN)
            if unverified and not a.pid_checked:
                raise SystemExit(unverifiable_message(a.name, unverified))
            if w["status"] == "busy":
                pid = as_pid(w.get("launcher_pid"))
                if not pid:
                    raise SystemExit(f"worker {a.name} is not available")
                # The launcher and its child are gone without recording the end of the turn: resumable.
                print(f"WARNING: {a.name} was busy but its launcher (pid {pid}) is gone; resuming")
            finding = audit_finding(w)
            pinned = w.get("git_audit")
            if finding:
                # T-024 F16/CX2: a turn without a proven Git audit is judged as changing Git (fail closed); its start
                # state, if any, stays in the state directory as `<id>.flagged.json` for the PM's review.
                w["git_changes"] = [*w.get("git_changes", []), f"{finding}; compare it by hand"][-50:]
                print(f"WARNING: {a.name}: {finding}: flagged {GIT_HISTORY_CHANGED}", file=sys.stderr)
            # Counted once: the pin leaves the entry; a proven audit's closed file goes, unproven evidence is kept.
            w.pop("git_audit", None)
            if isinstance(pinned, dict) and isinstance(pinned.get("id"), str) and AUDIT_ID.fullmatch(pinned["id"]):
                (unaudited if finding else proven).append(pinned["id"])
            if a.effort:
                w["effort"] = (
                    sibling("codex_backend").validate_effort("--effort", a.effort)
                    if w.get("backend") == "codex"
                    else a.effort
                )
            ttl_minutes = 60 if w.get("cache_ttl", "1h") == "1h" else 5
            if w.get("last_used") and w.get("backend") != "codex":
                idle_minutes = (
                    dt.datetime.now() - dt.datetime.fromisoformat(w["last_used"])
                ).total_seconds() / 60
                if idle_minutes > ttl_minutes:
                    # The cache expired: resuming rewrites the whole context at the write
                    # price. A fresh worker with a short handoff is usually cheaper.
                    print(
                        f"WARNING: {a.name} idle {idle_minutes:.0f} min > TTL {ttl_minutes} min; "
                        f"resume rewrites about {w.get('context_tokens')} tokens"
                    )
        w = data[a.name]
        log_number = next_log_number(JOBS / a.name, w.get("turns", 0))
        for key in (
            PROCESS_KEYS
        ):  # none of the previous turn's processes runs (checked above): drop stale identities
            w.pop(key, None)
        w.update(
            status="busy",
            label=a.label or w.get("label", ""),
            last_used=now(),
            launcher_pid=os.getpid(),
            current_log=f"{log_number:03d}.jsonl",
        )
        launcher_created = own_start(os.getpid())
        if launcher_created is not None:
            w["launcher_created"] = launcher_created
        save(data)
        for audit_id in proven:
            drop_closed_audit(audit_id)
        for audit_id in unaudited:  # flagged and saved: whatever evidence exists is kept under a name not read again
            for state in ("open", "closed"):
                if audit_path(audit_id, state).exists():
                    os.replace(audit_path(audit_id, state), audit_path(audit_id, "flagged"))
    timeout = (
        a.timeout_minutes
        if a.timeout_minutes is not None
        else w.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
    )
    try:
        return run_turn(
            a.name,
            {**w, "_usage": usage_settings},
            message,
            resume=a.cmd == "send",
            timeout_minutes=timeout,
            log_number=log_number,
        )
    except BaseException as exc:
        # T-024 F16: whatever fails after the entry was marked busy, it never stays busy (a flag stays the status).
        mark_error(a.name, f"error {type(exc).__name__}", w["session_id"], only_busy=True)
        raise


def claude_entry(a: argparse.Namespace, cwd: Path, task_file: str | None) -> dict[str, Any]:
    """Registry entry of a new Claude worker (validates the window and nudge, and that the hook interpreter exists)."""
    entry: dict[str, Any] = {
        "session_id": str(uuid.uuid4()),
        "model": a.model,
        "effort": a.effort,
        "cwd": str(cwd),
        "created": now(),
        "turns": 0,
        "cache_ttl": a.cache_ttl,
        "profile": a.profile,
        "mcp": a.mcp,
        "advisor": None if a.advisor == "off" else a.advisor,
        BRIEF_ORDER_KEY: BRIEF_ORDER,
        "hooks": {
            "process_guard": a.process_guard == "on",
            "ctx_nudge": validate_ctx_nudge(a.ctx_nudge),
            "loop_window": max(0, a.loop_warning),
            "tool_guard": a.tool_guard == "on",
            "git_guard": a.git_guard,
            "task_file": task_file,
        },
    }
    window = validate_autocompact_window(a.autocompact_window)
    problem = nudge_window_error(entry["hooks"]["ctx_nudge"], window)
    if problem:
        raise SystemExit(problem)
    entry["autocompact_window"] = window
    venv_python()  # fail at start, not at the first turn, when the interpreter for the hooks is missing
    return entry


# Start flags that only mean something to a Claude worker (value: the argparse default); a Codex worker refuses them.
CLAUDE_ONLY_FLAGS = (
    ("cache_ttl", None),
    ("ctx_nudge", None),
    ("autocompact_window", None),
    ("mcp", "default"),
    ("advisor", "off"),
    ("git_guard", "readonly"),
)


def codex_start_fields(ap: argparse.ArgumentParser, a: argparse.Namespace) -> dict[str, Any]:
    """Validated settings of a new Codex worker (`start --backend codex`): the role of codex_profiles.json, explicit
    --model/--effort/--timeout-minutes flags on top, resolved to absolute paths. Refuses what a Codex worker cannot honour."""
    backend = sibling("codex_backend")
    for attr, default in CLAUDE_ONLY_FLAGS:
        if getattr(a, attr) != default:
            ap.error(
                f"--{attr.replace('_', '-')} applies to claude workers only (Codex has no such mechanism)"
            )
    if not a.profile:
        ap.error("--backend codex needs --profile <role of codex_profiles.json>")
    config_path = Path(a.codex_config).resolve() if a.codex_config else None
    cfg = backend.load_codex_config(config_path)
    cwd = Path(a.cwd).resolve()
    if not cwd.is_relative_to(ROOT):
        raise SystemExit("cwd must be inside the project root")
    given = {"model": a.model, "effort": a.effort, "timeout_minutes": a.timeout_minutes}
    overrides = {key: value for key, value in given.items() if value is not None}
    cx, model, effort = backend.resolve_worker(
        cfg, a.profile, ROOT, cwd, a.name, config_path, os.environ, overrides
    )
    return {"codex": cx, "model": model, "effort": effort, "usage": cfg["usage"], "cwd": cwd}


def codex_gates(worker: dict[str, Any]) -> None:
    """Refusals before any registry change or process: files exist, the secrets deny is proven (fail closed)."""
    backend = sibling("codex_backend")
    backend.require_worker(worker)
    backend.preflight(worker["codex"], ROOT)
    env = backend.child_env(worker, os.environ, ROOT)
    try:
        backend.hook_module().check_version(worker["codex"], env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    backend.startup_check(worker, ROOT, env)


def turn_usage_settings(
    a: argparse.Namespace, codex_start: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Ledger settings of this turn, and the Codex gates (run before the registry lock: the check spawns a process).

    A Codex worker's settings come from its own config file and a refusal stops the turn. A Claude worker uses the
    tracked file's `usage` block; if that is unreadable the ledger is skipped with a warning (Claude turns still run)."""
    backend = sibling("codex_backend")
    if a.cmd == "start" and codex_start is not None:
        codex_gates(
            {
                "cwd": str(codex_start["cwd"]),
                "codex": codex_start["codex"],
                "model": codex_start["model"],
                "effort": codex_start["effort"],
            }
        )
        return dict(codex_start["usage"])
    entry = load().get(a.name) if a.cmd == "send" else None
    if (
        isinstance(entry, dict)
        and entry.get("backend") == "codex"
        and entry.get("status") != "retired"
    ):
        backend.require_worker(entry)
        if a.effort:
            entry = {**entry, "effort": backend.validate_effort("--effort", a.effort)}
        backend.require_thread(entry)
        codex_gates(entry)
        return dict(backend.load_usage_settings(Path(entry["codex"]["config_path"])))
    try:
        return dict(backend.load_usage_settings(None))
    except SystemExit as exc:
        print(f"WARNING: no ledger for this turn ({exc})", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
