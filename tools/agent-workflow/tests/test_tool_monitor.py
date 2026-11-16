"""tool_monitor.py / tool_health.py: the harness-error streak (P-01), `[ps-error-hidden]` (P-07), `[eol-warning]` (P-04).

Pre-mortem IDs (tasks/R3_GUARD_HOOKS_PREMORTEM.md) name the rows; `X-` rows are added cases. State goes to a
test-owned root (`tmp_path`), never the project's `.tmp/pm-state`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest
import tool_guard as tg
import tool_health as th
import tool_monitor as tm
from conftest import GITATTRIBUTES, git

CFG = gc.load()
HOOKS = Path(tm.__file__).resolve().parent


def with_cfg(**sections: dict[str, Any]) -> dict[str, Any]:
    cfg = json.loads(json.dumps(CFG))
    for name, values in sections.items():
        cfg[name].update(values)
    return gc.validate(cfg)


def failure(tool: str, error: str, session: str = "s1", interrupt: bool = False) -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUseFailure",
        "tool_name": tool,
        "session_id": session,
        "error": error,
        "is_interrupt": interrupt,
    }


def success(tool: str, session: str = "s1", **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "session_id": session, **extra}


def pre(
    root: Path,
    tool: str = "Bash",
    command: str = "ls",
    session: str = "s1",
    cfg: dict[str, Any] = CFG,
) -> str:
    args = argparse.Namespace(config=None, git_mode="pm", worktree=None)
    event = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": session,
        "cwd": str(root),
    }
    blocks, warns = tg.evaluate(tg.Context(event, cfg, args, root))
    return "B" if blocks else "W" if warns else "A"


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm.pg, "STATE_DIR", tmp_path / "pm-state")
    monkeypatch.setattr(tm, "CRASH_LOG", tmp_path / "pm-state" / "guard-crash.log")


STREAK_CASES = [
    (
        "ES-01/ES-NRM-01",
        [
            "bash: -c: line 74: unexpected EOF while looking for matching '\"'",
            "bash: -c: line 82: unexpected EOF while looking for matching '\"'",
        ],
        "B",
    ),
    (
        "ES-02/ES-NRM-02",
        [
            "ENAMETOOLONG: name too long, uv_spawn (PID 1234)",
            "ENAMETOOLONG: name too long, uv_spawn (PID 5678)",
        ],
        "B",
    ),
    ("ES-03", ["Exit code 1\nFAILED tests/a.py", "Exit code 1\nFAILED tests/a.py"], "A"),
    (
        "ES-FB-01",
        ["Exit code 1\nFAILED tests/unit/test_a.py", "Exit code 1\nFAILED tests/unit/test_b.py"],
        "A",
    ),
    (
        "X-S01",
        [
            "Exit code 127\nbash: uv_spawn: ENAMETOOLONG",
            "Exit code 127\nbash: uv_spawn: ENAMETOOLONG",
        ],
        "A",
    ),
    # Two different errors: no streak.
    ("X-S02", ["ENAMETOOLONG uv_spawn", "spawn bash ENOENT"], "A"),
    ("X-S03", ["something odd happened", "something odd happened"], "A"),  # not a harness pattern
]


@pytest.mark.parametrize(
    ("case", "errors", "expect"), STREAK_CASES, ids=[c[0] for c in STREAK_CASES]
)
def test_streak(tmp_path: Path, case: str, errors: list[str], expect: str) -> None:
    notes: list[str] = []
    for error in errors:
        notes += tm.run(failure("Bash", error), CFG, tmp_path)
    assert pre(tmp_path) == expect, case
    assert bool(notes) == (expect == "B")
    if notes:
        assert notes[0].startswith("[tool-broken] Bash") and "tool_health.py reset Bash" in notes[0]


def test_flag_is_per_tool_and_per_session(tmp_path: Path) -> None:
    for _ in range(2):
        tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    assert pre(tmp_path, "PowerShell") == "A"
    assert pre(tmp_path, session="s2") == "A"
    assert pre(tmp_path, "Monitor") == "B"  # dependent_tools: Monitor runs through Bash


def test_dependent_tools_and_ordinary_exit_overrides(tmp_path: Path) -> None:
    cfg = with_cfg(tool_broken={"dependent_tools": {}, "ordinary_exit_pattern": "^Fatal:"})
    for _ in range(2):
        tm.run(failure("Bash", "Exit code 127\nbash: uv_spawn: ENAMETOOLONG"), cfg, tmp_path)
    assert pre(tmp_path, cfg=cfg) == "B"  # the default pattern would treat these as ordinary exits
    assert pre(tmp_path, "Monitor", cfg=cfg) == "A"
    assert th.harness_signature("Fatal: uv_spawn ENAMETOOLONG", cfg) is None


def test_interrupts_and_successes_do_not_count(tmp_path: Path) -> None:
    tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    tm.run(failure("Bash", "ENAMETOOLONG uv_spawn", interrupt=True), CFG, tmp_path)
    tm.run(success("Bash"), CFG, tmp_path)
    tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    assert pre(tmp_path) == "A"


def test_a_success_does_not_clear_a_flag(tmp_path: Path) -> None:
    for _ in range(2):
        tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    tm.run(success("Bash"), CFG, tmp_path)
    assert pre(tmp_path) == "B"


def test_reset_file_clears_only_an_older_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fixed clock: flag times and reset mtimes taken from the coarse wall clock can share a tick.
    clock = argparse.Namespace(now=time.time() - 600, sleep=time.sleep)
    clock.time = lambda: clock.now
    monkeypatch.setattr(th, "time", clock)
    directory = th.state_dir(CFG, tmp_path)
    reset = th.reset_path(directory, "Bash")
    directory.mkdir(parents=True)
    reset.write_text("x", encoding="utf-8")
    os.utime(reset, (clock.now - 60, clock.now - 60))
    for _ in range(2):
        tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    assert pre(tmp_path) == "B"  # ES-FB-02: a reset older than the flag does not clear it
    os.utime(reset, (clock.now, clock.now))
    assert pre(tmp_path) == "B"  # a reset in the flag's own tick does not clear it either
    os.utime(reset, (clock.now + 1, clock.now + 1))
    assert pre(tmp_path) == "A"
    clock.now += 2
    tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    assert pre(tmp_path) == "A"  # counting starts again after the reset
    tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    assert pre(tmp_path) == "B"


def test_streak_length_override(tmp_path: Path) -> None:
    cfg = with_cfg(tool_broken={"streak_length": 3})
    for _ in range(2):
        tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), cfg, tmp_path)
    assert pre(tmp_path, cfg=cfg) == "A"
    tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), cfg, tmp_path)
    assert pre(tmp_path, cfg=cfg) == "B"


def test_mode_override(tmp_path: Path) -> None:
    off = with_cfg(tool_broken={"mode": "off"})
    for _ in range(2):
        assert tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), off, tmp_path) == []
    assert pre(tmp_path, cfg=off) == "A"
    for _ in range(2):
        tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), CFG, tmp_path)
    assert pre(tmp_path, cfg=with_cfg(tool_broken={"mode": "warn"})) == "W"


def test_harness_pattern_override(tmp_path: Path) -> None:
    cfg = with_cfg(tool_broken={"harness_error_patterns": ["socket hang up"]})
    for _ in range(2):
        tm.run(failure("Bash", "ENAMETOOLONG uv_spawn"), cfg, tmp_path)
    assert pre(tmp_path, cfg=cfg) == "A"
    for _ in range(2):
        tm.run(failure("Bash", "socket hang up (attempt 3)"), cfg, tmp_path)
    assert pre(tmp_path, cfg=cfg) == "B"


def test_normalise_collapses_numbers_and_hex() -> None:
    first = th.normalise("\n  line 74 at 0xDEAD pid 12\nrest")
    assert first == th.normalise("line 8 at 0x1f pid 99")
    assert th.normalise("a" * 1000) == "a" * 300


TEST_SHELL_ENV = "R3_GUARD_TEST_SHELL"  # test-only: keeps the developer's or CI's real variable out of these cases


def probe_cfg(shell: str | Path, args: list[str], **extra: Any) -> dict[str, Any]:
    """The config with this platform's probe shell set to `shell` (a test double where it is the Python
    interpreter); the other platform keeps the committed value."""
    spec = {"env": TEST_SHELL_ENV, "paths": [Path(shell).as_posix()]}
    shells = {**CFG["tool_broken"]["probe_shell"], th.platform_key(): spec}
    return with_cfg(tool_broken={"probe_shell": shells, "probe_args": args, **extra})


PY_OK = ["-I", "-c", "print('ok')"]


@pytest.fixture
def no_test_shell_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TEST_SHELL_ENV, raising=False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "")[:100])


@pytest.mark.usefixtures("no_test_shell_env")
def test_session_start_probe_only_warns(tmp_path: Path) -> None:
    ok = probe_cfg(sys.executable, PY_OK)
    assert th.session_start({"session_id": "s1"}, ok, tmp_path) is None
    bad = probe_cfg(sys.executable, ["-I", "-c", "raise SystemExit(3)"])
    note = th.session_start({"session_id": "s1"}, bad, tmp_path)
    assert note and "[tool-health] Bash probe failed" in note and "exited 3" in note
    assert "[tool-broken]" not in note
    # F11: a failed probe never flags; only the harness-error streak does.
    assert pre(tmp_path) == "A"
    assert not (tmp_path / CFG["tool_broken"]["reset_dir"]).exists()
    other = probe_cfg(sys.executable, PY_OK, probe_expect="ready")
    assert "probe failed" in (th.session_start({"session_id": "s3"}, other, tmp_path) or "")
    off = probe_cfg(sys.executable, ["-I", "-c", "raise SystemExit(3)"], mode="off")
    assert th.session_start({"session_id": "s4"}, off, tmp_path) is None


@pytest.mark.usefixtures("no_test_shell_env")
def test_session_start_probe_failures_are_reported(tmp_path: Path) -> None:
    missing = probe_cfg(tmp_path / "no-such-shell", PY_OK)
    assert "no configured shell exists" in (
        th.session_start({"session_id": "a"}, missing, tmp_path) or ""
    )
    slow = probe_cfg(
        sys.executable, ["-I", "-c", "import time; time.sleep(5)"], probe_timeout_seconds=1
    )
    assert "timed out" in (th.session_start({"session_id": "b"}, slow, tmp_path) or "")
    # The WSL launcher writes UTF-16: decoded, not compared as mojibake.
    utf16 = probe_cfg(
        sys.executable,
        ["-I", "-c", "import sys; sys.stdout.buffer.write('ok'.encode('utf-16-le'))"],
    )
    assert th.session_start({"session_id": "c"}, utf16, tmp_path) is None
    assert pre(tmp_path, session="a") == pre(tmp_path, session="b") == "A"


def test_probe_shell_environment_variable_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = probe_cfg(tmp_path / "no-such-shell", PY_OK)
    monkeypatch.setenv(TEST_SHELL_ENV, sys.executable)
    assert th.probe_shell(cfg) == (sys.executable, f"${TEST_SHELL_ENV}")
    assert th.probe(cfg) is None
    monkeypatch.setenv(TEST_SHELL_ENV, "bash")  # relative: refused, never looked up on PATH
    shell, why = th.probe_shell(cfg)
    assert shell is None and "names no file" in why
    monkeypatch.setenv(TEST_SHELL_ENV, str(tmp_path / "gone.exe"))
    assert th.probe_shell(cfg)[0] is None


def test_committed_probe_shell_is_the_harness_shell() -> None:
    """F11/F19: the committed config, the real shell, nothing faked. On Windows it is Git Bash, never the WSL
    launcher in System32 that a bare `bash` resolves to."""
    shell, how = th.probe_shell(CFG)
    if shell is None:
        pytest.skip(f"no harness shell on this machine: {how}")
    assert "system32" not in shell.lower().replace("\\", "/")
    assert th.probe(CFG) is None


def test_session_start_hook_with_the_real_event_shape(tmp_path: Path) -> None:
    """F11: the SessionStart JSON Claude Code sends, the committed config, the real probe; the output is empty or a
    `[tool-health]` warning, never a flag."""
    event = {
        "session_id": f"probe-{os.getpid()}",
        "transcript_path": str(tmp_path / "t.jsonl"),
        "cwd": str(gc.ROOT),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(HOOKS / "tool_health.py"), "session-start"],
        input=json.dumps(event).encode(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.decode().strip()
    if out:
        ctx = json.loads(out)["hookSpecificOutput"]
        assert ctx["hookEventName"] == "SessionStart"
        assert ctx["additionalContext"].startswith("[tool-health]")
    # A scratch root, never the project's live health directory: session_start only probes the shell and writes no
    # state (hooks/tool_health.py session_start), so the child leaves no flag to find.
    directory = th.state_dir(CFG, tmp_path)
    assert th.flagged(directory, event["session_id"], "Bash") is None


@pytest.mark.usefixtures("no_test_shell_env")
def test_session_start_reports_a_long_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = probe_cfg(sys.executable, PY_OK, max_path_chars=10)
    monkeypatch.setenv("PATH", os.pathsep.join(["x" * 20, os.environ.get("PATH", "")]))
    assert "[tool-health] PATH holds" in (th.session_start({}, ok, tmp_path) or "")


def test_reset_and_status_cli(tmp_path: Path) -> None:
    rel = f".tmp/r3-guard-hooks-test/health-{os.getpid()}"
    config = gc.ROOT / ".tmp" / "r3-guard-hooks-test" / f"cfg-{os.getpid()}.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    cfg = with_cfg(tool_broken={"reset_dir": rel})
    config.write_text(json.dumps(cfg), encoding="utf-8")
    directory = gc.ROOT / rel
    try:
        th.flag(directory, "s9", "Bash", "ENAMETOOLONG")
        cli = [sys.executable, "-B", "-I", str(HOOKS / "tool_health.py"), "--config", str(config)]
        opts: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 60, "check": False}
        status = subprocess.run([*cli, "status"], **opts)
        assert status.returncode == 0 and "s9\tBash\tflagged" in status.stdout
        bad = subprocess.run([*cli, "reset", "../x"], **opts)
        assert bad.returncode == 1
        time.sleep(0.05)
        done = subprocess.run([*cli, "reset", "Bash"], **opts)
        assert done.returncode == 0 and th.flagged(directory, "s9", "Bash") is None
    finally:
        for path in directory.glob("*"):
            path.unlink()
        directory.rmdir()
        config.unlink()


PS_CASES = [
    (
        "PW-PSE-01/PW-03",
        "retire : The term 'retire' is not recognized as the name of a cmdlet\n"
        "At line:1 char:1\n+ retire = $null\n+ ~~~~~~\n"
        "    + CategoryInfo          : ObjectNotFound: (retire:String) [], CommandNotFoundException\n"
        "    + FullyQualifiedErrorId : CommandNotFoundException\ndone",
        True,
    ),
    (
        "PW-PSE-FB-01",
        "Get-Help Set-Content\nNAME\n    Set-Content\nSYNOPSIS\n    Writes new content",
        False,
    ),
    ("X-E01", "the docs mention FullyQualifiedErrorId in prose", False),
]


@pytest.mark.parametrize(("case", "stdout", "warned"), PS_CASES, ids=[c[0] for c in PS_CASES])
def test_ps_error_hidden(tmp_path: Path, case: str, stdout: str, warned: bool) -> None:
    event = success("PowerShell", tool_response={"stdout": stdout, "stderr": ""})
    notes = tm.ps_error(event, CFG)
    assert bool(notes) == warned, case
    assert not notes or notes[0].startswith("[ps-error-hidden]")


def test_ps_error_only_for_powershell_and_configurable() -> None:
    text = "    + CategoryInfo          : ObjectNotFound"
    assert tm.ps_error(success("Bash", tool_response={"stdout": text}), CFG) == []
    off = with_cfg(ps_error={"mode": "off"})
    assert tm.ps_error(success("PowerShell", tool_response={"stdout": text}), off) == []
    custom = with_cfg(ps_error={"patterns": ["WARNING: retry"]})
    assert tm.ps_error(success("PowerShell", tool_response="WARNING: retry 2"), custom)


@pytest.fixture
def eol_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "eol-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitattributes").write_bytes(GITATTRIBUTES.encode())
    for rel, data in {
        "tasks/STATUS.md": b"# status\n",
        "assets/sample.png": b"\x89PNG\r\n\x1a\n\x00\x00\r\n",
        "frontend/app.js": b"let a = 1;\n",
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def edit(path: Path, session: str = "s1") -> dict[str, Any]:
    return success("Edit", session, tool_input={"file_path": str(path)})


def test_eol_warning_once_per_session(eol_repo: Path) -> None:
    status = eol_repo / "tasks" / "STATUS.md"
    status.write_bytes(b"# status\r\nnew row\r\n")  # PW-CRLF-01 / PW-01: Add-Content wrote CRLF
    notes = tm.eol(edit(status), CFG)
    assert len(notes) == 1 and "--fix" in notes[0]
    assert notes[0].startswith("[eol-warning] tasks/STATUS.md")
    assert tm.eol(edit(status), CFG) == []
    assert len(tm.eol(edit(status, "s2"), CFG)) == 1


def test_eol_no_warning_for_binary_or_text_auto(eol_repo: Path) -> None:
    # PW-CRLF-FB-01 / PW-02
    (eol_repo / "assets" / "sample.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\r\n\r\n")
    (eol_repo / "frontend" / "app.js").write_bytes(b"let a = 1;\r\n")  # text=auto: Git converts it
    assert tm.eol(edit(eol_repo / "assets" / "sample.png"), CFG) == []
    assert tm.eol(edit(eol_repo / "frontend" / "app.js"), CFG) == []
    assert tm.eol(success("PowerShell", cwd=str(eol_repo)), CFG) == []


def test_eol_after_shell_and_its_switch(eol_repo: Path) -> None:
    (eol_repo / "tasks" / "STATUS.md").write_bytes(b"a\r\n")
    event = success("PowerShell", cwd=str(eol_repo / "tasks"))
    assert tm.eol(event, with_cfg(eol={"check_after_shell": False})) == []
    assert tm.eol(event, with_cfg(eol={"mode": "off"})) == []
    assert [n.split(":")[0] for n in tm.eol(event, CFG)] == ["[eol-warning] tasks/STATUS.md"]


def test_eol_ignores_untracked_and_outside_files(eol_repo: Path, tmp_path: Path) -> None:
    (eol_repo / "new.md").write_bytes(b"a\r\n")
    (tmp_path / "loose.md").write_bytes(b"a\r\n")
    assert tm.eol(edit(eol_repo / "new.md"), CFG) == []
    assert tm.eol(edit(tmp_path / "loose.md"), CFG) == []
    assert tm.eol(edit(eol_repo / "missing.md"), CFG) == []


def run_monitor(event: Any, *args: str, raw: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-I", str(HOOKS / "tool_monitor.py"), *args],
        input=raw if raw is not None else json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def test_monitor_process_never_blocks() -> None:
    assert run_monitor(None, raw="{broken").returncode == 0
    ps = success("PowerShell", tool_response={"stdout": "    + FullyQualifiedErrorId : X"})
    out = run_monitor(ps)
    assert out.returncode == 0
    context = json.loads(out.stdout)["hookSpecificOutput"]
    assert context["hookEventName"] == "PostToolUse"
    assert "[ps-error-hidden]" in context["additionalContext"]
    bad = run_monitor(ps, "--config", str(gc.ROOT / "README.md"))
    assert bad.returncode == 0 and "[guard-config]" in bad.stdout


def test_monitor_crash_is_logged_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any) -> list[str]:
        raise RuntimeError("injected")

    monkeypatch.setattr(tm, "run", boom)
    monkeypatch.setattr(tm.pg, "read_event", lambda: json.dumps(success("Bash")))
    assert tm.main([]) == 0
    assert "injected" in (tmp_path / "pm-state" / "guard-crash.log").read_text(encoding="utf-8")
