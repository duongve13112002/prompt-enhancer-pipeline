"""Tests for test_slot.py: Windows crash-dialog suppression, exit code preservation, slot isolation."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_slot  # noqa: E402

TEST_SLOT_SCRIPT = Path(__file__).resolve().parents[1] / "test_slot.py"

# Protocol-bound Windows error mode constants (Microsoft SetErrorMode documentation).
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002


def _isolated_script(tmp_path: Path) -> Path:
    """A copy of test_slot.py at <tmp>/tools/agent-workflow/, so its ROOT and SLOTS resolve inside tmp_path.

    A subprocess run of the real script would take a slot in the live .tmp/pm-state/test-slots/ while the
    outer test command already holds one, and could wait forever when every slot is taken.
    """
    target = tmp_path / "tools" / "agent-workflow" / "test_slot.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(TEST_SLOT_SCRIPT, target)
    return target


def test_windows_crash_dialogs_suppressed_in_child_process(tmp_path: Path) -> None:
    """On Windows, a subprocess started under test_slot.py inherits suppressed crash dialogs."""
    if not hasattr(sys, "getwindowsversion"):
        pytest.skip("Windows error mode inheritance (Windows only)")

    script = _isolated_script(tmp_path)
    code = "import ctypes; print(ctypes.windll.kernel32.GetErrorMode())"
    cmd = [sys.executable, "-B", str(script), "--", sys.executable, "-c", code]
    # CREATE_DEFAULT_ERROR_MODE: test_slot.py starts without the error mode this test process may already carry
    # (it runs under test_slot.py itself), so only test_slot.py's own call can set the bits the child reports.
    # CREATE_NO_WINDOW: no console window appears on the user's screen (standing rule; LESSONS_LEARNED).
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60,
                            creationflags=subprocess.CREATE_DEFAULT_ERROR_MODE | subprocess.CREATE_NO_WINDOW)
    mode = int(result.stdout.strip())
    assert (mode & SEM_FAILCRITICALERRORS) != 0, f"SEM_FAILCRITICALERRORS (0x0001) not set in {mode:#x}"
    assert (mode & SEM_NOGPFAULTERRORBOX) != 0, f"SEM_NOGPFAULTERRORBOX (0x0002) not set in {mode:#x}"
    assert list((tmp_path / ".tmp" / "pm-state" / "test-slots").glob("slot-*.lock")) == []


def test_suppress_crash_dialogs_helper_keeps_existing_bits() -> None:
    """Direct helper test: sets the required bits and keeps a bit set beforehand (SEM_NOOPENFILEERRORBOX)."""
    if not hasattr(sys, "getwindowsversion"):
        pytest.skip("Windows error mode (Windows only)")

    import ctypes

    kernel32 = ctypes.windll.kernel32
    sem_noopenfileerrorbox = 0x8000  # protocol-bound: a third documented flag, set here so preservation is tested
    original = kernel32.GetErrorMode()
    kernel32.SetErrorMode(sem_noopenfileerrorbox)
    try:
        test_slot._suppress_crash_dialogs()
        mode = kernel32.GetErrorMode()
    finally:
        kernel32.SetErrorMode(original)
    assert mode & sem_noopenfileerrorbox
    assert (mode & SEM_FAILCRITICALERRORS) != 0
    assert (mode & SEM_NOGPFAULTERRORBOX) != 0


def test_nonzero_exit_code_returned_unchanged_and_slot_freed(tmp_path: Path) -> None:
    """On every platform, test_slot.py returns a non-zero exit code unchanged and frees its slot."""
    script = _isolated_script(tmp_path)
    exit_code = 42
    code = f"import sys; sys.exit({exit_code})"
    cmd = [sys.executable, "-B", str(script), "--", sys.executable, "-c", code]
    flags = subprocess.CREATE_NO_WINDOW if hasattr(sys, "getwindowsversion") else 0
    result = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=flags)
    assert result.returncode == exit_code
    assert list((tmp_path / ".tmp" / "pm-state" / "test-slots").glob("slot-*.lock")) == []


def test_usage_error_when_syntax_invalid() -> None:
    """Missing '--' or command after '--' returns exit code 2."""
    assert test_slot.main([]) == 2
    assert test_slot.main(["--"]) == 2
    assert test_slot.main(["arg_without_separator"]) == 2
