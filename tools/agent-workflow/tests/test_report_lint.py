"""Tests for the report lint (T-021 phase P2, rebuilt on markdown-it-py): report_lint.py and its turn-end call in
cli_worker.py.

Unit rows per rule, property tests of the section and table reading (random tables, separators, CRLF, BOM, fences and
headings inside containers), the parser's limits (the inline cap, maxNesting, the version pin), the running time, the
standalone CLI, and the turn-end record. Most tests come unchanged from the reviewed P2 build; a test whose expectation
changed because the parser now decides what the old hand-written reader refused says so in its docstring
("rebuild:"). The worker process of the turn tests is a labelled test double: a tiny Python script in place of the
Claude CLI.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import re
import string
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import git
from hypothesis import given, settings
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rl = load(TOOLS / "report_lint.py", "report_lint_t_p2")
cw = load(TOOLS / "cli_worker.py", "cli_worker_report_lint")

HEADER = "| Question | Source | Read | Finding | Class |\n|---|---|---|---|---|"
GOOD_ROW = "| q | `tools/agent-workflow/brief_lint.py:374-382` | raw | f | source-verified |"
GOOD_FENCE = "```text\n# pytest\n12 passed in 1.0s\n```"
LABEL = "- **Result and evidence:**"


def report(
    *,
    label: str | None = LABEL,
    fence: str | None = GOOD_FENCE,
    heading: str | None = "### Research evidence",
    header: str = HEADER,
    rows: tuple[str, ...] = (GOOD_ROW,),
    title: str = "## Completion report",
    gap: str = "",
) -> str:
    """A task file with one Completion report; `None` drops a part. `gap` goes between the label and the fence."""
    parts = [f"# T-000: x\n\n## Goal\n\ng\n\n{title}\n\n- **Work completed:** w"]
    if label is not None:
        parts.append(label)
    if gap:
        parts.append(f"\n{gap}")
    if fence is not None:
        parts.append(f"\n{fence}")
    if heading is not None:
        parts.append(f"\n{heading}\n\n{header}\n" + "\n".join(rows))
    return "\n".join(parts) + "\n\n### Configuration audit\n\n| a |\n|---|\n| b |\n\n## Handoff\n\n- **Current status:** s\n"


def rules(text: str) -> list[str]:
    return [f.rule for f in rl.lint_task_file(text)]


def test_a_complete_report_is_clean() -> None:
    assert rl.lint_task_file(report()) == []


TEMPLATE = rl.load_template()


def test_unwritten_sections_are_skipped_and_listed() -> None:
    assert rl.lint_report("# T\n\n## Goal\n\ng\n") == ([], [])  # no section at all
    for blank in ("## Completion report\n", "## Completion report\n\n\n", "## Completion report\n\n<!-- later -->\n\n## Handoff\n"):
        assert rl.lint_report(blank) == ([], [1])  # a heading and blank lines or a comment: not written yet
    assert rl.lint_report("## Completion report\n\n### Research evidence\n\n## Handoff\n") == ([], [1])  # headings only


def test_prose_only_reports_are_written_and_linted() -> None:
    """C-F2/G-F2: the lint's own required structure (heading, fence, table) must not decide that a report exists."""
    prose = "# T\n\n## Completion report\n\n- **Work completed:** Implemented P2.\n- **Result and evidence:** All passed.\n\n## Handoff\n"
    found = rl.lint_task_file(prose)
    assert {f.rule for f in found} == {"done-check-output", "evidence-table"}
    assert rl.lint_report(prose).unwritten == []
    for body in ("text only", "> quoted", "- item", "<div>x</div>", "    indented", "<!-- c -->\ntext", "[link]: x"):
        assert rl.lint_task_file(f"## Completion report\n\n{body}\n"), body  # any content outside headings, comments
    assert rl.lint_task_file("## Completion report\n\n```\n```\n") != []  # an empty fence is content


def test_the_untouched_template_is_unwritten_and_one_edit_makes_it_written() -> None:
    assert TEMPLATE is not None  # the checkout's template: the same file the lint reads
    start = TEMPLATE.index("## Completion report")
    copy = TEMPLATE[start : TEMPLATE.index("## Handoff")]
    assert rl.lint_report("# T\n\n" + copy, TEMPLATE) == ([], [3])
    assert rl.lint_report("# T\n\n" + copy.replace("\n", "\r\n") + "\n\n", TEMPLATE) == ([], [3])  # CRLF, blank tail
    assert rl.lint_task_file("# T\n\n" + copy.replace("- **Work completed:**", "- **Work completed:** w"), TEMPLATE)
    assert rl.lint_report("# T\n\n" + copy).unwritten == []  # no template given: nothing counts as untouched
    assert rl.lint_task_file("# T\n\n" + copy.replace("## Completion report", "## Completion report (phase 1)"), TEMPLATE)


@pytest.mark.parametrize(
    ("kwargs", "rule", "line_text"),
    [
        ({"label": None}, "done-check-output", "no `Result and evidence` label"),
        ({"fence": "```text\n\n  \n```"}, "done-check-output", "no non-empty fenced code block"),
        ({"fence": "```text\n```"}, "done-check-output", "no non-empty fenced code block"),
        ({"fence": None}, "done-check-output", "no non-empty fenced code block"),
        ({"gap": "### Notes\n"}, "done-check-output", "no non-empty fenced code block"),  # the fence is past a heading
        ({"heading": None}, "evidence-table", "no `Research evidence` heading"),
        ({"rows": ()}, "evidence-table", "no data row"),
        ({"rows": ("|  |  |  |  |  |",)}, "evidence-table", "no data row"),  # the template's placeholder row
        (
            {"header": "| Q | Source | Class |\n|---|---|---|", "rows": ("| q | `x:1` | source-verified |",)},
            "evidence-table",
            "3 columns",
        ),
        ({"rows": ("| q | `x:1` | raw | source-verified |",)}, "evidence-table", "4 cells"),
        ({"rows": ("| q | `x:1` | raw | f | a | b |",)}, "evidence-table", "6 cells"),
        ({"rows": ("| q | `x:1` | raw | f | proven |",)}, "evidence-table", "'proven'"),
        ({"rows": ("| q | `x:1` | raw | f |  |",)}, "evidence-table", "''"),
        ({"rows": ("| q | the docs | raw | f | documented |",)}, "evidence-pointer", "no pointer"),
        ({"rows": ("| q |  | raw | f | documented |",)}, "evidence-pointer", "no pointer"),
    ],
)
def test_each_rule_fails_alone(kwargs: dict[str, Any], rule: str, line_text: str) -> None:
    found = rl.lint_task_file(report(**kwargs))
    assert {f.rule for f in found} == {rule}, found
    assert any(line_text in f.message for f in found), found


def test_a_heading_with_no_table_fails_and_the_line_points_at_it() -> None:
    text = report(rows=())
    text = text.replace("| Question | Source | Read | Finding | Class |\n|---|---|---|---|---|\n", "none\n")
    found = rl.lint_task_file(text)
    heading = text.split("\n").index("### Research evidence") + 1
    assert [(f.rule, f.line) for f in found] == [("evidence-table", heading)]


def test_findings_name_the_line_of_the_offending_row() -> None:
    text = report(rows=(GOOD_ROW, "| q | nothing | raw | f | documented |", GOOD_ROW))
    (finding,) = rl.lint_task_file(text)
    assert text.split("\n")[finding.line - 1].startswith("| q | nothing")
    assert finding.rule == "evidence-pointer"


@pytest.mark.parametrize(
    "cell",
    [
        "tools/x.py:12",
        "see tools/x.py:12-40 for it",
        "(brief_lint.py:7)",
        "docs/note/N127-long-context.md:3,",
        "D:\\proj\\x.py:5",
        "a.py:12:3",
        "https://platform.claude.com/docs/en/x",
        "HTTP://Example.com",
        "`pytest -q`",
        "``a ` b``",
        "run `ruff check` then",
        "Makefile.am:3",
        ".gitignore:5",
        "src\\x:3",
        "\\\\`x\\\\`",  # `\\` is an escaped backslash, so the backticks after it are a real span
        "\\`a `b`",  # the first backtick is escaped; the next two make a span
    ],
)
def test_pointer_forms_are_accepted(cell: str) -> None:
    assert rl._has_pointer(cell), cell


@pytest.mark.parametrize(
    "cell",
    [
        "", "the docs", "12:30", "x.py:", "x.py:abc", "see x.py line 5", "``", "` `", "`unclosed", "https://", "a ` b ``",
        "Note:1", "Step:0", "Decision:91", "R:021", "v1.2:3", "see Note:1-2",  # G-F3: a citation is no file
        "\\`the docs\\`",  # C-F5: escaped backticks are literal text
        "\\``the docs\\``",  # an escaped first backtick leaves a run of one: no closer of that length
        "\\\\\\`x\\\\\\`",  # three backslashes: the backtick stays escaped
    ],
)
def test_other_cells_are_not_pointers(cell: str) -> None:
    assert not rl._has_pointer(cell), cell


def test_the_class_vocabulary_is_the_templates_and_case_is_folded() -> None:
    template = (TOOLS.parents[1] / ".agents" / "policy" / "AGENT_TASK_TEMPLATE.md").read_text(encoding="utf-8")
    assert "Class: " + " / ".join(rl.CLASSES) in template
    assert rl.lint_task_file(report(rows=("| q | `x:1` | raw | f | Source-Verified |",))) == []


def test_heading_forms_label_forms_and_a_blocked_section() -> None:
    assert rules(report(heading="### Research evidence ###")) == []  # ATX closing run
    # A Setext heading is level 1 or 2, so it ends the section (the title is not an evidence heading of the report).
    assert rules(report(heading="Research evidence\n-----")) == ["evidence-table"]
    assert rules(report(label=None, gap="### Result and evidence:\n")) == []  # a heading label, colon allowed
    assert rules(report(label="**Result and evidence**:")) == []  # a bare bold label
    assert rules(report(title="Completion report\n-----")) == []  # a Setext section heading
    # A report heading may carry a suffix (C-F1): it is linted like the plain one.
    assert rules(report(title="## Completion report: fix round 1", fence=None)) == ["done-check-output"]
    assert rules(report(title="## Completion report: fix round 1")) == []


def test_a_heading_inside_a_fence_ends_nothing() -> None:
    fence = "```text\n## Handoff\nResearch evidence\n---\n# x\n```"
    assert rl.lint_task_file(report(fence=fence)) == []
    assert rl.lint_task_file(report(fence="~~~~\n```\n## Result and evidence\n~~~~")) == []  # a tilde fence is read


def test_a_label_inside_a_fence_is_no_label() -> None:
    text = report(label=None, fence="```text\n- **Result and evidence:**\n12 passed\n```")
    (finding,) = rl.lint_task_file(text)
    assert finding.rule == "done-check-output" and "no `Result and evidence` label" in finding.message


def test_an_unclosed_fence_runs_to_the_end_of_the_file() -> None:
    text = "## Completion report\n\n- **Result and evidence:**\n\n```text\n12 passed"  # no closing line, no final newline
    assert rules(text) == ["evidence-table"]  # the output counts; only the evidence table is missing
    assert rules(text.replace("12 passed", "")) == ["evidence-table", "done-check-output"]  # sorted by line


def test_a_fence_in_the_labels_list_item_or_a_quote_is_the_done_check_output() -> None:
    """Rebuild (replaces `test_a_fence_in_a_container_is_not_read_and_the_finding_says_so`, PM answer Q2): the parser
    reads a fence nested in the label's list item or in a block quote, so it is the done-check output."""
    assert rl.lint_task_file(report(fence="  ```text\n  12 passed\n  ```")) == []  # under the list item of the label
    assert rl.lint_task_file(report(fence="> ```text\n> 12 passed\n> ```")) == []
    (plain,) = rl.lint_task_file(report(fence=None))
    assert plain.rule == "done-check-output" and "no non-empty fenced code block" in plain.message


def test_a_table_in_a_fence_or_list_is_no_table() -> None:
    fenced = report(heading="### Research evidence\n\n```text\n" + HEADER + "\n" + GOOD_ROW + "\n```", header="", rows=())
    assert {f.message for f in rl.lint_task_file(fenced)} == {"no table under the `Research evidence` heading"}
    listed = report(rows=(), header="- " + HEADER.replace("\n", "\n  "))
    assert {f.rule for f in rl.lint_task_file(listed)} == {"evidence-table"}


def test_every_research_evidence_heading_and_result_label_is_checked() -> None:
    twice = report().replace("## Handoff", "### Research evidence\n\nnone\n\n## Handoff")
    assert [f.rule for f in rl.lint_task_file(twice)] == ["evidence-table"]
    two_labels = report().replace("### Configuration audit", f"{LABEL}\n\n### Configuration audit")
    assert [f.rule for f in rl.lint_task_file(two_labels)] == ["done-check-output"]


def test_an_escaped_pipe_stays_in_its_cell() -> None:
    assert rl.lint_task_file(report(rows=("| q | `a \\| b` | raw | f | documented |",))) == []
    # An unescaped pipe splits the cell: six cells, and the source cell is left holding a lone backtick.
    assert rules(report(rows=("| q | `a | b` | raw | f | documented |",))) == ["evidence-pointer", "evidence-table"]


def test_text_after_the_section_is_not_read() -> None:
    assert rl.lint_task_file(report() + "\n### Research evidence\n\nnone\n") == []  # under `## Handoff`


# --- fix round 1: report discovery, written, visibility, pointers (reviews C-F1..C-F5, G-F1..G-F4) ---------------------


def line_of(text: str, start: str) -> int:
    return next(n for n, line in enumerate(text.split("\n"), 1) if line.startswith(start))


