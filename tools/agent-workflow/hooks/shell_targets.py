"""Shell command analysis for the guard hooks: every command a Bash, PowerShell or Monitor call runs, and what it writes.

`walk()` reuses the hardened process_guard parser and follows the same nesting (`bash -c`, `powershell -Command`,
encoded commands, `eval`/`iex`, substitutions, heredocs and pipes read by a shell, `[scriptblock]::Create`), plus the
inner command of a configured launcher (`test_slot.py -- <cmd>`) and of `find -exec`. `write_targets()` lists the paths
a command writes, moves or deletes, and the places where it cannot tell (fail closed). `PathPolicy` decides whether a
target is inside the configured shell-write allowlist. Standard library only (the hooks run with `python -B -I`).

Interpreter code (`python -c "open(p, 'w')"`) and scripts run by path are not inspected, like process_guard.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

# Sibling module: `python -I` leaves the script directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_guard as pg

Word = pg.Word
WINDOWS = os.name == "nt"
MAX_DEPTH = pg.MAX_DEPTH  # parser-bound: nesting deeper than this fails closed
# Plus pg.DATA_HEAD_PATTERN (python, python3, python3.12, pypy, ...).
PYTHON_NAMES = {"py", "pythonw"}
START_PROCESS = {"start-process", "saps", "start"}  # PowerShell Start-Process and its aliases
# Wrappers that run a command or code given at run time; with no literal command after them the name is unknown.
RUN_TIME_WRAPPERS = {"iex", "invoke-expression", "eval", "source", ".", "&", *START_PROCESS}
# Wrappers that also take their command's arguments from stdin or `:::` (see write_targets).
STDIN_ARG_WRAPPERS = {"xargs", "parallel"}
# Strings that are not files (protocol-bound device names and PowerShell sinks).
SINKS = {
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "nul",
    "nul:",
    "$null",
    "con",
    "\\\\.\\nul",
}
# PowerShell drives that hold no files. `alias:` and `function:` are not sinks: writing there defines a command.
PS_SINK_DRIVES = {"env", "variable"}
# PowerShell's built-in drives of providers that hold no files (about_Providers: Alias, Certificate, Environment,
# Function, Registry, Variable, WSMan); `Temp:` (PowerShell 7.3+) is a FileSystem drive and stays a path.
PS_PROVIDER_DRIVES = {"alias", "cert", "env", "function", "hkcu", "hklm", "variable", "wsman"}


class Cmd(NamedTuple):
    # The command words (redirections removed, PowerShell assignment prefix removed).
    words: list[Word]
    head: int | None  # index of the command word after wrappers and env assignments
    wrappers: list[str]  # wrapper words before the head (lower-case basenames)
    shell: str  # "bash", "ps" or "cmd"
    piped_in: bool
    piped_out: bool
    readers: list[str]  # heads of the commands its output is piped into
    redirects: list[pg.Redirect]
    stdin_text: bool  # a heredoc or here-string feeds its stdin
    captured: bool  # its output is captured (assignment, `$(...)`)
    launched: bool  # the inner command of a configured launcher (`test_slot.py -- <cmd>`)
    launcher: bool = False  # this command is a configured launcher running another command
    # Files the wrapper itself writes (`Start-Process -RedirectStandardOutput`).
    extra_writes: tuple[Word, ...] = ()
    # Programs whose code-carrying argument this command was read from, outermost first: `bash -c "<cmd>"`,
    # `powershell -EncodedCommand`, `env -S "<cmd>"`, `iex "<cmd>"`, a script piped into a shell (T-024 round 4, class
    # F). Empty for a command the current shell runs itself (also inside `$(...)`).
    via: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return pg._head(self.words[self.head]) if self.head is not None else ""

    @property
    def args(self) -> list[Word]:
        return self.words[self.head + 1 :] if self.head is not None else []


class Walk(NamedTuple):
    cmds: list[Cmd]
    texts: list[tuple[str, str, str]]  # (raw text, masked text, shell) of every parsed text
    too_deep: bool  # nested deeper than MAX_DEPTH, or a launcher with more than pg.MAX_LAUNCHER_ARGS arguments


def shell_of(tool: str) -> str:
    return "ps" if tool == "PowerShell" else "bash"


def is_python(name: str) -> bool:
    return name in PYTHON_NAMES or bool(pg.DATA_HEAD_PATTERN.fullmatch(name))


def walk(command: str, tool: str, launchers: list[str]) -> Walk:
    state = Walk([], [], False)
    deep = _Walker(state, {_norm_name(x) for x in launchers}).text(
        command, shell_of(tool), 0, False
    )
    return state._replace(too_deep=deep)


def _norm_name(name: str) -> str:
    return re.split(r"[\\/]", name)[-1].lower()


class _Walker:
    def __init__(self, state: Walk, launchers: set[str]) -> None:
        self.state, self.launchers = state, launchers
        self.deep = False
        # Launcher texts already walked: the fail-closed scans overlap, and nested launchers would otherwise
        # rescan every suffix at every level.
        self.seen: set[tuple[str, str]] = set()

    def text(
        self, text: str, shell: str, depth: int, executed: bool, via: tuple[str, ...] = ()
    ) -> bool:
        if depth > MAX_DEPTH:
            self.deep = True
            return True
        parsed = pg._parse(text, shell, executed)
        self.state.texts.append((text, parsed.masked, shell))
        if shell == "ps":
            for script in pg._embedded_scripts(text):
                self.text(script, "ps", depth + 1, True, via)
        segs = parsed.segments
        for idx, seg in enumerate(segs):
            end = idx
            while end + 1 < len(segs) and segs[end + 1].piped:
                end += 1
            readers = [_head_name(s.words) for s in segs[idx + 1 : end + 1]]
            prev = segs[idx - 1] if idx else None
            captured = seg.assigned or (
                seg.after == "("
                and prev is not None
                and (
                    (prev.assigned and not prev.words)
                    or bool(prev.words and str(prev.words[-1]).endswith("$"))
                )
            )
            self.cmd(
                seg.words,
                shell,
                depth,
                piped_in=seg.piped,
                piped_out=end > idx,
                readers=readers,
                redirects=seg.redirects,
                stdin_text=bool(seg.heredocs or seg.here_strings),
                captured=captured,
                launched=False,
                via=via,
            )
            scripts, own_code = pg._stream_scripts(segs, idx)
            # A script fed on stdin runs in the program that reads it; a substitution runs in this shell.
            reader = next((r for r in readers if r), "stdin")
            inner = [(script, kind or shell, (*via, reader)) for script, kind in scripts]
            if not own_code:
                inner += [
                    (sub, shell, via)
                    for h in seg.heredocs
                    if h.expand
                    for sub in pg._substitutions(h.body, "bash")
                ]
            inner += [(sub, shell, via) for sub in seg.substitutions]
            for script, script_shell, script_via in inner:
                self.text(script, script_shell, depth + 1, False, script_via)
        return self.deep

    def cmd(
        self, words: list[Word], shell: str, depth: int, scan: bool = True, **ctx: object
    ) -> None:
        if depth > MAX_DEPTH:
            self.deep = True
            return
        if shell == "cmd":  # `^` is cmd's escape character: `g^it` runs git
            words = [Word(re.sub(r"\^(.)", r"\1", w), w.quoted, w.literal) for w in words]
        head, _ = pg._command_head(words)
        wrappers = [pg._head(w) for w in words[: head if head is not None else len(words)]]
        wrappers = [w for w in wrappers if w in pg.WRAPPERS]
        extra: tuple[Word, ...] = ()
        if shell == "ps" and set(wrappers) & START_PROCESS:
            words, head, extra = _start_process(words)
        elif shell != "ps" and "start" in wrappers and head is not None:
            # cmd `start ["title"] [/b /wait ...] program args`: the first quoted word is the window title.
            while head < len(words) and (
                words[head].literal or (shell == "cmd" and str(words[head]).startswith("/"))
            ):
                head += 1
            head = head if head < len(words) else None
        launched: list[Word] | None = None
        if head is not None:
            name, head_args = pg._head(words[head]), words[head + 1 :]
            launched = _launched_words(
                head_args,
                self.launchers,
                shell_file=name in pg.SHELLS,
                head=_norm_name(str(words[head])),
                # `& $PY -B test_slot.py -- ...`: an interpreter held in a variable is read like python.
                python=is_python(name) or str(words[head]).startswith("$"),
            )
        cmd = Cmd(
            words,
            head,
            wrappers,
            shell,
            **ctx,  # type: ignore[arg-type]
            launcher=launched is not None,
            extra_writes=extra,
        )
        self.state.cmds.append(cmd)
        via: tuple[str, ...] = ctx.get("via", ())  # type: ignore[assignment]
        # GNU env `-S, --split-string=S  process and split S into separate arguments` (`env --help`): the value is a
        # command line env runs, read as one (T-024 round 4, class F); attached forms leave no head at all.
        # All words: a quoted `X="y"` prefix leaves the head on the assignment, before `env`.
        for packed in _split_strings(words):
            self.text(packed, shell, depth + 1, True, (*via, "env"))
        if head is None:
            return
        word = words[head]
        # Bash `eval` joins all its words and parses them again: `eval echo x '>' f` writes f.
        evaluated = shell == "bash" and "eval" in wrappers and len(words) > head + 1
        if evaluated or (wrappers and word.quoted and re.search(r"\s", word)):
            # `iex "Set-Content ..."`, `eval "echo > f"`
            text = " ".join(str(w) for w in words[head:]) if evaluated else str(word)
            self.text(text, shell, depth + 1, True, (*via, wrappers[-1]))
            return
        name = cmd.name
        args = [str(a) for a in cmd.args]
        if name in pg.SHELLS:
            inner_shell = pg.SHELLS[name]
            encoded = pg._decode_encoded(args, inner_shell == "ps")
            if encoded:  # Gemini P2 (round 3): the decoded text runs under this launcher, so it keeps it in `via`
                self.text(encoded, "ps", depth + 1, True, (*via, name))
            # Every argument suffix and glued option value is read as inner text (fail closed, review N1); a
            # configured launcher script's own command (`-File test_slot.py -- <cmd>`) is walked as launched below.
            if launched is not None:
                script = (k for k, a in enumerate(args) if _norm_name(a) in self.launchers)
                args = args[: next(script, len(args))]
            split = bool(wrappers) and any(re.search(r"[\s,]", a) for a in args)
            texts = pg._launcher_texts(args, inner_shell, split)
            if texts is None:
                self.deep = True  # too many arguments to scan: fails closed like deep nesting
            for inner in texts or []:
                if (inner, inner_shell) not in self.seen:
                    self.seen.add((inner, inner_shell))
                    self.text(inner, inner_shell, depth + 1, True, (*via, name))
        if scan and pg._wrapper_option(words[:head]):
            # `sudo -u root rm f`, `env -u X rm f`: a wrapper option's value was taken as the command name, so
            # every later word may start the real command (fail closed, review N7). One level: a suffix of a
            # suffix is itself scanned here.
            for k in range(head + 1, len(words)):
                if not str(words[k]).startswith("-"):
                    self.cmd(words[k:], shell, depth + 1, scan=False, **{**ctx, "redirects": []})
        if launched is not None:
            self.cmd(
                launched,
                shell,
                depth + 1,
                piped_in=cmd.piped_in,
                piped_out=cmd.piped_out,
                readers=cmd.readers,
                redirects=[],
                stdin_text=cmd.stdin_text,
                captured=cmd.captured,
                launched=True,
                via=via,
            )
        if name == "find":
            for inner_words in _find_exec(cmd.args):
                self.cmd(
                    inner_words,
                    shell,
                    depth + 1,
                    piped_in=False,
                    piped_out=False,
                    readers=[],
                    redirects=[],
                    stdin_text=False,
                    captured=False,
                    launched=False,
                    via=(*via, "find"),
                )


START_PROCESS_VALUES = (
    *("filepath", "argumentlist", "credential", "workingdirectory", "redirectstandarderror"),
    *("redirectstandardinput", "redirectstandardoutput", "verb", "windowstyle", "environment"),
)
START_PROCESS_SWITCHES = ("loaduserprofile", "nonewwindow", "passthru", "wait", "usenewenvironment")
START_PROCESS_ALIASES = {
    "args": "argumentlist",
    "lup": "loaduserprofile",
    "nnw": "nonewwindow",
    "rse": "redirectstandarderror",
    "rsi": "redirectstandardinput",
    "rso": "redirectstandardoutput",
    "pspath": "filepath",
    "path": "filepath",
}


def _start_process(words: list[Word]) -> tuple[list[Word], int | None, tuple[Word, ...]]:
    """Rebuild `Start-Process [-FilePath] prog [-ArgumentList] 'a','b c' ...` as `Start-Process prog a b c`.
    Returns the words, the program's index (None when there is no literal program) and the redirect files."""
    at = next(k for k, w in enumerate(words) if pg._head(w) in START_PROCESS)
    names = [*START_PROCESS_VALUES, *START_PROCESS_SWITCHES]
    program: Word | None = None
    argv: list[Word] = []
    outputs: list[Word] = []
    k = at + 1
    while k < len(words):
        w = words[k]
        text = str(w)
        if text.startswith("-") and len(text) > 1 and not w.literal:
            raw_name, _, glued = text[1:].partition(":")
            name, _ = _ps_param("-" + START_PROCESS_ALIASES.get(raw_name.lower(), raw_name), names)
            if name is None:
                # Unknown parameter: kept, so the command's own checks see it (and refuse it).
                argv.append(w)
                k += 1
                continue
            if name in START_PROCESS_SWITCHES:
                k += 1
                continue
            if glued:
                values, k = [Word(glued, w.quoted, False)], k + 1
            elif k + 1 < len(words):
                values, k = _ps_value(words, k + 1)
            else:
                values, k = [], k + 1
            if name == "filepath" and values:
                program = values[0]
            elif name == "argumentlist":
                argv += _split_arguments(values)
            elif name in ("redirectstandardoutput", "redirectstandarderror"):
                outputs += values or [Word("", False, False)]
            continue
        if program is None:
            program = w
        else:
            argv += _split_arguments([w])
        k += 1
    if program is None:
        return words[: at + 1] + argv, None, tuple(outputs)
    return [*words[: at + 1], program, *argv], at + 1, tuple(outputs)


