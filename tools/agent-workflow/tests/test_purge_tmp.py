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


# --- T-025: dry run, configured sweep, archive retention -------------------------------------------------------------

CLEANUP_KEYS = ("sweep_age_hours", "archive_retention_hours", "scan_timeout_seconds")
EXAMPLE = {"sweep_age_hours": 72, "archive_retention_hours": 336, "scan_timeout_seconds": 2}


def age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def cfg(pt: ModuleType, **override: float):
    return pt.CleanupConfig(**{**{k: float(v) for k, v in EXAMPLE.items()}, **override})


def tmp_dir(pt: ModuleType, name: str, hours: float = 100, parent: str = "") -> Path:
    path = pt.PROJECT / ".tmp" / parent / name
    path.mkdir(parents=True)
    (path / "f.txt").write_text("x", encoding="utf-8")
    age(path, hours)
    return path


def write_note(pt: ModuleType, inflight: str) -> None:
    note = pt.PROJECT / ".tmp" / "pm-state" / "pm-continuation.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"# note\n## IN-FLIGHT\n{inflight}\n## NOW\n- other T-999\n", encoding="utf-8")


def test_shipped_cleanup_config_loads(pt: ModuleType) -> None:
    config = pt.load_cleanup_config()
    assert (config.sweep_age_hours, config.archive_retention_hours) == (72, 336)
    assert config.scan_timeout_seconds == 2


@pytest.mark.parametrize("key", CLEANUP_KEYS)
def test_cleanup_config_refuses_a_missing_key(pt: ModuleType, tmp_path: Path, key: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({k: v for k, v in EXAMPLE.items() if k != key}), encoding="utf-8")
    with pytest.raises(pt.CleanupConfigError, match=key):
        pt.load_cleanup_config(path)


@pytest.mark.parametrize("key", CLEANUP_KEYS)
@pytest.mark.parametrize("bad", [0, -1, "72", True, None, float("nan"), float("inf")])
def test_cleanup_config_refuses_an_invalid_value(
    pt: ModuleType, tmp_path: Path, key: str, bad: object
) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({**EXAMPLE, key: bad}), encoding="utf-8")
    with pytest.raises(pt.CleanupConfigError, match=key):
        pt.load_cleanup_config(path)


def test_cleanup_config_refuses_unknown_keys_and_bad_files(pt: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({**EXAMPLE, "extra": 1}), encoding="utf-8")
    with pytest.raises(pt.CleanupConfigError, match="extra"):
        pt.load_cleanup_config(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(pt.CleanupConfigError):
        pt.load_cleanup_config(path)
    with pytest.raises(pt.CleanupConfigError):
        pt.load_cleanup_config(tmp_path / "absent.json")


def test_cleanup_config_accepts_non_default_values(pt: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {"sweep_age_hours": 1.5, "archive_retention_hours": 5, "scan_timeout_seconds": 0.25}
        ),
        encoding="utf-8",
    )
    config = pt.load_cleanup_config(path)
    assert (config.sweep_age_hours, config.archive_retention_hours) == (1.5, 5)
    assert config.scan_timeout_seconds == 0.25


def test_sweep_refuses_a_broken_config_from_the_command_line(
    pt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"sweep_age_hours": 72}), encoding="utf-8")
    monkeypatch.setattr(pt, "CLEANUP_CONFIG", path)
    with pytest.raises(SystemExit, match="archive_retention_hours"):
        pt.main(["sweep", "--dry-run"])


def test_sweep_deletes_only_old_unowned_children(pt: ModuleType) -> None:
    old = tmp_dir(pt, "old-scratch")
    fresh = tmp_dir(pt, "fresh", hours=1)
    assert pt.sweep(config=cfg(pt)) == 0
    assert not old.exists() and fresh.exists()


def test_sweep_age_is_the_configured_value(pt: ModuleType) -> None:
    mid = tmp_dir(pt, "mid", hours=10)
    pt.sweep(config=cfg(pt))
    assert mid.exists()
    pt.sweep(config=cfg(pt, sweep_age_hours=5))
    assert not mid.exists()


