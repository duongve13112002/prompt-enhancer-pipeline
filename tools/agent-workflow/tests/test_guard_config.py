"""guard_config.py: every field is required, unknown keys and invalid values are refused, `--config` stays in the root.

Non-default overrides of each guard are tested where the value takes effect (test_tool_guard.py, test_tool_monitor.py,
test_worker_progress.py); this file tests the refusals.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest

DEFAULT = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))


def leaves(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out = []
    for key, value in data.items():
        out.append((*prefix, key))
        if isinstance(value, dict) and key not in ("dependent_tools", "code_flags"):
            out += leaves(value, (*prefix, key))
    return out


def edited(path: tuple[str, ...], value: Any = None, delete: bool = False) -> dict[str, Any]:
    data = copy.deepcopy(DEFAULT)
    node = data
    for key in path[:-1]:
        node = node[key]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


def test_committed_config_is_valid() -> None:
    assert gc.load() == gc.validate(DEFAULT)


@pytest.mark.parametrize("path", leaves(DEFAULT), ids=lambda p: ".".join(p))
def test_every_field_is_required(path: tuple[str, ...]) -> None:
    with pytest.raises(gc.ConfigError, match=f"missing required keys: {path[-1]}"):
        gc.validate(edited(path, delete=True))


@pytest.mark.parametrize(
    "section", [(), *[(k,) for k, v in DEFAULT.items() if isinstance(v, dict)]], ids=str
)
def test_unknown_keys_are_refused(section: tuple[str, ...]) -> None:
    with pytest.raises(gc.ConfigError, match="unknown keys: surprise"):
        gc.validate(edited((*section, "surprise"), 1))


INVALID = [
    (("schema_version",), 2),
    (("schema_version",), True),
    (("max_command_chars",), 0),
    (("max_command_chars",), "100"),
    (("git_timeout_seconds",), -1),
    (("git_timeout_seconds",), True),
    (("tool_broken", "mode"), "strict"),
    (("tool_broken", "streak_length"), 0),
    (("tool_broken", "harness_error_patterns"), []),
    (("tool_broken", "harness_error_patterns"), ["("]),
    (("tool_broken", "harness_error_patterns"), ["a", "a"]),
    (("tool_broken", "ordinary_exit_pattern"), "["),
    (("tool_broken", "reset_dir"), "../outside"),
    (("tool_broken", "reset_dir"), "C:/state"),
    (("tool_broken", "reset_dir"), "/abs"),
    (("tool_broken", "reset_dir"), ".tmp\\state"),
    (("tool_broken", "reset_dir"), ".tmp//state"),
    (("tool_broken", "dependent_tools"), {"Monitor": "../Bash"}),
    (("tool_broken", "dependent_tools"), []),
    (("tool_broken", "probe_args"), "-c 'echo ok'"),
    (("tool_broken", "probe_args"), []),
    (("tool_broken", "probe_shell"), "bash"),
    (("tool_broken", "probe_shell", "windows", "env"), "GIT BASH"),
    (("tool_broken", "probe_shell", "windows", "env"), ""),
    # A PATH lookup finds WSL's launcher (F11).
    (("tool_broken", "probe_shell", "windows", "paths"), ["bash"]),
    (("tool_broken", "probe_shell", "windows", "paths"), ["C:\\Program Files\\Git\\bin\\bash.exe"]),
    (("tool_broken", "probe_shell", "windows", "paths"), ["/bin/bash"]),
    (("tool_broken", "probe_shell", "windows", "paths"), []),
    (("tool_broken", "probe_shell", "posix", "paths"), ["bin/bash"]),
    (("tool_broken", "probe_shell", "posix", "paths"), ["/usr/../tmp/bash"]),
    (("tool_broken", "probe_shell", "posix", "env"), 7),
    (("tool_broken", "probe_expect"), ""),
    (("tool_broken", "probe_timeout_seconds"), 1.5),
    (("tool_broken", "max_path_chars"), None),
    (("write_guard", "mode"), "BLOCK"),
    (("write_guard", "allowed_dirs"), []),
    (("write_guard", "allowed_dirs"), [".."]),
    (("write_guard", "allowed_dirs"), ["."]),
    (("write_guard", "allowed_dirs"), [".tmp", ".tmp"]),
    (("git_guard", "mode"), None),
    (("git_guard", "readonly_subcommands"), []),
    (("git_guard", "readonly_subcommands"), ["status", ""]),
    (("git_guard", "merge_subcommands"), "merge"),
    (("git_guard", "allowed_config_keys"), [1]),
    (("git_guard", "worktrees_dir"), "../worktrees"),
    (("inline_interp", "mode"), "on"),
    (("inline_interp", "max_inline_chars"), 0),
    (("inline_interp", "code_flags"), {}),
    (("inline_interp", "code_flags"), {"python": ["c"]}),
    (("inline_interp", "code_flags"), {"python": []}),
    (("pytest_slot", "mode"), 1),
    (("pytest_slot", "launchers"), []),
    (("pytest_slot", "exempt_flags"), ["--co", "--co"]),
    (("read_router", "mode"), "loud"),
    (("ps_error", "mode"), "block"),  # warn-only guard (P-07): never blocks
    (("ps_error", "patterns"), ["(unclosed"]),
    (("eol", "mode"), "block"),  # warn-only guard (P-04)
    (("eol", "check_after_shell"), "yes"),
    (("worker_progress", "heading"), "# AUTO PROGRESS"),
    (("worker_progress", "heading"), "## "),
    (("worker_progress", "heading"), "## A\n## B"),
    (("worker_progress", "max_files"), 0),
    (("worker_progress", "max_excerpt_chars"), -5),
    (("worker_progress", "git_status_lines"), False),
    (("worker_progress", "transcript_tail_bytes"), 0),
]


@pytest.mark.parametrize(
    ("path", "value"), INVALID, ids=[f"{'.'.join(p)}={v!r}" for p, v in INVALID]
)
def test_invalid_values_are_refused(path: tuple[str, ...], value: Any) -> None:
    with pytest.raises(gc.ConfigError, match=".".join(path).replace(".", r"\.")):
        gc.validate(edited(path, value))


def test_non_default_values_are_accepted() -> None:
    data = edited(("write_guard", "allowed_dirs"), ["runs", ".tmp/agents"])
    data["tool_broken"]["streak_length"] = 5
    data["eol"]["mode"] = "off"
    data["worker_progress"]["heading"] = "## Progress (auto)"
    data["tool_broken"]["probe_shell"]["windows"] = {
        "env": "MY_BASH",
        "paths": ["D:/PortableGit/bin/bash.exe", "C:/Program Files/Git/bin/bash.exe"],
    }
    cfg = gc.validate(data)
    assert cfg["tool_broken"]["probe_shell"]["windows"]["paths"][0] == "D:/PortableGit/bin/bash.exe"
    assert cfg["write_guard"]["allowed_dirs"] == ["runs", ".tmp/agents"]
    assert cfg["tool_broken"]["streak_length"] == 5


def test_load_refuses_bad_files(tmp_path: Path) -> None:
    with pytest.raises(gc.ConfigError, match="cannot read"):
        gc.load(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(gc.ConfigError, match="not valid JSON"):
        gc.load(broken)
    listing = tmp_path / "list.json"
    listing.write_text("[]", encoding="utf-8")
    with pytest.raises(gc.ConfigError, match="must be an object"):
        gc.load(listing)
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(DEFAULT).encode())
    assert gc.load(bom)["schema_version"] == 1


def test_config_path_must_stay_in_the_root() -> None:
    assert gc.resolve_path(None) == gc.DEFAULT_PATH
    assert gc.resolve_path("tools/agent-workflow/guard_config.json") == gc.DEFAULT_PATH.resolve()
    with pytest.raises(gc.ConfigError, match="outside the project root"):
        gc.resolve_path(str(gc.ROOT.parent / "x.json"))
    with pytest.raises(gc.ConfigError, match="outside the project root"):
        gc.resolve_path("../outside.json")
    with pytest.raises(gc.ConfigError, match="outside the project root"):
        gc.resolve_path(os.path.join("tools", "..", "..", "x.json"))
