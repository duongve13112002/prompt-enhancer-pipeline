"""Tests for project_config.py: the repository's own binding loads, and every malformed form is refused by name."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_config as pc  # noqa: E402

REAL = json.loads((pc.ROOT / pc.REL_PATH).read_text(encoding="utf-8"))


def test_the_checked_in_binding_loads() -> None:
    cfg = pc.load()
    assert cfg.root == pc.ROOT
    assert cfg.name and cfg.integration_branch
    assert set(cfg.agents) == set(pc.AGENT_KEYS)


def test_a_non_default_value_is_used(tmp_path: Path) -> None:
    data = copy.deepcopy(REAL)
    data["git"]["integration_branch"] = "batch/2026-11"
    data["python"].update(package="pkg", runtime_config_module="pkg.config")
    cfg = pc.parse(data, tmp_path)
    assert cfg.integration_branch == "batch/2026-11"
    assert cfg.runtime_config_module == "pkg.config"


def _mutated(path: list[str], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(REAL)
    node = data
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (["schema_version"], 2, "schema_version"),
        (["git", "integration_branch"], KeyError, "missing"),
        (["git", "surprise"], "x", "unknown"),
        (["python", "venv_dir"], "/abs/.venv", "relative"),
        (["python", "src_dir"], "../other/src", "relative"),
        (["python", "src_dir"], "C:/x", "relative"),
        (["python", "runtime_config_module"], "pkg.config", "needs python.package"),
        (["python", "package"], "bad-name", "invalid form"),
        (["python", "min_version"], "3", "invalid form"),
        (["git", "default_branch"], "-x", "invalid form"),
        (["agents", "codex", "enabled"], "yes", "true or false"),
        (["agents", "gemini"], {"enabled": True, "cli": "g"}, "unknown"),
        (["foreign_bindings"], "image_correction", "list"),
        (["foreign_scan_exclude"], ["../outside"], "relative"),
    ],
)
def test_malformed_bindings_are_refused_by_field(path: list[str], value: Any, message: str) -> None:
    with pytest.raises(pc.ProjectConfigError, match=message):
        pc.parse(_mutated(path, value), Path("."))


def test_a_missing_or_broken_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(pc.ProjectConfigError, match="not found"):
        pc.load(tmp_path)
    (tmp_path / ".agents").mkdir()
    (tmp_path / pc.REL_PATH).write_text("{not json", encoding="utf-8")
    with pytest.raises(pc.ProjectConfigError, match="unreadable"):
        pc.load(tmp_path)
