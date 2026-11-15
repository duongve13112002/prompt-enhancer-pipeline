"""tool_guard.py (PreToolUse): write guard, git guard, inline interpreter, pytest slot, read router, failure policy.

Table rows carry the pre-mortem case IDs (tasks/R3_GUARD_HOOKS_PREMORTEM.md) or `X-` for added cases. Verdicts:
B block, W warn (additionalContext), A allow. Commands are only parsed, never run; path checks use the `guard_repo`
fixture repository (conftest.py).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import guard_config as gc
import process_guard as pg
import pytest
import shell_targets as st
import tool_guard as tg
from hypothesis import given
from hypothesis import strategies as s

HOOKS = Path(tg.__file__).resolve().parent
_SHIPPED = gc.load()
# The push marker is tested in test_push_guard.py; here `git push` cases only exercise the force-push rules.
CFG = {**_SHIPPED, "push_guard": {**_SHIPPED["push_guard"], "mode": "off"}}
PY = sys.executable


def verdict(
    repo: Path,
    command: str,
    tool: str = "Bash",
    mode: str = "pm",
    cwd: Path | None = None,
    cfg: dict[str, Any] = CFG,
) -> tuple[str, list[str]]:
    args = argparse.Namespace(config=None, git_mode=mode)
    event = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "t-guard",
        "cwd": str(cwd or repo),
    }
    blocks, warns = tg.evaluate(tg.Context(event, cfg, args, repo))
    return ("B" if blocks else "W" if warns else "A"), blocks + warns


def encoded(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode()


def check(repo: Path, case: str, tool: str, command: str, expect: str, **kw: Any) -> None:
    root = str(repo) if tool == "PowerShell" else repo.as_posix()  # a bash word drops backslashes
    got, messages = verdict(repo, command.replace("<ROOT>", root), tool, **kw)
    assert got == expect, f"{case}: {command!r} -> {got} {messages}"


# ---------------------------------------------------------------------------------------------------------------
# Write guard (P-03): shell writes only under the configured allowlist (.tmp/, runs/) of the innermost work tree.

WRITE_CASES = [
    ("WG-01", "Bash", "powershell -c \"Set-Content -Path 'tasks/STATUS.md' 'leak'\"", "B"),
    ("WG-02", "PowerShell", "powershell -c \"S`et-C`ontent tasks/STATUS.md 'leak'\"", "B"),
    ("WG-03", "Bash", "echo data > .tmp/../tasks/STATUS.md", "B"),
    ("WG-04", "Bash", "echo data > .tmp/junction_to_root/tasks/STATUS.md", "B"),
    ("WG-05", "Bash", "echo data > TASKS/status.md", "B"),
    ("WG-06", "PowerShell", "echo data > tasks\\STATUS.md", "B"),
    ("WG-07", "PowerShell", "echo data > \\\\?\\<ROOT>\\tasks\\STATUS.md", "B"),
    ("WG-08", "Bash", "echo data > STATUS~1.MD", "B"),
    ("WG-09", "Bash", "echo data > /tmp/bf.txt", "B"),
    ("WG-10", "PowerShell", "powershell -c \"Add-Content $env:TEMP\\a.txt 'leak'\"", "B"),
    ("WG-11", "Bash", "echo data > ~/.bashrc", "B"),
    ("WG-12", "Bash", "echo data | (read x; echo $x > tasks/STATUS.md)", "B"),
    ("WG-13", "PowerShell", "sc tasks/STATUS.md 'corrupt'", "B"),
    ("WG-14", "PowerShell", "[System.IO.File]::WriteAllText('tasks/STATUS.md','x')", "B"),
    ("WG-15", "PowerShell", f"powershell -enc {encoded('Set-Content tasks/STATUS.md x')}", "B"),
    ("WG-16", "Bash", 'sed -i "s/a/b/" tasks/STATUS.md', "B"),
    ("WG-17", "Bash", "echo data > .tmp/scratch/output.log", "A"),
    (
        "WG-QTE-01",
        "Bash",
        "powershell -Command \"Set-Content -Path 'tasks/STATUS.md' -Value 'corrupt'\"",
        "B",
    ),
    (
        "WG-QTE-02",
        "PowerShell",
        "powershell -Command \"S`et-C`ontent tasks/STATUS.md 'corrupt'\"",
        "B",
    ),
    ("WG-QTE-03", "Bash", 'echo data > "tasks"/"STATUS.md"', "B"),
    ("WG-QTE-FB-01", "Bash", 'grep ">" tasks/STATUS.md', "A"),
    ("WG-UNI-01", "Bash", "echo data > t\u0430sks/STATUS.md", "B"),
    ("WG-UNI-02", "Bash", "echo data > .tmp/out\u200bput.txt", "B"),
    ("WG-PTH-01", "Bash", "echo data > .tmp/../tasks/STATUS.md", "B"),
    (
        "WG-PTH-02",
        "PowerShell",
        'powershell -Command "New-Item -ItemType Junction -Path .tmp/link -Target . ; echo data > .tmp/link/tasks/STATUS.md"',
        "B",
    ),
    ("WG-PTH-03", "Bash", "echo data > TASKS/status.md", "B"),
    ("WG-PTH-04", "PowerShell", "echo data > tasks\\STATUS.md", "B"),
    ("WG-PTH-05", "PowerShell", "echo data > \\\\?\\<ROOT>\\tasks\\STATUS.md", "B"),
    ("WG-PTH-06", "Bash", "echo data > STATUS~1.MD", "B"),
    ("WG-PTH-07", "Bash", "echo data > /tmp/bf.txt", "B"),
    (
        "WG-PTH-08",
        "Bash",
        "powershell -Command \"Add-Content -Path $env:TEMP\\scratch.txt -Value 'leak'\"",
        "B",
    ),
    ("WG-PTH-09", "Bash", "echo data > ~/.bashrc", "B"),
    ("WG-PTH-FB-01", "Bash", "echo data > .tmp/scratch/run-01.json", "A"),
    ("WG-CHN-01", "Bash", "echo data | (read x; echo $x > tasks/STATUS.md)", "B"),
    ("WG-CHN-02", "Bash", 'eval "echo data > tasks/STATUS.md"', "B"),
    ("WG-PSA-01", "PowerShell", "sc tasks/STATUS.md 'malicious'", "B"),
    ("WG-PSA-02", "PowerShell", "ac tasks/STATUS.md 'malicious'", "B"),
    (
        "WG-PSA-03",
        "PowerShell",
        "[System.IO.File]::WriteAllText('tasks/STATUS.md', 'payload')",
        "B",
    ),
    (
        "WG-PSA-04",
        "PowerShell",
        "[System.IO.File]::AppendAllText('tasks/STATUS.md', 'payload')",
        "B",
    ),
    ("WG-PSA-05", "PowerShell", "& (Get-Command Set-Content) tasks/STATUS.md 'data'", "B"),
    (
        "WG-PSA-06",
        "PowerShell",
        f"powershell -EncodedCommand {encoded('Set-Content tasks/STATUS.md x')}",
        "B",
    ),
    ("WG-OS-01", "Bash", 'sed -i "s/old/new/g" tasks/STATUS.md', "B"),
    ("WG-OS-02", "Bash", "cat <<EOF > tasks/STATUS.md\nline\nEOF", "B"),
    # Added: the allowlist, tracked files, worktrees, deletes and moves.
    ("X-W01", "Bash", "echo x > runs/log.txt", "A"),
    ("X-W02", "Bash", "echo x > .tmp/tracked.txt", "B"),
    ("X-W03", "Bash", "echo x > .tmp/worktrees/wt1/src/fix.py", "B"),
    ("X-W04", "Bash", "echo x > .tmp/worktrees/wt1/.tmp/a.txt", "A"),
    ("X-W05", "Bash", "rm -rf .tmp/worktrees", "B"),
    ("X-W06", "Bash", "rm -rf .tmp", "B"),
    ("X-W07", "Bash", "rm -rf .tmp/scratch/old", "A"),
    ("X-W08", "Bash", "rm tasks/STATUS.md", "B"),
    ("X-W09", "Bash", "mv tasks/STATUS.md .tmp/scratch/", "B"),
    ("X-W10", "PowerShell", "Move-Item .tmp/scratch/a.txt docs/", "B"),
    ("X-W11", "Bash", "echo x > .git/config", "B"),
    ("X-W12", "Bash", "echo x > .secrets/key", "B"),
    ("X-W13", "Bash", "echo x > models/a.bin", "B"),
    ("X-W14", "Bash", "echo x > docs/new.md", "B"),
    ("X-W15", "Bash", "echo x > ../outside.txt", "B"),
    ("X-W16", "Bash", "cd docs && echo x > new.md", "B"),
    # Residual (fail-closed): `cd` scoping is not tracked, so a relative target after `cd` must be allowed from every
    # candidate directory; use an absolute target (or no `cd`) instead.
    ("X-W17", "Bash", "cd .tmp/scratch && echo x > new.md", "B"),
    ("X-W17b", "Bash", "cd .tmp/scratch && echo x > <ROOT>/.tmp/scratch/new.md", "A"),
    ("X-W18", "Bash", 'cd "$DIR" && echo x > new.md', "B"),
    ("X-W19", "Bash", "echo x > $OUT", "B"),
    ("X-W20", "Bash", "echo x > .tmp/*/a.txt", "B"),
    ("X-W21", "Bash", "echo x > .tmp/scratch/*.log", "A"),
    ("X-W22", "Bash", "ls > /dev/null 2>&1", "A"),
    ("X-W23", "PowerShell", "Get-ChildItem | Out-Null; 'x' > $null", "A"),
    ("X-W24", "PowerShell", "Set-Content -Path .tmp/scratch/a.txt -Value x", "A"),
    ("X-W25", "PowerShell", "Copy-Item src/fix.py .tmp/scratch/", "A"),
    ("X-W26", "PowerShell", "Remove-Item -Recurse .tmp/scratch/old", "A"),
    (
        "X-W27",
        "PowerShell",
        "New-Item -ItemType SymbolicLink -Path .tmp/scratch/l -Target tasks",
        "B",
    ),
    ("X-W28", "Bash", "ln -s ../../tasks .tmp/scratch/l", "B"),
    ("X-W29", "Bash", "tee tasks/STATUS.md < /dev/null", "B"),
    ("X-W30", "Bash", "cp src/fix.py src/fix2.py", "B"),
    ("X-W31", "Bash", "find . -name '*.pyc' -delete", "B"),
    ("X-W32", "Bash", "bash -c 'echo x > tasks/STATUS.md'", "B"),
    ("X-W33", "Bash", "echo $(echo x > tasks/STATUS.md)", "B"),
    ("X-W34", "Bash", "cat tasks/STATUS.md | xargs rm", "B"),
    ("X-W35", "PowerShell", "'x' | Out-File $env:USERPROFILE/a.txt", "B"),
    (
        "X-W36",
        "PowerShell",
        "Start-Process cmd -ArgumentList '/c','echo x > tasks\\STATUS.md'",
        "B",
    ),
    ("X-W37", "Bash", "git status > .tmp/scratch/status.txt", "A"),
    ("X-W38", "Bash", "curl -o docs/page.html https://example.invalid", "B"),
    ("X-W40", "PowerShell", "$c = 'Set-Content'; & $c tasks/STATUS.md x", "B"),
    ("X-W41", "PowerShell", "$PY = 'python'; & $PY tools/agent-workflow/cli_worker.py start", "A"),
    ("X-W42", "Bash", "$(echo rm) tasks/STATUS.md", "B"),
    ("X-W43", "Bash", "find . -name '*.md' | xargs grep -l x", "A"),
    ("X-W44", "Bash", "`echo rm` tasks/STATUS.md", "B"),
    ("X-W45", "PowerShell", "& (gcm 'S`et-Content') tasks/STATUS.md x", "B"),
    ("X-W46", "Bash", "(cd .tmp/scratch; ls)", "A"),
    ("X-W47", "Bash", "$(git rev-parse --show-toplevel)/scripts/run.sh", "A"),
    ("X-W48", "Bash", "c=truncate; $c -s 0 tasks/STATUS.md", "B"),
    ("X-W49", "Bash", "c=grep; $c -l x docs", "A"),
    # Allowlisted directories themselves are not writable: `runs/` holds run evidence.
    ("X-W50", "Bash", "rm -rf runs", "B"),
    ("X-W51", "PowerShell", "Remove-Item -Recurse -Force .tmp", "B"),
    (
        "X-W39",
        "Bash",
        "echo x > /c/Windows/Temp/a.txt" if os.name == "nt" else "echo x > /var/tmp/a.txt",
        "B",
    ),
]


@pytest.mark.parametrize(
    ("case", "tool", "command", "expect"), WRITE_CASES, ids=[c[0] for c in WRITE_CASES]
)
def test_write_guard(guard_repo: Path, case: str, tool: str, command: str, expect: str) -> None:
    if case == "WG-04" and not (guard_repo / ".tmp" / "junction_to_root").exists():
        pytest.skip("the platform refused to create the junction/symlink fixture")
    check(guard_repo, case, tool, command, expect)


def test_write_guard_block_names_the_safe_alternative(guard_repo: Path) -> None:
    _, messages = verdict(guard_repo, "echo x > tasks/STATUS.md")
    assert any("Write or Edit tool" in m for m in messages)


# ---------------------------------------------------------------------------------------------------------------
# Git guard (P-05): worker read-only allowlist; merge job: merge/add in its own worktree; PM: no force push.

GIT_WORKER_CASES = [
    ("GG-01", 'git "commit" -m "bypass"', "B"),
    ("GG-02", 'git -c user.useConfigOnly=true commit -m "x"', "B"),
    ("GG-03", 'git -C .. commit -m "x"', "B"),
    ("GG-04", 'git --git-dir=.git commit -m "x"', "B"),
    ("GG-05", "git merge-tree --write-tree a b", "B"),
    ("GG-06", "git update-ref refs/heads/main 4b825dc6", "B"),
    ("GG-07", "git hash-object -w src/bad.py", "B"),
    ("GG-08", 'git commit-tree 4b825dc6 -m "x"', "B"),
    ("GG-09", 'GIT_DIR=.git git commit -m "x"', "B"),
    ("GG-12", "git merge origin/feat", "B"),
    ("GG-13", "git diff HEAD~1", "A"),
    ("GG-QTE-01", 'git "commit" -m "bypass"', "B"),
    ("GG-QTE-02", "git 'merge' origin/main", "B"),
    ("GG-OPT-01", 'git -c user.useConfigOnly=true commit -m "bypass"', "B"),
    ("GG-OPT-02", 'git -C /other/repo commit -m "bypass"', "B"),
    ("GG-OPT-03", 'git --git-dir=.git commit -m "bypass"', "B"),
    ("GG-OPT-04", "git merge-tree --write-tree branch1 branch2", "B"),
    ("GG-OPT-05", "git update-ref refs/heads/main a1b2c3d", "B"),
    ("GG-OPT-06", "git hash-object -w src/bad.py", "B"),
    (
        "GG-OPT-07",
        'git commit-tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 -m "stealth commit"',
        "B",
    ),
    ("GG-OPT-08", "git symbolic-ref HEAD refs/heads/corrupted", "B"),
    ("GG-OPT-09", 'git -c alias.c=commit c -m "stealth"', "B"),
    ("GG-ENV-01", 'GIT_DIR=.git git commit -m "bypass"', "B"),
    ("GG-MJ-01", "git merge origin/feature-branch", "B"),
    # T-024 final (F08 sibling): git-merge-tree(1) defaults to its modern `--write-tree` mode, which writes objects
    # and runs merge drivers; only the deprecated `--trivial-merge` mode reads.
    ("GG-FB-01", "git merge-tree HEAD origin/main", "B"),
    ("GG-FB-02", "git merge-tree --trivial-merge HEAD~1 HEAD origin/main", "A"),
    ("X-G01", "git status", "A"),
    ("X-G02", "git --no-pager log --oneline -5", "A"),
    ("X-G03", "git -c core.quotepath=false status", "A"),
    ("X-G04", "git branch", "A"),
    ("X-G05", "git branch -a --list 'r3-*'", "A"),
    ("X-G06", "git branch -D main", "B"),
    ("X-G07", "git branch newbranch", "B"),
    ("X-G08", "git config --get user.name", "A"),
    ("X-G09", "git config user.name x", "B"),
    ("X-G10", "git stash", "B"),
    ("X-G11", "git reset --hard", "B"),
    ("X-G12", "git checkout main", "B"),
    ("X-G13", "git log --output=.tmp/x.txt", "B"),
    ("X-G14", "git grep -O foo", "B"),
    ("X-G15", "git worktree list --porcelain", "A"),
    ("X-G16", "git worktree add .tmp/x", "B"),
    ("X-G17", "git tag", "A"),
    ("X-G18", "git tag v1", "B"),
    ("X-G19", "git remote -v", "A"),
    ("X-G20", "git remote add o https://example.invalid", "B"),
    ("X-G21", "git reflog expire --all", "B"),
    ("X-G22", "git status && git commit -m x", "B"),
    ("X-G23", "(git commit -m x)", "B"),
    ("X-G24", "echo $(git commit -m x)", "B"),
    ("X-G25", "bash -c 'git commit -m x'", "B"),
    ("X-G26", "alias g=git; g commit -m x", "B"),
    ("X-G27", "env GIT_DIR=.git git status", "B"),
    ("X-G28", "git add src/fix.py", "B"),
    ("X-G29", "git push", "B"),
    ("X-G30", "git -C src status", "A"),
    ("X-G31", "git --exec-path=/x status", "B"),
    ("X-G32", "python -c \"import subprocess; subprocess.run(['git','commit','-m','x'])\"", "B"),
    ("X-G33", 'cmd /c "g^it commit -m x"', "B"),
]


@pytest.mark.parametrize(
    ("case", "command", "expect"), GIT_WORKER_CASES, ids=[c[0] for c in GIT_WORKER_CASES]
)
def test_git_guard_worker(guard_repo: Path, case: str, command: str, expect: str) -> None:
    check(guard_repo, case, "Bash", command, expect, mode="worker")


GIT_PS_WORKER_CASES = [
    ("GG-ENV-02", "powershell -Command \"$env:GIT_DIR='.git'; git commit -m 'bypass'\"", "B"),
    ("X-GP01", "$env:GIT_DIR='.git'; git status", "B"),
    ("X-GP02", "& git commit -m x", "B"),
    ("X-GP03", "$g = 'git'; & $g commit -m x", "B"),
    ("X-GP04", "Set-Alias g git; g commit -m x", "B"),
    ("X-GP05", "git status --short", "A"),
    ("X-GP06", "git log -1 --format=%H", "A"),
]


@pytest.mark.parametrize(
    ("case", "command", "expect"), GIT_PS_WORKER_CASES, ids=[c[0] for c in GIT_PS_WORKER_CASES]
)
def test_git_guard_worker_powershell(
    guard_repo: Path, case: str, command: str, expect: str
) -> None:
    check(guard_repo, case, "PowerShell", command, expect, mode="worker")


# Workers never commit, push, merge or change history, in their own worktree either (decision 77; T-024 approach note
# cases C-10 to C-15). The merge mode that allowed `merge --no-commit --no-ff` and `git add` is gone.
WORKER_HISTORY_CASES = [
    ("C-10", 'git commit -m "worker code"', "B"),
    ("C-11", "git push origin main", "B"),
    ("C-12", "git merge origin/main", "B"),
    ("C-13", "git rebase origin/main", "B"),
    ("C-14", "git reset --hard HEAD~1", "B"),
    ("C-15", "git status -sb", "A"),
    ("W-01", "git merge --no-commit --no-ff origin/feat", "B"),
    ("W-02", "git merge --squash origin/feat", "B"),
    ("W-03", "git add src/fix.py", "B"),
    ("W-04", "git cherry-pick abc123", "B"),
    ("W-05", "git revert abc123", "B"),
    ("W-06", "git am fix.patch", "B"),
    ("W-07", "git tag v1", "B"),
    ("W-08", "git branch -D wt1", "B"),
    ("W-09", "git update-ref refs/heads/wt1 HEAD", "B"),
    ("W-10", "git commit --amend --no-edit", "B"),
    ("W-11", "git stash push -m x", "B"),
    ("W-12", "git restore src/fix.py", "B"),
    ("W-13", "git diff --cached", "A"),
    ("W-14", "git log --oneline -3", "A"),
]


@pytest.mark.parametrize(
    ("case", "command", "expect"),
    WORKER_HISTORY_CASES,
    ids=[c[0] for c in WORKER_HISTORY_CASES],
)
def test_worker_never_changes_history_in_its_worktree(
    guard_repo: Path, case: str, command: str, expect: str
) -> None:
    wt = guard_repo / ".tmp" / "worktrees" / "wt1"
    check(guard_repo, case, "Bash", command, expect, mode="worker", cwd=wt)
    check(guard_repo, case, "PowerShell", command, expect, mode="worker", cwd=wt)


PM_CASES = [
    ("X-P01", "git push --force origin main", "B"),
    ("X-P02", "git push -f", "B"),
    ("X-P03", "git push -uf origin main", "B"),
    ("X-P04", "git push --force-with-lease", "B"),
    ("X-P05", "git push --forc origin main", "B"),
    ("X-P06", "git push origin +main", "B"),
    ("X-P07", "git push --mirror", "B"),
    ("X-P08", "git -c alias.p=push p --force", "B"),
    ("X-P09", "git --bare push --force", "B"),
    ("X-P10", "git -C . push --force", "B"),
    ("X-P11", "git push origin main", "A"),
    # Round 2 (write forms): only `git push <push_remote> <push_branch>`; merge is a dropped form.
    ("X-P12", "git push -o ci.skip origin main", "B"),
    ("X-P13", "git commit -m x", "A"),
    ("X-P14", "git merge --no-ff --no-commit feature", "B"),
    ("X-P15", "git status; git push --force", "B"),
    ("X-P16", "git push origin main -- +main", "B"),
]


@pytest.mark.parametrize(("case", "command", "expect"), PM_CASES, ids=[c[0] for c in PM_CASES])
def test_git_guard_pm(guard_repo: Path, case: str, command: str, expect: str) -> None:
    check(guard_repo, case, "Bash", command, expect, mode="pm")


# Orchestrator deny set of decision 78 (T-024 Step 0 question 1): each entry has a deny row and a near-miss allow row.
# Commands run from the main working tree unless they name a worker worktree.
PM_REWRITE_CASES = [
    # C-01 to C-09 of the approach note (C-03/C-04, the marker, are in test_push_guard.py).
    ("C-01", "git push origin main 2>&1 | Out-String; git status -sb | Select-Object -First 1", "A"),
    ("C-02", 'git commit -m "feat: integrated task"', "A"),
    ("C-05", "git push --force origin main", "B"),
    ("C-06", "git commit --amend", "B"),
    ("C-07", "git push origin --delete feat-branch", "B"),
    ("C-08", "git -C .tmp/worktrees/wt1 add src/fix.py", "A"),
    ("C-09", "git worktree remove .tmp/worktrees/wt1; git branch -d wt1", "A"),
    # push: remote ref deletion
    ("PR-01", "git push -d origin feat", "B"),
    # Round 2: any push other than `git push origin main` (the configured form) is refused.
    ("PR-02", "git push -u origin feat", "B"),
    ("PR-03", "git push origin :feat", "B"),
    ("PR-04", "git push origin feat:feat", "B"),
    ("PR-05", "git push origin :", "B"),
    ("PR-06", "git push --prune origin", "B"),
    ("PR-07", "git push --progress origin main", "B"),
    ("PR-08", "git push --del origin feat", "B"),
    ("PR-09", "git push -o :x origin main", "B"),
    ("PR-10", "git push origin main -- :feat", "B"),
    # commit --amend
    ("CA-01", "git commit --am -m x", "B"),
    ("CA-02", "git commit -m amend", "A"),
    ("CA-03", "git commit --author=x -m y", "B"),  # round 2: only -q, -m, -F
    # rebase (round 2: every form, `--abort` included, is a dropped form)
    ("RB-01", "git rebase origin/main", "B"),
    ("RB-02", "git rebase -i HEAD~3", "B"),
    ("RB-03", "git rebase --continue", "B"),
    ("RB-04", "git rebase --abort", "B"),
    # pull only as `git pull --ff-only [<remote> [<branch>]]`
    ("PL-01", "git pull", "B"),
    ("PL-02", "git pull --ff-only --rebase", "B"),
    ("PL-03", "git pull -r --ff-only", "B"),
    ("PL-04", "git pull --ff-only", "A"),
    ("PL-05", "git pull --ff-only --no-rebase origin main", "B"),
    ("PL-06", "git pull --ff-only origin main", "A"),
    # reset that moves the branch
    ("RS-01", "git reset --hard", "B"),
    ("RS-02", "git reset --hard HEAD~1", "B"),
    ("RS-03", "git reset --soft HEAD~1", "B"),
    ("RS-04", "git reset --keep", "B"),
    ("RS-05", "git reset --merge", "B"),
    ("RS-06", "git reset HEAD~1", "B"),
    ("RS-07", "git reset origin/main", "B"),
    ("RS-08", "git reset --ha", "B"),
    ("RS-09", "git reset src/fix.py", "B"),  # without `--` a path and a commit look alike: fail closed
    ("RS-10", "git reset", "A"),
    ("RS-11", "git reset -q", "B"),  # round 2: bare or `[HEAD] -- <paths>` only
    ("RS-12", "git reset HEAD -- src/fix.py", "A"),
    ("RS-13", "git reset -- src/fix.py", "A"),
    ("RS-14", "git reset HEAD src/fix.py", "B"),
    ("RS-15", "git reset --mixed HEAD", "B"),
    # always denied
    ("AD-01", "git filter-branch --tree-filter x HEAD", "B"),
    ("AD-02", "git filter-repo --path src", "B"),
    ("AD-03", "git replace abc def", "B"),
    ("AD-04", "git update-ref refs/heads/main abc", "B"),
    ("AD-05", "git show-ref --heads", "A"),
    ("AD-06", "git log --all --oneline", "A"),
    # symbolic-ref with a value
    ("SR-01", "git symbolic-ref HEAD refs/heads/x", "B"),
    ("SR-02", "git symbolic-ref -d HEAD", "B"),
    ("SR-03", "git symbolic-ref --short HEAD", "B"),  # not on the read-only allowlist: refused in both modes
    # reflog rewrites
    ("RL-01", "git reflog expire --all", "B"),
    ("RL-02", "git reflog delete HEAD@{1}", "B"),
    ("RL-03", "git reflog show -5", "A"),
    # branch force forms
    ("BR-01", "git branch -D wt1", "B"),
    ("BR-02", "git branch -f main HEAD~1", "B"),
    ("BR-03", "git branch -M a b", "B"),
    ("BR-04", "git branch -C a b", "B"),
    ("BR-05", "git branch --delete --force wt1", "B"),
    ("BR-06", "git branch -df wt1", "B"),
    ("BR-07", "git branch -d wt1", "A"),
    ("BR-08", "git branch -m a b", "B"),  # round 2: only `branch -d <name>` writes
    ("BR-09", "git branch -u origin/main", "B"),
    ("BR-10", "git branch --format=%(refname) -a", "A"),
    # checkout and switch: dropped forms in round 2
    ("CO-01", "git checkout -B main", "B"),
    ("CO-02", "git checkout -fB main", "B"),
    ("CO-03", "git checkout -b new-feature", "B"),
    ("CO-04", "git checkout -- src/fix.py", "B"),
    ("SW-01", "git switch -C main", "B"),
    ("SW-02", "git switch --force-create main", "B"),
    ("SW-03", "git switch -c new-feature", "B"),
    ("SW-04", "git switch main", "B"),
    # tags: listing only (tag creation is a dropped form in round 2)
    ("TG-01", "git tag -d v1", "B"),
    ("TG-02", "git tag -f v1", "B"),
    ("TG-03", "git tag --delete v1", "B"),
    ("TG-04", "git tag --force v1", "B"),
    ("TG-05", "git tag -fm msg v1", "B"),
    ("TG-06", "git tag v1", "B"),
    ("TG-07", "git tag -a v1 -m release", "B"),
    ("TG-08", "git tag -md v1", "B"),
    ("TG-09", "git tag --format=%(refname)", "A"),
    # worktree remove --force
    ("WT-01", "git worktree remove --force .tmp/worktrees/wt1", "B"),
    ("WT-02", "git worktree remove -f .tmp/worktrees/wt1", "B"),
    ("WT-03", "git worktree remove .tmp/worktrees/wt1", "A"),
    ("WT-04", "git worktree prune", "A"),
    # history writes only from the main working tree
    ("MT-01", "git -C .tmp/worktrees/wt1 commit -m x", "B"),
    ("MT-02", "git -C .tmp/worktrees/wt1 merge feature", "B"),
    ("MT-03", "git -C .tmp/worktrees/wt1 cherry-pick abc", "B"),
    ("MT-04", "git -C .tmp/worktrees/wt1 revert abc", "B"),
    ("MT-05", "git -C .tmp/worktrees/wt1 am fix.patch", "B"),
    ("MT-06", "cd .tmp/worktrees/wt1 && git commit -m x", "B"),
    ("MT-07", "git -C $WT commit -m x", "B"),
    ("MT-08", "git --git-dir=.tmp/worktrees/wt1/.git commit -m x", "B"),
    ("MT-09", "git merge --no-ff feature", "B"),  # merge, cherry-pick, am: dropped forms (round 2)
    ("MT-10", "git cherry-pick abc", "B"),
    ("MT-11", "git revert abc", "B"),
    ("MT-11b", "git revert --no-edit abc", "A"),
    ("MT-12", "git am .tmp/pm-state/patches/T-1.patch", "B"),
    ("MT-13", "git -C .tmp/worktrees/wt1 diff --cached --binary", "A"),
    ("MT-14", "git -C .tmp/worktrees/wt1 apply --check ../p.patch", "A"),
    ("MT-15", "cd .tmp/worktrees/wt1 && git add -A", "A"),
    ("MT-16", "git apply --index .tmp/pm-state/patches/T-1.patch", "B"),
    ("MT-17", "git apply .tmp/pm-state/patches/T-1.patch", "A"),
]


@pytest.mark.parametrize(
    ("case", "command", "expect"), PM_REWRITE_CASES, ids=[c[0] for c in PM_REWRITE_CASES]
)
def test_git_guard_pm_never_rewrites(guard_repo: Path, case: str, command: str, expect: str) -> None:
    tool = "PowerShell" if "Select-Object" in command else "Bash"
    check(guard_repo, case, tool, command, expect, mode="pm")


@pytest.mark.parametrize(
    ("command", "expect"),
    [
        ("git reset --hard; Write-Host done", "B"),
        ("git status; git push origin --delete feat", "B"),
        ("git -C .tmp\\worktrees\\wt1 commit -m x", "B"),
        ("Set-Location .tmp/worktrees/wt1; git commit -m x", "B"),
        ("git -C .tmp\\worktrees\\wt1 add -A; git -C .tmp\\worktrees\\wt1 diff --cached", "A"),
    ],
)
def test_git_guard_pm_never_rewrites_powershell(guard_repo: Path, command: str, expect: str) -> None:
    check(guard_repo, command, "PowerShell", command, expect, mode="pm")


@pytest.mark.parametrize(
    "command",
    ["git -C $WT commit -m x", "cd $WT && git commit -m x", "git --work-tree=x commit -m x"],
)
def test_pm_history_write_in_an_unprovable_repository_names_the_reason(
    guard_repo: Path, command: str
) -> None:
    got, messages = verdict(guard_repo, command)
    assert got == "B" and any(w in m for m in messages for w in ("run-time", "run time", "redirects")), messages


def test_pm_commit_from_a_worker_worktree_cwd_is_refused(guard_repo: Path) -> None:
    wt = guard_repo / ".tmp" / "worktrees" / "wt1"
    assert verdict(guard_repo, "git commit -m x", cwd=wt)[0] == "B"
    assert verdict(guard_repo, "git add src/fix.py", cwd=wt)[0] == "A"
    blocks = verdict(guard_repo, "git commit -m x", cwd=wt)[1]
    assert any("worker worktree" in b for b in blocks) and any("decision 77" in b for b in blocks)


def test_aliases_are_never_run(guard_repo: Path) -> None:
    # Round 2: aliases are never expanded; a non-built-in name is unknown and refused in both modes, even one whose
    # expansion is harmless (review R1: Git's alias quoting cannot be read reliably by the guard).
    cases = ["rb HEAD~2", "ci --amend", "ci -m x", "st", "qa -m x"]
    for words in cases:
        for mode in ("pm", "worker"):
            got, blocks = verdict(guard_repo, f"git {words}", mode=mode)
            assert got == "B" and any("read-only" in b for b in blocks), (words, mode, blocks)
    assert verdict(guard_repo, "git commit -m x")[0] == "A"  # near miss: the built-in itself


# ---------------------------------------------------------------------------------------------------------------
# T-024 fix round 1: one normaliser for every git spelling (review F1-F4, item 9), through the hook entry point.
ENTRY_ROOT = HOOKS.parents[2]  # the hook's project root when run as a script
WORKER_WT = (ENTRY_ROOT / ".tmp" / "worktrees" / "probe-wt").as_posix()


def entry(command: str, tool: str = "Bash", mode: str = "pm", cwd: Path | None = None) -> tuple[str, str]:
    """Verdict of the real hook process (exit 2 blocks) and its stderr."""
    event = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "t-entry",
        "cwd": str(cwd or ENTRY_ROOT),
    }
    done = run_hook(event, "--git-mode", mode)
    assert done.returncode in (0, 2), done.stderr
    return ("B" if done.returncode == 2 else "A"), done.stderr


@pytest.fixture
def alias_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Repository A (no aliases, the session's directory) and B (`rw` = `rebase -i`, `st` = `status`, `nil` = "")."""
    from conftest import git

    a, b = tmp_path / "repo-a", tmp_path / "repo b"
    for repo in (a, b):
        repo.mkdir()
        git(repo, "init", "-q")
    git(b, "config", "alias.rw", "rebase -i")
    git(b, "config", "alias.st", "status")
    git(b, "config", "alias.nil", "")
    return a, b


