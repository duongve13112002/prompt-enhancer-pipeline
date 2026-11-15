"""agy launch helpers (scripts/agy-runner/agy-lib.ps1) on fake inputs only: no agy, no scheduled task."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[1]
DRIVER = Path(__file__).with_name("agy_lib_driver.ps1")
POWERSHELL = shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell 5.1 tooling"
)


def run(tmp_path: Path, **spec: Any) -> dict[str, Any]:
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    assert POWERSHELL is not None
    done = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(DRIVER),
            "-InputJson",
            str(spec_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",  # the driver pins its output to UTF-8; the locale code page would garble `D:\Dự án`
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def violations(tmp_path: Path, text: str, depth: int = 0) -> list[str]:
    out = run(tmp_path, case="lint", text=text, depth=depth)
    assert out["ok"]
    return out["value"] or []


@pytest.mark.parametrize(
    "line",
    [
        "Read .tmp/cli-jobs/sonnet-x/001.result.md first.",
        "see .tmp\\worktrees\\r3\\src\\a.py",
        "open D:/Project_github/x/file.py",
        "open c:\\Users\\x\\file.py",
        "read /etc/passwd now",
        "read (/docs/x.md)",
        "read /home/user/a and /Users/x/b and /tmp/c/d and /mnt/e/f",
        "use the .venv/Scripts/python.exe",
        "read ../.secrets/key",
        "cat ~/.gemini/settings.json",
        "see \\\\server\\share\\a",
        "write .tmp/agy-work.bak/x and .tmp/agy-workshop/x",
    ],
)
def test_lint_flags_unreadable_paths(tmp_path: Path, line: str) -> None:
    found = violations(tmp_path, f"ok line\n{line}\n")
    assert found
    assert all(item.startswith("prompt:2:") for item in found)


@pytest.mark.parametrize(
    "line",
    [
        "Review notes/delta.diff and write .tmp/agy-work/ws1/REVIEW.md.",
        "Read src/pkg/mod.py and tests/test_mod.py; and/or tasks/X.md.",
        "never read or mention .secrets; use the project .venv python only",
        "see https://example.com/a/b and 2026/10/22 about 1/2 of TCP/IP",
        "takes ~5 minutes",
        "the .tmp folder is ignored",
        "",
    ],
)
def test_lint_allows_workspace_relative_text(tmp_path: Path, line: str) -> None:
    assert violations(tmp_path, line) == []


@pytest.mark.parametrize(
    "line",
    [
        "Stay under .tmp/agy-work.",
        "Write only below (.tmp/agy-work), `.tmp/agy-work`, or .tmp/agy-work: done",
        "POST /knowledge-v1/concept-pool and GET /service-info; /ready, /health/live, /v1/drain",
        "keyed `/ready` answers 401 with `/v1/service-info` at `/metrics`",
        "never write to `/tmp` or `$env:TEMP`; `%TEMP%` is also outside",
        "git diff Base..Head and a ... b and 1..5 and wait.. then go",
        "see https://example.com/a/../b",
    ],
)
def test_lint_allows_routes_trailing_period_bare_names_and_non_paths(
    tmp_path: Path, line: str
) -> None:
    assert violations(tmp_path, line) == []


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("read ../x", "parent-directory path"),
        ("read ..\\x", "parent-directory path"),
        ("read notes/../../x", "parent-directory path"),
        ("read ..\\..\\x", "parent-directory path"),
        ("write .tmp/agy-work/../x", "parent-directory path"),
        ("write .tmp\\agy-work\\ws\\..\\..\\x", "parent-directory path"),
        ("see [doc](../docs/a.md)", "parent-directory path"),
        ("read %USERPROFILE%\\x", "environment-variable path"),
        ("read %temp%/x", "environment-variable path"),
        ("read $env:TEMP\\x", "environment-variable path"),
        ("read $ENV:Home/x", "environment-variable path"),
        ("read ${env:TEMP}", "environment-variable path"),
    ],
)
def test_lint_flags_parent_traversal_and_environment_variable_paths(
    tmp_path: Path, line: str, rule: str
) -> None:
    found = violations(tmp_path, line)
    assert any(f": {rule}:" in item for item in found), (line, found)


def test_markdown_link_to_repo_docs_is_allowed_only_inside_the_workspace_depth(
    tmp_path: Path,
) -> None:
    ok = "[note](../docs/note/a.md#top), ([doc](../tools/b.md)) and [x](https://e.com/a/../b)"
    assert violations(tmp_path, ok, depth=1) == []
    for bad in (
        "[x](../../docs/a.md)",
        "[x](../docs/../../a.md)",
        "[x](notes/../../a.md)",
        "[x](./../a.md)",
        "plain ../docs/a.md and `../docs/a.md`",
    ):
        assert violations(tmp_path, bad, depth=1), bad
    assert violations(tmp_path, "[x](../docs/a.md)", depth=0)


def test_lint_scans_prompt_and_every_workspace_note(tmp_path: Path) -> None:
    notes = tmp_path / ".tmp" / "agy-work" / "ws" / "notes"
    notes.mkdir(parents=True)
    (notes / "good.md").write_text("fine, see [doc](../docs/a.md#x)\n", encoding="utf-8")
    (notes / "fix.md").write_text("line one\nsee .tmp/cli-jobs/a/001.result.md\n", encoding="utf-8")
    (notes / "delta.diff").write_text("D:/ignored/not-markdown\n", encoding="utf-8")
    clean = run(
        tmp_path, case="lint_ws", root=str(tmp_path), workspace="ws", prompt="Review notes/good.md"
    )
    assert not clean["ok"]
    assert "notes/fix.md:2: .tmp path outside agy-work" in clean["error"]
    assert "delta.diff" not in clean["error"]
    bad_prompt = run(
        tmp_path, case="lint_ws", root=str(tmp_path), workspace="ws", prompt="Read C:/x/y"
    )
    assert "prompt:1: drive-letter path" in bad_prompt["error"]
    (notes / "fix.md").unlink()
    ok = run(
        tmp_path, case="lint_ws", root=str(tmp_path), workspace="ws", prompt="Review notes/good.md"
    )
    assert ok["ok"] and ok["value"] == "clean"


def test_suffix_is_fixed_one_line_unquoted_and_idempotent(tmp_path: Path) -> None:
    once = run(tmp_path, case="suffix", prompt="Review notes/a.md.\r\n")["value"]
    twice = run(tmp_path, case="suffix", prompt=once)["value"]
    assert once == twice
    assert once.startswith("Review notes/a.md. Execution rules:")
    assert "\n" not in once and '"' not in once
    for phrase in (
        "no background or detached commands",
        "one foreground command",
        "never end a turn while waiting",
    ):
        assert phrase in once
    assert violations(tmp_path, once) == []


def test_extra_note_accepts_project_file_and_relative_path(tmp_path: Path) -> None:
    (tmp_path / "tasks").mkdir()
    note = tmp_path / "tasks" / "fix.md"
    note.write_text("x", encoding="utf-8")
    absolute = run(tmp_path, case="extra", root=str(tmp_path), path=str(note))
    relative = run(tmp_path, case="extra", root=str(tmp_path), path="tasks/fix.md")
    assert absolute["ok"] and relative["ok"]
    assert Path(absolute["value"]).samefile(note) and Path(relative["value"]).samefile(note)


def test_extra_note_refuses_outside_missing_and_protected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / ".secrets").mkdir(parents=True)
    (root / ".secrets" / "k.md").write_text("s", encoding="utf-8")
    (root / "models").mkdir()
    (root / "models" / "m.md").write_text("m", encoding="utf-8")
    (tmp_path / "outside.md").write_text("o", encoding="utf-8")
    (root / "sub").mkdir()
    nested = {
        ".tmp/worktrees/w/.secrets/k.md": "protected directory",
        "sub/models/m.md": "protected directory",
        "sub/.venv/Lib/n.md": "protected directory",
        "sub/.GIT/n.md": "protected directory",
        "sub/.Secrets/deep/er/n.md": "protected directory",
        "sub/notes.txt": "must be a .md file",
        "sub/data.json": "must be a .md file",
        "sub/README": "must be a .md file",
    }
    for rel in nested:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    cases = {
        str(tmp_path / "outside.md"): "outside the project root",
        "sub/../../outside.md": "outside the project root",
        "nope.md": "not found",
        ".secrets/k.md": "protected directory",
        "models/m.md": "protected directory",
        "sub": "not found",
        **nested,
    }
    for path, message in cases.items():
        out = run(tmp_path, case="extra", root=str(root), path=path)
        assert not out["ok"] and message in out["error"], (path, out)
    (root / "sub" / "modelsx.md").write_text("ok", encoding="utf-8")
    (root / "sub" / "A.MD").write_text("ok", encoding="utf-8")
    for fine in ("sub/modelsx.md", "sub/A.MD"):
        assert run(tmp_path, case="extra", root=str(root), path=fine)["ok"], fine


def test_extra_note_refuses_junction(tmp_path: Path) -> None:
    root = tmp_path / "root"
    real = tmp_path / "real"
    root.mkdir()
    real.mkdir()
    (real / "n.md").write_text("n", encoding="utf-8")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "link"), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("junction could not be created")
    out = run(tmp_path, case="extra", root=str(root), path="link/n.md")
    assert not out["ok"] and "link or junction" in out["error"]


def test_busy_message_names_job_and_start_time_and_idle_is_silent(tmp_path: Path) -> None:
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"id": "ref-downscale-r1", "workspace": "w"}), encoding="utf-8-sig")
    busy = run(
        tmp_path, case="busy", state="Running", job_json=str(job), last_run="2026-10-22 11:28:55"
    )
    assert busy["ok"]
    assert "ref-downscale-r1" in busy["value"] and "2026-10-22 11:28:55" in busy["value"]
    assert "does not stop the task" in busy["value"]
    for state in ("Ready", "Disabled", "Queued"):
        assert run(tmp_path, case="busy", state=state, job_json=str(job))["value"] is None
    never_run = run(
        tmp_path, case="busy", state="Running", job_json=str(job), last_run="1999-11-30 00:00:00"
    )
    assert "job.json written" in never_run["value"]
    no_marker = run(tmp_path, case="busy", state="Running", job_json=str(tmp_path / "none.json"))
    assert "'unknown'" in no_marker["value"]


def quota(tmp_path: Path, text: str | None) -> Any:
    stderr = tmp_path / "job.stderr.txt"
    if text is not None:
        stderr.write_text(text, encoding="utf-8")
    out = run(tmp_path, case="quota", stderr_path=str(stderr), now="2026-11-08 12:00:00")
    assert out["ok"], out
    return out["value"]


def test_quota_notice_parses_lowercase_reset_and_is_silent_without_quota(tmp_path: Path) -> None:
    # 2026-11-08: an empty stderr made -notmatch filter an empty array and the old $Matches parse crashed.
    assert quota(tmp_path, None) is None
    assert quota(tmp_path, "") is None
    assert quota(tmp_path, "warning: slow network") is None
    short = quota(tmp_path, "Error: RESOURCE_EXHAUSTED ... resets in 4h2m59s")
    assert "5-hour window" in short and "2026-11-08 16:02" in short
    weekly = quota(tmp_path, "quota reached. Resets in 30h5m")
    assert "weekly window" in weekly and "30h5m" in weekly
    assert "not stated" in quota(tmp_path, "RESOURCE_EXHAUSTED")


GOOD_REPLY = {"status": "SUCCESS", "response": "Review written.", "denied_actions": []}


def incomplete(tmp_path: Path, reply: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
    spec = {"workspace": str(tmp_path), "stderr": "", "expect": "", **kwargs}
    out = run(tmp_path, case="incomplete", reply=GOOD_REPLY if reply is None else reply, **spec)
    assert out["ok"], out
    return out["value"] or []


def test_complete_reply_has_no_reasons(tmp_path: Path) -> None:
    assert incomplete(tmp_path) == []


def test_each_incomplete_signal_is_reported(tmp_path: Path) -> None:
    denied = incomplete(tmp_path, {**GOOD_REPLY, "denied_actions": ["read_file"]})
    assert len(denied) == 1 and "denied_actions" in denied[0] and "read_file" in denied[0]
    assert incomplete(tmp_path, {**GOOD_REPLY, "response": ""}) == ["response is empty"]
    assert incomplete(tmp_path, {**GOOD_REPLY, "response": "  \n"}) == ["response is empty"]
    no_field = {"status": "SUCCESS"}
    assert incomplete(tmp_path, no_field) == ["response is empty"]
    for text in (
        'a tool required the "read_file" permission, so it was auto-denied.',
        "root agent idle; waiting up to 40m0s for 1 Background Task(s)",
    ):
        assert len(incomplete(tmp_path, stderr=text)) == 1
    assert incomplete(tmp_path, stderr="warning: slow network") == []


def test_expect_file_missing_present_and_stale(tmp_path: Path) -> None:
    assert incomplete(tmp_path, expect="REVIEW.md") == ["expected file missing: REVIEW.md"]
    (tmp_path / "REVIEW.md").write_text("done", encoding="utf-8")
    assert incomplete(tmp_path, expect="REVIEW.md", not_before="2000-01-01 00:00:00") == []
    stale = incomplete(tmp_path, expect="REVIEW.md", not_before="2999-01-01 00:00:00")
    assert len(stale) == 1 and "stale" in stale[0]
    assert incomplete(tmp_path, expect="out/REVIEW.md") == ["expected file missing: out/REVIEW.md"]
    for bad in ("../REVIEW.md", str(tmp_path / "REVIEW.md")):
        out = run(
            tmp_path,
            case="incomplete",
            reply=GOOD_REPLY,
            workspace=str(tmp_path),
            stderr="",
            expect=bad,
        )
        assert not out["ok"] and "workspace-relative" in out["error"]


def test_expect_file_is_validated_before_a_job_is_submitted(tmp_path: Path) -> None:
    for good in ("", "REVIEW.md", "out/REVIEW.md", "a b/r.md"):
        assert run(tmp_path, case="expect_assert", expect=good)["ok"], good
    for bad in (
        "../REVIEW.md",
        "out/../../REVIEW.md",
        "..\\REVIEW.md",
        "/REVIEW.md",
        "\\REVIEW.md",
        "C:\\x\\REVIEW.md",
        "C:REVIEW.md",
        "\\\\server\\share\\r.md",
        "REVIEW.md:stream",
        "a|b.md",
    ):
        out = run(tmp_path, case="expect_assert", expect=bad)
        assert not out["ok"] and "workspace-relative" in out["error"], (bad, out)


def test_expect_file_default_notice_only_when_the_default_is_in_force(tmp_path: Path) -> None:
    notice = run(tmp_path, case="expect_notice", expect="REVIEW.md", explicit=False)["value"]
    assert "REVIEW.md" in notice and "INCOMPLETE" in notice and "-ExpectFile ''" in notice
    assert run(tmp_path, case="expect_notice", expect="REVIEW.md", explicit=True)["value"] is None
    assert run(tmp_path, case="expect_notice", expect="", explicit=False)["value"] is None


def test_reasons_accumulate(tmp_path: Path) -> None:
    reply = {"status": "SUCCESS", "response": "", "denied_actions": ["read_file"]}
    reasons = incomplete(tmp_path, reply, stderr="auto-denied", expect="REVIEW.md")
    assert len(reasons) == 4


def text_of(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_the_launcher_checks_the_binding_before_anything_and_never_creates_the_job_folder() -> None:
    launch = text_of("tools/agent-workflow/gemini-launch.ps1")
    check = launch.index("Assert-AgyRunnerBinding -ProjectRoot $root -TaskName $TaskName")
    first_effect = min(
        launch.index(s) for s in ("Write-Host", "WriteAllText", "Assert-AgyIdle", "brief_lint.py")
    )
    assert launch.index("Get-AgyRunnerTaskName -ProjectRoot $root") < check < first_effect
    assert first_effect < launch.rindex("invoke-job.ps1")
    assert not re.search(r"New-Item[^\n]*agy-jobs|CreateDirectory", launch)
    lib = text_of("scripts/agy-runner/agy-lib.ps1")
    assert "Test-AgyPathExists -Path (Join-Path $ProjectRoot '.tmp\\agy-jobs') -PathType Container" in lib
    invoke = text_of("scripts/agy-runner/invoke-job.ps1")
    assert invoke.index("Assert-AgyRunnerBinding") < invoke.index("Copy-Item")


ROOT_HERE = "D:\\Work\\proj-a"
ARGS_HERE = f'-NoProfile -File "C:\\Tools\\agy\\agy-job.ps1" -ProjectRoot "{ROOT_HERE}"'


def binding(tmp_path: Path, **kw: Any) -> Any:
    spec = {"root": ROOT_HERE, "task_name": "AgyRunner-proj-a-0", "arguments": ARGS_HERE, "exists": True,
            "jobs": True, **kw}
    out = run(tmp_path, case="binding", **spec)
    assert out["ok"], out
    return out["value"]


def test_binding_refuses_missing_task_other_root_unparsed_and_missing_job_folder(tmp_path: Path) -> None:
    assert binding(tmp_path) is None
    # Case-insensitive, trailing separator and a relative ".." segment all resolve to the same root.
    assert binding(tmp_path, root="d:\\work\\PROJ-A\\") is None
    assert binding(tmp_path, root="D:\\Work\\x\\..\\proj-a") is None
    missing = binding(tmp_path, exists=False, arguments=None)
    assert "AgyRunner-proj-a-0 for this project is missing" in missing and "setup.ps1 as administrator" in missing
    other = binding(tmp_path, arguments=ARGS_HERE.replace("proj-a", "proj-b"))
    assert "bound to D:\\Work\\proj-b, not to this project D:\\Work\\proj-a" in other
    assert "left as it is" in other and "setup.ps1" in other
    prefix = binding(tmp_path, arguments=ARGS_HERE.replace("proj-a", "proj-ab"))
    assert "bound to D:\\Work\\proj-ab" in prefix
    unparsed = binding(tmp_path, arguments='-File "C:\\Tools\\agy\\agy-job.ps1"')
    assert "does not have the action layout setup.ps1 writes" in unparsed and "left as it is" in unparsed
    # Review F4: parameter-looking text inside another argument is not a binding.
    note = binding(tmp_path, arguments=f"-NoProfile -File \"C:\\Tools\\agy\\agy-job.ps1\" -Note '-ProjectRoot \"{ROOT_HERE}\"'")
    assert "does not have the action layout" in note
    jobs = binding(tmp_path, jobs=False)
    assert ".tmp\\agy-jobs is missing" in jobs and "never creates it" in jobs


def test_task_name_is_derived_per_root_and_python_gets_it_from_the_helper(tmp_path: Path) -> None:
    """Review F2: one source. The Python check asks Get-AgyRunnerTaskName instead of reimplementing it."""
    sys.path.insert(0, str(TOOLS))
    import setup_project

    roots = [
        "D:\\Work\\prompt-enhancer-pipeline",
        "d:\\work\\PROMPT-enhancer-pipeline\\",
        "D:\\Other\\prompt-enhancer-pipeline",
        "D:\\Dự án\\ảnh mẫu (v2)",
        "E:\\" + "x" * 60,
        # The review's reproduced roots: casing expansion, contextual Greek sigma, a character outside the BMP.
        "D:\\Work\\\u0130",
        "D:\\Work\\\u039f\u03a3",
        "D:\\Work\\\U0001f600",
        "D:\\Work\\Same",
        "d:\\work\\SAME\\",
    ]
    names = run(tmp_path, case="task_name", roots=roots)["value"]
    assert names == [setup_project.runner_task_name(r) for r in roots]
    assert names[0] == names[1]  # case and a trailing separator do not change the name
    assert names[0] != names[2]  # same leaf, other folder: the hash keeps them apart
    assert names[8] == names[9]
    assert re.fullmatch(r"AgyRunner-prompt-enhancer-pipeline-[0-9a-f]{8}", names[0])
    assert names[3].startswith("AgyRunner--nh-m-u--v2--")  # non-ASCII and spaces become dashes
    for name in names:
        assert re.fullmatch(r"AgyRunner-[a-z0-9._-]+-[0-9a-f]{8}", name), name
    assert names[4] == "AgyRunner-" + "x" * 40 + "-" + names[4][-8:]
    assert names[7] == "AgyRunner-" + "--" + "-" + names[7][-8:]  # one dash per UTF-16 unit of the surrogate pair
    assert "AgyRunner" not in names  # the legacy single task name is never derived


def test_python_name_call_is_one_hidden_bounded_child(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(TOOLS))
    import setup_project

    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:  # labelled double
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"task_name":"AgyRunner-x-0badc0de"}\n', "")

    monkeypatch.setattr(setup_project.subprocess, "run", fake_run)
    assert setup_project.runner_task_name("D:\\x") == "AgyRunner-x-0badc0de"
    assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW and seen["timeout"] == setup_project.CLI_TIMEOUT_SECONDS
    assert seen["env"]["AGY_CHECK_ROOT"] == "D:\\x" and seen["env"]["AGY_CHECK_MODE"] == "name"
    assert Path(seen["env"]["AGY_CHECK_LIB"]) == ROOT / "scripts" / "agy-runner" / "agy-lib.ps1"
    monkeypatch.setattr(setup_project.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "labelled failure\nAt line:1"))
    with pytest.raises(OSError, match="labelled failure"):
        setup_project.runner_task_name("D:\\x")


@pytest.fixture
def links(tmp_path: Path) -> Any:
    """real/child, a junction link -> real and a chained junction link2 -> link; removed as links afterwards."""
    import _winapi

    real = tmp_path / "real"
    (real / "child").mkdir(parents=True)
    made = []
    try:
        for name, target in (("link", real), ("link2", tmp_path / "link")):
            _winapi.CreateJunction(str(target), str(tmp_path / name))
            made.append(tmp_path / name)
        yield tmp_path
    finally:
        for link in reversed(made):
            os.rmdir(link)  # removes the junction itself, never its target


def test_paths_compare_after_resolving_every_component(tmp_path: Path, links: Path) -> None:
    """Review F3: a junction in an ancestor, a chained link, and the policy for a path that does not exist."""
    real, link, link2 = (str(links / n) for n in ("real", "link", "link2"))
    pairs = [
        (f"{real}\\child", f"{link}\\child"),  # parent junction
        (f"{real}\\child", f"{link2}\\child"),  # chained: link2 -> link -> real
        (f"{real}\\child", f"{link2}"),  # different folders stay different
        (f"{real}\\child\\missing\\x", f"{link2}\\child\\missing\\x"),  # missing tail: prefix resolved, rest as written
        (f"{real}\\child\\missing\\x", f"{real}\\child\\missing\\y"),
        (f"{real.upper()}\\CHILD\\", f"{link}\\child"),  # case and trailing separator
    ]
    assert run(tmp_path, case="same_path", pairs=pairs)["value"] == [True, True, False, True, False, True]
    resolved = run(tmp_path, case="resolve", paths=[f"{link2}\\child", f"{link}\\child\\nope\\Z"])["value"]
    assert resolved == [os.path.realpath(f"{real}\\child"), os.path.realpath(f"{real}\\child") + "\\nope\\Z"]
    names = run(tmp_path, case="task_name", roots=[f"{real}\\child", f"{link2}\\child", f"{link}\\CHILD"])["value"]
    assert len(set(names)) == 1


def test_path_reads_fail_closed(tmp_path: Path) -> None:
    """Review F1: missing is False, a read failure throws instead of looking absent."""
    (tmp_path / "d").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    specs = [{"path": str(tmp_path / p), "type": t} for p, t in
             (("d", "Container"), ("d", "Leaf"), ("f.txt", "Leaf"), ("f.txt", "Container"), ("nope", "Any"),
              ("nope\\deeper", "Any"), ("f.txt\\under-a-file", "Any"))]
    specs.append({"path": "Q:\\no-such-drive\\x", "type": "Any"})
    assert run(tmp_path, case="exists", paths=specs)["value"] == [True, False, True, False, False, False, False, False]
    failed = run(tmp_path, case="sched", scheduler="tasks", tasks=[], path_fail=True, root=str(tmp_path),
                 task_name="t", calls=["exists"])["value"]
    assert failed["exists"].startswith("threw: labelled access-denied double")
    # A real read that fails for another reason than absence (a name Windows cannot hold) throws too.
    bad = run(tmp_path, case="exists", paths=[{"path": str(tmp_path / "a|b"), "type": "Any"}])
    assert bad["ok"] is False and bad["error"], bad


def sched(tmp_path: Path, tasks: list[dict[str, Any]] | str, **kw: Any) -> dict[str, str]:
    spec = {"scheduler": "fail" if tasks == "fail" else "tasks", "tasks": [] if tasks == "fail" else tasks,
            "root": ROOT_HERE, "task_name": "AgyRunner-proj-a-0",
            "calls": ["runner_tasks", "named", "binding", "idle"], **kw}
    out = run(tmp_path, case="sched", **spec)
    assert out["ok"], out
    return out["value"]  # type: ignore[no-any-return]


def task(name: str, args: str = ARGS_HERE, state: str = "Ready", path: str = "\\",
         execute: str = "powershell.exe", workdir: str = "") -> dict[str, Any]:
    return {"TaskName": name, "TaskPath": path, "State": state,
            "Actions": [{"Execute": execute, "Arguments": args, "WorkingDirectory": workdir}]}


# Retest r1 class probes (.tmp/S42-agyrunner-review/class_probes_r1.py), inverted into regressions. They use only the
# scheduler-double calls both rounds share, so each one fails on round 1 and passes on round 2.
BARE_WRAPPER = '-File agy-job.ps1 -ProjectRoot "E:\\Other"'
QUOTED_HOST = f'"-ExecutionPolicy Bypass" -File "C:\\Tools\\agy\\agy-job.ps1" -ProjectRoot "{ROOT_HERE}"'


def test_retest_rf1_a_running_bare_wrapper_is_a_runner_for_idle_and_undo(tmp_path: Path) -> None:
    """RF1: a wrapper started by bare name from the task's working directory is a runner task. It is listed, it
    blocks idle while Running, and undo's keep-list keeps the shared parts for it, even when undo removes its own."""
    bare = task("Other", BARE_WRAPPER, state="Running", workdir="C:\\Tools\\agy")
    out = sched(tmp_path, [task("AgyRunner-proj-a-0"), bare], calls=["runner_tasks", "idle", "retained"])
    assert out["runner_tasks"] == "ok:2"
    assert "Another runner task is running: Other (project unknown)" in out["idle"]
    assert out["retained"] == "ok:Other"


