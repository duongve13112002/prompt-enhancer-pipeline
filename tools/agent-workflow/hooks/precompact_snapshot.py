"""PreCompact hook for the PM session: append a state snapshot to the PM continuation note.

Reads the hook JSON on stdin and replaces the previous snapshot section with a new one: time, compaction trigger, `git status --short` summary,
busy cli_worker sessions, Gemini jobs without a result and a copy (at most `inflight_snapshot_chars` of
memory_hooks.json) of the PM's `## IN-FLIGHT` section: worker, task, next action, pending user questions (decision 353,
WP-02). The copy is indented so none of its lines is a `## ` heading. PreCompact output is not shown to the model
(documented: its `systemMessage` and `continue` are discarded); the copy survives because the note does, and
session_context.py injects the PM's own `## IN-FLIGHT` section at SessionStart. Sessions that are cli_worker workers
(by recorded session id) are skipped, so only the PM's own compactions write the note.
Standard library only (runs with `python -B -I`); never blocks compaction (always exits 0).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR") or ROOT / ".tmp" / "pm-state")
NOTE = STATE / "pm-continuation.md"
# Bounded git call: a hung git must never delay compaction for long.
SNAPSHOT_HEADING = "## AUTO SNAPSHOT"
# Note format names, shared with the PM's note and session_context.py (protocol-bound).
INFLIGHT_HEADING = "## IN-FLIGHT"
TOOLS = Path(__file__).resolve().parents[1]
CONFIG = TOOLS / "memory_hooks.json"
# Hooks run with `python -I` (no script directory on sys.path): the settings loader is imported by file path.
CONFIG_MODULE = TOOLS / "memory_hooks_config.py"
# Protocol-bound (OS file locking, not a deployment choice): the retry budget is about 2 s in total.
# The PM may have the note open in an editor or a reader may hold it: `os.replace` onto it fails on Windows while
# another process holds it without delete sharing, usually for milliseconds.
REPLACE_ATTEMPTS = 40
REPLACE_WAIT_SECONDS = 0.05
# Set only for cli_worker workers (same switch as ctx_nudge and session_context).
WORKER_ENV_FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def worker_registry() -> dict[str, dict]:
    data = load_json(STATE / "cli-workers.json")
    if not isinstance(data, dict):
        return {}
    workers = data.get("workers", data)
    return {k: v for k, v in workers.items() if isinstance(v, dict)}


def int_setting(field: str) -> int:
    """A required integer of memory_hooks.json; raises NotConfigured (a path-loaded class: catch Exception)."""
    spec = importlib.util.spec_from_file_location("memory_hooks_config", CONFIG_MODULE)
    if spec is None or spec.loader is None:
        raise ImportError(CONFIG_MODULE.name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load(CONFIG).int_(field)  # type: ignore[no-any-return]


def git_status() -> list[str]:
    try:
        timeout = int_setting("precompact_git_timeout_seconds")
        max_lines = int_setting("precompact_git_status_lines")
    except (
        Exception
    ) as exc:  # NotConfigured or a broken loader: say so, keep the rest of the snapshot
        return [f"git status skipped: {exc}"]
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"git status unavailable: {type(exc).__name__}"]
    if len(out) > max_lines:
        out = [*out[:max_lines], f"... {len(out) - max_lines} more lines"]
    return out


def gemini_open_jobs() -> list[str]:
    jobs = load_json(STATE / "agents.json")
    if not isinstance(jobs, list):
        return []
    results = ROOT / ".tmp" / "agy-work" / "_results"
    return [
        f"{j.get('id')} ({j.get('label', '')}, started {j.get('started', '?')})"
        for j in jobs
        if isinstance(j, dict)
        and j.get("kind") == "gemini"
        and j.get("id")
        and not any(results.glob(f"{j['id']}*"))
    ]


def inflight_copy(note_text: str) -> list[str]:
    """Snapshot lines for the note's `## IN-FLIGHT` section, bounded by `inflight_snapshot_chars`."""
    try:
        limit = int_setting("inflight_snapshot_chars")
    except (
        Exception
    ) as exc:  # NotConfigured or a broken loader: say so, keep the rest of the snapshot
        return [f"- IN-FLIGHT copy skipped: {exc}"]
    body: list[str] = []
    inside = False
    for line in note_text.split("\n"):
        line = line.removesuffix("\r")
        if line.startswith("## "):
            inside = line.startswith(INFLIGHT_HEADING)
            continue
        if inside:
            body.append(line)
    text = "\n".join(body).strip()
    if not text:
        return [f"- IN-FLIGHT: none (the note has no `{INFLIGHT_HEADING}` section or it is empty)"]
    cut = len(text) > limit
    lines = text[:limit].split("\n")
    return [
        f"- IN-FLIGHT (copy of the note's `{INFLIGHT_HEADING}` section at snapshot time):",
        *[f"  {line}" for line in lines],
        *([f"  [cut at {limit} characters]"] if cut else []),
    ]


