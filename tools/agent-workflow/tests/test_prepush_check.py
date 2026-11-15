"""prepush_check.py (T-024 item 5): every part clean, then the push marker; any failing part writes none.

Repositories are temporary (never the project repository); `refs/remotes/origin/main` is set by `update-ref`, so
`refs/remotes/origin/main..HEAD` is the range (round 4, R14). The two sub-check tools are labelled test doubles: tiny scripts at
`<tmp root>/tools/agent-workflow/{setup_project,decisions_applied}.py` that print a fixed text and exit with a fixed code.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import guard_config as gc
import pytest
from conftest import git

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepush_check as pc

CFG = gc.load()
MARKERS = CFG["push_guard"]["marker_dir"]


def stub(root: Path, script: str, out: str = "OK   all good", code: int = 0) -> None:
    """Test double of a project sub-check tool."""
    path = root / "tools" / "agent-workflow" / script
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"print({out!r})\nraise SystemExit({code})\n", encoding="utf-8")


def commit(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"add {rel}")


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    for script in ("setup_project.py", "decisions_applied.py"):
        stub(root, script)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "core.autocrlf", "false")
    commit(repo, "README.md", "base\n")
    # The configured destination `origin/main` as a remote-tracking ref of the scratch repository (no remote, no
    # network); round 4 (R14) scans from it, never from the branch's upstream.
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root, repo


def marker(root: Path, repo: Path) -> Path:
    return root / MARKERS / git(repo, "rev-parse", "HEAD").strip()


def test_clean_range_writes_the_marker(world: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, repo = world
    commit(repo, "src/app.py", "print('ok')\n")
    commit(repo, "docs/models.md", "notes about models\n")  # a docs file named models is not under models/
    assert pc.main([str(repo)], root=root) == 0
    assert marker(root, repo).is_file()
    out = capsys.readouterr().out
    assert "OK   destination" in out and "prepush marker written" in out


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        (".secrets/token.txt", "x\n"),
        (".Secrets/token.txt", "x\n"),  # Windows treats it as the same directory
        ("models/m.txt", "x\n"),
        ("database/kb.sqlite", "x\n"),
        ("data/run/out.json", "{}\n"),
        ("src/weights/model.GGUF", "x\n"),
        ("src/w.safetensors", "x\n"),
        ("src/w.onnx", "x\n"),
    ],
)
def test_forbidden_path_writes_no_marker(
    world: tuple[Path, Path], rel: str, text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root, repo = world
    commit(repo, rel, text)
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()
    assert f"FAIL paths" in capsys.readouterr().out


def test_a_file_added_and_removed_inside_the_range_is_still_found(world: tuple[Path, Path]) -> None:
    root, repo = world
    commit(repo, ".secrets/key.txt")
    git(repo, "rm", "-q", ".secrets/key.txt")
    git(repo, "commit", "-q", "-m", "remove it again")
    results = {part: clean for part, clean, _ in pc.run(repo, CFG, root)}
    assert results["paths"] is False


def test_similar_names_pass(world: tuple[Path, Path]) -> None:
    root, repo = world
    for rel in ("src/data_loader.py", "dataset/x.txt", "src/models/x.py", "tests/fixtures/bin.txt"):
        commit(repo, rel)
    results = {part: clean for part, clean, _ in pc.run(repo, CFG, root)}
    assert results["paths"] is True


def test_whitespace_error_writes_no_marker(world: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, repo = world
    commit(repo, "src/app.py", "x = 1   \n")
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()
    assert "FAIL whitespace" in capsys.readouterr().out


def test_no_destination_fails_and_names_it(world: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, repo = world
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert pc.main([str(repo)], root=root) == 1
    out = capsys.readouterr().out
    assert "FAIL destination" in out and "`refs/remotes/origin/main` does not exist" in out
    assert "user's decision" in out and not marker(root, repo).exists()


def test_the_range_starts_at_the_destination_not_the_upstream(
    world: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """R14: `main` tracks a local branch at its own tip, so `@{upstream}..HEAD` is empty; the push to origin/main
    would still publish `data/forbidden.txt`. Near miss: the same tracking set-up with a clean commit passes."""
    root, repo = world
    commit(repo, "src/ok.py")
    git(repo, "branch", "unrelated")
    git(repo, "branch", "-q", "--set-upstream-to=unrelated")
    assert pc.main([str(repo)], root=root) == 0  # near miss
    commit(repo, "data/forbidden.txt")
    git(repo, "branch", "-f", "unrelated", "HEAD")
    capsys.readouterr()
    assert pc.main([str(repo)], root=root) == 1
    out = capsys.readouterr().out
    assert "FAIL paths" in out and "data/forbidden.txt" in out and not marker(root, repo).exists()


def test_head_on_another_branch_fails(world: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """The one allowed push sends `refs/heads/main`; a check of another branch's commit marks nothing."""
    root, repo = world
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "src/side.py")
    assert pc.main([str(repo)], root=root) == 1
    assert "FAIL destination" in capsys.readouterr().out and not marker(root, repo).exists()


