"""Gate for worker briefs: Git writes (R3 audit F-10, P-10) and the PM's own repeated brief mistakes (T-MEM).

Usage: python -B -I brief_lint.py [--require-footer] [--brief [--footer F]] <brief.md>...
Exit 0: clean. Exit 1: findings (one `file:line: [rule] text` per hit). `cli_worker.py start` and `send` call
`lint_message` and refuse to launch on a finding (`--lint-waive "<reason>"` logs an override).

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


def _strip_footer(text: str) -> str:
    cut = text.find(FOOTER_HEADING)
    return text if cut < 0 else text[:cut]


def _mask(text: str) -> str:
    """Blank quoted text and code (same length); a ref-like code span becomes the word REFNAME."""

    def code(match: re.Match[str]) -> str:
        span = match.group()
        if len(span) >= 9 and _REF_LIKE.fullmatch(span[1:-1]):
            return ("REFNAME" + " " * len(span))[: len(span)]
        return " " * len(span)

    text = _CODE.sub(code, text)
    return _QUOTED.sub(lambda m: " " * len(m.group()), text)


def _blocks(text: str) -> list[list[tuple[int, str]]]:
    """Paragraph blocks of (line number, stripped line); wrapped lines of one paragraph stay together."""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            skip = True
        else:
            skip = in_fence or stripped.startswith(">")
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


def lint_brief(text: str) -> list[tuple[int, str]]:
    """Return (1-based line number, offending sentence) for every worker Git-write request."""
    findings: list[tuple[int, str]] = []
    for block in _blocks(_strip_footer(text)):
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
_FENCE = re.compile(r"^\s*```")
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


def header_fields(text: str) -> dict[str, tuple[int, str]]:
    """First `Kind:`, `R-IDs:`, `Task file:`, `Root cause:`, `Class sweep:` and `Approach note:` header line outside
    fenced code:
    key -> (line, value)."""
    found: dict[str, tuple[int, str]] = {}
    in_fence = False
    for number, line in enumerate(text.split("\n"), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADER.match(line)
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


def is_brief_message(text: str) -> bool:
    """Whether a worker message is a task brief and gets the full lint. Allowlist invariant: a message is a brief iff,
    after folding, some line opens with a `[NEW TASK` tag (any case, any line, a fence does not hide it) or a `Kind:`
    header stands outside a fenced block. Feedback (`[QA FEEDBACK]`, `[RETEST]`, `[EVALUATE]`) without a `Kind:`
    header is not one and keeps the always-rules. Fail closed: a doubtful message is linted, and a waiver is logged."""
    folded = _fold(_strip_footer(text))
    return any(_TAG_LINE.match(line) for line in folded.split("\n")) or "kind" in header_fields(
        folded
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


def task_file_findings(text: str, root: Path) -> tuple[str | None, list[Finding]]:
    """(text of the brief's `Task file:`, findings): an outside path is a finding (rule `task-file`), not a read."""
    row = header_fields(_strip_footer(text.replace("\r\n", "\n"))).get("task file")
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


def _slice_ids(task_text: str) -> list[str]:
    """First-column ids of the first Markdown table whose first header cell starts with `Slice`."""
    ids: list[str] = []
    in_table = False
    for line in task_text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
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
    footer: str | None = None,
    task_file_text: str | None = None,
    class_sets_path: Path | None = None,
) -> list[Finding]:
    """All findings of one worker message; `full` adds the rules of a complete brief (see the module doc).

    `footer` is the text of .agents/policy/AGENT_BRIEF_FOOTER.md (None = unreadable, itself a finding of a full lint);
    `task_file_text` is the text of the brief's `Task file:` (None = no slice check);
    `class_sets_path` is the class-test-set file (None = `brief_lint_config.json` next to this module)."""
    text = text.replace("\r\n", "\n")
    body = _strip_footer(text)
    findings = [
        Finding("git-write", n, f"worker Git write requested: {s}") for n, s in lint_brief(text)
    ]
    findings += _base_url_findings(body)
    findings += _line_findings(body)
    if full:
        findings += _brief_findings(text, body, footer, task_file_text, class_sets_path)
    return sorted(findings, key=lambda f: (f.line, f.rule))


def _brief_findings(
    text: str,
    body: str,
    footer: str | None,
    task_file_text: str | None,
    class_sets_path: Path | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    if footer is None or not footer.strip():
        message = ".agents/policy/AGENT_BRIEF_FOOTER.md could not be read: the footer cannot be checked"
        findings.append(Finding("footer", 1, message))
    elif _clean(footer) not in _clean(text):
        message = "the brief does not carry .agents/policy/AGENT_BRIEF_FOOTER.md verbatim"
        findings.append(Finding("footer", 1, message))
    header = header_fields(body)
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
            waived = any(_SETS_WAIVER.match(line) for line in body.split("\n"))
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
            for ident in _slice_ids(task_file_text)
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
    args = parser.parse_args(argv)
    failed = False
    for path in args.briefs:
        text = path.read_text(encoding="utf-8-sig")
        task_text, extra = (
            task_file_findings(text, args.footer.parents[2]) if args.brief else (None, [])
        )
        findings = lint_message(
            text,
            full=args.brief,
            footer=_read(args.footer) if args.brief else None,
            task_file_text=task_text,
        )
        findings = sorted([*findings, *extra], key=lambda f: (f.line, f.rule))
        for finding in findings:
            print(f"{path}:{finding.line}: [{finding.rule}] {finding.message}")
            failed = True
        if args.require_footer and FOOTER_HEADING not in text:
            print(f"{path}: footer missing")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
