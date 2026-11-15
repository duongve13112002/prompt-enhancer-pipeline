"""push_guard in tool_guard.py and prepush_mark.py (decision 503): `git push` needs the marker of the pushed HEAD.

Every repository and marker lives under pytest's tmp_path; the project's own marker directory is never touched.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest
import tool_guard as tg
from conftest import git
from hypothesis import given
from hypothesis import strategies as st

SHIPPED = gc.load()
# The marker logic is tested on its own here: the git guard's write forms (T-024 round 2) would refuse the bare
# `git push` spellings these cases use; test_tool_guard.py tests that guard.
CFG = {**SHIPPED, "git_guard": {**SHIPPED["git_guard"], "mode": "off"}}
MARKER_DIR = ".tmp/pm-state/prepush-ok"
SCRIPT = Path(__file__).resolve().parents[1] / "prepush_mark.py"
spec = importlib.util.spec_from_file_location("prepush_mark", SCRIPT)
assert spec is not None and spec.loader is not None
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """A project root that is also a git repository with one commit."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "one")
    return root


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def mark(root: Path, sha: str) -> None:
    marker = root / MARKER_DIR / sha
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok\n", encoding="utf-8")


def verdict(
    root: Path,
    command: str,
    tool: str = "Bash",
    cwd: Path | None = None,
    mode: str = "pm",
    cfg: dict[str, Any] = CFG,
) -> tuple[str, list[str]]:
    args = argparse.Namespace(config=None, git_mode=mode, worktree=None)
    event = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "t-push",
        "cwd": str(cwd or root),
    }
    blocks, warns = tg.evaluate(tg.Context(event, cfg, args, root))
    return ("B" if blocks else "W" if warns else "A"), blocks + warns


BLOCKED = [
    "git push",
    "git-push origin main",
    "git push origin main",
    "git push -u origin main",
    "git -c core.quotepath=false push",
    "git status && git push",
    "git status; git push origin main",
    "(git push)",
    "git --no-pager push",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_push_without_marker_is_blocked_and_names_sha_and_lint(proj: Path, command: str) -> None:
    got, messages = verdict(proj, command)
    assert got == "B", messages
    text = "\n".join(messages)
    assert head(proj) in text
    assert CFG["push_guard"]["check_command"] in text
    assert "tools/agent-workflow/prepush_check.py" in text  # a tracked script (T-024 item 5)


@pytest.mark.parametrize("command", BLOCKED)
def test_push_with_marker_passes(proj: Path, command: str) -> None:
    mark(proj, head(proj))
    assert verdict(proj, command)[0] == "A"


def test_marker_is_per_head(proj: Path) -> None:
    mark(proj, head(proj))
    (proj / "a.txt").write_text("b\n", encoding="utf-8")
    git(proj, "commit", "-q", "-am", "two")
    assert verdict(proj, "git push")[0] == "B"
    mark(proj, head(proj))
    assert verdict(proj, "git push")[0] == "A"


def test_marker_for_a_prefix_or_other_name_does_not_count(proj: Path) -> None:
    sha = head(proj)
    mark(proj, sha[:12])
    mark(proj, "latest")
    assert verdict(proj, "git push")[0] == "B"


def test_powershell_spellings(proj: Path) -> None:
    for command in ("git push", "& git push origin main", "git.exe push", "& 'git' push"):
        assert verdict(proj, command, tool="PowerShell")[0] == "B", command
    mark(proj, head(proj))
    assert verdict(proj, "& git.exe push origin main", tool="PowerShell")[0] == "A"


def test_c_dir_selects_the_pushing_repository(proj: Path, other: Path, tmp_path: Path) -> None:
    mark(proj, head(proj))  # the project's HEAD is marked, the other repository's is not
    here = tmp_path
    assert verdict(proj, f"git -C {other.as_posix()} push", cwd=here)[0] == "B"
    assert verdict(proj, f"git -C {proj.as_posix()} push", cwd=other)[0] == "A"
    assert verdict(proj, "git -C ../proj push", cwd=other)[0] == "A"
    assert verdict(proj, "git -C ../other push", cwd=proj)[0] == "B"
    assert verdict(proj, "git -C .. -C proj push", cwd=other)[0] == "A"


@pytest.fixture
def other(tmp_path: Path) -> Path:
    """A second repository next to `proj` whose HEAD is never marked unless a test marks it."""
    repo = tmp_path / "other"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "o.txt").write_text("o\n", encoding="utf-8")
    git(repo, "add", "o.txt")
    git(repo, "commit", "-q", "-m", "o")
    return repo


