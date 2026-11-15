"""Orchestrator Git write ledger (hooks/git_ledger.py, T-024 round 5, classes J and K).

Concurrency regressions on scratch repositories (pytest `tmp_path`, never the project repository) with linked
worktrees: the orchestrator's allowed writes in the main tree, recorded through the real `tool_guard.py --git-mode
pm` path and the PostToolUse hook function, do not flag a concurrent worker turn; a worker's write to a shared ref, to
a sibling worktree, a deleted ref and an expired remote or tag reflog do, also with a forged ledger entry.

Test doubles (labelled): the worker process is a tiny Python script in place of the Claude CLI; the orchestrator's
commands run as plain subprocesses between the guard call and the hook call, inside the worker's turn.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import git_ledger as gl
import process_guard as pg
import pytest
import tool_guard as tg
from conftest import git

SCRIPT = Path(__file__).resolve().parents[1] / "cli_worker.py"
spec = importlib.util.spec_from_file_location("cli_worker_ledger", SCRIPT)
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)

ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.main = tmp / "main"
        self.worker = self.main / ".tmp" / "worktrees" / "w"
        self.sibling = self.main / ".tmp" / "worktrees" / "sib"
        self.calls = 0

    def pm(self, command: str) -> None:
        """The orchestrator's command: the real guard (pm mode) allows it and calls begin(), the command runs (test
        double of the shell), then the PostToolUse hook function appends the transitions."""
        self.calls += 1
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.main),
            "session_id": "pm-session",
            "tool_use_id": f"toolu_{self.calls}",
        }
        pg.read_event = lambda: json.dumps(event)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            assert tg.main(["--git-mode", "pm"], root=self.main) == 0, (command, err.getvalue())
        subprocess.run([*shlex.split(command)[:1], *ID, *shlex.split(command)[1:]], cwd=self.main, check=True,
                       capture_output=True, timeout=30)
        gl.finish(self.main, event, 10)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    tmp_path = Path(tmp_path).resolve()
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(tg, "LOG", tmp_path / "guard.log")
    w = World(tmp_path)
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(w.origin))
    w.main.mkdir()
    git(w.main, "init", "-q", "-b", "main")
    for key, value in [("core.autocrlf", "false"), ("user.name", "t"), ("user.email", "t@example.invalid")]:
        git(w.main, "config", key, value)
    (w.main / ".gitignore").write_text(".tmp/\n", encoding="utf-8", newline="\n")
    git(w.main, "add", ".gitignore")
    git(w.main, "commit", "-q", "-m", "base")
    git(w.main, "remote", "add", "origin", str(w.origin))
    git(w.main, "push", "-q", "origin", "main")  # fixture setup in a scratch remote
    git(w.main, "fetch", "-q", "origin")
    git(w.main, "branch", "old")
    git(w.main, "update-ref", "--create-reflog", "-m", "fixture", "refs/tags/t1", "HEAD")
    (w.main / ".tmp" / "worktrees").mkdir(parents=True)
    git(w.main, "worktree", "add", "-q", "--detach", str(w.worker), "HEAD")
    git(w.main, "worktree", "add", "-q", "--detach", str(w.sibling), "HEAD")
    git(w.main, "worktree", "add", "-q", "-b", "done", str(w.main / ".tmp" / "worktrees" / "done"))
    other = tmp_path / "other"  # a second clone moves origin/main, so a fetch changes the remote-tracking ref
    git(tmp_path, "clone", "-q", str(w.origin), str(other))
    git(other, *ID, "commit", "-q", "--allow-empty", "-m", "upstream")
    git(other, "push", "-q", "origin", "main")
    return w


def fake_turn(code: str = "") -> str:
    """Test double of one worker turn: run the given Python lines, then print a result event."""
    return f"import json, subprocess\n{code}\nprint(json.dumps({{'type': 'result', 'result': 'done'}}))\n"


def run(w: World, monkeypatch: pytest.MonkeyPatch, pm: list[str] = (), code: str = "") -> tuple[int, dict]:
    entry = {"model": "haiku", "effort": "low", "session_id": "sid", "cwd": str(w.worker), "status": "busy",
             "turns": 0}
    cw.save({"w": entry})
    monkeypatch.setattr(cw, "build_command", lambda *_a: [sys.executable, "-c", fake_turn(code)])
    real = cw.launch_turn

    def launch(*args: Any, **kwargs: Any) -> int:
        for command in pm:  # the orchestrator integrates in the main tree while the turn runs
            w.pm(command)
        return real(*args, **kwargs)

    monkeypatch.setattr(cw, "launch_turn", launch)
    rc = cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    return rc, cw.load()["w"]


def worker_git(where: Path, *argv: str) -> str:
    return f"subprocess.run(['git', '-C', {str(where)!r}, *{[*ID, *argv]!r}], check=True, capture_output=True)"


PM_CASES = {
    "clean": [],
    "commit": ["git add .gitignore", "git commit -m integrate"],
    "fetch": ["git fetch origin"],
    "worktree add": ["git worktree add --detach .tmp/worktrees/new HEAD"],
    "worktree remove and branch -d": ["git worktree remove .tmp/worktrees/done", "git branch -d done"],
}


@pytest.mark.parametrize("case", sorted(PM_CASES))
def test_orchestrator_writes_during_a_turn_do_not_flag_it(world: World, monkeypatch: pytest.MonkeyPatch,
                                                         case: str) -> None:
    if case == "commit":
        (world.main / ".gitignore").write_text(".tmp/\nruns/\n", encoding="utf-8", newline="\n")
    before = cw.git_snapshot(world.worker, 10)
    rc, stored = run(world, monkeypatch, PM_CASES[case])
    assert rc == 0 and stored["status"] == "idle" and "git_changes" not in stored, (case, stored)
    if case != "clean":  # the writes really changed the compared state, and the ledger explains them
        assert before["state"] != cw.git_snapshot(world.worker, 10)["state"]
        assert gl.entries(world.main)


WORKER_CASES = {
    "commit to main": ("main", ["commit", "--allow-empty", "-qm", "w"], "refs/heads/main:"),
    "commit in a sibling": ("sibling", ["commit", "--allow-empty", "-qm", "w"], "HEAD of "),
    "delete a ref": ("worker", ["update-ref", "-d", "refs/heads/old"], "refs/heads/old: "),
    "expire the remote reflog": ("worker", ["reflog", "expire", "--expire=all", "refs/remotes/origin/main"],
                                 "reflog logs/refs/remotes/origin/main"),
    "expire a tag reflog": ("worker", ["reflog", "expire", "--expire=all", "refs/tags/t1"],
                            "reflog logs/refs/tags/t1"),
    "own HEAD": ("worker", ["commit", "--allow-empty", "-qm", "w"], "HEAD of "),
}


@pytest.mark.parametrize("case", sorted(WORKER_CASES))
def test_worker_writes_during_a_turn_are_flagged(world: World, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    where, argv, changed = WORKER_CASES[case]
    path = {"main": world.main, "sibling": world.sibling, "worker": world.worker}[where]
    if case == "expire the remote reflog":
        assert git(world.main, "reflog", "show", "--format=%H", "refs/remotes/origin/main")  # non-empty before
    rc, stored = run(world, monkeypatch, code=worker_git(path, *argv))
    assert rc == 1 and stored["status"] == cw.GIT_HISTORY_CHANGED, (case, stored)
    assert any(c.startswith(changed) for c in stored["git_changes"]), (case, stored["git_changes"])


def test_a_worker_write_next_to_an_orchestrator_write_is_flagged(world: World, monkeypatch: pytest.MonkeyPatch
                                                                 ) -> None:
    (world.main / ".gitignore").write_text(".tmp/\nruns/\n", encoding="utf-8", newline="\n")
    rc, stored = run(world, monkeypatch, ["git add .gitignore", "git commit -m integrate"],
                     worker_git(world.main, "commit", "--allow-empty", "-qm", "w"))
    assert rc == 1 and any(c.startswith("refs/heads/main:") for c in stored["git_changes"]), stored


def _forge(w: World, session: str, keys: list[str]) -> str:
    """Turn code (test double) that writes ledger entries claiming the worker's own transitions after it made them."""
    return (
        "import time, sys\n"
        f"sys.path.insert(0, {str(Path(gl.__file__).parent)!r})\n"
        "import git_ledger as gl\n"
        f"before = gl.snapshot({str(w.worker)!r}, 10)['state']\n"
        + worker_git(w.sibling, "commit", "--allow-empty", "-qm", "w") + "\n"
        + worker_git(w.worker, "commit", "--allow-empty", "-qm", "w") + "\n"
        f"after = gl.snapshot({str(w.worker)!r}, 10)['state']\n"
        f"steps = [s for s in gl.transitions(before, after) if any(s[0].startswith(k) for k in {keys!r})]\n"
        "now = time.time()\n"
        f"entry = dict(start=now - 0.01, end=now, session={session!r}, cwd={str(w.main)!r}, transitions=steps)\n"
        f"d = gl.ledger_dir({str(w.main)!r}); d.mkdir(parents=True, exist_ok=True)\n"
        "open(d / gl.LEDGER_NAME, 'a').write(json.dumps(entry) + '\\n')\n"
    )


