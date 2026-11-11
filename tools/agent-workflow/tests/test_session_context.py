"""Unit tests for hooks/session_context.py (SessionStart agreements injection and document budgets)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session_context.py"


@pytest.fixture
def hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("session_context", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / ".tmp" / "pm-state"
    state.mkdir(parents=True)
    budgets = tmp_path / "doc_budgets.json"
    budgets.write_text(
        json.dumps(
            {"project": {"CLAUDE.md": 10}, "pm_note": 1000, "memory": {"index": 5, "topic": 50}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.setattr(module, "BUDGETS", budgets)
    config = tmp_path / "memory_hooks.json"
    config.write_text(module.CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(module, "CONFIG", config)
    monkeypatch.delenv(module.WORKER_ENV_FLAG, raising=False)
    (state / "cli-workers.json").write_text(
        json.dumps({"workers": {"w1": {"session_id": "s-worker", "status": "busy"}}}),
        encoding="utf-8",
    )
    (state / "pm-continuation.md").write_text(
        "# PM note\n## USER AGREEMENTS 2026-10-11\n- ask before CI\n## History\n- old detail\n"
        "## NOW\n- worker x busy\n## AUTO SNAPSHOT t\n- git\n",
        encoding="utf-8",
    )
    return module


def run(
    hook, monkeypatch: pytest.MonkeyPatch, capsys, event: object, argv: tuple[str, ...] = ()
) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert hook.main(list(argv)) == 0
    out = capsys.readouterr().out
    if not out:
        return ""
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "SessionStart"
    return data["additionalContext"]


def test_compact_injects_only_agreements_and_now(hook, monkeypatch, capsys, tmp_path) -> None:
    text = run(
        hook,
        monkeypatch,
        capsys,
        {"session_id": "s-pm", "source": "compact", "transcript_path": str(tmp_path / "t.jsonl")},
    )
    assert "Session start (compact)" in text
    assert "- ask before CI" in text and "- worker x busy" in text
    assert "old detail" not in text and "AUTO SNAPSHOT" not in text


def test_worker_sessions_are_skipped(hook, monkeypatch, capsys) -> None:
    assert run(hook, monkeypatch, capsys, {"session_id": "s-worker", "source": "compact"}) == ""
    monkeypatch.setenv(hook.WORKER_ENV_FLAG, "1")
    assert run(hook, monkeypatch, capsys, {"session_id": "s-pm", "source": "startup"}) == ""


def test_over_budget_documents_are_listed(hook, monkeypatch, capsys, tmp_path) -> None:
    (tmp_path / "CLAUDE.md").write_text("x" * 11, encoding="utf-8")
    memory = tmp_path / "proj" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("123456", encoding="utf-8")
    (memory / "small.md").write_text("ok", encoding="utf-8")
    text = run(
        hook,
        monkeypatch,
        capsys,
        {
            "session_id": "s-pm",
            "source": "resume",
            "transcript_path": str(tmp_path / "proj" / "t.jsonl"),
        },
    )
    assert "CLAUDE.md 11 > 10" in text and "memory/MEMORY.md 6 > 5" in text
    budget_line = text.split("over their size budget", 1)[1]
    assert "small.md" not in budget_line and "pm-continuation" not in budget_line


def test_long_section_is_cut_and_flagged(hook, monkeypatch, capsys) -> None:
    settings = json.loads(hook.CONFIG.read_text(encoding="utf-8"))
    settings["note_inject_chars"] = 30
    hook.CONFIG.write_text(json.dumps(settings), encoding="utf-8")
    text = run(hook, monkeypatch, capsys, {"session_id": "s-pm", "source": "compact"})
    assert "[cut at 30 characters" in text


def test_missing_note_and_bad_input_still_exit_zero(hook, monkeypatch, capsys) -> None:
    (hook.STATE / "pm-continuation.md").unlink()
    text = run(hook, monkeypatch, capsys, {"session_id": "s-pm", "source": "startup"})
    assert "has no agreements or NOW section" in text
    monkeypatch.setattr("sys.stdin", io.StringIO("[1"))
    assert hook.main() == 0


DECISIONS = (
    "# Decisions Index\n\n| Id | Date | Rule | Where |\n|---|---|---|---|\n"
    "| 9 | 2026-01-09 | nine **bold** rule | x |\n"
    "| 10 | 2026-01-10 | ten rule " + "long " * 40 + "| x |\n"
    "| 10a | 2026-01-11 | ten-a rule | x |\n"
    "| 2 | 2026-01-02 | two rule | x |\n| not a row | text |\n"
)


def write_decisions(hook, text: str = DECISIONS) -> None:
    (hook.ROOT / "tasks").mkdir(exist_ok=True)
    (hook.ROOT / "tasks" / "DECISIONS_INDEX.md").write_text(text, encoding="utf-8")


def test_decision_index_lists_newest_rows_clipped(hook, monkeypatch, capsys) -> None:
    write_decisions(hook)
    text = run(
        hook, monkeypatch, capsys, {"session_id": "s-pm", "source": "startup"}, ("--decisions", "3")
    )
    lines = [line for line in text.split("\n") if line.startswith("- 1") or line.startswith("- 9")]
    assert [line.split(" ")[1] for line in lines] == ["10a", "10", "9"]
    assert "- 2 (" not in text and "recall.py search" in text
    assert "nine bold rule" in text and "..." in lines[1] and len(lines[1]) < 140
    assert "- ask before CI" in text  # agreements unchanged


def test_decision_index_default_size_and_off_switch(hook, monkeypatch, capsys) -> None:
    rows = "".join(f"| {n} | 2026-01-01 | rule {n} | x |\n" for n in range(1, 21))
    write_decisions(hook, rows)
    event = {"session_id": "s-pm", "source": "startup"}
    text = run(hook, monkeypatch, capsys, event)
    assert text.count("\n- 20 (") == 1 and "- 13 (" in text and "- 12 (" not in text
    assert "Newest decisions" not in run(hook, monkeypatch, capsys, event, ("--decisions", "0"))


@pytest.mark.parametrize(
    "argv", [("--decisions", "abc"), ("--decisions",), ("--decisions", "-5"), ("--bogus",)]
)
def test_bad_decision_option_never_fails_the_hook(hook, monkeypatch, capsys, argv) -> None:
    write_decisions(hook)
    text = run(hook, monkeypatch, capsys, {"session_id": "s-pm", "source": "startup"}, argv)
    assert "Session start" in text


@pytest.mark.parametrize(
    "content", [None, "", "no table here\n| x | y |\n", "| 1 | not-a-date | r |\n", "�\x00|"]
)
def test_missing_or_malformed_decision_index_is_omitted(hook, monkeypatch, capsys, content) -> None:
    if content is not None:
        write_decisions(hook, content)
    text = run(hook, monkeypatch, capsys, {"session_id": "s-pm", "source": "startup"})
    assert "Newest decisions" not in text and "- ask before CI" in text
