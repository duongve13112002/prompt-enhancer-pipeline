"""Write guard (P-03): an `allowed_dirs` entry matches as a whole path prefix, component by component.

Nested entries (`.cache/tmp`) were dead because only the first path component was compared (R3-GUARD-NESTED, user
decision 432(1)). A target is allowed only strictly inside an entry; mkdir of the entry itself is allowed; a parent or
sibling of an entry is not. Commands are only parsed, never run. Verdicts: B block, A allow.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import guard_config as gc
import pytest
import shell_targets as st
import tool_guard as tg
from conftest import GITATTRIBUTES, git, make_link
from hypothesis import given
from hypothesis import strategies as s

NESTED = [".tmp", ".cache/tmp"]
CASE_INSENSITIVE = os.name == "nt"
ZERO_WIDTH = chr(0x200B)  # a Cf character the guard refuses in any target
FULLWIDTH_DOT = chr(0xFF0E)
FULLWIDTH_SLASH = chr(0xFF0F)  # not a path separator: a lookalike stays one component


def config(allowed: list[str]) -> dict[str, Any]:
    cfg = json.loads(json.dumps(gc.load()))
    cfg["write_guard"]["allowed_dirs"] = allowed
    return gc.validate(cfg)


def verdict(repo: Path, command: str, tool: str = "Bash", allowed: list[str] | None = None) -> str:
    args = argparse.Namespace(config=None, git_mode="pm", worktree=None)
    event = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "t-nested",
        "cwd": str(repo),
    }
    blocks, _ = tg.evaluate(tg.Context(event, config(allowed or NESTED), args, repo))
    return "B" if blocks else "A"


@pytest.fixture(scope="module")
def nested_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fixture repository with a nested entry `.cache/tmp`: a tracked file forced into it, a hard link to a tracked
    file, a registered work tree, a tracked file in the parent `.cache`, and junctions out of and into the entry."""
    repo = Path(os.path.realpath(tmp_path_factory.mktemp("nested-repo")))
    outside = Path(os.path.realpath(tmp_path_factory.mktemp("nested-outside")))
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitattributes").write_bytes(GITATTRIBUTES.encode())
    (repo / ".gitignore").write_bytes(b".tmp/\n.cache/\nruns/\n")
    for rel, data in {
        "tasks/STATUS.md": b"# status\n",
        "src/fix.py": b"x = 1\n",
        ".cache/tmp/tracked.txt": b"tracked in the entry\n",
        ".cache/loose.txt": b"tracked in the parent\n",
        ".cache/other/tracked_other.txt": b"tracked in a sibling\n",
    }.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, "add", "-A")
    git(
        repo,
        "add",
        "-f",
        ".cache/tmp/tracked.txt",
        ".cache/loose.txt",
        ".cache/other/tracked_other.txt",
    )
    git(repo, "commit", "-q", "-m", "fixture")
    for rel in (".cache/tmp/sub", ".cache/tmpx", ".tmp/scratch", "runs"):
        (repo / rel).mkdir(parents=True, exist_ok=True)
    for name in ("a.log", "b.log"):
        (repo / ".cache" / "tmp" / name).write_bytes(b"log\n")
    (repo / ".cache" / "tmp" / "holder").mkdir()
    git(
        repo,
        "worktree",
        "add",
        "-q",
        str(repo / ".cache" / "tmp" / "holder" / "wt"),
        "-b",
        "wt-nested",
    )
    with contextlib.suppress(OSError):  # the test then skips on the missing fixture
        os.link(repo / "src" / "fix.py", repo / ".cache" / "tmp" / "hard.txt")
        os.link(repo / "src" / "fix.py", repo / ".cache" / "tmp" / "sub" / "hard_sub.txt")
    make_link(repo / ".cache" / "tmp" / "out_root", repo)
    make_link(repo / ".cache" / "tmp" / "out_src", repo / "src")
    make_link(repo / ".cache" / "tmp" / "out_side", outside)
    make_link(repo / ".tmp" / "into", repo / ".cache" / "tmp")
    make_link(repo / ".tmp" / "into_parent", repo / ".cache")
    return repo