def bare_report(label: str) -> str:
    """A report whose work line is a paragraph, not a list item: a blank line then four spaces is indented code there,
    while after a list item it would continue the item."""
    return report(label=None).replace("- **Work completed:** w", "w\n" + label)


NESTED_BODY = "\n### Phase P2 fix round 1\n\n- **Work completed:** Replaced the parser.\n- **Current status:** Done.\n"
NESTED_GOOD = (
    "\n### Phase P3\n\n- **Work completed:** w\n" + LABEL + f"\n\n{GOOD_FENCE}\n\n### Research evidence\n\n{HEADER}\n{GOOD_ROW}\n"
)


@pytest.mark.parametrize(
    "title",
    [
        "## Completion report (phase 1)",
        "## Completion report: fix round 1",
        "## Completion report(phase 1)",  # `(` and `:` need no space before them
        "## Completion report - round 2",
        "## Completion Report (x)",
        "## COMPLETION REPORT:",
        "## Completion report (phase 1) ##",
        "Completion report (phase 1)\n-----",
    ],
)
def test_suffixed_report_titles_are_reports_and_linted(title: str) -> None:
    text = report(title=title, label=None)
    assert [(f.rule, f.line) for f in rl.lint_task_file(text)] == [("done-check-output", line_of(text, title.split("\n")[0]))]
    assert rl.lint_task_file(report(title=title)) == []
    suffixed = "## Completion report (phase 1)\n\n- **Result and evidence:**\n\n\n### Research evidence\n\nnone\n"
    assert {f.rule for f in rl.lint_task_file(suffixed)} == {"done-check-output", "evidence-table"}  # review case


@pytest.mark.parametrize(
    "title", ["## Completion reports", "## Completion report-2", "# Completion report", "### Completion report", "## Completion reportx"]
)
def test_a_heading_that_looks_like_a_report_but_is_not_one_is_a_finding_never_skipped(title: str) -> None:
    text = f"# T\n\n{title}\n\n- **Work completed:** w\n"
    (finding,) = rl.lint_task_file(text)
    assert finding.rule == "report-section" and finding.line == 3 and "not a report heading" in finding.message


def test_each_nested_phase_report_is_linted_on_its_own() -> None:
    base = report()
    later = base.replace("\n## Handoff", NESTED_BODY + "\n## Handoff")
    found = rl.lint_task_file(later)  # the review case: the earlier report's checks are not borrowed
    nested = line_of(later, "### Phase P2 fix round 1")
    assert [(f.rule, f.line) for f in found] == [("done-check-output", nested), ("evidence-table", nested)]
    assert rl.lint_task_file(base.replace("\n## Handoff", NESTED_GOOD + "\n## Handoff")) == []
    both = base.replace("\n## Handoff", NESTED_GOOD + NESTED_BODY + "\n## Handoff")
    assert {f.line for f in rl.lint_task_file(both)} == {line_of(both, "### Phase P2 fix round 1")}
    titled = base.replace("\n## Handoff", "\n### Completion report (phase 3)\n\ntext\n\n## Handoff")  # a nested title heading
    assert [f.line for f in rl.lint_task_file(titled)] == [line_of(titled, "### Completion report (phase 3)")] * 2


def test_a_result_heading_or_a_blank_parent_does_not_split_a_report() -> None:
    assert rl.lint_task_file(report(label=None, gap="### Result and evidence:\n")) == []  # a label heading, not a report
    parent = "## Completion report\n\n### Phase P1\n\n- **Work completed:** w\n"
    assert {f.line for f in rl.lint_task_file(parent)} == {3}  # the empty parent is silent; the phase is linted
    assert rl.lint_report(parent).unwritten == []


def test_an_unwritten_nested_report_is_listed() -> None:
    text = report().replace("\n## Handoff", "\n### Completion report (phase 3)\n\n<!-- later -->\n\n## Handoff")
    assert rl.lint_report(text) == ([], [line_of(text, "### Completion report (phase 3)")])


def test_tables_in_indented_code_are_no_tables() -> None:
    """C-F3: a four-space indented table is a code example, not the evidence table."""
    table = HEADER + "\n" + GOOD_ROW
    text = report(rows=(), header="\n".join("    " + line for line in table.split("\n")))
    assert [(f.rule, "no table" in f.message) for f in rl.lint_task_file(text)] == [("evidence-table", True)]
    tabbed = report(rows=(), header="\n".join("\t" + line for line in table.split("\n")))
    assert {f.rule for f in rl.lint_task_file(tabbed)} == {"evidence-table"}
    assert rl.lint_task_file(report(rows=(), header="\n".join("   " + line for line in table.split("\n")))) == []  # 3 is fine


@pytest.mark.parametrize(
    "label",
    [
        "<!--\n- **Result and evidence:**\n-->",  # C-F4
        "\n    **Result and evidence:**",  # indented code
        "\n    - **Result and evidence:**",
        "<div>\n- **Result and evidence:**\n</div>",  # an HTML block
        "<!-- x\n**Result and evidence:** -->",
        "<!--\n```text\n- **Result and evidence:**\n```\n-->",
    ],
)
def test_a_label_in_a_comment_an_html_block_or_indented_code_is_no_label(label: str) -> None:
    text = bare_report(label)
    assert [(f.rule, "no `Result and evidence` label" in f.message) for f in rl.lint_task_file(text)] == [
        ("done-check-output", True)
    ]


def test_a_label_in_an_inline_comment_that_spans_lines_is_hidden() -> None:
    text = report(label=None).replace("- **Work completed:** w", "- **Work completed:** w <!--\n**Result and evidence:**\n-->")
    assert [f.rule for f in rl.lint_task_file(text)] == ["done-check-output"]
    closed = report().replace("- **Work completed:** w", "- **Work completed:** w <!-- c -->")
    assert rl.lint_task_file(closed) == []  # a closed comment before the label hides nothing


def test_a_label_in_a_list_item_is_the_templates_own_form() -> None:
    assert rl.lint_task_file(report(label="  - **Result and evidence:**")) == []
    assert rl.lint_task_file(report(label="* **Result and evidence:**")) == []
    assert rl.lint_task_file(report(label="\n- **Result and evidence:**")) == []


def test_a_bold_title_inside_a_line_is_no_label() -> None:
    """A label starts its block or one of its lines (after emphasis delimiters, which leave empty text tokens); the
    same bold words later in a line are prose (the rebuild's mutant M5)."""
    for label in ("- see **Result and evidence:**", "text *x* **Result and evidence:**", "a\nb **Result and evidence:**"):
        (finding,) = rl.lint_task_file(report(label=label))
        assert finding.rule == "done-check-output" and "no `Result and evidence` label" in finding.message, label
    assert rl.lint_task_file(report(label="a\n**Result and evidence:** b")) == []  # the start of a later line


def test_every_table_under_the_evidence_heading_is_checked() -> None:
    """G-F4: a second table is evidence too."""
    bad = "| q | nothing | raw | f | documented |"
    two = report(rows=(GOOD_ROW, "", HEADER, bad))
    (finding,) = rl.lint_task_file(two)
    assert finding.rule == "evidence-pointer" and two.split("\n")[finding.line - 1] == bad
    three = report(rows=(GOOD_ROW, "", HEADER, GOOD_ROW, "", HEADER, "| q | `x:1` | raw | f | proven |"))
    assert [f.rule for f in rl.lint_task_file(three)] == ["evidence-table"]
    assert rl.lint_task_file(report(rows=(GOOD_ROW, "", HEADER, GOOD_ROW))) == []


@pytest.mark.parametrize("source", ["Note:1", "Step:0", "Decision:91", "R:021", "see Note:1-2"])
def test_a_citation_that_is_no_file_is_no_pointer(source: str) -> None:
    (finding,) = rl.lint_task_file(report(rows=(f"| q | {source} | raw | f | documented |",)))
    assert finding.rule == "evidence-pointer"


def test_escaped_backticks_are_not_a_code_span() -> None:
    """C-F5: a backslash-escaped backtick is literal text."""
    (finding,) = rl.lint_task_file(report(rows=("| q | \\`the docs\\` | raw | f | documented |",)))
    assert finding.rule == "evidence-pointer"
    assert rl.lint_task_file(report(rows=("| q | \\\\`cmd` | raw | f | documented |",))) == []  # an escaped backslash


# --- property tests of the section and table reading ---------------------------------------------------------------

LETTERS = string.ascii_letters + " "
words = st.text(LETTERS, min_size=0, max_size=12)
stems = st.text(string.ascii_letters + string.digits + "_", min_size=1, max_size=8)
names = st.one_of(  # a path with a separator or an extension, the forms the lint accepts
    st.builds(lambda d, f: f"{d}/{f}", stems, stems),
    st.builds(lambda f, e: f"{f}.{e}", stems, st.text(string.ascii_lowercase, min_size=1, max_size=4)),
)
pointers = st.one_of(
    st.builds(lambda p, n: f"{p}:{n}", names, st.integers(1, 9999)),
    st.builds(lambda p, a, b: f"{p}:{a}-{b}", names, st.integers(1, 99), st.integers(100, 999)),
    st.builds(lambda h: f"https://{h}.example/x", st.text(string.ascii_lowercase, min_size=1, max_size=8)),
    st.builds(lambda c: f"`{c}`", st.text(string.ascii_letters + " -", min_size=1, max_size=10).filter(str.strip)),
)
classes = st.sampled_from(rl.CLASSES).flatmap(lambda c: st.sampled_from([c, c.upper(), c.title(), f" {c} "]))
eols = st.sampled_from(["\n", "\r\n"])
boms = st.sampled_from(["", "\ufeff"])


class Table:
    """A drawn evidence table: the cells of its data rows and the style it is written in."""

    def __init__(self, rows: list[list[str]], lead: bool, trail: bool, pad: str) -> None:
        self.rows, self.lead, self.trail, self.pad = rows, lead, trail, pad

    def line(self, cells: list[str]) -> str:
        body = "|".join(f"{self.pad}{c}{self.pad}" for c in cells)
        return ("|" if self.lead else "") + body + ("|" if self.trail else "")

    def lines(self) -> list[str]:
        head = [self.line(["Question", "Source", "Read", "Finding", "Class"]), self.line(["---", ":--", "--:", ":-:", "---"])]
        return head + [self.line(cells) for cells in self.rows]

    def task_file(self) -> str:
        return report(rows=(), header="\n".join(self.lines()), heading="### Research evidence")


@st.composite
def tables(draw: st.DrawFn) -> Table:
    """Random data rows (a pointer inside the source cell, a class in any case) with optional edge pipes, padding."""
    rows = [
        [draw(words).strip() or "q", f"{draw(words)} {draw(pointers)} {draw(words)}", draw(words), draw(words), draw(classes)]
        for _ in range(draw(st.integers(1, 5)))
    ]
    return Table(rows, draw(st.booleans()), draw(st.booleans()), draw(st.sampled_from(["", " ", "  "])))


@given(tables(), eols, boms)
def test_property_a_valid_table_is_clean_in_any_encoding_of_the_lines(table: Table, eol: str, bom: str) -> None:
    assert rl.lint_task_file(bom + table.task_file().replace("\n", eol)) == []


@pytest.mark.parametrize(("column", "rule"), [(4, "evidence-table"), (1, "evidence-pointer")])
@given(table=tables(), data=st.data())
def test_property_one_bad_cell_is_exactly_one_finding_on_its_row(
    column: int, rule: str, table: Table, data: st.DataObject
) -> None:
    victim = data.draw(st.integers(0, len(table.rows) - 1))
    # Letters and spaces only: no pointer form, and (filtered) no class.
    table.rows[victim][column] = data.draw(words.filter(lambda s: s.strip().casefold() not in rl.CLASSES))
    text = table.task_file()
    (finding,) = rl.lint_task_file(text)
    assert finding.rule == rule
    assert text.split("\n")[finding.line - 1] == table.lines()[2 + victim]


@given(eols, boms, st.integers(0, 4))
def test_property_the_same_findings_in_every_line_ending_and_with_a_bom(eol: str, bom: str, defect: int) -> None:
    kwargs: list[dict[str, Any]] = [
        {"label": None},
        {"fence": "```\n\n```"},
        {"heading": None},
        {"rows": ("| q | nothing | raw | f | nope |",)},
        {"rows": ()},
    ]
    text = report(**kwargs[defect])
    assert rl.lint_task_file(bom + text.replace("\n", eol)) == rl.lint_task_file(text) != []


@given(
    st.lists(st.sampled_from(["# a", "## Completion report", "### Research evidence", "Setext\n===", "---", "## Handoff"]), max_size=6)
)
def test_property_headings_inside_a_fence_never_split_the_report(inner: list[str]) -> None:
    fence = "````text\n" + "\n".join(["ok", *inner]) + "\n```\n````"  # a longer fence holds a shorter delimiter
    assert rl.lint_task_file(report(fence=fence)) == []


@given(
    st.sampled_from([("- ", "  ", True), ("* ", "  ", True), ("1. ", "   ", True), ("> ", "> ", True), ("<div>\n", "", False), ("      ", "      ", False)]),
    st.booleans(),
)
def test_property_a_fence_in_a_container_counts_and_one_in_html_or_code_does_not(
    container: tuple[str, str, bool], tilde: bool
) -> None:
    """Rebuild (replaces `test_property_a_fence_under_a_container_is_never_taken_for_the_done_check`, PM answer Q2):
    a fence in a list item or a block quote is a fence for the parser and counts; inside an HTML block or indented code
    it is no fence (the fence follows the label's list item, whose content starts at column 2, so indented code there
    takes six spaces)."""
    opener, indent, counts = container
    mark = "~~~" if tilde else "```"
    found = rl.lint_task_file(report(fence=f"{opener}{mark}\n{indent}12 passed\n{indent}{mark}"))
    assert [f.rule for f in found] == ([] if counts else ["done-check-output"])


