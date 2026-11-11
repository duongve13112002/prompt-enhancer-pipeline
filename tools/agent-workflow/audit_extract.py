"""Digest of the user's own words for the periodic audit agent (T-MEM, standing rule S22).

Usage: python -B -I audit_extract.py [--since YYYY-MM-DD[THH:MM]] [--until ...] [--out FILE]
       [--transcripts-dir DIR] [--max-message-chars N] [--max-total-chars N] [--default-days N]

Reads the PM session transcripts (`<projects>/<project slug>/*.jsonl`, read only) and writes ONE Markdown digest of
the messages the user typed: records with `type: user` and `origin.kind: human` (the field Claude Code writes; a
record without `origin` counts only when its content is plain text that does not start with `<`). Tool results,
task notifications, peer-session messages, compaction summaries, meta and sidechain records, and assistant text are
never read into the digest. Secrets typed into a message are masked (known key shapes and `.secrets` paths); the
`.secrets` directory is never opened. `--since` defaults to the date of the newest `audit-YYYY-MM-DD*.md` in the
audit directory, else `--default-days` days ago. A digest over `--max-total-chars` keeps the newest messages and
says so on its first line. The digest goes under the project root (default
`.tmp/pm-state/audit/user-words-<date>.md`); nothing else is written. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
AUDIT_REL = Path(".tmp") / "pm-state" / "audit"
AUDIT_NAME = re.compile(r"audit-(\d{4}-\d{2}-\d{2})")
# Key shapes that must never reach the audit agent (a pasted credential): provider prefixes and bearer tokens.
SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"
)
SECRETS_PATH = re.compile(r"[^\s\"'`]*\.secrets[^\s\"'`]*", re.IGNORECASE)
NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


class Message(NamedTuple):
    when: dt.datetime
    session: str
    text: str


def default_transcripts_dir(root: Path) -> Path:
    """Claude Code keeps a project's sessions in `~/.claude/projects/<path with every non-alphanumeric as ->`."""
    return Path.home() / ".claude" / "projects" / NON_ALNUM.sub("-", str(root))


def redact(text: str) -> str:
    return SECRETS_PATH.sub("[secrets-path]", SECRET.sub("[secret]", text))


def message_text(record: dict[str, object]) -> str | None:
    """The typed text of a user record, or None when it carries tool results or anything but text."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in ("text", "image"):
                return None
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return None


def is_human(record: object) -> bool:
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("isSidechain"):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, str) and not content.lstrip().startswith("<")


def parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC).astimezone()


def read_messages(files: Iterable[Path]) -> Iterator[Message]:
    for path in files:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"user"' not in line:  # cheap pre-filter before the JSON parse
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not is_human(record):
                    continue
                text = message_text(record)
                when = parse_time(record.get("timestamp"))
                if text and text.strip() and when is not None:
                    yield Message(when, str(record.get("sessionId", path.stem))[:8], text.strip())


def last_audit_date(audit_dir: Path) -> dt.date | None:
    dates = []
    for path in audit_dir.glob("audit-*.md"):
        match = AUDIT_NAME.match(path.name)
        if match:
            try:
                dates.append(dt.date.fromisoformat(match[1]))
            except ValueError:
                continue
    return max(dates) if dates else None


def parse_moment(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).astimezone()  # naive text = local time


def render(
    messages: list[Message], since: dt.datetime, max_message: int, max_total: int, sources: int
) -> str:
    entries = []
    for msg in sorted(messages, key=lambda m: m.when):
        body = redact(msg.text)
        if len(body) > max_message:
            body = (
                body[:max_message].rstrip()
                + f" [cut: {len(msg.text) - max_message} more characters]"
            )
        body = body.replace("\n", "\n  ")
        entries.append(f"- [{msg.when:%Y-%m-%d %H:%M}] ({msg.session}) {body}")
    dropped = 0
    while entries and sum(len(e) + 1 for e in entries) > max_total:
        entries.pop(0)
        dropped += 1
    head = [
        f"# User words since {since:%Y-%m-%d %H:%M} ({len(entries)} messages from {sources} transcript files)",
        "",
        "Typed by the user (`origin.kind: human`); tool output, notifications and assistant text are excluded.",
    ]
    if dropped:
        head.append(
            f"**{dropped} OLDER MESSAGES OMITTED** (digest over {max_total} characters): rerun with a later --since."
        )
    return "\n".join([*head, "", *entries]) + "\n"


def contained(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise SystemExit(f"{resolved} is outside the project root")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--transcripts-dir", type=Path)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--default-days", type=int, default=3)
    parser.add_argument("--max-message-chars", type=int, default=1500)
    parser.add_argument("--max-total-chars", type=int, default=40000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    audit_dir = contained(root, root / AUDIT_REL)
    if args.since:
        since = parse_moment(args.since)
    else:
        last = last_audit_date(audit_dir) if audit_dir.is_dir() else None
        since = (
            parse_moment(last.isoformat())
            if last
            else dt.datetime.now().astimezone() - dt.timedelta(days=args.default_days)
        )
    until = parse_moment(args.until) if args.until else None
    source = args.transcripts_dir or default_transcripts_dir(root)
    if not source.is_dir():
        print(f"transcripts directory not found: {source}", file=sys.stderr)
        return 2
    cutoff = since.timestamp()
    files = sorted(p for p in source.glob("*.jsonl") if p.stat().st_mtime >= cutoff)
    messages = [
        m for m in read_messages(files) if m.when >= since and (until is None or m.when <= until)
    ]
    text = render(messages, since, args.max_message_chars, args.max_total_chars, len(files))
    out = contained(root, args.out or audit_dir / f"user-words-{dt.date.today().isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{len(messages)} messages from {len(files)} files since {since:%Y-%m-%d %H:%M} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
