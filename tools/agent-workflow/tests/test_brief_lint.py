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
    "Run these sets: the tool suite `tools/agent-workflow/tests/`, the configuration override and refusal "
    "tests, and the property tests of parsers and validators.\n"
)
PIPELINE_SETS = SETS.replace("the tool suite `tools/agent-workflow/tests/`", "the pipeline suite `tests/`")
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
    """A brief in the T-021 order: header lines and context, the footer, then the closing `## Task` block."""
    head = f"[NEW TASK: X]\nKind: {kind}\n" + approach + (f"R-IDs: {rids}\n" if rids else "")
    return head + CONTEXT + footer + "\n\n" + TASK_BLOCK + extra


CONTEXT = "Context: the task file and the scout note.\n\n"
TASK_BLOCK = "## Task\n\nGoal: do it.\n"


def legacy_brief(kind: str = "docs", extra: str = "") -> str:
    """A brief in the order used before T-021: header and task first, the footer last."""
    return f"[NEW TASK: X]\nKind: {kind}\n{APPROACH}Goal: do it.\n{extra}\n\n{FOOTER}"


def rules(
    text: str,
    full: bool = True,
    footer: str | None = FOOTER,
    task: str | None = None,
    order: bool = True,
):
    found = bl.lint_message(text, full=full, order=order, footer=footer, task_file_text=task)
    return {f.rule for f in found}


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
    return f"[NEW TASK: {tag}]\nKind: {kind}\n{APPROACH}" + CONTEXT + FOOTER + "\n\n" + TASK_BLOCK + SETS + extra


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
    # Hand-built old-order texts: the order rule is off so that only the header rule is under test.
    assert rules("Goal: x\n\n" + FOOTER, order=False) == {"kind"}
    assert rules(brief("banana")) == {"kind"}
    assert rules(brief("")) == {"kind"}
    bold = "**Kind:** docs\n\n" + FOOTER
    assert rules(bold, order=False) == set()
    fenced = "```\nKind: docs\n```\n\n" + FOOTER
    assert rules(fenced, order=False) == {"kind"}  # an example in a code block is not the header


def test_footer_must_be_verbatim() -> None:
    # No footer heading at all: nothing sits above the task block, so the order rule fires as well.
    assert rules(brief("docs", footer="")) == {"footer", "brief-order"}
    # Position-based, so a later footer rewording cannot turn this into a no-op. The order rule is bound to the
    # verbatim footer span (fix round 1), so an edited footer places no task block either; the old order keeps only
    # the unchanged verbatim rule.
    edited = FOOTER[:-40] + "x" + FOOTER[-40:]
    assert rules(brief("docs", footer=edited)) == {"footer", "brief-order"}
    assert rules(legacy_brief("docs").replace(FOOTER, edited), order=False) == {"footer"}
    assert rules(brief("docs", footer=FOOTER.replace("\n", "\r\n"))) == set()
    # An unreadable footer: no span can be bound, so the order rule fails closed beside the `footer` rule.
    assert rules(brief("docs"), footer=None) == {"footer", "brief-order"}
    assert rules(brief("docs"), footer="") == {"footer", "brief-order"}
    assert rules(legacy_brief("docs"), footer=None, order=False) == {"footer"}


def test_feature_needs_r_ids() -> None:
    assert rules(brief("feature", SETS, rids="")) == {"r-ids"}
    assert rules(brief("feature", SETS, rids="none")) == {"r-ids"}
    assert rules(brief("feature", SETS, rids="R-261")) == set()


