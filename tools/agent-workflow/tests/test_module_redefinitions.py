"""No module binds a top-level name twice (S42-ci3 fix round 2, P0-1).

`storage/_anchored.py` defined a POSIX `_open_root(root, flags)` and, further down, a Windows `_open_root(api, root)`.
The later definition won, so every POSIX store write raised `AttributeError`, and the Windows suite never noticed,
because the POSIX arms are skipped there. This scan fails CI on the next such redefinition, on every platform.

Rule: a name bound at module level by `def`, `class`, `import` or assignment to a plain name, more than once.
Bindings in exclusive branches (`if`/`else`, `try`/`except`) are alternatives, not redefinitions: that is the
platform switch and the optional-import fallback. Assignments to attributes or subscripts mutate, they do not
bind. Scope: this tooling tree and the host project's `python.src_dir` (`.agents/project.json`), so a copy of
the tooling checks its own project.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import project_config  # noqa: E402

SKIPPED_DIRS = {"__pycache__", ".venv", "node_modules"}


def _targets(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _targets(item)]
    if isinstance(node, ast.Starred):
        return _targets(node.value)
    return []  # attribute or subscript: a mutation


def _bound(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in _targets(target)]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return _targets(node.target)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in node.names if alias.name != "*"]
    return []


def _branches(node: ast.stmt) -> list[list[ast.stmt]] | None:
    """The mutually exclusive bodies of a compound statement, or None when it binds in sequence."""
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.Try):
        return [node.body + node.orelse, *(handler.body for handler in node.handlers)]
    return None


def bindings(body: list[ast.stmt]) -> dict[str, list[int]]:
    """Line numbers of the bindings of each name in `body` that a single run can execute one after another."""
    found: dict[str, list[int]] = {}
    for node in body:
        exclusive = _branches(node)
        if exclusive is not None:
            merged: dict[str, list[int]] = {}
            for branch in exclusive:  # one branch runs: keep the longest chain each name can see
                for name, lines in bindings(branch).items():
                    if len(lines) > len(merged.get(name, [])):
                        merged[name] = lines
            if isinstance(node, ast.Try):
                for name, lines in bindings(node.finalbody).items():
                    merged.setdefault(name, []).extend(lines)
            groups = merged
        elif isinstance(node, ast.With):
            groups = bindings(node.body)
        else:
            groups = {name: [node.lineno] for name in _bound(node)}
        for name, lines in groups.items():
            found.setdefault(name, []).extend(lines)
    return found


def redefinitions(source: str) -> dict[str, list[int]]:
    return {name: lines for name, lines in bindings(ast.parse(source).body).items() if len(lines) > 1}


def _modules() -> list[Path]:
    config = project_config.load()
    roots = [TOOLS, config.root / config.src_dir]
    found = set()
    for root in roots:
        for path in root.rglob("*.py"):
            if not SKIPPED_DIRS.intersection(path.relative_to(root).parts):
                found.add(path.resolve())
    return sorted(found)


def test_the_scan_covers_the_tooling_and_the_project_sources() -> None:
    modules = _modules()
    assert Path(__file__).resolve() in modules
    src = project_config.load()
    assert any(src.root / src.src_dir in path.parents for path in modules)


def test_no_module_binds_a_top_level_name_twice() -> None:
    found = {}
    for path in _modules():
        clashes = redefinitions(path.read_text(encoding="utf-8"))
        if clashes:
            found[path.as_posix()] = clashes
    assert found == {}


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def f(a, b): pass\ndef f(c): pass\n", {"f": [1, 2]}),  # the P0-1 shape
        ("class C: pass\nC = 1\n", {"C": [1, 2]}),
        ("X = 1\nX = 2\n", {"X": [1, 2]}),
        ("a, (b, *c) = 1, (2, 3)\nc = 4\n", {"c": [1, 2]}),
        ("import os\nos = None\n", {"os": [1, 2]}),
        ("if 1:\n    def f(): pass\ndef f(): pass\n", {"f": [2, 3]}),
        ("try:\n    X = 1\nfinally:\n    X = 2\n", {"X": [2, 4]}),
        ("with open('x'):\n    X = 1\nX = 2\n", {"X": [2, 3]}),
    ],
)
def test_the_scan_sees_each_redefinition_form(source: str, expected: dict[str, list[int]]) -> None:
    assert redefinitions(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "if a:\n    def f(): pass\nelse:\n    def f(): pass\n",  # platform switch
        "try:\n    import st\nexcept ImportError:\n    st = None\n",  # optional import
        "X = {}\nX['k'] = 1\nX.attr = 2\n",  # mutations
        "x: int\nx = 1\n",  # an annotation alone binds nothing
        "def f():\n    g = 1\n    g = 2\n",  # function scope is not module scope
    ],
)
def test_the_scan_passes_alternatives_and_mutations(source: str) -> None:
    assert redefinitions(source) == {}
