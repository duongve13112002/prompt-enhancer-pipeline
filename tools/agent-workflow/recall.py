"""Local full-text recall over the project's own records (decision 272, idea 2 of the claude-mem evaluation).

`build --settled` also writes `.tmp/pm-state/settled.md`, a bounded keyword -> decision -> one line index of the decision
rows (newest first; a row that an "Amends/Supersedes/Replaces N" row replaces is dropped, a partial "N(1)" amendment is
annotated), which session_context.py injects at session start (decision 353, WP-09); its sizes and stop words are the
`settled_*` fields of `memory_hooks.json`.
`build` (re)creates `.tmp/pm-state/recall.sqlite` from `tasks/DECISIONS_INDEX.md`, `tasks/archive/*.md`,
`docs/note/*.md`, `tasks/STATUS.md`, `tasks/LESSONS_LEARNED.md`, `tasks/REQUIREMENTS.md` and, only when given, the PM
memory directory (`--memory-dir`, never guessed). Chunks are decision rows, requirement rows and markdown sections, each
with source path and line. `search "<query>"` prints ranked hits as `path:line [kind] excerpt`.

Rules (the privacy filter is the point of this tool, so every one of them fails closed):
- every path is resolved and must stay inside the project root; links or junctions leaving it are refused;
- nothing under a `.secrets` directory is read, indexed or used as database location;
- before text is stored, `<private>...</private>` spans, private-key blocks and secret-like tokens are replaced by
  `[REDACTED]` (newlines kept so line numbers stay true); a file or chunk whose redaction raises is skipped.
Incremental by file mtime and size; a changed redactor version or storage mode forces a full rebuild. Uses SQLite FTS5
when the interpreter has it and a plain `LIKE` search otherwise. Standard library only (runs with `python -B -I`).
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR") or ROOT / ".tmp" / "pm-state")
DB_NAME = "recall.sqlite"
SETTLED_NAME = "settled.md"
CONFIG = Path(__file__).resolve().parent / "memory_hooks.json"
# `python -I` keeps the script directory off sys.path: the settings loader is imported by file path.
CONFIG_MODULE = Path(__file__).resolve().parent / "memory_hooks_config.py"
# artifact-bound: where decision rows live (live index first, then the numbered archives, never the `-full` copy).
DECISION_FILES = ("tasks/DECISIONS_INDEX.md",)
DECISION_ARCHIVE_GLOB = "tasks/archive/DECISIONS_INDEX-[0-9]*.md"
# artifact-bound: the indexed project layout (relative to the root); the memory directory is an explicit option.
SOURCE_FILES = (
    "tasks/DECISIONS_INDEX.md",
    "tasks/STATUS.md",
    "tasks/LESSONS_LEARNED.md",
    "tasks/REQUIREMENTS.md",
)
SOURCE_GLOBS = ("tasks/archive/*.md", "docs/note/*.md")
SECRETS_DIR = ".secrets"
SCHEMA = "1"
REDACTOR_VERSION = "2"  # bump when the redaction rules change so stored chunks are rebuilt
DEFAULT_CHUNK_CHARS = 1500
DEFAULT_LIMIT = 10
EXCERPT_CHARS = 160
REDACTED = "[REDACTED]"

# safety-bound: redaction rules. Token shapes are the providers' documented prefixes; the entropy rule is a heuristic
# catch-all (mixed-case alphanumerics with a 20+ character unbroken run, 32+ characters, >= 4.0 bits per character) and leaves lowercase hex digests alone.
PRIVATE_SPAN = re.compile(r"<private>.*?(?:</private>|\Z)", re.IGNORECASE | re.DOTALL)
KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL,
)
TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE),
)
ASSIGNMENT = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*[\"']?)(?=[^\s\"'`]*\d)[^\s\"'`]{12,}"
)
LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
ENTROPY_BITS = 4.0
MIN_ALNUM_RUN = 20  # file names and identifiers are short words joined by `-`, `_`, `/`; secrets have long unbroken runs

DECISION_ROW = re.compile(r"^\|\s*(\d+[a-z]?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.*)$")
REQUIREMENT_ROW = re.compile(r"^\|\s*(R-\d+[A-Za-z]?)\s*\|(.*)$")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")


class RecallError(Exception):
    """A refused path or an unusable database; the CLI prints it and exits non-zero."""


def _blank_lines(match: re.Match[str]) -> str:
    return REDACTED + "\n" * match.group(0).count("\n")


def _entropy(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _redact_long(match: re.Match[str]) -> str:
    token = match.group(0)
    mixed = (
        any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and any(c.isdigit() for c in token)
    )
    longest = max(len(run) for run in re.findall(r"[A-Za-z0-9]+", token))
    return (
        REDACTED
        if mixed and longest >= MIN_ALNUM_RUN and _entropy(token) >= ENTROPY_BITS
        else token
    )


def redact(text: str) -> str:
    """Strip `<private>` spans and key blocks, then secret-like tokens; newlines are preserved. An unterminated
    `<private>` or key block runs to the end of the text (fail closed)."""
    text = PRIVATE_SPAN.sub(_blank_lines, text)
    text = KEY_BLOCK.sub(_blank_lines, text)
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, text)
    return LONG_TOKEN.sub(_redact_long, text)


def has_secrets_part(path: Path) -> bool:
    return any(part.lower() == SECRETS_DIR for part in path.parts)


def inside_root(path: Path, root: Path) -> Path:
    """Resolve `path` (links and junctions included) and require it to be inside `root` and outside `.secrets`."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise RecallError(f"path outside the project root refused: {path}")
    if has_secrets_part(resolved.relative_to(root)) or has_secrets_part(path):
        raise RecallError(f"path under {SECRETS_DIR} refused: {path}")
    return resolved


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True


