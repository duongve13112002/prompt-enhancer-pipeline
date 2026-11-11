"""eol_check.py (P-04): findings from Git's own classification, `--fix` only for `eol=lf` files whose index is LF."""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest
from conftest import GITATTRIBUTES, git

SCRIPT = Path(__file__).resolve().parents[1] / "eol_check.py"
spec = importlib.util.spec_from_file_location("eol_check_tests", SCRIPT)
assert spec is not None and spec.loader is not None
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """`old/legacy.md` is committed with CRLF before `.gitattributes` exists, so its index copy stays CRLF."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "old").mkdir()
    (repo / "old" / "legacy.md").write_bytes(b"a\r\nb\r\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "legacy")
    (repo / ".gitattributes").write_bytes(GITATTRIBUTES.encode())
    for rel, data in {
        "tasks/STATUS.md": b"# status\n",
        "tasks/lonecr.md": b"a\rb\n",
        "assets/sample.png": b"\x89PNG\r\n\x1a\n\x00\x00\r\n",
        "frontend/app.js": b"let a = 1;\n",
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    # Not old/: `add -A` would renormalise it.
    git(repo, "add", ".gitattributes", "tasks", "assets", "frontend")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def problems(repo: Path) -> dict[str, tuple[str, bool]]:
    """The first finding per path (old/legacy.md has two: index and working copy, neither fixable)."""
    out: dict[str, tuple[str, bool]] = {}
    for f in ec.findings(repo, ec.ls_eol(repo)):
        out.setdefault(f.path, (f.problem, f.fixable))
    return out


def test_findings(repo: Path) -> None:
    (repo / "tasks" / "STATUS.md").write_bytes(b"# status\r\nrow\r\n")
    # text=auto: Git converts it, no finding.
    (repo / "frontend" / "app.js").write_bytes(b"let a = 1;\r\n")
    (repo / "assets" / "sample.png").write_bytes(b"\r\n\r\n\x00")  # binary: no finding
    found = problems(repo)
    assert set(found) == {"old/legacy.md", "tasks/STATUS.md", "tasks/lonecr.md"}
    assert found["tasks/STATUS.md"] == ("working copy has CRLF line endings (eol=lf)", True)
    assert found["old/legacy.md"][1] is False and "index copy has CRLF" in found["old/legacy.md"][0]
    assert found["tasks/lonecr.md"] == ("lone CR: Git treats this eol=lf file as binary", False)


def test_fix_rewrites_only_fixable_files_and_prints_them(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "tasks" / "STATUS.md").write_bytes(b"# status\r\nrow\r\n")
    assert ec.main(["--root", str(repo), "--fix"]) == 1  # legacy index CRLF and the lone CR remain
    out = capsys.readouterr().out
    assert "fixed tasks/STATUS.md: CRLF -> LF" in out
    assert "old/legacy.md: index copy has CRLF" in out and "tasks/lonecr.md: lone CR" in out
    assert (repo / "tasks" / "STATUS.md").read_bytes() == b"# status\nrow\n"
    assert (repo / "tasks" / "lonecr.md").read_bytes() == b"a\rb\n"
    assert (repo / "old" / "legacy.md").read_bytes() == b"a\r\nb\r\n"
    assert not list(repo.rglob("*.eolfix.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_fix_keeps_the_file_mode(repo: Path) -> None:
    """F17: a tracked executable stays executable (no mode change in `git diff`)."""
    script = repo / "tasks" / "STATUS.md"
    script.write_bytes(b"#!/bin/sh\r\necho\r\n")
    script.chmod(0o755)
    assert ec.fix(repo, ec.Finding("tasks/STATUS.md", "crlf", True))
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert script.read_bytes() == b"#!/bin/sh\necho\n"


def test_fix_never_follows_or_replaces_a_symlink(repo: Path) -> None:
    """F17: the link and the file it names stay as they are."""
    outside = repo.parent / "outside.md"
    outside.write_bytes(b"a\r\n")
    link = repo / "tasks" / "link.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert ec.fix(repo, ec.Finding("tasks/link.md", "crlf", True)) is False
    assert link.is_symlink() and outside.read_bytes() == b"a\r\n"


def test_without_fix_nothing_changes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "tasks" / "STATUS.md").write_bytes(b"x\r\n")
    assert ec.main(["--root", str(repo), "tasks/STATUS.md"]) == 1
    assert (
        "tasks/STATUS.md: working copy has CRLF line endings (eol=lf) (run with --fix)"
        in capsys.readouterr().out
    )
    assert (repo / "tasks" / "STATUS.md").read_bytes() == b"x\r\n"


def test_clean_and_path_limits(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ec.main(["--root", str(repo), "tasks/STATUS.md", "frontend/app.js"]) == 0
    assert "eol_check: clean" in capsys.readouterr().out
    assert ec.main(["--root", str(repo), "--fix", "old/legacy.md"]) == 1


def test_git_failure_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ec.main(["--root", str(tmp_path / "missing")]) == 2
    assert "git ls-files failed" in capsys.readouterr().err


def test_timeout_override_and_refusal(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake_run(command: list[str], **kwargs: object) -> ec.subprocess.CompletedProcess[bytes]:
        seen.append(kwargs.get("timeout"))
        return ec.subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(ec.subprocess, "run", fake_run)
    assert ec.main(["--root", str(repo), "--timeout", "7"]) == 0
    assert seen == [7]
    with pytest.raises(SystemExit) as exc:
        ec.main(["--root", str(repo), "--timeout", "soon"])
    assert exc.value.code == 2


def test_parse_eol() -> None:
    raw = b"i/lf    w/crlf  attr/text eol=lf      \ttasks/a b.md\0i/-text w/-text attr/-text            \tx.png\0"
    assert ec.parse_eol(raw) == [
        ec.Entry("tasks/a b.md", "lf", "crlf", "text eol=lf"),
        ec.Entry("x.png", "-text", "-text", "-text"),
    ]
    assert ec.parse_eol(b"") == []
