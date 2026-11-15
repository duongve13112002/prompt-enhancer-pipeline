"""Usage snapshot of the CLI agents and the per-turn ledger (decision 596). Documentation: README.md, "Codex workers".

  python -B usage_snapshot.py [--config <codex_profiles.json>] [--codex-home <dir>] [--claude-file <PM-written json>]
  python -B usage_snapshot.py pm-set [--config <file>] [--block PROVIDER WINDOW] --text|--json NAME VALUE ... (pm-set -h)

Prints one JSON object: `collected_at`, `claude`, `codex`, `antigravity`, `quota_blocks`. A provider is `ok` only when
at least one window holds a present, schema-valid, finite and in-range value (percent 0..100, fraction 0..1, epoch
seconds a date can hold); a fresh source that validly reports no limit window is `{"status": "ok", "unlimited": true}`
(decision 44); anything else (absent, unreadable, stale, malformed) is `{"status": "unknown", "reason": ...}`, never 0.
Every window has the same shape (`label`, `window_minutes`, `used_percent`, `resets_at`, `state`, `fresh`, `source`,
`blocked`): it is valid until its `resets_at` and counts as 0% (`state: reset`) after it; only a window without
`resets_at` needs a reading within `usage.stale_minutes`; a malformed window is `state: unknown` with a `reason`.
`blocked` compares the used percent with the provider's line in `usage.block_percent` (S32, decisions 44 and 46).

- Codex: the newest `token_count` row by its own timestamp across the recent session files (file mtime only orders the
  scan); windows are named by their `window_minutes`, never by position.
- Claude: per window, the newest reading of the status-line file (`usage.claude_statusline_file`, written by
  statusline.py in the terminal CLI) and of the last `rate_limit_event` in the newest worker turn logs (.tmp/cli-jobs);
  each window names its source. `--claude-file` replaces both with a PM-written file passed through as before.
- Antigravity: the status-line file (`usage.antigravity_statusline_file`, the `quota` object per bucket). The optional
  `usage.antigravity_command` (`-p /usage --output-format json`, no shell) is passed through untouched (a JSON object as
  `data`, other text as `report`); agy 1.5.9 sends that slash command to the model, so it spends quota (T-026 probe).

Every source passes `observe`: one reading becomes an `Observation` (source, observation time not after now, windows)
or is refused; per window the newest observation wins.

The stored quota files have one validator, used by every consumer (this command, `pm-set`, the SessionStart hook,
statusline.py): `stored_blocks` for `usage.quota_blocks_file` and `stored_status` for the status-line files, both
over `decode_object` (UTF-8, strict JSON, one object). Its result is VALID, ABSENT (no file) or MALFORMED with one
reason per unreadable part; blank bytes, a repeated member name, `NaN`, or an entry of `blocks` that is no object
are malformed, never absent. A malformed file is an error for the writers (nothing written, exit 1, repaired by
hand) and an `unreadable` or `refused` finding for the readers, which still show its valid entries.

Only the command line writes `usage.quota_blocks_file`, always through `transact`: the snapshot keeps its `blocks`
list in step with the blocked windows and leaves every byte outside that value as it was; `pm-set` is the PM's
writer for the PM fields and the PM's own blocks and writes the document again canonically (equal values, not
equal spellings). The file keeps one name; a commit replaces it atomically, and every read and write
of this tool holds `<file>.lock` (`locked`), so a reader gets the file, a proven absence, or an error, never a guess
(`read_quota_file`). Each entry of `blocks` moves by one table (`block_step`): it leaves when its own reset passes
or through a reading observed after it, of the same or a later cycle; an `unknown` reading never clears one. Every
ISO time is read by `parse_time` (a UTC offset is required) and written in UTC. Every quota file path goes through
`state_path` (under `.tmp/pm-state/`, no link). `collect` has no side effects. The ledger helpers write one JSONL
row per worker turn (cli_worker.py calls them).

Exit codes of the snapshot: 0 with the JSON; 1 with the JSON when the quota-blocks file is malformed or could not be
read or written (`quota_blocks.status` is `error`: the recorded blocks are then unknown, not empty); 2 for refused
arguments; a refused configuration or path exits with its message before anything is read.
"""

from __future__ import annotations

import argparse
import datetime as dt
import heapq
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# Protocol-bound maximum tail bytes decoded from one session file; candidate count lives in usage config.
TAIL_BYTES = 1_048_576
WEEKLY_MINUTES = 10080
# Worker turn logs (artifact-bound: the same folder as cli_worker.JOBS, asserted by a test).
JOBS = ROOT / ".tmp" / "cli-jobs"
# Protocol-bound names of the Claude status line's windows and their lengths (code.claude.com/docs/en/statusline:
# "rate_limits.five_hour", "rate_limits.seven_day"); any other window (`spend_limit`) keeps its own name.
CLAUDE_WINDOW_MINUTES = {"five_hour": 300, "seven_day": WEEKLY_MINUTES}
# Protocol-bound marker of the worker stream event (Agent SDK `SDKRateLimitEvent`): a cheap pre-filter before parsing.
RATE_LIMIT_EVENT = "rate_limit_event"
# Windows short replace retries (another process holding the target for a moment): protocol-bound platform mechanics.
REPLACE_ATTEMPTS = 5
REPLACE_PAUSE_SECONDS = 0.05
# Artifact-bound: the lock file beside the quota-blocks file that serialises its readers and writers, and the temp
# file a commit writes before it replaces the stable name (see `locked`, `transact`).
LOCK_SUFFIX = ".lock"
TEMP_SUFFIX = ".tmp"
# Safety-bound: the critical section is a few milliseconds of local file work; the bound only stops a hung lock holder
# from hanging a writer or the SessionStart reader (a busy lock is reported, the file left as it is).
LOCK_WAIT_SECONDS = 5.0


def sibling(name: str, folder: Path = HERE) -> Any:
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {folder / (name + '.py')}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def unknown(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason, **extra}


def window_label(minutes: int) -> str:
    """Name derived from the window length itself (300 -> 5h, 10080 -> weekly), never from its position."""
    if minutes == WEEKLY_MINUTES:
        return "weekly"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)  # protocol-bound: the origin of epoch seconds


def iso(stamp: float) -> str | None:
    """Epoch seconds as UTC ISO text. Arithmetic on the epoch, not `fromtimestamp`, whose range is the C library's
    (Windows stops at year 3001), so the same stamps are accepted on every OS: up to the end of year 9999."""
    try:
        return (UNIX_EPOCH + dt.timedelta(seconds=stamp)).isoformat()
    except (OverflowError, ValueError):
        return None


def parse_time(value: object) -> float | None:
    """The one parser of every ISO 8601 time that enters the tool (source files, the quota-blocks file, `pm-set`, the
    SessionStart hook): UTC epoch seconds of a text that carries its UTC offset (`Z` or `+hh:mm`), else None. A text
    without an offset (a date, a local time) is refused, because `datetime.timestamp()` would read it in the zone of
    whatever host runs the tool."""
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.utcoffset() is None:
            return None
        stamp = (moment - UNIX_EPOCH).total_seconds()
    except (ValueError, OverflowError):
        return None
    return stamp if iso(stamp) is not None else None


