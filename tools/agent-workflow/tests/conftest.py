"""Shared test settings for tools/agent-workflow/tests.

Property-based tests (decision 130): the default `ci` profile is derandomized and keeps no example database, so CI
and local runs see the same examples; reviewers hunt with `--hypothesis-profile=explore`. Hypothesis storage stays
under the project-local `.tmp` (as tests/conftest.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import hypothesis
import pytest
from hypothesis import settings

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
# The guard hooks import their siblings by name (they run with `python -I`).
sys.path.insert(0, str(HOOKS))

hypothesis.configuration.set_hypothesis_home_dir(
    os.environ.get("HYPOTHESIS_STORAGE_DIRECTORY")
    or Path(__file__).resolve().parents[3] / ".tmp" / "hypothesis"
)
settings.register_profile("ci", derandomize=True, database=None, deadline=None)
settings.register_profile("explore", max_examples=2000, database=None, deadline=None)
settings.load_profile("ci")

import live_state_guard as lsg  # noqa: E402 - sits beside this file; needs the sys.path entry pytest adds

# Live-state guard (decision 72): no test reads or writes the live PM state directory. Installed at import, before any
# test module is collected, so import-time reads are covered too.
lsg.install()
STATE_ENV = lsg.STATE_ENV
_LIVE_BEFORE: dict[str, dict[str, tuple[int, int, int]]] = {}
_LIVE_CHANGES: list[str] = []


def pytest_sessionstart(session: pytest.Session) -> None:
    _LIVE_BEFORE.update(lsg.snapshot_all())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    after = lsg.snapshot_all()
    for root, before in _LIVE_BEFORE.items():
        _LIVE_CHANGES.extend(f"{root}: {line}" for line in lsg.changes(before, after.get(root, {})))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    # Metadata only (a snapshot sees writes by any process, never reads): other sessions may change the live
    # directory legitimately, so a change is reported, not failed; the in-process guard fails the test itself.
    if _LIVE_CHANGES:
        terminalreporter.section("live PM state metadata changed during the run (writer unknown)")
        for line in _LIVE_CHANGES[:20]:
            terminalreporter.line(line)
    else:
        terminalreporter.line("live PM state: no metadata change during the run (test-slots excluded)")


@pytest.fixture(autouse=True)
def pm_state_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Every test gets its own empty PM state directory through AGENT_WORKFLOW_STATE_DIR, so an implicit state
    default can never reach the live one and every child process inherits the same routing. A test that checks the
    default rule (`<root>/.tmp/pm-state` of a scratch root) calls `monkeypatch.delenv(STATE_ENV)` itself. The
    teardown fails the test when the access guard saw the live directory, even if the error was swallowed."""
    state = tmp_path_factory.mktemp("pm-state")
    monkeypatch.setenv(STATE_ENV, str(state))
    seen = len(lsg.violations())
    yield state
    assert lsg.violations()[seen:] == [], "test touched the live PM state directory"


GITATTRIBUTES ="* text=auto\n*.md text eol=lf\n*.py text eol=lf\n*.txt text eol=lf\n*.png binary\n"


def git(repo: Path, *args: str) -> str:
    """Run git in a test-owned fixture repository (never the project repository)."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=guard-test",
            "-c",
            "user.email=guard-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.hooksPath=",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


def make_link(link: Path, target: Path) -> bool:
    """A directory junction (Windows) or symlink (POSIX); False when the platform refuses."""
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


@pytest.fixture(scope="session")
def guard_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fixture Git repository laid out like the project: tracked `tasks/STATUS.md` and `src/fix.py`, a tracked
    file forced under `.tmp/`, a registered worktree `.tmp/worktrees/wt1`, a junction `.tmp/junction_to_root` to the
    root, and untracked `.tmp/scratch/` and `runs/`."""
    repo = Path(os.path.realpath(tmp_path_factory.mktemp("guard-repo")))
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitattributes").write_bytes(GITATTRIBUTES.encode())
    (repo / ".gitignore").write_bytes(b".tmp/\nruns/\n")
    for rel, data in {
        "tasks/STATUS.md": b"# status\n",
        "src/fix.py": b"x = 1\n",
        "docs/a.md": b"a\n",
        "assets/sample.png": b"\x89PNG\r\n\x1a\n\x00\x00\r\n",
        "frontend/app.js": b"let a = 1;\n",
        ".tmp/tracked.txt": b"tracked under .tmp\n",
    }.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "add", "-f", ".tmp/tracked.txt")
    git(repo, "commit", "-q", "-m", "fixture")
    (repo / ".tmp" / "worktrees").mkdir(parents=True)
    git(repo, "worktree", "add", "-q", str(repo / ".tmp" / "worktrees" / "wt1"), "-b", "wt1")
    (repo / ".tmp" / "scratch").mkdir()
    (repo / "runs").mkdir()
    make_link(repo / ".tmp" / "junction_to_root", repo)
    return repo
