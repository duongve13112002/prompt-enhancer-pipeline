"""PID reuse defence of cli_worker.py (decision 503): a recorded PID is a worker only with a matching creation time."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

SCRIPT = Path(__file__).resolve().parents[1] / "cli_worker.py"
spec = importlib.util.spec_from_file_location("cli_worker_identity", SCRIPT)
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


@pytest.fixture(autouse=True)
def no_brief_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests start workers with one-word briefs; the lint gate has its own tests."""
    monkeypatch.setattr(cw, "lint_gate", lambda *args, **kwargs: None)


TOL = cw.PROCESS_TIME_TOLERANCE_SECONDS


@pytest.fixture
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    return tmp_path


def worker(**extra: object) -> dict[str, Any]:
    return {
        "model": "haiku",
        "effort": "low",
        "session_id": "sid",
        "cwd": ".",
        "status": "busy",
        "turns": 0,
        **extra,
    }


def iso(offset_seconds: float) -> str:
    """A `last_used` value `offset_seconds` from now (the registry writes naive local time)."""
    moment = dt.datetime.now() + dt.timedelta(seconds=offset_seconds)
    return moment.isoformat(timespec="seconds")


class Sleeper:
    """A real process standing in for a recorded one; stopped by the test through its own Popen handle."""

    def __enter__(self) -> Sleeper:
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.pid = self.proc.pid
        self.created = cw.process_start(self.pid)
        assert self.created is not None
        return self

    def __exit__(self, *exc: object) -> None:
        self.proc.kill()
        self.proc.wait()


def argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["cli_worker.py", *args])
    return int(cw.main())


def test_process_start_reads_the_creation_time_of_a_running_process() -> None:
    before = time.time()
    with Sleeper() as child:
        assert child.created is not None
        assert before - TOL <= child.created <= time.time() + TOL
        assert cw.process_start(child.pid) == child.created  # stable between reads
    assert cw.process_start(child.pid) is None  # exited and reaped
    mine = cw.process_start(os.getpid())
    assert mine is not None and mine < before


def test_a_reused_pid_with_another_creation_time_is_not_ours_and_is_never_killed() -> None:
    with Sleeper() as child:
        assert child.created is not None
        for wrong in (child.created - 3600, child.created + 3600, child.created - TOL - 0.5):
            assert cw.process_state(child.pid, wrong) == cw.GONE
            assert cw.kill_tree(child.pid, wrong) is False
        assert cw.kill_tree(child.pid, None) is False  # nothing recorded: nothing provable
        assert child.proc.poll() is None  # still running after every refused kill
        entry = {"worker_pid": child.pid, "worker_created": child.created - 3600}
        assert cw.live_processes(entry) == []
        assert cw.entry_process_state(entry, "worker") == (child.pid, cw.GONE)


def test_the_same_pid_and_creation_time_is_ours_within_the_tolerance() -> None:
    with Sleeper() as child:
        assert child.created is not None
        for shift in (0.0, TOL - 0.1, -(TOL - 0.1)):
            assert cw.process_state(child.pid, child.created + shift) == cw.ALIVE
        entry = {"launcher_pid": child.pid, "launcher_created": child.created}
        assert cw.live_processes(entry) == [f"launcher_pid {child.pid}"]
        assert cw.kill_tree(child.pid, child.created) is True
        child.proc.wait(timeout=20)


@given(delta=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False))
def test_property_alive_exactly_when_the_recorded_time_is_within_the_tolerance(
    delta: float,
) -> None:
    real = 1_700_000_000.0
    with mock.patch.object(cw, "process_start", return_value=real):
        state = cw.process_state(4321, real + delta)
    assert state == (cw.ALIVE if abs(delta) <= TOL else cw.GONE)


