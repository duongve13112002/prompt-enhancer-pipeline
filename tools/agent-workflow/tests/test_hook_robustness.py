"""Regression tests for the hook input/output hardening (stdin decoding, BOM, atomic writes, worker skip)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
NON_ASCII = "D:/Projekte/Đức/Müller"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_robust", HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocaleStdin:
    """A stdin whose text layer fails like a legacy code page on non-ASCII input; only `.buffer` is usable."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)

    def read(self, *_a: object) -> str:
        raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "character maps to <undefined>")


def locale_stdin(event: object, bom: bool = False) -> LocaleStdin:
    raw = json.dumps(event, ensure_ascii=False).encode("utf-8")
    return LocaleStdin((b"\xef\xbb\xbf" if bom else b"") + raw)


@pytest.fixture(autouse=True)
def no_worker_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite may itself run inside a cli_worker, which sets the worker switch.
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)


@pytest.mark.parametrize(
    "name", ["process_guard", "ctx_nudge", "session_context", "precompact_snapshot"]
)
def test_every_hook_decodes_stdin_as_utf8_and_strips_a_bom(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load(name)
    monkeypatch.setattr("sys.stdin", locale_stdin({"cwd": NON_ASCII}, bom=True))
    assert json.loads(module.read_event())["cwd"] == NON_ASCII
    monkeypatch.setattr("sys.stdin", LocaleStdin(b'{"x": "\xff\xfe"}'))
    assert "x" in json.loads(module.read_event())  # undecodable bytes are replaced, never raised
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"y": 1}')
    )  # a double without a byte layer still works
    assert json.loads(module.read_event()) == {"y": 1}


def test_process_guard_still_blocks_when_the_event_has_non_ascii_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = load("process_guard")
    monkeypatch.setattr(guard, "LOG", tmp_path / "guard.log", raising=False)
    event = {"tool_name": "Bash", "tool_input": {"command": f"cd '{NON_ASCII}' && pkill python"}}
    monkeypatch.setattr("sys.stdin", locale_stdin(event))
    assert guard.main() == 2
    assert "BLOCKED by process_guard" in capsys.readouterr().err


@pytest.mark.parametrize(
    "event",
    [
        [1, 2],
        "text",
        None,
        {"tool_name": "Bash", "tool_input": ["pkill python"]},
        {"tool_name": "Bash"},
    ],
)
def test_process_guard_ignores_malformed_events_without_a_traceback(
    event: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = load("process_guard")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert guard.main() == 0


def test_ctx_nudge_fires_for_a_non_ascii_event_and_leaves_no_temp_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nudge = load("ctx_nudge")
    transcript = tmp_path / "t.jsonl"
    usage = {"input_tokens": 200000}
    transcript.write_text(
        json.dumps({"message": {"role": "assistant", "usage": usage}}) + "\n", encoding="utf-8"
    )
    event = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": f"ls {NON_ASCII}"},
        "transcript_path": str(transcript),
    }
    state = tmp_path / "state"
    monkeypatch.setattr(
        "sys.argv", ["ctx_nudge.py", "--thresholds", "170000", "--state-dir", str(state)]
    )
    monkeypatch.setattr("sys.stdin", locale_stdin(event))
    assert nudge.main() == 0
    assert "[ctx-nudge]" in capsys.readouterr().out
    assert [p.name for p in state.iterdir()] == ["s1.json"]


