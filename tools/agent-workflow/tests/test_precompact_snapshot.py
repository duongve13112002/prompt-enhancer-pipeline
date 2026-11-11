"""Unit tests for hooks/precompact_snapshot.py (PM continuation snapshot before compaction)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "precompact_snapshot.py"


@pytest.fixture()
def hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("precompact_snapshot", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / ".tmp" / "pm-state"
    state.mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.setattr(module, "NOTE", state / "pm-continuation.md")
    monkeypatch.setattr(module, "git_status", lambda: ["## main...origin/main"])
    # The suite may itself run inside a cli_worker, which sets the worker switch the hook skips on.
    monkeypatch.delenv(module.WORKER_ENV_FLAG, raising=False)
    (state / "cli-workers.json").write_text(
        json.dumps(
            {
                "w-busy": {
                    "session_id": "s-worker",
                    "status": "busy",
                    "model": "sonnet",
                    "label": "T1",
                },
                "w-idle": {"session_id": "s-idle", "status": "idle", "model": "opus"},
            }
        ),
        encoding="utf-8",
    )
    (state / "agents.json").write_text(
        json.dumps(
            [
                {"kind": "gemini", "id": "job-open", "label": "review"},
                {"kind": "gemini", "id": "job-done", "label": "scout"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".tmp" / "agy-work" / "_results" / "job-done").mkdir(parents=True)
    return module


def run(hook, monkeypatch: pytest.MonkeyPatch, event: object) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    return hook.main()


def test_pm_session_appends_snapshot(hook, monkeypatch: pytest.MonkeyPatch) -> None:
    hook.NOTE.write_text("# PM note\n", encoding="utf-8")
    assert run(hook, monkeypatch, {"session_id": "s-pm", "trigger": "auto"}) == 0
    text = hook.NOTE.read_text(encoding="utf-8")
    assert text.startswith("# PM note\n")
    assert "trigger=auto" in text
    assert "w-busy (sonnet, T1" in text and "w-idle" not in text
    assert "job-open (review" in text and "job-done" not in text


def test_worker_session_is_skipped(hook, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(hook, monkeypatch, {"session_id": "s-idle", "trigger": "auto"}) == 0
    assert not hook.NOTE.exists()


def test_bad_input_and_missing_registries_still_write(
    hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    (hook.STATE / "cli-workers.json").write_text("{not json", encoding="utf-8")
    (hook.STATE / "agents.json").unlink()
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2]"))
    assert hook.main() == 0
    text = hook.NOTE.read_text(encoding="utf-8")
    assert "trigger=?" in text and "busy cli workers: none" in text


def test_snapshot_replaces_the_previous_one_and_keeps_pm_sections(
    hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook.NOTE.write_text(
        "# PM note\n## USER AGREEMENTS\n- a\n## AUTO SNAPSHOT old (trigger=manual)\n- stale\n"
        "## NOW\n- b\n## AUTO SNAPSHOT older\n- stale2\n",
        encoding="utf-8",
    )
    assert run(hook, monkeypatch, {"session_id": "s-pm", "trigger": "auto"}) == 0
    assert run(hook, monkeypatch, {"session_id": "s-pm", "trigger": "auto"}) == 0
    text = hook.NOTE.read_text(encoding="utf-8")
    assert text.count("## AUTO SNAPSHOT") == 1 and "stale" not in text
    assert "## USER AGREEMENTS\n- a\n## NOW\n- b\n" in text and "trigger=auto" in text
