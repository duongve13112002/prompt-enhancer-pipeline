"""gemini-ws.ps1 (T-024 item 3): an Antigravity review workspace from a commit, a working tree or a saved patch.

Each test copies gemini-ws.ps1 and scripts/agy-runner/agy-lib.ps1 into a temporary project root and builds the workspace
from a temporary Git repository (never the project repository), with -ReadOnlyJob so no scheduled task is queried.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[1]
POWERSHELL = shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell 5.1 tooling"
)
UNICODE_NAME = "src/näme_漢.py"  # a non-ASCII path must survive PowerShell 5.1 and git output


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "root"
    for rel in ("tools/agent-workflow/gemini-ws.ps1", "scripts/agy-runner/agy-lib.ps1"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, root / rel)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t"), ("core.autocrlf", "false")):
        git(repo, "config", key, value)
    write(repo / ".gitignore", "ignored.txt\n.secrets/\n")
    write(repo / "src" / "a.py", "a = 1\n")
    write(repo / "tasks" / "T-9.md", "# T-9\n")
    write(repo / "README.md", "readme\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return root, repo, git(repo, "rev-parse", "HEAD").strip()


def build(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    script = root / "tools" / "agent-workflow" / "gemini-ws.ps1"
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args, "-ReadOnlyJob"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def test_working_tree_workspace_holds_the_uncommitted_patch(world: tuple[Path, Path, str]) -> None:
    root, repo, base = world
    write(repo / "src" / "a.py", "a = 2\n")
    write(repo / "src" / "new.py", "new = True\n")
    write(repo / UNICODE_NAME, "x = 1\n")
    write(repo / "src" / "ignored.txt", "ignored\n")
    write(repo / ".secrets" / "key.txt", "secret\n")
    write(repo / "notes.txt", "a root file outside the allowlist\n")
    status_before = git(repo, "status", "--porcelain")
    index_before = (repo / ".git" / "index").read_bytes()
    done = build(root, "-Worktree", str(repo), "-Base", base, "-WorkingTree", "-TaskFile", "tasks/T-9.md", "-Workspace", "wt")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "wt"
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "a = 2\n"
    assert (ws / "src" / "new.py").is_file() and (ws / UNICODE_NAME).is_file()
    assert (ws / "README.md").is_file()
    assert not (ws / "src" / "ignored.txt").exists() and not (ws / ".secrets").exists()
    assert not (ws / "notes.txt").exists()
    delta = (ws / "notes" / "delta.diff").read_text(encoding="utf-8")
    assert "+a = 2" in delta and "b/src/new.py" in delta and "näme_漢.py" in delta
    assert "secret" not in delta and "ignored" not in delta
    assert (ws / "notes" / "T-9.md").read_text(encoding="utf-8").startswith("# T-9")
    # The worktree's index and status are untouched (the temporary index is removed).
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert git(repo, "status", "--porcelain") == status_before
    assert not (root / ".tmp" / "agy-work" / "wt.index").exists()


def test_patch_workspace_applies_the_saved_patch_to_base(world: tuple[Path, Path, str], tmp_path: Path) -> None:
    root, repo, base = world
    write(repo / "src" / "a.py", "a = 3\n")
    write(repo / ".agents" / "rule.md", "outside the review directories\n")
    git(repo, "add", "-A")
    patch = tmp_path / "T-9.patch"
    patch.write_text(git(repo, "diff", "--cached", "--binary", base), encoding="utf-8", newline="\n")
    git(repo, "reset", "-q", "--hard", base)  # temporary repository only
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "pt")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "pt"
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "a = 3\n"
    # One filter for every output (review F5): the hunk outside the allowlist is neither applied nor in the notes.
    assert not (ws / ".agents").exists()
    delta = (ws / "notes" / "delta.diff").read_text(encoding="utf-8")
    assert "+a = 3" in delta and "diff --git a/src/a.py b/src/a.py" in delta
    assert ".agents" not in delta and "outside the review directories" not in delta
    stat = (ws / "notes" / "stat.txt").read_text(encoding="utf-8")
    assert "src/a.py" in stat and ".agents" not in stat
    assert not (root / ".tmp" / "agy-work" / "pt.delta").exists()  # scratch removed


def workspace_text(ws: Path) -> str:
    """Every file of the workspace, concatenated (binary-safe), to prove excluded content appears nowhere."""
    return "".join(p.read_bytes().decode("utf-8", "replace") for p in sorted(ws.rglob("*")) if p.is_file())


def test_working_tree_exclusions_reach_no_workspace_file(world: tuple[Path, Path, str]) -> None:
    """Review F5: a nested `.tmp` file and a root file outside the allowlist were absent from the copy but present
    in delta.diff. Near miss: an allowlisted file with a space and a `tmp` name (no dot) is reviewed."""
    root, repo, base = world
    write(repo / "src" / ".tmp" / "probe.txt", "NESTED_FORBIDDEN\n")
    write(repo / "notes.txt", "OUTSIDE_ALLOWLIST\n")
    write(repo / "src" / "tmp" / "a b.py", "NEAR_MISS = 1\n")
    done = build(root, "-Worktree", str(repo), "-Base", base, "-WorkingTree", "-TaskFile", "tasks/T-9.md", "-Workspace", "ex")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "ex"
    text = workspace_text(ws)
    assert "NESTED_FORBIDDEN" not in text and "OUTSIDE_ALLOWLIST" not in text
    assert "notes.txt" not in text and "src/.tmp" not in text
    assert (ws / "src" / "tmp" / "a b.py").is_file() and "NEAR_MISS" in (ws / "notes" / "delta.diff").read_text(encoding="utf-8")


def test_head_workspace_applies_the_same_filter(world: tuple[Path, Path, str]) -> None:
    root, repo, base = world
    write(repo / "src" / ".tmp" / "probe.txt", "NESTED_FORBIDDEN\n")
    write(repo / "notes.txt", "OUTSIDE_ALLOWLIST\n")
    write(repo / "src" / "b.py", "b = 1\n")
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "head")
    head = git(repo, "rev-parse", "HEAD").strip()
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Head", head, "-TaskFile", "tasks/T-9.md", "-Workspace", "hf")
    assert done.returncode == 0, done.stderr
    text = workspace_text(root / ".tmp" / "agy-work" / "hf")
    assert "NESTED_FORBIDDEN" not in text and "OUTSIDE_ALLOWLIST" not in text and "b = 1" in text


@pytest.mark.parametrize("rel", ["src/.tmp/a b.txt", ".secrets/a b.txt", "models/a b.gguf"])
def test_patch_path_with_a_space_is_read_structurally(world: tuple[Path, Path, str], tmp_path: Path, rel: str) -> None:
    """Review F6: header text split at the space, so `src/.tmp/a b.txt` was applied (rc 0). Paths now come from
    `git apply --numstat -z`; the refusal happens before the workspace exists."""
    root, repo, base = world
    write(repo / rel, "NESTED_FORBIDDEN\n")
    git(repo, "add", "-f", "--", rel)
    patch = tmp_path / "space.patch"
    patch.write_text(git(repo, "diff", "--cached", "--binary", base), encoding="utf-8", newline="\n")
    git(repo, "reset", "-q", "--hard", base)  # temporary repository only
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "sp")
    assert done.returncode != 0 and "forbidden path" in done.stderr, done.stderr
    assert not (root / ".tmp" / "agy-work" / "sp").exists()


def test_patch_path_with_a_space_outside_forbidden_dirs_applies(world: tuple[Path, Path, str], tmp_path: Path) -> None:
    root, repo, base = world
    write(repo / "src" / "a b.txt", "SPACED\n")
    git(repo, "add", "-A")
    patch = tmp_path / "ok.patch"
    patch.write_text(git(repo, "diff", "--cached", "--binary", base), encoding="utf-8", newline="\n")
    git(repo, "reset", "-q", "--hard", base)  # temporary repository only
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "ok")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "ok"
    assert (ws / "src" / "a b.txt").read_text(encoding="utf-8") == "SPACED\n"
    assert "+SPACED" in (ws / "notes" / "delta.diff").read_text(encoding="utf-8")


def saved_patch(repo: Path, base: str, path: Path) -> Path:
    """The staged change of the temporary repository as a saved patch (renames detected), then back to Base."""
    path.write_text(git(repo, "diff", "--cached", "--binary", "-M", base), encoding="utf-8", newline="\n")
    git(repo, "reset", "-q", "--hard", base)  # temporary repository only
    return path


def test_rename_out_of_the_review_directories_shows_the_deletion(
    world: tuple[Path, Path, str], tmp_path: Path
) -> None:
    """Review R7: `src/a.py -> outside.txt` reported ready with an empty diff and kept `src/a.py`."""
    root, repo, base = world
    git(repo, "mv", "src/a.py", "outside.txt")
    patch = saved_patch(repo, base, tmp_path / "out.patch")
    assert "rename from src/a.py" in patch.read_text(encoding="utf-8")
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "ro")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "ro"
    assert not (ws / "src" / "a.py").exists() and not (ws / "outside.txt").exists()
    delta = (ws / "notes" / "delta.diff").read_text(encoding="utf-8")
    assert "deleted file mode" in delta and "-a = 1" in delta and "outside.txt" not in delta
    assert "src/a.py" in (ws / "notes" / "stat.txt").read_text(encoding="utf-8")
    assert not (root / ".tmp" / "agy-work" / "ro.patch-index").exists()


def test_rename_inside_the_review_directories_shows_both_sides(world: tuple[Path, Path, str], tmp_path: Path) -> None:
    root, repo, base = world
    git(repo, "mv", "src/a.py", "src/renamed.py")
    patch = saved_patch(repo, base, tmp_path / "in.patch")
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "ri")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "ri"
    assert not (ws / "src" / "a.py").exists()
    assert (ws / "src" / "renamed.py").read_text(encoding="utf-8") == "a = 1\n"
    delta = (ws / "notes" / "delta.diff").read_text(encoding="utf-8")
    assert "deleted file mode" in delta and "new file mode" in delta and "src/renamed.py" in delta


@pytest.mark.parametrize("mode", ["Head", "WorkingTree", "Patch"])
def test_a_task_file_missing_from_the_source_is_refused(
    world: tuple[Path, Path, str], tmp_path: Path, mode: str
) -> None:
    """Item 9: `-Head` with a task file absent from the commit wrote an empty note and reported ready."""
    root, repo, base = world
    source = {"Head": ["-Head", base], "WorkingTree": ["-WorkingTree"]}
    if mode == "Patch":
        write(repo / "src" / "a.py", "a = 5\n")
        git(repo, "add", "-A")
        source["Patch"] = ["-Patch", str(saved_patch(repo, base, tmp_path / "p.patch"))]
    done = build(root, "-Worktree", str(repo), "-Base", base, *source[mode], "-TaskFile", "tasks/absent.md", "-Workspace", "nt")
    assert done.returncode != 0 and "absent.md" in done.stderr, done.stderr
    assert not (root / ".tmp" / "agy-work" / "nt").exists()
    near = build(root, "-Worktree", str(repo), "-Base", base, *source[mode], "-TaskFile", "tasks/T-9.md", "-Workspace", "nt2")
    assert near.returncode == 0, near.stderr  # near miss: the task file exists


def test_a_file_below_a_junction_is_refused(world: tuple[Path, Path, str], tmp_path: Path) -> None:
    """A tracked file whose directory was replaced by a junction to an outside folder: its content comes from
    outside the worktree, so the build stops (kills the `Assert-NoLink` reparse-point mutant)."""
    import _winapi  # Windows only, like the module

    root, repo, base = world
    write(repo / "src" / "d" / "f.py", "inside\n")
    git(repo, "add", "-A")
    outside = tmp_path / "outside"
    write(outside / "f.py", "OUTSIDE_CONTENT\n")
    (repo / "src" / "d" / "f.py").unlink()
    (repo / "src" / "d").rmdir()
    junction = repo / "src" / "d"
    try:
        _winapi.CreateJunction(str(outside), str(junction))
    except OSError as exc:
        pytest.skip(f"cannot create a junction here: {exc}")
    try:
        done = build(root, "-Worktree", str(repo), "-Base", base, "-WorkingTree", "-TaskFile", "tasks/T-9.md", "-Workspace", "jn")
        assert done.returncode != 0 and "linked path" in done.stderr, done.stderr
        ws = root / ".tmp" / "agy-work" / "jn"
        assert not ws.exists() or "OUTSIDE_CONTENT" not in workspace_text(ws)
    finally:
        os.rmdir(junction)  # removes the junction itself, never the folder it points to


@pytest.mark.parametrize("rel", [".secrets/key.txt", "models/m.gguf", "src/.git/config", ".tmp/x.txt"])
def test_patch_touching_a_forbidden_path_is_refused(
    world: tuple[Path, Path, str], tmp_path: Path, rel: str
) -> None:
    root, repo, base = world
    patch = tmp_path / "bad.patch"
    patch.write_text(
        f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1 @@\n+x\n",
        encoding="utf-8",
        newline="\n",
    )
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "bad")
    assert done.returncode != 0 and "forbidden path" in done.stderr
    assert not (root / ".tmp" / "agy-work" / "bad").exists()  # refused before anything is created


def test_patch_that_does_not_apply_is_refused(world: tuple[Path, Path, str], tmp_path: Path) -> None:
    root, repo, base = world
    patch = tmp_path / "stale.patch"
    patch.write_text(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a = 99\n+a = 100\n",
        encoding="utf-8",
        newline="\n",
    )
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "st")
    assert done.returncode != 0 and "does not apply" in done.stderr


def test_head_workspace_still_works(world: tuple[Path, Path, str]) -> None:
    root, repo, base = world
    write(repo / "src" / "a.py", "a = 4\n")
    git(repo, "commit", "-q", "-am", "head")
    head = git(repo, "rev-parse", "HEAD").strip()
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Head", head, "-TaskFile", "tasks/T-9.md", "-Workspace", "hd")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "hd"
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "a = 4\n"
    assert "+a = 4" in (ws / "notes" / "delta.diff").read_text(encoding="utf-8")
    assert not (ws / "notes" / "head.tar").exists()


def start(root: Path, *args: str) -> subprocess.Popen[str]:
    assert POWERSHELL is not None
    script = root / "tools" / "agent-workflow" / "gemini-ws.ps1"
    return subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args, "-ReadOnlyJob"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def snapshot(ws: Path) -> dict[str, bytes]:
    return {p.relative_to(ws).as_posix(): p.read_bytes() for p in sorted(ws.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("mode", ["Head", "WorkingTree", "Patch"])
def test_concurrent_builds_of_one_workspace_leave_exactly_one_intact_winner(
    world: tuple[Path, Path, str], tmp_path: Path, mode: str
) -> None:
    """T-024 final F26: the check `Workspace exists` reserved nothing, both builds wrote into one folder and shared
    one patch index, and a loser's cleanup removed the winner's index. Now each build works in a private staging
    folder and publishes it with one atomic move: exactly one wins, the others fail with an ownership error and
    the winner's workspace is complete and byte-identical to a solo build."""
    root, repo, base = world
    write(repo / "src" / "a.py", "a = 7\n")
    write(repo / "src" / "n.py", "n = 1\n")
    git(repo, "add", "-A")
    source = {"Head": [], "WorkingTree": ["-WorkingTree"], "Patch": []}
    if mode == "Head":
        git(repo, "commit", "-q", "-m", "head")
        source["Head"] = ["-Head", git(repo, "rev-parse", "HEAD").strip()]
    elif mode == "Patch":
        source["Patch"] = ["-Patch", str(saved_patch(repo, base, tmp_path / "c.patch"))]
    common = ["-Worktree", str(repo), "-Base", base, *source[mode], "-TaskFile", "tasks/T-9.md"]
    solo = build(root, *common, "-Workspace", "solo")
    assert solo.returncode == 0, solo.stderr
    expected = snapshot(root / ".tmp" / "agy-work" / "solo")
    jobs = [start(root, *common, "-Workspace", "race") for _ in range(3)]
    results = [(job.communicate(timeout=180), job.returncode) for job in jobs]
    winners = [r for r in results if r[1] == 0]
    losers = [r for r in results if r[1] != 0]
    assert len(winners) == 1 and len(losers) == 2, [(r[1], r[0][1]) for r in results]
    for (_, err), _code in losers:
        assert "Workspace exists" in err, err
    assert snapshot(root / ".tmp" / "agy-work" / "race") == expected
    # Nothing of any build is left next to the workspace: no staging folder, no private index.
    assert sorted(p.name for p in (root / ".tmp" / "agy-work").iterdir()) == ["race", "solo"]


