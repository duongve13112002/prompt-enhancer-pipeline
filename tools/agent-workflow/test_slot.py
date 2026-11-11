"""Run one test command inside a machine-wide test slot (user decision 197).

Usage: python -B test_slot.py -- <command> [args...]
At most N test commands run at once, N read from .tmp/pm-state/test-slots/max
(4 normally, 2 while a GPU job runs). The command waits for a free slot, runs,
and its exit code is returned. A slot whose owner process is gone is reclaimed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLOTS = ROOT / ".tmp" / "pm-state" / "test-slots"
POLL_SECONDS = 15.0
NATIVE_WINDOWS = hasattr(sys, "getwindowsversion")


def _limit() -> int:
    try:
        value = int((SLOTS / "max").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 2  # Unknown state: assume the stricter GPU-time limit.
    return value if 1 <= value <= 8 else 2


def _alive(pid: int) -> bool:
    if NATIVE_WINDOWS:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire() -> Path:
    SLOTS.mkdir(parents=True, exist_ok=True)
    announced = False
    while True:
        for index in range(_limit()):
            slot = SLOTS / f"slot-{index}.lock"
            try:
                fd = os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    owner = int(slot.read_text(encoding="ascii").split()[0])
                except (OSError, ValueError, IndexError):
                    continue
                if not _alive(owner):
                    slot.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')} {os.getcwd()}\n")
            return slot
        if not announced:
            print(f"test_slot: waiting for one of {_limit()} test slots", flush=True)
            announced = True
        time.sleep(POLL_SECONDS)


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print("usage: test_slot.py -- <command> [args...]", file=sys.stderr)
        return 2
    command = argv[argv.index("--") + 1 :]
    slot = _acquire()
    try:
        return subprocess.call(command)
    finally:
        slot.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
