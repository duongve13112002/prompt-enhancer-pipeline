"""Validated loader for `memory_hooks.json`, the settings of the session-memory hooks (decision 353).

Every field is required; there is no source default. A missing or invalid field does not fail the whole file: `get()`
raises `NotConfigured` for that field only, so a consumer switches off the block that needs it and prints
`not configured: <field> (<reason>)`. Consumers outside the package (hooks run with `python -B -I`, which keeps the script
directory off `sys.path`) load this module by file path. Standard library only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "memory_hooks.json"
# Protocol-bound: Claude Code caps a hook's `additionalContext` and plain stdout at 10,000 characters and does not show
# the excess to the model (https://code.claude.com/docs/en/hooks, SessionStart section).
HARD_INJECT_CAP = 10000
# Protocol-bound: events share the PostToolUse `additionalContext` (10,000 characters) with the nudge and loop lines, so
# the PM event queue may hold at most 9,000 of them. hooks/ctx_nudge.py PM_EVENTS_MAX_CHARS must equal this value.
HARD_EVENTS_CAP = 9000
BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class NotConfigured(Exception):  # noqa: N818 - a state name, not an error type; consumers catch Exception
    """A required settings field is missing or invalid."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"not configured: {field} ({reason})")
        self.field = field
        self.reason = reason


def _int(minimum: int, maximum: int | None = None) -> Callable[[object], int]:
    def check(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer")
        if value < minimum:
            raise ValueError(f"must be at least {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"must be at most {maximum}")
        return value

    return check


def _branch(value: object) -> str:
    if (
        not isinstance(value, str)
        or not BRANCH_NAME.fullmatch(value)
        or ".." in value
        or value.endswith((".lock", "/", "."))
    ):
        raise ValueError("must be a plain Git branch name")
    return value


def _text_list(minimum_items: int, prefix: str = "") -> Callable[[object], tuple[str, ...]]:
    def check(value: object) -> tuple[str, ...]:
        if (
            not isinstance(value, list)
            or len(value) < minimum_items
            or not all(isinstance(item, str) and item.strip() for item in value)
            or not all(item.startswith(prefix) for item in value)
        ):
            raise ValueError(
                f"must be a list of at least {minimum_items} non-empty strings"
                + (f" starting with {prefix!r}" if prefix else "")
            )
        return tuple(value)

    return check


SPEC: dict[str, Callable[[object], object]] = {
    "batch_branch": _branch,
    "git_timeout_seconds": _int(1),
    "max_branch_lines": _int(1),
    "max_worktree_lines": _int(1),
    "total_inject_chars": _int(1, HARD_INJECT_CAP),
    "note_inject_chars": _int(1),
    "decisions_default": _int(0),
    "decisions_max": _int(1),
    "decision_text_chars": _int(20),
    "inject_headings": _text_list(1, "## "),
    "inflight_snapshot_chars": _int(1),
    "precompact_git_timeout_seconds": _int(1),
    "precompact_git_status_lines": _int(1),
    "settled_max_chars": _int(1),
    "settled_head_chars": _int(1),
    "settled_line_chars": _int(20),
    "settled_keywords_per_row": _int(1),
    "settled_stopwords": _text_list(0),
    "applied_from_decision": _int(1),
    "pending_rows_max": _int(1),
    "inbox_max_chars": _int(1, HARD_EVENTS_CAP),
}


class Settings:
    def __init__(
        self,
        values: dict[str, object],
        problems: dict[str, str],
        unknown: tuple[str, ...],
        file_problem: str | None,
    ) -> None:
        self.values = values
        self.problems = problems
        self.unknown = unknown
        self.file_problem = file_problem

    def get(self, field: str) -> object:
        if field not in SPEC:
            raise KeyError(field)  # a programming error, not a configuration problem
        if field in self.values:
            return self.values[field]
        raise NotConfigured(field, self.file_problem or self.problems.get(field, "missing"))

    def int_(self, field: str) -> int:
        value = self.get(field)
        assert isinstance(value, int)
        return value

    def str_(self, field: str) -> str:
        value = self.get(field)
        assert isinstance(value, str)
        return value

    def tuple_(self, field: str) -> tuple[str, ...]:
        value = self.get(field)
        assert isinstance(value, tuple)
        return value

    def notes(self) -> list[str]:
        """File-level problems worth one visible line each (unreadable file, unknown fields)."""
        out = []
        if self.file_problem:
            out.append(f"memory_hooks.json unusable: {self.file_problem}")
        if self.unknown:
            out.append("memory_hooks.json has unknown field(s): " + ", ".join(self.unknown))
        return out


def load(path: Path = CONFIG_PATH) -> Settings:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return Settings({}, {}, (), f"{type(exc).__name__} reading {path.name}")
    if not isinstance(data, dict):
        return Settings({}, {}, (), f"{path.name} is not a JSON object")
    values: dict[str, object] = {}
    problems: dict[str, str] = {}
    for field, check in SPEC.items():
        if field not in data:
            problems[field] = "missing"
            continue
        try:
            values[field] = check(data[field])
        except ValueError as exc:
            problems[field] = str(exc)
    unknown = tuple(sorted(k for k in data if k not in SPEC and not k.startswith("_")))
    return Settings(values, problems, unknown, None)