def test_a_failed_build_leaves_no_usable_partial_workspace(world: tuple[Path, Path, str]) -> None:
    """T-024 final F26: a failure after the files were copied (here `-Sanitize` with an interpreter that cannot run
    agy_sanitize.py) left a populated workspace that looked ready. Now the build fails and creates no workspace."""
    root, repo, base = world
    done = build(
        root, "-Worktree", str(repo), "-Base", base, "-Head", base, "-TaskFile", "tasks/T-9.md",
        "-Workspace", "bad", "-Sanitize", "-Python", sys.executable,
    )  # fmt: skip
    assert done.returncode != 0 and "agy_sanitize.py failed" in done.stderr, done.stderr
    assert "can't open file" in done.stderr, done.stderr  # the sanitizer's own stderr is in the message
    assert list((root / ".tmp" / "agy-work").iterdir()) == []
    near = build(root, "-Worktree", str(repo), "-Base", base, "-Head", base, "-TaskFile", "tasks/T-9.md", "-Workspace", "bad")
    assert near.returncode == 0, near.stderr  # the name is free again after the failure


def test_a_failed_second_build_removes_nothing_of_an_existing_workspace(world: tuple[Path, Path, str]) -> None:
    root, repo, base = world
    args = ("-Worktree", str(repo), "-Base", base, "-Head", base, "-TaskFile", "tasks/T-9.md", "-Workspace", "keep")
    assert build(root, *args).returncode == 0
    ws = root / ".tmp" / "agy-work" / "keep"
    before = snapshot(ws)
    again = build(root, *args)
    assert again.returncode != 0 and "Workspace exists" in again.stderr
    assert snapshot(ws) == before
    assert [p.name for p in (root / ".tmp" / "agy-work").iterdir()] == ["keep"]


