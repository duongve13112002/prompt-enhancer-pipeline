"""Tests for purge_tmp.py (PM disk cleanup): containment, archive-before-delete, manifest and secrets protection."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

SCRIPT = Path(__file__).resolve().parents[1] / "purge_tmp.py"


@pytest.fixture
def pt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("purge_tmp", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = (tmp_path / "proj").resolve()
    (project / ".tmp" / "worktrees").mkdir(parents=True)
    monkeypatch.setattr(module, "PROJECT", project)
    monkeypatch.setattr(module, "ARCHIVE", project / ".tmp" / "pm-archive")
    return module


def make_worktree(project: Path, name: str = "wt1") -> Path:
    wt = project / ".tmp" / "worktrees" / name
    (wt / ".tmp" / "pytest-1").mkdir(parents=True)
    (wt / ".tmp" / "report.md").write_text("report", encoding="utf-8")
    (wt / ".tmp" / "big.bin").write_bytes(b"x" * 10)
    (wt / ".tmp" / "pytest-1" / "log.txt").write_text("basetemp", encoding="utf-8")
    (wt / ".secrets").mkdir()  # the tracked placeholder only, as in every checkout
    (wt / ".secrets" / "README.md").write_text("placeholder", encoding="utf-8")
    return wt


def write_registry(project: Path, workers: object) -> Path:
    path = project / ".tmp" / "pm-state" / "cli-workers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workers if isinstance(workers, str) else json.dumps(workers), encoding="utf-8")
    return path


def busy_worker(cwd: Path) -> dict:
    return {"w": {"status": "busy", "cwd": str(cwd)}}


def test_project_scripts_dir_is_two_levels_below_the_root_in_the_tracked_location() -> None:
    assert (
        SCRIPT.parents[2] == Path(__file__).resolve().parents[3]
    )  # tools/agent-workflow -> project root


def test_archive_remove_keeps_small_reports_and_deletes_the_tree(pt: ModuleType) -> None:
    wt = make_worktree(pt.PROJECT)
    pt.archive_remove([str(wt)])
    assert not wt.exists()
    archived = pt.ARCHIVE / "wt1"
    assert (archived / "report.md").read_text(encoding="utf-8") == "report"
    assert not (archived / "big.bin").exists()  # not a report suffix
    assert not (archived / "pytest-1").exists()  # basetemp trees are skipped
    assert not (archived / ".secrets").exists()  # .secrets is never archived


@pytest.mark.parametrize("relative", ["..", "../outside", "."])
def test_paths_outside_the_project_tmp_are_refused(pt: ModuleType, relative: str) -> None:
    for target in (pt.PROJECT / relative, pt.PROJECT, pt.PROJECT / "src"):
        with pytest.raises(SystemExit, match="refusing"):
            pt.archive_remove([str(target)])


def test_a_sibling_directory_outside_tmp_is_refused_even_inside_the_project(
    pt: ModuleType,
) -> None:
    (pt.PROJECT / "models").mkdir()
    (pt.PROJECT / "models" / "w.bin").write_bytes(b"1")
    with pytest.raises(SystemExit, match=r"outside \.tmp"):
        pt.purge_older(str(pt.PROJECT / "models"), 0)
    assert (pt.PROJECT / "models" / "w.bin").exists()


def test_purge_children_removes_matching_dirs_only_and_never_secrets(pt: ModuleType) -> None:
    tmp = pt.PROJECT / ".tmp"
    for name in ("pytest-a", "pytest-b", "keep-me", ".secrets"):
        (tmp / name).mkdir()
        (tmp / name / "f.txt").write_text("x", encoding="utf-8")
    pt.purge_children(str(tmp), ["pytest-*", ".secrets"])
    assert not (tmp / "pytest-a").exists() and not (tmp / "pytest-b").exists()
    assert (tmp / "keep-me" / "f.txt").exists() and (tmp / ".secrets" / "f.txt").exists()


def test_a_tree_holding_a_manifest_bound_file_is_kept(pt: ModuleType) -> None:
    tmp = pt.PROJECT / ".tmp"
    (tmp / "review").mkdir()
    (tmp / "pytest-evidence").mkdir()
    (tmp / "pytest-evidence" / "bound.json").write_text("{}", encoding="utf-8")
    (tmp / "pytest-other").mkdir()
    (tmp / "review" / "candidate-manifest.json").write_text(
        json.dumps({"files": [{"path": ".tmp/pytest-evidence/bound.json"}]}), encoding="utf-8"
    )
    pt.purge_children(str(tmp), ["pytest-*"])
    assert (tmp / "pytest-evidence" / "bound.json").exists() and not (tmp / "pytest-other").exists()


def test_purge_older_deletes_by_age_and_skips_secrets_and_bound_files(pt: ModuleType) -> None:
    parent = pt.PROJECT / ".tmp" / "scratch"
    parent.mkdir()
    old, recent = parent / "old-dir", parent / "recent-dir"
    for d in (old, recent, parent / ".secrets"):
        d.mkdir()
        (d / "f").write_text("x", encoding="utf-8")
    (parent / "old.txt").write_text("x", encoding="utf-8")
    past = time.time() - 48 * 3600
    for p in (old, parent / "old.txt", parent / ".secrets"):
        os.utime(p, (past, past))
    pt.purge_older(str(parent), 24)
    assert not old.exists() and not (parent / "old.txt").exists()
    assert recent.exists() and (parent / ".secrets" / "f").exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_inside_a_purged_tree_is_not_followed(pt: ModuleType) -> None:
    tmp = pt.PROJECT / ".tmp"
    target = tmp / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    victim = tmp / "pytest-victim"
    victim.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(victim / "link"), str(target)],
        check=True,
        capture_output=True,
    )
    pt.purge_children(str(tmp), ["pytest-*"])
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "argv", [[], ["nonsense"], ["archive-remove"], ["purge-children", "d"], ["purge-older", "d"]]
)
def test_bad_command_lines_exit_with_usage_not_a_traceback(pt: ModuleType, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        pt.main(argv)
    assert "purge_tmp.py" in str(info.value) or "unknown" in str(info.value)


def test_purge_older_rejects_a_non_numeric_age(pt: ModuleType) -> None:
    with pytest.raises(SystemExit, match="hours must be a number"):
        pt.main(["purge-older", str(pt.PROJECT / ".tmp"), "soon"])


def junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )


def test_archive_remove_refuses_a_tree_holding_real_credentials(pt: ModuleType) -> None:
    wt = make_worktree(pt.PROJECT)
    (wt / ".secrets" / "key.txt").write_text("dummy", encoding="utf-8")
    assert pt.archive_remove([str(wt)]) == 1
    assert (wt / ".secrets" / "key.txt").exists() and (wt / ".tmp" / "report.md").exists()
    assert not pt.ARCHIVE.exists()  # refused before anything is archived


@pytest.mark.parametrize(
    "name", ["pm-state", "pm-archive", "models", ".venv", "runs", ".secrets", "cli-jobs"]
)
def test_every_entry_point_refuses_a_protected_name_at_depth(pt: ModuleType, name: str) -> None:
    wt = make_worktree(pt.PROJECT)
    deep = wt / ".tmp" / "scratch" / "deeper" / name
    deep.mkdir(parents=True)
    (deep / "evidence.txt").write_text("keep", encoding="utf-8")
    assert pt.archive_remove([str(wt)]) == 1
    assert pt.purge_children(str(wt / ".tmp"), ["scratch"]) == 1
    old = time.time() - 48 * 3600
    os.utime(wt / ".tmp" / "scratch", (old, old))
    assert pt.purge_older(str(wt / ".tmp"), 24) >= 1
    assert (deep / "evidence.txt").exists()


def test_gemini_results_are_protected_but_other_agy_work_is_not(pt: ModuleType) -> None:
    tmp = pt.PROJECT / ".tmp"
    (tmp / "agy-work" / "_results").mkdir(parents=True)
    (tmp / "agy-work" / "_results" / "g1.md").write_text("result", encoding="utf-8")
    (tmp / "agy-work" / "scratch-1").mkdir()
    assert pt.purge_children(str(tmp), ["agy-work"]) == 1
    assert (tmp / "agy-work" / "_results" / "g1.md").exists()
    assert pt.purge_children(str(tmp / "agy-work"), ["scratch-*"]) == 0
    assert not (tmp / "agy-work" / "scratch-1").exists()


@pytest.mark.parametrize("name", ["pm-state", "pm-archive", "cli-jobs", "runs"])
def test_a_protected_name_given_as_the_target_is_refused(pt: ModuleType, name: str) -> None:
    target = pt.PROJECT / ".tmp" / name
    target.mkdir()
    (target / "ledger.json").write_text("{}", encoding="utf-8")
    assert pt.archive_remove([str(target)]) == 1
    assert pt.purge_children(str(pt.PROJECT / ".tmp"), [name]) == 1
    assert (target / "ledger.json").exists()


def test_a_nested_models_placeholder_is_removable_but_real_weights_are_not(
    pt: ModuleType,
) -> None:
    wt = make_worktree(pt.PROJECT)
    (wt / "models").mkdir()
    (wt / "models" / "README.md").write_text("placeholder", encoding="utf-8")
    assert pt.archive_remove([str(wt)]) == 0 and not wt.exists()
    wt = make_worktree(pt.PROJECT, "wt2")
    (wt / "models").mkdir()
    (wt / "models" / "w.safetensors").write_bytes(b"1")
    assert pt.archive_remove([str(wt)]) == 1 and (wt / "models" / "w.safetensors").exists()


def test_a_placeholder_directory_holding_a_subdirectory_is_not_a_placeholder(
    pt: ModuleType,
) -> None:
    wt = make_worktree(pt.PROJECT)
    (wt / ".secrets" / "keys").mkdir()
    assert pt.archive_remove([str(wt)]) == 1 and (wt / ".secrets" / "keys").is_dir()


@pytest.mark.parametrize("where", ["equal", "inside", "contains"])
def test_a_busy_worker_cwd_blocks_every_entry_point(pt: ModuleType, where: str) -> None:
    tmp = pt.PROJECT / ".tmp"
    wt = make_worktree(pt.PROJECT)
    (wt / "sub").mkdir()
    cwd = {"equal": wt, "inside": wt / "sub", "contains": tmp / "worktrees"}[where]
    write_registry(pt.PROJECT, busy_worker(cwd))
    assert pt.archive_remove([str(wt)]) == 1
    assert pt.purge_children(str(tmp / "worktrees"), ["wt*"]) == 1
    old = time.time() - 48 * 3600
    os.utime(wt, (old, old))
    assert pt.purge_older(str(tmp / "worktrees"), 24) == 1
    assert (wt / ".tmp" / "report.md").exists()


def test_an_idle_worker_or_another_cwd_does_not_block(pt: ModuleType) -> None:
    wt, other = make_worktree(pt.PROJECT), make_worktree(pt.PROJECT, "wt2")
    write_registry(
        pt.PROJECT,
        {"workers": {"a": {"status": "idle", "cwd": str(wt)}, "b": busy_worker(other)["w"]}},
    )
    assert pt.archive_remove([str(wt)]) == 0 and not wt.exists()
    assert pt.archive_remove([str(other)]) == 1 and other.exists()


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "[]",
        '{"workers": []}',
        '{"w": {"status": "busy"}}',
        '{"w": {"status": "busy", "cwd": 5}}',
    ],
)
def test_an_unreadable_or_unusable_registry_refuses_everything(
    pt: ModuleType, content: str
) -> None:
    wt = make_worktree(pt.PROJECT)
    write_registry(pt.PROJECT, content)
    with pytest.raises(SystemExit, match="refusing"):
        pt.archive_remove([str(wt)])
    with pytest.raises(SystemExit, match="refusing"):
        pt.purge_children(str(pt.PROJECT / ".tmp"), ["worktrees"])
    with pytest.raises(SystemExit, match="refusing"):
        pt.purge_older(str(pt.PROJECT / ".tmp"), 0)
    assert (wt / ".tmp" / "report.md").exists()


def test_a_registry_that_is_a_directory_refuses(pt: ModuleType) -> None:
    (pt.PROJECT / ".tmp" / "pm-state" / "cli-workers.json").mkdir(parents=True)
    with pytest.raises(SystemExit, match="refusing"):
        pt.archive_remove([str(make_worktree(pt.PROJECT))])


def test_main_returns_one_when_archive_remove_refuses(pt: ModuleType) -> None:
    wt = make_worktree(pt.PROJECT)
    write_registry(pt.PROJECT, busy_worker(wt))
    assert pt.main(["archive-remove", str(wt)]) == 1
    write_registry(pt.PROJECT, {})
    assert pt.main(["archive-remove", str(wt)]) == 0


def test_refusals_are_reported(pt: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    wt = make_worktree(pt.PROJECT)
    write_registry(pt.PROJECT, busy_worker(wt))
    pt.archive_remove([str(wt)])
    pt.purge_children(str(pt.PROJECT / ".tmp" / "worktrees"), ["wt1"])
    out = capsys.readouterr().out
    assert out.count("busy worker cwd") == 2 and "refused" in out


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_child_is_removed_as_a_link_and_counted_truthfully(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp = pt.PROJECT / ".tmp"
    target = tmp / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    junction(tmp / "pytest-link", target)
    assert pt.purge_children(str(tmp), ["pytest-*"]) == 0
    assert not os.path.lexists(tmp / "pytest-link") and (target / "keep.txt").exists()
    assert "(link only)" in capsys.readouterr().out
    (tmp / "scratch").mkdir()
    junction(tmp / "scratch" / "old-link", target)
    assert pt.purge_older(str(tmp / "scratch"), -1) == 0  # a negative age makes every entry old
    assert not os.path.lexists(tmp / "scratch" / "old-link") and (target / "keep.txt").exists()
    assert "removed 1 entries" in capsys.readouterr().out


def test_a_link_that_cannot_be_removed_is_reported_as_kept(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp = pt.PROJECT / ".tmp"
    (tmp / "pytest-x").mkdir()
    with (
        mock.patch.object(pt, "_is_link", lambda path: Path(path).name == "pytest-x"),
        mock.patch.object(pt, "_remove_link", lambda path: False),
    ):
        assert pt.purge_children(str(tmp), ["pytest-*"]) == 1
    assert "kept pytest-x (link)" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_protected_name_behind_a_junction_is_neither_followed_nor_refused(
    pt: ModuleType,
) -> None:
    tmp = pt.PROJECT / ".tmp"
    ledgers = tmp / "ledgers"
    (ledgers / "runs").mkdir(parents=True)
    (ledgers / "runs" / "e.json").write_text("{}", encoding="utf-8")
    victim = tmp / "pytest-victim"
    victim.mkdir()
    junction(victim / "to-ledgers", ledgers)
    assert pt.purge_children(str(tmp), ["pytest-*"]) == 0
    assert not victim.exists() and (ledgers / "runs" / "e.json").exists()


NAMES = st.sampled_from(
    ["a", "b", "c", "pm-state", "models", ".secrets", "runs", "cli-jobs", "keep"]
)


@settings(
    max_examples=40,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    paths=st.lists(st.lists(NAMES, min_size=1, max_size=3), min_size=1, max_size=5),
    busy_index=st.integers(min_value=-1, max_value=4),
)
def test_property_no_target_set_deletes_a_protected_or_busy_path(
    pt: ModuleType,
    tmp_path_factory: pytest.TempPathFactory,
    paths: list[list[str]],
    busy_index: int,
) -> None:
    """Invariant: after every entry point ran, each file under a protected name (real content) or inside a busy
    cwd still exists."""
    project = tmp_path_factory.mktemp("prop").resolve()
    wt = project / ".tmp" / "worktrees" / "wt"
    wt.mkdir(parents=True)
    files: list[Path] = []
    for parts in paths:
        directory = wt.joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / "f.txt"
        marker.write_text("x", encoding="utf-8")
        files.append(marker)
    busy_dir = wt.joinpath(*paths[busy_index]) if 0 <= busy_index < len(paths) else None
    write_registry(project, busy_worker(busy_dir) if busy_dir else {})
    protected_files = [
        f
        for f in files
        if any(part in pt.PROTECTED_NAMES for part in f.relative_to(wt).parts[:-1])
        or (busy_dir is not None and busy_dir in f.parents)
    ]
    old = time.time() - 48 * 3600

    def age(directory: Path) -> None:
        for child in directory.iterdir():
            os.utime(child, (old, old))

    with (
        mock.patch.object(pt, "PROJECT", project),
        mock.patch.object(pt, "ARCHIVE", project / ".tmp" / "pm-archive"),
    ):
        pt.archive_remove([str(wt)])
        for parent in (wt, project / ".tmp" / "worktrees"):
            if parent.exists():
                age(parent)
                pt.purge_older(str(parent), 24)
        for parent in (wt, project / ".tmp" / "worktrees"):
            if parent.exists():
                pt.purge_children(str(parent), ["*"])
    assert all(f.exists() for f in protected_files)


def test_remove_link_reports_success_only_when_the_entry_is_really_gone(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = pt.PROJECT / ".tmp" / "stuck"
    stuck.mkdir()

    def refuse(_path: str) -> None:
        raise PermissionError("held open")

    monkeypatch.setattr(pt.os, "unlink", refuse)
    monkeypatch.setattr(pt.os, "rmdir", refuse)
    assert pt._remove_link(str(stuck)) is False and stuck.exists()
    monkeypatch.undo()
    assert pt._remove_link(str(stuck)) is True and not stuck.exists()


@pytest.mark.parametrize(
    "name", ["pm-state", "pm-archive", "cli-jobs", "runs", "PM-State", ".Secrets"]
)
def test_purging_inside_a_protected_tree_is_refused(pt: ModuleType, name: str) -> None:
    # Re-review NF-01/NF-03: the parent itself, any ancestor and case variants are protected.
    tree = pt.PROJECT / ".tmp" / name
    (tree / "sub").mkdir(parents=True)
    (tree / "ledger.json").write_text("{}", encoding="utf-8")
    (tree / "sub" / "state.json").write_text("{}", encoding="utf-8")
    old = time.time() - 48 * 3600
    for path in (tree / "ledger.json", tree / "sub"):
        os.utime(path, (old, old))
    with pytest.raises(SystemExit, match="protected tree"):
        pt.purge_older(str(tree), 0)
    with pytest.raises(SystemExit, match="protected tree"):
        pt.purge_children(str(tree), ["*"])
    with pytest.raises(SystemExit, match="protected tree"):
        pt.purge_children(str(tree / "sub"), ["*"])
    assert pt.archive_remove([str(tree / "sub")]) == 1
    assert (tree / "ledger.json").exists() and (tree / "sub" / "state.json").exists()


@pytest.mark.parametrize("name", ["Models", "RUNS", "Pm-Archive"])
def test_a_case_variant_of_a_protected_name_at_depth_is_refused(pt: ModuleType, name: str) -> None:
    wt = make_worktree(pt.PROJECT)
    deep = wt / ".tmp" / "scratch" / name
    deep.mkdir(parents=True)
    (deep / "evidence.txt").write_text("keep", encoding="utf-8")
    assert pt.archive_remove([str(wt)]) == 1
    assert pt.purge_children(str(wt / ".tmp"), ["scratch"]) == 1
    assert (deep / "evidence.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_archive_remove_removes_a_junction_worktree_as_a_link_only(pt: ModuleType) -> None:
    # Re-review NF-02: archive-remove uses the same link-safe removal as the purge commands.
    target = pt.PROJECT / ".tmp" / "real-tree"
    (target / ".tmp").mkdir(parents=True)
    (target / ".tmp" / "report.md").write_text("report", encoding="utf-8")
    link = pt.PROJECT / ".tmp" / "worktrees" / "wt-link"
    junction(link, target)
    assert pt.archive_remove([str(link)]) == 0
    assert not os.path.lexists(link)
    assert (target / ".tmp" / "report.md").exists()  # never followed
    assert not (pt.ARCHIVE / "wt-link").exists()  # nothing archived through the link


def test_archive_remove_counts_a_tree_left_partially(pt: ModuleType) -> None:
    wt = make_worktree(pt.PROJECT)
    with mock.patch.object(pt, "_remove", return_value=False):
        assert pt.archive_remove([str(wt)]) == 1


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_link_inside_a_protected_tree_is_refused(pt: ModuleType) -> None:
    target = pt.PROJECT / ".tmp" / "elsewhere"
    target.mkdir()
    state = pt.PROJECT / ".tmp" / "pm-state"
    state.mkdir()
    junction(state / "wt-link", target)
    assert pt.archive_remove([str(state / "wt-link")]) == 1
    assert os.path.lexists(state / "wt-link")