def test_a_forged_entry_with_the_workers_session_or_for_its_own_head_is_still_flagged(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = Path(cw.git_snapshot(world.worker, 10)["own"][1].split(" ", 1)[1]).as_posix()
    rc, stored = run(world, monkeypatch, code=_forge(world, "sid", ["head ", "reflog "]))
    assert rc == 1 and any(c.startswith(f"HEAD of {world.sibling.as_posix()}") for c in stored["git_changes"])
    rc, stored = run(world, monkeypatch, code=_forge(world, "pm-session", ["head ", f"reflog {rel}"]))
    assert rc == 1 and any(c.startswith(f"HEAD of {world.worker.as_posix()}") for c in stored["git_changes"])
    assert any(c.startswith(f"reflog {rel}") for c in stored["git_changes"]), stored["git_changes"]


# Final round (decision 100, F27 detection at the system of record): the ledger is append-only, so a worker-side write
# that changes the bytes it held at the start of the turn, or appends a line that is no usable entry, is flagged,
# while the orchestrator's own appends during the turn are not. The worker's write is a labelled test double (turn
# code that rewrites the file, standing for a script or background process the guard could not prove).
LEDGER_TAMPER = {
    "edit an earlier entry": "open(p, 'wb').write(d.replace(b'pm-session', b'pm-sessioX'))",
    "re-time an earlier entry": "e = json.loads(d); e['end'] = e['end'] + 3600; "
                                "open(p, 'wb').write(json.dumps(e).encode() + b'\\n')",
    "truncate": "open(p, 'wb').write(b'')",
    "append a malformed line": "open(p, 'ab').write(b'not an entry\\n')",
}


@pytest.mark.parametrize("how", sorted(LEDGER_TAMPER))
def test_a_worker_write_to_the_ledger_during_its_turn_is_flagged(world: World, monkeypatch: pytest.MonkeyPatch,
                                                                 how: str) -> None:
    world.pm("git worktree add --detach .tmp/worktrees/new HEAD")  # an entry written before the turn
    path = gl.ledger_dir(world.main) / gl.LEDGER_NAME
    assert len(path.read_bytes().splitlines()) == 1
    code = f"p = {str(path)!r}\nd = open(p, 'rb').read()\n{LEDGER_TAMPER[how]}\n"
    rc, stored = run(world, monkeypatch, code=code)
    assert rc == 1 and stored["status"] == cw.GIT_HISTORY_CHANGED, (how, stored)
    assert any("orchestrator ledger" in c for c in stored["git_changes"]), (how, stored["git_changes"])


def test_an_orchestrator_append_during_the_turn_is_not_flagged(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.pm("git worktree add --detach .tmp/worktrees/new HEAD")
    path = gl.ledger_dir(world.main) / gl.LEDGER_NAME
    start = path.read_bytes()
    rc, stored = run(world, monkeypatch, ["git fetch origin"])
    assert rc == 0 and stored["status"] == "idle" and "git_changes" not in stored, stored
    grown = path.read_bytes()
    assert grown.startswith(start) and len(grown.splitlines()) == 2


@pytest.mark.parametrize(
    ("command", "expect"),
    [
        ("echo x >> <LEDGER>/ledger.jsonl", "B"),
        ("cp forged.jsonl <LEDGER>", "B"),
        ("cp forged.jsonl <STATE>/*/ledger.jsonl", "B"),
        ("mkdir -p <LEDGER>/pending", "B"),
        ("cp forged.jsonl <STATE>/git-*", "B"),
        ("echo x > .tmp/scratch.txt", "A"),  # near miss: the worker's own scratch
        ("rm .tmp/*.log", "A"),  # near miss: a wildcard that cannot reach the ledger
        ("rm <STATE>/*.log", "B"),  # the whole state directory is closed to worker shells (T-024 final, PD1)
        ("echo x > <STATE>/other.txt", "B"),
    ],
)
def test_a_worker_shell_never_writes_the_ledger(guard_repo: Path, monkeypatch: pytest.MonkeyPatch, command: str,
                                                expect: str) -> None:
    monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR", raising=False)
    ledger = gl.ledger_dir(guard_repo)
    text = command.replace("<LEDGER>", ledger.as_posix()).replace("<STATE>", ledger.parent.as_posix())
    for mode in ("worker", "pm"):
        ctx = tg.Context({"tool_name": "Bash", "tool_input": {"command": text}, "cwd": str(guard_repo)},
                         tg.gc.load(), __import__("argparse").Namespace(config=None, git_mode=mode), guard_repo)
        blocks = tg.check_write(ctx)
        want = expect if mode == "worker" else "A"
        assert ("B" if blocks else "A") == want, (mode, text, blocks)


def test_unexplained_rules() -> None:
    """Unit: a chain of entries explains a key; a gap, a wrong old value, a containing entry, an entry outside the
    window, the worker's session and the worker's own linked worktree keys explain nothing."""
    own = ["head /r/.tmp/worktrees/w", "reflog worktrees/w/"]
    before = {"state": {"ref refs/heads/main": "A", "head /r/.tmp/worktrees/w": "detached A",
                        "reflog worktrees/w/logs/HEAD": "1", "reflog logs/HEAD": "1"}, "main": False, "own": own}
    after = {"state": {"ref refs/heads/main": "C", "head /r/.tmp/worktrees/w": "detached C",
                       "reflog worktrees/w/logs/HEAD": "2", "reflog logs/HEAD": "3"}, "main": False, "own": own}
    chain = [{"start": 2, "end": 3, "session": "pm", "transitions": [["ref refs/heads/main", "A", "B"],
                                                                      ["reflog logs/HEAD", "1", "2"]]},
             {"start": 4, "end": 5, "session": "pm", "transitions": [["ref refs/heads/main", "B", "C"],
                                                                      ["reflog logs/HEAD", "2", "3"],
                                                                      ["head /r/.tmp/worktrees/w", "detached A",
                                                                       "detached C"],
                                                                      ["reflog worktrees/w/logs/HEAD", "1", "2"]]}]
    flagged = gl.unexplained(before, after, (1, 9), chain, "sid")
    assert flagged == ["HEAD of /r/.tmp/worktrees/w: detached A -> detached C",
                       "reflog worktrees/w/logs/HEAD: 1 -> 2 entries"]
    assert len(gl.unexplained(before, after, (1, 9), chain[1:], "sid")) == 4  # a gap: A -> C has no A -> B
    assert len(gl.unexplained(before, after, (3.5, 9), chain, "sid")) == 4  # first entry before the window
    assert len(gl.unexplained(before, after, (2.5, 4.5), [{**chain[0], "start": 0, "end": 99}, chain[1]], "sid")) == 4
    assert len(gl.unexplained(before, after, (1, 9), chain, "pm")) == 4  # the worker's own session
    assert gl.unexplained({**before, "main": True}, {**after, "main": True}, (1, 9), chain, "sid") == []
    assert gl.unexplained(None, None, (1, 9), [], None) == []
    assert gl.unexplained({"problem": "p"}, after, (1, 9), [], None) == ["p"]


def test_finish_without_begin_and_a_malformed_ledger_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path))
    assert gl.finish(tmp_path, {"tool_use_id": "toolu_x"}, 10) is False and gl.entries(tmp_path) == []
    (tmp_path / "git-ledger").mkdir()
    (tmp_path / "git-ledger" / gl.LEDGER_NAME).write_text('{"start": 1}\nnot json\n', encoding="utf-8")
    assert gl.entries(tmp_path) == []
    started = time.monotonic()
    assert gl.settle(tmp_path, 0, 1, 5) == []  # nothing pending: no wait
    assert time.monotonic() - started < 1