@pytest.mark.parametrize(
    ("case", "tool", "command", "expect"),
    [
        ("F1-attached-C", "Bash", f"git -C{WORKER_WT} commit -m x", "B"),
        ("F1-git-dir", "Bash", f"git --git-dir={WORKER_WT}/.git commit -m x", "B"),
        ("F1-env-bash", "Bash", f'GIT_DIR="{WORKER_WT}/.git" git commit -m x', "B"),
        ("F1-env-export", "Bash", f"export GIT_WORK_TREE={WORKER_WT}; git commit -m x", "B"),
        ("F1-env-ps", "PowerShell", f'$env:GIT_DIR="{WORKER_WT}/.git"; git commit -m x', "B"),
        ("F1-env-ps-item", "PowerShell", f"Set-Item 'env:GIT_COMMON_DIR' x; git commit -m x", "B"),
        # Round 2: a write runs with no GIT_* variable at all.
        ("F1-pager", "Bash", "GIT_PAGER=cat git commit -m x", "B"),
        ("F1-near-message", "Bash", 'git commit -m "explain GIT_DIR=x handling"', "A"),
        ("F1-ps-pager", "PowerShell", "$env:GIT_PAGER='cat'; git commit -m x", "B"),
        ("F1-near-pager-read", "Bash", "GIT_PAGER=cat git log -1", "A"),
        ("F3-ps-var", "PowerShell", "& $g rebase -i HEAD~2", "B"),
        ("F3-bash-var", "Bash", "$g reset --hard", "B"),
        ("F3-built", "PowerShell", "& ('g'+'it') rebase", "B"),
        ("F3-near-var", "PowerShell", "& $g status", "A"),
        ("F4-tag", "Bash", "git tag --d v1", "B"),
        ("F4-switch", "Bash", "git switch --force- main", "B"),
        ("F4-symref", "Bash", "git symbolic-ref --del HEAD", "B"),
        ("F4-reset", "Bash", "git reset --h", "B"),
        ("F4-worktree", "Bash", "git worktree remove --f .tmp/worktrees/x", "B"),
        ("F4-amend", "PowerShell", "git commit --amen -m x", "B"),
        ("F4-switch-force", "Bash", "git switch --force main", "B"),  # round 2: switch is a dropped form
        ("F4-near-tag-list", "Bash", "git tag --list", "A"),
        ("F4-near-worktree", "Bash", "git worktree remove .tmp/worktrees/x", "A"),
        ("F4-symref-read", "Bash", "git symbolic-ref HEAD", "B"),  # not on the read-only allowlist
        ("F4-no-amend", "Bash", "git commit --no-amend -m x", "B"),  # round 2: only -q, -m, -F
    ],
)
def test_pm_spellings_through_the_hook(case: str, tool: str, command: str, expect: str) -> None:
    got, err = entry(command, tool)
    assert got == expect, f"{case}: {command!r} -> {got} {err}"


