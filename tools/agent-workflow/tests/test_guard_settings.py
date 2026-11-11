"""Hook registration: the PM template (settings.example.json) and the worker settings built by cli_worker.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import tool_guard as tg

TOOLS = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((TOOLS / "settings.example.json").read_text(encoding="utf-8"))
spec = importlib.util.spec_from_file_location("cli_worker_guard_settings", TOOLS / "cli_worker.py")
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


@pytest.fixture(autouse=True)
def no_brief_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests start workers with one-word briefs; the lint gate has its own tests."""
    monkeypatch.setattr(cw, "lint_gate", lambda *args, **kwargs: None)


def commands(settings: dict[str, Any], event: str) -> list[tuple[str, list[str]]]:
    return [
        (entry.get("matcher", ""), hook["args"])
        for entry in settings.get("hooks", {}).get(event, [])
        for hook in entry["hooks"]
    ]


def scripts(settings: dict[str, Any], event: str) -> list[str]:
    return [Path(args[2]).name for _, args in commands(settings, event)]


def test_example_registers_every_guard_hook() -> None:
    assert scripts(EXAMPLE, "PreToolUse") == [
        "process_guard.py",
        "tool_guard.py",
        "question_guard.py",
    ]
    assert commands(EXAMPLE, "PreToolUse")[2][0] == "ArtifactData"
    assert scripts(EXAMPLE, "PostToolUse") == [
        "ctx_nudge.py",
        "tool_monitor.py",
        "pm_invariants.py",
    ]
    assert scripts(EXAMPLE, "PostToolUseFailure") == ["tool_monitor.py"]
    assert scripts(EXAMPLE, "SessionStart") == [
        "standing_rules.py",
        "session_context.py",
        "tool_health.py",
    ]
    matcher, args = commands(EXAMPLE, "PreToolUse")[1]
    assert matcher == "Bash|PowerShell|Monitor" and tg.parse_args(args[3:]).git_mode == "pm"
    assert commands(EXAMPLE, "SessionStart")[2][1][3:] == ["session-start"]


def test_example_hooks_exist_and_run_isolated() -> None:
    for event in EXAMPLE["hooks"]:
        for _, args in commands(EXAMPLE, event):
            assert args[:2] == ["-B", "-I"]
            rel = args[2].replace("<PROJECT_ROOT>/", "")
            assert (TOOLS.parents[1] / rel).is_file(), rel


def test_example_denies_secrets_and_force_push() -> None:
    deny = EXAMPLE["permissions"]["deny"]
    for tool in ("Read", "Grep", "Glob"):
        assert f"{tool}(**/.secrets/**)" in deny
    for shell in ("Bash", "PowerShell"):
        forced = ("git push --force*", "git push -f*", "git push *--force*")
        assert {f"{shell}({p})" for p in forced} <= set(deny)
    assert not any("git commit" in d for d in deny)  # the PM commits


def test_worker_with_tool_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    settings = cw.hook_settings(
        {"tool_guard": True, "git_guard": "readonly", "ctx_nudge": "100", "loop_window": 2}
    )
    assert not set(cw.LEGACY_GIT_DENIES) & set(settings["permissions"]["deny"])
    assert scripts(settings, "PreToolUse") == ["tool_guard.py"]
    matcher, args = commands(settings, "PreToolUse")[0]
    assert matcher == "Bash|PowerShell|Monitor" and tg.parse_args(args[3:]).git_mode == "worker"
    assert scripts(settings, "PostToolUse") == ["ctx_nudge.py", "tool_monitor.py"]
    assert scripts(settings, "PostToolUseFailure") == ["tool_monitor.py"]
    assert scripts(settings, "SessionStart") == ["tool_health.py"]
    assert "PreCompact" not in settings["hooks"]


def test_merge_worker_and_task_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    settings = cw.hook_settings(
        {
            "tool_guard": True,
            "git_guard": "merge",
            "cwd": "D:/p/.tmp/worktrees/m1",
            "task_file": "D:/p/tasks/T.md",
        }
    )
    args = tg.parse_args(commands(settings, "PreToolUse")[0][1][3:])
    assert (args.git_mode, args.worktree) == ("worker-merge", "D:/p/.tmp/worktrees/m1")
    assert commands(settings, "PreCompact")[0][1][2:] == [
        (cw.HOOKS / "worker_progress.py").as_posix(),
        "--task-file",
        "D:/p/tasks/T.md",
    ]


