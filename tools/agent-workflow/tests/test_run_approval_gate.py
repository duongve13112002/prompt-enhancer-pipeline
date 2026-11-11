"""Tests for run_approval_gate.py: flatten/diff/approval logic, the CLI, and one real profile."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_config  # noqa: E402

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parents[1]
PROFILE = "configs/evaluation/r3-gen-8gb.example.yaml"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_approval_gate_t", TOOLS / "run_approval_gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_approval_gate_t"] = module
    spec.loader.exec_module(module)
    return module


gate = load()
BASE: dict[str, Any] = {
    "vlm": {"route": "a", "n": 3, "models": ["x", "y"]},
    "device": "cuda:0",
    "e": {},
}


@pytest.fixture
def area() -> Iterator[Path]:
    """A scratch directory inside the project root (the gate refuses manifests outside it)."""
    parent = REPO / ".tmp" / "approval-gate-tests"
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=parent))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def values(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The 'resolved profile' the CLI sees; tests change it between record and check."""
    current = {"v": gate.flatten(BASE)}
    monkeypatch.setattr(gate, "resolve_values", lambda root, profile: current["v"])
    return current


def cli(area: Path, *args: str) -> int:
    return gate.main(["--project-root", str(REPO), "--approvals-dir", str(area), *args])


def record(area: Path, run: str = "r1") -> int:
    return cli(
        area, "record", "--run", run, "--profile", "p.yaml", "--approved-by", "2026-10-29 ok"
    )


def test_flatten_uses_escaped_json_pointers_and_keeps_lists_whole() -> None:
    flat = gate.flatten({"a/b": {"c~d": 1}, "l": [1, {"x": 2}], "e": {}, "n": None})
    assert flat == {"/a~1b/c~0d": 1, "/l": [1, {"x": 2}], "/e": {}, "/n": None}
    assert gate.flatten(5) == {"/": 5}


def test_unchanged_profile_passes(area: Path, values, capsys) -> None:
    assert record(area) == 0
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 0
    assert "0 difference(s)" in capsys.readouterr().out


def test_an_unlisted_change_is_refused_and_named(area: Path, values, capsys) -> None:
    record(area)
    values["v"] = {**values["v"], "/vlm/n": 4}
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1
    out = capsys.readouterr().out
    assert "REFUSED changed /vlm/n: approved 3, now 4" in out


def test_added_and_removed_keys_are_refused(area: Path, values, capsys) -> None:
    record(area)
    changed = dict(values["v"])
    del changed["/device"]
    changed["/new"] = True
    values["v"] = changed
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1
    out = capsys.readouterr().out
    assert "REFUSED removed /device" in out and "REFUSED added /new" in out


def test_listing_the_exact_change_passes_and_a_different_value_does_not(area: Path, values) -> None:
    record(area)
    values["v"] = {**values["v"], "/vlm/n": 4}
    ok = ("approve", "--run", "r1", "--key", "/vlm/n", "--source", "2026-10-29 user: n=4")
    assert cli(area, *ok, "--to", "4") == 0
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 0
    assert cli(area, *ok, "--to", "5") == 0  # replaces the entry: approved 5, profile says 4
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1


def test_removal_needs_a_removal_approval(area: Path, values) -> None:
    record(area)
    values["v"] = {k: v for k, v in values["v"].items() if k != "/device"}
    assert (
        cli(area, "approve", "--run", "r1", "--key", "/device", "--to", "null", "--source", "s")
        == 0
    )
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1
    assert (
        cli(area, "approve", "--run", "r1", "--key", "/device", "--removed", "--source", "s") == 0
    )
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 0


def test_an_approval_without_a_source_is_not_an_approval(area: Path, values) -> None:
    record(area)
    values["v"] = {**values["v"], "/vlm/n": 4}
    path = area / "r1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["approved_changes"] = [{"key": "/vlm/n", "to": 4, "source": "  "}]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1
    assert cli(area, "approve", "--run", "r1", "--key", "/vlm/n", "--to", "4", "--source", " ") == 2