ENV_VALUE_SHORT = "uC"  # GNU env short options that take the next word as their value (`-u NAME`, `-C DIR`)


def _split_strings(words: list[Word]) -> list[str]:
    """The command lines given to GNU env's `-S`/`--split-string` (attached, separate, clustered `-iS`, or a unique
    long prefix such as `--split`), for every `env` word of the command (also after a quoted `X="y"` prefix)."""
    found: list[str] = []
    for k, w in enumerate(words):
        if w.quoted or pg._head(w) != "env":
            continue
        j = k + 1
        while j < len(words):
            a = str(words[j])
            nxt = str(words[j + 1]) if j + 1 < len(words) else ""
            if re.match(r"^\w+=", a) or a == "-":
                j += 1
            elif a.startswith("--") and len(a) >= 3:
                name, eq, value = a.partition("=")
                split = long_matches(name, "--split-string")  # `env --s CMD` too (getopt_long prefix, review CX1)
                if split:
                    found.append(value if eq else nxt)
                j += 1 if eq or not split else 2
            elif a.startswith("-") and len(a) > 1 and not a.startswith("--"):
                letters = a[1:]
                cut = next((i for i, ch in enumerate(letters) if ch in "S" + ENV_VALUE_SHORT), None)
                if cut is not None and letters[cut] == "S":
                    found.append(letters[cut + 1 :] or nxt)
                j += 2 if cut is not None and not letters[cut + 1 :] else 1
            else:
                break
    return [f for f in found if f.strip()]


def _split_arguments(values: list[Word]) -> list[Word]:
    """Start-Process joins its argument array with spaces into one command line; split it back (over-splitting a
    quoted argument only adds words for the checks to refuse)."""
    return [Word(p, True, False) for v in values for p in re.split(r"[\s,]+", str(v)) if p]


def _head_name(words: list[Word]) -> str:
    head, _ = pg._command_head(words)
    return pg._head(words[head]) if head is not None else ""


def _launched_words(
    args: list[Word],
    launchers: set[str],
    *,
    shell_file: bool,
    head: str = "",
    python: bool = False,
) -> list[Word] | None:
    """Words of the command a launcher runs: `python test_slot.py -- <cmd>`, `powershell -File x.ps1 -- <cmd>`.
    Without `--` the words after the launcher (test_slot.py then refuses to run, so nothing escapes the slot)."""
    start: int | None = None
    if head in launchers:
        start = 0
    elif python:
        index = _python_script_index([str(a) for a in args])
        if index is not None and _norm_name(str(args[index])) in launchers:
            start = index + 1
    elif shell_file:
        for k, a in enumerate(args[:-1]):
            if a.lower() in ("-file", "-f") and _norm_name(str(args[k + 1])) in launchers:
                start = k + 2
                break
    if start is None:
        return None
    rest = args[start:]
    if "--" in rest:
        rest = rest[[str(a) for a in rest].index("--") + 1 :]
    return list(rest) or None


def _find_exec(args: list[Word]) -> list[list[Word]]:
    out: list[list[Word]] = []
    k = 0
    while k < len(args):
        if str(args[k]) in ("-exec", "-execdir", "-ok", "-okdir"):
            end = k + 1
            while end < len(args) and str(args[end]) not in (";", "\\;", "+"):
                end += 1
            out.append(list(args[k + 1 : end]))
            k = end
        k += 1
    return out


# Python options that take a value (python --help, 3.12).
PY_VALUE_OPTS = {"W", "X"}
PY_LONG_VALUE_OPTS = {"--check-hash-based-pycs"}


def _python_script_index(args: list[str]) -> int | None:
    """Index of the script path among python's arguments; None for `-c`, `-m`, `-` or no script."""
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            return k + 1 if k + 1 < len(args) else None
        if a in PY_LONG_VALUE_OPTS:
            k += 2
            continue
        if a.startswith("--"):
            k += 1
            continue
        if a.startswith("-") and len(a) > 1:
            body = a[1:]
            for j, ch in enumerate(body):
                if ch in ("c", "m"):
                    return None
                if ch in PY_VALUE_OPTS:
                    if j == len(body) - 1:
                        k += 1
                    break
            k += 1
            continue
        if a == "-":
            return None
        return k
    return None


def python_code(args: list[str], code_letters: str = "c") -> tuple[str | None, str | None, bool]:
    """(code of `-c` (or another configured code option), module of `-m`, reads code from stdin) for python's
    argument list. Options may be clustered (`-Bc`) and the value glued (`-cprint(1)`)."""
    k = 0
    while k < len(args):
        a = args[k]
        if a == "-":
            return None, None, True
        if a == "--":
            return None, None, k + 1 >= len(args)
        if a in PY_LONG_VALUE_OPTS:
            k += 2
            continue
        if a.startswith("--"):
            k += 1
            continue
        if a.startswith("-") and len(a) > 1:
            body = a[1:]
            for j, ch in enumerate(body):
                if ch in code_letters or ch == "m":
                    value = body[j + 1 :] or (args[k + 1] if k + 1 < len(args) else "")
                    return (None, value, False) if ch == "m" else (value, None, False)
                if ch in PY_VALUE_OPTS:
                    if j == len(body) - 1:
                        k += 1
                    break
            k += 1
            continue
        return None, None, False  # a script path
    return None, None, True  # no script: python reads its program from stdin


# ---------------------------------------------------------------------------------------------------------------
# Write targets


class Target(NamedTuple):
    word: str  # the path as written (unquoted)
    verb: str  # what writes it (for the message)
    shell: str
    # A copy/move destination: the file lands inside it when it is a directory.
    into_dir: bool = False
    source: str = ""  # with into_dir: the source whose basename lands inside the directory
    # T-024 round 6 (class L): names the guard cannot know land below a directory, recursively: with into_dir the
    # destination receives the source's contents or relative paths (`cp -T`, `--parents`, rsync, Expand-Archive);
    # without, the last component is a placeholder for unknown names in its parent (archive members, downloads).
    spread: bool = False
    # T-024 final (F27): the write may leave a link (symbolic, junction, hard) at or below its effective path.
    # LINK_MAKES: always (ln, mklink, an extractor); LINK_COPIES: when the source holds a link (cp -r, mv, rsync).
    links: str = ""
    group: int = -1  # index of the command in the walk (run order); -1: order unknown
    # Review CX4: a PowerShell cmdlet's path (or a PowerShell redirection), which PowerShell resolves through its
    # drives (`Env:X` is no file). A native program or .NET call gets the text itself: `name:rest` is the NTFS stream
    # `rest` of the file `name`, whatever `name` is.
    provider: bool = False


LINK_MAKES = "makes"
LINK_COPIES = "copies"


class Writes(NamedTuple):
    targets: list[Target]
    problems: list[str]  # writes whose targets cannot be determined (fail closed)
    # `cd` arguments with the shell of their command (relative targets resolve against every one); the shell is kept
    # per change because a nested `pwsh -c` inside Bash reads its `Set-Location` as PowerShell does (S42 F1).
    cwd_changes: list[tuple[str, str]]
    # T-024 final (F06): (command name, word) for every argument word that is not one of the command's own modelled
    # targets, and the value inside `--opt=V`, `-oV`, `of=V`, `/opt:V`: a program the guard does not model may write
    # below any path it is given. The caller decides which programs only read.
    probes: list[tuple[str, Target]]


def _is_option(a: str, shell: str) -> bool:
    return len(a) > 1 and (a.startswith("-") or (shell == "cmd" and a.startswith("/")))


def _split_list(word: str, literal: bool) -> list[str]:
    """PowerShell array arguments: `a,b` is two paths unless the whole word was one quoted string."""
    if literal or "," not in word:
        return [word]
    parts = [p for p in word.split(",") if p]
    return [word, *parts]  # check the unsplit form as well: a split can only add targets


class PsSpec(NamedTuple):
    paths: tuple[str, ...]  # parameters whose values are written paths
    positional: tuple[str, ...]  # roles of positional arguments, in order
    values: tuple[str, ...] = ()  # parameters with a non-path value
    switches: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()  # parameters whose values are read paths
    # Destination parameters (a directory receives the source's basename).
    dest: tuple[str, ...] = ()
    pipeline_path: bool = True  # the path can come from the pipeline
    contents: bool = False  # the destination receives the source's contents, not the source (Target.spread)


COMMON_SWITCHES = ("verbose", "debug", "whatif", "confirm")
COMMON_VALUES = (
    *("erroraction", "warningaction", "informationaction", "errorvariable", "warningvariable"),
    *("informationvariable", "outvariable", "outbuffer", "pipelinevariable", "progressaction"),
)
COMMON_ALIASES = {
    "ea": "erroraction",
    "wa": "warningaction",
    "infa": "informationaction",
    "ev": "errorvariable",
    "wv": "warningvariable",
    "iv": "informationvariable",
    "ov": "outvariable",
    "ob": "outbuffer",
    "pv": "pipelinevariable",
    "proga": "progressaction",
    "lp": "literalpath",
    "pspath": "literalpath",
    "type": "itemtype",
    "target": "value",
    "nti": "notypeinformation",
    "iti": "includetypeinformation",
    "rso": "redirectstandardoutput",
    "rse": "redirectstandarderror",
}
_CONTENT = PsSpec(
    paths=("path", "literalpath"),
    positional=("path", "value"),
    values=(
        "value",
        "encoding",
        "stream",
        "filter",
        "include",
        "exclude",
        "credential",
        "delimiter",
    ),
    switches=("force", "passthru", "nonewline", "asbytestream", "usetransaction"),
)
_REMOVE = PsSpec(
    paths=("path", "literalpath"),
    positional=("path",),
    values=("filter", "include", "exclude", "credential", "stream"),
    switches=("recurse", "force", "usetransaction"),
)
_MOVE = PsSpec(
    paths=("path", "literalpath"),
    positional=("path", "destination"),
    values=("filter", "include", "exclude", "credential"),
    switches=("force", "passthru", "usetransaction"),
    dest=("destination",),
)
_COPY = _MOVE._replace(
    paths=(),
    reads=("path", "literalpath"),
    values=("filter", "include", "exclude", "credential", "fromsession", "tosession"),
    switches=("container", "force", "recurse", "passthru", "usetransaction"),
)
_NEW_ITEM = PsSpec(
    paths=("path",),
    positional=("path", "value"),
    values=("name", "itemtype", "value", "credential"),
    switches=("force", "usetransaction"),
    pipeline_path=False,
)
_ITEM_PROPERTY = PsSpec(
    paths=("path", "literalpath"),
    positional=("path", "name", "value"),
    values=(
        "name",
        "value",
        "propertytype",
        "newname",
        "filter",
        "include",
        "exclude",
        "credential",
    ),
    switches=("force", "passthru", "usetransaction"),
    dest=("destination",),
)
_EXPORT = PsSpec(
    paths=("path", "literalpath"),
    positional=("path",),
    values=(
        "inputobject",
        "delimiter",
        "encoding",
        "quotefields",
        "usequotes",
        "depth",
        "name",
        "description",
        "scope",
        "as",
    ),
    switches=(
        "append",
        "force",
        "noclobber",
        "notypeinformation",
        "includetypeinformation",
        "useculture",
        "passthru",
        "includescriptblock",
        "noheader",
    ),
    pipeline_path=False,
)
PS_CMDLETS: dict[str, tuple[str, PsSpec]] = {}
for _names, _spec in (
    (("set-content", "sc", "add-content", "ac"), _CONTENT),
    (("clear-content", "clc"), _CONTENT._replace(positional=("path",))),
    (
        ("out-file",),
        PsSpec(
            paths=("filepath", "literalpath"),
            positional=("filepath", "encoding"),
            values=("encoding", "width", "inputobject"),
            switches=("append", "force", "noclobber", "nonewline"),
            pipeline_path=False,
        ),
    ),
    (
        ("tee-object", "tee"),
        PsSpec(
            paths=("filepath", "literalpath"),
            positional=("filepath",),
            values=("variable", "inputobject", "encoding"),
            switches=("append",),
            pipeline_path=False,
        ),
    ),
    (("new-item", "ni", "mkdir", "md"), _NEW_ITEM),
    (("remove-item", "rm", "ri", "del", "erase", "rd", "rmdir"), _REMOVE),
    (("move-item", "mv", "mi", "move"), _MOVE),
    (("copy-item", "cp", "cpi", "copy"), _COPY),
    (
        ("rename-item", "ren", "rni"),
        PsSpec(
            paths=("path", "literalpath"),
            positional=("path", "newname"),
            values=("newname", "credential"),
            switches=("force", "passthru", "usetransaction"),
        ),
    ),
    (
        ("set-item", "si"),
        _CONTENT._replace(values=("value", "filter", "include", "exclude", "credential")),
    ),
    (("clear-item", "cli"), _REMOVE),
    (
        (
            *("new-itemproperty", "set-itemproperty", "sp", "remove-itemproperty", "rp"),
            *("clear-itemproperty", "clp", "rename-itemproperty", "rnp"),
            *("copy-itemproperty", "cpp", "move-itemproperty", "mp"),
        ),
        _ITEM_PROPERTY,
    ),
    (
        ("export-csv", "epcsv", "export-clixml", "export-alias", "epal", "export-formatdata"),
        _EXPORT,
    ),
    (
        ("expand-archive",),
        PsSpec(
            paths=(),
            positional=("path", "destinationpath"),
            reads=("path", "literalpath"),
            switches=("force", "passthru"),
            dest=("destinationpath",),
            pipeline_path=False,
            contents=True,
        ),
    ),
    (
        ("compress-archive",),
        PsSpec(
            paths=("destinationpath",),
            positional=("path", "destinationpath"),
            reads=("path", "literalpath"),
            values=("compressionlevel",),
            switches=("update", "force", "passthru"),
            pipeline_path=False,
        ),
    ),
    (
        ("start-transcript",),
        PsSpec(
            paths=("path", "literalpath", "outputdirectory"),
            positional=("path",),
            switches=(
                "append",
                "force",
                "noclobber",
                "includeinvocationheader",
                "useminimalheader",
            ),
            pipeline_path=False,
        ),
    ),
    (
        ("set-acl", "unblock-file"),
        _REMOVE._replace(
            values=("aclobject", "securitydescriptor", "filter", "include", "exclude"),
            switches=("passthru", "clearcentralaccesspolicy"),
        ),
    ),
):
    for _name in _names:
        PS_CMDLETS[_name] = (_names[0], _spec)

