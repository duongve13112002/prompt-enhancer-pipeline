"""PreToolUse hook: block Bash/PowerShell commands that stop processes other than by explicit numeric PID.

Footer rule 12: stop only processes you started, by the PID you recorded; never kill by name or
command-line match. Standard library only (runs with `python -B -I`). Reads the hook JSON on stdin;
exit 2 with a short stderr reason blocks the tool call (documented PreToolUse contract), exit 0 allows.

Only command positions are classified. Heredoc bodies, PowerShell here-strings, comments and the quoted arguments of
data commands (`echo`, `cat`, `sed`, `git`, `python`, ...) are text, not commands, and are skipped. They are parsed again
when something executes them: a shell reading a heredoc or a pipe, `bash -c`, `iex`, `$(...)` or backticks inside double
quotes, and quoted arguments of any command that is not a known data command.

This is a safety net against accidents (name/wildcard/pipeline kills), not a sandbox: code that stops
processes from inside an interpreter (`python -c "os.kill(...)"`) is not inspected.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

GUARDED_TOOLS = {"Bash", "PowerShell"}
MAX_DEPTH = 4
RULE = "Footer rule 12: stop only processes you started, by the PID you recorded; never by name, image or command-line match."
HINT = "Use an explicit numeric PID: `Stop-Process -Id <n>` / `taskkill /PID <n>` / `kill <n>`."
# State directory: AGENT_WORKFLOW_STATE_DIR, else <project root>/.tmp/pm-state (root = three levels above this file).
STATE_DIR = Path(
    os.environ.get("AGENT_WORKFLOW_STATE_DIR")
    or Path(__file__).resolve().parents[3] / ".tmp" / "pm-state"
)
LOG = STATE_DIR / "process-guard.log"

NAME_KILLERS = {"pkill", "killall", "killall5", "skill"}
PS_KILL = {"stop-process", "spps"}
NUMERIC_ONLY = {"pskill", "pskill64", "tskill"}
NUMERIC_ONLY_OPTS = {"-t", "-x", "/a", "/v", "-nobanner", "/?", "-?"}
WRAPPERS = {
    "sudo",
    "xargs",
    "env",
    "command",
    "exec",
    "nohup",
    "time",
    "call",
    "start",
    "then",
    "do",
    "else",
    "elif",
    "if",
    "while",
    "until",
    "!",
    "&",
    ".",
    "builtin",
    "stdbuf",
    "nice",
    "ionice",
    "timeout",
    "start-process",
    "saps",
    "invoke-expression",
    "iex",
    "eval",
    "wsl",
    "invoke-command",
    "icm",
    "foreach-object",
    "%",
    "foreach",
    "where-object",
    "source",
    "busybox",  # `busybox sh -c ...`, `busybox rm f`: the applet is the command
    "parallel",  # GNU parallel runs its command with arguments from `:::` or stdin, like xargs
}
# Wrappers that run the command in their own arguments; their options may take a separate value (`sudo -u root`,
# `env -u NAME`, `timeout -s KILL`), so the word after an option may be its value, not the command (review N7).
ARGV_WRAPPERS = {"sudo", "env", "nohup", "nice", "ionice", "stdbuf", "timeout", "time", "exec"}
ARGV_WRAPPERS |= {"command", "builtin", "busybox", "wsl", "parallel"}
# xargs options whose value is the next word (xargs(1), GNU findutils 4.9); the command follows the value.
XARGS_VALUE_OPTS = {"-I", "-d", "-E", "-a", "-L", "-n", "-P", "-s"}
SHELLS = {
    "powershell": "ps",
    "pwsh": "ps",
    "bash": "bash",
    "sh": "bash",
    "zsh": "bash",
    "dash": "bash",
    "ksh": "bash",
    "mksh": "bash",
    "ash": "bash",
    "cmd": "cmd",
}
# A shell launcher's arguments are all scanned as inner command text (`_launcher_texts`); more than this many are
# not split (parser-bound) and fail closed.
MAX_LAUNCHER_ARGS = 64
# A value glued to an option of any spelling: `-opt:value`, `--opt=value`, `/opt:value`, `//opt=value`.
GLUED_OPTION_VALUE = re.compile(r"^(?:-{1,2}|/{1,2})[A-Za-z][\w-]*[:=](.+)$", re.S)
SHELL_C_OPTS = {"-c", "-command", "-commandwithargs", "/c", "/k", "/r", "-lc", "-ec", "-cwa"}
ENCODED_OPTS = {"-e", "-ec", "-enc", "-encodedcommand"}
# PowerShell accepts any unambiguous prefix of a parameter name (about_PowerShell_exe); aliases are explicit.
PS_COMMAND_NAMES = ("-command", "-commandwithargs")
PS_COMMAND_ALIASES = {"-cwa", "-ec"}
PS_ENCODED_NAMES = ("-encodedcommand",)
PS_ENCODED_ALIASES = {"-ec"}
# cmd takes the text glued to /c as the command; Git Bash passes `//c` to cmd as `/c`.
CMD_COMMAND_OPT = re.compile(r"^//?[ckr]", re.I)
PS_FILE_NAMES = ("-file",)
# powershell.exe / pwsh options that take a value (about_PowerShell_exe, about_Pwsh); a positional argument after
# the options is the command text.
PS_EXE_VALUE_OPTS = (
    *("-executionpolicy", "-inputformat", "-outputformat", "-windowstyle", "-configurationname"),
    *("-psconsolefile", "-version", "-workingdirectory", "-settingsfile", "-custompipename"),
    *("-encodedarguments", "-configurationfile"),
)
PS_SWITCHES = {"-force", "-passthru", "-confirm", "-whatif", "-verbose", "-debug"}
PS_VALUE_OPTS = {
    "-erroraction": {"silentlycontinue", "stop", "continue", "ignore", "inquire", "suspend"}
}
PS_VALUE_OPTS["-ea"] = PS_VALUE_OPTS["-erroraction"]
TASKKILL_SWITCHES = {"f", "t"}
SEPARATORS = ";|&(){}\n"

# Whole-text patterns for process termination that is not a command word.
TEXT_PATTERNS = [
    (
        re.compile(r"(\$\w+|\$_|\)|\]|\})\s*\.\s*['\"]?(kill|terminate)['\"]?\s*\(", re.I),
        "calls .Kill()/.Terminate() on a process object",
    ),
    (
        re.compile(r"\|\s*(%|foreach(-object)?)\s+(-memberName\s+)?kill\b", re.I),
        "pipes processes into ForEach-Object Kill",
    ),
    (
        re.compile(r"-MethodName\s*:?\s*['\"]?terminate\b", re.I),
        "invokes the Terminate method (CIM/WMI)",
    ),
    (
        re.compile(r"\bInvoke-(Cim|Wmi)Method\b.{0,400}?\bterminate\b", re.I | re.S),
        "invokes the Terminate method (CIM/WMI, any parameter spelling)",
    ),
    (
        re.compile(r"(?is)(?=.*\bwin32_process\b)(?=.*\bRemove-(CimInstance|WmiObject)\b)"),
        "removes a Win32_Process through CIM/WMI",
    ),
    (
        re.compile(r"\bwmic\b[^\n]*\bprocess\b[^\n]*\b(delete|call\s+terminate)\b", re.I),
        "wmic process delete/terminate",
    ),
]
# Parse-failure fallback: every verb the classifier or TEXT_PATTERNS can block, so that a parser crash fails closed.
# tests/test_process_guard.py checks it against every blocked example.
LOOSE_VERB = re.compile(
    r"(?<![\w.-])(taskkill|pkill|killall5?|skill|kill|terminate|stop-process|spps|wmic|tskill|pskill(?:64)?|iwmi"
    r"|invoke-(?:cim|wmi)method|remove-(?:cim|wmi)(?:instance|object))(\.exe)?(?![\w-])"
    r"|\.\s*['\"]?(?:kill|terminate)['\"]?\s*\(",
    re.I,
)
# Commands whose quoted arguments are data, not code. Anything else keeps its quoted text under the whole-text patterns.
DATA_HEADS = {
    *(
        "echo",
        "printf",
        "cat",
        "sed",
        "awk",
        "gawk",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "head",
        "tail",
        "tee",
        "wc",
    ),
    *("sort", "uniq", "diff", "cut", "tr", "git", "node", "ruby", "perl", "py"),
    *(
        "write-output",
        "write-host",
        "write-error",
        "write-warning",
        "write-verbose",
        "write-information",
    ),
    *(
        "out-file",
        "set-content",
        "add-content",
        "select-string",
        "sls",
        "tee-object",
        "set-variable",
    ),
}
DATA_HEAD_PATTERN = re.compile(r"(python|pypy)[\d.]*")
# PowerShell assignment prefix, optionally typed: `$x=`, `$env:X=`, `[int]$x+=`, `${x}=`, `$h.Name=`.
PS_VARIABLE = r"(?:\[[^\]]*\])*\$(?:\{[^}]*\}|[\w:.]+)"
PS_ASSIGN_PREFIX = re.compile(PS_VARIABLE + r"[-+*/%]?=")
PS_VARIABLE_ONLY = re.compile(PS_VARIABLE + "$")
PS_ASSIGN_OP = re.compile(r"[-+*/%]?=")
# What reads a heredoc, here-string or piped text. Only a known data command may skip the text; an interpreter runs it
# as its own language (not inspected, like all interpreter code); anything else is treated as code.
EVALUATORS = {"iex", "invoke-expression"}  # PowerShell: run their input as code
STDIN_EXECUTORS = {"source", ".", "eval"}  # bash: run their input as code
INTERPRETER_NAMES = {
    "py",
    "node",
    "nodejs",
    "deno",
    "bun",
    "perl",
    "ruby",
    "php",
    "lua",
    "rscript",
    "julia",
}
DATA_CONSUMERS = {
    *(
        "cat",
        "echo",
        "printf",
        "tee",
        "sed",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "nl",
        "fold",
    ),
    *(
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "diff",
        "cmp",
        "column",
        "base64",
        "xxd",
        "od",
        "less",
        "more",
    ),
    *(
        "md5sum",
        "sha1sum",
        "sha256sum",
        "read",
        "true",
        "false",
        ":",
        "git",
        "gh",
        "jq",
        "curl",
        "patch",
    ),
    *("clip", "write-output", "write-host", "set-content", "add-content", "out-file", "tee-object"),
    *("out-null", "out-string", "select-string", "sls", "measure-object", "sort-object"),
    *("convertto-json", "convertfrom-json"),
}
SED_EXECUTES = (
    re.compile(r"(^|[;{}\n])\s*[\d$,]*\s*[ew]\b"),  # the `e` and `w` commands
    re.compile(r"s(.).*?\1.*?\1[A-Za-z0-9]*[ew]"),  # the `e` and `w` flags of `s`
)
# A redirection word: optional stream prefix (`2`, `*`, `&`), the operator, an optional `|` (bash `>|`), then
# `&` + stream (a stream copy, not a file) or the target glued to the operator (`>out`, `>"my file"`).
REDIRECT = re.compile(r"^(?P<fd>[*\d&]*)(?P<op>[<>]{1,2})(?P<clobber>\|?)(?P<rest>.*)$", re.S)
REDIRECT_PREFIX = re.compile(r"^[*\d&]*[<>]{0,2}$")  # text that a following `<`/`>` extends


class Word(str):
    quoted: bool  # some part of the word was quoted
    literal: bool  # the whole word was one quoted string (no unquoted text glued to it)

    def __new__(cls, text: str, quoted: bool = False, literal: bool = False) -> Word:
        obj = super().__new__(cls, text)
        obj.quoted = quoted
        obj.literal = literal
        return obj


class Heredoc:
    """A bash heredoc: `expand` is False when the delimiter was quoted (the body is then literal text)."""

    def __init__(self, delim: str, strip_tabs: bool, expand: bool) -> None:
        self.delim, self.strip_tabs, self.expand, self.body = delim, strip_tabs, expand, ""


class Redirect(NamedTuple):
    op: str  # `<`, `>` or `>>`
    target: Word | None  # None: a stream copy (`2>&1`) or an operator without a target


class Segment(NamedTuple):
    words: list[Word]
    piped: bool
    heredocs: list[Heredoc]
    substitutions: list[
        str
    ]  # `$(...)` and backtick text inside double-quoted strings (they execute)
    here_strings: list[str]  # operands of `<<<`
    redirects: list[Redirect]  # file redirections, removed from `words`
    assigned: bool  # PowerShell: the segment was the right-hand side of an assignment (`$x = Get-Content f`)
    after: str  # the separator before the segment ("" at the start, "(", "|", ";", "&", "{", "}", ")", newline)


class Parsed(NamedTuple):
    segments: list[Segment]
    masked: str  # the text with comments, heredoc bodies and data-command strings blanked, for the whole-text patterns


def _substitutions(raw: str, shell: str) -> list[str]:
    """Command-substitution texts (`$(...)`, bash backticks) inside the body of an expanding string."""
    escape = {"bash": "\\", "ps": "`"}.get(shell)
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if escape and c == escape:
            i += 2
        elif raw.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (raw[j] == "(") - (raw[j] == ")")
                j += 1
            out.append(raw[i + 2 : j - 1] if depth == 0 else raw[i + 2 :])
            i = j
        elif shell == "bash" and c == "`":
            body, end = _backtick_body(raw, i)
            out.append(body)
            i = end + 1
        else:
            i += 1
    return out


# Inside backticks a backslash quotes only `$`, a backtick or a backslash (bash(1), Command Substitution).
BACKTICK_ESCAPE = re.compile(r"\\([$`\\])")


def _backtick_body(text: str, i: int) -> tuple[str, int]:
    """The command text of the backtick substitution opening at text[i], un-escaped so a nested substitution
    (`` `echo \\`rm f\\`` ``) parses, and the index of its closing backtick (len(text) when unterminated)."""
    j = i + 1
    while j < len(text) and text[j] != "`":
        j += 2 if text[j] == "\\" else 1
    j = min(j, len(text))
    return BACKTICK_ESCAPE.sub(r"\1", text[i + 1 : j]), j


def _read_delimiter(text: str, j: int) -> tuple[str, bool, int]:
    """The heredoc delimiter word at text[j:]: (delimiter, expands, end). Quotes or a backslash stop expansion."""
    n = len(text)
    out: list[str] = []
    expand = True
    while j < n and not text[j].isspace() and text[j] not in ";|&()<>":
        c = text[j]
        if c in "\"'":
            end = text.find(c, j + 1)
            end = n if end < 0 else end
            out.append(text[j + 1 : end])
            expand, j = False, end + 1
        elif c == "\\" and j + 1 < n:
            out.append(text[j + 1])
            expand, j = False, j + 2
        else:
            out.append(c)
            j += 1
    return "".join(out), expand, j


def _read_heredoc_body(text: str, pos: int, heredoc: Heredoc) -> int | None:
    """Store the body that starts at `pos`, return the position after its terminator line; None when there is none
    (a misread `<<` must not hide the rest of the command, so the text then stays code)."""
    n, start = len(text), pos
    while pos < n:
        eol = text.find("\n", pos)
        end = n if eol < 0 else eol
        line = text[pos:end].rstrip("\r")
        if heredoc.strip_tabs:
            line = line.lstrip("\t")
        following = n if eol < 0 else eol + 1
        if line == heredoc.delim:
            heredoc.body = text[start:pos]
            return following
        pos = following
    return None


def _mask(text: str, regions: list[tuple[int, int]]) -> str:
    out: list[str] = []
    pos = 0
    for start, end in sorted(regions):
        if end <= pos:
            continue
        out += [text[pos : max(start, pos)], " "]
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _parse(text: str, shell: str, executed: bool = False) -> Parsed:
    """Split into command segments; quotes are removed and redirections dropped. Text that is not a command position
    (comments, heredoc bodies, here-strings) is not tokenized: heredoc bodies are attached to their segment.
    `executed` text (a `bash -c`, `eval` or `iex` argument) keeps heredoc bodies as code: `bash -c "$(cat <<'EOF' ..."`
    runs the body."""
    segments: list[Segment] = []
    words: list[Word] = []
    cur: list[str] = []
    cur_quoted = False
    cur_plain = False  # unquoted text in the current word
    in_word = False
    piped = False
    seg_heredocs: list[Heredoc] = []
    seg_subs: list[str] = []
    seg_regions: list[tuple[int, int]] = []  # quoted text of the segment being built
    blank: list[tuple[int, int]] = []
    pending: list[Heredoc] = []  # heredocs whose body starts after the next newline
    arith: list[
        bool
    ] = []  # open parentheses; True inside bash `((`, where `<<` is a shift, not a heredoc
    escape = {"bash": "\\", "ps": "`"}.get(shell)
    after = ""  # separator before the segment being built

    def end_word() -> None:
        nonlocal cur, cur_quoted, cur_plain, in_word
        if in_word:
            words.append(Word("".join(cur), cur_quoted, cur_quoted and not cur_plain))
        cur, cur_quoted, cur_plain, in_word = [], False, False, False

    def end_segment(pipe: bool, sep: str) -> None:
        nonlocal words, piped, seg_heredocs, seg_subs, seg_regions, after
        end_word()
        if words or seg_heredocs or seg_subs:
            stripped, here_strings, redirects = _strip_redirects(words)
            assigned = False
            if shell == "ps":
                unassigned = _strip_assignments(stripped)
                assigned = unassigned is not stripped
                stripped = unassigned
            segments.append(
                Segment(
                    stripped,
                    piped,
                    seg_heredocs,
                    seg_subs,
                    here_strings,
                    redirects,
                    assigned,
                    after,
                )
            )
            if _is_data_segment(stripped):
                blank.extend(seg_regions)
            words, piped, seg_heredocs, seg_subs, seg_regions = [], pipe, [], [], []
            after = sep
        else:
            piped = piped or pipe
            after = sep if sep not in ("&", "|") or after in ("", "\n") else after

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if shell == "ps" and c == "$" and text.startswith("${", i) and "}" in text[i:]:
            end = text.index("}", i)  # `${name}` is one variable token, not a script block
            cur.append(text[i : end + 1])
            cur_plain = in_word = True
            i = end + 1
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                if c == '"' and escape and text[j] == escape and j + 1 < n:
                    j += 1
                j += 1
            piece = text[i + 1 : j]
            cur.append(piece)
            cur_quoted = in_word = True
            seg_regions.append((i, j + 1))
            if c == '"' and shell in ("bash", "ps"):
                seg_subs += _substitutions(piece, shell)
            i = j + 1
            continue
        if not in_word and shell in ("bash", "ps") and c == "#":
            eol = text.find("\n", i)
            end = n if eol < 0 else eol
            blank.append((i, end))
            i = end
            continue
        if not in_word and shell == "ps" and text.startswith("<#", i):
            close = text.find("#>", i + 2)
            end = n if close < 0 else close + 2
            blank.append((i, end))
            i = end
            continue
        if (
            (not in_word or (cur and cur[-1].endswith("=")))
            and shell == "ps"
            and c == "@"
            and i + 1 < n
            and text[i + 1] in "\"'"
        ):
            quote, eol = text[i + 1], text.find("\n", i + 2)
            if (
                eol >= 0 and not text[i + 2 : eol].strip()
            ):  # here-string header: nothing after the opening quote
                body_start, pos, end = eol + 1, eol + 1, n
                while pos < n:
                    if text.startswith(quote + "@", pos):  # the terminator must start its line
                        end = pos
                        break
                    nl = text.find("\n", pos)
                    pos = n if nl < 0 else nl + 1
                close = min(n, end + 2)
                body = text[body_start:end]
                cur.append(body)
                cur_quoted = in_word = True
                seg_regions.append((i, close))
                if quote == '"':
                    seg_subs += _substitutions(body, "ps")
                i = close
                continue
        if shell == "bash" and text.startswith("<<<", i):
            end_word()
            cur.append("<<<")  # here-string operator; _strip_redirects reads its operand
            cur_plain = in_word = True
            i += 3
            continue
        if (
            shell == "bash"
            and c == "<"
            and text.startswith("<<", i)
            and not any(arith)
            and not executed
        ):
            j = i + 2
            strip_tabs = j < n and text[j] == "-"
            j += strip_tabs
            while j < n and text[j] in " \t":
                j += 1
            delim, expand, end = _read_delimiter(text, j)
            if end > j:
                end_word()
                heredoc = Heredoc(delim, strip_tabs, expand)
                seg_heredocs.append(heredoc)
                pending.append(heredoc)
                i = end
                continue
        if escape and c == escape and text.startswith(("\n", "\r\n"), i + 1):
            # Line continuation: bash joins the text across it, PowerShell reads it as whitespace in the command.
            if shell == "ps":
                end_word()
            i += 2 if text[i + 1] == "\n" else 3
            continue
        if escape and c == escape and i + 1 < n and text[i + 1] != "\n":
            cur.append(text[i + 1])
            cur_plain = in_word = True
            i += 2
            continue
        if shell == "bash" and c == "`":
            # Unquoted command substitution: its text runs (like `$(...)`); the word keeps it, so a backtick in
            # command position is a computed command name.
            body, j = _backtick_body(text, i)
            seg_subs.append(body)
            cur.append(text[i : j + 1])
            cur_plain = in_word = True
            i = j + 1
            continue
        if (
            shell == "bash"
            and c in "{}"
            and (in_word or not (i + 1 == n or text[i + 1].isspace() or text[i + 1] in ";|&)"))
        ):
            # `{` and `}` are bash reserved words only on their own; `{}` (find/xargs placeholder), `{a,b}` and
            # `${x}` are word text.
            cur.append(c)
            cur_plain = in_word = True
            i += 1
            continue
        if c.isspace() and c != "\n":
            end_word()
        elif c == "&" and ((i > 0 and text[i - 1] in "<>") or (i + 1 < n and text[i + 1] == ">")):
            if text[i - 1 : i] not in ("<", ">") and not REDIRECT_PREFIX.match("".join(cur)):
                end_word()  # `cmd&>out`: the operator starts a new word
            cur.append(c)
            cur_plain = in_word = True
        elif c in "<>" and not any(arith):
            # An unquoted `<`/`>` is an operator, not part of the word before it (`echo hi>out`), except after a
            # stream prefix (`2>`, `*>`, `&>`) or another operator character (`>>`).
            if not REDIRECT_PREFIX.match("".join(cur)):
                end_word()
            cur.append(c)
            cur_plain = in_word = True
        elif c == "|" and shell == "bash" and "".join(cur) in (">", "1>", "2>"):
            cur.append(c)  # bash `>|out` (write even with noclobber), not a pipe
        elif c in SEPARATORS:
            if shell == "bash" and c == "(":
                arith.append(
                    text.startswith("((", i) or bool(arith and arith[-1] and text[i - 1] == "(")
                )
            elif shell == "bash" and c == ")" and arith:
                arith.pop()
            is_pipe = c == "|" and not (i + 1 < n and text[i + 1] == "|")
            if c == "|" and not is_pipe:
                i += 1
            end_segment(is_pipe, c)
            if c == "\n" and pending:
                pos = i + 1
                for heredoc in pending:
                    following = _read_heredoc_body(text, pos, heredoc)
                    if following is None:
                        break
                    blank.append((pos, following))
                    pos = following
                pending = []
                i = pos
                continue
        else:
            cur.append(c)
            cur_plain = in_word = True
        i += 1
    end_segment(False, "")
    return Parsed(segments, _mask(text, blank))


def _strip_redirects(words: list[Word]) -> tuple[list[Word], list[str], list[Redirect]]:
    """Remove redirections from a segment's words; return the words, `<<<` operands and file redirections."""
    out: list[Word] = []
    here_strings: list[str] = []
    redirects: list[Redirect] = []
    pending: str | None = None  # operator of a bare redirection whose target is the next word
    # bash `>& word`: a stream copy when the word is a number or `-`, else a file
    pending_dup = False
    take = False  # the next word is the operand of a bare `<<<`
    for w in words:
        if take:
            here_strings.append(str(w))
            take = False
            continue
        if pending is not None:
            dup = pending_dup and (str(w).isdigit() or str(w) == "-")
            redirects.append(Redirect(pending, None if dup else w))
            pending, pending_dup = None, False
            continue
        if str(w).startswith("<<<") and (not w.quoted or len(w) > 3):
            if len(w) > 3:
                here_strings.append(str(w)[3:])
            else:
                take = True
            continue
        match = None if w.literal else REDIRECT.match(w)
        if match:
            op, rest = match["op"], match["rest"]
            if rest.startswith("&") and (rest[1:].isdigit() or rest[1:] == "-"):
                redirects.append(Redirect(op, None))  # stream copy or close: `2>&1`, `>&-`
            elif rest == "&":
                pending, pending_dup = op, True
            elif rest:
                rest = rest[1:] if rest.startswith("&") else rest  # bash `>&file`: both streams
                redirects.append(Redirect(op, Word(rest, w.quoted, False)))
            else:
                pending = op
            continue
        out.append(w)
    if pending is not None:  # `>(cmd)` process substitution or a dangling operator
        redirects.append(Redirect(pending, None))
    return out, here_strings, redirects


