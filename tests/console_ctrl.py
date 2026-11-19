"""Test helper (Windows only): deliver a console control event to another process, from a process that has no window.

Usage: `python console_ctrl.py <pid> [c|break]`. A child started with CREATE_NO_WINDOW owns a hidden console. This helper
(itself started with CREATE_NO_WINDOW) leaves its own console, attaches to the target's, ignores the event it is about
to send, generates CTRL_C_EVENT (`c`, the default) or CTRL_BREAK_EVENT (`break`) for that console and leaves again.
Only the processes on the target's console receive it; the test process and its console are not involved. Exit status:
0 sent, 3 could not attach, 4 could not send."""
import ctypes
import sys
import time

CTRL_C_EVENT, CTRL_BREAK_EVENT = 0, 1  # protocol-bound Windows constants


@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
def _handled(event):
    return True  # this process takes either event as handled, so neither ends it


def main():
    event = CTRL_BREAK_EVENT if len(sys.argv) > 2 and sys.argv[2] == 'break' else CTRL_C_EVENT
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(int(sys.argv[1])):
        return 3
    kernel.SetConsoleCtrlHandler(_handled, True)
    sent = kernel.GenerateConsoleCtrlEvent(event, 0)
    time.sleep(0.5)  # let the console dispatch the event before this process leaves it
    kernel.FreeConsole()
    return 0 if sent else 4


if __name__ == '__main__':
    sys.exit(main())
