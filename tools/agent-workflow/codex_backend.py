"""Codex CLI backend of the PM worker launcher (decision 596). Documentation: README.md, "Codex workers".

Pure helpers used by cli_worker.py (and usage_snapshot.py): config loading and refusals, the `codex exec` command
line, the child environment, the fail-closed secrets check, `--json` event parsing, per-turn usage deltas and the
exact-session purge. Nothing here starts a model call by itself; `startup_check` runs `codex sandbox` (no model).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "codex_profiles.json"
# Only reader selection uses this seam; tests never mutate the shared sys module.
READER_PLATFORM = sys.platform
VENV_READ_GRANT_HELP = (
    "The base interpreter needs a read-only grant for CodexSandboxOffline and CodexSandboxOnline; "
    "the user must set this up, never an agent. See README.md, Codex workers."
)

TOP_KEYS = ("runtime", "roles", "usage")
RUNTIME_KEYS = (
    "codex_command",
    "codex_home",
    "windows_sandbox",
    "permission_profile",
    "extra_deny_paths",
    "extra_write_paths",
    "scratch_dir",
    "venv_dir",
    "check_timeout_seconds",
    "env_allow",
    "disabled_features",
    "tested_codex_version",
    "hooks",
)
# Variables a Codex child may inherit (compared upper-case): process basics, locations and locale only. Everything
# else, in particular every credential variable, is dropped; `runtime.env_allow` adds names (never credential-like).
ENV_ALLOWLIST = frozenset(
    {
        *(
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "COMSPEC",
            "OS",
            "USERNAME",
            "USERDOMAIN",
        ),
        *(
            "USERPROFILE",
            "HOME",
            "HOMEDRIVE",
            "HOMEPATH",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMDATA",
            "PUBLIC",
        ),
        *(
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "PROGRAMW6432",
            "ALLUSERSPROFILE",
            "COMMONPROGRAMFILES",
        ),
        *(
            "COMMONPROGRAMFILES(X86)",
            "COMMONPROGRAMW6432",
            "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS",
        ),
        *(
            "LANG",
            "LANGUAGE",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "SHELL",
            "USER",
            "LOGNAME",
            "CODEX_HOME",
        ),
    }
)
CREDENTIAL_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|ANTHROPIC|OPENAI", re.IGNORECASE
)
ROLE_KEYS = ("model", "effort", "web_search", "timeout_minutes", "hook_allowed_tools")
USAGE_KEYS = (
    "stale_minutes",
    "session_file_limit",
    "cache_warn_ratio",
    "antigravity_command",
    "command_timeout_seconds",
)
# `web_search` values of Codex 0.160 (developers.openai.com/codex/config-sample).
WEB_SEARCH = ("disabled", "cached", "live")
WINDOWS_SANDBOX = ("elevated", "unelevated")
# Refused by decision 596: an effort above the task's plan. Other efforts are the model's own business.
REFUSED_EFFORTS = ("ultra",)
# Keys that would change the sandbox or the service tier: refused by name so the message says why.
REFUSED_KEYS = {
    "sandbox": "the sandbox is fixed by the permission profile",
    "sandbox_mode": "`sandbox_mode` replaces default_permissions (Codex docs)",
    "service_tier": "the fast service tier is refused (decision 596)",
}
# Pins that switch the fast service tier off on every call, so an ambient `service_tier` or `features.fast_mode`
# of ~/.codex/config.toml cannot apply (safety-bound, decision 596). `features.fast_mode=false` is verified offline
# (`codex features list -c features.fast_mode=false` shows it false); `service_tier="default"` is the documented
# name of Standard (config reference, `memories.consolidation_service_tier`) and awaits the PM's live check.
SERVICE_TIER_PIN = 'service_tier="default"'
FAST_MODE_PIN = "features.fast_mode=false"
# Platform mechanics of the read probe, not deployment choices (protocol-bound). The reader exits with a code that
# names the exception class, so the check never matches message text (it varies with locale and shell).
CONTROL_TOKEN = "codex-startup-check-control"
READER_DENIED, READER_MISSING, READER_OTHER = 77, 66, 74
READER_SOURCE = (
    "import sys\n"
    "try:\n"
    "    with open(sys.argv[1], 'rb') as handle:\n"
    "        data = handle.read(256)\n"
    "except PermissionError:\n"
    f"    sys.exit({READER_DENIED})\n"
    "except FileNotFoundError:\n"
    f"    sys.exit({READER_MISSING})\n"
    "except OSError:\n"
    f"    sys.exit({READER_OTHER})\n"
    "if sys.argv[2:] == ['echo']:\n"
    "    sys.stdout.write(data.decode('utf-8', 'replace'))\n"
)
# Markers of directories this launcher created (ownership proof before any deletion; tool-specific, never a
# generic file name).
SCRATCH_ROOT_MARKER = ".codex-scratch-root"
SCRATCH_WORKER_MARKER = ".codex-worker-scratch"
THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ROLLOUT = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<id>[0-9a-f-]{36})\.jsonl$")
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
PROFILE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
EFFORT_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def refuse(message: str) -> NoReturn:
    raise SystemExit(message)


def hook_module() -> Any:
    name = "codex_hooks"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def is_placeholder(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def _text(owner: str, field: str, value: object, spaces: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        refuse(f"{owner}, field {field!r}: expected a non-empty string")
    if is_placeholder(value):
        refuse(
            f"{owner}, field {field!r}: {value!r} is a placeholder; set the real value "
            "(README.md, Codex workers) or pass --codex-config <local file>"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or (
        not spaces and any(c.isspace() for c in value)
    ):
        refuse(f"{owner}, field {field!r}: control or white-space characters are not allowed")
    return value


def _positive_int(owner: str, field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        refuse(f"{owner}, field {field!r}: expected a positive integer")
    return value


def _closed(owner: str, entry: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in entry:
        if key in REFUSED_KEYS:
            refuse(f"{owner}: key {key!r} is refused: {REFUSED_KEYS[key]}")
        if key.startswith("dangerously"):
            refuse(f"{owner}: key {key!r} is refused: --dangerously-* flags are refused")
    unknown = sorted(set(entry) - set(keys))
    if unknown:
        refuse(f"{owner}: unknown key(s) {', '.join(unknown)}")
    for key in keys:
        if key not in entry:
            refuse(f"{owner}: missing field {key!r}")


def validate_effort(owner: str, value: object) -> str:
    effort = _text(owner, "effort", value)
    if effort in REFUSED_EFFORTS or not EFFORT_NAME.fullmatch(effort):
        refuse(
            f"{owner}, field 'effort': {effort!r} is refused"
            + (" (decision 596)" if effort in REFUSED_EFFORTS else " (lower-case word expected)")
        )
    return effort


def _relative_dir(owner: str, field: str, value: object) -> str:
    text = _text(owner, field, value, spaces=True)
    parts = re.split(r"[\\/]", text)  # either separator, whatever the platform
    if (
        text.startswith(("/", "\\"))
        or Path(text).is_absolute()
        or ".." in parts
        or Path(text).drive
    ):
        refuse(f"{owner}, field {field!r}: expected a relative path without '..': {text!r}")
    return text


def validate_command(owner: str, field: str, value: object, shim_ok: bool = False) -> list[str]:
    """One executable path and nothing else: arguments never come from configuration, so no value in it can carry a
    forbidden flag. A command line, a list, a `-`-leading word or (unless `shim_ok`) a shim (`.cmd`/`.bat`/`.ps1`,
    which breaks `-c` quoting) is refused."""
    if isinstance(value, list):
        refuse(
            f"{owner}, field {field!r}: expected one executable path (a string), not a command line or a list"
        )
    path = _text(owner, field, value, spaces=True)
    if path.startswith("-"):
        refuse(f"{owner}, field {field!r}: an executable path cannot start with '-'")
    if not shim_ok and Path(path).suffix.lower() in (".cmd", ".bat", ".ps1"):
        refuse(f"{owner}, field {field!r}: a shim breaks -c quoting; name the native executable")
    return [path]


def validate_usage(owner: str, entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict):
        refuse(f"{owner}: expected an object")
    _closed(owner, entry, USAGE_KEYS)
    ratio = entry["cache_warn_ratio"]
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
        refuse(f"{owner}, field 'cache_warn_ratio': expected a number above 0 and at most 1")
    agy = entry["antigravity_command"]
    return {
        "stale_minutes": _positive_int(owner, "stale_minutes", entry["stale_minutes"]),
        "session_file_limit": _positive_int(
            owner, "session_file_limit", entry["session_file_limit"]
        ),
        "cache_warn_ratio": float(ratio),
        "antigravity_command": None
        if agy is None
        else validate_command(owner, "antigravity_command", agy, shim_ok=True)[0],
        "command_timeout_seconds": _positive_int(
            owner, "command_timeout_seconds", entry["command_timeout_seconds"]
        ),
    }


def validate_codex_home(owner: str, value: object) -> str | None:
    """`null` (Codex's own home: CODEX_HOME if set, else ~/.codex) or a path anywhere (PM message 2026-11-07)."""
    return None if value is None else _text(owner, "codex_home", value, spaces=True)


def validate_runtime(entry: object) -> dict[str, Any]:
    owner = "codex runtime"
    if not isinstance(entry, dict):
        refuse(f"{owner}: expected an object")
    _closed(owner, entry, RUNTIME_KEYS)
    hooks = hook_module()
    try:
        disabled = hooks.names(
            entry["disabled_features"], hooks.DISABLED_FEATURES, "runtime.disabled_features", "apps"
        )
        hook_options = hooks.options(entry["hooks"])
    except ValueError as exc:
        refuse(str(exc))
    version = _text(owner, "tested_codex_version", entry["tested_codex_version"])
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        refuse("runtime.tested_codex_version must be an exact release version")
    items = validate_command(owner, "codex_command", entry["codex_command"])
    sandbox = entry["windows_sandbox"]
    if sandbox is not None and sandbox not in WINDOWS_SANDBOX:
        refuse(
            f"{owner}, field 'windows_sandbox': null or one of {', '.join(WINDOWS_SANDBOX)}: {sandbox!r}"
        )
    profile = _text(owner, "permission_profile", entry["permission_profile"])
    if not PROFILE_NAME.fullmatch(profile):
        refuse(
            f"{owner}, field 'permission_profile': a custom profile name (no ':' built-ins): {profile!r}"
        )
    extras = entry["extra_deny_paths"]
    if not isinstance(extras, list):
        refuse(f"{owner}, field 'extra_deny_paths': expected a list")
    writes = entry["extra_write_paths"]
    if not isinstance(writes, list):
        refuse(f"{owner}, field 'extra_write_paths': expected a list")
    venv_dir = _relative_dir(owner, "venv_dir", entry["venv_dir"])
    if any(
        ":" in part or (part != "." and part.rstrip(" .") != part)
        for part in re.split(r"[\\/]", venv_dir)
    ):
        refuse(f"{owner}, field 'venv_dir': expected an unambiguous project-relative path")
    allow = entry["env_allow"]
    if not isinstance(allow, list):
        refuse(f"{owner}, field 'env_allow': expected a list of variable names")
    allowed = [_text(owner, "env_allow", n) for n in allow]
    credential_like = [n for n in allowed if CREDENTIAL_NAME.search(n)]
    if credential_like:
        refuse(
            f"{owner}, field 'env_allow': credential-like names are never passed: {', '.join(credential_like)}"
        )
    return {
        "env_allow": allowed,
        "disabled_features": disabled,
        "tested_codex_version": version,
        "hooks": hook_options,
        "codex_command": items,
        "codex_home": validate_codex_home(owner, entry["codex_home"]),
        "windows_sandbox": sandbox,
        "permission_profile": profile,
        "extra_deny_paths": [_text(owner, "extra_deny_paths", p, spaces=True) for p in extras],
        "extra_write_paths": [_write_relative(owner, p) for p in writes],
        "scratch_dir": _relative_dir(owner, "scratch_dir", entry["scratch_dir"]),
        "venv_dir": venv_dir,
        "check_timeout_seconds": _positive_int(
            owner, "check_timeout_seconds", entry["check_timeout_seconds"]
        ),
    }


def validate_model(owner: str, value: object) -> str:
    """A model id is one word that cannot be read as a flag (it follows `-m`)."""
    model = _text(owner, "model", value)
    if model.startswith("-"):
        refuse(f"{owner}, field 'model': {model!r} cannot start with '-'")
    return model


def validate_role(role: str, entry: object) -> dict[str, Any]:
    owner = f"codex role {role!r}"
    if not isinstance(entry, dict):
        refuse(f"{owner}: expected an object")
    _closed(owner, entry, ROLE_KEYS)
    hooks = hook_module()
    try:
        allowed_tools = hooks.names(entry["hook_allowed_tools"], hooks.TOOLS, "hook_allowed_tools")
        if hooks.IMAGE_TOOL in allowed_tools and role != "codex-image":
            raise ValueError("image tool requires the codex-image role")
    except ValueError as exc:
        refuse(str(exc))
    web = entry["web_search"]
    if web not in WEB_SEARCH:
        refuse(f"{owner}, field 'web_search': one of {', '.join(WEB_SEARCH)}: {web!r}")
    if hooks.WEB_TOOL in allowed_tools and web == "disabled":
        refuse(f"{owner}: the web tool needs web_search other than disabled")
    return {
        "model": validate_model(owner, entry["model"]),
        "hook_allowed_tools": allowed_tools,
        "effort": validate_effort(owner, entry["effort"]),
        "web_search": web,
        "timeout_minutes": _positive_int(owner, "timeout_minutes", entry["timeout_minutes"]),
    }


def read_config(path: Path | None) -> dict[str, Any]:
    path = CONFIG if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        refuse(f"codex config {path}: unreadable or not JSON: {exc}")
    if not isinstance(raw, dict):
        refuse(f"codex config {path}: expected an object")
    raw.pop("_comment", None)
    unknown = sorted(set(raw) - set(TOP_KEYS))
    if unknown or any(key not in raw for key in TOP_KEYS):
        refuse(
            f"codex config {path}: expected exactly the keys {', '.join(TOP_KEYS)} (found {sorted(raw)})"
        )
    return raw


def load_codex_config(path: Path | None = None) -> dict[str, Any]:
    """Validate codex_profiles.json fully; a placeholder, a missing field or a refused value is an error."""
    raw = read_config(path)
    roles = raw["roles"]
    if not isinstance(roles, dict) or not roles:
        refuse("codex config: 'roles' must be a non-empty object of role -> settings")
    return {
        "runtime": validate_runtime(raw["runtime"]),
        "roles": {str(r): validate_role(str(r), e) for r, e in roles.items()},
        "usage": validate_usage("codex usage", raw["usage"]),
    }


def load_usage_settings(path: Path | None = None) -> dict[str, Any]:
    """Only the `usage` block, for the ledger and the snapshot (they must work before the runtime is filled in)."""
    return validate_usage("codex usage", read_config(path)["usage"])


def load_home_setting(path: Path | None = None) -> str | None:
    runtime = read_config(path)["runtime"]
    if not isinstance(runtime, dict) or "codex_home" not in runtime:
        refuse("codex runtime: missing field 'codex_home'")
    return validate_codex_home("codex runtime", runtime["codex_home"])


def effective_home(configured: str | None, root: Path, env: Mapping[str, str]) -> Path:
    """The Codex home: the configured value, else CODEX_HOME of the environment, else ~/.codex."""
    text = configured or env.get("CODEX_HOME")
    if text:
        path = Path(os.path.expanduser(text))
        return path if path.is_absolute() else root / path
    return Path.home() / ".codex"


def protected_roots(root: Path, cwd: Path) -> list[Path]:
    """Checkouts whose `.secrets/` must stay unreadable: this root, the main checkout it is a worktree of, the cwd."""
    roots = [root, cwd]
    if root.parent.name == "worktrees" and root.parent.parent.name == ".tmp":
        roots.append(root.parents[2])
    unique: list[Path] = []
    for candidate in roots:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _write_relative(owner: str, value: object) -> str:
    text = _relative_dir(owner, "extra_write_paths", value)
    parts = re.split(r"[\\/]", text)
    # Ambiguous Windows aliases/streams must not bypass a denied path on either supported host.
    if any(":" in part or (part != "." and part.rstrip(" .") != part) for part in parts):
        refuse(f"{owner}, extra_write_paths: ambiguous path component: {text!r}")
    return str(Path(*parts))


def refuse_link_components(path: Path) -> None:
    """Inspect metadata only; never follow a private leaf or an unproved reparse point."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            refuse(f"cannot inspect path metadata {part}: {type(exc).__name__}")
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            refuse(f"linked path cannot authorize extra_write_paths: {part}")


def write_paths(
    root: Path, denied: Sequence[str], entries: Sequence[str], *, relative: bool
) -> list[str]:
    """Resolve grants without granting a denied path or an ancestor that would contain it.

    Check lexical overlap first so a direct private-path request is refused without resolving that path.
    Reject linked deny/grant components using metadata, without opening private leaves.
    """
    root = root.resolve()
    denies = [Path(os.path.abspath(path)) for path in denied]
    # Denies are admission requirements even when there are no extra grants.
    for path in denies:
        refuse_link_components(path)
    grants: list[str] = []
    for entry in entries:
        requested = root / _write_relative("codex runtime", entry) if relative else Path(entry)
        if not requested.is_absolute():
            refuse("stored extra write path must be absolute; retire and restart the worker")
        lexical = Path(os.path.abspath(requested))
        if any(lexical.is_relative_to(d) or d.is_relative_to(lexical) for d in denies):
            refuse(f"extra_write_paths overlaps a denied path: {entry!r}")
        refuse_link_components(lexical)
        try:
            resolved = requested.resolve()
        except (OSError, RuntimeError) as exc:
            refuse(f"extra_write_paths cannot resolve {entry!r}: {type(exc).__name__}")
        if not resolved.is_relative_to(root):
            refuse(f"extra_write_paths leaves the project root: {entry!r}")
        # A more-specific deny under an already denied directory is redundant; don't resolve inside it.
        try:
            canonical_denies = [
                path.parent.resolve() / path.name
                if path.name.casefold() in (".secrets", "auth.json")
                else path.resolve()
                for path in denies
                if not any(path != other and path.is_relative_to(other) for other in denies)
            ]
        except (OSError, RuntimeError) as exc:
            refuse(f"extra_write_paths cannot bind deny paths: {type(exc).__name__}")
        if any(resolved.is_relative_to(d) or d.is_relative_to(resolved) for d in canonical_denies):
            refuse(f"extra_write_paths overlaps a denied path after resolving links: {entry!r}")
        grants.append(str(resolved))
    return list(dict.fromkeys(grants))


def refuse_venv_writes(venv_dir: str | Path, areas: Sequence[str | Path], root: Path) -> None:
    """Protect both configured and canonical project environments from every writable area."""
    try:
        root = root.resolve()
        environments = {Path(venv_dir).resolve(), (root / ".venv").resolve()}
        if any(env == root or not env.is_relative_to(root) for env in environments):
            refuse("venv_dir must be strictly inside the project root after resolving links")
        for area in areas:
            writable = Path(area).resolve()
            if any(
                writable.is_relative_to(env) or env.is_relative_to(writable) for env in environments
            ):
                refuse(
                    f"writable workspace or extra_write_paths overlaps venv_dir: {area}; "
                    "use a separate worktree to keep the project venv read-only"
                )
    except (OSError, RuntimeError) as exc:
        refuse(f"cannot bind venv/write separation: {type(exc).__name__}")


def resolve_worker(
    cfg: dict[str, Any],
    role: str,
    root: Path,
    cwd: Path,
    name: str,
    config_path: Path | None,
    env: Mapping[str, str],
    overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """(`codex` registry dict, model, effort) of a new Codex worker: the validated role plus the runtime, resolved to
    absolute paths. Everything the worker needs later (`send`, `retire`, the secrets check) is stored, so a later
    edit of the config file never changes a running worker. `overrides` (model, effort, timeout_minutes) come from
    explicit start flags and are validated like the file."""
    if role not in cfg["roles"]:
        refuse(f"unknown codex role {role!r}; known: {', '.join(sorted(cfg['roles']))}")
    runtime, settings = cfg["runtime"], {**cfg["roles"][role], **(overrides or {})}
    given = overrides or {}
    if "effort" in given:
        settings["effort"] = validate_effort("--effort", given["effort"])
    if "model" in given:
        settings["model"] = validate_model("--model", given["model"])
    if "timeout_minutes" in given:
        settings["timeout_minutes"] = _positive_int(
            "--timeout-minutes", "timeout_minutes", given["timeout_minutes"]
        )
    home = effective_home(runtime["codex_home"], root, env)
    checkout = cwd.resolve()
    scratch_root = (cwd / runtime["scratch_dir"]).resolve()
    if scratch_root == checkout or not scratch_root.is_relative_to(checkout):
        refuse(
            f"runtime.scratch_dir {runtime['scratch_dir']!r} must name a directory strictly inside the "
            f"worker's checkout {cwd} (never the checkout itself or an ancestor of its files)"
        )
    # A sanitized name alone could collide (`a b` / `a_b`): the digest of the raw name keeps directories apart.
    slug = (
        re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40]
        + "-"
        + hashlib.sha256(name.encode()).hexdigest()[:10]
    )
    scratch = scratch_root / slug
    try:
        venv_dir = (root / Path(*re.split(r"[\\/]", runtime["venv_dir"]))).resolve()
    except (OSError, RuntimeError) as exc:
        refuse(f"cannot resolve venv_dir: {type(exc).__name__}")
    deny = [str(home / "auth.json")]
    for checkout in protected_roots(root, cwd):
        deny.append(str(checkout / ".secrets"))
    for extra in runtime["extra_deny_paths"]:
        path = Path(os.path.expanduser(extra))
        deny.append(str(path if path.is_absolute() else root / path))
    grants = write_paths(root, deny, runtime["extra_write_paths"], relative=True)
    refuse_venv_writes(venv_dir, [cwd, *grants], root)
    cx: dict[str, Any] = {
        "role": role,
        "config_path": str(CONFIG if config_path is None else config_path),
        "command": list(runtime["codex_command"]),
        "home": str(home),
        "export_home": runtime["codex_home"] is not None,
        "permission_profile": runtime["permission_profile"],
        "windows_sandbox": runtime["windows_sandbox"],
        "deny": list(dict.fromkeys(deny)),
        "write": grants,
        "project_root": str(root.resolve()),
        "web_search": settings["web_search"],
        "scratch": str(scratch),
        "scratch_root": str(scratch_root),
        "scratch_owner": name,
        "venv_dir": str(venv_dir),
        "check_timeout_seconds": runtime["check_timeout_seconds"],
        "env_allow": list(runtime["env_allow"]),
        "timeout_minutes": settings["timeout_minutes"],
        "disabled_features": runtime["disabled_features"],
        "tested_codex_version": runtime["tested_codex_version"],
        "hooks": runtime["hooks"],
        "hook_allowed_tools": settings["hook_allowed_tools"],
        "hook_id": uuid.uuid4().hex,
    }
    try:
        hook_module().image_destination(cx, "validation", "validation.png")
        cx["hook_table"] = hook_module().declaration(cx, cwd, venv_layout(venv_dir)[1])
    except (ValueError, OSError) as exc:
        refuse(f"Codex host hooks: {exc}")
    return (
        cx,
        settings["model"],
        settings["effort"],
    )


class StaleWorkerError(SystemExit):
    """The stored resolved configuration cannot be used by this version of the launcher."""


class ReaderUnavailableError(SystemExit):
    """A usable project interpreter is required for typed denial evidence."""


def stored_codex_problem(cx: object) -> str | None:
    """Inspect the current resolved-row contract without filling or rewriting any stored value."""
    if not isinstance(cx, dict):
        return "codex must be an object"
    if "python_dir" in cx:
        return "codex.python_dir is obsolete"
    if not hook_module().valid_table(cx.get("hook_table")):
        return "codex.hook_table is missing or incompatible"
    try:
        hooks = hook_module()
        hooks.options(cx.get("hooks"))
        hooks.names(
            cx.get("disabled_features"), hooks.DISABLED_FEATURES, "disabled_features", "apps"
        )
        hooks.names(cx.get("hook_allowed_tools"), hooks.TOOLS, "hook_allowed_tools")
        if hooks.IMAGE_TOOL in cx["hook_allowed_tools"] and cx.get("role") != "codex-image":
            return "image tool requires the codex-image role"
        if hooks.WEB_TOOL in cx["hook_allowed_tools"] and cx.get("web_search") == "disabled":
            return "the web tool needs web_search other than disabled"
        if re.fullmatch(r"[0-9a-f]{32}", cx.get("hook_id", "")) is None:
            return "invalid hook_id"
        if (
            not isinstance(cx.get("tested_codex_version"), str)
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", cx["tested_codex_version"]) is None
        ):
            return "invalid tested_codex_version"
    except (TypeError, ValueError):
        return "incompatible hook/feature settings"
    text_keys = (
        "role",
        "config_path",
        "home",
        "permission_profile",
        "web_search",
        "scratch",
        "scratch_root",
        "scratch_owner",
        "project_root",
        "venv_dir",
    )
    for key in text_keys:
        if not isinstance(cx.get(key), str) or not cx[key].strip():
            return f"codex.{key} must be nonempty text"
    if not Path(cx["venv_dir"]).is_absolute():
        return "codex.venv_dir must be absolute"
    for key in ("windows_sandbox",):
        if key not in cx or (
            cx[key] is not None and (not isinstance(cx[key], str) or not cx[key].strip())
        ):
            return f"codex.{key} must be null or nonempty text"
    for key in ("command", "deny", "env_allow", "write"):
        value = cx.get(key)
        if (
            not isinstance(value, list)
            or (key in ("command", "deny") and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            return f"codex.{key} must be a list of text"
    if type(cx.get("export_home")) is not bool:
        return "codex.export_home must be boolean"
    for key in ("check_timeout_seconds", "timeout_minutes"):
        if type(cx.get(key)) is not int or cx[key] <= 0:
            return f"codex.{key} must be a positive integer"
    return None


def require_stored_codex(cx: object) -> None:
    problem = stored_codex_problem(cx)
    if problem:
        raise StaleWorkerError(
            f"incompatible Codex registry row: {problem}; retire and restart the worker"
        )


def require_worker(worker: Mapping[str, Any]) -> None:
    require_stored_codex(worker.get("codex"))
    cx = worker["codex"]
    refuse_venv_writes(cx["venv_dir"], [worker["cwd"]], Path(cx["project_root"]))


def toml_string(text: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        refuse("a control character cannot go into a -c override")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def filesystem_table(cx: Mapping[str, Any]) -> str:
    """Inline TOML table of the permission profile: `.secrets` in every workspace root plus every absolute deny path.
    An inline table is required: a dotted key with ":workspace_roots" fails (PM message 2026-11-07)."""
    require_stored_codex(cx)
    grants = write_paths(Path(cx["project_root"]), cx["deny"], cx["write"], relative=False)
    refuse_venv_writes(cx["venv_dir"], grants, Path(cx["project_root"]))
    if grants != cx["write"]:
        raise StaleWorkerError(
            "stored write paths changed after resolving links; retire and restart the worker"
        )
    entries = [f'{toml_string(":workspace_roots")}={{{toml_string(".secrets")}="deny"}}']
    entries += [f'{toml_string(path)}="deny"' for path in cx["deny"]]
    entries += [f'{toml_string(path)}="write"' for path in grants]
    return "{" + ",".join(entries) + "}"


def permission_overrides(cx: Mapping[str, Any]) -> list[str]:
    """`-c` values that define the effective permission profile (never `--sandbox`: it replaces the profile)."""
    require_stored_codex(cx)
    name = cx["permission_profile"]
    items = [
        f"default_permissions={toml_string(name)}",
        f'permissions.{name}.extends=":workspace"',
        f"permissions.{name}.filesystem={filesystem_table(cx)}",
    ]
    if cx["windows_sandbox"]:
        items.append(f"windows.sandbox={toml_string(cx['windows_sandbox'])}")
    return items


def require_thread(worker: Mapping[str, Any]) -> None:
    """A resumable worker has the thread id its first turn reported; refused before the registry marks it busy."""
    thread = worker.get("thread_id")
    if not isinstance(thread, str) or not THREAD_ID.fullmatch(thread):
        refuse(
            "no Codex thread id was captured for this worker, so it cannot be resumed; retire it and start again"
        )


def check_prefix(prefix: Sequence[str]) -> None:
    """The executable part of an argv is exactly one path: every flag comes from this module's grammar."""
    if (
        len(prefix) != 1
        or not isinstance(prefix[0], str)
        or not prefix[0]
        or prefix[0].startswith("-")
    ):
        refuse(f"refused command prefix {list(prefix)!r}: exactly one executable path is allowed")


EXEC_FLAGS = ("-C", "-m", "-c", "-o")
RESUME_FLAGS = ("-m", "-c", "-o")  # a resumed thread keeps its directory: no -C
SANDBOX_FLAGS = ("-C", "-c", "-P")
OVERRIDE = re.compile(r"^(?P<key>[A-Za-z0-9_.-]+)=(?P<value>.*)$", re.DOTALL)
PERMISSION_KEY = re.compile(
    r"^permissions\.(?P<name>[A-Za-z][A-Za-z0-9_-]*)\.(?P<field>extends|filesystem)$"
)
QUOTED_WORD = re.compile(r'^"(?P<word>[A-Za-z][A-Za-z0-9_-]*)"$')


def _deny_only(value: object) -> bool:
    if isinstance(value, dict):
        return bool(value) and all(_deny_only(v) for v in value.values())
    return value == "deny"


def check_override(
    item: str, write_grants: Sequence[str] = (), hooks_table: str | None = None
) -> tuple[str, str]:
    """One `-c key=value` of the allowlist: the key must be known and the value must have its exact safe shape.
    Returns (key, value). Anything else is refused, so no equivalent spelling of a forbidden setting gets through."""
    match = OVERRIDE.match(item)
    if match is None:
        refuse(f"refused -c value {item!r}: expected key=value")
    key, value = match["key"], match["value"]
    word = QUOTED_WORD.match(value)
    permission = PERMISSION_KEY.match(key)
    ok = False
    if key == "service_tier":
        ok = item == SERVICE_TIER_PIN
    elif key == "features.fast_mode":
        ok = item == FAST_MODE_PIN
    elif key == "features.hooks":
        ok = value == "true"
    elif key.startswith("features.") and key[9:] in hook_module().DISABLED_FEATURES:
        ok = value == "false"
    elif key == "hooks":
        ok = hooks_table is not None and value == hooks_table
    elif key == "default_permissions":
        ok = word is not None
    elif key == "windows.sandbox":
        ok = word is not None and word["word"] in WINDOWS_SANDBOX
    elif key == "model_reasoning_effort":
        ok = word is not None and word["word"] not in REFUSED_EFFORTS
    elif key == "web_search":
        ok = word is not None and word["word"] in WEB_SEARCH
    elif permission is not None and permission["field"] == "extends":
        ok = value == '":workspace"'
    elif permission is not None:
        try:
            table = tomllib.loads("v = " + value)["v"]
        except tomllib.TOMLDecodeError:
            table = None
        ok = (
            isinstance(table, dict)
            and bool(table)
            and all(
                _deny_only(access) or (path in write_grants and access == "write")
                for path, access in table.items()
            )
        )
    if not ok:
        refuse(
            f"refused -c override {item[:120]!r}: only the host's permission profile, effort, web mode and the "
            "service-tier pins are allowed (no sandbox override, no fast tier, no effort ultra)"
        )
    return key, value


def check_argv(
    args: Sequence[str], mode: str, write_grants: Sequence[str] = (), hooks_table: str | None = None
) -> int:
    """Fail-closed grammar of the arguments after the executable. `mode` is `exec`, `resume` or `sandbox`.

    Only the flags below are allowed (`-s`, `--sandbox`, `--dangerously-*`, `--full-auto`, `--add-dir`, `--oss`,
    `-p`, attached forms such as `-sread-only` or `--config=...` are all unknown flags), every `-c` passes
    `check_override`, no key repeats, and the pins that switch the fast tier off must be present. For `sandbox` the
    index where the sandboxed command starts is returned."""
    head = {"exec": ["exec"], "resume": ["exec", "resume"], "sandbox": ["sandbox"]}[mode]
    if list(args[: len(head)]) != head:
        refuse(f"refused arguments: expected {' '.join(head)} first, got {list(args[:2])!r}")
    flags = {"sandbox": SANDBOX_FLAGS, "exec": EXEC_FLAGS, "resume": RESUME_FLAGS}[mode]
    keys: dict[str, str] = {}
    values: dict[str, str] = {}
    index = len(head)
    while index < len(args) and args[index].startswith("-") and args[index] != "-":
        flag = args[index]
        if flag == "--json" and mode != "sandbox":
            index += 1
            keys["--json"] = ""
            continue
        if flag not in flags or index + 1 >= len(args) or args[index + 1].startswith("-"):
            refuse(
                f"refused argument {flag!r}: sandbox overrides, --dangerously-*, the fast tier, effort ultra and "
                "every flag outside the launcher's grammar are not allowed"
            )
        value = args[index + 1]
        if flag == "-c":
            key, _ = check_override(value, write_grants, hooks_table)
            if key in keys:
                refuse(f"refused arguments: -c {key} is given twice")
            keys[key] = value
        else:
            if flag in values:
                refuse(f"refused arguments: {flag} is given twice")
            values[flag] = value
        index += 2
    rest = list(args[index:])
    if mode == "exec" and rest != ["-"]:
        refuse(f"refused arguments: expected `-` (the prompt on stdin) last, got {rest!r}")
    if mode == "resume" and not (
        len(rest) == 2 and THREAD_ID.fullmatch(rest[0]) and rest[1] == "-"
    ):
        refuse(f"refused arguments: expected `<thread id> -` last, got {rest!r}")
    if mode == "sandbox" and not rest:
        refuse("refused arguments: no command to run in the sandbox")
    profile = QUOTED_WORD.match(keys.get("default_permissions", "")[len("default_permissions=") :])
    needed = {"default_permissions"}
    if profile is not None:
        needed |= {
            f"permissions.{profile['word']}.extends",
            f"permissions.{profile['word']}.filesystem",
        }
    if mode != "sandbox":
        needed |= {
            "service_tier",
            "features.fast_mode",
            "features.apps",
            "features.hooks",
            "hooks",
            "model_reasoning_effort",
            "web_search",
            "--json",
        }
        if "-m" not in values or "-o" not in values:
            refuse("refused arguments: -m and -o are required")
    missing = sorted(needed - set(keys))
    if missing:
        refuse(f"refused arguments: missing required setting(s) {', '.join(missing)}")
    permission_names = {m["name"] for k in keys if (m := PERMISSION_KEY.match(k))}
    if profile is None or permission_names != {profile["word"]}:
        refuse("refused arguments: the permission profile keys do not match default_permissions")
    if mode == "sandbox" and values.get("-P") != profile["word"]:
        refuse("refused arguments: -P must name the job's permission profile")
    return index


def build_command(worker: Mapping[str, Any], resume: bool, result_file: Path) -> list[str]:
    """`codex exec` (or `exec resume <thread>`) for one turn; the prompt comes on stdin (`-`)."""
    require_worker(worker)
    cx = worker["codex"]
    hooks = hook_module()
    try:
        table = hooks.declaration(cx, Path(worker["cwd"]), venv_layout(Path(cx["venv_dir"]))[1])
    except (ValueError, OSError) as exc:
        refuse(f"Codex host hooks: {exc}")
    hook_value = hooks.toml(table)
    prefix = list(cx["command"])
    check_prefix(prefix)
    args = ["exec"]
    if resume:
        require_thread(worker)
        thread = worker["thread_id"]
        args += ["resume"]
    else:
        args += ["-C", str(worker["cwd"])]
    args += ["--json", "-m", worker["model"]]
    for item in [
        *permission_overrides(cx),
        f"model_reasoning_effort={toml_string(worker['effort'])}",
        f"web_search={toml_string(cx['web_search'])}",
        SERVICE_TIER_PIN,
        FAST_MODE_PIN,
        *(f"features.{feature}=false" for feature in cx["disabled_features"]),
        "features.hooks=true",
        "hooks=" + hook_value,
    ]:
        args += ["-c", item]
    args += ["-o", str(result_file)]
    args += [thread, "-"] if resume else ["-"]
    check_argv(args, "resume" if resume else "exec", cx["write"], hook_value)
    return [*prefix, *args]


def _marker_owner(marker: Path) -> str | None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    except (OSError, ValueError):
        return None
    return str(data.get("owner")) if isinstance(data, dict) else None


def scratch_problem(cx: Mapping[str, Any], cwd: Path) -> str | None:
    """Why the worker's scratch directory must not be used or deleted, or None when this launcher owns it.

    The directory is created and marked by `prepare_scratch` (a marker in the root and one in the worker directory
    naming the worker); an existing unmarked directory, a link, the checkout itself or anything outside it is
    never touched, so a misconfigured `scratch_dir` cannot make the host delete project files."""
    if any(
        not isinstance(cx.get(key), str) or not cx[key]
        for key in ("scratch", "scratch_root", "scratch_owner")
    ):
        return f"{cx.get('scratch')}: the registry entry predates scratch ownership markers; nothing is deleted"
    checkout = cwd.resolve()
    root, worker = Path(cx["scratch_root"]), Path(cx["scratch"])
    if root == checkout or not root.is_relative_to(checkout) or worker.parent != root:
        return f"{worker}: not strictly inside the checkout {cwd}"
    if root.resolve() != root or worker.resolve() != worker:
        return f"{worker}: the path contains a link"
    if not root.is_dir() or _marker_owner(root / SCRATCH_ROOT_MARKER) != "root":
        return f"{root}: exists but was not created by this launcher (no {SCRATCH_ROOT_MARKER})"
    if worker.exists() and (
        not worker.is_dir() or _marker_owner(worker / SCRATCH_WORKER_MARKER) != cx["scratch_owner"]
    ):
        return f"{worker}: exists but is not this worker's scratch directory (marker missing or other owner)"
    return None


def _marked_directory(path: Path, marker_name: str, owner: str, *, parents: bool = False) -> None:
    """Roll back only the directory just created here if writing its ownership marker fails."""
    path.mkdir(parents=parents)
    identity = path.stat()
    marker = path / marker_name
    try:
        marker.write_text(json.dumps({"owner": owner}), encoding="utf-8")
    except BaseException as exc:
        try:
            current = path.stat()
            if (
                path.resolve() != path
                or path.is_symlink()
                or os.path.isjunction(path)
                or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
            ):
                raise OSError("scratch directory identity changed; rollback deletes nothing")
            marker.unlink(missing_ok=True)
            path.rmdir()
        except OSError as cleanup:
            exc.add_note(f"scratch preparation rollback failed: {cleanup}")
        raise


def prepare_scratch(cx: Mapping[str, Any], cwd: Path) -> Path:
    """Create the root and the worker's scratch directory with their ownership markers (or validate existing ones)."""
    require_stored_codex(cx)
    checkout = cwd.resolve()
    root, worker = Path(cx["scratch_root"]), Path(cx["scratch"])
    if root == checkout or not root.is_relative_to(checkout) or worker.parent != root:
        refuse(f"scratch directory {worker} is not strictly inside the worker's checkout {cwd}")
    # A link is refused before anything is created or written through it.
    for existing in (root, worker):
        if (
            existing.is_symlink()
            or os.path.isjunction(existing)
            or (existing.exists() and existing.resolve() != existing)
        ):
            refuse(f"scratch directory {existing} is or contains a link; it is never used")
    if root.exists():
        if _marker_owner(root / SCRATCH_ROOT_MARKER) != "root":
            refuse(
                f"scratch_dir {root} already exists and was not created by this launcher "
                f"(no {SCRATCH_ROOT_MARKER}); name a new directory in runtime.scratch_dir"
            )
    else:
        _marked_directory(root, SCRATCH_ROOT_MARKER, "root", parents=True)
    if not worker.exists():
        _marked_directory(worker, SCRATCH_WORKER_MARKER, cx["scratch_owner"])
    problem = scratch_problem(cx, cwd)
    if problem:
        refuse(f"scratch directory refused: {problem}")
    return worker


def child_env(worker: Mapping[str, Any], base: Mapping[str, str], root: Path) -> dict[str, str]:
    """Environment of a Codex turn: an allowlist of the ambient one (no credential variable survives), CODEX_HOME
    only when configured (an ambient one is kept so the derived home matches), TEMP/TMP in the checkout, the project
    venv scripts first on PATH and the worker's own src on PYTHONPATH."""
    require_worker(worker)
    cx = worker["codex"]
    allowed = ENV_ALLOWLIST | {n.upper() for n in cx["env_allow"]}
    env = {k: v for k, v in base.items() if k.upper() in allowed}
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cx["export_home"]:
        env["CODEX_HOME"] = cx["home"]
    tmp = prepare_scratch(cx, Path(worker["cwd"])) / "tmp"
    tmp.mkdir(exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(tmp)
    scripts, _ = venv_layout(Path(cx["venv_dir"]))
    env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = (Path(worker["cwd"]) / "src").as_posix()
    return env


def venv_layout(venv: Path) -> tuple[Path, Path]:
    """Platform adapter for CPython venv artifact names; shared callers join no native suffixes."""
    scripts_name, executable = (
        ("Scripts", "python.exe") if READER_PLATFORM == "win32" else ("bin", "python")
    )
    scripts = venv / scripts_name
    return scripts, scripts / executable


def preflight(cx: Mapping[str, Any], root: Path) -> None:
    """Files the turn needs, refused before the registry marks the worker busy."""
    require_stored_codex(cx)
    reader_interpreter(cx, root)
    first = cx["command"][0]
    if not (Path(first).is_file() or shutil.which(first)):
        refuse(f"codex executable not found: {first!r} (runtime.codex_command)")


def reader_interpreter(cx: Mapping[str, Any], root: Path) -> Path:
    """Use the configured project venv on either platform, never an ambient PATH interpreter."""
    require_stored_codex(cx)
    refuse_venv_writes(cx["venv_dir"], [], root)
    _, interpreter = venv_layout(Path(cx["venv_dir"]))
    if not interpreter.is_file():
        raise ReaderUnavailableError(
            f"typed reader interpreter missing: {interpreter}; create the configured project venv. "
            + VENV_READ_GRANT_HELP
        )
    return interpreter


def reader_argv(interpreter: Path, script: Path, path: Path, echo: bool = False) -> list[str]:
    return [str(interpreter), "-I", "-S", str(script), str(path), *(["echo"] if echo else [])]


def startup_check(
    worker: Mapping[str, Any],
    root: Path,
    env: Mapping[str, str],
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Fail closed: the effective profile must deny `.secrets` before a job starts.

    Through `codex sandbox` with the exact permission overrides of the job: (1) a control read of a project file
    must succeed (the sandbox and the reader work), (2) a read of each existing tracked `.secrets/README.md` must
    end with a typed denial code and empty stdout while the host sees the file. The reader uses the configured
    project venv interpreter on either platform. Its exit code must name the exception
    (permission denied, or not found for a file the host sees). Error text is never matched. Anything else
    refuses. No other path under `.secrets/` and never auth.json is read; the auth.json deny is part of the same
    profile (`cx['deny']`) and cannot be probed without reading it.
    """
    require_worker(worker)
    cx = worker["codex"]
    interpreter = reader_interpreter(cx, root)
    cwd = Path(worker["cwd"])
    home_auth = str(Path(cx["home"]) / "auth.json")
    secrets = [str(c / ".secrets") for c in protected_roots(root, cwd)]
    missing = [p for p in [home_auth, *secrets] if p not in cx["deny"]]
    if missing:
        refuse(f"codex startup check: the profile does not deny {', '.join(missing)}")
    targets = [c / ".secrets" / "README.md" for c in protected_roots(root, cwd)]
    targets = [t for t in targets if t.is_file()]
    if not targets:
        refuse(
            "codex startup check: no tracked .secrets/README.md to probe; the check cannot prove the deny"
        )
    check_prefix(cx["command"])
    check_dir = prepare_scratch(cx, cwd) / "check"
    check_dir.mkdir(exist_ok=True)
    control = check_dir / "control.txt"
    control.write_text(CONTROL_TOKEN, encoding="utf-8")
    reader_script = check_dir / "reader.py"
    reader_script.write_text(READER_SOURCE, encoding="utf-8")
    prefix = [*cx["command"], "sandbox", "-C", str(cwd)]
    for item in permission_overrides(cx):
        prefix += ["-c", item]
    prefix += ["-P", cx["permission_profile"]]

    def read(path: Path, echo: bool = False) -> tuple[int, str, str]:
        argv = [*prefix, *reader_argv(interpreter, reader_script, path, echo)]
        check_argv(argv[len(cx["command"]) :], "sandbox", cx["write"])
        try:
            done = runner(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=cx["check_timeout_seconds"],
                env=dict(env),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            refuse(
                f"codex startup check could not run `codex sandbox`: {type(exc).__name__}: {exc}. "
                + VENV_READ_GRANT_HELP
            )
        return int(done.returncode), str(done.stdout or ""), str(done.stderr or "")

    code, out, err = read(control, echo=True)
    if code != 0 or CONTROL_TOKEN not in out:
        refuse(
            f"codex startup check inconclusive: the control read failed (rc={code}, stderr {err.strip()[:120]!r}); "
            "the job is not started. " + VENV_READ_GRANT_HELP
        )
    for target in targets:
        label = f"{target.parent.name}/{target.name}"
        code, out, err = read(target)
        if code == 0 or out.strip():
            refuse(
                f"codex startup check FAILED: {label} was readable in the sandbox (rc={code}); "
                "the profile does not deny .secrets, the job is not started"
            )
        # Evidence, never message text (it varies with locale and shell): the host saw the file exist, the control
        # read through the identical reader worked, and the read failed with empty stdout. The typed reader's
        # exit code must also name the exception class (permission denied, or not found while the
        # host still sees the file: hidden by the deny); any other code is a reader or sandbox failure.
        conclusive = code in (READER_DENIED, READER_MISSING)
        if not conclusive or not target.is_file():
            refuse(
                f"codex startup check inconclusive: the read of {label} ended with rc={code} "
                f"(stderr {err.strip()[:120]!r}), which does not prove a permission denial; the job is not started"
            )


def parse_log(text: str) -> dict[str, Any]:
    """Thread id, last agent message, last cumulative usage and failures from a `codex exec --json` log.

    A turn succeeded only when `turn.completed` was seen and no `turn.failed` or `error` event came after it
    (`fatal`); earlier `error` events are retries (`failures`). Result text is never searched for words."""
    found: dict[str, Any] = {
        "thread_id": None,
        "result": None,
        "usage": None,
        "completed": False,
        "failures": [],
        "fatal": [],
    }
    for line in text.split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            found["thread_id"] = found["thread_id"] or event["thread_id"]
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                found["result"] = item["text"]
        elif kind == "turn.completed":
            found["completed"] = True
            found["fatal"] = []
            if isinstance(event.get("usage"), dict):
                found["usage"] = usage_fields(event["usage"])
        elif kind in ("turn.failed", "error"):
            error = event.get("error")
            message = str(
                error.get("message") if isinstance(error, dict) else event.get("message")
            )[:300]
            found["failures"].append(message)
            found["fatal"].append(message)
    return found


def usage_fields(usage: Mapping[str, Any]) -> dict[str, int | None]:
    """The four counts of a usage object; absent or malformed counts are None (never invented as 0)."""
    return {
        key: value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
        for key in USAGE_FIELDS
        for value in [usage.get(key)]
    }


def usage_delta(
    current: Mapping[str, int | None] | None, previous: Mapping[str, int | None] | None
) -> tuple[dict[str, int | None] | None, str]:
    """Usage of one turn from the cumulative counts of `turn.completed` (cumulative on resume): current - previous.
    A count that went down means the counts were not cumulative: the reported counts stand and the basis says so."""
    if current is None:
        return None, "none"
    if previous is None:
        return dict(current), "first-turn"
    delta: dict[str, int | None] = {}
    for key in USAGE_FIELDS:
        now_value, before = current.get(key), previous.get(key)
        if now_value is None:
            delta[key] = None
        elif before is None:
            delta[key] = now_value
        elif now_value < before:
            return dict(current), "reported-not-cumulative"
        else:
            delta[key] = now_value - before
    return delta, "delta"


def attribute_usage(
    current: Mapping[str, int | None] | None,
    previous: Mapping[str, int | None] | None,
    unreported_turns: int,
) -> tuple[dict[str, int | None] | None, str, int]:
    """`usage_delta` plus the gap rule: turns that ended without a usage object (a failed turn) left their tokens in the
    next cumulative total, so a row that follows `unreported_turns` such turns holds all of them and is labelled
    `aggregate` instead of `delta`/`first-turn`; the third value is the number of turns folded into it."""
    delta, basis = usage_delta(current, previous)
    if delta is None or unreported_turns <= 0 or basis == "reported-not-cumulative":
        return delta, basis, 0
    return delta, "aggregate", unreported_turns


def session_files(home: Path, thread_id: str) -> list[Path]:
    """The rollout file(s) `sessions/**/rollout-<timestamp>-<thread id>.jsonl` of exactly this thread id."""
    if not THREAD_ID.fullmatch(thread_id):
        return []
    sessions = home / "sessions"
    if not sessions.is_dir() or sessions.is_symlink() or os.path.isjunction(sessions):
        return []
    found: list[Path] = []
    for folder, dirs, files in os.walk(sessions, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if not (Path(folder, d).is_symlink() or os.path.isjunction(Path(folder, d)))
        ]
        for file in files:
            match = ROLLOUT.fullmatch(file)
            path = Path(folder, file)
            if match and match["id"] == thread_id and path.is_file() and not path.is_symlink():
                found.append(path)
    return sorted(found)


def purge_session(home: Path, thread_id: str, failed: list[str] | None = None) -> list[Path]:
    """Delete only the exact session file(s) of this thread id (no directory, no other session, no app data)."""
    failed = failed if failed is not None else []
    try:
        canonical = str(uuid.UUID(thread_id))
    except (ValueError, TypeError, AttributeError):
        canonical = ""
    if not thread_id or canonical != thread_id:
        refuse(f"thread id {thread_id!r} is not a canonical UUID; nothing deleted")
    removed: list[Path] = []
    for path in session_files(home, thread_id):
        try:
            path.unlink()
        except OSError as exc:
            failed.append(f"{path}: {exc.__class__.__name__}: {exc}")
        else:
            removed.append(path)
    return removed