def _command_head(words: list[Word]) -> tuple[int | None, bool]:
    """Index of the command word after assignments and wrappers, and whether a wrapper was seen."""
    i = 0
    seen_wrapper = False
    wrapper = ""
    while i < len(words):
        w = str(words[i])
        if re.match(r"^\w+=", w) and not words[i].quoted:
            i += 1
        elif _head(w) in WRAPPERS and not (words[i].quoted and " " in w):
            seen_wrapper = True
            wrapper = _head(w)
            i += 1
        elif seen_wrapper and (w.startswith("-") or w.isdigit()):
            i += 1  # option or count of the wrapper, e.g. `xargs -r kill`, `timeout 5 kill`
            if wrapper == "xargs" and w in XARGS_VALUE_OPTS and i < len(words):
                i += 1  # its value: `xargs -I {} rm {}`
        else:
            break
    return (i if i < len(words) else None), seen_wrapper


def _wrapper_option(before_head: list[Word]) -> bool:
    """An argv wrapper with an option before the head (`sudo -u root pkill x`): the head may be the option's value."""
    names = {_head(w) for w in before_head}
    return bool(names & ARGV_WRAPPERS) and any(
        str(w).startswith("-") and not w.quoted for w in before_head
    )


def _strip_assignments(words: list[Word]) -> list[Word]:
    """Drop PowerShell assignment prefixes so the right-hand side is a command position (`$x=Stop-Process ...`)."""
    while words:
        first = str(words[0])
        match = PS_ASSIGN_PREFIX.match(first)
        if match:
            rest = first[match.end() :]
            # What follows `=` inside the same word: a plain command word, or the quoted literal that was assigned.
            words = (
                [Word(rest, words[0].quoted, words[0].quoted), *words[1:]] if rest else words[1:]
            )
        elif (
            PS_VARIABLE_ONLY.match(first)
            and len(words) > 1
            and (operator := PS_ASSIGN_OP.match(words[1]))
        ):
            rest = str(words[1])[operator.end() :]  # `$x = cmd` and `$x =cmd`
            words = (
                [Word(rest, words[1].quoted, words[1].quoted), *words[2:]] if rest else words[2:]
            )
        else:
            break
    return words