def need(repo: Path, *rels: str) -> None:
    for rel in rels:
        if not (repo / rel).exists():
            pytest.skip(f"the platform refused to create the fixture {rel}")


CASES: list[tuple[str, str, str, str]] = [
    # A nested entry works.
    ("N-01", "Bash", "echo x > .cache/tmp/a.txt", "A"),
    ("N-02", "Bash", "echo x > .cache/tmp/sub/deep/a.txt", "A"),
    ("N-03", "PowerShell", "Set-Content .cache/tmp/a.txt x", "A"),
    ("N-04", "Bash", "rm .cache/tmp/a.log", "A"),
    ("N-05", "Bash", "echo x > .cache//tmp/./a.txt", "A"),
    # The single-segment entry next to it is unchanged.
    ("N-06", "Bash", "echo x > .tmp/scratch/a.txt", "A"),
    ("N-07", "Bash", "echo x > .tmpx/a.txt", "B"),
    ("N-08", "Bash", "echo x > runs/a.txt", "B"),
    # Prefix confusion: components, never string prefixes.
    ("N-10", "Bash", "echo x > .cache/tmpx/f", "B"),
    ("N-11", "Bash", "echo x > .cache/tmp2/f", "B"),
    ("N-12", "Bash", "echo x > .cache/tm/f", "B"),
    ("N-13", "Bash", "echo x > .cache/tmp.bak", "B"),
    ("N-14", "Bash", "echo x > .cache/other/f", "B"),
    # The parent of an entry is never allowed.
    ("N-20", "Bash", "echo x > .cache/f", "B"),
    ("N-21", "Bash", "rm -rf .cache", "B"),
    ("N-22", "Bash", "mkdir .cache", "B"),
    ("N-23", "Bash", "echo x > .cache/loose.txt", "B"),
    ("N-24", "PowerShell", "Remove-Item -Recurse .cache", "B"),
    ("N-25", "Bash", "mkdir -p .cache", "B"),
    # The entry itself: no write, but mkdir (a missing empty parent may be created by -p).
    ("N-30", "Bash", "rm -rf .cache/tmp", "B"),
    ("N-31", "Bash", "echo x > .cache/tmp", "B"),
    ("N-32", "Bash", "mkdir .cache/tmp", "A"),
    ("N-33", "Bash", "mkdir -p .cache/tmp", "A"),
    ("N-34", "Bash", "mkdir .cache/tmp/new", "A"),
    ("N-35", "PowerShell", "mkdir .cache/tmp", "A"),
    ("N-36", "PowerShell", "Remove-Item -Recurse .cache/tmp", "B"),
    ("N-37", "Bash", "mkdir .cache/tmpx", "B"),
    # `..` is resolved before the comparison.
    ("N-40", "Bash", "echo x > .cache/tmp/../f", "B"),
    ("N-41", "Bash", "echo x > .cache/tmp/../../src/fix.py", "B"),
    ("N-42", "Bash", "echo x > .cache/other/../tmp/f", "A"),
    ("N-43", "Bash", "echo x > .cache/tmp/sub/../../tmpx/f", "B"),
    ("N-44", "Bash", "echo x > .cache/tmp/*/../f", "B"),
    # Wildcards: directly in the entry (every match checked), in the parent, or spanning the entry name.
    ("N-50", "Bash", "rm .cache/tmp/*.log", "A"),
    ("N-51", "Bash", "rm .cache/tmp/tracked*", "B"),
    ("N-52", "Bash", "rm .cache/tmp/*", "B"),
    ("N-53", "Bash", "rm .cache/*", "B"),
    ("N-54", "Bash", "rm .cache/t*/x", "B"),
    ("N-55", "Bash", "echo x > .cache/tm?/f", "B"),
    ("N-56", "PowerShell", "Remove-Item .cache/tmp/*.log", "A"),
    ("N-57", "Bash", "rm .cache/tmp/sub/*.log", "A"),
    # Tracked file, hard link, work tree, links inside the entry.
    ("N-60", "Bash", "echo x >> .cache/tmp/tracked.txt", "B"),
    ("N-61", "Bash", "rm .cache/tmp/hard.txt", "B"),
    ("N-62", "Bash", "rm -rf .cache/tmp/holder", "B"),
    ("N-69", "Bash", "echo x > .cache/tmp/holder/wt/f", "B"),
    ("N-63", "Bash", "echo x > .cache/tmp/out_root/tasks/STATUS.md", "B"),
    ("N-64", "Bash", "echo x > .cache/tmp/out_src/fix.py", "B"),
    ("N-65", "Bash", "echo x > .cache/tmp/out_side/f", "B"),
    ("N-66", "Bash", "echo x > .tmp/into/a.txt", "A"),
    ("N-67", "Bash", "echo x > .tmp/into_parent/f", "B"),
    ("N-68", "Bash", "echo x > .tmp/into/tracked.txt", "B"),
    # Outside the root and run-time targets stay refused.
    ("N-70", "Bash", "echo x > ../outside.txt", "B"),
    ("N-71", "Bash", "echo x > $HOME/.cache/tmp/f", "B"),
    ("N-72", "Bash", "echo x > .cache/tmp/$(echo f)", "B"),
    ("N-73", "Bash", f"echo x > .cache/tmp{ZERO_WIDTH}/f", "B"),
    ("N-74", "Bash", f"echo x > {FULLWIDTH_DOT}cache{FULLWIDTH_SLASH}tmp/f", "B"),
]


