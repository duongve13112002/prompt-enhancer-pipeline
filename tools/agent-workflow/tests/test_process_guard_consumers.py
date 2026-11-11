"""Attempt 2 tests for hooks/process_guard.py: who reads a heredoc or piped text, assignment prefixes, parse failure.

Invariant: text (a heredoc or here-string body, a piped string) may be skipped only when its consumer is a known data
command (or an interpreter, whose language the guard never inspects); every other consumer gets the text as code.
"""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load("process_guard_a2", TESTS.parent / "hooks" / "process_guard.py")
base = _load("test_process_guard_a1", TESTS / "test_process_guard.py")

B, P = "Bash", "PowerShell"


def blocked(cmd: str, tool: str) -> bool:
    return guard.classify(cmd, tool) is not None


# F-01 was not reproduced: a line continuation joins the words before the parser splits segments.
@pytest.mark.parametrize(
    ("cmd", "tool"),
    [
        ("pkill \\\npython", B),
        ("pkill \\\r\npython", B),
        ("kill \\\n$(pgrep python)", B),
        ("taskkill \\\n/IM python.exe", B),
        ("Stop-Process `\n-Name python", P),
        ("Stop-Process `\r\n-Name python", P),
        ("Get-Process python | `\nStop-Process", P),
        ("Get-Process python | `\r\nStop-Process -Force", P),
        ("pgrep python | \\\nxargs kill", B),
        ("pk\\\nill python", B),
        ("kill \\\n$p", B),
        ("kill \\\r\n-9 $p", B),
    ],
)
def test_line_continuations_still_block(cmd: str, tool: str) -> None:
    assert blocked(cmd, tool), cmd


@pytest.mark.parametrize(
    ("cmd", "tool"),
    [
        ("kill \\\n123", B),
        ("kill -9 \\\r\n123", B),
        ("Stop-Process `\n-Id 12", P),
        ("taskkill `\n/PID 12", P),
    ],
)
def test_line_continuations_do_not_block_numeric_targets(cmd: str, tool: str) -> None:
    assert not blocked(cmd, tool), (cmd, guard.classify(cmd, tool))


# F-02: a string or here-string piped into a PowerShell evaluator is code.
EVALUATOR_BLOCKED = [
    "@'\nStop-Process -Name python\n'@ | iex",
    '@"\nStop-Process -Name python\n"@ | Invoke-Expression',
    '"Stop-Process -Name python" | Invoke-Expression',
    "'Stop-Process -Name python' | iex",
    "'Stop-Process -Name python' | Out-String | iex",
    "Write-Output 'Stop-Process -Name python' | iex",
    "echo 'taskkill /IM python.exe' | iex",
    "'Stop-Process -Name python' | powershell -NoProfile -Command -",
    "'Stop-Process -Name python' | pwsh -",
    "'Stop-Process -Name python' | cmd /c",
    "& ([scriptblock]::Create('Stop-Process -Name python'))",
    '& ([scriptblock]::Create("Stop-Process -Name python"))',
    "[scriptblock]::Create(@'\nStop-Process -Name python\n'@).Invoke()",
    "$ExecutionContext.InvokeCommand.InvokeScript('Stop-Process -Name python')",
    "@'\nStop-Process -Name python\n'@ | unknowncli",
]


@pytest.mark.parametrize("cmd", EVALUATOR_BLOCKED)
def test_text_reaching_an_evaluator_is_code(cmd: str) -> None:
    assert blocked(cmd, P), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "'Stop-Process -Id 12' | iex",
        "'hello' | iex",
        "[scriptblock]::Create('Get-Date').Invoke()",
        "'text with Stop-Process -Name' | Set-Content f.txt",
        "@'\nStop-Process -Name p\n'@ | Set-Content f.ps1",
        "@'\nStop-Process -Name p\n'@ | Out-File f.ps1",
        "Write-Output 'Stop-Process -Name p' | Out-File f.txt",
    ],
)
def test_text_for_data_cmdlets_and_numeric_targets_is_allowed(cmd: str) -> None:
    assert not blocked(cmd, P), (cmd, guard.classify(cmd, P))


# F-03: the command after a variable assignment is a command position.
ASSIGNMENT_PREFIXES = [
    "$x=",
    "$x = ",
    "[int]$x=",
    "$env:X=",
    "$script:x=",
    "$x+=",
    "${x}=",
    "$h.Name=",
    "$null = ",
]
ASSIGNED_BLOCKED = [
    "Stop-Process -Name python",
    "taskkill /IM python.exe",
    "& taskkill /IM python.exe",
    "iex 'Stop-Process -Name python'",
    "Invoke-Expression 'Stop-Process -Name python'",
    "Get-Process python | Stop-Process",
]