def test_override_destination(world: tuple[Path, Path]) -> None:
    """`git_guard.push_remote`/`push_branch` choose the range: `upstream/trunk` instead of `origin/main`."""
    root, repo = world
    cfg = copy.deepcopy(CFG)
    cfg["git_guard"].update(push_remote="upstream", push_branch="trunk")
    git(repo, "branch", "-m", "main", "trunk")
    results = {part: clean for part, clean, _ in pc.run(repo, cfg, root)}
    assert results["destination"] is False  # no refs/remotes/upstream/trunk yet
    git(repo, "update-ref", "refs/remotes/upstream/trunk", "HEAD")
    results = {part: clean for part, clean, _ in pc.run(repo, cfg, root)}
    assert results["destination"] is True and results["paths"] is True


@pytest.mark.parametrize(
    ("script", "out", "code", "part"),
    [
        ("setup_project.py", "FAIL links  dead link", 0, "setup"),
        ("setup_project.py", "summary: 1 fail", 1, "setup"),
        ("decisions_applied.py", "pending 1", 1, "decisions"),
    ],
)
def test_failing_subcheck_writes_no_marker(
    world: tuple[Path, Path],
    script: str,
    out: str,
    code: int,
    part: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, repo = world
    stub(root, script, out, code)
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()
    assert f"FAIL {part}" in capsys.readouterr().out


def test_setup_warnings_pass(world: tuple[Path, Path]) -> None:
    root, repo = world
    stub(root, "setup_project.py", "WARN state  .tmp/pm-state missing\nsummary: 16 ok, 4 warn, 0 fail", 0)
    assert pc.main([str(repo)], root=root) == 0


def test_head_moving_during_the_check_writes_no_marker(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = world
    real_run = pc.run

    def run_then_commit(*a, **k):  # type: ignore[no-untyped-def]
        out = real_run(*a, **k)
        commit(repo, "src/late.py")
        return out

    monkeypatch.setattr(pc, "run", run_then_commit)
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()


def test_a_commit_made_after_the_check_never_gets_the_marker(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F7: HEAD moves between the last check and the marker write (here: inside the marker writer's own HEAD
    read). The late commit adds a forbidden path, so a marker for it would vouch for an unchecked commit."""
    root, repo = world
    commit(repo, "src/app.py")
    checked = git(repo, "rev-parse", "HEAD").strip()
    real_head = pc.prepush_mark.head_id
    calls = 0

    def head_then_commit(*a, **k):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:  # the marker writer's read: a commit lands just before it
            commit(repo, "data/late.txt")
        return real_head(*a, **k)

    monkeypatch.setattr(pc.prepush_mark, "head_id", head_then_commit)
    assert pc.main([str(repo)], root=root) == 1
    late = git(repo, "rev-parse", "HEAD").strip()
    assert late != checked
    assert not (root / MARKERS / late).exists() and not (root / MARKERS / checked).exists()


def test_the_marker_names_the_checked_commit(world: tuple[Path, Path]) -> None:
    root, repo = world
    commit(repo, "src/app.py")
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert pc.prepush_mark.main([str(repo), "--expect", sha.upper()], root=root) == 0
    assert (root / MARKERS / sha).is_file()
    assert pc.prepush_mark.main([str(repo), "--expect", "0" * 40], root=root) == 1  # near miss: another commit
    assert not (root / MARKERS / ("0" * 40)).exists()


@pytest.mark.parametrize("error", ["TimeoutExpired", "OSError"])
def test_a_whitespace_check_that_cannot_run_fails(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: str
) -> None:
    root, repo = world
    commit(repo, "src/app.py")
    real = pc._git

    def broken_diff(repo_: Path, timeout: int, *args: str):  # type: ignore[no-untyped-def]
        if args[:2] == ("diff", "--check"):
            raise pc.CheckError(f"git diff: {error}")
        return real(repo_, timeout, *args)

    monkeypatch.setattr(pc, "_git", broken_diff)
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()
    assert f"FAIL whitespace git diff: {error}" in capsys.readouterr().out


def test_override_forbidden_lists(world: tuple[Path, Path]) -> None:
    root, repo = world
    commit(repo, "corpus/a.txt")
    commit(repo, "src/w.pt")
    cfg = copy.deepcopy(CFG)
    cfg["push_guard"]["forbidden_path_prefixes"] = ["corpus"]
    cfg["push_guard"]["forbidden_suffixes"] = [".xyz"]
    detail = {part: (clean, d) for part, clean, d in pc.run(repo, cfg, root)}["paths"]
    assert detail == (False, "corpus/a.txt")  # `.pt` is no longer forbidden, `corpus/` is


def test_override_check_timeout(world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo = world
    seen: list[tuple[str, object]] = []
    real = pc.subprocess.run

    def spy(cmd, **kw):  # type: ignore[no-untyped-def]
        seen.append((Path(cmd[3]).name if cmd[0] == sys.executable else "git", kw.get("timeout")))
        return real(cmd, **kw)

    monkeypatch.setattr(pc.subprocess, "run", spy)
    cfg = copy.deepcopy(CFG)
    cfg["push_guard"]["check_timeout_seconds"] = 7
    cfg["git_timeout_seconds"] = 3
    pc.run(repo, cfg, root)
    assert ("setup_project.py", 7) in seen and ("decisions_applied.py", 7) in seen
    assert {t for name, t in seen if name == "git"} == {3}


def test_forbidden_paths_property() -> None:
    from hypothesis import given
    from hypothesis import strategies as s

    assert pc.forbidden_paths(["data", "DATA", "data2"], ["data"], [".bin"]) == ["data", "DATA"]
    word = s.text(alphabet="abdt_-.", min_size=1, max_size=6).filter(lambda n: n not in (".", ".."))
    name = s.one_of(s.sampled_from(["data", "Data", "x.bin", "data.bin.txt"]), word)

    @given(s.lists(name, min_size=1, max_size=4))
    def prop(parts: list[str]) -> None:
        path = "/".join(parts)
        bad = pc.forbidden_paths([path], ["data"], [".bin"])
        expect = parts[0].casefold() == "data" and len(parts) > 1 or path.casefold().endswith(".bin")
        expect = expect or path.casefold() == "data"
        assert bool(bad) == expect, path

    prop()


# F23 (T-024 final round): the marker of the checked commit exists only after this run passed every part.


def stale_marker(root: Path, repo: Path) -> Path:
    """The marker an earlier, successful run left for the current HEAD."""
    path = marker(root, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("earlier run\n", encoding="utf-8")
    return path


def test_the_marker_of_an_earlier_run_is_gone_before_any_part_runs(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = world
    stale = stale_marker(root, repo)
    seen: list[bool] = []
    real = pc.run

    def spy(*a, **k):  # type: ignore[no-untyped-def]
        seen.append(stale.exists())
        return real(*a, **k)

    monkeypatch.setattr(pc, "run", spy)
    assert pc.main([str(repo)], root=root) == 0
    assert seen == [False]  # absent while the parts ran, written afterwards
    assert stale.is_file() and stale.read_text(encoding="utf-8") != "earlier run\n"


@pytest.mark.parametrize(
    ("script", "out", "code"),
    [("setup_project.py", "FAIL links  dead link", 0), ("decisions_applied.py", "pending 1", 1)],
)
def test_a_failed_subcheck_removes_the_marker_of_an_earlier_success(
    world: tuple[Path, Path], script: str, out: str, code: int
) -> None:
    root, repo = world
    stale = stale_marker(root, repo)
    stub(root, script, out, code)
    assert pc.main([str(repo)], root=root) == 1
    assert not stale.exists()


@pytest.mark.parametrize("what", ["paths", "whitespace", "destination"])
def test_a_failed_range_part_removes_the_marker_of_an_earlier_success(
    world: tuple[Path, Path], what: str
) -> None:
    root, repo = world
    if what == "paths":
        commit(repo, "data/x.txt")
    elif what == "whitespace":
        commit(repo, "src/a.py", "x = 1   \n")
    else:
        commit(repo, "src/a.py")
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    stale = stale_marker(root, repo)
    assert pc.main([str(repo)], root=root) == 1
    assert not stale.exists()


def test_a_marker_that_appears_during_a_failing_run_is_removed_at_its_end(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another session's run for the same commit may write its marker while this run is still checking."""
    root, repo = world
    stub(root, "decisions_applied.py", "pending 1", 1)
    real = pc.run

    def run_while_another_run_marks(*a, **k):  # type: ignore[no-untyped-def]
        out = real(*a, **k)
        stale_marker(root, repo)
        return out

    monkeypatch.setattr(pc, "run", run_while_another_run_marks)
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()


def test_a_check_cut_short_removes_the_marker_and_lets_the_interruption_through(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = world
    stale = stale_marker(root, repo)

    def interrupted(*a, **k):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr(pc, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        pc.main([str(repo)], root=root)
    assert not stale.exists()


def test_an_unexpected_error_in_a_part_removes_the_marker(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = world
    commit(repo, "src/a.py")
    stale = stale_marker(root, repo)

    def boom(*a):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(pc, "forbidden_paths", boom)
    with pytest.raises(RuntimeError):
        pc.main([str(repo)], root=root)
    assert not stale.exists()


def test_a_head_that_moved_removes_the_marker_of_the_checked_commit(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = world
    commit(repo, "src/app.py")
    checked = stale_marker(root, repo)
    real_run = pc.run

    def run_then_commit(*a, **k):  # type: ignore[no-untyped-def]
        out = real_run(*a, **k)
        commit(repo, "src/late.py")
        return out

    monkeypatch.setattr(pc, "run", run_then_commit)
    assert pc.main([str(repo)], root=root) == 1
    assert not checked.exists()


def test_a_marker_that_cannot_be_removed_fails_the_run_closed(
    world: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, repo = world
    blocked = marker(root, repo)
    (blocked / "keep").mkdir(parents=True)  # a non-empty directory: unlink refuses it on every platform
    assert pc.main([str(repo)], root=root) == 1
    assert blocked.is_dir()
    captured = capsys.readouterr()
    assert "cannot remove the earlier marker" in captured.err
    assert "destination" not in captured.out  # no part ran


def test_a_marker_write_cut_short_leaves_neither_marker_nor_partial_file(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo = world

    def broken_replace(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(pc.prepush_mark.os, "replace", broken_replace)
    assert pc.main([str(repo)], root=root) == 1
    assert not marker(root, repo).exists()
    assert list((root / MARKERS).iterdir()) == []


def test_a_successful_run_leaves_exactly_the_marker(world: tuple[Path, Path]) -> None:
    root, repo = world
    assert pc.main([str(repo)], root=root) == 0
    assert [p.name for p in (root / MARKERS).iterdir()] == [git(repo, "rev-parse", "HEAD").strip()]


# F24 (T-024 final round): `git log -z --name-only` ends every name with NUL and never quotes it (git-log(1): "do not
# munge pathnames and use NULs as output field terminators"), so only NUL splits the output.


def log_output(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> None:
    """Test double of the `git log -z` call, labelled: the protocol bytes of git-log(1)."""

    real = pc._git

    def fake(repo: Path, timeout: int, *args: str):  # type: ignore[no-untyped-def]
        if args[0] == "log":
            return pc.subprocess.CompletedProcess(args, 0, stdout, b"")
        return real(repo, timeout, *args)

    monkeypatch.setattr(pc, "_git", fake)


def test_a_name_with_lf_tab_and_unicode_is_one_path(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ["src/a\nmodels/x.png", "src/tab\there.txt", "src/café/模型.txt", " lead and trail ", "cr\r.txt"]
    log_output(monkeypatch, "\0".join(names).encode() + b"\0\0")  # a commit boundary may add an empty record
    assert pc.changed_paths(Path("."), 5, "a" * 40, "b" * 40) == names


def test_an_lf_inside_a_name_does_not_make_a_forbidden_path(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 6 split `src/a\\nmodels/x.png` into `src/a` and `models/x.png` and refused the push for `models/`."""
    root, repo = world
    log_output(monkeypatch, b"src/a\nmodels/x.png\0")
    results = {part: (clean, detail) for part, clean, detail in pc.run(repo, CFG, root)}
    assert results["paths"] == (True, "1 paths, none forbidden")


def test_a_forbidden_name_with_lf_is_still_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    log_output(monkeypatch, b".secrets/k\nx.txt\0src/ok.py\0")
    paths = pc.changed_paths(Path("."), 5, "a" * 40, "b" * 40)
    assert paths == [".secrets/k\nx.txt", "src/ok.py"]
    assert pc.forbidden_paths(paths, CFG["push_guard"]["forbidden_path_prefixes"], []) == [".secrets/k\nx.txt"]


def test_changed_paths_splits_on_nul_only_property(monkeypatch: pytest.MonkeyPatch) -> None:
    from hypothesis import given, settings
    from hypothesis import strategies as s

    text = s.text(alphabet=s.characters(blacklist_categories=("Cs",), blacklist_characters="\0"), min_size=1, max_size=12)

    @settings(max_examples=60, deadline=None)
    @given(s.lists(text, min_size=0, max_size=6))
    def prop(names: list[str]) -> None:
        log_output(monkeypatch, "".join(n + "\0" for n in names).encode())
        assert pc.changed_paths(Path("."), 5, "a" * 40, "b" * 40) == list(dict.fromkeys(names))

    prop()


def _git_bytes(repo: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, check=False)


@pytest.mark.parametrize("name", ["src/a\nmodels/x.png", "src/tab\there.txt", "src/café/模型.txt", " lead.txt"])
def test_real_git_output_keeps_an_unusual_name_whole(world: tuple[Path, Path], name: str) -> None:
    """Real `git log -z` output through plumbing (no work tree file, so a name the platform cannot hold still gets a
    commit). Git for Windows refuses LF and tab in a path: that case is skipped there and measured on Linux."""
    _, repo = world
    base = git(repo, "rev-parse", "HEAD").strip()
    blob = _git_bytes(repo, "hash-object", "-w", "--stdin", data=b"x\n").stdout.decode().strip()
    probe = _git_bytes(
        repo, "update-index", "--add", "-z", "--index-info", data=f"100644 {blob}\t{name}\0".encode()
    )
    listed = _git_bytes(repo, "ls-files", "-z").stdout.decode("utf-8", "surrogateescape").split("\0")
    if name not in listed:
        pytest.skip(f"this platform's Git refuses the path ({probe.stderr.decode(errors='replace').strip()[:80]})")
    tree = _git_bytes(repo, "write-tree").stdout.decode().strip()
    new = _git_bytes(repo, "commit-tree", tree, "-p", base, "-m", "c").stdout.decode().strip()
    assert pc.changed_paths(repo, 30, base, new) == [name]