# Round 6 (R17, class L): every effective destination of a write form, with the state directory and a forged
# ledger copy existing, so the "into an existing directory" readings are the ones judged. B: refused, A: allowed.
EFFECTIVE_CASES = [
    ("Bash", "cp -r .tmp/forged/git-ledger <STATE>", "B"),  # into the parent: lands at <STATE>/git-ledger
    ("Bash", "cp -r .tmp/forged/git-ledger <STATE>/", "B"),  # trailing separator
    ("Bash", "cp -r -t <STATE> .tmp/forged/git-ledger", "B"),
    ("Bash", "cp -r --target-directory=<STATE> .tmp/forged/git-ledger", "B"),
    ("Bash", "cp -r .tmp/forged/. <STATE>", "B"),  # the source's contents
    ("Bash", "cp -rT .tmp/forged <STATE>", "B"),  # no target directory: <STATE> becomes the copy
    ("Bash", "cp -r .tmp/forged/* <STATE>", "B"),  # a wildcard source
    ("Bash", "cp --parents .tmp/forged/git-ledger/ledger.jsonl <STATE>", "B"),
    ("Bash", "mv .tmp/forged/git-ledger <STATE>", "B"),
    ("Bash", "mv <STATE> .tmp/old-state", "B"),  # moving a parent
    ("Bash", "rm -rf <STATE>", "B"),  # removing a parent
    ("Bash", "install -D -t <STATE> .tmp/forged/git-ledger/ledger.jsonl", "B"),  # lands in the state directory (PD1)
    ("Bash", "install -T .tmp/forged/git-ledger/ledger.jsonl <STATE>", "B"),
    ("Bash", "ln -sfn .tmp/forged/git-ledger <STATE>", "B"),
    ("Bash", "rsync -a .tmp/forged/ <STATE>", "B"),
    ("Bash", "rsync -a .tmp/forged/other <STATE>", "B"),  # rsync is not modelled: any subtree of <STATE>
    ("Bash", "tar -xf .tmp/forged.tar -C <STATE>", "B"),
    ("Bash", "unzip .tmp/forged.zip -d <STATE>", "B"),
    ("Bash", "7z x .tmp/forged.7z -o<STATE>", "B"),
    ("Bash", "wget -r -nH -P <STATE> http://127.0.0.1/git-ledger/", "B"),
    ("Bash", "robocopy .tmp/forged <STATE> /E", "B"),
    ("Bash", "xcopy .tmp/forged <STATE> /E", "B"),
    ("PowerShell", "Copy-Item -Recurse .tmp/forged/git-ledger -Destination <STATE>", "B"),
    ("PowerShell", "Copy-Item -Recurse .tmp/forged/* -Destination <STATE>/", "B"),
    ("PowerShell", "Move-Item .tmp/forged/git-ledger <STATE>", "B"),
    ("PowerShell", "Remove-Item -Recurse -Force <STATE>", "B"),
    ("PowerShell", "Rename-Item <STATE> old-state", "B"),
    ("PowerShell", "Expand-Archive .tmp/forged.zip -DestinationPath <STATE>", "B"),
    ("PowerShell", "New-Item -ItemType Junction -Path <STATE>/git-ledger -Value .tmp/forged", "B"),
    # a link made earlier in the same command: its target is a judged path (`ln`, `New-Item -Value`)
    ("PowerShell", "New-Item -ItemType Junction -Path .tmp/j -Value <STATE>; "
                   "Copy-Item -Recurse .tmp/forged/git-ledger -Destination .tmp/j", "B"),
    ("PowerShell", "New-Item -ItemType Junction -Path .tmp/j -Value <LEDGER>; Set-Content .tmp/j/ledger.jsonl x", "B"),
    ("Bash", "ln -s <STATE> .tmp/j && cp -r .tmp/forged/git-ledger .tmp/j", "B"),
    # every other plain target is compared by containment as well
    ("Bash", "find <STATE> -delete", "B"),
    ("Bash", "chmod -R 777 <STATE>", "B"),
    ("Bash", "curl -O --output-dir <STATE> http://127.0.0.1/x", "B"),
    ("Bash", "cmd /c rd /s /q <STATE>", "B"),
    # near misses: ordinary copies into the worker's own `.tmp/`, a parent of the ledger in this fixture
    ("Bash", "cp -r .tmp/forged/git-ledger .tmp/copy", "A"),
    ("Bash", "cp .tmp/forged/git-ledger/ledger.jsonl .tmp/", "A"),
    ("Bash", "cp -r .tmp/forged/other <STATE>", "B"),  # lands at <STATE>/other, inside the state directory (PD1)
    ("Bash", "tar -xf .tmp/forged.tar -C .tmp/forged", "A"),
    ("Bash", "mkdir -p .tmp", "A"),  # creating an existing parent writes nothing below it
    ("Bash", "rm -rf .tmp/forged/other", "A"),
    ("PowerShell", "Copy-Item -Recurse .tmp/forged/git-ledger -Destination .tmp/copy", "A"),
    ("PowerShell", "Expand-Archive .tmp/forged.zip -DestinationPath .tmp/unpacked", "A"),
]


