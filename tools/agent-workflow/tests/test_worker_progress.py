"""worker_progress.py (WP-14): the PreCompact progress block in a worker's task file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest
import worker_progress as wp
from conftest import git

CFG = gc.load()
HEADING = CFG["worker_progress"]["heading"]


def with_progress(**values: Any) -> dict[str, Any]:
    cfg = json.loads(json.dumps(CFG))
    cfg["worker_progress"].update(values)
    return gc.validate(cfg)


def transcript(path: Path, edits: list[str], text: str) -> Path:
    records: list[dict[str, Any]] = [{"type": "user", "message": {"role": "user", "content": "go"}}]
    for name in edits:
        block = {"type": "tool_use", "name": "Edit", "input": {"file_path": name}}
        records.append({"message": {"role": "assistant", "content": [block]}})
    records.append({"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    path.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8")
    return path


def test_replace_section_is_idempotent_and_keeps_other_sections() -> None:
    text = (
        f"# Task\r\n\r\n## Steps\r\n- a\r\n\r\n{HEADING} old\r\n- stale\r\n\r\n## Notes\r\nkeep\r\n"
    )
    once = wp.replace_section(text, HEADING, HEADING + " new\n- fresh\n")
    twice = wp.replace_section(once, HEADING, HEADING + " new\n- fresh\n")
    assert once == twice
    assert once.count(HEADING) == 1 and "stale" not in once
    assert "## Steps\n- a" in once and "## Notes\nkeep" in once
    assert once.endswith(HEADING + " new\n- fresh\n")
    assert wp.replace_section("", HEADING, "B\n") == "B\n"


def test_build_block_from_transcript(tmp_path: Path) -> None:
    edits = ["src/a.py", "src/b.py", "src/a.py", "src/c.py"]
    path = transcript(tmp_path / "t.jsonl", edits, "Done `x`\n  next: y")
    event = {"cwd": str(tmp_path / "not-a-repo"), "transcript_path": str(path), "trigger": "auto"}
    block = wp.build_block(event, with_progress(max_files=2, max_excerpt_chars=8))
    lines = block.splitlines()
    assert lines[0].startswith(HEADING) and "trigger=auto" in lines[0]
    # Newest first, unique, capped.
    assert "- last changed files: `src/c.py`; `src/a.py`" in block
    # One line, backticks neutralised, truncated.
    assert "- last assistant text: Done 'x' ..." in block
    assert "git status failed" in block or "git status unavailable" in block


def test_build_block_git_status_and_limits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name in ("a.txt", "b.txt", "c.txt"):
        (repo / name).write_text("x", encoding="utf-8")
    block = wp.build_block({"cwd": str(repo)}, with_progress(git_status_lines=2))
    assert "  - `## No commits yet on main`" in block and "  - `?? a.txt`" in block
    assert "  - `... 2 more lines`" in block and "b.txt" not in block
    assert "- last changed files: none recorded" in block
    assert "- last assistant text: none recorded" in block
    other = wp.build_block({"cwd": str(repo)}, with_progress(heading="## Progress (auto)"))
    assert other.startswith("## Progress (auto) ")


def test_transcript_tail_limit(tmp_path: Path) -> None:
    path = transcript(tmp_path / "t.jsonl", ["src/old.py"], "x" * 50)
    files, text = wp.transcript_facts(path, with_progress(transcript_tail_bytes=30))
    # The cut first line is dropped, so a tiny tail sees no record.
    assert files == [] and text == ""
    assert wp.transcript_facts(path, CFG)[0] == ["src/old.py"]
    assert wp.transcript_facts(tmp_path / "missing.jsonl", CFG) == ([], "")


def test_task_path_refusals(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "tasks").mkdir(parents=True)
    task = root / "tasks" / "T.md"
    assert wp.task_path(str(task), root) == Path(os.path.realpath(task))
    for bad in (root / "tasks" / "T.txt", tmp_path / "T.md", root / ".." / "T.md"):
        with pytest.raises(ValueError, match="inside the project root"):
            wp.task_path(str(bad), root)


def test_main_only_in_workers_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = gc.ROOT / ".tmp" / "r3-guard-hooks-test" / f"T-{os.getpid()}.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text("# T\n\n## Steps\n- a\n", encoding="utf-8")
    event = json.dumps({"cwd": str(gc.ROOT), "trigger": "manual"})
    monkeypatch.setattr(wp.pg, "read_event", lambda: event)
    try:
        monkeypatch.delenv(wp.WORKER_ENV_FLAG, raising=False)
        assert wp.main(["--task-file", str(task)]) == 0
        assert HEADING not in task.read_text(encoding="utf-8")  # the PM session: no block
        monkeypatch.setenv(wp.WORKER_ENV_FLAG, "1")
        assert wp.main(["--task-file", str(task)]) == 0
        assert wp.main(["--task-file", str(task)]) == 0
        body = task.read_text(encoding="utf-8")
        assert body.count(HEADING) == 1 and "trigger=manual" in body
        assert body.startswith("# T\n\n## Steps\n- a\n")
        assert wp.main(["--task-file", str(gc.ROOT.parent / "T.md")]) == 0
        assert "cannot write the progress block" in capsys.readouterr().err
        assert wp.main(["--task-file", str(task), "--config", "README.md"]) == 0
        assert "cannot write the progress block" in capsys.readouterr().err
    finally:
        task.unlink(missing_ok=True)
