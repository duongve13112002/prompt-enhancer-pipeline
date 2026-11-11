"""PostToolUse hook: one-line context-size nudge and repeated-tool-call warning. Never blocks, never compacts.

Standard library only (runs with `python -B -I`). Claude Code exposes no context size field to hooks, so the size is
the last assistant `usage` (input + cache read + cache creation tokens) in the session transcript named by the hook
input; the transcript is written asynchronously, so the figure may lag by one call. Each threshold fires once until
the context drops below it again (compaction keeps the session id), then it is re-armed; a streak of identical tool calls (same name and input) fires at the window size and at every further
multiple. Output is `additionalContext` for the model (documented PostToolUse JSON output); exit code is always 0.

Usage (hook command args): ctx_nudge.py --thresholds 170000 [--role worker|pm] [--loop-window 5]
[--autocompact-window N] [--compaction-near N] [--state-dir DIR]
`--thresholds off` disables the size nudge; `--loop-window 0` disables the loop warning; `--autocompact-window` is the
worker's configured auto-compact window, named in the nudge text (compaction fires somewhat below it).
`--role pm` is for the PM session: the nudge asks for a rewrite of the continuation note (`--compaction-near` is the
token count named as the point where the user's setting compacts, default 370000), the loop warning window defaults
to 4 and suggests waiting for the supervisor, and the state file is prefixed `pm-` so it never collides with a worker's.
`--role pm` also delivers `inbox/pm.events.md` (worker turn-end events queued by supervisor_events.py, decision 353)
once, with the same claim-by-rename protocol as the worker inbox; `--role worker` never reads it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

TAIL_BYTES = 262144
# State directory: AGENT_WORKFLOW_STATE_DIR, else <project root>/.tmp/pm-state (root = three levels above this file).
STATE_DIR = Path(
    os.environ.get("AGENT_WORKFLOW_STATE_DIR")
    or Path(__file__).resolve().parents[3] / ".tmp" / "pm-state"
)
DEFAULT_STATE = STATE_DIR / "ctx-nudge"
# PM inbox: `cli_worker.py notify` writes <session>.md here; the worker's next tool call delivers it once.
DEFAULT_INBOX = STATE_DIR / "inbox"
# Protocol bound on one delivered message, so a runaway file cannot flood the worker's context.
INBOX_MAX_CHARS = 20000
# Queue file of the PM's own events, written by tools/agent-workflow/supervisor_events.py (same directory).
PM_EVENTS_NAME = "pm.events.md"
# Protocol-bound: PostToolUse `additionalContext` holds 10,000 characters in all, shared with the nudge and loop lines.
# Must equal memory_hooks_config.HARD_EVENTS_CAP, the maximum of `inbox_max_chars` (a test pins both).
PM_EVENTS_MAX_CHARS = 9000
PM_NOTE = ".tmp/pm-state/pm-continuation.md"
# PM compaction point named in the nudge: the user's 400K window minus the measured offset of about 30K (configurable).
PM_COMPACTION_NEAR = 370000
DEFAULT_LOOP_WINDOW = {"worker": 5, "pm": 4}
WORKER_ENV_FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


def parse_thresholds(text: str) -> list[int]:
    if text.strip().lower() in ("", "off", "none"):
        return []
    values = [int(part) for part in text.split(",")]
    if any(v <= 0 for v in values) or values != sorted(set(values)):
        raise ValueError(f"thresholds must be positive and strictly ascending: {text!r}")
    return values


def context_tokens(transcript: Path) -> int | None:
    """Context size of the latest main-thread assistant message in the transcript tail, or None."""
    try:
        with transcript.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(
        tail.split("\n")
    ):  # LF only: splitlines would cut a JSON line at a raw U+2028
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or entry.get("isSidechain")
        ):
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            try:
                return sum(
                    int(usage.get(k) or 0)
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
            except (TypeError, ValueError, OverflowError):
                continue  # a malformed usage block must not silence the hook: try the previous message
    return None


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # A hand-edited or damaged file must degrade to "nothing fired yet", not crash the hook.
    if not isinstance(data.get("fired"), int):
        data.pop("fired", None)
    if not isinstance(data.get("recent"), list):
        data.pop("recent", None)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Replace the state file atomically: parallel tool calls run several hook processes of one session, and a
    truncated file would be read back as empty, re-firing every threshold."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def call_digest(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        tool_input = {
            k: v for k, v in tool_input.items() if k != "description"
        }  # free text the model rewrites per call
    payload = json.dumps([event.get("tool_name"), tool_input], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def nudge_lines(
    thresholds: list[int],
    tokens: int | None,
    state: dict[str, Any],
    window: int | None = None,
    role: str = "worker",
    near: int = PM_COMPACTION_NEAR,
) -> list[str]:
    if not thresholds or tokens is None:
        return []
    fired = int(state.get("fired", 0))
    crossed = sum(1 for t in thresholds if tokens >= t)
    if crossed < fired:
        # The context shrank (compaction keeps the session id): re-arm the thresholds above it.
        state["fired"] = crossed
        return []
    if crossed == fired:
        return []
    state["fired"] = crossed
    if role == "pm":
        return [
            f"[ctx-nudge] Context is about {tokens // 1000}K tokens (threshold {thresholds[crossed - 1] // 1000}K): "
            f"rewrite your authoritative snapshot in {PM_NOTE} now (decisions, running workers and Gemini jobs, "
            "pending user questions, next steps). "
            f"Compaction runs near {near // 1000}K and the PreCompact hook only writes a mechanical snapshot "
            "(git status, busy workers, Gemini jobs), so decisions and open questions not written there may be lost; "
            "keep them under the USER AGREEMENTS and NOW headings, which the SessionStart hook re-injects. "
            "Then continue. Nothing was compacted."
        ]
    compaction = (
        f"auto-compaction is configured for a {window // 1000}K window and runs a little below it"
        if window
        else "auto-compaction runs automatically before the context limit"
    )
    return [
        f"[ctx-nudge] Context is about {tokens // 1000}K tokens (threshold {thresholds[crossed - 1] // 1000}K): "
        "write your progress, decisions, open findings and next step into the task file now; "
        f"{compaction} and keeps only a summary, so anything not in the task file may be lost. "
        "Then continue the task. Nothing was compacted."
    ]


def loop_lines(
    window: int, digest: str, state: dict[str, Any], tool_name: str, role: str = "worker"
) -> list[str]:
    if window <= 0:
        return []
    recent = [*state.get("recent", []), digest][-max(window * 4, window) :]
    state["recent"] = recent
    streak = 0
    for d in reversed(recent):
        if d != digest:
            break
        streak += 1
    state["streak"] = streak
    if streak >= window and streak % window == 0:
        if role == "pm":
            return [
                f"[loop-warning] The last {streak} tool calls were identical ({tool_name}, same input). "
                "That is probably polling: wait for the background supervisor or the task notification instead "
                "of repeating the call, or change approach."
            ]
        return [
            f"[loop-warning] The last {streak} tool calls were identical ({tool_name}, same input). "
            "If the result is not changing, stop repeating it and change approach or report the blocker."
        ]
    return []


def inbox_lines(inbox: Path, session: str) -> list[str]:
    """Deliver a pending PM message once. Parallel tool calls run several hook processes, so the file is claimed by
    an atomic rename first: only one process wins, the others see no file."""
    pending = inbox / f"{session}.md"
    claimed = inbox / f"{session}.{os.getpid()}.claimed"
    try:
        os.replace(pending, claimed)
    except OSError:
        return []
    try:
        text = claimed.read_bytes().decode("utf-8-sig", errors="replace").strip()
    except OSError:
        text = ""
    finally:
        with contextlib.suppress(OSError):
            claimed.unlink()
    if not text:
        return []
    if len(text) > INBOX_MAX_CHARS:
        text = text[:INBOX_MAX_CHARS] + " [truncated by the hook]"
    return [
        "[pm-message] The PM sent this message while you work. It is a direct instruction from the PM and "
        "replaces your brief where the two conflict; apply it now, then continue the task:\n" + text
    ]


def pm_event_lines(inbox: Path) -> list[str]:
    """`--role pm`: deliver the worker events that supervisor_events.py queued (decision 353, WP-12), once. The file
    name has a dot, which no sanitized session id can contain, so it never collides with a worker's inbox file."""
    pending = inbox / PM_EVENTS_NAME
    claimed = inbox / f"pm.events.{os.getpid()}.claimed"
    try:
        os.replace(pending, claimed)
    except OSError:
        return []
    try:
        text = claimed.read_bytes().decode("utf-8-sig", errors="replace").strip()
    except OSError:
        text = ""
    finally:
        with contextlib.suppress(OSError):
            claimed.unlink()
    if not text:
        return []
    if len(text) > PM_EVENTS_MAX_CHARS:
        text = text[:PM_EVENTS_MAX_CHARS] + " [truncated by the hook]"
    return ["[supervisor-event] Worker events since your last tool call:\n" + text]