def bounded(value: object, low: float, high: float | None) -> float | None:
    """A JSON number (never a bool) that is finite and within [low, high] (no upper bound when `high` is None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        found = float(value)
    except OverflowError:
        return None
    if not math.isfinite(found) or found < low or (high is not None and found > high):
        return None
    return found


def epoch(value: object) -> float | None:
    """Epoch seconds that a UTC datetime can hold. A millisecond stamp of this century lies beyond year 9999, so it is
    refused instead of being read as a reset thousands of years away."""
    stamp = bounded(value, 0, None)
    return stamp if stamp is not None and iso(stamp) is not None else None


def field(window: Mapping[str, Any], name: str, parse: Any) -> tuple[float | None, str | None]:
    """(value, problem) of one window field: absent or null is (None, None); present but refused by `parse` (wrong type,
    not finite, out of range) is (None, problem), so a malformed value is never read as a missing one."""
    raw = window.get(name)
    if raw is None:
        return None, None
    value = parse(raw)
    return value, None if value is not None else f"invalid {name}: {json.dumps(raw)[:40]}"


def percent_0_100(value: object) -> float | None:
    return bounded(value, 0, 100)


def fraction_0_1(value: object) -> float | None:
    return bounded(value, 0, 1)


def amount_0_up(value: object) -> float | None:
    return bounded(value, 0, None)


# The reason of a window whose fields are all well formed but give no used percent (an Antigravity bucket that
# reports only a positive `remaining_amount` is one): the status line tells it from a malformed window by this value.
NO_USAGE_VALUE = "no usage value"


def window_entry(
    label: str,
    minutes: int | None,
    used: float | None,
    resets: float | None,
    now: float,
    fresh: bool,
    problem: str | None = None,
) -> dict[str, Any]:
    """One window in the common shape. Valid until `resets` and 0% (`state: reset`) after it (a reading moves only
    when the provider is used, PM note 2026-11-07); without `resets` it is trusted only while the reading is `fresh`.
    A `problem` (a malformed field) or a missing value makes the window `unknown`, which never clears a block."""
    entry: dict[str, Any] = {"label": label, "window_minutes": minutes, "fresh": fresh}
    if resets is not None:
        entry["resets_at"] = iso(resets)
    if problem is not None:
        entry.update(state="unknown", used_percent=None, reason=problem)
    elif used is None:
        entry.update(state="unknown", used_percent=None, reason=NO_USAGE_VALUE)
    elif resets is not None and resets < now:
        entry.update(
            state="reset", used_percent=0.0, reason="window reset after the reading was taken"
        )
    elif resets is None and not fresh:
        entry.update(
            state="unknown", used_percent=None, reason="no reset time and the reading is stale"
        )
    else:
        entry.update(state="ok", used_percent=used)
    return entry


# Protocol-bound: the window slots of a Codex `rate_limits` object, each a window object or null (live row,
# 2026-11-15: `"primary":{"used_percent":26.0,"window_minutes":10080,"resets_at":...},"secondary":null,"credits":{...}`).
CODEX_WINDOW_SLOTS = ("primary", "secondary")


def windows_of(limits: Mapping[str, Any], now: float, fresh: bool = True) -> list[dict[str, Any]]:
    """Every window present in a Codex `rate_limits` object, none invented, named by its `window_minutes`. A window
    slot (`primary`, `secondary`) holding anything but null is a window and is type-checked: a list, bool, string,
    number or an object without valid fields is an `unknown` window under its slot name, so it can never read as "no
    limit". Another member is a window when it carries `window_minutes` or `used_percent` (`credits` is not). Only a
    window without `resets_at` is trusted while the row is `fresh` (age within `usage.stale_minutes`)."""
    found = []
    for key in sorted(limits):
        window = limits[key]
        if key in CODEX_WINDOW_SLOTS:
            if window is None:
                continue
            if not isinstance(window, dict):
                found.append(window_entry(str(key), None, None, None, now, fresh, "window is not an object"))
                continue
        elif not isinstance(window, dict) or ("window_minutes" not in window and "used_percent" not in window):
            continue
        minutes = window.get("window_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            found.append(window_entry(str(key), None, None, None, now, fresh, "invalid window_minutes"))
            continue
        used, bad_used = field(window, "used_percent", percent_0_100)
        resets, bad_reset = field(window, "resets_at", epoch)
        found.append(window_entry(window_label(minutes), minutes, used, resets, now, fresh, bad_used or bad_reset))
    return found


def claude_status_windows(limits: Mapping[str, Any], now: float, fresh: bool) -> list[dict[str, Any]]:
    """Windows of the Claude status line's `rate_limits` (`used_percentage` 0-100, `resets_at` epoch seconds); a
    member that is not an object, or a field out of range, is an `unknown` window under its name."""
    found = []
    for key in sorted(limits):
        window = limits[key]
        minutes = CLAUDE_WINDOW_MINUTES.get(key)
        label = window_label(minutes) if minutes else str(key)
        if not isinstance(window, dict):
            found.append(window_entry(label, minutes, None, None, now, fresh, "window is not an object"))
            continue
        used, bad_used = field(window, "used_percentage", percent_0_100)
        resets, bad_reset = field(window, "resets_at", epoch)
        found.append(window_entry(label, minutes, used, resets, now, fresh, bad_used or bad_reset))
    return found


# Protocol-bound: `SDKRateLimitEvent.status` values (code.claude.com/docs/en/agent-sdk/typescript).
EVENT_STATUSES = ("allowed", "allowed_warning", "rejected")
# Protocol-bound: the top-level fields of `rate_limit_info` that describe the window named by `rateLimitType`.
EVENT_FIELDS = ("status", "resetsAt", "utilization")


def rate_limit_windows(info: object, now: float, fresh: bool) -> list[dict[str, Any]]:
    """Windows of a worker's `rate_limit_event.rate_limit_info`.

    `resetsAt` is epoch seconds and `utilization` a fraction 0..1 (T-026 experiment: live `resetsAt` 1795010400 is the
    weekly reset Claude Desktop showed, 2026-11-18T14:00Z; `utilization` rose 0.08 -> 0.15 over one day); the
    window name comes from the undocumented `unifiedWindows` map or `rateLimitType` field, so an event without a
    name gives no window. The top-level fields describe the `rateLimitType` window: they fill what its
    `unifiedWindows` entry leaves out, and a top-level `status: rejected` holds for it. `rejected` counts as fully
    used."""
    if not isinstance(info, dict):
        return []
    named: dict[str, Any] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        named.update(unified)
    kind = info.get("rateLimitType")
    if isinstance(kind, str):
        top = {name: info[name] for name in EVENT_FIELDS if name in info}
        own = named.get(kind)
        if own is None:
            named[kind] = top
        elif isinstance(own, dict):
            named[kind] = {**top, **own}
            if top.get("status") == "rejected":
                named[kind]["status"] = "rejected"
    found = []
    for key in sorted(named):
        window = named[key]
        minutes = CLAUDE_WINDOW_MINUTES.get(key)
        label = window_label(minutes) if minutes else str(key)
        if not isinstance(window, dict):
            found.append(window_entry(label, minutes, None, None, now, fresh, "window is not an object"))
            continue
        status = window.get("status")
        bad_status = None if status is None or status in EVENT_STATUSES else f"invalid status: {str(status)[:40]}"
        fraction, bad_used = field(window, "utilization", fraction_0_1)
        resets, bad_reset = field(window, "resetsAt", epoch)
        used = None if fraction is None else round(fraction * 100, 2)
        if status == "rejected":
            used = 100.0
        found.append(window_entry(label, minutes, used, resets, now, fresh, bad_status or bad_used or bad_reset))
    return found


def antigravity_windows(quota: Mapping[str, Any], now: float, fresh: bool) -> list[dict[str, Any]]:
    """Windows of the Antigravity status line's `quota` (bucket id -> `remaining_fraction` 0..1 or `remaining_amount`
    0 or more, `reset_time` ISO, `disabled`). The bucket id is the label, verbatim; a `remaining_amount` gives no
    percent, only `remaining_amount`, and counts as fully used at 0. A malformed bucket is `unknown`."""
    found = []
    for bucket in sorted(quota):
        info = quota[bucket]
        if not isinstance(info, dict):
            found.append(window_entry(str(bucket), None, None, None, now, fresh, "bucket is not an object"))
            continue
        fraction, bad_fraction = field(info, "remaining_fraction", fraction_0_1)
        amount, bad_amount = field(info, "remaining_amount", amount_0_up)
        resets, bad_reset = field(info, "reset_time", parse_time)
        disabled = info.get("disabled")
        bad_disabled = None if disabled is None or isinstance(disabled, bool) else "invalid disabled"
        used = None if fraction is None else round((1 - fraction) * 100, 2)
        if used is None and amount is not None and amount <= 0:
            used = 100.0
        problem = bad_fraction or bad_amount or bad_reset or bad_disabled
        entry = window_entry(str(bucket), None, used, resets, now, fresh, problem)
        if amount is not None:
            entry["remaining_amount"] = amount
        if disabled is True and problem is None:
            entry.update(state="disabled", used_percent=None)
        found.append(entry)
    return found


def window_shapes(
    member: Mapping[str, Any], parse: Callable[[Mapping[str, Any], float, bool], list[dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    """Each window of a stored quota object (`Stored.member`) judged by its shape alone, name -> entry of the
    source's window validator `parse`: read as fresh and before any reset, because whether a reset has passed is
    the snapshot's question at its own time, not the format's. A malformed window is `state: unknown` with its
    `reason`. statusline.py prints from this, so its line and the snapshot judge a window by the same rule."""
    return {name: parse({name: member[name]}, -math.inf, True)[0] for name in sorted(member)}


# Window states that hold a valid reading; `unknown` does not.
KNOWN_STATES = ("ok", "reset", "disabled")


class Observation(NamedTuple):
    """One reading of one source, validated as a whole by `observe`: where it came from, when it was observed (epoch
    seconds, never after the snapshot's `now`), whether that is within `usage.stale_minutes`, its windows (each
    carrying `source` and `observed_at`), and whether it validly states that no limit window applies."""

    source: str
    at: float
    fresh: bool
    windows: list[dict[str, Any]]
    no_limit: bool


def observe(
    source: str,
    at: float | None,
    now: float,
    stale_minutes: int,
    read: Callable[[bool], tuple[list[dict[str, Any]], bool]],
) -> Observation | str:
    """The one gate every source passes: raw input becomes an `Observation`, or a reason it is none. The observation
    time must be epoch seconds a date can hold and not later than `now`: a future time (a wrong clock, a restored
    file) is refused, because a reading from the future would outrank every real one and could clear a block.
    `read(fresh)` turns the payload into (windows, states_no_limit); "no limit" counts only for a fresh reading with
    no window at all."""
    if at is None or iso(at) is None:
        return f"{source}: no valid observation time"
    if at > now:
        return f"{source}: observed at {iso(at)}, after now ({iso(now)}): refused"
    fresh = (now - at) / 60 <= stale_minutes
    windows, states_no_limit = read(fresh)
    stamp = iso(at)
    tagged = [{**w, "source": source, "observed_at": stamp} for w in windows]
    return Observation(source, at, fresh, tagged, states_no_limit and fresh and not windows)


def provider_from(
    observations: list[Observation], refused: list[str], reason: str, **extra: Any
) -> dict[str, Any]:
    """A provider from its valid observations: per window the newest observation wins; `unlimited` only when some
    fresh observation validly states no limit and no observation holds a window. Refused readings are listed."""
    windows = newest_per_window(observations)
    no_limit = not windows and any(o.no_limit for o in observations)
    if refused:
        extra["refused"] = refused
    return provider_result(windows, no_limit, reason, **extra)


def provider_result(windows: list[dict[str, Any]], no_limit: bool, reason: str, **extra: Any) -> dict[str, Any]:
    """`ok` when at least one window holds a valid reading; `unlimited` only when the source validly reports no window
    (`no_limit`: schema-valid and fresh); anything else is `unknown`, its windows kept for display. An `unknown`
    provider is skipped by the blocks writer, so its blocks stay."""
    if any(w["state"] in KNOWN_STATES for w in windows):
        return {"status": "ok", **extra, "windows": windows, "unlimited": False}
    if not windows and no_limit:
        return {"status": "ok", **extra, "windows": [], "unlimited": True}
    return unknown(reason, **extra, windows=windows)


def last_token_row(path: Path) -> dict[str, Any] | None:
    """The last `token_count` row of a session file (bounded tail), with its time."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.split("\n")):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        payload = row.get("payload") if isinstance(row, dict) else None
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            return {"payload": payload, "timestamp": row.get("timestamp"), "file": path}
    return None


