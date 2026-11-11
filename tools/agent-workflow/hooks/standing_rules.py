"""SessionStart hook for the PM session: inject the peer rules and the user's standing rules, each whole.

A second SessionStart hook next to `session_context.py` (decision 165 follow-up, T-MEM): the harness caps one hook's
`additionalContext` at 10,000 characters, so the rules get their own output instead of competing with the
continuation note, git state and decision index. Prints, in this order:
- loud warnings first (they must survive any cut): a file over its `doc_budgets.json` budget, a missing or malformed
  rule line, a total over the harness cap;
- `.agents/policy/AGENT_PEER_RULES.md`, whole;
- `.agents/policy/USER_STANDING_RULES.md`, whole, with a one-line count of its rules and of those without a mechanical check.
A file over its budget is never cut here: the line `OVER BUDGET` names it and the whole text still goes out.
Runs on `startup`, `resume` and `compact`; cli_worker sessions are skipped. Standard library only (`python -B -I`);
fails open: any problem becomes one visible warning line and the exit code stays 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR") or ROOT / ".tmp" / "pm-state")
BUDGETS = Path(__file__).resolve().parents[1] / "doc_budgets.json"
WORKER_ENV_FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
RULES_REL = ".agents/policy/USER_STANDING_RULES.md"
PEER_REL = ".agents/policy/AGENT_PEER_RULES.md"
# Protocol-bound: Claude Code shows the model at most 10,000 characters of one hook's additionalContext
# (memory_hooks_config.HARD_INJECT_CAP; a test keeps the two equal).
HARD_INJECT_CAP = 10000
# Grammar of one rule line (protocol-bound: the format the file documents): `Sxx | rule | source | check`.
RULE_LINE = re.compile(r"^S(\d+)\s*\|")
NO_CHECK = "remembered only"
FIELD_COUNT = 4


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n").strip()
    except OSError:
        return None


def is_worker(session: object) -> bool:
    if os.environ.get(WORKER_ENV_FLAG) == "1":
        return True
    data = load_json(STATE / "cli-workers.json")
    if not isinstance(data, dict) or not session:
        return False
    workers = data.get("workers", data)
    return isinstance(workers, dict) and any(
        isinstance(w, dict) and w.get("session_id") == session for w in workers.values()
    )


def parse_rules(text: str) -> tuple[list[str], list[str], list[str]]:
    """(ids, ids without a mechanical check, problems) of the rule lines.

    A line starting `S<digits> |` is a rule line and needs exactly four non-empty `|` separated fields; a repeated id
    is a problem. Other lines (title, notes, blank) are free text."""
    ids: list[str] = []
    unchecked: list[str] = []
    problems: list[str] = []
    for line in text.split(
        "\n"
    ):  # not splitlines(): U+2028 inside a rule must not start a fake line
        match = RULE_LINE.match(line)
        if not match:
            continue
        fields = [part.strip() for part in line.split("|")]
        rule_id = fields[0]
        if len(fields) != FIELD_COUNT or not all(fields):
            problems.append(f"{rule_id}: needs {FIELD_COUNT} non-empty fields")
            continue
        if rule_id in ids:
            problems.append(f"{rule_id}: repeated id")
            continue
        ids.append(rule_id)
        if fields[3].strip("`") == NO_CHECK:
            unchecked.append(rule_id)
    return ids, unchecked, problems


def budget_of(rel: str) -> int | None:
    spec = load_json(BUDGETS)
    project = spec.get("project") if isinstance(spec, dict) else None
    limit = project.get(rel) if isinstance(project, dict) else None
    return limit if isinstance(limit, int) and not isinstance(limit, bool) else None


def build_context() -> str:
    """The text to inject: warnings first, then the peer rules, then the standing rules."""
    warnings: list[str] = []
    parts: list[str] = []
    for label, rel in (("peer rules", PEER_REL), ("standing rules", RULES_REL)):
        text = read_text(ROOT / rel)
        if text is None:
            warnings.append(f"MISSING: {rel} could not be read; the {label} are NOT injected")
            continue
        limit = budget_of(rel)
        if limit is None:
            warnings.append(f"no budget for {rel} in {BUDGETS.name}")
        elif len(text) > limit:
            warnings.append(
                f"OVER BUDGET: {rel} has {len(text)} > {limit} characters; injected UNCUT, "
                "condense or archive it verbatim (decision 165)"
            )
        if rel == RULES_REL:
            ids, unchecked, problems = parse_rules(text)
            warnings.extend(f"MALFORMED rule line {problem}" for problem in problems[:5])
            if not ids:
                warnings.append(f"no valid rule line in {rel}")
            text = f"User standing rules ({rel}; {len(ids)} rules, {len(unchecked)} remembered only):\n{text}"
        else:
            text = f"Peer rules ({rel}):\n{text}"
        parts.append(text)
    body = "\n\n".join(parts)
    head = "\n".join(f"[standing_rules] {warning}" for warning in warnings)
    total = len(head) + len(body) + (2 if head and body else 0)
    if total > HARD_INJECT_CAP:
        over = f"[standing_rules] TOTAL {total} > {HARD_INJECT_CAP}: the harness drops the tail of this injection"
        head = f"{over}\n{head}" if head else over
    return f"{head}\n\n{body}".strip() if head else body


def emit(context: str) -> None:
    if context:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
            )
        )


def read_event() -> object:
    stream = getattr(sys.stdin, "buffer", None)
    raw = stream.read().decode("utf-8-sig", errors="replace") if stream else sys.stdin.read()
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def main() -> int:
    try:
        event = read_event()
        if not isinstance(event, dict) or is_worker(event.get("session_id")):
            return 0
        emit(build_context())
    except Exception as exc:  # never block the PM: show the failure instead
        emit(
            f"[standing_rules] hook failed ({type(exc).__name__}); the standing rules are NOT injected"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
