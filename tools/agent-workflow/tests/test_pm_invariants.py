"""hooks/pm_invariants.py (PostToolUse in the PM session, decision 503): four rules, rate limit, config, fail-open.

Everything runs against a fake project root under tmp_path with a fake process list; the real process listing is
exercised by one test that starts and stops its own child process.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pm_invariants as pi
import pytest
from conftest import git
from hypothesis import assume, given
from hypothesis import strategies as st

HOOK = Path(pi.__file__).resolve()
SHIPPED = pi.DEFAULT_CONFIG
NOW = time.time()
NOTHING: list[str] = ["python -m http.server", "code.exe"]
FOREIGN = "D:\\Other\\Project"  # another project on the same machine (test-only path)


def sup(root: Path) -> str:
    """A supervisor command line that names the project, as an absolute-path start does."""
    tool = os.path.join(str(root), "tools", "agent-workflow", "pm-supervisor.ps1")
    return f"powershell.exe -NoProfile -File {tool} -ExitOnEvent"


def watcher(root: Path) -> str:
    return f"pwsh -File {os.path.join(str(root), '.tmp', 'pm-scripts', 'ci_wait_private.ps1')} -Run 123"


def procs(*lines: str) -> pi.Processes:
    return lambda _timeout: list(lines)


def no_list(_timeout: int) -> list[str] | None:
    return None


@pytest.fixture(autouse=True)
def _git_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tmp_path may sit inside a work tree: git must not find the enclosing repository from a fake project."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


@pytest.fixture
def world(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".tmp" / "pm-state").mkdir(parents=True)
    return root


def state(root: Path) -> Path:
    return root / ".tmp" / "pm-state"


def config(tmp_path: Path, **override: Any) -> Path:
    data = json.loads(SHIPPED.read_text(encoding="utf-8"))
    data.update(override)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def go(
    root: Path,
    cfg: Path = SHIPPED,
    now: float = NOW,
    processes: pi.Processes | None = None,
    event: dict[str, Any] | None = None,
) -> str:
    listing = processes or procs()
    return pi.run(event or {"tool_name": "Bash"}, cfg, root, state(root), now, listing)


def busy_worker(root: Path, pid: int | None = os.getpid()) -> None:
    entry: dict[str, Any] = {"status": "busy", "label": "T"}
    if pid is not None:
        entry["launcher_pid"] = pid
    (state(root) / "cli-workers.json").write_text(
        json.dumps({"dev-1": entry, "old": {"status": "retired"}}), encoding="utf-8"
    )


def note(root: Path, age_minutes: float) -> None:
    path = state(root) / "pm-continuation.md"
    path.write_text("note", encoding="utf-8")
    stamp = NOW - age_minutes * 60
    os.utime(path, (stamp, stamp))


def result(root: Path, name: str, age_minutes: float = 1, job: str = "dev-1") -> Path:
    path = root / ".tmp" / "cli-jobs" / job / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("done", encoding="utf-8")
    stamp = NOW - age_minutes * 60
    os.utime(path, (stamp, stamp))
    return path


def read_event(path: Path | str, cwd: Path | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
    if cwd:
        event["cwd"] = str(cwd)
    return event


def quiet(root: Path, **kw: Any) -> None:
    assert go(root, **kw) == ""


def forget_check(root: Path) -> None:
    (state(root) / pi.STATE_FILE).unlink(missing_ok=True)


# -- silence ----------------------------------------------------------------------------------------------------


def test_idle_project_prints_nothing(world: Path) -> None:
    quiet(world)
    assert (state(world) / pi.STATE_FILE).is_file()


# -- rule (a): supervisor ----------------------------------------------------------------------------------------


def test_busy_worker_without_supervisor_says_start_it(world: Path) -> None:
    busy_worker(world)
    note(world, 1)
    out = go(world, processes=procs(*NOTHING))
    assert "start the supervisor" in out
    assert "cli worker dev-1" in out
    assert out.count("[pm-invariants]") == 1


def test_supervisor_process_silences_rule_a(world: Path) -> None:
    busy_worker(world)
    note(world, 1)
    quiet(world, processes=procs(*NOTHING, sup(world)))


def test_unknown_process_list_is_not_a_violation(world: Path) -> None:
    busy_worker(world)
    note(world, 1)
    quiet(world, processes=no_list)


@pytest.mark.parametrize(
    "line",
    [
        "x-pm-supervisor.ps1.bak",
        "pm-supervisor.ps12",
        "grep pm_supervisor",
        "pm-supervisor",
    ],
)
def test_marker_must_be_a_whole_file_name(world: Path, line: str) -> None:
    busy_worker(world)
    note(world, 1)
    assert "start the supervisor" in go(world, processes=procs(f"{world} {line}"))


def test_marker_matching_is_case_insensitive_and_path_agnostic(world: Path) -> None:
    for line in (
        f'"{world}\\a b\\PM-Supervisor.PS1"',
        f"{world}/tools/pm-supervisor.ps1 -x",
        f"pwsh {world}/pm-supervisor.ps1",
    ):
        busy_worker(world)
        note(world, 1)
        forget_check(world)
        quiet(world, processes=procs(line))


FOREIGN_SUPERVISORS = [
    rf"powershell.exe -NoProfile -File {FOREIGN}\tools\agent-workflow\pm-supervisor.ps1 -ExitOnEvent",
    "pwsh -File .tmp/pm-scripts/pm-supervisor.ps1 -MaxMinutes 45",  # a relative start names no project
    "& .tmp/pm-scripts/pm-supervisor.ps1 -ExitOnEvent",
]


@pytest.mark.parametrize("line", FOREIGN_SUPERVISORS)
def test_a_foreign_or_relative_supervisor_command_line_does_not_count(
    world: Path, line: str
) -> None:
    """The process list is machine-wide: another project's supervisor has the same script name."""
    busy_worker(world)
    note(world, 1)
    assert "start the supervisor" in go(world, processes=procs(*NOTHING, line))