@pytest.mark.parametrize(("case", "tool", "command", "expect"), CASES, ids=[c[0] for c in CASES])
def test_nested_entry(nested_repo: Path, case: str, tool: str, command: str, expect: str) -> None:
    if case in {"N-61"}:
        need(nested_repo, ".cache/tmp/hard.txt")
    if case in {"N-63", "N-64", "N-65"}:
        need(nested_repo, ".cache/tmp/out_root", ".cache/tmp/out_src", ".cache/tmp/out_side")
    if case in {"N-66", "N-67", "N-68"}:
        need(nested_repo, ".tmp/into", ".tmp/into_parent")
    assert verdict(nested_repo, command, tool) == expect, case


CASE_CASES: list[tuple[str, str, str]] = [
    ("N-80", "PowerShell", "Set-Content .CACHE\\tmp\\f x"),
    ("N-81", "PowerShell", "Set-Content .cache\\tmp/f x"),
    ("N-82", "Bash", "echo x > .Cache/TMP/f"),
    ("N-83", "PowerShell", "Set-Content .cache\\TMP\\sub\\f x"),
]


@pytest.mark.parametrize(("case", "tool", "command"), CASE_CASES, ids=[c[0] for c in CASE_CASES])
def test_case_and_separator_mixes(nested_repo: Path, case: str, tool: str, command: str) -> None:
    """The comparison follows the filesystem: case-insensitive on Windows, exact on POSIX (fail-closed)."""
    expect = "A" if CASE_INSENSITIVE else "B"
    assert verdict(nested_repo, command, tool) == expect, case


def test_mixed_separators_exact_case(nested_repo: Path) -> None:
    assert verdict(nested_repo, "Set-Content .cache\\tmp/sub\\f x", "PowerShell") == "A"
    assert verdict(nested_repo, "Set-Content .cache\\tmpx\\f x", "PowerShell") == "B"
    assert verdict(nested_repo, "Set-Content .cache\\f x", "PowerShell") == "B"


def test_nested_entry_alone_replaces_the_single_entries(nested_repo: Path) -> None:
    only = [".cache/tmp"]
    assert verdict(nested_repo, "echo x > .tmp/scratch/a.txt", allowed=only) == "B"
    assert verdict(nested_repo, "echo x > .cache/tmp/a.txt", allowed=only) == "A"
    assert verdict(nested_repo, "echo x > .cache/tmp/a.txt", allowed=["runs"]) == "B"


