"""Tests for purge_tmp.py (PM disk cleanup): containment, archive-before-delete, manifest and secrets protection."""

from __future__ import annotations

import errno
import importlib.util
import itertools
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR", raising=False)  # hermetic: the default state directory
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
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # footer rule 7: no console window on the user's screen
    )


def worker_worktree(project: Path, name: str = "wt1") -> Path:
    """A worker worktree as tools leave it: a `.git` file and its own `.tmp/pm-state` (test slots, guard log)."""
    wt = make_worktree(project, name)
    (wt / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    state = wt / ".tmp" / "pm-state"
    (state / "test-slots").mkdir(parents=True)
    (state / "tool-guard.log").write_text("guard log", encoding="utf-8")
    return wt


def test_archive_remove_takes_a_worker_worktree_with_its_own_state_and_archives_its_reports(
    pt: ModuleType,
) -> None:
    wt = worker_worktree(pt.PROJECT)
    assert pt.archive_remove([str(wt)]) == 0
    assert not wt.exists()
    assert (pt.ARCHIVE / "wt1" / "pm-state" / "tool-guard.log").read_text(encoding="utf-8") == "guard log"


def test_only_archive_remove_waives_a_worker_worktrees_own_state(pt: ModuleType) -> None:
    wt = worker_worktree(pt.PROJECT)
    assert pt.Gate(check_inflight=False, worker_state=True).check(wt) is None
    verdict = pt.Gate(check_inflight=False).check(wt)  # sweep, purge-children, purge-older: no archive first
    assert verdict is not None and "pm-state" in verdict[1]
    assert pt.purge_children(str(wt.parent), [wt.name]) == 1 and wt.exists()


@pytest.mark.parametrize(
    "shape",
    ["no .git file", ".git directory", "outside worktrees", "other protected name", "state nested deeper"],
)
def test_the_waiver_covers_only_the_worker_worktrees_own_state(pt: ModuleType, shape: str) -> None:
    wt = worker_worktree(pt.PROJECT, "wt-" + shape.replace(" ", "-").replace(".", ""))
    if shape == "no .git file":
        (wt / ".git").unlink()
    elif shape == ".git directory":
        (wt / ".git").unlink()
        (wt / ".git").mkdir()
    elif shape == "outside worktrees":
        moved = pt.PROJECT / ".tmp" / "elsewhere" / wt.name
        moved.parent.mkdir(parents=True)
        wt.rename(moved)
        wt = moved
    elif shape == "other protected name":
        (wt / "runs").mkdir()
        (wt / "runs" / "evidence.json").write_text("{}", encoding="utf-8")  # an empty `runs` holds nothing
    elif shape == "state nested deeper":
        (wt / ".tmp" / "pm-state").rename(wt / ".tmp" / "keep")
        (wt / ".tmp" / "keep" / "pm-state").mkdir()
        (wt / ".tmp" / "keep" / "pm-state" / "ledger.json").write_text("{}", encoding="utf-8")
    assert pt.archive_remove([str(wt)]) == 1
    assert wt.exists()


def test_a_linked_state_dir_is_removed_as_a_link_and_its_target_stays(pt: ModuleType) -> None:
    """A junction is never followed (T-025 design): the worktree goes, the link goes as a link, what it points to stays."""
    if os.name != "nt":
        pytest.skip("directory junctions need NTFS")
    wt = worker_worktree(pt.PROJECT)
    real = pt.PROJECT / ".tmp" / "pm-state" / "ledger"
    real.mkdir(parents=True)
    (real / "kept.json").write_text("{}", encoding="utf-8")
    state = wt / ".tmp" / "pm-state"
    shutil.rmtree(state)
    junction(state, real)
    assert pt.archive_remove([str(wt)]) == 0
    assert not os.path.lexists(wt)
    assert (real / "kept.json").read_text(encoding="utf-8") == "{}"
    assert not (pt.ARCHIVE / "wt1" / "pm-state").exists()  # nothing is archived through a link


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


def test_audit_reports_are_protected_but_a_nested_audit_folder_is_not(pt: ModuleType) -> None:
    tmp = pt.PROJECT / ".tmp"
    (tmp / "audit").mkdir()
    (tmp / "audit" / "audit-2026-11-15.md").write_text("report", encoding="utf-8")
    (tmp / "scratch" / "audit").mkdir(parents=True)
    assert pt.purge_children(str(tmp), ["audit"]) == 1
    assert (tmp / "audit" / "audit-2026-11-15.md").exists()
    assert pt.purge_children(str(tmp), ["scratch"]) == 0
    assert not (tmp / "scratch").exists()


def test_the_agy_runner_job_folder_survives_every_deletion_path(pt: ModuleType) -> None:
    """Only scripts/agy-runner/setup.ps1 can recreate `.tmp/agy-jobs` with its ACL, so no command may remove it,
    while a folder of that name elsewhere stays ordinary scratch."""
    jobs = tmp_dir(pt, "agy-jobs")
    (jobs / "job.json").write_text("{}", encoding="utf-8")
    age(jobs, 100)
    nested = pt.PROJECT / ".tmp" / "scratch-2" / "agy-jobs"
    nested.mkdir(parents=True)
    assert pt.archive_remove([str(jobs)]) == 1
    assert pt.purge_children(str(pt.PROJECT / ".tmp"), ["agy-jobs"]) == 1
    pt.sweep(config=cfg(pt))
    assert (jobs / "job.json").exists()
    assert {i.path.name: i.status for i in pt.plan_sweep(72, refusals=False)}.get("agy-jobs") == "protected"
    assert pt.purge_children(str(pt.PROJECT / ".tmp"), ["scratch-2"]) == 0
    assert not nested.exists()


def test_pinned_tool_binaries_survive_every_deletion_path(pt: ModuleType) -> None:
    """`.tmp/tools` holds hash-pinned binaries the user approved downloading (decision 113); an aged sweep, an archive
    removal and a child purge all keep it, while a folder of that name deeper in `.tmp` stays ordinary scratch."""
    tools = tmp_dir(pt, "tools")
    (tools / "gitleaks.exe").write_bytes(b"pinned")
    age(tools, 100)
    nested = pt.PROJECT / ".tmp" / "scratch-3" / "tools"
    nested.mkdir(parents=True)
    assert pt.archive_remove([str(tools)]) == 1
    assert pt.purge_children(str(pt.PROJECT / ".tmp"), ["tools"]) == 1
    pt.sweep(config=cfg(pt))
    assert (tools / "gitleaks.exe").read_bytes() == b"pinned"
    assert {i.path.name: i.status for i in pt.plan_sweep(72, refusals=False)}.get("tools") == "protected"
    assert pt.purge_children(str(pt.PROJECT / ".tmp"), ["scratch-3"]) == 0
    assert not nested.exists()


@pytest.mark.parametrize("spelling", ["agy-work", "AGY-Work"])
def test_the_agy_work_container_is_kept_without_results_while_its_workspaces_are_purgeable(
    pt: ModuleType, spelling: str
) -> None:
    """`.tmp/agy-work` carries the runner's write ACL from setup.ps1, so it is kept even when `_results` is absent."""
    tmp = pt.PROJECT / ".tmp"
    work = tmp_dir(pt, "agy-work")
    workspace = tmp_dir(pt, "review-1", parent="agy-work")
    folds = os.path.normcase("A") == "a"
    target = tmp / spelling if folds else work
    assert pt.archive_remove([str(target)]) == 1
    # fnmatch folds case through os.path.normcase: on a POSIX host `AGY-Work` matches no child, so nothing is refused
    # and nothing is removed; the container is kept either way (asserted below).
    assert pt.purge_children(str(tmp), [spelling]) == (1 if folds or spelling == "agy-work" else 0)
    assert work.exists() and workspace.exists()
    assert pt.purge_children(str(work), ["review-*"]) == 0
    assert not workspace.exists() and work.exists()
    pt.sweep(config=cfg(pt))
    assert work.exists()


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


@pytest.mark.parametrize("name", ["runs", "pm-state", "cli-jobs", "models", "RUNS"])
def test_archive_remove_prunes_an_empty_nested_protected_tree_then_removes_the_worktree(
    pt: ModuleType, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    wt = make_worktree(pt.PROJECT)
    (wt / name / "sub" / "deeper").mkdir(parents=True)  # directories only: an empty `runs` after its evidence moved
    (wt / "src" / "runs").mkdir(parents=True)  # a second empty one on another branch
    assert pt.archive_remove([str(wt)]) == 0 and not wt.exists()
    assert "pruned 2 empty protected-name tree(s)" in capsys.readouterr().out


def test_a_dry_run_lists_the_prune_and_its_verdict_but_removes_nothing(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    wt = make_worktree(pt.PROJECT)
    (wt / "runs" / "sub").mkdir(parents=True)
    assert pt.archive_remove([str(wt)], dry_run=True) == 0
    out = capsys.readouterr().out
    assert "would prune 1 empty protected-name tree(s) (a directory in use or denied still refuses)" in out
    assert "would archive reports and remove" in out
    assert (wt / "runs" / "sub").is_dir()
    (wt / "runs" / "sub" / "evidence.json").write_text("{}", encoding="utf-8")
    assert pt.archive_remove([str(wt)], dry_run=True) == 1
    assert "protected tree" in capsys.readouterr().out and (wt / "runs" / "sub" / "evidence.json").exists()


@pytest.mark.parametrize("name", ["runs", "pm-state", "cli-jobs"])
def test_a_nested_protected_tree_holding_one_file_anywhere_still_refuses(pt: ModuleType, name: str) -> None:
    wt = make_worktree(pt.PROJECT)
    deep = wt / name / "sub" / "deeper"
    deep.mkdir(parents=True)
    (deep / "evidence.json").write_text("{}", encoding="utf-8")
    assert pt.archive_remove([str(wt)]) == 1
    assert pt.purge_children(str(pt.PROJECT / ".tmp" / "worktrees"), ["wt1"]) == 1
    assert (deep / "evidence.json").exists() and (wt / name / "sub").is_dir()


@pytest.mark.parametrize("command", ["purge-children", "purge-older", "sweep"])
def test_only_archive_remove_prunes_empty_protected_trees(pt: ModuleType, command: str) -> None:
    holder = pt.PROJECT / ".tmp" / ("holder" if command == "purge-older" else "")
    scratch = holder / "scratch-9"
    (scratch / "runs" / "sub").mkdir(parents=True)
    age(scratch, 100)
    if command == "purge-children":
        assert pt.purge_children(str(holder), ["scratch-9"]) == 1
    elif command == "purge-older":
        pt.purge_older(str(holder), 24)
    else:
        pt.sweep(config=cfg(pt))
    assert (scratch / "runs" / "sub").is_dir()


def test_an_owned_worktree_is_not_pruned(pt: ModuleType) -> None:
    """The prune runs only after every other check passed: a busy worker's empty `runs` stays as it is."""
    wt = make_worktree(pt.PROJECT)
    (wt / "runs" / "sub").mkdir(parents=True)
    write_registry(pt.PROJECT, busy_worker(wt))
    assert pt.archive_remove([str(wt)]) == 1
    assert (wt / "runs" / "sub").is_dir()


def test_a_kept_container_is_not_pruned(pt: ModuleType) -> None:
    """`.tmp/agy-work` is kept: the prune's precheck refuses it like the strict gate, so nothing inside changes."""
    empty = pt.PROJECT / ".tmp" / "agy-work" / "review-1" / "runs" / "sub"
    empty.mkdir(parents=True)
    assert pt.archive_remove([str(pt.PROJECT / ".tmp" / "agy-work")]) == 1
    assert empty.is_dir()


def test_an_empty_secrets_directory_keeps_its_own_rule(pt: ModuleType) -> None:
    wt = make_worktree(pt.PROJECT)
    (wt / ".secrets" / "README.md").unlink()
    (wt / ".secrets" / "keys").mkdir()  # no file at all, yet `.secrets` is never pruned
    assert pt.archive_remove([str(wt)]) == 1 and (wt / ".secrets" / "keys").is_dir()


def test_an_empty_protected_name_given_as_the_target_is_still_refused(pt: ModuleType) -> None:
    target = pt.PROJECT / ".tmp" / "runs"
    target.mkdir()
    assert pt.archive_remove([str(target)]) == 1
    assert pt.purge_children(str(pt.PROJECT / ".tmp"), ["runs"]) == 1
    assert target.is_dir()


def test_f1_a_file_written_into_the_empty_tree_after_the_last_check_survives(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review F1: the write lands between the fresh per-directory check and the `rmdir` itself (the last moment
    before the act); `rmdir` refuses a directory that holds it, the strict gate then refuses the worktree."""
    wt = make_worktree(pt.PROJECT)
    (wt / "runs" / "sub").mkdir(parents=True)
    real_rmdir = os.rmdir
    late = wt / "runs" / "sub" / "late-evidence.json"

    def racing_rmdir(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not late.exists() and os.path.normcase(str(path)).endswith(os.path.normcase(os.path.join("runs", "sub"))):
            late.write_text("{}", encoding="utf-8")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(pt.os, "rmdir", racing_rmdir)
    assert pt.archive_remove([str(wt)]) == 1
    assert late.read_text(encoding="utf-8") == "{}"
    out = capsys.readouterr().out  # the strict gate judges what the failed prune left, before any archive or removal
    assert "refused, protected tree" in out and "archived" not in out


def test_f1_the_removal_itself_never_deletes_a_file_it_did_not_plan(
    pt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_rmdir_each` only ever calls `os.rmdir`: a plan that names a directory holding a file stops there."""
    tree = pt.PROJECT / ".tmp" / "plan"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "f.txt").write_text("x", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(pt.os, "unlink", lambda *a, **k: calls.append("unlink"))
    monkeypatch.setattr(pt.os, "remove", lambda *a, **k: calls.append("remove"))
    assert not pt._rmdir_each([str(tree / "a"), str(tree)])
    assert (tree / "a" / "f.txt").exists() and calls == []


@pytest.mark.parametrize("spelling", [".secrets", ".SeCrEtS"])
def test_f2_nothing_below_a_nested_secrets_directory_is_listed_or_removed(
    pt: ModuleType, spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = make_worktree(pt.PROJECT)
    hidden = wt / "runs" / spelling
    (hidden / "deeper").mkdir(parents=True)
    (wt / "src" / spelling / "deeper").mkdir(parents=True)
    listed: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path=".", *args, **kwargs):  # type: ignore[no-untyped-def]
        listed.append(os.path.normcase(os.fspath(path)))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(pt.os, "scandir", recording_scandir)
    assert pt.archive_remove([str(wt)]) == 1
    assert hidden.joinpath("deeper").is_dir() and (wt / "src" / spelling / "deeper").is_dir()
    below = [os.path.normcase(str(hidden)), os.path.normcase(str(wt / "src" / spelling))]
    assert not [p for p in listed if any(p == b or p.startswith(b + os.sep) for b in below)]


@pytest.mark.parametrize("where", ["direct", "below a protected tree"])
def test_f3_an_empty_path_pair_directory_is_never_pruned(pt: ModuleType, where: str) -> None:
    wt = make_worktree(pt.PROJECT)
    pair = wt / ".tmp" / "agy-jobs" if where == "direct" else wt / "runs" / ".tmp" / "agy-jobs"
    pair.mkdir(parents=True)
    assert pt.archive_remove([str(wt)]) == 1
    assert pair.is_dir()


def test_f3_an_empty_protected_tree_below_a_path_pair_is_never_pruned(pt: ModuleType) -> None:
    """Review P3-3 (mutant MF): the prune walk never descends into a `PROTECTED_PAIRS` directory."""
    wt = make_worktree(pt.PROJECT)
    inside = [wt / "a" / ".tmp" / "audit" / "runs", wt / "b" / "agy-work" / "_results" / "runs",
              wt / "c" / ".tmp" / "agy-jobs" / "pm-state", wt / "d" / ".tmp" / "tools" / "cli-jobs"]
    for folder in inside:
        (folder / "sub").mkdir(parents=True)
    assert pt.archive_remove([str(wt)]) == 1
    assert all((folder / "sub").is_dir() for folder in inside)


def test_a_refused_worktree_keeps_its_empty_protected_trees(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review P3-2: the prune runs only when the strict gate would accept the worktree with the plan done."""
    wt = make_worktree(pt.PROJECT)
    (wt / "runs" / "sub").mkdir(parents=True)
    (wt / "cli-jobs").mkdir()
    (wt / "pm-state").mkdir()
    (wt / "pm-state" / "ledger.md").write_text("ledger", encoding="utf-8")
    for dry_run in (True, False):
        assert pt.archive_remove([str(wt)], dry_run=dry_run) == 1
        out = capsys.readouterr().out
        assert "prune" not in out and "refused" in out
        assert (wt / "runs" / "sub").is_dir() and (wt / "cli-jobs").is_dir()


def test_a_planned_directory_swapped_for_a_link_before_its_rmdir_survives(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P3-3 (mutant MA): the fresh no-follow check right before each `rmdir` stops at a directory that became
    a link after the plan; the link and what it points at stay, and the worktree is refused."""
    wt = make_worktree(pt.PROJECT)
    planned = wt / "runs" / "sub"
    planned.mkdir(parents=True)
    outside = pt.PROJECT / ".tmp" / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    real_plan = pt._only_directories
    calls = itertools.count(1)

    def plan_then_swap(top):  # type: ignore[no-untyped-def]
        plan = real_plan(top)
        if next(calls) == 2:  # the real prune's plan, after the dry plan that judged the worktree
            planned.rmdir()
            if os.name == "nt":
                junction(planned, outside)
            else:
                os.symlink(outside, planned, target_is_directory=True)
        return plan

    monkeypatch.setattr(pt, "_only_directories", plan_then_swap)
    assert pt.archive_remove([str(wt)]) == 1
    assert os.path.lexists(planned) and (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_f4_a_failed_no_follow_stat_below_a_protected_tree_fails_closed(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = make_worktree(pt.PROJECT)
    child = wt / "runs" / "child"
    child.mkdir(parents=True)
    real_stat = os.stat

    def denying_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(child)):
            raise PermissionError(errno.EACCES, "denied", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(pt.os, "stat", denying_stat)
    assert pt.archive_remove([str(wt)]) == 1
    monkeypatch.undo()
    assert child.is_dir()


def test_only_directories_fails_closed_on_a_link_a_file_or_a_listing_error(
    pt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    (tree / "a").mkdir(parents=True)
    plan = pt._only_directories(str(tree))
    assert plan == [str(tree / "a"), str(tree)]  # deepest first
    (tree / "a" / "f").write_text("x", encoding="utf-8")
    assert pt._only_directories(str(tree)) is None
    (tree / "a" / "f").unlink()
    monkeypatch.setattr(pt, "_real_directory", lambda name, dir_fd=None: os.path.basename(name) != "a")
    assert pt._only_directories(str(tree)) is None  # a link, a junction or a failed stat is never judged a directory
    monkeypatch.undo()

    def scandir_error(path=".", *args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(pt.os, "scandir", scandir_error)
    assert pt._only_directories(str(tree)) is None


def test_a_link_inside_an_empty_protected_tree_blocks_the_prune(pt: ModuleType) -> None:
    """A junction (Windows) or a symbolic link (POSIX) to an empty directory is never judged a directory."""
    wt = make_worktree(pt.PROJECT)
    (wt / "runs").mkdir()
    outside = pt.PROJECT / ".tmp" / "outside"
    outside.mkdir()
    if os.name == "nt":
        junction(wt / "runs" / "link", outside)
    else:
        os.symlink(outside, wt / "runs" / "link", target_is_directory=True)
    assert pt.archive_remove([str(wt)]) == 1
    assert os.path.lexists(wt / "runs" / "link") and outside.is_dir()


_TREE_NAMES = st.sampled_from(["runs", "pm-state", "cli-jobs", "RUNS", ".secrets", ".SeCrEtS", "plain", "sub"])
_GUARDED = {"runs", "pm-state", "cli-jobs", ".secrets"}
_COUNTER = itertools.count()


def _expected_removable(top: Path) -> bool:
    """Independent oracle: every top-most guarded directory below `top` must be a `.secrets` with no entry at all,
    or another guarded name whose tree holds only directories and no `.secrets` anywhere below it."""
    for folder, dirs, _files in os.walk(top):
        for name in list(dirs):
            if name.casefold() not in _GUARDED:
                continue
            dirs.remove(name)  # judged here as a whole tree
            tree = Path(folder) / name
            entries = list(tree.rglob("*"))
            if name.casefold() == ".secrets":
                if entries:
                    return False
            elif any(p.is_file() or p.name.casefold() == ".secrets" for p in entries):
                return False
    return True


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.lists(_TREE_NAMES, min_size=1, max_size=3), st.booleans()), min_size=1, max_size=6))
def test_property_archive_remove_never_removes_a_file_of_a_protected_tree(
    pt: ModuleType, layout: list[tuple[list[str], bool]]
) -> None:
    """Whatever mix of empty and non-empty protected-name and `.secrets` trees a worktree holds, no file inside one
    is removed, and the worktree goes exactly when the independent oracle says nothing guarded is left."""
    wt = make_worktree(pt.PROJECT, f"wt-{next(_COUNTER)}")
    kept: list[Path] = []
    for parts, with_file in layout:
        folder = wt.joinpath("x", *parts)
        if folder.exists() and not folder.is_dir():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        if with_file:
            (folder / "evidence.json").write_text("{}", encoding="utf-8")
            if any(p.casefold() in _GUARDED for p in parts):
                kept.append(folder / "evidence.json")
    removable = _expected_removable(wt / "x")
    result = pt.archive_remove([str(wt)])
    assert all(p.exists() for p in kept)
    assert (result == 0 and not wt.exists()) if removable else (result == 1 and wt.exists())


@pytest.mark.skipif(os.name != "nt", reason="Windows handle open and its last error")
def test_an_access_denied_directory_is_reported_as_such(
    pt: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    top = pt.PROJECT / ".tmp" / "denied"
    top.mkdir()
    win = pt._win()

    def denied(path, access, share):  # type: ignore[no-untyped-def]
        win.last_error = pt._WIN_ERROR_ACCESS_DENIED
        return None

    monkeypatch.setattr(win, "open", denied)
    pt._remove_tree_nt(str(top), str(pt.PROJECT / ".tmp"))
    out = capsys.readouterr().out
    assert out.strip().endswith(": access denied") and "ACL" not in out and top.is_dir()  # no claimed cause (F5)
    for code in (2, 32):  # another open failure names its code and claims no cause (review P3-1)
        monkeypatch.setattr(win, "open", lambda path, access, share, c=code: (setattr(win, "last_error", c), None)[1])
        pt._remove_tree_nt(str(top), str(pt.PROJECT / ".tmp"))
        out = capsys.readouterr().out
        assert out.strip().endswith(f": open failed (winerror {code})") and "a link" not in out
    monkeypatch.setattr(win, "open", lambda path, access, share: (setattr(win, "last_error", 0), None)[1])
    pt._remove_tree_nt(str(top), str(pt.PROJECT / ".tmp"))
    assert "a link or not a directory now" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows handle open and its last error")
def test_win_open_records_the_last_error_and_clears_it_on_success(pt: ModuleType, tmp_path: Path) -> None:
    win = pt._win()
    assert win.open(str(tmp_path / "missing"), pt._WIN_LIST_DIRECTORY, pt._WIN_SHARE_READ_WRITE) is None
    assert win.last_error != 0
    opened = win.open(str(tmp_path), pt._WIN_LIST_DIRECTORY, pt._WIN_SHARE_READ_WRITE)
    assert opened is not None and win.last_error == 0
    win.close(opened[0])


@pytest.mark.skipif(os.name != "nt", reason="Windows handle open and its last error")
def test_f5_win_open_keeps_the_error_of_a_failed_attribute_query(
    pt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attribute query fails after CreateFileW succeeded; CloseHandle then clears the thread's error. The error
    kept is the query's, read before the close."""
    win = pt._win()
    real_close = win.close

    def failing_query(*_args):  # type: ignore[no-untyped-def]
        win.ctypes.set_last_error(pt._WIN_ERROR_ACCESS_DENIED)
        return 0

    def clearing_close(handle):  # type: ignore[no-untyped-def]
        result = real_close(handle)
        win.ctypes.set_last_error(0)
        return result

    monkeypatch.setattr(win, "get_info", failing_query)
    monkeypatch.setattr(win, "close", clearing_close)
    win.last_error = 0
    assert win.open(str(tmp_path), pt._WIN_LIST_DIRECTORY, pt._WIN_SHARE_READ_WRITE) is None
    assert win.last_error == pt._WIN_ERROR_ACCESS_DENIED


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


def test_ownership_follows_the_state_directory_override(
    pt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hooks and cli_worker read AGENT_WORKFLOW_STATE_DIR; purge must see the same registry.
    wt = make_worktree(pt.PROJECT)
    write_registry(pt.PROJECT, {})  # the default location says idle
    moved = tmp_path / "moved-state"
    moved.mkdir()
    (moved / "cli-workers.json").write_text(json.dumps(busy_worker(wt)), encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(moved))
    assert pt._worker_registry() == moved / "cli-workers.json"
    assert pt.main(["archive-remove", str(wt)]) == 1
    assert wt.exists()
    monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR")
    assert pt._worker_registry() == pt.PROJECT / ".tmp" / "pm-state" / "cli-workers.json"
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
        mock.patch.object(pt, "_is_link", lambda path, dir_fd=None: Path(path).name == "pytest-x"),
        mock.patch.object(pt, "_remove_link", lambda path, dir_fd=None: False),
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


def dir_link(link: Path, target: Path) -> None:
    """A directory link without privilege: a junction on Windows, a symlink on POSIX."""
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def linked_target(pt: ModuleType, link: Path) -> tuple[Path, int]:
    """A directory with one file outside the purged tree, linked from `link`; returns it and its mode."""
    target = pt.PROJECT / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("k", encoding="utf-8")
    dir_link(link, target)
    return target, os.stat(target).st_mode


def assert_target_kept(target: Path, mode: int) -> None:
    assert (target / "keep.txt").read_text(encoding="utf-8") == "k"
    assert os.stat(target).st_mode == mode  # not even its mode is changed through the link


def test_remove_link_reports_success_only_when_the_entry_is_really_gone(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S42-ci3 L3: the refusal doubles live in their own context. `monkeypatch.undo()` also undid the fixture's
    # PROJECT patch, so the old assertion looked for the target below the real checkout, where it never was.
    stuck = pt.PROJECT / ".tmp" / "stuck"
    target, mode = linked_target(pt, stuck)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("held open")

    with monkeypatch.context() as patched:
        patched.setattr(pt.os, "unlink", refuse)
        patched.setattr(pt.os, "rmdir", refuse)
        assert pt._remove_link(str(stuck)) is False and os.path.lexists(stuck)
    assert pt._remove_link(str(stuck)) is True and not os.path.lexists(stuck)
    assert_target_kept(target, mode)


@pytest.mark.parametrize("path", ["link", "tree", "tree-refused"])
def test_no_removal_path_reaches_a_link_target(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Every way `_remove` meets a link keeps its target and the target's mode, on POSIX and Windows (S42-ci3):
    the link itself, a tree holding it (rmtree), and a tree whose link entry refuses removal (rmtree's error hook,
    which used to chmod through the link on POSIX)."""
    tree = pt.PROJECT / ".tmp" / "tree"
    tree.mkdir()
    (tree / "file.txt").write_text("x", encoding="utf-8")
    link = tree / "the-link"
    target, mode = linked_target(pt, link)
    chmodded: list[tuple[str, bool]] = []
    real_chmod = os.chmod

    def recording_chmod(entry: str, *args: object, **kwargs: object) -> None:  # test double: records, then acts
        chmodded.append((os.fspath(entry), pt._is_link(entry)))
        real_chmod(entry, *args, **kwargs)

    with monkeypatch.context() as patched:  # its own context: undoing it keeps the fixture's PROJECT patch
        patched.setattr(pt.os, "chmod", recording_chmod)
        if path == "tree-refused":
            for name in ("unlink", "rmdir"):

                def refuse_the_link(entry: str, *args: object, _real=getattr(os, name), **kwargs: object) -> None:
                    if os.path.basename(os.fspath(entry)) == "the-link":  # test double: the link entry is held
                        raise PermissionError("held open")
                    _real(entry, *args, **kwargs)

                patched.setattr(pt.os, name, refuse_the_link)
        gone = pt._remove(str(link if path == "link" else tree))
    assert gone is (path != "tree-refused")
    assert os.path.lexists(link) is (path == "tree-refused")
    assert not [entry for entry, is_link in chmodded if is_link]
    assert_target_kept(target, mode)


@pytest.mark.parametrize("func", [os.open, os.scandir, os.path.islink, os.lstat])
def test_rmtree_error_hook_never_acts_through_a_link_or_crashes(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch, func: object
) -> None:
    """rmtree reports `os.open` and `os.path.islink` ("Cannot call rmtree on a symbolic link") for a directory link
    swapped in during the walk: the hook neither chmods through it nor calls `os.open(path)` (TypeError)."""
    link = pt.PROJECT / ".tmp" / "swapped"
    target, mode = linked_target(pt, link)
    pt._on_error(func, str(link), (OSError, OSError("reported"), None), root=str(pt.PROJECT / ".tmp"))
    assert os.path.lexists(link)
    assert_target_kept(target, mode)


# --- S42-ci3 fix round 1: read-only entries, and directories swapped for links after a check -------------------------


def read_only(path: Path) -> None:
    """Mode 0o444 on POSIX; on Windows `os.chmod` without S_IWRITE sets the read-only attribute."""
    os.chmod(path, 0o444)


def test_a_tree_and_a_file_holding_read_only_entries_are_removed_silently(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review P2-2 and P3-2: the hook exists to clear the Windows read-only attribute. A read-only file at depth, a
    read-only directory and a read-only top-level file all go, on both platforms (POSIX needs no chmod: the parent's
    mode governs unlink), and a successful retry prints nothing."""
    tree = pt.PROJECT / ".tmp" / "tree"
    (tree / "sub").mkdir(parents=True)
    for name in ("ro.txt", "sub/ro.txt"):
        (tree / name).write_text("x", encoding="utf-8")
        read_only(tree / name)
    if os.name == "nt":
        read_only(tree / "sub")  # the read-only attribute on a directory blocks RemoveDirectoryW
    single = pt.PROJECT / ".tmp" / "single.txt"
    single.write_text("x", encoding="utf-8")
    read_only(single)
    assert pt._remove(str(tree)) is True and not tree.exists()
    assert pt._remove(str(single)) is True and not single.exists()
    assert capsys.readouterr().out == ""


def test_the_hook_never_retries_through_a_directory_swapped_for_a_link(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review P2-1, probe2 as a test: rmtree reported `os.unlink` failing for `tree/X/victim.txt`, and `X` has since
    become a link to a directory outside the tree. The hook must not clear the victim's read-only attribute or
    delete it (the first-round hook did both on Windows, and unlinked it on POSIX)."""
    tmp = pt.PROJECT / ".tmp"
    outside = pt.PROJECT / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    read_only(victim)
    mode = os.stat(victim).st_mode
    (tmp / "tree").mkdir()
    dir_link(tmp / "tree" / "X", outside)
    reported = str(tmp / "tree" / "X" / "victim.txt")
    pt._on_error(os.unlink, reported, (OSError, OSError("EACCES"), None), root=str(tmp))
    assert victim.read_text(encoding="utf-8") == "precious" and os.stat(victim).st_mode == mode
    assert f"could not remove {reported}" in capsys.readouterr().out
    os.chmod(victim, 0o644)  # cleanup of the test's own file


def test_a_directory_swapped_for_a_link_above_the_target_is_never_followed(
    pt: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Class audit A3 to A5: between the Gate's check and the delete, a directory above the target becomes a link.
    The delete must refuse instead of removing the tree, file or link of the same name behind it."""
    tmp = pt.PROJECT / ".tmp"
    outside = pt.PROJECT / "outside"
    (outside / "tree").mkdir(parents=True)
    (outside / "tree" / "keep.txt").write_text("k", encoding="utf-8")
    (outside / "file.txt").write_text("f", encoding="utf-8")
    dir_link(tmp / "hop", outside)
    for name in ("tree", "file.txt"):
        assert pt._remove(str(tmp / "hop" / name)) is False
    assert (outside / "tree" / "keep.txt").read_text(encoding="utf-8") == "k"
    assert (outside / "file.txt").read_text(encoding="utf-8") == "f"
    assert capsys.readouterr().out.count("on its way") == 2


class Listing:
    """Labelled double of an `os.scandir` iterator: a list already read, usable as `os.walk` and `_remove_tree_nt`
    use it (context manager, iterator)."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self.entries = iter(entries)

    def __enter__(self) -> Listing:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Listing:
        return self

    def __next__(self) -> os.DirEntry[str]:
        return next(self.entries)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions; POSIX rmtree lists by descriptor")
def test_a_directory_swapped_for_a_junction_during_the_walk_is_never_entered(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Class audit A2, Windows regression: `shutil.rmtree` (`_rmtree_unsafe`, `os.walk` by path) listed `tree`, then
    `tree/X` was swapped for a junction to an outside directory before its own scan; rmtree entered it and unlinked
    the files behind it. The held walk removes the junction only."""
    tmp = pt.PROJECT / ".tmp"
    outside = pt.PROJECT / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("precious", encoding="utf-8")
    tree = tmp / "tree"
    (tree / "X").mkdir(parents=True)
    (tree / "X" / "own.txt").write_text("x", encoding="utf-8")
    real_scandir = os.scandir
    swapped: list[bool] = []

    def scandir_then_swap(path: object = ".") -> Listing:  # labelled double: the swap happens right after listing
        with real_scandir(path) as listing:
            entries = list(listing)
        if os.fspath(path) == str(tree) and not swapped:
            os.rename(tree / "X", tmp / "X-moved")
            junction(tree / "X", outside)
            swapped.append(True)
        return Listing(entries)

    with monkeypatch.context() as patched:
        patched.setattr(pt.os, "scandir", scandir_then_swap)
        assert pt._remove(str(tree)) is True
    assert swapped and (outside / "victim.txt").read_text(encoding="utf-8") == "precious"
    assert (tmp / "X-moved" / "own.txt").exists()  # moved away by the double, not by the purge


@pytest.mark.skipif(os.name != "nt", reason="Windows share modes; POSIX acts by dir_fd instead")
def test_a_held_way_cannot_be_renamed_or_replaced_while_the_act_runs(pt: ModuleType) -> None:
    """The Windows half of `_held_way`: every directory from the root down is held without FILE_SHARE_DELETE, so it
    cannot be renamed (the first step of swapping it for a junction) until the way is released; a junction on the
    way is refused."""
    tmp = pt.PROJECT / ".tmp"
    (tmp / "a" / "b").mkdir(parents=True)
    with pt._held_way(str(tmp), str(tmp / "a" / "b" / "leaf")) as way:
        assert way is not None and way.name == str(tmp / "a" / "b" / "leaf") and way.dir_fd is None
        for held in (tmp / "a", tmp / "a" / "b"):
            with pytest.raises(PermissionError):
                os.rename(held, held.with_name(held.name + "-moved"))
    os.rename(tmp / "a" / "b", tmp / "a" / "b-moved")  # released afterwards
    junction(tmp / "a" / "b", tmp / "a" / "b-moved")
    with pt._held_way(str(tmp), str(tmp / "a" / "b" / "leaf")) as way:
        assert way is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd acts; Windows holds the way instead")
def test_a_posix_held_way_acts_on_the_directory_it_opened(pt: ModuleType) -> None:
    """The POSIX half of `_held_way`: a directory swapped for a symlink after the way was opened is not followed,
    because the act names the entry relative to the descriptor of the directory that was verified."""
    tmp = pt.PROJECT / ".tmp"
    (tmp / "a").mkdir()
    (tmp / "a" / "leaf").write_text("mine", encoding="utf-8")
    outside = pt.PROJECT / "outside"
    outside.mkdir()
    (outside / "leaf").write_text("precious", encoding="utf-8")
    with pt._held_way(str(tmp), str(tmp / "a" / "leaf")) as way:
        assert way is not None and way.name == "leaf"
        os.rename(tmp / "a", tmp / "a-moved")
        (tmp / "a").symlink_to(outside, target_is_directory=True)
        os.unlink(way.name, dir_fd=way.dir_fd)
    assert (outside / "leaf").read_text(encoding="utf-8") == "precious"
    assert not (tmp / "a-moved" / "leaf").exists()
    with pt._held_way(str(tmp), str(tmp / "a" / "leaf")) as way:
        assert way is None  # a symlink on the way is refused when the way is opened


def test_a_held_way_refuses_a_path_that_is_not_strictly_inside_its_root(pt: ModuleType) -> None:
    """Review P3-1, mutant R4: the root itself and any path outside it yield None, so no later caller of `_held_way`
    can act at or above its root (the Gate refuses these first; this guard is the second line)."""
    tmp = pt.PROJECT / ".tmp"
    (pt.PROJECT / "outside" / "a").mkdir(parents=True)
    paths = [str(tmp), str(pt.PROJECT / "outside" / "a" / "leaf"), str(pt.PROJECT / "leaf")]
    if os.name == "nt":  # another drive: `relpath` raises ValueError
        paths.append("Y:\\leaf" if not str(tmp).upper().startswith("Y:") else "Z:\\leaf")
    for path in paths:
        with pt._held_way(str(tmp), path) as way:
            assert way is None, path


def test_a_held_way_refuses_a_root_that_is_a_link(pt: ModuleType) -> None:
    """Review P3-1, mutant R5: the root is verified like every directory below it. A root that is a junction or
    symlink yields None, though every directory behind it is real."""
    real = pt.PROJECT / "real-root"
    (real / "a").mkdir(parents=True)
    dir_link(pt.PROJECT / "linked-root", real)
    with pt._held_way(str(pt.PROJECT / "linked-root"), str(pt.PROJECT / "linked-root" / "a" / "leaf")) as way:
        assert way is None
    with pt._held_way(str(real), str(real / "a" / "leaf")) as way:
        assert way is not None


class PosixOs:
    """Labelled double: the `os` of `purge_tmp` as on a POSIX host, over the real tree, so `_hold_posix` runs on
    every platform (fix round 2, class audit X8). `open` takes the root by full path and every other directory as
    one name relative to a held descriptor. It refuses a link or junction with ELOOP, as open(2) does under
    O_NOFOLLOW, and asserts both no-follow flags. `mkdir` works relative to a descriptor. Everything else is the
    real `os`. The flag values are Linux x86-64 (asm-generic/fcntl.h)."""

    name = "posix"
    O_DIRECTORY, O_NOFOLLOW = 0o200000, 0o400000

    def __init__(self) -> None:
        self.held: dict[int, str] = {}
        self.opens: list[tuple[str, int | None]] = []
        self.next = 1 << 20

    def __getattr__(self, attribute: str) -> object:
        return getattr(os, attribute)

    def _path(self, name: str, dir_fd: int | None) -> str:
        if dir_fd is None:
            return name
        assert os.sep not in name and "/" not in name, f"not one component: {name!r}"
        return os.path.join(self.held[dir_fd], name)

    def open(self, name: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        path = self._path(name, dir_fd)
        self.opens.append((name, dir_fd))
        assert flags & self.O_DIRECTORY and flags & self.O_NOFOLLOW, "a directory opened without no-follow"
        info = os.lstat(path)  # FileNotFoundError when it is gone
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:  # link or junction
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", path)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)
        self.next += 1
        self.held[self.next] = path
        return self.next

    def close(self, descriptor: int) -> None:
        del self.held[descriptor]

    def mkdir(self, name: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        os.mkdir(self._path(name, dir_fd))


def test_the_posix_held_way_opens_each_directory_by_descriptor_on_any_platform(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Class audit X8: `_hold_posix` opens the root without following a link, then each directory relative to the
    one before, creates missing ones relative to the held parent, refuses a link on the way without creating
    anything behind it, and closes every descriptor."""
    tmp = pt.PROJECT / ".tmp"
    (tmp / "a").mkdir()
    outside = pt.PROJECT / "outside"
    outside.mkdir()
    dir_link(tmp / "hop", outside)
    double = PosixOs()
    monkeypatch.setattr(pt, "os", double)
    monkeypatch.setattr(pt, "shutil", SimpleNamespace(rmtree=SimpleNamespace(avoids_symlink_attacks=True)))
    with pt._held_way(str(tmp), str(tmp / "a" / "b" / "leaf"), create=True) as way:
        assert way is not None and way.name == "leaf" and double.held[way.dir_fd] == str(tmp / "a" / "b")
        assert double.opens == [(str(tmp), None), ("a", double.opens[1][1]), ("b", double.opens[2][1])]
        assert None not in (double.opens[1][1], double.opens[2][1])
    assert (tmp / "a" / "b").is_dir() and double.held == {}
    for root, path in ((tmp, tmp / "hop" / "x" / "leaf"), (tmp / "hop", tmp / "hop" / "x" / "leaf")):
        with pt._held_way(str(root), str(path), create=True) as way:
            assert way is None
    assert list(outside.iterdir()) == [] and double.held == {}


def test_archiving_replaces_an_older_read_only_copy(pt: ModuleType) -> None:
    """Review P3-3: an archive from an older run can hold a read-only copy (the old `copy2` kept the source's mode).
    Re-archiving a worktree of the same name replaces it; fix round 1 refused the worktree on Windows."""
    wt = make_worktree(pt.PROJECT)
    archive = pt.ARCHIVE / "wt1"
    archive.mkdir(parents=True)
    (archive / "report.md").write_text("older run", encoding="utf-8")
    read_only(archive / "report.md")
    assert pt.archive_remove([str(wt)]) == 0 and not wt.exists()
    assert (archive / "report.md").read_text(encoding="utf-8") == "report"


@pytest.mark.parametrize("where", ["ancestor", "leaf-directory", "leaf-file", "leaf-hardlink"])
def test_archiving_never_writes_through_a_link(pt: ModuleType, where: str) -> None:
    """Class audit A6: a link already under `pm-archive`, on the way to a report's copy or at its name. A link on the
    way refuses the worktree (nothing is written behind it, nothing is deleted). A link at the name is replaced as a
    name (POSIX rename(2) replaces a symlink itself), except a Windows junction, which no file can replace: refused.
    The first-round `shutil.copy2` wrote through all three. A hard link aliases an outside file the way a file
    symlink does, and needs no privilege on Windows, so `leaf-hardlink` runs where `leaf-file` is skipped (review
    mutant R6: a copy written straight to the name changes the outside file)."""
    wt = make_worktree(pt.PROJECT)
    (wt / ".tmp" / "sub").mkdir()
    (wt / ".tmp" / "sub" / "r.md").write_text("sub report", encoding="utf-8")
    outside = pt.PROJECT / "outside"
    outside.mkdir()
    archive = pt.ARCHIVE / "wt1"
    archive.mkdir(parents=True)
    if where == "ancestor":
        dir_link(archive / "sub", outside)
    elif where == "leaf-directory":
        dir_link(archive / "report.md", outside)
    elif where == "leaf-hardlink":
        (outside / "report.md").write_text("precious", encoding="utf-8")
        os.link(outside / "report.md", archive / "report.md")
    else:
        (outside / "report.md").write_text("precious", encoding="utf-8")
        try:
            os.symlink(outside / "report.md", archive / "report.md")
        except OSError as error:  # Windows without the symlink privilege (WinError 1314)
            pytest.skip(f"file symlink unavailable: {error}")
    refused = pt.archive_remove([str(wt)])
    leaf_file = where in ("leaf-file", "leaf-hardlink")
    if leaf_file or (where == "leaf-directory" and os.name != "nt"):
        assert refused == 0 and not wt.exists()
        assert not pt._is_link(archive / "report.md")
        assert (archive / "report.md").read_text(encoding="utf-8") == "report"
    else:
        assert refused == 1 and (wt / ".tmp" / "report.md").exists()
    expected = ["report.md"] if leaf_file else []
    assert sorted(p.name for p in outside.iterdir()) == expected
    if expected:
        assert (outside / "report.md").read_text(encoding="utf-8") == "precious"


def test_archiving_never_reads_through_a_directory_swapped_for_a_link(
    pt: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Class audit A7: a report directory listed by `_walk` and swapped for a link to a credentials-like tree before
    the copy. The copy must not read the file behind the link (the module never reads a `.secrets` tree); the
    worktree is refused instead."""
    wt = make_worktree(pt.PROJECT)
    (wt / ".tmp" / "notes").mkdir()
    (wt / ".tmp" / "notes" / "n.md").write_text("note", encoding="utf-8")
    secrets = pt.PROJECT / "outside-secrets"
    secrets.mkdir()
    (secrets / "n.md").write_text("credential", encoding="utf-8")
    real_walk = pt._walk
    swapped: list[bool] = []

    def walk_then_swap(top: object) -> Iterator[tuple[str, list[str], list[str]]]:  # labelled double: the race
        listing = list(real_walk(top))
        if Path(os.fspath(top)) == wt / ".tmp" and not swapped:  # the archive's walk, not the Gate's
            os.rename(wt / ".tmp" / "notes", wt / ".tmp" / "notes-moved")
            dir_link(wt / ".tmp" / "notes", secrets)
            swapped.append(True)
        yield from listing

    with monkeypatch.context() as patched:
        patched.setattr(pt, "_walk", walk_then_swap)
        assert pt.archive_remove([str(wt)]) == 1
    assert swapped and "report not archived" in capsys.readouterr().out and wt.exists()
    copies = [p for p in pt.ARCHIVE.rglob("n.md")] if pt.ARCHIVE.exists() else []
    assert all(p.read_text(encoding="utf-8") != "credential" for p in copies)


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


SWEEP_NAMES = ["plain-1", "plain-2", "T-owned", "pm-state", "runs", "cli-jobs", "models", "wt-busy"]


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    old=st.sets(st.sampled_from(SWEEP_NAMES)),
    archived=st.sets(st.sampled_from(SWEEP_NAMES)),
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
                (path / "runs" / "evidence.json").write_text("{}", encoding="utf-8")  # an empty one is not protected
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
