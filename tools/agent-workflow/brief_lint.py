"""Gate for worker briefs: Git writes (R3 audit F-10, P-10) and the PM's own repeated brief mistakes (T-MEM).

Usage: python -B -I brief_lint.py [--require-footer] [--brief [--footer F] [--legacy-order]] <brief.md>...
Exit 0: clean. Exit 1: findings (one `file:line: [rule] text` per hit). `cli_worker.py start` and `send` call
`lint_message` and refuse to launch on a finding (`--lint-waive "<reason>"` logs an override).

Brief layout (T-021, "context first, task last", N127): the CLI wrapper, the header lines, the context material, the
role pack and the footer come first; the brief ends with one task block under the exact heading `## Task` (Goal, Done
check, Scope, Out of scope, Output). The footer is its verbatim span (the first copy of the footer file outside fenced
code); without a `## Task` line after it (a brief in the old order) the footer runs to the end of the text, as before.
Every rule reads the text without the footer, so the task block and any text around the footer are linted too.
Markdown structure (fences, headings, the footer span) comes from one block scanner, `_scan`, after CommonMark.

Rules that run on every message (`full=False`):
- git-write: workers never commit, merge, stash, push or create branches (footer rule 2; the PM does). Only Git
  actions are flagged: `git <verb>`, a branch-creating phrase, or a bare verb followed by a Git object in the same
  clause ("merge the masks" or "commit the transaction" are plain English and pass). Checked per clause (split on
  commas and but/and/then/so/or/nor): a clause with a negation ("do not", "is forbidden", "is denied") or the PM
  before the verb is allowed, and that allowance carries to the next list item of a Git verb ("never merge,
  stash or push") but not across but/then/so. Not checked: the footer block, fenced code, blockquotes, inline code,
  double- and single-quoted text.
- base-url-empty / base-url-unset (mistake #27): the brief never makes `ANTHROPIC_BASE_URL` empty and, when it
  names the variable, says it is unset. Checked in code and quotes too.
- gemini-absolute-path (#12, #18) and inline-python (#14).
Rules of a complete brief (`full=True`: a start, or a send for which `is_brief_message` holds, i.e. a `[NEW TASK`
line in any case or a `Kind:` header outside a fence):
- kind: a `Kind:` header line with one of KINDS (feature, code, review, docs, scout, run, trivial);
- footer: .agents/policy/AGENT_BRIEF_FOOTER.md verbatim;
- r-ids: `R-IDs:` with an R-<n> for a feature;
- test-sets (#25): feature, code and review briefs name every `required` test set and every set of one
  `alternatives` group of `brief_lint_config.json` (the tool suite for tooling, the pipeline suite for the pipeline;
  an absent or invalid file is itself a finding), or carry a line
  `Test sets: not applicable: <reason of at least 10 characters>`;
- run-gates (#8, #9): a run brief names the one-job smoke, `run_approval_gate.py check` and `gpu_preflight`;
- slices (#26): with a `Task file:` header, every id of that file's slice table (a table whose first header cell
  starts with `Slice`) appears in the brief;
- task-file: the `Task file:` path stays inside the checkout (`read_task_file`); an outside path is not read.
- root-cause (decision 568): a non-review brief whose `[NEW TASK ...]` tag names a fix carries `Root cause:` and
  `Class sweep:` header lines (at least 20 characters each), so no fix goes out before its root cause and the audit
  of every sibling site.
- brief-order (T-021, with `order=True`): outside fenced code (backtick or tilde) there is exactly one `## Task` line,
  the verbatim footer span ends before it, and no level-1 or level-2 heading follows it, ATX (up to 3 spaces of
  indent) or Setext (so the role pack and the footer sit above the task, and the task block is last). Block quotes,
  list items and HTML blocks are not parsed: a heading, Setext underline or fence delimiter inside one is refused by
  name, anywhere in the brief (fail closed; a blank line after a tag line such as `<goal>` ends its HTML block).
  Structure only: no wording is read. Without `order` every reader is the base one, byte for byte. `cli_worker.py` runs it for a `start`
  and for a `send` to a worker whose registry entry carries `brief_order: 2`; a worker started before T-021 keeps the
  old order. The command line runs it with `--brief` unless `--legacy-order` is given.

Approach-note provenance (decision 582; the approach scout comes before the code):
- a feature brief requires an `Approach note:` path; `none` is refused even when it names a file read;
- a code brief may instead use `none: <reason>` naming the file the PM read to skip the scout;
- a note path must differ from `Task file:` after the same lexical path normalization on both values (slashes,
  dot components and case for Windows paths); the scout's output is separate from the task record;
- the existing minimum of 20 characters and read-file pattern remain unchanged. This rule compares header values;
  it does not read note files or introduce a vocabulary of reasons.
"""

from __future__ import annotations

import argparse
import bisect
import json
import ntpath
import os
import re
import sys
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

FOOTER_HEADING = "# Agent brief footer"

_VERBS = r"(?:commit|merge|stash|push|rebase|cherry-pick)"
# Bare verb in imperative or infinitive form; "commits", "merged", "merge-tree", "merge check" do not match.
_VERB = re.compile(
    rf"(?<![\w-]){_VERBS}(?![\w-])"
    r"(?!\s+(?:check|gate|base|conflicts?|procedure|candidates?|commits?|requests?)\b)",
    re.IGNORECASE,
)
_GIT_CMD = re.compile(rf"(?<![\w-])git\s+{_VERBS}(?![\w-])", re.IGNORECASE)
# A Git object after the verb makes the bare verb a Git action.
_OBJECT = re.compile(
    r"\b(?:changes?|commits?|branch(?:es)?|worktree|HEAD|origin|upstream|remote|main|master|locally|"
    r"the\s+batch|REFNAME)\b",
    re.IGNORECASE,
)
_BRANCH = re.compile(
    r"(?<![\w-])(?:(?:create|make|open|cut|start|switch\s+to|check\s*out)\s+"
    r"(?:an?\s+|the\s+|your\s+)?(?:new\s+|own\s+|separate\s+)?branch"
    r"|branch\s+off|git\s+branch|git\s+checkout\s+-b|git\s+switch\s+-c)(?![\w-])",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:do\s+not|don't|dont|never|must\s+not|may\s+not|cannot|can't|not\s+allowed|forbidden|"
    r"denied|without|no\s+one|nobody|not)\b",
    re.IGNORECASE,
)
_PM = re.compile(r"\bPM\b")
_LQ, _RQ = chr(0x201C), chr(0x201D)
_QUOTED = re.compile(rf'"[^"\n]*"|{_LQ}[^{_RQ}\n]*{_RQ}|(?<!\w)\'[^\'\n]*\'(?!\w)')
_CODE = re.compile(r"`[^`\n]*`")
_REF_LIKE = re.compile(r"[\w.-]+/[\w./-]+|[A-Za-z][\w.]*-[\w.-]*\d{6,}")
_SENTENCE_END = re.compile(r"(?<=[.;:!?])\s+|\s+[-" + chr(0x2013) + chr(0x2014) + r"]\s+")
_CLAUSE_SEP = re.compile(
    r"\s*,\s*(?:(?:but|and|then|so|or|nor)\s+)?|\s+(?:but|and|then|so|or|nor)\s+", re.IGNORECASE
)
_NO_CARRY = {"but", "then", "so"}
_LIST_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\||#)")