def test_a_root_that_only_starts_like_ours_is_not_ours(world: Path) -> None:
    busy_worker(world)
    note(world, 1)
    lookalike = Path(f"{world}2")
    assert "start the supervisor" in go(world, processes=procs(sup(lookalike)))
    forget_check(world)
    quiet(world, processes=procs(sup(world)))


def heartbeat(
    root: Path, pid: int, created: float | None, name: str = "pm-supervisor.json"
) -> Path:
    """The record pm-supervisor.ps1 writes: pid plus the creation time of that process (epoch seconds)."""
    path = state(root) / name
    body = {"pid": pid, **({} if created is None else {"created": created}), "root": str(root)}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_a_live_heartbeat_counts_without_any_command_line(world: Path) -> None:
    busy_worker(world)
    note(world, 1)
    heartbeat(world, os.getpid(), own_start())
    quiet(world, processes=procs(*NOTHING))
    forget_check(world)
    quiet(world, processes=no_list)  # the record needs no process list at all


def test_a_heartbeat_of_an_exited_supervisor_does_not_count(world: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    created = pi.load_cli_worker().process_start(child.pid)
    child.wait(timeout=30)
    busy_worker(world)
    note(world, 1)
    heartbeat(world, child.pid, created or NOW)
    assert "start the supervisor" in go(world, processes=procs(*NOTHING))


def test_a_heartbeat_whose_pid_was_reused_does_not_count(world: Path) -> None:
    """The PID runs, but it is not the process that wrote the record: PID reuse is not liveness."""
    busy_worker(world)
    note(world, 1)
    heartbeat(world, os.getpid(), own_start() - 3600)
    assert "start the supervisor" in go(world, processes=procs(*NOTHING))


@pytest.mark.parametrize("created", [None, "5", True, -1.0])
def test_a_heartbeat_without_a_usable_creation_time_does_not_count(
    world: Path, created: Any
) -> None:
    busy_worker(world)
    note(world, 1)
    path = heartbeat(world, os.getpid(), None)
    body = json.loads(path.read_text(encoding="utf-8"))
    if created is not None:
        body["created"] = created
    path.write_text(json.dumps(body), encoding="utf-8")
    assert "start the supervisor" in go(world, processes=procs(*NOTHING))


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[1]", "null", "[" * 100000, "\x00\x00"],
    ids=["empty", "not-json", "list", "null", "deep-nesting", "nul-bytes"],
)
def test_a_damaged_heartbeat_does_not_count_and_does_not_crash(world: Path, text: str) -> None:
    busy_worker(world)
    note(world, 1)
    (state(world) / "pm-supervisor.json").write_text(text, encoding="utf-8")
    assert "start the supervisor" in go(world, processes=procs(*NOTHING))


def test_the_heartbeat_file_name_comes_from_the_configuration(world: Path, tmp_path: Path) -> None:
    busy_worker(world)
    note(world, 1)
    heartbeat(world, os.getpid(), own_start(), name="mine.json")
    assert "start the supervisor" in go(
        world, processes=procs(*NOTHING)
    )  # the shipped name is absent
    forget_check(world)
    quiet(world, cfg=config(tmp_path, supervisor_heartbeat_file="mine.json"), processes=procs())


def test_a_heartbeat_of_another_projects_state_directory_is_not_read(
    world: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    (other / ".tmp" / "pm-state").mkdir(parents=True)
    heartbeat(other, os.getpid(), own_start())
    busy_worker(world)
    note(world, 1)
    assert "start the supervisor" in go(world, processes=procs(*NOTHING))


def test_busy_worker_with_dead_launcher_is_not_running_work(world: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    busy_worker(world, pid=child.pid)
    quiet(world, processes=procs())


def test_busy_worker_without_recorded_pid_counts(world: Path) -> None:
    busy_worker(world, pid=None)
    note(world, 1)
    assert "start the supervisor" in go(world, processes=procs())


def test_agy_job_without_done_is_running_work(world: Path) -> None:
    (state(world) / "agents.json").write_text(
        json.dumps(
            [
                {"kind": "gemini", "id": "g1", "label": "L"},
                {"kind": "gemini", "id": "g2", "label": "L"},
                {"kind": "codex", "id": "c1", "label": "L"},
            ]
        ),
        encoding="utf-8",
    )
    done = world / ".tmp" / "agy-work" / "_results"
    done.mkdir(parents=True)
    (done / "g2.done").write_text("0", encoding="ascii")
    note(world, 1)
    out = go(world, processes=procs())
    assert "agy job g1" in out
    assert "g2" not in out
    assert "c1" not in out


def test_agy_job_with_done_is_not_work(world: Path) -> None:
    (state(world) / "agents.json").write_text(
        json.dumps([{"kind": "gemini", "id": "g1"}]), encoding="utf-8"
    )
    done = world / ".tmp" / "agy-work" / "_results"
    done.mkdir(parents=True)
    (done / "g1.done").write_text("0", encoding="ascii")
    quiet(world)


def test_running_ci_watcher_counts_as_work(world: Path) -> None:
    note(world, 1)
    out = go(world, processes=procs(watcher(world)))
    assert "CI watcher" in out
    assert "start the supervisor" in out
    forget_check(world)
    quiet(world, processes=procs(watcher(world), sup(world)))


@pytest.mark.parametrize("registry", ["", "not json", "[1]", '{"a": 1}', "null"])
def test_damaged_registries_are_ignored(world: Path, registry: str) -> None:
    (state(world) / "cli-workers.json").write_text(registry, encoding="utf-8")
    (state(world) / "agents.json").write_text(registry, encoding="utf-8")
    quiet(world)


# -- rule (d): continuation note ---------------------------------------------------------------------------------


def test_old_note_while_work_runs(world: Path) -> None:
    busy_worker(world)
    note(world, 90)
    out = go(world, processes=procs(sup(world)))
    assert "update the continuation note" in out
    assert "90 min" in out
    assert "supervisor" not in out


def test_fresh_note_is_quiet_and_boundary_is_inclusive(world: Path) -> None:
    busy_worker(world)
    note(world, 59)
    quiet(world, processes=procs(sup(world)))
    forget_check(world)
    note(world, 60)
    assert "update the continuation note" in go(world, processes=procs(sup(world)))


def test_missing_note_while_work_runs(world: Path) -> None:
    busy_worker(world)
    out = go(world, processes=procs(sup(world)))
    assert "is missing" in out
    assert "update the continuation note" in out


def test_old_note_without_work_is_quiet(world: Path) -> None:
    note(world, 500)
    quiet(world)


# -- rule (b): unpushed commits ----------------------------------------------------------------------------------


def commit_at(repo: Path, name: str, when: float) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", name)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + "+0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": iso, "GIT_AUTHOR_DATE": iso}
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", "commit", "-q", "-m", name],
        env=env, check=True, capture_output=True, timeout=60,
    )  # fmt: skip


@pytest.fixture
def repo_with_upstream(world: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    git(world, "init", "-q", "-b", "main")
    commit_at(world, "base.txt", NOW - 7200)
    git(world, "remote", "add", "origin", str(bare))
    git(world, "push", "-q", "-u", "origin", "main")
    return world


def test_old_unpushed_commit_without_ci_watcher(repo_with_upstream: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    out = go(repo_with_upstream)
    assert "push and run CI" in out
    assert "1 unpushed" in out
    assert "45 min" in out


def test_ci_watcher_silences_rule_b(repo_with_upstream: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    out = go(
        repo_with_upstream, processes=procs(watcher(repo_with_upstream), sup(repo_with_upstream))
    )
    assert "push and run CI" not in out


def watch_record(root: Path, run: str, pid: int, created: float | None, **extra: Any) -> Path:
    """The record ci_wait_private.ps1 writes: pid plus the creation time of that process (epoch seconds)."""
    path = state(root) / f"ci-watch-{run}.json"
    body = {"run_id": run, "pid": pid, **({} if created is None else {"created": created}), **extra}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def own_start() -> float:
    started = pi.load_cli_worker().process_start(os.getpid())
    assert started is not None
    return float(started)


def test_recorded_live_watcher_silences_rule_b_without_a_marker(
    repo_with_upstream: Path,
) -> None:
    """A watcher started through the Claude tool has no marker in its command line: its own record is the signal."""
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    watch_record(repo_with_upstream, "1", os.getpid(), own_start())
    assert "push and run CI" not in go(repo_with_upstream, processes=procs(*NOTHING))


def test_recorded_live_watcher_counts_as_work(world: Path) -> None:
    note(world, 1)
    watch_record(world, "1", os.getpid(), own_start())
    out = go(world, processes=procs(*NOTHING))
    assert "CI watcher" in out and "start the supervisor" in out


def test_record_of_an_exited_watcher_is_ignored(repo_with_upstream: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    created = pi.load_cli_worker().process_start(child.pid)
    child.wait()
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    watch_record(repo_with_upstream, "1", child.pid, created or NOW)
    assert "push and run CI" in go(repo_with_upstream)


def test_record_with_a_reused_pid_is_ignored(repo_with_upstream: Path) -> None:
    """The PID runs, but not the process that wrote the record: it was created at another time."""
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    watch_record(repo_with_upstream, "1", os.getpid(), own_start() - 3600)
    assert "push and run CI" in go(repo_with_upstream)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[1]",
        '{"pid": 1}',
        '{"created": 1.5}',
        '{"pid": true, "created": 5}',
        '{"pid": 0, "created": 5}',
        '{"pid": "x", "created": 5}',
        '{"pid": 4, "created": "5"}',
        '{"pid": 4, "created": -1}',
    ],
)
def test_damaged_watch_records_are_no_signal(repo_with_upstream: Path, text: str) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    (state(repo_with_upstream) / "ci-watch-1.json").write_text(text, encoding="utf-8")
    assert "push and run CI" in go(repo_with_upstream)


@pytest.mark.parametrize(
    "text",
    [
        "[" * 100000,  # RecursionError in the JSON reader
        '{"pid": 5, "created": ' + "9" * 400 + "}",  # OverflowError in the number check
        '{"pid": ' + "9" * 400 + ', "created": 5}',
        "\x00\x00",
        "{",
    ],
    ids=["deep-nesting", "huge-created", "huge-pid", "nul-bytes", "truncated"],
)
def test_a_record_that_makes_the_reader_raise_does_not_hide_a_live_one(
    repo_with_upstream: Path, text: str
) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    (state(repo_with_upstream) / "ci-watch-1.json").write_text(text, encoding="utf-8")
    watch_record(repo_with_upstream, "2", os.getpid(), own_start())
    assert "push and run CI" not in go(repo_with_upstream)


def test_one_live_record_among_stale_ones_is_enough(repo_with_upstream: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    watch_record(repo_with_upstream, "1", os.getpid(), own_start() - 3600)
    watch_record(repo_with_upstream, "2", os.getpid(), own_start())
    assert "push and run CI" not in go(repo_with_upstream)


def test_only_ci_watch_records_by_run_count(repo_with_upstream: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    for name in ("ci-watch.json", "ci-watch-1.txt", "other-1.json"):
        (state(repo_with_upstream) / name).write_text(
            json.dumps({"pid": os.getpid(), "created": own_start()}), encoding="utf-8"
        )
    assert "push and run CI" in go(repo_with_upstream)


def test_young_unpushed_commit_is_quiet(repo_with_upstream: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 10 * 60)
    quiet(repo_with_upstream)


def test_age_is_taken_from_the_oldest_unpushed_commit(repo_with_upstream: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 100 * 60)
    commit_at(repo_with_upstream, "b.txt", NOW - 1 * 60)
    out = go(repo_with_upstream)
    assert "2 unpushed" in out
    assert "100 min" in out


def test_nothing_unpushed_is_quiet(repo_with_upstream: Path) -> None:
    quiet(repo_with_upstream)


def test_no_upstream_is_quiet(world: Path) -> None:
    """No remote at all: there is nowhere to push, and counting the whole history would be noise."""
    git(world, "init", "-q", "-b", "main")
    commit_at(world, "a.txt", NOW - 500 * 60)
    quiet(world)


def test_a_branch_without_upstream_reports_its_unpushed_commits(repo_with_upstream: Path) -> None:
    git(repo_with_upstream, "switch", "-q", "-c", "feature")
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    commit_at(repo_with_upstream, "b.txt", NOW - 5 * 60)
    out = go(repo_with_upstream)
    assert "push and run CI" in out
    assert "2 unpushed" in out
    assert "45 min" in out


def test_a_never_pushed_repository_with_a_remote_reports_every_commit(
    world: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    git(world, "init", "-q", "-b", "main")
    commit_at(world, "a.txt", NOW - 90 * 60)
    git(world, "remote", "add", "origin", str(bare))
    out = go(world)
    assert "1 unpushed" in out and "90 min" in out


def test_a_branch_that_lost_its_upstream_still_reports(repo_with_upstream: Path) -> None:
    git(repo_with_upstream, "branch", "--unset-upstream")
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    assert "1 unpushed" in go(repo_with_upstream)


def test_a_new_branch_without_new_commits_is_quiet(repo_with_upstream: Path) -> None:
    git(repo_with_upstream, "switch", "-q", "-c", "feature")
    quiet(repo_with_upstream)


def test_a_young_commit_on_a_branch_without_upstream_is_quiet(repo_with_upstream: Path) -> None:
    git(repo_with_upstream, "switch", "-q", "-c", "feature")
    commit_at(repo_with_upstream, "a.txt", NOW - 10 * 60)
    quiet(repo_with_upstream)


@pytest.fixture
def github_repo(repo_with_upstream: Path) -> Path:
    """The remote URL names `me/proj`; nothing is fetched, so no network is used."""
    git(repo_with_upstream, "remote", "set-url", "origin", "https://github.com/me/proj.git")
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    return repo_with_upstream


def gh_config(tmp_path: Path) -> Path:
    return config(tmp_path, ci_watcher_markers=["gh run watch"])


def test_a_foreign_gh_run_watch_does_not_count_as_our_ci_watcher(
    github_repo: Path, tmp_path: Path
) -> None:
    cfg = gh_config(tmp_path)
    for line in (
        "gh run watch 99 -R other/repo",
        "gh run watch 99 -R me/proj-fork",
        "gh run watch 99",  # the repository is the working directory's: the command line cannot say which
        "gh run watch 99 -R other/proj",
    ):
        forget_check(github_repo)
        assert "push and run CI" in go(github_repo, cfg=cfg, processes=procs(line)), line


def test_a_gh_run_watch_naming_our_repository_counts(github_repo: Path, tmp_path: Path) -> None:
    cfg = gh_config(tmp_path)
    for line in ("gh run watch 99 -R me/proj", "gh  run   watch 99 --repo ME/Proj"):
        forget_check(github_repo)
        out = go(github_repo, cfg=cfg, processes=procs(line, sup(github_repo)))
        assert "push and run CI" not in out, line


@pytest.mark.parametrize(
    ("url", "named"),
    [
        ("https://github.com/me/proj.git", True),
        ("https://user@github.com/me/proj", True),
        ("git@github.com:me/proj.git", True),
        ("ssh://git@github.com:22/me/proj.git", True),
        ("git://example.org/me/proj.git/", True),
        ("/some/where/me/proj.git", False),
        ("C:\\some\\where\\me\\proj.git", False),
        ("file:///some/where/me/proj.git", False),
        ("../proj", False),
    ],
)
def test_the_repository_name_is_read_from_a_remote_url_only(
    world: Path, url: str, named: bool
) -> None:
    git(world, "init", "-q", "-b", "main")
    git(world, "remote", "add", "origin", url)
    identity = pi.project_identity(world, 30)
    assert pi.names_project("gh run watch 1 -R me/proj", identity) is named


def test_the_root_in_either_slash_form_names_the_project(world: Path) -> None:
    identity = pi.project_identity(world, 30)
    text = str(world)
    for form in (text, text.replace("\\", "/"), text.replace("/", "\\")):
        assert pi.names_project(f'pwsh -File "{form}{os.sep}x.ps1"', identity), form
        assert pi.names_project(f"pwsh -File {form}", identity), form
    assert not pi.names_project(f"pwsh -File {text}-other{os.sep}x.ps1", identity)
    assert not pi.names_project(f"pwsh -File x{text}{os.sep}x.ps1", identity)


def test_unpushed_age_override(repo_with_upstream: Path, tmp_path: Path) -> None:
    commit_at(repo_with_upstream, "a.txt", NOW - 45 * 60)
    quiet(repo_with_upstream, cfg=config(tmp_path, unpushed_age_minutes=60))
    forget_check(repo_with_upstream)
    assert "push and run CI" in go(
        repo_with_upstream, cfg=config(tmp_path, unpushed_age_minutes=40)
    )


# -- rule (c): unread results ------------------------------------------------------------------------------------


def test_unread_result_is_named(world: Path) -> None:
    result(world, "001.result.md")
    out = go(world)
    assert "read .tmp/cli-jobs/dev-1/001.result.md" in out


def test_read_of_the_result_silences_it_until_it_changes(world: Path) -> None:
    path = result(world, "001.result.md")
    quiet(world, event=read_event(path))
    # A later rewrite by the worker is unread again.
    stamp = NOW + 30
    os.utime(path, (stamp, stamp))
    forget_check(world)
    assert "001.result.md" in go(world, now=NOW + 40)


def test_read_is_recorded_even_when_the_check_is_rate_limited(world: Path) -> None:
    path = result(world, "001.result.md")
    assert "001.result.md" in go(world)  # the check runs and stamps its time
    assert (
        go(world, event=read_event(path)) == ""
    )  # rate limited: nothing printed, but the Read is recorded
    assert go(world, now=NOW + 11 * 60) == ""  # the next check finds it read


def test_relative_read_path_resolves_against_the_event_cwd(world: Path) -> None:
    path = result(world, "001.result.md")
    quiet(world, event=read_event(path.relative_to(world), cwd=world))


def test_read_of_another_file_is_not_a_read_of_the_result(world: Path) -> None:
    result(world, "001.result.md")
    other = world / "notes.md"
    other.write_text("x", encoding="utf-8")
    assert "001.result.md" in go(world, event=read_event(other))
    assert not (state(world) / pi.READS_DIR).exists()


def test_grep_of_a_result_is_not_a_read(world: Path) -> None:
    path = result(world, "001.result.md")
    out = go(world, event={"tool_name": "Grep", "tool_input": {"file_path": str(path)}})
    assert "001.result.md" in out


def test_old_results_are_outside_the_lookback(world: Path, tmp_path: Path) -> None:
    result(world, "001.result.md", age_minutes=300)
    quiet(world)
    forget_check(world)
    assert "001.result.md" in go(world, cfg=config(tmp_path, result_lookback_minutes=600))


def test_listing_is_capped_and_newest_first_kept(world: Path) -> None:
    for k in range(5):
        result(world, f"00{k}.result.md", age_minutes=10 - k)
    out = go(world)
    assert "004.result.md" in out and "003.result.md" in out and "002.result.md" in out
    assert "000.result.md" not in out
    assert "(+2 older)" in out
    forget_check(world)
    wide = go(world, cfg=config(world.parent, max_paths_listed=5))
    assert "000.result.md" in wide and "older" not in wide


def test_only_result_files_count(world: Path) -> None:
    result(world, "001.md")
    result(world, "001.result.txt")
    result(world, "log.jsonl")
    quiet(world)


# -- rate limit and state ---------------------------------------------------------------------------------------


def test_checks_run_at_most_once_per_interval(world: Path, tmp_path: Path) -> None:
    result(world, "001.result.md")
    assert go(world, now=NOW)
    assert go(world, now=NOW + 9 * 60) == ""
    assert go(world, now=NOW + 10 * 60)


def test_interval_override(world: Path, tmp_path: Path) -> None:
    result(world, "001.result.md")
    cfg = config(tmp_path, check_interval_minutes=1)
    assert go(world, cfg=cfg, now=NOW)
    assert go(world, cfg=cfg, now=NOW + 30) == ""
    assert go(world, cfg=cfg, now=NOW + 60)


def test_clock_going_backwards_reruns(world: Path) -> None:
    result(world, "001.result.md")
    assert go(world, now=NOW)
    assert go(world, now=NOW - 3600)


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", '{"last_check": "soon"}', '{"last_check": true}']
)
def test_damaged_state_means_no_previous_check(world: Path, text: str) -> None:
    result(world, "001.result.md")
    (state(world) / pi.STATE_FILE).write_text(text, encoding="utf-8")
    assert "001.result.md" in go(world)


def test_state_is_stamped_before_a_failing_check(world: Path) -> None:
    busy_worker(world)

    def boom(_timeout: int) -> list[str]:
        raise RuntimeError("process list exploded")

    with pytest.raises(RuntimeError):
        go(world, processes=boom)
    assert (
        go(world, processes=procs()) == ""
    )  # the failing check is not retried on the very next call


# -- configuration -----------------------------------------------------------------------------------------------


def test_shipped_values() -> None:
    cfg = pi.load_config()
    assert (
        cfg["check_interval_minutes"],
        cfg["unpushed_age_minutes"],
        cfg["note_age_minutes"],
    ) == (10, 30, 60)
    assert cfg["supervisor_markers"] == ["pm-supervisor.ps1"]
    assert cfg["ci_watcher_markers"] == ["ci_wait_private.ps1"]
    assert cfg["supervisor_heartbeat_file"] == "pm-supervisor.json"


def test_note_age_and_marker_overrides(world: Path, tmp_path: Path) -> None:
    busy_worker(world)
    note(world, 30)
    cfg = config(tmp_path, note_age_minutes=20, supervisor_markers=["my-supervisor.sh"])
    out = go(world, cfg=cfg, processes=procs(f"bash {world}/my-supervisor.sh"))
    assert "update the continuation note" in out
    assert "supervisor process" not in out
    forget_check(world)
    assert "start the supervisor" in go(world, cfg=cfg, processes=procs(sup(world)))


def _broken(field: str, value: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SHIPPED.read_text(encoding="utf-8"))
    data[field] = value
    return data


REFUSED = [
    ("check_interval_minutes", 0),
    ("check_interval_minutes", -5),
    ("unpushed_age_minutes", "30"),
    ("note_age_minutes", True),
    ("note_age_minutes", 1.5),
    ("note_age_minutes", None),
    ("result_lookback_minutes", 0),
    ("max_paths_listed", 0),
    ("git_timeout_seconds", 0),
    ("process_list_timeout_seconds", False),
    ("supervisor_markers", []),
    ("supervisor_markers", [""]),
    ("ci_watcher_markers", "ci_wait_private.ps1"),
    ("ci_watcher_markers", [3]),
    ("schema_version", 1),
    ("schema_version", 3),
    ("supervisor_heartbeat_file", ""),
    ("supervisor_heartbeat_file", " x.json"),
    ("supervisor_heartbeat_file", "sub/x.json"),
    ("supervisor_heartbeat_file", "..\\x.json"),
    ("supervisor_heartbeat_file", ".."),
    ("supervisor_heartbeat_file", "C:x.json"),
    ("supervisor_heartbeat_file", 5),
]


@pytest.mark.parametrize(("field", "value"), REFUSED)
def test_invalid_value_is_refused_and_the_hook_prints_one_line(
    tmp_path: Path, world: Path, field: str, value: Any
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(_broken(field, value)), encoding="utf-8")
    with pytest.raises(pi.ConfigError, match=field):
        pi.load_config(path)
    out = go(world, cfg=path)
    assert out.startswith("pm_invariants config invalid: ")
    assert "\n" not in out


@pytest.mark.parametrize(
    "field", [*pi.INT_FIELDS, *pi.LIST_FIELDS, *pi.STR_FIELDS, "schema_version"]
)
def test_missing_value_is_refused(tmp_path: Path, world: Path, field: str) -> None:
    data = json.loads(SHIPPED.read_text(encoding="utf-8"))
    del data[field]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pi.ConfigError, match=field):
        pi.load_config(path)
    out = go(world, cfg=path)
    assert out.startswith("pm_invariants config invalid: ") and field in out


@pytest.mark.parametrize("text", ["", "{", "[]", "null"])
def test_unreadable_or_non_object_config_is_refused(tmp_path: Path, world: Path, text: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    assert go(world, cfg=path).startswith("pm_invariants config invalid: ")
    assert go(world, cfg=tmp_path / "absent.json").startswith("pm_invariants config invalid: ")


def test_unknown_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**_broken("note_age_minutes", 60), "extra": 1}), encoding="utf-8")
    with pytest.raises(pi.ConfigError, match="unknown keys: extra"):
        pi.load_config(path)


def test_invalid_config_does_not_run_the_checks(world: Path, tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    go(world, cfg=path)
    assert not (state(world) / pi.STATE_FILE).exists()


# -- the hook process --------------------------------------------------------------------------------------------


def hook(
    root: Path,
    stdin: str,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "AGENT_WORKFLOW_STATE_DIR")
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-B", "-I", str(HOOK), "--root", str(root), *args],
        input=stdin, capture_output=True, text=True, timeout=120, check=False, env=env,
    )  # fmt: skip


def test_process_prints_posttooluse_context_for_a_violation(world: Path, tmp_path: Path) -> None:
    result(world, "001.result.md")
    proc = hook(world, json.dumps({"tool_name": "Bash"}), "--config", str(config(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "PostToolUse"
    assert "001.result.md" in body["additionalContext"]


def isolated(tmp_path: Path) -> list[str]:
    """`--config` args whose watcher markers no real process carries (the PM's own watchers may run meanwhile)."""
    cfg = config(
        tmp_path,
        supervisor_markers=["no-such-supervisor-7c1f.ps1"],
        ci_watcher_markers=["no-such-watcher-7c1f.ps1"],
    )
    return ["--config", str(cfg)]


def test_process_without_violation_prints_nothing(world: Path, tmp_path: Path) -> None:
    proc = hook(world, json.dumps({"tool_name": "Bash"}), *isolated(tmp_path))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_process_state_dir_comes_from_the_environment_or_flag(world: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    event = json.dumps({"tool_name": "Bash"})
    hook(world, event, *isolated(tmp_path), env_extra={"AGENT_WORKFLOW_STATE_DIR": str(other)})
    assert (other / pi.STATE_FILE).is_file()
    flag = tmp_path / "flagged"
    hook(world, event, *isolated(tmp_path), "--state-dir", str(flag))
    assert (flag / pi.STATE_FILE).is_file()


def test_process_ignores_worker_sessions(world: Path, tmp_path: Path) -> None:
    result(world, "001.result.md")
    proc = hook(
        world, json.dumps({"tool_name": "Bash"}), "--config", str(config(tmp_path)),
        env_extra={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )  # fmt: skip
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert not (state(world) / pi.STATE_FILE).exists()


@pytest.mark.parametrize("stdin", ["", "garbage", "[]", "null", "\ufeff"])
def test_process_survives_unreadable_input(world: Path, stdin: str) -> None:
    proc = hook(world, stdin)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_process_survives_bad_arguments(world: Path) -> None:
    proc = hook(world, json.dumps({"tool_name": "Bash"}), "--no-such-flag")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_process_reports_an_invalid_config_as_one_line(world: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    proc = hook(world, json.dumps({"tool_name": "Bash"}), "--config", str(bad))
    assert proc.returncode == 0
    assert (
        "pm_invariants config invalid"
        in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    )


def test_main_swallows_any_crash(
    world: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(pi, "run", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    assert pi.main(["--root", str(world)]) == 0
    assert capsys.readouterr() == ("", "")


def test_real_busy_worker_without_supervisor_end_to_end(world: Path, tmp_path: Path) -> None:
    """Uses the real process listing; the supervisor marker is one that no process on this machine carries."""
    busy_worker(world)
    note(world, 1)
    cfg = config(tmp_path, supervisor_markers=["no-such-supervisor-7c1f.ps1"])
    proc = hook(world, json.dumps({"tool_name": "Bash"}), "--config", str(cfg))
    assert proc.returncode == 0, proc.stderr
    assert (
        "start the supervisor" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    )


# -- real process listing ----------------------------------------------------------------------------------------


def test_process_listing_finds_a_real_child_by_its_command_line() -> None:
    marker = "pm-invariants-probe-3f9a.ps1"
    ours = pi.Identity(
        (re.compile(re.escape(marker)),)
    )  # the child's command line names the "project"
    foreign = pi.Identity((re.compile("no-such-project-7c1f"),))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
    try:
        lines: list[str] | None = None
        deadline = time.time() + 30
        while time.time() < deadline:
            lines = pi.process_command_lines(25)
            if lines is not None and pi.marker_running(lines, [marker], ours):
                break
            time.sleep(0.5)
        assert lines is not None, "this platform gave no process list"
        assert pi.marker_running(lines, [marker], ours)
        assert not pi.marker_running(lines, ["pm-invariants-probe-absent.ps1"], ours)
        assert not pi.marker_running(
            lines, [marker], foreign
        )  # another project's process is not ours
    finally:
        child.terminate()
        child.wait(timeout=30)


def test_launcher_gone_judges_by_creation_time_not_by_pid_alone(world: Path) -> None:
    cw = pi.load_cli_worker()
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait(timeout=30)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = cw.process_start(sleeper.pid)
        assert created is not None
        assert pi.launcher_gone({"launcher_pid": done.pid})  # no such process
        assert pi.launcher_gone(
            {"launcher_pid": sleeper.pid, "launcher_created": created - 3600}
        )  # reused PID
        assert not pi.launcher_gone({"launcher_pid": sleeper.pid, "launcher_created": created})
        assert not pi.launcher_gone(
            {"launcher_pid": sleeper.pid}
        )  # legacy, no comparable time: not provably gone
        assert not pi.launcher_gone({})
        # The rule counts a busy worker whose launcher PID was reused as gone, i.e. not as running work.
        busy_worker(world, sleeper.pid)
        registry = state(world) / "cli-workers.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        data["dev-1"]["launcher_created"] = created - 3600
        registry.write_text(json.dumps(data), encoding="utf-8")
        assert pi.busy_cli_workers(state(world)) == []
        data["dev-1"]["launcher_created"] = created
        registry.write_text(json.dumps(data), encoding="utf-8")
        assert pi.busy_cli_workers(state(world)) == ["dev-1"]
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


def test_launcher_gone_fails_quiet_when_cli_worker_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pi, "load_cli_worker", lambda: (_ for _ in ()).throw(OSError("missing")))
    assert pi.launcher_gone({"launcher_pid": 1}) is False


# -- property tests (decision 130): the marker / heartbeat decision ----------------------------------------------

PROJECT_ROOT = Path(os.sep + "work" + os.sep + "proj")
PROJECT = pi.project_identity(PROJECT_ROOT, 5)
FREE_TEXT = st.text(
    st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=40
)
MARKERS = st.sampled_from(["pm-supervisor.ps1", "ci_wait_private.ps1", "gh run watch"])


@given(prefix=FREE_TEXT, suffix=FREE_TEXT, marker=MARKERS)
def test_property_a_marker_line_that_does_not_name_the_project_never_counts(
    prefix: str, suffix: str, marker: str
) -> None:
    """Fail-closed invariant: no root and no owner/repo in the line means the process is not ours."""
    line = f"{prefix} {marker} {suffix}"
    assume("work" not in line.lower())
    assert pi.marker_running([line], [marker], PROJECT) is False


@given(prefix=FREE_TEXT, suffix=FREE_TEXT, marker=MARKERS, slash=st.sampled_from(["/", "\\"]))
def test_property_a_marker_line_that_names_the_project_always_counts(
    prefix: str, suffix: str, marker: str, slash: str
) -> None:
    root = str(PROJECT_ROOT).replace("/", slash).replace("\\", slash)
    assert pi.marker_running([f"{prefix} {root} {marker} {suffix}"], [marker], PROJECT) is True


@given(text=FREE_TEXT)
def test_property_arbitrary_heartbeat_text_is_never_liveness_and_never_raises(
    tmp_path_factory: pytest.TempPathFactory, text: str
) -> None:
    folder = tmp_path_factory.mktemp("hb")
    (folder / "pm-supervisor.json").write_text(text, encoding="utf-8")
    assert pi.supervisor_alive(folder, "pm-supervisor.json") is False