@pytest.mark.parametrize(("tool", "command", "expect"), EFFECTIVE_CASES)
def test_no_effective_destination_reaches_the_ledger(guard_repo: Path, monkeypatch: pytest.MonkeyPatch, tool: str,
                                                     command: str, expect: str) -> None:
    monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR", raising=False)
    ledger = gl.ledger_dir(guard_repo)
    made = [ledger, guard_repo / ".tmp" / "forged" / "git-ledger", guard_repo / ".tmp" / "forged" / "other"]
    # the fixture repository is shared by the session: remove only the top folders this test creates
    created = [top for top in (ledger.parent, guard_repo / ".tmp" / "forged") if not top.exists()]
    try:
        for folder in made:
            folder.mkdir(parents=True, exist_ok=True)
        text = command.replace("<STATE>", ledger.parent.as_posix()).replace("<LEDGER>", ledger.as_posix())
        for mode in ("worker", "pm"):
            ctx = tg.Context({"tool_name": tool, "tool_input": {"command": text}, "cwd": str(guard_repo)},
                             tg.gc.load(), __import__("argparse").Namespace(config=None, git_mode=mode), guard_repo)
            blocks = tg.check_write(ctx)
            want = expect if mode == "worker" else "A"
            assert ("B" if blocks else "A") == want, (mode, text, blocks)
    finally:
        for top in created:
            shutil.rmtree(top, ignore_errors=True)