def _is_literal_statement(words: list[Word]) -> bool:
    """A bare string statement, not a quoted command name followed by arguments."""
    index, seen_wrapper = _command_head(words)
    if index is None or seen_wrapper:
        return False
    head = words[index]
    return head.literal and bool(re.search(r"\s", head) or index == len(words) - 1)


def _is_data_segment(words: list[Word]) -> bool:
    """True when the segment's quoted text is data: a data command, an assignment, or a bare string statement."""
    if not words:
        return True
    index, _ = _command_head(words)
    if index is None or _is_literal_statement(words):
        return True
    name = _head(words[index])
    return name in DATA_HEADS or bool(DATA_HEAD_PATTERN.fullmatch(name))


# Windows runs a program named without its PATHEXT suffix (`git.cmd`, `taskkill.com`): the head drops any of them.
EXE_SUFFIX = re.compile(r"\.(?:exe|com|bat|cmd)$", re.I)


def _head(word: str) -> str:
    return EXE_SUFFIX.sub("", re.split(r"[\\/]", word)[-1].lower())


def _is_numeric_list(word: str) -> bool:
    parts = [p for p in word.split(",") if p]
    return bool(parts) and all(p.isdigit() for p in parts)


def _check_taskkill(args: list[str]) -> str | None:
    pids = 0
    i = 0
    while i < len(args):
        a = args[i].lower()
        opt = re.sub(r"^(/{1,2}|-)", "", a) if re.match(r"^(/{1,2}|-)", a) else None
        if opt in ("?",):
            i += 1
            continue
        if opt in TASKKILL_SWITCHES:
            i += 1
        elif opt == "pid":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return "taskkill /PID needs a literal numeric PID"
            pids += 1
            i += 2
        else:
            return f"taskkill option `{args[i]}` selects processes by name, image or filter"
    if pids == 0 and not any(a in ("/?", "//?", "-?") for a in args):
        return "taskkill without /PID <n>"
    return None


