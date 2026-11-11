"""PowerShell-level tests of pm-supervisor.ps1's CLI worker watch (skipped when no PowerShell is installed).

Each test builds a throw-away project tree holding copies of the supervisor and cli_worker.py, so the real registry
(`.tmp/pm-state/cli-workers.json`) is never read or written.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell not installed")
WAIT_SECONDS = 90


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    tools = tmp_path / "tools" / "agent-workflow"
    tools.mkdir(parents=True)
    for name in ("pm-supervisor.ps1", "cli_worker.py"):
        shutil.copy2(TOOLS / name, tools / name)
    (tmp_path / ".tmp" / "pm-state").mkdir(parents=True)
    return tmp_path


def worker(**extra: object) -> dict:
    base = {"model": "haiku", "effort": "low", "session_id": "s", "cwd": ".", "turns": 1}
    return {**base, "label": "T1", "status": "idle", **extra}


def write_registry(tree: Path, data: dict) -> None:
    (tree / ".tmp" / "pm-state" / "cli-workers.json").write_text(json.dumps(data), encoding="utf-8")


def start(
    tree: Path,
    *args: str,
    max_minutes: str = "2",
    poll: str = "1",
    python: str = sys.executable,
) -> subprocess.Popen[str]:
    cmd = [
        str(POWERSHELL),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(tree / "tools" / "agent-workflow" / "pm-supervisor.ps1"),
        "-MaxMinutes",
        max_minutes,
        "-PollSeconds",
        poll,
        "-Companion",
        "none",
        "-Python",
        python,
        *args,
    ]
    env = {**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env
    )


def finish(proc: subprocess.Popen[str]) -> str:
    try:
        out, _ = proc.communicate(timeout=WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()  # the supervisor started by this test, by its own handle
        proc.communicate()
        raise
    return out


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_busy_worker_whose_launcher_died_is_reported(tree: Path) -> None:
    write_registry(
        tree, {"w1": worker(status="busy", launcher_pid=dead_pid(), current_log="001.jsonl")}
    )
    out = finish(start(tree, "-ExitOnEvent"))
    assert "EVENT dead_launcher cli w1 [T1]" in out and "cli_worker.py send" in out


def test_a_busy_worker_with_an_idle_log_is_reported_stalled_only_past_the_threshold(
    tree: Path,
) -> None:
    jobs = tree / ".tmp" / "cli-jobs" / "w1"
    jobs.mkdir(parents=True)
    log = jobs / "001.jsonl"
    log.write_text("{}\n", encoding="utf-8")
    old = time.time() - 20 * 60
    os.utime(log, (old, old))
    write_registry(
        tree, {"w1": worker(status="busy", launcher_pid=os.getpid(), current_log="001.jsonl")}
    )
    out = finish(start(tree, "-ExitOnEvent", "-CliStallMinutes", "10"))
    assert "EVENT stalled cli w1 [T1] log idle" in out

    # Non-default threshold above the idle time and the disabling value 0: no stall event, heartbeat only.
    proc = start(tree, "-CliStallMinutes", "0", max_minutes="0")
    out = finish(proc)
    assert "stalled" not in out and "HEARTBEAT" in out and "w1=running" in out


def test_a_worker_that_was_busy_and_is_now_idle_is_reported_finished(tree: Path) -> None:
    write_registry(
        tree, {"w1": worker(status="busy", launcher_pid=os.getpid(), current_log="001.jsonl")}
    )
    proc = start(tree, "-ExitOnEvent", poll="3")
    marker = tree / ".tmp" / "pm-state" / "cli-status.out"
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline and not (marker.exists() and marker.stat().st_size > 0):
        time.sleep(0.2)
    assert marker.exists() and marker.stat().st_size > 0, "first poll did not run"
    write_registry(tree, {"w1": worker(status="idle")})
    out = finish(proc)
    assert "EVENT finished cli w1 [T1] status=idle" in out


def test_retired_and_idle_workers_raise_no_event_and_the_run_ends_with_a_heartbeat(
    tree: Path,
) -> None:
    write_registry(tree, {"a": worker(), "b": worker(status="retired")})
    out = finish(start(tree, max_minutes="0"))
    assert "EVENT" not in out and "HEARTBEAT" in out


def test_an_unusable_interpreter_is_reported_once_and_the_supervisor_still_ends_cleanly(
    tree: Path,
) -> None:
    write_registry(tree, {})
    proc = start(tree, max_minutes="0", python=str(tree / "missing-python"))
    out = finish(proc)
    assert out.count("cli_watch_unavailable") == 1 and "HEARTBEAT" in out
    assert proc.returncode == 0


def dead_launcher_output(tree: Path, with_created: bool) -> tuple[str, int]:
    """Supervisor output for a busy worker whose launcher is gone and whose worker process (a child this test
    started) runs; `with_created` records the creation time `cli_worker.py send` needs to prove its identity."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        extra: dict[str, object] = {}
        if with_created:
            spec = importlib.util.spec_from_file_location(
                "cli_worker_sup", tree / "tools" / "agent-workflow" / "cli_worker.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules["cli_worker_sup"] = module
            spec.loader.exec_module(module)
            extra["worker_created"] = module.process_start(child.pid)
        write_registry(
            tree,
            {
                "w1": worker(
                    status="busy",
                    launcher_pid=dead_pid(),
                    worker_pid=child.pid,
                    current_log="001.jsonl",
                    **extra,
                )
            },
        )
        return finish(start(tree, "-ExitOnEvent")), child.pid
    finally:
        child.kill()  # the process this test started, by its own handle
        child.wait()


def test_a_dead_launcher_with_a_live_worker_process_says_send_refuses(tree: Path) -> None:
    out, pid = dead_launcher_output(tree, with_created=True)
    assert "EVENT dead_launcher cli w1 [T1]" in out
    assert f"worker pid {pid} still runs; send refuses until it ends" in out
    assert "send resumes it" not in out


def test_a_dead_launcher_with_an_unverifiable_worker_pid_does_not_say_send_resumes(
    tree: Path,
) -> None:
    out, pid = dead_launcher_output(tree, with_created=False)
    assert "EVENT dead_launcher cli w1 [T1]" in out
    assert f"worker pid {pid} cannot be verified; send refuses until --pid-checked" in out
    assert "send resumes it" not in out


def test_a_gemini_result_with_non_ascii_text_is_read_as_utf8(tree: Path) -> None:
    results = tree / ".tmp" / "agy-work" / "_results"
    results.mkdir(parents=True)
    (results / "g1.done").write_text("", encoding="utf-8")
    (results / "g1.json").write_bytes(
        json.dumps(
            {"response": "Kết quả: Müller “ok”", "denied_actions": ["đ"]},
            ensure_ascii=False,
        ).encode("utf-8")  # no BOM, as the Gemini wrapper writes it
    )
    (tree / ".tmp" / "pm-state" / "agents.json").write_text(
        json.dumps(
            [{"kind": "gemini", "id": "g1", "label": "G", "started": "2026-10-11T10:00:00"}]
        ),
        encoding="utf-8",
    )
    write_registry(tree, {})
    out = finish(start(tree, "-ExitOnEvent"))
    assert "EVENT completed gemini g1 [G] denied=1" in out
    assert "result_unreadable" not in out and "empty_response" not in out


def wait_for(path: Path, seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.is_file():
            return True
        time.sleep(0.25)
    return False


def test_the_running_supervisor_writes_a_heartbeat_the_hook_accepts(tree: Path) -> None:
    """pid and creation time of the supervisor process, in this project's state directory only."""
    import pm_invariants as pi

    state = tree / ".tmp" / "pm-state"
    proc = start(tree)
    try:
        assert wait_for(state / "pm-supervisor.json"), "no heartbeat written"
        body = json.loads((state / "pm-supervisor.json").read_text(encoding="utf-8-sig"))
        assert body["pid"] == proc.pid and Path(body["root"]) == tree.resolve()
        started = pi.load_cli_worker().process_start(proc.pid)
        assert started is not None and abs(body["created"] - started) <= 2.0
        assert pi.supervisor_alive(state, "pm-supervisor.json") is True
    finally:
        proc.kill()  # the supervisor started by this test, by its own handle
        proc.communicate()
    assert (
        pi.supervisor_alive(state, "pm-supervisor.json") is False
    )  # killed: the stale file is no liveness


def test_the_heartbeat_is_removed_when_the_supervisor_exits(tree: Path) -> None:
    write_registry(
        tree, {"w1": worker(status="busy", launcher_pid=dead_pid(), current_log="1.jsonl")}
    )
    out = finish(start(tree, "-ExitOnEvent"))
    assert "EVENT dead_launcher" in out
    assert not (tree / ".tmp" / "pm-state" / "pm-supervisor.json").exists()


def test_the_heartbeat_file_name_is_a_parameter_and_stays_in_the_state_directory(
    tree: Path,
) -> None:
    write_registry(tree, {})
    proc = start(tree, "-HeartbeatFile", "..\\..\\escape.json")
    try:
        assert wait_for(tree / ".tmp" / "pm-state" / "escape.json"), (
            "name must be reduced to a file name"
        )
    finally:
        proc.kill()
        proc.communicate()
    assert not (tree / "escape.json").exists() and not (tree / ".tmp" / "escape.json").exists()