def test_ctx_nudge_state_is_replaced_atomically_and_a_failed_write_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nudge = load("ctx_nudge")
    target = tmp_path / "s.json"
    nudge.save_state(target, {"fired": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"fired": 1}

    def boom(src: object, dst: object) -> None:
        raise PermissionError("held open")

    monkeypatch.setattr(nudge.os, "replace", boom)
    nudge.save_state(target, {"fired": 2})  # must not raise
    assert json.loads(target.read_text(encoding="utf-8")) == {"fired": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


@pytest.mark.parametrize(
    "content", ['{"fired": "two", "recent": 5}', '{"fired": null}', "[1]", "{broken", ""]
)
def test_ctx_nudge_damaged_state_degrades_to_nothing_fired(content: str, tmp_path: Path) -> None:
    nudge = load("ctx_nudge")
    path = tmp_path / "s.json"
    path.write_text(content, encoding="utf-8")
    state = nudge.load_state(path)
    assert isinstance(state.get("fired", 0), int) and isinstance(state.get("recent", []), list)
    assert nudge.nudge_lines([100], 150, state)  # fires instead of raising
    assert nudge.loop_lines(2, "d", state, "Bash") == []


@pytest.mark.parametrize("value", ["many", [1], {"n": 1}])
def test_ctx_nudge_skips_a_malformed_usage_block_and_uses_the_previous_message(
    tmp_path: Path, value: object
) -> None:
    nudge = load("ctx_nudge")
    path = tmp_path / "t.jsonl"
    good = {"message": {"role": "assistant", "usage": {"input_tokens": 321}}}
    bad = {"message": {"role": "assistant", "usage": {"input_tokens": value}}}
    path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    assert nudge.context_tokens(path) == 321


def make_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    hook = load("session_context")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(hook, "ROOT", tmp_path)
    monkeypatch.setattr(hook, "STATE", state)
    budgets = tmp_path / "doc_budgets.json"
    budgets.write_text(json.dumps({"project": {}, "pm_note": 100000}), encoding="utf-8")
    monkeypatch.setattr(hook, "BUDGETS", budgets)
    return hook


def injected(
    hook: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"session_id": "pm", "source": "compact"}))
    )
    assert hook.main() == 0
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


def test_note_saved_with_a_bom_still_injects_its_first_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = make_session(tmp_path, monkeypatch)
    (hook.STATE / hook.NOTE_NAME).write_bytes(
        b"\xef\xbb\xbf## USER AGREEMENTS\r\n- ask before CI\r\n## History\r\n- old\r\n"
    )
    text = injected(hook, monkeypatch, capsys)
    assert "ask before CI" in text and "old" not in text and "\ufeff" not in text


def test_note_with_a_stray_byte_still_injects_the_agreements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = make_session(tmp_path, monkeypatch)
    (hook.STATE / hook.NOTE_NAME).write_bytes(b"# n\n## NOW\n- caf\xe9 busy\n")
    assert "busy" in injected(hook, monkeypatch, capsys)


def test_note_without_agreement_headings_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = make_session(tmp_path, monkeypatch)
    (hook.STATE / hook.NOTE_NAME).write_text("# n\n## Other\n- x\n", encoding="utf-8")
    assert "no agreements or NOW section" in injected(hook, monkeypatch, capsys)


@pytest.mark.parametrize(
    "spec",
    [{"project": {"CLAUDE.md": "big"}}, {"project": []}, {"memory": 5}, {"pm_note": "x"}, []],
)
def test_malformed_budget_file_cannot_drop_the_agreements(
    spec: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = make_session(tmp_path, monkeypatch)
    (hook.STATE / hook.NOTE_NAME).write_text("## NOW\n- keep me\n", encoding="utf-8")
    hook.BUDGETS.write_text(json.dumps(spec), encoding="utf-8")
    assert "keep me" in injected(hook, monkeypatch, capsys)


def test_a_failing_budget_check_still_injects_the_agreements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = make_session(tmp_path, monkeypatch)
    (hook.STATE / hook.NOTE_NAME).write_text("## NOW\n- keep me\n", encoding="utf-8")

    def boom(_memory: object) -> list[str]:
        raise RuntimeError("x")

    monkeypatch.setattr(hook, "over_budget", boom)
    text = injected(hook, monkeypatch, capsys)
    assert "keep me" in text and "budget check failed: RuntimeError" in text


def make_precompact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    hook = load("precompact_snapshot")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(hook, "STATE", state)
    monkeypatch.setattr(hook, "NOTE", state / "pm-continuation.md")
    monkeypatch.setattr(hook, "git_status", lambda: ["## main"])
    monkeypatch.setattr(hook, "worker_registry", lambda: {})
    monkeypatch.setattr(hook.time, "sleep", lambda _s: None)
    return hook


def run_precompact(hook: ModuleType, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "pm"})))
    return hook.main()


