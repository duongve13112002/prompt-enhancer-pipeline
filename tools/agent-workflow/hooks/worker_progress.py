"""PreCompact hook for cli_worker workers (WP-14): write a progress block into the worker's task file.

`worker_progress.py --task-file <file> [--config F]` replaces the task file's `worker_progress.heading` section (it
runs to the next `## ` heading) with: time and compaction trigger, `git status --short` of the worker's cwd, the files
the session last changed with Write/Edit (from the transcript tail) and an excerpt of its last assistant text.
Mechanical facts only; the worker's own sections stay authoritative. Runs only in workers (the cli_worker
environment flag), only for a task file inside the project root; never blocks compaction (always exits 0).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_config as gc
import process_guard as pg

# Set only for cli_worker workers (as precompact_snapshot).
WORKER_ENV_FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
EDIT_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
REPLACE_ATTEMPTS = 40  # Windows: an editor or reader may hold the task file for a moment
REPLACE_DELAY = 0.05


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    lines = data.decode("utf-8", "replace").split("\n")
    return lines[1:] if size > max_bytes else lines  # the first line of a tail is usually cut


def transcript_facts(path: Path | None, cfg: dict[str, Any]) -> tuple[list[str], str]:
    """(files last changed with Write/Edit, newest first; last assistant text) from the transcript JSONL tail."""
    wp = cfg["worker_progress"]
    if path is None or not path.is_file():
        return [], ""
    files: list[str] = []
    text = ""
    for line in reversed(_tail_lines(path, wp["transcript_tail_bytes"])):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        for block in reversed(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                value = (block.get("input") or {}).get(EDIT_TOOLS[block["name"]])
                if isinstance(value, str) and value not in files and len(files) < wp["max_files"]:
                    files.append(value)
            elif block.get("type") == "text" and not text and isinstance(block.get("text"), str):
                text = block["text"].strip()
        if text and len(files) >= wp["max_files"]:
            break
    return files, text


def git_status(cwd: Path, cfg: dict[str, Any]) -> list[str]:
    limit = cfg["worker_progress"]["git_status_lines"]
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "status", "--short", "--branch"],
            capture_output=True,
            timeout=cfg["git_timeout_seconds"],
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"git status unavailable: {type(exc).__name__}"]
    out = proc.stdout.decode("utf-8", "replace").splitlines()
    if proc.returncode != 0:
        return [f"git status failed (exit {proc.returncode})"]
    return [*out[:limit], f"... {len(out) - limit} more lines"] if len(out) > limit else out


def _single_line(text: str) -> str:
    return " ".join(text.split()).replace("`", "'")


def build_block(event: dict[str, Any], cfg: dict[str, Any]) -> str:
    wp = cfg["worker_progress"]
    cwd = Path(event["cwd"]) if isinstance(event.get("cwd"), str) and event["cwd"] else Path.cwd()
    transcript = event.get("transcript_path")
    files, text = transcript_facts(Path(transcript) if isinstance(transcript, str) else None, cfg)
    excerpt = _single_line(text)
    if len(excerpt) > wp["max_excerpt_chars"]:
        excerpt = excerpt[: wp["max_excerpt_chars"]] + " ..."
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"{wp['heading']} {stamp} (PreCompact, trigger={event.get('trigger', '?')})",
        "Written by the worker's PreCompact hook (mechanical); the sections above stay authoritative.",
        f"- git status ({cwd}):",
        *[f"  - `{_single_line(line)}`" for line in git_status(cwd, cfg)],
        "- last changed files: "
        + ("; ".join(f"`{_single_line(f)}`" for f in files) or "none recorded"),
        "- last assistant text: " + (excerpt or "none recorded"),
        "",
    ]
    return "\n".join(lines)


def replace_section(text: str, heading: str, block: str) -> str:
    """Drop every earlier `heading` section (to the next `## ` heading) and append the new block."""
    kept: list[str] = []
    skipping = False
    for line in text.split("\n"):  # not splitlines(): that also splits at U+2028, form feeds, ...
        line = line.removesuffix("\r")
        if line.startswith("## "):
            skipping = line.startswith(heading)
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip("\n")
    return (body + "\n\n" if body else "") + block


def write_task_file(path: Path, heading: str, block: str) -> None:
    old = path.read_text(encoding="utf-8-sig", errors="surrogateescape") if path.exists() else ""
    data = replace_section(old, heading, block).encode("utf-8", errors="surrogateescape")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.progress")
    try:
        tmp.write_bytes(data)
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_DELAY)
    finally:
        tmp.unlink(missing_ok=True)


def task_path(text: str, root: Path = gc.ROOT) -> Path:
    path = Path(os.path.realpath(text))
    top = os.path.normcase(os.path.realpath(root))
    here = os.path.normcase(str(path))
    if not here.startswith(top.rstrip("\\/") + os.sep) or path.suffix.lower() != ".md":
        raise ValueError(f"--task-file {text!r} must be a .md file inside the project root {root}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    if os.environ.get(WORKER_ENV_FLAG) != "1":
        return 0  # only workers write task-file progress
    try:
        event = json.loads(pg.read_event() or "{}")
    except ValueError:
        event = {}
    try:
        cfg = gc.load(gc.resolve_path(args.config))
        path = task_path(args.task_file)
        write_task_file(
            path,
            cfg["worker_progress"]["heading"],
            build_block(event if isinstance(event, dict) else {}, cfg),
        )
    except Exception as exc:  # never block compaction
        print(f"worker_progress: cannot write the progress block: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