Chunk = tuple[int, str, str, str]  # line, kind, title, body


def chunk_file(text: str, chunk_chars: int) -> list[Chunk]:
    """Decision rows and requirement rows become one chunk each; everything else is grouped by markdown section and cut
    at line boundaries (a single longer line is cut by characters) into chunks of at most `chunk_chars`."""
    chunks: list[Chunk] = []
    title = ""
    start = 1
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        if any(line.strip() for line in buffer):
            chunks.append((start, "section", title, "\n".join(buffer).strip("\n")))
        buffer, size = [], 0

    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        decision, requirement = DECISION_ROW.match(line), REQUIREMENT_ROW.match(line)
        if decision or requirement:
            if decision:
                label = f"decision {decision[1]} ({decision[2]})"
                body = decision[3]
            else:
                assert requirement is not None
                label, body = requirement[1], requirement[2]
            chunks.append(
                (
                    number,
                    "decision" if decision else "requirement",
                    label,
                    f"{label} {body}".strip(),
                )
            )
            continue
        heading = HEADING.match(line)
        if heading:
            flush()
            title, start = heading[1].strip(), number
            continue
        if not buffer and not line.strip():
            continue  # a chunk starts at its first non-blank line, so reported lines point at text
        if not buffer and not line.strip():
            continue  # a chunk starts at its first non-blank line, so reported lines point at text
        for piece in [line[i : i + chunk_chars] for i in range(0, len(line), chunk_chars)] or [""]:
            if buffer and size + len(piece) + 1 > chunk_chars:
                flush()
                start = number
            if not buffer:
                start = number
            buffer.append(piece)
            size += len(piece) + 1
    flush()
    return chunks


def collect_sources(root: Path, memory_dir: Path | None) -> tuple[list[Path], list[str]]:
    """Existing source files (resolved, inside the root) and a list of skipped-path notes. An explicit memory directory
    that is outside the root or under `.secrets` raises; a link inside a glob that leaves the root is skipped."""
    found: list[Path] = []
    skipped: list[str] = []
    candidates = [root / rel for rel in SOURCE_FILES]
    for pattern in SOURCE_GLOBS:
        candidates += sorted(root.glob(pattern))
    if memory_dir is not None:
        resolved_dir = inside_root(memory_dir, root)
        if not resolved_dir.is_dir():
            raise RecallError(f"memory directory not found: {memory_dir}")
        candidates += sorted(resolved_dir.glob("*.md"))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            found.append(inside_root(candidate, root))
        except RecallError as exc:
            skipped.append(str(exc))
    return list(dict.fromkeys(found)), skipped


