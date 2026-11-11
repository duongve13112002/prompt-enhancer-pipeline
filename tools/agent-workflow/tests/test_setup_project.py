"""Tests for setup_project.py: the real checkout passes, each check catches its defect, apply repairs and is idempotent."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_config  # noqa: E402
import setup_project as sp  # noqa: E402

ROOT = sp.ROOT


def _fails(results: list[sp.Result]) -> list[str]:
    return [f"{r.area}: {r.message}" for r in results if r.level == "FAIL"]


def test_the_real_checkout_has_no_failures() -> None:
    """Guards the repository itself: no dead links, no previous-project tokens, bindings and skills in sync."""
    results = sp.run_checks(ROOT, run_cli=False)
    if not (ROOT / sp.SETTINGS_LOCAL).is_file():
        results = [r for r in results if r.area != "settings"]
    skills_unsynced = [r for r in results if r.area == "skills" and "out of sync" in r.message]
    if skills_unsynced and not (ROOT / sp.SKILLS_CLAUDE).is_dir():
        pytest.skip("this checkout has not run setup_project.py apply")
    assert _fails(results) == []


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    """A disposable copy of the repository's text (no venv, Git, scratch or local settings)."""
    target = tmp_path / "proj"
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(".venv*", ".git", ".tmp", "__pycache__", ".pytest_cache", "settings.local.json"),
        symlinks=True,
    )
    return target


def test_apply_binds_a_copy_and_is_idempotent(copy: Path) -> None:
    data = json.loads((copy / project_config.REL_PATH).read_text(encoding="utf-8"))
    data["git"]["integration_branch"] = "batch/next"
    (copy / project_config.REL_PATH).write_text(json.dumps(data), encoding="utf-8")
    shutil.rmtree(copy / sp.SKILLS_CLAUDE, ignore_errors=True)

    before = _fails(sp.run_checks(copy, run_cli=False))
    assert any("batch_branch" in f for f in before) and any("out of sync" in f for f in before)

    done, _ = sp.apply(copy, force_settings=False)
    assert any("batch_branch = batch/next" in d for d in done)
    assert any(d.startswith("rendered") for d in done)
    settings = (copy / sp.SETTINGS_LOCAL).read_text(encoding="utf-8")
    assert copy.as_posix() in settings and "<PROJECT_ROOT>" not in settings and "_comment" not in settings
    assert _fails(sp.run_checks(copy, run_cli=False)) == []

    again, skipped = sp.apply(copy, force_settings=False)
    assert again == [] and not any("differs" in s for s in skipped)


def test_settings_from_another_checkout_are_refused_until_forced(copy: Path) -> None:
    sp.apply(copy, force_settings=False)
    path = copy / sp.SETTINGS_LOCAL
    path.write_text(path.read_text(encoding="utf-8").replace(copy.as_posix(), "D:/old/other-project"), encoding="utf-8")
    assert any("another checkout" in f for f in _fails(sp.check_claude_settings(copy, project_config.load(copy))))
    _, skipped = sp.apply(copy, force_settings=False)
    assert any("--force-settings" in s for s in skipped)
    sp.apply(copy, force_settings=True)
    assert _fails(sp.check_claude_settings(copy, project_config.load(copy))) == []


def test_previous_project_tokens_are_found_outside_excluded_paths(copy: Path) -> None:
    cfg = project_config.load(copy)
    token = cfg.foreign_bindings[0]
    (copy / "docs" / "leftover.md").write_text(f"see {token} here\n", encoding="utf-8")
    (copy / "tasks" / "archive" / "old.md").write_text(f"{token}\n", encoding="utf-8")
    files = [copy / "docs" / "leftover.md", copy / "tasks" / "archive" / "old.md"]
    failures = _fails(sp.check_foreign(copy, cfg, files))
    assert len(failures) == 1 and "docs/leftover.md:1" in failures[0] and "archive" not in failures[0]


def test_dead_links_are_found_but_fenced_and_external_links_are_not(tmp_path: Path) -> None:
    (tmp_path / "ok.md").write_text("x", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "[a](ok.md) [b](missing.md#x) [c](https://example.invalid) [d](#anchor)\n```\n[e](gone.md)\n```\n",
        encoding="utf-8",
    )
    failures = _fails(sp.check_links(tmp_path, [page]))
    assert len(failures) == 1 and "missing.md" in failures[0] and "gone.md" not in failures[0]


def test_instruction_files_must_import_and_carry_the_marker_blocks(copy: Path) -> None:
    (copy / "CLAUDE.md").write_text("# rules copied here\n", encoding="utf-8")
    assert any("@AGENTS.md" in f for f in _fails(sp.check_instructions(copy)))
    text = (copy / "AGENTS.md").read_text(encoding="utf-8")
    (copy / "AGENTS.md").write_text(text.replace("<!-- AGENT-WORKER:END -->", ""), encoding="utf-8")
    assert any("marker" in f for f in _fails(sp.check_instructions(copy)))


def test_a_skill_without_matching_frontmatter_fails(copy: Path) -> None:
    skill = copy / sp.SKILLS_SRC / "broken"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: other\n---\nbody\n", encoding="utf-8")
    assert any("broken: frontmatter" in f for f in _fails(sp.check_skills(copy, project_config.load(copy))))


@pytest.mark.parametrize(
    ("root", "expected"),
    [("D:/Work/proj", "//d/Work/proj"), ("/home/u/proj", "//home/u/proj")],
)
def test_permission_root_form(root: str, expected: str) -> None:
    assert sp.permission_root(Path(root)) == expected
