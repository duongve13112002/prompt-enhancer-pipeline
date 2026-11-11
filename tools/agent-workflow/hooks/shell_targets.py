"""Shell command analysis for the guard hooks: every command a Bash, PowerShell or Monitor call runs, and what it writes.

`walk()` reuses the hardened process_guard parser and follows the same nesting (`bash -c`, `powershell -Command`,
encoded commands, `eval`/`iex`, substitutions, heredocs and pipes read by a shell, `[scriptblock]::Create`), plus the
inner command of a configured launcher (`test_slot.py -- <cmd>`) and of `find -exec`. `write_targets()` lists the paths
a command writes, moves or deletes, and the places where it cannot tell (fail closed). `PathPolicy` decides whether a
target is inside the configured shell-write allowlist. Standard library only (the hooks run with `python -B -I`).

Interpreter code (`python -c "open(p, 'w')"`) and scripts run by path are not inspected, like process_guard.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import unicodedata
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

    def text(self, text: str, shell: str, depth: int, executed: bool) -> bool:
        if depth > MAX_DEPTH:
            self.deep = True
            return True
        parsed = pg._parse(text, shell, executed)
        self.state.texts.append((text, parsed.masked, shell))
        if shell == "ps":
            for script in pg._embedded_scripts(text):
                self.text(script, "ps", depth + 1, True)
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
            )
            scripts, own_code = pg._stream_scripts(segs, idx)
            inner = [(script, kind or shell) for script, kind in scripts]
            if not own_code:
                inner += [
                    (sub, shell)
                    for h in seg.heredocs
                    if h.expand
                    for sub in pg._substitutions(h.body, "bash")
                ]
            inner += [(sub, shell) for sub in seg.substitutions]
            for script, script_shell in inner:
                self.text(script, script_shell, depth + 1, False)
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
        if head is None:
            return
        word = words[head]
        # Bash `eval` joins all its words and parses them again: `eval echo x '>' f` writes f.
        evaluated = shell == "bash" and "eval" in wrappers and len(words) > head + 1
        if evaluated or (wrappers and word.quoted and re.search(r"\s", word)):
            # `iex "Set-Content ..."`, `eval "echo > f"`
            text = " ".join(str(w) for w in words[head:]) if evaluated else str(word)
            self.text(text, shell, depth + 1, True)
            return
        name = cmd.name
        args = [str(a) for a in cmd.args]
        if name in pg.SHELLS:
            inner_shell = pg.SHELLS[name]
            encoded = pg._decode_encoded(args, inner_shell == "ps")
            if encoded:
                self.text(encoded, "ps", depth + 1, True)
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
                    self.text(inner, inner_shell, depth + 1, True)
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


class Writes(NamedTuple):
    targets: list[Target]
    problems: list[str]  # writes whose targets cannot be determined (fail closed)
    cwd_changes: list[str]  # `cd` arguments (relative targets resolve against every one)


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
    if canonical == "new-item":
        base = path_values or [Word(".", False, False)]
        names = named.get("name", [])
        path_values = [
            Word(_join(str(b), str(n)), b.quoted, False) for b in base for n in names
        ] or base
        item_type = " ".join(str(v) for v in named.get("itemtype", [])).lower()
        if item_type not in ("", "file", "directory"):
            # A link's target must be inside the allowlist too.
            path_values += named.get("value", [])
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
            writes.targets.append(Target(part, verb, cmd.shell))
    for value in dests:
        for part in _split_list(str(value), value.literal):
            if sources:
                for src in sources:
                    for src_part in _split_list(str(src), src.literal):
                        writes.targets.append(Target(part, canonical, cmd.shell, True, src_part))
            else:
                writes.targets.append(Target(part, canonical, cmd.shell, True, "x"))


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
                writes.targets.append(Target(value, verb, cmd.shell))
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


def _opt_value(args: list[Word], names: tuple[str, ...]) -> list[str]:
    """Values of `-o X`, `-oX`, `--output X`, `--output=X` and of a short cluster ending in the letter (`-sSo X`)."""
    out: list[str] = []
    for k, w in enumerate(args):
        a = str(w)
        if w.literal or not a.startswith("-"):
            continue
        nxt = str(args[k + 1]) if k + 1 < len(args) else ""
        for name in names:
            if name.startswith("--"):
                if a == name:
                    out.append(nxt)
                elif a.startswith(name + "="):
                    out.append(a[len(name) + 1 :])
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
    if shell == "cmd" and name in CMD_BUILTINS:
        if name in ("copy", "move"):
            if len(pos) >= 2:
                if name == "move":
                    add(pos[:-1])
                for src in pos[:-1]:
                    add([pos[-1]], into_dir=True, source=str(src))
            else:
                add(["."], into_dir=True, source=str(pos[0]) if pos else "x")
        elif name in ("ren", "rename") and len(pos) == 2:
            add([pos[0], _join(_dirname(str(pos[0])), str(pos[1]))])
        else:
            add(pos)
        if not pos:
            writes.problems.append(f"`{name}` has no literal path")
        return
    if name in POSIX_ALL:
        add(pos)
    elif name in ("chmod", "chown", "chgrp"):
        mode_as_option = any(re.fullmatch(r"-[rwxXst]+", str(a)) for a in args)
        reference = any(str(a).startswith("--reference") for a in args)
        add(pos if (mode_as_option or reference) else pos[1:])
    elif name == "tee":
        add(pos)
    elif name in ("cp", "mv", "install", "ln"):
        target_dir = _opt_value(args, ("--target-directory", "-t"))
        values = {"-S", "--suffix", "-m", "-o", "-g", "--mode", "--owner", "--group"}
        pos = _positionals(args, shell, values | {"-t"})
        if name == "ln":
            add(pos + target_dir)  # link names and targets both
        elif name == "install" and any(str(a) in ("-d", "--directory") for a in args):
            add(pos)
        elif target_dir:
            for d in target_dir:
                for src in pos:
                    add([d], into_dir=True, source=str(src))
        elif len(pos) >= 2:
            for src in pos[:-1]:
                add([pos[-1]], into_dir=True, source=str(src))
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
            add([_join(d, "remote-name") for d in out_dir])
    elif name == "wget":
        documents = _opt_value(args, ("-O", "--output-document"))
        add([d for d in documents if d != "-"])
        add(_opt_value(args, ("-o", "--output-file", "-a", "--append-output")))
        if not documents and "--spider" not in [str(a) for a in args]:
            prefix = _opt_value(args, ("-P", "--directory-prefix")) or ["."]
            add([_join(p, "remote-name") for p in prefix])
    elif name in ("tar", "bsdtar"):
        _tar_targets(cmd, writes)
    elif name == "unzip":
        if not any(str(a) in ("-l", "-v", "-t", "-p", "-Z", "-z") for a in args):
            dest = _opt_value(args, ("-d",)) or ["."]
            add([_join(d, "archive-member") for d in dest])
    elif name in ("7z", "7za", "7zr"):
        if pos and str(pos[0]).lower() in ("x", "e"):
            dirs = [str(a)[2:] for a in args if str(a).startswith("-o")] or ["."]
            add([_join(d, "archive-member") for d in dirs])
        elif pos and str(pos[0]).lower() in ("a", "u", "d", "rn") and len(pos) > 1:
            add([pos[1]])
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
                add([pos[-1]], into_dir=True, source=str(src))
        if "--remove-source-files" in [str(a) for a in args]:
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
            add([pos[1]])
            if any(str(a).lower() in ("/mov", "/move") for a in args):
                add([pos[0]])
    elif name == "xcopy":
        add([pos[1]] if len(pos) >= 2 else ["."])
    elif name == "mklink":
        add(pos)


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
            # getopt_long accepts unique prefixes (`--expr=...`, `--in-pl`).
            if len(long_name) >= 3 and "--in-place".startswith(long_name):
                in_place = True
            elif len(long_name) >= 4 and "--expression".startswith(long_name):
                scripts.append(a.split("=", 1)[1] if "=" in a else nxt)
                k += 0 if "=" in a else 1
            elif len(long_name) >= 4 and "--file".startswith(long_name):
                from_file = True
                k += 0 if "=" in a else 1
            elif "=" not in a and long_name in ("--line-length",):
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


def _tar_targets(cmd: Cmd, writes: Writes) -> None:
    args = [str(a) for a in cmd.args]
    letters = ""
    if args and not args[0].startswith("-"):
        letters = args[0]  # old style: `tar xzf a.tgz`
    letters += "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    longs = {a.split("=")[0] for a in args if a.startswith("--")}
    extract = "x" in letters or longs & {"--extract", "--get"}
    create = any(c in letters for c in "cruA") or longs & {
        "--create",
        "--append",
        "--update",
        "--concatenate",
        "--catenate",
        "--delete",
    }
    if extract:
        dirs = _opt_value(cmd.args, ("-C", "--directory"))
        writes.targets.extend(
            Target(_join(d, "archive-member"), "tar -x", cmd.shell) for d in (dirs or ["."])
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


def write_targets(walked: Walk) -> Writes:
    writes = Writes([], [], [])
    for cmd in walked.cmds:
        # `[[ a > b ]]` compares strings; in bash `[ a > b ]` and `test a > b` redirect like any command.
        in_test = cmd.name == "[["
        for r in cmd.redirects:
            if ">" in r.op and not in_test:  # `>`, `>>`, `>|`, `&>`, `>&`, `<>`
                if r.target is None:
                    continue
                writes.targets.append(Target(str(r.target), "redirection", cmd.shell))
        for word in cmd.extra_writes:
            writes.targets.append(Target(str(word), "Start-Process redirection", cmd.shell))
        if cmd.head is None:
            continue
        if cmd.name in CD_COMMANDS:
            pos = [a for a in cmd.args if not _is_option(str(a), cmd.shell)]
            writes.cwd_changes.append(str(pos[0]) if pos else "~")
            continue
        before = len(writes.targets) + len(writes.problems)
        resolved = resolved_cmds(cmd, walked)
        for one in [cmd] if resolved is None else resolved:
            _cmd_targets(one, writes)
        wrote = len(writes.targets) + len(writes.problems) > before
        stdin_args = STDIN_ARG_WRAPPERS.intersection(cmd.wrappers)
        if stdin_args and (wrote or WRITE_TRIGGER.search(trigger_text(" ".join(cmd.words)))):
            writes.problems.append(
                f"`{min(stdin_args)} {cmd.name}` takes its targets from stdin or `:::`"
            )
    for raw, masked, shell in walked.texts:
        if shell == "ps":
            _dotnet_targets(raw, masked, writes)
    writes.targets[:] = [t2 for t in writes.targets for t2 in _expand_target(t, walked)]
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
    r"|SetAccessControl|Save|WriteAllText|WriteAllBytes|WriteAllLines|AppendAllText|AppendAllLines)\s*\(",
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
        _literal_args(args, positions, f"[IO.{match['cls']}]::{match['meth']}", writes)
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


class PathPolicy:
    """Decide whether shell-written paths stay inside the allowlist (relative to the innermost Git work tree)."""

    def __init__(self, root: Path, allowed_dirs: list[str], git_timeout: int) -> None:
        self.root = Path(os.path.realpath(root))
        # An entry is a path prefix (`.cache/tmp`): compared component by component, never as a string prefix.
        self._entries = [tuple(os.path.normcase(p) for p in d.split("/")) for d in allowed_dirs]
        self.allowed_text = ", ".join(f"`{d}/`" for d in allowed_dirs)
        self.git_timeout = git_timeout
        self._worktrees: dict[Path, list[Path] | None] = {}

    def check(self, target: Target, bases: list[Path]) -> str | None:
        """None when the target is allowed for every possible working directory, else the reason."""
        text = target.word
        if target.shell == "ps":
            text = re.sub(r"(?i)^(?:Microsoft\.PowerShell\.Core\\)?FileSystem::", "", text)
        low = text.lower()
        if low in SINKS:
            return None
        if not text:
            return f"{target.verb} has an empty target"
        bad = [ch for ch in text if unicodedata.category(ch) in ("Cc", "Cf")]
        if bad:
            return f"{target.verb} target {text!r} holds invisible or control characters ({', '.join(f'U+{ord(c):04X}' for c in bad)})"
        drive = re.match(r"^([A-Za-z][\w]+):", text)
        if target.shell == "ps" and drive:
            if drive.group(1).lower() in PS_SINK_DRIVES:
                return None
            return f"{target.verb} target `{text}` is on the PowerShell drive `{drive.group(1)}:`"
        dynamic = "$`%" + ("{}" if target.shell == "bash" else "")
        if any(ch in text for ch in dynamic) or text.startswith("@"):
            return f"{target.verb} target `{text}` is built at run time (variable, substitution or expansion)"
        paths = self._candidates(text, target.shell, bases)
        if isinstance(paths, str):
            return f"{target.verb} target `{text}`: {paths}"
        for path in paths:
            # Checked after the `\\?\` prefix is removed: its `?` is no wildcard (review N9).
            glob = bool(re.search(r"[*?\[]", str(path)))
            reason = self._check_path(path, glob, target)
            if reason:
                return f"{target.verb} target `{text}`: {reason}"
        return None

    def _candidates(self, text: str, shell: str, bases: list[Path]) -> list[Path] | str:
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

    def _check_path(self, path: Path, glob: bool, target: Target) -> str | None:
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
        real = Path(os.path.realpath(path))
        if target.into_dir and real.is_dir():
            real = Path(os.path.realpath(real / _basename(target.source)))
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
        real = Path(os.path.realpath(path))
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
                    Path(os.path.realpath(line[9:].decode("utf-8", "replace")))
                    for line in out.split(b"\0")
                    if line.startswith(b"worktree ")
                ]
        return self._worktrees[tree]


def _basename(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return parts[-1] if parts else "x"


def cwd_candidates(cwd: Path, changes: list[str], shell: str) -> list[Path] | None:
    """Every working directory a relative target may resolve against; None when a `cd` is dynamic.
    The parser does not keep `&&`/`;`/subshell scoping, so each `cd` adds a candidate instead of replacing one."""
    bases = [cwd]
    for change in changes:
        if any(ch in change for ch in "$`%") or change in ("-", "+"):
            return None
        if change == "~" or change.startswith(("~/", "~\\")):
            new = [Path.home() / change[2:]]
        elif Path(change).is_absolute():
            new = [Path(change)]
        else:
            new = [b / change for b in bases]
        bases += [Path(os.path.realpath(n)) for n in new]
    return bases