def test_overlapping_nested_entries_form_a_union(nested_repo: Path) -> None:
    """Both entries are nested, so the single-component comparison of the old code refused every row but `B`."""
    both = [".cache/tmp", ".cache/tmp/sub"]
    assert verdict(nested_repo, "echo x > .cache/tmp/a.txt", allowed=both) == "A"
    assert verdict(nested_repo, "echo x > .cache/tmp/sub/deep.txt", allowed=both) == "A"
    assert verdict(nested_repo, "echo x > .cache/tmp/sub/new/deep.txt", allowed=both) == "A"
    assert verdict(nested_repo, "mkdir .cache/tmp", allowed=both) == "A"
    assert verdict(nested_repo, "mkdir .cache/tmp/sub", allowed=both) == "A"
    assert verdict(nested_repo, "mkdir .cache/tmp/sub/new", allowed=both) == "A"
    assert verdict(nested_repo, "echo x > .cache/tmp", allowed=both) == "B"
    assert verdict(nested_repo, "rm -rf .cache/tmp", allowed=both) == "B"
    assert verdict(nested_repo, "echo x > .cache/tmpx/f", allowed=both) == "B"
    assert verdict(nested_repo, "rm -rf .cache", allowed=both) == "B"
    assert verdict(nested_repo, "echo x > .cache/loose.txt", allowed=both) == "B"
    assert verdict(nested_repo, "rm .cache/tmp/*.log", allowed=both) == "A"


def test_wildcard_in_a_subdirectory_takes_the_literal_directory_check(nested_repo: Path) -> None:
    """Pins today's verdict (unchanged for single entries, M7 killer): `.cache/tmp/sub` is no entry, so
    `.cache/tmp/sub/*` is judged by the literal directory `sub` and not per match, and a hard-linked file below it
    does not block. Once `sub` is itself an entry, the per-match check runs and refuses the hard link."""
    need(nested_repo, ".cache/tmp/sub/hard_sub.txt")
    assert verdict(nested_repo, "rm .cache/tmp/sub/*", allowed=[".cache/tmp"]) == "A"
    assert (
        verdict(nested_repo, "rm .cache/tmp/sub/*", allowed=[".cache/tmp", ".cache/tmp/sub"]) == "B"
    )
    assert verdict(nested_repo, "rm .cache/tmp/sub/hard_sub.txt", allowed=[".cache/tmp"]) == "B"


def test_mkdir_of_an_entry_that_a_broader_entry_covers(nested_repo: Path) -> None:
    """The old code treated `mkdir .cache/tmp` as an ordinary write below `.cache` and refused it on the tracked file
    inside the entry; mkdir of any entry is allowed whatever overlaps it."""
    assert verdict(nested_repo, "mkdir .cache/tmp", allowed=[".cache", ".cache/tmp"]) == "A"


def test_regression_overlap_with_a_single_component_entry(nested_repo: Path) -> None:
    """Regression guard, not proof of the nested logic: passes on the old code too, because `.cache` alone covers
    every row there (the old code compared the first component only)."""
    both = [".cache", ".cache/tmp"]
    assert verdict(nested_repo, "echo x > .cache/other/f", allowed=both) == "A"
    assert verdict(nested_repo, "echo x > .cache/tmp/f", allowed=both) == "A"
    assert verdict(nested_repo, "echo x > .cache/loose.txt", allowed=both) == "B"  # tracked
    assert verdict(nested_repo, "rm -rf .cache", allowed=both) == "B"


def test_regression_entry_with_wildcard_stays_literal(nested_repo: Path) -> None:
    """Regression guard: passes on the old code too (`.cache` was never an entry there)."""
    star = [".cache/t*"]
    assert verdict(nested_repo, "echo x > .cache/tmp/f", allowed=star) == "B"
    assert verdict(nested_repo, "echo x > .cache/tmpx/f", allowed=star) == "B"


