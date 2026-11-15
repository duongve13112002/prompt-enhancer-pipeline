"""Load and validate tools/agent-workflow/guard_config.json, the configuration of the guard hooks.

Every field is required and unknown keys are refused: a value missing from the file is an error, never a source
default. Standard library only (the hooks run with `python -B -I`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "tools" / "agent-workflow" / "guard_config.json"
SCHEMA_VERSION = 1  # protocol-bound: the layout this module reads
MODES = ("block", "warn", "off")
WARN_MODES = ("warn", "off")
# Hook names Claude Code passes in `tool_name` (protocol-bound).
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """The guard configuration is missing or invalid; the message names the file and the field."""


Check = Callable[[Any, str], Any]


def _mode(choices: tuple[str, ...]) -> Check:
    def check(value: Any, where: str) -> str:
        if value not in choices:
            raise ConfigError(f"{where} must be one of {', '.join(choices)}, got {value!r}")
        return str(value)

    return check


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a positive integer, got {value!r}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string, got {value!r}")
    return value


def _ref_word(value: Any, where: str) -> str:
    """A remote or branch name used as one plain `git push <remote> <branch>` word: no option, refspec or range
    syntax (`-`, `+`, `:`, `..`, `^`, `~`, `@{`, `*`, whitespace), so it can only name, never force or delete."""
    _text(value, where)
    if (
        value.startswith("-")
        or any(ch.isspace() or ch in "+:^~*?[\\$`'\"" for ch in value)
        or ".." in value
        or "@{" in value
    ):
        raise ConfigError(f"{where} must be a plain remote or branch name, got {value!r}")
    return value


def _regex(value: Any, where: str) -> str:
    _text(value, where)
    try:
        re.compile(value)
    except re.error as exc:
        raise ConfigError(f"{where} is not a valid regular expression: {exc}") from None
    return str(value)


def _group_regex(value: Any, where: str) -> str:
    """A regular expression with exactly one capture group (the id the hook extracts)."""
    _regex(value, where)
    if re.compile(value).groups != 1:
        raise ConfigError(f"{where} must have exactly one capture group")
    return str(value)


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false, got {value!r}")
    return value


def _relative_dir(value: Any, where: str) -> str:
    """A project-relative directory: forward slashes, no `..`, no absolute or drive form."""
    _text(value, where)
    parts = value.split("/")
    if (
        "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ConfigError(
            f"{where} must be a relative directory with forward slashes, no `.`/`..`: {value!r}"
        )
    return str(value)


def _suffix(value: Any, where: str) -> str:
    """A file-name suffix such as `.gguf`: a dot, then name characters only (no separator, no second dot)."""
    _text(value, where)
    if not re.fullmatch(r"\.[A-Za-z0-9_-]+", value):
        raise ConfigError(f"{where} must be a file suffix like '.gguf', got {value!r}")
    return value


def _list(item: Check, *, unique: bool = True) -> Check:
    def check(value: Any, where: str) -> list[Any]:
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{where} must be a non-empty list, got {value!r}")
        out = [item(v, f"{where}[{i}]") for i, v in enumerate(value)]
        if unique and len(set(map(str, out))) != len(out):
            raise ConfigError(f"{where} has duplicate entries")
        return out

    return check


def _command(value: Any, where: str) -> list[str]:
    out: list[str] = _list(_text, unique=False)(value, where)
    return out


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ENV_NAME.match(value):
        raise ConfigError(f"{where} must be an environment variable name, got {value!r}")
    return value


def _abs_path(pattern: str, form: str) -> Check:
    def check(value: Any, where: str) -> str:
        _text(value, where)
        if not re.match(pattern, value) or "\\" in value or "/../" in f"{value}/":
            raise ConfigError(
                f"{where} must be {form} with forward slashes and no `..`, got {value!r}"
            )
        return str(value)

    return check


def _shell_spec(pattern: str, form: str) -> Check:
    return _section({"env": _env_name, "paths": _list(_abs_path(pattern, form))})


def _tool_map(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object, got {value!r}")
    for k, v in value.items():
        if not TOOL_NAME.match(str(k)) or not isinstance(v, str) or not TOOL_NAME.match(v):
            raise ConfigError(f"{where} maps tool names to tool names, got {k!r}: {v!r}")
    return dict(value)


def _flag_map(value: Any, where: str) -> dict[str, list[str]]:
    if not isinstance(value, dict) or not value:
        raise ConfigError(f"{where} must be a non-empty object, got {value!r}")
    out: dict[str, list[str]] = {}
    for k, v in value.items():
        flags = _list(_text)(v, f"{where}.{k}")
        if any(not f.startswith("-") for f in flags):
            raise ConfigError(f"{where}.{k} entries must be options starting with '-'")
        out[_text(k, f"{where} key")] = flags
    return out


def _section(spec: dict[str, Check]) -> Check:
    def check(value: Any, where: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ConfigError(f"{where} must be an object, got {value!r}")
        unknown = sorted(set(value) - set(spec))
        if unknown:
            raise ConfigError(f"{where} has unknown keys: {', '.join(unknown)}")
        missing = sorted(set(spec) - set(value))
        if missing:
            raise ConfigError(f"{where} is missing required keys: {', '.join(missing)}")
        return {k: spec[k](value[k], f"{where}.{k}") for k in spec}

    return check


def _heading(value: Any, where: str) -> str:
    """A level-2 Markdown heading on one line: the progress section runs to the next `## ` heading."""
    _text(value, where)
    if not value.startswith("## ") or len(value.splitlines()) != 1 or not value[3:].strip():
        raise ConfigError(f"{where} must be one line starting with '## ', got {value!r}")
    return str(value)


def _version(value: Any, where: str) -> int:
    if value != SCHEMA_VERSION or isinstance(value, bool):
        raise ConfigError(f"{where} must be {SCHEMA_VERSION}, got {value!r}")
    return SCHEMA_VERSION


SCHEMA = _section(
    {
        "schema_version": _version,
        "max_command_chars": _positive_int,
        "git_timeout_seconds": _positive_int,
        "tool_broken": _section(
            {
                "mode": _mode(MODES),
                "streak_length": _positive_int,
                "harness_error_patterns": _list(_regex),
                "ordinary_exit_pattern": _regex,
                "reset_dir": _relative_dir,
                "dependent_tools": _tool_map,
                "probe_shell": _section(
                    {
                        "windows": _shell_spec(r"^[A-Za-z]:/", "an absolute drive path"),
                        "posix": _shell_spec(r"^/", "an absolute path"),
                    }
                ),
                "probe_args": _command,
                "probe_expect": _text,
                "probe_timeout_seconds": _positive_int,
                "max_path_chars": _positive_int,
            }
        ),
        "write_guard": _section({"mode": _mode(MODES), "allowed_dirs": _list(_relative_dir)}),
        "git_guard": _section(
            {
                "mode": _mode(MODES),
                "readonly_subcommands": _list(_text),
                "allowed_config_keys": _list(_text),
                "worktrees_dir": _relative_dir,
                # The one push form of the orchestrator: `git push <push_remote> <push_branch>` (T-024 round 2).
                "push_remote": _ref_word,
                "push_branch": _ref_word,
                # Where the orchestrator's `git diff --cached --binary --output=<file>` may save a worker's patch
                # (T-024 round 3; ORCHESTRATION section 2 "Accepted patches").
                "patch_dir": _relative_dir,
            }
        ),
        "inline_interp": _section(
            {
                "mode": _mode(MODES),
                "max_inline_chars": _positive_int,
                "code_flags": _flag_map,
            }
        ),
        "pytest_slot": _section(
            {
                "mode": _mode(MODES),
                "launchers": _list(_text),
                "exempt_flags": _list(_text),
            }
        ),
        "read_router": _section({"mode": _mode(MODES)}),
        "push_guard": _section(
            {
                "mode": _mode(MODES),
                "marker_dir": _relative_dir,
                "check_command": _text,
                "check_timeout_seconds": _positive_int,
                "forbidden_path_prefixes": _list(_relative_dir),
                "forbidden_suffixes": _list(_suffix),
            }
        ),
        "question_guard": _section(
            {
                "mode": _mode(MODES),
                "collection": _text,
                "verified_field": _text,
                "checked_against_field": _text,
                "decision_pattern": _group_regex,
                "rule_pattern": _group_regex,
                "decision_files": _list(_relative_dir),
                "decision_row_pattern": _group_regex,
                "rules_file": _relative_dir,
                "rule_row_pattern": _group_regex,
                "answered_dir": _relative_dir,
                "status_field": _text,
                "answered_status": _text,
                "reopens_field": _text,
                "reopen_evidence_pattern": _regex,
                "min_option_chars": _positive_int,
            }
        ),
        "ps_error": _section({"mode": _mode(WARN_MODES), "patterns": _list(_regex)}),
        "eol": _section({"mode": _mode(WARN_MODES), "check_after_shell": _bool}),
        "worker_progress": _section(
            {
                "heading": _heading,
                "max_files": _positive_int,
                "max_excerpt_chars": _positive_int,
                "git_status_lines": _positive_int,
                "transcript_tail_bytes": _positive_int,
            }
        ),
    }
)


def resolve_path(text: str | None) -> Path:
    """The config path from `--config` (relative to the project root), contained in the root."""
    if not text:
        return DEFAULT_PATH
    path = Path(text)
    path = (path if path.is_absolute() else ROOT / path).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        raise ConfigError(f"--config {text!r} is outside the project root {ROOT}") from None
    return path


def validate(data: Any, where: str = "guard_config") -> dict[str, Any]:
    cfg: dict[str, Any] = SCHEMA(data, where)
    return cfg


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or DEFAULT_PATH
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    return validate(data, path.name)
