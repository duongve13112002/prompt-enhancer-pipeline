"""Lint for the `applied:` marker of `tasks/DECISIONS_INDEX.md` rows (decision 353, WP-15).

A decision row from `applied_from_decision` on (`memory_hooks.json`) carries exactly one marker, at the end of a cell:
`applied: <path>[, <path>...]` (relative repository files that implement it and exist) or `applied: pending` (decided,
not yet applied). A second marker, a placeholder (`none`, `todo`, `n/a`), an absolute or `..` path and, with a root, a
missing file are errors. `check` prints rows without a valid marker and the `pending` ids, and exits 1 when a marker
is missing, malformed or unresolved (`pending` is valid). The session-start hook imports `scan` to list pending rows. The index file is PM-owned: this tool
only reads it. Standard library only (runs with `python -B -I`).

Usage: decisions_applied.py check [--index PATH] [--config PATH] [--root PATH]
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "tasks" / "DECISIONS_INDEX.md"
CONFIG_MODULE = Path(__file__).resolve().parent / "memory_hooks_config.py"
ROW = re.compile(r"^\|\s*(\d+)([a-z]?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.*)$")
# The marker value runs to the end of its cell (the next `|`) or of the line; `**applied:**` is accepted.
MARKER = re.compile(r"(?:^|[\s(*_])applied:\**[ \t]*([^|\n]*?)[ \t]*(?:\||$)", re.IGNORECASE)
PATH_ITEM = re.compile(
    r"[^\s,;|()\\]+"
)  # no parentheses or backslashes: "(applied: x.py)" is malformed
BAD_STATES = ("missing", "malformed", "unresolved")


class Row(NamedTuple):
    """NamedTuple, not a dataclass: hooks load this file by path, where dataclasses cannot find the module."""

    ident: str
    state: str  # applied | pending | missing | malformed | unresolved
    files: tuple[str, ...]
    line: int


def path_shaped(item: str) -> bool:
    """A relative in-repository path: not a placeholder word, and no drive, absolute or `..` part."""
    return bool(
        PATH_ITEM.fullmatch(item)
        and ("/" in item or "." in item)
        and not item.startswith(("/", "~"))
        and ":" not in item
        and ".." not in item.split("/")
        and item.rstrip("/") not in ("", ".")
        and item.lower() != "n/a"
    )


def parse_marker(cell_text: str) -> tuple[str, tuple[str, ...]]:
    """(state, files) of the `applied:` marker in the text of a row; more than one marker is malformed."""
    matches = list(MARKER.finditer(cell_text))
    if not matches:
        return "missing", ()
    if len(matches) > 1:
        return "malformed", ()
    value = matches[0].group(1).strip().strip("*").strip()
    items = [part.strip().strip("`").strip() for part in re.split(r"[,;]", value)]
    if not value or not all(items):
        return "malformed", ()
    if len(items) == 1 and items[0].lower() == "pending":
        return "pending", ()
    if not all(path_shaped(item) for item in items):
        return "malformed", ()
    return "applied", tuple(items)


def exists_inside(root: Path, item: str) -> bool:
    try:
        target = (root / item).resolve()
        return target.exists() and target.is_relative_to(root.resolve())
    except (OSError, ValueError):  # ValueError: an embedded NUL in the path
        return False


def scan(text: str, from_decision: int, root: Path | None = None) -> list[Row]:
    """One `Row` per decision row whose number is at least `from_decision`, in file order.

    With a `root`, a row whose files do not all exist inside it is `unresolved`."""
    rows: list[Row] = []
    for number, line in enumerate(text.split("\n"), start=1):
        match = ROW.match(line.strip())
        if not match or int(match[1]) < from_decision:
            continue
        state, files = parse_marker(match[4])
        if (
            state == "applied"
            and root is not None
            and not all(exists_inside(root, i) for i in files)
        ):
            state = "unresolved"
        rows.append(Row(f"{match[1]}{match[2]}", state, files, number))
    return rows


def load_settings(config: Path | None = None) -> Any:
    spec = importlib.util.spec_from_file_location("memory_hooks_config", CONFIG_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load(config) if config else module.load()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="report rows without a valid applied marker")
    check.add_argument("--index", type=Path, default=INDEX)
    check.add_argument("--config", type=Path, default=None)
    check.add_argument(
        "--root", type=Path, default=ROOT, help="repository root the files must exist in"
    )
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    try:
        from_decision = settings.int_("applied_from_decision")
    except Exception as exc:  # NotConfigured, from a module loaded by path (class identity differs)
        print(f"decisions_applied: {exc}", file=sys.stderr)
        return 2
    try:
        text = args.index.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        print(f"decisions_applied: cannot read {args.index}: {type(exc).__name__}", file=sys.stderr)
        return 2
    rows = scan(text, from_decision, args.root)
    bad = [row for row in rows if row.state in BAD_STATES]
    pending = [row.ident for row in rows if row.state == "pending"]
    for row in bad:
        print(f"{args.index.name}:{row.line} decision {row.ident}: {row.state} `applied:` marker")
    print(
        f"rows from {from_decision}: {len(rows)}; applied {sum(r.state == 'applied' for r in rows)}; "
        f"pending {len(pending)}"
        + (f" ({', '.join(pending)})" if pending else "")
        + f"; bad {len(bad)}"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
