"""Unit tests for recall.py (local full-text recall with a capture-time privacy filter)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "recall.py"

# Test-only fakes shaped like real secrets (none is a live credential).
FAKE_SK = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
FAKE_HF = "hf_" + "AbCdEfGhIjKlMnOpQrStUv"
FAKE_AIZA = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
FAKE_GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_BEARER = "Bearer abcDEF123456.tokenvalue"
FAKE_ENTROPY = "q7Zk2XvB9nLm4WcR8tYp3HsD6fGj1aEu5N"


@pytest.fixture
def recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("recall", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path.resolve()
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "STATE", root / ".tmp" / "pm-state")
    return module


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(recall) -> Path:
    root = recall.ROOT
    write(
        root,
        "tasks/DECISIONS_INDEX.md",
        "# Decisions\n\n| Id | Date | Rule |\n|---|---|---|\n"
        "| 7 | 2026-02-01 | Use the zebra scheduler for queues |\n"
        "| 8a | 2026-02-02 | Hooks never block the session |\n",
    )
    write(
        root,
        "tasks/REQUIREMENTS.md",
        "# Req\n\n| R-001 | Generation is in scope: giraffe pipeline. | 185 |\n",
    )
    write(root, "tasks/archive/OLD.md", "# Old\n\nArchived walrus story\nsecond line\n")
    write(
        root,
        "docs/note/N1.md",
        "# Note\n\n## Section A\n\nalpha text\n\n## Section B\n\nbeta quokka text\n",
    )
    return root


def hits(recall, query: str, limit: int = 10):
    return recall.search(recall.STATE / recall.DB_NAME, query, limit, recall.ROOT)


def test_build_and_search_find_chunks_with_path_and_line(recall, tree) -> None:
    stats = recall.build(tree, recall.STATE / recall.DB_NAME, None)
    assert stats["indexed"] == 4 and stats["skipped_chunks"] == 0
    assert hits(recall, "zebra scheduler")[0][:3] == ("tasks/DECISIONS_INDEX.md", 5, "decision")
    assert hits(recall, "giraffe")[0][:3] == ("tasks/REQUIREMENTS.md", 3, "requirement")
    path, line, kind, excerpt = hits(recall, "quokka")[0]
    assert (path, line, kind) == ("docs/note/N1.md", 9, "section") and "quokka" in excerpt
    assert hits(recall, "walrus")[0][:2] == ("tasks/archive/OLD.md", 3)
    assert hits(recall, "walrus nonexistentword") == []
    assert len(hits(recall, "text", limit=1)) == 1


def test_incremental_rebuild_only_touches_changed_and_removed_files(recall, tree) -> None:
    db = recall.STATE / recall.DB_NAME
    recall.build(tree, db, None)
    assert recall.build(tree, db, None)["unchanged"] == 4
    write(tree, "docs/note/N1.md", "# Note\n\nnew content pangolin and more\n")
    (tree / "tasks/archive/OLD.md").unlink()
    stats = recall.build(tree, db, None)
    assert (stats["indexed"], stats["unchanged"], stats["removed"]) == (1, 2, 1)
    assert hits(recall, "pangolin") and not hits(recall, "walrus") and not hits(recall, "quokka")


def test_memory_dir_inside_root_is_indexed(recall, tree) -> None:
    write(tree, "mem/feedback.md", "# Fb\n\nremember the okapi rule\n")
    recall.build(tree, recall.STATE / recall.DB_NAME, tree / "mem")
    assert hits(recall, "okapi")[0][0] == "mem/feedback.md"


def test_paths_outside_the_root_are_refused(recall, tree, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    write(outside, "m.md", "# x\n\nforeign\n")
    with pytest.raises(recall.RecallError, match="outside the project root"):
        recall.build(tree, recall.STATE / recall.DB_NAME, outside)
    with pytest.raises(recall.RecallError, match="outside the project root"):
        recall.build(tree, outside / "x.sqlite", None)
    with pytest.raises(recall.RecallError, match="outside the project root"):
        recall.build(tree, tree / ".." / "escape.sqlite", None)
    with pytest.raises(recall.RecallError, match="outside the project root"):
        recall.search(outside / "x.sqlite", "q", 3, tree)


def link_dir(link: Path, target: Path) -> None:
    """Directory link: a symlink, or a junction on Windows hosts that do not grant symlink rights."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("links not permitted on this host")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )


