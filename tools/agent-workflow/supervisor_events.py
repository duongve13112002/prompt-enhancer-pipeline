"""Queue cli_worker turn-end events for the PM (decision 353, WP-12).

`poll` compares the worker registry (`.tmp/pm-state/cli-workers.json`) with the statuses and `turns` counters it saw
last time (`.tmp/pm-state/supervisor-events.json`). A worker that was `busy` and now is `idle` (same session), or is
`idle` with a larger `turns` counter (a turn that began and ended between two polls), yields
`turn done: <worker> <task>`, labelled `Step 0 ready / ` when its `turns` counter is 1; one that ended in an `error ...`
status yields `turn ended with error: ...`. Lines are appended to `.tmp/pm-state/inbox/pm.events.md`, which
`hooks/ctx_nudge.py --role pm` delivers once at the PM's next tool call. The first poll only records a baseline.
Delivery is at-least-once (the inbox is written before the state) and bounded: when the pending file would pass
`inbox_max_chars` (memory_hooks.json) the oldest new lines are dropped and a one-line count says so. Run it from one
supervisor only; it prints nothing unless `--verbose` and exits 2 when `inbox_max_chars` is not configured.

Usage: supervisor_events.py poll [--registry PATH] [--state-file PATH] [--inbox-dir PATH] [--config PATH] [--verbose]
Standard library only (runs with `python -B -I`).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR") or ROOT / ".tmp" / "pm-state")
CONFIG_MODULE = Path(__file__).resolve().parent / "memory_hooks_config.py"
# Same file name as hooks/ctx_nudge.py PM_EVENTS_NAME (protocol-bound: writer and reader must agree).
PM_EVENTS_NAME = "pm.events.md"
# Layout bound: a worker name or task label is clipped so one event stays one short line.
FIELD_CHARS = 100
CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def load_settings(config: Path | None) -> Any:
    spec = importlib.util.spec_from_file_location("memory_hooks_config", CONFIG_MODULE)
    if spec is None or spec.loader is None:
        raise ImportError(str(CONFIG_MODULE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load(config) if config else module.load()


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def registry_rows(path: Path) -> dict[str, dict[str, Any]] | None:
    """Registry rows by worker name; None when the file is unreadable (the caller then leaves its state untouched)."""
    data = read_json(path)
    workers = data.get("workers", data) if isinstance(data, dict) else None
    if not isinstance(workers, dict):
        return None
    return {str(k): v for k, v in workers.items() if isinstance(v, dict)}


def field(value: object) -> str:
    return CONTROL.sub(" ", str(value if value is not None else "")).strip()[:FIELD_CHARS]


def turn_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def event_lines(seen: dict[str, Any], rows: dict[str, dict[str, Any]], stamp: str) -> list[str]:
    """Events for workers whose turn ended since `seen`, in registry order.

    A turn ended when the worker was `busy` and is `idle` or in error now (same session), when it is `idle` and its
    `turns` counter grew (a whole turn between two polls), or when it is new or restarted and idle with `turns` >= 1."""
    out = []
    for name, row in rows.items():
        status = str(row.get("status", ""))
        if status in ("retired", "busy"):
            continue
        before = seen.get(name)
        prev: dict[str, Any] = before if isinstance(before, dict) else {}
        known = bool(prev) and prev.get("session_id") == str(row.get("session_id", ""))
        was_busy = known and prev.get("status") == "busy"
        turns = turn_count(row.get("turns"))
        who = f"{field(name)} {field(row.get('label'))}".strip()
        if status == "idle":
            earlier = turn_count(prev.get("turns"))
            grew = turns is not None and earlier is not None and turns > earlier
            if (was_busy or grew) if known else bool(turns):
                step0 = "Step 0 ready / " if turns == 1 else ""
                out.append(f"- {stamp} {step0}turn done: {who} (turn {field(row.get('turns'))})")
        elif status.startswith("error") and was_busy:
            out.append(f"- {stamp} turn ended with error: {who} ({field(status)})")
    return out


def snapshot(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, row in rows.items():
        if row.get("status") == "retired":
            continue
        out[name] = {
            "status": str(row.get("status", "")),
            "session_id": str(row.get("session_id", "")),
        }
        turns = turn_count(row.get("turns"))
        if turns is not None:
            out[name]["turns"] = turns
    return out


def append_events(inbox: Path, lines: list[str], limit: int) -> int:
    """Append `lines` to the PM events file within `limit` characters; returns the number of dropped lines."""
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / PM_EVENTS_NAME
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    kept = list(lines)
    while kept and size + sum(len(line) + 1 for line in kept) > limit:
        kept.pop(0)
    dropped = len(lines) - len(kept)
    if dropped:
        note = f"- [{dropped} earlier event(s) dropped: the PM inbox is full]"
        while kept and size + len(note) + 1 + sum(len(line) + 1 for line in kept) > limit:
            kept.pop(0)
        if size + len(note) + 1 + sum(len(line) + 1 for line in kept) <= limit:
            kept.insert(0, note)
    if kept:
        # One append call: the hook claims the file by rename, so it sees whole lines or nothing.
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(kept) + "\n")
    return dropped


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def poll(args: argparse.Namespace) -> int:
    try:
        limit = load_settings(args.config).int_("inbox_max_chars")
    except Exception as exc:  # NotConfigured (class identity differs for a path-loaded module)
        print(f"supervisor_events: {exc}", file=sys.stderr)
        return 2
    rows = registry_rows(args.registry)
    if rows is None:
        print("supervisor_events: registry unreadable; state kept", file=sys.stderr)
        return 0
    previous = read_json(args.state_file)
    seen = previous.get("seen") if isinstance(previous, dict) else None
    lines: list[str] = []
    if isinstance(seen, dict):
        lines = event_lines(seen, rows, datetime.now().astimezone().strftime("%H:%M"))
    dropped = append_events(args.inbox_dir, lines, limit) if lines else 0
    write_json(args.state_file, {"seen": snapshot(rows)})
    if dropped:
        print(f"supervisor_events: {dropped} event(s) dropped, inbox full", file=sys.stderr)
    if args.verbose:
        print("\n".join(lines) if lines else "no events")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser(
        "poll", help="queue events for workers whose turn ended since the last poll"
    )
    cmd.add_argument("--registry", type=Path, default=STATE / "cli-workers.json")
    cmd.add_argument("--state-file", type=Path, default=STATE / "supervisor-events.json")
    cmd.add_argument("--inbox-dir", type=Path, default=STATE / "inbox")
    cmd.add_argument("--config", type=Path, default=None)
    cmd.add_argument("--verbose", action="store_true")
    return poll(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