@pytest.mark.parametrize(
    ("case", "tool", "command", "expect"),
    [
        ("9-dashed-bash", "Bash", "git-commit -m probe", "B"),
        ("9-dashed-ps", "PowerShell", "& git-commit -m probe", "B"),
        ("9-dashed-exe", "Bash", "git-push.exe origin main", "B"),
        ("9-upper", "Bash", "git COMMIT -m probe", "B"),
        ("9-near-dashed-read", "Bash", "git-status --short", "A"),
        ("9-near-read", "PowerShell", "& git status --short", "A"),
    ],
)
def test_worker_dashed_git_through_the_hook(case: str, tool: str, command: str, expect: str) -> None:
    got, err = entry(command, tool, mode="worker")
    assert got == expect, f"{case}: {command!r} -> {got} {err}"


def test_pm_aliases_never_run_in_any_repository(alias_repos: tuple[Path, Path]) -> None:
    """Round 2 (review F2, R1): an alias is an unknown subcommand, whichever repository defines it."""
    a, b = alias_repos
    for command in (
        f'git -C "{b.as_posix()}" rw',
        f'cd "{b.as_posix()}" && git rw',
        f"git --git-dir={b.as_posix()}/.git rw",
        f"GIT_CONFIG_GLOBAL='{b.as_posix()}/.git/config' git rw",
        "git nil",
        "git st",
    ):
        got, err = entry(command, cwd=b)
        assert got == "B" and "crash" not in err.lower(), (command, err)
    assert entry("git status --short", cwd=b)[0] == "A"  # near miss: the built-in


# Review round 2 findings through the hook entry point: (id, mode, tool, command, verdict).
ROUND2_CASES = [
    ("R1-quoted-alias", "pm", "Bash", "git qa -m x", "B"),
    ("R1-near-commit", "pm", "Bash", "git commit -m x", "A"),
    ("R2-pm-launcher", "pm", "Bash", 'X="y" bash -c "git commit --amend -m x"', "B"),
    ("R2-worker-launcher", "worker", "Bash", 'X="y" bash -c "git commit -m x"', "B"),
    ("R2-worker-cmd", "worker", "Bash", 'X="y" cmd /c git commit -m x', "B"),
    ("R2-ps-launcher", "pm", "PowerShell", "powershell -Command 'git commit --amend -m x'", "B"),
    ("R2-start-process", "pm", "PowerShell", "Start-Process git -ArgumentList 'commit','--amend'", "B"),
    ("R2-near-launcher", "pm", "Bash", 'X="y" bash -c "echo hi"', "A"),
    ("R3-runtime-push", "pm", "Bash", 'git -C "$x" push origin main', "B"),
    ("R3-git-dir-push", "pm", "Bash", "git --git-dir=elsewhere push origin main", "B"),
    ("R3-runtime-tag", "pm", "Bash", 'git -C "$x" tag new', "B"),
    ("R3-runtime-branch", "pm", "Bash", 'git -C "$x" branch new', "B"),
    ("R3-near-tag-list", "pm", "Bash", "git tag --list", "A"),
    ("R4-include", "pm", "Bash", "git -c include.path=.tmp/a.gitconfig injected -m x", "B"),
    ("R4-include-ps", "pm", "PowerShell", "git -c include.path=.tmp/a.gitconfig injected -m x", "B"),
    ("R4-include-commit", "pm", "Bash", "git -c include.path=.tmp/a.gitconfig commit -m x", "B"),
    ("R4-near-config-read", "pm", "Bash", "git -c core.quotepath=false log -1", "A"),
    ("R5-no-ff", "pm", "Bash", "git pull --ff-only --no-ff", "B"),
    ("R5-ff", "pm", "Bash", "git pull --ff-only --ff", "B"),
    ("R5-reordered", "pm", "Bash", "git pull --no-ff --ff-only", "B"),
    ("R5-near", "pm", "Bash", "git pull --ff-only", "A"),
    ("R6-fast-import", "pm", "Bash", "git fast-import --force < .tmp/changes.fi", "B"),
    ("R6-fetch-force", "pm", "Bash", "git fetch --update-head-ok --force . HEAD~1:main", "B"),
    ("R6-fetch-refspec", "pm", "Bash", "git fetch origin +main:main", "B"),
    ("R6-near-fetch", "pm", "Bash", "git fetch origin", "A"),
    ("R8-message-amend", "pm", "Bash", "git commit -m --amend", "A"),
    # `-m` takes `-q` and `-F` as its values; one message source (round 3 item 9: `-m` with `-F` is refused).
    ("R8-message-options", "pm", "PowerShell", "git commit -m -q -m -F", "A"),
    ("R8-tag-message", "pm", "Bash", "git tag -m -f newtag", "B"),  # tag creation is a dropped form
    ("R8-near-amend", "pm", "Bash", "git commit --amend -m x", "B"),
]


@pytest.mark.parametrize(("case", "mode", "tool", "command", "expect"), ROUND2_CASES, ids=[c[0] for c in ROUND2_CASES])
def test_round2_findings_through_the_hook(case: str, mode: str, tool: str, command: str, expect: str) -> None:
    got, err = entry(command, tool, mode=mode)
    assert got == expect, f"{case}: {command!r} -> {got} {err}"


# Every orchestrator write form (positive), run from the guard_repo main tree or its worker worktree wt1.
WRITE_FORM_CASES = [
    "git add src/fix.py",
    "git add -A",
    "git add -- src/fix.py",
    "git commit -q -m x -m y",
    "git commit -F .tmp/msg.txt",
    "git push origin main",
    "git revert --no-edit abc123",
    "git pull --ff-only",
    "git pull --ff-only origin main",
    "git fetch",
    "git fetch origin",
    "git reset",
    "git reset -- src/fix.py",
    "git reset HEAD -- src/fix.py",
    "git restore --staged src/fix.py",
    "git restore --staged -- src/fix.py",
    "git apply .tmp/p.patch",
    "git apply --check .tmp/p.patch",
    "git worktree add --detach .tmp/worktrees/new HEAD",
    "git worktree remove .tmp/worktrees/wt1",
    "git worktree prune",
    "git branch -d wt1",
    "git -C .tmp/worktrees/wt1 add -A",
    "git -C .tmp/worktrees/wt1 apply --check ../p.patch",
    "git.exe commit -m x",
]


def _split_sub(command: str) -> tuple[list[str], str, list[str]]:
    """(`git` and its `-C <dir>` words, subcommand, the rest) of a WRITE_FORM_CASES command."""
    words = command.split()
    k = 1
    while words[k] == "-C":
        k += 2
    return words[:k], words[k], words[k + 1 :]


MUTATIONS = {
    "extra option": lambda pre, sub, rest: [*pre, sub, "--x-extra", *rest],
    "alias name": lambda pre, sub, rest: [*pre, sub + "x", *rest],
    "-c config": lambda pre, sub, rest: [pre[0], "-c", "user.name=x", *pre[1:], sub, *rest],
    "-c include": lambda pre, sub, rest: [pre[0], "-c", "include.path=.tmp/a.gitconfig", *pre[1:], sub, *rest],
    "global option": lambda pre, sub, rest: [pre[0], "--no-pager", *pre[1:], sub, *rest],
    "env prefix": lambda pre, sub, rest: ["GIT_EDITOR=true", *pre, sub, *rest],
    "quoted env prefix": lambda pre, sub, rest: ['X="y"', "GIT_DIR=.git", *pre, sub, *rest],
    "launcher": lambda pre, sub, rest: ["bash", "-c", "'" + " ".join([*pre, sub, *rest]) + "'"],
    "dashed program": lambda pre, sub, rest: [f"git-{sub}", *rest],
    # Round 3: run-time expansion (class E), wrappers and programs that run git (class F).
    "variable argument": lambda pre, sub, rest: ["X=--amend;", *pre, sub, *rest, "$X"],
    "quoted expansion": lambda pre, sub, rest: [*pre, sub, *rest, '"$(echo --force)"'],
    "glob argument": lambda pre, sub, rest: [*pre, sub, *rest, "*"],
    "env wrapper": lambda pre, sub, rest: ["env", *pre, sub, *rest],
    "timeout wrapper": lambda pre, sub, rest: ["timeout", "9", *pre, sub, *rest],
    "command wrapper": lambda pre, sub, rest: ["command", *pre, sub, *rest],
    "quoted prefix and wrapper": lambda pre, sub, rest: ['X="y"', "env", *pre, sub, *rest],
    "plain prefix": lambda pre, sub, rest: ["LC_ALL=C", *pre, sub, *rest],
    "runner": lambda pre, sub, rest: ["uv", "run", *pre, sub, *rest],
    "python inline": lambda pre, sub, rest: ["python", "-c", f"\"import os; os.system('{' '.join([*pre, sub, *rest])}')\""],
    "node inline": lambda pre, sub, rest: ["node", "-e", f"\"require('child_process').execSync('{' '.join([*pre, sub, *rest])}')\""],
    "upper case": lambda pre, sub, rest: ["GIT", *pre[1:], sub, *rest],
    "run-time dir": lambda pre, sub, rest: [pre[0], "-C", "$d", *pre[1:], sub, *rest],
    "variable program": lambda pre, sub, rest: ["g=git;", "$g", *pre[1:], sub, *rest],
    "last-wins option": lambda pre, sub, rest: [*pre, sub, *rest, "--no-ff"],
    # Round 4 (class F): code-carrying argument roles, suffixed and versioned program names, PATH selectors.
    "env split-string": lambda pre, sub, rest: ["env", f"--split-string=\"{' '.join([*pre, sub, *rest])}\""],
    "env -S after prefix": lambda pre, sub, rest: ['X="y"', "env", "-S", f"\"{' '.join([*pre, sub, *rest])}\""],
    "unknown wrapper of a shell": lambda pre, sub, rest: ["chrt", "-b", "0", "sh", "-c", f"\"{' '.join([*pre, sub, *rest])}\""],
    "runner of git.cmd": lambda pre, sub, rest: ["uv", "run", "git.cmd", *pre[1:], sub, *rest],
    "versioned interpreter": lambda pre, sub, rest: ["ruby3.4", "-e", f"\"system('{' '.join([*pre, sub, *rest])}')\""],
    "nodejs": lambda pre, sub, rest: ["nodejs", "-e", f"\"require('child_process').execSync('{' '.join([*pre, sub, *rest])}')\""],
    "PATH statement": lambda pre, sub, rest: ["PATH=.tmp/shims:/usr/bin;", *pre, sub, *rest],
    "PATH prefix": lambda pre, sub, rest: ["PATH=.tmp/shims", *pre, sub, *rest],
    "PATHEXT export": lambda pre, sub, rest: ["export", "PATHEXT=.CMD;", *pre, sub, *rest],
}


@pytest.mark.parametrize("command", WRITE_FORM_CASES)
def test_every_write_form_is_allowed(guard_repo: Path, command: str) -> None:
    got, messages = verdict(guard_repo, command)
    assert got == "A", f"{command!r} -> {got} {messages}"