def test_git_c_into_the_unmarked_repo_from_its_own_directory_is_blocked(
    proj: Path, other: Path
) -> None:
    assert verdict(proj, f"git -C {other.as_posix()} push", cwd=other)[0] == "B"


# Directory-changing forms. The start directory is the marked project; the target is an unmarked repository.
CD_BASH = [
    "cd {d} && git push",
    "cd {d}; git push origin main",
    "(cd {d} && git push)",
    "{{ cd {d} && git push; }}",
    "cd {d}\ngit push",
    "chdir {d} && git push",
    "pushd {d} && git push",
    "true && cd {d} && git status && git push",
    "cd '{d}' && git push",
]
CD_PS = [
    "Set-Location {d}; git push",
    "Set-Location -Path {d}; git push",
    "sl {d}; git push",
    "cd {d}; git push",
    "Push-Location {d}; git push",
    "pushd {d}; git push",
    "Set-Location {d} && git push",
    "& {{ Set-Location {d}; git push }}",
]


@pytest.mark.parametrize("form", CD_BASH)
def test_cd_into_an_unmarked_repo_is_blocked_although_the_start_repo_is_marked(
    proj: Path, other: Path, form: str
) -> None:
    mark(proj, head(proj))
    got, messages = verdict(proj, form.format(d=other.as_posix()), cwd=proj)
    assert got == "B", messages
    assert head(other) in "\n".join(messages)
    assert "git -C <dir> push" in "\n".join(messages)


@pytest.mark.parametrize("form", CD_PS)
def test_powershell_directory_change_into_an_unmarked_repo_is_blocked(
    proj: Path, other: Path, form: str
) -> None:
    mark(proj, head(proj))
    got, messages = verdict(proj, form.format(d=str(other)), tool="PowerShell", cwd=proj)
    assert got == "B", messages
    assert head(other) in "\n".join(messages)


@pytest.mark.parametrize("form", ["cd {d} && git push", "Set-Location {d}; git push"])
def test_cd_with_every_candidate_marked_passes(proj: Path, other: Path, form: str) -> None:
    mark(proj, head(proj))
    mark(proj, head(other))
    tool = "PowerShell" if form.startswith("Set-") else "Bash"
    assert verdict(proj, form.format(d=other.as_posix()), tool=tool, cwd=proj)[0] == "A"


def test_cd_from_an_unmarked_start_repo_into_a_marked_one_is_blocked(
    proj: Path, other: Path
) -> None:
    mark(
        proj, head(other)
    )  # only the cd target is marked; the start repository is the one that lacks it
    got, messages = verdict(proj, f"cd {other.as_posix()} && git push", cwd=proj)
    assert got == "B"
    assert head(proj) in "\n".join(messages)


def test_cd_inside_the_same_repository_needs_one_marker(proj: Path) -> None:
    (proj / "sub").mkdir()
    assert verdict(proj, "cd sub && git push")[0] == "B"
    mark(proj, head(proj))
    assert verdict(proj, "cd sub && git push")[0] == "A"


def test_absolute_git_c_after_a_cd_ignores_the_start_directory(proj: Path, other: Path) -> None:
    mark(proj, head(other))
    assert (
        verdict(proj, f"cd {proj.as_posix()} && git -C {other.as_posix()} push", cwd=proj)[0] == "A"
    )