def test_worker_without_tool_guard_keeps_the_prefix_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    settings = cw.hook_settings({"process_guard": True, "tool_guard": False})
    assert set(cw.LEGACY_GIT_DENIES) <= set(settings["permissions"]["deny"])
    assert scripts(settings, "PreToolUse") == ["process_guard.py"]


def test_build_command_passes_the_worker_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    worker = {
        "model": "haiku",
        "effort": "low",
        "session_id": "s",
        "cwd": "D:/p/.tmp/worktrees/m1",
        "hooks": {"tool_guard": True, "git_guard": "merge"},
    }
    cmd = cw.build_command(worker, "m", resume=False)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    args = tg.parse_args(commands(settings, "PreToolUse")[0][1][3:])
    assert args.worktree == "D:/p/.tmp/worktrees/m1"


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    monkeypatch.setattr(cw, "run_turn", lambda *a, **k: 0)
    monkeypatch.setattr(cw, "ROOT", tmp_path)
    (tmp_path / "wt").mkdir()
    (tmp_path / ".tmp" / "worktrees" / "m1").mkdir(parents=True)
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "T.md").write_text("# T\n", encoding="utf-8")
    msg = tmp_path / "brief.md"
    msg.write_text("hi", encoding="utf-8")
    return msg


def start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, msg: Path, *args: str) -> int:
    base = ["cli_worker.py", "start", "--name", "w", "--model", "haiku", "--effort", "low"]
    base += ["--message-file", str(msg), "--cache-ttl", "5m"]
    monkeypatch.setattr("sys.argv", [*base, *args])
    return cw.main()


def stored(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "cli-workers.json").read_text(encoding="utf-8"))["w"]["hooks"]


def test_start_defaults_to_the_tool_guard(
    registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert start(monkeypatch, tmp_path, registry, "--cwd", str(tmp_path / "wt")) == 0
    hooks = stored(tmp_path)
    assert (hooks["tool_guard"], hooks["git_guard"], hooks["task_file"]) == (True, "readonly", None)


def test_start_merge_job_and_task_file(
    registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = tmp_path / ".tmp" / "worktrees" / "m1"
    # The worktree's own copy: a root task file is refused for a worktree worker (PM mistake #30).
    task = wt / "tasks" / "T.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text("x", encoding="utf-8")
    args = ("--cwd", str(wt), "--git-guard", "merge", "--task-file", str(task))
    assert start(monkeypatch, tmp_path, registry, *args) == 0
    hooks = stored(tmp_path)
    assert (hooks["git_guard"], hooks["task_file"]) == ("merge", task.resolve().as_posix())


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--cwd", "{wt}", "--git-guard", "merge"), "inside a worktree"),
        (("--cwd", "{worktrees}", "--git-guard", "merge"), "inside a worktree"),
        (("--cwd", "{m1}", "--git-guard", "merge", "--tool-guard", "off"), "needs --tool-guard on"),
        (("--cwd", "{wt}", "--task-file", "{brief_txt}"), "--task-file"),
        (("--cwd", "{wt}", "--task-file", "{missing}"), "--task-file"),
        (("--cwd", "{wt}", "--task-file", "{outside}"), "--task-file"),
    ],
)
def test_start_refusals(
    registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    message: str,
) -> None:
    (tmp_path / "brief.txt").write_text("x", encoding="utf-8")
    values = {
        "wt": str(tmp_path / "wt"),
        "worktrees": str(tmp_path / ".tmp" / "worktrees"),
        "m1": str(tmp_path / ".tmp" / "worktrees" / "m1"),
        "brief_txt": str(tmp_path / "brief.txt"),
        "missing": str(tmp_path / "tasks" / "missing.md"),
        "outside": str(tmp_path.parent / "outside.md"),
    }
    with pytest.raises(SystemExit, match=message):
        start(monkeypatch, tmp_path, registry, *[a.format(**values) for a in args])
    assert not (tmp_path / "cli-workers.json").exists()