# Protocol-bound marker of the task block that ends a brief (T-021; documented in .agents/policy/AGENT_TASK_TEMPLATE.md).
TASK_HEADING = "## Task"
# The exact marker line: `## Task brief` of the template body is not it, nor an indented or Setext "Task" heading
# (those are ordinary headings to the scanner, so after the marker they are late headings).
_TASK_LINE = re.compile(r"## Task[ \t]*")

# The block scanner of the new-order readers (T-021 fix rounds 1 and 2), after CommonMark 0.31: top-level fenced code
# (4.5), ATX (4.2) and Setext (4.3) headings told apart from thematic breaks (4.1), paragraphs (4.8), and the extent
# of HTML blocks (4.6), block quotes (5.1) and list items (5.2). The inside of a block quote, list item or HTML block
# is not parsed: its lines are inert text that never changes fence, paragraph or heading state, and an inert line that
# looks like a heading, an underline or a fence delimiter is reported, so `brief-order` refuses what it cannot read
# (fail closed). Indented code (4.4) is text that is never a block start. Patterns apply after the indentation.
_FENCE_OPEN = re.compile(r"(`{3,})[^`]*|(~{3,}).*")  # 4.5: a backtick info string holds no backtick
_FENCE_CLOSE = re.compile(r"(`{3,}|~{3,})[ \t]*")
_FENCE_LIKE = re.compile(r"`{3}|~{3}")
_ATX = re.compile(r"(#{1,6})(?:[ \t].*)?")
_SETEXT = re.compile(r"(=+|-+)[ \t]*")
_BREAK = re.compile(r"(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,}")
_LIST_MARKER = re.compile(r"(?:[-+*]|(\d{1,9})[.)])(?=[ \t]|$)")  # 5.2: bullet or ordered marker, then space or end
# 4.6 start conditions 1-5 with the end marker that closes each (searched from the given offset of the start line);
# conditions 6 and 7 end at a blank line.
_HTML_RAW_TAGS = ("pre", "script", "style", "textarea")
_HTML_MARKED = (
    (
        re.compile(rf"<(?:{'|'.join(_HTML_RAW_TAGS)})(?:[ \t>]|$)", re.IGNORECASE),
        re.compile(rf"</(?:{'|'.join(_HTML_RAW_TAGS)})>", re.IGNORECASE),
        0,
    ),
    (re.compile(r"<!--"), re.compile(r"-->"), 2),
    (re.compile(r"<\?"), re.compile(r"\?>"), 1),
    (re.compile(r"<!\[CDATA\["), re.compile(r"\]\]>"), 9),
    (re.compile(r"<![A-Za-z]"), re.compile(r">"), 2),
)
# Condition 6 tag names (CommonMark 0.30 plus `search` of 0.31; a superset, since a name missing here only makes the
# line a condition-7 candidate). Protocol-bound HTML vocabulary, not natural language.
_HTML_BLOCK_TAGS = frozenset(
    "address article aside base basefont blockquote body caption center col colgroup dd details dialog dir div dl dt "
    "fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li link "
    "main menu menuitem nav noframes ol optgroup option p param search section source summary table tbody td tfoot th "
    "thead title tr track ul".split()
)
_HTML_BLOCK_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)(?:[ \t]|/?>|$)")
_HTML_ATTRIBUTE = r"[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*(?:[ \t]*=[ \t]*(?:[^ \t\"'=<>`]+|'[^']*'|\"[^\"]*\"))?"
_HTML_COMPLETE_TAG = re.compile(
    rf"(?:<[A-Za-z][A-Za-z0-9-]*(?:{_HTML_ATTRIBUTE})*[ \t]*/?>|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*"
)
# Protocol-bound (CommonMark 2.2, 4.1-4.5, 5.2): a tab stop is 4 columns; 4 columns of indentation end block syntax,
# and a list item whose text starts 5 or more columns after its marker begins with indented code.
_TAB_STOP = 4
_CODE_INDENT = 4
_BOM = chr(0xFEFF)  # protocol-bound: the Unicode byte order mark
_UNSUPPORTED = (
    "unsupported Markdown structure in a new-order brief: a heading, Setext underline or fence delimiter inside a "
    "block quote, list item or HTML block, which this lint does not parse; put it at the top level (a blank line "
    "ends an HTML block such as a `<goal>` tag line)"
)


class _Scan(NamedTuple):
    """The text split on LF (`lines`, as given; `bare`, without CR and the first without a leading BOM); per line
    whether it is a top-level fence line or fenced content (`code`) or a line of a block quote, list item or HTML
    block (`inert`); the top-level headings as (index of the first line, level); and the inert lines that look like
    a heading, underline or fence delimiter (`unsupported`)."""

    lines: list[str]
    bare: list[str]
    code: list[bool]
    inert: list[bool]
    headings: list[tuple[int, int]]
    unsupported: list[int]


def _indent(line: str, column: int = 0) -> tuple[int, str]:
    """(column after the leading spaces and tabs of `line`, which starts at `column`; the rest of the line)."""
    for index, char in enumerate(line):
        if char == " ":
            column += 1
        elif char == "\t":
            column += _TAB_STOP - column % _TAB_STOP
        else:
            return column, line[index:]
    return column, ""