# Round 6 (R15, class M): one allowed orchestrator command makes two commits A -> B -> C while a worker turn runs;
# the first after-turn reading sees B, then the command finishes and its PostToolUse entry records A -> C.
def _two_commit_schedule(w: World, monkeypatch: pytest.MonkeyPatch, code: str = "", finish: bool = True,
                         early: tuple[Path, list[str]] | None = None) -> tuple:
    event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m first; git commit -m second"},
             "cwd": str(w.main), "session_id": "pm-session", "tool_use_id": "toolu_two"}
    real_snapshot, calls = cw.git_snapshot, []

    def snapshot(*args: Any) -> Any:  # test double around the real reading: the PM finishes right after it
        value = real_snapshot(*args)
        calls.append(value)
        if len(calls) == 2 and finish:
            git(w.main, *ID, "commit", "-q", "--allow-empty", "-m", "second")
            assert gl.finish(w.main, event, 10)
        return value

    def launch(*args: Any, **kwargs: Any) -> int:
        if early:  # test double of a worker write early in its turn, before the PM command begins
            git(early[0], *ID, *early[1])
        pg.read_event = lambda: json.dumps(event)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            assert tg.main(["--git-mode", "pm"], root=w.main) == 0
        git(w.main, *ID, "commit", "-q", "--allow-empty", "-m", "first")
        return real_launch(*args, **kwargs)

    real_launch = cw.launch_turn
    monkeypatch.setattr(cw, "git_snapshot", snapshot)
    monkeypatch.setattr(cw, "launch_turn", launch)
    entry = {"model": "haiku", "effort": "low", "session_id": "sid", "cwd": str(w.worker), "status": "busy",
             "turns": 0}
    cw.save({"w": entry})
    monkeypatch.setattr(cw, "build_command", lambda *_a: [sys.executable, "-c", fake_turn(code)])
    rc = cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    return rc, cw.load()["w"], calls


