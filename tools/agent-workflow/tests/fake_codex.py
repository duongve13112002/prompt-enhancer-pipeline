"""TEST DOUBLE of the `codex` executable (never real Codex, never evidence of Codex behaviour).

Used only by test_cli_worker_codex.py: the tests substitute `[python, -B, this file]` for the single executable path that
production requires (the test seam in World). The plan file named by
the FAKE_CODEX_PLAN environment variable (passed through `runtime.env_allow`) is JSON:
  record: directory that receives one `<n>.json` per call (argv, stdin, cwd, selected environment)
  sandbox: {"secrets": "denied" | "denied-missing" | "other-oserror" | "bad-code" | "readable" | "readable-rc1" |
            "silent-fail" | "timeout" | "crash", "control": "ok" | "fail"}; the exit code follows the reader in use
  turns: list of per-call exec behaviours, the last repeats; each: thread_id, usage (cumulative), message, mode
    mode: ok | fail (turn.failed, rc 1) | failed_rc0 | no_completed | retry_error | error_after | leave_scratch | hang
    no_hook_receipt: true leaves no PostModelRequest receipt, as a turn whose host hooks never ran
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ENV_KEYS = (
    "CODEX_HOME",
    "TEMP",
    "TMP",
    "PATH",
    "PYTHONPATH",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "SOME_SECRET",
    "FAKE_ALLOWED",
)


def emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def sandbox_read(sandbox: dict[str, str], args: list[str]) -> int:
    """Emulate `codex sandbox ... -P <profile> <reader command>`: either the project-Python reader script (exit codes
    77 permission denied, 66 not found, 74 other OSError) or the plain reader (`cmd /c type` / `cat`, rc 1 on failure)."""
    command = args[args.index("-P") + 2 :]
    python_reader = len(command) >= 5 and command[1:3] == ["-I", "-S"]
    target = Path(command[4] if python_reader else command[-1])
    if target.name == "control.txt":
        if sandbox.get("control", "ok") == "fail":
            return 2
        if not python_reader or command[-1] == "echo":
            sys.stdout.write(target.read_text(encoding="utf-8"))
        return 0
    mode = sandbox.get("secrets", "denied")
    if mode == "readable":
        sys.stdout.write("secret content\n")
        return 0
    if mode == "readable-rc1":
        sys.stdout.write("secret content\n")  # content AND a message AND a failing exit code
        sys.stderr.write("warning: partial read\n")
        return 1
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "crash":
        os._exit(139)  # killed: no stdout, no stderr
    if mode == "silent-fail":
        return 1
    if mode == "bad-code":
        return 3 if python_reader else 127  # a reader or launcher failure, not a recognised denial
    if mode == "denied-missing":
        sys.stderr.write("(the deny hides the file)\n")
        return 66 if python_reader else 1
    if mode == "other-oserror":
        sys.stderr.write("some other OSError\n")
        return 74 if python_reader else 1
    sys.stderr.write("Access is denied.\n")
    return 77 if python_reader else 1


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.167.0")
        return 0
    plan = json.loads(Path(os.environ["FAKE_CODEX_PLAN"]).read_text(encoding="utf-8"))
    record = Path(plan["record"])
    record.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    stdin_text = "" if args and args[0] == "sandbox" else sys.stdin.read()
    number = len(list(record.glob("[0-9]*.json"))) + 1
    (record / f"{number}.json").write_text(
        json.dumps(
            {
                "argv": args,
                "stdin": stdin_text,
                "cwd": os.getcwd(),
                "env": {k: os.environ.get(k) for k in ENV_KEYS},
                "env_names": sorted(os.environ),
            }
        ),
        encoding="utf-8",
    )
    if args[0] == "sandbox":
        return sandbox_read(plan.get("sandbox", {}), args)
    counter = record / "exec.count"
    executed = int(counter.read_text(encoding="utf-8")) + 1 if counter.is_file() else 1
    counter.write_text(str(executed), encoding="utf-8")
    turns = plan["turns"]
    turn = turns[min(executed, len(turns)) - 1]
    mode = turn.get("mode", "ok")
    output = None
    for position, arg in enumerate(args):
        if arg in ("-o", "--output-last-message"):
            output = Path(args[position + 1])
    thread_id = turn.get("thread_id") or str(uuid.uuid4())
    if mode != "no_thread":
        emit({"type": "thread.started", "thread_id": thread_id})
    emit({"type": "turn.started"})
    # Test double for the PostModelRequest observer: a real turn leaves this receipt.
    policy = next((m for a in args if (m := re.search(r"--policy (\S+\.json)", a))), None)
    if policy and mode != "no_thread" and not turn.get("no_hook_receipt"):
        path = Path(policy[1])
        launch = json.loads(path.read_text(encoding="utf-8"))["launch_id"]
        session = args[-2] if args[:2] == ["exec", "resume"] else thread_id
        line = {"type": "context", "session_id": session, "input_tokens": None}
        with (path.parent / (launch + ".jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line) + "\n")
    if mode == "retry_error":
        emit({"type": "error", "message": "Reconnecting... 1/5"})
    if mode == "leave_scratch":
        scratch = Path(os.environ["TEMP"])
        (scratch / "left-behind.txt").write_text("x", encoding="utf-8")
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        (record / "child.pid").write_text(str(child.pid), encoding="utf-8")
        time.sleep(120)
    message = turn.get("message", "done")
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": message}})
    if output is not None and mode != "no_output":
        output.write_text(message, encoding="utf-8")
    if mode == "fail":
        emit({"type": "turn.failed", "error": {"message": "model refused"}})
        return 1
    if mode == "failed_rc0":
        emit({"type": "turn.failed", "error": {"message": "stream died"}})
        return 0
    if mode == "no_completed":
        return 0
    emit({"type": "turn.completed", "usage": turn.get("usage", {})})
    if mode == "error_after":
        emit({"type": "error", "message": "late failure"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