def test_code_brief_needs_every_class_set_or_a_reasoned_waiver() -> None:
    assert rules(brief("code")) == {"test-sets"}
    for needle in (
        "tools/agent-workflow/tests/",
        "configuration override and refusal tests",
        "property tests",
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


def sets_message(text: str, path: Path | None = None) -> str:
    found = bl.lint_message(text, full=True, order=True, footer=FOOTER, class_sets_path=path)
    return " | ".join(f.message for f in found if f.rule == "test-sets")


def write_sets(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "brief_lint_config.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_shipped_class_sets_load_and_name_this_repositorys_suites() -> None:
    sets = bl.load_class_sets()
    assert [label for label, _ in sets.required]
    assert set(sets.alternatives) == {"tooling", "pipeline"}
    # Either suite satisfies the alternatives; the root `tests/` is not confused with the tool suite's `tests/`.
    assert rules(brief("code", SETS)) == set()
    assert rules(brief("code", PIPELINE_SETS)) == set()
    assert rules(brief("review", PIPELINE_SETS)) == set()


def test_a_missing_set_is_reported_by_its_configured_label() -> None:
    message = sets_message(brief("code", SETS.replace("property tests", "x")))
    assert "the property tests of parsers and validators" in message
    assert "configuration override" not in message
    message = sets_message(brief("code", SETS.replace("the tool suite `tools/agent-workflow/tests/`, ", "")))
    assert "tooling: the tool suite `tools/agent-workflow/tests/`" in message
    assert "name every set of one group: tooling, pipeline" in message
    # The tool suite's path does not satisfy the pipeline group.
    assert "pipeline: " not in message


def test_the_nearest_group_is_reported_and_ties_keep_file_order(tmp_path: Path) -> None:
    entry = lambda label, pattern: {"label": label, "pattern": pattern}  # noqa: E731
    path = write_sets(
        tmp_path,
        {
            "required": [],
            "alternatives": {
                "first": [entry("A1", "aaa"), entry("A2", "bbb")],
                "second": [entry("B1", "ccc"), entry("B2", "ddd")],
            },
        },
    )
    assert "first: A1; first: A2" in sets_message(brief("code"), path)
    assert "second: B2" in sets_message(brief("code", "ccc"), path)
    assert sets_message(brief("code", "ddd ccc"), path) == ""
    assert sets_message(brief("code", "aaa bbb"), path) == ""


def test_a_non_default_class_set_file_changes_the_result(tmp_path: Path) -> None:
    custom = write_sets(
        tmp_path,
        {
            "required": [{"label": "the zebra suite", "pattern": "zebra"}],
            "alternatives": {"only": [{"label": "the lion suite", "pattern": r"LION\d"}]},
        },
    )
    assert "the zebra suite" in sets_message(brief("code", SETS), custom)
    assert sets_message(brief("code", "zebra lion1"), custom) == ""  # patterns are case-insensitive
    assert sets_message(brief("code", SETS)) == ""  # the shipped file is unchanged by the override


def test_the_waiver_holds_for_any_valid_configuration(tmp_path: Path) -> None:
    waiver = "Test sets: not applicable: tooling only, no configs or src module\n"
    assert sets_message(brief("code", waiver)) == ""
    entry = {"label": "the quokka suite", "pattern": "quokka-suite"}
    custom = write_sets(tmp_path, {"required": [], "alternatives": {"g": [entry]}})
    assert sets_message(brief("code", waiver), custom) == ""
    assert "the quokka suite" in sets_message(brief("code", "Test sets: not applicable: n/a\n"), custom)


_ENTRY = {"label": "L", "pattern": "x"}
_GOOD = {"required": [], "alternatives": {"g": [_ENTRY]}}
_BAD_CONFIGS = {
    "empty object": ({}, "missing key required"),
    "no alternatives key": ({"required": []}, "missing key alternatives"),
    "no required key": ({"alternatives": {"g": [_ENTRY]}}, "missing key required"),
    "unknown key": ({**_GOOD, "extra": 1}, "unknown key(s) extra"),
    "not an object": ([], "must hold a JSON object"),
    "alternatives empty": ({"required": [], "alternatives": {}}, "non-empty object"),
    "alternatives a list": ({"required": [], "alternatives": [_ENTRY]}, "non-empty object"),
    "group empty": ({"required": [], "alternatives": {"g": []}}, "at least one entry"),
    "group not a list": ({"required": [], "alternatives": {"g": "x"}}, "expected a list"),
    "required not a list": ({"required": "x", "alternatives": {"g": [_ENTRY]}}, "expected a list"),
    "entry not an object": ({"required": ["x"], "alternatives": {"g": [_ENTRY]}}, "expected an object"),
    "entry without label": ({"required": [{"pattern": "x"}], "alternatives": {"g": [_ENTRY]}}, "missing key label"),
    "entry without pattern": ({"required": [{"label": "L"}], "alternatives": {"g": [_ENTRY]}}, "missing key pattern"),
    "blank label": ({"required": [{"label": " ", "pattern": "x"}], "alternatives": {"g": [_ENTRY]}}, "label: expected"),
    "pattern not a string": ({"required": [{"label": "L", "pattern": 3}], "alternatives": {"g": [_ENTRY]}}, "pattern: expected"),
    "entry unknown key": ({"required": [{**_ENTRY, "flags": "i"}], "alternatives": {"g": [_ENTRY]}}, "unknown key(s) flags"),
    "invalid regex": ({"required": [{"label": "L", "pattern": "("}], "alternatives": {"g": [_ENTRY]}}, "invalid regular expression"),
    "pattern matches empty text": ({"required": [{"label": "L", "pattern": ".*"}], "alternatives": {"g": [_ENTRY]}}, "matches the empty text"),
}


def test_a_valid_minimal_class_set_file_loads(tmp_path: Path) -> None:
    sets = bl.load_class_sets(write_sets(tmp_path, _GOOD))
    assert sets.required == () and list(sets.alternatives) == ["g"]
    assert bl.load_class_sets(write_sets(tmp_path, {"_comment": "c", **_GOOD})).required == ()


@pytest.mark.parametrize("name", list(_BAD_CONFIGS))
def test_an_invalid_class_set_file_is_refused_with_a_precise_message(tmp_path: Path, name: str) -> None:
    data, expected = _BAD_CONFIGS[name]
    path = write_sets(tmp_path, data)
    with pytest.raises(bl.ClassSetsConfigError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        bl.load_class_sets(path)
    # The lint turns the refusal into a precise finding instead of a crash (cli_worker refuses on a linter crash).
    message = sets_message(brief("code", SETS), path)
    assert message.startswith("the class test sets cannot be loaded") and expected in message


def test_a_missing_empty_or_unreadable_class_set_file_is_refused(tmp_path: Path) -> None:
    for path in (tmp_path / "absent.json", write_sets(tmp_path, ""), write_sets(tmp_path, "{not json")):
        with pytest.raises(bl.ClassSetsConfigError, match="cannot read"):
            bl.load_class_sets(path)
        assert "cannot be loaded" in sets_message(brief("review", SETS), path)
    # A waiver does not hide a broken file, and other kinds never read it.
    waiver = "Test sets: not applicable: tooling only, no configs or src module\n"
    assert "cannot be loaded" in sets_message(brief("code", waiver), tmp_path / "absent.json")
    assert sets_message(brief("docs"), tmp_path / "absent.json") == ""


_COMPILER_FAILURES = {
    "too large a repetition (OverflowError)": ("x{4294967296}", "OverflowError"),
    "deep nesting (RecursionError)": ("(" * 600 + "x" + ")" * 600, "RecursionError"),
    "syntax error (re.error)": ("(", "error"),
}


@pytest.mark.parametrize("name", list(_COMPILER_FAILURES))
def test_a_pattern_the_regex_compiler_rejects_blocks_with_and_without_a_waiver(
    tmp_path: Path, name: str
) -> None:
    pattern, kind = _COMPILER_FAILURES[name]
    path = write_sets(tmp_path, {"required": [{"label": "L", "pattern": pattern}], "alternatives": {"g": [_ENTRY]}})
    with pytest.raises(bl.ClassSetsConfigError, match=kind):
        bl.load_class_sets(path)  # a refusal, never another exception type
    waiver = "Test sets: not applicable: tooling only, no configs or src module\n"
    for extra in (SETS, waiver):
        for kind_name in ("code", "review", "feature"):
            text = brief(kind_name, extra, rids="R-261")
            found = bl.lint_message(text, full=True, order=True, footer=FOOTER, class_sets_path=path)
            assert [f.rule for f in found if f.rule == "test-sets"] == ["test-sets"]
            assert "cannot be loaded" in sets_message(text, path)


def test_a_deeply_nested_class_set_file_is_refused(tmp_path: Path) -> None:
    # 100000 levels: past the C recursion guard of the JSON decoder on every platform (5000 parse on Linux CPython).
    path = write_sets(tmp_path, "[" * 100000 + "0" + "]" * 100000)
    with pytest.raises(bl.ClassSetsConfigError, match="cannot read"):
        bl.load_class_sets(path)
    assert "cannot be loaded" in sets_message(brief("code", SETS), path)


def test_a_failure_while_matching_blocks_instead_of_escaping(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(body: str, sets: object) -> list[str]:
        raise RecursionError("boom")

    monkeypatch.setattr(bl, "missing_class_sets", explode)
    assert "cannot be applied: RecursionError: boom" in sets_message(brief("code", SETS))
    waiver = "Test sets: not applicable: tooling only, no configs or src module\n"
    assert sets_message(brief("code", waiver)) == ""  # a waiver never matches, so it never fails to match


def test_override_and_refusal_tests_are_two_independent_requirements() -> None:
    both = "the tool suite `tools/agent-workflow/tests/`, the property tests, "
    for naming, needs in (
        ("configuration override tests", "refusal"),
        ("configuration refusal tests", "override"),
        ("override tests and refusal tests", None),
        ("configuration override and refusal tests", None),
        ("refusal tests and override tests", None),
    ):
        message = sets_message(brief("code", both + naming))
        if needs is None:
            assert message == "", naming
        else:
            assert f"the configuration {needs} tests" in message, naming
    # The property tests need their own name, not only the word.
    message = sets_message(brief("code", SETS.replace("property tests", "property values")))
    assert "the property tests of parsers and validators" in message
    # Naming neither reports both labels.
    message = sets_message(brief("code", both))
    assert "the configuration override tests" in message and "the configuration refusal tests" in message


@given(st.text(max_size=40))
def test_the_class_set_loader_never_crashes_on_arbitrary_patterns(pattern: str) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = write_sets(Path(folder), {"required": [{"label": "L", "pattern": pattern}], "alternatives": {"g": [_ENTRY]}})
        try:
            sets = bl.load_class_sets(path)
        except bl.ClassSetsConfigError:
            return
        assert sets.required[0][1].search("") is None


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
    assert bl._slice_ids(TASK, order=True) == bl._slice_ids(TASK, order=False) == ["E2a", "E2f"]


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


@given(st.text(max_size=400), st.booleans(), st.booleans())
def test_property_lint_never_raises(text: str, full: bool, order: bool) -> None:
    result = bl.lint_message(text, full=full, order=order, footer=FOOTER, task_file_text=TASK)
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
    # The `send` lookup of the brief-order marker reads this registry, never the live one.
    monkeypatch.setattr(module, "REGISTRY", tmp_path / "registry" / "cli-workers.json")
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


def test_a_crashing_linter_refuses_and_is_logged(
    cw: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(cw.importlib.util, "spec_from_file_location", boom)
    with pytest.raises(SystemExit, match=r"brief lint crashed \(RuntimeError: x\)"):
        cw.lint_gate("start", "w", "anything", None)
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
    for order in (True, False):  # the invariant holds for both layouts
        assert bl.is_brief_message(text, footer=FOOTER, order=order) is True


@pytest.mark.parametrize("text", list(NOT_BRIEFS.values()), ids=list(NOT_BRIEFS))
def test_feedback_without_a_kind_header_is_not_a_brief(text: str) -> None:
    for order in (True, False):
        assert bl.is_brief_message(text, footer=FOOTER, order=order) is False


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
    for order in (True, False):
        assert bl.is_brief_message(first + second + tag + eol + "Goal: x", footer=FOOTER, order=order)


@given(
    st.sampled_from(FEEDBACK_TAGS),
    st.text(alphabet="abcdfghjlmopqrsuvwxyz 0123456789\n.:", max_size=80),
)
def test_property_feedback_without_the_letter_k_can_never_be_a_brief(tag: str, body: str) -> None:
    """`kind` and `task` both need a `k`: such a body has neither a header nor a tag."""
    for order in (True, False):
        assert bl.is_brief_message(tag + "\n" + body, footer=FOOTER, order=order) is False


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


# ---- brief order (T-021: context first, task last) ----
# Context blocks a brief may carry above the footer; the last one hides a task heading and top headings in a fence.
TOP_CONTEXT = [
    "## Wrapper: Claude Code workers\n\n- State the action mode.\n",
    "# T-1 stub task file\n\nThe task file is tasks/T.md.\n",
    "## Role: implementer\n\n- Make the change the brief asks for.\n",
    "## PM dispatch: T-1 phase P1\n\nContext for the phase.\n",
]
CONTEXT_POOL = [
    *TOP_CONTEXT,
    "### Notes\n\nA level-3 heading is context too.\n",
    "Plain context paragraph.\n",
    "```\n## Task\n# an output skeleton heading\n```\n",
]
TASK_POOL = [
    "<goal>finish phase P1</goal>\n",
    "### Done check\n\nthe tool suite passes\n",
    "Scope: tools/agent-workflow/brief_lint.py\n",
    "```\n# Output skeleton\n## Section\n```\n",
]


def ordered(context: list[str], task: list[str], footer: str = FOOTER) -> str:
    head = f"[NEW TASK: X]\nKind: docs\n{APPROACH}"
    return head + "\n".join(context) + "\n" + footer + "\n\n## Task\n\n" + "".join(task)


@given(
    st.lists(st.sampled_from(CONTEXT_POOL), max_size=6),
    st.lists(st.sampled_from(TASK_POOL), max_size=4),
)
def test_property_context_in_any_order_with_the_task_block_last_passes(
    context: list[str], task: list[str]
) -> None:
    assert rules(ordered(context, task)) == set()


@given(
    st.lists(st.sampled_from(CONTEXT_POOL), max_size=4),
    st.sampled_from(TOP_CONTEXT),
    st.lists(st.sampled_from(TASK_POOL), max_size=3),
    st.integers(min_value=0, max_value=3),
)
def test_property_a_top_level_context_block_after_the_task_fails(
    context: list[str], moved: str, task: list[str], at: int
) -> None:
    task = [*task[:at], moved, *task[at:]]
    assert "brief-order" in rules(ordered(context, task))


@given(
    st.lists(st.sampled_from(CONTEXT_POOL), max_size=4),
    st.lists(st.sampled_from(TASK_POOL), max_size=3),
)
def test_property_crlf_line_ends_keep_the_same_verdict(context: list[str], task: list[str]) -> None:
    text = ordered(context, task)
    assert rules(text.replace("\n", "\r\n")) == rules(text) == set()


def test_the_task_heading_is_exact_and_fences_hide_it() -> None:
    head = "[NEW TASK: X]\nKind: docs\n" + APPROACH + CONTEXT + FOOTER
    assert rules(head + "\n\n## Task brief\n\nGoal: x.\n") == {"brief-order"}
    assert rules(head + "\n\n```\n## Task\n```\nGoal: x.\n") == {"brief-order"}
    assert rules(brief("docs")) == set()


def test_two_task_blocks_are_flagged_at_the_second() -> None:
    text = brief("docs", "\n## Task\n\nGoal: again.\n")
    lines = text.split("\n")
    second = lines.index("## Task", lines.index("## Task") + 1) + 1
    found = bl.lint_message(text, full=True, order=True, footer=FOOTER)
    assert [(f.rule, f.line) for f in found] == [("brief-order", second)]


def test_the_footer_and_the_role_pack_must_come_before_the_task() -> None:
    footer_after = "[NEW TASK: X]\nKind: docs\n" + APPROACH + "## Task\n\nGoal: x.\n\n" + FOOTER
    assert rules(footer_after) == {"brief-order"}
    assert rules(brief("docs", "\n## Role: implementer\n\n- rule\n")) == {"brief-order"}


def test_the_old_order_passes_only_without_the_order_rule() -> None:
    assert rules(legacy_brief("docs"), order=False) == set()
    assert rules(legacy_brief("docs"), order=True) == {"brief-order"}
    assert rules(legacy_brief("docs"), full=False, order=True) == set()  # feedback has no order rule


def test_the_task_block_after_the_footer_is_linted_with_its_own_line_numbers() -> None:
    text = brief("docs", "Then git commit the changes.\n")
    line = text.split("\n").index("Then git commit the changes.") + 1
    found = bl.lint_message(text, full=True, order=True, footer=FOOTER)
    assert [(f.rule, f.line) for f in found] == [("git-write", line)]
    # Old order: text after the footer heading without a `## Task` line is still the footer, as before T-021.
    assert rules(legacy_brief("docs") + "\nThen git commit the changes.\n", order=False) == set()


def test_the_footer_span_keeps_line_numbers_and_a_header_in_the_task_block_makes_a_brief() -> None:
    text = brief("docs")
    stripped = bl._strip_footer(text, FOOTER, order=True)
    assert stripped.count("\n") == text.count("\n") and bl.FOOTER_HEADING not in stripped
    assert stripped.endswith(TASK_BLOCK)
    late_kind = "[QA FEEDBACK]\n" + CONTEXT + FOOTER + "\n\n## Task\n\nKind: docs\n"
    assert bl.is_brief_message(late_kind, footer=FOOTER, order=True) is True
    # A worker started before T-021 keeps the base cut: everything from the footer heading on is the footer.
    assert bl._strip_footer(text, FOOTER, order=False) == text[: text.find(bl.FOOTER_HEADING)]
    assert bl.is_brief_message(late_kind, footer=FOOTER, order=False) is False


# ---- T-021 P1 fix round 1: one CommonMark block scanner (review .tmp/T-021/review-p1/review.md, F1-F3) ----
HEAD = "[NEW TASK: X]\nKind: docs\n" + APPROACH
# The reviewer's six bypasses (probes.py there), rebuilt on this file's brief; each passed before the fix.
REVIEW_PROBES = {
    "F2-tilde-fence-hides-the-only-task": HEAD + CONTEXT + FOOTER + "\n\n~~~markdown\n## Task\nGoal: x.\n~~~\n",
    "F2-shorter-closer-keeps-the-fence": brief("docs", "\n````markdown\n```\nexample\n````\n## Role: reviewer\nx\n"),
    "F3-setext-h1": brief("docs", "\nLate context\n============\ntext\n"),
    "F3-setext-h2": brief("docs", "\nLate context\n------------\ntext\n"),
    "F3-indented-atx": brief("docs", "\n   ## Role: reviewer\nlate context\n"),
    "F1-spoof-heading-and-fenced-footer": HEAD
    + "# Agent brief footer placeholder\n\n## Task\n\nGoal: x.\n\n```markdown\n"
    + FOOTER
    + "\n```\n",
}


@pytest.mark.parametrize("text", list(REVIEW_PROBES.values()), ids=list(REVIEW_PROBES))
def test_the_review_bypasses_are_refused(text: str) -> None:
    assert rules(text) == {"brief-order"}


INFO = st.sampled_from(["", "markdown", "text x", " python"])


@st.composite
def fences(draw: st.DrawFn) -> tuple[str, list[str], str]:
    """(opener, decoy lines that cannot close it, closer): CommonMark 4.5 with both kinds, lengths 3 to 6 and
    indents 0 to 3; decoys are the other kind, a shorter run, a run with an info string and a 4-space indent."""
    char = draw(st.sampled_from("`~"))
    length = draw(st.integers(3, 6))
    other = "~" if char == "`" else "`"
    opener = " " * draw(st.integers(0, 3)) + char * length + draw(INFO)
    decoys = draw(
        st.lists(
            st.sampled_from(
                [
                    other * draw(st.integers(3, 8)),
                    char * draw(st.integers(1, length - 1)),
                    char * length + " x",
                    "    " + char * length,
                ]
            ),
            max_size=4,
        )
    )
    closer = " " * draw(st.integers(0, 3)) + char * (length + draw(st.integers(0, 2))) + " " * draw(st.integers(0, 2))
    return opener, decoys, closer


HIDDEN = ["## Role: hidden", "# Late", "Late\n====", "Late\n----", "  ## Task"]


@given(fences())
def test_property_a_fence_hides_headings_until_its_own_closer(fence: tuple[str, list[str], str]) -> None:
    opener, decoys, closer = fence
    block = "\n".join([opener, *decoys, *HIDDEN, closer]) + "\n"
    assert rules(brief("docs", "\n" + block)) == set()
    assert rules(brief("docs", "\n" + block + "\n## Role: late\n")) == {"brief-order"}
    # Unmatched: without its closer the fence runs to the end of the text (4.5), so nothing after it is a heading.
    assert rules(brief("docs", "\n" + "\n".join([opener, *decoys, "## Role: x"]) + "\n")) == set()


@given(fences())
def test_property_a_fenced_task_heading_is_not_the_task(fence: tuple[str, list[str], str]) -> None:
    opener, decoys, closer = fence
    fenced = "\n".join([opener, "## Task", *decoys, closer]) + "\n"
    assert rules(HEAD + CONTEXT + FOOTER + "\n\n" + fenced + "Goal: x.\n") == {"brief-order"}
    assert rules(HEAD + CONTEXT + FOOTER + "\n\n" + fenced + "\n" + TASK_BLOCK) == set()


@given(fences())
def test_property_the_footer_counts_only_outside_a_fence(fence: tuple[str, list[str], str]) -> None:
    opener, _, closer = fence
    inside = HEAD + CONTEXT + opener + "\n" + FOOTER + "\n" + closer + "\n\n" + TASK_BLOCK
    assert rules(inside) == {"brief-order"}  # verbatim, so the `footer` rule passes; its position does not
    assert rules(HEAD + CONTEXT + opener + "\n" + closer + "\n" + FOOTER + "\n\n" + TASK_BLOCK) == set()


@given(
    st.integers(1, 6),
    st.integers(0, 6),
    st.sampled_from([" ", "\t", "  "]),
    st.sampled_from(["Role: reviewer", "Late context", "Task"]),
    st.sampled_from(["", " #", " ##"]),
)
def test_property_atx_headings_after_the_task(
    level: int, indent: int, gap: str, title: str, closing: str
) -> None:
    line = " " * indent + "#" * level + gap + title + closing
    late = level <= 2 and indent <= 3  # 4 columns of indentation make no heading (4.2)
    assert rules(brief("docs", "\n" + line + "\nx\n")) == ({"brief-order"} if late else set())


@given(
    st.integers(0, 3),
    st.sampled_from("=-"),
    st.integers(1, 6),
    st.integers(0, 3),
    st.sampled_from(["", " ", "\t "]),
    st.booleans(),
)
def test_property_setext_headings_after_the_task(
    text_indent: int, char: str, length: int, underline_indent: int, trailing: str, two_lines: bool
) -> None:
    paragraph = " " * text_indent + "Late context" + ("\nmore of it" if two_lines else "")
    underline = " " * underline_indent + char * length + trailing
    assert rules(brief("docs", "\n" + paragraph + "\n" + underline + "\ntext\n")) == {"brief-order"}
    # The same underline with a blank line before it heads nothing: `-` runs of 3 or more are a thematic break.
    assert rules(brief("docs", "\n" + paragraph + "\n\n" + underline + "\ntext\n")) == set()


@given(
    st.sampled_from("*-_"),
    st.integers(3, 6),
    st.sampled_from(["", " ", "\t", "  "]),
    st.integers(0, 3),
    st.booleans(),
)
def test_property_thematic_breaks_are_not_headings(
    char: str, count: int, gap: str, indent: int, after_text: bool
) -> None:
    line = " " * indent + gap.join([char] * count)
    if after_text and char == "-" and not gap:
        return  # `text` then `---` is a Setext heading, covered above
    before = "\nSome text\n" if after_text else "\n"
    assert rules(brief("docs", before + line + "\nmore\n")) == set()


def test_the_task_follows_the_end_of_the_verbatim_span() -> None:
    # A footer without a heading of its own: only the span position can place it (the late-heading check cannot).
    plain = "Footer rule one.\nFooter rule two.\n"
    assert rules(HEAD + CONTEXT + plain + "\n" + TASK_BLOCK, footer=plain) == set()
    assert rules(HEAD + CONTEXT + TASK_BLOCK + "\n" + plain, footer=plain) == {"brief-order"}
    # Inside an HTML block the footer is raw HTML, not top-level text (fix round 2).
    assert rules(HEAD + CONTEXT + "<div>\n" + plain + "\n" + TASK_BLOCK, footer=plain) == {"brief-order"}


def test_text_between_the_footer_and_the_task_is_linted() -> None:
    # The footer is exactly its verbatim span, so nothing after it hides from the rules.
    text = HEAD + CONTEXT + FOOTER + "\nThen git commit the changes.\n\n" + TASK_BLOCK
    assert rules(text) == {"git-write"}


def test_every_fence_reader_uses_the_scanner() -> None:
    # git-write: any fence kind hides a command; a shorter closer does not end the fence, a matching one does.
    assert rules(brief("docs", "\n~~~\ngit commit -m x\n~~~\n")) == set()
    assert rules(brief("docs", "\n````\n```\ngit commit -m x\n````\n")) == set()
    assert rules(brief("docs", "\n````\n```\n````\nThen git commit the changes.\n")) == {"git-write"}
    # Header lines and the test-set waiver count only outside fences of either kind.
    assert bl.header_fields("~~~\nKind: code\n~~~\nKind: docs\n", order=True)["kind"] == (4, "docs")
    waiver = "Test sets: not applicable: a docs-only change of one table\n"
    assert rules(brief("code", waiver)) == set()
    assert rules(brief("code", "~~~~\n" + waiver + "~~~~\n")) == {"test-sets"}
    # The slice table of a task file is read outside fences only.
    table = "| Slice | a |\n|---|---|\n| {} | x |\n"
    fenced_table = "~~~\n" + table.format("S1") + "~~~\n\n" + table.format("S2")
    assert bl._slice_ids(fenced_table, order=True) == ["S2"]
    # A worker started before T-021 keeps the base readers: backtick fences only, every waiver line, every table.
    assert bl.header_fields("~~~\nKind: code\n~~~\nKind: docs\n", order=False)["kind"] == (2, "code")
    assert bl._slice_ids(fenced_table, order=False) == ["S1"]
    legacy_waived = legacy_brief("code", "~~~~\n" + waiver + "~~~~\n")
    assert rules(legacy_waived, order=False) == set()


def test_the_cli_requires_a_footer_heading_outside_fences(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fenced, real = tmp_path / "fenced.md", tmp_path / "real.md"
    fenced.write_text(brief("docs") + "```\n" + bl.FOOTER_HEADING + "\n```\n", encoding="utf-8")
    fenced.write_text(fenced.read_text(encoding="utf-8").replace(FOOTER, "x\n"), encoding="utf-8")
    real.write_text(brief("docs"), encoding="utf-8")
    footer = str(REPO / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md")
    bl.main(["--brief", "--require-footer", "--footer", footer, str(fenced)])
    assert "footer missing" in capsys.readouterr().out  # a fenced heading is no footer heading
    assert bl.main(["--brief", "--require-footer", "--footer", footer, str(real)]) == 0
    # A message lint keeps the base presence check, which is a substring test.
    assert bl.main(["--require-footer", str(fenced)]) == 0


# ---- T-021 P1 fix round 2: containers and HTML (retest .tmp/T-021/retest1/review.md, F4 and F5) ----
def kinds(text: str, order: bool = True) -> set[str]:
    """Rules of a full lint, with each `brief-order` finding named by its cause."""
    out = set()
    for f in bl.lint_message(text, full=True, order=order, footer=FOOTER):
        if f.rule != "brief-order":
            out.add(f.rule)
        elif f.message == bl._UNSUPPORTED:
            out.add("unsupported")
        else:
            out.add(f.message.split(";")[0].split("`")[0].strip())  # "no", "a second", "a level-1 ...", ...
    return out


NO_TASK, SECOND, LATE = "no", "a second", "a level-1 or level-2 heading after"
TOP = HEAD + CONTEXT + FOOTER + "\n\n"
# Every construct row of the retest and its case file; before the fix each bypass passed and each example in a
# container or HTML block was refused as a late heading.
RETEST_ROWS = {
    "quote-only-task": (TOP + "> ## Task\n> Goal: example\n", {NO_TASK, "unsupported"}),
    "quote-late-heading": (brief("docs", "\n> ## Role: reviewer\n> late context\n"), {"unsupported"}),
    "quote-fenced-heading": (brief("docs", "\n> ```\n> ## Role: reviewer\n> ```\n"), {"unsupported"}),
    "list-only-task": (TOP + "- ## Task\n  Goal: example\n", {NO_TASK, "unsupported"}),
    "list-late-heading": (brief("docs", "\n- ## Role: reviewer\n  late context\n"), {"unsupported"}),
    "list-fenced-heading": (brief("docs", "\n- ```\n  ## Role: reviewer\n  ```\n"), {"unsupported"}),
    "list-fence-then-real-heading": (
        brief("docs", "\n- ```\n  example\n  ```\n\n## Role: reviewer\nlate context\n"),
        {"unsupported", LATE},
    ),
    "list-fence-then-second-task": (
        brief("docs", "\n- ```\n  example\n  ```\n\n## Task\nsecond task\n"),
        {"unsupported", SECOND},
    ),
    "comment-only-task": (TOP + "<!--\n## Task\nGoal: example\n-->\n", {NO_TASK, "unsupported"}),
    "div-only-task": (TOP + "<div>\n## Task\nGoal: example\n</div>\n", {NO_TASK, "unsupported"}),
    "comment-late-example": (brief("docs", "\n<!--\n## Role: reviewer\n-->\n"), {"unsupported"}),
    "pre-backticks-then-real-heading": (
        brief("docs", "\n<pre>\n```\n</pre>\n\n## Role: reviewer\nlate context\n"),
        {"unsupported", LATE},
    ),
    "indented-only-task": (TOP + "    ## Task\n    example\n", {NO_TASK, "unsupported"}),  # inside footer item 17
    "indented-example-heading": (brief("docs", "\n    ## Role: reviewer\n    example\n"), set()),
    "unclosed-fence-before-task": (TOP + "```\n## Task\nexample\n", {NO_TASK}),
    "unclosed-fence-after-task": (brief("docs", "\n```\n## Role: reviewer\nexample\n"), set()),
    "nested-fence-kinds": (brief("docs", "\n````\n~~~\n```\n## Role: example\n~~~\n````\n"), set()),
    "nested-fences-then-heading": (
        brief("docs", "\n````\n~~~\n```\n## Role: example\n~~~\n````\n## Role: real\n"),
        {LATE},
    ),
    # Tag lines of the Claude wrapper are CommonMark HTML blocks (condition 7); without structure inside they pass.
    "goal-tags": (brief("docs", "\n<goal>\nfinish it\n- one step\n</goal>\n\n<output>\nReply.\n</output>\n"), set()),
    "fence-right-under-a-tag": (brief("docs", "\n<output>\n```text\nx\n```\n</output>\n"), {"unsupported"}),
    # A lone tag cannot interrupt a paragraph (4.6, condition 7), so the heading after it is a real one.
    "tag-inside-a-paragraph": (brief("docs", "\ntext\n<goal>\n## Role: real\n"), {LATE}),
    "blank-line-after-the-tag": (brief("docs", "\n<output>\n\n```text\n## x\n```\n\n</output>\n"), set()),
}


@pytest.mark.parametrize(("text", "expected"), list(RETEST_ROWS.values()), ids=list(RETEST_ROWS))
def test_the_retest_rows(text: str, expected: set[str]) -> None:
    assert kinds(text) == expected


LEGACY_HTML_TASK = legacy_brief("docs") + "\n<!--\n## Task\ngit commit the changes.\n-->\n"


def test_a_markerless_send_reads_as_the_base_commit_did(cw: ModuleType) -> None:
    # F5 C: a commented `## Task` after an old-order footer is footer text to the base reader; no layout inference.
    assert kinds(LEGACY_HTML_TASK, order=False) == set()
    write_registry(cw, {"status": "idle"})
    cw.lint_gate("send", "w", LEGACY_HTML_TASK, None)
    write_registry(cw, {"status": "idle", cw.BRIEF_ORDER_KEY: cw.BRIEF_ORDER})
    with pytest.raises(SystemExit, match=r"\[brief-order\]"):
        cw.lint_gate("send", "w", LEGACY_HTML_TASK, None)
    # The marker is read before `is_brief_message`: a `Kind:` in a tilde fence is a header to the base reader (so the
    # send is a brief without a footer), and fenced example text to the scanner (so it is feedback).
    tilde_kind = "[QA FEEDBACK]\n~~~\nKind: code\n~~~\n"
    cw.lint_gate("send", "w", tilde_kind, None)
    write_registry(cw, {"status": "idle"})
    with pytest.raises(SystemExit, match=r"\[footer\]"):
        cw.lint_gate("send", "w", tilde_kind, None)


def test_a_leading_byte_order_mark_is_no_text() -> None:
    bom = chr(0xFEFF)
    text = bom + FOOTER + "\n\n## Task\n\n[NEW TASK: X]\nKind: docs\n" + APPROACH
    assert kinds(text) == set()  # the footer span and its heading start on line 1, after the mark
    assert bl._task_lines(bl._scan(bom + "## Task\n")) == [0]


def wrap(kind: str, body: list[str]) -> str:
    """`body` inside one container or HTML block of `kind`, then a blank line."""
    if kind == "quote":
        lines = ["> " + line for line in body]
    elif kind in ("bullet", "ordered"):
        marker = "- " if kind == "bullet" else "1. "
        # Continuation lines start at the item's content column (5.2): the marker plus the first line's indent.
        width = len(marker) + len(body[0]) - len(body[0].lstrip(" "))
        lines = [marker + body[0], *(" " * width + line for line in body[1:])]
    else:
        opener, closer = {
            "comment": ("<!--", "-->"),
            "pre": ("<pre>", "</pre>"),
            "div": ("<div>", "</div>"),
            "tag": ("<goal>", "</goal>"),
        }[kind]
        lines = [opener, *body, closer]
    return "\n" + "\n".join(lines) + "\n\n"


WRAPPERS = st.sampled_from(["quote", "bullet", "ordered", "comment", "pre", "div", "tag"])


@st.composite
def structures(draw: st.DrawFn) -> list[str]:
    """Lines that hold structure: a fence (closed or not) around a heading, an ATX heading or a Setext heading."""
    opener, decoys, closer = draw(fences())
    choice = draw(st.sampled_from(["fence", "open-fence", "atx", "setext"]))
    if choice == "fence":
        return [opener, *decoys, "## Role: hidden", closer]
    if choice == "open-fence":
        return [opener, "## Task"]
    return ["## Role: hidden"] if choice == "atx" else ["Late", "---"]


@given(WRAPPERS, structures(), st.sampled_from(["## Role: real", "## Task", "Real\n===="]))
def test_property_wrapped_structure_after_the_task_never_hides_a_real_heading(
    kind: str, inner: list[str], real: str
) -> None:
    found = kinds(brief("docs", wrap(kind, inner) + real + "\nx\n"))
    assert "unsupported" in found and (SECOND in found or LATE in found)


@given(WRAPPERS, structures())
def test_property_wrapped_structure_above_the_footer_is_refused_by_name_only(kind: str, inner: list[str]) -> None:
    text = HEAD + CONTEXT + wrap(kind, inner) + FOOTER + "\n\n" + TASK_BLOCK
    assert kinds(text) == {"unsupported"}  # the real footer and task are still found


@given(WRAPPERS, st.lists(st.sampled_from(["plain text", "Kind: docs", "git status", "- item", "1. step"]), min_size=1, max_size=4))
def test_property_wrapped_plain_text_passes(kind: str, inner: list[str]) -> None:
    assert kinds(HEAD + CONTEXT + wrap(kind, inner) + FOOTER + "\n\n" + TASK_BLOCK + wrap(kind, inner)) == set()


def write_registry(cw: ModuleType, entry: dict) -> None:
    cw.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    cw.REGISTRY.write_text(json.dumps({"w": entry}), encoding="utf-8")


def test_start_holds_every_new_brief_to_the_order(cw: ModuleType) -> None:
    cw.lint_gate("start", "w", brief("docs"), None)
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("start", "w", legacy_brief("docs"), None)
    assert "[brief-order]" in str(refused.value)


def test_a_send_follows_the_registry_marker(cw: ModuleType) -> None:
    write_registry(cw, {"status": "idle"})  # started before T-021: no marker, old order
    cw.lint_gate("send", "w", legacy_brief("docs"), None)
    write_registry(cw, {"status": "idle", cw.BRIEF_ORDER_KEY: cw.BRIEF_ORDER})
    cw.lint_gate("send", "w", brief("docs"), None)
    with pytest.raises(SystemExit) as refused:
        cw.lint_gate("send", "w", legacy_brief("docs"), None)
    assert "[brief-order]" in str(refused.value)


@pytest.mark.parametrize("value", [3, "2", True, None, 2.0])
def test_an_unknown_marker_refuses_a_brief_send(cw: ModuleType, value: object) -> None:
    write_registry(cw, {"status": "idle", cw.BRIEF_ORDER_KEY: value})
    with pytest.raises(SystemExit, match="unknown brief_order marker"):
        cw.lint_gate("send", "w", brief("docs"), None)
    cw.lint_gate("send", "w", "[QA FEEDBACK]\nfix the null check", None)  # feedback: no layout to check


def test_the_cli_legacy_order_flag(tmp_path: Path) -> None:
    footer = REPO / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md"
    old = tmp_path / "old.md"
    old.write_text(legacy_brief("docs"), encoding="utf-8")
    assert bl.main(["--brief", "--footer", str(footer), str(old)]) == 1
    assert bl.main(["--brief", "--legacy-order", "--footer", str(footer), str(old)]) == 0
    with pytest.raises(SystemExit) as refused:
        bl.main(["--legacy-order", str(old)])  # meaningless without --brief: refused, not ignored
    assert refused.value.code == 2


# S42 (R-025): a brief is shared text, so a `..` walk leaves the checkout with either separator on every host.
@given(
    ups=st.integers(min_value=1, max_value=3),
    seps=st.lists(st.sampled_from(["/", "\\"]), min_size=4, max_size=4),
    inside=st.sampled_from(["", "tasks/", "tasks\\"]),
)
def test_property_a_parent_walk_with_either_separator_is_refused(
    tmp_path_factory: pytest.TempPathFactory, ups: int, seps: list[str], inside: str
) -> None:
    root = tmp_path_factory.getbasetemp() / "s42-task-file" / "root"
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    walk = inside + "".join(".." + seps[i] for i in range(ups + (1 if inside else 0))) + "T.md"
    content, problem = bl.read_task_file(root, walk)
    assert content is None and problem is not None and "outside the checkout" in problem, walk
    kept, none = bl.read_task_file(root, inside + "T.md")  # a path inside is no finding (absent file: no text)
    assert none is None, kept
