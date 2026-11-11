"""Usage snapshot of the CLI agents and the per-turn ledger (decision 596). Documentation: README.md, "Codex workers".

  python -B usage_snapshot.py [--config <codex_profiles.json>] [--codex-home <dir>] [--claude-file <PM-written json>]

Prints one JSON object: `collected_at`, `claude`, `codex`, `antigravity`. A source that is absent, unreadable, failing or
(Claude file) older than `usage.stale_minutes` reports `{"status": "unknown", "reason": ...}`, never 0. Codex windows come
from the newest `token_count` row by its own timestamp across the recent session files (file mtime only orders the
scan) and are named by their `window_minutes`, never by position. Antigravity comes from the optional
`usage.antigravity_command` (`-p /usage --output-format json`, no shell), `unknown` when it is not configured. A Codex row moves
only on a Codex call, so an idle provider is not stale by row age: a window is valid until its `resets_at` and counts as
0% (`state: reset`) after it; only a window without `resets_at` needs a row within `usage.stale_minutes`. The ledger
helpers write one JSONL row per worker turn (cli_worker.py calls them).
"""

from __future__ import annotations

import argparse
import datetime as dt
import heapq
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# Protocol-bound maximum tail bytes decoded from one session file; candidate count lives in usage config.
TAIL_BYTES = 1_048_576
WEEKLY_MINUTES = 10080


def sibling(name: str) -> Any:
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {HERE / (name + '.py')}")
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


def parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def windows_of(limits: Mapping[str, Any], now: float, fresh: bool = True) -> list[dict[str, Any]]:
    """Every window present in a `rate_limits` object (`primary`, `secondary`, ...), none invented.

    A Codex row only moves on a Codex call, so an idle provider is not stale: a window stays valid until its
    `resets_at`, and a window past it counts as 0% (PM note 2026-11-07). Only a window without `resets_at` has no
    expiry of its own and is trusted while the row is `fresh` (age within `usage.stale_minutes`)."""
    found = []
    for key in sorted(limits):
        window = limits[key]
        if not isinstance(window, dict):
            continue
        minutes = window.get("window_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            continue
        used, resets = number(window.get("used_percent")), number(window.get("resets_at"))
        entry: dict[str, Any] = {"label": window_label(minutes), "window_minutes": minutes}
        if resets is not None:
            entry["resets_at"] = dt.datetime.fromtimestamp(resets, dt.UTC).isoformat()
        if used is None:
            entry.update(state="unknown", used_percent=None)
        elif resets is not None and resets < now:
            entry.update(
                state="reset", used_percent=0.0, reason="window reset after the row was written"
            )
        elif resets is None and not fresh:
            entry.update(
                state="unknown", used_percent=None, reason="no reset time and the row is stale"
            )
        else:
            entry.update(state="ok", used_percent=used)
        found.append(entry)
    return found


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
            return {"payload": payload, "time": parse_time(row.get("timestamp")), "file": path}
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
    can precede row timestamps: every bounded candidate is compared. A row without a timestamp uses file mtime."""
    sibling("codex_backend")._positive_int("usage", "session_file_limit", session_file_limit)
    sessions = home / "sessions"
    if not sessions.is_dir():
        return unknown("no sessions directory", source=str(sessions))
    best: tuple[float, str, dict[str, Any]] | None = None
    for modified, path in newest_sessions(sessions, session_file_limit):
        row = last_token_row(path)
        if row is None:
            continue
        seen, basis = row["time"], "row timestamp"
        if seen is None:
            seen, basis = modified, "file mtime (the row has no timestamp)"
        if best is None or seen > best[0]:
            best = (seen, basis, row)
    if best is None:
        return unknown("no token_count row in the newest sessions", source=str(sessions))
    seen, basis, row = best
    age = max(0.0, now - seen) / 60
    payload = row["payload"]
    limits = payload.get("rate_limits")
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    return {
        "status": "ok",
        "source": "newest session token_count row",
        "observed_at": dt.datetime.fromtimestamp(seen, dt.UTC).isoformat(),
        "observed_from": basis,
        "age_minutes": round(age, 1),
        "plan_type": limits.get("plan_type") if isinstance(limits, dict) else None,
        "windows": windows_of(limits, now, age <= stale_minutes)
        if isinstance(limits, dict)
        else [],
        "total_token_usage": usage if isinstance(usage, dict) else None,
    }


# Fixed arguments of the Antigravity usage query: protocol-bound (the CLI's own slash command and output format).
ANTIGRAVITY_ARGS = ("-p", "/usage", "--output-format", "json")


def antigravity_snapshot(
    command: str | None, timeout_seconds: int | None, runner: Any = subprocess.run
) -> dict[str, Any]:
    """`<antigravity_command> -p /usage --output-format json`, run from an argument list (no shell). The JSON object it
    prints is passed through untouched; a missing command, a spawn failure, a timeout, a non-zero exit or output that is
    not a JSON object is `unknown`, never 0. The live shape of the output is checked by the PM, not assumed here."""
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
    try:
        data = json.loads(done.stdout)
    except (ValueError, RecursionError):
        return unknown("the usage command did not print JSON")
    if not isinstance(data, dict):
        return unknown("the usage command did not print a JSON object")
    return {"status": "ok", "source": "usage command", "data": data}


def claude_snapshot(path: Path | None, now: float, stale_minutes: int) -> dict[str, Any]:
    """The PM-written Claude usage file: passed through when it is a fresh JSON object, otherwise unknown."""
    if path is None:
        return unknown("no --claude-file given")
    try:
        modified = path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return unknown("file missing or not JSON", source=str(path))
    if not isinstance(data, dict):
        return unknown("file is not a JSON object", source=str(path))
    age = max(0.0, now - modified) / 60
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
) -> dict[str, Any]:
    stamp = time.time() if now is None else now
    return {
        "collected_at": dt.datetime.fromtimestamp(stamp, dt.UTC).isoformat(),
        "claude": guarded(claude_snapshot, claude_file, stamp, stale_minutes),
        "codex": guarded(codex_snapshot, home, stamp, stale_minutes, session_file_limit),
        "antigravity": guarded(
            antigravity_snapshot, antigravity_command, command_timeout_seconds, runner
        ),
    }


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
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, help="codex_profiles.json (default: the tracked file)")
    ap.add_argument(
        "--codex-home",
        type=Path,
        help="Codex home (default: runtime.codex_home, CODEX_HOME, ~/.codex)",
    )
    ap.add_argument("--claude-file", type=Path, help="PM-written JSON with the Claude usage")
    args = ap.parse_args()
    backend = sibling("codex_backend")
    settings = backend.load_usage_settings(args.config)
    home = args.codex_home or backend.effective_home(
        backend.load_home_setting(args.config), ROOT, os.environ
    )
    result = collect(
        home,
        args.claude_file,
        settings["stale_minutes"],
        settings["session_file_limit"],
        antigravity_command=settings["antigravity_command"],
        command_timeout_seconds=settings["command_timeout_seconds"],
    )
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