@pytest.mark.parametrize("damaged", ["yesterday", True, float("nan"), -5, [1], {}])
def test_a_damaged_recorded_time_is_unknown_never_alive_or_gone(damaged: object) -> None:
    with Sleeper() as child:
        entry = {"worker_pid": child.pid, "worker_created": damaged, "last_used": iso(0)}
        assert cw.entry_process_state(entry, "worker") == (child.pid, cw.UNKNOWN)


def test_legacy_launcher_is_judged_against_its_own_turn_start() -> None:
    with Sleeper() as child:
        entry = {"launcher_pid": child.pid}
        # The launcher wrote last_used after it started, so a process created before it can be it ...
        assert cw.entry_process_state({**entry, "last_used": iso(+60)}, "launcher")[1] == cw.ALIVE
        # ... and one created after it cannot (a reused PID).
        assert cw.entry_process_state({**entry, "last_used": iso(-3600)}, "launcher")[1] == cw.GONE
        # No comparable time: unknown.
        assert cw.entry_process_state(entry, "launcher")[1] == cw.UNKNOWN
        assert cw.entry_process_state({**entry, "last_used": "never"}, "launcher")[1] == cw.UNKNOWN


def test_legacy_worker_is_gone_before_its_turn_and_unknown_after() -> None:
    with Sleeper() as child:
        entry = {"worker_pid": child.pid}
        # The worker starts after last_used: a process older than that is not it.
        assert cw.entry_process_state({**entry, "last_used": iso(+3600)}, "worker")[1] == cw.GONE
        # A younger one cannot be told from a reused PID.
        assert cw.entry_process_state({**entry, "last_used": iso(-3600)}, "worker")[1] == cw.UNKNOWN
        assert cw.entry_process_state(entry, "worker")[1] == cw.UNKNOWN


def test_no_recorded_pid_has_no_state_and_a_missing_process_is_gone() -> None:
    assert cw.entry_process_state({}, "launcher") == (None, cw.NO_PID)
    assert cw.entry_process_state({"worker_pid": "x"}, "worker") == (None, cw.NO_PID)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert cw.entry_process_state({"worker_pid": dead.pid}, "worker")[1] == cw.GONE


