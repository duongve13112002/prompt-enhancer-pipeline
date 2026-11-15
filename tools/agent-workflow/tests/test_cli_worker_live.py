"""Regression tests for cli_worker.py liveness, registry safety and `status` (no claude process is started)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cli_worker.py"
spec = importlib.util.spec_from_file_location("cli_worker_live", SCRIPT)
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


@pytest.fixture(autouse=True)
def no_brief_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests start workers with one-word briefs; the lint gate has its own tests."""
    monkeypatch.setattr(cw, "lint_gate", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def no_git_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test double: these turns run in "." (the project) with a faked `Popen`; the turn snapshot (T-024 round 4) has
    its own tests on scratch repositories in test_cli_worker_git_snapshot.py."""
    monkeypatch.setattr(cw, "git_snapshot", lambda cwd, timeout: None)


FAKE_CLAUDE = (
    "import json\n"
    "print(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 5,"
    " 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20}}}))\n"
    "print(json.dumps({'type': 'result', 'result': 'done', 'total_cost_usd': 0.5}))\n"
)


@pytest.fixture
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    return tmp_path


def worker(**extra: object) -> dict:
    return {
        "model": "haiku",
        "effort": "low",
        "session_id": "sid",
        "cwd": ".",
        "status": "idle",
        "turns": 0,
        **extra,
    }


def assistant(tokens: int) -> str:
    usage = {"input_tokens": tokens, "cache_read_input_tokens": 0}
    return json.dumps({"type": "assistant", "message": {"usage": usage}})


def test_pid_alive_distinguishes_a_running_and_an_exited_process() -> None:
    assert cw.pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not cw.pid_alive(child.pid)


def ours(pid: int, role: str) -> dict[str, object]:
    """Registry fields of a recorded process, as the launcher writes them: the PID and its creation time."""
    return {f"{role}_pid": pid, f"{role}_created": cw.process_start(pid)}


def test_kill_tree_ends_only_the_given_process() -> None:
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
    try:
        assert cw.pid_alive(child.pid)
        assert cw.kill_tree(child.pid, cw.process_start(child.pid))
        child.wait(timeout=20)
        assert not cw.pid_alive(child.pid)
        assert cw.pid_alive(os.getpid())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_stale_registry_lock_is_broken_and_a_fresh_one_is_not(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.LOCK.write_text("", encoding="utf-8")
    old = time.time() - cw.STALE_LOCK_SECONDS - 5
    os.utime(cw.LOCK, (old, old))
    with cw.RegistryLock():
        assert cw.LOCK.exists()
    assert not cw.LOCK.exists()
    assert not list(reg.glob("*.stale-*"))

    cw.LOCK.write_text("", encoding="utf-8")  # fresh: held by a live launcher
    monkeypatch.setattr(cw.time, "sleep", lambda _s: None)
    with pytest.raises(SystemExit, match="lock busy"), cw.RegistryLock():
        pass
    assert cw.LOCK.exists()


def test_save_leaves_no_temp_file_and_retries_a_blocked_replace(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.replace
    calls: list[int] = []

    def flaky(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("reader holds the registry open")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(cw.os, "replace", flaky)
    monkeypatch.setattr(cw.time, "sleep", lambda _s: None)
    cw.save({"a": worker()})
    assert len(calls) == 3
    assert json.loads(cw.REGISTRY.read_text(encoding="utf-8"))["a"]["model"] == "haiku"
    assert [p.name for p in reg.iterdir()] == [cw.REGISTRY.name]


def test_save_gives_up_after_the_attempt_budget_and_cleans_up(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(src: object, dst: object) -> None:
        raise PermissionError("blocked")

    monkeypatch.setattr(cw.os, "replace", blocked)
    monkeypatch.setattr(cw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cw, "REPLACE_ATTEMPTS", 3)
    with pytest.raises(PermissionError):
        cw.save({})
    assert not list(reg.glob("*.tmp"))


def test_next_log_number_never_reuses_a_log_of_a_retired_namesake(reg: Path) -> None:
    job = reg / "jobs" / "w"
    job.mkdir(parents=True)
    assert cw.next_log_number(job, 0) == 1
    (job / "001.jsonl").write_text("", encoding="utf-8")
    (job / "002.jsonl").write_text("", encoding="utf-8")
    assert cw.next_log_number(job, 0) == 3  # restarted name: registry says 0 turns
    assert cw.next_log_number(job, 5) == 6  # registry ahead of the logs


def test_live_log_reads_context_from_the_tail_of_a_growing_log(reg: Path) -> None:
    log = reg / "001.jsonl"
    log.write_text(assistant(10) + "\n" + assistant(777) + "\n" + '{"type": "us', encoding="utf-8")
    info = cw.live_log(log)
    assert (
        info["live_context_tokens"] == 777
    )  # last complete assistant event, partial last line ignored
    assert info["log_bytes"] == log.stat().st_size and info["log_idle_minutes"] < 1
    assert cw.live_log(reg / "missing.jsonl") == {}


def test_live_log_tail_is_bounded_and_survives_a_cut_first_line(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cw, "LOG_TAIL_BYTES", 400)
    log = reg / "001.jsonl"
    filler = json.dumps({"type": "user", "text": "x" * 300})
    log.write_text(
        "\n".join([assistant(1), filler, filler, assistant(42), filler[:200]]), encoding="utf-8"
    )
    assert cw.live_log(log)["live_context_tokens"] == 42
    log.write_text("\n".join([assistant(1), filler, filler, filler]), encoding="utf-8")
    assert (
        cw.live_log(log)["live_context_tokens"] is None
    )  # the only assistant event is outside the tail


def test_status_shows_live_context_log_activity_and_launcher_for_a_busy_worker(
    reg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (reg / "jobs" / "b").mkdir(parents=True)
    (reg / "jobs" / "b" / "004.jsonl").write_text(assistant(1234) + "\n", encoding="utf-8")
    cw.save(
        {
            "b": worker(status="busy", **ours(os.getpid(), "launcher"), current_log="004.jsonl"),
            "gone": worker(status="busy", launcher_pid=2**22 + 12345, current_log="x.jsonl"),
            "old": worker(status="retired"),
        }
    )
    monkeypatch.setattr("sys.argv", ["cli_worker.py", "status"])
    assert cw.main() == 0
    out = capsys.readouterr().out
    assert "b: haiku/low busy" in out and "ctx=1234(live)" in out
    assert "log=004.jsonl" in out and "launcher=alive" in out
    assert "launcher=DEAD" in out.split("gone:")[1].split("\n")[0]
    assert "old:" not in out and "1 retired workers hidden" in out

    monkeypatch.setattr("sys.argv", ["cli_worker.py", "status", "--all", "--json"])
    assert cw.main() == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert set(rows) == {"b", "gone", "old"}
    assert rows["b"]["live_context_tokens"] == 1234 and rows["b"]["launcher_alive"] is True
    assert rows["gone"]["launcher_alive"] is False
    assert rows["old"]["status"] == "retired"


def test_status_of_a_busy_worker_registered_before_the_pid_field_stays_readable(
    reg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save({"legacy": worker(status="busy")})
    monkeypatch.setattr("sys.argv", ["cli_worker.py", "status"])
    assert cw.main() == 0
    assert "legacy: haiku/low busy" in capsys.readouterr().out


def test_run_turn_records_the_result_and_clears_the_launcher_fields(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save({"w": worker(status="busy", launcher_pid=1, current_log="003.jsonl")})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: [sys.executable, "-c", FAKE_CLAUDE])
    assert cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=3) == 0
    stored = cw.load()["w"]
    assert stored["status"] == "idle" and stored["context_tokens"] == 125 and stored["turns"] == 1
    assert "launcher_pid" not in stored and "current_log" not in stored
    assert (reg / "jobs" / "w" / "003.result.md").read_text(encoding="utf-8") == "done"


def test_a_launcher_that_cannot_start_the_worker_leaves_it_in_error_not_busy(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save({"w": worker(status="busy", launcher_pid=os.getpid(), current_log="001.jsonl")})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["definitely-not-a-program-xyz"])
    with pytest.raises(OSError):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    stored = cw.load()["w"]
    assert stored["status"].startswith("error") and "launcher_pid" not in stored


def test_an_interrupted_launcher_kills_the_worker_and_records_the_error(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, float | None]] = []

    class FakeProc:
        pid = 4242

        def wait(self, timeout: float) -> int:
            raise KeyboardInterrupt

    cw.save({"w": worker(status="busy", launcher_pid=os.getpid())})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["x"])
    monkeypatch.setattr(cw.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(cw, "own_start", lambda pid: 1234.5)
    monkeypatch.setattr(cw, "kill_tree", lambda pid, created: killed.append((pid, created)) or True)
    with pytest.raises(KeyboardInterrupt):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    assert killed == [(4242, 1234.5)]  # the PID together with the creation time recorded for it
    assert cw.load()["w"]["status"] == "error KeyboardInterrupt"


@pytest.fixture
def send_env(reg: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[tuple[dict, int]]]:
    runs: list[tuple[dict, int]] = []
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    monkeypatch.setattr(
        cw,
        "run_turn",
        lambda name, w, *a, log_number, **k: runs.append((w, log_number)) or 0,
    )
    msg = reg / "m.md"
    msg.write_text("hi", encoding="utf-8")
    return msg, runs


def send(monkeypatch: pytest.MonkeyPatch, msg: Path) -> int:
    monkeypatch.setattr(
        "sys.argv", ["cli_worker.py", "send", "--name", "w", "--message-file", str(msg)]
    )
    return cw.main()


def test_send_refuses_a_busy_worker_whose_launcher_is_alive(
    reg: Path, send_env: tuple[Path, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, runs = send_env
    cw.save({"w": worker(status="busy", **ours(os.getpid(), "launcher"))})
    with pytest.raises(SystemExit, match="not available"):
        send(monkeypatch, msg)
    cw.save({"w": worker(status="busy")})  # legacy entry without a PID: cannot be judged dead
    with pytest.raises(SystemExit, match="not available"):
        send(monkeypatch, msg)
    assert runs == []


def test_send_resumes_a_busy_worker_whose_launcher_died(
    reg: Path,
    send_env: tuple[Path, list],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    msg, runs = send_env
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    cw.save({"w": worker(status="busy", launcher_pid=dead.pid, turns=2)})
    (reg / "jobs" / "w").mkdir(parents=True)
    (reg / "jobs" / "w" / "002.jsonl").write_text("", encoding="utf-8")
    assert send(monkeypatch, msg) == 0
    assert "launcher" in capsys.readouterr().out
    stored = cw.load()["w"]
    assert runs[0][1] == 3 and stored["launcher_pid"] == os.getpid()
    assert stored["current_log"] == "003.jsonl"


def test_a_retired_worker_cannot_be_sent_to(
    reg: Path, send_env: tuple[Path, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = send_env
    cw.save({"w": worker(status="retired")})
    with pytest.raises(SystemExit, match="not available"):
        send(monkeypatch, msg)


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class Sleeper:
    """A real process standing in for a worker that outlives its launcher; stopped by its recorded PID."""

    def __enter__(self) -> int:
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        return self.proc.pid

    def __exit__(self, *exc: object) -> None:
        self.proc.kill()
        self.proc.wait()


def argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["cli_worker.py", *args])
    return cw.main()


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "many"},
        {"input_tokens": [1]},
        {"input_tokens": float("inf")},
        {"cache_read_input_tokens": {"a": 1}},
        {"input_tokens": float("nan")},
    ],
)
def test_context_of_returns_none_for_malformed_counts(usage: dict) -> None:
    assert cw.context_of(usage) is None
    assert cw.context_of({"input_tokens": 3, "cache_read_input_tokens": None}) == 3


def test_live_log_skips_non_object_lines_and_malformed_usage(reg: Path) -> None:
    log = reg / "001.jsonl"
    bad_usage = json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": "x"}}})
    odd = ["[1, 2]", '"text"', "null", "7", json.dumps({"type": "assistant", "message": [1]})]
    odd += [json.dumps({"type": "assistant", "message": {"usage": 5}}), '{"type": "assistant"']
    log.write_text("\n".join([assistant(321), *odd, bad_usage]) + "\n", encoding="utf-8")
    assert cw.live_log(log)["live_context_tokens"] == 321  # falls back to the last well-formed call


def test_live_log_ignores_a_non_finite_count_and_a_deeply_nested_line(reg: Path) -> None:
    log = reg / "001.jsonl"
    infinite = '{"type": "assistant", "message": {"usage": {"input_tokens": Infinity}}}'
    deep = "[" * 100000 + "]" * 100000
    log.write_text("\n".join([assistant(9), infinite, deep]) + "\n", encoding="utf-8")
    assert cw.live_log(log)["live_context_tokens"] == 9


def test_live_log_reads_a_line_holding_a_raw_line_separator(reg: Path) -> None:
    log = reg / "001.jsonl"
    event = {
        "type": "assistant",
        "message": {"usage": {"input_tokens": 55}, "content": "a\u2028b\x85c\x0cd"},
    }
    log.write_text(
        "\n" + json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8"
    )  # a splitlines() reader would cut the JSON at U+2028
    assert cw.live_log(log)["live_context_tokens"] == 55


def test_status_survives_a_malformed_log_in_both_formats(
    reg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (reg / "jobs" / "b").mkdir(parents=True)
    (reg / "jobs" / "b" / "001.jsonl").write_text(
        "[1]\nnull\n" + '{"type": "assistant", "message": {"usage": {"input_tokens": "x"}}}\n',
        encoding="utf-8",
    )
    cw.save({"b": worker(status="busy", launcher_pid=os.getpid(), current_log="001.jsonl")})
    assert argv(monkeypatch, "status") == 0
    assert "b: haiku/low busy" in capsys.readouterr().out
    assert argv(monkeypatch, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["live_context_tokens"] is None


def test_status_tolerates_damaged_pid_fields(
    reg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save(
        {
            "a": worker(status="busy", launcher_pid="x", worker_pid=-3),
            "b": worker(status="busy", launcher_pid=True, worker_pid=[1]),
        }
    )
    assert argv(monkeypatch, "status") == 0
    assert "launcher=unknown" in capsys.readouterr().out


def test_run_turn_survives_a_malformed_stream_and_records_only_valid_fields(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = (
        "import json\n"
        "print('[1]'); print('null'); print('{broken')\n"
        "print(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 'x'}}}))\n"
        "print(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 7}}}))\n"
        "print(json.dumps({'type': 'result', 'result': ['not text'], 'total_cost_usd': 'free'}))\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'total_cost_usd': 0.25}))\n"
    )
    cw.save({"w": worker(status="busy", launcher_pid=os.getpid())})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: [sys.executable, "-c", script])
    assert cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1) == 0
    stored = cw.load()["w"]
    assert stored["context_tokens"] == 7 and stored["last_cost_usd"] == 0.25
    assert (reg / "jobs" / "w" / "001.result.md").read_text(encoding="utf-8") == "ok"


def test_run_turn_records_the_worker_pid_while_it_runs_and_clears_it_after(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    class FakeProc:
        pid = 777777

        def wait(self, timeout: float) -> int:
            seen.append(cw.load()["w"].get("worker_pid"))
            return 0

    cw.save({"w": worker(status="busy", launcher_pid=os.getpid())})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["x"])
    monkeypatch.setattr(cw.subprocess, "Popen", lambda *a, **k: FakeProc())
    assert cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1) == 0
    assert seen == [777777] and "worker_pid" not in cw.load()["w"]


def test_a_failed_turn_clears_the_worker_pid_too(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save({"w": worker(status="busy", launcher_pid=os.getpid(), worker_pid=5)})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["definitely-not-a-program-xyz"])
    with pytest.raises(OSError):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    assert set(cw.load()["w"]).isdisjoint({"launcher_pid", "worker_pid"})


def test_run_turn_does_not_write_into_an_entry_that_was_replaced_meanwhile(
    reg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeProc:
        pid = 888888

        def wait(
            self, timeout: float
        ) -> int:  # retired and restarted under the same name during the turn
            cw.save({"w": worker(session_id="new-sid", status="busy", launcher_pid=1, turns=0)})
            return 0

    cw.save({"w": worker(status="busy", launcher_pid=os.getpid())})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["x"])
    monkeypatch.setattr(cw.subprocess, "Popen", lambda *a, **k: FakeProc())
    assert cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1) == 0
    entry = cw.load()["w"]
    assert entry["session_id"] == "new-sid" and entry["status"] == "busy" and entry["turns"] == 0
    assert "result not recorded" in capsys.readouterr().out
    assert (reg / "jobs" / "w" / "001.result.md").exists()


def test_the_error_path_does_not_touch_an_entry_of_another_session(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProc:
        pid = 999999

        def wait(self, timeout: float) -> int:
            cw.save({"w": worker(session_id="new-sid", status="busy", launcher_pid=1)})
            raise KeyboardInterrupt

    cw.save({"w": worker(status="busy", launcher_pid=os.getpid())})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["x"])
    monkeypatch.setattr(cw.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(cw, "own_start", lambda pid: 1234.5)
    monkeypatch.setattr(cw, "kill_tree", lambda pid, created: True)
    with pytest.raises(KeyboardInterrupt):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    assert cw.load()["w"]["status"] == "busy" and cw.load()["w"]["launcher_pid"] == 1


def test_retire_refuses_a_worker_whose_launcher_or_child_still_runs(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save({"w": worker(status="busy", **ours(os.getpid(), "launcher"))})
    with pytest.raises(SystemExit, match="still runs"):
        argv(monkeypatch, "retire", "--name", "w")
    with pytest.raises(SystemExit, match="still runs"):
        argv(
            monkeypatch, "retire", "--name", "w", "--force"
        )  # force never overrides a live process
    with Sleeper() as child:
        cw.save({"w": worker(status="busy", launcher_pid=dead_pid(), **ours(child, "worker"))})
        with pytest.raises(SystemExit, match=f"worker_pid {child}"):
            argv(monkeypatch, "retire", "--name", "w")
        assert cw.load()["w"]["status"] == "busy"


def test_retire_of_an_idle_or_dead_worker_succeeds_and_clears_the_pids(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = dead_pid()
    cw.save(
        {
            "idle": worker(),
            "dead": worker(
                status="busy", launcher_pid=gone, worker_pid=gone, current_log="1.jsonl"
            ),
        }
    )
    assert argv(monkeypatch, "retire", "--name", "idle") == 0
    assert argv(monkeypatch, "retire", "--name", "dead") == 0
    entry = cw.load()["dead"]
    assert entry["status"] == "retired"
    assert set(entry).isdisjoint({"launcher_pid", "worker_pid", "current_log"})


def test_retire_of_a_busy_entry_without_a_pid_needs_force(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.save({"w": worker(status="busy")})
    with pytest.raises(SystemExit, match="--force"):
        argv(monkeypatch, "retire", "--name", "w")
    assert argv(monkeypatch, "retire", "--name", "w", "--force") == 0
    assert cw.load()["w"]["status"] == "retired"


def test_retire_of_an_unknown_name_fails(reg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cw.save({})
    with pytest.raises(SystemExit, match="not registered"):
        argv(monkeypatch, "retire", "--name", "nobody")


def test_a_retired_name_cannot_be_started_again_while_its_process_lives(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = reg / "m.md"
    msg.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    monkeypatch.setattr(cw, "run_turn", lambda *a, **k: 0)
    monkeypatch.setattr(cw, "ROOT", reg)
    (reg / "wt").mkdir()
    start = [
        "start", "--name", "w", "--model", "haiku", "--effort", "low", "--cwd", str(reg / "wt"),
        "--message-file", str(msg), "--cache-ttl", "5m",
    ]  # fmt: skip
    with Sleeper() as child:
        cw.save(
            {"w": worker(status="retired", **ours(child, "worker"))}
        )  # e.g. retired by an older version
        with pytest.raises(SystemExit, match="cannot be reused"):
            argv(monkeypatch, *start)
        assert cw.load()["w"]["session_id"] == "sid"
    assert argv(monkeypatch, *start) == 0  # the process ended: the name is free
    assert cw.load()["w"]["session_id"] != "sid"


def test_send_refuses_while_the_worker_process_lives_even_if_the_launcher_died(
    reg: Path, send_env: tuple[Path, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, runs = send_env
    with Sleeper() as child:
        cw.save({"w": worker(status="busy", launcher_pid=dead_pid(), **ours(child, "worker"))})
        with pytest.raises(SystemExit, match=f"worker_pid {child} still runs"):
            send(monkeypatch, msg)
        cw.save(
            {"w": worker(status="idle", **ours(child, "worker"))}
        )  # damaged idle entry with a live child
        with pytest.raises(SystemExit, match="still runs"):
            send(monkeypatch, msg)
    assert runs == []


def test_send_resumes_when_launcher_and_worker_are_both_gone(
    reg: Path, send_env: tuple[Path, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, runs = send_env
    gone = dead_pid()
    cw.save({"w": worker(status="busy", launcher_pid=gone, worker_pid=gone)})
    assert send(monkeypatch, msg) == 0 and len(runs) == 1


def test_status_shows_whether_the_worker_process_is_alive(
    reg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with Sleeper() as child:
        cw.save(
            {
                "orphan": worker(status="busy", launcher_pid=dead_pid(), **ours(child, "worker")),
                "ended": worker(status="busy", launcher_pid=dead_pid(), worker_pid=dead_pid()),
            }
        )
        assert argv(monkeypatch, "status") == 0
        text = capsys.readouterr().out
        assert "launcher=DEAD worker=alive" in text.split("orphan:")[1].split("\n")[0]
        assert "launcher=DEAD worker=DEAD" in text.split("ended:")[1].split("\n")[0]
        assert argv(monkeypatch, "status", "--json") == 0
        rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
        assert rows["orphan"]["worker_alive"] is True and rows["ended"]["worker_alive"] is False


def test_a_live_lock_owner_is_never_broken_however_old_the_lock(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.LOCK.write_text(f"{os.getpid()} tok", encoding="ascii")
    old = time.time() - 10 * cw.STALE_LOCK_SECONDS
    os.utime(cw.LOCK, (old, old))
    monkeypatch.setattr(cw.time, "sleep", lambda _s: None)
    with pytest.raises(SystemExit, match=f"owner pid {os.getpid()}"), cw.RegistryLock():
        pass
    assert cw.LOCK.read_text(encoding="ascii") == f"{os.getpid()} tok"


def test_a_dead_owner_lock_is_broken_at_once_even_when_fresh(reg: Path) -> None:
    cw.LOCK.write_text(f"{dead_pid()} tok", encoding="ascii")
    with cw.RegistryLock():
        assert cw.LOCK.read_text(encoding="ascii").split(" ")[0] == str(os.getpid())
    assert not cw.LOCK.exists() and not list(reg.glob("*.stale-*"))


def test_an_ownerless_lock_is_broken_by_age_only(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cw.time, "sleep", lambda _s: None)
    cw.LOCK.write_text("garbage", encoding="ascii")
    with pytest.raises(SystemExit, match="lock busy"), cw.RegistryLock():
        pass
    old = time.time() - cw.STALE_LOCK_SECONDS - 5
    os.utime(cw.LOCK, (old, old))
    with cw.RegistryLock():
        pass


def test_break_stale_puts_back_a_lock_created_after_the_judgement(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw.LOCK.write_text(f"{dead_pid()} old", encoding="ascii")
    old = time.time() - 100
    os.utime(cw.LOCK, (old, old))
    real = os.replace
    swapped: list[int] = []

    def racing(src: object, dst: object) -> None:
        if Path(str(src)) == cw.LOCK and not swapped:
            swapped.append(1)  # another process breaks the dead lock and takes a fresh one first
            cw.LOCK.unlink()
            cw.LOCK.write_text(f"{os.getpid()} fresh", encoding="ascii")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(cw.os, "replace", racing)
    cw.RegistryLock.break_stale()
    assert swapped and cw.LOCK.read_text(encoding="ascii") == f"{os.getpid()} fresh"
    assert not list(reg.glob("*.stale-*"))


def test_exit_removes_only_its_own_lock(reg: Path) -> None:
    with cw.RegistryLock():
        pass
    assert not cw.LOCK.exists()
    with cw.RegistryLock():
        cw.LOCK.write_text(f"{os.getpid()} somebody-else", encoding="ascii")  # a breaker misjudged
    assert cw.LOCK.read_text(encoding="ascii").endswith("somebody-else")
    cw.LOCK.unlink()


def test_a_lock_that_cannot_record_its_owner_is_removed_again(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(fd: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(cw.os, "write", failing)
    with pytest.raises(OSError), cw.RegistryLock():
        pass
    assert not cw.LOCK.exists()


@pytest.mark.parametrize(
    ("raises", "expected"), [(None, True), (ProcessLookupError, False), (PermissionError, True)]
)
def test_pid_alive_on_posix_uses_signal_zero(
    monkeypatch: pytest.MonkeyPatch, raises: type[OSError] | None, expected: bool
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if raises is not None:
            raise raises

    monkeypatch.setattr(cw, "NATIVE_WINDOWS", False)
    # A module-local proxy, never the shared os module (which xdist/subprocess also use).
    from types import SimpleNamespace

    monkeypatch.setattr(cw, "os", SimpleNamespace(kill=fake_kill))
    assert cw.pid_alive(4321) is expected and calls == [(4321, 0)]


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes.wintypes declarations")
@pytest.mark.parametrize(("exit_code", "expected"), [(259, True), (0, False)])
def test_pid_alive_on_windows_declares_the_kernel32_types(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, expected: bool
) -> None:
    """A 64-bit HANDLE must round-trip through OpenProcess/CloseHandle: restype and argtypes are declared."""
    import ctypes
    from ctypes import wintypes

    big_handle = 0x1_0000_0001
    closed: list[int] = []

    class Fn:
        def __init__(self, impl: object) -> None:
            self.impl = impl
            self.restype: object = None
            self.argtypes: object = None

        def __call__(self, *args: object) -> object:
            return self.impl(*args)  # type: ignore[operator]

    def get_exit(_handle: int, ref: object) -> int:
        ref._obj.value = exit_code  # type: ignore[attr-defined]
        return 1

    def close(handle: int) -> int:
        closed.append(handle)
        return 1

    class FakeKernel32:
        def __init__(self, *_a: object, **_k: object) -> None:
            self.OpenProcess = Fn(lambda _acc, _inh, pid: big_handle if pid == 4321 else 0)
            self.GetExitCodeProcess = Fn(get_exit)
            self.CloseHandle = Fn(close)

    made: list[FakeKernel32] = []

    def factory(*a: object, **k: object) -> FakeKernel32:
        made.append(FakeKernel32(*a, **k))
        return made[-1]

    monkeypatch.setattr(ctypes, "WinDLL", factory)
    assert cw.pid_alive(4321) is expected
    assert closed == [big_handle]
    k32 = made[0]
    assert k32.OpenProcess.restype is wintypes.HANDLE
    assert k32.OpenProcess.argtypes == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert k32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert k32.GetExitCodeProcess.argtypes[0] is wintypes.HANDLE  # type: ignore[index]
    assert cw.pid_alive(9999) is False


def test_a_posix_lock_of_a_live_owner_survives_even_when_rename_would_succeed(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On POSIX `rename` works on an open file, so only the owner check protects a stalled holder."""
    cw.LOCK.write_text("4321 tok", encoding="ascii")
    old = time.time() - 10 * cw.STALE_LOCK_SECONDS
    # Labelled POSIX owner/boot doubles: no host API or shared platform string is changed.
    monkeypatch.setattr(cw, "process_start", lambda pid: old - 60)
    monkeypatch.setattr(cw, "boot_time", lambda: old - 120)
    os.utime(cw.LOCK, (old, old))
    cw.RegistryLock.break_stale()
    assert cw.LOCK.read_text(encoding="ascii") == "4321 tok"
    monkeypatch.setattr(cw, "process_start", lambda pid: None)
    cw.RegistryLock.break_stale()
    assert not cw.LOCK.exists()