@given(command=s.sampled_from(WRITE_FORM_CASES), mutation=s.sampled_from(sorted(MUTATIONS)))
def test_one_mutation_of_a_write_form_is_denied(guard_repo: Path, command: str, mutation: str) -> None:
    """Property (round 2): a write form plus one mutation (extra option, alias, `-c`, env prefix, launcher, dashed or
    upper-case program, run-time `-C`, variable program, a trailing last-wins option) is refused in pm mode."""
    pre, sub, rest = _split_sub(command)
    if mutation == "last-wins option" and sub == "commit":
        rest = [*rest, "--amend"]  # commit's last-wins counterpart; `--no-ff` would be refused anyway
    mutated = " ".join(MUTATIONS[mutation](pre, sub, rest))
    got, messages = verdict(guard_repo, mutated)
    assert got == "B", f"{mutation}: {mutated!r} -> {got} {messages}"


def test_worker_worktree_allows_only_staging_and_applying(guard_repo: Path) -> None:
    for command in ("git -C .tmp/worktrees/wt1 commit -m x", "git -C .tmp/worktrees/wt1 push origin main",
                    "git -C .tmp/worktrees/wt1 reset -- src/fix.py", "git -C .tmp/worktrees/wt1 branch -d x"):
        assert verdict(guard_repo, command)[0] == "B", command
    assert verdict(guard_repo, "git -C .tmp/worktrees/wt1 add src/fix.py")[0] == "A"


def test_a_repository_that_is_not_the_project_is_refused(guard_repo: Path, tmp_path: Path) -> None:
    from conftest import git

    other = guard_repo / ".tmp" / "scratch" / "other"
    other.mkdir(parents=True, exist_ok=True)
    git(other, "init", "-q")
    assert verdict(guard_repo, "git -C .tmp/scratch/other commit -m x")[0] == "B"
    assert verdict(guard_repo, "git -C src commit -m x")[0] == "A"  # near miss: a subdirectory of the main tree


# Round 3 (Codex retest 2: R9, R2 reopened, R12, reflog; Gemini F2, F3): through the real hook process, each with a
# near miss that still passes.
ROUND3_CASES = [
    ("R9-bash-var", "pm", "Bash", "MSG='x --amend'; git commit -m $MSG", "B"),
    ("R9-bash-quoted-var", "pm", "Bash", 'git commit -m "$MSG"', "B"),
    ("R9-bash-subst", "pm", "Bash", "git commit -m x $(echo --amend)", "B"),
    ("R9-ps-splat", "pm", "PowerShell", "$msg=@('x','--amend'); git commit -m @msg", "B"),
    ("R9-ps-array-var", "pm", "PowerShell", "$a='-D','done'; git branch -d $a", "B"),
    ("R9-ps-embedded-quote", "pm", "PowerShell", "git commit -m 'say \"a --amend\" b'", "B"),
    ("R9-ps-stop-parsing", "pm", "PowerShell", "git commit --% -m x --amend", "B"),
    ("R9-fetch-var", "pm", "Bash", "ARGS='--update-head-ok --force . HEAD~1:main'; git fetch $ARGS", "B"),
    ("R9-branch-var", "pm", "Bash", "BR='-D done'; git branch -d $BR", "B"),
    ("R9-worker-config-var", "worker", "Bash", "X='--add a.b c'; git config $X", "B"),
    ("R9-glob", "pm", "Bash", "git add *.py", "B"),
    ("R9-near-literal", "pm", "Bash", "git commit -m 'x --amend'", "A"),
    ("R9-near-ps-literal", "pm", "PowerShell", "git commit -m 'x --amend'", "A"),
    ("R9-near-quoted-glob", "pm", "Bash", "git add 'src/*.py'", "A"),
    ("R2-env", "pm", "Bash", 'X="y" env git commit --amend -m x', "B"),
    ("R2-env-worker", "worker", "Bash", 'X="y" env git commit --amend -m x', "B"),
    ("R2-env-bash", "pm", "Bash", 'X="y" env bash -c "git commit -m x"', "B"),
    ("R2-timeout-bash", "pm", "Bash", 'X="y" timeout 10 bash -c "git commit -m x"', "B"),
    ("R2-command", "pm", "Bash", 'X="y" command git commit --amend -m x', "B"),
    ("R2-command-plain-form", "pm", "Bash", "command git commit -m x", "B"),
    ("R2-prefix", "pm", "Bash", "LC_ALL=C git commit -m x", "B"),
    ("R2-runner", "pm", "Bash", "uv run git commit -m x", "B"),
    ("R2-worker-timeout-read", "worker", "Bash", "timeout 9 git status", "B"),
    ("R2-near-env-echo", "pm", "Bash", 'X="y" env echo hi', "A"),
    ("R2-near-echo", "worker", "Bash", "echo git commit --amend", "A"),
    ("R2-near-grep", "worker", "Bash", "rg -n git docs", "A"),
    ("R2-near-keyword", "pm", "Bash", "if true; then git commit -m x; fi", "A"),
    ("R12-python", "pm", "Bash", "python -c \"import os; os.system('git commit --amend -m x')\"", "B"),
    ("R12-node", "pm", "Bash", "node -e \"require('child_process').execSync('git commit --amend -m x')\"", "B"),
    ("R12-python-worker", "worker", "PowerShell", "python -c \"import os; os.system('git status')\"", "B"),
    ("R12-near-python", "pm", "Bash", 'python -c "print(1)"', "A"),
    ("RL-expire", "pm", "Bash", "git reflog expire --expire=now --all", "B"),
    ("RL-delete", "worker", "Bash", "git reflog delete HEAD@{1}", "B"),
    ("RL-write", "pm", "Bash", "git reflog write refs/heads/main 1234567 89abcde forged", "B"),
    ("RL-drop", "pm", "PowerShell", "git reflog drop main", "B"),
    ("RL-near-show", "worker", "Bash", "git reflog show -5 main", "A"),
    ("RL-near-bare", "pm", "Bash", "git reflog", "A"),
    ("RL-near-exists", "worker", "Bash", "git reflog exists refs/heads/main", "A"),
    ("F2-revert-tilde", "pm", "Bash", "git revert --no-edit HEAD~1", "A"),
    ("F2-revert-caret", "pm", "PowerShell", "git revert --no-edit abc123^2", "A"),
    ("F2-revert-range", "pm", "Bash", "git revert --no-edit HEAD~3..HEAD", "B"),
    ("F2-revert-reflog", "pm", "Bash", "git revert --no-edit 'HEAD@{1}'", "B"),
    ("F3-bare-commit", "pm", "Bash", "git commit", "B"),
    ("F3-quiet-commit", "pm", "Bash", "git commit -q", "B"),
    ("F3-two-sources", "pm", "Bash", "git commit -m x -F .tmp/msg.txt", "B"),
    ("F3-near-paragraphs", "pm", "Bash", "git commit -m x -m y", "A"),
]


@pytest.mark.parametrize(("case", "mode", "tool", "command", "expect"), ROUND3_CASES, ids=[c[0] for c in ROUND3_CASES])
def test_round3_findings_through_the_hook(case: str, mode: str, tool: str, command: str, expect: str) -> None:
    got, err = entry(command, tool, mode=mode)
    assert got == expect, f"{case}: {command!r} -> {got} {err}"


# Round 4 (Codex retest 3: R2, R12 reopened, R13; Gemini P1, P2), each mode where the reviewer reproduced it, through
# the real hook process, with near misses that still pass. Opaque inline code that builds the git word at run time is
# not in this table: the worker turn snapshot detects it (test_cli_worker_git_snapshot.py), pm mode accepts it.
ROUND4_CASES = [
    *[
        (f"R2-{tag}-{mode}", mode, "Bash", command, "B")
        for mode in ("pm", "worker")
        for tag, command in (
            ("split-string", 'env --split-string="git commit --amend -m x"'),
            ("split-string-prefix", 'X="y" env -S "git commit --amend -m x"'),
            ("split-attached", 'env -S"git status"'),
            ("split-cluster", 'env -iS "git status"'),
            ("chrt-sh", 'chrt -b 0 sh -c "git commit --amend -m x"'),
            ("taskset-bash", 'taskset 1 bash -lc "git status"'),
        )
    ],
    ("R2-near-split-echo", "worker", "Bash", 'env -S "echo hi"', "A"),
    ("R2-near-chrt-echo", "worker", "Bash", 'chrt -b 0 sh -c "echo hi"', "A"),
    *[
        (f"G7-encoded-{mode}", mode, "PowerShell", f"powershell -EncodedCommand {encoded('git status')}", "B")
        for mode in ("pm", "worker")
    ],
    ("G7-encoded-pwsh", "worker", "PowerShell", f"pwsh -enc {encoded('git log -1')}", "B"),
    ("G7-near-direct-read", "worker", "PowerShell", "git status", "A"),
    *[
        (f"G6-{tag}-{mode}", mode, "Bash", command, "B")
        for mode in ("pm", "worker")
        for tag, command in (
            ("uv-cmd", "uv run git.cmd commit --amend -m x"),
            ("uv-bat", "uv run git.bat commit --amend -m x"),
            ("uv-path-exe", "uv run C:/Git/cmd/git.EXE status"),
        )
    ],
    ("G6-near-git-cmd-read", "worker", "Bash", "git.cmd status", "A"),
    ("G6-near-uv-tool", "worker", "Bash", "uv run ruff check src", "A"),
    *[
        (f"R12-{tag}-{mode}", mode, "Bash", command, "B")
        for mode in ("pm", "worker")
        for tag, command in (
            ("nodejs", "nodejs -e \"require('child_process').execSync('git commit --amend -m x')\""),
            ("ruby-versioned", "ruby3.4 -e \"system('git commit --amend -m x')\""),
            ("python-versioned", "python3.12 -c \"import os; os.system('git status')\""),
            ("node-exe", "node.exe -e \"require('child_process').execSync('git status')\""),
        )
    ],
    ("R12-near-nodejs", "worker", "Bash", 'nodejs -e "console.log(1)"', "A"),
    ("R12-near-ruby", "worker", "Bash", 'ruby3.4 -e "puts 1"', "A"),
    *[
        (f"R13-{tag}-{mode}", mode, tool, command, "B")
        for mode in ("pm", "worker")
        for tag, tool, command in (
            ("bash-statement", "Bash", "PATH=.tmp/shims:$PATH; git status"),
            ("bash-prefix", "Bash", "PATH=.tmp/shims:$PATH git status"),
            ("bash-export", "Bash", "export PATH=.tmp/shims:$PATH; git log -1"),
            ("bash-pathext", "Bash", "PATHEXT=.CMD; git status"),
            ("ps-env", "PowerShell", "$env:PATH='.tmp/shims;'+$env:PATH; git status"),
            ("ps-env-append", "PowerShell", "$env:Path += ';.tmp/shims'; git status"),
            ("ps-pathext", "PowerShell", "$env:PATHEXT='.CMD;.EXE'; git status"),
            ("ps-set-item", "PowerShell", "Set-Item 'env:PATH' '.tmp/shims'; git status"),
        )
    ],
    ("R13-pm-commit", "pm", "Bash", "PATH=.tmp/shims:$PATH; git commit -m x", "B"),
    ("R13-near-message", "pm", "Bash", 'git commit -m "PATH=x"', "A"),
    ("R13-near-read-path", "worker", "Bash", "echo $PATH; git status", "A"),
    ("R13-near-ps-read-path", "worker", "PowerShell", "$env:PATH -split ';'; git status", "A"),
    ("R13-near-other-var", "worker", "Bash", "MYPATH=x; git status", "A"),
    ("R13-near-no-git", "worker", "Bash", "PATH=.tmp/bin:$PATH; echo hi", "A"),
]


@pytest.mark.parametrize(("case", "mode", "tool", "command", "expect"), ROUND4_CASES, ids=[c[0] for c in ROUND4_CASES])
def test_round4_findings_through_the_hook(case: str, mode: str, tool: str, command: str, expect: str) -> None:
    got, err = entry(command, tool, mode=mode)
    assert got == expect, f"{case}: {command!r} -> {got} {err}"


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("git", "git"), ("GIT.CMD", "git"), ("C:/Git/cmd/git.bat", "git"), ("git.ps1", "git"),
        ("git-commit.exe", "git-commit"), ("python3.12", "python"), ("ruby3.4", "ruby"), ("nodejs", "node"),
        ("node22", "node"), ("perl5.36", "perl"), ("digit", "digit"), ("gitk", "gitk"), ("md5sum", "md5sum"),
    ],
)
def test_program_stem(word: str, stem: str) -> None:
    """Round 4, items 2 and 6: one normaliser for every program-name comparison."""
    assert tg.program_stem(word) == stem


def test_a_worktree_pointing_at_the_project_is_not_the_project(guard_repo: Path) -> None:
    """R10: another repository whose `core.worktree` names the project root has the project's toplevel but its own
    Git directory; a repository that is no registered worktree is no worker worktree either."""
    import shutil

    from conftest import git

    other = guard_repo / ".tmp" / "scratch" / "toplevel-other"
    fake = guard_repo / ".tmp" / "worktrees" / "fake"
    try:
        for repo in (other, fake):
            repo.mkdir(parents=True, exist_ok=True)
            git(repo, "init", "-q")
        git(other, "config", "core.worktree", guard_repo.as_posix())
        assert verdict(guard_repo, "git -C .tmp/scratch/toplevel-other commit -m x")[0] == "B"
        assert verdict(guard_repo, "git -C .tmp/worktrees/fake add -A")[0] == "B"
        assert verdict(guard_repo, "git commit -m x")[0] == "A"  # near miss: the project itself
        assert verdict(guard_repo, "git -C .tmp/worktrees/wt1 add -A")[0] == "A"  # near miss: a registered worktree
    finally:
        for repo in (other, fake):
            shutil.rmtree(repo, ignore_errors=True)


def test_the_push_sends_only_the_checked_head(tmp_path: Path) -> None:
    """R11: with `main` at A and HEAD at B on another branch, a marker for B does not let `git push origin main` send
    A; near miss: HEAD on `main` with the marker of its tip."""
    from conftest import git

    repo = tmp_path / "push-src"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name in ("a", "b"):
        if name == "b":
            git(repo, "checkout", "-q", "-b", "other")
        (repo / f"{name}.txt").write_text(name, encoding="utf-8")
        git(repo, "add", f"{name}.txt")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", name)
    cfg = json.loads(json.dumps(_SHIPPED))
    markers = repo / cfg["push_guard"]["marker_dir"]
    markers.mkdir(parents=True)
    head = git(repo, "rev-parse", "HEAD").strip()
    (markers / head).write_text("", encoding="utf-8")
    got, messages = verdict(repo, "git push origin main", cfg=cfg)
    assert got == "B" and any("is not `refs/heads/main`" in m for m in messages), messages
    git(repo, "checkout", "-q", "--detach")
    assert verdict(repo, "git push origin main", cfg=cfg)[0] == "B"  # detached HEAD
    git(repo, "checkout", "-q", "main")
    tip = git(repo, "rev-parse", "HEAD").strip()
    assert verdict(repo, "git push origin main", cfg=cfg)[0] == "B"  # main's tip has no marker
    (markers / tip).write_text("", encoding="utf-8")
    assert verdict(repo, "git push origin main", cfg=cfg)[0] == "A"


