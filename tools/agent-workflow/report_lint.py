"""Report lint for task files (T-021 phase P2, R-021): does the Completion report hold the done-check output and
evidence pointers?

Usage: python -B -I report_lint.py <task file>...
Exit 0: clean. Exit 1: findings (one `file:line: [rule] text` per hit). Exit 2: a file that cannot be read, a file the
lint could not finish (`cannot lint`, an internal error), or the pinned parser (`markdown-it-py`, `PARSER_VERSION`) is
missing or another version.
`cli_worker.py` runs the same check after every worker turn on the registered `--task-file`; there it only warns and
records the findings (the turn has ended, so it cannot block), within the deadline `guard_config.json`
`report_lint.timeout_seconds` (`lint_report(..., timeout=...)`; past it `LintDeadline` is raised and recorded).

The report is read from the token stream of markdown-it-py (CommonMark preset with GFM tables, decision 127), never
from a hand-written block scanner or a regular expression over Markdown structure. The parser decides every block and
inline construct (containers, lazy lines, fences, HTML, code spans, links, references, images, emphasis).

Input: every report of the task file. A report section is a top-level level-2 heading whose plain text (the text and
code spans it shows, image descriptions included, HTML dropped) starts with the template's title `Completion report`
(case-folded) and then ends or continues with a space, `(` or `:`; a heading that starts with the title and goes on
otherwise, a title heading of another level outside a report, or one inside a container is a `report-section` finding
(never skipped in silence). Inside a section, a deeper top-level heading that starts with the title, or whose own body
carries the template's `Work completed` or `Result and evidence` bold label, opens a nested report that runs to the
next one; each report is linted on its own. A report is written when it holds any non-blank line that is no top-level
heading and no top-level HTML comment block; one that is blank or is the template's own text (`AGENT_TASK_TEMPLATE.md`)
is unwritten: not linted and listed in `unwritten`. For each written report:
- done-check-output: a `Result and evidence` label (a paragraph that starts, or has a line that starts, with that
  title in bold, or a heading of that title) is followed, before the next top-level heading, by a fenced code block
  (at any container depth) with at least one non-blank line;
- evidence-table: a heading titled `Research evidence` is followed, before the next top-level heading, by top-level
  tables, each of the template's five columns with at least one data row (a row of empty cells is the template's
  placeholder); every data row has five cells (counted by the parser's own cell splitter, since the parser pads or
  cuts rows) and a class cell that is one of `CLASSES` (case-folded);
- evidence-pointer: the source cell (second column) of every data row shows a pointer of a fixed form: `path:line`
  or `path:line-line` where the path has a separator or an extension, an `http(s)://` URL (in shown text or a link
  destination), or a non-blank inline code span. HTML, image parts and link titles show no pointer.
What the parser does not read is a `report-section` finding naming the cause, never a pass: a container nested to the
parser's `maxNesting` (its content is dropped; reported once per top-level block anywhere in the file, before and
independent of report discovery, since the dropped part may itself be a report), a link nested to `maxNesting` (it is
not resolved), and an inline block longer than `INLINE_MAX` characters (not parsed, to keep the lint linear).
Structure only: no wording is read and no word list is used. A bare file name with no separator or extension
(`Makefile:3`) is no pointer: the lint reads the text alone, never the checkout, so the finding recommends
`./Makefile:3`. Raw text quoted in a message is cut to `PREVIEW_MAX` characters. Running time is linear in the input:
one block parse (the reference rule's multi-line title is joined once, `_title_continued`, instead of the library's
copy per line), inline parsing only of the blocks the lint reads (each at most `INLINE_MAX`), one pass over the tokens.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

# artifact-bound: the one parser version whose token stream this lint is written and tested against (decision 127);
# a test compares it with the pin in pyproject.toml, and another version is refused, never used silently (R-015).
PARSER_VERSION = "4.2.0"
PARSER_PACKAGE = "markdown-it-py"

# `_PARSER_PROBLEM` is bound once in each exclusive branch (test_module_redefinitions); `lint_report` and `main`
# report it, and the module still loads for cli_worker.
try:
    import markdown_it
except ImportError as _exc:
    _PARSER_PROBLEM: str | None = f"{PARSER_PACKAGE}=={PARSER_VERSION} is required but is not installed ({_exc})"
else:
    _INSTALLED = getattr(markdown_it, "__version__", "unknown")
    if _INSTALLED != PARSER_VERSION:
        _PARSER_PROBLEM = f"{PARSER_PACKAGE}=={PARSER_VERSION} is required, {PARSER_PACKAGE} {_INSTALLED} is installed"
    else:
        _PARSER_PROBLEM = None
        from markdown_it import MarkdownIt, helpers
        from markdown_it.helpers.parse_link_title import _State as _TitleState  # the pinned version's result type
        from markdown_it.rules_block.table import escapedSplit

# Protocol-bound vocabulary of the template (.agents/policy/AGENT_TASK_TEMPLATE.md, "Completion report"), like
# brief_lint.KINDS: heading and label titles, the table width and the four evidence classes.
SECTION_TITLE = "Completion report"
WORK_TITLE = "Work completed"
RESULT_TITLE = "Result and evidence"
EVIDENCE_TITLE = "Research evidence"
TITLE_SEPARATORS = " \t(:"  # what the template family puts after a title: a suffix, `(phase 1)` or `: fix round 1`
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / ".agents" / "policy" / "AGENT_TASK_TEMPLATE.md"
EVIDENCE_COLUMNS = 5
SOURCE_COLUMN = 1  # zero-based: the second column holds the source
CLASS_COLUMN = 4
CLASSES = ("documented", "source-verified", "experiment", "assumption")

# safety-bound: the longest inline block (paragraph, heading, table cell) the lint parses. markdown-it-py's inline
# parser is superlinear inside one block (helpers/parse_link_label.py walks to the block's end from each `[`), so a
# bound keeps the lint linear in the file; measured in `.tmp/T-021/p2r/parser-scaling-capped.log` (T-021 task file,
# "Phase P2 rebuild"). The longest real task-file block is 2184 characters.
INLINE_MAX = 8192
# protocol-bound (decision 127): the CommonMark preset, plus GFM tables for the evidence table.
PARSER_PRESET = "commonmark"
PARSER_EXTRAS = ("table",)
# The block rules that tokenize their content as nested blocks (rules_block/blockquote.py:309,320 and list.py:286);
# at `maxNesting` that nested call drops the content (parser_block.py:75-79), which this lint must not read as absent.
_CONTAINERS = frozenset({"blockquote_open", "list_item_open"})
_DEEP_FLAG = "report_lint_inline_depth"  # env key set by `_depth_guard`
# protocol-bound: the block rule that checks the deadline, the `env` key that holds it, and the terminator chains it
# joins (parser_block.py:30-42), so it also runs at every line that paragraph, lheading, reference, table, blockquote
# and list scan for a terminator.
_DEADLINE_RULE = "report_lint_deadline"
_DEADLINE_KEY = "report_lint_deadline"
_DEADLINE_CHAINS = ["paragraph", "reference", "blockquote", "list"]
# safety-bound: the longest raw text (a heading, a class cell, an error) quoted in a message; the rest is counted.
PREVIEW_MAX = 80

# Pointers in plain text (not Markdown structure): path:line[-line] (no whitespace in the path; the line is digits),
# an http(s) URL. A path is a run of characters other than these (read by `_path_lines`); it may not follow a word
# character, `.`, `/`, `\` or `-`.
_PATH_STOP = frozenset(":`|<>()[]\"'")  # and whitespace (`str.isspace`, as regex `\s`)
_PATH_AFTER = frozenset("./\\-")
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9_+-]*$")  # a file name ends in `.ext`; `Note:1` and `v1.2:3` are no files
_URL = re.compile(r"https?://[^\s<>`)\]|]+", re.IGNORECASE)

_HIDERS = {
    "html_inline": "inline HTML (a comment, a tag or its attributes)",
    "image": "an image (its description, source or title)",
    "title": "a link title",
}
_UNREAD_DEEP = (
    "blocks nested {depth} levels deep (the parser's maxNesting); the parser drops what they hold, so a report "
    "heading, label or table there is not read: nest less"
)
_UNREAD_LINK = (
    "a link or image nested to the parser's maxNesting ({depth}), which the parser does not resolve; this block is "
    "not read: nest fewer brackets"
)
_UNREAD_RECURSION = "inline content nested deeper than the parser can recurse; this block is not read: nest less"
_UNREAD_LONG = (
    "an inline block of {length} characters, more than INLINE_MAX ({limit}), which this lint does not parse; "
    "this block is not read: split it"
)
_NO_POINTER_HINT = " (a file with no separator or extension needs a `./` prefix: `./Makefile:3`)"
_HIDDEN_POINTER = " (the pointer-like text of the cell is in {where}, which shows no pointer: write it as plain text)"


class Finding(NamedTuple):
    rule: str
    line: int
    message: str


class Result(NamedTuple):
    findings: list[Finding]
    unwritten: list[int]  # 1-based heading lines of the reports that were not linted: blank or the template's text


class ParserUnavailable(RuntimeError):
    """The pinned parser is missing or another version (exit 2 on the CLI, `error` in the turn-end record)."""


class LintDeadline(RuntimeError):
    """The lint passed the `timeout` it was given (the turn-end call's `report_lint.timeout_seconds`); the file is
    not read. Recorded in the turn-end record's `error`, so the turn completes."""


def _check_deadline(env: dict[str, Any]) -> None:
    limit = env.get(_DEADLINE_KEY)
    if limit is not None and time.monotonic() >= limit[0]:  # `>=`: Windows' clock ticks in about 16 ms steps
        raise LintDeadline(f"the lint did not finish within {limit[1]} s; the file is not read")


def _deadline_rule(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
    """A block rule that matches nothing (the reading is unchanged) and stops the parse at the deadline. It is first
    in the block chain and in the terminator chains, so it runs at every block start and at every line a multi-line
    rule scans."""
    _check_deadline(state.env)
    return False


def _depth_guard(state: Any, silent: bool) -> bool:
    """An inline rule that matches nothing and records when the inline parser reaches `maxNesting`: from there
    `skipToken` jumps to the block's end and links are misread (parser_inline.py:146-168). Rules run inside
    `skipToken` one level up, so a rule that sees `maxNesting` is in the call whose nested label scan hits the limit.
    It runs at every inline position, so it also checks the deadline."""
    _check_deadline(state.env)
    if state.level >= state.md.options["maxNesting"]:
        state.env[_DEEP_FLAG] = True
    return False


def _title_continued(string: str, start: int, maximum: int, prev_state: Any = None) -> Any:
    """`helpers.parseLinkTitle` with the same result, joined once. The reference rule calls it again for each line of
    a title that has not closed (rules_block/reference.py:111-118), and the library copies the whole title so far into
    each new state (helpers/parse_link_title.py:37,74): quadratic in the title's lines (T-021 P2R-F2). Here a
    continuation calls the library with a fresh state that carries only the marker, so it copies nothing, and the
    unescaped parts (the library unescapes each call's slice on its own, :62,74) are joined when the title ends. Only
    a finished title's `str` is read (reference.py:120-121); an open one keeps its parts."""
    if prev_state is None:
        return helpers.parseLinkTitle(string, start, maximum, None)
    seed = _TitleState()
    seed.marker = prev_state.marker
    step = helpers.parseLinkTitle(string, start, maximum, seed)
    parts: list[str] = prev_state.parts if isinstance(prev_state, _TitleParts) else [prev_state.str]
    parts.append(step.str)
    state = _TitleParts()
    state.parts = parts
    state.ok, state.can_continue, state.pos, state.marker = step.ok, step.can_continue, step.pos, step.marker
    state.str = "" if step.can_continue else "".join(parts)
    return state


if _PARSER_PROBLEM is None:

    class _TitleParts(_TitleState):  # type: ignore[misc,valid-type]
        """A title state that carries its unescaped parts until the title ends."""

        __slots__ = ("parts",)


def _parser() -> Any:
    if _PARSER_PROBLEM is not None:
        raise ParserUnavailable(_PARSER_PROBLEM)
    return _configured()


@lru_cache(maxsize=1)
def _configured() -> Any:
    """One configured parser per process: `parse` keeps its state in a fresh state object per call."""
    md = MarkdownIt(PARSER_PRESET).enable(list(PARSER_EXTRAS))
    md.disable("inline")  # core rule: inline blocks are parsed one by one, only where the lint reads them
    md.inline.ruler.before("text", "report_lint_depth_guard", _depth_guard)
    md.block.ruler.before(md.block.ruler.get_all_rules()[0], _DEADLINE_RULE, _deadline_rule, {"alt": _DEADLINE_CHAINS})
    # Every rule calls `state.md.helpers.*` (rules_block/reference.py:78,110,118; rules_inline/link.py and image.py),
    # and `MarkdownIt.__init__` binds the shared package module (main.py:50): an instance namespace changes this
    # parser only, never a library global.
    md.helpers = SimpleNamespace(
        parseLinkLabel=helpers.parseLinkLabel,
        parseLinkDestination=helpers.parseLinkDestination,
        parseLinkTitle=_title_continued,
    )
    return md


class _Heading(NamedTuple):
    index: int  # token index of `heading_open`
    first: int  # 0-based first line
    end: int  # line after the heading (a Setext heading includes its underline)
    level: int
    text: str | None  # the plain text; None when the inline block is not read (over INLINE_MAX or unresolved)


class _Doc:
    """One parsed text: block tokens, source lines and the per-index lookups the checks share."""

    def __init__(self, text: str, md: Any, limit: tuple[float, float] | None = None) -> None:
        self.md = md
        # `limit`: (monotonic deadline, the timeout in seconds), read by `_deadline_rule` and `_depth_guard`.
        self.env: dict[str, Any] = {} if limit is None else {_DEADLINE_KEY: limit}
        self.lines = text.split("\n")
        self.tokens: list[Any] = md.parse(text, self.env)
        _check_deadline(self.env)
        self.children: dict[int, list[Any] | str] = {}  # inline token index -> children, or the reason it is unread
        tokens = self.tokens
        count = len(tokens)
        self.max_nesting: int = md.options["maxNesting"]
        # One backward pass: the next top-level heading and the next non-empty fence after each index.
        self.next_heading = [count] * (count + 1)
        self.next_fence = [count] * (count + 1)
        for k in range(count - 1, -1, -1):
            token = tokens[k]
            self.next_heading[k] = k if token.type == "heading_open" and token.level == 0 else self.next_heading[k + 1]
            self.next_fence[k] = k if token.type == "fence" and token.content.strip() else self.next_fence[k + 1]
        # Containers whose nested content the parser dropped at maxNesting (state_block.py:119-127: an open token
        # at level maxNesting - 1 tokenizes its content at maxNesting).
        self.deep = [
            k
            for k, t in enumerate(tokens)
            if t.type in _CONTAINERS and t.nesting == 1 and t.level >= self.max_nesting - 1
        ]
        # The top-level block each token belongs to (the last level-0 token that is no close), for F1's one finding
        # per top-level block.
        self.top = [0] * count
        current = 0
        for k, t in enumerate(tokens):
            if t.level == 0 and t.nesting != -1:
                current = k
            self.top[k] = current
        self.tables = [k for k, t in enumerate(tokens) if t.type == "table_open" and t.level == 0]
        # Lines that are no content: top-level heading lines and top-level HTML comment blocks.
        covered = [False] * (len(self.lines) + 1)
        self.headings: list[_Heading] = []
        for k, token in enumerate(tokens):
            if token.level != 0 or token.map is None:
                continue
            if token.type == "heading_open" or (
                token.type == "html_block" and token.content.lstrip().startswith("<!--")
            ):
                for line in range(token.map[0], min(token.map[1], len(self.lines))):
                    covered[line] = True
            if token.type == "heading_open":
                self.headings.append(_Heading(k, token.map[0], token.map[1], int(token.tag[1:]), self.plain(k + 1)))
        self.content = [0]  # prefix sums of content lines
        for line, source in enumerate(self.lines):
            self.content.append(self.content[-1] + (1 if source.strip() and not covered[line] else 0))

    def inline(self, index: int) -> list[Any] | str:
        """The children of the inline token at `index`, parsed once; a string names why the block is not read."""
        cached = self.children.get(index)
        if cached is not None:
            return cached
        token = self.tokens[index]
        found: list[Any] | str
        if len(token.content) > INLINE_MAX:
            found = _UNREAD_LONG.format(length=len(token.content), limit=INLINE_MAX)
        else:
            children: list[Any] = []
            self.env.pop(_DEEP_FLAG, None)
            try:
                self.md.inline.parse(token.content, self.md, self.env, children)
            except RecursionError:
                found = _UNREAD_RECURSION
            else:
                found = _UNREAD_LINK.format(depth=self.max_nesting) if self.env.pop(_DEEP_FLAG, False) else children
        self.children[index] = found
        return found

    def plain(self, index: int) -> str | None:
        children = self.inline(index)
        return None if isinstance(children, str) else _plain(children).strip()

    def line_of(self, index: int) -> int:
        """1-based line of the token at `index` (its block's first line)."""
        k = index
        while self.tokens[k].map is None:
            k -= 1
        return self.tokens[k].map[0] + 1

    def scan_labels(self, start: int, stop: int, unread: list[tuple[int, str]]) -> list[tuple[int, str, int]]:
        """(inline token index, title, 1-based line) of every bold `Work completed` or `Result and evidence` label in
        a paragraph between two token indices, in token order; a paragraph that is not read goes to `unread`."""
        found: list[tuple[int, str, int]] = []
        for k in range(start, stop):
            token = self.tokens[k]
            if token.type != "inline" or self.tokens[k - 1].type != "paragraph_open":
                continue
            children = self.inline(k)
            if isinstance(children, str):
                unread.append((k, children))
                continue
            for title, line in _label_titles(children):
                found.append((k, title, token.map[0] + 1 + line))
        return found


def _plain(children: list[Any]) -> str:
    """What an inline block shows as text: text, escapes, entities and code-span contents, image descriptions included
    (their children), line breaks as spaces; HTML and markup tokens show nothing."""
    parts: list[str] = []
    stack = [iter(children)]
    while stack:
        token = next(stack[-1], None)
        if token is None:
            stack.pop()
        elif token.type in ("text", "text_special", "code_inline"):
            parts.append(token.content)
        elif token.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif token.type == "image" and token.children:
            stack.append(iter(token.children))
    return "".join(parts)


def _joined(children: list[Any], start: int) -> tuple[str, int]:
    """The text of the run of `text` and `text_special` tokens from `start` (the core rule `text_join` would merge
    them), and the index after it."""
    parts = []
    k = start
    while k < len(children) and children[k].type in ("text", "text_special"):
        parts.append(children[k].content)
        k += 1
    return "".join(parts), k


def _label_titles(children: list[Any]) -> list[tuple[str, int]]:
    """(title, line offset) of each bold template label that starts the block or a line of it: `strong_open`, then
    text that is the title with or without `:`, then `strong_close`. Emphasis leaves an empty `text` token where its
    delimiter run was (rules_inline/emphasis.py:22-23), so empty text keeps the line start."""
    found = []
    offset = 0
    line_start = True
    for k, token in enumerate(children):
        if token.type in ("softbreak", "hardbreak"):
            offset += 1
            line_start = True
            continue
        if token.type == "text" and not token.content:
            continue
        if token.type == "strong_open" and line_start:
            text, after = _joined(children, k + 1)
            if after < len(children) and children[after].type == "strong_close":
                for title in (WORK_TITLE, RESULT_TITLE):
                    if text in (title, title + ":"):
                        found.append((title, offset))
        if token.type == "html_inline":
            offset += token.content.count("\n")
        line_start = False
    return found


def _form(text: str | None, title: str) -> bool | None:
    """Whether a heading text is `title` (case-folded) at its start: True when it ends there or goes on with a
    separator of `TITLE_SEPARATORS`; None when it goes on with anything else (`Completion reports`: ambiguous); False
    when it starts otherwise or is not read. None and False are both falsy, so for the other titles an odd
    continuation is simply no match; the section title tests `is None`."""
    if text is None:
        return False
    folded, want = text.casefold(), title.casefold()
    if not folded.startswith(want):
        return False
    rest = folded[len(want) :]
    return True if not rest or rest[0] in TITLE_SEPARATORS else None


def _path_lines(text: str) -> list[str]:
    """The paths of the `path:line` and `path:line-line` pointers in the text, left to right: a path is a maximal
    run of characters outside `_PATH_STOP` that follows no word character, `.`, `/`, `\\` or `-`, then `:`, digits,
    optionally `-` and digits, and no word character. A pointer starting inside a run would end at the same `:`, so
    each run is read once from its start; after a pointer the reading resumes where it ended."""
    paths: list[str] = []
    at = 0
    while at < len(text):
        start = at
        while at < len(text) and text[at] not in _PATH_STOP and not text[at].isspace():  # `isspace` is regex `\s`
            at += 1
        if at == start:
            at += 1
            continue
        # The run's first character that may start a path: the one before it is no word character or `.`/`/`/`\`/`-`.
        begin = next(
            (k for k in range(start, at) if k == 0 or not (text[k - 1].isalnum() or text[k - 1] in "_./\\-")), None
        )
        digits = at + 1
        while digits < len(text) and text[digits].isdecimal():  # regex `\d`; `\w` is `isalnum` or `_`
            digits += 1
        if begin is None or text[at : at + 1] != ":" or digits == at + 1:
            continue
        end = digits
        if text[end : end + 1] == "-":
            last = end + 1
            while last < len(text) and text[last].isdecimal():
                last += 1
            if last > end + 1 and not (text[last : last + 1].isalnum() or text[last : last + 1] == "_"):
                end = last
        if end == digits and (text[end : end + 1].isalnum() or text[end : end + 1] == "_"):
            continue  # `\d+` followed by a word character: the regex's `(?!\w)` failed for every length
        paths.append(text[begin:at])
        at = end
    return paths


def _points(text: str) -> bool:
    """Whether the plain text holds a URL or a `path:line` pointer."""
    if _URL.search(text):
        return True
    # `12:30` is a time and `Note:1` a citation, not a file: the path needs a separator or an extension.
    return any(
        not path.isdigit() and ("/" in path or "\\" in path or _EXTENSION.search(path)) for path in _path_lines(text)
    )


class _Cell(NamedTuple):
    code: bool  # a non-blank code span
    shown: str  # shown text (links' text included), and link destinations, space-separated
    hidden: list[tuple[str, str]]  # (kind, text) of what shows no pointer: inline HTML, images, link titles


def _cell(children: list[Any], md: Any) -> _Cell:
    code = False
    shown: list[str] = []
    hidden: list[tuple[str, str]] = []
    for token in children:  # top level: images keep their parts in attrs and children, which are hidden
        if token.type in ("text", "text_special"):
            shown.append(token.content)
        elif token.type in ("softbreak", "hardbreak"):
            shown.append(" ")
        elif token.type == "code_inline":
            code = code or bool(token.content.strip())
            shown.append(" ")
        elif token.type == "link_open":
            if token.info != "auto":  # an autolink shows its target as its text already
                shown.append(" " + md.normalizeLinkText(str(token.attrs.get("href", ""))) + " ")
            if token.attrs.get("title"):
                hidden.append(("title", str(token.attrs["title"])))
        elif token.type == "html_inline":
            hidden.append(("html_inline", token.content))
        elif token.type == "image":
            parts = (token.attrs.get("src", ""), token.attrs.get("title", ""), token.content)  # content: the description
            hidden.append(("image", " ".join(str(part) for part in parts)))
            shown.append(" ")
    return _Cell(code, "".join(shown), hidden)


def _cell_has_pointer(cell: _Cell) -> bool:
    return cell.code or _points(cell.shown)


def _cell_hider(cell: _Cell) -> str | None:
    """The construct that holds a pointer-like text or a backtick of a cell with no pointer, for the finding."""
    for kind, text in cell.hidden:
        if "`" in text or _points(text):
            return _HIDERS[kind]
    return None


def _parse_cell(cell: str) -> tuple[_Cell, Any]:
    md = _parser()
    children: list[Any] = []
    md.inline.parse(cell, md, {}, children)
    return _cell(children, md), md


def _has_pointer(cell: str) -> bool:
    """Whether a table cell's inline text shows a pointer (a non-blank code span, or a URL or `path:line` in its shown
    text or a link destination). For tests and callers that hold a cell's text."""
    return _cell_has_pointer(_parse_cell(cell)[0])


def _has_code_span(cell: str) -> bool:
    """Whether the cell's inline text holds a code span with something in it."""
    return _parse_cell(cell)[0].code


def _check_table(doc: _Doc, table: int) -> list[Finding]:
    tokens = doc.tokens
    header: list[int] = []
    rows: list[tuple[int, list[int]]] = []  # (tr_open index, inline indices of its cells)
    k = table + 1
    current: list[int] | None = None
    while tokens[k].type != "table_close":
        token = tokens[k]
        if token.type == "tr_open":
            current = []
            if tokens[k - 1].type != "thead_open":
                rows.append((k, current))
        elif token.type == "inline" and current is not None:
            current.append(k)
            if tokens[k - 1].type == "th_open":
                header.append(k)
        k += 1
    first = tokens[table].map[0] + 1
    if len(header) != EVIDENCE_COLUMNS:
        return [
            Finding(
                "evidence-table",
                first,
                f"the table has {len(header)} columns, the template's `{EVIDENCE_TITLE}` table has {EVIDENCE_COLUMNS}",
            )
        ]
    found: list[Finding] = []
    data = []
    for tr, cells in rows:
        line = tokens[tr].map[0]
        # The parser pads or cuts a row to the header's width (rules_block/table.py:220-233), so the row's own cell
        # count comes from the parser's splitter on its source line, outer pipes dropped as the parser drops them.
        raw = escapedSplit(doc.lines[line].strip())
        if raw and raw[0] == "":
            raw.pop(0)
        if raw and raw[-1] == "":
            raw.pop()
        if any(part.strip() for part in raw):
            data.append((line + 1, raw, cells))
    if not data:
        return [Finding("evidence-table", first, "the table has no data row (empty cells are a placeholder)")]
    for line, raw, cells in data:
        if len(raw) != EVIDENCE_COLUMNS:
            found.append(
                Finding(
                    "evidence-table",
                    line,
                    f"the row has {len(raw)} cells, the template has {EVIDENCE_COLUMNS} (write a literal pipe as `\\|`)",
                )
            )
        elif tokens[cells[CLASS_COLUMN]].content.casefold() not in CLASSES:
            found.append(
                Finding(
                    "evidence-table",
                    line,
                    f"the class cell {_preview(tokens[cells[CLASS_COLUMN]].content)} is none of: {', '.join(CLASSES)}",
                )
            )
        children = doc.inline(cells[SOURCE_COLUMN])
        if isinstance(children, str):
            found.append(Finding("report-section", line, children))
            continue
        cell = _cell(children, doc.md)
        if not _cell_has_pointer(cell):
            hider = _cell_hider(cell)
            found.append(
                Finding(
                    "evidence-pointer",
                    line,
                    "the source cell has no pointer: `path:line`, `path:line-line`, an http(s) URL or an inline "
                    "code span (a command)"
                    + _NO_POINTER_HINT
                    + ("" if hider is None else _HIDDEN_POINTER.format(where=hider)),
                )
            )
    return found


def _preview(text: str | None) -> str:
    """Raw text quoted in a message: its repr, cut to `PREVIEW_MAX` characters with the full length named, so a
    message stays small whatever the input holds (T-021 P2R-F3)."""
    if text is None or len(text) <= PREVIEW_MAX:
        return repr(text)
    return f"{text[:PREVIEW_MAX]!r}… ({len(text)} characters)"


def _normal(lines: list[str]) -> list[str]:
    return [line.rstrip() for line in lines if line.strip()]


def _prepare(text: str) -> str:
    """Line endings as the parser normalizes them (rules_core/normalize.py), and one leading BOM dropped: the parser
    keeps a BOM as text, so `\\ufeff## x` would be no heading."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text[1:] if text.startswith("﻿") else text


def _template_lines(template: str | None, limit: tuple[float, float] | None = None) -> list[str] | None:
    """The template's own `## Completion report` section as normalized lines, or None when there is no template."""
    if template is None:
        return None
    doc = _Doc(_prepare(template), _parser(), limit)
    heads = doc.headings
    for position, head in enumerate(heads):
        if head.level == 2 and head.text == SECTION_TITLE:
            hi = next((h.first for h in heads[position + 1 :] if h.level <= 2), len(doc.lines))
            return _normal(doc.lines[head.first : hi])
    return None


def load_template() -> str | None:
    """Text of the checkout's task template, or None when it cannot be read (then no report counts as untouched)."""
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _odd_title(heading: _Heading) -> Finding:
    return Finding(
        "report-section",
        heading.first + 1,
        f"the heading {_preview(heading.text)} starts with `{SECTION_TITLE}` but is not a report heading: a top-level "
        "level-2 heading whose title ends or continues with a space, `(` or `:` (a report under it is not linted)",
    )


class _Unit(NamedTuple):
    head: _Heading
    stop: int  # token index where the unit ends
    stop_line: int


def _done_check(doc: _Doc, unit: _Unit, inner: list[_Heading], labels: list[tuple[int, str, int]]) -> list[Finding]:
    marks = [(k, line) for k, title, line in labels if title == RESULT_TITLE]
    marks += [(h.index, h.first + 1) for h in inner if _form(h.text, RESULT_TITLE)]
    if not marks:
        return [
            Finding(
                "done-check-output",
                unit.head.first + 1,
                f"no `{RESULT_TITLE}` label (the template's bold bullet label or a heading) in a written report",
            )
        ]
    found = []
    for index, line in sorted(marks):
        if doc.next_fence[index] >= doc.next_heading[index + 1]:
            found.append(
                Finding(
                    "done-check-output",
                    line,
                    f"no non-empty fenced code block (the done-check output) between the `{RESULT_TITLE}` label "
                    "and the next heading",
                )
            )
    return found


def _evidence(doc: _Doc, unit: _Unit, inner: list[_Heading]) -> list[Finding]:
    marks = [h for h in inner if _form(h.text, EVIDENCE_TITLE)]
    if not marks:
        return [
            Finding(
                "evidence-table", unit.head.first + 1, f"no `{EVIDENCE_TITLE}` heading with its table in a written report"
            )
        ]
    found: list[Finding] = []
    for mark in marks:
        stop = doc.next_heading[mark.index + 1]
        tables = doc.tables[bisect_left(doc.tables, mark.index) : bisect_left(doc.tables, stop)]
        if not tables:
            found.append(Finding("evidence-table", mark.first + 1, f"no table under the `{EVIDENCE_TITLE}` heading"))
        for table in tables:  # every table under the heading is evidence, not only the first
            found += _check_table(doc, table)
    return found


def _nested_headings(doc: _Doc, start: int, stop: int) -> list[Finding]:
    """A heading inside a container (a list item or block quote) that shows the section title is never a report
    heading, so its report would be skipped: a finding instead."""
    found = []
    for k in range(start, stop):
        token = doc.tokens[k]
        if token.type == "heading_open" and token.level > 0:
            text = doc.plain(k + 1)
            if text is None or _form(text, SECTION_TITLE) is not False:
                found.append(
                    Finding(
                        "report-section",
                        token.map[0] + 1,
                        f"the heading {_preview(text)} inside a list item or block quote starts with "
                        f"`{SECTION_TITLE}` or is not read; a report heading is a top-level level-2 heading (its "
                        "report is not linted)",
                    )
                )
    return found


def _dropped(doc: _Doc) -> list[Finding]:
    """One finding per top-level block that holds a container whose content the parser dropped at `maxNesting`,
    anywhere in the file: the dropped part may itself be a report heading, so this runs before and independent of
    report discovery (T-021 P2R-F1)."""
    found = []
    last = -1
    for k in doc.deep:
        if doc.top[k] != last:
            last = doc.top[k]
            line = doc.tokens[k].map[0] + 1
            found.append(Finding("report-section", line, _UNREAD_DEEP.format(depth=doc.max_nesting)))
    return found


def lint_report(text: str, template: str | None = None, timeout: float | None = None) -> Result:
    """Findings (rule, 1-based line, message) of every report of a task file's text, and the reports not linted
    because they are unwritten. `template` is the text of the task template (`load_template()`); a report that is
    exactly its `Completion report` section is unwritten. Without it no report counts as untouched. `timeout` (seconds)
    bounds the run: past it `LintDeadline` is raised; None (the standalone CLI) sets no bound. Raises
    `ParserUnavailable` when the pinned parser is missing or another version."""
    md = _parser()
    limit = None if timeout is None else (time.monotonic() + timeout, timeout)
    doc = _Doc(_prepare(text), md, limit)
    untouched = _template_lines(template, limit)
    heads = doc.headings
    found: list[Finding] = _dropped(doc)
    unwritten: list[int] = []
    for head in heads:
        if head.text is None:  # an unread heading may be a report heading: never skipped in silence
            found.append(Finding("report-section", head.first + 1, str(doc.inline(head.index + 1))))
    found += _nested_headings(doc, 0, len(doc.tokens))
    _check_deadline(doc.env)
    position = 0
    while position < len(heads):
        head = heads[position]
        form = _form(head.text, SECTION_TITLE)
        if form is False:
            position += 1
            continue
        if form is None or head.level != 2:
            found.append(_odd_title(head))
            position += 1
            continue
        after = position + 1
        while after < len(heads) and heads[after].level > 2:
            after += 1
        inner = heads[position + 1 : after]
        stop = heads[after].index if after < len(heads) else len(doc.tokens)
        hi = heads[after].first if after < len(heads) else len(doc.lines)
        found += _section(doc, head, inner, stop, hi, untouched, unwritten)
        _check_deadline(doc.env)
        position = after
    return Result(sorted(found, key=lambda f: (f.line, f.rule)), unwritten)


def _section(
    doc: _Doc,
    head: _Heading,
    inner: list[_Heading],
    stop: int,
    hi: int,
    untouched: list[str] | None,
    unwritten: list[int],
) -> list[Finding]:
    found: list[Finding] = []
    unread: list[tuple[int, str]] = []
    labels = doc.scan_labels(head.index, stop, unread)
    label_at = [k for k, _, _ in labels]
    # Nested reports: a deeper heading that shows the title, or whose own body carries a template label; a heading of
    # the `Result` or `Evidence` title stays in its parent unit, where it is read as that title.
    starts: list[_Heading] = []
    for position, heading in enumerate(inner):
        body_stop = inner[position + 1].index if position + 1 < len(inner) else stop
        form = _form(heading.text, SECTION_TITLE)
        own = bisect_left(label_at, heading.index) < bisect_left(label_at, body_stop)
        label = any(_form(heading.text, title) for title in (RESULT_TITLE, EVIDENCE_TITLE))
        if form is None:
            found.append(_odd_title(heading))
        elif form or (own and not label):
            starts.append(heading)
    bounds = [*(s.index for s in starts), stop]
    lines = [*(s.first for s in starts), hi]
    inner_at = [h.index for h in inner]  # sorted: units are consecutive slices of `inner`, `labels` and `unread`
    unread_at = [k for k, _ in unread]
    for unit_head, unit_stop, unit_line in zip([head, *starts], bounds, lines, strict=True):
        unit = _Unit(unit_head, unit_stop, unit_line)
        whole = unit_head is head and not starts
        written = doc.content[unit_line] - doc.content[unit_head.end] > 0
        if (whole and untouched is not None and _normal(doc.lines[head.first : hi]) == untouched) or not written:
            if whole or unit_head is not head:  # an empty heading above nested reports is only their parent
                unwritten.append(unit_head.first + 1)
            continue
        unit_inner = inner[bisect_left(inner_at, unit_head.index + 1) : bisect_left(inner_at, unit_stop)]
        unit_labels = labels[bisect_left(label_at, unit_head.index) : bisect_left(label_at, unit_stop)]
        found += _done_check(doc, unit, unit_inner, unit_labels) + _evidence(doc, unit, unit_inner)
        # Dropped containers are reported once per top-level block by `_dropped`, not again per unit.
        found += [
            Finding("report-section", doc.line_of(k), reason)
            for k, reason in unread[bisect_left(unread_at, unit_head.index) : bisect_left(unread_at, unit_stop)]
        ]
    return found


def lint_task_file(text: str, template: str | None = None) -> list[Finding]:
    """The findings of `lint_report`."""
    return lint_report(text, template).findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("task_files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if _PARSER_PROBLEM is not None:
        print(f"report_lint: {_PARSER_PROBLEM}", file=sys.stderr)
        return 2
    status = 0
    template = load_template()
    for path in args.task_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"{path}: cannot read ({type(exc).__name__}: {exc})")
            status = 2
            continue
        try:
            result = lint_report(text, template)
        except Exception as exc:  # an internal error is no finding: exit 2, never a traceback that exits 1
            print(f"{path}: cannot lint ({type(exc).__name__}: {_preview(str(exc))})")
            status = 2
            continue
        for finding in result.findings:
            print(f"{path}:{finding.line}: [{finding.rule}] {finding.message}")
            status = max(status, 1)
        for line in result.unwritten:  # a note on stderr: not a finding, the exit status does not change
            print(f"{path}:{line}: note: report not written (blank or the template's text), not linted", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