def _list_item(column: int, rest: str) -> tuple[int, bool, str] | None:
    """(content column, whether it may interrupt a paragraph, the text after the marker) of the list item that
    `rest`, at `column`, opens (5.2), or None. It may interrupt a paragraph when its text is not blank and an ordered
    one starts at 1."""
    marker = _LIST_MARKER.match(rest)
    if marker is None:
        return None
    end = column + len(marker[0])
    after, content = _indent(rest[len(marker[0]) :], end)
    interrupts = bool(content) and marker[1] in (None, "1")
    if not content or after - end > _CODE_INDENT:  # blank start, or text that begins with indented code
        return end + 1, interrupts, rest[len(marker[0]) :]
    return after, interrupts, rest[len(marker[0]) :]


def _html_start(rest: str, paragraph_open: bool) -> tuple[re.Pattern[str] | None, int] | None:
    """(end marker or None for "ends at a blank line", offset to search the start line from) of the HTML block that
    `rest` opens (4.6), or None. Condition 7, a lone complete tag such as `<goal>`, cannot interrupt a paragraph."""
    for start, end, offset in _HTML_MARKED:
        if start.match(rest):
            return end, offset
    tag = _HTML_BLOCK_TAG.match(rest)
    name = tag[1].lower() if tag else ""
    if name in _HTML_BLOCK_TAGS or (
        not paragraph_open and name not in _HTML_RAW_TAGS and _HTML_COMPLETE_TAG.fullmatch(rest)
    ):
        return None, 0
    return None


def _looks_structural(content: str) -> bool:
    """Whether a line inside a container or HTML block, with its indentation and any container markers taken off,
    looks like an ATX heading, a Setext underline or a fence delimiter. An over-approximation: it only refuses."""
    while True:
        content = content.lstrip(" \t")
        if _ATX.fullmatch(content) or _SETEXT.fullmatch(content) or _FENCE_LIKE.match(content):
            return True
        marker = _LIST_MARKER.match(content)
        if content.startswith(">"):
            content = content[1:]
        elif marker:
            content = content[marker.end() :]
        else:
            return False


def _scan(text: str) -> _Scan:
    lines = text.split("\n")
    bare = [line.removesuffix("\r") for line in lines]
    bare[0] = bare[0].removeprefix(_BOM)  # a byte order mark is no text (str.split always gives one item)
    code: list[bool] = []
    inert: list[bool] = []
    headings: list[tuple[int, int]] = []
    unsupported: list[int] = []
    fence: tuple[str, int] | None = None  # delimiter character and length of the open top-level fence
    paragraph: int | None = None  # first line of the open top-level paragraph
    html: re.Pattern[str] | None | bool = False  # end marker of the open HTML block; None: a blank line; False: none
    container: tuple[bool, int] | None = None  # open outer container: (a block quote, list content column)
    for index, line in enumerate(bare):
        column, rest = _indent(line)
        if fence is not None:
            code.append(True)
            inert.append(False)
            close = _FENCE_CLOSE.fullmatch(rest) if column < _CODE_INDENT else None
            if close and close[1][0] == fence[0] and len(close[1]) >= fence[1]:
                fence = None
            continue
        if html is not False:
            if html is None and not rest:
                html = False  # the blank line ends the block and is not part of it
            else:
                code.append(False)
                inert.append(True)
                if _looks_structural(line):
                    unsupported.append(index)
                if html is not None and html.search(line):
                    html = False
                continue
        if container is not None:
            quote, offset = container
            content: str | None = None
            if quote:
                content = rest[1:] if column < _CODE_INDENT and rest.startswith(">") else None
            elif not rest or column >= offset:
                content = line
            elif column < _CODE_INDENT and not _BREAK.fullmatch(rest):
                sibling = _list_item(column, rest)  # a marker left of the content column: the next item
                if sibling is not None:
                    container, content = (False, sibling[0]), sibling[2]
            # No lazy continuation (5.1, 5.2): such a line starts a top-level paragraph here, which can only add a
            # Setext heading under it, never hide one.
            if content is not None:
                code.append(False)
                inert.append(True)
                if _looks_structural(content):
                    unsupported.append(index)
                continue
            container = None
        code.append(False)
        inert.append(False)
        if not rest:
            paragraph = None
            continue
        if column >= _CODE_INDENT:
            # Not a heading, fence or underline (4.2-4.5): text that continues a paragraph or is indented code.
            continue
        opened = _FENCE_OPEN.fullmatch(rest)
        atx = _ATX.fullmatch(rest)
        if opened:
            delimiter = opened[1] or opened[2]
            fence = (delimiter[0], len(delimiter))
            code[-1] = True
            paragraph = None
            continue
        if atx:
            headings.append((index, len(atx[1])))
            paragraph = None
            continue
        if paragraph is not None and _SETEXT.fullmatch(rest):
            headings.append((paragraph, 1 if rest[0] == "=" else 2))  # the underline takes precedence over a break
            paragraph = None
            continue
        if _BREAK.fullmatch(rest):  # also before a list item (4.1)
            paragraph = None
            continue
        block = _html_start(rest, paragraph is not None)
        item = _list_item(column, rest)
        structural = False
        if block is not None:
            end, offset = block
            html = end if end is None or not end.search(rest, offset) else False
        elif rest.startswith(">"):
            container = (True, 0)
            structural = _looks_structural(rest[1:])
        elif item is not None and (paragraph is None or item[1]):
            container = (False, item[0])
            structural = _looks_structural(item[2])
        else:
            if paragraph is None:
                paragraph = index
            continue
        inert[-1] = True
        paragraph = None
        if structural:
            unsupported.append(index)
    return _Scan(lines, bare, code, inert, headings, unsupported)


def _task_lines(scan: _Scan) -> list[int]:
    """Indexes of the `## Task` marker headings, in order."""
    return [i for i, level in scan.headings if level == 2 and _TASK_LINE.fullmatch(scan.bare[i])]