def newest_sessions(sessions: Path, count: int) -> list[tuple[float, Path]]:
    """The `count` most recently modified session files as (mtime, path), newest first."""
    candidates: list[tuple[float, Path]] = []
    for folder, dirs, files in os.walk(sessions, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.isjunction(Path(folder, d))]
        for file in files:
            if file.startswith("rollout-") and file.endswith(".jsonl"):
                path = Path(folder, file)
                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    return heapq.nlargest(count, candidates)


def codex_snapshot(
    home: Path, now: float, stale_minutes: int, session_file_limit: int
) -> dict[str, Any]:
    """The newest `token_count` row by its own timestamp across the candidate session files.

    File mtime only orders the scan: a later non-token append to an old session moves its mtime but not its last
    token row, so the first file with a row is not necessarily the newest observation. Restored or coarse mtimes
    can precede row timestamps: every bounded candidate is compared. A row without a timestamp uses file mtime.
    Each row passes `observe`, so a row dated after `now`, or whose timestamp is present but not a time with a UTC
    offset, is refused, never the newest."""
    sibling("codex_backend")._positive_int("usage", "session_file_limit", session_file_limit)
    sessions = home / "sessions"
    if not sessions.is_dir():
        return unknown("no sessions directory", source=str(sessions))
    best: tuple[Observation, str, dict[str, Any]] | None = None
    refused: list[str] = []
    for modified, path in newest_sessions(sessions, session_file_limit):
        row = last_token_row(path)
        if row is None:
            continue
        # Only an absent timestamp falls back to the file time; a present one that is no UTC-offset time is refused
        # by `observe` (a malformed value is never read as a missing one).
        if row["timestamp"] is None:
            seen, basis = modified, "file mtime (the row has no timestamp)"
        else:
            seen, basis = parse_time(row["timestamp"]), "row timestamp"
        limits = row["payload"].get("rate_limits", ())  # () marks an absent member: neither a null nor an object
        found = observe(
            f"session token_count row {path}",
            seen,
            now,
            stale_minutes,
            lambda fresh, x=limits: codex_windows(x, now, fresh),
        )
        if isinstance(found, str):
            refused.append(found)
        elif best is None or found.at > best[0].at:
            best = (found, basis, row)
    if best is None:
        reason = "no token_count row in the newest sessions" + (" with a valid time" if refused else "")
        return unknown(reason, source=str(sessions), **({"refused": refused} if refused else {}))
    found, basis, row = best
    payload = row["payload"]
    limits = payload.get("rate_limits")
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    reason = "no valid limit reading in the newest token_count row" + ("" if found.fresh else " (the row is stale)")
    return provider_from(
        [found],
        refused,
        reason,
        source="newest session token_count row",
        observed_at=iso(found.at),
        observed_from=basis,
        age_minutes=round((now - found.at) / 60, 1),
        plan_type=limits.get("plan_type") if isinstance(limits, dict) else None,
        total_token_usage=usage if isinstance(usage, dict) else None,
    )


def codex_windows(limits: object, now: float, fresh: bool) -> tuple[list[dict[str, Any]], bool]:
    """(windows, states_no_limit) of a row's `rate_limits` member. Decision 44: a row states no limit when the member is
    null, or an object whose window slots are all null or absent and that holds no other window; an absent or
    malformed member says nothing."""
    if limits is None:
        return [], True
    if not isinstance(limits, dict):
        return [], False
    return windows_of(limits, now, fresh), True


# Fixed arguments of the Antigravity usage query: protocol-bound (the CLI's own slash command and output format).
ANTIGRAVITY_ARGS = ("-p", "/usage", "--output-format", "json")