@pytest.mark.parametrize("name", ["..", "../x", "a/b", "a\\b", "", ".hidden", "_results", "a b", "a.", "a ", "a\n"])
def test_a_workspace_name_that_is_not_one_plain_folder_name_is_refused(
    world: tuple[Path, Path, str], name: str
) -> None:
    root, repo, base = world
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Head", base, "-TaskFile", "tasks/T-9.md", "-Workspace", name)
    assert done.returncode != 0
    assert not (root / ".tmp" / "agy-work").exists() or list((root / ".tmp" / "agy-work").iterdir()) == []
    assert not (root / ".tmp" / "x").exists()


def build_hosted(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run gemini-ws.ps1 inside an embedded PowerShell runspace (what an agent harness or `Start-Job` does).
    There is no console stderr there, so Windows PowerShell 5.1 turns every line a native command writes to stderr
    into an error record, which `$ErrorActionPreference = 'Stop'` makes terminating (experiment
    `.tmp/T-024/final/exp_stderr.py`; the `-File` run with pipes does not show it)."""
    assert POWERSHELL is not None
    script = root / "tools" / "agent-workflow" / "gemini-ws.ps1"

    def quote(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    calls = ""
    tokens = [*args, "-ReadOnlyJob"]
    k = 0
    while k < len(tokens):
        name = tokens[k].lstrip("-")
        if k + 1 < len(tokens) and not tokens[k + 1].startswith("-"):
            calls += f".AddParameter({quote(name)}, {quote(tokens[k + 1])})"
            k += 2
        else:
            calls += f".AddParameter({quote(name)})"
            k += 1
    driver = (
        "$ps = [powershell]::Create(); "
        f"[void]$ps.AddCommand({quote(str(script))}){calls}; "
        "try { $out = $ps.Invoke() } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }; "
        "if ($ps.HadErrors) { foreach ($e in $ps.Streams.Error) { [Console]::Error.WriteLine($e.ToString()) }; exit 1 }; "
        "$out | ForEach-Object { $_ }"
    )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", driver],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def test_patch_mode_reads_the_task_file_from_base_plus_the_patch_else_from_the_worktree(
    world: tuple[Path, Path, str], tmp_path: Path
) -> None:
    """The tolerated `cat-file -e` failure decides the source: a task file the patch changes comes from the patched
    index, one that Base lacks comes from the worktree (kills the inverted exit-code read)."""
    root, repo, base = world
    write(repo / "tasks" / "T-9.md", "# T-9 from the patch\n")
    write(repo / "src" / "a.py", "a = 8\n")
    git(repo, "add", "-A")
    patch = saved_patch(repo, base, tmp_path / "t.patch")
    write(repo / "tasks" / "T-10.md", "# T-10 only in the worktree\n")
    from_index = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-9.md", "-Workspace", "fi")
    assert from_index.returncode == 0, from_index.stderr
    assert (root / ".tmp" / "agy-work" / "fi" / "notes" / "T-9.md").read_text(encoding="utf-8") == "# T-9 from the patch"
    from_tree = build(root, "-Worktree", str(repo), "-Base", base, "-Patch", str(patch), "-TaskFile", "tasks/T-10.md", "-Workspace", "ft")
    assert from_tree.returncode == 0, from_tree.stderr
    assert (root / ".tmp" / "agy-work" / "ft" / "notes" / "T-10.md").read_text(encoding="utf-8") == "# T-10 only in the worktree"


@pytest.mark.parametrize("host", ["console", "hosted"])
def test_a_git_warning_on_stderr_does_not_fail_a_working_tree_build(world: tuple[Path, Path, str], host: str) -> None:
    """Review round 1: with core.autocrlf=true and an LF file git warns on stderr ("LF will be replaced by CRLF");
    Windows PowerShell 5.1 turned that into a terminating error under $ErrorActionPreference='Stop' when the script
    ran in a host without a console stderr. Success of a native command is its exit code; stderr is diagnostics."""
    root, repo, base = world
    run = build if host == "console" else build_hosted
    git(repo, "config", "core.autocrlf", "true")
    git(repo, "config", "core.safecrlf", "warn")
    write(repo / "src" / "a.py", "a = 2\nb = 3\n")  # LF on disk, so git warns whenever it looks at the file
    write(repo / "src" / "new.py", "new = True\n")
    probe = subprocess.run(
        ["git", "-C", str(repo), "diff", base], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert "LF will be replaced by CRLF" in probe.stderr, probe.stderr  # the fixture really triggers the warning
    done = run(root, "-Worktree", str(repo), "-Base", base, "-WorkingTree", "-TaskFile", "tasks/T-9.md", "-Workspace", "crlf")
    assert done.returncode == 0, done.stderr
    ws = root / ".tmp" / "agy-work" / "crlf"
    assert "+b = 3" in (ws / "notes" / "delta.diff").read_text(encoding="utf-8")
    assert (ws / "src" / "new.py").is_file()
    assert [p.name for p in (root / ".tmp" / "agy-work").iterdir()] == ["crlf"]


@pytest.mark.parametrize("host", ["console", "hosted"])
def test_a_failing_git_call_fails_the_build_with_its_own_message(world: tuple[Path, Path, str], host: str) -> None:
    """The exit code still decides: a failing git call stops the build and the message carries git's stderr."""
    root, repo, base = world
    done = (build if host == "console" else build_hosted)(
        root, "-Worktree", str(repo), "-Base", "no-such-revision", "-WorkingTree", "-TaskFile", "tasks/T-9.md",
        "-Workspace", "gf",
    )  # fmt: skip
    assert done.returncode != 0
    # The command, its exit code and git's own stderr line are all in the message.
    assert "git" in done.stderr and "failed (exit 128)" in done.stderr, done.stderr
    assert "fatal:" in done.stderr and "no-such-revision" in done.stderr, done.stderr
    assert not (root / ".tmp" / "agy-work" / "gf").exists()
    assert list((root / ".tmp" / "agy-work").iterdir()) == []


# Reserved device names of the Windows file-name rules ("Naming Files, Paths, and Namespaces"): any case, with an
# extension, with a trailing dot or space, and the superscript-digit forms.
DEVICE_NAMES = [
    "CON", "con", "Prn", "AUX", "nul", "NUL.txt", "nul.tar.gz", "COM1", "com9", "COM5", "LPT1", "lpt9", "LPT4",
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "com².log",
    "COM1.txt", "con.", "CON ", "NUL  ", "COM1 .", "lpt1.", "Aux.",
]  # fmt: skip


@pytest.mark.parametrize("name", DEVICE_NAMES)
def test_a_windows_reserved_device_name_is_refused(world: tuple[Path, Path, str], name: str) -> None:
    root, repo, base = world
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Head", base, "-TaskFile", "tasks/T-9.md", "-Workspace", name)
    assert done.returncode != 0, done.stdout
    if name == name.rstrip(" ."):  # a trailing dot or space is refused by the plain-name rule instead
        assert "reserved Windows device name" in done.stderr, done.stderr
    assert not (root / ".tmp" / "agy-work").exists() or list((root / ".tmp" / "agy-work").iterdir()) == []


@pytest.mark.parametrize("name", ["CONSOLE", "COM0", "COM10", "LPT0", "con-1", "a.con", "nulx", "com", "x.nul", "COMX"])
def test_a_name_that_only_looks_like_a_device_name_is_built(world: tuple[Path, Path, str], name: str) -> None:
    """Near misses of the device-name rule: the reserved list is exact (COM0 and COM10 are not on the Microsoft page)."""
    root, repo, base = world
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Head", base, "-TaskFile", "tasks/T-9.md", "-Workspace", name)
    assert done.returncode == 0, done.stderr
    assert (root / ".tmp" / "agy-work" / name / "notes" / "T-9.md").is_file()


def test_two_sources_at_once_are_refused(world: tuple[Path, Path, str], tmp_path: Path) -> None:
    root, repo, base = world
    done = build(root, "-Worktree", str(repo), "-Base", base, "-Head", base, "-WorkingTree", "-TaskFile", "tasks/T-9.md", "-Workspace", "two")
    assert done.returncode != 0
    assert not (root / ".tmp" / "agy-work" / "two").exists()