def _footer_span(scan: _Scan, footer: str | None) -> tuple[int, int] | None:
    """Lines [start, end) of the first verbatim copy of the footer (lines compared as `_clean` compares them) that
    starts at the top level (outside fenced code, block quotes, list items and HTML blocks), or None when the footer
    is unknown or not there."""
    want = _clean(footer).split("\n") if footer and footer.strip() else []
    if not want:
        return None
    have = [line.rstrip() for line in scan.bare]
    for start in range(len(have) - len(want) + 1):
        top = not scan.code[start] and not scan.inert[start]
        if have[start] == want[0] and top and have[start : start + len(want)] == want:
            return start, start + len(want)
    return None


def _footer_heading(scan: _Scan) -> int | None:
    """Index of the first top-level level-1 heading that opens with the footer heading, or None."""
    return next(
        (
            i
            for i, level in scan.headings
            if level == 1 and _indent(scan.bare[i])[1].startswith(FOOTER_HEADING)
        ),
        None,
    )


def _strip_lines(scan: _Scan, footer: str | None) -> tuple[int, int] | None:
    """Lines [start, end) the rules treat as the footer. New order: exactly the verbatim footer span, when a `## Task`
    marker follows it, so all text around it is linted. Old order (no marker after it): from the footer to the end,
    as before T-021. Footer text unknown or not verbatim: from the first footer heading outside code to the next
    marker, else to the end (the `footer` rule of a full lint reports the missing verbatim copy)."""
    tasks = _task_lines(scan)
    span = _footer_span(scan, footer)
    if span is not None:
        return span if any(t >= span[1] for t in tasks) else (span[0], len(scan.lines))
    heading = _footer_heading(scan)
    if heading is None:
        return None
    return heading, next((t for t in tasks if t > heading), len(scan.lines))


def _strip_footer(text: str, footer: str | None, *, order: bool) -> str:
    """The text without the footer. `order` (a new-order brief): the lines of `_strip_lines` become empty but stay, so
    line numbers are unchanged; `footer` is the text of .agents/policy/AGENT_BRIEF_FOOTER.md, or None when it could not
    be read. Not `order` (a worker started before T-021): the base cut at the first footer heading, byte for byte, so
    no new-order inference (a `## Task` line, a span) reaches a legacy send."""
    if not order:
        cut = text.find(FOOTER_HEADING)
        return text if cut < 0 else text[:cut]
    scan = _scan(text)
    cut = _strip_lines(scan, footer)
    if cut is None:
        return text
    start, end = cut
    return "\n".join(scan.lines[:start] + [""] * (end - start) + scan.lines[end:])


def _order_findings(text: str, footer: str | None) -> list[Finding]:
    """Rule `brief-order`: one top-level `## Task` marker, the verbatim top-level footer ending above it, no level-1
    or level-2 heading (ATX or Setext) below it, and no heading, underline or fence delimiter inside a block quote,
    list item or HTML block anywhere (unparsed, so refused). Headings in fenced code do not count."""
    scan = _scan(text)
    tasks = _task_lines(scan)
    layout = (
        "context first, task last: wrapper, header lines, context, role pack, footer, then one `## Task` block "
        "with Goal, Done check, Scope, Out of scope and Output (T-021, N127)"
    )
    findings = [Finding("brief-order", n + 1, _UNSUPPORTED) for n in scan.unsupported]
    if not tasks:
        message = f"no `{TASK_HEADING}` heading line outside fenced code; {layout}"
        return [Finding("brief-order", 1, message), *findings]
    findings += [
        Finding("brief-order", n + 1, f"a second `{TASK_HEADING}` heading; a brief has one task block; {layout}")
        for n in tasks[1:]
    ]
    span = _footer_span(scan, footer)
    if span is None or span[1] > tasks[0]:
        message = (
            "the verbatim footer (.agents/policy/AGENT_BRIEF_FOOTER.md) does not stand at the top level before "
            f"`{TASK_HEADING}`; {layout}"
        )
        findings.append(Finding("brief-order", tasks[0] + 1, message))
    findings += [
        Finding("brief-order", n + 1, f"a level-1 or level-2 heading after `{TASK_HEADING}`; {layout}")
        for n, level in scan.headings
        if n > tasks[0] and level <= 2 and n not in tasks
    ]
    return findings


def _mask(text: str) -> str:
    """Blank quoted text and code (same length); a ref-like code span becomes the word REFNAME."""

    def code(match: re.Match[str]) -> str:
        span = match.group()
        if len(span) >= 9 and _REF_LIKE.fullmatch(span[1:-1]):
            return ("REFNAME" + " " * len(span))[: len(span)]
        return " " * len(span)

    text = _CODE.sub(code, text)
    return _QUOTED.sub(lambda m: " " * len(m.group()), text)


def _legacy_code(lines: list[str]) -> list[bool]:
    """The base fence reading, kept byte for byte for workers started before T-021: a line whose stripped text opens
    with three backticks toggles a fence and is itself fenced."""
    flags: list[bool] = []
    in_fence = False
    for line in lines:
        toggle = line.strip().startswith("```")
        in_fence ^= toggle
        flags.append(toggle or in_fence)
    return flags


def _code_lines(text: str, *, order: bool, splitlines: bool) -> list[tuple[str, bool]]:
    """(line, fenced) pairs: the scanner's lines (without CR and a leading BOM) and top-level fences for a new-order
    brief; else the base reading on the base split of the calling reader (`str.splitlines` or LF)."""
    if order:
        scan = _scan(text)
        return list(zip(scan.bare, scan.code, strict=True))
    lines = text.splitlines() if splitlines else text.split("\n")
    return list(zip(lines, _legacy_code(lines), strict=True))


def _blocks(text: str, *, order: bool) -> list[list[tuple[int, str]]]:
    """Paragraph blocks of (line number, stripped line); wrapped lines of one paragraph stay together. Fenced code
    (`_code_lines`) and blockquote lines are skipped."""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    rows = _code_lines(text, order=order, splitlines=True)
    for number, (line, in_code) in enumerate(rows, start=1):
        stripped = line.strip()
        skip = in_code or stripped.startswith(">")
        if skip or not stripped or _LIST_START.match(line):
            if current:
                blocks.append(current)
            current = []
        if skip or not stripped:
            continue
        current.append((number, stripped))
    if current:
        blocks.append(current)
    return blocks