def _check_ps_stop(args: list[str], piped: bool) -> str | None:
    if piped:
        return "Stop-Process/kill receives processes from a pipeline"
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low.startswith("-"):
            name, sep, val = low.partition(":")
            if name in ("-id", "-pid"):
                if sep:
                    if not _is_numeric_list(val):
                        return f"Stop-Process -Id value `{val}` is not a literal PID"
                    i += 1
                elif i + 1 < len(args) and _is_numeric_list(args[i + 1].rstrip(",")):
                    i += 2
                    while i < len(args) and _is_numeric_list(args[i]):
                        i += 1
                else:
                    return "Stop-Process -Id needs literal numeric PIDs"
            elif name in PS_SWITCHES:
                if sep and val not in ("$true", "$false"):
                    return f"Stop-Process option `{a}` has an unexpected value"
                i += 1
            elif name in PS_VALUE_OPTS:
                value = val if sep else (args[i + 1].lower() if i + 1 < len(args) else "")
                if value not in PS_VALUE_OPTS[name]:
                    return f"Stop-Process option `{a}` has an unexpected value"
                i += 1 if sep else 2
            else:
                return f"Stop-Process option `{a}` selects processes by name or object"
        elif _is_numeric_list(a):
            i += 1
        else:
            return f"Stop-Process target `{a}` is not a literal numeric PID"
    return None