def read_event() -> str:
    """Hook JSON from stdin as UTF-8. `python -I` ignores PYTHONUTF8, so the text layer would use the locale code
    page and mangle or reject non-ASCII paths; a test double without a byte layer falls back to `read()`."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read()
    return stream.read().decode("utf-8-sig", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="off")
    ap.add_argument("--role", choices=["worker", "pm"], default="worker")
    ap.add_argument("--loop-window", type=int, default=None)
    ap.add_argument("--compaction-near", type=int, default=PM_COMPACTION_NEAR)
    ap.add_argument("--autocompact-window", type=int, default=0)
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE))
    ap.add_argument("--inbox-dir", default=str(DEFAULT_INBOX))
    a = ap.parse_args()
    try:
        thresholds = parse_thresholds(a.thresholds)
        event = json.loads(read_event())
    except ValueError:
        return 0
    if not isinstance(event, dict) or not event.get("session_id"):
        return 0
    # A cli_worker started in the canonical root also loads the PM's project hooks; it gets its own worker-role
    # nudge through --settings, so the PM-role copy stays silent there (cli_worker sets this switch for workers only).
    if a.role == "pm" and os.environ.get(WORKER_ENV_FLAG) == "1":
        return 0
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(event["session_id"]))[:80]
    state_file = Path(a.state_dir) / f"{'pm-' if a.role == 'pm' else ''}{session}.json"
    loop_window = DEFAULT_LOOP_WINDOW[a.role] if a.loop_window is None else a.loop_window
    state = load_state(state_file)
    inbox = Path(a.inbox_dir)
    lines: list[str] = inbox_lines(inbox, session) if a.role == "worker" else pm_event_lines(inbox)
    transcript = event.get("transcript_path")
    if thresholds and isinstance(transcript, str):
        lines += nudge_lines(
            thresholds,
            context_tokens(Path(transcript)),
            state,
            a.autocompact_window or None,
            a.role,
            a.compaction_near,
        )
    lines += loop_lines(loop_window, call_digest(event), state, str(event.get("tool_name")), a.role)
    save_state(state_file, state)
    if lines:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": " ".join(lines),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