def _segments(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str, str]]:
    """(start offset, segment, lower-cased separator before it) for each piece between separators."""
    out: list[tuple[int, str, str]] = []
    pos, before = 0, ""
    for match in pattern.finditer(text):
        out.append((pos, text[pos : match.start()], before))
        pos, before = match.end(), match.group().strip().lower()
    out.append((pos, text[pos:], before))
    return out


def _lint_sentence(sentence: str) -> int | None:
    """Offset of the first Git-write request in the sentence, or None."""
    carry = False
    for start, clause, separator in _segments(sentence, _CLAUSE_SEP):
        words = re.findall(r"[a-z]+", separator)
        inherit = carry and bool(separator) and not (words and words[-1] in _NO_CARRY)
        git = _GIT_CMD.search(clause) or _BRANCH.search(clause)
        bare = _VERB.search(clause)
        hit = git or bare
        if hit is None:
            carry = False
            continue
        is_action = git is not None or (
            bare is not None and _OBJECT.search(clause[bare.end() :]) is not None
        )
        allowed = bool(_NEGATION.search(clause) or _PM.search(clause[: hit.start()])) or inherit
        if is_action and not allowed:
            return start + hit.start()
        carry = allowed
    return None


def lint_brief(text: str, *, footer: str | None, order: bool) -> list[tuple[int, str]]:
    """Return (1-based line number, offending sentence) for every worker Git-write request outside the footer;
    `footer` and `order` as for `_strip_footer`."""
    findings: list[tuple[int, str]] = []
    for block in _blocks(_strip_footer(text, footer, order=order), order=order):
        joined = _mask(" ".join(line for _, line in block))
        starts: list[int] = []
        offset = 0
        for _, line in block:
            starts.append(offset)
            offset += len(line) + 1
        for s_start, sentence, _ in _segments(joined, _SENTENCE_END):
            found = _lint_sentence(sentence)
            if found is not None:
                index = bisect.bisect_right(starts, s_start + found) - 1
                findings.append((block[index][0], sentence.strip()))
    return findings


class Finding(NamedTuple):
    rule: str
    line: int
    message: str


# Brief kinds (protocol-bound: the header vocabulary documented in .agents/policy/AGENT_TASK_TEMPLATE.md and the footer).
KINDS = ("feature", "code", "review", "docs", "scout", "run", "trivial")
# Kinds whose diff or subject can change code, configs or contracts: they carry the class test sets (#25).
SETS_KINDS = ("feature", "code", "review")
# Kinds that change code and so carry the approach note (decision 582): the approach scout comes first.
APPROACH_KINDS = ("feature", "code")
BASE_URL = "ANTHROPIC_BASE_URL"
_HEADER = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}(Kind|R-IDs|Task file|Root cause|Class sweep|Approach note)\*{0,2}"
    r"\s*:\s*\*{0,2}\s*(.*?)\s*$",
    re.IGNORECASE,
)
# Decision 568 (user 2026-11-05: no fix loops, root cause and audit before any fix): a fix brief, i.e. a
# `[NEW TASK ...]` tag naming a fix, carries the root cause and the bug-class sweep as header lines.
_TAG_TEXT = re.compile(r"^(?:[\s#>*_`+|-]|<!--)*\[\s*new\s+task([^\]\n]*)", re.IGNORECASE)
_FIX_WORD = re.compile(r"(?<![a-z])fix", re.IGNORECASE)
# Layout of the wording check, not a deployment choice: shortest header value that can name a cause or a sweep.
_RCA_MIN_CHARS = 20
# Same layout reason for the approach note: shortest value that can name a project path or `none: <reason>`.
_APPROACH_MIN_CHARS = 20
_APPROACH_NONE = re.compile(r"none\b", re.IGNORECASE)
# A skipped scout is justified by a file actually read, not by a phrase.
_APPROACH_READ_FILE = re.compile(r"[\w./-]+\.(?:py|md|yaml|yml|json|ps1)\b")
_RID = re.compile(r"(?<![\w-])R-\d+[a-z]?(?![\w-])")
_SETS_WAIVER = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}Test sets\*{0,2}\s*:\s*\*{0,2}\s*not applicable\s*:\s*(\S.{9,})$",
    re.IGNORECASE,
)
# Class test sets a code brief must name (mistake #25) come from this repository-bound file (no source default).
CLASS_SETS_CONFIG = Path(__file__).resolve().parent / "brief_lint_config.json"
_SETS_KEYS = ("required", "alternatives")
_ENTRY_KEYS = ("label", "pattern")


class ClassSetsConfigError(ValueError):
    """`brief_lint_config.json` is missing, unreadable or invalid."""


class ClassSets(NamedTuple):
    """Entries are (label, compiled pattern). A brief names every `required` entry and every entry of one group."""

    required: tuple[tuple[str, re.Pattern[str]], ...]
    alternatives: dict[str, tuple[tuple[str, re.Pattern[str]], ...]]