@pytest.mark.parametrize("command", ASSIGNED_BLOCKED)
@pytest.mark.parametrize("prefix", ASSIGNMENT_PREFIXES)
def test_command_after_an_assignment_prefix_is_classified(prefix: str, command: str) -> None:
    assert blocked(prefix + command, P), prefix + command
    assert blocked(prefix + prefix + command, P), prefix + prefix + command


@pytest.mark.parametrize("prefix", ASSIGNMENT_PREFIXES)
def test_assigned_literals_and_numeric_targets_stay_allowed(prefix: str) -> None:
    assert not blocked(prefix + "Stop-Process -Id 12", P)
    assert not blocked(prefix + "'$p.Kill()'", P)
    assert not blocked(prefix + "@'\n$p.Kill()\n'@\nGet-Date", P)
    assert not blocked(prefix + "Get-Date", P)


def test_assigned_here_string_piped_into_an_evaluator_blocks() -> None:
    assert blocked("$x=@'\nStop-Process -Name python\n'@ | iex", P)
    assert blocked("[string]$x = 'a'; $y=@'\nStop-Process -Name p\n'@ | Invoke-Expression", P)


# F-04: a heredoc or here-string is code for every consumer except a known data command or an interpreter.
CODE_CONSUMERS = [
    "source /dev/stdin",
    ". /dev/stdin",
    "eval",
    "bash",
    "sh",
    "zsh",
    "dash",
    "ksh",
    "fish",
    "busybox sh",
    "bash -s --",
    "exec bash",
    "env X=1 bash",
    "nohup sh",
    "sudo -u root bash",
    "xargs",
    "awk -f -",
    "ssh host",
    "unknowncli",
    "powershell -Command -",
    "pwsh -",
    "pwsh -Command - ",
    "sed -e e",
    "sed s/x/y/e",
    "sed -n w/dev/stdout",
]


@pytest.mark.parametrize("consumer", CODE_CONSUMERS)
@pytest.mark.parametrize("delim", ["'EOF'", "EOF", '"EOF"'])
def test_heredoc_for_a_code_consumer_is_parsed(consumer: str, delim: str) -> None:
    body = (
        "Stop-Process -Name python"
        if "pwsh" in consumer or "powershell" in consumer
        else "pkill python"
    )
    assert blocked(f"{consumer} <<{delim}\n{body}\nEOF", B), consumer


@pytest.mark.parametrize("consumer", ["unknowncli", "xargs", "source /dev/stdin", "eval"])
def test_here_string_for_a_code_consumer_is_parsed(consumer: str) -> None:
    assert blocked(f"{consumer} <<< 'pkill python'", B)
    assert blocked(f"cat <<'EOF' | {consumer}\npkill python\nEOF", B)


DATA_OR_INTERPRETER_CONSUMERS = [
    "cat",
    "cat > f",
    "tee out.txt",
    "sed s/a/b/",
    "sed -n p",
    ": ",
    "true",
    "head -n 2",
    "wc -l",
    "grep sh",
    "git commit -F -",
    "gh pr create --body-file -",
    "python -",
    "python3",
    "python3.12 -",
    "node -",
    "perl",
    "ruby",
]


@pytest.mark.parametrize("consumer", DATA_OR_INTERPRETER_CONSUMERS)
def test_heredoc_for_a_data_command_or_interpreter_is_skipped(consumer: str) -> None:
    assert not blocked(f"{consumer} <<'EOF'\npkill python\n$p.Kill()\nEOF", B), consumer
    assert not blocked(f"{consumer} <<< 'pkill python'", B), consumer
    assert not blocked(f"cat <<'EOF' | {consumer}\npkill python\nEOF", B), consumer


def test_a_pipeline_is_code_when_any_reader_is() -> None:
    assert not blocked("cat <<'EOF' | tee x | wc -l\npkill p\nEOF", B)
    assert blocked("cat <<'EOF' | tee x | sh\npkill p\nEOF", B)
    assert blocked("cat <<'EOF' | tee x | unknowncli\npkill p\nEOF", B)


def test_echo_arguments_for_a_data_consumer_stay_data() -> None:
    assert not blocked("echo 'pkill note' | gh issue create --body-file -", B)
    assert blocked("echo 'pkill p' | source /dev/stdin", B)
    assert blocked("echo 'pkill p' | .", B)
    assert blocked("echo pkill p | sh", B)


# F-05: a parse failure fails closed for every verb the guard can block, not only a subset.
EVERY_BLOCKED = [(c, B) for c in base.BLOCKED_BASH] + [(c, P) for c in base.BLOCKED_PS]