def test_missing_wrong_or_broken_manifests_refuse(area: Path, values, capsys) -> None:
    assert cli(area, "check", "--run", "none", "--profile", "p.yaml") == 1
    assert "no approval manifest" in capsys.readouterr().err
    record(area)
    assert cli(area, "check", "--run", "r1", "--profile", "other.yaml") == 1
    (area / "r1.json").write_text("{not json", encoding="utf-8")
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1
    (area / "r1.json").write_text(json.dumps({"schema": "x"}), encoding="utf-8")
    assert cli(area, "check", "--run", "r1", "--profile", "p.yaml") == 1


SPELLED = "configs/evaluation/x.yaml"


def test_every_spelling_of_the_profile_path_is_one_profile(area: Path, values) -> None:
    """A2 (review rev-mem-1): `/` versus `\\` (PowerShell tab completion) must not refuse a recorded profile."""
    assert cli(area, "record", "--run", "r1", "--profile", SPELLED, "--approved-by", "ok") == 0
    assert json.loads((area / "r1.json").read_text(encoding="utf-8"))["profile"] == SPELLED
    absolute = REPO / "configs" / "evaluation" / "x.yaml"
    for spelling in (
        "configs\\evaluation\\x.yaml",
        "./configs/evaluation/x.yaml",
        "configs//evaluation/x.yaml",
        "configs/evaluation/../evaluation/x.yaml",
        " configs/evaluation/x.yaml ",
        str(absolute),
        absolute.as_posix(),
    ):
        assert cli(area, "check", "--run", "r1", "--profile", spelling) == 0, spelling
    assert cli(area, "check", "--run", "r1", "--profile", "configs/evaluation/y.yaml") == 1


def test_a_backslash_spelling_recorded_by_the_old_gate_still_matches() -> None:
    manifest = {"profile": "configs\\evaluation\\x.yaml", "values": {}, "approved_changes": []}
    assert gate.check(manifest, SPELLED, {})[0] == 0
    assert gate.check({**manifest, "profile": SPELLED}, "configs\\evaluation\\x.yaml", {})[0] == 0
    assert gate.check({**manifest, "profile": 5}, SPELLED, {})[0] == 1
    assert gate.check({**manifest, "profile": None}, SPELLED, {})[0] == 1


def test_a_profile_outside_the_project_root_is_a_gate_error(area: Path, values) -> None:
    elsewhere = Path(REPO.anchor) / "elsewhere" / "x.yaml"
    for profile in ("../x.yaml", "configs/../../x.yaml", str(elsewhere), elsewhere.as_posix(), " "):
        code = cli(area, "record", "--run", "o", "--profile", profile, "--approved-by", "ok")
        assert code == 2, profile


def test_record_does_not_overwrite_without_replace(area: Path, values) -> None:
    assert record(area) == 0
    assert record(area) == 2
    assert (
        cli(area, "record", "--run", "r1", "--profile", "p.yaml", "--approved-by", "x", "--replace")
        == 0
    )


def test_bad_arguments_and_paths_are_gate_errors_not_tracebacks(
    area: Path, values, tmp_path: Path
) -> None:
    assert record(area, run="../x") == 2
    assert cli(area, "record", "--run", "r", "--profile", "p", "--approved-by", " ") == 2
    outside = ["--project-root", str(REPO), "--approvals-dir", str(tmp_path / "out")]
    if not tmp_path.resolve().is_relative_to(REPO.resolve()):
        assert gate.main([*outside, "check", "--run", "r", "--profile", "p"]) == 2
    record(area)
    assert cli(area, "approve", "--run", "r1", "--key", "/a", "--to", "{bad", "--source", "s") == 2


def test_an_unloadable_profile_is_exit_2(area: Path) -> None:
    code = cli(area, "record", "--run", "r", "--profile", "configs/none.yaml", "--approved-by", "x")
    assert code == 2


_BINDING = project_config.load()


def test_an_unbound_runtime_module_is_a_clear_setup_error(area: Path, capsys) -> None:
    """Before the project has code, `.agents/project.json` binds no runtime module: refuse with a setup message."""
    if _BINDING.runtime_config_module is not None:
        pytest.skip("this checkout binds a runtime configuration module")
    assert cli(area, "record", "--run", "real", "--profile", PROFILE, "--approved-by", "test") == 2
    assert "python.runtime_config_module" in capsys.readouterr().err