def test_a_ref_moved_while_the_guard_reads_it_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R11 reopened (round 4): with only A marked, `main` moves to unmarked B right after the guard's first reading;
    the push is refused. Near miss: no move, the marked tip passes. Scratch repository, setup writes only."""
    from conftest import git

    repo = tmp_path / "push-race"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name in ("a", "b"):
        (repo / f"{name}.txt").write_text(name, encoding="utf-8")
        git(repo, "add", f"{name}.txt")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", name)
    b = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    a = git(repo, "rev-parse", "HEAD").strip()
    cfg = json.loads(json.dumps(_SHIPPED))
    markers = repo / cfg["push_guard"]["marker_dir"]
    markers.mkdir(parents=True)
    (markers / a).write_text("", encoding="utf-8")
    assert verdict(repo, "git push origin main", cfg=cfg)[0] == "A"  # near miss
    real = tg._push_source
    calls: list[int] = []

    def move_after_first(*args: Any) -> Any:
        got = real(*args)
        if not calls:
            git(repo, "update-ref", "refs/heads/main", b)
        calls.append(1)
        return got

    monkeypatch.setattr(tg, "_push_source", move_after_first)
    result, messages = verdict(repo, "git push origin main", cfg=cfg)
    assert result == "B" and any("changed while the guard checked it" in m for m in messages), messages
    assert git(repo, "rev-parse", "HEAD").strip() == b and not (markers / b).exists()


def test_commit_needs_one_message_source(guard_repo: Path) -> None:
    """Gemini F3 in the fixture main tree: no message source would open an editor; `-m` with `-F` is two sources."""
    for command in ("git commit", "git commit -q", "git commit -m x -F .tmp/msg.txt", "git commit -F a -F b"):
        assert verdict(guard_repo, command)[0] == "B", command
    for command in ("git commit -m x", "git commit -q -m x -m y", "git commit -F .tmp/msg.txt"):
        assert verdict(guard_repo, command)[0] == "A", command


def test_patch_save_in_a_worker_worktree(guard_repo: Path) -> None:
    """Gemini F1: `git -C <worker> diff --cached --binary --output=<file>` passes only into `git_guard.patch_dir`."""
    root = guard_repo.as_posix()
    save = "git -C .tmp/worktrees/wt1 diff --cached --binary --output="
    assert verdict(guard_repo, f"{save}{root}/.tmp/pm-state/patches/T-1.patch")[0] == "A"
    assert verdict(guard_repo, f"{save}../../pm-state/patches/T-1.patch")[0] == "A"  # relative to the -C directory
    for command in (
        f"{save}../../../tasks/STATUS.md",
        f"{save}T-1.patch",
        f"{save}{root}/.tmp/pm-state/patches",
        "git diff --cached --binary --output=.tmp/pm-state/patches/T-1.patch",  # the main tree, not a worker
        "git -C .tmp/worktrees/wt1 diff --cached --binary --ext-diff --output=../../pm-state/patches/T-1.patch",
        "git -C .tmp/worktrees/wt1 diff --output=../../pm-state/patches/T-1.patch",
    ):
        assert verdict(guard_repo, command)[0] == "B", command
    assert verdict(guard_repo, f"{save}../../pm-state/patches/T-1.patch", mode="worker")[0] == "B"
    cfg = with_cfg(git_guard={"patch_dir": ".tmp/other-patches"})
    assert verdict(guard_repo, f"{save}../../other-patches/T-1.patch", cfg=cfg)[0] == "A"
    assert verdict(guard_repo, f"{save}../../pm-state/patches/T-1.patch", cfg=cfg)[0] == "B"
    raw = json.loads(json.dumps(_SHIPPED))
    del raw["git_guard"]["patch_dir"]
    with pytest.raises(gc.ConfigError, match="patch_dir"):
        gc.validate(raw)


def test_the_orchestrator_procedure_passes(guard_repo: Path) -> None:
    """Round 3 done check: the orchestrator's own procedure, with the shipped config (push guard on)."""
    from conftest import git

    cfg = json.loads(json.dumps(_SHIPPED))
    head = git(guard_repo, "rev-parse", "HEAD").strip()
    markers = guard_repo / cfg["push_guard"]["marker_dir"]
    markers.mkdir(parents=True, exist_ok=True)
    root = guard_repo.as_posix()
    steps = [
        ("Bash", "git -C .tmp/worktrees/wt1 add -A"),
        ("PowerShell", "git -C .tmp/worktrees/wt1 add -A"),
        ("Bash", f"git -C .tmp/worktrees/wt1 diff --cached --binary --output={root}/.tmp/pm-state/patches/T-9.patch"),
        ("PowerShell", "git -C .tmp/worktrees/wt1 diff --cached --binary > .tmp/pm-state/patches/T-9.patch"),
        ("Bash", "git apply --check .tmp/pm-state/patches/T-9.patch"),
        ("Bash", "git apply .tmp/pm-state/patches/T-9.patch"),
        ("Bash", "git add -A"),
        ("Bash", 'git commit -m "T-9: integrate the task"'),
        ("PowerShell", "git commit -F .tmp/pm-state/msg.txt"),
        ("Bash", "python -B tools/agent-workflow/prepush_check.py"),
        ("Bash", f"python -B tools/agent-workflow/prepush_mark.py --expect {head}"),
        ("Bash", "git worktree add --detach .tmp/worktrees/T-10 HEAD"),
        ("Bash", "git worktree remove .tmp/worktrees/wt1"),
        ("Bash", "git worktree prune"),
        ("Bash", "git branch -d wt1"),
        ("Bash", f"git revert --no-edit {head[:12]}"),
    ]
    try:
        assert verdict(guard_repo, "git push origin main", cfg=cfg)[0] == "B"  # no marker yet
        (markers / head).write_text("", encoding="utf-8")
        steps.append(("Bash", "git push origin main"))
        for tool, command in steps:
            got, messages = verdict(guard_repo, command, tool, cfg=cfg)
            assert got == "A", f"{command!r} -> {got} {messages}"
    finally:
        (markers / head).unlink(missing_ok=True)


DENIED_SPELLINGS = [
    ("commit", "--amend", [], ["-m", "x"]),
    ("tag", "--delete", [], ["v1"]),
    ("tag", "--force", [], ["v1", "HEAD"]),
    ("switch", "--force-create", [], ["main"]),
    ("symbolic-ref", "--delete", [], ["HEAD"]),
    ("reset", "--hard", [], []),
    ("reset", "--soft", [], ["HEAD~1"]),
    ("reset", "--keep", [], []),
    ("worktree", "--force", ["remove"], ["p"]),
    ("push", "--force", [], ["origin", "main"]),
    ("push", "--force-with-lease", [], ["origin", "main"]),
    ("push", "--mirror", [], ["origin"]),
    ("push", "--delete", [], ["origin", "x"]),
    ("push", "--prune", [], ["origin"]),
    ("branch", "--force", [], ["x", "HEAD"]),
]
SAFE_SPELLINGS = [
    "commit -m x",
    "tag --list",
    "reset -- f.py",
    "worktree remove .tmp/worktrees/wt1",
    "push origin main",
    "branch -d x",
    "pull --ff-only",
]
EXEC_FORMS = ["git {sub}", "git-{sub}", "git.exe {sub}", "GIT {sub}", "git --no-pager {sub}", "git -C <ROOT> {sub}"]
PLAIN_EXEC_FORMS = ["git {sub}", "git.exe {sub}", "git -C <ROOT> {sub}"]  # round 2: the only spellings of a write
REAL_PREFIX_OPTIONS = {"switch": ("--force",), "pull": ("--ff",)}  # real options that prefix a ruled one


@given(
    row=s.sampled_from(DENIED_SPELLINGS),
    cut=s.integers(min_value=0, max_value=20),
    form=s.sampled_from(EXEC_FORMS),
    value=s.sampled_from(["", "=x"]),
)
def test_every_spelling_of_a_denied_option_is_denied(
    guard_repo: Path, row: tuple[str, str, list[str], list[str]], cut: int, form: str, value: str
) -> None:
    """Property (review F4): any unique-prefix spelling of a ruled long option (`--d` ... `--delete`), in any
    executable spelling, is refused (round 2: by the allowlist, since no write form takes these options)."""
    sub, full, before, after = row
    spelled = full[: max(3, len(full) - cut)]
    if spelled in REAL_PREFIX_OPTIONS.get(sub, ()):
        spelled = full
    option = spelled + (value if full.startswith("--force-with") else "")
    words = " ".join([*before, option, *after])
    command = form.format(sub=sub).replace("<ROOT>", guard_repo.as_posix()) + " " + words
    got, messages = verdict(guard_repo, command)
    assert got == "B", f"{command!r} -> {got} {messages}"


@pytest.mark.parametrize("words", SAFE_SPELLINGS)
@pytest.mark.parametrize("form", PLAIN_EXEC_FORMS)
def test_safe_spellings_stay_allowed(guard_repo: Path, form: str, words: str) -> None:
    sub, _, rest = words.partition(" ")
    command = form.format(sub=sub).replace("<ROOT>", guard_repo.as_posix()) + " " + rest
    got, messages = verdict(guard_repo, command)
    assert got == "A", f"{command!r} -> {got} {messages}"


def test_monitor_runs_through_the_git_guard(guard_repo: Path) -> None:
    assert verdict(guard_repo, "git commit -m x", tool="Monitor", mode="worker")[0] == "B"
    assert verdict(guard_repo, "git status", tool="Monitor", mode="worker")[0] == "A"


# ---------------------------------------------------------------------------------------------------------------
# Inline interpreter (P-13): no multi-line or long inline code, no program from stdin.

LONG = "import sys; " + "x = 1; " * 40
INLINE_CASES = [
    # Residuals: nested quotes and code built inside the interpreter are not P-13's rule.
    ("IG-01", "Bash", "python -c \"print('hello')\"", "A"),
    ("IG-02", "Bash", "python -c \"exec(chr(10).join(['a=1','print(a)']))\"", "A"),
    ("IG-03", "Bash", "python -c \"import base64; exec(base64.b64decode('...'))\"", "A"),
    ("IG-04", "Bash", 'python -c "x=1\u2028print(x)"', "B"),
    ("IG-05", "Bash", "python - << 'EOF'\nimport os\nEOF", "B"),
    ("IG-06", "Bash", 'python <<< "import sys"', "B"),
    ("IG-07", "PowerShell", "Get-Content script.py | python", "B"),
    ("IG-08", "PowerShell", f'py -c "{LONG}"', "B"),
    ("IG-09", "Bash", 'python -c "import math; print(math.sqrt(4))"', "A"),
    # Residuals, see IG-01.
    ("IG-QTE-01", "Bash", "python -c \"import sys; print('a'); print('b')\"", "A"),
    (
        "IG-QTE-02",
        "Bash",
        "python -c \"exec(chr(10).join(['import os', 'os.system(\\\"dir\\\")']))\"",
        "A",
    ),
    (
        "IG-QTE-03",
        "Bash",
        "python -c \"import base64; exec(base64.b64decode('aW1wb3J0IG9zCnByaW50KDEp'))\"",
        "A",
    ),
    ("IG-UNI-01", "Bash", 'python -c "x=1\u2028print(x)"', "B"),
    ("IG-STD-01", "Bash", "python - << 'EOF'\nimport os\nprint(os.getcwd())\nEOF", "B"),
    ("IG-STD-02", "Bash", 'python <<< "import sys; print(sys.version)"', "B"),
    (
        "IG-STD-03",
        "Bash",
        "powershell -Command \"@'\nimport sys\nprint(sys.version)\n'@ | python -\"",
        "B",
    ),
    ("IG-STD-04", "PowerShell", "Get-Content script.py | python", "B"),
    ("IG-BIN-01", "Bash", f'py -c "{LONG}"', "B"),
    ("IG-FB-01", "Bash", 'python -c "import math; print(math.sqrt(144))"', "A"),
    ("X-I01", "Bash", 'python -c "import os\nprint(1)"', "B"),
    ("X-I02", "Bash", "python -", "B"),
    ("X-I03", "Bash", "cat x.py | python3", "B"),
    ("X-I04", "Bash", "python < script.py", "B"),
    ("X-I05", "Bash", "python script.py < input.txt", "A"),
    ("X-I06", "Bash", "echo x | python script.py", "A"),
    ("X-I07", "Bash", "python -m json.tool < a.json", "A"),
    ("X-I08", "Bash", 'node -e "a()\nb()"', "B"),
    ("X-I09", "Bash", 'node -e "console.log(1)"', "A"),
    ("X-I10", "Bash", 'python -Bc "x=1\ny=2"', "B"),
    ("X-I11", "Bash", "python -cprint(1)", "A"),
    ("X-I12", "PowerShell", '& $PY -c "a=1\nb=2"', "B"),
    ("X-I13", "PowerShell", "@'\nprint(1)\n'@ | python -", "B"),
    ("X-I14", "Bash", "perl -e 'print 1;\nprint 2;'", "B"),
    ("X-I15", "Bash", 'python3.12 -c "a\rb"', "B"),
    ("X-I16", "Bash", "python -I -B tools/x.py", "A"),
]


@pytest.mark.parametrize(
    ("case", "tool", "command", "expect"), INLINE_CASES, ids=[c[0] for c in INLINE_CASES]
)
def test_inline_interpreter(
    guard_repo: Path, case: str, tool: str, command: str, expect: str
) -> None:
    check(guard_repo, case, tool, command, expect)


# ---------------------------------------------------------------------------------------------------------------
# Pytest slot (P-15).

PYTEST_CASES = [
    ("TS-01", "Bash", 'python -m "pytest" tests/', "B"),
    ("TS-02", "Bash", "pytest tests/", "B"),
    ("TS-03", "Bash", "python test_slot.py true && pytest tests/", "B"),
    ("TS-04", "Bash", "python test_slot.py python -m pytest tests/", "A"),
    ("TS-QTE-01", "Bash", 'python -m "pytest" tests/unit/test_config.py', "B"),
    ("TS-QTE-02", "Bash", 'python "-m" pytest tests/unit/test_config.py', "B"),
    ("TS-BIN-01", "Bash", "pytest tests/unit/test_config.py", "B"),
    ("TS-BIN-02", "Bash", ".venv/Scripts/pytest tests/unit/test_config.py", "B"),
    (
        "TS-CHN-01",
        "Bash",
        'python tools/agent-workflow/test_slot.py python -c "print(1)" && pytest tests/unit/',
        "B",
    ),
    ("TS-FB-01", "Bash", "python -m pytest --collect-only", "A"),
    ("X-T01", "Bash", "python tools/agent-workflow/test_slot.py -- python -m pytest -q tests", "A"),
    (
        "X-T02",
        "PowerShell",
        "& $PY -B $R/tools/agent-workflow/test_slot.py -- $PY -m pytest -q tests",
        "A",
    ),
    ("X-T03", "PowerShell", "& $PY -m pytest -q tests", "B"),
    ("X-T04", "Bash", "uv run pytest", "B"),
    ("X-T05", "Bash", "python -mpytest tests", "B"),
    ("X-T06", "Bash", "python -m pytest --version", "A"),
    (
        "X-T07",
        "Bash",
        "powershell -File scripts/int_delta_tests.ps1 -- python -m pytest tests",
        "A",
    ),
    ("X-T08", "Bash", "python -c \"import pytest; pytest.main(['tests'])\"", "B"),
    ("X-T09", "Bash", "bash -c 'pytest tests'", "B"),
    (
        "X-T10",
        "Bash",
        "D:/x/.venv/Scripts/python.exe -B D:/x/tools/agent-workflow/test_slot.py"
        " -- D:/x/.venv/Scripts/python.exe -m pytest",
        "A",
    ),
]


@pytest.mark.parametrize(
    ("case", "tool", "command", "expect"), PYTEST_CASES, ids=[c[0] for c in PYTEST_CASES]
)
def test_pytest_slot(guard_repo: Path, case: str, tool: str, command: str, expect: str) -> None:
    check(guard_repo, case, tool, command, expect)


# ---------------------------------------------------------------------------------------------------------------
# Read router (P-12, warn only).

READ_CASES = [
    ("X-R01", "PowerShell", "Get-Content tasks/STATUS.md", "W"),
    ("X-R02", "PowerShell", "Get-Content tasks/STATUS.md | Select-Object -First 5", "W"),
    ("X-R03", "PowerShell", "Get-Content a.txt | python tools/x.py", "A"),
    ("X-R04", "PowerShell", "$t = Get-Content a.txt", "A"),
    ("X-R05", "Bash", "cat a.txt", "W"),
    ("X-R06", "PowerShell", "Select-String -Path a.txt -Pattern x", "W"),
    ("X-R07", "PowerShell", "Get-ChildItem -Recurse src", "W"),
    ("X-R08", "PowerShell", "gci -r src", "W"),
    ("X-R09", "Bash", "ls -R src", "W"),
    ("X-R10", "PowerShell", "[IO.File]::ReadAllText('a.txt')", "W"),
    ("X-R11", "PowerShell", "$t = [IO.File]::ReadAllText('a.txt')", "A"),
    ("X-R12", "PowerShell", "Get-ChildItem src", "A"),
    ("X-R13", "Bash", "ls -la", "A"),
    ("X-R14", "PowerShell", "git log | Select-String fix", "A"),
]


