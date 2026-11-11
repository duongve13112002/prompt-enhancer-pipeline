"""Line-ending lint for tracked text files (P-04), using Git's own classification (`git ls-files --eol`).

Reported:
- an index copy with CRLF or mixed endings in a text file (`i/crlf`, `i/mixed`);
- a working copy with CRLF or mixed endings in an `eol=lf` file (`w/crlf`, `w/mixed`);
- a lone CR in an `eol=lf` file, which makes Git treat it as binary (`-text` without NUL bytes).
`text=auto` files checked out as CRLF by `core.autocrlf=true` are expected and not reported.

`--fix` rewrites the working copy of `eol=lf` files whose index copy is LF (`i/lf`), CRLF to LF only, and prints each
changed file; other findings need a manual fix. Exit status: 0 clean (or all fixed), 1 findings remain, 2 git failed.
Standard library only (also imported by hooks/tool_monitor.py, which runs with `python -B -I`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
BAD_ENDINGS = ("crlf", "mixed")  # git ls-files --eol states (protocol-bound)


class Entry(NamedTuple):
    path: str  # repository-relative, forward slashes
    staged: str  # `lf`,`crlf`, `mixed`, `none`, `-text` or "" (git ls-files --eol `i/` field)
    work: str  # same for the working copy (`w/`)
    attr: str  # the `attr/` field, e.g. `text eol=lf`, `text=auto`, `-text`


class Finding(NamedTuple):
    path: str
    problem: str
    fixable: bool


def parse_eol(output: bytes) -> list[Entry]:
    """Entries of `git ls-files --eol -z`: `i/<x> w/<y> attr/<z>\\t<path>` separated by NUL."""
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        info, _, path = record.decode("utf-8", "replace").partition("\t")
        fields = info.split(None, 2)
        values = {f.split("/", 1)[0]: f.split("/", 1)[1] if "/" in f else "" for f in fields[:2]}
        attr = info.split("attr/", 1)[1].strip() if "attr/" in info else ""
        entries.append(Entry(path, values.get("i", ""), values.get("w", ""), attr))
    return entries


def ls_eol(
    tree: Path, paths: list[str] | None = None, *, modified: bool = False, timeout: int = 60
) -> list[Entry]:
    """`git ls-files --eol` of the tree (optionally only `paths`, or only modified files). Raises OSError or
    subprocess.SubprocessError when git fails."""
    command = ["git", "-C", str(tree), "ls-files", "--eol", "-z"]
    if modified:
        command.append("-m")
    if paths is not None:
        command += ["--", *(f":(literal){p}" for p in paths)]
    proc = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise subprocess.SubprocessError(
            proc.stderr.decode("utf-8", "replace").strip()[:200] or "git failed"
        )
    return parse_eol(proc.stdout)


def _eol_lf(entry: Entry) -> bool:
    return "eol=lf" in entry.attr.split()


def findings(tree: Path, entries: list[Entry]) -> list[Finding]:
    out = []
    for e in entries:
        binary_attr = any(a in ("-text", "binary") for a in e.attr.split())
        if e.staged in BAD_ENDINGS and not binary_attr:
            out.append(Finding(e.path, f"index copy has {e.staged.upper()} line endings", False))
        if _eol_lf(e) and e.work in BAD_ENDINGS:
            out.append(
                Finding(
                    e.path,
                    f"working copy has {e.work.upper()} line endings (eol=lf)",
                    e.staged == "lf",
                )
            )
        elif _eol_lf(e) and "-text" in (e.staged, e.work):
            try:
                data = (tree / e.path).read_bytes()
            except OSError:
                continue
            if b"\0" not in data and b"\r" in data:
                out.append(Finding(e.path, "lone CR: Git treats this eol=lf file as binary", False))
    return out


def fix(tree: Path, finding: Finding) -> bool:
    """Rewrite one fixable working copy CRLF -> LF (atomic replace, file mode kept); True when the file changed.
    A symbolic link is never followed or replaced (Git stores the link, not the file it names)."""
    path = tree / finding.path
    if path.is_symlink() or not path.is_file():
        return False
    data = path.read_bytes()
    new = data.replace(b"\r\n", b"\n")
    if new == data:
        return False
    tmp = path.with_name(f".{path.name}.eolfix.{os.getpid()}")
    try:
        tmp.write_bytes(new)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", default=str(ROOT), help="Git work tree to check (default: this checkout)"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite CRLF working copies of eol=lf files whose index is LF",
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="git timeout in seconds (default 60)"
    )
    parser.add_argument("paths", nargs="*", help="limit to these repository-relative paths")
    args = parser.parse_args(argv)
    tree = Path(args.root).resolve()
    try:
        found = findings(tree, ls_eol(tree, args.paths or None, timeout=args.timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"eol_check: git ls-files failed in {tree}: {exc}", file=sys.stderr)
        return 2
    remaining = []
    for f in found:
        if args.fix and f.fixable and fix(tree, f):
            print(f"fixed {f.path}: CRLF -> LF")
        else:
            remaining.append(f)
    for f in remaining:
        hint = " (run with --fix)" if f.fixable else ""
        print(f"{f.path}: {f.problem}{hint}")
    if not remaining:
        print(f"eol_check: clean ({tree})")
    return 1 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