def _check_bash_kill(args: list[str], piped: bool) -> str | None:
    if piped:
        return "kill receives its targets from a pipeline"
    i = 0
    have_signal = False
    options_done = False
    while i < len(args):
        a = args[i]
        if not options_done and a == "--":
            options_done = True
        elif not options_done and a.lower() in ("-s", "-n", "--signal") and not have_signal:
            have_signal = True
            i += 1
        elif not options_done and a in (
            "-l",
            "-L",
            "-V",
            "--list",
            "--table",
            "--help",
            "--version",
        ):
            pass
        elif (
            not options_done and not have_signal and re.fullmatch(r"-(\d+|[A-Za-z][A-Za-z0-9]*)", a)
        ):
            have_signal = True
        elif a.isdigit():
            pass
        else:
            return f"kill target `{a}` is not a literal numeric PID (process groups and names are not allowed)"
        i += 1
    return None


def _check_numeric_only(args: list[str], piped: bool) -> str | None:
    if piped:
        return "process killer receives its targets from a pipeline"
    for a in args:
        if a.lower() not in NUMERIC_ONLY_OPTS and not a.isdigit():
            return f"`{a}` is not a literal numeric PID"
    return None


def _ps_opt(arg: str, names: tuple[str, ...], aliases: set[str]) -> tuple[bool, str]:
    """Match a PowerShell parameter by alias or unique-prefix spelling; return (match, `-opt:value` value)."""
    name, _, _value = arg.lower().partition(":")
    if name in aliases or (
        len(name) >= 2 and name.startswith("-") and any(n.startswith(name) for n in names)
    ):
        return True, arg.partition(":")[2]
    return False, ""