SUFFIXES = st.sampled_from(["", " (phase 1)", ": fix round 1", " (x) ", " - y", ":"])


@given(SUFFIXES, st.lists(st.booleans(), min_size=1, max_size=4), eols, boms)
def test_property_every_nested_report_is_judged_alone(suffix: str, valid: list[bool], eol: str, bom: str) -> None:
    """A phase report is clean when it has its own checks and gets findings on its own heading when it has not."""
    parts = [f"# T\n\n## Completion report{suffix}\n"]
    for number, good in enumerate(valid):
        parts.append(f"### Phase P{number}\n\n- **Work completed:** w\n")
        if good:
            parts.append(f"{LABEL}\n\n{GOOD_FENCE}\n\n### Research evidence\n\n{HEADER}\n{GOOD_ROW}\n")
    text = bom + "\n".join(parts).replace("\n", eol) + eol
    found = rl.lint_task_file(text)
    bad = [number for number, good in enumerate(valid) if not good]
    heading_lines = {n: line_of(text.replace(eol, "\n").lstrip(bom), f"### Phase P{n}") for n in bad}
    assert sorted(f.line for f in found) == sorted(line for line in heading_lines.values() for _ in range(2))
    assert {f.rule for f in found} <= {"done-check-output", "evidence-table"}


@given(st.lists(st.text(string.ascii_letters + " ", min_size=1, max_size=20).filter(str.strip), min_size=1, max_size=6))
def test_property_a_prose_only_body_is_written_and_fails_both_checks(lines: list[str]) -> None:
    text = "## Completion report\n\n" + "\n".join(line.strip() for line in lines) + "\n\n## Handoff\n"
    assert rl.lint_report(text, TEMPLATE).unwritten == []
    assert {f.rule for f in rl.lint_task_file(text)} == {"done-check-output", "evidence-table"}


# --- fix round 2: one structural visibility step for labels, pointers and code spans (RT1 to RT4) ---------------------

BARE = "**Result and evidence:**"
# Where the label is no label, nested inside a list item: `{m}` is a bullet marker, `{t}` the item's text, `{b}` the label.
CONTAINED_HIDDEN = [
    "{m} {t}\n\n  ```text\n  {b}\n  ```",  # RT1: a fence inside a list item
    "{m} {t}\n\n  ~~~\n  {b}\n  ~~~",
    "{m} {t}\n\n      {b}",  # indented code relative to the item
    "{m} {t}\n\n  <!--\n  {b}\n  -->",  # an HTML comment block in the item
    "{m} {t}\n\n  <div>\n  {b}\n  </div>",  # raw HTML in the item
    "{m} a\n\n  {m} {t}\n\n    ```\n    {b}\n    ```",  # two levels down
    "{m} a\n\n  {m} {t}\n\n        {b}",
    "1. {t}\n\n   ```\n   {b}\n   ```",  # an ordered item
    "{m}      {b}",  # a marker and five spaces: the item starts with indented code
    "{m} {t} <!--\n  {b}\n  -->",  # an inline comment over the lines of the item
    "{m} {t} `\n  {b}\n  `",  # a code span over them
    "> ```\n> {b}\n> ```",  # a fence in a block quote
    "{t} <!-- `x`\n{b}\n-->",  # a real comment hides the label (a backtick inside it opens no span)
    "{t} `\n{b}\n`",  # a code span over the lines of a paragraph
]
CONTAINED_VISIBLE = [  # the same label where Markdown shows it
    "{m} {b}",
    "  {m} {b}",
    "{m} {t}\n\n  {b}",
    "{m} {t}\n  {m} {b}",
    "{m} a\n\n  {m} {t}\n\n    {b}",
    "1. {t}\n\n   {b}",
    "{m} {t} <!-- c -->\n\n  {b}",
    "{m} {t} `<!--`\n\n  {b}",  # RT4: a code span holding the opener of a comment
    "{m} {t} `<!--` and `-->`\n  {b}",
    "{m} {t} `<!--`\n{b}",  # RT4 as the probe had it: the label right after the bullet that holds the code span
    "{t} `<!--`\n{b}",
    "{t} ``a <!-- b``\n{b}",
]
MARKERS = st.sampled_from(["-", "*", "+"])
ITEM_TEXT = st.text(string.ascii_letters, min_size=1, max_size=8)


def contained(block: str, marker: str, text: str, eol: str, bom: str) -> str:
    return bom + bare_report(block.format(m=marker, t=text, b=BARE)).replace("\n", eol)


@given(st.sampled_from(CONTAINED_HIDDEN), MARKERS, ITEM_TEXT, eols, boms)
def test_property_a_label_nested_in_code_html_or_a_comment_of_a_list_item_is_no_label(
    block: str, marker: str, text: str, eol: str, bom: str
) -> None:
    found = rl.lint_task_file(contained(block, marker, text, eol, bom))
    assert [(f.rule, "no `Result and evidence` label" in f.message) for f in found] == [("done-check-output", True)]


@given(st.sampled_from(CONTAINED_VISIBLE), MARKERS, ITEM_TEXT, eols, boms)
def test_property_a_label_shown_inside_a_list_item_still_counts(
    block: str, marker: str, text: str, eol: str, bom: str
) -> None:
    assert rl.lint_task_file(contained(block, marker, text, eol, bom)) == []


def test_a_hidden_label_gets_the_missing_label_finding_on_the_report_heading() -> None:
    """Rebuild (replaces `test_the_finding_for_a_hidden_label_names_its_line`): the old hint named the construct that
    hid a label-like line, read by the old line scanner; the parser gives no such candidate, so a hidden label is the
    plain missing-label finding at the report heading."""
    text = bare_report("- x\n\n  ```text\n  " + BARE + "\n  ```")
    (finding,) = rl.lint_task_file(text)
    assert finding.rule == "done-check-output" and "no `Result and evidence` label" in finding.message
    assert finding.line == line_of(text, "## Completion report")


HIDING_WRAPPERS = ["<!-- {p} -->", "<!--{p}-->", '<span title="{p}">x</span>', '<a href="{p}">x</a>', "<?{p}?>"]
SHOWING_WRAPPERS = ["{p}", "<!-- c --> {p}", "{p} <!-- c -->", "<b>x</b> {p}", "<!-- c -->{p}"]


@given(st.sampled_from(HIDING_WRAPPERS), pointers)
def test_property_a_pointer_or_span_hidden_in_a_comment_or_a_tag_is_no_pointer(wrapper: str, pointer: str) -> None:
    cell = wrapper.format(p=pointer)
    assert not rl._has_pointer(cell), cell
    (finding,) = rl.lint_task_file(report(rows=(f"| q | {cell} | raw | f | documented |",)))
    assert finding.rule == "evidence-pointer"


@given(st.sampled_from(SHOWING_WRAPPERS), pointers)
def test_property_a_pointer_beside_a_comment_or_a_tag_still_counts(wrapper: str, pointer: str) -> None:
    cell = wrapper.format(p=pointer)
    assert rl._has_pointer(cell), cell
    assert rl.lint_task_file(report(rows=(f"| q | {cell} | raw | f | documented |",))) == []


def test_a_comment_with_a_pointer_in_its_text_and_a_real_one_outside_counts() -> None:
    assert rl._has_pointer("<!-- `x` --> `y`")
    assert not rl._has_pointer("<!-- `x` -->  ")
    assert rl._has_pointer("`<!--` tools/x.py:12")  # RT4 in a cell: the code span opens no comment, the pointer shows


def test_a_bare_file_name_needs_a_dot_slash_and_the_finding_says_so() -> None:
    """RT3: the lint reads text, not the checkout, so `Makefile:3` stays refused (like `Note:1`) and `./Makefile:3` counts."""
    (finding,) = rl.lint_task_file(report(rows=("| q | Makefile:3 | raw | f | documented |",)))
    assert finding.rule == "evidence-pointer" and "`./Makefile:3`" in finding.message
    assert rl.lint_task_file(report(rows=("| q | ./Makefile:3 | raw | f | documented |",))) == []
    assert rl.lint_task_file(report(rows=("| q | Makefile.am:3 | raw | f | documented |",))) == []


HIDERS: list[tuple[str, Any]] = [  # (what is hidden, wrapper): the construct must not count as the real one
    ("label", lambda s: "<!--\n" + s + "\n-->"),
    ("label", lambda s: "<div>\n" + s + "\n</div>"),
    ("label", lambda s: "\n    " + s.removeprefix("- ")),
    ("table", lambda s: "\n".join("    " + line for line in s.split("\n"))),
    ("table", lambda s: "<!--\n" + s + "\n-->"),
    ("table", lambda s: "<div>\n" + s + "\n</div>"),
    ("table", lambda s: "- " + s.replace("\n", "\n  ")),
    ("table", lambda s: "> " + s.replace("\n", "\n> ")),
]


@given(st.sampled_from(HIDERS), eols)
def test_property_a_label_or_table_hidden_by_markdown_never_counts(hider: tuple[str, Any], eol: str) -> None:
    kind, wrap = hider
    text = bare_report(wrap(LABEL)) if kind == "label" else report(rows=(), header=wrap(HEADER + "\n" + GOOD_ROW))
    expected = "done-check-output" if kind == "label" else "evidence-table"
    assert expected in {f.rule for f in rl.lint_task_file(text.replace("\n", eol))}


@given(st.integers(0, 6), st.text(string.ascii_letters, min_size=1, max_size=6))
def test_property_a_backtick_is_escaped_by_an_odd_number_of_backslashes(backslashes: int, body: str) -> None:
    assert rl._has_code_span("\\" * backslashes + f"`{body}`") == (backslashes % 2 == 0)


@given(st.lists(st.sampled_from(["good", "bad"]), min_size=1, max_size=4))
def test_property_every_evidence_table_is_checked(kinds: list[str]) -> None:
    bad_row = "| q | nothing | raw | f | documented |"
    blocks = [HEADER + "\n" + (GOOD_ROW if kind == "good" else bad_row) for kind in kinds]
    text = report(rows=(), header="\n\n".join(blocks))
    assert [f.rule for f in rl.lint_task_file(text)] == ["evidence-pointer"] * kinds.count("bad")


@settings(max_examples=300)
@given(st.lists(st.sampled_from(["#", "##", "###", "|", "---", "`", "```", "~~~", ">", "- ", "*", "\\|", " ", "a", "1.", "**Result and evidence:**", "Research evidence", "https://x", "x.py:1"]), max_size=40), st.integers(0, 3))
def test_property_arbitrary_markup_never_crashes_and_lines_are_in_range(parts: list[str], breaks: int) -> None:
    body = "".join(p + ("\n" if i % (breaks + 2) == 0 else " ") for i, p in enumerate(parts))
    text = "## Completion report\n\n### Research evidence\n\n" + body
    lines = text.count("\n") + 1
    found = rl.lint_task_file(text)
    assert all(1 <= f.line <= lines for f in found)
    assert [(f.line, f.rule) for f in found] == sorted((f.line, f.rule) for f in found)


# --- fix round 3: every CommonMark construct is read exactly (R2F1, R2F2, the class audit) ---------------------------

RETEST_BASE = (  # the retest 2 reproducer's report (.tmp/T-021/retest2-p2/regressions.py)
    "## Completion report\n\n{body}\n\n```text\n12 passed\n```\n\n### Research evidence\n\n"
    "| Q | Source | Read | Finding | Class |\n|---|---|---|---|---|\n| q | tools/x.py:12 | raw | f | source-verified |\n"
)
RETEST_2 = {  # name: (body, whether the label shows)
    "valid_thematic_star": ("example `\n***\n{b}\nend `", True),  # R2F2
    "valid_thematic_underscore": ("example ``\n___\n{b}\nend ``", True),
    "valid_thematic_star_comment": ("example <!--\n***\n{b}\n-->", True),
    "valid_thematic_inside_list": ("- example `\n\n  a `\n  ***\n  {b}\n  end `", True),
    "hidden_multiline_tag": ('text <span\ntitle="\n{b}\n">x</span>', False),  # R2F1
    "hidden_multiline_tag_equals": ('text <span title\n= "\n{b}\n">x</span>', False),
    "hidden_list_multiline_tag": ('- text <span\n  title="\n  {b}\n  ">x</span>', False),
}


def retest_report(body: str, eol: str = "\n", bom: str = "") -> str:
    return bom + RETEST_BASE.format(body=body.format(b=BARE)).replace("\n", eol)


@pytest.mark.parametrize("name", sorted(RETEST_2))
@pytest.mark.parametrize(("bom", "eol"), [("", "\n"), ("\ufeff", "\r\n")])
def test_the_retest_2_regressions(name: str, bom: str, eol: str) -> None:
    body, shown = RETEST_2[name]
    assert [f.rule for f in rl.lint_task_file(retest_report(body, eol, bom))] == ([] if shown else ["done-check-output"])