def test_regression_message_lists_the_full_entry(nested_repo: Path) -> None:
    """Regression guard: passes on the old code too (the advice text already printed every entry in full)."""
    args = argparse.Namespace(config=None, git_mode="pm", worktree=None)
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x > .cache/f"},
        "session_id": "t-nested",
        "cwd": str(nested_repo),
    }
    blocks, _ = tg.evaluate(tg.Context(event, config(NESTED), args, nested_repo))
    assert any("`.cache/tmp/`" in b for b in blocks)


# ---------------------------------------------------------------------------------------------------------------
# Invariant (decision 130): a path is `inside` iff some entry is a proper component prefix of it, and an `entry` iff
# it equals one. The oracle uses list slicing on lowercased components, never string prefixes.

SEGMENTS = ["a", "ab", "b", "A", "Ab"]
ENTRIES = s.lists(
    s.lists(s.sampled_from(SEGMENTS), min_size=1, max_size=3).map("/".join),
    min_size=1,
    max_size=3,
    unique=True,
)
PARTS = s.lists(s.sampled_from(SEGMENTS), min_size=0, max_size=5)


def fold(parts: list[str] | tuple[str, ...]) -> list[str]:
    return [p.lower() for p in parts] if CASE_INSENSITIVE else list(parts)


def is_inside(entries: list[str], parts: list[str]) -> bool:
    target = fold(parts)
    return any(
        len(e) < len(target) and target[: len(e)] == e
        for e in (fold(x.split("/")) for x in entries)
    )


def is_entry(entries: list[str], parts: list[str]) -> bool:
    return any(fold(parts) == fold(x.split("/")) for x in entries)


@given(ENTRIES, PARTS)
def test_placement_property(entries: list[str], parts: list[str]) -> None:
    policy = st.PathPolicy(Path(os.getcwd()), entries, 5)
    assert policy._is_inside(tuple(parts)) == is_inside(entries, parts)
    assert policy._is_entry(tuple(parts)) == is_entry(entries, parts)


SEGMENTS_E2E = [
    ".cache",
    ".CACHE",
    ".tmp",
    "runs",
    "tmp",
    "TMP",
    "tmpx",
    "other",
    "sub",
    "Sub",
    "zz",
]
E2E_ENTRIES = s.lists(
    s.lists(s.sampled_from(SEGMENTS_E2E), min_size=1, max_size=3).map("/".join),
    min_size=1,
    max_size=3,
    unique=True,
)
E2E_DIRS = s.lists(s.sampled_from(SEGMENTS_E2E), min_size=1, max_size=4)
E2E_LEAF = s.sampled_from(
    [None, "new_leaf.txt"]
)  # None: the target is a directory path, possibly an entry itself
E2E_VERBS = s.sampled_from(["write", "rm", "mkdir", "md"])


class NoGitPolicy(st.PathPolicy):
    """Labelled test double: no `git` subprocess per Hypothesis example. Both git queries answer "nothing", so the
    property covers the placement decision only; the tracked-file and work-tree refusals have their own cases above."""

    def _tracked(self, tree: Path, path: Path) -> str | None:
        return None

    def _worktree_paths(self, tree: Path) -> list[Path] | None:
        return []


@given(E2E_ENTRIES, E2E_DIRS, E2E_LEAF, E2E_VERBS)
def test_check_allows_iff_strictly_inside_an_entry(
    nested_repo: Path, entries: list[str], dirs: list[str], leaf: str | None, verb: str
) -> None:
    """End to end through `check()`: allowed iff strictly inside an entry, or an entry itself under a mkdir verb."""
    parts = [*dirs, leaf] if leaf else dirs
    rel = "/".join(parts)
    reason = NoGitPolicy(nested_repo, entries, 30).check(
        st.Target(rel, verb, "bash"), [nested_repo]
    )
    expect = is_inside(entries, parts) or (verb in {"mkdir", "md"} and is_entry(entries, parts))
    assert (reason is None) == expect, (entries, verb, rel, reason)
