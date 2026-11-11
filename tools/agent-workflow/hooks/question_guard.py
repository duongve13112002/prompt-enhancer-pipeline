"""PreToolUse guard for the `ArtifactData` tool (decisions 503, T-AUDIT-GATES): a question is checked before it is asked.

A write into the configured collection (`question_guard.collection`, today `questions`) that creates or replaces a
question with options, as a single `set` or as a `set` inside a `batch`, is refused unless:

1. `data.<verified_field>` is a non-empty list of non-empty strings: one per option, or one for the whole question.
   Each string is evidence (`file.py:123`, `decision 148`, `user words`, a quoted test name).
2. `data.<checked_against_field>` (S03) is a non-empty string or list of strings naming at least one decision
   (`decision_pattern`) and one standing rule (`rule_pattern`), and every named id exists in the decision files /
   the standing-rules file (rows matched by `decision_row_pattern` / `rule_row_pattern`).
3. No option repeats, after normalisation (NFKC, accents and case folded, punctuation and spacing collapsed), an option
   of an already answered question (S06): the mirror is `answered_dir`, one JSON document per question, those whose
   `status_field` equals `answered_status`. Options shorter than `min_option_chars` are exempt (labels such as "A"),
   as is the question's own earlier text. A question may repeat an answered option only when `data.<reopens_field>`
   lists that question's id (file stem or `ref`) and an evidence string matches `reopen_evidence_pattern` (the user's
   verbatim words); the exemption covers the listed ids only. Only exact normalised equality of an option's label,
   description or both together is detected, not a paraphrase.

A mirror or id file that cannot be read is itself a finding (fail closed), named by path. An `update` passes unless it
sets `options` (then the same rules apply); updates that only set `status`, `answer` or `pm_status` pass. Reads, other
tools and other collections pass.

Input shape (observed in PM session transcripts, not documented by Claude Code): `tool_input` is
`{action, url, collection, doc_id, data, if_version}`; `action: "batch"` carries `writes: [{op, collection,
doc_id, data, if_version}]`. Fail-open policy: unreadable input, an invalid config or any crash prints nothing
(config errors print one `additionalContext` line) and exits 0; only a malformed question exits 2 (mode `block`).
Standard library only (runs with `python -B -I`).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_config as gc

TOOL_NAME = "ArtifactData"  # protocol-bound: the tool name Claude Code reports
WRITE_OPS = ("set", "update")  # protocol-bound: the ArtifactData operations that store a document
# Protocol-bound: the keys of an option object in a question document (an option may also be a plain string).
OPTION_KEYS = ("label", "description")


def read_event() -> str:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read()
    return str(stream.read().decode("utf-8-sig", errors="replace"))


def writes_of(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Every document write of the call: the call itself for `set`/`update`, each item for `batch`."""
    action = tool_input.get("action")
    if action in WRITE_OPS:
        return [{**tool_input, "op": action}]
    items = tool_input.get("writes")
    if action == "batch" and isinstance(items, list):
        return [
            {"collection": tool_input.get("collection"), **w} for w in items if isinstance(w, dict)
        ]
    return []


def normalise(text: str) -> str:
    """Case, accent, punctuation and spacing folded: two spellings of one sentence compare equal."""
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", stripped)).split())


def option_texts(option: Any, min_chars: int) -> set[str]:
    """The normalised texts an option can repeat: its label, its description and both together."""
    parts: list[str] = []
    if isinstance(option, str):
        parts = [option]
    elif isinstance(option, dict):
        values = [option.get(key) for key in OPTION_KEYS]
        texts = [v for v in values if isinstance(v, str)]
        parts = [*texts, " ".join(texts)] if texts else []
    return {n for n in (normalise(p) for p in parts) if len(n) >= min_chars}


def evidence_problem(name: str, data: dict[str, Any], field: str) -> str | None:
    verified = data.get(field)
    if (
        not isinstance(verified, list)
        or not verified
        or not all(isinstance(v, str) and v.strip() for v in verified)
    ):
        return (
            f"question `{name}` has options but `data.{field}` is not a non-empty list of non-empty strings "
            "(evidence such as `file.py:123`, `decision 148`, `user words`)"
        )
    options = data["options"]
    if isinstance(options, list) and len(verified) not in (1, len(options)):
        return (
            f"question `{name}` has {len(options)} options but `data.{field}` has {len(verified)} entries: "
            "give one per option or one for the whole question"
        )
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def known_ids(root: Path, patterns: Iterable[str], row_pattern: str) -> tuple[set[str], list[str]]:
    """The ids found in the first cell of the rows of the files matched by ``patterns``.

    Returns the ids (casefolded) and the files that matched nothing or could not be read."""
    found: set[str] = set()
    unreadable: list[str] = []
    resolved_root = root.resolve()
    for pattern in patterns:
        matched = sorted(p for p in root.glob(pattern) if p.is_file())
        if not matched:
            unreadable.append(pattern)
        for path in matched:
            try:
                if not path.resolve().is_relative_to(resolved_root):
                    raise OSError("outside the project root")
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                unreadable.append(path.relative_to(root).as_posix())
                continue
            found.update(i.casefold() for i in re.findall(row_pattern, text))
    return found, unreadable