@pytest.mark.parametrize(("cmd", "tool"), EVERY_BLOCKED)
def test_parse_failure_blocks_every_blocked_example(
    cmd: str, tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(guard, "_parse", boom)
    assert guard.classify(cmd, tool) is not None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "kill python",
        "kill -9 $PID",
        "(Get-Process p).Kill()",
        '$p."Kill"()',
        "$x.Terminate()",
        "Invoke-CimMethod $x -MethodName Terminate",
        "Invoke-WmiMethod -Path $x terminate",
        "iwmi -Class Win32_Process -Name Terminate",
        "Remove-CimInstance $x",
        "Remove-WmiObject $x",
        "Get-Process | % Kill",
        "killall5",
        "tskill python",
        "pskill python",
        "spps -Name p",
        "wmic process delete",
        "taskkill.exe /IM p",
    ],
)
def test_loose_verb_covers_every_guarded_verb_spelling(cmd: str) -> None:
    assert guard.LOOSE_VERB.search(cmd), cmd


def test_loose_verb_ignores_plain_commands() -> None:
    for cmd in [
        "ls -la",
        "git status",
        "python -m pytest",
        "Get-Process -Id 1",
        "echo hello",
        "skillet --run",
    ]:
        assert not guard.LOOSE_VERB.search(cmd), cmd


def test_every_command_verb_the_classifier_knows_is_in_the_loose_pattern() -> None:
    names = guard.NAME_KILLERS | guard.PS_KILL | guard.NUMERIC_ONLY | {"kill", "taskkill", "wmic"}
    for name in names:
        assert guard.LOOSE_VERB.search(f"echo hi; {name} x"), name


def test_nesting_beyond_the_depth_limit_fails_closed_on_a_guarded_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep, safe = "pkill python", "echo hello"
    for _ in range(guard.MAX_DEPTH + 3):
        deep, safe = "bash -c " + shlex.quote(deep), "bash -c " + shlex.quote(safe)
    assert blocked(deep, B)
    assert not blocked(safe, B)


try:
    from hypothesis import assume, given, settings
    from hypothesis import strategies as st
except ImportError:  # hypothesis is optional for this tooling suite
    st = None

if st is not None:
    allowlisted = (
        guard.DATA_CONSUMERS
        | guard.DATA_HEADS
        | guard.INTERPRETER_NAMES
        | guard.EVALUATORS
        | guard.STDIN_EXECUTORS
        | set(guard.SHELLS)
        | guard.WRAPPERS
    )
    consumer_names = st.from_regex(r"[a-z][a-z0-9_-]{1,10}", fullmatch=True).filter(
        lambda n: n not in allowlisted and not guard.DATA_HEAD_PATTERN.fullmatch(n)
    )
    safe_line = st.text(alphabet=st.sampled_from(list("abc xyz 012-_.,:=+/@%^~")), max_size=20)

    @settings(max_examples=300, derandomize=True, deadline=None)
    @given(
        name=consumer_names,
        before=st.lists(safe_line, max_size=3),
        after=st.lists(safe_line, max_size=3),
        verb=st.sampled_from(
            ["pkill python", "kill python", "taskkill /IM python.exe", "killall x"]
        ),
        delim=st.sampled_from(["EOF", "'EOF'", '"EOF"', "\\EOF"]),
    )
    def test_property_non_allowlisted_consumer_gets_the_heredoc_as_code(
        name: str, before: list[str], after: list[str], verb: str, delim: str
    ) -> None:
        assume(all(line.strip() != "EOF" for line in [*before, *after]))
        body = "\n".join([*before, verb, *after])
        cmd = f"{name} <<{delim}\n{body}\nEOF"
        assert blocked(cmd, B), cmd
        assert blocked(f"{name} <<< {shlex.quote(verb)}", B), name
        assert blocked(f"cat <<{delim} | {name}\n{body}\nEOF", B), name

    @settings(max_examples=200, derandomize=True, deadline=None)
    @given(name=consumer_names, quote=st.sampled_from(["'", '"']))
    def test_property_non_allowlisted_consumer_gets_a_piped_literal_as_code(
        name: str, quote: str
    ) -> None:
        assert blocked(f"{quote}Stop-Process -Name python{quote} | {name}", P), name
        assert blocked(f"@'\nStop-Process -Name python\n'@ | {name}", P), name

    @settings(max_examples=200, derandomize=True, deadline=None)
    @given(
        name=st.sampled_from(sorted(guard.DATA_CONSUMERS - {"sed", "read", ":"})),
        verb=st.sampled_from(["pkill python", "kill python", "taskkill /IM python.exe"]),
    )
    def test_property_data_consumers_skip_the_heredoc(name: str, verb: str) -> None:
        assert not blocked(f"{name} <<'EOF'\n{verb}\nEOF", B), name

    @settings(max_examples=200, derandomize=True, deadline=None)
    @given(
        prefix=st.sampled_from(ASSIGNMENT_PREFIXES),
        command=st.sampled_from(ASSIGNED_BLOCKED),
        glue=st.sampled_from(["", " ", "  "]),
    )
    def test_property_assignment_prefix_never_hides_a_command(
        prefix: str, command: str, glue: str
    ) -> None:
        assert blocked(prefix.rstrip() + glue + command, P), prefix + command