def _decode_encoded(args: list[str], ps: bool) -> str | None:
    import base64

    for k, a in enumerate(args):
        matched, glued = (
            _ps_opt(a, PS_ENCODED_NAMES, PS_ENCODED_ALIASES)
            if ps
            else (a.lower() in ENCODED_OPTS, "")
        )
        value = glued or (args[k + 1] if k + 1 < len(args) else "")
        if matched and value:
            try:
                return base64.b64decode(value).decode("utf-16-le")
            except Exception:
                return None
    return None


def _inner_command(args: list[str], shell_kind: str) -> str | None:
    """Return the command text a shell invocation executes: cmd `/c` (Git Bash spells it `//c`), PowerShell
    `-Command` or its first positional argument (powershell.exe reads that as the command), bash `-c` and short
    clusters holding `c` (`-xc`, `-lic`)."""
    k = 0
    while k < len(args):
        a = args[k]
        rest = args[k + 1 :]
        if shell_kind == "cmd":
            opt = CMD_COMMAND_OPT.match(a)
            if opt:
                glued = a[opt.end() :]
                return " ".join(([glued] if glued else []) + rest)
        elif shell_kind == "ps":
            matched, glued = _ps_opt(a, PS_COMMAND_NAMES, PS_COMMAND_ALIASES)
            if matched and (glued or rest):
                return " ".join(([glued] if glued else []) + rest)
            name = a.lower().partition(":")[0]
            if (
                _ps_opt(a, PS_FILE_NAMES, set())[0]
                or _ps_opt(a, PS_ENCODED_NAMES, PS_ENCODED_ALIASES)[0]
            ):
                return None  # a script file (launcher checks) or an encoded command (decoded separately)
            if name.startswith("-") and len(name) > 1:
                if any(v.startswith(name) for v in PS_EXE_VALUE_OPTS) and ":" not in a:
                    k += 1  # its value
            elif a:
                return " ".join(args[k:])  # the first positional argument is the command text
        elif (a.lower() in SHELL_C_OPTS or re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a)) and rest:
            return " ".join(rest)
        k += 1
    return None


