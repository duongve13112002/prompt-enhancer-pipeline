"""Unit tests for hooks/process_guard.py (classifier and hook entry point)."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "process_guard.py"
spec = importlib.util.spec_from_file_location("process_guard", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def blocked(cmd: str, tool: str = "Bash") -> bool:
    return guard.classify(cmd, tool) is not None


BLOCKED_BASH = [
    "taskkill /IM python.exe",
    "taskkill /F /IM python.exe",
    "TASKKILL /f /im PYTHON.EXE",
    "taskkill.exe /F /IM python.exe /T",
    "taskkill.com /IM python.exe",  # any PATHEXT suffix names the same program
    "TASKKILL.CMD /IM python.exe",
    # Every launcher argument is inner text (review N1/N7): aliases, unknown options, `/` prefixes, more shells.
    'powershell -ep Bypass -Command "Stop-Process -Name python"',
    'pwsh -x y -c "Stop-Process -Name python"',
    'powershell /c "Stop-Process -Name python"',
    'pwsh -Command:"Stop-Process -Name python"',
    'bash -c -- "pkill python"',
    'ksh -c "pkill python"',
    'busybox sh -c "pkill python"',
    "sudo -u root pkill python",  # a wrapper option's value is not the command
    "env -u X pkill python",
    "echo `echo \\`pkill python\\``",  # nested escaped backticks run (review N4)
    "taskkill //F //IM python.exe",
    "taskkill",
    "taskkill /F",
    'taskkill /F /FI "IMAGENAME eq python.exe"',
    'taskkill /PID 123 /FI "STATUS eq running"',
    "taskkill /PID $p",
    "taskkill /PID abc",
    "taskkill /S host /PID 12",
    "pkill python",
    "pkill -f cli_worker",
    "pkill -P 123",
    "killall python",
    "/usr/bin/killall -9 python",
    "skill -KILL -u bob",
    "kill $(pgrep python)",
    "kill `pgrep python`",
    "kill -9 $PID",
    "kill %1",
    "kill python",
    "kill -9 -123",
    "kill -s KILL -123",
    "pgrep python | xargs kill",
    "pgrep python | xargs -r kill -9",
    "ps aux | grep python | awk '{print $2}' | xargs kill -9",
    "sudo pkill python",
    "sudo -n killall python",
    "timeout 5 pkill python",
    "echo hi; pkill python",
    "echo hi && taskkill /IM python.exe",
    "false || taskkill /IM python.exe",
    "if true; then pkill python; fi",
    "FOO=bar pkill python",
    'powershell -Command "Stop-Process -Name python"',
    "powershell.exe -NoProfile -Command 'Get-Process python | Stop-Process -Force'",
    'pwsh -c "Get-CimInstance Win32_Process | Invoke-CimMethod -MethodName Terminate"',
    "cmd /c taskkill /IM python.exe",
    'cmd.exe /c "taskkill /F /IM python.exe & echo done"',
    "bash -c 'pkill python'",
    'sh -c "kill $(pgrep x)"',
    "wmic process where \"name='python.exe'\" delete",
    "wmic process where name='python.exe' call terminate",
    "wmic process delete",
    "pskill python",
    "tskill python",
    "pkill python 2>/dev/null",
    "taskkill /IM python.exe > nul 2>&1",
    "(Get-Process python).Kill()",
    "$p = Get-Process python; $p.Kill()",
    "Get-Process python | ForEach-Object { $_.Kill() }",
    "Get-Process python | % Kill",
    "Get-CimInstance Win32_Process | Where-Object CommandLine -like '*python*' | Remove-CimInstance",
    "Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | ForEach-Object { $_.Terminate() }",
]

BLOCKED_PS = [
    "Stop-Process -Name python",
    "Stop-Process -Name python -Force",
    "stop-process -name python",
    "STOP-PROCESS -NAME PYTHON",
    "Stop-Process -ProcessName python",
    "Stop-Process -Na python",
    "Stop-Process python",
    "Stop-Process -Id $pid",
    "Stop-Process -Id (Get-Process python).Id",
    "Stop-Process -Id $proc.Id",
    "Stop-Process -Id abc",
    "Stop-Process -InputObject $p",
    "Get-Process python | Stop-Process",
    "Get-Process python | Stop-Process -Force",
    "Get-Process -Name python* | Stop-Process -Force -ErrorAction SilentlyContinue",
    "Get-Process -Id 123 | Stop-Process",
    "gps python | spps",
    "spps -Name python",
    "spps python",
    "kill -Name python",
    "kill $p",
    "Get-Process python | kill",
    "Get-Process python | ForEach-Object { Stop-Process -Id $_.Id }",
    "Get-Process python | % { spps -Id $_.Id -Force }",
    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'python' } | ForEach-Object { Stop-Process -Id $_.ProcessId }",
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Invoke-CimMethod -MethodName Terminate",
    "Get-WmiObject Win32_Process | ForEach-Object { $_.Terminate() }",
    "Get-CimInstance Win32_Process | Remove-CimInstance",
    "& taskkill /IM python.exe",
    "& 'C:\\Windows\\System32\\taskkill.exe' /F /IM python.exe",
    "C:\\Windows\\System32\\taskkill.exe /IM python.exe",
    "Start-Process taskkill -ArgumentList '/IM python.exe'",
    'Invoke-Expression "Stop-Process -Name python"',
    "iex 'taskkill /IM python.exe'",
    "Stop-Process -Id 123 -Name python",
    "Stop-Process -Id 123 -Force:$x",
    "Stop-Process -Id 123 -ErrorAction $x",
    "Stop-Process -Id 123 | Out-Null; Stop-Process -Name python",
    "$ErrorActionPreference='SilentlyContinue'; taskkill /F /IM python.exe",
    "Get-Process python *>$null | Stop-Process",
    "Get-Process | Where-Object { $_.Name -eq 'python' } | Stop-Process -Force",
]

ALLOWED_BASH = [
    "kill 123",
    "kill -9 123",
    "kill -KILL 123 456",
    "kill -s TERM 123",
    "kill -SIGTERM 123",
    "kill -l",
    "kill -- 123",
    "kill 123 2>/dev/null",
    "kill 123; echo done",
    "kill 123 && echo done",
    "taskkill /PID 123",
    "taskkill /F /PID 123",
    "taskkill /T /F /PID 123",
    "taskkill /PID 123 /PID 456 /F",
    "taskkill //PID 123 //F",
    "TASKKILL /pid 123 /t /f",
    "taskkill.exe /PID 123 /F > nul 2>&1",
    "taskkill.bat /PID 123",
    'powershell -ep Bypass -Command "Stop-Process -Id 123"',
    'bash -c -- "kill 123"',
    "sudo -u root kill 123",
    "nice -n 5 python -B x.py",
    "taskkill /?",
    "cmd /c taskkill /PID 123 /F",
    'powershell -Command "Stop-Process -Id 123 -Force"',
    "powershell -NoProfile -Command 'Stop-Process -Id 123,456'",
    "sudo kill -9 123",
    "xargs kill 123",
    "pgrep python",
    "pgrep -f cli_worker",
    "ps aux | grep python",
    "tasklist | findstr python",
    'echo "pkill python"',
    "echo 'taskkill /IM python.exe'",
    'grep -rn "taskkill" tools/agent-workflow',
    'git commit -m "document pkill and taskkill rule"',
    "git log --oneline",
    "ls -la",
    "python -m pytest -q",
    "cat notes.md | head",
    "wmic process get name,processid",
    "docker kill my-container",
    "skillet --run",
    "pskill 123",
    "python -c \"print('kill python')\"",
    "echo hello 2>&1 | tee out.txt",
    "true || echo failed",
]

ALLOWED_PS = [
    "Stop-Process -Id 123",
    "Stop-Process -Id 123,456",
    "Stop-Process -Id 123, 456",
    "stop-process -id 123 -force",
    "STOP-PROCESS -ID 123 -FORCE",
    "Stop-Process -Id:123",
    "Stop-Process 123",
    "Stop-Process -Id 123 -Force -ErrorAction SilentlyContinue",
    "Stop-Process -Id 123 -ErrorAction:Ignore",
    "Stop-Process -Id 123 -Confirm:$false",
    "Stop-Process -Id 123 -WhatIf",
    "spps -Id 123 -Force",
    "spps 123",
    "kill 123",
    "kill -Id 123",
    "Stop-Process -Id 123 *>$null",
    "Stop-Process -Id 123 2>$null",
    "Stop-Process -Id 123; Write-Output done",
    "taskkill /PID 123 /F",
    "& taskkill /PID 123 /T /F",
    "& 'C:\\Windows\\System32\\taskkill.exe' /PID 123 /F",
    "Get-Process -Id 123",
    "Get-Process python | Select-Object Id, ProcessName",
    "Get-CimInstance Win32_Process | Select-Object ProcessId, CommandLine",
    'Get-CimInstance Win32_Process -Filter "ProcessId=123" | Select-Object CommandLine',
    "Get-Process python | Format-Table -AutoSize",
    "Write-Output 'Stop-Process -Name python'",
    '"Stop-Process -Name python" | Out-File notes.txt',
    "Select-String -Path README.md -Pattern 'taskkill'",
    "Get-Content notes.md | Select-String 'pkill'",
    "Get-ChildItem | Where-Object { $_.Name -like '*.py' }",
]


@pytest.mark.parametrize("cmd", BLOCKED_BASH)
def test_bash_blocked(cmd: str) -> None:
    assert blocked(cmd, "Bash"), cmd


@pytest.mark.parametrize("cmd", BLOCKED_PS)
def test_powershell_blocked(cmd: str) -> None:
    assert blocked(cmd, "PowerShell"), cmd


@pytest.mark.parametrize("cmd", ALLOWED_BASH)
def test_bash_allowed(cmd: str) -> None:
    assert guard.classify(cmd, "Bash") is None, (cmd, guard.classify(cmd, "Bash"))


@pytest.mark.parametrize("cmd", ALLOWED_PS)
def test_powershell_allowed(cmd: str) -> None:
    assert guard.classify(cmd, "PowerShell") is None, (cmd, guard.classify(cmd, "PowerShell"))


def test_bash_tool_blocks_powershell_spellings_and_vice_versa() -> None:
    # Cross-shell: PowerShell verbs reached through Bash and bash-style kill through PowerShell.
    assert blocked("powershell -c 'spps -Name python'", "Bash")
    assert blocked("Stop-Process -Name python", "Bash")
    assert blocked("bash -c 'pkill python'", "PowerShell")
    assert blocked("wsl pkill python", "PowerShell")
    assert guard.classify("bash -c 'kill 123'", "PowerShell") is None


def test_encoded_command_is_decoded() -> None:
    payload = base64.b64encode("Stop-Process -Name python".encode("utf-16-le")).decode()
    assert blocked(f"powershell -NoProfile -EncodedCommand {payload}", "PowerShell")
    ok = base64.b64encode("Stop-Process -Id 123".encode("utf-16-le")).decode()
    assert guard.classify(f"powershell -EncodedCommand {ok}", "PowerShell") is None


def test_reason_mentions_the_cause() -> None:
    assert "name" in guard.classify("pkill python")
    assert "/PID" in guard.classify("taskkill /IM python.exe") or "image" in guard.classify(
        "taskkill /IM python.exe"
    )
    assert "pipeline" in guard.classify("Get-Process x | Stop-Process", "PowerShell")


def test_empty_and_odd_inputs_do_not_crash() -> None:
    for cmd in [
        "",
        "   ",
        "\n",
        "'",
        '"',
        "|",
        "&&",
        "((((",
        "}}}",
        "echo 'unterminated",
        "kill",
        "taskkill /?",
    ]:
        guard.classify(cmd, "Bash")
        guard.classify(cmd, "PowerShell")


def test_unparseable_text_with_a_guarded_verb_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(guard, "_classify", boom)
    assert guard.classify("pkill python") is not None
    assert guard.classify("ls -la") is None


def run_hook(payload: dict | str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    # AGENT_WORKFLOW_STATE_DIR keeps the block log out of the real .tmp/pm-state.
    env = {**os.environ, "AGENT_WORKFLOW_STATE_DIR": str(tmp_path / "pm-state")}
    return subprocess.run(
        [sys.executable, "-B", "-I", str(HOOK)],
        input=data,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        env=env,
    )


def test_hook_exit_codes_and_message(tmp_path: Path) -> None:
    deny = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "taskkill /IM notepad-does-not-exist.exe"}},
        tmp_path,
    )
    assert deny.returncode == 2
    assert "BLOCKED by process_guard" in deny.stderr and "Footer rule 12" in deny.stderr
    assert deny.stdout == ""
    log = tmp_path / "pm-state" / "process-guard.log"
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["tool"] == "Bash"
    ok = run_hook(
        {"tool_name": "PowerShell", "tool_input": {"command": "Stop-Process -Id 99999999"}},
        tmp_path,
    )
    assert ok.returncode == 0 and ok.stderr == ""


def test_hook_ignores_other_tools_and_bad_input(tmp_path: Path) -> None:
    other = run_hook({"tool_name": "Read", "tool_input": {"command": "pkill python"}}, tmp_path)
    assert other.returncode == 0
    assert run_hook("not json", tmp_path).returncode == 0
    assert run_hook({"tool_name": "Bash", "tool_input": {}}, tmp_path).returncode == 0
    assert run_hook({"tool_name": "Bash"}, tmp_path).returncode == 0


try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # hypothesis is optional for this tooling suite
    st = None

if st is not None:
    names = st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789_*.-", min_size=1, max_size=12
    ).filter(lambda s: not s.isdigit() and not s.startswith("-"))
    pids = st.integers(min_value=1, max_value=10**7)

    @settings(max_examples=150, derandomize=True, deadline=None)
    @given(
        name=names,
        spelling=st.sampled_from(
            [
                "Stop-Process -Name {n}",
                "stop-process -name {n} -force",
                "spps -ProcessName {n}",
                "Get-Process {n} | Stop-Process",
                "Get-Process {n} | kill",
                "taskkill /IM {n}",
                "taskkill /F /IM {n}.exe",
                "pkill {n}",
                "killall {n}",
                "kill {n}",
                "echo x; taskkill /IM {n}",
                'powershell -Command "Stop-Process -Name {n}"',
            ]
        ),
    )
    def test_property_name_targets_are_always_blocked(name: str, spelling: str) -> None:
        cmd = spelling.format(n=name)
        assert blocked(cmd, "Bash"), cmd
        assert blocked(cmd, "PowerShell"), cmd

    @settings(max_examples=150, derandomize=True, deadline=None)
    @given(
        pid=pids,
        spelling=st.sampled_from(
            [
                "Stop-Process -Id {p}",
                "stop-process -id {p} -Force",
                "spps {p}",
                "taskkill /PID {p}",
                "taskkill /F /PID {p} /T",
                "kill {p}",
                "kill -9 {p}",
            ]
        ),
    )
    def test_property_literal_pids_are_allowed(pid: int, spelling: str) -> None:
        cmd = spelling.format(p=pid)
        tool = "PowerShell" if spelling.lower().startswith(("stop-process", "spps")) else "Bash"
        assert not blocked(cmd, tool), cmd


def _prefixes(full: str, start: int) -> list[str]:
    return [full[:k] for k in range(start, len(full) + 1)]


def _enc(text: str) -> str:
    return base64.b64encode(text.encode("utf-16-le")).decode()


@pytest.mark.parametrize(
    "opt", _prefixes("-command", 2) + _prefixes("-commandwithargs", 9) + ["-cwa", "-COM"]
)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_powershell_command_parameter_prefixes_are_inspected(opt: str, tool: str) -> None:
    assert blocked(f'powershell {opt} "Stop-Process -Name python"', tool), opt
    assert blocked(f'pwsh -NoProfile {opt} "Stop-Process -Name python"', tool), opt
    assert blocked(f'powershell {opt}:"Stop-Process -Name python"', tool), opt
    assert not blocked(f'powershell {opt} "Stop-Process -Id 123"', tool), opt


@pytest.mark.parametrize("opt", [*_prefixes("-encodedcommand", 2), "-ec", "-EN"])
def test_powershell_encoded_parameter_prefixes_are_decoded(opt: str) -> None:
    bad, ok = _enc("Stop-Process -Name python"), _enc("Stop-Process -Id 123")
    assert blocked(f"powershell {opt} {bad}", "Bash"), opt
    assert blocked(f"powershell -NoProfile {opt} {bad}", "PowerShell"), opt
    assert not blocked(f"powershell {opt} {ok}", "PowerShell"), opt


@pytest.mark.parametrize(
    "cmd",
    [
        'cmd /c"taskkill /IM python.exe"',
        'cmd /C"taskkill /F /IM python.exe"',
        'cmd /k"taskkill /IM python.exe"',
        'cmd.exe /s /c"taskkill /IM python.exe"',
        "cmd /ctaskkill /IM python.exe",
        'cmd /c "taskkill /IM python.exe"',
        'cmd /r"taskkill /IM python.exe"',
    ],
)
def test_cmd_glued_and_spaced_switches_are_inspected(cmd: str) -> None:
    assert blocked(cmd, "Bash"), cmd
    assert blocked(cmd, "PowerShell"), cmd


def test_cmd_with_numeric_pid_is_allowed() -> None:
    assert not blocked('cmd /c"taskkill /PID 123"')
    assert not blocked("cmd /c echo hi")


@pytest.mark.parametrize(
    "cmd",
    [
        '$p."Kill"()',
        "$p.'Kill'()",
        "(Get-Process python).'Kill'()",
        '(Get-Process python)."Terminate"()',
        "$_.'kill' ()",
        "@(Get-Process python).ForEach({ $_.Kill() })",
        "Invoke-CimMethod (Get-CimInstance Win32_Process) Terminate",
        "Invoke-CimMethod -InputObject $x -Method Terminate",
        "Invoke-CimMethod -Me Terminate -InputObject $x",
        "Invoke-CimMethod $x -MethodName 'Terminate'",
        "Invoke-WmiMethod -Path $x terminate",
        "Invoke-CimMethod $x\nTerminate",
    ],
)
def test_powershell_method_spellings_are_blocked(cmd: str) -> None:
    assert blocked(cmd, "PowerShell"), cmd


def test_unrelated_method_calls_are_allowed() -> None:
    assert not blocked("$p.'Name'()", "PowerShell")
    assert not blocked("Invoke-CimMethod $x -MethodName Create", "PowerShell")


# --- Command positions only (WFTOOLS3): the three logged false positives, their negative twins and the siblings. ---
# The logs in .tmp/pm-state/process-guard.log keep the first 300 characters of each command; the part after that is
# reconstructed here from the same task (same shape: a heredoc whose body mentions the guarded words).
LOGGED_FALSE_POSITIVES = [
    # 1. 2026-10-10T19:29 heredoc writing a test file: a body line starting with `kill` ("kill target `=`")
    (
        "cd /d/Project_github/x/tests && cat > test_cli_worker.py <<'EOF'\n"
        '"""Unit tests for cli_worker.py command building (no claude process is started)."""\n\n'
        "from __future__ import annotations\n\nimport json\n\nkill = 1\n\n"
        "def test_x() -> None:\n    assert kill\nEOF\npytest -q",
        "Bash",
    ),
    # 2. 2026-10-10T20:03 sed script whose text mentions options, then a heredoc with `.Kill()` in the body
    (
        "sed -i '358s#-c/-Command/\\\\c option#-c / -Command / /c option#' hooks/process_guard.py"
        " && sed -n 358p hooks/process_guard.py\n"
        "cat >> tests/test_process_guard.py <<'EOF'\n\n\ndef _prefixes(full: str, start: int) -> list[str]:\n"
        "    return [full[:k] for k in range(start, len(full) + 1)]\n\n"
        'BAD = ["$p.Kill()", "(Get-Process x).Terminate()", "Get-Process | % Kill"]\nEOF',
        "Bash",
    ),
    # 3. 2026-10-10T20:04 python reading a heredoc body with README text about .Kill()/.Terminate()
    (
        "python - <<'EOF'\np='README.md'\ns=open(p,encoding='utf-8').read()\n"
        'a="`.Kill()`/`.Terminate()` is caught only on `$var`, `$_`, `)`, `]` or `}` receivers"\n'
        'old="`.Kill()`/`.Terminate()` is caught only on `$var`; Invoke-WmiMethod terminate"\n'
        "assert old in s\nopen(p,'w',encoding='utf-8').write(s.replace(old, a))\nEOF",
        "Bash",
    ),
]


@pytest.mark.parametrize(("cmd", "tool"), LOGGED_FALSE_POSITIVES)
def test_logged_false_positives_are_now_allowed(cmd: str, tool: str) -> None:
    assert guard.classify(cmd, tool) is None, guard.classify(cmd, tool)


NEGATIVE_TWINS = [
    # a real command after the heredoc ended, or on the line of the operator
    ("cat > f <<'EOF'\ntext\nEOF\nkill python", "Bash"),
    ("cat > f <<'EOF'\ntext\nEOF\npkill python", "Bash"),
    ("cat <<'EOF' && pkill python\nbody\nEOF", "Bash"),
    ("cat <<'EOF'; kill python\nbody\nEOF", "Bash"),
    ("cat <<A <<B\n1\nA\n2\nB\nkill python", "Bash"),
    ("cat <<-EOF\n\tx\n\tEOF\nkill python", "Bash"),
    ("cat <<EOF\nEOF\ntaskkill /IM python.exe", "Bash"),
    ("python - <<'EOF'\nprint(1)\nEOF\nStop-Process -Name python", "Bash"),
    # shell strings are still parsed
    ('bash -c "pkill x"', "Bash"),
    ("cat > f <<'EOF'\nx\nEOF\nbash -c \"pkill x\"", "Bash"),
    ("sh -c 'cat <<EOF\nkill python\nEOF'", "Bash"),
    ("bash -c \"$(cat <<'EOF'\npkill x\nEOF\n)\"", "Bash"),
    ("eval \"$(cat <<'EOF'\npkill x\nEOF\n)\"", "Bash"),
    ('powershell -Command "$p.Kill()"', "Bash"),
    ("Start-Process powershell -ArgumentList '-Command','Stop-Process -Name x'", "PowerShell"),
    ("Start-Process powershell -ArgumentList '-Command', '$p.Kill()'", "PowerShell"),
    ("Start-Process powershell -ArgumentList '-Command Stop-Process -Name x'", "PowerShell"),
    ("Invoke-Expression 'Stop-Process -Name x'", "PowerShell"),
    ("iex @'\nStop-Process -Name python\n'@", "PowerShell"),
    # a shell reads the heredoc, here-string or pipe
    ("bash <<'EOF'\npkill x\nEOF", "Bash"),
    ("sudo -u root bash <<EOF\npkill x\nEOF", "Bash"),
    ("cat <<'EOF' | sh\npkill x\nEOF", "Bash"),
    ("cat <<EOF | cmd\ntaskkill /IM x\nEOF", "Bash"),
    ('bash <<< "pkill x"', "Bash"),
    ('echo "pkill x" | bash', "Bash"),
    ("echo pkill x | sh", "Bash"),
    ("printf '%s\\n' 'pkill x' | sh", "Bash"),
    ("@'\nStop-Process -Name python\n'@ | powershell -NoProfile -", "PowerShell"),
    # expansion executes: unquoted heredoc delimiter, double quotes
    ("cat <<EOF\n$(pkill x)\nEOF", "Bash"),
    ("cat <<EOF\n`pkill x`\nEOF", "Bash"),
    ('echo "$(pkill x)"', "Bash"),
    ('echo "a `pkill x`"', "Bash"),
    ('@"\n$(Stop-Process -Name python)\n"@ | Out-Null', "PowerShell"),
    ('Write-Output "$(Stop-Process -Name python)"', "PowerShell"),
    # text that only looks like a heredoc, comment or data
    ("echo $((1<<EOF))\npkill x\nEOF", "Bash"),
    ("cat <<EOF\npkill x", "Bash"),  # unterminated: the text stays code
    ("echo hi # cat <<EOF\npkill x\nEOF", "Bash"),
    ("echo a # don't\npkill x", "Bash"),
    ("mytool '$p.Kill()'", "Bash"),
    ("$x = Invoke-Expression '$p.Kill()'", "PowerShell"),
    ("$x = @'\nhi\n'@\nStop-Process -Name x", "PowerShell"),
    ("<# c #> Stop-Process -Name x", "PowerShell"),
    ("Stop-Process -Name x # -Id 12", "PowerShell"),
]


@pytest.mark.parametrize(("cmd", "tool"), NEGATIVE_TWINS)
def test_negative_twins_still_block(cmd: str, tool: str) -> None:
    assert guard.classify(cmd, tool) is not None, cmd


CONTEXT_ALLOWED = [
    # heredoc bodies, comments, data-command strings and string literals are not commands
    ("cat <<'EOF'\n$(pkill x)\nEOF", "Bash"),
    ("cat <<'EOF' | grep sh\npkill x\nEOF", "Bash"),
    ("cat <<-EOF\n\tpkill\n\tEOF\nkill 12", "Bash"),
    ("git commit -F - <<'EOF'\nDocument pkill and $p.Kill()\nEOF", "Bash"),
    ("git commit -m \"$(cat <<'EOF'\npkill is blocked; $p.Kill() too\nEOF\n)\"", "Bash"),
    (
        "python - <<'EOF'\nimport os\nos.kill(1, 9)\nEOF",
        "Bash",
    ),  # interpreter code stays uninspected
    ("echo a # pkill x", "Bash"),
    ("echo '$p.Kill()'", "Bash"),
    ("sed -e 's/(Get-Process x).Kill()/y/' f", "Bash"),
    ('git commit -m "mention $p.Kill() and .Terminate()"', "Bash"),
    ("echo '$(pkill x)'", "Bash"),
    ("$x = @'\n$p.Kill()\n'@\nGet-Date", "PowerShell"),
    ("$x=@'\n$p.Kill()\n'@\nGet-Date", "PowerShell"),
    ("Write-Output '$p.Kill()'", "PowerShell"),
    ("Set-Content f.txt -Value '(Get-Process x).Kill()'", "PowerShell"),
    ("$x = '$p.Kill()'", "PowerShell"),
    ("# $p.Kill()\nGet-Date", "PowerShell"),
    ("<# $p.Kill() #> Get-Date", "PowerShell"),
    ("Stop-Process -Id 12 # -Name x", "PowerShell"),
]


@pytest.mark.parametrize(("cmd", "tool"), CONTEXT_ALLOWED)
def test_non_command_text_is_ignored(cmd: str, tool: str) -> None:
    assert guard.classify(cmd, tool) is None, (cmd, guard.classify(cmd, tool))


def test_hook_allows_a_logged_false_positive_end_to_end(tmp_path: Path) -> None:
    cmd, tool = LOGGED_FALSE_POSITIVES[2]
    ok = run_hook({"tool_name": tool, "tool_input": {"command": cmd}}, tmp_path)
    assert ok.returncode == 0 and ok.stderr == ""
    assert not (tmp_path / "pm-state" / "process-guard.log").exists()
    twin = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": cmd + "\nkill python"}}, tmp_path
    )
    assert twin.returncode == 2 and "BLOCKED by process_guard" in twin.stderr


def test_parse_failure_with_a_guarded_verb_still_blocks_heredoc_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(guard, "_parse", boom)
    assert (
        guard.classify("cat <<'EOF'\npkill python\nEOF") is not None
    )  # fail closed on the raw text
    assert guard.classify("cat <<'EOF'\nhello\nEOF") is None


def test_heredoc_edge_forms_do_not_crash() -> None:
    for cmd in [
        "cat <<",
        "cat <<-",
        "cat << ",
        "cat <<''\n\n",
        "cat <<'EOF",
        "cat <<EOF\r\nbody\r\nEOF\r\nkill 1",
        "<<EOF",
        "cat <<EOF\n",
        "@'",
        "@'\n",
        '@"\n$(\n"@',
        "<#",
        "echo $((1<<",
        "(( 1 << 2 ))\ncat <<EOF\nx\nEOF",
    ]:
        for tool in ("Bash", "PowerShell"):
            guard.classify(cmd, tool)
    assert guard.classify("cat <<EOF\r\nbody\r\nEOF\r\nkill python") is not None
    assert guard.classify("cat <<EOF\r\npkill\r\nEOF\r\nkill 1") is None


if st is not None:
    # The body alphabet includes quotes, separators, guarded verbs, `#` and `<<`; `$` and the backtick stay out
    # because an unquoted delimiter expands them (that case is the substitution tests above).
    body_text = st.text(
        alphabet=st.sampled_from(
            list(
                "abc kill pkill Stop-Process -Name taskkill /IM $_.Kill() Terminate(){}|;&<>'\"#\\\n\t\r"
            )
        ),
        max_size=120,
    )
    tails = st.sampled_from(
        ["", "echo done", "kill 123", "pkill python", "Stop-Process -Name x", "ls | wc"]
    )

    def _verdict(cmd: str, tool: str) -> bool:
        return guard.classify(cmd, tool) is not None

    @settings(max_examples=300, derandomize=True, deadline=None)
    @given(
        body=body_text,
        tail=tails,
        head=st.sampled_from(
            ["cat > f", "python -", "cat >> notes.md", "tee out.txt", "git commit -F -"]
        ),
        delim=st.sampled_from(["EOF", "'EOF'", '"EOF"', "\\EOF", "E'O'F"]),
        dash=st.sampled_from(["", "-"]),
    )
    def test_property_heredoc_body_never_changes_the_verdict(
        body: str, tail: str, head: str, delim: str, dash: str
    ) -> None:
        # Quoted delimiter: the body is literal, so any body gives the verdict of an empty body.
        name = delim.replace("'", "").replace('"', "").replace("\\", "")
        if any(line.rstrip("\r").lstrip("\t") == name for line in body.split("\n")):
            return  # the body would end early
        sep = "\n" if tail else ""
        tab = "\t" if dash else ""
        quoted = delim != "EOF"
        make = lambda b: f"{head} <<{dash}{delim}\n{b}\n{tab}{name}{sep}{tail}"  # noqa: E731
        if quoted:
            assert _verdict(make(body), "Bash") == _verdict(make(""), "Bash"), repr(make(body))
        else:
            clean = body.replace("$", "").replace("`", "")
            assert _verdict(make(clean), "Bash") == _verdict(make(""), "Bash"), repr(make(clean))

    ps_body = st.text(
        alphabet=st.sampled_from(
            list(
                "abc kill Stop-Process -Name taskkill /IM _.Kill() Terminate(){}|;&<>'\"#\\\n\t\r%[]="
            )
        ),
        max_size=120,
    )

    @settings(max_examples=300, derandomize=True, deadline=None)
    @given(
        body=ps_body,
        tail=st.sampled_from(["", "Get-Date", "Stop-Process -Id 12", "Stop-Process -Name x"]),
        quote=st.sampled_from(["'", '"']),
        prefix=st.sampled_from(["$x = ", "Set-Content f.txt -Value ", "Write-Output ", "$x="]),
    )
    def test_property_here_string_body_never_changes_the_verdict(
        body: str, tail: str, quote: str, prefix: str
    ) -> None:
        if any(line.startswith(quote + "@") for line in body.split("\n")):
            return  # the body would end early
        make = lambda b: f"{prefix}@{quote}\n{b}\n{quote}@\n{tail}"  # noqa: E731
        assert _verdict(make(body), "PowerShell") == _verdict(make(""), "PowerShell"), repr(
            make(body)
        )

    @settings(max_examples=200, derandomize=True, deadline=None)
    @given(body=body_text, delim=st.sampled_from(["", "'", '"']))
    def test_property_shell_fed_heredoc_is_parsed_as_a_script(body: str, delim: str) -> None:
        # The opposite invariant: when a shell reads the heredoc, a guarded command in the body blocks.
        body = "".join(
            ch for ch in body if ch not in "'\"\\<"
        )  # no quote or nested heredoc may swallow the verb
        if any(line.rstrip("\r") == "EOF" for line in body.split("\n")):
            return
        cmd = f"bash <<{delim}EOF{delim}\n{body}\npkill python\nEOF"
        assert _verdict(cmd, "Bash"), repr(cmd)
