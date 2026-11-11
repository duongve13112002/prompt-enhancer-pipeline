"""Unit tests for hooks/ctx_nudge.py (context nudge and loop warning)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "ctx_nudge.py"


def transcript(tmp_path: Path, *usages: int, sidechain_last: int | None = None) -> Path:
    path = tmp_path / "t.jsonl"
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})]
    for total in usages:
        usage = {
            "input_tokens": 2,
            "cache_read_input_tokens": total - 2 - 10,
            "cache_creation_input_tokens": 10,
        }
        lines.append(
            json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": usage}})
        )
    if sidechain_last is not None:
        usage = {"input_tokens": sidechain_last}
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {"role": "assistant", "usage": usage},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(
    tmp_path: Path, event: dict | str, *args: str, worker_env: bool = False
) -> tuple[int, str, str]:
    data = event if isinstance(event, str) else json.dumps(event)
    # The suite may itself run inside a cli_worker, which sets the worker switch; control it explicitly.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_DISABLE_AUTO_MEMORY"}
    if worker_env:
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-I", str(HOOK), "--state-dir", str(tmp_path / "state"), *args],
        input=data,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def event(tmp_path: Path, t: Path | None, command: str = "ls", session: str = "s1") -> dict:
    return {
        "session_id": session,
        "transcript_path": str(t) if t else str(tmp_path / "missing.jsonl"),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def message(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"] if stdout else ""


def test_below_threshold_is_silent(tmp_path: Path) -> None:
    rc, out, err = run(
        tmp_path, event(tmp_path, transcript(tmp_path, 50_000)), "--thresholds", "120000,150000"
    )
    assert (rc, out, err) == (0, "", "")


def test_crossing_prints_once_per_threshold(tmp_path: Path) -> None:
    args = ("--thresholds", "120000,150000")
    first = run(tmp_path, event(tmp_path, transcript(tmp_path, 125_000)), *args)
    assert first[0] == 0 and "125K" in message(first[1]) and "120K" in message(first[1])
    assert "retire" not in message(first[1]) and "task file" in message(first[1])
    again = run(tmp_path, event(tmp_path, transcript(tmp_path, 130_000)), *args)
    assert again[1] == ""
    second = run(tmp_path, event(tmp_path, transcript(tmp_path, 155_000)), *args)
    assert "150K" in message(second[1]) and "retire" not in message(second[1])
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 190_000)), *args)[1] == ""


def test_nudge_text_names_the_policy_and_keeps_the_truth(tmp_path: Path) -> None:
    out = message(
        run(tmp_path, event(tmp_path, transcript(tmp_path, 175_000)), "--thresholds", "170000")[1]
    )
    assert "175K" in out and "170K" in out
    for needed in ("progress", "decisions", "open findings", "next step", "task file", "summary"):
        assert needed in out, needed
    assert (
        "Nothing was compacted." in out and "window" not in out
    )  # no window configured: not named


def test_nudge_text_states_the_configured_window(tmp_path: Path) -> None:
    out = message(
        run(
            tmp_path,
            event(tmp_path, transcript(tmp_path, 175_000)),
            "--thresholds",
            "170000",
            "--autocompact-window",
            "230000",
        )[1]
    )
    assert "230K window" in out and "Nothing was compacted." in out


def test_both_thresholds_crossed_at_once_prints_one_line(tmp_path: Path) -> None:
    out = run(
        tmp_path, event(tmp_path, transcript(tmp_path, 160_000)), "--thresholds", "120000,150000"
    )[1]
    assert message(out).count("[ctx-nudge]") == 1 and "150K" in message(out)


def test_sessions_are_independent(tmp_path: Path) -> None:
    args = ("--thresholds", "100000")
    t = transcript(tmp_path, 120_000)
    assert run(tmp_path, event(tmp_path, t, session="a"), *args)[1] != ""
    assert run(tmp_path, event(tmp_path, t, session="b"), *args)[1] != ""
    assert run(tmp_path, event(tmp_path, t, session="a"), *args)[1] == ""


def test_uses_latest_main_thread_message_and_ignores_sidechain(tmp_path: Path) -> None:
    t = transcript(tmp_path, 130_000, 60_000, sidechain_last=900_000)
    assert run(tmp_path, event(tmp_path, t), "--thresholds", "120000")[1] == ""


@pytest.mark.parametrize("payload", ["", "not json", "[]", "{}", '{"session_id": ""}'])
def test_bad_input_is_silent_and_exit_zero(tmp_path: Path, payload: str) -> None:
    assert run(tmp_path, payload, "--thresholds", "100")[:2] == (0, "")


def test_missing_or_garbage_transcript_is_silent(tmp_path: Path) -> None:
    assert run(tmp_path, event(tmp_path, None), "--thresholds", "100")[:2] == (0, "")
    junk = tmp_path / "junk.jsonl"
    junk.write_bytes(b"\xff\xfe not json\n{\n")
    assert run(tmp_path, event(tmp_path, junk), "--thresholds", "100")[:2] == (0, "")


def test_off_and_invalid_thresholds_disable_the_size_nudge(tmp_path: Path) -> None:
    t = transcript(tmp_path, 500_000)
    assert run(tmp_path, event(tmp_path, t), "--thresholds", "off")[1] == ""
    assert run(tmp_path, event(tmp_path, t, session="x"), "--thresholds", "150000,120000")[:2] == (
        0,
        "",
    )
    assert run(tmp_path, event(tmp_path, t, session="y"), "--thresholds", "abc")[:2] == (0, "")


def test_loop_warning_after_five_identical_calls_only(tmp_path: Path) -> None:
    for n in range(1, 5):
        assert (
            run(tmp_path, event(tmp_path, None, "tail -3 log"), "--thresholds", "off")[1] == ""
        ), n
    fifth = run(tmp_path, event(tmp_path, None, "tail -3 log"), "--thresholds", "off")
    assert "[loop-warning]" in message(fifth[1]) and "5 tool calls" in message(fifth[1])
    assert fifth[0] == 0
    assert run(tmp_path, event(tmp_path, None, "tail -3 log"), "--thresholds", "off")[1] == ""


def test_different_call_resets_the_streak(tmp_path: Path) -> None:
    for _ in range(4):
        run(tmp_path, event(tmp_path, None, "same"), "--thresholds", "off")
    assert run(tmp_path, event(tmp_path, None, "other"), "--thresholds", "off")[1] == ""
    for n in range(4):
        out = run(tmp_path, event(tmp_path, None, "same"), "--thresholds", "off")[1]
        assert out == "", n
    assert "[loop-warning]" in message(
        run(tmp_path, event(tmp_path, None, "same"), "--thresholds", "off")[1]
    )


def test_loop_window_is_configurable_and_can_be_disabled(tmp_path: Path) -> None:
    run(
        tmp_path,
        event(tmp_path, None, "x", session="w3"),
        "--thresholds",
        "off",
        "--loop-window",
        "3",
    )
    run(
        tmp_path,
        event(tmp_path, None, "x", session="w3"),
        "--thresholds",
        "off",
        "--loop-window",
        "3",
    )
    third = run(
        tmp_path,
        event(tmp_path, None, "x", session="w3"),
        "--thresholds",
        "off",
        "--loop-window",
        "3",
    )
    assert "3 tool calls" in message(third[1])
    for _ in range(8):
        out = run(
            tmp_path,
            event(tmp_path, None, "x", session="w0"),
            "--thresholds",
            "off",
            "--loop-window",
            "0",
        )[1]
        assert out == ""


def test_nudge_and_loop_warning_combine_in_one_message(tmp_path: Path) -> None:
    t = transcript(tmp_path, 130_000)
    for _ in range(4):
        run(tmp_path, event(tmp_path, t, "same"), "--thresholds", "off")
    out = message(run(tmp_path, event(tmp_path, t, "same"), "--thresholds", "120000")[1])
    assert "[ctx-nudge]" in out and "[loop-warning]" in out


def test_description_field_does_not_hide_a_loop(tmp_path: Path) -> None:
    for n in range(2):
        ev = event(tmp_path, None, "echo same", session="d")
        ev["tool_input"]["description"] = f"variant {n}"
        assert run(tmp_path, ev, "--thresholds", "off", "--loop-window", "3")[1] == ""
    ev = event(tmp_path, None, "echo same", session="d")
    ev["tool_input"]["description"] = "another wording"
    assert "[loop-warning]" in message(
        run(tmp_path, ev, "--thresholds", "off", "--loop-window", "3")[1]
    )


PM = ("--role", "pm", "--thresholds", "300000,350000")


def test_pm_nudge_names_the_continuation_note_and_never_the_worker_advice(tmp_path: Path) -> None:
    out = message(run(tmp_path, event(tmp_path, transcript(tmp_path, 310_000)), *PM)[1])
    assert "310K" in out and "300K" in out and ".tmp/pm-state/pm-continuation.md" in out
    for needed in (
        "authoritative snapshot",
        "decisions",
        "Gemini jobs",
        "pending user questions",
        "next steps",
    ):
        assert needed in out, needed
    assert "370K" in out and "PreCompact" in out and "mechanical snapshot" in out
    assert "Nothing was compacted." in out
    assert "retire" not in out.lower() and "task file" not in out


def test_pm_nudge_fires_once_per_threshold_and_names_a_configured_compaction_point(
    tmp_path: Path,
) -> None:
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 250_000)), *PM)[1] == ""
    assert "300K" in message(run(tmp_path, event(tmp_path, transcript(tmp_path, 305_000)), *PM)[1])
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 320_000)), *PM)[1] == ""
    out = message(
        run(
            tmp_path,
            event(tmp_path, transcript(tmp_path, 355_000)),
            *PM,
            "--compaction-near",
            "385000",
        )[1]
    )
    assert "350K" in out and "385K" in out and "370K" not in out


def test_pm_nudge_rearms_after_compaction_in_the_same_session(tmp_path: Path) -> None:
    # Compaction keeps the session id; once the context drops below a threshold it must fire again.
    assert "300K" in message(run(tmp_path, event(tmp_path, transcript(tmp_path, 305_000)), *PM)[1])
    assert "350K" in message(run(tmp_path, event(tmp_path, transcript(tmp_path, 355_000)), *PM)[1])
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 60_000)), *PM)[1] == ""
    assert "300K" in message(run(tmp_path, event(tmp_path, transcript(tmp_path, 301_000)), *PM)[1])
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 310_000)), *PM)[1] == ""
    assert "350K" in message(run(tmp_path, event(tmp_path, transcript(tmp_path, 351_000)), *PM)[1])


def test_pm_role_is_silent_in_a_cli_worker_and_worker_role_still_nudges(tmp_path: Path) -> None:
    t = transcript(tmp_path, 355_000)
    assert run(tmp_path, event(tmp_path, t), *PM, worker_env=True)[1] == ""
    worker = run(tmp_path, event(tmp_path, t), "--thresholds", "120000", worker_env=True)
    assert "120K" in message(worker[1])


def test_partial_drop_rearms_only_the_thresholds_above_the_context(tmp_path: Path) -> None:
    args = ("--thresholds", "120000,150000")
    run(tmp_path, event(tmp_path, transcript(tmp_path, 155_000)), *args)
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 130_000)), *args)[1] == ""
    assert run(tmp_path, event(tmp_path, transcript(tmp_path, 140_000)), *args)[1] == ""
    assert "150K" in message(
        run(tmp_path, event(tmp_path, transcript(tmp_path, 151_000)), *args)[1]
    )


def test_pm_loop_warning_defaults_to_four_and_only_warns(tmp_path: Path) -> None:
    for n in range(1, 4):
        assert run(tmp_path, event(tmp_path, None, "poll"), "--role", "pm")[1] == "", n
    fourth = run(tmp_path, event(tmp_path, None, "poll"), "--role", "pm")
    out = message(fourth[1])
    assert fourth[0] == 0 and "4 tool calls" in out
    assert "polling" in out and "supervisor" in out and "task notification" in out
    assert run(tmp_path, event(tmp_path, None, "poll"), "--role", "pm")[1] == ""


def test_pm_loop_window_can_be_overridden_or_disabled(tmp_path: Path) -> None:
    args = ("--role", "pm", "--loop-window", "2")
    run(tmp_path, event(tmp_path, None, "a"), *args)
    assert "2 tool calls" in message(run(tmp_path, event(tmp_path, None, "a"), *args)[1])
    for _ in range(8):
        out = run(
            tmp_path,
            event(tmp_path, None, "b", session="off"),
            "--role",
            "pm",
            "--loop-window",
            "0",
        )
        assert out[1] == ""


def test_worker_defaults_are_unchanged_by_the_pm_role(tmp_path: Path) -> None:
    for _ in range(4):
        assert run(tmp_path, event(tmp_path, None, "same"), "--thresholds", "off")[1] == ""
    out = message(run(tmp_path, event(tmp_path, None, "same"), "--thresholds", "off")[1])
    assert "5 tool calls" in out and "polling" not in out and "change approach" in out
    nudge = message(
        run(
            tmp_path,
            event(tmp_path, transcript(tmp_path, 175_000), session="w"),
            "--thresholds",
            "170000",
        )[1]
    )
    assert "task file" in nudge and "pm-continuation" not in nudge and "PreCompact" not in nudge
    explicit = message(
        run(
            tmp_path,
            event(tmp_path, transcript(tmp_path, 175_000), session="w2"),
            "--role",
            "worker",
            "--thresholds",
            "170000",
        )[1]
    )
    assert explicit == nudge


def test_pm_and_worker_state_do_not_collide(tmp_path: Path) -> None:
    t = transcript(tmp_path, 320_000)
    pm = run(tmp_path, event(tmp_path, t, session="same"), *PM)
    worker = run(tmp_path, event(tmp_path, t, session="same"), "--thresholds", "300000")
    assert pm[1] != "" and worker[1] != ""
    assert (tmp_path / "state" / "pm-same.json").exists() and (
        tmp_path / "state" / "same.json"
    ).exists()
    assert (
        run(tmp_path, event(tmp_path, t, session="same"), *PM)[1] == ""
    )  # per-session state still dedupes


def test_inbox_message_is_delivered_once_to_the_worker(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "s1.md").write_text("Replace item 4: batch the removal.", encoding="utf-8")
    args = ("--thresholds", "off", "--inbox-dir", str(inbox))
    rc, out, _ = run(tmp_path, event(tmp_path, None), *args)
    assert rc == 0
    assert "[pm-message]" in message(out) and "batch the removal" in message(out)
    assert list(inbox.iterdir()) == []  # claimed and removed: never delivered twice
    rc, out, _ = run(tmp_path, event(tmp_path, None, command="pwd"), *args)
    assert rc == 0 and out == ""


def test_inbox_is_per_session_and_skipped_by_the_pm_role(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "other.md").write_text("not for s1", encoding="utf-8")
    (inbox / "s1.md").write_text("for s1", encoding="utf-8")
    rc, out, _ = run(
        tmp_path,
        event(tmp_path, None),
        "--role",
        "pm",
        "--thresholds",
        "off",
        "--loop-window",
        "0",
        "--inbox-dir",
        str(inbox),
    )
    assert rc == 0 and out == "" and (inbox / "s1.md").exists()
    rc, out, _ = run(
        tmp_path, event(tmp_path, None), "--thresholds", "off", "--inbox-dir", str(inbox)
    )
    assert message(out).endswith("for s1") and (inbox / "other.md").exists()


def test_empty_or_oversized_inbox_messages(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "s1.md").write_text("  \n", encoding="utf-8")
    args = ("--thresholds", "off", "--inbox-dir", str(inbox))
    rc, out, _ = run(tmp_path, event(tmp_path, None), *args)
    assert rc == 0 and out == "" and list(inbox.iterdir()) == []
    (inbox / "s1.md").write_text("x" * 30000, encoding="utf-8")
    rc, out, _ = run(tmp_path, event(tmp_path, None, command="pwd"), *args)
    assert "[truncated by the hook]" in message(out) and len(message(out)) < 21000


def test_parallel_hooks_deliver_the_message_exactly_once(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "s1.md").write_text("once", encoding="utf-8")
    data = json.dumps(event(tmp_path, None))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_DISABLE_AUTO_MEMORY"}
    cmd = [sys.executable, "-B", "-I", str(HOOK), "--state-dir", str(tmp_path / "state")]
    cmd += ["--thresholds", "off", "--loop-window", "0", "--inbox-dir", str(inbox)]
    procs = [
        subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=tmp_path, env=env
        )
        for _ in range(6)
    ]
    outs = [p.communicate(data, timeout=30)[0] for p in procs]
    assert sum("[pm-message]" in o for o in outs) == 1