def _entries(value: object, where: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    if not isinstance(value, list):
        raise ClassSetsConfigError(f"{where}: expected a list of {{label, pattern}} entries")
    entries = []
    for index, item in enumerate(value):
        here = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ClassSetsConfigError(f"{here}: expected an object with label and pattern")
        for key in _ENTRY_KEYS:
            if key not in item:
                raise ClassSetsConfigError(f"{here}: missing key {key}")
            if not isinstance(item[key], str) or not item[key].strip():
                raise ClassSetsConfigError(f"{here}.{key}: expected a non-empty string")
        unknown = sorted(set(item) - set(_ENTRY_KEYS))
        if unknown:
            raise ClassSetsConfigError(f"{here}: unknown key(s) {', '.join(unknown)}")
        try:
            pattern = re.compile(item["pattern"], re.IGNORECASE)
            matches_empty = pattern.search("") is not None
        except Exception as error:  # noqa: BLE001 - the compiler also raises OverflowError, RecursionError, ...
            # Any failure of the configuration must block, never escape: cli_worker allows a crashed linter.
            raise ClassSetsConfigError(
                f"{here}.pattern: invalid regular expression ({type(error).__name__}): {error}"
            ) from None
        if matches_empty:
            # Such a pattern is met by every brief, so the set would silently never be required.
            raise ClassSetsConfigError(f"{here}.pattern: matches the empty text, so it never fails")
        entries.append((item["label"], pattern))
    return tuple(entries)


def load_class_sets(path: Path | None = None) -> ClassSets:
    """Validated class test sets; both keys are required and no source default exists (AGENTS.md section 5)."""
    path = CLASS_SETS_CONFIG if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError, MemoryError) as error:  # deep nesting, huge file
        raise ClassSetsConfigError(f"cannot read {path}: {error!r}") from None
    if not isinstance(data, dict):
        raise ClassSetsConfigError(f"{path} must hold a JSON object")
    body = {key: value for key, value in data.items() if not key.startswith("_")}
    for key in _SETS_KEYS:
        if key not in body:
            raise ClassSetsConfigError(f"{path}: missing key {key}")
    unknown = sorted(set(body) - set(_SETS_KEYS))
    if unknown:
        raise ClassSetsConfigError(f"{path}: unknown key(s) {', '.join(unknown)}")
    required = _entries(body["required"], f"{path}: required")
    groups = body["alternatives"]
    if not isinstance(groups, dict) or not groups:
        raise ClassSetsConfigError(f"{path}: alternatives must be a non-empty object of named groups")
    alternatives = {}
    for name, value in groups.items():
        entries = _entries(value, f"{path}: alternatives.{name}")
        if not entries:
            raise ClassSetsConfigError(f"{path}: alternatives.{name} must list at least one entry")
        alternatives[name] = entries
    return ClassSets(required, alternatives)


def missing_class_sets(body: str, sets: ClassSets) -> list[str]:
    """Labels a brief still has to name: every `required` entry, plus the nearest group (fewest labels missing,
    first in the file on a tie), written `<group>: <label>`. Empty when the brief names the class sets."""
    missing = [label for label, pattern in sets.required if not pattern.search(body)]
    per_group = {
        name: [label for label, pattern in entries if not pattern.search(body)]
        for name, entries in sets.alternatives.items()
    }
    if all(per_group.values()):
        nearest = min(per_group, key=lambda name: len(per_group[name]))
        missing += [f"{nearest}: {label}" for label in per_group[nearest]]
        missing.append(f"(name every set of one group: {', '.join(per_group)})")
    return missing
_SMOKE = re.compile(
    r"(?:one|1)[- ]job\s+smoke|smoke\s+(?:one|1)\s+job|smoke\s+test\s+of\s+(?:one|1)\s+job",
    re.IGNORECASE,
)
_GATE_CHECK = re.compile(r"run_approval_gate\.py\s+check")
_PREFLIGHT = re.compile(r"gpu_preflight")
_GEMINI_ABS = re.compile(
    r"-(?:PromptPath|Workspace|TaskPath)\s+[\"']?(?:[A-Za-z]:[\\/]|/)", re.IGNORECASE
)
_INLINE_PY = re.compile(r"python[\w.]*\s+(?:-\s*<<|-c\s+[\"'][^\"'\n]*$)", re.IGNORECASE)
_BASE_ASSIGN_EMPTY = re.compile(r"[`*]{0,2}\s*=\s*(?:\"\"|'')?\s*$")
# "empty"/"blank" right after the name ("X empty", "X is blank", "X set it to empty"); a later sentence about
# something else being empty ("X unset and the output directory empty") is not about the variable.
_EMPTY_AFTER = re.compile(
    r"^[`*\"'(:\s]{0,5}(?:(?:is|are|was|be|being|stays?|left|set|it|to|as|kept|keep|an?)\s+){0,4}"
    r"(?:empty|blank)\b",
    re.IGNORECASE,
)
_EMPTY_BEFORE = re.compile(r"\b(?:empty|blank)\s+(?:value\s+)?[`*(]*$", re.IGNORECASE)
_BASE_NEGATION = re.compile(r"\b(?:never|not|no|don't|dont|instead|rather)\b", re.IGNORECASE)
_BASE_UNSET = re.compile(
    rf"{BASE_URL}[`*\s]{{0,4}}(?:(?:is|stays|stay|must\s+be|being|remains)\s+)?unset",
    re.IGNORECASE,
)
# Layout of the wording check, not a deployment choice: how far before the name "empty" still refers to it.
_BASE_BEFORE = 25