def open_db(db_path: Path, use_fts: bool) -> sqlite3.Connection:
    """Open the database, deleting it first when the schema, redactor version or storage mode differs."""
    mode = "fts5" if use_fts else "like"
    expected = {"schema": SCHEMA, "redactor": REDACTOR_VERSION, "mode": mode}
    if db_path.exists():
        try:
            probe = sqlite3.connect(db_path)
            try:
                current = dict(probe.execute("SELECT key, value FROM meta").fetchall())
            finally:
                probe.close()
        except sqlite3.DatabaseError:
            current = {}
        if current != expected:
            db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER)"
    )
    columns = "path UNINDEXED, line UNINDEXED, kind UNINDEXED, title, body"
    if use_fts:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5({columns}, tokenize='unicode61')"
        )
    else:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks(path TEXT, line INTEGER, kind TEXT, title TEXT, body TEXT)"
        )
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", expected.items())
    conn.commit()
    return conn


def build(
    root: Path,
    db_path: Path,
    memory_dir: Path | None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    fts_probe: Callable[[sqlite3.Connection], bool] | None = None,
) -> dict[str, int]:
    root = root.resolve()
    db_path = inside_root(db_path, root)
    sources, skipped = collect_sources(root, memory_dir)
    probe = fts_probe or fts5_available
    scratch = sqlite3.connect(":memory:")
    try:
        use_fts = probe(scratch)
    finally:
        scratch.close()
    stats = {
        "indexed": 0,
        "unchanged": 0,
        "removed": 0,
        "refused": len(skipped),
        "chunks": 0,
        "skipped_chunks": 0,
    }
    conn = open_db(db_path, use_fts)
    try:
        known = {
            path: (m, s) for path, m, s in conn.execute("SELECT path, mtime_ns, size FROM files")
        }
        live = {path.relative_to(root).as_posix(): path for path in sources}
        for gone in sorted(set(known) - set(live)):
            conn.execute("DELETE FROM chunks WHERE path = ?", (gone,))
            conn.execute("DELETE FROM files WHERE path = ?", (gone,))
            stats["removed"] += 1
        for rel, path in live.items():
            info = path.stat()
            if known.get(rel) == (info.st_mtime_ns, info.st_size):
                stats["unchanged"] += 1
                continue
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            try:
                # whole-file pass first so a `<private>` span or key block crossing a chunk border never leaks
                text = redact(path.read_text(encoding="utf-8-sig", errors="replace"))
                chunks = chunk_file(text, chunk_chars)
            except Exception:
                stats["skipped_chunks"] += 1
                conn.execute("DELETE FROM files WHERE path = ?", (rel,))
                continue
            for line, kind, title, body in chunks:
                try:
                    row = (rel, line, kind, redact(title), redact(body))
                except Exception:
                    stats["skipped_chunks"] += 1
                    continue
                conn.execute(
                    "INSERT INTO chunks(path, line, kind, title, body) VALUES (?, ?, ?, ?, ?)", row
                )
                stats["chunks"] += 1
            conn.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?)",
                (rel, info.st_mtime_ns, info.st_size),
            )
            stats["indexed"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats


def _terms(query: str) -> list[str]:
    return re.findall(r"\w+", query.lower())


def _excerpt(title: str, body: str, line: int, terms: list[str]) -> tuple[int, str]:
    """Line and one-line excerpt of the first body line that mentions a query term (else the chunk start)."""
    for offset, text in enumerate(body.split("\n")):
        if any(term in text.lower() for term in terms):
            return line + offset, _clip(text)
    return line, _clip(title or body.split("\n", 1)[0])


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS].rstrip() + "..."