# One row per construct of the class audit that can hide or reveal a label: a body where it hides the label (or, for
# a construct that only ends or escapes something, the same body without it) and one where the label shows. `top`:
# also at the top level (a heading there would end the report section, so headings are checked inside an item).
CONSTRUCTS = [
    ("thematic break", "example `\n{b}\nend `", "example `\n***\n{b}\nend `", True),
    ("ATX heading", "a `\n{b}\n`", "a `\n# h\n{b}\n`", False),
    ("Setext heading", "a `\n{b}\n`", "a `\n===\n{b}\n`", False),
    ("indented code", "    {b}", "text\n    {b}", True),
    ("fenced code", "```\n{b}\n```", "```\nx\n```\n{b}", True),
    ("HTML block 1", "<pre>\n{b}\n</pre>", "<pre>x</pre>\n{b}", True),
    ("HTML block 2", "<!--\n{b}\n-->", "<!-- c -->\n{b}", True),
    ("HTML block 3", "<?x\n{b}\n?>", "<?x ?>\n{b}", True),
    ("HTML block 4", "<!X\n{b}\n>", "<!X >\n{b}", True),
    ("HTML block 5", "<![CDATA[\n{b}\n]]>", "<![CDATA[x]]>\n{b}", True),
    ("HTML block 6", "<div>\n{b}", "<div>\n\n{b}", True),
    ("HTML block 7", "<span>\n{b}", "<span>\n\n{b}", True),
    ("link reference definition", '[x]: /u "\n{b}\n"', "[x]: /u\n\n{b}", True),
    ("paragraph", "a `\n{b}\n`", "a `x`\n{b}\n`", True),
    ("blank line", "a `\n{b}\n`", "a `\n\n{b}\n`", True),
    # rebuild: a label inside a block quote is shown and counts (PM answer Q2); a fence in the quote still hides it
    ("block quote", "> ```\n> {b}\n> ```", "> {b}", True),
    ("list item", "- t `\n  {b}\n  `", "- t\n  {b}", True),
    ("lazy continuation", "- t `\n{b}\n`", "- t `x`\n{b}", True),
    ("backslash escape", "a `\n{b}\n`", "a \\`\n{b}\n`", True),
    ("entity reference", "a `\n{b}\n`", "a &#96;\n{b}\n`", True),
    ("code span", "a ``\n{b}\n``", "a ``x``\n{b}\n``", True),
    ("link", '[x](u "\n{b}\n")', "[\n{b}\n](u)", True),  # a title hides, link text shows
    ("image", "![\n{b}\n](x.png)", "![a](x.png)\n{b}", True),
    # rebuild: the parser resolves references, so the label of a defined full reference is hidden; without a
    # definition `[x][y]` is literal text and the label after it shows
    ("reference link", "[x][\n{b}\n]\n\n[{b}]: /u", "[x][y]\n{b}", True),
    ("autolink", "<http://x >`\n{b}\n`", "<http://x`>\n{b}\n`", True),  # an autolink holds the first backtick
    ("open tag", 'a <span\ntitle="\n{b}\n">x</span>', 'a <span\ntitle="t">x</span>\n{b}', True),
    ("HTML comment", "a <!--\n{b}\n-->", "a <!-- c -->\n{b}", True),
    ("processing instruction", "a <?\n{b}\n?>", "a <? c ?>\n{b}", True),
    ("declaration", "a <!X\n{b}\nx>", "a <!X c>\n{b}", True),  # a line that starts with `>` would open a quote
    ("CDATA", "a <![CDATA[\n{b}\n]]>", "a <![CDATA[c]]>\n{b}", True),
]


def placed(body: str, item: bool) -> str:
    return "- " + body.replace("\n", "\n  ") if item else body


@given(st.sampled_from(CONSTRUCTS), st.booleans(), eols, boms)
def test_property_every_construct_hides_or_shows_the_label_as_commonmark_does(
    row: tuple[str, str, str, bool], item: bool, eol: str, bom: str
) -> None:
    name, hidden, shown, top = row
    item = item or not top
    (finding,) = rl.lint_task_file(retest_report(placed(hidden, item), eol, bom))
    assert finding.rule == "done-check-output" and "no `Result and evidence` label" in finding.message, name
    assert rl.lint_task_file(retest_report(placed(shown, item), eol, bom)) == [], name


@settings(deadline=None)
@given(st.sampled_from([" ", "\n", "\t", " \n "]), st.sampled_from(["", " ", "\n", "\t"]), st.booleans(), eols, boms)
def test_property_a_tag_whose_attributes_run_over_lines_hides_its_value(
    gap: str, equals_gap: str, item: bool, eol: str, bom: str
) -> None:
    """R2F1 as the retest's property has it: whitespace inside a tag holds up to one line ending (6.6)."""
    body = "text <span" + gap + "title" + equals_gap + '= "\n{b}\n">x</span>'
    assert [f.rule for f in rl.lint_task_file(retest_report(placed(body, item), eol, bom))] == ["done-check-output"]
    shown = "text <span" + gap + "title" + equals_gap + '= "t">x</span>\n{b}'
    assert rl.lint_task_file(retest_report(placed(shown, item), eol, bom)) == []


@given(st.sampled_from(["***", "___", "* * *", "- - -", "_ _ _"]), st.integers(1, 4), st.booleans(), eols, boms)
def test_property_inline_state_never_crosses_a_thematic_break(
    marker: str, ticks: int, item: bool, eol: str, bom: str
) -> None:
    """R2F2: a break ends the paragraph, so its open backtick run cannot pair with one after the break (`---` right
    under a paragraph is a Setext underline instead, which would end the report section)."""
    run = "`" * ticks
    body = f"example {run}\n{marker}\n{{b}}\nend {run}"
    assert rl.lint_task_file(retest_report(placed(body, item), eol, bom)) == []
    joined = f"example {run}\n{{b}}\nend {run}"  # without the break the span runs over the label
    assert [f.rule for f in rl.lint_task_file(retest_report(placed(joined, item), eol, bom))] == ["done-check-output"]


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ("- a\n  - t `\n    {b}\n`", False),  # a lazy line after the outer item closes the inner item's span
        ("- a\n  - t `\n`\n{b}", True),  # the lazy line closes it before the label, which joins the same paragraph
        ("- t `\n  {b}\n`", False),
        ("- t\n\n  {b}\n`", True),
        ("> q `\n{b}\n`", False),  # the lazy line continues the quote's paragraph, and its span hides the label
        ("1. t `\n{b}\n`", False),
    ],
)
def test_a_lazy_continuation_line_joins_the_paragraph_it_continues(body: str, shown: bool) -> None:
    found = rl.lint_task_file(retest_report(body))
    assert [f.rule for f in found] == ([] if shown else ["done-check-output"])


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ('[a [b](u) c](v "\n{b}\n")', True),  # the inner link deactivates the outer `[`: its `(...)` is text
        ('[a [b] c](v "\n{b}\n")', False),  # without a definition for `b` the outer link's title holds the label
        ("[a [b] c](u)\n{b}", True),  # either reading: the label shows
        # rebuild: with no definition `[b]` is text, so the outer link is a link and the backtick in `<u`>` is in its
        # destination; the old lint refused these as unsure
        ("[a [b] c](<u`>)\n{b}\n`", True),
        ("[a [b] c](<u`>)\n{b}", True),
        # with a definition `[b]` is a link, the outer `[a` is deactivated, `(<u` is text and its backtick opens a span
        ("[a [b] c](<u`>)\n{b}\n`\n\n[b]: /x", False),
        ("[x][y]\n{b}", True),
        # rebuild: the inner links deactivate the outer `[a`, so its `(w "...")` is text and the label shows
        ('[a [x](u) [b [r] c](v) d](w "\n{b}\n")', True),
    ],
)
def test_a_link_inside_a_link_and_a_reference_are_read_both_ways(body: str, shown: bool) -> None:
    found = rl.lint_task_file(retest_report(body))
    assert [f.rule for f in found] == ([] if shown else ["done-check-output"])


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ('text <span\ntitle="\n{b}\n">x</span>', False),
        ('[x](u "\n{b}\n")', False),
        ("![\n{b}\n](x.png)", False),
        ('[x]: /u "\n{b}\n"', False),
        ("> q\n{b}", True),  # the lazy line continues the quote's paragraph; a label in a quote counts
        ("[x][a`b]\n{b}", True),  # no definition: literal text, the backtick has no closer
        ("    {b}", False),
        ("<div>\n{b}", False),
    ],
)
def test_a_construct_that_hides_the_label_gives_the_missing_label_finding(body: str, shown: bool) -> None:
    """Rebuild (replaces `test_the_finding_names_the_construct_that_hides_the_label`): the parser decides; the old
    hint naming the construct came from the old line scanner and is gone, and the quote and reference rows show."""
    found = rl.lint_task_file(retest_report(body))
    assert [(f.rule, "no `Result and evidence` label" in f.message) for f in found] == (
        [] if shown else [("done-check-output", True)]
    )


POINTER_CELLS = [  # (source cell, a pointer shows)
    ("[x](tools/a.py:3)", True),  # a link's destination is its target
    ("[x](<tools/a.py:3>)", True),
    ("[x](u) tools/a.py:3", True),
    ("[tools/a.py:3]", True),  # a shortcut reference shows its text with or without a definition
    ("\\[x](u 'tools/a.py:3')", True),  # an escaped bracket opens no link: the title is text
    ("<https://x.example/p>", True),
    ("&#96;tools/a.py:3&#96;", True),  # entity backticks open no span: the path is text
    ("[x](u 'tools/a.py:3')", False),  # a title is not shown
    ('[x](u "`cmd`")', False),  # a backtick in a title opens no span
    ("![tools/a.py:3](i.png)", False),  # an image description is not shown as text
    ("![x](tools/a.py:3)", False),
    ("![`cmd`](i.png)", False),
    # rebuild: with no definition a full reference is literal text, so the path in it shows (the old lint took the
    # safe reading of a matching definition)
    ("[x][tools/a.py:3]", True),
    ("[x][a`b] tools/a.py:3", True),
]


@pytest.mark.parametrize(("cell", "shown"), POINTER_CELLS)
def test_a_pointer_counts_only_where_a_link_or_image_shows_it(cell: str, shown: bool) -> None:
    assert rl._has_pointer(cell) == shown
    found = rl.lint_task_file(report(rows=(f"| q | {cell} | raw | f | documented |",)))
    assert [f.rule for f in found] == ([] if shown else ["evidence-pointer"])


def test_a_defined_reference_label_shows_no_pointer() -> None:
    """Rebuild: the parser resolves the definition, so the label of `[x][tools/a.py:3]` is hidden and the destination
    of the definition is the link's target."""
    text = report(rows=("| q | [x][tools/a.py:3] | raw | f | documented |",))
    assert rules(text + "\n[tools/a.py:3]: /nowhere\n") == ["evidence-pointer"]
    assert rules(text + "\n[tools/a.py:3]: tools/b.py:9\n") == []


def test_the_pointer_finding_names_the_construct_that_hides_it() -> None:
    (finding,) = rl.lint_task_file(report(rows=("| q | [x](u 'tools/a.py:3') | raw | f | documented |",)))
    assert "is in a link title, which shows no pointer" in finding.message
    (plain,) = rl.lint_task_file(report(rows=("| q | nothing | raw | f | documented |",)))
    assert "which shows no pointer" not in plain.message


def test_a_heading_is_read_by_the_title_it_shows() -> None:
    """A comment, an escape or an entity in a heading's source does not change the title a reader sees (6, 2.5).
    Rebuild: the old `_shown` assertion is dropped with that reader; `_plain` reads the parser's tokens."""
    assert rules(report(title="## <!-- c -->Completion report", label=None)) == ["done-check-output"]
    assert rules(report(title="## Completion&#32;report", label=None)) == ["done-check-output"]
    assert rl.lint_task_file(report(label="### <!-- c -->Result and evidence")) == []
    assert rules(report(title="## Completion report\\: x", label=None)) == ["done-check-output"]


@given(st.integers(1, 4), st.sampled_from(["", " "]))
def test_property_tabs_in_a_list_item_indent_by_tab_stops(tabs: int, lead: str) -> None:
    """The retest's M17: a tab is a tab stop (2.2), so two tabs under an item are indented code there."""
    found = rl.lint_task_file(retest_report("- item\n\n" + lead + "\t" * tabs + BARE))
    # The first tab reaches column 4 with or without a space before it; the item's content starts at column 2, so
    # indented code (4 more columns) starts at column 6: two tabs or more.
    assert [f.rule for f in found] == (["done-check-output"] if tabs >= 2 else [])


# --- fix round 4 cases (R3F1, R3F2, R3F3), read by the parser ---------------------------------------------------------

RETEST_3 = {  # the retest 3 cases (.tmp/T-021/retest3-p2/confirm.py): name -> (body, the label shows)
    "title_escaped_double": ('[x](u "a `\\")\n{b}\nend `', False),  # R3F1: `\"` does not close
    "title_escaped_single": ("[x](u 'a `\\')\n{b}\nend `", False),
    "title_even_control": ('[x](u "a `\\\\")\n{b}\nend `', True),  # `\\` escapes itself: the quote closes
    # rebuild (R3F2): markdown-it starts a new list at a marker in the column of the outer item, whatever its start
    # number or text (rules_block/list.py:130-135 sets `isTerminatingParagraph` only when the line is indented to the
    # paragraph's `blkIndent`, so the start-at-1 rule of :146 does not apply), so the paragraph ends and the label
    # shows; the old lint refused these as unsure
    "non1_ordered_lazy_span": ("- a `\n  {b}\n2. end\n`", True),
    "non1_ordered_lazy_html": ('- a <span title="\n  {b}\n2. end\n">x</span>', True),
    "empty_marker_lazy_span": ("- a `\n  {b}\n-\n`", True),
    "start1_control": ("- a `\n  {b}\n1. end\n`", True),  # a marker that may interrupt: a new list
    "unfinished_title": ('{b} [x](u "' + "\\" * 64 + ")", True),  # the round 3 watchdog case
}