def test_message_without_a_directory_change_has_no_cd_hint(proj: Path) -> None:
    _got, messages = verdict(proj, "git push")
    assert "git -C <dir> push" not in "\n".join(messages)


def test_dry_run_passes(proj: Path) -> None:
    for command in ("git push --dry-run", "git push -n origin main", "git push --dry origin"):
        assert verdict(proj, command)[0] == "A", command


@pytest.mark.parametrize(
    "command",
    [
        "git push -o -n origin",
        "git push --push-option -n origin",
        "git push --push-option=-n origin",
        "git push -uo -n origin",
        "git push -o--dry-run origin",
        "git push --receive-pack --dry-run origin",
        "git push --repo --dry-run",
        "git push --exec -n origin",
    ],
)
def test_a_dry_run_look_alike_option_value_is_not_a_dry_run(proj: Path, command: str) -> None:
    assert verdict(proj, command)[0] == "B", command


@pytest.mark.parametrize(
    "command",
    [
        "git push -o ci.skip -n origin",
        "git push --push-option x --dry-run",
        "git push -uo x -n origin",
    ],
)
def test_a_real_dry_run_after_an_option_value_passes(proj: Path, command: str) -> None:
    assert verdict(proj, command)[0] == "A", command


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -- a.txt && git push",
        "git checkout HEAD -- a.txt && git push",
        "git reset -- a.txt && git push",
        "git reset HEAD -- a.txt; git push",
        "git checkout main -- a.txt b.txt && git push",
        "git checkout $BRANCH -- a.txt && git push",
        "git checkout main -- $FILES a.txt && git push",
        "git symbolic-ref HEAD && git push",
        "git symbolic-ref --short HEAD; git push",
    ],
)
def test_path_only_checkout_and_reset_do_not_move_head(proj: Path, command: str) -> None:
    mark(proj, head(proj))
    assert verdict(proj, command)[0] == "A", command


@pytest.mark.parametrize(
    "command",
    [
        "git checkout main && git push",
        "git checkout -b x -- && git push",
        "git reset --hard HEAD~1 && git push",
        "git reset --soft HEAD~1 -- && git push",
        "git checkout main -- && git push",
        "git reset HEAD~1 -- && git push",
        "git checkout -- && git push",
        'git checkout main -- "" && git push',
        "git checkout --detach -- && git push",
        "git switch main && git push",
        # A word after `--` that may expand to nothing leaves a bare `--` (F1, review 1).
        "git checkout other -- $EMPTY && git push",
        'git checkout other -- "$EMPTY" && git push',
        "git checkout other -- ${EMPTY} && git push",
        "git checkout other -- $(true) && git push",
        "git checkout other -- `true` && git push",
        "git checkout other -- @() ; git push",
        "git checkout other -- $env:EMPTY ; git push",
        "git checkout other -- %EMPTY% && git push",
        "git reset HEAD~1 -- $EMPTY && git push",
        # Other ways to move HEAD, an alias for one, and a subcommand that is not a known built-in (F3).
        "git update-ref HEAD other && git push",
        "git symbolic-ref HEAD refs/heads/other && git push",
        "git stash pop && git push",
        "git bisect start && git push",
        "git co other && git push",
        "git co other -- a.txt && git push",
        "git -c alias.co=checkout co other && git push",
        "git -calias.co=checkout co other && git push",
        "git -c alias.status=checkout status && git push",
        "git --config-env=alias.status=ALIAS status && git push",
        "git $SUB other && git push",
        "git-co other && git push",
    ],
)
def test_head_moving_checkout_and_reset_still_block(proj: Path, command: str) -> None:
    mark(proj, head(proj))
    got, messages = verdict(proj, command)
    assert got == "B", command
    assert "moves HEAD" in "\n".join(messages)


