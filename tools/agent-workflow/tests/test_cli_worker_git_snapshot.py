"""cli_worker.py turn snapshot (T-024 round 4, class F detection): a worker turn that changes any ref, HEAD or reflog of
its repository is flagged `git_history_changed`; a turn that only edits, stages or applies a patch is not.

The worker process is a labelled test double: a tiny Python script in place of the Claude CLI that runs one git action
in a scratch repository (pytest `tmp_path`, never the project repository) and prints a result event.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git

SCRIPT = Path(__file__).resolve().parents[1] / "cli_worker.py"
spec = importlib.util.spec_from_file_location("cli_worker_snapshot", SCRIPT)
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)

ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def fake_turn(*git_args: list[str]) -> str:
    """Test double of one worker turn: run each git argv in the worker's cwd, then print a result event."""
    return (
        "import json, subprocess\n"
        f"for argv in {list(git_args)!r}:\n"
        "    subprocess.run(['git', *argv], check=True, capture_output=True, timeout=30)\n"
        "print(json.dumps({'type': 'result', 'result': 'done'}))\n"
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))  # an empty orchestrator ledger
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "core.autocrlf", "false")
    (work / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    git(work, "add", "a.txt")
    git(work, *ID, "commit", "-q", "-m", "a")
    git(work, "branch", "done")
    return work


def run(repo: Path, monkeypatch: pytest.MonkeyPatch, *git_args: list[str], name: str = "w") -> tuple[int, dict]:
    entry = {"model": "haiku", "effort": "low", "session_id": "sid", "cwd": str(repo), "status": "busy", "turns": 0}
    cw.save({**cw.load(), name: {**cw.load().get(name, {}), **entry}})  # a later turn keeps the earlier flag
    monkeypatch.setattr(cw, "build_command", lambda w, r: [sys.executable, "-c", fake_turn(*git_args)])
    rc = cw.run_turn(name, cw.load()[name], "hi", False, 5, log_number=1)
    return rc, cw.load()[name]


@pytest.mark.parametrize(
    ("case", "actions", "changed"),
    [
        ("commit", [[*ID, "commit", "-q", "--allow-empty", "-m", "x"]], "refs/heads/main"),
        ("move branch", [["branch", "-f", "done", "main"]], "refs/heads/done"),
        ("delete ref", [["branch", "-q", "-D", "done"]], "refs/heads/done: "),
        ("new tag", [["tag", "v1"]], "refs/tags/v1"),
        ("reflog entry only", [["update-ref", "-m", "same", "refs/heads/main", "HEAD"]], "reflog "),
        ("detach HEAD", [["checkout", "-q", "--detach"]], "refs/heads/main "),
    ],
)
def test_a_turn_that_changes_git_is_flagged(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str,
    actions: list[list[str]], changed: str,
) -> None:
    if case == "move branch":  # setup outside the turn: main gets a second commit, `done` stays at the first
        git(repo, *ID, "commit", "-q", "--allow-empty", "-m", "b")
    rc, stored = run(repo, monkeypatch, *actions)
    assert rc == 1 and stored["status"] == cw.GIT_HISTORY_CHANGED, (case, stored)
    assert any(changed in c for c in stored["git_changes"]), (case, stored["git_changes"])
    result = (cw.JOBS / "w" / "001.result.md").read_text(encoding="utf-8")
    assert result.startswith("done") and "## git_history_changed" in result
    monkeypatch.setattr("sys.argv", ["cli_worker.py", "status", "--json"])
    capsys.readouterr()
    assert cw.main() == 0
    row = {r["name"]: r for r in json.loads(capsys.readouterr().out)}["w"]
    assert row["status"] == cw.GIT_HISTORY_CHANGED and row["git_changes"] == stored["git_changes"]


@pytest.mark.parametrize(
    ("case", "actions"),
    [
        ("nothing", []),
        ("status and log", [["status", "--short"], ["log", "-1", "--format=%H"]]),
        ("stage", [["add", "a.txt"]]),
        ("apply a patch", [["apply", "--cached", "p.diff"], ["apply", "p.diff"]]),
    ],
)
def test_a_turn_that_only_edits_stages_or_applies_is_not_flagged(
    repo: Path, monkeypatch: pytest.MonkeyPatch, case: str, actions: list[list[str]]
) -> None:
    (repo / "a.txt").write_text("a\nedited\n", encoding="utf-8", newline="\n")  # an edit made by the turn
    patch = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, check=True).stdout
    git(repo, "checkout", "-q", "--", "a.txt")
    (repo / "p.diff").write_bytes(patch)
    rc, stored = run(repo, monkeypatch, *actions)
    assert rc == 0 and stored["status"] == "idle" and "git_changes" not in stored, (case, stored)