def test_retest_rf2_a_combined_host_option_token_is_not_a_binding(tmp_path: Path) -> None:
    """RF2: one quoted argument "-ExecutionPolicy Bypass" is not setup's two arguments; the binding refuses before any
    launcher write, and the task still counts as a runner task."""
    (tmp_path / ".tmp" / "agy-jobs").mkdir(parents=True)
    quoted = task("AgyRunner-proj-a-0", QUOTED_HOST.replace(ROOT_HERE, str(tmp_path)))
    out = sched(tmp_path, [quoted], root=str(tmp_path), calls=["runner_tasks", "binding"])
    assert out["runner_tasks"] == "ok:1"
    assert out["binding"].startswith("threw: The runner task AgyRunner-proj-a-0 does not have the action layout")
    assert "[-ExecutionPolicy Bypass]" in out["binding"]


# Fix round 3 (retest 2, R2F1): PowerShell unrolls a returned array, so a caller that assigns a helper's list without
# @(...) gets $null for zero results and a bare CimInstance (no Count in Windows PowerShell 5.1) for one. These tests run
# each production caller's own statements, read from the real file, on client-only CIM doubles of the production task
# type, with zero, one and two results; the driver adds no wrapping of its own.
OWN_TASK = "AgyRunner-own-0"


def cim_task(name: str, root: str, state: str = "Ready") -> dict[str, str]:
    return {"name": name, "state": state,
            "arguments": f'-NoProfile -ExecutionPolicy Bypass -File "C:\\Tools\\agy\\agy-job.ps1" -ProjectRoot "{root}"'}