def test_a_state_read_mid_orchestrator_write_is_read_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, stored, calls = _two_commit_schedule(world, monkeypatch)
    assert rc == 0 and stored["status"] == "idle" and "git_changes" not in stored, stored
    assert len(calls) == 3  # before, the mid-write reading, the reading after the entry ended


@pytest.mark.parametrize("where", ["own HEAD during the command", "sibling before the command"])
def test_a_worker_write_in_the_same_schedule_is_flagged(world: World, monkeypatch: pytest.MonkeyPatch,
                                                        where: str) -> None:
    """A sibling commit made while the PM command runs is the accepted residual of round 5 (decision 89: the
    command's entry records it), so the sibling case writes before the command begins."""
    commit = ["commit", "-q", "--allow-empty", "-m", "w"]
    if where.startswith("own"):
        rc, stored, _ = _two_commit_schedule(world, monkeypatch, worker_git(world.worker, *commit))
        shown = f"HEAD of {world.worker.as_posix()}"
    else:
        rc, stored, _ = _two_commit_schedule(world, monkeypatch, early=(world.sibling, commit))
        shown = f"HEAD of {world.sibling.as_posix()}"
    assert rc == 1 and stored["status"] == cw.GIT_HISTORY_CHANGED, stored
    assert any(c.startswith(shown) for c in stored["git_changes"]), stored["git_changes"]
    assert not any(c.startswith("refs/heads/main:") for c in stored["git_changes"]), stored["git_changes"]


