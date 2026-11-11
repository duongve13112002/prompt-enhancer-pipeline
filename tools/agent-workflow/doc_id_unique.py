"""Report duplicate note numbers and duplicate decision numbers (R3 audit F-11, P-11).

Usage: python -B -I doc_id_unique.py [--root <project root>]
Exit 0: all ids unique. Exit 1: duplicates found, each group listing its files or rows, tracked and
untracked separately. Nothing is renamed or written; the PM decides how to resolve a collision.
Identity is the id only, so a collision still needs a content check (title, hash) before concluding
which note is meant.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

NOTE_NAME = re.compile(r"^N(\d+)([a-z]?)-.*\.md$")
DECISION_ROW = re.compile(r"^\|\s*(\d+[a-z]?)\s*\|")


def duplicate_notes(names: dict[str, str]) -> dict[str, list[str]]:
    """names: file name -> 'tracked' | 'untracked'. Returns id (digits plus optional letter, as in N078a) -> sorted '<name> (<state>)' when shared."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name, state in names.items():
        match = NOTE_NAME.match(name)
        if match:
            groups[f"{int(match.group(1)):03d}{match.group(2)}"].append(f"{name} ({state})")
    return {number: sorted(files) for number, files in groups.items() if len(files) > 1}


def duplicate_decisions(index_text: str) -> dict[str, list[int]]:
    """Decision id -> 1-based line numbers of every table row that opens with it, when repeated."""
    rows: dict[str, list[int]] = defaultdict(list)
    for number, line in enumerate(index_text.splitlines(), start=1):
        match = DECISION_ROW.match(line)
        if match:
            rows[match.group(1)].append(number)
    return {key: lines for key, lines in rows.items() if len(lines) > 1}


def _git_lines(root: Path, *args: str) -> set[str] | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {line for line in done.stdout.splitlines() if line}


def note_states(root: Path) -> dict[str, str]:
    """File name in docs/note -> tracked/untracked (everything counts as tracked when Git is unavailable)."""
    note_dir = root / "docs" / "note"
    present = {p.name for p in note_dir.glob("*.md")}
    tracked = _git_lines(root, "ls-files", "--", "docs/note")
    if tracked is None:
        return dict.fromkeys(present, "tracked")
    tracked_names = {Path(t).name for t in tracked}
    return {name: ("tracked" if name in tracked_names else "untracked") for name in present}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    found = 0
    for number, files in sorted(duplicate_notes(note_states(args.root)).items()):
        print(f"duplicate note N{number}: {', '.join(files)}")
        found += 1
    index = args.root / "tasks" / "DECISIONS_INDEX.md"
    for key, lines in sorted(duplicate_decisions(index.read_text(encoding="utf-8")).items()):
        print(f"duplicate decision {key}: lines {', '.join(map(str, lines))} of {index.name}")
        found += 1
    print(f"{found} duplicate id(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