@pytest.mark.parametrize(
    ("case", "tool", "command", "expect"), READ_CASES, ids=[c[0] for c in READ_CASES]
)
def test_read_router_warns_only(
    guard_repo: Path, case: str, tool: str, command: str, expect: str
) -> None:
    check(guard_repo, case, tool, command, expect)


# ---------------------------------------------------------------------------------------------------------------
# Configuration overrides (non-default values change the verdict).


def with_cfg(**sections: dict[str, Any]) -> dict[str, Any]:
    cfg = json.loads(json.dumps(CFG))
    for name, values in sections.items():
        cfg[name].update(values)
    return gc.validate(cfg)


def test_override_allowed_dirs(guard_repo: Path) -> None:
    cfg = with_cfg(write_guard={"allowed_dirs": ["runs"]})
    assert verdict(guard_repo, "echo x > .tmp/scratch/a.txt", cfg=cfg)[0] == "B"
    assert verdict(guard_repo, "echo x > runs/a.txt", cfg=cfg)[0] == "A"


def test_override_modes(guard_repo: Path) -> None:
    for mode, expect in (("warn", "W"), ("off", "A")):
        cfg = with_cfg(write_guard={"mode": mode})
        assert verdict(guard_repo, "echo x > docs/a.md", cfg=cfg)[0] == expect
    assert verdict(guard_repo, "pytest", cfg=with_cfg(pytest_slot={"mode": "off"}))[0] == "A"
    for mode, expect in (("off", "A"), ("block", "B")):
        cfg = with_cfg(read_router={"mode": mode})
        assert verdict(guard_repo, "Get-Content a", "PowerShell", cfg=cfg)[0] == expect


def test_override_inline_limit_and_flags(guard_repo: Path) -> None:
    cfg = with_cfg(inline_interp={"max_inline_chars": 5})
    assert verdict(guard_repo, 'python -c "print(1)"', cfg=cfg)[0] == "B"
    cfg = with_cfg(inline_interp={"code_flags": {"python": ["-c"], "deno": ["-e"]}})
    assert verdict(guard_repo, 'deno -e "a\nb"', cfg=cfg)[0] == "B"
    assert verdict(guard_repo, 'node -e "a\nb"', cfg=cfg)[0] == "A"


def test_override_pytest_launchers_and_exemptions(guard_repo: Path) -> None:
    cfg = with_cfg(pytest_slot={"launchers": ["slot2.py"], "exempt_flags": ["--co"]})
    assert verdict(guard_repo, "python slot2.py -- python -m pytest", cfg=cfg)[0] == "A"
    assert verdict(guard_repo, "python test_slot.py -- python -m pytest", cfg=cfg)[0] == "B"
    assert verdict(guard_repo, "python -m pytest --collect-only", cfg=cfg)[0] == "B"


def test_override_git_lists(guard_repo: Path) -> None:
    readonly = [x for x in CFG["git_guard"]["readonly_subcommands"] if x != "log"]
    cfg = with_cfg(
        git_guard={"readonly_subcommands": readonly, "allowed_config_keys": ["color.ui"]}
    )
    assert verdict(guard_repo, "git log", mode="worker", cfg=cfg)[0] == "B"
    quotepath = "git -c core.quotepath=false status"
    assert verdict(guard_repo, quotepath, mode="worker", cfg=cfg)[0] == "B"
    assert verdict(guard_repo, "git -c color.ui=never status", mode="worker", cfg=cfg)[0] == "A"


def test_override_git_and_inline_modes(guard_repo: Path) -> None:
    for mode, expect in (("warn", "W"), ("off", "A")):
        cfg = with_cfg(git_guard={"mode": mode})
        assert verdict(guard_repo, "git commit -m x", mode="worker", cfg=cfg)[0] == expect
        cfg = with_cfg(inline_interp={"mode": mode})
        assert verdict(guard_repo, 'python -c "a=1\nb=2"', cfg=cfg)[0] == expect


def test_override_worktrees_dir_for_the_main_tree_rule(guard_repo: Path) -> None:
    wt = guard_repo / ".tmp" / "worktrees" / "wt1"
    assert verdict(guard_repo, "git commit -m x", cwd=wt)[0] == "B"
    assert verdict(guard_repo, "git -C .tmp/worktrees/wt1 add src/fix.py")[0] == "A"
    from conftest import git

    w2 = guard_repo / ".tmp" / "other" / "w2"
    if not w2.exists():  # round 3 class G: a worker worktree is a registered worktree of the project, not a path
        git(guard_repo, "worktree", "add", "-q", "--detach", str(w2))
    cfg = with_cfg(git_guard={"worktrees_dir": ".tmp/other"})
    # wt1 is no worker worktree any more, and its toplevel is not the project root: neither kind, refused.
    assert verdict(guard_repo, "git -C .tmp/worktrees/wt1 add src/fix.py", cfg=cfg)[0] == "B"
    assert verdict(guard_repo, "git -C .tmp/other/w2 add x.py", cfg=cfg)[0] == "A"
    assert verdict(guard_repo, "git -C .tmp/other/w2 commit -m x", cfg=cfg)[0] == "B"


def test_override_push_remote_and_branch(guard_repo: Path) -> None:
    assert verdict(guard_repo, "git push origin main")[0] == "A"
    cfg = with_cfg(git_guard={"push_remote": "upstream", "push_branch": "trunk"})
    assert verdict(guard_repo, "git push upstream trunk", cfg=cfg)[0] == "A"
    assert verdict(guard_repo, "git push origin main", cfg=cfg)[0] == "B"
    raw = json.loads(json.dumps(_SHIPPED))
    del raw["git_guard"]["push_branch"]
    with pytest.raises(gc.ConfigError, match="push_branch"):
        gc.validate(raw)


def test_removed_merge_list_is_refused_by_the_config() -> None:
    raw = json.loads(json.dumps(_SHIPPED))
    raw["git_guard"]["merge_subcommands"] = ["merge", "add"]
    with pytest.raises(gc.ConfigError, match="unknown keys: merge_subcommands"):
        gc.validate(raw)