def test_an_orchestrator_write_that_never_settles_flags_with_its_reason(world: World) -> None:
    """The PM command began during the turn and made its first commit, but its entry never comes (no PostToolUse):
    after the bound (`git_timeout_seconds`, here 1 s) the turn is flagged and the reason names the pending write."""
    worker = {"cwd": str(world.worker), "session_id": "sid"}
    t0 = time.time()
    before = cw.git_snapshot(world.worker, 10)
    event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m first"}, "cwd": str(world.main),
             "session_id": "pm-session", "tool_use_id": "toolu_slow"}
    pg.read_event = lambda: json.dumps(event)
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        assert tg.main(["--git-mode", "pm"], root=world.main) == 0
    git(world.main, *ID, "commit", "-q", "--allow-empty", "-m", "first")
    started = time.monotonic()
    changes = cw.turn_git_changes(worker, before, t0, 1)
    assert time.monotonic() - started < 6
    assert "toolu_slow" in changes[0] and "did not settle within 1 s" in changes[0], changes
    assert any(c.startswith("refs/heads/main:") for c in changes[1:]), changes


def test_an_unsettled_write_that_changed_nothing_flags_nothing(world: World) -> None:
    """Deviation from "the turn is flagged" on every timeout (PM decision pending user review): a pending record
    begun in the window with no Git change has nothing to attribute (an orphan record, left when another hook blocked
    after the guard allowed, would otherwise flag every turn that overlaps it); its later writes follow the reading."""
    worker = {"cwd": str(world.worker), "session_id": "sid"}
    t0 = time.time()
    before = cw.git_snapshot(world.worker, 10)
    event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m first"}, "cwd": str(world.main),
             "session_id": "pm-session", "tool_use_id": "toolu_orphan"}
    pg.read_event = lambda: json.dumps(event)
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        assert tg.main(["--git-mode", "pm"], root=world.main) == 0
    assert gl.pending(world.main, t0, time.time()) == ["toolu_orphan"]
    assert cw.turn_git_changes(worker, before, t0, 1) == []
