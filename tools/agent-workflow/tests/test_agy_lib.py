"""agy launch helpers (scripts/agy-runner/agy-lib.ps1) on fake inputs only: no agy, no scheduled task."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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
        < ws.index("& $Python -B -I")
    )
    assert "agy_sanitize.py" in ws[ws.index("& $Python -B -I") :]
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
    assert "tar -xf notes/head.tar" in ws and "tar -xf $tar" not in ws
    assert ws.index("Push-Location -LiteralPath $ws") < ws.index("tar -xf notes/head.tar")


def test_helpers_never_stop_tasks_or_touch_the_wrapper_and_gemini_store() -> None:
    lib = text_of("scripts/agy-runner/agy-lib.ps1")
    for forbidden in (
        "agy-job.ps1",
        "schtasks",
        "Stop-ScheduledTask",
        "Stop-Process",
        ".gemini",
        "agy-runner\\",
    ):
        assert forbidden not in lib, forbidden