@pytest.mark.parametrize("name", sorted(RETEST_3))
@pytest.mark.parametrize(("bom", "eol"), [("", "\n"), ("\ufeff", "\r\n")])
def test_the_retest_3_regressions(name: str, bom: str, eol: str) -> None:
    body, shown = RETEST_3[name]
    found = rl.lint_task_file(retest_report(body, eol, bom))
    assert [f.rule for f in found] == ([] if shown else ["done-check-output"])


HEADED = RETEST_BASE.replace("{body}", "no output")  # a written report with no label


@pytest.mark.parametrize(
    ("text", "rules_found"),
    [  # R3F3: a heading that shows the section title through markup is linted as a report, never skipped
        (HEADED.replace("## Completion report", "## **Completion report**"), ["done-check-output"]),
        ("[r]: /u\n\n" + HEADED.replace("## Completion report", "## [Completion report][r]"), ["done-check-output"]),
        (HEADED.replace("## Completion report", "## `  Completion report  `"), ["done-check-output"]),
        # rebuild: the parser resolves the reference, so `[Result and evidence][r]` is that heading and the fence
        # after it is the output (the old lint named it as unresolved)
        (RETEST_BASE.format(body="### [Result and evidence][r]") + "\n[r]: /u\n", []),
    ],
)
def test_the_retest_3_heading_cases(text: str, rules_found: list[str]) -> None:
    for variant in (text, "\ufeff" + text.replace("\n", "\r\n")):
        assert [f.rule for f in rl.lint_task_file(variant)] == rules_found


# The retest's three properties (.tmp/T-021/retest3-p2/test_new_contract.py), copied with parentheses added to the
# title kinds (a parenthesized title closes at `)`, and the link needs one `)` more).
NEW_CONTRACT_BASE = (
    "## Completion report\n\nBODY\n\n```text\n12 passed\n```\n\n### Research evidence\n\n"
    "| Q | S | R | F | C |\n|---|---|---|---|---|\n| q | tools/x.py:12 | raw | f | source-verified |\n"
)


def contract_rules(body: str, eol: str, bom: str) -> list[str]:
    return [f.rule for f in rl.lint_report(bom + NEW_CONTRACT_BASE.replace("BODY", body).replace("\n", eol)).findings]


@settings(max_examples=60, deadline=None)
@given(st.sampled_from(['"', "'", "("]), st.integers(1, 6), st.booleans(), eols, boms)
def test_property_link_title_closers_respect_backslash_parity(
    quote: str, slashes: int, item: bool, eol: str, bom: str
) -> None:
    """2.4, 6.3: an odd run of backslashes escapes the closer, so the link fails and the backtick in its would-be
    title pairs with the one after the label; an even run escapes itself and the title closes."""
    close = {"(": ")"}.get(quote, quote)
    body = "[x](u " + quote + "a `" + "\\" * slashes + close + ")\n" + BARE + "\nend `"
    if item:
        body = "- " + body.replace("\n", "\n  ")
    assert contract_rules(body, eol, bom) == (["done-check-output"] if slashes % 2 else [])


@settings(max_examples=50, deadline=None)
@given(st.integers(2, 99), st.sampled_from([". ", ") "]), eols, boms)
def test_property_an_ordered_marker_after_an_item_paragraph_starts_a_new_list(
    n: int, delimiter: str, eol: str, bom: str
) -> None:
    """Rebuild (replaces `test_property_noninterrupting_ordered_marker_does_not_close_item_paragraph`): the parser
    starts a new list there (see RETEST_3), so the span does not reach the closer and the label shows."""
    body = "- a `\n  " + BARE + "\n" + str(n) + delimiter + "end\n`"
    assert contract_rules(body, eol, bom) == []


@settings(max_examples=20, deadline=None)
@given(
    st.sampled_from(
        [
            "**Completion report**",
            "_Completion report_",
            "[Completion report][r]",
            "[Completion report][]",
            "[Completion report]",
            "![Completion report](i.png)",
            "![`Completion report`](i.png)",  # R4F1: code in an image description
            "![<b>Completion report</b>](i.png)",  # R4F1: HTML in an image description
            "![Completion `report`](i.png)",
            "![`Completion report`][r]",
        ]
    ),
    st.sampled_from(["## TITLE", "TITLE\n---"]),
)
def test_property_shown_report_heading_is_linted(title: str, form: str) -> None:
    """R3F3, R4F1: a heading that shows the section title through emphasis, a reference or an image (with code or HTML
    in its description) is linted as a report. Rebuild (replaces `test_property_shown_report_heading_is_linted_or_explicitly_refused`):
    `~~Completion report~~` left this list: strikethrough is no
    CommonMark construct, so its tildes are literal text and the heading shows no section title (open PM question)."""
    text = "[r]: /u\n[Completion report]: /u\n\n" + NEW_CONTRACT_BASE.replace("BODY", "no output").replace(
        "## Completion report", form.replace("TITLE", title)
    )
    assert rl.lint_report(text).findings, "written report was silently skipped"
    good = text.replace("no output", BARE)  # the same report, written in full: linted, and clean
    assert rl.lint_report(good).findings == []


@settings(max_examples=40, deadline=None)
@given(
    st.sampled_from([("**{t}**", True), ("_{t}_", True), ("*{t}*", True), ("[{t}][r]", True), ("![{t}](i.png)", True),
                     ("![`{t}`](i.png)", True), ("[{t}][]", False), ("[{t}]", False), ("~~{t}~~", False)]),
    st.sampled_from(["### ", "#### "]),
    st.sampled_from([rl.RESULT_TITLE, rl.EVIDENCE_TITLE]),
    eols,
    boms,
)
def test_property_a_title_through_markup_is_read_as_the_parser_shows_it(
    markup: tuple[str, bool], level: str, title: str, eol: str, bom: str
) -> None:
    """Rebuild (replaces `test_property_a_title_shown_only_through_markup_is_named`): the parser resolves emphasis,
    references and images, so a `Result and evidence` or `Research evidence` heading shown through them is that title
    (R4F1 for images). Without a definition for the title itself, `[title][]` and `[title]` are literal brackets, and
    `~~` is literal in CommonMark: those headings are no title, so the missing-title finding fires."""
    form, read = markup
    heading = level + form.format(t=title)
    if title == rl.RESULT_TITLE:
        text = NEW_CONTRACT_BASE.replace("BODY", "- **Work completed:** w\n\n" + heading)
    else:
        text = NEW_CONTRACT_BASE.replace("BODY", BARE).replace("### Research evidence", heading)
    text = "[r]: /u\n\n" + text
    found = [f.rule for f in rl.lint_report(bom + text.replace("\n", eol)).findings]
    assert found == ([] if read else ["done-check-output" if title == rl.RESULT_TITLE else "evidence-table"])


@settings(max_examples=150, deadline=None)
@given(
    st.sampled_from(["-", "*", "1.", "1)"]),
    st.sampled_from(["-", "*", "+", "1.", "2.", "7.", "1)", "2)", "01.", "001)"]),
    st.booleans(),
    st.sampled_from([("`", "`"), ('<span title="', '">x</span>')]),
    st.booleans(),
    eols,
    boms,
)
def test_property_a_marker_after_an_item_paragraph_ends_it(
    above: str, marker: str, empty: bool, state: tuple[str, str], nested: bool, eol: str, bom: str
) -> None:
    """Rebuild (replaces `test_property_a_marker_after_an_item_paragraph_splits_it_or_fails_closed`, R3F2): a span or
    tag opened in the paragraph that ends an item, a label, then a marker at the item's column and a closer. The parser
    starts a new item or list at every such marker (empty or not, any start number), so the label shows."""
    opening, closing = state
    pad = " " * (len(above) + 1)
    body = f"{above} a {opening}\n{pad}{BARE}\n{marker}{'' if empty else ' end'}\n{closing}"
    if nested:
        body = "- outer\n\n  " + body.replace("\n", "\n  ")
    assert rl.lint_report(bom + NEW_CONTRACT_BASE.replace("BODY", body).replace("\n", eol)).findings == []


@pytest.mark.parametrize(
    ("body", "found"),
    [
        ("- a `\n  {b}\n<span>\n`", ["done-check-output"]),  # kind 7 cannot interrupt: a lazy line, the span hides it
        ("a `\n01. x\n{b}\n`", []),  # `01.` starts at 1 and interrupts; the label joins the item's paragraph lazily
        ("> q\n2. x\n   - {b}", []),
    ],
)
def test_an_uncertain_split_of_the_old_lint_is_read_by_the_parser(body: str, found: list[str]) -> None:
    """Rebuild (replaces `test_an_uncertain_split_fails_closed_by_name`): the old named refusals are the parser's
    reading now; the 20-level list row moved to `test_a_container_at_max_nesting_is_named_never_read_as_absent`."""
    assert rules(retest_report(body)) == found


@pytest.mark.parametrize(
    "body",
    ["- a `\n  {b}\n<div>\n`", "1. a `\n   {b}\n2. end\n`", "- a `\n  {b}\n- end\n`", "a `\n1. x\n{b}\n`"],
)
def test_a_certain_split_still_shows_the_label(body: str) -> None:
    """Kinds 1-6 interrupt a paragraph; the same list's next item and a start at 1 split it: nothing fails closed."""
    assert rl.lint_task_file(retest_report(body)) == []


# --- the parser's limits: the version pin, the inline cap, maxNesting (rebuild) -----------------------------------------