def _launcher_texts(args: list[str], shell_kind: str, split: bool = False) -> list[str] | None:
    """Every text a shell launcher may run, fail closed (review N1/N7): the inner command its options name, plus
    each argument suffix and each glued option value read as inner command text, so an alias, an unknown option,
    an option prefix (`-`, `--`, `/`, `//`) or its case cannot hide the command. Text that runs nothing parses to
    nothing. `split`: the arguments may also arrive joined in one word (`Start-Process pwsh -ArgumentList '-c','..'`).
    None when there are more than MAX_LAUNCHER_ARGS arguments (the caller fails closed)."""
    lists = [args, [t for a in args for t in re.split(r"[\s,]+", a) if t]] if split else [args]
    if any(len(words) > MAX_LAUNCHER_ARGS for words in lists):
        return None
    texts: list[str] = []
    for words in lists:
        texts.append(_inner_command(words, shell_kind) or "")
        for k, a in enumerate(words):
            texts.append(" ".join(words[k:]))
            glued = GLUED_OPTION_VALUE.match(a) or CMD_COMMAND_OPT.match(a)
            if glued:
                value = glued.group(1) if glued.re is GLUED_OPTION_VALUE else a[glued.end() :]
                texts.append(" ".join([value, *words[k + 1 :]]))
    return [t for t in dict.fromkeys(texts) if t.strip()]


def _shell_kind(word: str) -> str | None:
    return SHELLS.get(_head(word))


def _is_interpreter(name: str) -> bool:
    return name in INTERPRETER_NAMES or bool(DATA_HEAD_PATTERN.fullmatch(name))


def _reader(words: list[Word]) -> tuple[str, str | None]:
    """What consumes the text a segment receives: ("data" | "interpreter" | "code", shell kind when known).
    Fail closed: only a known data command or an interpreter skips the text; every other consumer gets it as code."""
    index, seen_wrapper = _command_head(words)
    for word in words[
        : len(words) if index is None else index + 1
    ]:  # `iex` and `source` are also wrappers
        name = _head(word)
        if name in EVALUATORS or name in STDIN_EXECUTORS:
            return "code", "ps" if name in EVALUATORS else "bash"
    if index is None:
        return ("code", None) if seen_wrapper else ("data", None)
    name = _head(words[index])
    args = [str(a) for a in words[index + 1 :]]
    if name in SHELLS:
        return "code", SHELLS[name]
    if _is_interpreter(name):
        return "interpreter", None
    if name == "sed" and any(p.search(a) for a in args for p in SED_EXECUTES):
        return "code", None
    if name in DATA_CONSUMERS:
        return "data", None
    # Unknown consumer: code, parsed as the shell of any shell word it carries (`sudo -u root bash <<EOF`).
    return "code", next((k for k in map(_shell_kind, words) if k), None)


def _stream_scripts(segments: list[Segment], idx: int) -> tuple[list[tuple[str, str | None]], bool]:
    """Texts a segment feeds to something that runs them, with the shell kind (None: the current shell), and
    whether the segment's own heredocs are among them.

    Own stdin (heredocs, `<<<`) is code unless every reader in the pipeline is a data command or an interpreter.
    A bare string or here-string piped on is code for any code reader; `echo`/`printf` arguments only for a known
    executor (shell, `iex`, `source`), so `echo "$msg" | gh ...` stays data."""
    seg = segments[idx]
    end = idx
    while end + 1 < len(segments) and segments[end + 1].piped:
        end += 1
    readers = [_reader(s.words) for s in segments[idx : end + 1]]
    own_kind = next((k for cls, k in readers if cls == "code" and k), None)
    scripts: list[tuple[str, str | None]] = []
    own_code = any(cls == "code" for cls, _ in readers)
    if own_code:
        scripts += [(h.body, own_kind) for h in seg.heredocs] + [
            (t, own_kind) for t in seg.here_strings
        ]
    downstream = readers[1:]
    executor = next((k for cls, k in downstream if cls == "code" and k), None)
    literal = _is_literal_statement(seg.words) and any(c == "code" for c, _ in downstream)
    if seg.words and (executor or literal):
        head, _ = _command_head(seg.words)
        text = [
            " ".join(seg.words[0 if head is None else head + 1 :]),
            *(str(w) for w in seg.words if w.quoted),
        ]
        scripts += [(t, executor) for t in text]
    return scripts, own_code


EMBEDDED_SCRIPT = re.compile(
    r"(?:\[scriptblock\]\s*::\s*create|\.\s*invokescript)\s*\(\s*"
    r"(?:@'\r?\n(?P<h1>.*?)\r?\n'@|@\"\r?\n(?P<h2>.*?)\r?\n\"@|'(?P<s1>(?:[^']|'')*)'|\"(?P<s2>(?:[^\"`]|`.)*)\")",
    re.I | re.S,
)