@pytest.mark.skipif(
    _BINDING.runtime_config_module is None or not (_BINDING.root / PROFILE).exists(),
    reason="needs a bound runtime configuration module and the example profile",
)
def test_a_real_profile_records_and_checks_clean(area: Path, capsys) -> None:
    assert cli(area, "record", "--run", "real", "--profile", PROFILE, "--approved-by", "test") == 0
    assert cli(area, "check", "--run", "real", "--profile", PROFILE) == 0
    data = json.loads((area / "real.json").read_text(encoding="utf-8"))
    assert len(data["values"]) > 100
    # a hand-edit of one stored value (as if the profile had been changed) is refused
    key = next(
        k for k, v in data["values"].items() if isinstance(v, int) and not isinstance(v, bool)
    )
    data["values"][key] = data["values"][key] + 1
    (area / "real.json").write_text(json.dumps(data), encoding="utf-8")
    assert cli(area, "check", "--run", "real", "--profile", PROFILE) == 1
    assert key in capsys.readouterr().out


# ---- property tests (decision 130) ----
SEGMENT = st.text("abcXYZ09._-", min_size=1, max_size=6).filter(lambda s: s not in {".", ".."})


@given(st.lists(SEGMENT, min_size=1, max_size=5))
def test_property_slash_spelling_never_changes_the_canonical_profile(parts: list[str]) -> None:
    forward, backward = "/".join(parts), "\\".join(parts)
    assert gate.canonical_text(forward) == gate.canonical_text(backward)
    assert gate.canonical_text(gate.canonical_text(forward)) == gate.canonical_text(forward)
    assert gate.canonical_text("./" + forward) == gate.canonical_text(forward)


@given(st.lists(SEGMENT, min_size=1, max_size=5), st.lists(SEGMENT, min_size=1, max_size=5))
def test_property_different_profiles_stay_different(a: list[str], b: list[str]) -> None:
    same = [s.lower() for s in a] == [s.lower() for s in b]
    if gate.canonical_text("/".join(a)) == gate.canonical_text("/".join(b)):
        assert a == b or (same and sys.platform == "win32")


KEY = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=6)
LEAF = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-5, 5),
    st.text(max_size=4),
    st.lists(st.integers(), max_size=3),
)
TREE = st.recursive(LEAF, lambda kids: st.dictionaries(KEY, kids, max_size=4), max_leaves=12)


@given(TREE)
def test_property_flatten_is_deterministic_and_diff_with_itself_is_empty(tree: Any) -> None:
    flat = gate.flatten(tree)
    assert flat == gate.flatten(json.loads(json.dumps(tree)))
    assert gate.diff(flat, flat) == []


@given(TREE, TREE)
def test_property_listing_exactly_the_diff_approves_it_and_nothing_less_does(
    a: Any, b: Any
) -> None:
    old, new = gate.flatten(a), gate.flatten(b)
    rows = gate.diff(old, new)
    changes = [
        {"key": r.key, "to": gate.REMOVED if r.kind == "removed" else r.current, "source": "s"}
        for r in rows
    ]
    assert gate.unapproved(rows, changes) == []
    assert gate.unapproved(rows, []) == rows
    for index, row in enumerate(rows):
        partial = changes[:index] + changes[index + 1 :]
        assert gate.unapproved(rows, partial) == [row]


@given(TREE, TREE)
def test_property_diff_counts_are_symmetric(a: Any, b: Any) -> None:
    old, new = gate.flatten(a), gate.flatten(b)
    forward, backward = gate.diff(old, new), gate.diff(new, old)
    assert len(forward) == len(backward)
    assert {r.key for r in forward} == {r.key for r in backward}
    assert {r.key for r in forward if r.kind == "added"} == {
        r.key for r in backward if r.kind == "removed"
    }


@given(TREE, st.integers(-5, 5))
def test_property_an_approval_for_the_wrong_value_never_passes(a: Any, delta: int) -> None:
    old = gate.flatten(a)
    new = {**old, "/__probe__": 7}  # generated keys have at most 6 characters: no collision
    rows = gate.diff(old, new)
    wrong = [{"key": "/__probe__", "to": 7 + delta, "source": "s"}]
    assert (gate.unapproved(rows, wrong) == []) == (delta == 0)