def _clean(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


def header_fields(text: str, *, order: bool) -> dict[str, tuple[int, str]]:
    """First `Kind:`, `R-IDs:`, `Task file:`, `Root cause:`, `Class sweep:` and `Approach note:` header line outside
    fenced code (`_code_lines`; a list-item header line counts, as the pattern allows): key -> (line, value)."""
    found: dict[str, tuple[int, str]] = {}
    for number, (line, in_code) in enumerate(_code_lines(text, order=order, splitlines=False), start=1):
        match = None if in_code else _HEADER.match(line)
        if match:
            found.setdefault(match[1].lower(), (number, match[2].strip("` *")))
    return found


def _fold(text: str) -> str:
    """NFKC (fullwidth brackets and colons become ASCII), no format characters (BOM, zero-width space), LF lines."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return folded.replace("\r\n", "\n").replace("\r", "\n")


# A line that opens with the tag, after whitespace and the markup a heading, quote, list or comment adds.
_TAG_LINE = re.compile(r"^(?:[\s#>*_`+|-]|<!--)*\[\s*new\s+task", re.IGNORECASE)


def is_brief_message(text: str, *, footer: str | None, order: bool) -> bool:
    """Whether a worker message is a task brief and gets the full lint. Allowlist invariant: a message is a brief iff,
    after folding, some line opens with a `[NEW TASK` tag (any case, any line, a fence does not hide it) or a `Kind:`
    header stands outside a fenced block. Feedback (`[QA FEEDBACK]`, `[RETEST]`, `[EVALUATE]`) without a `Kind:`
    header is not one and keeps the always-rules. Fail closed: a doubtful message is linted, and a waiver is logged.
    `footer` and `order` as for `_strip_footer`, so the footer is the one the rules skip for that worker's layout."""
    folded = _fold(_strip_footer(text, footer, order=order))
    return any(_TAG_LINE.match(line) for line in folded.split("\n")) or "kind" in header_fields(
        folded, order=order
    )


def is_fix_brief(text: str) -> bool:
    """Whether some `[NEW TASK ...]` tag line (after folding) names a fix: `fix`, `fixes`, `fix2`, `fix round`.
    A word that merely contains the letters after another letter (`prefix`) does not count."""
    folded = _fold(text)
    return any(
        (match := _TAG_TEXT.match(line)) is not None and _FIX_WORD.search(match[1]) is not None
        for line in folded.split("\n")
    )


def read_task_file(root: Path, relative: str) -> tuple[str | None, str | None]:
    """(text of the `Task file:` or None, problem or None). The path is resolved first and must stay inside `root`:
    an absolute path or a `..` walk to another checkout is refused, never read."""
    try:
        base = root.resolve()
        target = (base / relative).resolve()
    except (OSError, ValueError, RuntimeError):
        return None, f"`Task file:` path {relative!r} cannot be resolved"
    if not target.is_relative_to(base):
        return (
            None,
            f"`Task file:` {relative!r} is outside the checkout {base.name}; only project files are read",
        )
    return _read(target), None


def task_file_findings(
    text: str, root: Path, *, footer: str | None, order: bool
) -> tuple[str | None, list[Finding]]:
    """(text of the brief's `Task file:`, findings): an outside path is a finding (rule `task-file`), not a read.
    `footer` and `order` as for `_strip_footer`."""
    body = _strip_footer(text.replace("\r\n", "\n"), footer, order=order)
    row = header_fields(body, order=order).get("task file")
    if not row:
        return None, []
    content, problem = read_task_file(root, row[1])
    return content, [Finding("task-file", row[0], problem)] if problem else []


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _same_approach_task_path(approach: str, task: str) -> bool:
    # Explicit Windows path syntax selects its lexical rules on either host;
    # otherwise native path rules preserve POSIX case sensitivity.
    paths = (approach, task)
    windows = any(PureWindowsPath(path).drive or "\\" in path for path in paths)
    normalizer = ntpath if windows else os.path
    normalized_approach: str = normalizer.normcase(normalizer.normpath(approach))
    normalized_task: str = normalizer.normcase(normalizer.normpath(task))
    return normalized_approach == normalized_task


def _base_url_findings(body: str) -> list[Finding]:
    """Mistake #27: the variable is never "empty"; a brief that names it says `unset`."""
    findings: list[Finding] = []
    named = False
    for match in re.finditer(BASE_URL, body):
        named = True
        start, end = match.span()
        rest = body[end:].split("\n", 1)[0]
        before = body[:start].rsplit("\n", 1)[-1][-_BASE_BEFORE:]
        empty = (
            _BASE_ASSIGN_EMPTY.match(rest) is not None
            or _EMPTY_AFTER.match(rest) is not None
            or (_EMPTY_BEFORE.search(before) is not None and not _BASE_NEGATION.search(before))
        )
        if empty:
            message = f"{BASE_URL} is never empty: write `{BASE_URL}` unset (remove the variable)"
            findings.append(Finding("base-url-empty", _line_of(body, start), message))
    if named and not findings and not _BASE_UNSET.search(body):
        findings.append(
            Finding(
                "base-url-unset",
                1,
                f"the brief names {BASE_URL} but never writes `{BASE_URL}` unset "
                "(remove the variable, never set it to empty)",
            )
        )
    return findings


def _line_findings(body: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(body.split("\n"), start=1):
        if _GEMINI_ABS.search(line):
            message = "gemini script paths are relative to the repository (mistakes #12, #18)"
            findings.append(Finding("gemini-absolute-path", number, message))
        if _INLINE_PY.search(line):
            message = "multi-line inline Python is blocked: use a script file (mistake #14)"
            findings.append(Finding("inline-python", number, message))
    return findings


def _slice_ids(task_text: str, *, order: bool) -> list[str]:
    """First-column ids of the first Markdown table whose first header cell starts with `Slice`; for a new-order
    brief outside top-level fenced code (the base reading, kept for older workers, ignores fences)."""
    ids: list[str] = []
    in_table = False
    for line, in_code in _code_lines(task_text.replace("\r\n", "\n"), order=order, splitlines=False):
        stripped = line.strip()
        if (order and in_code) or not stripped.startswith("|"):
            if in_table:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        first = cells[0].strip("` *")
        if not in_table:
            in_table = first.lower().startswith("slice")
            continue
        token = re.match(r"[\w.-]+", first)
        if token and not re.fullmatch(r":?-{2,}:?", first):
            ids.append(token[0])
    return ids


def lint_message(
    text: str,
    *,
    full: bool,
    order: bool,
    footer: str | None = None,
    task_file_text: str | None = None,
    class_sets_path: Path | None = None,
) -> list[Finding]:
    """All findings of one worker message; `full` adds the rules of a complete brief (see the module doc).

    `order` holds the message to the T-021 layout: the readers use the block scanner and a full lint adds rule
    `brief-order`. Without it (a worker started before T-021) every reader is the base one, byte for byte. It has no
    default, so every caller states the worker's layout;
    `footer` is the text of .agents/policy/AGENT_BRIEF_FOOTER.md (None = unreadable, itself a finding of a full lint);
    every rule skips the footer (`_strip_footer`), and `brief-order` places the task block after its verbatim span;
    `task_file_text` is the text of the brief's `Task file:` (None = no slice check);
    `class_sets_path` is the class-test-set file (None = `brief_lint_config.json` next to this module)."""
    text = text.replace("\r\n", "\n")
    body = _strip_footer(text, footer, order=order)
    findings = [
        Finding("git-write", n, f"worker Git write requested: {s}")
        for n, s in lint_brief(text, footer=footer, order=order)
    ]
    findings += _base_url_findings(body)
    findings += _line_findings(body)
    if full:
        findings += _brief_findings(text, body, footer, task_file_text, class_sets_path, order=order)
        if order:
            findings += _order_findings(text, footer)
    return sorted(findings, key=lambda f: (f.line, f.rule))


def _brief_findings(
    text: str,
    body: str,
    footer: str | None,
    task_file_text: str | None,
    class_sets_path: Path | None = None,
    *,
    order: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    if footer is None or not footer.strip():
        message = ".agents/policy/AGENT_BRIEF_FOOTER.md could not be read: the footer cannot be checked"
        findings.append(Finding("footer", 1, message))
    elif _clean(footer) not in _clean(text):
        message = "the brief does not carry .agents/policy/AGENT_BRIEF_FOOTER.md verbatim"
        findings.append(Finding("footer", 1, message))
    header = header_fields(body, order=order)
    kind_row = header.get("kind")
    kind = kind_row[1].lower() if kind_row else ""
    if kind not in KINDS:
        shown = f"`{kind_row[1]}`" if kind_row else "missing"
        message = f"`Kind:` header {shown}; allowed: {', '.join(KINDS)}"
        findings.append(Finding("kind", kind_row[0] if kind_row else 1, message))
    if kind == "feature" and not _RID.search(header.get("r-ids", (0, ""))[1]):
        message = "a feature brief needs an `R-IDs:` header line with at least one R-<n>"
        findings.append(Finding("r-ids", 1, message))
    if kind in SETS_KINDS:
        # A broken configuration is a finding (cli_worker lets a linter crash through), also behind a waiver.
        try:
            class_sets = load_class_sets(class_sets_path)
        except ClassSetsConfigError as error:
            findings.append(Finding("test-sets", 1, f"the class test sets cannot be loaded: {error}"))
        else:
            # New order: a waiver line quoted in fenced code waives nothing (the base reading takes every line).
            waived = any(
                _SETS_WAIVER.match(line)
                for line, in_code in _code_lines(body, order=order, splitlines=False)
                if not (order and in_code)
            )
            try:
                missing = [] if waived else missing_class_sets(body, class_sets)
            except Exception as error:  # noqa: BLE001 - a failure while matching must block as well
                missing = []
                findings.append(
                    Finding(
                        "test-sets",
                        1,
                        f"the class test sets cannot be applied: {type(error).__name__}: {error}",
                    )
                )
            if missing:
                message = (
                    "name the class test sets (mistake #25) or add `Test sets: not applicable: "
                    f"<reason>`; missing: {'; '.join(missing)}"
                )
                findings.append(Finding("test-sets", 1, message))
    if kind != "review" and is_fix_brief(body):
        absent = [
            label
            for label, key in (("`Root cause:`", "root cause"), ("`Class sweep:`", "class sweep"))
            if len(header.get(key, (0, ""))[1]) < _RCA_MIN_CHARS
        ]
        if absent:
            message = (
                f"a fix brief states {' and '.join(absent)} (at least {_RCA_MIN_CHARS} characters each): "
                "root cause and the audit of every sibling site come before any fix (decision 568)"
            )
            findings.append(Finding("root-cause", 1, message))
    approach = header.get("approach note", (0, ""))[1]
    task_file = header.get("task file", (0, ""))[1]
    skipped_scout = _APPROACH_NONE.match(approach) is not None
    if kind in APPROACH_KINDS and (
        len(approach) < _APPROACH_MIN_CHARS
        or (skipped_scout and (kind == "feature" or not _APPROACH_READ_FILE.search(approach)))
        or (not skipped_scout and task_file and _same_approach_task_path(approach, task_file))
    ):
        message = (
            "a feature brief needs an `Approach note:` path distinct from `Task file:`; a code brief may instead "
            "use `none: <reason>` naming the file read. "
            f"The value is at least {_APPROACH_MIN_CHARS} characters: the approach scout comes before the code "
            "(decision 582)"
        )
        findings.append(Finding("approach-note", 1, message))
    if kind == "run":
        needs = [
            label
            for label, pattern in (
                ("the one-job smoke", _SMOKE),
                ("`run_approval_gate.py check`", _GATE_CHECK),
                ("`gpu_preflight`", _PREFLIGHT),
            )
            if not pattern.search(body)
        ]
        if needs:
            message = f"a run brief names {', '.join(needs)} (mistakes #8, #9)"
            findings.append(Finding("run-gates", 1, message))
    if task_file_text is not None:
        absent = [
            ident
            for ident in _slice_ids(task_file_text, order=order)
            if not re.search(rf"(?<![\w.-]){re.escape(ident)}(?![\w-])", body)
        ]
        if absent:
            message = (
                f"slice ids of the task file missing from the brief: {', '.join(absent)} (#26)"
            )
            findings.append(Finding("slices", 1, message))
    return findings


def _read(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("briefs", nargs="+", type=Path)
    parser.add_argument(
        "--require-footer", action="store_true", help="also fail when the footer heading is absent"
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="lint each file as a complete brief (Kind, footer, R-IDs, test sets, run gates, slices)",
    )
    parser.add_argument(
        "--footer",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".agents" / "policy" / "AGENT_BRIEF_FOOTER.md",
        help="footer file compared with --brief (default: .agents/policy/AGENT_BRIEF_FOOTER.md of this checkout)",
    )
    parser.add_argument(
        "--legacy-order",
        action="store_true",
        help="with --brief: lint a brief in the order used before T-021 (footer last) as the base lint did, "
        "without rule brief-order",
    )
    args = parser.parse_args(argv)
    if args.legacy_order and not args.brief:
        parser.error("--legacy-order only applies to --brief (the order rule is part of the complete-brief lint)")
    # Like cli_worker: only a complete new-order brief gets the scanner readers; a message lint keeps the base ones.
    order = args.brief and not args.legacy_order
    failed = False
    footer = _read(args.footer)  # also bounds the skipped footer span of a new-order brief
    for path in args.briefs:
        text = path.read_text(encoding="utf-8-sig")
        task_text, extra = (
            task_file_findings(text, args.footer.parents[2], footer=footer, order=order)
            if args.brief
            else (None, [])
        )
        findings = lint_message(
            text,
            full=args.brief,
            order=order,
            footer=footer,
            task_file_text=task_text,
        )
        findings = sorted([*findings, *extra], key=lambda f: (f.line, f.rule))
        for finding in findings:
            print(f"{path}:{finding.line}: [{finding.rule}] {finding.message}")
            failed = True
        heading = (
            _footer_heading(_scan(text.replace("\r\n", "\n"))) is not None
            if order
            else FOOTER_HEADING in text
        )
        if args.require_footer and not heading:
            print(f"{path}: footer missing")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