def test_the_parser_version_is_the_pin_in_pyproject() -> None:
    """One source of truth (PM answer Q3): the lint's required version is the pin CI installs from `.[test]`."""
    project = tomllib.loads((TOOLS.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert f"{rl.PARSER_PACKAGE}=={rl.PARSER_VERSION}" in project["project"]["optional-dependencies"]["test"]
    import markdown_it

    assert markdown_it.__version__ == rl.PARSER_VERSION  # the environment running these tests has the pin


def test_another_parser_version_exits_2_and_names_both_versions(tmp_path: Path) -> None:
    """A different markdown_it package first on the import path (a labelled test double: a stub package with only a
    version) is refused with exit 2, never read with another parser."""
    stub = tmp_path / "stub" / "markdown_it"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    task = tmp_path / "task.md"
    task.write_text(report(), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-B", str(TOOLS / "report_lint.py"), str(task)],
        capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW, cwd=tmp_path, check=False,
        env={"PYTHONPATH": str(tmp_path / "stub"), "PYTHONNOUSERSITE": "1", "SYSTEMROOT": str(Path(sys.executable).anchor)},
    )
    assert proc.returncode == 2, proc.stderr
    assert "markdown-it-py==4.2.0 is required, markdown-it-py 9.9.9 is installed" in proc.stderr
    assert proc.stdout == ""


def test_a_missing_parser_raises_and_the_cli_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rl, "_PARSER_PROBLEM", "markdown-it-py==4.2.0 is required but is not installed (x)")
    with pytest.raises(rl.ParserUnavailable, match="is required but is not installed"):
        rl.lint_report(report())
    task = tmp_path / "task.md"
    task.write_text(report(), encoding="utf-8")
    assert rl.main([str(task)]) == 2


def capped(length: int) -> str:
    """A report whose label paragraph's inline content is exactly `length` characters."""
    head = BARE + " "
    return retest_report("{b} " + "x" * (length - len(head)))


def test_the_inline_cap_reads_a_block_at_the_cap_and_names_one_over_it() -> None:
    """PM answer Q1: `INLINE_MAX` characters are parsed; one more is a named finding, never a pass."""
    assert rules(capped(rl.INLINE_MAX)) == []
    found = rl.lint_task_file(capped(rl.INLINE_MAX + 1))
    assert [f.rule for f in found] == ["done-check-output", "report-section"]  # the label is not read, and why
    assert f"an inline block of {rl.INLINE_MAX + 1} characters, more than INLINE_MAX ({rl.INLINE_MAX})" in found[1].message
    cell = "tools/a.py:3 " + "x" * (rl.INLINE_MAX - len("tools/a.py:3 "))
    assert rules(report(rows=(f"| q | {cell} | raw | f | documented |",))) == []
    over = report(rows=(f"| q | {cell}x | raw | f | documented |",))
    assert [(f.rule, "INLINE_MAX" in f.message) for f in rl.lint_task_file(over)] == [("report-section", True)]


def test_a_heading_over_the_cap_is_a_finding_never_skipped() -> None:
    long_title = "## Completion report " + "x" * rl.INLINE_MAX
    (finding,) = [f for f in rl.lint_task_file(report(title=long_title)) if f.rule == "report-section"]
    assert "INLINE_MAX" in finding.message and finding.line == line_of(report(title=long_title), "## Completion report")


@pytest.mark.parametrize(("depth", "named"), [(18, False), (19, False), (20, True), (25, True)])
def test_a_container_at_max_nesting_is_named_never_read_as_absent(depth: int, named: bool) -> None:
    """The parser drops what a container nested to `maxNesting` holds (parser_block.py:75-79); the lint sees it from
    the token depth of `blockquote_open` and `list_item_open`, never from a character count."""
    found = rl.lint_task_file(retest_report("> " * depth + "{b}"))
    assert [f.rule for f in found] == (["done-check-output", "report-section"] if named else [])
    assert not named or "maxNesting" in found[1].message
    deep_list = "".join("  " * d + "- x\n" for d in range(20)) + "  " * 20 + "- {b}"
    assert "report-section" in rules(retest_report(deep_list))


@pytest.mark.parametrize(("brackets", "named"), [(5, False), (21, True)])
def test_a_link_nested_to_max_nesting_is_named_not_misread(brackets: int, named: bool) -> None:
    """At `maxNesting` the inline parser skips to the block's end and reads a link with a title as text
    (parser_inline.py:146-168), which would show the title's pointer; `_depth_guard` names it instead."""
    cell = "[" * brackets + "x](u 'tools/a.py:3')"
    found = rl.lint_task_file(report(rows=(f"| q | {cell} | raw | f | documented |",)))
    if named:
        assert [f.rule for f in found] == ["report-section"] and "does not resolve" in found[0].message
    else:
        assert [f.rule for f in found] == ["evidence-pointer"]


def test_a_report_heading_inside_a_container_is_a_finding_never_skipped() -> None:
    for text in ("- ## Completion report\n\n  - **Work completed:** w\n", "> ## Completion report\n>\n> w\n"):
        (finding,) = rl.lint_task_file(text)
        assert finding.rule == "report-section" and "inside a list item or block quote" in finding.message


def test_result_and_evidence_headings_read_through_an_image_description() -> None:
    """R4F1: code or HTML inside an image description in a Result or Evidence heading is read as its title."""
    assert rl.lint_task_file(report(label=None, gap="### ![`Result and evidence`](u)\n")) == []
    assert rl.lint_task_file(report(heading="### ![<b>Research evidence</b>](u)")) == []


# Running time (R4F2): the reviewer's shapes and every adversarial family grow linearly. `TIME_BOUND` and `SIZE` are
# test-only: each family at about 4000 characters finishes within the bound, and four times the input takes at most
# `RATIO` times as long (linear is about 4, quadratic 16).
TIME_BOUND = 1.0
SIZE = 4000
RATIO = 8.0


def _ladder(n: int) -> str:
    out, length = [], 1
    while sum(len(part) for part in out) < n:
        out.append("`" * length + " ")
        length += 1
    return "".join(out)


ADVERSARIAL = {  # family -> (body, evidence cell) of a report, at size n
    "title_backslashes_double": lambda n: (BARE + ' [x](u "' + "\\" * n + ")", None),
    "title_backslashes_single": lambda n: (BARE + " [x](u '" + "\\" * n + ")", None),
    "title_backslashes_paren": lambda n: (BARE + " [x](u (" + "\\" * n + ")", None),
    "titles_unclosed": lambda n: (BARE + ' [x](u "a `\\' * (n // 9), None),
    "nested_links": lambda n: (BARE + " " + "[" * (n // 5) + "](u)" * (n // 5), None),
    "nested_images": lambda n: (BARE + " " + "![" * (n // 6) + "](u)" * (n // 6), None),
    "brackets": lambda n: (BARE + " " + "[" * (n // 2) + "]" * (n // 2), None),
    "references_unclosed": lambda n: (BARE + " " + "[a][" * (n // 4), None),
    "backtick_ladder": lambda n: (BARE + " " + _ladder(n), None),
    "backtick_run": lambda n: (BARE + " " + "`" * n, None),
    "comments_unclosed": lambda n: (BARE + " " + "<!--" * (n // 4), None),
    "instructions_unclosed": lambda n: (BARE + " " + "<?" * (n // 2), None),
    "cdata_unclosed": lambda n: (BARE + " " + "<![CDATA[" * (n // 9), None),
    "declarations_unclosed": lambda n: (BARE + " " + "<!a" * (n // 3), None),
    "tags_quote_unclosed": lambda n: (BARE + " " + "<a b='" * (n // 6), None),
    "tags_alternating_quotes": lambda n: (BARE + " " + "<a x='<a y=\"" * (n // 12), None),
    "tag_attributes": lambda n: (BARE + " <a" + " b" * (n // 2), None),
    "tag_spaces": lambda n: (BARE + " <a" + " " * n + "b", None),
    "autolink_domain": lambda n: (BARE + " <a@" + "a." * (n // 2), None),
    "autolink_scheme": lambda n: (BARE + " <ab:" + "x" * n, None),
    "angles": lambda n: (BARE + " " + "<" * n, None),
    "atx_spaces": lambda n: ("#" + " " * n + "x\n\n" + BARE, None),
    "heading_emphasis": lambda n: ("### " + "*" * n + "\n\n" + BARE, None),
    "cell_percent": lambda n: (BARE, "%" * n),
    "cell_colons": lambda n: (BARE, "a:" * (n // 2)),
    "nested_markers": lambda n: ("- " * (n // 2) + BARE, None),
    "item_label_lines": lambda n: ("- a\n" + ("  " + BARE + "\n") * (n // 27), None),
    "paragraph_label_lines": lambda n: ((BARE + "\n") * (n // 25), None),
    "empty_markers": lambda n: ("- a `\n" + "-\n" * (n // 2) + BARE, None),
    "label_fence_pairs": lambda n: (("- " + BARE + "\n\n```\n```\n\n") * (n // 36), None),
    "sections": lambda n: ("x\n\n" + "## Completion report\n\nx\n\n" * (n // 26) + "## Completion report\n\n" + BARE, None),
    "nested_reports": lambda n: (("### Phase\n\n- **Work completed:** w\n\n") * (n // 36) + BARE, None),
}


def adversarial_report(name: str, n: int = SIZE) -> str:
    body, cell = ADVERSARIAL[name](n)
    text = NEW_CONTRACT_BASE.replace("BODY", body)
    return text if cell is None else text.replace("tools/x.py:12", cell)


def _seconds(text: str) -> float:
    """The fastest of three runs (scheduler noise only ever adds time), each with the cyclic garbage collector paused.

    A collection traverses every live object of the whole test process, so inside a full suite its cost tracks the
    suite's heap, not the report: CI window 6 measured 9.6 times for four times the `sections` input with it on, while
    the same shape alone grows about 4.0 times per step up to 1.28 MB with it off. Pausing it keeps the ratio a
    property of the lint; reference counting still frees memory, and a quadratic step still costs 16 times."""
    best = float("inf")
    for _ in range(3):
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            rl.lint_report(text)
            best = min(best, time.perf_counter() - start)
        finally:
            gc.enable()
    return best


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_property_adversarial_inputs_finish_in_bounded_time(name: str) -> None:
    text = adversarial_report(name)
    start = time.perf_counter()
    rl.lint_report(text)
    assert time.perf_counter() - start < TIME_BOUND, name


@pytest.mark.parametrize("name", ["paragraph_label_lines", "item_label_lines", "sections", "nested_reports", "brackets"])
def test_the_running_time_grows_linearly(name: str) -> None:
    """R4F2's shapes (labels in one paragraph, many sections) and nested reports: four times the input costs at most
    `RATIO` times the time. Sizes stay small for the default suite; the 2 MB sweep is in the task file."""
    small, large = _seconds(adversarial_report(name, 20000)), _seconds(adversarial_report(name, 80000))
    assert large <= RATIO * max(small, 0.002), (name, small, large)


@settings(max_examples=40, deadline=None)
@given(st.lists(st.sampled_from(["\\" * 7, '"', "'", "(", ")", "[", "]", "![", "`", "``", "<", "<!--", "<a b='", ">", "&#", "*", "_", "- ", "2. ", "\n", "a", ":", "x.py:1"]), max_size=600))
def test_property_random_adversarial_text_finishes_in_bounded_time(parts: list[str]) -> None:
    text = NEW_CONTRACT_BASE.replace("BODY", "".join(parts) + "\n\n" + BARE)
    start = time.perf_counter()
    rl.lint_report(text)
    assert time.perf_counter() - start < TIME_BOUND


def test_the_cli_reads_every_adversarial_family_in_bounded_time(tmp_path: Path) -> None:
    """The real CLI, once over a file per family (one run), the way `cli_worker.report_lint_record` calls it
    synchronously: it returns well within the bound, with the faulthandler watchdog armed in case it does not."""
    paths = []
    for name in sorted(ADVERSARIAL):
        path = tmp_path / f"{name}.md"
        path.write_text(adversarial_report(name), encoding="utf-8")
        paths.append(str(path))
    watchdog = tmp_path / "watchdog.py"
    watchdog.write_text(
        "import faulthandler, runpy, sys\nfaulthandler.enable()\nfaulthandler.dump_traceback_later(30, exit=True)\n"
        "tool = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(tool, run_name='__main__')\n",
        encoding="utf-8",
    )
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-B", "-I", str(watchdog), str(TOOLS / "report_lint.py"), *paths],
        capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW, cwd=tmp_path, check=False,
    )
    elapsed = time.perf_counter() - start
    assert not proc.stderr.startswith("Timeout"), proc.stderr[:500]
    assert proc.returncode in (0, 1), proc.stderr[:500]
    assert elapsed < len(paths) * TIME_BOUND + 5, elapsed


# Pointer paths against the round 3 regular expression (a test-only oracle: the old `_PATH_LINE`, which is right but
# restarts at each position; `_path_lines` is carried over unchanged).
PATH_ORACLE = re.compile(r"(?<![\w./\\-])([^\s:`|<>()\[\]\"']+):(\d+)(?:-(\d+))?(?!\w)")


@settings(max_examples=400, deadline=None)
@given(st.lists(st.sampled_from(["a", ".py", "/", "\\", "-", ":", "1", "2", "%", "_", " ", "|", "x", "\u0663", "\u00b2", "\u00a0"]), max_size=16))
def test_property_the_path_reader_finds_the_round_3_pointers(parts: list[str]) -> None:
    text = "".join(parts)
    assert rl._path_lines(text) == [match[1] for match in PATH_ORACLE.finditer(text)]


@pytest.mark.parametrize(
    "text",
    [
        "12:3-b.py:4", "a.py:1-2x", "a.py:1-2", "x/a.py:3x", "%a.py:3", "a.py:3:4", "٣:٣", "a.py:²",
        "a b.py:1", "v1.2:3", "x-a.py:3", "(a.py:3)", "a.py:3-", "a.py:3-4-5", "_a.py:1", "é/a:1",
    ],
)
def test_the_path_reader_finds_the_round_3_pointers_on_edge_cells(text: str) -> None:
    assert rl._path_lines(text) == [match[1] for match in PATH_ORACLE.finditer(text)]


# --- the standalone CLI ------------------------------------------------------------------------------------------


def test_the_cli_prints_path_line_rule_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text(report(), encoding="utf-8")
    bad.write_text(report(rows=("| q | nothing | raw | f | documented |",)), encoding="utf-8")
    assert rl.main([str(good)]) == 0
    assert capsys.readouterr().out == ""
    assert rl.main([str(good), str(bad)]) == 1
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith(f"{bad}:") and "[evidence-pointer]" in line


def test_the_cli_refuses_an_unreadable_file_and_a_missing_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = tmp_path / "bin.md"
    binary.write_bytes(b"\xff\xfe\x00\xd8 not utf-8 \x80")
    assert rl.main([str(tmp_path / "absent.md"), str(binary)]) == 2
    out = capsys.readouterr().out
    assert out.count("cannot read") == 2
    with pytest.raises(SystemExit) as refused:
        rl.main([])
    assert refused.value.code == 2


def test_the_script_runs_isolated_with_no_import_path(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text(report(label=None), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-B", "-I", str(TOOLS / "report_lint.py"), str(bad)],
        capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW, cwd=tmp_path, check=False,
    )
    assert proc.returncode == 1, proc.stderr
    assert "[done-check-output]" in proc.stdout and proc.stderr == ""


# --- the turn-end call in cli_worker -------------------------------------------------------------------------------


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "JOBS", tmp_path / "jobs")
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    return repo


def turn(work: Path, monkeypatch: pytest.MonkeyPatch, task: Path | None, name: str = "w") -> tuple[int, dict[str, Any]]:
    entry: dict[str, Any] = {
        "model": "haiku", "effort": "low", "session_id": "sid", "cwd": str(work), "status": "busy", "turns": 0,
        "hooks": {"task_file": None if task is None else task.as_posix()},
    }
    cw.save({**cw.load(), name: {**cw.load().get(name, {}), **entry}})
    script = "import json\nprint(json.dumps({'type': 'result', 'result': 'done'}))\n"  # labelled test double
    monkeypatch.setattr(cw, "build_command", lambda w, r: [sys.executable, "-c", script])
    rc = cw.run_turn(name, cw.load()[name], "hi", False, 5, log_number=1)
    return rc, cw.load()[name]


def test_a_turn_records_the_findings_and_warns_but_does_not_block(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = work / "task.md"
    task.write_text(report(rows=("| q | nothing | raw | f | nope |",)), encoding="utf-8")
    rc, stored = turn(work, monkeypatch, task)
    assert rc == 0 and stored["status"] == "idle"  # advisory: the turn's own result stands
    record = stored["report_lint"]
    assert record["error"] is None and record["turn"] == 1 and record["task_file"] == task.as_posix()
    assert record["total"] == 2 == len(record["findings"])
    assert {f["rule"] for f in record["findings"]} == {"evidence-table", "evidence-pointer"}
    err = capsys.readouterr().err
    assert "WARNING: worker w: report lint, 2 finding(s)" in err and "advisory" in err
    row = next(r for r in cw.status_rows(False) if r["name"] == "w")
    assert row["report_lint"]["total"] == 2
    assert json.loads(cw.REGISTRY.read_text(encoding="utf-8"))["w"]["report_lint"] == record


def test_a_clean_report_and_an_unwritten_one_leave_a_quiet_record(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = work / "task.md"
    task.write_text(report(), encoding="utf-8")
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["total"] == 0 and stored["report_lint"]["findings"] == []
    task.write_text("# T\n\n## Goal\n\ng\n", encoding="utf-8")  # no Completion report yet: skipped
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["total"] == 0
    assert "report lint" not in capsys.readouterr().err
    assert "report_lint" not in next(r for r in cw.status_rows(False) if r["name"] == "w")
    task.write_text("# T\n\n## Completion report\n\n<!-- later -->\n", encoding="utf-8")  # unwritten: not linted, said so
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["total"] == 0 and stored["report_lint"]["unwritten"] == [3]
    assert "NOTE: worker w: report lint skipped the report at line(s) 3" in capsys.readouterr().err
    assert next(r for r in cw.status_rows(False) if r["name"] == "w")["report_lint"]["unwritten"] == [3]


def test_a_worker_without_a_task_file_keeps_no_record(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = work / "task.md"
    task.write_text(report(label=None), encoding="utf-8")
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["total"] == 1
    _, stored = turn(work, monkeypatch, None)  # a later turn of a worker with no registered task file: the record goes
    assert "report_lint" not in stored


@pytest.mark.parametrize("failure", ["crash", "missing", "undecodable"])
def test_a_lint_crash_or_unreadable_file_is_recorded_never_silent(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    task = work / "task.md"
    task.write_bytes(b"\xff\xfe\x80 bad") if failure == "undecodable" else task.write_text(report(), encoding="utf-8")
    if failure == "missing":
        task.unlink()
    if failure == "crash":

        def boom(name: str) -> Any:
            raise RuntimeError("scanner exploded")

        monkeypatch.setattr(cw, "sibling", boom)
    rc, stored = turn(work, monkeypatch, task)
    assert rc == 0  # still advisory
    record = stored["report_lint"]
    assert record["error"] and record["total"] == 0 and record["findings"] == []
    assert {"crash": "RuntimeError: scanner exploded", "missing": "FileNotFoundError", "undecodable": "UnicodeDecodeError"}[
        failure
    ] in record["error"]
    assert "report lint could not run" in capsys.readouterr().err
    assert next(r for r in cw.status_rows(False) if r["name"] == "w")["report_lint"]["error"] == record["error"]


def test_a_parser_of_another_version_is_recorded_by_the_turn_end_call(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PM answer Q3: the refused parser reaches the registry record as an error, never a silent pass."""
    task = work / "task.md"
    task.write_text(report(), encoding="utf-8")
    monkeypatch.setattr(rl, "_PARSER_PROBLEM", "markdown-it-py==4.2.0 is required, markdown-it-py 9.9.9 is installed")
    monkeypatch.setattr(cw, "sibling", lambda name: rl)
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["error"].startswith("ParserUnavailable: markdown-it-py==4.2.0 is required")


def test_a_loader_exit_is_recorded_too(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = work / "task.md"
    task.write_text(report(), encoding="utf-8")

    def gone(name: str) -> Any:
        raise SystemExit("cannot load report_lint.py")

    monkeypatch.setattr(cw, "sibling", gone)
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["error"] == "SystemExit: cannot load report_lint.py"


GC = load(TOOLS / "hooks" / "guard_config.py", "guard_config_report_lint")


def lint_bounds(monkeypatch: pytest.MonkeyPatch, **values: Any) -> dict[str, Any]:
    """The tracked guard config with `report_lint` values overridden, validated the way `cli_worker.guard_config()`
    validates it, as the config the turn reads."""
    data = json.loads(GC.DEFAULT_PATH.read_text(encoding="utf-8"))
    data["report_lint"].update(values)
    cfg = GC.validate(data)
    monkeypatch.setattr(cw, "guard_config", lambda: cfg)
    return cfg


def test_the_record_is_bounded_by_the_configured_max_findings(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix round 1: the count bound moved from a module constant to `guard_config.json` `report_lint.max_findings`."""
    rows = tuple("| q | nothing | raw | f | documented |" for _ in range(4))
    task = work / "task.md"
    task.write_text(report(rows=rows), encoding="utf-8")
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["total"] == 4 == len(stored["report_lint"]["findings"])  # tracked bound 50
    assert stored["report_lint"]["truncated"] == 0
    lint_bounds(monkeypatch, max_findings=2)  # a non-default bound
    _, stored = turn(work, monkeypatch, task)
    assert stored["report_lint"]["total"] == 4 and len(stored["report_lint"]["findings"]) == 2
    assert stored["report_lint"]["truncated"] == 2


def test_the_warning_shows_the_configured_number_of_findings(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = tuple("| q | nothing | raw | f | documented |" for _ in range(4))
    task = work / "task.md"
    task.write_text(report(rows=rows), encoding="utf-8")
    turn(work, monkeypatch, task)
    assert capsys.readouterr().err.count("[evidence-pointer]") == 4  # tracked `shown` 5
    lint_bounds(monkeypatch, shown=1)
    turn(work, monkeypatch, task)
    assert capsys.readouterr().err.count("[evidence-pointer]") == 1


# --- fix round 1 (Codex review P2R-F1, P2R-F2, P2R-F3) -------------------------------------------------------------

from markdown_it import MarkdownIt  # noqa: E402  (the pinned parser, read as the stock reference)

STOCK = MarkdownIt("commonmark").enable(["table"])
STOCK_BLOCK = MarkdownIt("commonmark").enable(["table"]).disable("inline")

# P2R-F1: a report dropped by the parser at maxNesting is a finding wherever it sits, never a silent pass.
VALID_REPORT = (
    "## Completion report{suffix}\n\n- **Work completed:** w\n- **Result and evidence:**\n\n```text\n1 passed\n```\n\n"
    f"### Research evidence\n\n{HEADER}\n{GOOD_ROW}\n\n"
)
OTHER_SECTION = "## Notes\n\nn\n\n"  # ends the report above it, so a block after it lies outside every report
CHAIN = st.lists(st.sampled_from(["> ", "- ", "* ", "1. "]), min_size=6, max_size=26)


def _placed(reports: int, position: int, block: str) -> tuple[str, int]:
    """`reports` valid reports, each followed by an other section, and `block` before the first (0), between the first
    two (1) or after the last (2); the text and the block's 0-based line."""
    parts = [VALID_REPORT.format(suffix=f" (phase {k})") + OTHER_SECTION for k in range(reports)]
    at = {0: 0, 1: min(1, reports), 2: reports}[position]
    head = "# T\n\n" + "".join(parts[:at])
    return head + block + "\n\n" + "".join(parts[at:]), head.count("\n")


@settings(max_examples=150, deadline=None)
@given(CHAIN, st.integers(1, 3), st.integers(0, 2), st.booleans(), eols, boms)
def test_property_a_dropped_report_is_named_before_between_and_after_reports(
    chain: list[str], reports: int, position: int, label: bool, eol: str, bom: str
) -> None:
    """Quote, list and mixed chains around the parser's depth limit: when the stock parser drops the nested report
    heading (no heading token on its line) the finding names maxNesting; when it keeps it, the finding is the
    container-heading one. Either way exactly one finding, on the block's line, and the valid reports stay clean."""
    prefix = "".join(chain)
    block = prefix + "## Completion report" + ("\n" + prefix + "**Work completed:** written" if label else "")
    text, line = _placed(reports, position, block)
    shown = any(t.type == "heading_open" and t.map[0] == line for t in STOCK.parse(text))
    found = rl.lint_report(bom + text.replace("\n", eol)).findings
    assert [(f.rule, f.line) for f in found] == [("report-section", line + 1)], (found, shown)
    assert ("maxNesting" in found[0].message) is not shown
    assert shown or "a report heading, label or table there is not read" in found[0].message


@pytest.mark.parametrize(("bom", "eol"), [("", "\n"), ("﻿", "\r\n")])
def test_the_reviewers_dropped_report_cases_are_named(bom: str, eol: str) -> None:
    """The reviewer's own.log reproducers (`.tmp/T-021/review-p2r/review_driver.py own`), which exited 0."""
    dropped = "> " * 20 + "## Completion report\n\n" + "> " * 20 + "- **Work completed:** written\n"
    for text in (dropped, dropped + "\n" + VALID_REPORT.format(suffix="")):
        found = rl.lint_report(bom + text.replace("\n", eol)).findings
        assert [(f.rule, f.line) for f in found] == [("report-section", 1), ("report-section", 3)]
        assert all("maxNesting" in f.message for f in found)


def test_dropped_containers_give_one_finding_per_top_level_block_inside_or_outside_a_report() -> None:
    two_deep = "- " + "> " * 19 + "x\n- " + "> " * 19 + "y"  # one top-level list, two dropped quotes
    assert [f.line for f in rl.lint_report(two_deep).findings] == [1]
    inside = VALID_REPORT.format(suffix="") + two_deep + "\n"  # in a report: once, not again for the unit
    (finding,) = rl.lint_report(inside).findings
    assert "maxNesting" in finding.message and finding.line == inside.count("\n") - 1


@settings(max_examples=60, deadline=None)
@given(st.integers(0, 2), st.integers(1, 3))
def test_property_an_unread_heading_before_between_or_after_reports_is_named(position: int, reports: int) -> None:
    text, line = _placed(reports, position, "## Completion report " + "x" * rl.INLINE_MAX)
    assert [(f.rule, f.line, "INLINE_MAX" in f.message) for f in rl.lint_report(text).findings] == [
        ("report-section", line + 1, True)
    ]


# P2R-F2: the block reference rule's multi-line title is joined once (an instance-level helper), read as stock.
def _same_reading(text: str) -> None:
    ours_env: dict[str, Any] = {}
    stock_env: dict[str, Any] = {}
    md = rl._configured()
    ours = md.parse(text, ours_env)
    stock = STOCK_BLOCK.parse(text, stock_env)
    assert [t.as_dict() for t in ours] == [t.as_dict() for t in stock]
    assert ours_env.get("references") == stock_env.get("references")
    for token in ours:  # inline links and images call the same helper without a continuation
        if token.type == "inline":
            mine: list[Any] = []
            theirs: list[Any] = []
            md.inline.parse(token.content, md, dict(ours_env), mine)
            STOCK_BLOCK.inline.parse(token.content, STOCK_BLOCK, dict(stock_env), theirs)
            assert [t.as_dict() for t in mine] == [t.as_dict() for t in theirs]


TITLE_PARTS = st.lists(
    st.sampled_from(
        ['"', "'", "(", ")", "\\", '\\"', "\\'", "\\)", "\\(", "a", " ", "&amp;", "\n", "\n\n", "\n# h\n", "\n> q\n",
         "\n- i\n", "\n```\n", "\n<div>\n", "\n***\n", "\n| a |\n|---|\n", "\n    code\n", "\n[b]: /v\n", "x y"]
    ),
    max_size=30,
)


@settings(max_examples=400, deadline=None)
@given(
    st.sampled_from(["[a]: /u", "[a]: /u ", "[a]:\n/u", "[b\nc]: <u>", "[a]: /u\n", "[a]:", "[a\\]]: /u", "[a]: <>"]),
    st.sampled_from(['"', "'", "(", "", " "]),
    TITLE_PARTS,
    st.sampled_from(["", "\n[a]\n", "\n[x][a]\n", "\n![i][a]\n", '\n[l](/d "t\nu")\n']),
    st.booleans(),
)
def test_property_the_joined_title_reads_as_the_stock_parser(
    head: str, marker: str, parts: list[str], tail: str, quoted: bool
) -> None:
    """PM condition: tokens and `env["references"]` equal to the stock parser over each title marker, escapes,
    unterminated titles, blank lines and terminators, at the top level and inside a quote."""
    text = head + " " + marker + "".join(parts) + tail
    _same_reading("\n".join("> " + line for line in text.split("\n")) if quoted else text)


def test_the_title_helper_is_this_parsers_own_and_the_library_is_untouched() -> None:
    import markdown_it.helpers as library

    assert rl._configured().helpers.parseLinkTitle is rl._title_continued
    assert MarkdownIt("commonmark").helpers is library  # a fresh parser still binds the shared module
    assert library.parseLinkTitle is not rl._title_continued


def test_a_multiline_reference_title_is_copied_once_not_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2R-F2's cost, counted rather than timed: the library copies the title so far into each continuation state
    (helpers/parse_link_title.py:37,74). A spy on the library function counts those bytes through the real lint; the
    stock path copies about lines² / 2 characters, the joined helper none."""
    import markdown_it.helpers as library

    original = library.parseLinkTitle
    copied = [0]

    def spy(string: str, start: int, maximum: int, prev_state: Any = None) -> Any:
        if prev_state is not None:
            copied[0] += len(prev_state.str)
        return original(string, start, maximum, prev_state)

    monkeypatch.setattr(library, "parseLinkTitle", spy)
    for title in ('"', "("):
        copied[0] = 0
        text = NEW_CONTRACT_BASE.replace("BODY", BARE) + f"\n[a]: /url {title}\n" + "a\n" * 4000
        assert rl.lint_report(text).findings == []
        assert copied[0] <= len(text), (title, copied[0])
    # Nor does the helper join per line: an open title keeps its parts and no text; the finished one joins them once.
    state = rl._title_continued('"a\n', 0, 3)
    for _ in range(50):
        state = rl._title_continued("b\n", 0, 2, state)
        assert state.can_continue and state.str == ""
    done = rl._title_continued('c"', 0, 2, state)
    assert done.ok and done.str == "a\n" + "b\n" * 50 + "c"
    # A finished title is read whole, as the library reads it.
    env: dict[str, Any] = {}
    rl._configured().parse('[a]: /u "x\ny\\"z\nw"\n', env)
    assert env["references"]["A"]["title"] == 'x\ny"z\nw'


REFERENCE_FAMILIES = {  # the reviewer's multiline-reference shapes at n characters
    "reference_title_lines": lambda n: '\n[a]: /url "\n' + "a\n" * (n // 2),
    "reference_paren_title_lines": lambda n: "\n[a]: /url (\n" + "a\n" * (n // 2),
    "reference_label_lines": lambda n: "\n[" + "a\n" * (n // 2),
    "reference_label_colon_lines": lambda n: "\n" + "[a\n" * (n // 3) + "]: /u\n",
    "reference_definitions": lambda n: "\n" + '[a]: /u "t"\n' * (n // 12),
}


@pytest.mark.parametrize("name", sorted(REFERENCE_FAMILIES))
def test_multiline_reference_titles_and_labels_grow_linearly(name: str) -> None:
    base = NEW_CONTRACT_BASE.replace("BODY", BARE)
    small = _seconds(base + REFERENCE_FAMILIES[name](20000))
    large = _seconds(base + REFERENCE_FAMILIES[name](80000))
    assert large <= RATIO * max(small, 0.002), (name, small, large)


# P2R-F2: the turn-end deadline. The rule matches nothing, so the reading is unchanged; past the deadline it stops.
def _with_deadline_rule() -> Any:
    md = MarkdownIt("commonmark").enable(["table"])
    md.block.ruler.before("table", rl._DEADLINE_RULE, rl._deadline_rule, {"alt": rl._DEADLINE_CHAINS})
    return md


@settings(max_examples=300, deadline=None)
@given(
    st.lists(st.sampled_from(["#", "##", "|", "---", "`", "```", "~~~", ">", "- ", "1. ", "*", "\\|", " ", "a", "[a]:",
                              " /u", ' "t', "<div>", "<!--", "    ", "===", "**Result and evidence:**", "x.py:1"]),
             max_size=40),
    st.integers(0, 3),
)
def test_property_the_deadline_rule_changes_no_token(parts: list[str], breaks: int) -> None:
    text = ("\n" * (breaks + 1)).join(["".join(parts[i::3]) for i in range(3)])
    far = {rl._DEADLINE_KEY: (time.monotonic() + 600, 600)}
    assert [t.as_dict() for t in _with_deadline_rule().parse(text, dict(far))] == [
        t.as_dict() for t in STOCK.parse(text, {})
    ]
    assert rl._configured().block.ruler.get_all_rules()[0] == rl._DEADLINE_RULE  # first: before every block rule


def test_a_passed_deadline_stops_the_lint_by_name() -> None:
    assert rl.lint_report(report(), timeout=600) == rl.lint_report(report())
    with pytest.raises(rl.LintDeadline, match=r"did not finish within 0 s; the file is not read"):
        rl.lint_report(report(), timeout=0)
    long = NEW_CONTRACT_BASE.replace("BODY", BARE) + REFERENCE_FAMILIES["reference_title_lines"](600000)
    start = time.perf_counter()
    with pytest.raises(rl.LintDeadline, match="within 0.2 s"):
        rl.lint_report(long, timeout=0.2)
    assert time.perf_counter() - start < 0.2 + 2.0  # the PM's overshoot limit; the sweep measures it per family


def test_a_deadline_at_the_turn_end_is_recorded_and_the_turn_saves_its_state(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PM condition: expiry is recorded in `error` naming `LintDeadline`, and the turn completes and saves its state.
    About 9 s of parsing at 2 MB, stopped at `timeout_seconds` 1."""
    lint_bounds(monkeypatch, timeout_seconds=1)
    task = work / "task.md"
    task.write_text(
        NEW_CONTRACT_BASE.replace("BODY", BARE) + REFERENCE_FAMILIES["reference_title_lines"](2_000_000),
        encoding="utf-8",
    )
    real = cw.report_lint_record
    spent: list[float] = []

    def timed(*args: Any) -> Any:
        start = time.perf_counter()
        try:
            return real(*args)
        finally:
            spent.append(time.perf_counter() - start)

    monkeypatch.setattr(cw, "report_lint_record", timed)
    rc, stored = turn(work, monkeypatch, task)
    assert rc == 0 and stored["status"] == "idle" and stored["turns"] == 1  # the turn's state was saved
    assert stored["report_lint"]["error"] == "LintDeadline: the lint did not finish within 1 s; the file is not read"
    assert json.loads(cw.REGISTRY.read_text(encoding="utf-8"))["w"]["report_lint"] == stored["report_lint"]
    assert "report lint could not run" in capsys.readouterr().err
    assert spent and spent[0] < 1 + 2.0, spent


# P2R-F3: messages quote at most PREVIEW_MAX characters of raw text; the record and the warning are byte-bounded.
def test_raw_text_in_a_message_is_cut_to_the_preview_bound() -> None:
    def class_message(cell: str) -> str:
        (finding,) = rl.lint_report(report(rows=(f"| q | `a.py:1` | raw | f | {cell} |",))).findings
        return finding.message

    assert f"{'x' * rl.PREVIEW_MAX!r} is none" in class_message("x" * rl.PREVIEW_MAX)  # at the bound: whole
    over = class_message("x" * (rl.PREVIEW_MAX + 1))
    assert f"{'x' * rl.PREVIEW_MAX!r}… ({rl.PREVIEW_MAX + 1} characters)" in over
    assert len(class_message("x" * 2_000_000)) < 300
    title = "## Completion reports" + "y" * 5000  # an odd title, quoted in its finding
    (odd,) = [f for f in rl.lint_report(report(title=title)).findings if "is not a report heading" in f.message]
    assert len(odd.message) < 400 and "(5018 characters)" in odd.message


class FakeLint:
    """Labelled test double for the report_lint module at the turn end: fixed findings, or a fixed exception."""

    def __init__(self, findings: list[Any], error: BaseException | None = None) -> None:
        self.findings, self.error, self.timeouts = findings, error, []

    def load_template(self) -> None:
        return None

    def lint_report(self, text: str, template: None, timeout: float) -> Any:
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return rl.Result(self.findings, [])


def _recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: FakeLint, capsys: pytest.CaptureFixture[str], **bounds: Any
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    cfg = lint_bounds(monkeypatch, **bounds)["report_lint"]
    monkeypatch.setattr(cw, "sibling", lambda name: fake)
    task = tmp_path / "task.md"
    task.write_text("x", encoding="utf-8")
    record = cw.report_lint_record("w", {"hooks": {"task_file": task.as_posix()}}, 1)
    assert record is not None
    return record, capsys.readouterr().err, cfg


@pytest.mark.parametrize(
    "bounds", [{}, {"message_max_bytes": 64, "record_max_bytes": 384}, {"message_max_bytes": 1000, "record_max_bytes": 4000}]
)
@pytest.mark.parametrize("char", ["x", "é", "\U0001f600"])
def test_the_record_and_the_warning_stay_within_their_byte_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], bounds: dict[str, int], char: str
) -> None:
    """The reviewer's record.log shapes (1000, 100000 and 2000000 characters) as messages, 60 findings, and a huge
    error; ASCII, a two-byte and a four-byte character (JSON escapes them to 6 and 12 bytes). Tracked bounds and two
    non-default ones."""
    for n in (1000, 100_000, 2_000_000):
        findings = [rl.Finding("evidence-table", k + 1, char * n) for k in range(60)]
        fake = FakeLint(findings)
        record, err, cfg = _recorded(monkeypatch, tmp_path, fake, capsys, **bounds)
        assert fake.timeouts == [cfg["timeout_seconds"]]
        assert len(json.dumps(record)) <= cfg["record_max_bytes"]
        assert len(err.rstrip("\n").encode("utf-8")) <= cfg["record_max_bytes"]
        assert record["total"] == 60 and record["truncated"] == 60 - len(record["findings"]) > 0
        assert record["findings"], "at least one finding fits"
        over = cw._json_bytes(char * n) > cfg["message_max_bytes"]
        for item in record["findings"]:
            assert cw._json_bytes(item["message"]) <= cfg["message_max_bytes"]
            assert item["message"].endswith(f"… [cut: {cw._json_bytes(char * n)} bytes]") is over
            assert over or item["message"] == char * n
        assert "finding(s)" in err and (not over or "[cut:" in err)  # a cut message shows its marker in the warning
        error, err, cfg = _recorded(monkeypatch, tmp_path, FakeLint([], RuntimeError(char * n)), capsys, **bounds)
        assert cw._json_bytes(error["error"]) <= cfg["message_max_bytes"] and "[cut:" in error["error"]
        assert len(json.dumps(error)) <= cfg["record_max_bytes"]
        assert "could not run" in err and len(err.rstrip("\n").encode("utf-8")) <= cfg["record_max_bytes"]


def test_a_large_max_findings_keeps_the_record_linear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Findings are kept while the record fits, one JSON size per finding; the final trim only absorbs the counters'
    digits. Re-measuring the whole record per dropped finding would cost findings × record size."""
    findings = [rl.Finding("evidence-pointer", k + 1, "m" * 60) for k in range(5000)]
    start = time.perf_counter()
    record, _, cfg = _recorded(monkeypatch, tmp_path, FakeLint(findings), capsys, max_findings=100_000)
    assert time.perf_counter() - start < 3.0
    assert record["total"] == 5000 and len(json.dumps(record)) <= cfg["record_max_bytes"]
    assert 100 < len(record["findings"]) == 5000 - record["truncated"]


def test_the_real_lint_on_the_reviewers_class_cell_gives_a_small_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """record.log: a 2000000-character class cell made a 2000335-byte record through the real parser."""
    monkeypatch.setattr(cw, "sibling", lambda name: rl)
    lint_bounds(monkeypatch)
    for n in (1000, 100_000, 2_000_000):
        task = tmp_path / "task.md"
        task.write_text(report(rows=(f"| q | `a.py:1` | raw | f | {'x' * n} |",)), encoding="utf-8")
        record = cw.report_lint_record("w", {"hooks": {"task_file": task.as_posix()}}, 1)
        assert record is not None and record["total"] == 1 and record["error"] is None
        assert len(json.dumps(record)) < 1024 and len(capsys.readouterr().err) < 1024


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=300), st.integers(64, 400), st.sampled_from(["json", "utf8"]))
def test_property_a_cut_fits_its_bound_and_keeps_a_visible_marker(text: str, limit: int, measure: str) -> None:
    size = cw._json_bytes if measure == "json" else cw._utf8_bytes
    cut = cw.cut_bytes(text, limit, size)
    assert size(cut) <= limit
    if size(text) <= limit:
        assert cut == text
    else:
        kept = cut[: len(cut) - len(f"… [cut: {size(text)} bytes]")]
        assert text.startswith(kept) and cut.endswith(f"… [cut: {size(text)} bytes]")
        assert size(kept + text[len(kept)]) + size(cut) - size(kept) > limit  # the longest prefix that fits


def test_an_invalid_report_lint_section_is_recorded_by_name_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusal: the turn-end call reads its bounds from the validated config; a config without the section (or with an
    invalid value) reaches `error` through the SystemExit of `cli_worker.guard_config()`, cut to the fallback bound."""
    data = json.loads(GC.DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["report_lint"]

    def refused() -> dict[str, Any]:
        try:
            return GC.validate(data)
        except GC.ConfigError as exc:
            raise SystemExit(f"guard config: {exc}") from None

    monkeypatch.setattr(cw, "guard_config", refused)
    task = tmp_path / "task.md"
    task.write_text(report(), encoding="utf-8")
    record = cw.report_lint_record("w", {"hooks": {"task_file": task.as_posix()}}, 1)
    assert record is not None
    assert record["error"] == "SystemExit: guard config: guard_config is missing required keys: report_lint"
    assert "report lint could not run" in capsys.readouterr().err
    data["report_lint"] = {**json.loads(GC.DEFAULT_PATH.read_text(encoding="utf-8"))["report_lint"], "shown": 0}
    record = cw.report_lint_record("w", {"hooks": {"task_file": task.as_posix()}}, 1)
    assert record is not None and "guard_config.report_lint.shown must be a positive integer" in record["error"]


def test_the_hooks_start_with_the_tracked_config_and_refuse_one_without_the_section() -> None:
    """PM condition (guard_config.py is loaded by every hook): tool_guard runs with the tracked file and blocks shells,
    naming the section, with a file that lacks it."""
    hook = [sys.executable, "-X", "faulthandler", "-B", "-I", str(TOOLS / "hooks" / "tool_guard.py"), "--git-mode", "worker"]
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    ok = subprocess.run(hook, input=event, capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW, check=False)
    assert ok.returncode == 0, ok.stderr
    bad = GC.ROOT / ".tmp" / "t021-fix1-guard" / f"no-report-lint-{time.time_ns()}.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(GC.DEFAULT_PATH.read_text(encoding="utf-8"))
        del data["report_lint"]
        bad.write_text(json.dumps(data), encoding="utf-8")
        blocked = subprocess.run(
            [*hook, "--config", str(bad)], input=event, capture_output=True, text=True, timeout=60,
            creationflags=NO_WINDOW, check=False,
        )
        assert blocked.returncode == 2
        assert f"{bad.name} is missing required keys: report_lint" in blocked.stderr
    finally:
        bad.unlink(missing_ok=True)
        bad.parent.rmdir()


def test_the_cli_exits_2_on_an_internal_error_and_says_cannot_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PM answer Q4: an exception while linting is no finding (exit 1) and no traceback, but exit 2 naming the file."""
    good, broken = tmp_path / "good.md", tmp_path / "broken.md"
    good.write_text(report(), encoding="utf-8")
    broken.write_text("boom", encoding="utf-8")
    real = rl.lint_report

    def fragile(text: str, template: str | None = None, timeout: float | None = None) -> Any:
        if text == "boom":
            raise RecursionError("y" * 5000)
        return real(text, template, timeout)

    monkeypatch.setattr(rl, "lint_report", fragile)
    assert rl.main([str(good), str(broken)]) == 2
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith(f"{broken}: cannot lint (RecursionError: ") and "(5000 characters)" in line
