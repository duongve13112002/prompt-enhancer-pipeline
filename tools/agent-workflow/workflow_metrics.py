"""Per-worker cost and guard metrics from Claude CLI worker transcripts and the worker registry (read-only).

Sources: `.tmp/pm-state/cli-workers.json` (written by cli_worker.py) and `.tmp/cli-jobs/<worker>/NNN.jsonl`.
A task is one registry worker. Columns:

- first call: input + cache read + cache write tokens of the first API call (the fixed context a worker pays to start);
- input-eq: sum over API calls of input x1 + cache read x0.1 + cache write x2 (weights are options);
- cost: the largest `total_cost_usd` of the result events (the field is cumulative per session), n/a when none;
- resume miss / call miss: a call whose cache read is more than 4096 tokens below the previous call's context
  (first call of a resumed turn / any later call); miss write = tokens rewritten by those calls;
- blocked: tool results carrying "BLOCKED by process_guard" (actual, only for workers started with the guard);
  would-block: the same classifier replayed over every Bash/PowerShell call (what the guard would have stopped);
- nudge / loop: the hook rules replayed over the transcript with the worker's own settings, or the defaults
  (120000,150000 and window 5) for workers started before the hooks existed. Hook output is not echoed in the
  stream, so these are replays of the deterministic hook rules, not observations.

Usage: python -B workflow_metrics.py [--since TS] [--until TS] [--last N] [--guarded any|yes|no]
                                    [--split TS] [--out FILE] [--json]
TS is an ISO date or datetime compared with the worker's `created` time (since inclusive, until exclusive).
--last N keeps the N newest completed workers (retired or idle, at least one log) after the other filters.
--split TS prints two groups (before: newest --last N completed; after: all completed from TS on) with medians.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import statistics
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
# Data root: AGENT_WORKFLOW_ROOT (read-only use, e.g. from a worktree), else the project root above tools/.
ROOT = Path(os.environ.get("AGENT_WORKFLOW_ROOT") or HERE.parents[1])
REGISTRY = ROOT / ".tmp" / "pm-state" / "cli-workers.json"
JOBS = ROOT / ".tmp" / "cli-jobs"
MISS_TOKENS = 4096  # same noise floor as cache_check.py
DEFAULT_NUDGE = "120000,150000"
DEFAULT_LOOP_WINDOW = 5
BLOCK_MARKER = "BLOCKED by process_guard"


def load_hook(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / "hooks" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load hook module {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_ts(text: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"not an ISO date or datetime: {text!r}") from None


def read_events(log: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    # LF only: str.splitlines would also cut a JSON line at a raw U+2028.
    for line in log.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def analyse_worker(
    name: str, entry: dict[str, Any], weights: tuple[float, float, float]
) -> dict[str, Any] | None:
    logs = sorted((JOBS / name).glob("*.jsonl"))
    if not logs:
        return None
    guard, nudge = load_hook("process_guard"), load_hook("ctx_nudge")
    hooks = entry.get("hooks") or {}
    thresholds = nudge.parse_thresholds(str(hooks.get("ctx_nudge", DEFAULT_NUDGE)))
    window = int(hooks.get("loop_window", DEFAULT_LOOP_WINDOW))
    state: dict[str, Any] = {}
    row: dict[str, Any] = {
        "worker": name,
        "label": entry.get("label", ""),
        "model": entry.get("model", "?"),
        "effort": entry.get("effort", "?"),
        "status": entry.get("status", "?"),
        "created": entry.get("created", ""),
        "guarded": bool(hooks),
        "mcp": entry.get("mcp", "default"),
        "turns": len(logs),
        "calls": 0,
        "first_call": None,
        "input_eq": 0.0,
        "cost_usd": None,
        "resume_misses": 0,
        "call_misses": 0,
        "miss_write": 0,
        "blocked": 0,
        "would_block": 0,
        "nudges": 0,
        "loops": 0,
    }
    previous = 0
    w_in, w_read, w_write = weights
    for index, log in enumerate(logs):
        seen: set[str] = set()
        call_no = 0
        for event in read_events(log):
            kind = event.get("type")
            if kind == "result" and event.get("total_cost_usd") is not None:
                # total_cost_usd is cumulative over a resumed session (sonnet-r239h3: 0.55, then 0.72 for a 0.17 turn).
                row["cost_usd"] = max(row["cost_usd"] or 0.0, float(event["total_cost_usd"]))
            elif kind == "user":
                content = (event.get("message") or {}).get("content")
                if isinstance(content, list):
                    row["blocked"] += sum(
                        1
                        for c in content
                        if isinstance(c, dict) and BLOCK_MARKER in json.dumps(c.get("content", ""))
                    )
            elif kind == "assistant":
                message = event.get("message") or {}
                message_id = str(message.get("id"))
                if message_id in seen:
                    continue
                seen.add(message_id)
                usage = message.get("usage") or {}
                fresh = int(usage.get("input_tokens") or 0)
                read = int(usage.get("cache_read_input_tokens") or 0)
                write = int(usage.get("cache_creation_input_tokens") or 0)
                context = fresh + read + write
                row["calls"] += 1
                row["input_eq"] += fresh * w_in + read * w_read + write * w_write
                if row["first_call"] is None:
                    row["first_call"] = context
                if previous and read + MISS_TOKENS < previous:
                    row["resume_misses" if call_no == 0 and index > 0 else "call_misses"] += 1
                    row["miss_write"] += write
                previous = context
                call_no += 1
                for part in message.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        replay_call(row, part, context, guard, nudge, thresholds, window, state)
    return row


def replay_call(
    row: dict[str, Any],
    part: dict[str, Any],
    context: int,
    guard: ModuleType,
    nudge: ModuleType,
    thresholds: list[int],
    window: int,
    state: dict[str, Any],
) -> None:
    name = str(part.get("name"))
    raw_input = part.get("input")
    tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    command = tool_input.get("command")
    if (
        name in ("Bash", "PowerShell")
        and isinstance(command, str)
        and guard.classify(command, name)
    ):
        row["would_block"] += 1
    row["nudges"] += len(nudge.nudge_lines(thresholds, context, state))
    digest = nudge.call_digest({"tool_name": name, "tool_input": tool_input})
    row["loops"] += len(nudge.loop_lines(window, digest, state, name))


def completed(row: dict[str, Any]) -> bool:
    return row["status"] in ("retired", "idle") and row["first_call"] is not None


def select(
    rows: list[dict[str, Any]],
    since: dt.datetime | None,
    until: dt.datetime | None,
    guarded: str,
    last: int | None,
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        created = parse_ts(row["created"]) if row["created"] else None
        if (since and (created is None or created < since)) or (
            until and (created is None or created >= until)
        ):
            continue
        if guarded != "any" and row["guarded"] != (guarded == "yes"):
            continue
        out.append(row)
    out.sort(key=lambda r: r["created"])
    done = [r for r in out if completed(r)]
    return done[-last:] if last else out


def fmt_k(value: float | int | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.1f}K"


def table(rows: list[dict[str, Any]]) -> list[str]:
    head = (
        "| Worker | Task | Model | Created | Guard | MCP | Turns | Calls | First call | Input-eq | Cost USD "
        "| Resume miss | Call miss | Miss write | Blocked | Would-block | Nudge | Loop |"
    )
    out = [head, "|" + "---|" * 18]
    for r in rows:
        cost = "n/a" if r["cost_usd"] is None else f"{r['cost_usd']:.2f}"
        out.append(
            f"| {r['worker']} | {r['label'][:40]} | {r['model']} | {r['created'][:16]} | "
            f"{'yes' if r['guarded'] else 'no'} | {r['mcp']} | {r['turns']} | {r['calls']} | "
            f"{fmt_k(r['first_call'])} | {fmt_k(r['input_eq'])} | {cost} | {r['resume_misses']} | "
            f"{r['call_misses']} | {fmt_k(r['miss_write'])} | {r['blocked']} | {r['would_block']} | "
            f"{r['nudges']} | {r['loops']} |"
        )
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def med(key: str) -> float | None:
        values = [r[key] for r in rows if r[key] is not None]
        return statistics.median(values) if values else None

    return {
        "workers": len(rows),
        "median_first_call": med("first_call"),
        "median_input_eq": med("input_eq"),
        "median_calls": med("calls"),
        "median_cost_usd": med("cost_usd"),
        "total_input_eq": sum(r["input_eq"] for r in rows),
        "total_cost_usd": sum(r["cost_usd"] or 0.0 for r in rows),
        "resume_misses": sum(r["resume_misses"] for r in rows),
        "call_misses": sum(r["call_misses"] for r in rows),
        "blocked": sum(r["blocked"] for r in rows),
        "would_block": sum(r["would_block"] for r in rows),
        "nudges": sum(r["nudges"] for r in rows),
        "loops": sum(r["loops"] for r in rows),
    }


def summary_lines(title: str, rows: list[dict[str, Any]]) -> list[str]:
    s = summary(rows)
    cost = "n/a" if s["median_cost_usd"] is None else f"{s['median_cost_usd']:.2f}"
    return [
        f"### {title}",
        f"- Workers: {s['workers']}; median first call {fmt_k(s['median_first_call'])}; "
        f"median input-eq {fmt_k(s['median_input_eq'])}; median calls {s['median_calls']}; median cost USD {cost}",
        f"- Totals: input-eq {fmt_k(s['total_input_eq'])}; cost USD {s['total_cost_usd']:.2f}; "
        f"resume misses {s['resume_misses']}; in-turn call misses {s['call_misses']}",
        f"- Guard signals: blocked {s['blocked']}; would-block {s['would_block']}; "
        f"nudges {s['nudges']}; loop warnings {s['loops']}",
    ]


def render(
    groups: list[tuple[str, list[dict[str, Any]]]], weights: tuple[float, float, float]
) -> str:
    out = [
        "# Workflow metrics (read-only)",
        f"Weights: input x{weights[0]}, cache read x{weights[1]}, cache write x{weights[2]}. "
        "Nudge, loop and would-block columns replay the hook rules on the transcripts (see script docstring).",
        "",
    ]
    for title, rows in groups:
        out += [*summary_lines(title, rows), "", *table(rows), ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--last", type=int)
    ap.add_argument("--guarded", choices=["any", "yes", "no"], default="any")
    ap.add_argument("--split", help="print the before and after groups around this timestamp")
    ap.add_argument("--read-weight", type=float, default=0.1)
    ap.add_argument("--write-weight", type=float, default=2.0)
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.last is not None and a.last < 1:
        raise SystemExit("--last must be at least 1")
    weights = (1.0, a.read_weight, a.write_weight)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8-sig")) if REGISTRY.exists() else {}
    rows = [r for n, e in registry.items() if (r := analyse_worker(n, e, weights)) is not None]
    since = parse_ts(a.since) if a.since else None
    until = parse_ts(a.until) if a.until else None
    if a.split:
        cut = parse_ts(a.split)
        groups = [
            (f"Before {a.split}", select(rows, since, cut, a.guarded, a.last)),
            (
                f"From {a.split}",
                [r for r in select(rows, cut, until, a.guarded, None) if completed(r)],
            ),
        ]
    else:
        groups = [("Selected", select(rows, since, until, a.guarded, a.last))]
    if a.json:
        text = json.dumps({t: {"summary": summary(r), "workers": r} for t, r in groups}, indent=1)
    else:
        text = render(groups, weights)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
