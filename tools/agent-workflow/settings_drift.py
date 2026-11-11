"""Report drift between workflow templates, docs and the code they describe (R3 audit F-21, P-16).

Usage: python -B -I settings_drift.py [--root <project root>] [--live <settings.local.json>]
Exit 0: no drift. Exit 1: drift found. A missing live settings file is reported as not_run for that
check (the file is git-ignored and absent on CI); the other checks still run. Nothing is written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PreCompact", "SessionStart")


def _hook_summary(
    settings: dict[str, object],
) -> dict[str, list[tuple[str | None, int | None, str]]]:
    """Event -> list of (matcher, timeout, script name) in file order."""
    hooks = settings.get("hooks", {})
    out: dict[str, list[tuple[str | None, int | None, str]]] = {}
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        rows: list[tuple[str | None, int | None, str]] = []
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []):
                parts = [str(a) for a in hook.get("args", [])] or [str(hook.get("command", ""))]
                script = next((Path(p).name for p in parts if p.endswith(".py")), "")
                rows.append((group.get("matcher"), hook.get("timeout"), script))
        out[event] = rows
    return out


def compare_hooks(example: dict[str, object], live: dict[str, object]) -> list[str]:
    """Differences in matcher, timeout and hook script per event; the example is the template side."""
    ex, lv = _hook_summary(example), _hook_summary(live)
    drifts: list[str] = []
    for event in sorted(set(ex) | set(lv)):
        if event not in ex or event not in lv:
            drifts.append(
                f"hooks.{event}: present only in {'live' if event in lv else 'example'} settings"
            )
            continue
        if len(ex[event]) != len(lv[event]):
            drifts.append(
                f"hooks.{event}: {len(ex[event])} example hook(s) vs {len(lv[event])} live"
            )
            continue
        for (e_match, e_time, e_script), (l_match, l_time, l_script) in zip(
            ex[event], lv[event], strict=True
        ):
            if e_script != l_script:
                drifts.append(
                    f"hooks.{event}: script {e_script!r} (example) vs {l_script!r} (live)"
                )
            if e_match != l_match:
                drifts.append(
                    f"hooks.{event} {e_script}: matcher {e_match!r} (example) vs {l_match!r} (live)"
                )
            if e_time != l_time:
                drifts.append(
                    f"hooks.{event} {e_script}: timeout {e_time} (example) vs {l_time} (live)"
                )
    return drifts


def check_footer_slots(footer: str, test_slot_src: str) -> list[str]:
    """The footer must state the normal limit and the fallback exactly as test_slot.py implements them."""
    normal = re.search(r"\((\d+) normally", test_slot_src)
    fallback = re.search(r"except \(OSError, ValueError\):\s*return (\d+)", test_slot_src)
    if normal is None or fallback is None:
        return [
            "test_slot.py: could not read the normal limit or the fallback (parser out of date)"
        ]
    drifts: list[str] = []
    stated = re.search(r"allows (\d+) test commands", footer)
    if stated is None or stated.group(1) != normal.group(1):
        drifts.append(
            f"footer rule 10: limit {stated.group(1) if stated else 'missing'} vs test_slot.py normal {normal.group(1)}"
        )
    fall = re.search(r"test-slots/max`[^.;)]*else (\d+)", footer)
    if fall is None or fall.group(1) != fallback.group(1):
        drifts.append(
            f"footer rule 10: fallback {fall.group(1) if fall else 'not stated'} vs test_slot.py fallback {fallback.group(1)}"
        )
    return drifts


def check_mcp_docstring(cli_worker_src: str) -> list[str]:
    """A 'no MCP servers' claim is only true if --mcp defaults to minimal."""
    default = re.search(r'"--mcp",\s*choices=\w+,\s*default="(\w+)"', cli_worker_src)
    if default is None:
        return ["cli_worker.py: could not read the --mcp default (parser out of date)"]
    doc = cli_worker_src.split('"""', 2)[1] if cli_worker_src.count('"""') >= 2 else ""
    if re.search(r"no\s+MCP\s+servers", doc) and default.group(1) != "minimal":
        return [
            f'cli_worker.py docstring says "no MCP servers" but --mcp defaults to {default.group(1)!r}'
        ]
    return []


def run(root: Path, live: Path) -> tuple[list[str], list[str]]:
    drifts: list[str] = []
    not_run: list[str] = []
    wf = root / "tools" / "agent-workflow"
    if live.is_file():
        example = json.loads((wf / "settings.example.json").read_text(encoding="utf-8"))
        drifts += compare_hooks(example, json.loads(live.read_text(encoding="utf-8")))
    else:
        not_run.append(f"live settings not found: {live}")
    footer = (root / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md").read_text(encoding="utf-8")
    drifts += check_footer_slots(footer, (wf / "test_slot.py").read_text(encoding="utf-8"))
    drifts += check_mcp_docstring((wf / "cli_worker.py").read_text(encoding="utf-8"))
    return drifts, not_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--live", type=Path, default=None)
    args = parser.parse_args(argv)
    live = args.live if args.live is not None else args.root / ".claude" / "settings.local.json"
    drifts, not_run = run(args.root, live)
    for line in not_run:
        print(f"not_run: {line}")
    for line in drifts:
        print(f"drift: {line}")
    print(f"{len(drifts)} drift(s)")
    return 1 if drifts else 0


if __name__ == "__main__":
    sys.exit(main())