def unlink_dir(link: Path) -> None:
    """Remove the link only, never its target: Windows removes a junction or directory symlink with rmdir, POSIX
    removes a directory symlink with unlink (rmdir raises NotADirectoryError there)."""
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


def test_link_leaving_the_root_is_skipped(recall, tree, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    write(outside, "leak.md", "# Leak\n\nforeign-marker\n")
    link = tree / "tasks" / "archive"
    shutil.rmtree(link)
    link_dir(link, outside)
    try:
        stats = recall.build(tree, recall.STATE / recall.DB_NAME, None)
        assert stats["refused"] == 1 and not hits(recall, "foreign-marker")
    finally:
        unlink_dir(link)


def test_secrets_directory_is_refused_everywhere(recall, tree) -> None:
    write(tree, ".secrets/key.md", "# K\n\nsecret-body\n")
    with pytest.raises(recall.RecallError, match=r"\.secrets"):
        recall.build(tree, recall.STATE / recall.DB_NAME, tree / ".secrets")
    with pytest.raises(recall.RecallError, match=r"\.secrets"):
        recall.build(tree, tree / ".secrets" / "r.sqlite", None)
    # a source directory that is a link into .secrets is skipped, not indexed
    link = tree / "tasks" / "archive"
    shutil.rmtree(link)
    link_dir(link, tree / ".secrets")
    try:
        stats = recall.build(tree, recall.STATE / recall.DB_NAME, None)
        assert stats["refused"] == 1 and not hits(recall, "secret-body")
    finally:
        unlink_dir(link)


@pytest.mark.parametrize(
    "secret",
    [
        FAKE_SK,
        FAKE_HF,
        FAKE_AIZA,
        FAKE_GHP,
        FAKE_BEARER,
        FAKE_ENTROPY,
        "api_key = abcd1234efgh5678",
    ],
)
def test_redact_removes_each_secret_class(recall, secret) -> None:
    out = recall.redact(f"before {secret} after")
    assert "before" in out and "after" in out and recall.REDACTED in out
    core = secret.split(" ")[-1].split("=")[-1].strip()
    assert core not in out


def test_redact_private_spans_and_key_blocks_keep_line_numbers(recall) -> None:
    text = "a\n<private>\nline1\nline2\n</PRIVATE>\nb\n-----BEGIN RSA PRIVATE KEY-----\nMIIx\nyy\n-----END RSA PRIVATE KEY-----\nc"
    out = recall.redact(text)
    assert "line1" not in out and "MIIx" not in out and "BEGIN" not in out
    assert (
        out.count("\n") == text.count("\n")
        and out.split("\n")[5] == "b"
        and out.split("\n")[-1] == "c"
    )


def test_unterminated_private_span_and_key_block_run_to_the_end(recall) -> None:
    assert "tail" not in recall.redact("ok <private> tail text")
    assert "tail" not in recall.redact("ok -----BEGIN PRIVATE KEY-----\ntail")


def test_redact_leaves_ordinary_text_alone(recall) -> None:
    text = (
        "tools/agent-workflow/hooks/session_context.py DECISIONS_INDEX-full-to-20261011.md 9b4f"
        + "0" * 40
    )
    assert recall.redact(text) == text


def test_private_span_across_chunk_border_never_reaches_the_database(recall, tree) -> None:
    body = "\n".join(f"filler {n}" for n in range(300))
    write(
        tree,
        "docs/note/N2.md",
        f"# Big\n\n{body}\n<private>\nhidden-one\n{body}\nhidden-two\n</private>\nvisible-end\n",
    )
    recall.build(tree, recall.STATE / recall.DB_NAME, None, chunk_chars=300)
    assert not hits(recall, "hidden") and not hits(recall, "one hidden")
    assert hits(recall, "visible")
    dump = (
        sqlite3.connect(recall.STATE / recall.DB_NAME)
        .execute("SELECT group_concat(body) FROM chunks")
        .fetchone()[0]
    )
    assert "hidden-one" not in dump and "hidden-two" not in dump


def test_secrets_in_sources_are_redacted_before_storage(recall, tree) -> None:
    write(tree, "docs/note/N3.md", f"# Keys\n\nthe key {FAKE_SK} and {FAKE_HF} end\n")
    recall.build(tree, recall.STATE / recall.DB_NAME, None)
    raw = (
        sqlite3.connect(recall.STATE / recall.DB_NAME)
        .execute("SELECT group_concat(body) FROM chunks")
        .fetchone()[0]
    )
    assert FAKE_SK not in raw and FAKE_HF not in raw and recall.REDACTED in raw


def test_redactor_failure_skips_the_chunk_not_stores_it(recall, tree, monkeypatch) -> None:
    real = recall.redact

    def flaky(text: str) -> str:
        # fails only for the per-chunk pass of the second section (the whole-file pass contains headings)
        if "unsafe-chunk" in text and "# Two" not in text:
            raise RuntimeError("redactor bug")
        return real(text)

    write(tree, "docs/note/N4.md", "# One\n\nsafe-chunk\n\n# Two\n\nunsafe-chunk\n")
    monkeypatch.setattr(recall, "redact", flaky)
    stats = recall.build(tree, recall.STATE / recall.DB_NAME, None)
    assert stats["skipped_chunks"] >= 1
    assert hits(recall, "safe-chunk") and not hits(recall, "unsafe-chunk")


def test_fts5_absent_falls_back_to_like_search(recall, tree, monkeypatch) -> None:
    # labelled double: pretends the interpreter's SQLite has no FTS5 module
    with monkeypatch.context() as patch:
        patch.setattr(recall, "fts5_available", lambda conn: False)
        recall.build(tree, recall.STATE / recall.DB_NAME, None)
        conn = sqlite3.connect(recall.STATE / recall.DB_NAME)
        assert conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0] == "like"
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='chunks' AND sql LIKE '%fts5%'"
            ).fetchone()[0]
            == 0
        )
        conn.close()
        assert hits(recall, "zebra scheduler")[0][:2] == ("tasks/DECISIONS_INDEX.md", 5)
        assert hits(recall, "100%_ nothing") == []
    # real FTS5 again: the stored mode differs, so everything is rebuilt instead of mixing storage formats
    stats = recall.build(tree, recall.STATE / recall.DB_NAME, None)
    assert stats["indexed"] == 4 and hits(recall, "zebra")


def test_search_errors_are_clear(recall, tree) -> None:
    with pytest.raises(recall.RecallError, match="build"):
        hits(recall, "zebra")
    recall.build(tree, recall.STATE / recall.DB_NAME, None)
    with pytest.raises(recall.RecallError, match="searchable"):
        hits(recall, "!!!")
    with pytest.raises(recall.RecallError, match="at least 1"):
        hits(recall, "zebra", limit=0)


def test_cli_build_and_search(recall, tree, capsys) -> None:
    assert recall.main(["build"]) == 0
    assert "indexed=4" in capsys.readouterr().out
    assert recall.main(["search", "zebra", "--limit", "2"]) == 0
    assert capsys.readouterr().out.startswith("tasks/DECISIONS_INDEX.md:5 [decision] ")
    assert recall.main(["build", "--chunk-chars", "10"]) == 2
    assert recall.main(["build", "--db", str(tree / ".secrets" / "r.sqlite")]) == 2