# Cmdlets whose only file write is one parameter among many (matched by unique-prefix spelling).
PS_OUTPUT_PARAMS = {
    "invoke-webrequest": ("outfile",),
    "iwr": ("outfile",),
    "invoke-restmethod": ("outfile",),
    "irm": ("outfile",),
    "curl": ("outfile",),
    "wget": ("outfile",),
}
PS_TEMP_WRITERS = {"new-temporaryfile": "creates a file in the OS temp directory"}
LINK_TYPES = ("symboliclink", "junction", "hardlink")
CD_COMMANDS = {"cd", "chdir", "set-location", "sl", "pushd", "push-location"}


def _ps_param(token: str, names: list[str]) -> tuple[str | None, str | None]:
    """Resolve `-Name[:value]` the way PowerShell binds it: exact name, alias or unique prefix."""
    name, sep, value = token[1:].partition(":")
    name = name.lower()
    name = COMMON_ALIASES.get(name, name)
    if name in names:
        return name, value if sep else None
    matches = [n for n in names if n.startswith(name)] if name else []
    return (matches[0], value if sep else None) if len(matches) == 1 else (None, None)


def _ps_bind(
    args: list[Word], spec: PsSpec, verb: str
) -> tuple[dict[str, list[Word]], list[Word], list[str]]:
    """Bind PowerShell arguments: named values, positional values, problems. Unknown parameters fail closed."""
    with_value = [*spec.paths, *spec.values, *spec.reads, *spec.dest, *COMMON_VALUES]
    names = list(dict.fromkeys([*with_value, *spec.switches, *COMMON_SWITCHES]))
    named: dict[str, list[Word]] = {}
    positional: list[Word] = []
    problems: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        text = str(a)
        if (
            text.startswith("-")
            and len(text) > 1
            and not a.literal
            and not re.fullmatch(r"-[\d.]+", text)
        ):
            name, glued = _ps_param(text, names)
            if name is None:
                problems.append(f"{verb}: parameter `{text}` is not recognised")
                k += 1
                continue
            if name in spec.switches or name in COMMON_SWITCHES:
                k += 1
                continue
            if glued is not None:
                values = [Word(glued, a.quoted, False)]
                k += 1
            elif k + 1 < len(args):
                values, k = _ps_value(args, k + 1)
            else:
                values = []
                k += 1
            if not values:
                problems.append(f"{verb}: parameter `{text}` has no literal value")
            named.setdefault(name, []).extend(values)
            continue
        values, k = _ps_value(args, k)
        positional.extend(values)
    return named, positional, problems


def _ps_value(args: list[Word], k: int) -> tuple[list[Word], int]:
    """One value, continued across `a, b` (a trailing comma joins the next word into the same array)."""
    values = [args[k]]
    while str(values[-1]).endswith(",") and not values[-1].literal and k + 1 < len(args):
        k += 1
        values.append(args[k])
    return values, k + 1


def _ps_targets(cmd: Cmd, writes: Writes) -> None:
    name = cmd.name
    raw = str(cmd.words[cmd.head]) if cmd.head is not None else ""
    program = bool(pg.EXE_SUFFIX.search(raw))
    if program or name not in PS_CMDLETS:
        if name in PS_TEMP_WRITERS:
            writes.problems.append(f"`{raw}` {PS_TEMP_WRITERS[name]}")
        elif name in PS_OUTPUT_PARAMS and not program:
            _param_values(cmd, PS_OUTPUT_PARAMS[name], name, writes)
            if name in ("curl", "wget"):
                # An alias in Windows PowerShell, the real program in pwsh.
                _external_targets(cmd, writes)
        else:
            _external_targets(cmd, writes)
        return
    canonical, spec = PS_CMDLETS[name]
    named, positional, problems = _ps_bind(cmd.args, spec, canonical)
    writes.problems.extend(problems)
    roles = list(spec.positional)
    # Named arguments take their positions out of the positional list.
    roles = [r for r in roles if r not in named and not (r == "path" and "literalpath" in named)]
    for k, value in enumerate(positional):
        role = roles[k] if k < len(roles) else "path"  # extra positionals: fail closed as paths
        named.setdefault(role, []).append(value)
    path_values = [v for p in spec.paths for v in named.get(p, [])]
    verb = canonical
    # F27: an archive may hold links; a copy or move carries the links its source holds.
    links = {"expand-archive": LINK_MAKES, "copy-item": LINK_COPIES, "move-item": LINK_COPIES}.get(canonical, "")
    if canonical == "new-item":
        base = path_values or [Word(".", False, False)]
        names = named.get("name", [])
        path_values = [
            Word(_join(str(b), str(n)), b.quoted, False) for b in base for n in names
        ] or base
        item_type = " ".join(str(v) for v in named.get("itemtype", [])).lower()
        if item_type not in ("", "file", "directory"):
            # A link's target must be inside the allowlist too, also read from the link's directory (F27).
            links = LINK_MAKES
            values = named.get("value", [])
            path_values += values + [
                Word(_join(_dirname(str(p)), str(v)), v.quoted, False)
                for p in path_values
                for v in values
                if not _is_absolute(str(v))
            ]
        elif item_type == "directory" or (name in ("mkdir", "md") and not item_type):
            verb = "mkdir"  # may create an allowlisted directory itself (PathPolicy)
    if canonical == "rename-item":
        for new in named.get("newname", []):
            for src in path_values or []:
                path_values = [
                    *path_values,
                    Word(_join(_dirname(str(src)), str(new)), new.quoted, False),
                ]
    dests = [v for p in spec.dest for v in named.get(p, [])]
    sources = [v for p in (*spec.reads, *spec.paths) for v in named.get(p, [])]
    if not path_values and spec.paths and cmd.piped_in and spec.pipeline_path:
        # `Get-Item f | Move-Item -Destination d` also removes f, which the pipeline names.
        writes.problems.append(f"{canonical} takes its path from the pipeline")
        return
    if not path_values and not dests:
        if cmd.piped_in and spec.pipeline_path:
            writes.problems.append(f"{canonical} takes its path from the pipeline")
            return
        if spec.dest:
            # Copy-Item/Move-Item/Expand-Archive without a destination: the cwd.
            dests = [Word(".", False, False)]
        elif not spec.paths:
            return
        else:
            writes.problems.append(f"{canonical} has no literal path")
            return
    for value in path_values:
        for part in _split_list(str(value), value.literal):
            writes.targets.append(
                Target(part, verb, cmd.shell, links=links if canonical == "new-item" else "", provider=True)
            )
    for value in dests:
        for part in _split_list(str(value), value.literal):
            if sources:
                for src in sources:
                    for src_part in _split_list(str(src), src.literal):
                        writes.targets.append(
                            Target(part, canonical, cmd.shell, True, src_part, spec.contents, links, provider=True)
                        )
            else:
                writes.targets.append(
                    Target(part, canonical, cmd.shell, True, "x", spec.contents, links, provider=True)
                )


def _param_values(cmd: Cmd, params: tuple[str, ...], verb: str, writes: Writes) -> None:
    """Values of the parameters (any prefix spelling, even an ambiguous one-letter `-o`) that name an output file."""
    args = cmd.args
    for k, a in enumerate(args):
        text = str(a)
        if not text.startswith("-") or a.literal:
            continue
        name, _, glued = text[1:].partition(":")
        name = COMMON_ALIASES.get(name.lower(), name.lower())
        if name and any(p.startswith(name) for p in params):
            value = glued or (str(args[k + 1]) if k + 1 < len(args) else "")
            if value:
                writes.targets.append(Target(value, verb, cmd.shell, provider=True))
            else:
                writes.problems.append(f"{verb}: `{text}` has no literal value")


def _join(base: str, name: str) -> str:
    return name if not base or base == "." else base.rstrip("/\\") + "/" + name


def _dirname(path: str) -> str:
    parts = re.split(r"[\\/]", path)
    return "/".join(parts[:-1]) if len(parts) > 1 else "."


def _positionals(args: list[Word], shell: str, value_opts: set[str] = frozenset()) -> list[Word]:  # type: ignore[assignment]
    """Non-option arguments; options in `value_opts` consume the next word. `--` ends options."""
    out: list[Word] = []
    k = 0
    done = False
    while k < len(args):
        a = str(args[k])
        if not done and a == "--":
            done = True
        elif not done and _is_option(a, shell) and not args[k].literal:
            if a in value_opts:
                k += 1
        else:
            out.append(args[k])
        k += 1
    return out


# Review CX1 (T-024 final, fix round 1): GNU getopt_long reads any unambiguous prefix of a long option as that
# option (`tar --extr` = `--extract`). The long options whose presence makes a command write more, write elsewhere or
# run a program (deciding options), per program; `long_matches` reads every prefix of three or more characters as the
# option, except a real option of the program that is itself a proper prefix (`tar --checkpoint` is not
# `--checkpoint-action`); a prefix of two or more deciding options is refused (`long_ambiguous`). Options whose presence makes a command write less
# (`--dry-run`, `--spider`, `--no-absolute-filenames`) stay exact, so a prefix spelling only adds checks.
LONG_DECIDING: dict[str, tuple[str, ...]] = {
    "tar": (
        *("--extract", "--get", "--create", "--append", "--update", "--concatenate", "--catenate", "--delete"),
        *("--absolute-names", "--to-command", "--use-compress-program", "--rsh-command", "--rmt-command"),
        *("--info-script", "--new-volume-script", "--checkpoint-action", "--remove-files"),
        *("--listed-incremental", "--index-file", "--directory", "--file"),
        *("--absolute-paths", "--insecure"),  # bsdtar (libarchive), which also reads unique prefixes
    ),
    "cp": (
        *("--target-directory", "--no-target-directory", "--parents", "--symbolic-link", "--link"),
        *("--recursive", "--archive", "--no-dereference"),
    ),
    "mv": ("--target-directory", "--no-target-directory"),
    "ln": ("--target-directory",),
    "install": ("--target-directory", "--no-target-directory", "--directory"),
    "rsync": ("--remove-source-files",),
    "wget": ("--output-document", "--output-file", "--append-output", "--directory-prefix"),
    "sort": ("--output",),
    "patch": ("--output",),
    "csplit": ("--prefix",),
    "cpio": ("--extract", "--pass-through", "--directory", "--file"),
    "sed": ("--in-place", "--expression", "--file", "--line-length"),
    **{name: ("--reference",) for name in ("chmod", "chown", "chgrp")},
    "env": ("--split-string",),
}
# Real options that are proper prefixes of a deciding option (experiment `.tmp/T-024/final/pA/longopts.out`, from each
# program's `--help` on this machine; bsdtar's `--absolute-paths`/`--insecure` are matched by the same rule).
LONG_EXACT_OTHERS = frozenset({"--checkpoint", "--list"})


def long_matches(name: str, full: str) -> bool:
    """Whether the long-option word `name` (before any `=`) is read as `full` (see LONG_DECIDING)."""
    return name == full or (len(name) >= 3 and full.startswith(name) and name not in LONG_EXACT_OTHERS)


def long_ambiguous(program: str, words: list[Word] | list[str]) -> list[str]:
    """Problems for long-option words that abbreviate two or more of the program's deciding options (`tar --c`:
    `--create`, `--catenate`, `--checkpoint-action`): getopt_long refuses an ambiguous prefix, other parsers may
    pick one, and the guard cannot know which, so the command is refused (review CX1, class sweep)."""
    deciding = LONG_DECIDING.get(program, ())
    out: list[str] = []
    for w in words:
        name = str(w).split("=", 1)[0]
        if not name.startswith("--") or name in deciding:
            continue
        hits = [f for f in deciding if long_matches(name, f)]
        if len(hits) >= 2:
            out.append(f"`{program} {name}` abbreviates {', '.join(hits)}; spell the option in full")
    return out


def _opt_value(args: list[Word], names: tuple[str, ...]) -> list[str]:
    """Values of `-o X`, `-oX`, `--output X`, `--output=X` (and any prefix getopt_long accepts, `--out=X`) and of a
    short cluster ending in the letter (`-sSo X`)."""
    out: list[str] = []
    for k, w in enumerate(args):
        a = str(w)
        if not a.startswith("-"):  # a quoted option is still an option to an external program
            continue
        nxt = str(args[k + 1]) if k + 1 < len(args) else ""
        for name in names:
            if name.startswith("--"):
                given, sep, value = a.partition("=")
                if given.startswith("--") and long_matches(given, name):
                    out.append(value if sep else nxt)
            elif not a.startswith("--") and name[1] in a[1:]:
                rest = a[a.index(name[1], 1) + 1 :]
                out.append(rest or nxt)
    return out