def test_the_flag_stays_until_the_worker_is_retired(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later clean turn does not clear the flag: the PM reviews the listed refs before integrating."""
    run(repo, monkeypatch, ["tag", "v2"])
    rc, stored = run(repo, monkeypatch)
    assert rc == 1 and stored["status"] == cw.GIT_HISTORY_CHANGED
    assert any("refs/tags/v2" in c for c in stored["git_changes"])


def test_snapshot_comparison() -> None:
    """Unit: created, moved, deleted refs, HEAD and reflog counts; no repository on either side compares equal."""
    own = ["head /r", "reflog /"]
    before = {"state": {"ref refs/heads/a": "1", "ref refs/heads/b": "2", "head /r": "refs/heads/a 1",
                        "reflog logs/HEAD": "3"}, "main": True, "own": own}
    after = {"state": {"ref refs/heads/a": "9", "ref refs/tags/t": "5", "head /r": "detached 9",
                       "reflog logs/HEAD": "2"}, "main": True, "own": own}
    changes = cw.git_changes(before, after)
    assert changes == [
        "HEAD of /r: refs/heads/a 1 -> detached 9",
        "refs/heads/a: 1 -> 9",
        "refs/heads/b: 2 -> absent",
        "refs/tags/t: absent -> 5",
        "reflog logs/HEAD: 3 -> 2 entries",
    ]
    assert cw.git_changes(before, before) == [] and cw.git_changes(None, None) == []
    assert cw.git_changes(before, None) == ["the repository could not be read after the turn"]


def test_a_broken_repository_refuses_the_turn_and_only_no_repository_runs_without_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-024 F15: a `.git` entry that git cannot read is a failed snapshot, not "outside Git": the turn is refused
    before its process starts and the entry leaves `busy`. Only a directory with no `.git` at or above it runs."""
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    started = tmp_path / "started"
    turn = f"open({str(started)!r}, 'w').close()\nprint('{{\"type\": \"result\", \"result\": \"done\"}}')\n"
    monkeypatch.setattr(cw, "build_command", lambda *_a: [sys.executable, "-c", turn])
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: missing\n", encoding="utf-8")  # broken link: git cannot read it
    (broken / "sub").mkdir()
    assert "problem" in cw.git_snapshot(broken / "sub", 10)  # found above the cwd too: git discovers upwards
    entry = {"model": "haiku", "effort": "low", "session_id": "sid", "cwd": str(broken), "status": "busy", "turns": 0}
    cw.save({"w": entry})
    with pytest.raises(SystemExit, match="could not be read before the turn"):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    assert not started.exists() and cw.load()["w"]["status"] == "error refused"
    plain = tmp_path / "plain"
    plain.mkdir()
    if cw.repository_evidence(plain) is not None:
        pytest.skip(f"the temporary folder lies inside a repository ({cw.repository_evidence(plain)})")
    assert cw.git_snapshot(plain, 10) is None
    cw.save({"w": {**entry, "cwd": str(plain)}})
    assert cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1) == 0
    assert started.exists() and cw.load()["w"]["status"] == "idle"