@pytest.mark.parametrize(
    "command",
    [
        "git fetch && git push",
        "git log --oneline -3; git push",
        "git add a.txt && git push",
        "git diff --stat && git push",
        "git branch -a && git push",
        "git tag v1 && git push",
        "git remote -v && git push",
        "git rev-parse HEAD && git push",
        "git -c core.quotepath=false status && git push",
        "git-fetch && git push",
        "git-status.exe; git push",
    ],
)
def test_known_non_moving_builtins_do_not_block_a_following_push(proj: Path, command: str) -> None:
    mark(proj, head(proj))
    assert verdict(proj, command)[0] == "A", command


def test_head_mover_and_non_moving_sets_are_disjoint_and_symbolic_ref_reads_are_safe() -> None:
    assert not tg.HEAD_MOVERS & tg.NON_MOVING_GIT
    assert not tg._moves_head("symbolic-ref", ["HEAD"])
    assert tg._moves_head("symbolic-ref", ["HEAD", "refs/heads/x"])
    assert tg._moves_head("symbolic-ref", ["-d", "HEAD"])


@given(st.from_regex(r"[a-z][a-z0-9-]{0,12}", fullmatch=True))
def test_property_a_subcommand_that_is_not_a_known_builtin_may_move_head(name: str) -> None:
    known = tg.HEAD_MOVERS | tg.NON_MOVING_GIT
    assert tg._moves_head(name, []) is (name not in tg.NON_MOVING_GIT)
    if name in tg.NON_MOVING_GIT:
        assert tg._moves_head(name, [], alias_cfg=True)  # `-c alias.*` on the command line
    assert name in known or tg._moves_head(name, ["-x", "--", "a.txt"])


WORD_PARTS = st.sampled_from(
    ["a", "b.txt", "$", "`", "@(", ")", "%", "X", "{", "}", ":", "env", '"', "-", ""]
)


@given(st.lists(st.lists(WORD_PARTS, max_size=4).map("".join), max_size=4))
def test_property_checkout_dash_dash_is_path_only_only_with_a_sure_nonempty_path(
    words: list[str],
) -> None:
    def surely_a_path(w: str) -> bool:
        return bool(w) and not re.search(r"[$`]|@[({]|@$|%[^%\s]+%", w)

    assert tg._moves_head("checkout", ["main", "--", *words]) is (
        not any(surely_a_path(w) for w in words)
    )


def test_commit_and_push_in_one_command_is_blocked_even_with_the_old_marker(proj: Path) -> None:
    mark(proj, head(proj))
    got, messages = verdict(proj, 'git commit -m "x" && git push')
    assert got == "B"
    assert "moves HEAD" in "\n".join(messages)
    assert (
        verdict(proj, "git push && git commit -m x")[0] == "A"
    )  # the push runs at the marked HEAD