POSIX_ALL = {"rm", "rmdir", "unlink", "shred", "touch", "mkdir", "mkfifo", "mknod", "truncate"}
POSIX_VALUE_OPTS = {
    "touch": {"-d", "-t", "-r"},
    "mkdir": {"-m"},
    "truncate": {"-s", "-r"},
    "mknod": {"-m"},
    "mkfifo": {"-m"},
    "shred": {"-n", "-s"},
}
COMPRESSORS = {"gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "unzstd", "lz4"}
CMD_BUILTINS = {
    "copy",
    "move",
    "del",
    "erase",
    "ren",
    "rename",
    "md",
    "mkdir",
    "rd",
    "rmdir",
    "mklink",
}


def _external_targets(cmd: Cmd, writes: Writes) -> None:
    """Write targets of non-PowerShell commands (coreutils, archivers, downloaders, Windows tools)."""
    name, shell, args = cmd.name, cmd.shell, cmd.args
    t = writes.targets

    def add(values: list[Word] | list[str], verb: str = name, **kw: object) -> None:
        for v in values:
            t.append(Target(str(v), verb, shell, **kw))  # type: ignore[arg-type]

    pos = _positionals(args, shell, POSIX_VALUE_OPTS.get(name, set()))
    # A quoted option is still an option to an external program (`cp '--link'`): quoting is not looked at here.
    short = "".join(str(a)[1:] for a in args if re.match(r"-[^-]", str(a)))
    longs = [str(a).split("=", 1)[0] for a in args if str(a).startswith("--")]

    def long_given(full: str, exact: bool = False) -> bool:
        """getopt_long reads a prefix as the option (review CX1, `long_matches`); `exact` for a flag that makes the
        command write less."""
        return full in longs if exact else any(long_matches(x, full) for x in longs)

    if shell != "cmd" or name not in CMD_BUILTINS:
        writes.problems.extend(long_ambiguous("tar" if name == "bsdtar" else name, args))
    if shell == "cmd" and name in CMD_BUILTINS:
        if name in ("copy", "move"):
            if len(pos) >= 2:
                if name == "move":
                    add(pos[:-1])
                for src in pos[:-1]:
                    add([pos[-1]], into_dir=True, source=str(src), links=LINK_COPIES)
            else:
                add(["."], into_dir=True, source=str(pos[0]) if pos else "x", links=LINK_COPIES)
        elif name in ("ren", "rename") and len(pos) == 2:
            add([pos[0], _join(_dirname(str(pos[0])), str(pos[1]))])
        elif name == "mklink":
            _link_targets(pos[:1], pos[1:2], add)
        else:
            add(pos)
        if not pos:
            writes.problems.append(f"`{name}` has no literal path")
        return
    if name in POSIX_ALL:
        add(pos)
    elif name in ("chmod", "chown", "chgrp"):
        mode_as_option = any(re.fullmatch(r"-[rwxXst]+", str(a)) for a in args)
        reference = long_given("--reference")  # `--ref=FILE`: no mode operand, every operand is a file
        add(pos if (mode_as_option or reference) else pos[1:])
    elif name == "tee":
        add(pos)
    elif name in ("cp", "mv", "install", "ln"):
        target_dir = _opt_value(args, ("--target-directory", "-t"))
        values = {"-S", "--suffix", "-m", "-o", "-g", "--mode", "--owner", "--group"}
        pos = _positionals(args, shell, values | {"-t"})
        # `-T` (no target directory: DEST is replaced by SOURCE's contents) and `--parents` (SOURCE's relative path
        # is rebuilt below DEST) write names other than DEST/basename(SOURCE). Any `T` in a short option cluster
        # counts: reading too much only makes the check stricter (Target.spread).
        spread = any(
            (
                any(long_matches(str(a).split("=")[0], f) for f in ("--no-target-directory", "--parents"))
                or (str(a).startswith("-") and not str(a).startswith("--") and "T" in str(a))
            )
            for a in args
        )
        # F27: `cp -s`/`-l` make links to the sources; `cp -r`/`-a`/`-d`/`-P` copy links as links and `mv` moves them.
        makes = name == "cp" and (
            bool(set(short) & set("sl")) or long_given("--symbolic-link") or long_given("--link")
        )
        copies = name == "mv" or (
            name == "cp"
            and (
                bool(set(short) & set("rRadP"))
                or any(long_given(f) for f in ("--recursive", "--archive", "--no-dereference"))
                or any(
                    re.search(r"(?i)links|all", str(a).partition("=")[2])
                    for a in args
                    if long_matches(str(a).partition("=")[0], "--preserve")
                )
            )
        )
        links = LINK_MAKES if makes else LINK_COPIES if copies else ""
        if name == "ln":
            sources = pos if target_dir else pos[:-1]
            _link_targets(target_dir or pos[-1:], sources, add, into_dir=True)  # `ln T`: the link ./T is T's word
        elif name == "install" and ("-d" in [str(a) for a in args] or long_given("--directory")):
            add(pos)
        elif target_dir:
            for d in target_dir:
                for src in pos:
                    add([d], into_dir=True, source=str(src), spread=spread, links=links)
        elif len(pos) >= 2:
            for src in pos[:-1]:
                add([pos[-1]], into_dir=True, source=str(src), spread=spread, links=links)
        if makes:
            # The sources become link targets: each must be writable itself, read from the destination too.
            _link_targets(target_dir or pos[-1:], pos if target_dir else pos[:-1], add, into_dir=True)
        if name == "mv":
            add(pos if target_dir else pos[:-1])
    elif name == "dd":
        add([str(a)[3:] for a in args if str(a).startswith("of=")])
    elif name == "sed":
        _sed_targets(cmd, writes)
    elif name in ("perl", "ruby"):
        _inplace_targets(cmd, writes)
    elif name == "curl":
        add(_opt_value(args, ("-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar")))
        add(_opt_value(args, ("--trace", "--trace-ascii", "--libcurl", "--etag-save", "--stderr")))
        if any(
            re.fullmatch(r"-[A-Za-z]*O[A-Za-z]*", str(a))
            or str(a) in ("--remote-name", "--remote-name-all")
            for a in args
        ):
            out_dir = _opt_value(args, ("--output-dir",)) or ["."]
            add([_join(d, "remote-name") for d in out_dir], spread=True)
    elif name == "wget":
        documents = _opt_value(args, ("-O", "--output-document"))
        add([d for d in documents if d != "-"])
        add(_opt_value(args, ("-o", "--output-file", "-a", "--append-output")))
        if not documents and "--spider" not in [str(a) for a in args]:
            prefix = _opt_value(args, ("-P", "--directory-prefix")) or ["."]
            add([_join(p, "remote-name") for p in prefix], spread=True)  # `-r`/`-x` build a subtree there
    elif name in ("tar", "bsdtar"):
        _tar_targets(cmd, writes)
    elif name == "unzip":
        if not any(str(a) in ("-l", "-v", "-t", "-p", "-Z", "-z") for a in args):
            dest = _opt_value(args, ("-d",)) or ["."]
            add([_join(d, "archive-member") for d in dest], spread=True, links=LINK_MAKES)
            # unzip(1) `-:`: "allows to extract archive members into locations outside of the current extraction
            # root folder" (`../` and absolute names); F07.
            if any(re.fullmatch(r"-[^d-]*:.*", str(a)) for a in args):
                writes.problems.append("`unzip -:` extracts members outside the target directory (`../`, absolute)")
    elif name in ("7z", "7za", "7zr"):
        lowered = [str(a).lower() for a in args]
        if pos and str(pos[0]).lower() in ("x", "e"):
            dirs = [str(a)[2:] for a in args if str(a).startswith("-o")] or ["."]
            add([_join(d, "archive-member") for d in dirs], spread=True, links=LINK_MAKES)
            # 7-Zip `-spf`: "Use fully qualified file paths" (absolute member names); `-snld`: allow dangerous links.
            bad = [a for a in lowered if a.startswith("-spf") or a.startswith("-snld")]
            if bad:
                writes.problems.append(f"`7z {bad[0]}` extracts members outside the target directory")
        elif pos and str(pos[0]).lower() in ("a", "u", "d", "rn") and len(pos) > 1:
            add([pos[1]])
        if any(a.startswith("-sdel") for a in lowered):
            writes.problems.append("`7z -sdel` deletes the archived files")
    elif name == "cpio":
        _cpio_targets(cmd, writes, short, long_given, pos)
    elif name == "pax":
        if "r" in short and "w" in short:
            add([_join(str(d), "archive-member") for d in pos[-1:]], spread=True, links=LINK_MAKES)
        elif "r" in short:
            writes.problems.append(
                "`pax -r` restores member names as stored, absolute ones included; extract with `tar -x -C <dir>`"
            )
        elif "w" in short:
            add(_opt_value(args, ("-f",)))
    elif name == "fsutil":
        # fsutil(8): `hardlink create <new> <existing>`, `reparsepoint delete <file>`, `file createnew <file> <n>`,
        # ...: every path after the two command words may be written, except the query and list forms.
        verb = str(pos[1]).lower() if len(pos) > 1 else ""
        if len(pos) > 2 and not verb.startswith(("query", "list")):
            add(pos[2:], links=LINK_MAKES)
    elif name in ("junction", "linkd"):
        _link_targets(pos[:1], pos[1:2], add)
    elif name == "zip":
        add(pos[:1])
    elif name in COMPRESSORS:
        if not any(str(a) in ("-c", "--stdout", "-t", "--test", "-l", "--list") for a in args):
            add(pos)
    elif name == "patch":
        outputs = _opt_value(args, ("-o", "--output"))
        if outputs:
            add(outputs)
        elif "--dry-run" not in [str(a) for a in args]:
            writes.problems.append(
                "`patch` writes the files named inside the diff; apply it under an allowed directory with `-o`, or use Edit"
            )
    elif name == "split":
        add([pos[-1] if len(pos) >= 2 else "x"])
    elif name == "csplit":
        add([_join(".", (_opt_value(args, ("-f", "--prefix")) or ["xx"])[0])])
    elif name == "sort":
        add(_opt_value(args, ("-o", "--output")))
    elif name == "uniq" and len(pos) >= 2:
        add([pos[1]])
    elif name in ("rsync", "scp"):
        local = [p for p in pos if not re.match(r"^[^/\\]{2,}:", str(p))]
        if len(pos) >= 2 and pos[-1] in local:
            for src in pos[:-1]:
                # A trailing `/` on the source, `-R`/`--relative` and `--files-from` change which names land in
                # DEST; the guard does not model rsync's rules, so DEST counts as receiving any subtree.
                add([pos[-1]], into_dir=True, source=str(src), spread=True, links=LINK_COPIES)
        if long_given("--remove-source-files"):
            add([p for p in pos[:-1] if p in local])
    elif name == "find":
        starts = []
        for a in args:
            if str(a).startswith(("-", "(", "!")):
                break
            starts.append(a)
        if "-delete" in [str(a) for a in args]:
            add(starts or ["."])
        add(_opt_value(args, ("-fprint", "-fprint0", "-fprintf", "-fls")))
    elif name == "robocopy":
        if len(pos) >= 2:
            # `/SL` copies symbolic links as links, `/SJ` junctions as junctions (F27).
            copied = any(str(a).lower() in ("/sl", "/sj") for a in args)
            add([pos[1]], links=LINK_MAKES if copied else "")
            if any(str(a).lower() in ("/mov", "/move") for a in args):
                add([pos[0]])
    elif name == "xcopy":
        # `/B` copies the symbolic link itself instead of its target (F27).
        copied = any(str(a).lower() == "/b" for a in args)
        add([pos[1]] if len(pos) >= 2 else ["."], links=LINK_MAKES if copied else "")
    elif name == "mklink":
        _link_targets(pos[:1], pos[1:2], add)


def _is_absolute(word: str) -> bool:
    return Path(word).is_absolute() or word.startswith(("/", "\\"))


def _link_targets(links: list[Word] | list[str], targets: list[Word] | list[str], add: Callable[..., None],
                  into_dir: bool = False) -> None:
    """A link and what it points to are both written paths (F27): writing through the link writes the target. A
    relative symbolic-link target is read from the link's directory, not the cwd, so that reading is checked too
    (and, for `ln T... DIR`, from DIR)."""
    add(list(links) + list(targets), links=LINK_MAKES)
    for target in targets:
        if _is_absolute(str(target)):
            continue
        for link in links:
            bases = [_dirname(str(link))] + ([str(link)] if into_dir else [])
            add([_join(b, str(target)) for b in bases], links=LINK_MAKES)


def _cpio_targets(cmd: Cmd, writes: Writes, short: str, long_given: Callable[..., bool],
                  pos: list[Word]) -> None:
    """GNU cpio: copy-in (`-i`) extracts below `-D` (default the cwd) and, without `--no-absolute-filenames`, keeps
    absolute member names ("Create all files relative to the current directory in copy-in mode, even if they have an
    absolute file name in the archive" is opt-in); copy-pass (`-p DIR`) writes below DIR; copy-out writes `-O`/`-F`."""
    extract = "i" in short or long_given("--extract")
    passing = "p" in short or long_given("--pass-through")
    # The flag that makes cpio write less is matched exactly: a prefix spelling only refuses more.
    if extract and not long_given("--no-absolute-filenames", True):
        writes.problems.append("`cpio -i` keeps absolute member names; add `--no-absolute-filenames`")
    if extract or passing:
        dirs = _opt_value(cmd.args, ("-D", "--directory")) or ["."]
        if passing:
            dirs = [str(p) for p in pos[-1:]] or ["."]
        writes.targets.extend(
            Target(_join(d, "archive-member"), "cpio", cmd.shell, spread=True, links=LINK_MAKES) for d in dirs
        )
    else:
        writes.targets.extend(Target(f, "cpio -o", cmd.shell) for f in _opt_value(cmd.args, ("-O", "-F", "--file")))


def _sed_targets(cmd: Cmd, writes: Writes) -> None:
    """GNU sed: `-i` edits the file operands; a script with `w`/`W`/`e` (or the `w`/`e` flags of `s`) writes or runs
    files, and a script read from a file (`-f`) cannot be inspected."""
    args = [str(a) for a in cmd.args]
    in_place = False
    scripts: list[str] = []
    from_file = False
    files: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        nxt = args[k + 1] if k + 1 < len(args) else ""
        long_name = a.split("=", 1)[0]
        if a == "--":
            rest = args[k + 1 :]
            if rest and not scripts and not from_file:
                scripts.append(rest.pop(0))
            files += rest
            break
        if a.startswith("--"):
            # getopt_long accepts unique prefixes (`--e=...`, `--in-pl`, `--l 5`; experiment
            # `.tmp/T-024/final/pA/curlprefix.out`); `--f` is sed's own ambiguity, read here as `--file`.
            if long_matches(long_name, "--in-place"):
                in_place = True
            elif long_matches(long_name, "--expression"):
                scripts.append(a.split("=", 1)[1] if "=" in a else nxt)
                k += 0 if "=" in a else 1
            elif long_matches(long_name, "--file"):
                from_file = True
                k += 0 if "=" in a else 1
            elif "=" not in a and long_matches(long_name, "--line-length"):
                k += 1
        elif a.startswith("-") and len(a) > 1:
            body = a[1:]
            for j, ch in enumerate(body):
                if ch == "i":
                    in_place = True
                    break  # the rest of the cluster is the backup suffix
                if ch in ("e", "f", "l"):
                    value = body[j + 1 :] or nxt
                    k += 0 if body[j + 1 :] else 1
                    if ch == "e":
                        scripts.append(value)
                    from_file = from_file or ch == "f"
                    break
        elif not scripts and not from_file:
            scripts.append(a)  # the first operand is the script
        else:
            files.append(a)
        k += 1
    if from_file:
        writes.problems.append(
            "`sed -f` reads its script from a file the guard cannot inspect (it may hold `w`)"
        )
    if any(_sed_writes(s) for s in scripts):
        writes.problems.append(
            "`sed` script uses `w`/`W`/`e` (or the `w`/`e` flag of `s`), which writes or runs files"
        )
    if in_place:
        writes.targets.extend(Target(f, "sed -i", cmd.shell) for f in files)


# Commands with no file operand (an optional number for l/L/q/Q).
SED_PLAIN = set("=dDgGhHnNpPxzFlLqQ")
SED_TO_EOL = set("aicrR#")  # text or a file to read, up to the end of the line
SED_LABEL = set(":btTv")  # a label (or version) up to `;` or the end of the line


def _sed_writes(script: str) -> bool:
    """True when a GNU sed script may write or run files; anything the scanner does not recognise counts as a write."""
    i, n = 0, len(script)
    while i < n:
        if script[i] in " \t\r\n;{}!":
            i += 1
            continue
        end = _sed_address(script, i)
        if end is None:
            return True
        i = end
        while i < n and script[i] in " \t!":
            i += 1
        if i >= n:
            return False
        c = script[i]
        if c in "{}":
            continue
        if c in "wWe":
            return True
        if c in "sy":
            if i + 1 >= n or script[i + 1] in "\\\n":
                return True
            delim, j, found = script[i + 1], i + 2, 0
            while j < n and found < 2:
                if script[j] == "\\":
                    j += 2
                    continue
                found += script[j] == delim
                j += 1
            if found < 2:
                return True
            if c == "s":
                flags = re.match(r"[0-9gpiImMew]*", script[j:])
                if flags and set(flags.group(0)) & {"w", "e"}:
                    return True
                j += len(flags.group(0)) if flags else 0
            i = j
        elif c in SED_TO_EOL:
            line_end = script.find("\n", i)
            i = n if line_end < 0 else line_end
        elif c in SED_LABEL:
            stop = re.search(r"[;\n]", script[i:])
            i = n if stop is None else i + stop.start()
        elif c in SED_PLAIN:
            i += 1
            while i < n and script[i].isdigit():
                i += 1
        else:
            return True
    return False


def _sed_address(s: str, i: int) -> int | None:
    """Index after the (up to two) addresses at `i`: numbers, `first~step`, `$`, `/re/I`, `\\cREc`, `,+N`, `,~N`."""
    n = len(s)
    for second in (False, True):
        start = i
        if second and i < n and s[i] in "+~":
            i += 1
        if i < n and s[i].isdigit():
            while i < n and (s[i].isdigit() or s[i] == "~"):
                i += 1
        elif i < n and s[i] == "$":
            i += 1
        elif i < n and s[i] in "/\\":
            if s[i] == "\\":
                if i + 1 >= n:
                    return None
                delim, i = s[i + 1], i + 2
            else:
                delim, i = "/", i + 1
            while i < n and s[i] != delim:
                i += 2 if s[i] == "\\" else 1
            if i >= n:
                return None
            i += 1
            while i < n and s[i] in "IM":
                i += 1
        elif second:
            return None if i == start else i  # `addr,` must be followed by an address
        while i < n and s[i] in " \t":
            i += 1
        if second or i >= n or s[i] != ",":
            return i
        i += 1
        while i < n and s[i] in " \t":
            i += 1
    return i


def _inplace_targets(cmd: Cmd, writes: Writes) -> None:
    """perl/ruby `-i[ext]` (also inside a cluster: `-pi`, `-pie`): the file operands are edited in place."""
    args = [str(a) for a in cmd.args]
    in_place = False
    code_given = False
    operands: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            operands += args[k + 1 :]
            break
        if a.startswith("-") and len(a) > 1 and not a.startswith("--"):
            body = a[1:]
            for j, ch in enumerate(body):
                if ch == "i":
                    in_place = True
                    break
                if ch in ("e", "E"):
                    code_given = True
                    if j == len(body) - 1:
                        k += 1
                    break
                if ch in ("I", "M", "m", "C", "x", "0", "l", "d", "D"):
                    break  # takes the rest of the cluster as its value
        elif not a.startswith("-"):
            operands.append(a)
        k += 1
    files = operands if code_given else operands[1:]
    if in_place:
        writes.targets.extend(Target(f, f"{cmd.name} -i", cmd.shell) for f in files)


TAR_VALUE_LETTERS = set("fCgFIKLNTVXbH")  # GNU tar short options that take a value (tar --help), protocol-bound
TAR_RUNS = (
    *("--to-command", "--use-compress-program", "--rsh-command", "--rmt-command", "--info-script"),
    *("--new-volume-script", "--checkpoint-action"),
)


def _tar_targets(cmd: Cmd, writes: Writes) -> None:
    args = [str(a) for a in cmd.args]
    letters = ""
    if args and not args[0].startswith("-"):
        letters = args[0]  # old style: `tar xzf a.tgz`
    letters += "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    longs = {a.split("=")[0] for a in args if a.startswith("--")}

    def given(full: str) -> bool:
        return any(long_matches(x, full) for x in longs)  # review CX1: `--extr` is `--extract`

    extract = "x" in letters or given("--extract") or given("--get")
    create = any(c in letters for c in "cruA") or any(
        given(f) for f in ("--create", "--append", "--update", "--concatenate", "--catenate", "--delete")
    )

    # The option letters as getopt reads them: a letter that takes a value ends its cluster (`-xf.tmp/Foo.tar`
    # holds no `F`); an old-style first word is all letters.
    short = args[0] if args and not args[0].startswith("-") else ""
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            for ch in a[1:]:
                short += ch
                if ch in TAR_VALUE_LETTERS:
                    break
    # GNU tar(1) `-P, --absolute-names`: "don't strip leading '/'s from file names"; bsdtar `-P`, `--absolute-paths`,
    # `--insecure`: also keep `..`. Members then land anywhere, not below `-C` (F07).
    if extract and ("P" in short or given("--absolute-names") or given("--absolute-paths") or given("--insecure")):
        writes.problems.append("`tar -P` extracts members by their absolute names, outside the target directory")
    # Options that run a program the guard cannot inspect (tar --help: `--to-command=COMMAND`, `-I,
    # --use-compress-program=PROG`, `--rsh-command=COMMAND`, `-F, --info-script=NAME`, `--checkpoint-action=ACTION`).
    runs = [f for f in TAR_RUNS if given(f)]
    if runs or "I" in short or "F" in short:
        writes.problems.append(f"`tar {runs[0] if runs else '-I/-F'}` runs a program the guard cannot inspect")
    if given("--remove-files"):
        writes.problems.append("`tar --remove-files` deletes the archived files; remove them with `rm`")
    # Files tar writes beside the archive: `-g, --listed-incremental=FILE`, `--index-file=FILE`.
    writes.targets.extend(
        Target(f, "tar", cmd.shell) for f in _opt_value(cmd.args, ("-g", "--listed-incremental", "--index-file")) if f
    )
    if extract:
        dirs = _opt_value(cmd.args, ("-C", "--directory"))
        writes.targets.extend(
            Target(_join(d, "archive-member"), "tar -x", cmd.shell, spread=True, links=LINK_MAKES)
            for d in (dirs or ["."])
        )
    if create:
        archive = _opt_value(cmd.args, ("--file",))
        if not archive:
            # `-f X`, `-cf X`, old-style `cf X`: the value is the word after the cluster that holds `f`.
            for k, a in enumerate(args):
                cluster = (
                    a[1:] if a.startswith("-") and not a.startswith("--") else (a if k == 0 else "")
                )
                if "f" in cluster:
                    rest = cluster[cluster.index("f") + 1 :] if a.startswith("-") else ""
                    archive = [rest or (args[k + 1] if k + 1 < len(args) else "")]
                    break
        if "f" not in letters and not archive:
            return  # no `-f`: the archive goes to stdout
        if not archive or not all(archive):
            writes.problems.append("`tar` creates an archive whose name the guard cannot find")
        writes.targets.extend(Target(a, "tar -c", cmd.shell) for a in archive if a and a != "-")


WRITE_VERBS = {
    *PS_CMDLETS,
    *PS_OUTPUT_PARAMS,
    *PS_TEMP_WRITERS,
    *START_PROCESS,
    *POSIX_ALL,
    *CMD_BUILTINS,
    *COMPRESSORS,
    *("chmod", "chown", "chgrp", "tee", "cp", "mv", "install", "ln", "dd", "sed", "perl"),
    *("ruby", "curl", "wget", "tar", "bsdtar", "unzip", "7z", "7za", "7zr", "zip", "patch"),
    *("split", "csplit", "sort", "uniq"),
    *("rsync", "scp", "find", "robocopy", "xcopy", "mklink"),
    *("cpio", "pax", "fsutil", "junction", "linkd"),
}
# Fail-closed trigger: a command the guard cannot analyse is blocked only when this matches (see trigger_text).
WRITE_TRIGGER = re.compile(
    r">|\bIO\.|\.\s*(?:Delete|MoveTo|CopyTo|Save)\s*\(|(?<![\w-])(?:"
    + "|".join(re.escape(v) for v in sorted(WRITE_VERBS, key=len, reverse=True))
    + r")(?![\w-])",
    re.I,
)


def trigger_text(command: str) -> str:
    """The command with quotes, escapes and invisible characters removed, for the fail-closed trigger patterns
    (`S`et-C`ontent`, `g"i"t`, `'Set-'+'Content'` and `g<U+200B>it` all still match)."""
    command = re.sub(r"(['\"])\s*\+\s*(['\"])", "", command)
    text = "".join(
        ch for ch in command if unicodedata.category(ch) not in ("Cf",) and ch not in "`'\"^\\"
    )
    return text


# Text that builds a string at run time (concatenation, format, decoding, character arithmetic, text filters):
# with an unresolved command name or method it may spell any command, so it counts as a trigger.
BUILT_STRING = re.compile(
    r"(?i)['\"]\s*\+|\+\s*['\"]|(?<![\w-])-(?:f|join|c?replace|ireplace|split|bxor)(?![\w-])|\[char\]"
    r"|\[string\]\s*::|frombase64|tochararray|getstring\s*\(|\.\s*(?:replace|substring|insert|remove)\s*\("
    r"|reverse|concat\s*\(|\.\s*join\s*\(|\$'|\\x[0-9a-f]{2}|\$\{\w+(?:/|:|\^|,)"
    r"|(?<![\w.-])(?:rev|tr|base64|xxd|od|printf|awk|cut|sed|perl|python[\w.]*|iconv)(?![\w-])"
)
# PowerShell code that calls something the guard cannot name: a method or type held in a string or variable,
# reflection, compiled or COM code, or a function/alias defined through its drive.
PS_OPAQUE_CODE = re.compile(
    r"(?i)::\s*[^\w\s]|\$[\w:]+\s*::|\.\s*(?:\$[\w:]+|[`\\]?['\"][^'\"]*[`\\]?['\"])\s*\(|\.\s*invoke\s*\(|invokemember"
    r"|invokescript|getmethods?\s*\(|createdelegate|newscriptblock|\[scriptblock\]\s*::\s*create|add-type"
    r"|\[activator\]|-comobject|process\]\s*::\s*start|\$\{?(?:alias|function|filter)\s*:"
)
# A variable used as a whole command name, per shell (cmd `%x%` is never resolved).
PLAIN_VAR = {
    "ps": re.compile(r"(?i)\$(?:\{(?:env:)?(\w+)\}|(?:env:)?(\w+))"),
    "bash": re.compile(r"\$(?:\{(\w+)\}|(\w+))"),
}
# Variables the shell sets itself (loop items, arguments, match results): their value is never a literal here.
SPECIAL_VARS = {
    "ps": {
        *("_", "psitem", "args", "input", "this", "matches", "foreach", "switch", "sender"),
        *("eventargs", "event", "error", "myinvocation", "psboundparameters", "executioncontext"),
    },
    "bash": {"_", "REPLY", "BASH_COMMAND", "OPTARG", "BASH_REMATCH", "FUNCNAME", "BASH_ARGV"}
    | {"MAPFILE", "COPROC"},
}
EVALUATORS = {"iex", "invoke-expression", "eval", "source", "."}
RUN_TIME_CHARS = re.compile(r"[$`(){}@]")
MAX_EXPANSIONS = 16  # parser-bound: words produced by substituting variable values into one target


def _ps_called(word: str, walked: Walk) -> bool:
    """`& <word>` or `. <word>` in PowerShell text (the parser drops `&`, a separator, before the command). A
    closer before the operator (`if(1){1}&$n`, `(x).$n`) still counts (review N3): a member access read as a
    call only adds checks."""
    call = re.compile(r"(?<![\w$.])[&.]\s*['\"]?" + re.escape(word) + r"(?![\w:])", re.I)
    return any(shell == "ps" and call.search(raw) for raw, _masked, shell in walked.texts)


def head_names(cmd: Cmd, walked: Walk) -> list[str] | None:
    """The command names this command can run: its literal name, or the literal values its variable is bound to
    in this command (`$PY = '...python.exe'; & $PY`). [] when it runs no command; None when the name is computed
    at run time (substitution, string building, an unknown or special variable): fail closed."""
    heads = head_words(cmd, walked)
    return None if heads is None else [h[0] for h in heads]


def resolved_cmds(cmd: Cmd, walked: Walk) -> list[Cmd] | None:
    """The command with its head replaced by each command line it can run (`head_words`); None when computed."""
    heads = head_words(cmd, walked)
    if heads is None:
        return None
    unique = dict.fromkeys(tuple(h) for h in heads)
    return [cmd if list(h) == [cmd.name] else _renamed(cmd, list(h)) for h in unique]


# cmd `%x%` and delayed-expansion `!x!` (`cmd /v:on`) are never resolved.
CMD_VARIABLE = re.compile(r"[%!]")


def head_words(cmd: Cmd, walked: Walk) -> list[list[str]] | None:
    """`head_names` with each name's whole command line: a bash variable bound to `rm README.md` runs `rm` with
    `README.md` as its first argument (review N2), so every word of the value is checked."""
    if cmd.head is None:
        return None if set(cmd.wrappers) & RUN_TIME_WRAPPERS else []
    word = cmd.words[cmd.head]
    text = str(word)
    called = cmd.shell != "ps" or bool(cmd.wrappers) or _ps_called(text, walked)
    if not called and word.literal:
        return []  # a PowerShell string statement (`$c = 'Set-Content'`) is a value, not a call
    cmd_var = cmd.shell == "cmd" and bool(CMD_VARIABLE.search(text))
    if not RUN_TIME_CHARS.search(text) and not cmd_var:
        return [[cmd.name]]
    if not called:
        # Without `&`/`.` a PowerShell statement that starts with a value is an expression, not a call.
        if text[:1] in "$@([" or text[:1].isdigit() or word.quoted:
            return [[cmd.name]]
        return None
    if (cmd.shell == "ps" or word.literal) and not set(cmd.wrappers) & EVALUATORS:
        # A computed directory with a literal program name (`& "$R/.venv/Scripts/python.exe"`); bash splits an
        # unquoted expansion into words, so there only a fully quoted word keeps its basename.
        base = cmd.name
        if base and not RUN_TIME_CHARS.search(base) and "%" not in base and not cmd_var:
            return [[base]]
    var = PLAIN_VAR.get(cmd.shell)
    match = var.fullmatch(text) if var else None
    if not match or set(cmd.wrappers) & EVALUATORS:
        return None
    name = match.group(1) or match.group(2)
    if (name.lower() if cmd.shell == "ps" else name) in SPECIAL_VARS[cmd.shell] or name.isdigit():
        return None
    values = _bindings(name, walked, cmd.shell)
    if values is None:
        return None
    if not values:
        return [[cmd.name]]  # not set in this command: an environment value, read like before
    heads = []
    for value in values:
        if cmd.shell == "bash":
            # An unquoted bash variable is split into words: a value holding another variable may hold any.
            parts = value.split() if "$" not in value else []
        else:
            # `& $x` runs the whole value as one name; only its basename matters.
            parts = [value.strip()]
        base = pg._head(parts[0]) if parts else ""
        if not base or RUN_TIME_CHARS.search(base):
            return None
        if base in RUN_TIME_WRAPPERS or base in pg.WRAPPERS or base in pg.SHELLS:
            return None
        heads.append([base, *parts[1:]])
    return heads


def _bindings(name: str, walked: Walk, shell: str) -> list[str] | None:
    """Every literal value assigned to the variable in this command; None when any assignment or other use
    can set it to something the guard cannot read (`Set-Variable`, `read`, `for x in`, `-OutVariable`, ...)."""
    esc = re.escape(name)
    flags = re.I if shell == "ps" else 0
    pattern = re.compile(
        rf"(?P<ref>\$\{{(?:env:)?{esc}\}}|\$(?:env:)?{esc}(?![\w:]))|(?<![\w.\\/$-]){esc}(?!\w)",
        flags,
    )
    values: list[str] = []
    for raw, _masked, text_shell in walked.texts:
        for m in pattern.finditer(raw):
            after = raw[m.end() :]
            if m["ref"]:
                before = raw[: m.start()].rstrip().lower()
                if re.match(r"\s*(?:[-+*/%]=|\+\+|--|,)", after) or before.endswith(
                    ("foreach (", "foreach(", "[ref]")
                ):
                    return None
                op = re.match(r"\s*=(?!=)", after)
                if op:
                    value = _literal_value(after[op.end() :], text_shell)
                    if value is None:
                        return None
                    values.append(value)
                continue
            # The bare name: bash `NAME=value` assigns; any other bare use may set it (`read NAME`, `sv NAME`).
            if text_shell == "bash" and after.startswith("=") and not after.startswith("=="):
                value = _literal_value(after[1:], "bash")
                if value is None:
                    return None
                values.append(value)
                continue
            return None
    return values


def _literal_value(text: str, shell: str) -> str | None:
    """The value at the start of an assignment's right-hand side: literal text that may still name plain variables
    (`"$R/.venv/python.exe"`, expanded by the caller); None when it is computed (substitution, concatenation)."""
    if shell == "bash":
        m = re.match(r"(?:'([^']*)'|\"([^\"`\\]*)\"|([^\s;&|()<>'\"`\\]*))(?=[\s;&|)]|$)", text)
        if not m:
            return None
        value = next(g for g in m.groups() if g is not None)
    else:
        m = re.match(
            r"[ \t]*(?:'((?:[^']|'')*)'|\"([^\"`]*)\"|(\d+))(?=[ \t]*(?:[;\r\n})]|$))", text
        )
        if not m:
            return None
        value = (
            m[1].replace("''", "'") if m[1] is not None else (m[2] if m[2] is not None else m[3])
        )
    return None if "$(" in value or "`" in value else value


def _expand_target(target: Target, walked: Walk) -> list[Target]:
    """The target with each variable replaced by its literal values in this command (and a leading unbound
    `$PWD` by `.`); a variable the guard cannot resolve stays, so the path policy blocks it."""
    var = PLAIN_VAR.get(target.shell)
    if var is None:
        return [target]
    words = [target.word]
    for _ in range(3):
        out: list[str] = []
        changed = False
        for word in words:
            m = var.search(word)
            if not m:
                out.append(word)
                continue
            name = m.group(1) or m.group(2)
            values = _bindings(name, walked, target.shell)
            pwd = name == "PWD" or (target.shell == "ps" and name.lower() == "pwd")
            if values == [] and pwd and m.start() == 0:
                values = ["."]
            if not values or (target.shell == "bash" and any(re.search(r"\s", v) for v in values)):
                return [target]
            out += [word[: m.start()] + v + word[m.end() :] for v in values]
            changed = True
        words = out
        if len(words) > MAX_EXPANSIONS:
            return [target]
        if not changed:
            break
    return [target._replace(word=w) for w in words]


def opaque_constructs(walked: Walk) -> list[tuple[str, str]]:
    """Constructs whose effect the guard cannot resolve: ("name", ...) for a command name computed at run time,
    ("code", ...) for PowerShell code that calls a method, type or function the guard cannot name."""
    found: list[tuple[str, str]] = []
    for cmd in walked.cmds:
        if head_names(cmd, walked) is None:
            shown = str(cmd.words[cmd.head]) if cmd.head is not None else " ".join(cmd.wrappers)
            found.append(("name", f"`{shown[:60]}` is a command name computed at run time"))
    for raw, masked, shell in walked.texts:
        computed = PS_COMPUTED_HEAD if shell == "ps" else BASH_COMPUTED_HEAD
        m = computed.search(masked)
        if m:
            found.append(("name", f"`{m.group(1)}...` computes a command name at run time"))
        if shell == "ps":
            code = PS_OPAQUE_CODE.search(pg.EMBEDDED_SCRIPT.sub("", raw))
            if code:
                found.append(
                    ("code", f"`{code.group(0).strip()}` calls code the guard cannot name")
                )
    return list(dict.fromkeys(found))


def _renamed(cmd: Cmd, head: str | list[str]) -> Cmd:
    """The command with its head replaced by a resolved name, or by the words of a resolved command line."""
    assert cmd.head is not None
    new = [Word(w) for w in ([head] if isinstance(head, str) else head)]
    return cmd._replace(words=[*cmd.words[: cmd.head], *new, *cmd.words[cmd.head + 1 :]])


def _cmd_targets(cmd: Cmd, writes: Writes) -> None:
    if cmd.shell == "ps":
        _ps_targets(cmd, writes)
    elif not (cmd.shell == "bash" and cmd.name in PS_CMDLETS and "-" in cmd.name):
        _external_targets(cmd, writes)  # a PowerShell cmdlet name is not a command in bash


def _probe_words(word: str, shell: str) -> list[str]:
    """The word and the values an option word may carry (`--directory=D`, `-DD`, `of=D`, `/log:D`, `-Path:D`):
    reading too many candidates only adds checks, each resolved as a path."""
    out = [word]
    if "=" in word:
        out.append(word.split("=", 1)[1])
    if word.startswith("-") and not word.startswith("--") and len(word) > 2:
        out.append(word[2:])
    if word[:1] in ("-", "/") and ":" in word:
        out.append(word.split(":", 1)[1])
    return [w for w in dict.fromkeys(out) if w]


def _add_probes(cmd: Cmd, writes: Writes, first_target: int) -> None:
    own = {t.word for t in writes.targets[first_target:]}
    for arg in cmd.args:
        for word in _probe_words(str(arg), cmd.shell):
            # cmd.exe takes `/x` switches and a command line, no path operand (the walker reads that line); Git
            # Bash would read `/c` as the drive root C:.
            if cmd.name == "cmd" and word == str(arg) and word.startswith("/"):
                continue
            if word not in own:
                writes.probes.append((cmd.name, Target(word, f"`{cmd.name}` argument", cmd.shell)))


def write_targets(walked: Walk) -> Writes:
    writes = Writes([], [], [], [])
    starts: list[int] = []
    for cmd in walked.cmds:
        starts.append(len(writes.targets))
        # `[[ a > b ]]` compares strings; in bash `[ a > b ]` and `test a > b` redirect like any command.
        in_test = cmd.name == "[["
        for r in cmd.redirects:
            if ">" in r.op and not in_test:  # `>`, `>>`, `>|`, `&>`, `>&`, `<>`
                if r.target is None:
                    continue
                writes.targets.append(Target(str(r.target), "redirection", cmd.shell, provider=cmd.shell == "ps"))
        for word in cmd.extra_writes:
            writes.targets.append(Target(str(word), "Start-Process redirection", cmd.shell, provider=True))
        if cmd.head is None:
            continue
        if cmd.name in CD_COMMANDS:
            pos = [a for a in cmd.args if not _is_option(str(a), cmd.shell)]
            writes.cwd_changes.append((str(pos[0]) if pos else "~", cmd.shell))
            continue
        before = len(writes.targets) + len(writes.problems)
        resolved = resolved_cmds(cmd, walked)
        for one in [cmd] if resolved is None else resolved:
            first_target = len(writes.targets)
            _cmd_targets(one, writes)
            _add_probes(one, writes, first_target)
        wrote = len(writes.targets) + len(writes.problems) > before
        stdin_args = STDIN_ARG_WRAPPERS.intersection(cmd.wrappers)
        if stdin_args and (wrote or WRITE_TRIGGER.search(trigger_text(" ".join(cmd.words)))):
            writes.problems.append(
                f"`{min(stdin_args)} {cmd.name}` takes its targets from stdin or `:::`"
            )
    # Each target's command, in run order (F27); .NET calls are found in the text, their order unknown (-1).
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(writes.targets)
        writes.targets[start:end] = [t._replace(group=i) for t in writes.targets[start:end]]
    for raw, masked, shell in walked.texts:
        if shell == "ps":
            _dotnet_targets(raw, masked, writes)
    writes.targets[:] = [t2 for t in writes.targets for t2 in _expand_target(t, walked)]
    writes.probes[:] = [(n, t2) for n, t in writes.probes if "$" in t.word for t2 in _expand_target(t, walked)] + [
        p for p in writes.probes if "$" not in p[1].word
    ]
    return writes


# A command name computed by a substitution: bash `$(...)`/backticks in command position, PowerShell `& (...)`,
# `& $(...)` or `. (...)`. A bare bash `( ... )` is a subshell whose commands the walker already sees.
_CMD_START = r"(?:^|[;|{\n]|&&|\|\|)\s*"
BASH_COMPUTED_HEAD = re.compile(_CMD_START + r"(?:\w+=\S*\s+)*(\$\(|`)")
PS_COMPUTED_HEAD = re.compile(r"(?:^|[;|{\n(])\s*[&.]\s*(\$?\()")


# .NET file APIs called from PowerShell. Read-only methods are allowed; any other static method writes.
DOTNET_STATIC = re.compile(
    # A quoted method name (`::"WriteAllText"`, escaped `` ::`"WriteAllText`" `` inside a string) calls the same method.
    r"\[\s*(?:System\.)?IO\.(?:Compression\.)?(?P<cls>File|Directory|ZipFile|Path)\s*\]\s*::\s*"
    r"(?:[`\\]?['\"])?(?P<meth>\w+)(?:[`\\]?['\"])?",
    re.I,
)
DOTNET_READS = {
    "file": {
        *("readalltext", "readalllines", "readallbytes", "readlines", "exists", "getattributes"),
        *("getcreationtime", "getcreationtimeutc", "getlastwritetime", "getlastwritetimeutc"),
        *("openread", "getlastaccesstime", "getlastaccesstimeutc", "opentext", "resolvelinktarget"),
        *("readalltextasync", "readalllinesasync", "readallbytesasync", "readlinesasync"),
        *("getunixfilemode",),
    },
    "directory": {
        *("exists", "getfiles", "getdirectories", "getfilesystementries", "enumeratefiles"),
        *("enumeratedirectories", "enumeratefilesystementries", "getcurrentdirectory", "getparent"),
        *("getdirectoryroot", "getlogicaldrives", "getcreationtime", "getlastwritetime"),
        *("getlastaccesstime", "resolvelinktarget"),
    },
    "zipfile": {"openread"},
    "path": {
        *("combine", "join", "getfullpath", "getfilename", "getdirectoryname", "getextension"),
        *("getfilenamewithoutextension", "changeextension", "hasextension", "ispathrooted"),
        *("getrelativepath", "getpathroot", "gettemppath", "getinvalidpathchars"),
        *("getinvalidfilenamechars", "endsindirectoryseparator", "trimendingdirectoryseparator"),
        *("ispathfullyqualified", "getrandomfilename", "exists"),
    },
}
# Path argument positions of the write methods; a method not listed here fails closed.
DOTNET_PATH_ARGS: dict[str, dict[str, tuple[int, ...]]] = {
    "file": {
        **dict.fromkeys(("writealltext", "writeallbytes", "writealllines", "appendalltext"), (0,)),
        **dict.fromkeys(("appendalllines", "appendtext", "create", "createtext"), (0,)),
        **dict.fromkeys(("delete", "open"), (0,)),
        **dict.fromkeys(("openwrite", "openhandle", "encrypt", "decrypt", "setattributes"), (0,)),
        **dict.fromkeys(("setcreationtime", "setcreationtimeutc", "setlastwritetime"), (0,)),
        **dict.fromkeys(("setlastwritetimeutc", "setlastaccesstime", "setlastaccesstimeutc"), (0,)),
        **dict.fromkeys(("writealltextasync", "writeallbytesasync", "writealllinesasync"), (0,)),
        **dict.fromkeys(("appendalltextasync", "appendalllinesasync", "setunixfilemode"), (0,)),
        "copy": (1,),
        "move": (0, 1),
        "replace": (0, 1, 2),
        "createsymboliclink": (0, 1),
    },
    "directory": {
        "createdirectory": (0,),
        "delete": (0,),
        "move": (0, 1),
        "createsymboliclink": (0, 1),
        **dict.fromkeys(("setcreationtime", "setlastwritetime", "setlastaccesstime"), (0,)),
    },
    "zipfile": {"extracttodirectory": (1,), "createfromdirectory": (1,), "open": (0,)},
    "path": {},
}
DOTNET_CTOR = re.compile(
    r"(?:New-Object\s+(?:-TypeName\s+)?(?:System\.)?IO\.(?P<a>StreamWriter|FileStream|BinaryWriter)"
    r"|\[\s*(?:System\.)?IO\.(?P<b>StreamWriter|FileStream|BinaryWriter)\s*\]\s*::\s*new)",
    re.I,
)
DOTNET_INSTANCE_WRITE = re.compile(
    r"\.\s*(?:Delete|MoveTo|CopyTo|CreateSubdirectory|CreateText|AppendText|OpenWrite|Encrypt|Decrypt"
    r"|SetAccessControl|Save|WriteAllText|WriteAllBytes|WriteAllLines|AppendAllText|AppendAllLines"
    r"|CreateAsSymbolicLink)\s*\(",
    re.I,
)
PS_LITERAL = re.compile(r"^\s*(?:'(?P<s>(?:[^']|'')*)'|\"(?P<d>[^\"`$]*)\")\s*$", re.S)


def _call_args(text: str, start: int) -> list[str] | None:
    """Top-level arguments of the call whose `(` is at or after `start`; None when there is no closed call."""
    open_at = text.find("(", start)
    if open_at < 0 or text[start:open_at].strip():
        return None
    depth, quote, args, cur = 0, "", [], []
    for ch in text[open_at + 1 :]:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                args.append("".join(cur))
                return [a for a in args if a.strip()] if any(a.strip() for a in args) else []
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    return None


def _literal(arg: str) -> str | None:
    match = PS_LITERAL.match(arg)
    if not match:
        return None
    return match["s"].replace("''", "'") if match["s"] is not None else match["d"]


def _dotnet_targets(raw: str, masked: str, writes: Writes) -> None:
    if DOTNET_INSTANCE_WRITE.search(masked):
        writes.problems.append(
            "a .NET instance method writes a file whose path the guard cannot resolve (`.Delete()`, `.MoveTo()`, `.Save()`, ...)"
        )
    # The raw text: the mask blanks a quoted method name.
    if not (DOTNET_STATIC.search(raw) or DOTNET_CTOR.search(raw)):
        return
    for match in DOTNET_STATIC.finditer(raw):
        cls, meth = match["cls"].lower(), match["meth"].lower()
        if meth in DOTNET_READS.get(cls, set()):
            continue
        if cls == "path" and meth == "gettempfilename":
            writes.problems.append(
                "[IO.Path]::GetTempFileName creates a file in the OS temp directory"
            )
            continue
        positions = DOTNET_PATH_ARGS.get(cls, {}).get(meth)
        if positions is None:
            writes.problems.append(
                f"[IO.{match['cls']}]::{match['meth']} is not a recognised read-only method"
            )
            continue
        args = _call_args(raw, match.end())
        if args is None:
            writes.problems.append(
                f"[IO.{match['cls']}]::{match['meth']} has arguments the guard cannot read"
            )
            continue
        first = len(writes.targets)
        _literal_args(args, positions, f"[IO.{match['cls']}]::{match['meth']}", writes)
        if meth == "createsymboliclink" and len(writes.targets) - first == 2:
            # (path, pathToTarget): a relative target is read from the link's directory (F27).
            link, target = writes.targets[first:]
            extra = [] if _is_absolute(target.word) else [target._replace(word=_join(_dirname(link.word), target.word))]
            writes.targets[first:] = [t._replace(links=LINK_MAKES) for t in (link, target, *extra)]
    for match in DOTNET_CTOR.finditer(raw):
        kind = match["a"] or match["b"]
        rest = raw[match.end() :]
        args = _call_args(rest, 0)
        if args is None:
            first = re.match(r"\s*(?:-ArgumentList\s+)?('(?:[^']|'')*'|\"[^\"]*\")", rest, re.I)
            args = [first.group(1)] if first else None
        if not args:
            writes.problems.append(f"IO.{kind} opens a file whose path the guard cannot read")
            continue
        _literal_args(args, (0,), f"IO.{kind}", writes)


def _literal_args(args: list[str], positions: tuple[int, ...], verb: str, writes: Writes) -> None:
    for pos in positions:
        value = _literal(args[pos]) if pos < len(args) else None
        if value is None:
            writes.problems.append(f"{verb}: path argument {pos + 1} is not a literal string")
        else:
            writes.targets.append(Target(value, verb, "ps"))


# ---------------------------------------------------------------------------------------------------------------
# Path policy

MKDIR_VERBS = {"mkdir", "md"}
# Bounded work per hook call: a wildcard directly in an allowlisted directory is checked match by match (each
# a `git ls-files` call); more matches fail closed.
MAX_GLOB_MATCHES = 200
# Bounded work per hook call (F27): entries a copy's source tree is scanned for links; more count as a link.
MAX_LINK_SCAN = 5000
# Safety-bound (S42 F6): `git ls-files` checks per path policy (one per allowed candidate path). Candidates multiply
# as targets x variable values (MAX_EXPANSIONS) x readings x bases (MAX_CWD_BASES): measured on the Windows dev
# machine at about 22 ms each, 1024 checks took 22 s, so 512 stays well inside the 30 s hook timeout. More fail closed.
MAX_TRACKED_CHECKS = 512
NAME_SURROGATE = 0x20000000  # Windows reparse tag bit of symbolic links and junctions (IsReparseTagNameSurrogate)


def fs_real(path: str | Path) -> str:
    """The path as the file system resolves it: `os.path.realpath` (links, `..`, and on Windows 8.3 short names and
    the case of existing components), then, on Windows, the aliases realpath leaves (T-024 final, package B's
    sibling): the `\\\\?\\` and `\\\\.\\` prefixes it may return (it does for a component with a trailing space), an
    alternate data stream on any component (`name:stream`, `name::$DATA` name the entry itself), and trailing dots
    and spaces, which Win32 strips from every component. Experiment: `.tmp/T-024/final/pA/alias_probe.out`."""
    if not WINDOWS:
        return os.path.realpath(path)
    # Folded before realpath too: ntpath reads a `name:stream` component as a drive-relative path and drops the
    # directories before it (`realpath('C:\\x\\d:s')` is `d:s`).
    return _win_fold(os.path.realpath(_win_fold(str(path))))


def ps_path_readings(text: str, shell: str, provider: bool, windows: bool) -> list[str]:
    """The spellings a guard checks for one path word. On a POSIX host a PowerShell cmdlet (provider) path holding
    `\\` has two readings: pwsh's FileSystem provider may take `\\` as a separator (not verified here, no network;
    S42 PM decision pending user review) or keep it in the name. Both are returned so the write is refused when
    either reading is refused, never allowed on a guess. Native-program words and Windows hosts keep one reading."""
    if windows or shell != "ps" or not provider or "\\" not in text:
        return [text]
    return [text, text.replace("\\", "/")]


def _win_fold(text: str) -> str:
    text = re.sub(r"^(?:\\\\|//)[?.][\\/]UNC[\\/]", r"\\\\", text, flags=re.I)
    text = re.sub(r"^(?:\\\\|//)[?.][\\/]", "", text)
    drive, rest = os.path.splitdrive(text)
    parts = [p if p in (".", "..") else (re.sub(r"[. ]+$", "", p.split(":", 1)[0]) or p)
             for p in re.split(r"[\\/]", rest)]
    return drive + "\\".join(parts)


def fs_key(path: str | Path) -> str:
    """`fs_real` folded for comparison (`os.path.normcase`: case and separators on Windows)."""
    return os.path.normcase(fs_real(path))


def _is_link(path: str | Path) -> bool:
    """A symbolic link, or on Windows a name-surrogate reparse point (junction, symbolic link); not a placeholder or
    deduplication reparse point, which the OS resolves to the file itself."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    tag = getattr(st, "st_reparse_tag", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and tag & NAME_SURROGATE)


def _link_component(path: str) -> str | None:
    """The first existing component of an already resolved path that is still a link, else None."""
    current = Path(path)
    for part in [*reversed(current.parents), current]:
        if _is_link(part):
            return str(part)
    return None


def _tree_has_link(path: Path, budget: list[int]) -> bool:
    """Whether the path or anything below it is a link; an unreadable tree or one over the budget counts as one."""
    if _is_link(path):
        return True
    if not path.is_dir():
        return False

    def fail(exc: OSError) -> None:
        raise exc

    try:
        for top, dirs, files in os.walk(path, onerror=fail):
            for name in dirs + files:
                budget[0] -= 1
                if budget[0] < 0 or _is_link(os.path.join(top, name)):
                    return True
    except OSError:
        return True
    return False


class PathPolicy:
    """Decide whether shell-written paths stay inside the allowlist (relative to the innermost Git work tree)."""

    def __init__(
        self, root: Path, allowed_dirs: list[str], git_timeout: int, protected: tuple[Path, ...] = ()
    ) -> None:
        self.root = Path(fs_real(root))
        # T-024 round 5 and final (PD1): directories no target may lie in, even inside the allowlist (in worker mode
        # the orchestrator's whole state directory: Git ledger, push markers, patches, registry); a wildcard whose
        # literal part holds such a directory may match into it.
        self.protected = [fs_key(p) for p in protected]
        # An entry is a path prefix (`.cache/tmp`): compared component by component, never as a string prefix.
        self._entries = [tuple(os.path.normcase(p) for p in d.split("/")) for d in allowed_dirs]
        self.allowed_text = ", ".join(f"`{d}/`" for d in allowed_dirs)
        self.git_timeout = git_timeout
        self._worktrees: dict[Path, list[Path] | None] = {}
        self._tracked_checks = 0  # spent of MAX_TRACKED_CHECKS

    @staticmethod
    def _text(target: Target) -> str:
        if target.shell == "ps":
            return re.sub(r"(?i)^(?:Microsoft\.PowerShell\.Core\\)?FileSystem::", "", target.word)
        return target.word

    def _resolved(
        self, target: Target, bases: list[Path], literal: bool = False, any_program: bool = False
    ) -> list[Path]:
        """The literal candidate paths of a word, [] when it names no place the guard can resolve. `literal`: read a
        word with `$`, `%` or `` ` `` as plain text too (`pm-state::$INDEX_ALLOCATION` names the directory).
        `any_program`: the word's program is unknown, so in PowerShell it may be a cmdlet and gets both readings."""
        text = self._text(target)
        dynamic = "$`%" + ("{}" if target.shell == "bash" else "")
        if not text or text.lower() in SINKS or text.startswith("@"):
            return []
        if not literal and any(ch in text for ch in dynamic):
            return []
        if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text):
            return []
        drive = re.match(r"^([A-Za-z][\w]+):", text)
        if target.provider and drive and drive.group(1).lower() in PS_PROVIDER_DRIVES:
            return []  # a provider path of a cmdlet (`Env:X`, `Function:f`), no file
        # Review CX4: any other `name:rest` is a file `name` with the NTFS stream `rest` (only a cmdlet resolves
        # PowerShell drives; a native program opens the text); fs_real folds the stream onto `name`.
        paths = self._candidates(text, target.shell, bases, target.provider or any_program)
        if isinstance(paths, str) and literal and re.match(r"^[A-Za-z]:(?![\\/])", text):
            # `D:x` is `x` in the current directory of drive D: the cwd when the cwd is on D.
            return [b / text[2:] for b in bases if os.path.splitdrive(str(b))[0].lower() == text[:2].lower()]
        return [] if isinstance(paths, str) else paths

    def check_protected(self, target: Target, bases: list[Path]) -> str | None:
        """F06: an argument of a program the guard does not model, judged by the protected rule only: it may not
        resolve into, onto or above a protected directory (the program may write below any path it is given). S42:
        an unmodelled command in PowerShell may be a cmdlet, so on POSIX its words get both readings."""
        for path in self._resolved(target, bases, literal=True, any_program=True):
            reason = self._protected_reason(path, target)
            if reason:
                return f"{target.verb} `{target.word}`: {reason}"
        return None

    def link_regions(self, target: Target, bases: list[Path]) -> list[tuple[str, bool]]:
        """F27: where this write may leave a link, as (normalised real path, links only strictly below it). A copy
        or move leaves one only when its source holds one (a bounded scan; a source it cannot read counts)."""
        if not target.links:
            return []
        if target.links == LINK_COPIES and not self._source_has_link(target, bases):
            return []
        regions = []
        for path in self._resolved(target, bases):
            effective = self._effective(path, target)
            parts = re.split(r"[\\/]", str(effective))
            first = next((k for k, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
            literal = fs_key(os.sep.join(parts[:first]) or os.sep)
            # An extracted member lands strictly below the directory; a link, copy or move may be the path itself.
            regions.append((literal, target.spread and not target.into_dir and first == len(parts)))
        return regions

    def through_link(self, target: Target, bases: list[Path], regions: list[tuple[str, bool]]) -> str | None:
        """F27: a write that passes through a place where an earlier part of the same command may have made a link:
        the guard resolved the path before that link existed."""
        for path in self._resolved(target, bases):
            here = fs_key(path)
            for top, strict in regions:
                below = here.startswith(top.rstrip("\\/") + os.sep)
                if below or (here == top and not strict):
                    return (
                        f"{target.verb} target `{target.word}` passes through {top}, where an earlier part of this "
                        "command may create a link; run the write as a separate command"
                    )
        return None

    def _source_has_link(self, target: Target, bases: list[Path]) -> bool:
        if not target.source or re.search(r"[*?\[$`%{}@]", target.source):
            return True  # a wildcard or run-time source: any of its matches may be a link
        paths = self._resolved(target._replace(word=target.source), bases)
        budget = [MAX_LINK_SCAN]
        return not paths or any(_tree_has_link(p, budget) for p in paths)

    def check(self, target: Target, bases: list[Path]) -> str | None:
        """None when the target is allowed for every possible working directory, else the reason."""
        text = self._text(target)
        low = text.lower()
        if low in SINKS:
            return None
        if not text:
            return f"{target.verb} has an empty target"
        bad = [ch for ch in text if unicodedata.category(ch) in ("Cc", "Cf")]
        if bad:
            return f"{target.verb} target {text!r} holds invisible or control characters ({', '.join(f'U+{ord(c):04X}' for c in bad)})"
        drive = re.match(r"^([A-Za-z][\w]+):", text)
        if target.shell == "ps" and drive and target.provider:
            if drive.group(1).lower() in PS_SINK_DRIVES:
                return None
            return f"{target.verb} target `{text}` is on the PowerShell drive `{drive.group(1)}:`"
        dynamic = "$`%" + ("{}" if target.shell == "bash" else "")
        if any(ch in text for ch in dynamic) or text.startswith("@"):
            return f"{target.verb} target `{text}` is built at run time (variable, substitution or expansion)"
        paths = self._candidates(text, target.shell, bases, target.provider)
        if isinstance(paths, str):
            return f"{target.verb} target `{text}`: {paths}"
        for path in paths:
            # Checked after the `\\?\` prefix is removed: its `?` is no wildcard (review N9).
            glob = bool(re.search(r"[*?\[]", str(path)))
            reason = self._check_path(path, glob, target)
            if reason:
                return f"{target.verb} target `{text}`: {reason}"
        return None

    def _candidates(self, text: str, shell: str, bases: list[Path], provider: bool = False) -> list[Path] | str:
        """Every place the word may name; a caller refuses the write when any one of them is refused."""
        found: list[Path] = []
        for reading in ps_path_readings(text, shell, provider, WINDOWS):
            paths = self._one_reading(reading, shell, bases)
            if isinstance(paths, str):
                return paths
            found.extend(p for p in paths if p not in found)
        return found

    def _one_reading(self, text: str, shell: str, bases: list[Path]) -> list[Path] | str:
        if text == "~" or text.startswith(("~/", "~\\")):
            return [Path.home() / text[2:]] if len(text) > 1 else [Path.home()]
        if text.startswith("~"):
            return "another user's home directory"
        if WINDOWS:
            text = re.sub(r"^(\\\\|//)[?.][\\/]UNC[\\/]", r"\\\\", text, flags=re.I)
            text = re.sub(r"^(\\\\|//)[?.][\\/]", "", text)
            if shell == "bash":
                drive = re.match(r"^/(?:cygdrive/|mnt/)?([A-Za-z])(?=/|$)", text)
                if drive:
                    text = drive.group(1) + ":/" + text[drive.end() :].lstrip("/")
                elif text.startswith("/") and not text.startswith("//"):
                    return "a POSIX absolute path, which Git Bash maps outside the project (Git install or OS temp)"
            if re.match(r"^[A-Za-z]:(?![\\/])", text):
                return "a drive-relative path"
        path = Path(text)
        if path.is_absolute() or (WINDOWS and text.startswith(("\\", "/"))):
            return [path]
        return [base / text for base in bases]

    def _effective(self, path: Path, target: Target) -> Path:
        """The path whose whole subtree the target may write (T-024 round 6, class L): a copy or move into an
        existing directory writes DEST/basename(SOURCE) and, recursively, everything below it; a destination that
        receives contents or unknown names (Target.spread) may get any subtree. Without a known existing directory
        the destination itself is the written path, the strictest reading."""
        if not target.into_dir:
            return path.parent if target.spread else path
        if not target.spread and Path(fs_real(path)).is_dir():
            return path / _basename(target.source)  # a wildcard source keeps its pattern component
        return path

    def _protected_reason(self, path: Path, target: Target) -> str | None:
        """Refuse when the effective written path equals, lies inside or contains a protected directory: copying,
        moving, removing or renaming a parent writes the protected subtree as well."""
        parts = re.split(r"[\\/]", str(self._effective(path, target)))
        first = next((k for k, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
        literal = fs_key(os.sep.join(parts[:first]) or os.sep)
        pattern =[os.path.normcase(p) for p in parts[first:] if p]
        # Creating a directory that already holds a protected one writes nothing below it.
        contain = bool(pattern) or target.verb not in MKDIR_VERBS
        for top in self.protected:
            under = literal == top or literal.startswith(top.rstrip("\\/") + os.sep)
            # A wildcard matches inside one component: it reaches the protected directory only when each pattern
            # component matches that directory's component at that depth (`*/ledger.jsonl`, not `runs/*.log`); a
            # literal path above it contains it.
            rest = Path(os.path.relpath(top, literal)).parts if top.startswith(literal.rstrip("\\/") + os.sep) else ()
            holds = contain and bool(rest) and all(fnmatch.fnmatchcase(r, p) for r, p in zip(rest, pattern))
            if under:
                return f"lies in {top}, the orchestrator's state directory, which workers never write"
            if holds:
                return f"writes {top}, the orchestrator's state directory, through a parent directory; workers never write it"
        # F27: after resolution no component may still be a link (a loop, or one the OS would not resolve): the
        # guard cannot prove where such a write lands.
        stuck = _link_component(literal)
        if stuck:
            return f"passes through the link {stuck}, which the guard cannot resolve"
        return None

    def _check_path(self, path: Path, glob: bool, target: Target) -> str | None:
        if self.protected:
            reason = self._protected_reason(path, target)
            if reason:
                return reason
        if glob:
            # A wildcard may match anything below its literal directory, so that directory is the checked target:
            # it must itself lie strictly inside an allowed directory and hold no tracked file or work tree.
            parts = re.split(r"[\\/]", str(path))
            first = next((k for k, p in enumerate(parts) if re.search(r"[*?\[]", p)), None)
            if first is None:
                return "a wildcard the guard cannot place in the path"
            if any(p == ".." for p in parts[first:]):
                return "a wildcard followed by `..`"
            if first == len(parts) - 1 and self._is_allowed_dir(Path(os.sep.join(parts[:first]))):
                return self._check_matches(path, target)
            path = Path(os.sep.join(parts[:first]) or os.sep)
            target = target._replace(into_dir=False)
        real = Path(fs_real(path))
        if target.into_dir and real.is_dir():
            real = Path(fs_real(real / _basename(target.source)))
        root = os.path.normcase(str(self.root))
        here = os.path.normcase(str(real))
        if here != root and not here.startswith(root.rstrip("\\/") + os.sep):
            return f"resolves to {real}, outside the project root"
        tree = self._tree(real)
        rel = os.path.relpath(real, tree)
        rel_parts = () if rel == "." else Path(rel).parts
        if target.verb in MKDIR_VERBS and self._is_entry(rel_parts):
            return None  # creating an allowlisted directory itself writes nothing else (`-p` may add empty parents)
        if not self._is_inside(rel_parts):
            shown = Path(rel).as_posix() if rel_parts else "."
            return f"`{shown}` (in {tree}) is outside the shell-write allowlist {self.allowed_text}; use the Write or Edit tool"
        if real.is_file():
            try:
                if os.stat(real).st_nlink > 1:
                    return "the file has other hard links (it may be a tracked file's content)"
            except OSError:
                pass
        tracked = self._tracked(tree, real)
        if tracked:
            return tracked
        if real.is_dir():
            worktrees = self._worktree_paths(tree)
            if worktrees is None:
                return "cannot list the Git work trees (git worktree list failed)"
            top = os.path.normcase(str(real))
            for worktree in worktrees:
                wt = os.path.normcase(str(worktree))
                if wt == top or wt.startswith(top.rstrip("\\/") + os.sep):
                    return f"it contains the Git work tree {worktree}"
        return None

    def _is_inside(self, parts: tuple[str, ...]) -> bool:
        """Some entry is a proper component prefix of the work-tree-relative path (a parent or sibling is not)."""
        folded = tuple(os.path.normcase(p) for p in parts)
        return any(len(e) < len(folded) and folded[: len(e)] == e for e in self._entries)

    def _is_entry(self, parts: tuple[str, ...]) -> bool:
        """The path is an allowlisted directory itself (`.tmp`, `.cache/tmp`), whatever other entries overlap it."""
        return tuple(os.path.normcase(p) for p in parts) in self._entries

    def _is_allowed_dir(self, path: Path) -> bool:
        """The path is an allowlisted directory itself of its work tree (not `.tmp/x`)."""
        real = Path(fs_real(path))
        rel = os.path.relpath(real, self._tree(real))
        return self._is_entry(() if rel == "." else Path(rel).parts)

    def _check_matches(self, pattern: Path, target: Target) -> str | None:
        """A wildcard directly in an allowlisted directory (`.tmp/*.log`): every current match must be allowed;
        with no match the literal name lands in that directory."""
        matches = []
        for match in glob.iglob(str(pattern), include_hidden=True):
            matches.append(match)
            if len(matches) > MAX_GLOB_MATCHES:
                return f"the wildcard matches more than {MAX_GLOB_MATCHES} paths; name a narrower directory"
        for match in matches:
            reason = self._check_path(Path(match), False, target._replace(into_dir=False))
            if reason:
                return f"match `{match}`: {reason}"
        return None

    def _tree(self, path: Path) -> Path:
        """The innermost Git work tree (a directory holding `.git`) at or above the path, up to the root."""
        current = path
        while True:
            if (current / ".git").exists():
                return current
            if current == self.root or current.parent == current:
                return self.root
            current = current.parent

    def _git(self, tree: Path, *args: str) -> tuple[int, bytes]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(tree), *args],
                capture_output=True,
                timeout=self.git_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, type(exc).__name__.encode()
        return proc.returncode, proc.stdout

    def _tracked(self, tree: Path, path: Path) -> str | None:
        if self._tracked_checks >= MAX_TRACKED_CHECKS:
            return (
                f"the command needs more than {MAX_TRACKED_CHECKS} tracked-file checks (one per candidate path, "
                "for every working directory and variable value); split it into smaller commands"
            )
        self._tracked_checks += 1
        rel = Path(os.path.relpath(path, tree)).as_posix()
        magic = "literal,icase" if WINDOWS else "literal"
        code, out = self._git(tree, "ls-files", "-z", "--", f":({magic}){rel}")
        if code != 0:
            return f"cannot check whether it is tracked (git ls-files failed: {out[:120]!r})"
        if out.strip(b"\0"):
            first = out.split(b"\0")[0].decode("utf-8", "replace")
            return f"`{first}` is tracked; change tracked files with the Edit or Write tool"
        return None

    def _worktree_paths(self, tree: Path) -> list[Path] | None:
        if tree not in self._worktrees:
            code, out = self._git(tree, "worktree", "list", "--porcelain", "-z")
            if code != 0:
                self._worktrees[tree] = None
            else:
                self._worktrees[tree] = [
                    Path(fs_real(line[9:].decode("utf-8", "replace")))
                    for line in out.split(b"\0")
                    if line.startswith(b"worktree ")
                ]
        return self._worktrees[tree]


def _basename(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return parts[-1] if parts else "x"


# Safety-bound (S42 F6): candidate working directories of one command. Each `cd` keeps every earlier base (no
# scoping), so the list doubles per `cd`, and triples on POSIX where a PowerShell `cd x\y` has two readings; every
# write target is then checked against every base. Past the bound the working directory counts as unplaceable,
# which every caller refuses, so the hook stays far inside its timeout instead of building 3^k paths.
MAX_CWD_BASES = 64
CWD_UNPLACED = f"a `cd` to a run-time directory, or more than {MAX_CWD_BASES} candidate working directories"


def cwd_candidates(cwd: Path, changes: list[tuple[str, str]]) -> list[Path] | None:
    """Every working directory a relative target may resolve against; None when a `cd` is dynamic or the candidates
    pass MAX_CWD_BASES (CWD_UNPLACED; callers refuse). The parser does not keep `&&`/`;`/subshell scoping, so each
    `cd` adds a candidate instead of replacing one."""
    bases = dict.fromkeys([cwd])  # an ordered set: O(1) dedupe, first-seen order kept
    for change, shell in changes:
        if any(ch in change for ch in "$`%") or change in ("-", "+"):
            return None
        new: list[Path] = []
        # S42 F1: PowerShell's `cd`/`Set-Location`/`Push-Location` are provider cmdlets, so on a POSIX host the
        # directory has both readings of a cmdlet target; every one is a base, so a write is refused when either is.
        for reading in ps_path_readings(change, shell, True, WINDOWS):
            if reading == "~" or reading.startswith(("~/", "~\\")):
                new.append(Path.home() / reading[2:])
            elif Path(reading).is_absolute():
                new.append(Path(reading))
            else:
                new += [b / reading for b in bases]
        for n in new:
            bases.setdefault(Path(fs_real(n)))
        if len(bases) > MAX_CWD_BASES:
            return None  # fail closed, never a partial list
    return list(bases)