def checked_against_problem(
    name: str, data: dict[str, Any], cfg: dict[str, Any], root: Path
) -> str | None:
    """S03: the question names the decisions and standing rules it was checked against, and they exist."""
    field = cfg["checked_against_field"]
    text = " ".join(_strings(data.get(field)))
    decisions = {i.casefold() for i in re.findall(cfg["decision_pattern"], text)}
    rules = {i.casefold() for i in re.findall(cfg["rule_pattern"], text)}
    if not decisions or not rules:
        return (
            f"question `{name}` has options but `data.{field}` does not name at least one decision "
            f"(`decision N`) and one standing rule (`Sxx`) it was checked against"
        )
    known_decisions, bad_d = known_ids(root, cfg["decision_files"], cfg["decision_row_pattern"])
    known_rules, bad_r = known_ids(root, [cfg["rules_file"]], cfg["rule_row_pattern"])
    if bad_d or bad_r:
        return f"question `{name}`: cannot read the id files to check `data.{field}`: {', '.join(bad_d + bad_r)}"
    unknown = sorted(decisions - known_decisions) + sorted(r.upper() for r in rules - known_rules)
    if unknown:
        return (
            f"question `{name}`: `data.{field}` names ids that do not exist: {', '.join(unknown)}"
        )
    return None


def answered_options(
    root: Path, cfg: dict[str, Any]
) -> tuple[list[tuple[set[str], list[set[str]]]], list[str]]:
    """Per answered question: its ids (file stem, `ref`) and the texts of each option; plus unreadable files."""
    directory = root / cfg["answered_dir"]
    if not directory.is_dir():
        return [], [cfg["answered_dir"]]
    answered: list[tuple[set[str], list[set[str]]]] = []
    unreadable: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            unreadable.append(path.name)
            continue
        if not isinstance(document, dict):
            # A valid JSON file that is not an object (`[]`, `null`) cannot be compared: fail closed.
            unreadable.append(path.name)
            continue
        if document.get(cfg["status_field"]) != cfg["answered_status"]:
            continue
        options = document.get("options")
        if options is not None and not isinstance(options, list):
            unreadable.append(path.name)
            continue
        ids = {path.stem}
        if isinstance(document.get("ref"), str):
            ids.add(document["ref"])
        texts = (
            [option_texts(o, cfg["min_option_chars"]) for o in options]
            if isinstance(options, list)
            else []
        )
        answered.append((ids, texts))
    return answered, unreadable


def repeat_problem(name: str, data: dict[str, Any], cfg: dict[str, Any], root: Path) -> str | None:
    """S06: no option repeats an option of an answered question unless the user reopened that question."""
    options = data.get("options")
    if not isinstance(options, list):
        return None
    answered, unreadable = answered_options(root, cfg)
    if unreadable:
        return (
            f"question `{name}`: cannot compare its options with the answered questions, unreadable: "
            f"{', '.join(unreadable)}"
        )
    reopens = set(_strings(data.get(cfg["reopens_field"])))
    evidence_ok = any(
        re.search(cfg["reopen_evidence_pattern"], v)
        for v in _strings(data.get(cfg["verified_field"]))
    )
    repeats: list[str] = []
    own = {name}
    for index, option in enumerate(options, start=1):
        mine = option_texts(option, cfg["min_option_chars"])
        if not mine:
            continue
        for ids, theirs in answered:
            if ids & own or not any(mine & t for t in theirs):
                continue
            label = sorted(ids)[0]
            if ids & reopens and evidence_ok:
                continue
            repeats.append(f"option {index} repeats an option of the answered question `{label}`")
    if not repeats:
        return None
    hint = (
        f"set `data.{cfg['reopens_field']}` to the ids and quote the user's words in `data.{cfg['verified_field']}` "
        '(`user words: "..."`) to reopen a question'
    )
    return f"question `{name}`: " + "; ".join(repeats) + f" ({hint})"


def problem(write: dict[str, Any], cfg: dict[str, Any], root: Path) -> list[str]:
    """Why this write is refused (empty when it passes)."""
    if write.get("op") not in WRITE_OPS or write.get("collection") != cfg["collection"]:
        return []
    data = write.get("data")
    if not isinstance(data, dict) or "options" not in data:
        return []
    name = str(write.get("doc_id") or "?")
    checks = (
        evidence_problem(name, data, cfg["verified_field"]),
        checked_against_problem(name, data, cfg, root),
        repeat_problem(name, data, cfg, root),
    )
    return [c for c in checks if c]


def main() -> int:
    try:
        event = json.loads(read_event())
        if not isinstance(event, dict) or event.get("tool_name") != TOOL_NAME:
            return 0
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        try:
            cfg = gc.load()["question_guard"]
        except gc.ConfigError as exc:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "additionalContext": f"question_guard config invalid: {exc}",
                        }
                    }
                )
            )
            return 0
        if cfg["mode"] == "off":
            return 0
        found = [p for w in writes_of(tool_input) for p in problem(w, cfg, gc.ROOT)]
        if not found:
            return 0
        message = "question_guard: " + "; ".join(found)
        if cfg["mode"] == "warn":
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "additionalContext": message,
                        }
                    }
                )
            )
            return 0
        sys.stderr.write("BLOCKED by " + message + "\n")
        return 2
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