def search(
    db_path: Path, query: str, limit: int, root: Path = ROOT
) -> list[tuple[str, int, str, str]]:
    """Ranked hits as (path, line, kind, excerpt). All query words must occur (AND)."""
    db_path = inside_root(db_path, root.resolve())
    if limit < 1:
        raise RecallError("--limit must be at least 1")
    terms = _terms(query)
    if not terms:
        raise RecallError("the query has no searchable word")
    if not db_path.is_file():
        raise RecallError(f"no database at {db_path}: run `recall.py build` first")
    conn = sqlite3.connect(db_path)
    try:
        mode = dict(conn.execute("SELECT key, value FROM meta").fetchall()).get("mode")
        if mode == "fts5":
            match = " ".join('"' + term + '"' for term in terms)
            rows = conn.execute(
                "SELECT path, line, kind, title, body FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        else:
            clause = " AND ".join("lower(title || ' ' || body) LIKE ? ESCAPE '\\'" for _ in terms)
            args = [
                "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                for t in terms
            ]
            found = conn.execute(
                f"SELECT path, line, kind, title, body FROM chunks WHERE {clause}", args
            ).fetchall()
            found.sort(key=lambda r: -sum((r[3] + " " + r[4]).lower().count(t) for t in terms))
            rows = found[:limit]
    finally:
        conn.close()
    hits = []
    for path, line, kind, title, body in rows:
        hit_line, excerpt = _excerpt(title, body, line, terms)
        hits.append((path, hit_line, kind, excerpt))
    return hits


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SETTLED_ROW = re.compile(r"^\|\s*(\d+)([a-z]?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|([^|]*)")
_REF = r"\d+[a-z]?(?:\(\d+\))?(?![A-Za-z0-9%])"
# A decision id list after the verb must end the clause: any punctuation or the end of the cell follows it, so
# "(supersedes 125)." and "Replaces 5 - user words" match, while "replaces 2 flaky tests" (a word follows) and
# "replaces 1.5 seconds", "replaces 125,000 rows", "replaces 5-minute polling" (punctuation glued to a word) do not.
_CLAUSE_END = r"(?=\s*(?:(?![.,-]\w)[^\w\s]|$))"
SUPERSEDE = re.compile(
    rf"\b(?:amends|supersedes|replaces)\s+(?:decisions?\s+)?({_REF}(?:\s*(?:,|/|&|and)\s*{_REF})*){_CLAUSE_END}",
    re.IGNORECASE,
)
REFERENCE = re.compile(r"(\d+)([a-z]?)(\((\d+)\))?", re.IGNORECASE)
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
TICKED_TERM = re.compile(
    r"[A-Za-z_][\w./-]*\w"
)  # a name, command or path; not a hash, a fragment or prose
QUOTED = re.compile(r"\"[^\"]{25,}\"")
SETTLED_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
# Layout of settled.md, not a deployment choice: the tail line naming how many rows did not fit.
SETTLED_TAIL = "[{n} older row(s) not listed: size limit]"


def _settings(config: Path) -> Any:
    spec = importlib.util.spec_from_file_location("memory_hooks_config", CONFIG_MODULE)
    if spec is None or spec.loader is None:
        raise RecallError(f"cannot load {CONFIG_MODULE.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load(config)


def _settled_rows(texts: list[str]) -> dict[tuple[int, str], str]:
    """(number, suffix) -> cleaned rule text; the first source wins for a repeated id."""
    rows: dict[tuple[int, str], str] = {}
    for text in texts:
        for line in text.split("\n"):
            match = SETTLED_ROW.match(line.strip())
            if match:
                rule = MD_LINK.sub(r"\1", match[4].replace("**", ""))
                rows.setdefault((int(match[1]), match[2]), " ".join(rule.split()))
    return rows


def _references(rule: str) -> list[tuple[int, str, bool]]:
    """(number, suffix, partial) for every id that the rule says it amends, supersedes or replaces."""
    found = []
    for match in SUPERSEDE.finditer(rule):
        for ref in REFERENCE.finditer(match[1]):
            found.append((int(ref[1]), ref[2].lower(), ref[3] is not None))
    return found


def _keywords(rule: str, frequency: Counter[str], count: int, stopwords: set[str]) -> str:
    ticked = [
        term.lower()
        for term in re.findall(r"`([^`]{3,30})`", rule)
        if TICKED_TERM.fullmatch(term) and term.lower() not in stopwords
    ]
    words: list[str] = []
    for word in SETTLED_WORD.findall(rule):
        low = word.lower()
        if low not in stopwords and low not in words:
            words.append(low)
    # Rare words say more than common ones; ties keep the order of appearance.
    ranked = sorted(words, key=lambda w: (frequency[w], words.index(w)))
    chosen: list[str] = []
    for term in [*dict.fromkeys(ticked)][: max(count // 2, 1)] + ranked:
        if term not in chosen:
            chosen.append(term)
        if len(chosen) == count:
            break
    return ", ".join(chosen)


def settled_text(
    texts: list[str], max_chars: int, line_chars: int, keywords: int, stopwords: set[str]
) -> str:
    """Deterministic settled index of at most `max_chars` characters from the decision-index texts."""
    rows = _settled_rows(texts)
    replaced: dict[tuple[int, str], str] = {}  # row -> label of the later row that replaces it
    partial: dict[tuple[int, str], str] = {}
    replaces: dict[tuple[int, str], list[str]] = {}
    for (number, suffix), rule in sorted(rows.items()):
        label = f"{number}{suffix}"
        for ref_number, ref_suffix, is_partial in _references(rule):
            if ref_number > number or (ref_number == number and ref_suffix >= suffix):
                continue  # only a later row replaces an earlier one
            targets = [
                key
                for key in rows
                if key[0] == ref_number and key[1] == ref_suffix  # "188" is not "188a"
            ]
            for key in targets:
                (partial if is_partial else replaced)[key] = label
            if targets and not is_partial:
                replaces.setdefault((number, suffix), []).append(f"{ref_number}{ref_suffix}")
    frequency: Counter[str] = Counter()
    for rule in rows.values():
        frequency.update({w.lower() for w in SETTLED_WORD.findall(rule)})
    header = (
        "# Settled decisions (newest first; replaced rows are dropped)\n"
        "Generated by `recall.py build --settled` from the decision index and its numbered archives. "
        'Columns: keywords | decision | first words of the rule. Details: `recall.py search "<words>"`.\n'
    )
    lines = []
    for key in sorted(rows, reverse=True):
        if key in replaced:
            continue
        rule = rows[key]
        brief = QUOTED.sub(
            "...", rule
        )  # long quoted user words are in the index row, not in this line
        text = brief if len(brief) <= line_chars else brief[:line_chars].rstrip() + "..."
        extra = f" [replaces {', '.join(replaces[key])}]" if key in replaces else ""
        extra += f" [amended in part by {partial[key]}]" if key in partial else ""
        lines.append(
            f"- {_keywords(rule, frequency, keywords, stopwords)} | {key[0]}{key[1]} | {text}{extra}"
        )
    out = header
    shown = 0
    for line in lines:
        tail = SETTLED_TAIL.format(n=len(lines) - shown - 1)
        if len(out) + len(line) + 1 + len(tail) + 1 > max_chars:
            break
        out += "\n" + line
        shown += 1
    if shown < len(lines):
        out += "\n" + SETTLED_TAIL.format(n=len(lines) - shown)
    text = "\n".join(redact(part) for part in out.split("\n"))
    return EMAIL.sub("[email]", text)[: max_chars - 1] + "\n"


def build_settled(root: Path, out: Path, config: Path = CONFIG) -> int:
    """Write the settled index to `out` (inside the root, outside `.secrets`); returns the number of listed rows."""
    root = root.resolve()
    out = inside_root(out, root)
    settings = _settings(config)
    try:
        max_chars = settings.int_("settled_max_chars")
        line_chars = settings.int_("settled_line_chars")
        keywords = settings.int_("settled_keywords_per_row")
        stopwords = {w.lower() for w in settings.tuple_("settled_stopwords")}
    except Exception as exc:  # NotConfigured (a path-loaded class, so no isinstance)
        raise RecallError(str(exc)) from exc
    candidates = [root / rel for rel in DECISION_FILES] + sorted(root.glob(DECISION_ARCHIVE_GLOB))
    texts = []
    for candidate in candidates:
        try:
            texts.append(
                inside_root(candidate, root).read_text(encoding="utf-8-sig", errors="replace")
            )
        except (OSError, RecallError):
            continue
    if not texts:
        raise RecallError("no decision index found for --settled")
    text = settled_text(texts, max_chars, line_chars, keywords, stopwords)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return sum(1 for line in text.split("\n") if line.startswith("- "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="(re)build the recall database")
    build_cmd.add_argument(
        "--memory-dir",
        type=Path,
        help="PM memory directory to index (must be inside the project root)",
    )
    build_cmd.add_argument("--db", type=Path, default=STATE / DB_NAME)
    build_cmd.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    build_cmd.add_argument(
        "--settled",
        action="store_true",
        help=f"also write .tmp/pm-state/{SETTLED_NAME} (keyword -> decision -> one line)",
    )
    search_cmd = sub.add_parser("search", help="print ranked hits for a query")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    search_cmd.add_argument("--db", type=Path, default=STATE / DB_NAME)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            if args.chunk_chars < 200:
                raise RecallError("--chunk-chars must be at least 200")
            stats = build(ROOT, args.db, args.memory_dir, args.chunk_chars)
            print(" ".join(f"{key}={value}" for key, value in stats.items()))
            if args.settled:
                print(f"settled={build_settled(ROOT, STATE / SETTLED_NAME, CONFIG)}")
        else:
            for path, line, kind, excerpt in search(args.db, args.query, args.limit, ROOT):
                print(f"{path}:{line} [{kind}] {excerpt}")
    except RecallError as exc:
        print(f"recall: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