def build_snapshot(event: dict, workers: dict[str, dict], note_text: str = "") -> str:
    busy = [
        f"{name} ({w.get('model', '?')}, {w.get('label', '')}, last_used {w.get('last_used', '?')})"
        for name, w in workers.items()
        if w.get("status") == "busy"
    ]
    lines = [
        "",
        f"{SNAPSHOT_HEADING} {datetime.now().astimezone().isoformat(timespec='seconds')}"
        f" (PreCompact, trigger={event.get('trigger', '?')})",
        "Written by the PreCompact hook; the PM-written sections above stay authoritative.",
        "- git status:",
        *[f"  - `{line}`" for line in git_status()],
        "- busy cli workers: " + ("; ".join(busy) if busy else "none"),
        "- Gemini jobs without result: " + ("; ".join(gemini_open_jobs()) or "none"),
        *inflight_copy(note_text),
        "",
    ]
    return "\n".join(lines)


def without_snapshots(text: str) -> str:
    """Drop earlier AUTO SNAPSHOT sections (each runs to the next `## ` heading) so the note never grows per compaction."""
    kept: list[str] = []
    skipping = False
    for line in text.split(
        "\n"
    ):  # not splitlines(): that would also break the PM's text at U+2028, form feeds, ...
        line = line.removesuffix("\r")
        if line.startswith("## "):
            skipping = line.startswith(SNAPSHOT_HEADING)
        if not skipping:
            kept.append(line)
    joined = "\n".join(kept).rstrip("\n")
    return joined + "\n" if joined else ""


def write_note(snapshot: str) -> None:
    # utf-8-sig drops an editor's BOM (it would hide the first heading); surrogateescape keeps stray bytes of the
    # PM's text intact instead of failing the whole snapshot.
    old = NOTE.read_text(encoding="utf-8-sig", errors="surrogateescape") if NOTE.exists() else ""
    data = (without_snapshots(old) + snapshot).encode("utf-8", errors="surrogateescape")
    tmp = NOTE.with_name(f"{NOTE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, NOTE)
                return
            except PermissionError:
                if attempt < REPLACE_ATTEMPTS - 1:
                    time.sleep(REPLACE_WAIT_SECONDS)
        NOTE.write_bytes(
            data
        )  # replace stayed blocked: an in-place write still records the snapshot
    finally:
        tmp.unlink(missing_ok=True)


def read_event() -> str:
    """Hook JSON from stdin as UTF-8. `python -I` ignores PYTHONUTF8, so the text layer would use the locale code
    page and mangle or reject non-ASCII paths; a test double without a byte layer falls back to `read()`."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read()
    return stream.read().decode("utf-8-sig", errors="replace")


def main() -> int:
    if os.environ.get(WORKER_ENV_FLAG) == "1":
        return 0  # a worker rooted in the PM's checkout loads this hook too and must not write the PM's note
    try:
        event = json.loads(read_event() or "{}")
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    workers = worker_registry()
    session = event.get("session_id")
    if session and any(w.get("session_id") == session for w in workers.values()):
        return 0
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            note_text = NOTE.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            note_text = ""
        write_note(build_snapshot(event, workers, note_text))
    except Exception as exc:  # never block compaction, whatever went wrong
        print(f"precompact_snapshot: cannot write {NOTE}: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