def caller_shape(tmp_path: Path, path: Path, first: str, last: str, tasks: list[dict[str, str]], report: list[str],
                 guard: str | None = None) -> dict[str, Any]:
    out = run(tmp_path, case="caller_shape", path=str(path), first=first, last=last, tasks=tasks, report=report,
              guard=guard, root=str(tmp_path), task_name=OWN_TASK)
    assert out["ok"], out
    return out["value"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("others", [0, 1, 2])
def test_undo_keep_guard_sees_every_retained_cim_task(tmp_path: Path, others: int) -> None:
    """R2F1: undo's own statements from `$all =` to `$others =`, then its keep guard (never its body): this project's
    task (bound here, so undo removes it) plus 0, 1 or 2 other runners. The shared parts stay exactly when one remains."""
    tasks = [cim_task(OWN_TASK, str(tmp_path))] + [cim_task(f"Other{i}", f"E:\\Other{i}") for i in range(others)]
    got = caller_shape(tmp_path, ROOT / "scripts/agy-runner/undo.ps1", "all", "others", tasks, ["all", "others"],
                       guard="others")
    assert got["guard_text"] == "$others.Count -gt 0"
    assert got["others"] == {"is_array": True, "count": others, "type": "System.Object[]"}, got
    assert got["all"] == {"is_array": True, "count": others + 1, "type": "System.Object[]"}, got
    assert got["guard"] is (others > 0), got
    assert got["enumerations"] == 1, got


# (file, first and last variable of the range that reads and then consumes the task list)
SCHEDULER_READ_SITES = [("scripts/agy-runner/undo.ps1", "all", "others"), ("scripts/agy-runner/update.ps1", "all", "busy"),
                        ("scripts/agy-runner/agy-lib.ps1", "all", "running"), ("setup_project.RUNNER_READ_PS", "all", "tasks")]


@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("site", SCHEDULER_READ_SITES, ids=[s[0].rsplit("/", 1)[-1] for s in SCHEDULER_READ_SITES])
def test_every_scheduler_read_keeps_its_array_shape_and_reads_once(tmp_path: Path, site: tuple[str, str, str],
                                                                   count: int) -> None:
    """The task list stays an array for 0, 1 and 2 tasks. Unwrapped, zero tasks give $null, which every helper reads as
    "no list given" and enumerates the scheduler again: a second, separate read inside one decision."""
    path, first, last = site
    if path.startswith("setup_project."):
        sys.path.insert(0, str(TOOLS))
        import setup_project

        reader = tmp_path / "reader.ps1"
        reader.write_text(setup_project.RUNNER_READ_PS, encoding="utf-8-sig")
        file = reader
    else:
        file = ROOT / path
    tasks = [cim_task(f"Other{i}", f"E:\\Other{i}") for i in range(count)]
    got = caller_shape(tmp_path, file, first, last, tasks, [first])
    assert got[first] == {"is_array": True, "count": count, "type": "System.Object[]"}, got
    assert got["enumerations"] == 1, got


PRODUCTION_POWERSHELL = sorted([*(ROOT / "scripts/agy-runner").glob("*.ps1"), *TOOLS.glob("*.ps1")])
# Unwrapped assignments the lint reports that are safe by construction: (file, start of the statement) -> reason.
SHAPE_SAFE = {
    ("scripts/agy-runner/agy-lib.ps1", "$tokens = Split-AgyTaskArgument"):
        "the helper returns its token array with a leading comma, so it is never unrolled; $null means 'does not split'",
    ("scripts/agy-runner/agy-lib.ps1", "$walk = Split-Path -Parent $walk"):
        "Split-Path -Parent returns one string; .Length is that string's length, compared with the root's",
    ("tools/agent-workflow/pm-supervisor.ps1", "$found = Get-ChildItem"):
        "Select-Object -First 1: read as one item only (truthiness and .FullName)",
}


def _shape_findings(tmp_path: Path, files: list[tuple[str, Path]]) -> list[str]:
    out = run(tmp_path, case="shape_lint", files=[{"label": label, "path": str(p)} for label, p in files])
    assert out["ok"], out
    value = out["value"]
    return [value] if isinstance(value, str) else list(value or [])


def test_no_production_powershell_assigns_a_collection_without_its_array_shape(tmp_path: Path) -> None:
    """By class: every unwrapped assignment from a command that is counted, indexed or reads a collection is a finding,
    except the reviewed safe ones above. Covers every production PowerShell file and the check's reader script."""
    sys.path.insert(0, str(TOOLS))
    import setup_project

    reader = tmp_path / "reader.ps1"
    reader.write_text(setup_project.RUNNER_READ_PS, encoding="utf-8-sig")
    files = [(p.relative_to(ROOT).as_posix(), p) for p in PRODUCTION_POWERSHELL]
    findings = _shape_findings(tmp_path, [*files, ("setup_project.RUNNER_READ_PS", reader)])

    def safe(finding: str) -> bool:
        label, _line, text = finding.split(":", 2)
        return any(label == file and text.strip().startswith(start) for file, start in SHAPE_SAFE)

    assert [f for f in findings if not safe(f)] == []
    assert len([f for f in findings if safe(f)]) == len(SHAPE_SAFE), findings  # the lint still sees the safe sites


def test_the_shape_lint_reports_only_unwrapped_counted_or_collection_assignments(tmp_path: Path) -> None:
    sample = tmp_path / "sample.ps1"
    sample.write_text(
        "$a = Get-Thing | Where-Object { $_ }\nif ($a.Count -gt 0) { }\n"  # counted: finding
        "$b = @(Get-Thing | Where-Object { $_ })\n$b.Count\n"  # wrapped: fine
        "$c = Get-ChildItem x\n"  # a collection read: finding even unused
        "$d = 'x,y' -split ','\n$d[0]\n"  # an operator keeps its shape: fine
        "function F { $e = Get-Thing; $e[0] }\n$e = Get-Thing\n"  # indexed only inside F: line 8 only, not line 9
        "$f = Get-AgyRetainingTask -Tasks $all\n",  # a runner list helper: finding
        encoding="utf-8")
    findings = _shape_findings(tmp_path, [("sample", sample)])
    assert [f.split(":")[1] for f in findings] == ["1", "5", "8", "10"], findings


def test_scheduler_reads_fail_closed_and_absence_is_only_a_read_absence(tmp_path: Path) -> None:
    """Review F1: a failed enumeration stops every caller; it never reads as "no runner task"."""
    failed = sched(tmp_path, "fail")
    for call, value in failed.items():
        assert value.startswith("threw: labelled scheduler-unavailable double"), (call, value)
    empty = sched(tmp_path, [])
    assert empty["runner_tasks"] == "ok:0" and empty["named"] == "ok:absent" and empty["idle"] == "ok:idle"
    assert "for this project is missing" in empty["binding"]
    busy = sched(tmp_path, [task("Other", ARGS_HERE.replace("proj-a", "proj-b"), state="Running")])
    assert "Another runner task is running: Other (D:\\Work\\proj-b)" in busy["idle"]
    twins = sched(tmp_path, [task("AgyRunner-proj-a-0"), task("agyrunner-PROJ-A-0", path="\\Other\\")])
    assert "ambiguous" in twins["named"] and "ambiguous" in twins["binding"]
    # A bound task whose job-folder read fails stops with that error, not with "job folder missing".
    denied = sched(tmp_path, [task("AgyRunner-proj-a-0")], path_fail=True, calls=["binding"])
    assert denied["binding"].startswith("threw: labelled access-denied double")


def test_the_legacy_task_name_is_reserved(tmp_path: Path) -> None:
    names = ["AgyRunner", "agyrunner", "\\AgyRunner", " AGYRUNNER ", "AgyRunner-proj-a-0badc0de", "AgyRunner2"]
    out = run(tmp_path, case="reserved", names=names)["value"]
    assert [v.split(":")[0] for v in out] == ["refused"] * 4 + ["allowed"] * 2
    assert "reserved" in out[0] and "another project" in out[0]
    for path in ("scripts/agy-runner/setup.ps1", "scripts/agy-runner/undo.ps1", "scripts/agy-runner/update.ps1",
                 "scripts/agy-runner/invoke-job.ps1", "tools/agent-workflow/gemini-launch.ps1",
                 "tools/agent-workflow/gemini-ws.ps1"):
        text = text_of(path)
        # After the default is derived, so an explicit -TaskName AgyRunner is refused too.
        derive = text.index("$TaskName = Get-AgyRunnerTaskName -ProjectRoot")
        assert derive < text.index("Assert-AgyTaskNameAllowed -TaskName $TaskName"), path


WRAPPER = '-File "C:\\Tools\\agy\\agy-job.ps1"'
PS = "powershell.exe"
R = '-ProjectRoot "D:\\Work\\P"'
W = '"C:\\Tools\\agy\\agy-job.ps1"'

# The accepted and refused action forms of the fix round 2 audit (task file), one row each:
# (execute, arguments, kind, bound root). Kind: bound (binding accepted), runner (binding refused, still counted by
# idle and undo's keep-list), other (not a runner task).
ACTION_FORMS: list[tuple[str, str, str, str | None]] = [
    # setup.ps1's layout, its host options optional, a spaced or non-ASCII root, a bare value, parameter case.
    (PS, f'-NoProfile -ExecutionPolicy Bypass {WRAPPER} -ProjectRoot "D:\\Work\\Project Space"', "bound", "D:\\Work\\Project Space"),
    (PS, f'{WRAPPER} -ProjectRoot "{ROOT_HERE}"', "bound", ROOT_HERE),
    (PS, f"-NoProfile {WRAPPER} {R}", "bound", "D:\\Work\\P"),
    (PS, f"-ExecutionPolicy Bypass {WRAPPER} -ProjectRoot {ROOT_HERE}  ", "bound", ROOT_HERE),
    (PS, '-file "C:\\Tools\\agy\\AGY-JOB.PS1" -projectroot "D:\\Dự án"', "bound", "D:\\Dự án"),
    ("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\PowerShell.EXE", f"-NoProfile {WRAPPER} {R}", "bound", "D:\\Work\\P"),
    # A UNC wrapper path is fully qualified (only its form is checked; nothing is opened).
    (PS, f'-File "\\\\host\\share\\agy\\agy-job.ps1" {R}', "bound", "D:\\Work\\P"),
    # A single option quoted alone is the same argument after Windows splitting.
    (PS, f'"-NoProfile" -ExecutionPolicy "Bypass" {WRAPPER} {R}', "bound", "D:\\Work\\P"),
    (PS, f"-noprofile -executionpolicy bypass {WRAPPER} {R}", "bound", "D:\\Work\\P"),
    # RF2: host options combined into one token, empty tokens, a missing or repeated value, other options.
    (PS, f'"-ExecutionPolicy Bypass" {WRAPPER} {R}', "runner", None),
    (PS, f'"-NoProfile -ExecutionPolicy Bypass" {WRAPPER} {R}', "runner", None),
    (PS, f'-NoProfile "-ExecutionPolicy Bypass" {WRAPPER} {R}', "runner", None),
    (PS, f'"" {WRAPPER} {R}', "runner", None),
    (PS, f'-NoProfile "" {WRAPPER} {R}', "runner", None),
    (PS, f"-ExecutionPolicy {WRAPPER} {R}", "runner", None),
    (PS, f"-ExecutionPolicy Bypass Bypass {WRAPPER} {R}", "runner", None),
    (PS, f"-NoProfile -NoProfile {WRAPPER} {R}", "runner", None),
    (PS, f"-ExecutionPolicy Bypass -NoProfile {WRAPPER} {R}", "runner", None),
    (PS, f"-nop -ep Bypass {WRAPPER} {R}", "runner", None),
    (PS, f"-Command Get-Date {WRAPPER} {R}", "runner", None),
    # RF1: the wrapper by bare name or a path that is not fully qualified.
    (PS, f"-File agy-job.ps1 {R}", "runner", None),
    (PS, f'-File "agy-job.ps1" {R}', "runner", None),
    (PS, f"-File .\\agy-job.ps1 {R}", "runner", None),
    (PS, f"-File \\Tools\\agy\\agy-job.ps1 {R}", "runner", None),
    (PS, f"-File C:agy-job.ps1 {R}", "runner", None),
    # Root forms and layouts refused since round 1 (review F4), plus drive-relative and current-drive roots.
    (PS, f'{WRAPPER} -ProjectRoot "proj-a"', "runner", None),
    (PS, f"{WRAPPER} -ProjectRoot D:Work", "runner", None),
    (PS, f"{WRAPPER} -ProjectRoot \\Work", "runner", None),
    (PS, f'{WRAPPER} -ProjectRoot "-x"', "runner", None),
    (PS, f'{WRAPPER} -ProjectRoot ""', "runner", None),
    (PS, f'{WRAPPER} -ProjectRoot "D:\\Work\\"', "runner", None),  # \" is an escaped quote for Windows splitting
    (PS, f'{WRAPPER} -ProjectRoot "D:\\a" -ProjectRoot "D:\\b"', "runner", None),  # duplicate
    (PS, f'-ProjectRoot "D:\\a" {WRAPPER}', "runner", None),  # misplaced
    (PS, f'{WRAPPER} -ProjectRoot "D:\\a" -Extra 1', "runner", None),
    (PS, f'{WRAPPER} -ProjectRoot "D:\\a -ProjectRoot D:\\b"', "runner", None),
    (PS, f'{WRAPPER} -ProjectRoot "D:\\a -File x"', "runner", None),
    (PS, f"-NoProfile {WRAPPER} -Note '-ProjectRoot \"D:\\Work\\Project Space\"'", "runner", None),
    (PS, f'{WRAPPER} -ProjectRoot "D:\\a', "runner", None),  # unbalanced quote: does not split, still a runner
    (PS, f'{WRAPPER} -ProjectRoot x"D:\\a"', "runner", None),  # glued to a quote
    (PS, f"{WRAPPER} -ProjectRoot", "runner", None),
    # The wrapper named elsewhere: inside a -Command script, as the program itself, under another host.
    (PS, f"-Command \"& 'C:\\Tools\\agy\\agy-job.ps1' {R}\"", "runner", None),
    ("C:\\Tools\\agy\\agy-job.ps1", R, "runner", None),
    ("cmd.exe", f"{WRAPPER} {R}", "runner", None),
    ("pwsh.exe", f"{WRAPPER} {R}", "runner", None),
    ("", f"{WRAPPER} {R}", "runner", None),
    (PS, f'-File "C:\\Tools\\agy\\agy-job.ps1." {R}', "runner", None),  # Windows drops trailing dots
    # Not a runner task.
    (PS, f'-File "C:\\x\\my-agy-job.ps1" {R}', "other", None),
    (PS, '-File "C:\\x\\other.ps1" -ProjectRoot "D:\\agy-job.ps1"', "runner", None),  # named, but not by -File
    (PS, f"-File C:\\x\\agy-job.ps1.bak {R}", "other", None),
    (PS, f"-File C:\\x\\agy-job.ps1-old {R}", "other", None),
    (PS, "-Command Get-Date", "other", None),
    (PS, "-File x.ps1", "other", None),
    (PS, "", "other", None),
]


def classify(tmp_path: Path, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = run(tmp_path, case="classify", root="D:\\Work\\P", cases=cases)
    assert out["ok"], out
    value = out["value"]
    return value if isinstance(value, list) else [value]  # type: ignore[no-any-return]


def assert_consumers_agree(case: dict[str, Any], got: dict[str, Any]) -> None:
    """Property: every consumer reads the one classification. Bound means a binding (setup's preflight and undo's
    removal use the same bound root); every runner or bound task is listed, kept by undo and blocks idle."""
    kind = got["kind"]
    assert kind in ("bound", "runner", "other"), got
    if kind == "bound":
        assert got["root"] and got["bound_root"] == got["root"] and got["binding"] is None, (case, got)
    else:
        assert got["bound_root"] is None and "does not have the action layout" in got["binding"], (case, got)
    counted = 0 if kind == "other" else 1
    assert (got["listed"], got["retained"], got["retained_after_own_removal"],
            got["retained_after_other_folder_removal"]) == (counted, counted, 0, counted), (case, got)
    assert (got["busy"] is not None) == (kind != "other"), (case, got)


@pytest.fixture(scope="module")
def action_forms(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    cases = [{"actions": [{"execute": e, "arguments": a}], "root": root} for e, a, _, root in ACTION_FORMS]
    return classify(tmp_path_factory.mktemp("forms"), cases)


@pytest.mark.parametrize("index", range(len(ACTION_FORMS)), ids=[f"{k}:{a[:40]}" for _, a, k, _ in ACTION_FORMS])
def test_action_form_table(action_forms: list[dict[str, Any]], index: int) -> None:
    execute, arguments, kind, root = ACTION_FORMS[index]
    got = action_forms[index]
    assert (got["kind"], got["root"]) == (kind, root), (execute, arguments, got)
    assert_consumers_agree({"execute": execute, "arguments": arguments}, got)


def test_a_task_is_bound_only_with_one_bound_action(tmp_path: Path) -> None:
    good = {"execute": PS, "arguments": f"-NoProfile {WRAPPER} {R}"}
    note = {"execute": "cmd.exe", "arguments": "/c echo x"}
    cases = [{"actions": [good]}, {"actions": [good, note]}, {"actions": [note, good]}, {"actions": [good, good]},
             {"actions": [note]}, {"actions": []}]
    got = classify(tmp_path, cases)
    assert [g["kind"] for g in got] == ["bound", "runner", "runner", "runner", "other", "other"]
    assert "2 actions" in got[1]["reason"]
    for case, g in zip(cases, got):
        assert_consumers_agree(case, g)


# Property test: actions composed from host-option tokens (each alone, quoted alone, or glued into one quoted token),
# a wrapper form, a root form, a program and an optional tail. The expectation comes from how each case was built,
# not from a second parser: bound exactly when every part is one setup.ps1 writes.
HOST_LAYOUTS = [[], ["-NoProfile"], ["-ExecutionPolicy", "Bypass"], ["-NoProfile", "-ExecutionPolicy", "Bypass"]]
WRAPPERS = [("C:\\Tools\\agy\\agy-job.ps1", True), ("D:\\Prog Files\\agy\\AGY-JOB.ps1", True),
            ("\\\\srv\\share\\agy-job.ps1", True), ("agy-job.ps1", False), (".\\agy-job.ps1", False),
            ("\\Tools\\agy-job.ps1", False), ("C:agy-job.ps1", False)]
# Local or absent drives only: the binding resolves the root, and a UNC root would open a network path.
ROOTS = [("D:\\Work\\P", True), ("E:\\Dự án\\ảnh (v2)", True), ("Q:\\No Drive\\P", True), ("proj-a", False),
         ("D:Work", False), ("\\Work", False)]
PROGRAMS = [("powershell.exe", True), ("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", True),
            ("cmd.exe", False), ("pwsh.exe", False)]


def _quote(token: str) -> str:
    return f'"{token}"'


@st.composite
def runner_action(draw: st.DrawFn) -> tuple[dict[str, Any], bool]:
    valid = True
    host = list(draw(st.sampled_from(HOST_LAYOUTS + [["-NoProfile", "-NoProfile"], ["-ExecutionPolicy"],
                                                     ["-ExecutionPolicy", "Bypass", "Bypass"], ["-ep", "Bypass"], [""]])))
    valid &= host in HOST_LAYOUTS
    host = [draw(st.sampled_from([t, t.upper(), t.lower()])) for t in host]
    rendered: list[str] = []
    i = 0
    while i < len(host):
        style = draw(st.sampled_from(["bare", "quoted", "glued"]))
        if style == "glued" and i + 1 < len(host):
            rendered.append(_quote(f"{host[i]} {host[i + 1]}"))  # two options in one argument
            valid = False
            i += 2
            continue
        rendered.append(_quote(host[i]) if style == "quoted" or not host[i] else host[i])
        i += 1
    wrapper, wrapper_ok = draw(st.sampled_from(WRAPPERS))
    root, root_ok = draw(st.sampled_from(ROOTS))
    program, program_ok = draw(st.sampled_from(PROGRAMS))
    tail = draw(st.sampled_from(["", "-Extra 1"]))
    valid &= wrapper_ok and root_ok and program_ok and not tail
    file_kw = draw(st.sampled_from(["-File", "-file", "-FILE"]))
    arguments = " ".join([*rendered, file_kw, _quote(wrapper), "-ProjectRoot", _quote(root), tail]).strip()
    return {"actions": [{"execute": program, "arguments": arguments}], "root": root}, valid


@settings(max_examples=3, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.lists(runner_action(), min_size=70, max_size=70))
def test_property_every_accepted_form_is_discovered_and_every_refused_form_is_refused_everywhere(
        tmp_path: Path, drawn: list[tuple[dict[str, Any], bool]]) -> None:
    got = classify(tmp_path, [case for case, _ in drawn])
    for (case, valid), g in zip(drawn, got):
        # Every drawn action names the wrapper, so it is never "other".
        assert g["kind"] == ("bound" if valid else "runner"), (case, g)
        assert_consumers_agree(case, g)


def test_another_projects_running_task_blocks_and_own_name_is_ignored(tmp_path: Path) -> None:
    running = [{"TaskName": "AgyRunner", "Arguments": ARGS_HERE.replace("proj-a", "proj-b")}]
    msg = run(tmp_path, case="other_busy", running=running, task_name="AgyRunner-proj-a-0")["value"]
    assert "AgyRunner (D:\\Work\\proj-b)" in msg and "one job at a time across projects" in msg
    assert run(tmp_path, case="other_busy", running=running, task_name="AgyRunner")["value"] is None
    assert run(tmp_path, case="other_busy", running=[], task_name="AgyRunner-proj-a-0")["value"] is None


CI_OI = 3  # InheritanceFlags ContainerInherit | ObjectInherit


def ace(tmp_path: Path, rules: list[dict[str, Any]], mode: str = "grant", rights: str = "Modify",
        inherit: bool = True, user: str = "agy-runner") -> str:
    out = run(tmp_path, case="ace", rules=rules, user=user, mode=mode, rights=rights, inherit=inherit)
    assert out["ok"], out
    return str(out["value"])


def rule(type_: str, rights: str, inherited: bool = False, flags: int = CI_OI,
         identity: str = "HOST\\agy-runner", propagation: int = 0) -> dict[str, Any]:
    return {"identity": identity, "type": type_, "rights": rights, "inherited": inherited, "flags": flags,
            "propagation": propagation}


NO_PROPAGATE, INHERIT_ONLY = 1, 2  # PropagationFlags


def test_ace_counts_a_rule_only_where_it_applies(tmp_path: Path) -> None:
    """Review "noticed": an InheritOnly rule skips the folder, a NoPropagateInherit rule skips deeper children."""
    assert ace(tmp_path, [rule("Allow", "Modify", propagation=INHERIT_ONLY)]) == "apply"
    assert ace(tmp_path, [rule("Allow", "Modify", propagation=NO_PROPAGATE)]) == "apply"
    assert ace(tmp_path, [rule("Allow", "Modify", propagation=INHERIT_ONLY | NO_PROPAGATE)]) == "apply"
    assert ace(tmp_path, [rule("Deny", "FullControl", propagation=INHERIT_ONLY)], mode="deny", rights="FullControl") == "apply"
    # The folder-only drive-root Deny: no inheritance wanted, so only InheritOnly makes it miss.
    assert ace(tmp_path, [rule("Deny", "Write", flags=0)], mode="deny", rights="Write", inherit=False) == "present"
    assert ace(tmp_path, [rule("Deny", "Write", flags=0, propagation=INHERIT_ONLY)], mode="deny", rights="Write",
               inherit=False) == "apply"
    # An explicit Deny that applies only below the folder still blocks a grant meant for its children.
    blocked = ace(tmp_path, [rule("Deny", "FullControl", propagation=INHERIT_ONLY)])
    assert blocked.startswith("blocked:") and "InheritOnly" in blocked


def test_ace_decision_counts_only_a_matching_allow_and_stops_on_an_explicit_deny(tmp_path: Path) -> None:
    # 2026-11-15 RCA: the old skip test took this explicit Deny for "already set".
    blocked = ace(tmp_path, [rule("Deny", "FullControl")])
    assert blocked.startswith("blocked: explicit Deny FullControl") and "HOST\\agy-runner" in blocked
    assert ace(tmp_path, [rule("Deny", "FullControl"), rule("Allow", "Modify")]).startswith("blocked:")
    assert ace(tmp_path, [rule("Allow", "Modify, Synchronize")]) == "present"
    assert ace(tmp_path, [rule("Allow", "FullControl")]) == "present"
    assert ace(tmp_path, [rule("Allow", "ReadAndExecute")]) == "apply"  # too few rights
    assert ace(tmp_path, [rule("Allow", "Modify", flags=0)]) == "apply"  # does not reach subfolders
    assert ace(tmp_path, [rule("Allow", "Modify", inherited=True)]) == "apply"
    assert ace(tmp_path, [rule("Deny", "FullControl", inherited=True)]) == "apply"  # an explicit Allow overrides it
    assert ace(tmp_path, [rule("Allow", "Modify", identity="HOST\\agy-runner2")]) == "apply"
    assert ace(tmp_path, [rule("Allow", "Modify", identity="HOST\\svc")], user="svc") == "present"
    assert ace(tmp_path, []) == "apply"
    # Deny mode: only a Deny with the rights counts; an Allow never makes it present.
    assert ace(tmp_path, [rule("Deny", "FullControl")], mode="deny", rights="FullControl") == "present"
    assert ace(tmp_path, [rule("Allow", "FullControl")], mode="deny", rights="FullControl") == "apply"
    assert ace(tmp_path, [rule("Deny", "Write", flags=0)], mode="deny", rights="Write", inherit=False) == "present"


def test_setup_and_undo_never_rebind_or_remove_another_projects_task() -> None:
    setup = text_of("scripts/agy-runner/setup.ps1")
    assert "[string]$TaskName = ''" in setup and "Get-AgyRunnerTaskName -ProjectRoot $ProjectRoot" in setup
    preflight = setup.index("Test-AgySamePath $bound $ProjectRoot")
    assert preflight < setup.index("foreach ($ace in $aceSpecs) { Get-RunnerAceDecision $ace") < setup.index("1/7 Account")
    assert setup.index("1/7 Account") < setup.index("Register-ScheduledTask")
    assert "IdentityReference -match" not in setup and "remove:" not in setup
    assert "Get-AgyAceDecision" in setup and "never removes a rule" in setup
    undo = text_of("scripts/agy-runner/undo.ps1")
    unregister = undo.index("Unregister-ScheduledTask")
    assert undo.index("Test-AgySamePath $bound $ProjectRoot") < unregister
    assert undo.index("$others = @(Get-AgyRetainingTask -Tasks $all -RemovedTask $removed)") < undo.index("Remove-LocalUser")
    assert undo.index("return") < undo.index("Remove-Item -LiteralPath $ToolDir")
    for path in ("scripts/agy-runner/update.ps1", "scripts/agy-runner/invoke-job.ps1", "tools/agent-workflow/gemini-ws.ps1",
                 "tools/agent-workflow/gemini-launch.ps1", "scripts/agy-runner/undo.ps1"):
        text = text_of(path)
        assert "[string]$TaskName = ''" in text and "Get-AgyRunnerTaskName" in text, path
        assert "'AgyRunner'" not in text, path
    assert "Get-AgyRunnerTask" in text_of("scripts/agy-runner/update.ps1")


def test_scripts_are_wired_to_the_helpers() -> None:
    launch = text_of("tools/agent-workflow/gemini-launch.ps1")
    assert re.search(r"\[int\]\$TimeoutMinutes = 20\b", launch)
    assert "[string]$ExpectFile = 'REVIEW.md'" in launch
    order = [
        launch.index(name)
        for name in ("Assert-AgyIdle", "Assert-AgyPathLint", "Add-AgyReviewSuffix")
    ]
    assert order == sorted(order) and order[-1] < launch.rindex("invoke-job.ps1")
    assert "exit $code" in launch
    ws = text_of("tools/agent-workflow/gemini-ws.ps1")
    assert "[string[]]$ExtraNotes" in ws
    assert (
        ws.index("Assert-AgyIdle")
        < ws.index("Resolve-AgyExtraNote")
        < ws.index("New-Item -ItemType Directory")
    )
    # -Sanitize: the interpreter is checked before anything is created; the notes are rewritten after every copy.
    assert "[switch]$Sanitize" in ws and "[string]$Python" in ws
    assert (
        ws.index("Python for -Sanitize not found")
        < ws.index("New-Item -ItemType Directory")
        < ws.index("Copy-Item")
        < ws.index("Invoke-Native $Python (@('-B', '-I'")
    )
    assert "agy_sanitize.py" in ws[ws.index("Invoke-Native $Python (@('-B', '-I'") :]
    assert (TOOLS / "agy_sanitize.py").is_file()
    invoke = text_of("scripts/agy-runner/invoke-job.ps1")
    assert "Assert-AgyIdle" in invoke and "Get-AgyIncompleteReason" in invoke and "exit 2" in invoke
    assert "[string]$ExpectFile" in invoke
    # Both entry points reject a bad -ExpectFile before the idle check and before anything is submitted.
    assert invoke.index("Assert-AgyExpectFile") < invoke.index("Assert-AgyIdle")
    assert launch.index("Assert-AgyExpectFile") < launch.index("Assert-AgyIdle")
    assert "PSBoundParameters.ContainsKey('ExpectFile')" in launch
    assert "Get-AgyExpectFileNotice" in launch
    assert "Depth 1" in text_of("scripts/agy-runner/agy-lib.ps1")


def test_review_job_allows_workspace_reads_and_keeps_its_result() -> None:
    # Defects found by the 2026-11-14 smoke run (decision 72): no _results folder, headless read_file denied,
    # and GNU tar reading `D:` as a host.
    review = text_of("tools/agent-workflow/gemini-review.ps1")
    assert review.index("New-Item -ItemType Directory -Force -Path $results") < review.index("$jsonPath = ")
    assert 'allow = @("read_file($wsFull)")' in review
    assert "'.secrets'" in review[review.index("deny  = @(") :]
    call = review[review.index("$out = & $agy") :].splitlines()[0]
    assert "--settings $settingsPath" in call
    assert "--dangerously-skip-permissions" not in call
    ws = text_of("tools/agent-workflow/gemini-ws.ps1")
    extract = "Invoke-Native tar @('-xf', 'notes/head.tar')"  # relative archive path, run from inside the workspace
    assert extract in ws and "tar -xf $tar" not in ws
    assert ws.index("Push-Location -LiteralPath $ws") < ws.index(extract)


def test_helpers_never_stop_tasks_or_touch_the_wrapper_and_gemini_store() -> None:
    lib = text_of("scripts/agy-runner/agy-lib.ps1")
    # The wrapper name appears once, as the constant that identifies runner tasks (read-only); nothing copies,
    # writes or runs it.
    assert lib.count("agy-job.ps1") == 1 and "$script:AgyWrapperName = 'agy-job.ps1'" in lib
    for forbidden in (
        "Copy-Item",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Set-ScheduledTask",
        "Start-ScheduledTask",
        "Set-Acl",
        "icacls",
        "schtasks",
        "Stop-ScheduledTask",
        "Stop-Process",
        ".gemini",
        "agy-runner\\",
    ):
        assert forbidden not in lib, forbidden


def test_every_caller_uses_the_one_action_classifier() -> None:
    """Fix round 2 by class: no caller classifies an action itself or joins action text before classifying it."""
    callers = {
        "scripts/agy-runner/setup.ps1": ["$bound = Get-AgyTaskBoundRoot $existing"],
        "scripts/agy-runner/undo.ps1": ["$bound = Get-AgyTaskBoundRoot $task", "if ($removeTask) { $removed = $task }",
                                        # Fix round 3: collection reads keep their array shape (R2F1).
                                        "$others = @(Get-AgyRetainingTask -Tasks $all -RemovedTask $removed)",
                                        "$all = @(Get-AgyScheduledTaskAll)", "Get-AgyTaskLabel $t"],
        "scripts/agy-runner/update.ps1": ["Get-AgyRunnerTask -Tasks $all", "$all = @(Get-AgyScheduledTaskAll)"],
        "tools/agent-workflow/setup_project.py": ["Get-AgyRunnerTask -Tasks $all", "$bound = Get-AgyTaskBoundRoot $t",
                                                  "$all = @(Get-AgyScheduledTaskAll)"],
    }
    removed = ("Test-AgyRunnerAction", "Get-AgyTaskProjectRoot", "Get-AgyTaskArgument")
    for path in [*callers, "scripts/agy-runner/agy-lib.ps1", "scripts/agy-runner/invoke-job.ps1",
                 "tools/agent-workflow/gemini-launch.ps1", "tools/agent-workflow/gemini-ws.ps1"]:
        text = text_of(path)
        for name in removed:
            assert name not in text, (path, name)
        for needle in callers.get(path, []):
            assert needle in text, (path, needle)
        assert ".Arguments" not in text or path == "scripts/agy-runner/agy-lib.ps1", path
    lib = text_of("scripts/agy-runner/agy-lib.ps1")
    # Host options are token sequences, never strings to compare after a join.
    assert "[string[]]@('-NoProfile', '-ExecutionPolicy', 'Bypass')" in lib
    assert lib.count("$_.Arguments") == 1 and "Get-AgyActionClass -Execute ([string]$_.Execute)" in lib


FAIL_CLOSED_SCRIPTS = ("scripts/agy-runner/agy-lib.ps1", "scripts/agy-runner/setup.ps1", "scripts/agy-runner/undo.ps1",
                       "scripts/agy-runner/update.ps1", "scripts/agy-runner/invoke-job.ps1",
                       "tools/agent-workflow/gemini-launch.ps1")


@pytest.mark.parametrize("path", FAIL_CLOSED_SCRIPTS)
def test_no_read_can_fail_into_absence(path: str) -> None:
    """Review F1, by class: Test-Path and SilentlyContinue turn a failed read into "absent"; none is left."""
    text = text_of(path)
    assert "SilentlyContinue" not in text and "Test-Path" not in text, path
    if path != "scripts/agy-runner/agy-lib.ps1":
        assert "$ErrorActionPreference = 'Stop'" in text, path
        assert "Get-ScheduledTask -TaskName" not in text, path  # by name, a missing task is an error, not $null


def test_undo_runs_every_check_before_the_first_removal() -> None:
    undo = text_of("scripts/agy-runner/undo.ps1")
    removals = undo.index("# ---- Removals ----")
    for check in ("Get-AgyScheduledTaskAll", "Get-AgyNamedTask", "Test-AgySamePath $bound $ProjectRoot",
                  "Get-AgyRetainingTask -Tasks $all", "Get-LocalUser -ErrorAction Stop", "$projectPaths =",
                  "$sharedPaths =", "$toolDirExists ="):
        assert undo.index(check) < removals, check
    first_removal = min(undo.index(s, removals) for s in ("Unregister-ScheduledTask", "icacls", "Remove-Item",
                                                            "Remove-LocalUser"))
    assert undo.index("Get-AgyScheduledTaskAll") < first_removal
    for removal in ("Unregister-ScheduledTask", "Remove-Item", "Remove-LocalUser"):
        assert undo.count(removal) == 1 and undo.index(removal) > removals, removal
    assert 'if ($LASTEXITCODE -ne 0) { throw "icacls failed' in undo
    setup = text_of("scripts/agy-runner/setup.ps1")
    assert setup.count("if ($LASTEXITCODE -ne 0)") == 4  # secedit export, secedit configure, icacls x2
    assert "schtasks /run failed" in text_of("scripts/agy-runner/invoke-job.ps1")
