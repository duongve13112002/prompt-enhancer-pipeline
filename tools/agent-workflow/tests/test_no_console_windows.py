"""No code or test opens a console window on the user's screen (docs/rules/PLATFORM_GPU_RULES.md, lesson 2026-11-15).

A process with no console that starts a console program makes Windows open a visible console, and the venv
`python.exe` is a redirector whose interpreter child is such a program; so `DETACHED_PROCESS` or `CREATE_NEW_CONSOLE`
in a creation-flags expression, or a PowerShell `Start-Process` without `-NoNewWindow` / `-WindowStyle Hidden`, puts
windows on the screen. Structural check of this repository's own source, not of natural language.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCANNED = ("tools", "scripts", "src", "tests")
SELF = Path(__file__).resolve()
# The two Windows creation flags that give a child no console or a new one (winbase.h values), by name or number.
WINDOW_FLAGS = re.compile(r"\b(DETACHED_PROCESS|CREATE_NEW_CONSOLE)\b|\b0x0*(8|10)\b")
FLAG_CONTEXT = re.compile(r"creationflags|\bflags\s*=")
START_PROCESS = re.compile(r"^\s*(\$\w+\s*=\s*)?(Start-Process|saps)\b", re.I)
HIDDEN = re.compile(r"-NoNewWindow\b|-WindowStyle\s+Hidden\b", re.I)


def python_offences(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if FLAG_CONTEXT.search(line) and WINDOW_FLAGS.search(line)]


def powershell_offences(text: str) -> list[str]:
    commands, current = [], ""
    for line in text.splitlines():  # a trailing backtick continues the command on the next line
        current += line.rstrip("`").rstrip() + " " if line.rstrip().endswith("`") else line
        if not line.rstrip().endswith("`"):
            commands.append(current)
            current = ""
    return [c.strip() for c in commands if START_PROCESS.search(c) and not HIDDEN.search(c)]


def sources(suffix: str) -> list[Path]:
    found = []
    for top in SCANNED:
        base = ROOT / top
        if base.is_dir():
            found += [p for p in base.rglob(f"*{suffix}") if ".tmp" not in p.relative_to(ROOT).parts]
    return found


def test_no_creation_flag_opens_a_console() -> None:
    bad = {
        str(p.relative_to(ROOT)): hits
        for p in sources(".py")
        if p.resolve() != SELF and (hits := python_offences(p.read_text(encoding="utf-8", errors="replace")))
    }
    assert not bad, bad


def test_no_start_process_opens_a_window() -> None:
    bad = {
        str(p.relative_to(ROOT)): hits
        for p in sources(".ps1")
        if (hits := powershell_offences(p.read_text(encoding="utf-8-sig", errors="replace")))
    }
    assert not bad, bad


def test_the_detectors_catch_the_window_forms_and_pass_the_hidden_ones() -> None:
    assert python_offences("flags = 0x00000008 | 0x00000200 if os.name == 'nt' else 0")  # the 2026-11-15 test
    assert python_offences("subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE)")
    assert python_offences("Popen(argv, creationflags=DETACHED_PROCESS)")
    assert not python_offences("flags = 0x08000000 | 0x00000200 if os.name == 'nt' else 0")  # CREATE_NO_WINDOW
    assert not python_offences("Popen(argv, creationflags=0x01000000)")  # CREATE_BREAKAWAY_FROM_JOB
    assert powershell_offences("$p = Start-Process -FilePath node -ArgumentList @('a') `\n    -PassThru")
    assert not powershell_offences("$p = Start-Process -FilePath node `\n    -NoNewWindow -PassThru")
    assert not powershell_offences("Start-Process pwsh -WindowStyle Hidden")
    assert not powershell_offences("# Start-Process in a comment is not a command")
