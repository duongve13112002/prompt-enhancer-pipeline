"""Status-line command that records the subscription quota for usage_snapshot.py (T-026). README.md, "Codex workers".

  <venv python> -B -I statusline.py --source claude|antigravity [--config <codex_profiles.json>]

Claude Code and Antigravity pipe one JSON object to the status-line command on every update (Claude Code:
code.claude.com/docs/en/statusline; Antigravity: antigravity.google/docs/cli/statusline). This script stores the quota
part verbatim with the time it was read and prints one short line:
- `--source claude`: the `rate_limits` object goes to `usage.claude_statusline_file`. A payload without `rate_limits`
  (or with null; before the session's first API response, or a plan without subscription limits) writes nothing, so a
  later empty start never hides an earlier reading; `rate_limits: {}` is written (every window dropped after reset).
- `--source antigravity`: the `quota` object (bucket id -> remaining fraction or amount, reset time) goes to
  `usage.antigravity_statusline_file` under the same rule.

The input and the stored document are judged by the validator of the stored quota format in usage_snapshot.py
(`decode_object`, `stored_status`, the window validators), the one the snapshot reads the file with, so the line never
says more than a reader of the file will find: each malformed window named as `unreadable` with its reason (first,
never cut), then each valid window with its value (a long line drops whole entries from the end and ends with
`+N more`), and "no limit window" / "no quota bucket" only for a valid empty object. Input that is
not one JSON object is `input unreadable`, never "no rate_limits yet", which is kept for a member that is absent.

The write is atomic (temp file and replace). Claude Code cancels a slow status-line run, so the script does no network
or subprocess work. It never fails the host: any problem is printed as the status line and the exit code is 0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Protocol-bound: the quota member of each CLI's status-line payload (sources in the docstring), the setting that
# names its file, the snapshot's validator of its windows and the prefix of the printed line.
SOURCES = {
    "claude": ("rate_limits", "claude_statusline_file", "claude_status_windows", "claude"),
    "antigravity": ("quota", "antigravity_statusline_file", "antigravity_windows", "agy"),
}
# Layout of the printed line, not a deployment choice: a status line is one short row. A malformed window's
# diagnostic is exempt (printed first, never cut); only the valid entries give way, whole, with `+N more`.
LINE_CHARS = 160


def snapshot_module() -> Any:
    name = "usage_snapshot"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def window_text(source: str, name: str, entry: dict[str, Any], no_usage: str) -> tuple[str | None, str | None]:
    """(what to print for one validated window, None) or (None, why it is unreadable). `entry` is the window as the
    snapshot's validator reads its shape (`usage_snapshot.window_shapes`)."""
    used = entry.get("used_percent")
    if entry["state"] == "disabled":
        return f"{name} disabled", None
    if used is not None:
        return (f"{name} {used:.0f}% used" if source == "claude" else f"{name} {100 - used:.0f}% left"), None
    amount = entry.get("remaining_amount")
    if amount is not None and entry.get("reason") == no_usage:  # a well-formed bucket counted in units, not percent
        return f"{name} {amount:g} left", None
    return None, f"{name} ({entry.get('reason')})"


def fitted(prefix: str, diagnostic: str, shown: list[str]) -> str:
    """`<prefix> unreadable: <diagnostic>; <valid entries>` within LINE_CHARS: whole valid entries are dropped from
    the end and the line ends with `+N more` (N dropped). The diagnostic is never cut or dropped, even when it alone
    exceeds the limit (then no entry is kept), so the width never hides a malformed state; no entry is cut in the
    middle."""
    head = f"{prefix} " + (f"unreadable: {diagnostic}" if diagnostic else "")
    if not shown:
        return head
    sep = "; " if diagnostic else ""
    line = head + sep + ", ".join(shown)
    if len(line) <= LINE_CHARS:
        return line
    for keep in range(len(shown) - 1, 0, -1):  # the longest run of entries that fits together with its count
        line = head + sep + ", ".join([*shown[:keep], f"+{len(shown) - keep} more"])
        if len(line) <= LINE_CHARS:
            return line
    return head + sep + f"+{len(shown)} more"


def quota_line(source: str, member: dict[str, Any], snap: Any) -> str:
    """The printed line for a stored quota object: the unreadable windows first, each named and never cut, then the
    valid ones with their values (whole entries give way when the line is long). The text for "no window" is
    printed only when the object is empty, never because nothing in it could be read."""
    _, _, parser, prefix = SOURCES[source]
    shown, unreadable = [], []
    for name, entry in snap.window_shapes(member, getattr(snap, parser)).items():
        text, problem = window_text(source, name, entry, snap.NO_USAGE_VALUE)
        (shown if problem is None else unreadable).append(text if problem is None else problem)
    if not member:
        return f"{prefix} " + ("no limit window" if source == "claude" else "no quota bucket")
    return fitted(prefix, ", ".join(unreadable), shown)


def clip(line: str) -> str:
    """A line that is not the quota line (a refusal, a reason) is one short row; the quota line fits itself."""
    return line[:LINE_CHARS]


def record(source: str, payload: object, config: Path | None, now: dt.datetime) -> str:
    """Write the quota member of `payload` (when present) and return the line to print."""
    snap = snapshot_module()
    settings = snap.sibling("codex_backend").load_usage_settings(config)
    member, setting, _, _ = SOURCES[source]
    if not isinstance(payload, dict):
        return clip(f"{source}: input unreadable: not a JSON object")
    # Absent or null: no reading (the docs name no null form), so the last reading stays.
    if payload.get(member) is None:
        return clip(f"{source}: no {member} yet")
    value = payload[member]
    if not isinstance(value, dict):
        return clip(f"{source}: {member} is not an object, not recorded")
    path = snap.state_path(settings[setting], snap.ROOT, create=True)  # under .tmp/pm-state, no link on the way
    text = json.dumps({"written_at": now.isoformat(), member: value}, ensure_ascii=False, indent=1) + "\n"
    # The bytes about to be stored, through the validator every reader of the file uses.
    stored = snap.stored_status(text.encode("utf-8"), member)
    if stored.tag != snap.VALID:
        return clip(f"{source}: not recorded: {'; '.join(stored.problems)}")
    snap.atomic_write_text(path, text)
    return quota_line(source, stored.member, snap)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--config", type=Path, help="codex_profiles.json (default: the tracked file)")
    args = ap.parse_args(argv)
    try:
        # The input through the stored format's decoder: blank or broken input is malformed, not an absent member.
        document = snapshot_module().decode_object(sys.stdin.buffer.read())
        if isinstance(document, str):
            line = clip(f"{args.source}: input unreadable: {document}")
        else:
            line = record(args.source, document.data, args.config, dt.datetime.now(dt.UTC))
    except (Exception, SystemExit) as exc:  # a refused config or a failed read or write is shown, never raised
        line = clip(f"quota not recorded: {exc}")
    # ASCII only: the host decodes the script's stdout with the console code page on Windows (one "?" per character,
    # so the length the line was fitted to does not change).
    print(line.encode("ascii", errors="replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
