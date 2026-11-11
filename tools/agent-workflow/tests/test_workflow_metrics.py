"""Unit tests for workflow_metrics.py on a synthetic registry and worker transcripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "workflow_metrics.py"
spec = importlib.util.spec_from_file_location("workflow_metrics", SCRIPT)
assert spec is not None and spec.loader is not None
wm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wm)

WEIGHTS = (1.0, 0.1, 2.0)


def call(msg_id: str, fresh: int, read: int, write: int, tools: list[dict[str, Any]]) -> str:
    usage = {
        "input_tokens": fresh,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }
    content = [{"type": "tool_use", "name": n["name"], "input": n["input"]} for n in tools]
    message = {"id": msg_id, "role": "assistant", "usage": usage, "content": content}
    return json.dumps({"type": "assistant", "message": message})


def write_log(path: Path, lines: list[str], cost: float | None) -> None:
    if cost is not None:
        lines = [*lines, json.dumps({"type": "result", "total_cost_usd": cost})]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(wm, "JOBS", tmp_path / "jobs")
    return tmp_path / "jobs"


def entry(created: str, **extra: Any) -> dict[str, Any]:
    return {"model": "haiku", "effort": "low", "status": "retired", "created": created, **extra}


def test_tokens_cost_and_resume_miss(jobs: Path) -> None:
    same = {"name": "Read", "input": {"file_path": "a"}}
    write_log(
        jobs / "w1" / "001.jsonl",
        [call("m1", 2, 0, 10_000, [same]), call("m2", 2, 10_000, 50, [])],
        0.5,
    )
    # Second turn: the first call reads far less than the previous context (cache expired).
    write_log(
        jobs / "w1" / "002.jsonl",
        [call("m3", 2, 0, 11_000, []), call("m3", 9, 9, 9, [])],
        0.9,
    )
    row = wm.analyse_worker("w1", entry("2026-10-10T10:00:00"), WEIGHTS)
    assert row is not None
    assert row["turns"] == 2 and row["calls"] == 3  # duplicate message id m3 counted once
    assert row["first_call"] == 10_002
    assert row["input_eq"] == pytest.approx((2 + 20_000) + (2 + 1000 + 100) + (2 + 22_000))
    assert row["cost_usd"] == 0.9  # cumulative field: the largest value, not the sum
    assert row["resume_misses"] == 1 and row["call_misses"] == 0 and row["miss_write"] == 11_000


def test_guard_signals_replayed_and_blocks_counted(jobs: Path) -> None:
    by_name = {"name": "Bash", "input": {"command": "taskkill /IM python.exe"}}
    by_pid = {"name": "Bash", "input": {"command": "taskkill /PID 123"}}
    same = {"name": "Grep", "input": {"pattern": "x"}}
    blocked = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "content": "hook error: BLOCKED by process_guard: x"}
            ]
        },
    }
    lines = [
        call("a", 1, 0, 130_000, [by_name, by_pid]),
        *[call(f"s{i}", 1, 130_000, 1, [same]) for i in range(5)],
        json.dumps(blocked),
    ]
    write_log(jobs / "w2" / "001.jsonl", lines, None)
    row = wm.analyse_worker("w2", entry("2026-10-10T11:00:00"), WEIGHTS)
    assert row is not None
    assert row["would_block"] == 1 and row["blocked"] == 1
    assert row["nudges"] == 1  # 130K crosses the first default threshold once per session
    assert row["loops"] == 1  # five identical Grep calls reach the default window of 5
    assert row["cost_usd"] is None


def test_worker_settings_override_defaults(jobs: Path) -> None:
    same = {"name": "Grep", "input": {"pattern": "x"}}
    write_log(jobs / "w3" / "001.jsonl", [call(f"s{i}", 1, 100, 1, [same]) for i in range(3)], None)
    hooks = {"process_guard": True, "ctx_nudge": "50,500", "loop_window": 3}
    row = wm.analyse_worker("w3", entry("2026-10-10T12:00:00", hooks=hooks), WEIGHTS)
    assert row is not None and row["guarded"] is True
    assert row["loops"] == 1 and row["nudges"] == 1


def test_worker_without_logs_is_skipped(jobs: Path) -> None:
    assert wm.analyse_worker("none", entry("2026-10-10T12:00:00"), WEIGHTS) is None


def rows_for_select() -> list[dict[str, Any]]:
    def mk(name: str, created: str, guarded: bool, status: str = "retired") -> dict[str, Any]:
        return {
            "worker": name,
            "created": created,
            "guarded": guarded,
            "status": status,
            "first_call": 1,
        }

    return [
        mk("a", "2026-10-10T09:00:00", False),
        mk("b", "2026-10-10T10:00:00", False),
        mk("c", "2026-10-10T11:00:00", True),
        mk("d", "2026-10-10T12:00:00", True, "busy"),
    ]


def names(rows: list[dict[str, Any]]) -> list[str]:
    return [r["worker"] for r in rows]


def test_select_filters_window_guard_and_last() -> None:
    rows = rows_for_select()
    parse = wm.parse_ts
    assert names(wm.select(rows, parse("2026-10-10T10:00:00"), None, "any", None)) == [
        "b",
        "c",
        "d",
    ]
    assert names(wm.select(rows, None, parse("2026-10-10T10:00:00"), "any", None)) == ["a"]
    assert names(wm.select(rows, None, None, "yes", None)) == ["c", "d"]
    assert names(wm.select(rows, None, None, "no", 1)) == ["b"]
    assert names(wm.select(rows, None, None, "any", 2)) == [
        "b",
        "c",
    ]  # busy workers are not completed


def test_parse_ts_rejects_garbage() -> None:
    with pytest.raises(SystemExit):
        wm.parse_ts("yesterday")
