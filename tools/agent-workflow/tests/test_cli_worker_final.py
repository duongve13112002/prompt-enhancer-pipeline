"""cli_worker.py, T-024 final round (package D): F05 native write denies, F14 settled first snapshot, F16 audit on
every exit with the flag persisted first, F17 every process of a turn stopped before the final snapshot, and the
fail-closed brief lint.

Repositories are scratch ones under pytest `tmp_path` (never the project repository). Test doubles (labelled): the
worker process is a tiny Python script in place of the Claude CLI; `rule_denies` emulates permission-rule glob
matching (gitignore-style `**`), it is not Claude Code itself; the orchestrator's command is the real
`git_ledger.begin`/`finish` pair around plain git subprocesses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import git_ledger as gl
import pytest
from conftest import git

SCRIPT = Path(__file__).resolve().parents[1] / "cli_worker.py"
spec = importlib.util.spec_from_file_location("cli_worker_final", SCRIPT)
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)

ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
WINDOWS = sys.platform == "win32"
CONTAINED = WINDOWS or sys.platform.startswith("linux")


# ---------------------------------------------------------------- F05: native Edit/Write denies


def rule_denies(rules: list[str], tool: str, path: str, cwd: str) -> bool:
    """Labelled emulation of Claude Code path rules: `//x` absolute, otherwise relative to `cwd`; `**/` is zero or more
    folders, `/**` at the end everything below, `*` one name part."""
    target = path.replace("\\", "/")
    for rule in rules:
        name, _, rest = rule.partition("(")
        if name != tool:
            continue
        pattern = rest[:-1]
        if pattern.startswith("//"):
            pattern = pattern[1:]
        else:
            pattern = cw.permission_path(Path(cwd))[1:] + "/" + pattern
        body = ""
        i = 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                body, i = body + "(?:.*/)?", i + 3
            elif pattern.startswith("/**", i) and i + 3 == len(pattern):
                body, i = body + "/.*", i + 3
            elif pattern[i] == "*":
                body, i = body + "[^/]*", i + 1
            else:
                body, i = body + re.escape(pattern[i]), i + 1
        if re.fullmatch(body, target, re.IGNORECASE if WINDOWS else 0):
            return True
    return False


def as_rule_path(path: Path) -> str:
    return cw.permission_path(path)[1:]


def test_native_writes_to_git_tooling_policy_and_state_are_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    wt = root / ".tmp" / "worktrees" / "w"
    state = root / ".tmp" / "pm-state"
    home = tmp_path / "home"
    for folder in (wt, state, home):
        folder.mkdir(parents=True)
    rules = cw.protected_write_rules(root, state, root / "tools" / "agent-workflow", home)
    denied = [
        wt / ".git",  # the link file of a linked worktree
        root / ".git" / "config",
        root / ".git" / "hooks" / "pre-commit",
        root / ".git" / "worktrees" / "w" / "HEAD",
        wt / ".git" / "info" / "attributes",
        root / "tools" / "agent-workflow" / "hooks" / "tool_guard.py",
        root / "tools" / "agent-workflow" / "x.py",
        root / ".agents" / "policy" / "PM_POLICY.md",
        root / ".agents" / "x.md",
        wt / ".claude" / "skills" / "x" / "SKILL.md",
        wt / ".claude" / "settings.local.json",
        root / ".claude" / "settings.local.json",
        state / "prepush-ok" / "abc",
        state / "cli-workers.json",
        state / "patches" / "T.patch",
        state / "git-ledger" / "ledger.jsonl",
        home / ".claude" / "settings.json",
    ]
    allowed = [wt / "src" / "a.py", wt / "tests" / "test_a.py", wt / ".tmp" / "T-024" / "x.txt",
               root / "src" / "b.py", root / ".tmp" / "other" / "x.txt", wt / "docs" / "agents.md",
               # the worker's own worktree copies of the tooling and the policy: data, the hooks run from the root
               wt / "tools" / "agent-workflow" / "x.py", wt / "tools" / "agent-workflow" / "hooks" / "tool_guard.py",
               wt / ".agents" / "x.md"]
    for tool in cw.NATIVE_WRITE_TOOLS:
        for path in denied:
            assert rule_denies(rules, tool, as_rule_path(path), str(wt)), (tool, path)
        for path in allowed:
            assert not rule_denies(rules, tool, as_rule_path(path), str(wt)), (tool, path)


def test_the_worker_settings_deny_native_writes_to_the_real_root_and_state() -> None:
    deny = cw.SETTINGS["permissions"]["deny"]
    state = Path(gl.ledger_dir(cw.ROOT)).parent.resolve()
    targets = [cw.ROOT / ".git" / "config", cw.ROOT / "tools" / "agent-workflow" / "hooks" / "tool_guard.py",
               cw.ROOT / ".agents" / "ORCHESTRATION.md", cw.ROOT / ".claude" / "settings.local.json",
               state / "prepush-ok" / "abc", state / "cli-workers.json", state / "patches" / "T.patch"]
    for tool in ("Edit", "Write"):
        for path in targets:
            assert rule_denies(deny, tool, as_rule_path(path), str(cw.ROOT)), (tool, path)
        assert not rule_denies(deny, tool, as_rule_path(cw.ROOT / "src" / "x.py"), str(cw.ROOT))
    assert set(deny) <= set(cw.LEGACY_SETTINGS["permissions"]["deny"])


def test_a_state_directory_override_moves_the_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moved = tmp_path / "elsewhere-state"
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(moved))
    assert cw.state_dir(cw.ROOT) == moved
    rules = cw.protected_write_rules(cw.ROOT, cw.state_dir(cw.ROOT), cw.HOOKS.parent, tmp_path)
    assert f"Write({cw.permission_path(moved.resolve())}/**)" in rules
    assert rule_denies(rules, "Edit", as_rule_path(moved / "prepush-ok" / "x"), str(cw.ROOT))


def test_the_worker_hooks_run_from_the_launchers_own_tree_never_the_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What makes a worktree copy of the tooling plain data (the narrowed F05 deny): every hook command of the rendered
    worker settings names an absolute script in the hooks folder next to this cli_worker.py (the main tree in
    production), never one under the worker's cwd."""
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    worktree = cw.ROOT / ".tmp" / "worktrees" / "w"
    cfg = {"process_guard": True, "tool_guard": True, "git_guard": "readonly", "ctx_nudge": "10,20",
           "loop_window": 3, "task_file": str(worktree / "tasks" / "t.md"), "cwd": str(worktree),
           "autocompact_window": 230000}
    hooks = cw.hook_settings(cfg)["hooks"]
    home = (SCRIPT.resolve().parent / "hooks").resolve()
    assert cw.HOOKS.resolve() == home
    scripts = [arg for groups in hooks.values() for group in groups for hook in group["hooks"]
               for arg in hook["args"] if arg.endswith(".py")]
    assert len(scripts) >= 6, scripts
    for script in scripts:
        path = Path(script)
        assert path.is_absolute() and path.resolve().parent == home, script
        assert not path.resolve().is_relative_to(worktree.resolve()), script