def _embedded_scripts(text: str) -> list[str]:
    """PowerShell text handed to a script-block factory: `[scriptblock]::Create('...')`, `.InvokeScript('...')`."""
    return [next(g for g in m.groups() if g is not None) for m in EMBEDDED_SCRIPT.finditer(text)]


# Pure; cached so the launcher scan (every argument suffix, at every nesting level) stays polynomial.
@functools.lru_cache(maxsize=4096)
def _classify(text: str, shell: str, depth: int, executed: bool = False) -> str | None:
    if depth > MAX_DEPTH:
        # Too deep to inspect: fail closed when a guarded verb appears anywhere in it.
        if LOOSE_VERB.search(text):
            return "nesting too deep to inspect a command that mentions a process-stopping verb"
        return None
    parsed = _parse(text, shell, executed)
    for pattern, reason in TEXT_PATTERNS:
        if pattern.search(parsed.masked):
            return reason
    if shell == "ps":
        for script in _embedded_scripts(text):
            embedded = _classify(script, "ps", depth + 1, True)
            if embedded:
                return embedded
    for idx, seg in enumerate(parsed.segments):
        seg_reason = _classify_segment(seg.words, seg.piped, shell, depth)
        if seg_reason:
            return seg_reason
        scripts, own_code = _stream_scripts(parsed.segments, idx)
        inner = [(script, kind or shell) for script, kind in scripts]
        if not own_code:  # an unquoted-delimiter heredoc expands `$(...)` and backticks even when nothing executes it
            inner += [
                (sub, shell)
                for h in seg.heredocs
                if h.expand
                for sub in _substitutions(h.body, "bash")
            ]
        inner += [(sub, shell) for sub in seg.substitutions]
        for script, script_shell in inner:
            seg_reason = _classify(script, script_shell, depth + 1)
            if seg_reason:
                return seg_reason
    return None


def _classify_segment(
    words: list[Word], piped: bool, shell: str, depth: int, scan: bool = True
) -> str | None:
    i, seen_wrapper = _command_head(words)
    if i is None:
        return None
    if scan and _wrapper_option(words[:i]):
        # `sudo -u root pkill x`: every later word may start the real command (review N7). One level: a suffix of
        # a suffix is itself scanned here.
        for k in range(i + 1, len(words)):
            if not str(words[k]).startswith("-"):
                reason = _classify_segment(words[k:], piped, shell, depth, scan=False)
                if reason:
                    return reason
    if seen_wrapper and words[i].quoted and re.search(r"\s", words[i]):
        return _classify(str(words[i]), shell, depth + 1, True)  # e.g. `iex "taskkill /IM x"`
    head = _head(words[i])
    args = [str(a) for a in words[i + 1 :]]
    if head in NAME_KILLERS:
        return f"`{head}` stops processes by name or pattern"
    if head in PS_KILL or (head == "kill" and shell == "ps"):
        return _check_ps_stop(args, piped)
    if head == "kill":
        return _check_bash_kill(args, piped)
    if head == "taskkill":
        return _check_taskkill(args)
    if head in NUMERIC_ONLY:
        return _check_numeric_only(args, piped)
    if head == "wmic" and re.search(r"\b(delete|terminate)\b", " ".join(args), re.I):
        return "wmic process delete/terminate"
    if head in SHELLS:
        inner_shell = SHELLS.get(head, shell)
        encoded = _decode_encoded(args, inner_shell == "ps")
        if encoded:
            reason = _classify(encoded, "ps", depth + 1, True)
            if reason:
                return reason
        # `Start-Process powershell -ArgumentList '-Command','...'`: the option and its text arrive as one word.
        split = seen_wrapper and any(re.search(r"[\s,]", a) for a in args)
        texts = _launcher_texts(args, inner_shell, split)
        if texts is None:
            if LOOSE_VERB.search(" ".join(args)):
                return f"`{head}` with more than {MAX_LAUNCHER_ARGS} arguments mentions a process-stopping verb"
            return None
        for inner in texts:
            reason = _classify(inner, inner_shell, depth + 1, True)
            if reason:
                return reason
    return None


CLASSIFY_CACHE = _classify  # cleared on every top-level call


def classify(command: str, tool_name: str = "Bash") -> str | None:
    """Return a short reason when the command stops processes other than by literal numeric PID, else None."""
    shell = "ps" if tool_name == "PowerShell" else "bash"
    CLASSIFY_CACHE.cache_clear()
    try:
        return _classify(command, shell, 0)
    except Exception:
        # Fail closed for the guarded verbs only: any appearance of one blocks, everything else passes.
        return (
            "could not parse a command that mentions a process-stopping verb"
            if LOOSE_VERB.search(command)
            else None
        )


def _log(tool: str, command: str, reason: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": dt.datetime.now().isoformat(timespec="seconds"),
                        "tool": tool,
                        "reason": reason,
                        "command": command[:300],
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def read_event() -> str:
    """Hook JSON from stdin as UTF-8. `python -I` ignores PYTHONUTF8, so the text layer would use the locale code
    page and reject a command with a non-ASCII path, which the guard would then let through unchecked; a test double
    without a byte layer falls back to `read()`."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read()
    return stream.read().decode("utf-8-sig", errors="replace")


def main() -> int:
    try:
        event = json.loads(read_event())
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    tool = event.get("tool_name")
    if tool not in GUARDED_TOOLS:
        return 0
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    reason = classify(command, tool)
    if not reason:
        return 0
    _log(tool, command, reason)
    sys.stderr.write(f"BLOCKED by process_guard: {reason}. {RULE} {HINT}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