def antigravity_snapshot(
    command: str | None, timeout_seconds: int | None, runner: Any = subprocess.run
) -> dict[str, Any]:
    """`<antigravity_command> -p /usage --output-format json`, run from an argument list (no shell). A JSON object it
    prints is passed through as `data`, any other non-empty output as `report` text, both untouched and never read for
    windows; a missing command, a spawn failure, a timeout, a non-zero exit or empty output is `unknown`, never 0.
    agy 1.5.9 sends this slash command to the model (T-026 probe: `num_turns` 1, about 28k input tokens), so a
    configured command spends Antigravity quota on every snapshot."""
    if command is None or timeout_seconds is None:
        return unknown("no usage.antigravity_command configured")
    try:
        done = runner(
            [command, *ANTIGRAVITY_ARGS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return unknown(f"cannot run the usage command: {type(exc).__name__}")
    if done.returncode != 0:
        return unknown("the usage command exited non-zero", returncode=done.returncode)
    text = done.stdout or ""
    if not text.strip():
        return unknown("the usage command printed nothing")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict):
        return {"status": "ok", "source": "usage command", "data": data}
    return {"status": "ok", "source": "usage command", "report": text}


def status_observation(
    path: Path,
    key: str,
    now: float,
    stale_minutes: int,
    parse: Callable[[Mapping[str, Any], float, bool], list[dict[str, Any]]],
) -> Observation | str:
    """The reading of `status_reading` alone: an `Observation`, or the reason there is none."""
    return status_reading(path, key, now, stale_minutes, parse)[1]


def status_reading(
    path: Path,
    key: str,
    now: float,
    stale_minutes: int,
    parse: Callable[[Mapping[str, Any], float, bool], list[dict[str, Any]]],
) -> tuple[str, Observation | str]:
    """A status-line file written by statusline.py: (tag of `stored_status`, the `Observation` observed at its
    `written_at` or the reason there is none). ABSENT only for a missing file, which is no source and no finding;
    MALFORMED for a file that exists and is not the documented shape, or cannot be read: a finding the provider
    lists under `refused`, also beside another source. A VALID file is a reading unless `observe` refuses its
    time (after `now`). An empty `key` object states no limit (Claude Code drops a window once its reset passes)."""
    try:
        stored = stored_status(path.read_bytes(), key)
    except FileNotFoundError:
        stored = stored_status(None, key)
    except OSError as exc:
        stored = Stored(MALFORMED, (f"cannot be read ({type(exc).__name__})",))
    if stored.tag == ABSENT:
        return ABSENT, f"no status-line file {path}"
    if stored.tag == MALFORMED:
        return MALFORMED, f"status-line file {path}: {'; '.join(stored.problems)}"
    member = stored.member
    assert member is not None
    source = f"status line {path}"
    return VALID, observe(source, stored.written, now, stale_minutes, lambda fresh: (parse(member, now, fresh), True))


def last_rate_limit_event(path: Path) -> dict[str, Any] | None:
    """The `rate_limit_info` of the last `rate_limit_event` in a worker turn log (bounded tail)."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.split("\n")):
        if RATE_LIMIT_EVENT not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict) and row.get("type") == RATE_LIMIT_EVENT:
            info = row.get("rate_limit_info")
            return info if isinstance(info, dict) else None
    return None


def newest_worker_logs(jobs: Path, count: int) -> list[tuple[float, Path]]:
    """The `count` most recently modified worker turn logs `<jobs>/<worker>/<n>.jsonl`, newest first."""
    candidates: list[tuple[float, Path]] = []
    try:
        folders = [p for p in jobs.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return []
    for folder in folders:
        try:
            files = list(folder.glob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    return heapq.nlargest(count, candidates)


def newest_per_window(observations: list[Observation]) -> list[dict[str, Any]]:
    """Per window label, the entry of the newest observation; entries carry `source` and `observed_at`."""
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for found in observations:
        for window in found.windows:
            label = window["label"]
            if label not in best or found.at > best[label][0]:
                best[label] = (found.at, window)
    return [best[label][1] for label in sorted(best)]


def claude_auto_snapshot(
    statusline: Path, jobs: Path, now: float, stale_minutes: int, log_limit: int
) -> dict[str, Any]:
    """Claude windows from the status-line file and the last `rate_limit_event` of the newest worker turn logs; per
    window the newest observation wins and names its source. A turn log has no event time, so its mtime stands for
    it (and a log dated after `now` is refused). Unlimited needs a fresh status-line reading with no window (Claude
    Code drops a window after its reset) and no window from any other source; a worker event without a window name
    is no evidence either way."""
    sibling("codex_backend")._positive_int("usage", "session_file_limit", log_limit)
    observations: list[Observation] = []
    refused: list[str] = []
    tag, status = status_reading(statusline, "rate_limits", now, stale_minutes, claude_status_windows)
    if isinstance(status, Observation):
        observations.append(status)
    elif tag != ABSENT:  # a file that exists and gives no reading is a finding, also beside the worker logs
        refused.append(status)
    events = 0
    for modified, path in newest_worker_logs(jobs, log_limit):
        info = last_rate_limit_event(path)
        if info is None:
            continue
        events += 1
        found = observe(
            f"worker rate_limit_event {path}",
            modified,
            now,
            stale_minutes,
            lambda fresh, x=info: (rate_limit_windows(x, now, fresh), False),
        )
        if isinstance(found, str):
            refused.append(found)
        else:
            observations.append(found)
    seen = "status line read" if isinstance(status, Observation) else status
    detail = f"{seen}; {events} rate_limit_event(s) in the newest worker logs under {jobs}"
    return provider_from(
        observations, refused, f"no valid Claude limit reading: {detail}", source="status line and worker logs"
    )


def antigravity_auto_snapshot(
    statusline: Path,
    now: float,
    stale_minutes: int,
    command: str | None,
    timeout_seconds: int | None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Antigravity from its status-line file; the configured usage command, if any, is added as `command` (or is the
    whole answer when the file is missing)."""
    tag, status = status_reading(statusline, "quota", now, stale_minutes, antigravity_windows)
    ran = antigravity_snapshot(command, timeout_seconds, runner) if command is not None else None
    if isinstance(status, str):
        # A missing file is no source; one that exists and gives no reading is listed, also beside the command.
        refused = {} if tag == ABSENT else {"refused": [status]}
        if ran is not None:
            return {**ran, **refused}
        return unknown(f"{status} and no usage.antigravity_command configured", **refused)
    extra: dict[str, Any] = {"source": status.source}
    if ran is not None:
        extra["command"] = ran
    why = "no valid bucket in the status-line file" + ("" if status.fresh else " (the reading is stale)")
    return provider_from([status], [], why, **extra)


def mark_blocked(snapshot: dict[str, Any], line: float) -> dict[str, Any]:
    """`blocked` on every window (used percent at or over the provider's line) and on the provider."""
    windows = snapshot.get("windows")
    if snapshot.get("status") != "ok" or not isinstance(windows, list):
        return snapshot
    for window in windows:
        used = window.get("used_percent")
        window["blocked"] = isinstance(used, (int, float)) and used >= line
    snapshot["block_percent"] = line
    snapshot["blocked"] = any(w["blocked"] for w in windows)
    return snapshot


def claude_snapshot(path: Path | None, now: float, stale_minutes: int) -> dict[str, Any]:
    """The PM-written Claude usage file: passed through when it is a fresh JSON object, otherwise unknown. A file
    dated after `now` is refused like every other observation (`observe`), never read as fresh."""
    if path is None:
        return unknown("no --claude-file given")
    try:
        modified = path.stat().st_mtime
        document = decode_object(path.read_bytes())
    except FileNotFoundError:
        return unknown("file missing", source=str(path))
    except OSError as exc:
        return unknown(f"file cannot be read ({type(exc).__name__})", source=str(path))
    if isinstance(document, str):
        return unknown(f"file is not a JSON object: {document}", source=str(path))
    data = document.data
    if modified > now:
        return unknown(f"file time {iso(modified)} is after now ({iso(now)}): refused", source=str(path))
    age = (now - modified) / 60
    if age > stale_minutes:
        return unknown(
            "stale", source=str(path), age_minutes=round(age, 1), stale_minutes=stale_minutes
        )
    return {"status": "ok", "source": str(path), "age_minutes": round(age, 1), "data": data}


def guarded(source: Any, *args: Any) -> dict[str, Any]:
    """One provider failing must not stop the snapshot: it reports `unknown`. `SystemExit` (a missing executable or
    a refusal deep in a helper) does not derive from `Exception`, so it is caught on purpose."""
    try:
        return source(*args)  # type: ignore[no-any-return]
    except (Exception, SystemExit) as exc:
        return unknown(f"{source.__name__} failed: {type(exc).__name__}: {str(exc)[:200]}")


def collect(
    home: Path,
    claude_file: Path | None,
    stale_minutes: int,
    session_file_limit: int,
    now: float | None = None,
    antigravity_command: str | None = None,
    command_timeout_seconds: int | None = None,
    runner: Any = subprocess.run,
    *,
    claude_statusline: Path | None = None,
    antigravity_statusline: Path | None = None,
    jobs: Path | None = None,
    block_percent: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Read every provider; no side effects. `main` passes every configured source; a caller that leaves a status-line
    path out (the older form) gets the older sources only: `--claude-file` and the usage command."""
    stamp = time.time() if now is None else now
    if claude_file is None and claude_statusline is not None and jobs is not None:
        claude = guarded(
            claude_auto_snapshot, claude_statusline, jobs, stamp, stale_minutes, session_file_limit
        )
    else:
        claude = guarded(claude_snapshot, claude_file, stamp, stale_minutes)
    if antigravity_statusline is not None:
        antigravity = guarded(
            antigravity_auto_snapshot,
            antigravity_statusline,
            stamp,
            stale_minutes,
            antigravity_command,
            command_timeout_seconds,
            runner,
        )
    else:
        antigravity = guarded(
            antigravity_snapshot, antigravity_command, command_timeout_seconds, runner
        )
    result = {
        "collected_at": dt.datetime.fromtimestamp(stamp, dt.UTC).isoformat(),
        "claude": claude,
        "codex": guarded(codex_snapshot, home, stamp, stale_minutes, session_file_limit),
        "antigravity": antigravity,
    }
    if block_percent is not None:
        for provider in ("claude", "codex", "antigravity"):
            mark_blocked(result[provider], block_percent[provider])
    return result


def state_path(relative: str, root: Path, create: bool = False) -> Path:
    """The one path of a configured quota file, used by every reader and writer (statusline.py, this script, the
    SessionStart hook): split on either separator whatever the host, required under `.tmp/pm-state/`, and refused when
    any component from `root` down to the file is a link or junction (AGENTS.md section 6), so nothing is read from or
    written outside the project. `create` makes the missing folders, each checked again after it is made. Raises
    SystemExit with the reason."""
    state = sibling("codex_backend").STATE_DIR
    parts = [p for p in re.split(r"[\\/]", relative) if p not in ("", ".")]
    if ".." in parts or any(":" in p for p in parts) or len(parts) <= len(state) or tuple(parts[: len(state)]) != state:
        raise SystemExit(f"quota path {relative!r} is not a file under {'/'.join(state)}/")
    is_link = sibling("purge_tmp")._is_link
    path = root
    for index, part in enumerate(parts):
        path = path / part
        if create and index < len(parts) - 1 and not os.path.lexists(path):
            path.mkdir(exist_ok=True)
        if is_link(path):
            raise SystemExit(f"quota path {relative!r} passes through a link: {path}")
    folder = (root / Path(*state)).resolve()
    if folder not in path.resolve().parents:
        raise SystemExit(f"quota path {relative!r} resolves outside {folder}")
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write through a sibling temp file and `os.replace`, retried briefly while another process holds the target
    (Windows sharing violation), so a reader never sees half a file. The temp file is removed on failure. The caller
    resolves `path` with `state_path`."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_PAUSE_SECONDS)
    finally:
        temp.unlink(missing_ok=True)


JSON_SPACE = " \t\n\r"  # protocol-bound: the white space of RFC 8259


BOM = "﻿"


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number out of range: {text[:40]}")
    return value


def _members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, value in pairs:
        if name in found:
            raise ValueError(f"member {json.dumps(name)[:40]} appears twice")
        found[name] = value
    return found


# Protocol-bound (RFC 8259): Python's decoder also reads `NaN`, `Infinity`, numbers beyond a float and a repeated
# member name; none of them survives being written again, and a repeated name hides the first value.
STRICT_JSON = json.JSONDecoder(parse_constant=_no_constant, parse_float=_finite, object_pairs_hook=_members)


class Document(NamedTuple):
    """One JSON object text as `parse_object` read it: the text, its top-level members, the span of each member's
    value in the text and the index of the closing brace (the snapshot replaces one value and keeps the rest)."""

    text: str
    data: dict[str, Any]
    spans: dict[str, tuple[int, int]]
    close: int


def parse_object(text: str) -> Document:
    """The one parser of the tool's stored JSON: `text` must be exactly one JSON object, strictly (`STRICT_JSON`,
    at every depth), after at most one leading BOM. Raises ValueError (or RecursionError) with the reason."""

    def skip(at: int) -> int:
        while at < len(text) and text[at] in JSON_SPACE:
            at += 1
        return at

    i = skip(1 if text.startswith(BOM) else 0)
    if i >= len(text):
        raise ValueError("no JSON text (empty or white space only)")
    if text[i] != "{":
        raise ValueError("not a JSON object")
    i, first = i + 1, True
    data: dict[str, Any] = {}
    spans: dict[str, tuple[int, int]] = {}
    while True:
        i = skip(i)
        if text[i : i + 1] == "}":
            break
        if not first:
            if text[i : i + 1] != ",":
                raise ValueError("expected ','")
            i = skip(i + 1)
        first = False
        name, i = STRICT_JSON.raw_decode(text, i)
        if not isinstance(name, str):
            raise ValueError("member name is not a string")
        if name in data:
            raise ValueError(f"member {json.dumps(name)[:40]} appears twice")
        i = skip(i)
        if text[i : i + 1] != ":":
            raise ValueError("expected ':'")
        start = skip(i + 1)
        data[name], i = STRICT_JSON.raw_decode(text, start)
        spans[name] = (start, i)
    if text[i + 1 :].strip(JSON_SPACE):
        raise ValueError("text after the JSON object")
    return Document(text, data, spans, i)


def decode_object(raw: bytes) -> Document | str:
    """The one decoder of every stored quota document (the quota-blocks file, the status-line files, the status
    line's input, `--claude-file`): the `Document` of bytes that are UTF-8 text holding exactly one strict JSON
    object, or the precise reason they are not. Blank bytes are not a document: only a missing file is absent."""
    try:
        return parse_object(raw.decode("utf-8"))
    except RecursionError:
        return "nested too deeply"
    except UnicodeDecodeError as exc:
        return f"not UTF-8 text ({exc.reason} at byte {exc.start})"
    except ValueError as exc:
        return str(exc)[:160]


# The three results of the stored-format validator (`Stored.tag`); a consumer branches on the tag, never on an
# empty list, empty text or None.
VALID, ABSENT, MALFORMED = "valid", "absent", "malformed"


class Stored(NamedTuple):
    """What `stored_blocks` or `stored_status` found in a stored quota file.

    `tag` is VALID, ABSENT (only when the caller passes None: it proved there is no file) or MALFORMED with
    `problems`, one precise reason per unreadable part. `blocks` holds the entries of the `blocks` list that are
    objects, also beside a malformed entry, so a reader can still show them. `document` (VALID only), `written` and
    `member` (a VALID status-line file: its `written_at` in epoch seconds and its quota object) are what the
    consumers work on."""

    tag: str
    problems: tuple[str, ...] = ()
    document: Document | None = None
    blocks: tuple[dict[str, Any], ...] = ()
    written: float | None = None
    member: dict[str, Any] | None = None


def stored_blocks(raw: bytes | None) -> Stored:
    """The validator of the quota-blocks file, for every reader and writer (the snapshot, `pm-set`, the SessionStart
    hook). Documented shape: one JSON object (`decode_object`) whose optional member `blocks` is a list of objects;
    an object without `blocks`, or with an empty list, is the validated empty shape, the only proof of "no block"
    besides a missing file. Anything else is MALFORMED: bytes that are no such document, a `blocks` that is no
    list, and every entry that is not an object (`null` included), each named."""
    if raw is None:
        return Stored(ABSENT)
    document = decode_object(raw)
    if isinstance(document, str):
        return Stored(MALFORMED, (f"not one JSON object: {document}",))
    if "blocks" not in document.data:
        return Stored(VALID, (), document)
    entries = document.data["blocks"]
    if not isinstance(entries, list):
        return Stored(MALFORMED, ("`blocks` is not a list",))
    blocks = tuple(entry for entry in entries if isinstance(entry, dict))
    problems = tuple(
        f"blocks[{index}] is not an object: {json.dumps(entry)[:40]}"
        for index, entry in enumerate(entries)
        if not isinstance(entry, dict)
    )
    if problems:
        return Stored(MALFORMED, problems, None, blocks)
    return Stored(VALID, (), document, blocks)


def stored_status(raw: bytes | None, member: str) -> Stored:
    """The validator of a status-line file (`statusline.py` checks the bytes it stores with it, the snapshot the
    bytes it reads). Documented shape: one JSON object (`decode_object`) with `written_at`, an ISO time with its
    UTC offset, and `member` (`rate_limits` or `quota`), an object; an empty `member` is the validated "no limit
    window" shape. Each window inside `member` is judged by the source's window validator (`claude_status_windows`,
    `antigravity_windows`), which marks a malformed one `unknown` beside the valid ones."""
    if raw is None:
        return Stored(ABSENT)
    document = decode_object(raw)
    if isinstance(document, str):
        return Stored(MALFORMED, (f"not one JSON object: {document}",))
    written = parse_time(document.data.get("written_at"))
    value = document.data.get(member)
    problems = []
    if written is None:
        problems.append("lacks a valid written_at")
    if not isinstance(value, dict):
        problems.append(f"{member} is not an object")
    if problems:
        return Stored(MALFORMED, tuple(problems))
    return Stored(VALID, (), document, (), written, value)


def find_member(text: str, key: str) -> tuple[tuple[int, int] | None, int]:
    """(span of the top-level member `key`'s value or None, index of the object's closing brace) of a JSON object
    text, by `parse_object`. Raises ValueError when the text is not one."""
    document = parse_object(text)
    return document.spans.get(key), document.close


def splice_blocks(text: str | None, blocks: list[Any]) -> str:
    """The file text with only its top-level `blocks` value replaced (added before the closing brace when absent);
    None (no file) gives a new document. `text` is the file's bytes decoded without newline translation; the new
    value is rendered canonically in the file's own line ending. Raises ValueError for a text that is no document."""
    newline = "\r\n" if text is not None and "\r\n" in text else "\n"
    value = json.dumps(blocks, indent=2, ensure_ascii=False).replace("\n", newline + "  ")
    if text is None:
        return "{" + newline + '  "blocks": ' + value + newline + "}" + newline
    span, close = find_member(text, "blocks")
    if span is not None:
        return text[: span[0]] + value + text[span[1] :]
    head = text[:close].rstrip(JSON_SPACE)
    joiner = newline if head.endswith("{") else "," + newline
    return head + joiner + '  "blocks": ' + value + newline + text[close:]


def block_key(block: Mapping[str, Any]) -> tuple[object, object]:
    return block.get("provider"), block.get("window")


def updated_blocks(
    existing: list[Any], snapshot: Mapping[str, Any], now: float
) -> list[Any]:
    """The new `blocks` list: every entry of the file takes one `block_step` with the reading of its provider and
    window (none when the provider could not be read or does not report that window), and a blocked window without
    a block records one."""
    measured: dict[tuple[object, object], dict[str, Any]] = {}
    for provider in PROVIDERS:
        snap = snapshot.get(provider)
        if not isinstance(snap, dict) or snap.get("status") != "ok" or not isinstance(snap.get("windows"), list):
            continue
        for window in snap["windows"]:
            measured[(provider, window.get("label"))] = window
    result: list[Any] = []
    seen: set[tuple[object, object]] = set()
    for block in existing:
        key = block_key(block) if isinstance(block, dict) else None
        # A provider or window that is not text (a hand-written entry) matches no reading; it is not hashed either.
        named = key is not None and all(isinstance(part, str) for part in key)
        if named:
            seen.add(key)  # type: ignore[arg-type]
        after = block_step(key, block, now, reading=measured.get(key) if named else None)
        if after is not NO_BLOCK:
            result.append(after)
    for key, window in measured.items():
        if key not in seen and window.get("blocked"):
            result.append(block_step(key, NO_BLOCK, now, reading=window))
    return result


class _NoBlock:
    def __repr__(self) -> str:
        return "NO_BLOCK"


# "No block" for `block_step`, as the state before an event and as its result. A value of its own, because every
# JSON value, `null` (None) included, can be an entry of a hand-written list.
NO_BLOCK: Any = _NoBlock()


def block_step(
    key: tuple[object, object] | None,
    block: Any,
    now: float,
    reading: Mapping[str, Any] | None = None,
    pm: PmEdit | None = None,
) -> Any:
    """The block state machine, whole: the next state of one entry of the `blocks` list (`NO_BLOCK`: none) after one
    event. The snapshot passes `reading` (the window read now for the block's provider and window, or None); `pm-set`
    passes `pm`. Ob, Rb: the block's `observed_at` and `resets_at` when they are times with a UTC offset; a block
    without Ob is "legacy". Ow, Rw: the same of the reading. The first matching row wins.

      #   block          event                                                    next state
      1   none           blocked reading                                          recorded from the reading
      2   none           pm edit                                                  recorded, `observed_at` given or now
      3   any            blocked reading, Ow >= Ob or legacy                      re-asserted: reading's fields, PM fields stay
      4   any            pm edit that assigns `resets_at`                         re-asserted: `observed_at` given or now
      5   any            pm edit of other fields                                  metadata only: `observed_at` untouched
      6   any            pm remove                                                none
      7   Rb < now       any snapshot run, whatever the provider's reading        expired: none (a blocked reading: row 1)
      8   Ob             reading `reset`, Ow > Ob, Rw > Ob                        none
      9   Ob             fresh `ok` reading below the line, Ow > Ob, Rw > Ob      none
      10  Ob             reading with Ow <= Ob or Rw <= Ob (an earlier cycle)     kept
      11  any            reading unknown, stale or disabled; no reading           kept
      12  legacy         any reading that is not blocked                          kept (it cannot be ordered)
      13  legacy, no Rb  none of rows 3, 4, 6                                     kept (the SessionStart hook marks it)
      14  not an object  any                                                      the same value (`null` included)

    Row 7 is the block's own statement, not a reading, so it holds for a provider that could not be read (an
    `unknown` reading never clears a block, rows 10 to 12). `pm-set` changes what it is told and never expires a
    block; the next snapshot does. Every time this function writes is UTC text (`iso`). Row 14 only states that
    the step never turns such an entry into "no block": a file holding one is malformed (`stored_blocks`), so no
    writer gets this far with it and nothing is written."""
    if block is not NO_BLOCK and not isinstance(block, dict):
        return block  # row 14
    if pm is not None:
        if pm.remove:
            return NO_BLOCK  # row 6
        assert key is not None
        if block is NO_BLOCK:  # row 2
            after = {"provider": key[0], "window": key[1], "observed_at": iso(now), "written_by": "pm"}
        else:
            after = dict(block)
        for name in pm.unset:
            after.pop(name, None)
        after.update(pm.assign)
        for name in BLOCK_TIMES:
            stamp = parse_time(pm.assign.get(name))
            if stamp is not None:
                after[name] = iso(stamp)
        if "resets_at" in pm.assign and "observed_at" not in pm.assign:
            after["observed_at"] = iso(now)  # row 4: the exhaustion is stated again now
        return after  # row 5 otherwise
    blocked = reading is not None and bool(reading.get("blocked"))
    if block is NO_BLOCK:
        return block_fields(key, reading) if blocked and key is not None and reading is not None else NO_BLOCK  # row 1
    if blocked and reading is not None and key is not None:
        read_at, blocked_at = parse_time(reading.get("observed_at")), parse_time(block.get("observed_at"))
        if read_at is not None and (blocked_at is None or read_at >= blocked_at):
            return {**block, **block_fields(key, reading)}  # row 3
    ends = parse_time(block.get("resets_at"))
    if ends is not None and ends < now:
        return block_fields(key, reading) if blocked and key is not None and reading is not None else NO_BLOCK  # row 7
    if reading is not None and not blocked and clears(block, reading):
        return NO_BLOCK  # rows 8, 9
    return block  # rows 10 to 13


def clears(block: Mapping[str, Any], window: Mapping[str, Any]) -> bool:
    """Whether a window reading ends a block (decision 46): only an observation made after the block's own
    (`observed_at` later than the block's; a block without a valid `observed_at` is never cleared by a reading) and of
    the same or a later reset cycle (a reading whose reset time is at or before the block's observation describes a
    cycle that had already ended when the block was seen). Then the window has reset since the block was seen, or it
    is a fresh `ok` reading below the line."""
    state = window.get("state")
    if state not in ("ok", "reset"):
        return False
    read_at, blocked_at = parse_time(window.get("observed_at")), parse_time(block.get("observed_at"))
    if read_at is None or blocked_at is None or read_at <= blocked_at:
        return False
    cycle_end = parse_time(window.get("resets_at"))
    if cycle_end is not None and cycle_end <= blocked_at:
        return False
    if state == "reset":
        return True
    return window.get("fresh") is True and window.get("blocked") is False


def block_fields(key: tuple[object, object], window: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": key[0],
        "window": key[1],
        "resets_at": window.get("resets_at"),
        "used_percent": window.get("used_percent"),
        "source": window.get("source"),
        "observed_at": window.get("observed_at"),
        "written_by": "usage_snapshot.py",
    }


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def planned_blocks(path: Path, raw: bytes | None, snapshot: Mapping[str, Any], now: float) -> bytes | dict[str, Any]:
    """The new file bytes, or the report when nothing is written: `unchanged`, or `error` for a file `stored_blocks`
    calls malformed in any part (left untouched). The `blocks` value is rendered again canonically (equal values,
    not equal spellings); the bytes outside it are kept, CRLF included."""
    stored = stored_blocks(raw)
    if stored.tag == MALFORMED:
        return malformed_report(path, stored)
    existing = list(stored.blocks)
    blocks = updated_blocks(existing, snapshot, now)
    if blocks == existing:  # also: no file and nothing blocked, so no file is created
        return {"status": "unchanged", "path": str(path), "blocks": len(blocks)}
    return splice_blocks(stored.document.text if stored.document is not None else None, blocks).encode("utf-8")


def malformed_report(path: Path, stored: Stored) -> dict[str, Any]:
    """The report of both writers for a malformed file: nothing is written, because a writer that cannot read
    every part cannot know what it would lose. `problems` lists each unreadable part."""
    return {
        "status": "error",
        "path": str(path),
        "reason": "; ".join(stored.problems) + " (nothing written: repair the file by hand)",
        "problems": list(stored.problems),
    }


def write_quota_blocks(path: Path, snapshot: Mapping[str, Any], now: float) -> dict[str, Any]:
    """Keep the `blocks` list of the quota-blocks file in step with the snapshot; every other byte stays."""
    return transact(path, lambda raw: planned_blocks(path, raw, snapshot, now))


def locked(path: Path, action: Callable[[], Any]) -> Any:
    """Run `action` while holding `<file>.lock` (the OS lock of hooks/git_ledger.py): the only way this tool opens
    the quota-blocks file, for readers and writers alike, so no read overlaps a commit. Raises OSError when the lock
    cannot be had: `TimeoutError` after `LOCK_WAIT_SECONDS`, or a link at the lock's path."""
    ledger = sibling("git_ledger", HERE / "hooks")
    lock = path.with_name(path.name + LOCK_SUFFIX)
    if sibling("purge_tmp")._is_link(lock):
        raise OSError(f"the lock file is a link: {lock}")
    with lock.open("a+b") as handle:
        try:
            ledger._lock(handle, time.monotonic() + LOCK_WAIT_SECONDS)
        except TimeoutError:
            raise TimeoutError(f"the lock stayed busy for {LOCK_WAIT_SECONDS:g} s: {lock}") from None
        try:
            return action()
        finally:
            ledger._unlock(handle)


def read_quota_file(path: Path) -> bytes | None:
    """The read contract of the quota-blocks file (the SessionStart hook): its bytes, or None only when the absence
    is proven: the state folder does not exist, or the file is missing while the writers' lock is held. Anything
    that cannot be proven raises OSError (the lock stayed busy, the file or the lock cannot be opened), and the
    caller reports the file as unreadable, never as "no block". The file keeps one name that commits replace
    atomically, so a reader outside this tool sees the old or the new file, never a missing one."""
    if not path.parent.is_dir():
        return None
    return locked(path, lambda: read_bytes(path))


def transact(path: Path, plan: Callable[[bytes | None], bytes | dict[str, Any]]) -> dict[str, Any]:
    """The one read-modify-write of the quota-blocks file, for every writer: the snapshot (`write_quota_blocks`) and
    the PM (`pm-set`). `plan(current bytes or None)` returns the new bytes, or a report when nothing is written.

    It runs under `<file>.lock` (`locked`, held across every retry), so writers and readers are serialised, and it
    is the file's one error boundary: whatever fails while the lock is opened, awaited or held (a busy lock, any
    `OSError`, a failing plan) becomes a `status: error` report, with the file as it was (the commit is one
    `os.replace` of a finished temp file onto the stable name). A hand edit without the lock is out of contract
    (PM_POLICY section 1); the re-read before the replace only narrows the moment in which one can be lost."""
    try:
        return locked(path, lambda: locked_write(path, plan))  # type: ignore[no-any-return]
    except Exception as exc:
        return {"status": "error", "path": str(path), "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


def locked_write(path: Path, plan: Callable[[bytes | None], bytes | dict[str, Any]]) -> dict[str, Any]:
    # One fixed temp name: only the lock holder writes it, and a leftover of a killed writer is overwritten here.
    temp = path.with_name(path.name + TEMP_SUFFIX)
    if sibling("purge_tmp")._is_link(temp):
        raise OSError(f"the temp file is a link: {temp}")
    held: OSError | None = None
    for _ in range(REPLACE_ATTEMPTS):
        try:
            raw = read_bytes(path)
            planned = plan(raw)
            if isinstance(planned, dict):
                return planned
            temp.write_bytes(planned)
            if read_bytes(path) != raw:  # a writer without the lock changed the file meanwhile: plan again
                continue
            os.replace(temp, path)  # the commit: the name never goes away
            return {"status": "written", "path": str(path), "blocks": blocks_count(planned)}
        except PermissionError as exc:  # Windows: a reader outside this tool holds the file open for a moment
            held = exc
            time.sleep(REPLACE_PAUSE_SECONDS)
        finally:
            temp.unlink(missing_ok=True)
    reason = "the file kept changing or stayed held; not written" + (f" ({type(held).__name__})" if held else "")
    return {"status": "error", "path": str(path), "reason": reason}


def blocks_count(raw: bytes) -> int:
    return len(stored_blocks(raw).blocks)


PROVIDERS = ("claude", "codex", "antigravity")
# Block fields that name the block (`--block`), and block fields the transitions read as times (`updated_blocks`).
BLOCK_IDENTITY = ("provider", "window")
BLOCK_TIMES = ("resets_at", "observed_at")


class PmEdit(NamedTuple):
    """One `pm-set` call: `block` (provider, window) or None for top-level members; `assign` name -> JSON value;
    `unset` names; `remove` drops the block."""

    block: tuple[str, str] | None
    assign: dict[str, Any]
    unset: list[str]
    remove: bool


def pm_edit_problem(edit: PmEdit, now: float) -> str | None:
    """Why a `pm-set` call is refused before anything is read, or None."""
    names = [*edit.assign, *edit.unset]
    if edit.remove:
        if edit.block is None or names:
            return "--remove-block needs --block and no field edits"
        return None
    if not names:
        return "nothing to change: give --text, --json, --unset or --remove-block"
    if len(set(names)) != len(names):
        return "a name is given twice"
    if any(not name for name in names):
        return "an empty name"
    if edit.block is None:
        return "the blocks list is edited with --block, not as a member" if "blocks" in names else None
    if edit.block[0] not in PROVIDERS:
        return f"provider must be one of {', '.join(PROVIDERS)}"
    if any(name in BLOCK_IDENTITY for name in names):
        return f"{' and '.join(BLOCK_IDENTITY)} name the block (--block); they are not edited"
    for name in BLOCK_TIMES:
        if name in edit.unset and name == "observed_at":
            return "observed_at orders the block against readings; it can be changed, not removed"
        if name in edit.assign:
            stamp = parse_time(edit.assign[name])
            if stamp is None:
                return f"{name} must be an ISO 8601 time with its UTC offset, for example 2026-11-18T14:00:00Z"
            if name == "observed_at" and stamp > now:
                return "observed_at is after now"
    return None


def planned_pm_edit(path: Path, raw: bytes | None, edit: PmEdit, now: float) -> bytes | dict[str, Any]:
    """The new file bytes for a validated `PmEdit`, or the report when nothing is written. A block edit is one
    `block_step` (record, re-assert, metadata edit or removal). A file `stored_blocks` calls malformed in any part
    is left untouched (`error`). A valid one is written again canonically, as indented JSON in its own line ending,
    a leading BOM kept: every value it does not edit stays equal, its spelling may change (`1.50` becomes `1.5`)."""
    stored = stored_blocks(raw)
    if stored.tag == MALFORMED:
        return malformed_report(path, stored)
    text = stored.document.text if stored.document is not None else None
    before: dict[str, Any] = stored.document.data if stored.document is not None else {}
    data = json.loads(json.dumps(before))
    if edit.block is None:
        for name in edit.unset:
            data.pop(name, None)
        data.update(edit.assign)
    else:
        blocks = data.get("blocks", [])
        mine = [i for i, b in enumerate(blocks) if block_key(b) == edit.block]
        if len(mine) > 1 and not edit.remove:
            return {"status": "error", "path": str(path), "reason": f"more than one block {edit.block}"}
        after = block_step(edit.block, blocks[mine[0]] if mine else NO_BLOCK, now, pm=edit)
        if after is NO_BLOCK:  # removing a block that is not there changes nothing, so no file is made for it
            if mine:
                data["blocks"] = [b for i, b in enumerate(blocks) if i not in mine]
        elif mine:
            blocks[mine[0]] = after
        else:
            data["blocks"] = [*blocks, after]
    if data == before:
        return {"status": "unchanged", "path": str(path), "blocks": len(stored.blocks)}
    newline = "\r\n" if text is not None and "\r\n" in text else "\n"
    bom = BOM if text is not None and text.startswith(BOM) else ""
    body = json.dumps(data, indent=2, ensure_ascii=False).replace("\n", newline)
    planned = (bom + body + newline).encode("utf-8")
    written = stored_blocks(planned)
    if written.tag != VALID:  # the edit itself must not make the file one this tool refuses to read
        return {"status": "error", "path": str(path), "reason": "the edit would write: " + "; ".join(written.problems)}
    return planned


PM_SET_HELP = """Change the PM fields of usage.quota_blocks_file through the same locked transaction as the snapshot.

  python -B usage_snapshot.py pm-set [--config F] --text wake_note "Desktop task pm-wake-codex at 14:03Z"
  python -B usage_snapshot.py pm-set --block codex weekly --text resets_at 2026-11-21T13:24:27Z --text source "..."
  python -B usage_snapshot.py pm-set --block codex weekly --json wake_scheduled true --text wake_note "..."
  python -B usage_snapshot.py pm-set --block claude weekly --remove-block

Without --block the edits apply to top-level members (`waiting_tasks`, `near_limit`, `wake_*`; never `blocks`);
with --block to that block, which is created when absent. A --block call that sets `resets_at`, even to the value
it has, records the exhaustion again: `observed_at` moves to now (or to the `observed_at` given), so an older
reading cannot clear the block. Every other edit leaves `observed_at` as it is. `resets_at` and `observed_at` need
their UTC offset (`Z` or `+hh:mm`) and are stored in UTC.

The file is one JSON object whose `blocks` is a list of objects. A file that is anything else in any part (blank,
not strict JSON, a member name twice, `NaN`, an entry of `blocks` that is no object) is malformed: nothing is
written, the report lists each problem, and the file is repaired by hand. A valid file is written again
canonically: the values this call does not edit stay equal, their spelling may change (`1.50` becomes `1.5`).

Exit codes: 0 with one JSON report (`written` or `unchanged`); 1 with one JSON report `status: error` when the file
is malformed or could not be read or written (it is left as it was); 2 for refused arguments; a refused
configuration or path exits with its message and touches nothing."""


def pm_set(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="usage_snapshot.py pm-set", description=PM_SET_HELP, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, help="codex_profiles.json (default: the tracked file)")
    ap.add_argument("--block", nargs=2, metavar=("PROVIDER", "WINDOW"), help="edit this block instead of the top level")
    ap.add_argument("--text", nargs=2, action="append", default=[], metavar=("NAME", "TEXT"), help="set NAME to TEXT")
    ap.add_argument("--json", nargs=2, action="append", default=[], metavar=("NAME", "JSON"), help="set NAME to a value")
    ap.add_argument("--unset", action="append", default=[], metavar="NAME", help="remove NAME")
    ap.add_argument("--remove-block", action="store_true", help="remove the block named by --block")
    args = ap.parse_args(argv)
    assign: dict[str, Any] = {}
    for name, value in args.text:
        assign.setdefault(name, value)
    for name, value in args.json:
        try:
            assign.setdefault(name, STRICT_JSON.decode(value))  # the strictness of the file it is written to
        except (ValueError, RecursionError):
            ap.error(f"--json {name}: not a JSON value")
    now = time.time()
    edit = PmEdit(tuple(args.block) if args.block else None, assign, list(args.unset), args.remove_block)
    given = len(args.text) + len(args.json) + len(args.unset)
    problem = "a name is given twice" if given != len(assign) + len(args.unset) else pm_edit_problem(edit, now)
    if problem:
        ap.error(problem)
    relative = sibling("codex_backend").load_usage_settings(args.config)["quota_blocks_file"]
    state_path(relative, ROOT)  # a refused path exits here, before anything is created
    report = quota_file_report(relative, lambda path: transact(path, lambda raw: planned_pm_edit(path, raw, edit, now)))
    print(json.dumps(report))
    return 1 if report["status"] == "error" else 0


def quota_file_report(relative: str, action: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
    """The error boundary of both commands for the quota-blocks file, once arguments and configuration are accepted:
    `action(path)` with the state folder made, or a `status: error` report for whatever went wrong on the way (the
    folder cannot be made, a link appeared on the path). `transact` reports its own failures the same way, so a
    command prints exactly one JSON document and no traceback."""
    try:
        return action(state_path(relative, ROOT, create=True))
    except (Exception, SystemExit) as exc:
        return {"status": "error", "path": relative, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


def ledger_usage(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The compact form stored before and after a turn: status plus each window's label and used percent."""
    if snapshot.get("status") != "ok":
        return {"status": "unknown", "reason": snapshot.get("reason")}
    return {
        "status": "ok",
        "windows": [
            {k: w.get(k) for k in ("label", "window_minutes", "used_percent", "state")}
            for w in snapshot.get("windows", [])
        ],
    }


def cache_warning(
    turn: int, input_tokens: int | None, cached_input_tokens: int | None, ratio: float
) -> str | None:
    """From a worker's second turn: cached input below `ratio` of the input means the prefix was not reused."""
    if turn < 2 or not input_tokens or cached_input_tokens is None:
        return None
    if cached_input_tokens < ratio * input_tokens:
        return (
            f"cached_input_tokens {cached_input_tokens} is {cached_input_tokens / input_tokens:.0%} of "
            f"input_tokens {input_tokens}, below {ratio:.0%}: the prompt prefix was not reused"
        )
    return None


def append_ledger(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    if sys.argv[1:2] == ["pm-set"]:
        return pm_set(sys.argv[2:])
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, help="codex_profiles.json (default: the tracked file)")
    ap.add_argument(
        "--codex-home",
        type=Path,
        help="Codex home (default: runtime.codex_home, CODEX_HOME, ~/.codex)",
    )
    ap.add_argument(
        "--claude-file",
        type=Path,
        help="PM-written JSON with the Claude usage; replaces the status-line file and worker logs",
    )
    args = ap.parse_args()
    backend = sibling("codex_backend")
    settings = backend.load_usage_settings(args.config)
    home = args.codex_home or backend.effective_home(
        backend.load_home_setting(args.config), ROOT, os.environ
    )
    now = time.time()
    # The three quota paths are refused the same way, before anything is read.
    claude_statusline = state_path(settings["claude_statusline_file"], ROOT)
    antigravity_statusline = state_path(settings["antigravity_statusline_file"], ROOT)
    state_path(settings["quota_blocks_file"], ROOT)
    try:
        result = collect(
            home,
            args.claude_file,
            settings["stale_minutes"],
            settings["session_file_limit"],
            now,
            antigravity_command=settings["antigravity_command"],
            command_timeout_seconds=settings["command_timeout_seconds"],
            claude_statusline=claude_statusline,
            antigravity_statusline=antigravity_statusline,
            jobs=JOBS,
            block_percent=settings["block_percent"],
        )
    except Exception as exc:  # every provider is guarded; this is the command's last line of reporting
        print(json.dumps({"status": "error", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}))
        return 1
    result["quota_blocks"] = quota_file_report(
        settings["quota_blocks_file"], lambda path: write_quota_blocks(path, result, now)
    )
    print(json.dumps(result, indent=1))
    # A blocks file that could not be read or written must not look like success: the recorded blocks are then
    # unknown, not empty (the readings above are still printed).
    return 1 if result["quota_blocks"]["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
