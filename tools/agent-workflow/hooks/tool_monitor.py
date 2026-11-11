"""PostToolUse / PostToolUseFailure monitor: P-01 harness-error streak, P-07 `[ps-error-hidden]`, P-04 `[eol-warning]`.

- PostToolUseFailure (not an interrupt): a harness-level error (guard_config `tool_broken.harness_error_patterns`, never
  an ordinary `Exit code N`) counts toward the tool's streak; the same normalised error `streak_length` times in a row
  flags the tool (`[tool-broken] <tool>`), and tool_guard.py then blocks it until the documented reset (tool_health.py).
- PostToolUse: a success ends the streak. A PowerShell result whose output holds an error record although the call
  "succeeded" gets `[ps-error-hidden]`. After Write/Edit (and after shell calls when `eol.check_after_shell`), a tracked
  file with CR line endings where Git expects LF gets `[eol-warning] <file>`, once per file per session.
Output is `additionalContext`; this hook never blocks. A crash is logged to `guard-crash.log` and exits 0.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(HOOKS.parent))
import guard_config as gc  # noqa: E402
import process_guard as pg  # noqa: E402
import tool_health as th  # noqa: E402

import eol_check  # noqa: E402

EDIT_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
SHELL_TOOLS = {"Bash", "PowerShell"}
CRASH_LOG = pg.STATE_DIR / "guard-crash.log"


def streak(event: dict[str, Any], cfg: dict[str, Any], root: Path) -> list[str]:
    tb = cfg["tool_broken"]
    tool = event.get("tool_name")
    if tb["mode"] == "off" or not isinstance(tool, str) or not gc.TOOL_NAME.match(tool):
        return []
    session = event.get("session_id") if isinstance(event.get("session_id"), str) else None
    directory = th.state_dir(cfg, root)
    signature = None
    if event.get("hook_event_name") == "PostToolUseFailure" and not event.get("is_interrupt"):
        error = event.get("error")
        signature = th.harness_signature(error, cfg) if isinstance(error, str) else None
    if th.record(directory, session, tool, signature, tb["streak_length"]):
        entry = th.flagged(directory, session, tool) or {"sig": signature}
        return [th.message(tool, entry, directory, root)]
    return []


def _response_text(response: Any) -> str:
    if isinstance(response, dict):
        return "\n".join(str(response.get(k) or "") for k in ("stdout", "stderr"))
    return response if isinstance(response, str) else ""


def ps_error(event: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    pe = cfg["ps_error"]
    if (
        pe["mode"] == "off"
        or event.get("tool_name") != "PowerShell"
        or event.get("hook_event_name") != "PostToolUse"
    ):
        return []
    text = _response_text(event.get("tool_response"))
    hits = [p for p in pe["patterns"] if re.search(p, text)]
    if not hits:
        return []
    return [
        "[ps-error-hidden] The PowerShell call reported success, but its output holds a PowerShell error record "
        "(CategoryInfo / FullyQualifiedErrorId). A command inside it failed: read the output, and use "
        "`-ErrorAction Stop` or check `$?` before relying on the result."
    ]


def _tree(path: Path) -> Path | None:
    """The innermost Git work tree (a directory holding `.git`) at or above the path."""
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _warned_path(session: str | None) -> Path:
    return pg.STATE_DIR / f"eol-warned-{th._session_key(session)}.json"


def _load_warned(session: str | None) -> set[str]:
    try:
        data = json.loads(_warned_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def _save_warned(session: str | None, warned: set[str]) -> None:
    path = _warned_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sorted(warned)), encoding="utf-8")
    for attempt in range(th.REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == th.REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(th.REPLACE_DELAY)


def eol(event: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    ec = cfg["eol"]
    tool = event.get("tool_name")
    if ec["mode"] == "off" or event.get("hook_event_name") != "PostToolUse":
        return []
    raw_input = event.get("tool_input")
    tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    timeout = cfg["git_timeout_seconds"]
    checks: list[tuple[Path, list[str] | None]] = []
    if tool in EDIT_TOOLS:
        name = tool_input.get(EDIT_TOOLS[tool])
        if not isinstance(name, str) or not name:
            return []
        path = Path(name)
        tree = _tree(path)
        if tree is None or not path.is_file():
            return []
        checks.append((tree, [Path(os.path.relpath(path, tree)).as_posix()]))
    elif tool in SHELL_TOOLS and ec["check_after_shell"]:
        cwd = event.get("cwd")
        tree = _tree(Path(cwd)) if isinstance(cwd, str) and cwd else None
        if tree is None:
            return []
        checks.append((tree, None))
    else:
        return []
    session = event.get("session_id") if isinstance(event.get("session_id"), str) else None
    warned = _load_warned(session)
    notes = []
    for tree, paths in checks:
        try:
            entries = eol_check.ls_eol(tree, paths, modified=paths is None, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            continue  # a warning only: no git, no warning
        for finding in eol_check.findings(tree, entries):
            key = os.path.normcase(str(tree / finding.path))
            if key in warned:
                continue
            warned.add(key)
            fix = (
                " Run `python tools/agent-workflow/eol_check.py --fix`." if finding.fixable else ""
            )
            notes.append(f"[eol-warning] {finding.path}: {finding.problem}.{fix}")
    if notes:
        _save_warned(session, warned)
    return notes


def run(event: dict[str, Any], cfg: dict[str, Any], root: Path = gc.ROOT) -> list[str]:
    return streak(event, cfg, root) + ps_error(event, cfg) + eol(event, cfg)


def _crash(event: Any) -> None:
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG.open("a", encoding="utf-8") as fh:
            record = {
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "hook": "tool_monitor",
                "tool": event.get("tool_name") if isinstance(event, dict) else None,
                "traceback": traceback.format_exc()[-2000:],
            }
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config = args[args.index("--config") + 1] if "--config" in args[:-1] else None
    try:
        event = json.loads(pg.read_event())
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    try:
        cfg = gc.load(gc.resolve_path(config))
        notes = run(event, cfg)
    except gc.ConfigError as exc:
        notes = [
            f"[guard-config] {exc}. Fix it with the Edit tool; the shell guards block Bash/PowerShell until then."
        ]
    except Exception:
        _crash(event)
        return 0
    if notes:
        name = (
            event.get("hook_event_name")
            if event.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure")
            else "PostToolUse"
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": name,
                        "additionalContext": "\n".join(notes),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