def test_status_shows_unknown_for_an_unverifiable_pid_and_gone_for_a_reused_one(
    reg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with Sleeper() as child:
        assert child.created is not None
        cw.save(
            {
                "legacy": worker(launcher_pid=child.pid),
                "reused": worker(launcher_pid=child.pid, launcher_created=child.created - 3600),
                "ours": worker(launcher_pid=child.pid, launcher_created=child.created),
            }
        )
        assert argv(monkeypatch, "status") == 0
        text = capsys.readouterr().out
        assert "launcher=unknown" in text.split("legacy:")[1].split("\n")[0]
        assert "launcher=DEAD" in text.split("reused:")[1].split("\n")[0]
        assert "launcher=alive" in text.split("ours:")[1].split("\n")[0]
        assert argv(monkeypatch, "status", "--json") == 0
        rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
        assert [rows[n]["launcher_state"] for n in ("legacy", "reused", "ours")] == [
            "unknown",
            "gone",
            "alive",
        ]
        assert [rows[n]["launcher_alive"] for n in ("legacy", "reused", "ours")] == [
            None,
            False,
            True,
        ]


@pytest.fixture
def send_env(reg: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []

    def fake_turn(name: str, w: dict[str, Any], *args: object, **kwargs: object) -> int:
        runs.append(w)
        return 0

    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    monkeypatch.setattr(cw, "run_turn", fake_turn)
    msg = reg / "m.md"
    msg.write_text("hi", encoding="utf-8")
    return msg, runs


def send(monkeypatch: pytest.MonkeyPatch, msg: Path, *extra: str) -> int:
    return argv(monkeypatch, "send", "--name", "w", "--message-file", str(msg), *extra)


def test_send_refuses_an_unverifiable_pid_with_the_remedy_and_resumes_once_it_is_checked(
    reg: Path, send_env: tuple[Path, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, runs = send_env
    with Sleeper() as child:
        cw.save(
            {"w": worker(launcher_pid=child.pid)}
        )  # legacy entry: no creation time, no last_used
        with pytest.raises(SystemExit) as refused:
            send(monkeypatch, msg)
        text = str(refused.value)
        assert f"launcher_pid {child.pid}" in text and "--pid-checked" in text
        assert "retire --name w --force" in text and "\n" not in text
        assert runs == [] and cw.load()["w"]["launcher_pid"] == child.pid
        assert send(monkeypatch, msg, "--pid-checked") == 0
    assert len(runs) == 1
    stored = cw.load()["w"]
    assert stored["launcher_pid"] == os.getpid()
    assert stored["launcher_created"] == pytest.approx(cw.process_start(os.getpid()))


def test_send_resumes_when_the_recorded_pid_was_reused_by_another_program(
    reg: Path,
    send_env: tuple[Path, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    msg, runs = send_env
    with Sleeper() as stranger:
        assert stranger.created is not None
        cw.save(
            {
                "w": worker(
                    launcher_pid=stranger.pid,
                    launcher_created=stranger.created - 86400,  # recorded before the reboot
                    worker_pid=stranger.pid,
                    worker_created=stranger.created - 86400,
                )
            }
        )
        assert send(monkeypatch, msg) == 0
        assert stranger.proc.poll() is None  # the unrelated program was neither stopped nor adopted
    assert "is gone; resuming" in capsys.readouterr().out
    stored = cw.load()["w"]
    assert "worker_pid" not in stored and "worker_created" not in stored
    assert stored["launcher_pid"] == os.getpid() and len(runs) == 1


def test_retire_needs_force_for_an_unverifiable_pid_and_never_overrides_a_proven_one(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Sleeper() as child:
        assert child.created is not None
        cw.save({"w": worker(launcher_pid=child.pid)})
        with pytest.raises(SystemExit, match="--pid-checked"):
            argv(monkeypatch, "retire", "--name", "w")
        assert cw.load()["w"]["status"] == "busy"
        assert argv(monkeypatch, "retire", "--name", "w", "--force") == 0
        stored = cw.load()["w"]
        assert stored["status"] == "retired" and "launcher_pid" not in stored
        cw.save({"w": worker(worker_pid=child.pid, worker_created=child.created)})
        with pytest.raises(SystemExit, match="still runs"):
            argv(monkeypatch, "retire", "--name", "w", "--force")


def test_retire_of_a_reused_pid_succeeds_and_drops_the_creation_times(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Sleeper() as stranger:
        assert stranger.created is not None
        cw.save(
            {
                "w": worker(
                    launcher_pid=stranger.pid,
                    launcher_created=stranger.created - 5000,
                    worker_pid=stranger.pid,
                    worker_created=stranger.created - 5000,
                )
            }
        )
        assert argv(monkeypatch, "retire", "--name", "w") == 0
    stored = cw.load()["w"]
    assert set(stored).isdisjoint(cw.PROCESS_KEYS)


def test_a_retired_name_with_an_unverifiable_pid_is_not_reused(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg = reg / "m.md"
    msg.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    monkeypatch.setattr(cw, "run_turn", lambda *a, **k: 0)
    monkeypatch.setattr(cw, "ROOT", reg)
    (reg / "wt").mkdir()
    with Sleeper() as child:
        cw.save({"w": worker(status="retired", worker_pid=child.pid)})
        with pytest.raises(SystemExit, match="--pid-checked"):
            argv(
                monkeypatch,
                *["start", "--name", "w", "--model", "haiku", "--effort", "low"],
                *["--cwd", str(reg / "wt"), "--message-file", str(msg), "--cache-ttl", "5m"],
            )
        assert cw.load()["w"]["session_id"] == "sid"


def test_run_turn_records_the_worker_creation_time_and_stops_only_that_identity(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    stopped: list[tuple[int, float | None]] = []

    class FakeProc:
        pid = 777777

        def wait(self, timeout: float) -> int:
            seen.append(cw.load()["w"].get("worker_created"))
            raise subprocess.TimeoutExpired("x", timeout)

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            raise AssertionError("the identity is recorded: the verified tree kill must be used")

    cw.save({"w": worker()})
    monkeypatch.setattr(cw, "build_command", lambda w, m, r: ["x"])
    monkeypatch.setattr(cw.subprocess, "Popen", lambda *a, **k: FakeProc())

    def fake_kill(pid: int, created: float | None) -> bool:
        stopped.append((pid, created))
        return True

    monkeypatch.setattr(cw, "own_start", lambda pid: 55.5)
    monkeypatch.setattr(cw, "kill_tree", fake_kill)
    cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    assert seen == [55.5] and stopped == [(777777, 55.5)]
    assert set(cw.load()["w"]).isdisjoint(cw.PROCESS_KEYS)


def test_without_a_readable_identity_only_the_popen_child_is_killed_by_its_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProc:
        pid = 31337

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            calls.append("kill")

    def refused_kill(pid: int, created: float | None) -> bool:
        calls.append("tree")
        return False

    monkeypatch.setattr(cw, "kill_tree", refused_kill)
    cw.stop_worker(cast("subprocess.Popen[bytes]", FakeProc()), None)
    assert calls == ["tree", "kill"]


STAT = (
    "1234 (my (weird) proc) S 1 1234 1234 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 5000 12345678 "
    + " ".join(["0"] * 30)
)


def test_linux_stat_parsing_counts_fields_after_the_last_parenthesis() -> None:
    assert cw.parse_stat_start(STAT, 1_700_000_000, 100) == 1_700_000_050.0
    assert cw.parse_stat_start(STAT.replace("5000", "123456"), 10, 1000) == 10 + 123.456


def test_linux_stat_parsing_treats_a_zombie_as_exited_and_refuses_damage() -> None:
    assert cw.parse_stat_start(STAT.replace(") S ", ") Z "), 1, 100) is None
    for damaged in ("", "1 (x) S 1 2", STAT.replace("5000", "soon")):
        with pytest.raises(cw.IdentityUnavailable):
            cw.parse_stat_start(damaged, 1, 100)
    with pytest.raises(cw.IdentityUnavailable):
        cw.parse_stat_start(STAT, 1, 0)


def test_posix_process_start_reads_a_procfs_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cw.os, "sysconf", lambda name: 100, raising=False)
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "stat").write_text("cpu 1 2\nbtime 1700000000\nprocesses 9\n", encoding="utf-8")
    (proc / "42" / "stat").write_text(STAT, encoding="utf-8")
    assert cw.posix_process_start(42, proc) == 1_700_000_050.0
    assert cw.posix_process_start(43, proc) is None
    (proc / "stat").write_text("cpu 1 2\n", encoding="utf-8")
    with pytest.raises(cw.IdentityUnavailable):
        cw.posix_process_start(42, proc)


def test_posix_process_start_without_procfs_knows_existence_but_not_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(cw.IdentityUnavailable):
        cw.posix_process_start(os.getpid(), tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert cw.posix_process_start(dead.pid, tmp_path) is None


def test_filetime_conversion_maps_the_unix_epoch_to_zero() -> None:
    assert cw.filetime_to_epoch(cw.WINDOWS_EPOCH_OFFSET_100NS) == 0.0
    assert cw.filetime_to_epoch(cw.WINDOWS_EPOCH_OFFSET_100NS + 10_000_000) == 1.0


def test_an_unreadable_identity_is_unknown_not_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(pid: int) -> float:
        raise cw.IdentityUnavailable("no")

    monkeypatch.setattr(cw, "process_start", refuse)
    assert cw.process_state(1, 5.0) == cw.UNKNOWN
    assert cw.kill_tree(1, 5.0) is False
    assert cw.own_start(1) is None


def write_lock(reg: Path, text: str, age_seconds: float = 0.0) -> None:
    cw.LOCK.write_text(text, encoding="ascii")
    stamp = time.time() - age_seconds
    os.utime(cw.LOCK, (stamp, stamp))


def test_a_lock_records_its_owner_creation_time(reg: Path) -> None:
    with cw.RegistryLock():
        pid, token, created = cw.LOCK.read_text(encoding="ascii").split()
    assert int(pid) == os.getpid() and token
    assert float(created) == pytest.approx(cw.process_start(os.getpid()))
    assert not cw.LOCK.exists()


def test_a_lock_of_a_reused_pid_is_broken_and_acquired_at_once(reg: Path) -> None:
    with Sleeper() as stranger:
        assert stranger.created is not None
        write_lock(
            reg, f"{stranger.pid} tok {stranger.created - 86400!r}"
        )  # written before the reboot
        with cw.RegistryLock():
            assert cw.LOCK.read_text(encoding="ascii").startswith(f"{os.getpid()} ")
        assert stranger.proc.poll() is None


def test_a_lock_of_its_real_owner_is_never_broken_however_old(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Sleeper() as owner:
        write_lock(
            reg, f"{owner.pid} tok {owner.created!r}", age_seconds=10 * cw.STALE_LOCK_SECONDS
        )
        monkeypatch.setattr(cw.time, "sleep", lambda _s: None)
        cw.RegistryLock.break_stale()
        assert cw.LOCK.exists()
        with pytest.raises(SystemExit, match=f"owner pid {owner.pid}"), cw.RegistryLock():
            pass


def test_a_legacy_lock_with_a_running_pid_is_held_only_if_written_after_the_boot(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cw, "boot_time", lambda: time.time() - 600)
    with Sleeper() as other:
        write_lock(
            reg, f"{other.pid} tok", age_seconds=3600
        )  # older than the boot: no owner can survive it
        cw.RegistryLock.break_stale()
        assert not cw.LOCK.exists()
        write_lock(reg, f"{other.pid} tok", age_seconds=60)  # written since the boot
        cw.RegistryLock.break_stale()
        assert cw.LOCK.exists()


def test_a_legacy_lock_is_held_when_the_boot_time_is_unknown_and_a_damaged_time_is_legacy(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Sleeper() as other:
        monkeypatch.setattr(cw, "boot_time", lambda: None)
        write_lock(reg, f"{other.pid} tok", age_seconds=10**6)
        cw.RegistryLock.break_stale()
        assert cw.LOCK.exists()
        monkeypatch.setattr(cw, "boot_time", lambda: time.time() - 600)
        write_lock(reg, f"{other.pid} tok soon", age_seconds=3600)
        cw.RegistryLock.break_stale()
        assert not cw.LOCK.exists()


def test_a_lock_whose_owner_identity_is_unreadable_is_held(
    reg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(pid: int) -> float:
        raise cw.IdentityUnavailable("no")

    monkeypatch.setattr(cw, "process_start", refuse)
    write_lock(reg, f"{os.getpid()} tok 123.5", age_seconds=10**6)
    cw.RegistryLock.break_stale()
    assert cw.LOCK.exists()


def test_a_lock_of_a_dead_owner_is_still_broken(reg: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_lock(reg, f"{dead.pid} tok 1.0")
    cw.RegistryLock.break_stale()
    assert not cw.LOCK.exists()


def test_boot_time_precedes_this_process_and_reads_procfs_btime(tmp_path: Path) -> None:
    booted = cw.boot_time()
    started = cw.process_start(os.getpid())
    assert booted is not None and started is not None and booted <= started + TOL
    (tmp_path / "stat").write_text("cpu 1\nbtime 1700000000\n", encoding="utf-8")
    assert cw.proc_btime(tmp_path) == 1_700_000_000
    assert cw.proc_btime(tmp_path / "missing") is None
