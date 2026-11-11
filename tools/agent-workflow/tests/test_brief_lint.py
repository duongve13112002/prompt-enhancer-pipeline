"""Tests for the brief gate: brief_lint.lint_message (T-MEM rules) and cli_worker.lint_gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parents[1]
FOOTER = (REPO / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md").read_text(encoding="utf-8")


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bl = load(TOOLS / "brief_lint.py", "brief_lint_t_mem")
SETS = (
    'Run these sets: `rg -l "configs/" tests`, test_config_key_effect, test_evidence_hash_scopes, '
    "test_json_depth, the digest-pin suites, and every test importing a changed module.\n"
)
BASE_OK = "Run with `ANTHROPIC_BASE_URL` unset (remove the variable, never set it to empty).\n"
SMOKE = "Smoke one job first (one-job smoke), `run_approval_gate.py check`, `gpu_preflight.py`.\n"


APPROACH = "Approach note: docs/note/R3-approach.md\n"


def brief(
    kind: str = "docs",
    extra: str = "",
    footer: str = FOOTER,
    rids: str = "",
    approach: str = APPROACH,
) -> str:
    head = f"[NEW TASK: X]\nKind: {kind}\n" + approach + (f"R-IDs: {rids}\n" if rids else "")
    return head + "Goal: do it.\n" + extra + "\n\n" + footer


def rules(text: str, full: bool = True, footer: str | None = FOOTER, task: str | None = None):
    return {f.rule for f in bl.lint_message(text, full=full, footer=footer, task_file_text=task)}


def test_clean_briefs_of_every_kind_pass() -> None:
    assert rules(brief("docs")) == set()
    assert rules(brief("scout")) == set()
    assert rules(brief("trivial")) == set()
    assert rules(brief("code", SETS + BASE_OK)) == set()
    assert rules(brief("review", SETS)) == set()
    assert rules(brief("feature", SETS + BASE_OK, rids="R-261, R-225")) == set()
    assert rules(brief("run", SMOKE)) == set()
    assert rules(brief("CODE", SETS)) == set()  # case of the value does not matter


RCA = (
    "Root cause: `count_qualification.py:1741` indexes `verification` on a translator without it.\n"
    'Class sweep: every `["verification"]` read in count_qualification and sam_route probed.\n'
)


def fix_brief(tag: str, kind: str = "code", extra: str = "") -> str:
    return (
        f"[NEW TASK: {tag}]\nKind: {kind}\n{APPROACH}Goal: do it.\n"
        + SETS
        + extra
        + "\n\n"
        + FOOTER
    )


@pytest.mark.parametrize(
    "tag", ["T-X fix round 2", "T-X FIX2", "T-SMALL-FIXES", "fix: T-X", "T-X fixes C1"]
)
def test_a_fix_brief_needs_root_cause_and_class_sweep(tag: str) -> None:
    assert rules(fix_brief(tag)) == {"root-cause"}
    assert rules(fix_brief(tag, extra=RCA)) == set()
    short = "Root cause: a bug.\nClass sweep: done.\n"
    assert rules(fix_brief(tag, extra=short)) == {"root-cause"}
    only_cause = RCA.split("\n")[0] + "\n"
    assert rules(fix_brief(tag, extra=only_cause)) == {"root-cause"}


def test_a_feature_or_code_brief_with_an_approach_note_passes() -> None:
    assert (
        rules(
            brief(
                "code", SETS, approach="Approach note: none: one-line fix, read `brief_lint.py`\n"
            )
        )
        == set()
    )
    assert rules(brief("feature", SETS + BASE_OK, rids="R-261", approach=APPROACH)) == set()


def test_a_feature_or_code_brief_without_an_approach_note_is_flagged() -> None:
    assert rules(brief("code", SETS, approach="")) == {"approach-note"}
    assert rules(brief("feature", SETS, rids="R-261", approach="")) == {"approach-note"}


def test_an_approach_note_shorter_than_twenty_characters_is_flagged() -> None:
    assert rules(brief("code", SETS, approach="Approach note: none: x\n")) == {"approach-note"}


def test_a_none_approach_note_must_name_the_file_read() -> None:
    vague = "Approach note: none: one-line fix, nothing to reuse here\n"
    assert rules(brief("code", SETS, approach=vague)) == {"approach-note"}


def test_a_review_brief_needs_no_approach_note() -> None:
    assert rules(brief("review", SETS, approach="")) == set()


@pytest.mark.parametrize("skip", ["none", "NONE", "None:"])
def test_a_feature_cannot_skip_the_scout_even_with_a_read_file(skip: str) -> None:
    note = f"Approach note: {skip} PM read tools/agent-workflow/brief_lint.py\n"
    assert rules(brief("feature", SETS, rids="R-261", approach=note)) == {"approach-note"}


@pytest.mark.parametrize("kind", ["feature", "code"])
@pytest.mark.parametrize(
    ("note", "task"),
    [
        ("tasks/R3_CODEX_WORKER_TASK.md", "tasks/R3_CODEX_WORKER_TASK.md"),
        ("./tasks//R3_CODEX_WORKER_TASK.md", "tasks/R3_CODEX_WORKER_TASK.md"),
        ("tasks/unused/../R3_CODEX_WORKER_TASK.md", "tasks/R3_CODEX_WORKER_TASK.md"),
        ("tasks/R3_CODEX_WORKER_TASK.md", r"tasks\R3_CODEX_WORKER_TASK.md"),
        (r"D:\project\tasks\scout-task.md", "d:/PROJECT/tasks/SCOUT-TASK.md"),
        (r"\\host\share\tasks\scout-task.md", "//HOST/share/tasks/SCOUT-TASK.md"),
        ("`tasks/R3_CODEX_WORKER_TASK.md`", "`tasks/R3_CODEX_WORKER_TASK.md`"),
    ],
)
def test_the_task_record_cannot_be_its_own_approach_note(kind: str, note: str, task: str) -> None:
    text = brief(
        kind, SETS + f"Task file: {task}\n", rids="R-261", approach=f"Approach note: {note}\n"
    )
    assert rules(text) == {"approach-note"}


@pytest.mark.parametrize("kind", ["feature", "code"])
def test_a_distinct_note_path_passes_even_with_the_same_basename(kind: str) -> None:
    text = brief(
        kind,
        SETS + "Task file: tasks/R3_CODEX_WORKER_TASK.md\n",
        rids="R-261",
        approach="Approach note: docs/note/R3_CODEX_WORKER_TASK.md\n",
    )
    assert rules(text) == set()


def test_code_still_allows_a_reason_naming_the_task_file_read() -> None:
    text = brief(
        "code",
        SETS + "Task file: tasks/R3_CODEX_WORKER_TASK.md\n",
        approach="Approach note: none: PM read tasks/R3_CODEX_WORKER_TASK.md\n",
    )
    assert rules(text) == set()


def test_forward_slash_relative_paths_keep_native_case_semantics() -> None:
    text = brief(
        "code",
        SETS + "Task file: tasks/Long_Task_Record.md\n",
        approach="Approach note: TASKS/LONG_TASK_RECORD.md\n",
    )
    expected = {"approach-note"} if os.path.normcase("A") == os.path.normcase("a") else set()
    assert rules(text) == expected


@pytest.mark.parametrize("kind", ["docs", "review", "scout", "run", "trivial"])
def test_note_task_separation_does_not_expand_to_other_kinds(kind: str) -> None:
    text = brief(
        kind,
        SETS + SMOKE + "Task file: tasks/R3_CODEX_WORKER_TASK.md\n",
        approach="Approach note: tasks/R3_CODEX_WORKER_TASK.md\n",
    )
    assert "approach-note" not in rules(text)


def test_note_task_separation_does_not_apply_to_feedback_lint() -> None:
    text = brief(
        "feature",
        SETS + "Task file: tasks/R3_CODEX_WORKER_TASK.md\n",
        rids="R-261",
        approach="Approach note: tasks/R3_CODEX_WORKER_TASK.md\n",
    )
    assert rules(text, full=False) == set()


WINDOWS_TASK_SPELLINGS = st.tuples(
    *(
        st.sampled_from(("/", "\\"))
        if char == "/"
        else st.sampled_from((char.lower(), char.upper()))
        for char in "D:/project/tasks/R3_CODEX_WORKER_TASK.md"
    )
).map("".join)


@given(WINDOWS_TASK_SPELLINGS, WINDOWS_TASK_SPELLINGS, st.sampled_from(["feature", "code"]))
def test_property_task_file_path_spellings_are_all_refused_as_notes(
    note: str, task: str, kind: str
) -> None:
    text = brief(
        kind, SETS + f"Task file: {task}\n", rids="R-261", approach=f"Approach note: {note}\n"
    )
    assert rules(text) == {"approach-note"}


def test_root_cause_rule_scope() -> None:
    # Not a fix: a word that only contains the letters, or no fix in the tag at all.
    assert rules(fix_brief("T-X prefix table")) == set()
    assert rules(fix_brief("T-X attempt 1")) == set()
    # A review of a fix checks the fix; it does not need its own root-cause header.
    assert rules(fix_brief("R-X fix round 2 delta review", kind="review")) == set()
    # A header inside a fenced block does not count.
    fenced = "```\n" + RCA + "```\n"
    assert rules(fix_brief("T-X fix", extra=fenced)) == {"root-cause"}
    # Feedback messages keep the always-rules only.
    assert rules("[QA FEEDBACK] fix the test\n", full=False) == set()


def test_kind_header_is_required_and_checked() -> None:
    assert rules("Goal: x\n\n" + FOOTER) == {"kind"}
    assert rules(brief("banana")) == {"kind"}
    assert rules(brief("")) == {"kind"}
    bold = "**Kind:** docs\n\n" + FOOTER
    assert rules(bold) == set()
    fenced = "```\nKind: docs\n```\n\n" + FOOTER
    assert rules(fenced) == {"kind"}  # an example in a code block is not the header


def test_footer_must_be_verbatim() -> None:
    assert rules(brief("docs", footer="")) == {"footer"}
    # Position-based, so a later footer rewording cannot turn this into a no-op.
    assert rules(brief("docs", footer=FOOTER[:-40] + "x" + FOOTER[-40:])) == {"footer"}
    assert rules(brief("docs", footer=FOOTER.replace("\n", "\r\n"))) == set()
    assert rules(brief("docs"), footer=None) == {"footer"}
    assert rules(brief("docs"), footer="") == {"footer"}


def test_feature_needs_r_ids() -> None:
    assert rules(brief("feature", SETS, rids="")) == {"r-ids"}
    assert rules(brief("feature", SETS, rids="none")) == {"r-ids"}
    assert rules(brief("feature", SETS, rids="R-261")) == set()


def test_code_brief_needs_every_class_set_or_a_reasoned_waiver() -> None:
    assert rules(brief("code")) == {"test-sets"}
    for needle in (
        "test_config_key_effect",
        "test_evidence_hash_scopes",
        "test_json_depth",
        "digest-pin",
        "importing a changed module",
        '`rg -l "configs/" tests`',
    ):
        assert rules(brief("code", SETS.replace(needle, "x"))) == {"test-sets"}, needle
    waiver = "Test sets: not applicable: tooling only, no configs or src module\n"
    assert rules(brief("code", waiver)) == set()
    assert rules(brief("code", "Test sets: not applicable: n/a\n")) == {
        "test-sets"
    }  # reason too short
    assert rules(brief("docs")) == set()  # docs briefs do not carry the sets


def test_the_class_sets_are_required_for_feature_code_and_review_only() -> None:
    assert "test-sets" in rules(brief("review"))
    assert "test-sets" in rules(brief("feature", rids="R-261"))
    assert rules(brief("review", SETS)) == set()
    for kind in ("docs", "scout", "trivial"):
        assert "test-sets" not in rules(brief(kind))


def test_run_brief_needs_smoke_gate_and_preflight() -> None:
    assert rules(brief("run")) == {"run-gates"}
    assert rules(brief("run", SMOKE.replace("run_approval_gate.py check", "x"))) == {"run-gates"}
    assert rules(brief("run", SMOKE.replace("gpu_preflight", "x"))) == {"run-gates"}


@pytest.mark.parametrize(
    "line",
    [
        "Use `ANTHROPIC_BASE_URL` empty.",
        "ANTHROPIC_BASE_URL=",
        "set ANTHROPIC_BASE_URL=",
        '$env:ANTHROPIC_BASE_URL = ""',
        "$env:ANTHROPIC_BASE_URL=''",
        "Keep a blank ANTHROPIC_BASE_URL for the run.",
        "ANTHROPIC_BASE_URL is blank",
        "Leave `ANTHROPIC_BASE_URL` set to empty.",
    ],
)
def test_base_url_is_never_empty(line: str) -> None:
    assert "base-url-empty" in rules(brief("docs", line + "\n"))
    assert "base-url-empty" in rules(brief("docs", line + "\nANTHROPIC_BASE_URL unset.\n"))


@pytest.mark.parametrize(
    "line",
    [
        "`ANTHROPIC_BASE_URL` unset (remove the variable, never set it to empty)",
        "ANTHROPIC_BASE_URL is unset",
        "Run with `ANTHROPIC_BASE_URL` unset, not empty.",
        "**ANTHROPIC_BASE_URL** unset",
        "Never an empty ANTHROPIC_BASE_URL; ANTHROPIC_BASE_URL unset",
        "Run with `ANTHROPIC_BASE_URL` unset and the output directory empty.",
    ],
)
def test_base_url_wording_that_passes(line: str) -> None:
    assert rules(brief("docs", line + "\n")) == set()


def test_naming_the_variable_without_unset_is_flagged() -> None:
    assert rules(brief("docs", "Check ANTHROPIC_BASE_URL first.\n")) == {"base-url-unset"}


def test_base_url_rule_runs_on_feedback_messages_too() -> None:
    assert rules("[QA FEEDBACK]\nset ANTHROPIC_BASE_URL=\n", full=False) == {"base-url-empty"}
    assert rules("[QA FEEDBACK]\nfix the null check\n", full=False) == set()


def test_git_write_rule_is_kept() -> None:
    assert rules("Commit the changes and push.\n", full=False) == {"git-write"}
    assert rules("Never commit; the PM pushes.\n", full=False) == set()


def test_gemini_absolute_paths_and_inline_python() -> None:
    bad = "gemini-launch.ps1 -PromptPath D:/x/prompt.md\n"
    assert "gemini-absolute-path" in rules(brief("docs", bad))
    assert "gemini-absolute-path" in rules(brief("docs", "gemini-ws.ps1 -Workspace /abs/ws\n"))
    assert rules(brief("docs", "gemini-launch.ps1 -PromptPath .tmp/p.md -Workspace ws\n")) == set()
    assert "inline-python" in rules(brief("docs", "python - <<'EOF'\nprint(1)\nEOF\n"))
    assert "inline-python" in rules(brief("docs", 'python -c "import os\nprint(1)"\n'))
    assert rules(brief("docs", 'python -B -I script.py --flag "x"\n')) == set()


TASK = "# T\n\n| Slice | Scope |\n|---|---|\n| E2a | one |\n| `E2f` | model memory |\n\nAfter.\n"


def test_every_slice_id_of_the_task_file_must_be_in_the_brief() -> None:
    assert rules(brief("docs", "Do E2a and E2f."), task=TASK) == set()
    assert rules(brief("docs", "Do E2a; E2f is left for later."), task=TASK) == set()
    assert rules(brief("docs", "Do E2a only."), task=TASK) == {"slices"}
    assert rules(brief("docs", "Do E2a and E2fx."), task=TASK) == {
        "slices"
    }  # not a substring match
    assert rules(brief("docs", "Do nothing."), task="# T\nno table\n") == set()
    assert bl._slice_ids(TASK) == ["E2a", "E2f"]


def test_main_brief_mode_reports_rule_and_line(tmp_path: Path, capsys) -> None:
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text(brief("docs"), encoding="utf-8")
    bad.write_text("Goal: x\n\n" + FOOTER, encoding="utf-8")
    footer = REPO / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md"
    assert bl.main(["--brief", "--footer", str(footer), str(good)]) == 0
    assert bl.main(["--brief", "--footer", str(footer), str(bad)]) == 1
    assert "[kind]" in capsys.readouterr().out
    assert bl.main([str(bad)]) == 0  # without --brief only the always-rules run


def test_the_real_footer_itself_is_clean_for_the_always_rules() -> None:
    assert rules(FOOTER, full=False) == set()


# ---- property tests (decision 130) ----
KIND_TOKEN = st.text(
    st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=0x24F),
    min_size=1,
    max_size=12,
)


@given(st.text(max_size=400), st.booleans())
def test_property_lint_never_raises(text: str, full: bool) -> None:
    result = bl.lint_message(text, full=full, footer=FOOTER, task_file_text=TASK)
    assert all(isinstance(f.line, int) and f.line >= 1 for f in result)


@given(KIND_TOKEN)
def test_property_kind_finding_iff_the_value_is_not_an_accepted_kind(token: str) -> None:
    found = "kind" in rules(brief(token))
    assert found == (token.lower() not in bl.KINDS)


@given(st.sampled_from(["empty", "blank"]), st.text(" \t", max_size=3), st.booleans())
def test_property_empty_wording_is_always_flagged(word: str, pad: str, before: bool) -> None:
    line = f"{word} ANTHROPIC_BASE_URL" if before else f"ANTHROPIC_BASE_URL{pad} {word}"
    assert "base-url-empty" in rules(brief("docs", line + "\n"))


@given(st.text(" \t", max_size=4), st.text(" \t", max_size=4))
def test_property_the_prescribed_wording_survives_padding(left: str, right: str) -> None:
    text = f"{left}`ANTHROPIC_BASE_URL` unset (remove the variable, never set it to empty){right}\n"
    assert rules(brief("docs", text)) == set()


@given(
    st.integers(min_value=0, max_value=len(FOOTER) - 1), st.characters(blacklist_categories=("Cs",))
)
def test_property_any_footer_edit_is_caught(index: int, char: str) -> None:
    edited = FOOTER[:index] + char + FOOTER[index + 1 :]
    if bl._clean(FOOTER) in bl._clean(edited):
        return  # the edit only touched trailing whitespace or a line ending: the footer is still verbatim
    assert "footer" in rules(brief("docs", footer=edited))


# ---- cli_worker.lint_gate ----
@pytest.fixture
def cw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load(TOOLS / "cli_worker.py", "cli_worker_lint_gate")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agents" / "policy").mkdir(parents=True)
    (tmp_path / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md").write_text(FOOTER, encoding="utf-8")
    return module


def test_start_with_a_bad_brief_is_refused_before_launch(cw: ModuleType) -> None:
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("start", "w", "just do it", None)
    assert "[kind]" in str(refused.value) and "[footer]" in str(refused.value)
    cw.lint_gate("start", "w", brief("docs"), None)  # a clean brief passes


def test_send_lints_fully_only_for_a_new_task(cw: ModuleType) -> None:
    cw.lint_gate("send", "w", "[QA FEEDBACK]\nfix the null check", None)
    cw.lint_gate("send", "w", "[RETEST]\nrun it again", None)
    with pytest.raises(SystemExit):
        cw.lint_gate("send", "w", "[NEW TASK: X]\nno header", None)
    with pytest.raises(SystemExit):
        cw.lint_gate("send", "w", "[QA FEEDBACK]\nset ANTHROPIC_BASE_URL=", None)


def test_waiver_needs_a_reason_and_is_logged(cw: ModuleType, tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit):
        cw.lint_gate("start", "w", "x", "   ")
    cw.lint_gate("start", "w", "x", "user-approved one-off")
    err = capsys.readouterr().err
    assert "WAIVED" in err and "user-approved one-off" in err
    row = json.loads(
        (tmp_path / ".tmp" / "pm-state" / "lint-waivers.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert row["worker"] == "w" and row["reason"] == "user-approved one-off"
    assert {"kind", "footer"} <= set(row["rules"])


def test_a_clean_brief_with_a_waiver_logs_nothing(cw: ModuleType, tmp_path: Path) -> None:
    cw.lint_gate("start", "w", brief("docs"), "unneeded")
    assert not (tmp_path / ".tmp" / "pm-state" / "lint-waivers.jsonl").exists()


def test_a_crashing_linter_warns_loudly_and_does_not_block(
    cw: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(cw.importlib.util, "spec_from_file_location", boom)
    cw.lint_gate("start", "w", "anything", None)
    assert "brief lint crashed (RuntimeError)" in capsys.readouterr().err
    row = json.loads(
        (tmp_path / ".tmp" / "pm-state" / "lint-waivers.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert row["lint_error"] == "RuntimeError"


def test_a_missing_footer_file_is_a_finding_not_a_pass(cw: ModuleType, tmp_path: Path) -> None:
    (tmp_path / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md").unlink()
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("start", "w", brief("docs"), None)
    assert "could not be read" in str(refused.value)


def test_task_file_slices_are_read_relative_to_the_root(cw: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "tasks" / "T.md").write_text(TASK, encoding="utf-8")
    text = brief("docs", "Task file: tasks/T.md\nDo E2a only.\n")
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("start", "w", text, None)
    assert "[slices]" in str(refused.value) and "E2f" in str(refused.value)
    cw.lint_gate("start", "w", brief("docs", "Task file: tasks/T.md\nE2a and E2f.\n"), None)


# ---- A1 (review rev-mem-1): which `send` is a complete brief ----
FEEDBACK_TAGS = ["[QA FEEDBACK]", "[RETEST]", "[EVALUATE]"]
BRIEF_MESSAGES = {
    "kind-first": "Kind: feature\nR-IDs: R-261\nGoal: run feature implementation",
    "comment-then-tag": "<!-- task brief -->\n[NEW TASK: T-1]\nKind: code\n",
    "lower-case-tag": "[new task: t-1]\nGoal: x",
    "mixed-case-tag": "[New Task: T-1]\nGoal: x",
    "heading-then-tag": "# Task\n\n[NEW TASK: T-1]\nGoal: x",
    "markup-before-tag": "> **[NEW TASK: T-1]**\nGoal: x",
    "blank-lines-first": "\n\n   \n[NEW TASK: T-1]\nGoal: x",
    "bom": "﻿[NEW TASK: T-1]\nGoal: x",
    "bom-then-kind": "﻿Kind: docs\nGoal: x",
    "zero-width-space": "​[NEW TASK: T-1]\nGoal: x",
    "fullwidth-brackets": "［NEW TASK: T-1］\nGoal: x",  # noqa: RUF001
    "fullwidth-colon": "Kind： docs\nGoal: x",  # noqa: RUF001
    "bold-kind": "**Kind:** code\nGoal: x",
    "crlf": "[NEW TASK: T-1]\r\nKind: docs\r\n",
    "tag-after-feedback-line": "[QA FEEDBACK]\n[NEW TASK: T-2]\nGoal: x",
    "tag-inside-a-fence": "Notes\n```\n[NEW TASK: T-1]\n```\n",
}
NOT_BRIEFS = {
    "qa-feedback": "[QA FEEDBACK]\nfix the null check",
    "retest": "[RETEST]\nrun it again",
    "evaluate": "[EVALUATE]\nplease judge the result",
    "kind-only-inside-a-fence": "[QA FEEDBACK]\nthe header looks like\n```\nKind: feature\n```\n",
    "prose-mention": "[QA FEEDBACK]\nthe new task list is long",
    "tag-mid-line": "[QA FEEDBACK]\nsee the [NEW TASK: X] brief above",
    "empty": "",
    "whitespace": " \n\t\n",
}


@pytest.mark.parametrize("text", list(BRIEF_MESSAGES.values()), ids=list(BRIEF_MESSAGES))
def test_a_message_with_a_tag_line_or_a_kind_header_is_a_brief(text: str) -> None:
    assert bl.is_brief_message(text) is True


@pytest.mark.parametrize("text", list(NOT_BRIEFS.values()), ids=list(NOT_BRIEFS))
def test_feedback_without_a_kind_header_is_not_a_brief(text: str) -> None:
    assert bl.is_brief_message(text) is False


@pytest.mark.parametrize("text", list(BRIEF_MESSAGES.values()), ids=list(BRIEF_MESSAGES))
def test_send_of_an_unchecked_brief_is_refused_not_skipped(
    cw: ModuleType, tmp_path: Path, text: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("send", "w", text, None)
    assert "[footer]" in str(refused.value)
    assert not (tmp_path / ".tmp" / "pm-state" / "lint-waivers.jsonl").exists()
    cw.lint_gate(
        "send", "w", text, "user-approved one-off"
    )  # a waiver is the only way through, and it is logged
    row = json.loads(
        (tmp_path / ".tmp" / "pm-state" / "lint-waivers.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert row["command"] == "send" and "footer" in row["rules"]


@pytest.mark.parametrize("text", list(NOT_BRIEFS.values()), ids=list(NOT_BRIEFS))
def test_feedback_messages_pass_the_gate_but_keep_the_always_rules(
    cw: ModuleType, text: str
) -> None:
    cw.lint_gate("send", "w", text, None)
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("send", "w", text + "\nset ANTHROPIC_BASE_URL= before the run\n", None)
    assert "[base-url-" in str(refused.value)  # an always-rule still applies to feedback


PREFIXES = [
    "",
    "\n",
    "\n\n  ",
    "<!-- c -->\n",
    "# H\n",
    "> ",
    "* ",
    "- ",
    "﻿",
    "​",
    "\t",
    "`",
    "<!-- ",
]
TAGS = ["[NEW TASK: X]", "[new task]", "[New Task: Y]", "［NEW TASK］", "[NEW  TASK", "[ NEW TASK"]  # noqa: RUF001


@given(
    st.sampled_from(PREFIXES),
    st.sampled_from(PREFIXES),
    st.sampled_from(TAGS),
    st.sampled_from(["\n", "\r\n"]),
)
def test_property_a_tag_line_is_a_brief_whatever_precedes_it(
    first: str, second: str, tag: str, eol: str
) -> None:
    assert bl.is_brief_message(first + second + tag + eol + "Goal: x") is True


@given(
    st.sampled_from(FEEDBACK_TAGS),
    st.text(alphabet="abcdfghjlmopqrsuvwxyz 0123456789\n.:", max_size=80),
)
def test_property_feedback_without_the_letter_k_can_never_be_a_brief(tag: str, body: str) -> None:
    """`kind` and `task` both need a `k`: such a body has neither a header nor a tag."""
    assert bl.is_brief_message(tag + "\n" + body) is False


# ---- A3, A4 ----
def test_the_cli_reads_a_brief_with_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "bom.md"
    path.write_bytes(b"\xef\xbb\xbf" + brief("docs").replace("[NEW TASK: X]\n", "").encode("utf-8"))
    footer = REPO / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md"
    assert bl.main(["--brief", "--footer", str(footer), str(path)]) == 0


def test_a_task_file_outside_the_checkout_is_refused_and_never_read(
    cw: ModuleType, tmp_path: Path
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text(TASK, encoding="utf-8")
    absolute = outside.as_posix()
    for row in (
        absolute,
        f"../{outside.name}",
        "..\\" + outside.name,
        f"tasks/../../{outside.name}",
    ):
        text = brief("docs", f"Task file: {row}\nE2a only.\n")
        with pytest.raises(SystemExit) as refused:
            cw.lint_gate("start", "w", text, None)
        assert "[task-file]" in str(refused.value), row
        assert "[slices]" not in str(refused.value), row  # the outside file was not read


def test_a_link_inside_the_checkout_that_leaves_it_is_refused(
    cw: ModuleType, tmp_path: Path
) -> None:
    outside_dir = tmp_path.parent / f"{tmp_path.name}-linked"
    outside_dir.mkdir()
    (outside_dir / "T.md").write_text(TASK, encoding="utf-8")
    link = tmp_path / "tasks" / "linked"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows without the symlink privilege: a directory junction needs none and resolves the same way.
        made = sys.platform == "win32" and subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside_dir)],
            capture_output=True, check=False, timeout=60,
        ).returncode == 0  # fmt: skip
        if not made:
            pytest.skip("this platform or account cannot create symbolic links")
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("start", "w", brief("docs", "Task file: tasks/linked/T.md\nE2a only.\n"), None)
    assert "[task-file]" in str(refused.value)


def test_the_cli_refuses_an_outside_task_file_too(tmp_path: Path, capsys) -> None:
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agents" / "policy").mkdir(parents=True)
    footer = tmp_path / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md"
    footer.write_text(FOOTER, encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-cli.md"
    outside.write_text(TASK, encoding="utf-8")
    path = tmp_path / "b.md"
    path.write_text(
        brief("docs", f"Task file: {outside.as_posix()}\nE2a only.\n"), encoding="utf-8"
    )
    assert bl.main(["--brief", "--footer", str(footer), str(path)]) == 1
    out = capsys.readouterr().out
    assert "[task-file]" in out and "[slices]" not in out


def test_a_missing_task_file_inside_the_checkout_is_not_a_containment_problem(
    tmp_path: Path,
) -> None:
    assert bl.read_task_file(tmp_path, "tasks/none.md") == (None, None)
    text, problem = bl.read_task_file(tmp_path, "x\0y")
    assert text is None and problem is not None