def test_undeterminable_repository_fails_closed(
    proj: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-024 final (F22, PD2): a push whose repository, HEAD or branch the guard cannot read is refused."""
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES", str(tmp_path)
    )  # tmp_path may sit inside a work tree
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    mark(proj, head(proj))  # the start repository is marked: only the unprovable part refuses
    got, messages = verdict(proj, "git push", cwd=not_a_repo)
    assert got == "B" and "cannot read HEAD" in "\n".join(messages)
    assert "chosen at run time" in "\n".join(verdict(proj, "git -C $DIR push")[1])
    assert verdict(proj, "cd $DIR && git push")[0] == "B"
    assert "--git-dir" in "\n".join(verdict(proj, "git --git-dir=x push")[1])
    # Read and repair commands next to the word stay open.
    for command in ("git status", "git log --grep=push", "cd $DIR && git log --grep push", "git push --dry-run"):
        assert verdict(proj, command)[0] == "A", command


def test_force_push_stays_blocked_by_the_git_guard_with_a_marker(proj: Path) -> None:
    mark(proj, head(proj))
    assert verdict(proj, "git push --force", cfg=SHIPPED)[0] == "B"
    assert verdict(proj, "git push origin main", cfg=SHIPPED)[0] == "A"  # the one push form, marked


FORCE_SPELLINGS = [
    "git push --force origin main",
    "git push -f origin main",
    "git push origin main --force",
    "git push --force-with-lease origin main",
    "git push --force-if-includes origin main",
    "git push --forc origin main",
    "git push origin +main",
    "git push origin +main:main",
    "git push -uf origin main",
    "git push --mirror origin",
    "git push --delete origin main",
    "git push origin :main",
    "git -c push.default=matching push",
]


@pytest.mark.parametrize("command", FORCE_SPELLINGS)
def test_pd3_the_structural_guard_refuses_every_force_spelling_with_a_marker(proj: Path, command: str) -> None:
    """PD3 evidence: with the marker present and the shipped config, the git guard (pm mode) refuses each spelling
    the settings glob `git push * --force*` covered, and more; the plain form passes."""
    mark(proj, head(proj))
    assert verdict(proj, command, cfg=SHIPPED)[0] == "B", command
    assert verdict(proj, "git push origin main", cfg=SHIPPED)[0] == "A"


def test_other_commands_and_modes_are_untouched(proj: Path) -> None:
    assert verdict(proj, "git status")[0] == "A"
    assert verdict(proj, "echo push")[0] == "A"
    assert verdict(proj, "git log --grep=push")[0] == "A"
    # A worker never pushes: its own rule blocks it, this check stays out of the way.
    assert verdict(proj, "git push", mode="worker", cfg=SHIPPED)[0] == "B"
    assert not tg.check_push(
        tg.Context(
            {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(proj)},
            CFG,
            argparse.Namespace(config=None, git_mode="worker", worktree=None),
            proj,
        )
    )


def test_mode_off_and_warn_overrides(proj: Path) -> None:
    for mode, expect in (("off", "A"), ("warn", "W")):
        cfg = copy.deepcopy(CFG)
        cfg["push_guard"]["mode"] = mode
        assert verdict(proj, "git push", cfg=cfg)[0] == expect, mode


def test_override_marker_dir_and_check_command(proj: Path) -> None:
    cfg = copy.deepcopy(CFG)
    cfg["push_guard"]["marker_dir"] = "custom/ok"
    cfg["push_guard"]["check_command"] = "run-my-lint"
    got, messages = verdict(proj, "git push", cfg=cfg)
    assert got == "B"
    assert "run-my-lint" in "\n".join(messages)
    marker = proj / "custom" / "ok" / head(proj)
    marker.parent.mkdir(parents=True)
    marker.write_text("ok", encoding="utf-8")
    assert verdict(proj, "git push", cfg=cfg)[0] == "A"


def test_a_crash_in_the_check_fails_closed_and_is_logged(
    proj: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F22 (PD2). Test double (labelled): `_push_reasons` replaced by a function that raises."""

    def boom(_ctx: Any) -> list[str]:
        raise RuntimeError("boom")

    mark(proj, head(proj))
    monkeypatch.setattr(tg, "_push_reasons", boom)
    monkeypatch.setattr(tg, "CRASH_LOG", tmp_path / "crash.log")
    got, messages = verdict(proj, "git push origin main")
    assert got == "B" and "push check failed (RuntimeError)" in "\n".join(messages)
    assert "push_guard" in (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert verdict(proj, "git status")[0] == "A"  # no push word: the check never runs
    assert verdict(proj, "git log -3")[0] == "A"


def test_a_crash_outside_the_check_body_fails_closed_for_a_push(
    proj: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F22: the check's trigger is the word `push`, so evaluate() blocks when the whole check raises. Test double
    (labelled): `check_push` replaced through the checks table."""

    def boom(_ctx: Any) -> list[str]:
        raise RuntimeError("boom")

    real = tg.checks
    monkeypatch.setattr(
        tg, "checks", lambda ctx: [(s, boom if s == "push_guard" else c, t) for s, c, t in real(ctx)]
    )
    monkeypatch.setattr(tg, "CRASH_LOG", tmp_path / "crash.log")
    assert verdict(proj, "git push origin main")[0] == "B"
    assert verdict(proj, "git status")[0] == "W"  # no push word: the crash only warns


@pytest.mark.parametrize("failure", ["oserror", "timeout", "rc", "garbage"])
def test_git_failure_fails_closed(proj: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """F22: git missing, timing out, failing or printing something that is no object id refuses the push. Test
    double (labelled): `subprocess.run` replaced inside tool_guard only for the push source reading."""
    mark(proj, head(proj))
    real_run = subprocess.run

    def broken(cmd: Any, *a: Any, **k: Any) -> Any:
        if "for-each-ref" not in cmd:
            return real_run(cmd, *a, **k)
        if failure == "oserror":
            raise OSError("no git")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        out = b"" if failure == "rc" else b"* not-an-id\n"
        return subprocess.CompletedProcess(cmd, 1 if failure == "rc" else 0, out, b"")

    monkeypatch.setattr(tg.subprocess, "run", broken)
    got, messages = verdict(proj, "git push origin main")
    assert got == "B" and "cannot read HEAD" in "\n".join(messages), messages
    assert verdict(proj, "git status")[0] == "A"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "maybe"),
        ("marker_dir", "../outside"),
        ("marker_dir", "C:/abs"),
        ("marker_dir", ""),
        ("lint_command", ""),
        ("lint_command", 3),
    ],
)
def test_invalid_push_guard_config_is_refused(field: str, value: Any) -> None:
    data = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    data["push_guard"][field] = value
    with pytest.raises(gc.ConfigError):
        gc.validate(data)


def test_missing_or_unknown_push_guard_field_is_refused() -> None:
    data = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["push_guard"]["marker_dir"]
    with pytest.raises(gc.ConfigError, match="marker_dir"):
        gc.validate(data)
    data = json.loads(gc.DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["push_guard"]
    with pytest.raises(gc.ConfigError, match="push_guard"):
        gc.validate(data)


def test_hook_process_blocks_push_with_exit_2(proj: Path) -> None:
    """End to end through the real hook entry: exit code 2 and the reason on stderr."""
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push"},
        "cwd": str(proj),
        "session_id": "s",
    }
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(Path(tg.__file__)), "--git-mode", "pm"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # The hook's root is the real project root, whose marker for this fixture's HEAD cannot exist.
    assert proc.returncode == 2, proc.stderr
    assert "pre-push marker" in proc.stderr


# prepush_mark.py


def test_prepush_mark_writes_the_marker_for_head(proj: Path) -> None:
    assert pm.main([str(proj)], root=proj) == 0
    marker = proj / MARKER_DIR / head(proj)
    assert marker.is_file()
    assert verdict(proj, "git push")[0] == "A"


def test_prepush_mark_marks_the_given_repository_not_the_root(proj: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "o.txt").write_text("o\n", encoding="utf-8")
    git(other, "add", "o.txt")
    git(other, "commit", "-q", "-m", "o")
    assert pm.main([str(other)], root=proj) == 0
    assert (proj / MARKER_DIR / head(other)).is_file()
    assert not (proj / MARKER_DIR / head(proj)).exists()


def test_prepush_mark_refuses_a_directory_without_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")  # no commit: no HEAD
    assert pm.main([str(empty)], root=tmp_path) == 1
    assert "no readable HEAD" in capsys.readouterr().err
    assert not (tmp_path / ".tmp").exists()


def test_prepush_mark_script_runs_standalone_in_a_copied_tree(proj: Path, tmp_path: Path) -> None:
    """The script's root is derived from its location: run a copy inside a fake project so nothing real is marked."""
    tree = tmp_path / "tree"
    tools = tree / "tools" / "agent-workflow"
    (tools / "hooks").mkdir(parents=True)
    shutil.copy(SCRIPT, tools / "prepush_mark.py")
    shutil.copy(Path(gc.__file__), tools / "hooks" / "guard_config.py")
    shutil.copy(gc.DEFAULT_PATH, tools / "guard_config.json")
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(tools / "prepush_mark.py"), str(proj)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tree / MARKER_DIR / head(proj)).is_file()