def test_a_worker_rooted_in_the_pm_checkout_does_not_write_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = make_precompact(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    assert run_precompact(hook, monkeypatch) == 0
    assert not hook.NOTE.exists()


def test_snapshot_keeps_a_bom_note_stray_bytes_and_unicode_line_separators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = make_precompact(tmp_path, monkeypatch)
    hook.NOTE.write_bytes(
        b"\xef\xbb\xbf## USER AGREEMENTS\r\n- a\xe9 b\xe2\x80\xa8c\r\n## AUTO SNAPSHOT old\r\n- stale\r\n"
    )
    assert run_precompact(hook, monkeypatch) == 0
    raw = hook.NOTE.read_bytes()
    assert raw.startswith(
        b"## USER AGREEMENTS\n- a\xe9 b\xe2\x80\xa8c\n"
    )  # BOM gone, bytes and U+2028 intact
    assert b"stale" not in raw and b"\r" not in raw and raw.count(b"## AUTO SNAPSHOT") == 1


def test_snapshot_survives_a_note_held_open_for_a_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = make_precompact(tmp_path, monkeypatch)
    hook.NOTE.write_text("## NOW\n- x\n", encoding="utf-8")
    real, calls = os.replace, []

    def flaky(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("editor holds the note")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(hook.os, "replace", flaky)
    assert run_precompact(hook, monkeypatch) == 0
    assert len(calls) == 3 and "## AUTO SNAPSHOT" in hook.NOTE.read_text(encoding="utf-8")
    assert [p.name for p in hook.STATE.iterdir()] == [hook.NOTE.name]


def test_snapshot_falls_back_to_an_in_place_write_when_replace_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = make_precompact(tmp_path, monkeypatch)
    hook.NOTE.write_text("## NOW\n- x\n", encoding="utf-8")

    def blocked(src: object, dst: object) -> None:
        raise PermissionError("held")

    monkeypatch.setattr(hook.os, "replace", blocked)
    assert run_precompact(hook, monkeypatch) == 0
    text = hook.NOTE.read_text(encoding="utf-8")
    assert "- x" in text and "## AUTO SNAPSHOT" in text
    assert [p.name for p in hook.STATE.iterdir()] == [hook.NOTE.name]


def test_an_unexpected_error_never_blocks_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = make_precompact(tmp_path, monkeypatch)

    def boom(_snapshot: str) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(hook, "write_note", boom)
    assert run_precompact(hook, monkeypatch) == 0
    assert "ValueError" in capsys.readouterr().err


def test_precompact_snapshot_honours_the_state_dir_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_WORKFLOW_STATE_DIR", str(tmp_path / "state"))
    module = load("precompact_snapshot")
    assert (
        tmp_path / "state" == module.STATE
        and tmp_path / "state" / "pm-continuation.md" == module.NOTE
    )
    monkeypatch.delenv("AGENT_WORKFLOW_STATE_DIR")
    assert load("precompact_snapshot").STATE.parts[-2:] == (".tmp", "pm-state")


def test_ctx_nudge_skips_a_non_finite_usage_and_reads_past_a_line_separator(tmp_path: Path) -> None:
    module = load("ctx_nudge")
    good = {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 41}}}
    odd = {
        "type": "assistant",
        "message": {"role": "assistant", "usage": {"input_tokens": 1}, "content": "a\u2028b"},
    }
    infinite = '{"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": Infinity}}}'
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join([json.dumps(good), json.dumps(odd, ensure_ascii=False), infinite]) + "\n",
        encoding="utf-8",
    )
    assert (
        module.context_tokens(path) == 1
    )  # the Infinity line is skipped; the U+2028 line stays one line
    path.write_text(json.dumps(good) + "\n" + infinite + "\n", encoding="utf-8")
    assert module.context_tokens(path) == 41


def test_session_context_does_not_start_a_section_at_a_raw_line_separator() -> None:
    module = load("session_context")
    text = "## USER AGREEMENTS\nkeep \u2028## NOT A HEADING\nstill agreements\n## OTHER\nskipped\n"
    kept = module.note_sections(text, ("## USER AGREEMENTS", "## NOW"), 8000)
    assert "NOT A HEADING" in kept and "still agreements" in kept and "skipped" not in kept


def test_stream_log_readers_skip_non_object_lines_and_keep_u2028_lines(tmp_path: Path) -> None:
    tools = HOOKS.parent
    event = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": "haiku",
            "usage": {"input_tokens": 3, "cache_read_input_tokens": 4},
            "content": "x\u2028y",
        },
    }
    log = tmp_path / "001.jsonl"
    log.write_text(
        "\n".join(["[1]", "null", '"s"', json.dumps(event, ensure_ascii=False), "[" * 100000])
        + "\n",
        encoding="utf-8",
    )

    def tool(name: str) -> ModuleType:
        spec = importlib.util.spec_from_file_location(f"{name}_robust", tools / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    assert tool("cache_check").calls(log) == [{"input": 3, "read": 4, "write": 0}]
    assert [m["id"] for _e, m in tool("usage_breakdown").assistant_messages(log)] == ["m1"]
    assert len(tool("workflow_metrics").read_events(log)) == 1