def test_override_git_timeout(guard_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_run(*_a: Any, **k: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(k.get("timeout"))
        return subprocess.CompletedProcess([], 1, b"", b"")

    monkeypatch.setattr(tg.subprocess, "run", fake_run)
    cfg = gc.validate({**json.loads(json.dumps(CFG)), "git_timeout_seconds": 7})
    # PM write: the main-tree check asks git for the toplevel; the double answers rc 1, so the write is refused.
    assert verdict(guard_repo, "git commit -m x", cfg=cfg)[0] == "B"
    assert seen and set(seen) == {7}


def test_override_max_command_chars(guard_repo: Path) -> None:
    cfg = gc.validate({**json.loads(json.dumps(CFG)), "max_command_chars": 20})
    assert verdict(guard_repo, "echo x > .tmp/scratch/a.txt", cfg=cfg)[0] == "B"
    assert verdict(guard_repo, "ls -la .tmp/scratch/aaaaaa", cfg=cfg)[0] == "A"


# ---------------------------------------------------------------------------------------------------------------
# Failure policy: never block other tools; config errors block only shells; crashes block only guarded commands.


def run_hook(event: Any, *args: str, raw: str | None = None) -> subprocess.CompletedProcess[str]:
    data = raw if raw is not None else json.dumps(event)
    return subprocess.run(
        [PY, "-B", "-I", str(HOOKS / "tool_guard.py"), *args],
        input=data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def test_other_tools_are_never_blocked(guard_repo: Path) -> None:
    for tool in ("Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"):
        event = {"tool_name": tool, "tool_input": {"command": "git push --force > tasks/STATUS.md"}}
        assert run_hook(event, "--git-mode", "worker").returncode == 0


def test_malformed_event_passes() -> None:
    assert run_hook(None, "--git-mode", "pm", raw="{not json").returncode == 0
    assert run_hook(None, "--git-mode", "pm", raw="[1, 2]").returncode == 0


def test_broken_config_blocks_only_shells(tmp_path: Path) -> None:
    bad = gc.ROOT / ".tmp" / "r3-guard-hooks-test" / f"bad-{os.getpid()}.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    try:
        bad.write_text('{"schema_version": 1}', encoding="utf-8")
        flags = ("--git-mode", "pm", "--config", str(bad))
        shell = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, *flags)
        assert shell.returncode == 2 and bad.name in shell.stderr and "Edit tool" in shell.stderr
        edit = run_hook({"tool_name": "Edit", "tool_input": {}}, *flags)
        assert edit.returncode == 0
    finally:
        bad.unlink(missing_ok=True)


def test_invalid_hook_arguments_block_only_shells() -> None:
    bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert run_hook(bash).returncode == 2  # --git-mode missing
    assert run_hook(bash, "--git-mode", "admin").returncode == 2
    # The merge mode was removed (decision 77): an old hook line fails closed for the shell tools.
    assert run_hook(bash, "--git-mode", "worker-merge", "--worktree", "x").returncode == 2
    assert run_hook(bash, "--git-mode", "worker", "--worktree", "x").returncode == 2
    assert run_hook({"tool_name": "Read", "tool_input": {}}).returncode == 0


def test_hook_process_blocks_and_warns(guard_repo: Path) -> None:
    cwd = str(guard_repo)
    pytest_cmd = {"tool_name": "Bash", "tool_input": {"command": "pytest"}, "cwd": cwd}
    blocked = run_hook(pytest_cmd, "--git-mode", "pm")
    assert blocked.returncode == 2 and "BLOCKED by tool_guard" in blocked.stderr
    assert "test_slot.py" in blocked.stderr
    read_cmd = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-Content a.txt"},
        "cwd": cwd,
    }
    warned = run_hook(read_cmd, "--git-mode", "pm")
    assert warned.returncode == 0
    context = json.loads(warned.stdout)["hookSpecificOutput"]
    assert context["hookEventName"] == "PreToolUse"
    assert "[read-router]" in context["additionalContext"]


def test_crash_blocks_only_commands_carrying_the_trigger(
    guard_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_ctx: Any) -> list[str]:
        raise RuntimeError("injected")

    monkeypatch.setattr(tg, "CRASH_LOG", guard_repo / ".tmp" / "scratch" / "crash.log")
    for name, trigger, harmless in [
        ("check_write", "echo x > .tmp/scratch/a", "ls -la"),
        ("check_git", "git status", "ls -la"),
        ("check_inline", "python x.py", "ls -la"),
        ("check_pytest", "pytest", "ls -la"),
    ]:
        with monkeypatch.context() as m:
            m.setattr(tg, name, boom)
            assert verdict(guard_repo, trigger)[0] == "B", name
            assert verdict(guard_repo, harmless)[0] == "W", name
    assert (guard_repo / ".tmp" / "scratch" / "crash.log").exists()


def test_unparseable_guarded_command_fails_closed(
    guard_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise ValueError("parser failure")

    monkeypatch.setattr(tg, "CRASH_LOG", guard_repo / ".tmp" / "scratch" / "crash.log")
    monkeypatch.setattr(st, "walk", broken)
    assert verdict(guard_repo, "Set-Content docs/a.md x", "PowerShell")[0] == "B"
    assert verdict(guard_repo, "git commit -m x", mode="worker")[0] == "B"
    assert verdict(guard_repo, "echo hello")[0] == "W"


def test_invisible_characters_next_to_guarded_commands_block(guard_repo: Path) -> None:
    assert verdict(guard_repo, "g\u200bit commit -m x", mode="worker")[0] == "B"
    assert verdict(guard_repo, "echo \u200b hello")[0] == "A"


def test_top_level_crash_does_not_lock_the_session(tmp_path: Path) -> None:
    """An import failure exits 1, which Claude Code treats as non-blocking: the tool call goes ahead."""
    hooks = tmp_path / "a" / "b" / "c" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("tool_guard.py", "guard_config.py", "process_guard.py", "tool_health.py", "git_ledger.py"):
        shutil.copy(HOOKS / name, hooks / name)
    (hooks / "shell_targets.py").write_text("raise ImportError('injected')\n", encoding="utf-8")
    proc = subprocess.run(
        [PY, "-B", "-I", str(hooks / "tool_guard.py"), "--git-mode", "pm"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push --force"}}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode not in (0, 2) and "injected" in proc.stderr


# ---------------------------------------------------------------------------------------------------------------
# Hypothesis properties of the tokenisers (decision 130).

SAFE_SEGMENT = s.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=8)


@given(
    parts=s.lists(SAFE_SEGMENT, min_size=1, max_size=3),
    quote=s.sampled_from(["", "'", '"']),
    allowed=s.booleans(),
)
def test_quoting_does_not_change_the_write_verdict(
    guard_repo: Path, parts: list[str], quote: str, allowed: bool
) -> None:
    rel = "/".join([".tmp", "scratch", *parts] if allowed else ["docs", *parts])
    plain = verdict(guard_repo, f"echo x > {rel}")[0]
    target = "/".join(f"{quote}{p}{quote}" for p in rel.split("/"))
    quoted = verdict(guard_repo, f"echo x > {target}")[0]
    assert plain == quoted == ("A" if allowed else "B")


@given(
    read=s.sampled_from(["git status", "git log -1", "git diff", "git show HEAD", "ls"]),
    sep=s.sampled_from([";", "&&", "||", "|", "\n", " & "]),
    write=s.sampled_from(
        ["echo x > tasks/STATUS.md", "rm docs/a.md", "touch src/new.py", "git commit -m x"]
    ),
)
def test_a_read_chained_with_a_write_is_blocked(
    guard_repo: Path, read: str, sep: str, write: str
) -> None:
    assert verdict(guard_repo, f"{read}{sep}{write}", mode="worker")[0] == "B"


@given(sub=s.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=2, max_size=12))
def test_worker_git_subcommands_outside_the_allowlist_are_blocked(
    guard_repo: Path, sub: str
) -> None:
    if sub in CFG["git_guard"]["readonly_subcommands"] or sub.startswith("-"):
        return
    assert verdict(guard_repo, f"git {sub} x", mode="worker")[0] == "B"


@given(
    command=s.text(min_size=0, max_size=60),
    verb=s.sampled_from(["Set-Content", "rm", ">", "git", "pytest", "python"]),
)
def test_a_crashing_parser_blocks_every_command_with_a_trigger(
    guard_repo: Path, command: str, verb: str
) -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise ValueError("parser failure")

    original, tg.CRASH_LOG = tg.CRASH_LOG, guard_repo / ".tmp" / "scratch" / "crash.log"
    walk, st.walk = st.walk, broken
    try:
        assert verdict(guard_repo, f"{command} {verb} docs/a.md", mode="worker")[0] == "B"
    finally:
        st.walk, tg.CRASH_LOG = walk, original


@given(command=s.text(max_size=80), tool=s.sampled_from(["Bash", "PowerShell", "Monitor"]))
def test_evaluate_never_raises(guard_repo: Path, command: str, tool: str) -> None:
    got, _ = verdict(guard_repo, command, tool, mode="worker")
    assert got in ("A", "W", "B")


@given(flips=s.lists(s.booleans(), min_size=14, max_size=14))
def test_case_variants_of_a_tracked_path_are_blocked(guard_repo: Path, flips: list[bool]) -> None:
    path = "tasks/STATUS.md"
    text = "".join(c.upper() if f else c for c, f in zip(path, flips, strict=False)) + path[14:]
    assert verdict(guard_repo, f"echo x > {text}")[0] == "B"


# ---------------------------------------------------------------------------------------------------------------
# Fix round 1: every reviewer probe (tasks/R3_GUARD_HOOKS_REVIEW.md F1-F15, R3_GUARD_HOOKS_REVIEW_GEMINI.md G1-G6)
# and the sibling forms of each class (invariant I-6 in the task file). Rows: (case, mode, tool, command, verdict).

FR1_CASES = [
    # F1 unquoted backticks are command substitution
    ("F1-1", "worker", "Bash", "echo `rm docs/a.md`", "B"),
    ("F1-2", "worker", "Bash", "x=`git commit -m x`", "B"),
    ("F1-3", "worker", "Bash", "ls `touch docs/a.md`", "B"),
    ("F1-4", "worker", "Bash", "echo `pytest tests`", "B"),
    # F2 `{}` placeholders of find -exec and xargs -I
    ("F2-1", "worker", "Bash", "find src -name '*.py' -exec sed -i s/a/b/ {} \\;", "B"),
    ("F2-2", "worker", "Bash", "find src -name '*.py' -exec sed -i s/a/b/ {} +", "B"),
    ("F2-3", "worker", "Bash", "find . -type f -exec chmod 644 {} \\;", "B"),
    ("F2-4", "worker", "Bash", "find . -name '*.orig' -exec mv {} .tmp/ \\;", "B"),
    ("F2-5", "worker", "Bash", "find . -name x | xargs -I{} rm {}", "B"),
    ("F2-6", "worker", "Bash", "find . -name x | xargs -I {} sh -c 'rm {}'", "B"),
    ("F2-7", "worker", "Bash", "git ls-files | xargs -I{} rm {}", "B"),
    ("F2-8", "worker", "Bash", "cat list | xargs -I{} cp {} docs/a.md", "B"),
    ("F2-9", "worker", "Bash", "cat list | xargs -I {} git commit -m {}", "B"),
    # F3 / G3 redirection spellings
    ("F3-1", "worker", "Bash", "echo x >& docs/a.md", "B"),
    ("F3-2", "worker", "Bash", "echo x 1<> docs/a.md", "B"),
    ("F3-3", "worker", "Bash", "exec 3<>docs/a.md", "B"),
    ("F3-4", "worker", "Bash", "cat <> docs/a.md", "B"),
    ("F3-5", "worker", "Bash", "bash -c 'echo corrupt 1<> tasks/STATUS.md'", "B"),
    ("F3-6", "worker", "Bash", "echo x >| docs/a.md", "B"),
    ("F3-7", "worker", "Bash", "echo x 2>&1", "A"),
    ("F3-8", "worker", "Bash", "echo x >&2", "A"),
    # F4 sed commands and flags that write or execute
    ("F4-1", "worker", "Bash", "sed -n 'w docs/a.md' x", "B"),
    ("F4-2", "worker", "Bash", "sed '/a/w docs/a.md' x", "B"),
    ("F4-3", "worker", "Bash", "sed -e '1w f' x", "B"),
    ("F4-4", "worker", "Bash", "sed '1e rm docs/a.md' x", "B"),
    ("F4-5", "worker", "Bash", "sed --expression='1W f' x", "B"),
    ("F4-6", "worker", "Bash", "sed -f script.sed x", "B"),
    ("F4-7", "worker", "Bash", "sed -n '1,5p' docs/a.md", "A"),
    ("F4-8", "worker", "Bash", "sed 's/a/b/g' docs/a.md", "A"),
    # F5 inner shells: cmd `//c`, positional PowerShell command, combined `-xc`
    ("F5-1", "worker", "Bash", 'cmd //c "del docs\\a.md"', "B"),
    ("F5-2", "worker", "Bash", 'cmd.exe //k "del docs\\a.md"', "B"),
    ("F5-3", "worker", "PowerShell", 'powershell -NoProfile "sc docs/a.md x"', "B"),
    (
        "F5-4",
        "worker",
        "PowerShell",
        'pwsh -NoProfile -ExecutionPolicy Bypass "Set-Content docs/a.md x"',
        "B",
    ),
    ("F5-5", "worker", "Bash", "bash -xc 'rm docs/a.md'", "B"),
    # F6 one-letter output parameters
    ("F6-1", "worker", "PowerShell", "iwr http://x -o docs/a.md", "B"),
    ("F6-2", "worker", "PowerShell", "irm http://x -O docs/a.md", "B"),
    # F7 a pipeline source moved away
    ("F7-1", "worker", "PowerShell", "Get-Item docs/a.md | Move-Item -Destination .tmp", "B"),
    # F8 git named by a run-time expression
    ("F8-1", "worker", "PowerShell", "& (Get-Command git) commit -m x", "B"),
    ("F8-2", "worker", "PowerShell", "& (gcm git) commit -m x", "B"),
    ("F8-3", "pm", "PowerShell", "& (gcm git) push -f", "B"),
    ("F8-4", "worker", "PowerShell", "$g='gi'+'t'; & $g commit -m x", "B"),
    ("F8-5", "pm", "PowerShell", "$g='gi'+'t'; & $g push -f", "B"),
    ("F8-6", "worker", "PowerShell", "$g='git'; & $g commit -m x", "B"),
    ("F8-7", "pm", "PowerShell", "$g='git'; & $g push --force", "B"),
    ("F8-8", "worker", "PowerShell", "$g='git'; & $g status", "A"),
    ("F8-9", "pm", "PowerShell", "$g='git'; & $g push", "B"),  # round 2: a write needs a literal `git`
    # F9 computed command names
    ("F9-1", "worker", "PowerShell", "iex ('Set-'+'Content docs/a.md x')", "B"),
    ("F9-2", "worker", "PowerShell", "& ('Set-'+'Content') docs/a.md x", "B"),
    ("F9-3", "worker", "PowerShell", "$c='Set-'+'Content'; & $c docs/a.md x", "B"),
    ("F9-4", "worker", "PowerShell", "& (\"{0}-{1}\" -f 'Set','Content') docs/a.md x", "B"),
    ("F9-5", "worker", "Bash", "$(printf '\\x72\\x6d') docs/a.md", "B"),
    ("F9-6", "worker", "Bash", "c=$'\\x72\\x6d'; $c docs/a.md", "B"),
    ("F9-7", "worker", "Bash", "$'\\x72\\x6d' docs/a.md", "B"),
    ("F9-8", "worker", "Bash", 'c=rm; "$c" docs/a.md', "B"),
    ("F9-9", "worker", "Bash", "c=r; ${c}m docs/a.md", "B"),
    ("F9-10", "worker", "Bash", "z='rm -f'; $z docs/a.md", "B"),
    ("F9-11", "worker", "Bash", "cmd='rm docs/a.md'; eval $cmd", "B"),
    ("F9-12", "worker", "PowerShell", "$s = 'Set-Content docs/a.md x'; iex $s", "B"),
    ("F9-13", "worker", "PowerShell", "$c = 'Set-Content'; & $c docs/a.md x", "B"),
    ("F9-14", "worker", "PowerShell", "$c = 'Set-Content'; & $c .tmp/scratch/x.txt x", "A"),
    ("F9-15", "worker", "Bash", "for c in rm; do $c docs/a.md; done", "B"),
    ("F9-16", "worker", "Bash", "read c <<< rm; $c docs/a.md", "B"),
    # F10 programs on stdin into an interpreter held in a variable
    ("F10-1", "worker", "PowerShell", '"print(1)" | & $PY -', "B"),
    ("F10-2", "worker", "PowerShell", "$code | & $PY -", "B"),
    ("F10-3", "worker", "PowerShell", "Get-Content x.py | & $PY -", "B"),
    ("F10-4", "worker", "PowerShell", "& $PY - < x.py", "B"),
    ("F10-5", "worker", "PowerShell", '$PY="D:/x/python.exe"; "print(1)" | & $PY -', "B"),
    # G1 quoted .NET method names
    (
        "G1-1",
        "worker",
        "PowerShell",
        "[System.IO.File]::\"WriteAllText\"('tasks/STATUS.md', 'x')",
        "B",
    ),
    ("G1-2", "worker", "PowerShell", "[IO.File]::'Delete'('docs/a.md')", "B"),
    (
        "G1-3",
        "worker",
        "Bash",
        "powershell -Command \"[System.IO.File]::\\\"WriteAllText\\\"('tasks/STATUS.md', 'x')\"",
        "B",
    ),
    (
        "G1-4",
        "worker",
        "PowerShell",
        "powershell -Command \"[IO.File]::`\"WriteAllText`\"('tasks/STATUS.md', 'x')\"",
        "B",
    ),
    ("G1-5", "worker", "PowerShell", "$m='WriteAllText'; [IO.File]::$m('docs/a.md','x')", "B"),
    ("G1-6", "worker", "PowerShell", "$f=[IO.File]; $f::WriteAllText('docs/a.md','x')", "B"),
    ("G1-7", "worker", "PowerShell", "[IO.File]::ReadAllText('docs/a.md').Length", "W"),
    ("G1-8", "worker", "PowerShell", "[IO.Path]::Combine('a','b')", "A"),
    # G2 Alias:/Function: drives and alias or function definitions
    (
        "G2-1",
        "worker",
        "PowerShell",
        "Set-Content Alias:sc2 -Value Set-Content; sc2 tasks/STATUS.md 'x'",
        "B",
    ),
    ("G2-2", "worker", "PowerShell", "Set-Item Alias:x Set-Content", "B"),
    ("G2-3", "worker", "PowerShell", "New-Item -Path Function:x -Value { 'a' }", "B"),
    ("G2-4", "worker", "PowerShell", "Set-Alias x Set-Content; x docs/a.md y", "B"),
    ("G2-5", "worker", "PowerShell", "sal x Set-Content", "B"),
    ("G2-6", "worker", "PowerShell", "nal x Remove-Item", "B"),
    ("G2-7", "worker", "PowerShell", "$alias:z = 'Set-Content'; z docs/a.md x", "B"),
    ("G2-8", "worker", "PowerShell", "${function:z} = { 'a' }; z > docs/a.md", "B"),
    ("G2-9", "worker", "Bash", "alias r=rm; r docs/a.md", "B"),
    ("G2-10", "worker", "PowerShell", "Set-Content env:X 1", "A"),
    # G4 variables set by a cmdlet, a loop or an output variable
    ("G4-1", "worker", "PowerShell", "Set-Variable c Set-Content; & $c tasks/STATUS.md 'x'", "B"),
    ("G4-2", "worker", "PowerShell", "sv c Set-Content; & $c docs/a.md x", "B"),
    (
        "G4-3",
        "worker",
        "PowerShell",
        "New-Variable -Name c -Value Set-Content; & $c docs/a.md x",
        "B",
    ),
    ("G4-4", "worker", "PowerShell", "Set-Item Variable:c Set-Content; & $c docs/a.md x", "B"),
    (
        "G4-5",
        "worker",
        "PowerShell",
        "Get-Command Set-Content -OutVariable c; & $c docs/a.md x",
        "B",
    ),
    ("G4-6", "worker", "PowerShell", "foreach ($c in 'Set-Content') { & $c docs/a.md x }", "B"),
    # G6 `[` and `test` redirect like any command; `[[` compares
    ("G6-1", "worker", "Bash", "test a > docs/a.md", "B"),
    ("G6-2", "worker", "Bash", "[ a > docs/a.md ]", "B"),
    ("G6-3", "worker", "Bash", "[[ a > b ]]", "A"),
    # F12 help is exempt from the test slot
    ("F12-1", "worker", "PowerShell", "pytest --help", "A"),
    ("F12-2", "worker", "Bash", "python -m pytest -h", "A"),
    # F14 dynamic targets resolved from literal bindings, `$PWD`, globs in an allowed directory, mkdir of it
    ("F14-1", "worker", "PowerShell", "$name='a'; Set-Content \".tmp/scratch/$name.txt\" x", "A"),
    ("F14-2", "worker", "Bash", 'echo x > "$PWD/.tmp/scratch/x"', "A"),
    ("F14-3", "worker", "PowerShell", '$p=".tmp\\scratch\\x"; Remove-Item -Recurse -Force $p', "A"),
    ("F14-4", "worker", "Bash", "rm .tmp/*.log", "A"),
    ("F14-5", "worker", "PowerShell", "Remove-Item .tmp\\*.log", "A"),
    ("F14-6", "worker", "Bash", "rm -rf .tmp/scratch/*", "A"),
    ("F14-7", "worker", "Bash", "mkdir -p runs", "A"),
    ("F14-8", "worker", "PowerShell", "mkdir runs", "A"),
    ("F14-9", "worker", "PowerShell", "New-Item -ItemType Directory -Force .tmp", "A"),
    ("F14-10", "worker", "Bash", "rm .tmp/*", "B"),
    ("F14-11", "worker", "Bash", "rm .tmp/*.txt", "B"),
    ("F14-12", "worker", "Bash", "touch runs", "B"),
    ("F14-13", "worker", "PowerShell", '$p="..\\x"; Remove-Item $p', "B"),
    ("F14-14", "worker", "PowerShell", 'Set-Content ".tmp/$name.txt" x', "B"),
    ("F14-15", "worker", "Bash", "rm -rf .tmp/$x", "B"),
    ("F14-16", "worker", "Bash", "rm src/*.py", "B"),
    ("F14-17", "worker", "PowerShell", "$p='docs/a.md'; Remove-Item $p", "B"),
    ("F14-18", "worker", "PowerShell", "Get-ChildItem .tmp -Filter *.log | Remove-Item", "B"),
    # F15 worker stash reads; PM aliases and run-time push arguments
    ("F15-1", "worker", "Bash", "git stash list", "A"),
    ("F15-2", "worker", "Bash", "git stash show -p", "A"),
    ("F15-3", "worker", "Bash", "git stash", "B"),
    ("F15-4", "worker", "Bash", "git stash pop", "B"),
    ("F15-5", "pm", "Bash", 'git config alias.pf "push -f"; git pf', "B"),
    ("F15-6", "pm", "Bash", "git config --global alias.x '!git push --force'", "B"),
    ("F15-7", "pm", "Bash", "git push $a", "B"),
    ("F15-8", "pm", "PowerShell", "git push @('-f')", "B"),
    ("F15-9", "pm", "Bash", "git config user.name x", "B"),  # round 2: config writes are no write form
    ("F15-10", "pm", "Bash", "git config alias.y '!sh x.sh'", "B"),  # a shell alias may push later
    ("F15-11", "pm", "Bash", "git config alias.st status", "B"),
    # F16 classes closed in fix round 1: PATHEXT spellings and bash `eval` joining its words
    ("F16-1", "worker", "Bash", "git.cmd commit -m x", "B"),
    ("F16-2", "pm", "PowerShell", "git.bat push --force", "B"),
    ("F16-3", "worker", "Bash", "git.cmd status", "A"),
    ("F16-4", "worker", "Bash", "eval 'echo x' '>' docs/a.md", "B"),
    ("F16-5", "worker", "Bash", "eval echo x '>' docs/a.md", "B"),
    ("F16-6", "worker", "Bash", "eval echo x '>' .tmp/scratch/x.log", "A"),
    ("F16-7", "worker", "Bash", "eval git 'commit' -m x", "B"),
    ("F16-8", "worker", "PowerShell", "rm.cmd docs/a.md", "B"),
    ("F15-12", "pm", "Bash", "git push origin main", "A"),
    # False-block guards: the PM's interpreter variables, loop items, expressions
    (
        "FB-1",
        "worker",
        "PowerShell",
        '$R="D:/p"; $PY="$R/.venv/Scripts/python.exe"; & $PY -B "$R/tools/agent-workflow/test_slot.py" -- $PY -m pytest -q x',
        "A",
    ),
    ("FB-2", "worker", "PowerShell", '$PY="D:/x/.venv/Scripts/python.exe"; & $PY -B x.py', "A"),
    ("FB-3", "worker", "PowerShell", '& "$R/.venv/Scripts/python.exe" -B x.py', "A"),
    ("FB-4", "worker", "PowerShell", "1..3 | ForEach-Object { $_ }", "A"),
    ("FB-5", "worker", "PowerShell", "Get-ChildItem . | ForEach-Object { $_.Name }", "A"),
    ("FB-6", "worker", "PowerShell", "$h = @{a=1}; $h.a", "A"),
    ("FB-7", "worker", "PowerShell", "Write-Output ('a'+'b')", "A"),
    ("FB-8", "worker", "PowerShell", "$out = git status --short; $out", "A"),
    ("FB-9", "worker", "Bash", "PY=/d/x/python.exe; $PY -B x.py", "A"),
    ("FB-10", "worker", "Bash", "PY=/d/x/python.exe; $PY -m pytest tests", "B"),
    ("FB-11", "worker", "Bash", "x=pytest; $x tests", "B"),
    ("FB-12", "worker", "PowerShell", "& (gcm pytest) tests", "B"),
    ("FB-13", "worker", "Bash", "`echo pytest` tests", "B"),
    ("FB-14", "worker", "PowerShell", "& $PY -m pytest", "B"),
]


@pytest.mark.parametrize(
    ("case", "mode", "tool", "command", "expect"), FR1_CASES, ids=[c[0] for c in FR1_CASES]
)
def test_fix_round_1_probes(
    guard_repo: Path, case: str, mode: str, tool: str, command: str, expect: str
) -> None:
    check(guard_repo, case, tool, command, expect, mode=mode)


# Fix round 2: the round-2 reviewers' exact inputs (tasks/R3_GUARD_HOOKS_REVIEW.md N1-N9, Gemini R2 P1), siblings
# and allowed controls. `README.md` is not tracked in the fixture; it is outside the allowlist like any root file.
FR2_CASES = [
    # N1, Gemini P1, N7: every launcher argument is scanned as inner text (alias, unknown option, `/` prefix)
    (
        "N1-1",
        "pm",
        "Bash",
        'powershell -NoProfile -ep Bypass -Command "Set-Content README.md x"',
        "B",
    ),
    ("N1-2", "pm", "PowerShell", 'powershell -ep Bypass -Command "sc README.md x"', "B"),
    ("N1-3", "pm", "Bash", 'pwsh -of Text -c "Set-Content README.md x"', "B"),
    ("N1-4", "pm", "Bash", 'pwsh -if Text -c "Set-Content README.md x"', "B"),
    ("N1-5", "worker", "Bash", 'powershell -NoProfile -ep Bypass -Command "Get-Date"', "A"),
    (
        "N1-6",
        "worker",
        "Bash",
        'powershell -ep Bypass -Command "Set-Content .tmp/scratch/x.txt y"',
        "A",
    ),
    (
        "N1-7",
        "pm",
        "Bash",
        'start pwsh "-c Set-Content README.md x"',
        "B",
    ),  # option and text in one word
    ("GP1-1", "pm", "Bash", 'powershell /c "Remove-Item tasks/STATUS.md"', "B"),
    ("GP1-2", "pm", "Bash", "powershell.exe /c \"sc tasks/STATUS.md 'corrupt'\"", "B"),
    ("GP1-3", "pm", "Bash", 'powershell //c "Remove-Item tasks/STATUS.md"', "B"),
    ("GP1-4", "pm", "Bash", 'pwsh /c "Remove-Item tasks/STATUS.md"', "B"),
    ("GP1-5", "pm", "PowerShell", 'pwsh /command "Remove-Item tasks/STATUS.md"', "B"),
    ("GP1-6", "pm", "Bash", 'pwsh -Command:"Remove-Item tasks/STATUS.md"', "B"),
    ("N7-1", "pm", "Bash", 'bash -c -- "rm README.md"', "B"),
    ("N7-2", "pm", "Bash", 'ksh -c "rm README.md"', "B"),
    ("N7-3", "pm", "Bash", 'busybox sh -c "rm README.md"', "B"),
    ("N7-4", "pm", "Bash", "source <(echo rm README.md)", "B"),
    ("N7-5", "pm", "Bash", "parallel rm ::: README.md", "B"),
    ("N7-6", "pm", "Bash", 'powershell -x y -c "Set-Content README.md x"', "B"),
    ("N7-7", "pm", "Bash", "sudo -u root rm README.md", "B"),
    ("N7-8", "pm", "Bash", "env -u X rm README.md", "B"),
    ("N7-9", "pm", "Bash", "ls | parallel rm", "B"),
    ("N7-10", "worker", "Bash", 'bash -c -- "rm .tmp/scratch/x.log"', "A"),
    ("N7-11", "worker", "Bash", "env -u X python -B x.py", "A"),
    ("N7-12", "worker", "Bash", "bash -lc 'git status --short'", "B"),  # round 2: git through a launcher
    # N2: a bound value's every word is the command line
    ("N2-1", "pm", "Bash", 'c="rm README.md"; $c', "B"),
    ("N2-2", "worker", "Bash", 'c="git commit -m x"; $c', "B"),
    ("N2-3", "pm", "Bash", 'c="git push -f"; $c', "B"),
    ("N2-4", "pm", "Bash", 'c="rm README.md"; bash -c "$c"', "B"),
    ("N2-5", "worker", "Bash", 'c="rm .tmp/scratch/x.log"; $c', "A"),
    ("N2-6", "worker", "Bash", 'c="git status --short"; $c', "A"),
    ("N2-7", "pm", "Bash", 'c="git push origin main"; $c', "B"),  # round 2: a write needs a literal `git`
    # N3: a closer before the call operator
    ("N3-1", "pm", "PowerShell", "$n='Set-Content'; if(1){1}&$n README.md x", "B"),
    ("N3-2", "pm", "PowerShell", "$n='Set-Content'; (1)&$n README.md x", "B"),
    ("N3-3", "worker", "PowerShell", "$n='Set-Content'; if(1){1}&$n .tmp/scratch/x.txt y", "A"),
    # N4: escaped backticks nested in a backtick substitution
    ("N4-1", "pm", "Bash", "echo `echo \\`rm README.md\\``", "B"),
    ("N4-2", "pm", "Bash", 'echo "`echo \\`rm README.md\\``"', "B"),
    ("N4-3", "worker", "Bash", "echo `echo \\`rm .tmp/scratch/x.log\\``", "A"),
    ("N4-4", "pm", "Bash", "cat <<EOF\n`echo \\`rm README.md\\``\nEOF", "B"),
    # N5: a computed command name with a git write word (worker)
    ("N5-1", "worker", "Bash", "$(echo g)it commit -m x", "B"),
    ("N5-2", "worker", "Bash", "g=g; ${g}it commit -m x", "B"),
    ("N5-3", "worker", "Bash", "g=g; i=it; $g$i commit -m x", "B"),
    ("N5-4", "worker", "PowerShell", "& (Get-Command g*t) commit -m x", "B"),
    ("N5-5", "pm", "Bash", "$(echo g)it push -f", "B"),
    ("N5-6", "worker", "Bash", "$(echo g)it status", "A"),
    # N6: cmd delayed expansion
    ("N6-1", "pm", "Bash", 'cmd /v:on /c "set c=del&!c! README.md"', "B"),
    ("N6-2", "worker", "Bash", 'cmd /v:on /c "echo !x!"', "A"),
]


@pytest.mark.parametrize(
    ("case", "mode", "tool", "command", "expect"), FR2_CASES, ids=[c[0] for c in FR2_CASES]
)
def test_fix_round_2_probes(
    guard_repo: Path, case: str, mode: str, tool: str, command: str, expect: str
) -> None:
    check(guard_repo, case, tool, command, expect, mode=mode)


# Launchers and every option spelling of the launcher tables, plus aliases, unknown options and other prefixes.
LAUNCH_PREFIXES = [
    *sorted(pg.SHELLS),
    *("powershell.exe", "bash.exe", "C:/Windows/System32/cmd.exe", "busybox sh", "env bash"),
    *("env -u X pwsh", "sudo -u root sh", "nohup dash"),
]
LAUNCH_OPTIONS = sorted(
    {
        *pg.SHELL_C_OPTS,
        *pg.PS_COMMAND_NAMES,
        *pg.PS_COMMAND_ALIASES,
        *pg.PS_FILE_NAMES,
        *(f"{o} x" for o in pg.PS_EXE_VALUE_OPTS),
        *("-ep x", "-of x", "-if x", "-x y", "/c", "/command", "//c", "-co", "-NoProfile", "-noni"),
        *("--", "-l", "-i", "/v:on /c", "-c --", "-s", "--norc", "-NoLogo -NonInteractive -c"),
    }
)


@given(
    launcher=s.sampled_from(LAUNCH_PREFIXES),
    option=s.sampled_from(LAUNCH_OPTIONS),
    prefix=s.sampled_from(["", "-", "--", "/", "//"]),
    case=s.sampled_from([str, str.upper, str.lower]),
    glued=s.booleans(),
)
def test_a_write_after_any_launcher_option_is_blocked(
    guard_repo: Path, launcher: str, option: str, prefix: str, case: Any, glued: bool
) -> None:
    # Fail-closed launcher rule: no option spelling hides the inner command.
    kind = pg._shell_kind(launcher.split()[-1])
    write = "del README.md" if kind == "cmd" else "rm README.md"
    first, _, rest = option.partition(" ")
    first = case(prefix + first.lstrip("-/") if prefix and first[:1] in "-/" else first)
    spelled = " ".join(p for p in (first, rest) if p)
    # `-Command:"..."`, `--opt=...`: a value glued to a named option.
    tail = (
        f':"{write}"' if glued and not rest and first.strip("-/")[:1].isalpha() else f' "{write}"'
    )
    command = f"{launcher} {spelled}{tail}"
    got, messages = verdict(guard_repo, command, mode="pm")
    assert got == "B", (command, messages)


def test_a_launcher_with_too_many_arguments_fails_closed_only_under_a_trigger(
    guard_repo: Path,
) -> None:
    filler = " ".join(["-x"] * pg.MAX_LAUNCHER_ARGS)
    assert verdict(guard_repo, f'bash {filler} -c "echo hi"', mode="worker")[0] == "A"
    got, messages = verdict(guard_repo, f'bash {filler} -c "rm .tmp/scratch/x.log"', mode="worker")
    assert got == "B" and any("too deeply" in m for m in messages), messages


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended-length path prefixes")
def test_an_extended_length_path_is_checked_like_the_plain_path(guard_repo: Path) -> None:
    # N9: the `?` of `\\?\` and `//?/` is no wildcard; the path is checked, never a crash that blocks.
    for prefix, tool, sep, root in (
        ("\\\\?\\", "PowerShell", "\\", str(guard_repo)),
        ("//?/", "Bash", "/", guard_repo.as_posix()),
    ):
        base = prefix + root + sep
        got, messages = verdict(guard_repo, f"echo d > {base}.tmp{sep}x.txt", tool, mode="worker")
        assert got == "A", messages
        command = f"echo d > {base}tasks{sep}STATUS.md"
        got, messages = verdict(guard_repo, command, tool, mode="worker")
        assert got == "B" and "outside the shell-write allowlist" in " ".join(messages), messages


@pytest.mark.parametrize("quote", ["", '"', "'", '`"'])
def test_a_quoted_dotnet_method_still_names_its_target(guard_repo: Path, quote: str) -> None:
    # G1: the target is reported even where the opaque-code reason alone would also block.
    command = f"[IO.File]::{quote}Encrypt{quote}('src/fix.py')"
    got, messages = verdict(guard_repo, command, "PowerShell", mode="worker")
    assert got == "B" and any("Encrypt target `src/fix.py`" in m for m in messages), messages


def test_a_wildcard_in_an_allowed_dir_fails_closed_above_the_match_cap(
    guard_repo: Path,
) -> None:
    # Only a wildcard directly in `.tmp/` is checked match by match; a deeper one checks its literal directory.
    made = [guard_repo / ".tmp" / f"cap-{i}.log" for i in range(st.MAX_GLOB_MATCHES + 1)]
    try:
        for path in made[:-1]:
            path.write_text("x", encoding="utf-8")
        check(guard_repo, "cap-at", "Bash", "rm .tmp/cap-*.log", "A", mode="worker")
        made[-1].write_text("x", encoding="utf-8")
        got, messages = verdict(guard_repo, "rm .tmp/cap-*.log", mode="worker")
        assert got == "B" and "narrower" in " ".join(messages), messages
    finally:
        for path in made:
            path.unlink(missing_ok=True)


def test_sed_script_scanner() -> None:
    for script in ("w f", "1w f", "/a/W f", "s/a/b/w f", "s/a/b/e", "1e ls", "s/a/b", "y/ab", "k"):
        assert st._sed_writes(script), script
    for script in (
        "p",
        "1,5p",
        "s/a/b/g",
        "/x/d",
        "1!G;h;$!d",
        "y/ab/cd/",
        "a\\text w f",
        ":l;n;bl",
    ):
        assert not st._sed_writes(script), script


WRITE_COMMANDS = [
    "rm docs/a.md",
    "touch src/new.py",
    "cp docs/a.md tasks/STATUS.md",
    "echo x > docs/a.md",
]
WRITE_VERBS_ONLY = ["rm", "touch", "sed -i s/a/b/", "chmod 644", "truncate -s 0", "mv -t .tmp"]
BASH_WRAPPERS = [
    "echo `{w}`",
    "x=$({w})",
    "bash -c '{w}'",
    "bash -xc '{w}'",
    'sh -c "{w}"',
    "eval '{w}'",
    'cmd //c "{w}"',
    "echo ok && {w}",
    "( {w} )",
    "{{ {w}; }}",
]
PLACEHOLDER_WRAPPERS = [
    "find docs -name a.md -exec {v} {{}} \\;",
    "find docs -name a.md -exec {v} {{}} +",
    "ls docs | xargs -I{{}} {v} {{}}",
    "ls docs | xargs -I {{}} sh -c '{v} {{}}'",
    "ls docs | xargs {v}",
]
PS_WRITES = ["Set-Content docs/a.md x", "Remove-Item docs/a.md", "sc docs/a.md x", "ni src/new.py"]
PS_WRAPPERS = [
    'powershell -NoProfile "{w}"',
    'powershell -Command "{w}"',
    "iex '{w}'",
    "Invoke-Expression '{w}'",
    "& {{ {w} }}",
]


@given(write=s.sampled_from(WRITE_COMMANDS), wrapper=s.sampled_from(BASH_WRAPPERS))
def test_a_write_inside_any_bash_construct_is_blocked(
    guard_repo: Path, write: str, wrapper: str
) -> None:
    assert verdict(guard_repo, wrapper.format(w=write), mode="worker")[0] == "B"


@given(verb=s.sampled_from(WRITE_VERBS_ONLY), wrapper=s.sampled_from(PLACEHOLDER_WRAPPERS))
def test_a_placeholder_write_is_blocked(guard_repo: Path, verb: str, wrapper: str) -> None:
    assert verdict(guard_repo, wrapper.format(v=verb), mode="worker")[0] == "B"


@given(op=s.sampled_from([">", ">>", ">|", "&>", "&>>", ">&", "<>", "1<>", "2>", "3>"]))
def test_every_writing_redirection_is_a_target(guard_repo: Path, op: str) -> None:
    assert verdict(guard_repo, f"echo x {op} docs/a.md", mode="worker")[0] == "B"
    assert verdict(guard_repo, f"echo x {op} .tmp/scratch/x.log", mode="worker")[0] == "A"


@given(write=s.sampled_from(PS_WRITES), wrapper=s.sampled_from(PS_WRAPPERS))
def test_a_write_inside_any_powershell_construct_is_blocked(
    guard_repo: Path, write: str, wrapper: str
) -> None:
    assert verdict(guard_repo, wrapper.format(w=write), "PowerShell", mode="worker")[0] == "B"


@given(
    verb=s.sampled_from(["Set-Content", "Remove-Item", "Out-File", "Add-Content"]),
    split=s.integers(min_value=1, max_value=6),
    form=s.sampled_from(
        [
            "& ('{a}'+'{b}') docs/a.md x",
            "$c='{a}'+'{b}'; & $c docs/a.md x",
            "iex ('{a}'+'{b} docs/a.md x')",
            "sv c '{a}{b}'; & $c docs/a.md x",
            "Set-Alias z '{a}{b}'; z docs/a.md x",
        ]
    ),
)
def test_a_computed_powershell_writer_is_blocked(
    guard_repo: Path, verb: str, split: int, form: str
) -> None:
    command = form.format(a=verb[:split], b=verb[split:])
    assert verdict(guard_repo, command, "PowerShell", mode="worker")[0] == "B"
