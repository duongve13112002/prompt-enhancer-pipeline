"""Tests for setup_project.py: the real checkout passes, each check catches its defect, apply repairs and is idempotent."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_config  # noqa: E402
import setup_project as sp  # noqa: E402

ROOT = sp.ROOT


def _fails(results: list[sp.Result]) -> list[str]:
    return [f"{r.area}: {r.message}" for r in results if r.level == "FAIL"]


def test_the_real_checkout_has_no_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the repository itself: no dead links, no previous-project tokens, bindings and skills in sync."""
    # check_clis yields only OK and WARN (setup_project.py check_clis) and reads this machine's local Codex profile
    # under the live `.tmp/pm-state`; the test asserts FAIL rows only, so that machine state stays unread.
    monkeypatch.setattr(sp, "check_clis", lambda root, cfg, run_cli: [])
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


def test_the_checkouts_own_path_is_not_a_previous_project_token(tmp_path: Path) -> None:
    """A foreign token that is a prefix of this checkout's path (e.g. the old project's parent folder) matches only
    text outside this checkout's own path; the PM found this when the test temp folder lay inside the project."""
    root = tmp_path / "parent" / "this-project"
    root.mkdir(parents=True)
    cfg = project_config.load(ROOT)
    token = (tmp_path / "parent").as_posix()
    cfg = dataclasses.replace(cfg, foreign_bindings=(token,), foreign_scan_exclude=())
    own = root / "own.json"
    own.write_text(f'{{"a": "{root.as_posix()}/tools/x.py", "b": "{root}"}}\n', encoding="utf-8")
    assert _fails(sp.check_foreign(root, cfg, [own])) == []
    old = root / "old.md"
    old.write_text(f"see {token}/old-project/README.md\n", encoding="utf-8")
    failures = _fails(sp.check_foreign(root, cfg, [own, old]))
    assert len(failures) == 1 and "old.md:1" in failures[0] and "own.json" not in failures[0]


@pytest.mark.parametrize("suffix", ["-old/tools/x.py", "2/tools/x.py", ".bak/x", "_copy"])
def test_a_sibling_of_the_checkout_still_shows_the_previous_project_token(tmp_path: Path, suffix: str) -> None:
    """Only this checkout's whole path is exempt: `<root>-old` is another folder under the old project's parent, so
    the token in it is reported (retest 6 F1: a plain prefix removal hid it)."""
    root = tmp_path / "parent" / "this-project"
    root.mkdir(parents=True)
    cfg = project_config.load(ROOT)
    token = (tmp_path / "parent").as_posix()
    cfg = dataclasses.replace(cfg, foreign_bindings=(token,), foreign_scan_exclude=())
    page = root / "page.md"
    page.write_text(f"see {root.as_posix()}{suffix} and {root.as_posix()}/kept.md\n", encoding="utf-8")
    failures = _fails(sp.check_foreign(root, cfg, [page]))
    assert len(failures) == 1 and "page.md:1" in failures[0]


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


# ---- agy runner row (S42) -------------------------------------------------------------------------------------------
# Labelled test doubles: the `read` callables below and `fake_runner_read` stand in for the one read-only PowerShell
# call; `reader_with_doubles` runs the real reader script and agy-lib.ps1 with only the scheduler and ACL reads
# replaced. No real scheduled task or ACL is read by these tests.


