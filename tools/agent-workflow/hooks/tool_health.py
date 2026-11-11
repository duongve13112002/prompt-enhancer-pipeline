"""Tool health (P-01): the harness-error streak state, the SessionStart Bash probe and the reset command.

State lives in `<root>/<tool_broken.reset_dir>/` (today `.tmp/pm-state/tool-health/`):
- `streak-<session>.json`: per tool, the last harness-error signature, its count and when the tool was flagged.
- `reset-<Tool>`: any file; when it is newer than a flag, the flag no longer blocks (write it with the Write tool, or
  run `python tools/agent-workflow/hooks/tool_health.py reset <Tool>`).

Hook use: `tool_health.py session-start [--config F]` (SessionStart). CLI: `reset <Tool>`, `status`.
Standard library only (the hooks run with `python -B -I`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_config as gc

# Windows: a reader can hold the file open for a moment (as precompact_snapshot).
REPLACE_ATTEMPTS = 40
REPLACE_DELAY = 0.05


def state_dir(cfg: dict[str, Any], root: Path = gc.ROOT) -> Path:
    return root / str(cfg["tool_broken"]["reset_dir"])


def _session_key(session: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session or "unknown")[:80] or "unknown"


def _streak_path(directory: Path, session: str | None) -> Path:
    return directory / f"streak-{_session_key(session)}.json"


def reset_path(directory: Path, tool: str) -> Path:
    if not gc.TOOL_NAME.match(tool):
        raise ValueError(f"not a tool name: {tool!r}")
    return directory / f"reset-{tool}"


def load_state(directory: Path, session: str | None) -> dict[str, Any]:
    try:
        data = json.loads(_streak_path(directory, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(directory: Path, session: str | None, data: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = _streak_path(directory, session)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(REPLACE_DELAY)


def _reset_time(directory: Path, tool: str) -> float | None:
    try:
        return reset_path(directory, tool).stat().st_mtime
    except (OSError, ValueError):
        return None


def flagged(directory: Path, session: str | None, tool: str) -> dict[str, Any] | None:
    """The flag entry when the tool is flagged in this session and no later reset exists."""
    entry = load_state(directory, session).get(tool)
    if not isinstance(entry, dict) or not isinstance(entry.get("flagged_at"), (int, float)):
        return None
    reset = _reset_time(directory, tool)
    # Strictly newer: a reset in the same clock tick as the flag (Windows ticks are coarse) does not clear it.
    if reset is not None and reset > entry["flagged_at"]:
        return None
    return entry


def normalise(error: str) -> str:
    """Signature of a harness error: its first non-empty line with numbers, hex and paths' digits collapsed, so the
    same failure with another line number or PID (`line 74` / `line 82`, `PID 1234` / `PID 5678`) matches."""
    line = next((ln.strip() for ln in error.splitlines() if ln.strip()), "")
    line = re.sub(r"0x[0-9a-fA-F]+", "<hex>", line)
    line = re.sub(r"\d+", "<n>", line)
    return re.sub(r"\s+", " ", line)[:300]


def harness_signature(error: str, cfg: dict[str, Any]) -> str | None:
    """The signature when the error is a harness-level failure (not an ordinary non-zero exit), else None."""
    tb = cfg["tool_broken"]
    first = next((ln for ln in error.splitlines() if ln.strip()), "")
    if re.search(tb["ordinary_exit_pattern"], first.strip()):
        return None
    if any(re.search(p, error) for p in tb["harness_error_patterns"]):
        return normalise(error)
    return None


def record(
    directory: Path, session: str | None, tool: str, signature: str | None, streak: int
) -> bool:
    """Record one result of `tool`: a harness-error signature, or None for anything else (which ends a streak).
    Returns True when this call flagged the tool."""
    state = load_state(directory, session)
    entry = state.get(tool) if isinstance(state.get(tool), dict) else None
    if signature is None:
        if entry is None or entry.get("flagged_at") is not None:
            return False  # nothing to reset; a flag stays until the documented reset
        state.pop(tool, None)
        save_state(directory, session, state)
        return False
    reset = _reset_time(directory, tool)
    if (
        entry
        and entry.get("flagged_at") is not None
        and reset is not None
        and reset > entry["flagged_at"]
    ):
        entry = None  # a reset clears the old flag: start counting again
    count = entry["count"] + 1 if entry and entry.get("sig") == signature else 1
    newly = count >= streak and not (entry and entry.get("flagged_at") is not None)
    flagged_at = time.time() if newly else (entry or {}).get("flagged_at")
    state[tool] = {"sig": signature, "count": count, "flagged_at": flagged_at}
    save_state(directory, session, state)
    return newly


def flag(directory: Path, session: str | None, tool: str, signature: str) -> None:
    state = load_state(directory, session)
    state[tool] = {"sig": signature, "count": 0, "flagged_at": time.time()}
    save_state(directory, session, state)


def message(tool: str, entry: dict[str, Any], directory: Path, root: Path = gc.ROOT) -> str:
    try:
        shown = reset_path(directory, tool).relative_to(root).as_posix()
    except ValueError:
        shown = str(reset_path(directory, tool))
    return (
        f"[tool-broken] {tool}: the same harness error repeated ({entry.get('sig', '?')}). Use another tool "
        f"(PowerShell, Read, Grep, Glob) instead of retrying. After the cause is fixed, reset it: write any file to "
        f"`{shown}` with the Write tool, or run `python tools/agent-workflow/hooks/tool_health.py reset {tool}`."
    )


def platform_key() -> str:
    return "windows" if os.name == "nt" else "posix"


def probe_shell(cfg: dict[str, Any]) -> tuple[str | None, str]:
    """The shell the harness's Bash tool runs: the platform's configured environment variable when set (on Windows
    Claude Code reads `CLAUDE_CODE_GIT_BASH_PATH`), else the first configured path that exists. Never a PATH lookup:
    on Windows `CreateProcess` finds the WSL launcher in System32 before Git Bash. Returns (path, how) or (None, why)."""
    spec = cfg["tool_broken"]["probe_shell"][platform_key()]
    named = os.environ.get(spec["env"], "")
    if named:
        if os.path.isabs(named) and os.path.isfile(named):
            return named, f"${spec['env']}"
        return None, f"${spec['env']} names no file: {named[:160]!r}"
    for candidate in spec["paths"]:
        if os.path.isfile(candidate):
            return candidate, "configured path"
    return None, f"no configured shell exists ({', '.join(spec['paths'])}); set ${spec['env']}"


def _decode(data: bytes) -> str:
    # The WSL launcher and some Windows tools write UTF-16; a NUL byte in short output is its marker.
    if b"\x00" in data:
        return data.decode("utf-16-le", "replace").replace("\x00", "")
    return data.decode("utf-8", "replace")


def probe(cfg: dict[str, Any]) -> str | None:
    """Run the probe with the harness's shell; return a problem description, or None when it answered as expected."""
    tb = cfg["tool_broken"]
    shell, how = probe_shell(cfg)
    if shell is None:
        return how
    try:
        proc = subprocess.run(
            [shell, *tb["probe_args"]],
            capture_output=True,
            timeout=tb["probe_timeout_seconds"],
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"probe of {shell} timed out after {tb['probe_timeout_seconds']} s"
    except OSError as exc:
        return f"probe could not start {shell}: {exc.strerror or exc}"
    out = _decode(proc.stdout).strip()
    if proc.returncode != 0 or out != tb["probe_expect"]:
        err = _decode(proc.stderr).strip().splitlines()
        detail = err[0][:160] if err else ""
        return f"probe of {shell} ({how}) exited {proc.returncode} with output {out[:80]!r} {detail}".strip()
    return None


def session_start(event: dict[str, Any], cfg: dict[str, Any], root: Path = gc.ROOT) -> str | None:
    """SessionStart: probe the harness's shell and check PATH length. Both only warn: a probe cannot reproduce the
    harness exactly, so only the real harness-error streak (tool_monitor) flags and blocks a tool."""
    tb = cfg["tool_broken"]
    if tb["mode"] == "off":
        return None
    notes: list[str] = []
    path_chars = len(os.environ.get("PATH", ""))
    if path_chars > tb["max_path_chars"]:
        notes.append(
            f"[tool-health] PATH holds {path_chars} characters (limit {tb['max_path_chars']}); Bash may fail to "
            "spawn with ENAMETOOLONG. Shorten PATH before relying on Bash."
        )
    problem = probe(cfg)
    if problem:
        notes.append(
            f"[tool-health] Bash probe failed: {problem}. Bash may be broken; prefer PowerShell, Read, Grep or Glob "
            "if a Bash call fails with a harness error (two in a row flag it)."
        )
    return "\n".join(notes) or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config", help="guard config (default tools/agent-workflow/guard_config.json)"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("session-start", help="SessionStart hook: probe Bash")
    reset = sub.add_parser("reset", help="clear a [tool-broken] flag")
    reset.add_argument("tool")
    sub.add_parser("status", help="list flagged tools per session")
    args = parser.parse_args(argv)
    if args.action == "session-start":
        try:
            event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace") or "{}")
        except ValueError:
            event = {}
        try:
            cfg = gc.load(gc.resolve_path(args.config))
        except gc.ConfigError as exc:
            print(
                json.dumps(
                    _context(
                        f"[guard-config] {exc}. The shell guards block Bash/PowerShell until it is fixed (use Edit)."
                    )
                )
            )
            return 0
        text = session_start(event if isinstance(event, dict) else {}, cfg)
        if text:
            print(json.dumps(_context(text)))
        return 0
    try:
        cfg = gc.load(gc.resolve_path(args.config))
    except gc.ConfigError as exc:
        print(f"guard config error: {exc}", file=sys.stderr)
        return 1
    directory = state_dir(cfg)
    if args.action == "reset":
        try:
            path = reset_path(directory, args.tool)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"), encoding="utf-8")
        os.utime(path)
        print(f"reset {args.tool}: {path}")
        return 0
    for path in sorted(directory.glob("streak-*.json")):
        session = path.stem[len("streak-") :]
        for tool in sorted(load_state(directory, session)):
            entry = flagged(directory, session, tool)
            if entry:
                print(f"{session}\t{tool}\tflagged\t{entry.get('sig')}")
    return 0


def _context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


if __name__ == "__main__":
    sys.exit(main())