def test_status_runs_from_a_copy_without_the_hooks(tmp_path: Path) -> None:
    """The worker settings are built on first use: `status` (pm-supervisor.ps1 runs copies of cli_worker.py) never
    loads hooks/git_ledger.py."""
    tools = tmp_path / "tools" / "agent-workflow"
    tools.mkdir(parents=True)
    (tools / "cli_worker.py").write_bytes(SCRIPT.read_bytes())
    proc = subprocess.run([sys.executable, "-B", str(tools / "cli_worker.py"), "status", "--json"],
                          capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert proc.returncode == 0 and json.loads(proc.stdout) == [], proc.stderr


def test_a_git_link_file_adds_the_common_directory_it_names(tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    private = common / "worktrees" / "r"
    private.mkdir(parents=True)
    root = tmp_path / "r"
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {private.as_posix()}\n", encoding="utf-8")
    rules = cw.protected_write_rules(root, tmp_path / "state", root / "tools" / "agent-workflow", tmp_path)
    assert rule_denies(rules, "Write", as_rule_path(common / "config"), str(root))
    assert rule_denies(rules, "Edit", as_rule_path(private / "HEAD"), str(root))


# ---------------------------------------------------------------- shared world


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "core.autocrlf", "false")
    git(work, *ID, "commit", "-q", "--allow-empty", "-m", "a")
    return work


def entry(cwd: Path, **extra: Any) -> dict[str, Any]:
    return {"model": "haiku", "effort": "low", "session_id": "sid", "cwd": str(cwd), "status": "busy", "turns": 0,
            **extra}


def run(repo: Path, monkeypatch: pytest.MonkeyPatch, code: str = "", timeout_minutes: float = 5) -> tuple[int, dict]:
    """One turn whose worker (test double) runs `code` and prints a result event."""
    cw.save({"w": entry(repo)})
    body = f"import json, os, subprocess, sys, time\n{code}\nprint(json.dumps({{'type': 'result', 'result': 'done'}}))\n"
    monkeypatch.setattr(cw, "build_command", lambda *_a: [sys.executable, "-c", body])
    rc = cw.run_turn("w", cw.load()["w"], "hi", False, timeout_minutes, log_number=1)
    return rc, cw.load()["w"]


def test_a_message_longer_than_a_windows_command_line_reaches_the_worker_on_stdin(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 56 KB brief on argv failed to launch with WinError 206 (limit 32767 characters): the message goes on stdin,
    byte for byte, and never onto the command line."""
    message = "brief line ạ\n" * 5000  # about 70 000 characters, non-ASCII included
    seen = tmp_path / "seen.txt"
    body = (
        "import json, sys\n"
        f"open({str(seen)!r}, 'wb').write(sys.stdin.buffer.read())\n"
        "print(json.dumps({'type': 'result', 'result': 'done'}))\n"
    )
    builder_args: list[tuple[Any, ...]] = []

    def command(*args: Any) -> list[str]:
        builder_args.append(args)
        return [sys.executable, "-c", body]

    real = cw.build_command({**WORKER_ENTRY, "hooks": {}}, resume=False)
    cw.save({"w": entry(repo)})
    monkeypatch.setattr(cw, "build_command", command)
    assert cw.run_turn("w", cw.load()["w"], message, False, 5, log_number=1) == 0
    assert seen.read_bytes() == message.encode("utf-8")
    assert builder_args and all(message not in str(arg) for arg in builder_args[0])
    assert sum(len(arg) for arg in real) < 32767 // 2  # the real command line stays far below the Windows limit


# ---------------------------------------------------------------- F14: the first snapshot is settled


def test_an_orchestrator_write_straddling_the_turn_start_does_not_flag_a_clean_worker(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PM command (real begin/finish) makes two commits A -> B -> C; it begins and makes B before the turn, and
    makes C and writes its entry while the first reading would run. Its entry records A -> C, so a first reading of B
    chains to nothing: the reading must wait for the entry and read C."""
    event = {"tool_name": "Bash", "tool_input": {"command": "git commit; git commit"}, "cwd": str(repo),
             "session_id": "pm-session", "tool_use_id": "toolu_straddle"}
    gl.begin(cw.ROOT, event, repo, 10)
    git(repo, *ID, "commit", "-q", "--allow-empty", "-m", "first")

    def finish() -> None:  # the rest of the orchestrator's command, during the start of the turn
        time.sleep(1.0)
        git(repo, *ID, "commit", "-q", "--allow-empty", "-m", "second")
        assert gl.finish(cw.ROOT, event, 10)

    pm = threading.Thread(target=finish)
    pm.start()
    try:
        rc, stored = run(repo, monkeypatch, "time.sleep(2)")  # the command ends inside a turn that reads B first
    finally:
        pm.join(30)
    assert rc == 0 and stored["status"] == "idle" and "git_changes" not in stored, stored


# ---------------------------------------------------------------- F16: audit on every exit, flag first


def test_a_launcher_failure_after_a_shared_ref_move_still_flags_and_leaves_busy(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch(*args: Any, **kwargs: Any) -> int:  # test double: the worker tags, then the launcher fails
        git(repo, "tag", "worker-tag")
        raise RuntimeError("launcher failed")

    cw.save({"w": entry(repo, launcher_pid=os.getpid(), worker_pid=5)})
    monkeypatch.setattr(cw, "build_command", lambda *_a: ["x"])
    monkeypatch.setattr(cw, "launch_turn", launch)
    with pytest.raises(RuntimeError):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    stored = cw.load()["w"]
    assert stored["status"] == cw.GIT_HISTORY_CHANGED, stored
    assert any("refs/tags/worker-tag" in c for c in stored["git_changes"]), stored
    assert set(stored).isdisjoint(cw.PROCESS_KEYS)
    assert "## git_history_changed" in (cw.JOBS / "w" / "001.result.md").read_text(encoding="utf-8")
    assert "git_audit" not in stored and not list(cw.audit_dir().glob("*.json"))


def test_a_launcher_failure_without_a_git_change_still_leaves_busy(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch(*args: Any, **kwargs: Any) -> int:  # test double: fails without marking the entry itself
        raise RuntimeError("launcher failed")

    cw.save({"w": entry(repo, launcher_pid=os.getpid(), worker_pid=5)})
    monkeypatch.setattr(cw, "build_command", lambda *_a: ["x"])
    monkeypatch.setattr(cw, "launch_turn", launch)
    with pytest.raises(RuntimeError):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    stored = cw.load()["w"]
    assert stored["status"] == "error RuntimeError" and "git_changes" not in stored, stored
    assert set(stored).isdisjoint(cw.PROCESS_KEYS)


def test_a_failing_result_write_still_persists_the_flag(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (cw.JOBS / "w" / "001.result.md").mkdir(parents=True)  # the result text cannot be written there
    with pytest.raises(OSError):
        run(repo, monkeypatch, "subprocess.run(['git', 'tag', 'w2'], check=True)")
    stored = cw.load()["w"]
    assert stored["status"] == cw.GIT_HISTORY_CHANGED and any("refs/tags/w2" in c for c in stored["git_changes"])


def test_a_failed_audit_is_itself_a_flag(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: Any) -> list[str]:  # test double of an audit that cannot run
        raise OSError("disk gone")

    monkeypatch.setattr(cw, "turn_git_changes", broken)
    rc, stored = run(repo, monkeypatch)
    assert rc == 1 and any("Git audit after the turn failed" in c for c in stored["git_changes"]), stored


def test_an_exception_after_busy_never_leaves_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(cw, "lint_gate", lambda *a, **k: None)
    monkeypatch.setattr(cw, "turn_usage_settings", lambda *a: None)
    cw.save({"w": {**entry(tmp_path, status="idle"), "turns": 1, "last_used": cw.now()}})
    msg = tmp_path / "m.md"
    msg.write_text("go", encoding="utf-8")

    def broken(*_a: Any, **_k: Any) -> int:  # test double: the turn fails before launching anything
        raise ValueError("before launch")

    monkeypatch.setattr(cw, "run_turn", broken)
    monkeypatch.setattr(sys, "argv", ["cli_worker.py", "send", "--name", "w", "--message-file", str(msg)])
    with pytest.raises(ValueError):
        cw.main()
    assert cw.load()["w"]["status"] == "error ValueError"


def tamper(case: str, audit: dict[str, Any]) -> None:
    """Test double of what can happen to the evidence between a launcher's death and the next command."""
    start = cw.audit_path(audit["id"])
    if case == "file removed":
        start.unlink()
    elif case == "file forged":
        start.write_text(start.read_text(encoding="utf-8").replace('"before"', '"before" ', 1), encoding="utf-8")
    elif case == "parent removed":
        start.unlink()
        cw.audit_dir().rmdir()
    elif case == "session changed":  # a consistent forgery: new content, and the pin follows it
        text = start.read_text(encoding="utf-8").replace('"session_id": "sid"', '"session_id": "other"')
        start.write_text(text, encoding="utf-8")
        rows = cw.load()
        rows["w"]["git_audit"]["sha256"] = cw.sha256_text(text)
        cw.save(rows)


@pytest.mark.parametrize("case", ["launcher died", "file removed", "file forged", "parent removed", "session changed"])
def test_a_turn_without_a_proven_audit_is_shown_and_flags_the_next_send(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """The launcher died after the start state was pinned (no close): whatever then happens to the evidence in the
    state directory, `status` shows `git_audit_open` and the next `send` flags the worker once."""
    cw.save({"w": {**entry(repo, status="idle"), "turns": 1, "last_used": cw.now()}})
    audit = cw.open_git_audit("w", cw.load()["w"], cw.JOBS / "w" / "002.jsonl", 5)
    assert cw.load()["w"]["git_audit"]["id"] == audit["id"]
    tamper(case, audit)
    monkeypatch.setattr(cw, "lint_gate", lambda *a, **k: None)
    monkeypatch.setattr(cw, "turn_usage_settings", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["cli_worker.py", "status", "--json"])
    capsys.readouterr()
    assert cw.main() == 0
    shown = json.loads(capsys.readouterr().out)[0]["git_audit_open"]
    expected = {"launcher died": "never audited", "file removed": "is missing", "file forged": "was changed",
                "parent removed": "is missing", "session changed": "another session"}[case]
    assert expected in shown, shown
    msg = repo.parent / "m.md"
    msg.write_text("go", encoding="utf-8")
    monkeypatch.setattr(cw, "run_turn", lambda name, w, *a, **k: 0)
    monkeypatch.setattr(sys, "argv", ["cli_worker.py", "send", "--name", "w", "--message-file", str(msg)])
    assert cw.main() == 0
    stored = cw.load()["w"]
    assert any(expected in c for c in stored["git_changes"]) and "git_audit" not in stored, stored
    assert cw.audit_finding(stored) is None  # counted once
    if case in ("launcher died", "file forged", "session changed"):
        assert cw.audit_path(audit["id"], "flagged").exists()  # the evidence is kept for the PM


def test_a_removed_start_state_then_a_launcher_failure_flags_the_turn(repo: Path, monkeypatch: pytest.MonkeyPatch
                                                                    ) -> None:
    """Codex review CX2 reproduction: the worker removes the start state, then the launcher fails."""
    def launch(*args: Any, **kwargs: Any) -> int:  # test double
        for path in cw.audit_dir().glob("*.json"):
            path.unlink()
        raise RuntimeError("launcher failed")

    cw.save({"w": entry(repo)})
    monkeypatch.setattr(cw, "build_command", lambda *_a: ["x"])
    monkeypatch.setattr(cw, "launch_turn", launch)
    with pytest.raises(RuntimeError):
        cw.run_turn("w", cw.load()["w"], "hi", False, 5, log_number=1)
    stored = cw.load()["w"]
    assert stored["status"] == cw.GIT_HISTORY_CHANGED, stored
    assert any("removed or changed during the turn" in c for c in stored["git_changes"]), stored


def test_a_clean_turn_leaves_no_audit_pin_or_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, stored = run(repo, monkeypatch)
    assert rc == 0 and "git_audit" not in stored and cw.audit_finding(stored) is None
    assert not list(cw.audit_dir().glob("*"))
    assert cw.audit_dir().parent == cw.REGISTRY.parent  # beside the registry, in the state directory


@pytest.mark.parametrize("override", [False, True])
def test_the_launchers_state_paths_follow_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: bool
) -> None:
    """REGISTRY, INBOX, LEDGER and the lint waivers follow AGENT_WORKFLOW_STATE_DIR like the hooks (git_ledger)."""
    moved = tmp_path / "moved-state"
    if override:
        monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(moved))
    else:
        monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR", raising=False)
    fresh_spec = importlib.util.spec_from_file_location("cli_worker_state_paths", SCRIPT)
    assert fresh_spec is not None and fresh_spec.loader is not None
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    state = Path(gl.ledger_dir(fresh.ROOT)).parent
    assert state == (moved if override else fresh.ROOT / ".tmp" / "pm-state")
    assert fresh.STATE == state and fresh.state_root() == state
    for path in (fresh.REGISTRY, fresh.INBOX, fresh.LEDGER, fresh.state_root() / fresh.LINT_WAIVERS_NAME):
        assert path.parent == state, path
    if override:
        fresh.log_waiver({"command": "send", "worker": "w", "reason": "test"})
        assert (moved / "lint-waivers.jsonl").exists()


def test_notebook_edit_is_not_offered_to_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """CX6: no primary source shows the Edit/Write denies govern NotebookEdit, so a worker never gets it."""
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    for hooks in ({}, {"tool_guard": True, "process_guard": True}):
        cmd = cw.build_command({**WORKER_ENTRY, "hooks": hooks}, resume=False)
        offered = cmd[cmd.index("--tools") + 1].split(",")
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        assert "NotebookEdit" not in offered and "NotebookEdit" in denied
        assert not any("NotebookEdit" in rule for rule in settings["permissions"]["allow"])
        assert {"Edit", "Write"} <= set(offered)  # the writers the deny rules govern


WORKER_ENTRY = {"model": "haiku", "effort": "low", "session_id": "sid-1", "cwd": str(cw.ROOT)}


def test_an_error_status_keeps_the_history_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    cw.save({"w": entry(tmp_path, git_changes=["refs/heads/main: 1 -> 2"], worker_pid=3)})
    cw.mark_error("w", "error KeyboardInterrupt", "sid")
    stored = cw.load()["w"]
    assert stored["status"] == cw.GIT_HISTORY_CHANGED and "worker_pid" not in stored


# ---------------------------------------------------------------- F17: every process of the turn ends


# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, not DETACHED_PROCESS: the venv `python.exe` is a redirector that
# starts the base interpreter as its own child, and a console child of a process with no console gets a new visible
# console window (Windows Terminal kept one open per sleeper with error 0x800700e8 once the turn killed it). A
# hidden console is inherited by that child, so nothing shows, and the sleeper still leaves the parent's group.
DETACH = (
    "flags = 0x08000000 | 0x00000200 if os.name == 'nt' else 0\n"
    "def detached(code):\n"
    "    return subprocess.Popen([sys.executable, '-c', code], creationflags=flags, start_new_session=os.name != 'nt',\n"
    "                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
)


def spawn_code(record: Path, through_orphan: bool) -> str:
    """Worker double: start a detached sleeper (Windows: a hidden console and a new process group; POSIX: a new
    session), directly or through an intermediate that exits at once (so the sleeper loses its parent), record its
    PID, exit."""
    sleeper = f"import os, time; open({str(record)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    if through_orphan:
        middle = DETACH + f"detached({sleeper!r})\n"
        code = DETACH + f"detached({('import os, subprocess, sys' + chr(10) + middle)!r}).wait()\n"
    else:
        code = DETACH + f"detached({sleeper!r})\n"
    return code + f"deadline = time.time() + 30\nwhile not os.path.exists({str(record)!r}) and time.time() < deadline:\n    time.sleep(0.05)\n"


def alive(pid: int) -> bool:
    return cw.process_start(pid) is not None


@pytest.mark.skipif(not CONTAINED, reason="process containment exists on Windows and Linux only")
@pytest.mark.parametrize("through_orphan", [False, True], ids=["detached child", "orphaned grandchild"])
@pytest.mark.parametrize("exit_path", ["normal", "timeout"])
def test_a_detached_process_of_the_turn_is_stopped_before_the_final_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, through_orphan: bool, exit_path: str
) -> None:
    record = tmp_path / "sleeper.pid"
    code = spawn_code(record, through_orphan)
    if exit_path == "timeout":
        code += "time.sleep(60)\n"  # the worker outlives its 4 s limit and is killed
    real_snapshot, states = cw.git_snapshot, []

    def snapshot(*args: Any) -> Any:  # test double around the real reading: is the sleeper alive at each one?
        if record.exists():
            states.append(alive(int(record.read_text(encoding="utf-8"))))
        return real_snapshot(*args)

    monkeypatch.setattr(cw, "git_snapshot", snapshot)
    pid = None
    try:
        rc, stored = run(repo, monkeypatch, code, timeout_minutes=4 / 60 if exit_path == "timeout" else 5)
        pid = int(record.read_text(encoding="utf-8"))
        assert states and states[-1] is False, states  # gone at the final snapshot
        assert not alive(pid)
        assert "git_changes" not in stored, stored
        assert rc == (1 if exit_path == "timeout" else 0)
    finally:
        if pid is not None and alive(pid):
            cw.kill_tree(pid, cw.process_start(pid))


@pytest.mark.skipif(not WINDOWS, reason="job breakaway is a Windows mechanism")
def test_a_child_asking_to_break_away_from_the_job_still_ends_with_the_turn(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CREATE_BREAKAWAY_FROM_JOB: refused (ERROR_ACCESS_DENIED) or, when an outer job allows breakaway, the child
    leaves only that one and stays in the worker's job (probe_breakaway.out, 2026-11-15): either way it is gone."""
    out = tmp_path / "breakaway.txt"
    code = (
        "try:\n"
        "    c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x01000000)\n"
        f"    open({str(out)!r}, 'w').write(str(c.pid))\n"
        "except OSError as exc:\n"
        f"    open({str(out)!r}, 'w').write('refused %s' % getattr(exc, 'winerror', None))\n"
    )  # 0x01000000: CREATE_BREAKAWAY_FROM_JOB
    rc, _ = run(repo, monkeypatch, code)
    text = out.read_text(encoding="utf-8")
    assert rc == 0 and (text == "refused 5" or not alive(int(text))), text


def test_a_process_that_outlives_the_stop_flags_the_turn(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_close = cw.TurnContainment.close

    def stubborn(self: Any, bound: float) -> list[str]:  # test double: one process survived the stop
        return [*real_close(self, bound), "1 process(es) of the worker's job still ran"]

    monkeypatch.setattr(cw.TurnContainment, "close", stubborn)
    rc, stored = run(repo, monkeypatch)
    assert rc == 1 and stored["status"] == cw.GIT_HISTORY_CHANGED
    assert any("outlived its stop" in c for c in stored["git_changes"]), stored


def test_containment_that_cannot_be_set_up_refuses_the_turn(repo: Path, monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    def unavailable(self: Any) -> None:  # test double: no job object / no subreaper on this host
        raise cw.ContainmentError("no process containment here")

    started = tmp_path / "started"
    monkeypatch.setattr(cw.TurnContainment, "__init__", unavailable)
    with pytest.raises(cw.ContainmentError):
        run(repo, monkeypatch, f"open({str(started)!r}, 'w').close()")
    assert not started.exists() and cw.load()["w"]["status"] == "error ContainmentError"


# ---------------------------------------------------------------- brief lint fails closed


def test_a_crashing_brief_lint_refuses_the_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "ROOT", tmp_path)

    def boom(*args: object, **kwargs: object) -> None:  # test double: the linter cannot be loaded
        raise RuntimeError("x")

    monkeypatch.setattr(cw.importlib.util, "spec_from_file_location", boom)
    with pytest.raises(SystemExit, match=r"brief lint crashed \(RuntimeError: x\); send w refused"):
        cw.lint_gate("send", "w", "anything", "a waiver covers findings only")
    row = json.loads((tmp_path / ".tmp" / "pm-state" / "lint-waivers.jsonl").read_text(encoding="utf-8"))
    assert row["lint_error"] == "RuntimeError"