@pytest.fixture(autouse=True)
def fake_runner_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps every run_checks call in this file off the real scheduled tasks (labelled double)."""
    monkeypatch.setattr(sp, "_read_runner_state", lambda root: {"task_name": "AgyRunner-x-00000000", "tasks": []})


NAME = "AgyRunner-proj-0badc0de"
PRESENT = {"account": "agy-runner", "decision": "present"}


def _task(root: str | None, own: bool = True, here: bool | None = True, name: str = NAME,
          error: str = "") -> dict[str, object]:
    return {"name": name, "own": own, "state": "Ready", "user": "agy-runner", "last_run": "2026-11-06 18:42:40",
            "project_root": root, "bound_here": here, "bound_error": error}


def _state(tasks: list[dict[str, object]] | None = None, jobs: bool = True, work: bool = True,
           rules: list[dict[str, object]] | None = None, acl_error: str = "") -> dict[str, object]:
    return {"task_name": NAME, "tasks": [_task("D:\\proj")] if tasks is None else tasks, "jobs_exists": jobs,
            "work_exists": work, "work_rules": [PRESENT] if rules is None else rules, "acl_error": acl_error}


def _runner(root: Path, state: dict[str, object] | BaseException, windows: bool = True,
            read: object = None) -> list[sp.Result]:
    def fixed(arg: Path) -> dict[str, object]:
        assert arg == root
        if isinstance(state, BaseException):
            raise state
        return state

    cfg = project_config.load(ROOT)
    agy = dataclasses.replace(cfg.agents["antigravity"], enabled=True)
    cfg = dataclasses.replace(cfg, agents={**cfg.agents, "antigravity": agy})
    return sp.check_runner(root, cfg, read=read or fixed, is_windows=windows)  # type: ignore[arg-type]


def _warns(out: list[sp.Result]) -> list[str]:
    return [r.message for r in out if r.level == "WARN"]


def test_runner_row_is_not_applicable_off_windows(tmp_path: Path) -> None:
    out = _runner(tmp_path, _state(), windows=False)
    assert [(r.level, r.message) for r in out] == [("OK", "agy runner: not applicable (Windows only)")]


def test_runner_rows_ok_when_every_item_is_present(tmp_path: Path) -> None:
    out = _runner(tmp_path, _state())
    assert [r.level for r in out] == ["OK", "OK", "OK"], out
    assert f"task {NAME} bound to this root, state Ready, last run 2026-11-06 18:42:40" in out[0].message
    assert "applying to the folder and its children" in out[2].message


def test_runner_row_reports_the_legacy_task_bound_to_another_root(tmp_path: Path) -> None:
    other = "D:\\Work\\another-project"
    deny = "blocked: explicit Deny FullControl (ContainerInherit, ObjectInherit; None) for HOST\\agy-runner"
    state = _state(tasks=[_task(other, own=False, here=False, name="AgyRunner")], jobs=False,
                   rules=[{"account": "agy-runner", "decision": deny}])
    out = _runner(tmp_path, state)
    assert {r.level for r in out} == {"WARN"} and len(out) == 3
    assert f"task {NAME} for this project is missing (AgyRunner is bound to {other})" in out[0].message
    assert "setup.ps1" in out[0].fix and ".tmp/agy-jobs missing" in out[1].message
    assert "explicit Deny FullControl" in out[2].message and "never removes" in out[2].fix


@pytest.mark.parametrize(
    ("state", "expect"),
    [
        (_state(tasks=[]), f"task {NAME} for this project is missing (no other runner task)"),
        (_state(jobs=False), ".tmp/agy-jobs missing"),
        (_state(work=False, rules=[]), ".tmp/agy-work missing"),
        (_state(rules=[{"account": "svc-agy", "decision": "apply"}]),
         "no explicit Allow Modify for svc-agy on .tmp/agy-work that applies to the folder and its children"),
        (_state(rules=[]), "not checked: no runner task names the account"),
        (_state(tasks=[_task(None)]), "does not have the action layout setup.ps1 writes"),
        (_state(tasks=[_task("D:\\elsewhere", here=False)]), "is bound to D:\\elsewhere, not to this root"),
        (_state(tasks=[_task("D:\\gone", here=None, error="labelled read error")]),
         "could not be resolved (labelled read error)"),
        (_state(acl_error="labelled access denied"), "ACL of .tmp/agy-work unreadable: labelled access denied"),
        (_state(rules=[{"account": "agy-runner", "decision": "blocked: explicit Deny Write (None; None) for H\\agy-runner"}]),
         "carries an explicit Deny Write"),
    ],
)
def test_each_absent_item_gets_its_own_warn(tmp_path: Path, state: dict[str, object], expect: str) -> None:
    """Review F5: every required item that is absent is named on its own row, never folded into an OK."""
    out = _runner(tmp_path, state)
    warns = _warns(out)
    assert len(warns) == 1 and expect in warns[0], out
    assert all(r.level != "FAIL" for r in out)
    assert all(r.fix for r in out if r.level == "WARN" and "unreadable" not in r.message)


def test_an_unreported_folder_state_is_not_present(tmp_path: Path) -> None:
    """A reader state without the folder facts warns for each folder; unknown never reads as present."""
    state = {k: v for k, v in _state().items() if k not in ("jobs_exists", "work_exists")}
    warns = _warns(_runner(tmp_path, state))
    assert any(".tmp/agy-jobs missing" in w for w in warns) and any(".tmp/agy-work missing" in w for w in warns)


@pytest.mark.parametrize(
    "failure",
    [OSError("labelled access denied"), subprocess.TimeoutExpired("labelled read double", 1), ValueError("bad json")],
)
def test_runner_read_failure_is_one_warn(tmp_path: Path, failure: BaseException) -> None:
    out = _runner(tmp_path, failure)
    assert [(r.level, "could not read the runner state" in r.message) for r in out] == [("WARN", True)]
    no_name = _runner(tmp_path, {**_state(), "task_name": None})
    assert [(r.level, "gave no task name" in r.message) for r in no_name] == [("WARN", True)]


def test_python_holds_no_twin_of_the_runner_derivations() -> None:
    """Review F2: the name, parse, path compare and rule decision come from agy-lib.ps1 only."""
    source = (ROOT / "tools" / "agent-workflow" / "setup_project.py").read_text(encoding="utf-8")
    for gone in ("hashlib", "sha256", "RUNNER_ROOT_RE", "RUNNER_TASK_PREFIX", "FS_RIGHTS_MODIFY", "realpath",
                 ".lower() == name", "def _same_path"):
        assert gone not in source, gone
    assert ". $env:AGY_CHECK_LIB" in sp.RUNNER_READ_PS and "Get-AgyRunnerTaskName" in sp.RUNNER_READ_PS


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@pytest.fixture
def reader_with_doubles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """The real reader script and agy-lib.ps1, with Get-ScheduledTask(Info) and Get-Acl replaced (labelled doubles)."""
    if sys.platform != "win32" or shutil.which("powershell") is None:
        pytest.skip("Windows PowerShell 5.1 tooling")
    real_lib = sp.AGY_LIB  # captured once: after the first patch AGY_LIB names the doubles file itself
    count = iter(range(100))

    def make(root: Path, scheduler: str, acl: str = "") -> object:
        lib = tmp_path / f"lib-with-doubles-{next(count)}.ps1"
        lib.write_text(
            f". {_ps_quote(str(real_lib))}\n"
            f"function Get-ScheduledTask {{ [CmdletBinding()] param([string]$TaskName) {scheduler} }}\n"
            "function Get-ScheduledTaskInfo { [CmdletBinding()] param([string]$TaskName, [string]$TaskPath) "
            "[pscustomobject]@{ LastRunTime = [datetime]'2026-11-15 10:00:00' } }\n"
            f"function Get-Acl {{ [CmdletBinding()] param([string]$LiteralPath) {acl} }}\n",
            encoding="utf-8-sig",
        )
        monkeypatch.setattr(sp, "AGY_LIB", lib)
        # A freshly written script can stall its first PowerShell load (a scan) past the 20 s CLI bound; the
        # timeout path itself is covered by test_runner_read_failure_is_one_warn.
        monkeypatch.setattr(sp, "CLI_TIMEOUT_SECONDS", 120)
        return lambda arg: sp._powershell_json(arg, "full")

    return make


def _ps_task(name: str, root: str, user: str = "svc-agy", args: str = "") -> str:
    args = args or f'-NoProfile -ExecutionPolicy Bypass -File "C:\\Tools\\agy\\agy-job.ps1" -ProjectRoot "{root}"'
    return ("[pscustomobject]@{ TaskName = " + _ps_quote(name) + "; TaskPath = '\\'; State = 'Ready'; "
            "Principal = [pscustomobject]@{ UserId = " + _ps_quote(user) + " }; "
            "Actions = @([pscustomobject]@{ Execute = 'powershell.exe'; Arguments = " + _ps_quote(args) + " }) }")


def _ps_allow(identity: str = "HOST\\svc-agy", propagation: str = "None") -> str:
    return ("[pscustomobject]@{ Access = @([Security.AccessControl.FileSystemAccessRule]::new(" + _ps_quote(identity)
            + ", 'Modify', 'ContainerInherit, ObjectInherit', " + _ps_quote(propagation) + ", 'Allow')) }")


def test_real_reader_with_labelled_doubles_covers_each_state(tmp_path: Path, reader_with_doubles: object) -> None:
    make = reader_with_doubles
    root = tmp_path / "proj"
    (root / ".tmp" / "agy-jobs").mkdir(parents=True)
    name = sp.runner_task_name(root)
    # The root written in upper case: the binding compare comes from Test-AgySamePath, not from Python.
    bound = _ps_task(name, str(root).upper())
    healthy = _runner(root, {}, read=make(root, bound, _ps_allow()))  # type: ignore[operator]
    # Review F5 reproduction: task and jobs present, work folder missing -> its own WARN.
    assert [(r.level, r.message.split(": ", 1)[1][:22]) for r in healthy] == [
        ("OK", f"task {name}"[:22]), ("OK", ".tmp/agy-jobs exists"), ("WARN", ".tmp/agy-work missing ")]
    (root / ".tmp" / "agy-work").mkdir()
    healthy = _runner(root, {}, read=make(root, bound, _ps_allow()))  # type: ignore[operator]
    assert [r.level for r in healthy] == ["OK", "OK", "OK"], healthy
    assert "explicit Allow Modify for svc-agy" in healthy[2].message
    inherit_only = _runner(root, {}, read=make(root, bound, _ps_allow(propagation="InheritOnly")))  # type: ignore[operator]
    assert _warns(inherit_only) == [f"agy runner: no explicit Allow Modify for svc-agy on .tmp/agy-work that applies "
                                    "to the folder and its children"]
    elsewhere = _runner(root, {}, read=make(root, _ps_task(name, str(tmp_path / "other")), _ps_allow()))  # type: ignore[operator]
    assert _warns(elsewhere) == [f"agy runner: task {name} is bound to {tmp_path / 'other'}, not to this root"]
    # Fix round 2 (retest RF1, RF2): a bare-wrapper task is still listed as a runner task, with its project unknown,
    # and this project's task with one combined host-option argument is not a binding.
    bare = _ps_task("Bare", "", args='-File agy-job.ps1 -ProjectRoot "E:\\Other"')
    listed = _runner(root, {}, read=make(root, bare, _ps_allow()))  # type: ignore[operator]
    assert listed[0].message == (f"agy runner: task {name} for this project is missing "
                                 "(Bare is bound to <not the action layout setup.ps1 writes>)")
    combined = _ps_task(name, "", args=f'"-ExecutionPolicy Bypass" -File "C:\\Tools\\agy\\agy-job.ps1" -ProjectRoot "{root}"')
    refused = _runner(root, {}, read=make(root, combined, _ps_allow()))  # type: ignore[operator]
    assert refused[0].message == f"agy runner: task {name} does not have the action layout setup.ps1 writes, so its project is unknown"
    acl_fails = _runner(root, {}, read=make(root, bound, "throw 'labelled ACL read double'"))  # type: ignore[operator]
    assert _warns(acl_fails) == ["agy runner: ACL of .tmp/agy-work unreadable: labelled ACL read double"]
    # Review F1: a failed enumeration is a read error, never "no runner task".
    failed = _runner(root, {}, read=make(root, "Write-Error 'labelled scheduler-unavailable double'"))  # type: ignore[operator]
    assert len(failed) == 1 and "could not read the runner state" in failed[0].message
    assert "labelled scheduler-unavailable double" in failed[0].message
