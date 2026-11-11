"""Loader of `.agents/project.json`, the one file that binds this tooling to a repository.

Every other tool derives the project root from its own location (`tools/agent-workflow/` is two levels below it);
the values that differ between repositories (runtime package, branches, enabled agent CLIs, tokens of a previous
project) live in `.agents/project.json`. Stdlib only, so hooks running as `python -B -I` can import it.

Validation is strict: an unknown key, a missing key or a wrong type is a `ProjectConfigError` naming the field.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REL_PATH = ".agents/project.json"
SCHEMA_VERSION = 1  # protocol-bound: the layout version this loader understands

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")
_VERSION = re.compile(r"^\d+\.\d+$")
AGENT_KEYS = ("claude", "codex", "antigravity")


class ProjectConfigError(ValueError):
    """The project binding file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class AgentCli:
    name: str
    enabled: bool
    cli: str
    runner_scripts_dir: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    name: str
    summary: str
    min_python: tuple[int, int]
    venv_dir: str
    src_dir: str
    package: str | None
    runtime_config_module: str | None
    runtime_config_loader: str
    default_branch: str
    integration_branch: str
    agents: dict[str, AgentCli]
    foreign_bindings: tuple[str, ...]
    foreign_scan_exclude: tuple[str, ...]

    @property
    def enabled_agents(self) -> list[str]:
        return [k for k, a in self.agents.items() if a.enabled]


def _keys(obj: Any, where: str, required: set[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ProjectConfigError(f"{where}: expected an object")
    present = {k for k in obj if not k.startswith("_")}
    missing = required - present
    unknown = present - required - optional
    if missing:
        raise ProjectConfigError(f"{where}: missing {sorted(missing)}")
    if unknown:
        raise ProjectConfigError(f"{where}: unknown {sorted(unknown)}")
    return obj


def _str(value: Any, where: str, *, pattern: re.Pattern[str] | None = None, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{where}: expected a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise ProjectConfigError(f"{where}: {value!r} has an invalid form")
    return value


def _relpath(value: Any, where: str) -> str:
    text = _str(value, where)
    assert text is not None
    norm = text.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
        raise ProjectConfigError(f"{where}: {text!r} must be a path relative to the project root")
    return norm


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProjectConfigError(f"{where}: expected a list")
    return tuple(str(_str(v, f"{where}[{i}]")) for i, v in enumerate(value))


def parse(data: Any, root: Path) -> ProjectConfig:
    top = _keys(
        data,
        REL_PATH,
        {"schema_version", "project", "python", "git", "agents", "foreign_bindings", "foreign_scan_exclude"},
    )
    if top["schema_version"] != SCHEMA_VERSION:
        raise ProjectConfigError(f"schema_version: expected {SCHEMA_VERSION}, got {top['schema_version']!r}")
    proj = _keys(top["project"], "project", {"name", "summary"})
    py = _keys(
        top["python"],
        "python",
        {"min_version", "venv_dir", "src_dir", "package", "runtime_config_module", "runtime_config_loader"},
    )
    git = _keys(top["git"], "git", {"default_branch", "integration_branch"})
    agents_raw = _keys(top["agents"], "agents", set(AGENT_KEYS))
    agents: dict[str, AgentCli] = {}
    for key in AGENT_KEYS:
        where = f"agents.{key}"
        optional = frozenset({"runner_scripts_dir"}) if key == "antigravity" else frozenset()
        item = _keys(agents_raw[key], where, {"enabled", "cli"}, optional)
        if not isinstance(item["enabled"], bool):
            raise ProjectConfigError(f"{where}.enabled: expected true or false")
        runner = item.get("runner_scripts_dir")
        agents[key] = AgentCli(
            name=key,
            enabled=item["enabled"],
            cli=str(_str(item["cli"], f"{where}.cli")),
            runner_scripts_dir=_relpath(runner, f"{where}.runner_scripts_dir") if runner is not None else None,
        )
    version = str(_str(py["min_version"], "python.min_version", pattern=_VERSION))
    major, minor = (int(p) for p in version.split("."))
    module = _str(py["runtime_config_module"], "python.runtime_config_module", pattern=_IDENT, nullable=True)
    package = _str(py["package"], "python.package", pattern=_IDENT, nullable=True)
    if module is not None and package is None:
        raise ProjectConfigError("python.runtime_config_module needs python.package")
    return ProjectConfig(
        root=root,
        name=str(_str(proj["name"], "project.name")),
        summary=str(_str(proj["summary"], "project.summary")),
        min_python=(major, minor),
        venv_dir=_relpath(py["venv_dir"], "python.venv_dir"),
        src_dir=_relpath(py["src_dir"], "python.src_dir"),
        package=package,
        runtime_config_module=module,
        runtime_config_loader=str(_str(py["runtime_config_loader"], "python.runtime_config_loader", pattern=_IDENT)),
        default_branch=str(_str(git["default_branch"], "git.default_branch", pattern=_BRANCH)),
        integration_branch=str(_str(git["integration_branch"], "git.integration_branch", pattern=_BRANCH)),
        agents=agents,
        foreign_bindings=_str_list(top["foreign_bindings"], "foreign_bindings"),
        foreign_scan_exclude=tuple(
            _relpath(v, f"foreign_scan_exclude[{i}]")
            for i, v in enumerate(_str_list(top["foreign_scan_exclude"], "foreign_scan_exclude"))
        ),
    )


def load(root: Path | None = None) -> ProjectConfig:
    """Read and validate `<root>/.agents/project.json` (root defaults to this checkout)."""
    base = (root or ROOT).resolve()
    path = base / REL_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectConfigError(f"{REL_PATH} not found under {base}") from None
    except (OSError, ValueError) as exc:
        raise ProjectConfigError(f"{REL_PATH} is unreadable: {type(exc).__name__}: {exc}") from exc
    return parse(data, base)


if __name__ == "__main__":
    cfg = load()
    print(f"{cfg.name}: agents={cfg.enabled_agents} package={cfg.package} branch={cfg.integration_branch}")
