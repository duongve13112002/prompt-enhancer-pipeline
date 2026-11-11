"""Scripts must work on a clean checkout where .tmp/cli-jobs and .tmp/pm-state do not exist yet."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_registry_lock_and_save_create_missing_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw = load("cli_worker")
    registry = tmp_path / "missing" / "pm-state" / "cli-workers.json"
    monkeypatch.setattr(cw, "REGISTRY", registry)
    monkeypatch.setattr(cw, "LOCK", registry.with_suffix(".lock"))
    assert cw.load() == {}
    with cw.RegistryLock():
        cw.save({"w": {"model": "haiku"}})
    assert cw.load() == {"w": {"model": "haiku"}}
    assert not cw.LOCK.exists()


@pytest.mark.parametrize("name", ["cache_check", "usage_breakdown"])
def test_log_scanners_treat_missing_jobs_dir_as_empty(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load(name)
    monkeypatch.setattr(mod, "JOBS", tmp_path / "no-such-dir")
    transcript = tmp_path / "pm.jsonl"
    transcript.write_text("", encoding="utf-8")
    args = ["--date", "2026-10-10", str(transcript)] if name == "usage_breakdown" else []
    monkeypatch.setattr(sys, "argv", [name, *args])
    mod.main()
    capsys.readouterr()


def test_context_audit_and_metrics_handle_missing_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca = load("context_audit")
    monkeypatch.setattr(ca, "JOBS", tmp_path / "no-such-dir")
    with pytest.raises(SystemExit, match="no worker log"):
        ca.find_log(None, None)
    assert ca.usage_evidence(5) == ({}, {})


def test_gemini_workspace_archive_allowlist() -> None:
    text = (TOOLS / "gemini-ws.ps1").read_text(encoding="utf-8")
    match = re.search(r"\$allow = (.+)", text)
    assert match is not None
    allowed = set(re.findall(r"'([^']+)'", match.group(1)))
    assert {"src", "tests", "tools", "docs", "tasks", "contracts", "prompts"} <= allowed
    assert not allowed & {".secrets", "models", ".venv", ".tmp"}