def test_dry_run_deletes_nothing_in_every_command(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_dir(pt, "old-scratch")
    wt = make_worktree(pt.PROJECT)
    age(wt, 1000)
    assert pt.sweep(dry_run=True, config=cfg(pt)) == 0
    assert "would delete old-scratch" in capsys.readouterr().out
    pt.purge_children(str(pt.PROJECT / ".tmp"), ["old*"], dry_run=True)
    pt.purge_older(str(pt.PROJECT / ".tmp" / "worktrees"), 1, dry_run=True)
    assert pt.archive_remove([str(wt)], dry_run=True) == 0
    assert old.exists() and (wt / ".tmp" / "report.md").exists()
    assert not (pt.ARCHIVE / wt.name).exists()


def test_main_accepts_dry_run_for_sweep(pt: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    old = tmp_dir(pt, "old-scratch")
    assert pt.main(["sweep", "--dry-run"]) == 0
    assert old.exists() and "would delete old-scratch" in capsys.readouterr().out


def test_sweep_keeps_a_task_named_in_flight(pt: ModuleType) -> None:
    flying = tmp_dir(pt, "T-025", parent="worktrees")
    done = tmp_dir(pt, "T-002", parent="worktrees")
    similar = tmp_dir(pt, "T-02", parent="worktrees")  # a prefix of another name is not a mention
    write_note(pt, "- worker in .tmp/worktrees/T-025, brief .tmp/T-025/brief.md")
    pt.sweep(config=cfg(pt))
    assert flying.exists()
    assert not done.exists() and not similar.exists()
    assert (pt.PROJECT / ".tmp" / "worktrees").is_dir()  # the container is never judged


def test_sweep_ignores_names_outside_the_in_flight_section(pt: ModuleType) -> None:
    other = tmp_dir(pt, "T-999")  # only mentioned under `## NOW`
    write_note(pt, "- nothing relevant")
    pt.sweep(config=cfg(pt))
    assert not other.exists()


def test_sweep_keeps_a_busy_worker_tree(pt: ModuleType) -> None:
    busy = tmp_dir(pt, "wt-busy", parent="worktrees")
    idle = tmp_dir(pt, "wt-idle", parent="worktrees")
    write_registry(pt.PROJECT, busy_worker(busy / "sub"))
    pt.sweep(config=cfg(pt))
    assert busy.exists() and not idle.exists()


def test_sweep_never_touches_protected_trees(pt: ModuleType) -> None:
    protected = [tmp_dir(pt, name) for name in ("pm-state", "pm-archive", "cli-jobs", "runs")]
    results = pt.PROJECT / ".tmp" / "agy-work" / "_results"
    results.mkdir(parents=True)
    age(results, 1000)
    secrets = tmp_dir(pt, ".secrets")
    pt.sweep(config=cfg(pt))
    assert all(p.exists() for p in [*protected, results, secrets])


def test_the_cheap_plan_never_counts_a_protected_name_as_an_orphan(pt: ModuleType) -> None:
    tmp_dir(pt, "pm-state")
    tmp_dir(pt, "old-scratch")
    status = {i.path.name: i.status for i in pt.plan_sweep(72, refusals=False)}
    assert status == {"pm-state": "protected", "old-scratch": "orphan"}


def test_sweep_skips_a_tree_holding_a_nested_protected_tree(pt: ModuleType) -> None:
    holder = tmp_dir(pt, "holder")
    (holder / "models").mkdir()
    (holder / "models" / "weights.bin").write_bytes(b"w")
    pt.sweep(config=cfg(pt))
    assert (holder / "models" / "weights.bin").exists()


def test_sweep_keeps_agy_work_while_a_job_is_unfinished(pt: ModuleType) -> None:
    job = tmp_dir(pt, "job-a", parent="agy-work")
    state = pt.PROJECT / ".tmp" / "pm-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(
        json.dumps([{"kind": "gemini", "id": "job-a", "label": "x"}]), encoding="utf-8"
    )
    pt.sweep(config=cfg(pt))
    assert job.exists()
    results = pt.PROJECT / ".tmp" / "agy-work" / "_results"
    results.mkdir(parents=True)
    # agy-job.ps1 writes `<id>.stderr.txt` while the job runs: only `<id>.done` means finished (QA finding 1)
    (results / "job-a.stderr.txt").write_text("", encoding="utf-8")
    (results / "job-a.json").write_text("{}", encoding="utf-8")
    pt.sweep(config=cfg(pt))
    assert job.exists()
    (results / "job-a.done").write_text("0", encoding="ascii")
    pt.sweep(config=cfg(pt))
    assert not job.exists() and results.exists()


@pytest.mark.parametrize("broken", ["cli-workers.json", "agents.json"])
def test_sweep_fails_closed_on_a_malformed_ownership_file(
    pt: ModuleType, broken: str, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_dir(pt, "old-scratch")
    state = pt.PROJECT / ".tmp" / "pm-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / broken).write_text("{broken", encoding="utf-8")
    assert pt.sweep(config=cfg(pt)) == 1
    assert old.exists()
    assert "ownership unknown" in capsys.readouterr().out


def test_sweep_fails_closed_on_an_unreadable_note(pt: ModuleType) -> None:
    old = tmp_dir(pt, "old-scratch")
    state = pt.PROJECT / ".tmp" / "pm-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "pm-continuation.md").mkdir()  # a directory cannot be read as the note
    assert pt.sweep(config=cfg(pt)) == 1
    assert old.exists()


def test_absent_ownership_files_mean_no_owner(pt: ModuleType) -> None:
    own = pt.load_ownership()
    assert own.problem is None and own.busy == [] and not own.agy_active


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_sweep_removes_a_junction_as_a_link_and_never_follows_it(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = pt.PROJECT / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("k", encoding="utf-8")
    link = pt.PROJECT / ".tmp" / "old-link"
    junction(link, target)
    real = time.time()  # a junction's own mtime cannot be set on Windows: move the clock instead (test double)
    monkeypatch.setattr(pt.time, "time", lambda: real + 1000 * 3600)
    pt.sweep(config=cfg(pt))
    assert not os.path.lexists(link)
    assert (target / "keep.txt").exists()


def test_prune_archive_deletes_only_old_children_and_keeps_the_archive(pt: ModuleType) -> None:
    pt.ARCHIVE.mkdir(parents=True)
    old = pt.ARCHIVE / "wt-old"
    new = pt.ARCHIVE / "wt-new"
    for path in (old, new):
        path.mkdir()
        (path / "r.md").write_text("r", encoding="utf-8")
    age(old, 400)
    age(new, 10)
    assert pt.prune_archive(dry_run=True, config=cfg(pt)) == 0
    assert old.exists()
    assert pt.prune_archive(config=cfg(pt)) == 0
    assert not old.exists() and new.exists() and pt.ARCHIVE.is_dir()
    assert pt.prune_archive(config=cfg(pt, archive_retention_hours=1)) == 0
    assert not new.exists() and pt.ARCHIVE.is_dir()


def test_sweep_and_purge_still_treat_the_archive_as_protected(pt: ModuleType) -> None:
    pt.ARCHIVE.mkdir(parents=True)
    old = pt.ARCHIVE / "wt-old"
    old.mkdir()
    age(old, 1000)
    age(pt.ARCHIVE, 1000)
    pt.sweep(config=cfg(pt))
    with pytest.raises(SystemExit):
        pt.purge_older(str(pt.ARCHIVE), 1)
    assert old.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_prune_archive_removes_a_link_child_without_following_it(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pt.ARCHIVE.mkdir(parents=True)
    target = pt.PROJECT / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("k", encoding="utf-8")
    link = pt.ARCHIVE / "wt-link"
    junction(link, target)
    real = time.time()  # test double: move the clock, a junction's own mtime cannot be set on Windows
    monkeypatch.setattr(pt.time, "time", lambda: real + 1000 * 3600)
    pt.prune_archive(config=cfg(pt))
    assert not os.path.lexists(link) and (target / "keep.txt").exists()


def test_bounded_size_reports_a_partial_scan(pt: ModuleType) -> None:
    tmp_dir(pt, "a")
    total, complete = pt._size_bounded(pt.PROJECT / ".tmp", time.monotonic() + 60)
    assert complete and total >= 1
    total, complete = pt._size_bounded(pt.PROJECT / ".tmp", time.monotonic() - 1)
    assert not complete and total == 0


# --- T-025 QA round 1: one authorization for every deleting path ------------------------------------------------------


def write_agents(pt: ModuleType, records: object) -> None:
    state = pt.PROJECT / ".tmp" / "pm-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(json.dumps(records), encoding="utf-8")


def archive_entry(pt: ModuleType, name: str, hours: float = 1000) -> Path:
    path = pt.ARCHIVE / name
    path.mkdir(parents=True)
    (path / "r.md").write_text("r", encoding="utf-8")
    age(path, hours)
    return path


def test_qa1_a_running_antigravity_job_is_not_finished_by_its_stderr_file(pt: ModuleType) -> None:
    job = tmp_dir(pt, "job-a", parent="agy-work")
    write_agents(pt, [{"kind": "gemini", "id": "job-a"}])
    results = pt.PROJECT / ".tmp" / "agy-work" / "_results"
    results.mkdir(parents=True)
    (results / "job-a.stderr.txt").write_text("", encoding="utf-8")
    status = {i.path.name: i.status for i in pt.plan_sweep(72)}
    assert status["job-a"] == "owned"
    pt.sweep(config=cfg(pt))
    assert job.exists()


@pytest.mark.parametrize(
    "records",
    [
        [{}],
        [{"kind": "other", "id": "x"}],
        [{"kind": "gemini"}],
        [{"kind": "gemini", "id": ""}],
        [{"kind": "gemini", "id": "../x"}],
        [{"kind": "codex", "id": "x"}],
        [{"kind": "claude", "id": "x", "cwd": 3}],
        ["text"],
        {"kind": "gemini", "id": "x"},
    ],
)
def test_qa1_a_malformed_agent_record_refuses_every_deletion(
    pt: ModuleType, records: object, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_dir(pt, "old-work")
    write_agents(pt, records)
    assert pt.main(["sweep"]) == 1
    assert old.exists() and "ownership unknown" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="refusing"):
        pt.purge_children(str(pt.PROJECT / ".tmp"), ["old*"])
    assert old.exists()


def test_qa1_prune_archive_keeps_an_entry_that_is_a_busy_workers_cwd(pt: ModuleType) -> None:
    entry = archive_entry(pt, "wt-busy")
    other = archive_entry(pt, "wt-idle")
    write_registry(pt.PROJECT, busy_worker(entry))
    assert pt.prune_archive(config=cfg(pt)) == 1
    assert entry.exists() and not other.exists()


def test_qa1_prune_archive_keeps_an_entry_holding_a_protected_tree(pt: ModuleType) -> None:
    entry = archive_entry(pt, "old-report")
    (entry / "runs").mkdir()
    (entry / "runs" / "evidence.txt").write_text("e", encoding="utf-8")
    age(entry, 1000)
    assert pt.prune_archive(config=cfg(pt)) == 1
    assert (entry / "runs" / "evidence.txt").exists()


def test_qa1_prune_archive_fails_closed_on_unreadable_ownership(pt: ModuleType) -> None:
    entry = archive_entry(pt, "wt-old")
    write_registry(pt.PROJECT, "{broken")
    assert pt.prune_archive(config=cfg(pt)) == 1
    assert entry.exists()


def test_qa1_prune_archive_keeps_a_task_named_in_flight(pt: ModuleType) -> None:
    entry = archive_entry(pt, "T-025")
    write_note(pt, "- T-025 in flight")
    pt.prune_archive(config=cfg(pt))
    assert entry.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_qa1_a_linked_tmp_is_never_enumerated(
    pt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    (outside / "pm-archive" / "outside-entry").mkdir(parents=True)
    (outside / "plain").mkdir()
    for path in (outside / "pm-archive" / "outside-entry", outside / "plain"):
        age(path, 1000)
    project = (tmp_path / "proj2").resolve()
    project.mkdir()
    junction(project / ".tmp", outside)
    monkeypatch.setattr(pt, "PROJECT", project)
    monkeypatch.setattr(pt, "ARCHIVE", project / ".tmp" / "pm-archive")
    assert pt.prune_archive(config=cfg(pt)) == 1
    assert pt.sweep(config=cfg(pt)) == 1
    assert (outside / "pm-archive" / "outside-entry").exists() and (outside / "plain").exists()
    assert "link on the path" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_qa1_a_linked_ancestor_refuses_the_explicit_commands(pt: ModuleType, tmp_path: Path) -> None:
    outside = tmp_path / "outside2"
    (outside / "victim").mkdir(parents=True)
    junction(pt.PROJECT / ".tmp" / "hop", outside)
    reason = pt.Gate(check_inflight=False).check(pt.PROJECT / ".tmp" / "hop" / "victim")
    assert reason is not None and "link on the path" in reason[1]


def test_qa1_planning_stops_at_the_deadline_and_says_incomplete(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_dir(pt, "old-a")
    tmp_dir(pt, "old-b")
    # test double: the clock stays before the deadline while ownership is read, then jumps past it
    ticks = iter([0.0, 0.0, 0.0, 0.0] + [100.0] * 100)
    monkeypatch.setattr(pt.time, "monotonic", lambda: next(ticks))
    items, complete = pt.plan_sweep_bounded(72, refusals=False, deadline=50.0)
    assert not complete and len(items) < 2


def test_qa1_authorize_refuses_outside_and_protected_paths(pt: ModuleType) -> None:
    gate = pt.Gate(check_inflight=True)
    assert gate.check(pt.PROJECT / "src") is not None
    assert gate.check(pt.PROJECT / ".tmp" / "pm-state") is not None
    assert gate.check(pt.PROJECT / ".tmp" / "plain") is None
    assert gate.check(pt.PROJECT / ".tmp" / "x" / ".." / "plain") is not None  # no lexical collapsing


NAMES = ["plain-1", "plain-2", "T-owned", "pm-state", "runs", "cli-jobs", "models", "wt-busy"]


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    old=st.sets(st.sampled_from(NAMES)),
    archived=st.sets(st.sampled_from(NAMES)),
    nested_runs=st.booleans(),
)
def test_qa1_property_no_command_removes_an_owned_or_protected_path(
    pt: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old: set[str],
    archived: set[str],
    nested_runs: bool,
) -> None:
    root = Path(
        __import__("tempfile").mkdtemp(dir=tmp_path)
    ).resolve()  # a fresh project per example
    with monkeypatch.context() as mp:
        mp.setattr(pt, "PROJECT", root)
        mp.setattr(pt, "ARCHIVE", root / ".tmp" / "pm-archive")
        (root / ".tmp" / "pm-state").mkdir(parents=True)
        (root / ".tmp" / "pm-state" / "pm-continuation.md").write_text(
            "## IN-FLIGHT\n- T-owned\n", encoding="utf-8"
        )
        busy = root / ".tmp" / "wt-busy"
        write_registry(root, busy_worker(busy / "sub"))
        for name in old | {"wt-busy"}:
            path = root / ".tmp" / name
            (path / "sub").mkdir(parents=True, exist_ok=True)
            age(path, 1000)
        for name in archived:
            path = root / ".tmp" / "pm-archive" / name
            (path / "sub").mkdir(parents=True, exist_ok=True)
            if nested_runs and name == "plain-1":
                (path / "runs").mkdir()
            age(path, 1000)
        keep = [root / ".tmp" / "pm-state", busy]
        keep += [root / ".tmp" / "pm-archive"] if archived else []
        keep += [root / ".tmp" / n for n in old if n in ("T-owned", "runs", "cli-jobs", "models")]
        protected = ("T-owned", "runs", "cli-jobs", "models", "pm-state")
        keep += [root / ".tmp" / "pm-archive" / n for n in archived if n in protected]
        if nested_runs and "plain-1" in archived:
            keep.append(root / ".tmp" / "pm-archive" / "plain-1")
        pt.sweep(config=cfg(pt))
        pt.prune_archive(config=cfg(pt))
        missing = [str(p) for p in keep if not p.exists()]
        assert not missing


# --- T-025 QA round 2: the gate takes the path as given and loads its own inputs --------------------------------------


def hop_to_real(pt: ModuleType) -> tuple[Path, Path]:
    """`.tmp/hop` is a junction to `.tmp/real`, which holds an old `victim`; returns (hop, victim)."""
    real = pt.PROJECT / ".tmp" / "real"
    (real / "victim").mkdir(parents=True)
    (real / "victim" / "f.txt").write_text("x", encoding="utf-8")
    age(real / "victim", 1000)
    hop = pt.PROJECT / ".tmp" / "hop"
    junction(hop, real)
    return hop, real / "victim"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
@pytest.mark.parametrize("dry_run", [True, False])
def test_qa2_archive_remove_refuses_a_path_through_a_linked_ancestor(
    pt: ModuleType, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    hop, victim = hop_to_real(pt)
    assert pt.archive_remove([str(hop / "victim")], dry_run=dry_run) == 1
    assert victim.exists() and "link on the path" in capsys.readouterr().out
    assert not (pt.ARCHIVE / "victim").exists()  # nothing archived through the link either


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
@pytest.mark.parametrize("dry_run", [True, False])
def test_qa2_purge_commands_refuse_a_directory_reached_through_a_link(
    pt: ModuleType, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    hop, victim = hop_to_real(pt)
    with pytest.raises(SystemExit, match="link on the path"):
        pt.purge_children(str(hop), ["victim"], dry_run=dry_run)
    with pytest.raises(SystemExit, match="link on the path"):
        pt.purge_older(str(hop), 1, dry_run=dry_run)
    with pytest.raises(SystemExit, match="link on the path"):
        pt.main(["purge-older", str(hop), "1", "--dry-run"])
    assert victim.exists()
    assert "would remove" not in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_qa2_sweep_never_selects_through_a_link(pt: ModuleType) -> None:
    _hop, victim = hop_to_real(pt)
    pt.sweep(config=cfg(pt))
    assert victim.exists()


def write_manifest(pt: ModuleType, bound: str) -> None:
    manifest = pt.PROJECT / ".tmp" / "review" / "candidate-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"files": [{"path": bound}]}), encoding="utf-8")


@pytest.mark.parametrize("dry_run", [True, False])
def test_qa2_archive_remove_keeps_a_manifest_bound_tree(
    pt: ModuleType, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    bound = tmp_dir(pt, "report-work", hours=1)
    write_manifest(pt, ".tmp/report-work/f.txt")
    assert pt.archive_remove([str(bound)], dry_run=dry_run) == 1
    assert (bound / "f.txt").exists() and "manifest-bound" in capsys.readouterr().out
    assert not (pt.ARCHIVE / "report-work").exists()


def test_qa2_every_deleting_command_keeps_a_manifest_bound_tree(pt: ModuleType) -> None:
    bound = tmp_dir(pt, "report-work")
    write_manifest(pt, ".tmp/report-work/f.txt")
    pt.purge_children(str(pt.PROJECT / ".tmp"), ["report*"])
    assert (bound / "f.txt").exists()
    pt.sweep(config=cfg(pt))
    assert (bound / "f.txt").exists()
    nested = tmp_dir(pt, "report-nested", parent="worktrees")
    write_manifest(pt, ".tmp/worktrees/report-nested/f.txt")
    pt.purge_older(str(pt.PROJECT / ".tmp" / "worktrees"), 1)
    assert (nested / "f.txt").exists()
    kept = archive_entry(pt, "report-work")
    write_manifest(pt, ".tmp/pm-archive/report-work/r.md")
    pt.prune_archive(config=cfg(pt))
    assert (kept / "r.md").exists()


def test_qa2_gate_remove_authorizes_again_and_never_deletes_a_refused_path(pt: ModuleType) -> None:
    protected = tmp_dir(pt, "pm-state")
    owned = tmp_dir(pt, "T-owned")
    write_note(pt, "- T-owned")
    gate = pt.Gate(check_inflight=True)
    assert not gate.remove(protected) and not gate.remove(owned) and not gate.remove(pt.PROJECT / "x")
    assert protected.exists() and owned.exists()
    plain = tmp_dir(pt, "plain")
    assert gate.remove(plain) and not plain.exists()


def test_qa2_a_parent_reference_is_refused_by_every_entrypoint(pt: ModuleType) -> None:
    keep = tmp_dir(pt, "keep")
    sneaky = str(pt.PROJECT / ".tmp" / "other" / ".." / "keep")
    with pytest.raises(SystemExit, match="parent reference"):
        pt.archive_remove([sneaky])
    with pytest.raises(SystemExit, match="parent reference"):
        pt.purge_children(str(pt.PROJECT / ".tmp" / "other" / ".."), ["keep"])
    assert keep.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    names=st.sets(st.sampled_from(["victim-1", "victim-2", "victim-3"]), min_size=1),
    command=st.sampled_from(["archive-remove", "purge-children", "purge-older", "sweep", "prune"]),
    dry_run=st.booleans(),
)
def test_qa2_property_no_command_selects_a_path_through_a_junction(
    pt: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    names: set[str],
    command: str,
    dry_run: bool,
) -> None:
    root = Path(__import__("tempfile").mkdtemp(dir=tmp_path)).resolve()
    with monkeypatch.context() as mp:
        mp.setattr(pt, "PROJECT", root)
        mp.setattr(pt, "ARCHIVE", root / ".tmp" / "pm-archive")
        real = root / ".tmp" / "real"
        victims = [real / n for n in sorted(names)]
        for victim in victims:
            victim.mkdir(parents=True)
            age(victim, 1000)
        hop = root / ".tmp" / "hop"
        junction(hop, real)
        try:
            if command == "archive-remove":
                pt.archive_remove([str(hop / n) for n in sorted(names)], dry_run=dry_run)
            elif command == "purge-children":
                pt.purge_children(str(hop), ["victim*"], dry_run=dry_run)
            elif command == "purge-older":
                pt.purge_older(str(hop), 1, dry_run=dry_run)
            elif command == "sweep":
                pt.sweep(dry_run=dry_run, config=cfg(pt))
            else:
                pt.prune_archive(dry_run=dry_run, config=cfg(pt))
        except SystemExit:
            pass  # a refused directory argument is a valid outcome
        assert all(v.exists() for v in victims)


# --- T-025 QA round 3: the gate reads the manifests of every root that can bind the target ----------------------------


def frozen_worktree(root: Path, rel: str, bound: str = "keep.txt") -> Path:
    """A worktree (`.git` marker) at `rel` under `.tmp` whose own candidate manifest binds `bound`; all aged."""
    wt = root / ".tmp" / rel
    (wt / ".tmp" / "review").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (wt / bound).parent.mkdir(parents=True, exist_ok=True)
    (wt / bound).write_text("frozen", encoding="utf-8")
    (wt / ".tmp" / "review" / "candidate-manifest.json").write_text(
        json.dumps({"files": [{"path": bound}]}), encoding="utf-8"
    )
    for path in [wt, *wt.parents]:
        if path == root / ".tmp":
            break
        age(path, 1000)
    return wt


@pytest.mark.parametrize("dry_run", [True, False])
def test_qa3_archive_remove_keeps_a_worktree_bound_by_its_own_manifest(
    pt: ModuleType, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    wt = frozen_worktree(pt.PROJECT, "worktrees/frozen")
    assert pt.archive_remove([str(wt)], dry_run=dry_run) == 1
    assert (wt / "keep.txt").exists() and "manifest-bound" in capsys.readouterr().out
    assert not (pt.ARCHIVE / "frozen").exists()


def test_qa3_the_cli_refuses_a_worktree_bound_by_its_own_manifest(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    wt = frozen_worktree(pt.PROJECT, "worktrees/frozen")
    assert pt.main(["archive-remove", "--dry-run", str(wt)]) == 1
    out = capsys.readouterr().out
    assert "refused" in out and "would archive" not in out
    assert pt.main(["archive-remove", str(wt)]) == 1
    assert (wt / "keep.txt").exists()


def test_qa3_a_parent_holding_a_worktree_with_a_manifest_is_kept_by_every_command(
    pt: ModuleType,
) -> None:
    wt = frozen_worktree(pt.PROJECT, "holder/frozen")
    holder = wt.parent
    assert pt.archive_remove([str(holder)]) == 1
    pt.purge_children(str(pt.PROJECT / ".tmp"), ["holder"])
    pt.sweep(config=cfg(pt))
    assert (wt / "keep.txt").exists()
    other = frozen_worktree(pt.PROJECT, "worktrees/inner/frozen")
    pt.purge_older(str(pt.PROJECT / ".tmp" / "worktrees"), 1)
    assert (other / "keep.txt").exists()


def test_qa3_a_target_below_a_worktree_is_bound_by_that_worktrees_manifest(pt: ModuleType) -> None:
    wt = frozen_worktree(pt.PROJECT, "worktrees/frozen", bound="sub/keep.txt")
    assert pt.archive_remove([str(wt / "sub")]) == 1
    pt.purge_children(str(wt), ["sub"])
    assert (wt / "sub" / "keep.txt").exists()


def test_qa3_prune_archive_keeps_an_archived_worktree_bound_by_its_own_manifest(
    pt: ModuleType,
) -> None:
    wt = frozen_worktree(pt.PROJECT, "pm-archive/frozen")
    assert pt.prune_archive(config=cfg(pt)) == 1
    assert (wt / "keep.txt").exists()


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    shape=st.sampled_from(["self", "holder", "below"]),
    command=st.sampled_from(["archive-remove", "purge-children", "purge-older", "sweep", "prune"]),
    dry_run=st.booleans(),
)
def test_qa3_property_no_command_removes_a_file_bound_by_any_root(
    pt: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    command: str,
    dry_run: bool,
) -> None:
    root = Path(__import__("tempfile").mkdtemp(dir=tmp_path)).resolve()
    with monkeypatch.context() as mp:
        mp.setattr(pt, "PROJECT", root)
        mp.setattr(pt, "ARCHIVE", root / ".tmp" / "pm-archive")
        rel = {"self": "worktrees/frozen", "holder": "holder/frozen", "below": "worktrees/frozen"}[
            shape
        ]
        bound = "sub/keep.txt" if shape == "below" else "keep.txt"
        wt = frozen_worktree(root, rel, bound=bound)
        archived = frozen_worktree(root, "pm-archive/frozen-copy", bound=bound)
        target = wt / "sub" if shape == "below" else (wt.parent if shape == "holder" else wt)
        try:
            if command == "archive-remove":
                pt.archive_remove([str(target)], dry_run=dry_run)
            elif command == "purge-children":
                pt.purge_children(str(target.parent), [target.name], dry_run=dry_run)
            elif command == "purge-older":
                if target.parent != root / ".tmp":
                    pt.purge_older(str(target.parent), 1, dry_run=dry_run)
            elif command == "sweep":
                pt.sweep(dry_run=dry_run, config=cfg(pt))
            else:
                pt.prune_archive(dry_run=dry_run, config=cfg(pt))
        except SystemExit:
            pass
        assert (wt / bound).exists() and (archived / bound).exists()
