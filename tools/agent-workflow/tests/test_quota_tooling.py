"""T-026 quota tooling: statusline.py, the file-based Claude and Antigravity sources of usage_snapshot.py, the
`quota-blocks.json` writer and the SessionStart quota line. Payloads are synthetic test doubles shaped like the
documented status-line JSON and a real worker `rate_limit_event` line."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

TOOLS = Path(__file__).resolve().parents[1]


def load(name: str, path: Path) -> Any:
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


sl = load("statusline", TOOLS / "statusline.py")
us = sl.snapshot_module()

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
LATER = NOW + 3600
EARLIER = NOW - 3600


def iso(stamp: float) -> str:
    return us.iso(stamp)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(us, "ROOT", tmp_path)
    monkeypatch.setattr(us, "JOBS", tmp_path / ".tmp" / "cli-jobs")
    return tmp_path


def feed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode("utf-8"))))


def run_statusline(monkeypatch, capsys, payload: object, *argv: str) -> str:
    feed(monkeypatch, payload if isinstance(payload, str) else json.dumps(payload))
    assert sl.main(list(argv)) == 0
    return capsys.readouterr().out.strip()


def claude_payload(five: float = 23.5, week: float = 41.2) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": int(LATER)},
            "seven_day": {"used_percentage": week, "resets_at": int(LATER + 86400)},
        },
    }


# ---- statusline.py -------------------------------------------------------------------------------------------


def test_statusline_records_claude_rate_limits_verbatim(root: Path, monkeypatch, capsys) -> None:
    line = run_statusline(monkeypatch, capsys, claude_payload(), "--source", "claude")
    assert line == "claude five_hour 24% used, seven_day 41% used"
    written = json.loads((root / ".tmp/pm-state/claude-statusline.json").read_text(encoding="utf-8"))
    assert written["rate_limits"] == claude_payload()["rate_limits"]
    assert us.parse_time(written["written_at"]) is not None
    assert not list((root / ".tmp/pm-state").glob("*.tmp"))  # the temp file of the atomic write is gone


def test_statusline_keeps_the_last_reading_when_a_payload_has_none(root: Path, monkeypatch, capsys) -> None:
    run_statusline(monkeypatch, capsys, claude_payload(), "--source", "claude")
    target = root / ".tmp/pm-state/claude-statusline.json"
    before = target.read_bytes()
    for payload in ({"session_id": "s2"}, {"rate_limits": None}):
        line = run_statusline(monkeypatch, capsys, payload, "--source", "claude")
        assert line == "claude: no rate_limits yet"
    # Fix round 4: input that is not one JSON object is malformed, not an absent member.
    for payload in ("", "{broken", "[1]"):
        line = run_statusline(monkeypatch, capsys, payload, "--source", "claude")
        assert line.startswith("claude: input unreadable: ") and "yet" not in line
    assert target.read_bytes() == before
    # An empty object is a reading: every window was dropped after its reset.
    assert run_statusline(monkeypatch, capsys, {"rate_limits": {}}, "--source", "claude") == (
        "claude no limit window"
    )
    assert json.loads(target.read_text(encoding="utf-8"))["rate_limits"] == {}
    assert "not an object" in run_statusline(monkeypatch, capsys, {"rate_limits": [1]}, "--source", "claude")


def test_statusline_records_the_antigravity_quota(root: Path, monkeypatch, capsys) -> None:
    quota = {
        "gemini-weekly": {"remaining_fraction": 0.9378, "reset_time": "2026-07-06T07:50:32Z", "reset_in_seconds": 5},
        "other": {"remaining_amount": 3},
    }
    line = run_statusline(monkeypatch, capsys, {"quota": quota}, "--source", "antigravity")
    assert line == "agy gemini-weekly 94% left, other 3 left"
    written = json.loads((root / ".tmp/pm-state/antigravity-statusline.json").read_text(encoding="utf-8"))
    assert written["quota"] == quota
    assert not (root / ".tmp/pm-state/claude-statusline.json").exists()


def test_statusline_shows_a_refused_config_and_still_exits_0(root: Path, monkeypatch, capsys, tmp_path) -> None:
    raw = json.loads((TOOLS / "codex_profiles.json").read_text(encoding="utf-8"))
    del raw["usage"]["block_percent"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    line = run_statusline(monkeypatch, capsys, claude_payload(), "--source", "claude", "--config", str(bad))
    assert line.startswith("quota not recorded:") and "block_percent" in line
    assert not (root / ".tmp/pm-state/claude-statusline.json").exists()


def test_statusline_runs_as_a_command_and_prints_one_ascii_line() -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run(
        [sys.executable, "-B", "-I", "-X", "faulthandler", str(TOOLS / "statusline.py"), "--source", "claude"],
        input=json.dumps({"session_id": "x", "workspace": {"current_dir": "D:/é"}}).encode("utf-8"),
        capture_output=True,
        timeout=60,
        creationflags=flags,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode("ascii").strip() == "claude: no rate_limits yet"


# ---- Claude sources ------------------------------------------------------------------------------------------


def write_status(path: Path, written: float, member: str, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"written_at": iso(written), member: value}), encoding="utf-8")


def worker_log(jobs: Path, name: str, info: dict[str, Any], mtime: float) -> Path:
    folder = jobs / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "001.jsonl"
    event = {"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u", "session_id": "s"}
    path.write_text(
        json.dumps({"type": "system"}) + "\n" + json.dumps(event) + "\n" + json.dumps({"type": "result"}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def weekly_event(utilization: float, resets: float = LATER + 86400, status: str = "allowed") -> dict[str, Any]:
    # Shape of a live line (.tmp/cli-jobs/t026-opus-dev/001.jsonl:10), with test values.
    return {
        "status": status,
        "resetsAt": int(resets),
        "rateLimitType": "seven_day",
        "unifiedWindows": {"seven_day": {"utilization": utilization, "resetsAt": int(resets), "status": status}},
    }


def test_claude_takes_the_newest_reading_per_window_and_names_its_source(root: Path) -> None:
    status = root / "c.json"
    write_status(status, NOW - 600, "rate_limits", claude_payload(five=12, week=40)["rate_limits"])
    newer = worker_log(us.JOBS, "w1", weekly_event(0.55), NOW - 60)
    worker_log(us.JOBS, "w0", weekly_event(0.10), NOW - 7200)
    snap = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)
    windows = {w["label"]: w for w in snap["windows"]}
    assert snap["status"] == "ok" and snap["unlimited"] is False
    assert windows["weekly"]["used_percent"] == 55.0
    assert windows["weekly"]["source"] == f"worker rate_limit_event {newer}"
    assert windows["weekly"]["window_minutes"] == 10080
    assert windows["5h"]["used_percent"] == 12 and windows["5h"]["source"] == f"status line {status}"
    assert windows["5h"]["observed_at"] == iso(NOW - 600)
    # The status line is newer now: it wins the weekly window too.
    write_status(status, NOW - 10, "rate_limits", claude_payload(five=12, week=60)["rate_limits"])
    again = {w["label"]: w for w in us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)["windows"]}
    assert again["weekly"]["used_percent"] == 60 and again["weekly"]["source"].startswith("status line")


def test_claude_worker_events_rejected_reset_and_bounded(root: Path) -> None:
    worker_log(us.JOBS, "w1", weekly_event(0.2, status="rejected"), NOW - 5)
    snap = us.claude_auto_snapshot(root / "none.json", us.JOBS, NOW, 60, 8)
    assert [(w["label"], w["used_percent"]) for w in snap["windows"]] == [("weekly", 100.0)]
    worker_log(us.JOBS, "w1", weekly_event(0.97, resets=EARLIER), NOW - 5)
    past = us.claude_auto_snapshot(root / "none.json", us.JOBS, NOW, 60, 8)["windows"][0]
    assert (past["state"], past["used_percent"]) == ("reset", 0.0)
    # Only the newest `log_limit` logs are read: an older log beyond the bound is not seen.
    worker_log(us.JOBS, "w2", {"status": "allowed"}, NOW)  # no window name: no window, and no evidence of no limit
    bounded = us.claude_auto_snapshot(root / "none.json", us.JOBS, NOW, 60, 1)
    assert bounded["status"] == "unknown" and bounded["windows"] == [] and "unlimited" not in bounded
    with pytest.raises(SystemExit):
        us.claude_auto_snapshot(root / "none.json", us.JOBS, NOW, 60, 0)


def test_claude_without_any_source_is_unknown_and_an_empty_reading_is_unlimited(root: Path) -> None:
    status = root / "c.json"
    snap = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)
    assert snap["status"] == "unknown" and "no status-line file" in snap["reason"]
    status.write_text(json.dumps({"rate_limits": {}}), encoding="utf-8")  # no written_at
    assert "lacks a valid written_at" in us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)["reason"]
    write_status(status, NOW - 5, "rate_limits", {})
    empty = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)
    assert empty["status"] == "ok" and empty["windows"] == [] and empty["unlimited"] is True
    # A window without reset time is trusted only while the reading is fresh.
    write_status(status, NOW - 7200, "rate_limits", {"five_hour": {"used_percentage": 50}})
    stale = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)["windows"][0]
    assert (stale["state"], stale["used_percent"]) == ("unknown", None)
    assert us.claude_auto_snapshot(status, us.JOBS, NOW, 180, 8)["windows"][0]["used_percent"] == 50


def test_the_claude_file_override_replaces_the_automatic_sources(root: Path) -> None:
    worker_log(us.JOBS, "w1", weekly_event(0.5), NOW - 5)
    manual = root / "manual.json"
    manual.write_text(json.dumps({"weekly": 97}), encoding="utf-8")
    os.utime(manual, (NOW - 5, NOW - 5))
    result = us.collect(root / "h", manual, 60, 8, NOW, claude_statusline=root / "c.json", jobs=us.JOBS)
    assert result["claude"]["data"] == {"weekly": 97}
    auto = us.collect(root / "h", None, 60, 8, NOW, claude_statusline=root / "c.json", jobs=us.JOBS)
    assert auto["claude"]["windows"][0]["used_percent"] == 50.0


# ---- Antigravity sources -------------------------------------------------------------------------------------


def test_antigravity_status_file_buckets(root: Path) -> None:
    status = root / "a.json"
    quota = {
        "gemini-weekly": {"remaining_fraction": 0.25, "reset_time": iso(LATER)},
        "gemini-5h": {"remaining_amount": 0, "reset_time": iso(LATER)},
        "off": {"remaining_fraction": 0.0, "disabled": True},
        "gone": {"remaining_fraction": 0.0, "reset_time": iso(EARLIER)},
    }
    write_status(status, NOW - 5, "quota", quota)
    snap = us.antigravity_auto_snapshot(status, NOW, 60, None, None)
    windows = {w["label"]: w for w in snap["windows"]}
    assert windows["gemini-weekly"]["used_percent"] == 75.0
    assert windows["gemini-weekly"]["resets_at"] == iso(LATER)
    assert (windows["gemini-5h"]["used_percent"], windows["gemini-5h"]["remaining_amount"]) == (100.0, 0)
    assert (windows["off"]["state"], windows["off"]["used_percent"]) == ("disabled", None)
    assert (windows["gone"]["state"], windows["gone"]["used_percent"]) == ("reset", 0.0)
    assert all(w["source"] == f"status line {status}" for w in snap["windows"])
    write_status(status, NOW - 5, "quota", {})
    assert us.antigravity_auto_snapshot(status, NOW, 60, None, None)["unlimited"] is True


def test_antigravity_falls_back_to_the_command_and_adds_it_beside_the_file(root: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "report text\n", "")

    status = root / "a.json"
    missing = us.antigravity_auto_snapshot(status, NOW, 60, None, None)
    assert missing["status"] == "unknown" and "no usage.antigravity_command" in missing["reason"]
    assert us.antigravity_auto_snapshot(status, NOW, 60, "agy", 5, runner)["report"] == "report text\n"
    write_status(status, NOW - 5, "quota", {"b": {"remaining_fraction": 1}})
    both = us.antigravity_auto_snapshot(status, NOW, 60, "agy", 5, runner)
    assert both["windows"][0]["used_percent"] == 0.0 and both["command"]["report"] == "report text\n"
    assert len(calls) == 2


# ---- blocked flags and the quota-blocks file -----------------------------------------------------------------


def test_collect_marks_windows_at_or_over_each_providers_line(root: Path) -> None:
    status = root / "c.json"
    write_status(status, NOW - 5, "rate_limits", claude_payload(five=89.9, week=90)["rate_limits"])
    lines = {"claude": 90.0, "codex": 100.0, "antigravity": 100.0}
    result = us.collect(root / "h", None, 60, 8, NOW, claude_statusline=status, jobs=us.JOBS, block_percent=lines)
    flags = {w["label"]: w["blocked"] for w in result["claude"]["windows"]}
    assert flags == {"5h": False, "weekly": True} and result["claude"]["blocked"] is True
    assert result["claude"]["block_percent"] == 90.0
    assert "blocked" not in result["codex"]  # unknown provider: no flag invented
    other = us.collect(
        root / "h", None, 60, 8, NOW, claude_statusline=status, jobs=us.JOBS, block_percent={**lines, "claude": 95.5}
    )
    assert other["claude"]["blocked"] is False


PM_FILE = """\ufeff{
  "written_at_utc": "2026-11-13T18:05:00Z",
  "blocks": [
    {"provider": "claude", "window": "weekly", "resets_at": "%(later)s", "wake_scheduled": true, "wake_note": "kept"},
    {"provider": "claude", "window": "weekly, all models", "resets_at": "%(later)s"},
    {"provider": "claude", "window": "old", "resets_at": "%(earlier)s"},
    {"provider": "antigravity", "window": "gemini", "resets_at": "%(earlier)s"}
  ],
  "waiting_tasks": [ {"task": "T-002",   "wake": "odd   spacing kept"} ]
}
"""


def snapshot_with(windows: list[dict[str, Any]], provider: str = "claude") -> dict[str, Any]:
    snap: dict[str, Any] = {p: {"status": "unknown", "reason": "x"} for p in ("claude", "codex", "antigravity")}
    snap[provider] = {"status": "ok", "windows": windows}
    return snap


def window(label: str, used: float, blocked: bool) -> dict[str, Any]:
    return {
        "label": label,
        "state": "ok",
        "fresh": True,
        "used_percent": used,
        "blocked": blocked,
        "resets_at": iso(LATER),
        "source": "status line x",
        "observed_at": iso(NOW),
    }


def test_quota_blocks_owns_only_the_blocks_list(tmp_path: Path) -> None:
    path = tmp_path / "quota-blocks.json"
    original = PM_FILE % {"later": iso(LATER), "earlier": iso(EARLIER)}
    path.write_text(original, encoding="utf-8")
    report = us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True), window("5h", 95, True)]), NOW)
    assert report["status"] == "written"
    text = path.read_text(encoding="utf-8")
    span, _ = us.find_member(text, "blocks")
    old_span, _ = us.find_member(original, "blocks")
    assert text[: span[0]] == original[: old_span[0]] and text[span[1] :] == original[old_span[1] :]
    blocks = json.loads(text.removeprefix("\ufeff"))["blocks"]
    by_window = {b["window"]: b for b in blocks}
    # Persisting block: measured fields refreshed, PM fields kept.
    assert by_window["weekly"]["wake_note"] == "kept" and by_window["weekly"]["used_percent"] == 97
    assert by_window["5h"]["written_by"] == "usage_snapshot.py"  # new block
    assert "weekly, all models" in by_window  # a window not read now, reset still ahead: kept
    assert "old" not in by_window  # read provider, reset passed: dropped
    assert "gemini" not in by_window  # provider not read, but the block's own reset passed: dropped (RT7)
    again = us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True), window("5h", 95, True)]), NOW)
    assert again["status"] == "unchanged"
    # Below the line in a reading taken after the block: the block goes, PM fields and all.
    below = window("weekly", 50, False) | {"observed_at": iso(NOW + 1)}
    us.write_quota_blocks(path, snapshot_with([below, window("5h", 95, True)]), NOW + 2)
    left = {b["window"] for b in json.loads(path.read_text(encoding="utf-8").removeprefix("\ufeff"))["blocks"]}
    assert left == {"5h", "weekly, all models"}


@pytest.mark.parametrize(
    ("text", "reason"),
    [("{broken", "not one JSON object"), ("[1]", "not one JSON object"), ('{"blocks": 3}', "not a list")],
)
def test_a_malformed_quota_blocks_file_is_left_untouched(tmp_path: Path, text: str, reason: str) -> None:
    path = tmp_path / "quota-blocks.json"
    path.write_text(text, encoding="utf-8")
    report = us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)
    assert report["status"] == "error" and reason in report["reason"]
    assert path.read_text(encoding="utf-8") == text


def test_quota_blocks_file_is_created_only_when_something_is_blocked(tmp_path: Path) -> None:
    path = tmp_path / "state" / "quota-blocks.json"
    path.parent.mkdir()  # main makes the folder through state_path(create=True); the lock file lives there
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 5, False)]), NOW)["status"] == "unchanged"
    assert not path.exists()
    us.write_quota_blocks(path, snapshot_with([window("weekly", 99, True)]), NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["blocks"][0]["window"] == "weekly"
    for start in ("{}", '{\n  "near_limit": []\n}\n'):
        path.write_text(start, encoding="utf-8")
        us.write_quota_blocks(path, snapshot_with([window("weekly", 99, True)]), NOW)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["blocks"]) == 1 and data.get("near_limit", []) == []


JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=12,
)
# Readings as the parsers meet them: Python's json reads NaN, Infinity and 1e400 (inf) as floats.
READING_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=12,
)


@settings(max_examples=60, deadline=None)
@given(
    others=st.dictionaries(st.text(max_size=6).filter(lambda k: k != "blocks"), JSON_VALUES, max_size=4),
    blocks=st.lists(st.dictionaries(st.text(max_size=4), JSON_VALUES, max_size=2), max_size=3),
    indent=st.sampled_from([None, 1, 2, 4]),
    with_blocks=st.booleans(),
)
def test_splice_keeps_every_other_member(others: dict, blocks: list, indent: Any, with_blocks: bool) -> None:
    data = {**others, "blocks": [{"old": 1}]} if with_blocks else dict(others)
    text = json.dumps(data, indent=indent)
    spliced = us.splice_blocks(text, blocks)
    parsed = json.loads(spliced)
    assert parsed["blocks"] == blocks
    assert {k: v for k, v in parsed.items() if k != "blocks"} == others
    if with_blocks:
        old, _ = us.find_member(text, "blocks")
        new, _ = us.find_member(spliced, "blocks")
        assert spliced[: new[0]] == text[: old[0]] and spliced[new[1] :] == text[old[1] :]


@settings(max_examples=150, deadline=None)
@given(value=READING_VALUES)
def test_payload_parsers_never_raise_and_only_pass_in_range_values(value: Any) -> None:
    """Every parser output is a known state, and an `ok` window always holds a finite percent in 0..100."""
    wrappers = ({"five_hour": value}, {"b": value}, {"seven_day": {"used_percentage": value, "resets_at": value}})
    events = (value, {"unifiedWindows": value}, {"rateLimitType": "seven_day", "utilization": value, "resetsAt": value})
    quotas = ({"b": {"remaining_fraction": value}}, {"b": {"remaining_amount": value, "reset_time": value}})
    limits = ({"primary": {"window_minutes": 300, "used_percent": value, "resets_at": value}},)
    entries = [e for w in wrappers for e in us.claude_status_windows(w, NOW, True)]
    entries += [e for w in events for e in us.rate_limit_windows(w, NOW, True)]
    entries += [e for w in quotas for e in us.antigravity_windows(w, NOW, True)]
    entries += [e for w in limits for e in us.windows_of(w, NOW, True)]
    if isinstance(value, dict):
        entries += us.claude_status_windows(value, NOW, True) + us.antigravity_windows(value, NOW, True)
    for entry in entries:
        assert entry["state"] in {"ok", "reset", "unknown", "disabled"}
        if entry["state"] == "ok":
            assert isinstance(entry["used_percent"], float) and 0 <= entry["used_percent"] <= 100


def test_main_writes_the_blocks_file_and_reports_it(root: Path, monkeypatch, capsys) -> None:
    worker_log(us.JOBS, "w1", weekly_event(0.93), time.time() - 5)  # main reads at the real now: a past log
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "--codex-home", str(root / "no-codex")])
    assert us.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claude"]["blocked"] is True and out["quota_blocks"]["status"] == "written"
    blocks = json.loads((root / ".tmp/pm-state/quota-blocks.json").read_text(encoding="utf-8"))["blocks"]
    assert [(b["provider"], b["window"], b["used_percent"]) for b in blocks] == [("claude", "weekly", 93.0)]


def test_state_path_splits_either_separator_and_stays_under_the_state_folder(tmp_path: Path) -> None:
    expected = tmp_path / ".tmp" / "pm-state" / "sub" / "b.json"
    assert us.state_path(".tmp\\pm-state/sub\\b.json", tmp_path) == expected
    assert not (tmp_path / ".tmp").exists()  # read paths create nothing
    assert us.state_path(".tmp/pm-state/sub/b.json", tmp_path, create=True) == expected
    assert expected.parent.is_dir() and not expected.exists()
    for bad in ("notes/q.json", ".tmp/pm-state", ".tmp/pm-state/../q.json", ".tmp/pm-state/C:q.json"):
        with pytest.raises(SystemExit, match="not a file under .tmp/pm-state/"):
            us.state_path(bad, tmp_path)


# ---- SessionStart quota line ---------------------------------------------------------------------------------


@pytest.fixture
def hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("session_context_quota", TOOLS / "hooks" / "session_context.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / ".tmp" / "pm-state"
    state.mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.delenv(module.WORKER_ENV_FLAG, raising=False)
    (state / "pm-continuation.md").write_text("## NOW\n- working\n", encoding="utf-8")
    return module


def session_text(hook: Any, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "pm", "source": "startup"})))
    assert hook.main([]) == 0
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


def test_session_start_prints_each_block_after_the_note(hook: Any, tmp_path: Path, monkeypatch, capsys) -> None:
    assert "Quota blocks" not in session_text(hook, monkeypatch, capsys)  # no file: no block
    blocks = [
        {"provider": "claude", "window": "weekly", "resets_at": iso(LATER), "used_percent": 97},
        {"provider": "codex", "window": "5h", "resets_at": iso(1.0)},
    ]
    (tmp_path / ".tmp/pm-state/quota-blocks.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
    text = session_text(hook, monkeypatch, capsys)
    assert f"- claude weekly blocked until {iso(LATER)}, 97% used\n" in text + "\n"
    assert "- codex 5h blocked until 1970-01-01T00:00:01+00:00 (reset time passed" in text
    assert text.index("Quota blocks (.tmp/pm-state/quota-blocks.json") > text.index("- working")
    # The PM's file starts with a BOM (PM_FILE): it is read the same.
    target = tmp_path / ".tmp/pm-state/quota-blocks.json"
    target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
    assert f"- claude weekly blocked until {iso(LATER)}, 97% used" in session_text(hook, monkeypatch, capsys)


def test_session_start_reports_an_unreadable_file_or_config(hook: Any, tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / ".tmp/pm-state/quota-blocks.json").write_text("{broken", encoding="utf-8")
    assert "Quota blocks file .tmp/pm-state/quota-blocks.json unreadable" in session_text(hook, monkeypatch, capsys)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"usage": {}}), encoding="utf-8")
    monkeypatch.setattr(hook, "USAGE_CONFIG", bad)
    assert "not configured: usage.quota_blocks_file" in session_text(hook, monkeypatch, capsys)


# ---- Fix round 1 (review findings F1-F9, Gemini P3) ----------------------------------------------------------
# Tests named `test_fix1_*` reproduce a review finding and ran against the phase 1 code too (before/after evidence in
# .tmp/T-026/fix1/). Every value is a synthetic test double.

LINES = {"claude": 90.0, "codex": 100.0, "antigravity": 100.0}
STATE_REL = ".tmp/pm-state"


def profile(tmp_path: Path, **usage: Any) -> Path:
    raw = json.loads((TOOLS / "codex_profiles.json").read_text(encoding="utf-8"))
    raw["usage"].update(usage)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def make_link(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no symlink right needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path) -> None:
    """Remove the link itself, never its target (AGENTS.md section 6)."""
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def codex_home(tmp_path: Path, row_time: float, rate_limits: object = ()) -> Path:
    home = tmp_path / "codex-home"
    folder = home / "sessions" / "2027" / "01" / "15"
    folder.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"type": "token_count", "info": None}
    if rate_limits != ():
        payload["rate_limits"] = rate_limits
    row = {"timestamp": iso(row_time), "type": "event_msg", "payload": payload}
    (folder / "rollout-x.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return home


# F1: null, empty or stale readings are unknown, never `ok, unlimited`.
@pytest.mark.parametrize(
    ("written", "value"),
    [(NOW - 5, None), (NOW - 5, []), (NOW - 5, "x"), (NOW - 5, 3), (NOW - 7200, {}), (NOW + 600, {})],
    ids=["null", "list", "string", "number", "stale-empty", "future-written_at"],
)
def test_fix1_claude_malformed_or_stale_status_line_is_unknown(root: Path, written: float, value: object) -> None:
    status = root / "c.json"
    status.write_text(json.dumps({"written_at": iso(written), "rate_limits": value}), encoding="utf-8")
    snap = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)
    assert snap["status"] == "unknown" and snap.get("unlimited") is not True


@pytest.mark.parametrize(
    ("written", "value"),
    [(NOW - 5, None), (NOW - 5, []), (NOW - 5, "x"), (NOW - 7200, {})],
    ids=["null", "list", "string", "stale-empty"],
)
def test_fix1_antigravity_malformed_or_stale_status_line_is_unknown(root: Path, written: float, value: object) -> None:
    status = root / "a.json"
    status.write_text(json.dumps({"written_at": iso(written), "quota": value}), encoding="utf-8")
    snap = us.antigravity_auto_snapshot(status, NOW, 60, None, None)
    assert snap["status"] == "unknown" and snap.get("unlimited") is not True


@pytest.mark.parametrize(
    ("age", "limits", "expected"),
    [
        (7200, {}, "unknown"),  # stale: says nothing now
        (7200, None, "unknown"),
        (5, "x", "unknown"),  # malformed member
        (5, (), "unknown"),  # absent member
        (5, {}, "unlimited"),  # fresh object without a window: no limit (decision 44)
        (5, None, "unlimited"),  # fresh explicit null: no limit
    ],
)
def test_fix1_codex_unlimited_needs_a_fresh_valid_row(tmp_path: Path, age: float, limits: object, expected: str) -> None:
    snap = us.codex_snapshot(codex_home(tmp_path, NOW - age, limits), NOW, 60, 8)
    got = "unlimited" if snap["status"] == "ok" and snap["unlimited"] else snap["status"]
    assert got == expected


def test_fix1_a_worker_event_without_a_window_is_no_evidence(root: Path) -> None:
    worker_log(us.JOBS, "w1", {"status": "allowed"}, NOW - 5)
    snap = us.claude_auto_snapshot(root / "none.json", us.JOBS, NOW, 60, 8)
    assert snap["status"] == "unknown" and snap.get("unlimited") is not True


# F9 and Gemini P3: out-of-range, non-finite, wrongly typed and millisecond values never read as healthy.
MS_RESET = 1_795_010_400_000  # the live weekly reset, in milliseconds


@pytest.mark.parametrize(
    "window_value",
    [
        {"used_percentage": -5},
        {"used_percentage": 100.5},  # NaN and Infinity are not JSON: the whole file is refused (test_fix4_*)
        {"used_percentage": True},
        {"used_percentage": "50"},
        {"used_percentage": 50, "resets_at": MS_RESET},
        {"used_percentage": 50, "resets_at": -1},
        {"used_percentage": 50, "resets_at": "tomorrow"},
        "not an object",
    ],
)
def test_fix1_claude_invalid_window_values_are_unknown(root: Path, window_value: object) -> None:
    status = root / "c.json"
    write_status(status, NOW - 5, "rate_limits", {"five_hour": window_value})
    snap = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)
    assert snap["status"] == "unknown"
    assert [w["state"] for w in snap["windows"]] == ["unknown"]


@pytest.mark.parametrize(
    "fields",
    [
        {"utilization": 1.5},
        {"utilization": -0.2},
        {"utilization": float("nan")},
        {"utilization": 0.5, "resetsAt": MS_RESET},
        {"utilization": 0.5, "status": "weird"},
    ],
)
def test_fix1_worker_event_invalid_values_are_unknown(root: Path, fields: dict[str, Any]) -> None:
    info = {"rateLimitType": "seven_day", "unifiedWindows": {"seven_day": {"status": "allowed", **fields}}}
    worker_log(us.JOBS, "w1", info, NOW - 5)
    snap = us.claude_auto_snapshot(root / "none.json", us.JOBS, NOW, 60, 8)
    assert snap["status"] == "unknown" and [w["state"] for w in snap["windows"]] == ["unknown"]


@pytest.mark.parametrize(
    "bucket",
    [
        {"remaining_fraction": 1.5},
        {"remaining_fraction": -0.1},
        {"remaining_amount": -3},
        {"remaining_fraction": 0.5, "reset_time": "garbage"},
        {"remaining_fraction": 0.5, "disabled": "yes"},
        "not an object",
    ],
)
def test_fix1_antigravity_invalid_bucket_values_are_unknown(root: Path, bucket: object) -> None:
    status = root / "a.json"
    write_status(status, NOW - 5, "quota", {"b": bucket})
    snap = us.antigravity_auto_snapshot(status, NOW, 60, None, None)
    assert snap["status"] == "unknown" and [w["state"] for w in snap["windows"]] == ["unknown"]


@pytest.mark.parametrize(
    "window_value",
    [{"window_minutes": 300, "used_percent": 150}, {"window_minutes": 300, "used_percent": 5, "resets_at": MS_RESET}],
)
def test_fix1_codex_invalid_window_values_are_unknown(tmp_path: Path, window_value: dict[str, Any]) -> None:
    snap = us.codex_snapshot(codex_home(tmp_path, NOW - 5, {"primary": window_value}), NOW, 60, 8)
    assert snap["status"] == "unknown" and [w["state"] for w in snap["windows"]] == ["unknown"]


def test_valid_boundary_values_are_accepted(root: Path, tmp_path: Path) -> None:
    status = root / "c.json"
    edges = {"five_hour": {"used_percentage": 0}, "seven_day": {"used_percentage": 100, "resets_at": int(LATER)}}
    write_status(status, NOW - 5, "rate_limits", edges)
    snap = us.claude_auto_snapshot(status, us.JOBS, NOW, 60, 8)
    assert snap["status"] == "ok" and [w["used_percent"] for w in snap["windows"]] == [0.0, 100.0]
    agy = root / "a.json"
    write_status(agy, NOW - 5, "quota", {"a": {"remaining_fraction": 0}, "b": {"remaining_fraction": 1}})
    assert [w["used_percent"] for w in us.antigravity_auto_snapshot(agy, NOW, 60, None, None)["windows"]] == [
        100.0,
        0.0,
    ]
    events = us.rate_limit_windows({"unifiedWindows": {"x": {"utilization": 1}, "y": {"utilization": 0}}}, NOW, True)
    assert [w["used_percent"] for w in events] == [100.0, 0.0]
    assert us.epoch(253402300799) is not None and us.epoch(253402300800) is None  # the last second a date can hold


# F6: a partial `unifiedWindows` entry keeps the top-level rejection and reset of its window.
def test_fix1_a_partial_unified_window_keeps_the_top_level_rejection_and_reset() -> None:
    info = {
        "status": "rejected",
        "resetsAt": int(LATER + 86400),
        "rateLimitType": "seven_day",
        "unifiedWindows": {"seven_day": {"utilization": 0.2}},
    }
    [weekly] = us.rate_limit_windows(info, NOW, True)
    assert (weekly["label"], weekly["used_percent"], weekly["resets_at"]) == ("weekly", 100.0, iso(LATER + 86400))
    # The top-level rejection holds even when the window's own entry still says `allowed`.
    allowed = {**info, "unifiedWindows": {"seven_day": {"utilization": 0.2, "status": "allowed"}}}
    assert us.rate_limit_windows(allowed, NOW, True)[0]["used_percent"] == 100.0
    # A window the top level does not name keeps its own fields only.
    other = {**info, "unifiedWindows": {"seven_day": {"utilization": 0.2}, "five_hour": {"utilization": 0.3}}}
    by_label = {w["label"]: w for w in us.rate_limit_windows(other, NOW, True)}
    assert by_label["5h"]["used_percent"] == 30.0 and "resets_at" not in by_label["5h"]


# F2 and block transitions: only a fresh reading below the line, or a passed reset, clears a block.
def test_fix1_a_stale_reading_without_reset_keeps_an_active_block(root: Path) -> None:
    status = root / "c.json"
    block = {"provider": "claude", "window": "5h", "resets_at": iso(NOW + 86400), "wake_note": "keep"}
    write_status(status, NOW - 7200, "rate_limits", {"five_hour": {"used_percentage": 95}})
    snap = us.collect(root / "h", None, 60, 8, NOW, claude_statusline=status, jobs=us.JOBS, block_percent=LINES)
    assert us.updated_blocks([block], snap, NOW) == [block]
    # Another valid window makes the provider readable; the unknown 5h window still keeps its block.
    week = {"used_percentage": 10, "resets_at": int(LATER)}
    write_status(status, NOW - 7200, "rate_limits", {"five_hour": {"used_percentage": 95}, "seven_day": week})
    snap = us.collect(root / "h", None, 60, 8, NOW, claude_statusline=status, jobs=us.JOBS, block_percent=LINES)
    assert snap["claude"]["status"] == "ok"
    assert us.updated_blocks([block], snap, NOW) == [block]


def entry(state: str, fresh: bool, blocked: bool, observed: float = NOW - 5) -> dict[str, Any]:
    return {
        "label": "weekly",
        "state": state,
        "fresh": fresh,
        "blocked": blocked,
        "used_percent": None if state == "unknown" else (95.0 if blocked else 10.0),
        "resets_at": iso(LATER),
        "source": "test double",
        "observed_at": iso(observed),
    }


@pytest.mark.parametrize(
    ("window_entry", "block_extra", "outcome"),
    [
        (entry("ok", True, False), {"observed_at": iso(NOW - 60)}, "cleared"),  # later fresh reading below the line
        (entry("ok", True, False), {}, "kept"),  # a block without observed_at cannot be ordered: a reading never clears it
        (entry("ok", False, False), {"observed_at": iso(NOW - 60)}, "kept"),  # stale reading below the line
        (entry("unknown", True, False), {"observed_at": iso(NOW - 60)}, "kept"),
        (entry("disabled", True, False), {"observed_at": iso(NOW - 60)}, "kept"),
        (entry("reset", False, False) | {"resets_at": iso(NOW - 30)}, {"observed_at": iso(NOW - 60)}, "cleared"),
        (entry("reset", False, False), {}, "kept"),
        (entry("ok", True, False, NOW - 600), {"observed_at": iso(NOW - 60)}, "kept"),  # older than the block
        (entry("ok", True, False, NOW - 60), {"observed_at": iso(NOW - 60)}, "kept"),  # the same instant: not after
        (entry("ok", False, True), {}, "refreshed"),  # still blocked: fields refreshed, PM fields kept
        (None, {}, "kept"),  # window not read now, reset ahead
        (None, {"resets_at": iso(EARLIER)}, "cleared"),  # the block's own reset passed
        (entry("unknown", True, False), {"resets_at": iso(EARLIER)}, "cleared"),
    ],
)
def test_block_transitions(window_entry: dict[str, Any] | None, block_extra: dict[str, Any], outcome: str) -> None:
    block = {"provider": "claude", "window": "weekly", "resets_at": iso(LATER), "wake_note": "pm", **block_extra}
    snap = snapshot_with([window_entry] if window_entry else [entry("ok", True, False) | {"label": "other"}])
    result = us.updated_blocks([block], snap, NOW)
    if outcome == "cleared":
        assert result == []
    elif outcome == "kept":
        assert result == [block]
    else:
        [refreshed] = result
        assert refreshed["wake_note"] == "pm" and refreshed["used_percent"] == 95.0


# F3: writers do not lose each other's updates.
PM_START = '{\n  "wake_note": "old",\n  "blocks": []\n}\n'
PM_EDIT = '{\n  "wake_note": "NEW-PM-UPDATE",\n  "blocks": []\n}\n'


def blocks_file(tmp_path: Path) -> Path:
    path = tmp_path / STATE_REL / "quota-blocks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PM_START, encoding="utf-8")
    return path


def test_fix1_a_pm_edit_during_the_merge_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = blocks_file(tmp_path)
    real = us.splice_blocks
    edits: list[int] = []

    def interleaved(text: str | None, blocks: list[Any]) -> str:
        if not edits:  # the PM writes while the snapshot merges
            edits.append(1)
            path.write_text(PM_EDIT, encoding="utf-8")
        return real(text, blocks)

    monkeypatch.setattr(us, "splice_blocks", interleaved)
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)["status"] == "written"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["wake_note"] == "NEW-PM-UPDATE" and [b["window"] for b in data["blocks"]] == ["weekly"]


def test_fix1_a_pm_edit_during_a_sharing_violation_retry_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = blocks_file(tmp_path)
    real_replace = os.replace
    calls: list[str] = []

    def held(src: Any, dst: Any) -> None:
        calls.append(str(src))
        if len(calls) == 1:  # the PM holds the file open and writes it: the replace fails once
            Path(dst).write_text(PM_EDIT, encoding="utf-8")
            raise PermissionError(13, "sharing violation (test double)")
        real_replace(src, dst)

    monkeypatch.setattr(us.os, "replace", held)
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)["status"] == "written"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["wake_note"] == "NEW-PM-UPDATE" and len(data["blocks"]) == 1 and len(calls) == 2
    assert not list(path.parent.glob("*.tmp"))


def test_a_held_lock_stops_the_write_and_leaves_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = blocks_file(tmp_path)
    ledger = us.sibling("git_ledger", TOOLS / "hooks")
    monkeypatch.setattr(us, "LOCK_WAIT_SECONDS", 0.2)
    with (path.parent / "quota-blocks.json.lock").open("a+b") as other:  # another writer holds the lock
        ledger._lock(other, 1e18)
        try:
            report = us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)
        finally:
            ledger._unlock(other)
    assert report["status"] == "error" and "stayed busy" in report["reason"]
    assert path.read_text(encoding="utf-8") == PM_START
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)["status"] == "written"


def test_two_writer_processes_both_land(tmp_path: Path) -> None:
    """Two real processes merge different blocks into one file at once; under the lock neither block is lost."""
    path = blocks_file(tmp_path)
    script = tmp_path / "writer.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "import usage_snapshot as us\n"
        "path, label, now = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
        "window = {'label': label, 'state': 'ok', 'fresh': True, 'used_percent': 99.0, 'blocked': True}\n"
        "snap = {p: {'status': 'unknown'} for p in ('claude', 'codex', 'antigravity')}\n"
        "snap['codex' if label == 'a' else 'antigravity'] = {'status': 'ok', 'windows': [window]}\n"
        "for _ in range(20):\n"
        "    us.write_quota_blocks(us.Path(path), snap, now)\n",
        encoding="utf-8",
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    args = [sys.executable, "-B", "-X", "faulthandler", str(script), str(path)]
    procs = [
        subprocess.Popen([*args, label, str(NOW)], creationflags=flags, stderr=subprocess.PIPE) for label in "ab"
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["wake_note"] == "old"
    assert sorted((b["provider"], b["window"]) for b in data["blocks"]) == [("antigravity", "b"), ("codex", "a")]


# F4: bytes outside `blocks` keep their CRLF line endings.
def test_fix1_crlf_outside_blocks_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "quota-blocks.json"
    original = '﻿{\r\n  "wake_note": "x",\r\n  "blocks": [],\r\n  "tail": [ 1,  2 ]\r\n}\r\n'.encode()
    path.write_bytes(original)
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)["status"] == "written"
    raw = path.read_bytes()
    assert raw.startswith(original[: original.index(b"[]")])
    assert raw.endswith(original[original.index(b"[]") + 2 :])
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare LF: the new value uses the file's line ending
    assert json.loads(raw.decode("utf-8-sig"))["blocks"][0]["window"] == "weekly"


def test_a_quota_file_that_is_not_utf8_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "quota-blocks.json"
    path.write_bytes(b'{"blocks": [], "note": "\xff"}')
    report = us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)
    assert report["status"] == "error" and path.read_bytes() == b'{"blocks": [], "note": "\xff"}'


# F5 and F7: configured quota paths lie under .tmp/pm-state and mean the same file on every OS.
def test_fix1_a_status_line_path_outside_the_state_folder_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = profile(tmp_path, claude_statusline_file="notes/quota.json")
    line = run_statusline(monkeypatch, capsys, claude_payload(), "--source", "claude", "--config", str(config))
    assert line.startswith("quota not recorded:") and ".tmp/pm-state" in line
    assert not (root / "notes" / "quota.json").exists()


def test_fix1_a_backslash_path_means_the_same_file_on_posix(tmp_path: Path) -> None:
    from pathlib import PurePosixPath

    backend = us.sibling("codex_backend")
    settings_ = backend.load_usage_settings(profile(tmp_path, antigravity_statusline_file=".tmp\\pm-state\\a.json"))
    value = settings_["antigravity_statusline_file"]
    joined = PurePosixPath("/srv/project") / value  # what a Linux consumer does with the configured string
    assert joined == PurePosixPath("/srv/project/.tmp/pm-state/a.json") and joined.name == "a.json"


# F8 and the class sweep: a link at any component of a quota path is refused by every reader and writer.
@pytest.mark.parametrize("linked", [".tmp", STATE_REL])
def test_fix1_a_link_on_a_quota_path_is_refused_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], hook: Any, linked: str
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    target = outside / Path(STATE_REL).relative_to(linked)
    target.mkdir(parents=True)
    (target / "quota-blocks.json").write_text(json.dumps({"blocks": [{"provider": "outside_fixture"}]}), "utf-8")
    write_status(target / "claude-statusline.json", NOW - 5, "rate_limits", claude_payload()["rate_limits"])
    link = project / linked
    link.parent.mkdir(parents=True)
    make_link(link, outside if linked == ".tmp" else target)
    try:
        # SessionStart hook: refused, the outside file never printed.
        monkeypatch.setattr(hook, "ROOT", project)
        text = hook.quota_block(NOW)
        assert "refused" in text and "outside_fixture" not in text
        # Reader and writers of usage_snapshot / statusline.py.
        for relative in ("claude-statusline.json", "quota-blocks.json"):
            with pytest.raises(SystemExit, match="link"):
                us.state_path(f"{STATE_REL}/{relative}", project)
        monkeypatch.setattr(us, "ROOT", project)
        line = run_statusline(monkeypatch, capsys, claude_payload(five=1, week=2), "--source", "claude")
        assert line.startswith("quota not recorded:") and "link" in line
        monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "--codex-home", str(tmp_path / "no-codex")])
        with pytest.raises(SystemExit, match="link"):
            us.main()
        assert json.loads((target / "claude-statusline.json").read_text("utf-8"))["rate_limits"]["five_hour"][
            "used_percentage"
        ] == 23.5  # the outside file was not rewritten
    finally:
        remove_link(link)
    assert target.is_dir()  # the link went, its target stayed


# ---- fix round 2: one observation per reading, ordered in time, and one transaction for every writer ----------
SOURCES = ("claude", "worker", "antigravity", "codex")
PROVIDER_OF = {"claude": "claude", "worker": "claude", "antigravity": "antigravity", "codex": "codex"}


def from_source(tmp_path: Path, source: str, slot: object, seen: float) -> dict[str, Any]:
    """A snapshot of one provider read from one real source whose weekly window slot holds `slot` (test double files)."""
    if source == "codex":
        return us.codex_snapshot(codex_home(tmp_path, seen, {"primary": slot}), NOW, 60, 8)
    jobs = tmp_path / "jobs"
    if source == "worker":
        worker_log(jobs, "w", {"rateLimitType": "seven_day", "unifiedWindows": {"seven_day": slot}}, seen)
        return us.claude_auto_snapshot(tmp_path / "absent.json", jobs, NOW, 60, 8)
    if source == "claude":
        write_status(tmp_path / "c.json", seen, "rate_limits", {"seven_day": slot})
        return us.claude_auto_snapshot(tmp_path / "c.json", jobs, NOW, 60, 8)
    write_status(tmp_path / "a.json", seen, "quota", {"weekly": slot})
    return us.antigravity_auto_snapshot(tmp_path / "a.json", NOW, 60, None, None)


def weekly_slot(source: str, used: float, resets: float) -> dict[str, Any]:
    if source == "codex":
        return {"used_percent": used, "window_minutes": 10080, "resets_at": int(resets)}
    if source == "worker":
        return {"utilization": used / 100, "resetsAt": int(resets)}
    if source == "claude":
        return {"used_percentage": used, "resets_at": int(resets)}
    return {"remaining_fraction": 1 - used / 100, "reset_time": iso(resets)}


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("slot", [[], False, "x", {}, 42, 1.5], ids=["list", "false", "string", "empty", "int", "float"])
def test_fix2_a_malformed_window_slot_is_unknown_never_unlimited(tmp_path: Path, source: str, slot: object) -> None:
    snap = from_source(tmp_path, source, slot, NOW - 5)
    assert snap["status"] == "unknown" and snap.get("unlimited") is not True
    assert [w["state"] for w in snap["windows"]] == ["unknown"]


def test_fix2_codex_null_slots_state_no_limit_and_a_bad_slot_beside_a_good_one_stays_visible(tmp_path: Path) -> None:
    empty = us.codex_snapshot(codex_home(tmp_path, NOW - 5, {"primary": None, "secondary": None}), NOW, 60, 8)
    assert empty["status"] == "ok" and empty["unlimited"] is True
    good = {"used_percent": 5, "window_minutes": 300, "resets_at": int(LATER)}
    mixed = us.codex_snapshot(codex_home(tmp_path, NOW - 5, {"primary": good, "secondary": []}), NOW, 60, 8)
    assert {w["label"]: w["state"] for w in mixed["windows"]} == {"5h": "ok", "secondary": "unknown"}


@pytest.mark.parametrize("source", SOURCES)
def test_fix2_an_observation_dated_after_now_is_refused(tmp_path: Path, source: str) -> None:
    future = from_source(tmp_path, source, weekly_slot(source, 0, LATER), NOW + 1)
    assert future["status"] == "unknown" and future.get("unlimited") is not True
    assert future.get("windows", []) == []
    block ={"provider": PROVIDER_OF[source], "window": "weekly", "observed_at": iso(NOW - 10), "resets_at": iso(LATER)}
    us.mark_blocked(future, LINES[PROVIDER_OF[source]])
    assert us.updated_blocks([block], {PROVIDER_OF[source]: future}, NOW) == [block]


def test_fix2_a_future_reading_never_outranks_a_real_one(tmp_path: Path) -> None:
    home = codex_home(tmp_path, NOW - 30, {"primary": {"used_percent": 40, "window_minutes": 300}})
    later = home / "sessions" / "2027" / "01" / "15" / "rollout-y.jsonl"
    row = {"timestamp": iso(NOW + 86400), "payload": {"type": "token_count", "rate_limits": {}}}
    later.write_text(json.dumps(row) + "\n", encoding="utf-8")
    snap = us.codex_snapshot(home, NOW, 60, 8)
    assert snap["status"] == "ok" and snap["windows"][0]["used_percent"] == 40 and snap["observed_at"] == iso(NOW - 30)
    assert len(snap["refused"]) == 1 and "after now" in snap["refused"][0]
    jobs = tmp_path / "jobs"
    worker_log(jobs, "past", weekly_event(0.4), NOW - 30)
    worker_log(jobs, "future", weekly_event(0.0), NOW + 60)
    claude = us.claude_auto_snapshot(tmp_path / "absent.json", jobs, NOW, 60, 8)
    assert claude["windows"][0]["used_percent"] == 40.0 and len(claude["refused"]) == 1


# Block observed at NOW-100 in the cycle ending at LATER (NOW+3600). Rows: (observed, used, reading's reset, outcome).
CYCLE_ROWS = [
    (NOW - 50, 10, LATER, "cleared"),  # later reading, same cycle, below the line
    (NOW - 50, 10, LATER + 7 * 86400, "cleared"),  # later reading of a later cycle
    (NOW - 50, 100, NOW - 10, "cleared"),  # the window reset after the block was seen
    (NOW - 150, 10, LATER, "kept"),  # older than the block
    (NOW - 100, 10, LATER, "kept"),  # the same instant is not after it
    (NOW - 7200, 100, NOW - 10, "kept"),  # RT3: an old reading whose reset passed
    (NOW - 50, 100, NOW - 120, "kept"),  # its cycle ended before the block was seen: an earlier cycle
    (NOW + 60, 10, LATER, "kept"),  # from the future: refused
    (NOW - 150, 100, LATER + 60, "kept-unrefreshed"),  # blocked but older: the block keeps its own fields
    (NOW - 50, 100, LATER + 60, "refreshed"),  # blocked and newer: refreshed, PM fields kept
]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(("observed", "used", "resets", "outcome"), CYCLE_ROWS)
def test_fix2_block_cycle_ordering(
    tmp_path: Path, source: str, observed: float, used: float, resets: float, outcome: str
) -> None:
    provider = PROVIDER_OF[source]
    snap = from_source(tmp_path, source, weekly_slot(source, used, resets), observed)
    us.mark_blocked(snap, LINES[provider])
    block = {
        "provider": provider,
        "window": "weekly",
        "observed_at": iso(NOW - 100),
        "resets_at": iso(LATER),
        "wake_note": "new-cycle",
    }
    result = us.updated_blocks([block], {provider: snap}, NOW)
    if outcome == "cleared":
        assert result == []
    elif outcome in ("kept", "kept-unrefreshed"):
        assert result == [block]
    else:
        [refreshed] = result
        assert refreshed["wake_note"] == "new-cycle" and refreshed["resets_at"] == iso(LATER + 60)
        assert refreshed["observed_at"] == iso(observed)


# PM writes go through the same transaction (`pm-set`).
def pm_set(root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "pm-set", *argv])
    code = us.main()
    return code, json.loads(capsys.readouterr().out)


def test_fix2_pm_set_edits_top_level_and_block_fields(root: Path, monkeypatch, capsys) -> None:
    path = root / STATE_REL / "quota-blocks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\xef\xbb\xbf{\r\n  "wake_note": "old",\r\n  "blocks": []\r\n}\r\n')
    code, report = pm_set(root, monkeypatch, capsys, "--text", "wake_note", "NEW", "--json", "waiting_tasks", '["T-1"]')
    assert (code, report["status"]) == (0, "written")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf{\r\n") and b"\n" not in raw.replace(b"\r\n", b"")  # BOM and CRLF kept
    data = json.loads(raw.decode("utf-8-sig"))
    assert data == {"wake_note": "NEW", "blocks": [], "waiting_tasks": ["T-1"]}
    block_args = ("--block", "codex", "weekly", "--text", "resets_at", iso(LATER), "--json", "wake_scheduled", "true")
    code, report = pm_set(root, monkeypatch, capsys, *block_args)
    [block] = json.loads(path.read_bytes().decode("utf-8-sig"))["blocks"]
    assert block["resets_at"] == iso(LATER) and block["wake_scheduled"] is True and block["written_by"] == "pm"
    assert us.parse_time(block["observed_at"]) <= time.time()
    assert pm_set(root, monkeypatch, capsys, "--text", "wake_note", "NEW")[1]["status"] == "unchanged"
    code, report = pm_set(root, monkeypatch, capsys, "--block", "codex", "weekly", "--remove-block")
    assert report["status"] == "written" and json.loads(path.read_bytes().decode("utf-8-sig"))["blocks"] == []
    code, report = pm_set(root, monkeypatch, capsys, "--unset", "waiting_tasks")
    assert "waiting_tasks" not in json.loads(path.read_bytes().decode("utf-8-sig"))


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--text", "blocks", "x"],
        ["--text", "a", "1", "--json", "a", "2"],
        ["--json", "a", "{broken"],
        ["--remove-block"],
        ["--block", "claude", "weekly", "--remove-block", "--text", "a", "b"],
        ["--block", "openai", "weekly", "--text", "a", "b"],
        ["--block", "claude", "weekly", "--text", "window", "other"],
        ["--block", "claude", "weekly", "--text", "resets_at", "tomorrow"],
        ["--block", "claude", "weekly", "--text", "observed_at", "2999-01-01T00:00:00Z"],
        ["--block", "claude", "weekly", "--unset", "observed_at"],
    ],
)
def test_fix2_pm_set_refusals_change_nothing(root: Path, monkeypatch, capsys, argv: list[str]) -> None:
    path = root / STATE_REL / "quota-blocks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PM_START, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "pm-set", *argv])
    with pytest.raises(SystemExit) as stop:
        us.main()
    assert stop.value.code == 2 and path.read_text(encoding="utf-8") == PM_START


def test_fix2_pm_set_waits_for_the_tool_writer_and_never_writes_around_it(root: Path, monkeypatch, capsys) -> None:
    """The PM command run while the snapshot holds the lock (between its read and its commit) is refused, not
    interleaved; run again afterwards it lands, and the snapshot's block stays."""
    path = blocks_file(root)
    monkeypatch.setattr(us, "LOCK_WAIT_SECONDS", 0.2)
    real = us.planned_blocks
    inner: list[Any] = []

    def snapshot_plan(*args: Any) -> Any:
        if not inner:
            inner.append(pm_set(root, monkeypatch, capsys, "--text", "wake_note", "PM-DURING"))
        return real(*args)

    monkeypatch.setattr(us, "planned_blocks", snapshot_plan)
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)["status"] == "written"
    code, report = inner[0]
    assert code == 1 and "stayed busy" in report["reason"]
    assert pm_set(root, monkeypatch, capsys, "--text", "wake_note", "PM-AFTER")[1]["status"] == "written"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["wake_note"] == "PM-AFTER" and [b["window"] for b in data["blocks"]] == ["weekly"]


def test_fix2_pm_set_and_the_snapshot_in_two_processes_lose_nothing(tmp_path: Path) -> None:
    """Real processes: one runs `pm-set` 15 times with a rising wake_note, one the snapshot writer 15 times."""
    path = blocks_file(tmp_path)
    script = tmp_path / "both.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "import usage_snapshot as us\n"
        "root, role = us.Path(sys.argv[1]), sys.argv[2]\n"
        "us.ROOT = root\n"
        "path = root / '.tmp' / 'pm-state' / 'quota-blocks.json'\n"
        "window = {'label': 'w', 'state': 'ok', 'fresh': True, 'used_percent': 99.0, 'blocked': True}\n"
        "snap = {'codex': {'status': 'ok', 'windows': [window]}}\n"
        "for i in range(15):\n"
        "    if role == 'pm':\n"
        "        assert us.pm_set(['--json', 'wake_note', str(i)]) == 0\n"
        "    else:\n"
        "        assert us.write_quota_blocks(path, snap, 1.0)['status'] in ('written', 'unchanged')\n",
        encoding="utf-8",
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    args = [sys.executable, "-B", "-X", "faulthandler", str(script), str(tmp_path)]
    procs = [
        subprocess.Popen([*args, role], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for role in ("pm", "tool")
    ]
    try:
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["wake_note"] == 14 and [(b["provider"], b["window"]) for b in data["blocks"]] == [("codex", "w")]


# ---- fix round 3 (retest 2: RT5-RT9): one read contract, one time parser, one block table, one error boundary ----
# Classes (a) to (e) of "Fix round 3 audit" in the task file. Values and injected faults are labelled test doubles;
# the lock, the files and the second processes are real. The claim-and-link commit of round 2 and its five tests are
# gone: the file keeps one name.
QUOTA_REL = f"{STATE_REL}/quota-blocks.json"
KEY = ("codex", "weekly")
ONE_BLOCK = {
    "provider": "codex",
    "window": "weekly",
    "resets_at": iso(LATER),
    "observed_at": iso(NOW - 100),
    "wake_note": "pm",
}
ONE_BLOCK_BYTES = json.dumps({"wake_note": "keep", "blocks": [ONE_BLOCK]}).encode("utf-8")
BLOCK_LINE = "- codex weekly blocked until"


class held_lock:
    """Another holder of the quota file's lock (a second handle, as another process would have)."""

    def __init__(self, path: Path) -> None:
        self.lock = path.with_name(path.name + ".lock")

    def __enter__(self) -> None:
        self.ledger = us.sibling("git_ledger", TOOLS / "hooks")
        self.handle = self.lock.open("a+b")
        self.ledger._lock(self.handle, 1e18)

    def __exit__(self, *exc: object) -> None:
        self.ledger._unlock(self.handle)
        self.handle.close()


def hook_module_with(hook: Any, monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> None:
    """The hook loads its own copy of usage_snapshot.py on every call: set `attrs` on that copy (the lock-wait
    override, a fault double)."""
    real = hook.load_module

    def load_module(path: Path, name: str) -> Any:
        module = real(path, name)
        if name == "usage_snapshot":
            for attr, value in attrs.items():
                setattr(module, attr, value)
        return module

    monkeypatch.setattr(hook, "load_module", load_module)


def denied(*args: Any, **kwargs: Any) -> Any:
    raise PermissionError(13, "sharing violation (test double)")


# (a) The read contract: bytes, a proven absence, or unreadable. RT5.
@pytest.mark.parametrize(
    ("content", "fault", "expected"),
    [
        (None, "no state folder", "empty"),  # proven: a file cannot exist in a folder that does not
        (None, None, "empty"),  # proven: missing while the lock is held
        (b'{"blocks": []}', None, "empty"),
        (b'{"wake_note": "x"}', None, "empty"),  # what pm-set writes for top-level fields only
        (ONE_BLOCK_BYTES, None, "block"),
        (b"\xef\xbb\xbf" + ONE_BLOCK_BYTES, None, "block"),
        (ONE_BLOCK_BYTES, "lock held", "unreadable"),
        (None, "lock held", "unreadable"),  # no file, but without the lock that is not proven
        (ONE_BLOCK_BYTES, "lock is a link", "unreadable"),
        (ONE_BLOCK_BYTES, "lock is a folder", "unreadable"),
        (ONE_BLOCK_BYTES, "open denied", "unreadable"),
        (b"\xff\xfe", None, "unreadable"),
        (b"{broken", None, "unreadable"),
        (b"", None, "unreadable"),
        (b"[1]", None, "unreadable"),
        (b'{"blocks": 3}', None, "unreadable"),
    ],
)
def test_fix3_read_contract_of_the_session_start_hook(
    hook: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes | None, fault: str | None, expected: str
) -> None:
    path = tmp_path / QUOTA_REL
    lock = path.with_name(path.name + ".lock")
    if content is not None:
        path.write_bytes(content)
    faulty = {"read_bytes": denied} if fault == "open denied" else {}
    hook_module_with(hook, monkeypatch, LOCK_WAIT_SECONDS=0.2, **faulty)
    if fault == "no state folder":
        (tmp_path / "bare").mkdir()
        monkeypatch.setattr(hook, "ROOT", tmp_path / "bare")
        text = hook.quota_block(NOW)
        assert not (tmp_path / "bare" / ".tmp").exists()  # a read creates no folder
    elif fault == "lock held":
        started = time.monotonic()
        with held_lock(path):
            text = hook.quota_block(NOW)
        assert "stayed busy" in text and 0.2 <= time.monotonic() - started < 4  # the reader's own bounded wait
    elif fault == "lock is a link":
        (tmp_path / "elsewhere").mkdir()
        make_link(lock, tmp_path / "elsewhere")
        try:
            text = hook.quota_block(NOW)
        finally:
            remove_link(lock)
        assert "link" in text
    else:
        if fault == "lock is a folder":
            lock.mkdir()
        text = hook.quota_block(NOW)
    if expected == "empty":
        assert text == ""
    elif expected == "block":
        assert text.startswith(f"Quota blocks ({QUOTA_REL}") and BLOCK_LINE in text
    else:
        assert text.startswith(f"Quota blocks file {QUOTA_REL} unreadable: ") and BLOCK_LINE not in text
    assert path.read_bytes() == content if content is not None else not path.exists()


def test_fix3_read_quota_file_proves_absence_or_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state" / "quota-blocks.json"
    assert us.read_quota_file(path) is None and not path.parent.exists()
    path.parent.mkdir()
    assert us.read_quota_file(path) is None  # under the lock
    path.write_bytes(ONE_BLOCK_BYTES)
    assert us.read_quota_file(path) == ONE_BLOCK_BYTES
    monkeypatch.setattr(us, "LOCK_WAIT_SECONDS", 0.2)
    for present in (True, False):
        if not present:
            path.unlink()
        with held_lock(path), pytest.raises(TimeoutError, match="stayed busy for 0.2 s"):
            us.read_quota_file(path)


def test_fix3_a_reader_during_a_commit_waits_and_never_sees_no_block(
    hook: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit in flight holds the lock: the hook, run in the middle of it, reports the file as unreadable (its wait
    is cut to 0.2 s here), never as empty; before and after the commit it prints the block."""
    path = tmp_path / QUOTA_REL
    path.write_bytes(ONE_BLOCK_BYTES)
    hook_module_with(hook, monkeypatch, LOCK_WAIT_SECONDS=0.2)
    during: list[str] = []
    edit = us.PmEdit(None, {"wake_note": "writer-one"}, [], False)

    def plan(raw: bytes | None) -> Any:
        during.append(hook.quota_block(NOW))
        return us.planned_pm_edit(path, raw, edit, NOW)

    assert BLOCK_LINE in hook.quota_block(NOW)
    assert us.transact(path, plan)["status"] == "written"
    assert len(during) == 1 and "unreadable" in during[0] and "stayed busy" in during[0]
    assert BLOCK_LINE in hook.quota_block(NOW)


def test_fix3_the_hook_reads_the_block_while_another_process_commits(
    hook: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real second process commits 150 times; every hook read made meanwhile prints the block: no read is empty
    and none is unreadable (the reader waits its turn on the lock).

    The reader's wait is set above the writer's whole run (fix round 4): on a loaded machine 150 commits take longer
    than the 5 s default, and a reader that polls a lock the writer takes again at once can then run into its bound
    and print `unreadable`, which the contract allows and the bound's own tests cover (0.2 s)."""
    hook_module_with(hook, monkeypatch, LOCK_WAIT_SECONDS=120.0)
    path = tmp_path / QUOTA_REL
    path.write_bytes(ONE_BLOCK_BYTES)
    script = tmp_path / "committer.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "import usage_snapshot as us\n"
        "path = us.Path(sys.argv[1])\n"
        "for i in range(150):\n"
        "    edit = us.PmEdit(None, {'wake_note': i}, [], False)\n"
        "    report = us.transact(path, lambda raw: us.planned_pm_edit(path, raw, edit, 1.0))\n"
        "    assert report['status'] == 'written', report\n",
        encoding="utf-8",
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    args = [sys.executable, "-B", "-X", "faulthandler", str(script), str(path)]
    child = subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    texts: list[str] = []
    try:
        while child.poll() is None:
            texts.append(hook.quota_block(NOW))
        _, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    assert child.returncode == 0, err
    assert texts and [t for t in texts if BLOCK_LINE not in t] == []
    assert json.loads(path.read_bytes())["wake_note"] == 149 and BLOCK_LINE in hook.quota_block(NOW)


def test_fix3_a_commit_replaces_the_stable_name_and_never_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = blocks_file(tmp_path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(b"left by a killed writer")  # overwritten by the next commit, then gone
    replaced: list[tuple[str, str, bool]] = []
    removed: list[str] = []
    real_replace, real_unlink = os.replace, Path.unlink

    def replace(src: Any, dst: Any) -> None:
        replaced.append((str(src), str(dst), path.exists()))
        real_replace(src, dst)

    def unlink(self: Path, missing_ok: bool = False) -> None:
        removed.append(str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(us.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    for name in ("rename", "link"):
        monkeypatch.setattr(us.os, name, denied)  # neither is part of a commit (removals are recorded above)
    assert us.write_quota_blocks(path, snapshot_with([window("weekly", 97, True)]), NOW)["status"] == "written"
    monkeypatch.undo()
    assert replaced == [(str(temp), str(path), True)] and str(path) not in removed
    assert sorted(p.name for p in path.parent.iterdir()) == ["quota-blocks.json", "quota-blocks.json.lock"]
    assert json.loads(path.read_bytes())["wake_note"] == "old"


# (b) Every ISO time that enters the tool carries its UTC offset. RT8.
MOMENT = 1_795_183_380.0  # 2026-11-20T14:03:00Z
SAME_MOMENT = [
    "2026-11-20T14:03:00Z",
    "2026-11-20T14:03:00+00:00",
    "2026-11-20T21:03:00+07:00",
    "2026-11-20T09:03:00-05:00",
]
NO_OFFSET = ["2026-11-20", "2026-11-20T14:03:00", "2026-11-20 14:03:00.5", "20261120T140300"]
NOT_A_TIME = ["tomorrow", "", "9999-12-31T23:59:59-23:59", 1795183380, 1795183380.5, True, ["2026-11-20T14:03:00Z"]]


def codex_row_home(tmp_path: Path, stamp: object, mtime: float) -> Path:
    """A Codex home whose one row carries `stamp` verbatim as its timestamp (`...` leaves the member out)."""
    home = tmp_path / "codex-home"
    folder = home / "sessions" / "2027" / "01" / "15"
    folder.mkdir(parents=True, exist_ok=True)
    window_ = {"used_percent": 12, "window_minutes": 10080, "resets_at": int(LATER)}
    row: dict[str, Any] = {"payload": {"type": "token_count", "rate_limits": {"primary": window_}}}
    if stamp is not ...:
        row["timestamp"] = stamp
    file = folder / "rollout-t.jsonl"
    file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.utime(file, (mtime, mtime))
    return home


def refused_by_codex_row(tmp_path: Path, bad: object) -> bool:  # T1
    snap = us.codex_snapshot(codex_row_home(tmp_path, bad, NOW - 5), NOW, 60, 8)
    return snap["status"] == "unknown" and len(snap["refused"]) == 1 and "windows" not in snap


def refused_by_status_line(tmp_path: Path, bad: object) -> bool:  # T4, T7
    found = []
    for member, parse in (("rate_limits", us.claude_status_windows), ("quota", us.antigravity_windows)):
        file = tmp_path / f"{member}.json"
        file.write_text(json.dumps({"written_at": bad, member: {}}), encoding="utf-8")
        found.append(us.status_observation(file, member, NOW, 60, parse))
    return all(isinstance(f, str) and "lacks a valid written_at" in f for f in found)


def refused_by_antigravity_reset(bad: object) -> bool:  # T8
    [entry_] = us.antigravity_windows({"weekly": {"remaining_fraction": 0.9, "reset_time": bad}}, NOW, True)
    return entry_["state"] == "unknown" and "invalid reset_time" in entry_["reason"] and "resets_at" not in entry_


def refused_in_the_blocks_file(bad: object) -> bool:  # T9
    # As `resets_at` it is no reset time, so the block never expires by it; as `observed_at` the block is legacy, so
    # the later low reading does not clear it.
    unexpiring = {"provider": "codex", "window": "weekly", "resets_at": bad, "observed_at": iso(NOW - 100)}
    legacy = {"provider": "codex", "window": "weekly", "resets_at": iso(LATER), "observed_at": bad}
    low = entry("ok", True, False, NOW - 5)
    return (
        us.updated_blocks([unexpiring], {}, NOW + 10**9) == [unexpiring]
        and us.updated_blocks([legacy], snapshot_with([low], "codex"), NOW) == [legacy]
    )


def refused_by_the_hook(tmp_path: Path, bad: object, hook: Any) -> bool:  # T10
    path = tmp_path / QUOTA_REL
    block = {"provider": "codex", "window": "weekly", "resets_at": bad}
    path.write_text(json.dumps({"blocks": [block]}), encoding="utf-8")
    text = hook.quota_block(NOW + 10**9)
    path.unlink()
    return BLOCK_LINE in text and "no valid reset time" in text and "reset time passed" not in text


def refused_by_pm_set(root: Path, bad: object, monkeypatch: pytest.MonkeyPatch) -> bool:  # T11
    codes = []
    for name in ("resets_at", "observed_at"):
        flag = ["--text", name, bad] if isinstance(bad, str) else ["--json", name, json.dumps(bad)]
        monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "pm-set", "--block", "codex", "weekly", *flag])
        with pytest.raises(SystemExit) as stop:
            us.main()
        codes.append(stop.value.code)
    return codes == [2, 2] and not (root / QUOTA_REL).exists()


@pytest.mark.parametrize("bad", [*NO_OFFSET, *NOT_A_TIME], ids=repr)
def test_fix3_a_time_without_a_utc_offset_is_refused_at_every_entry(
    bad: object, root: Path, hook: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert us.parse_time(bad) is None
    assert refused_by_codex_row(root, bad)
    assert refused_by_status_line(root, bad)
    assert refused_by_antigravity_reset(bad)
    assert refused_in_the_blocks_file(bad)
    assert refused_by_the_hook(root, bad, hook)
    assert refused_by_pm_set(root, bad, monkeypatch)
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("text", SAME_MOMENT)
def test_fix3_a_time_with_any_utc_offset_is_one_moment_at_every_entry(
    text: str, root: Path, hook: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = MOMENT + 50
    assert us.parse_time(text) == MOMENT
    snap = us.codex_snapshot(codex_row_home(root, text, MOMENT - 9000), now, 60, 8)  # T1: the row time, not the mtime
    assert snap["status"] == "ok" and snap["observed_at"] == iso(MOMENT)
    status = root / "c.json"
    status.write_text(json.dumps({"written_at": text, "rate_limits": {}}), encoding="utf-8")
    assert us.status_observation(status, "rate_limits", now, 60, us.claude_status_windows).at == MOMENT  # T4
    [bucket] = us.antigravity_windows({"weekly": {"remaining_fraction": 0.5, "reset_time": text}}, MOMENT - 5, True)
    assert bucket["state"] == "ok" and bucket["resets_at"] == iso(MOMENT)  # T8
    block = {"provider": "codex", "window": "weekly", "resets_at": text, "observed_at": text}
    assert us.updated_blocks([block], {}, MOMENT - 1) == [block]  # T9: the reset is this moment on every host
    assert us.updated_blocks([block], {}, MOMENT + 1) == []
    (root / QUOTA_REL).write_text(json.dumps({"blocks": [block]}), encoding="utf-8")
    assert "reset time passed" in hook.quota_block(MOMENT + 1)  # T10
    assert hook.quota_block(MOMENT - 1).splitlines()[1] == f"{BLOCK_LINE} {text}"
    monkeypatch.setattr(us.time, "time", lambda: now)
    code, report = pm_set(root, monkeypatch, capsys, "--block", "codex", "weekly", "--text", "resets_at", text)  # T11
    [stored] = json.loads((root / QUOTA_REL).read_bytes())["blocks"]
    assert code == 0 and stored["resets_at"] == iso(MOMENT) and stored["observed_at"] == iso(now)  # stored in UTC


def test_fix3_a_codex_row_without_a_timestamp_uses_the_file_time(tmp_path: Path) -> None:
    for stamp in (..., None):  # absent or null: missing, so the file time stands in; anything else must be a time
        snap = us.codex_snapshot(codex_row_home(tmp_path, stamp, NOW - 30), NOW, 60, 8)
        assert snap["status"] == "ok" and snap["observed_at"] == iso(NOW - 30) and "mtime" in snap["observed_from"]


def test_fix3_a_claude_file_dated_after_now_is_not_fresh(tmp_path: Path) -> None:  # T12
    file = tmp_path / "claude.json"
    file.write_text('{"weekly": 3}', encoding="utf-8")
    for mtime, status in ((NOW - 60, "ok"), (NOW, "ok"), (NOW + 2, "unknown"), (NOW + 86400, "unknown")):
        os.utime(file, (mtime, mtime))
        snap = us.claude_snapshot(file, NOW, 60)
        assert snap["status"] == status, mtime
    assert "after now" in snap["reason"]


@settings(max_examples=200, deadline=None)
@given(
    moment=st.datetimes(min_value=dt.datetime(1971, 1, 1), max_value=dt.datetime(9990, 1, 1)),
    minutes=st.integers(min_value=-1439, max_value=1439),
    separator=st.sampled_from(["T", " "]),
)
def test_fix3_parse_time_never_depends_on_the_host_zone(moment: dt.datetime, minutes: int, separator: str) -> None:
    assert us.parse_time(moment.isoformat(sep=separator)) is None  # no offset: refused, whatever the host zone
    assert us.parse_time(moment.date().isoformat()) is None
    aware = moment.replace(tzinfo=dt.timezone(dt.timedelta(minutes=minutes)))
    expected = (aware - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)).total_seconds()
    assert us.parse_time(aware.isoformat(sep=separator)) == expected
    assert us.parse_time(us.iso(expected)) == expected  # what the tool writes, it reads back as the same moment


# (c) The block state machine: one table (`block_step`), reached by the snapshot and by pm-set. RT6, RT7.
def reading(
    state: str = "ok", blocked: bool = False, fresh: bool = True, at: float = NOW - 50, resets: float = LATER
) -> dict[str, Any]:
    return entry(state, fresh, blocked, at) | {"resets_at": iso(resets), "used_percent": 100.0 if blocked else 10.0}


def block_with(**change: Any) -> dict[str, Any]:
    return {name: value for name, value in (ONE_BLOCK | change).items() if value is not ...}


LEGACY = block_with(observed_at=...)
NO_TIMES = block_with(observed_at=..., resets_at=...)
NAIVE_RESET = LEGACY | {"resets_at": "2020-01-01T00:00:00"}
EXPIRED = block_with(resets_at=iso(NOW - 1))
RECORDED = {
    "provider": "codex",
    "window": "weekly",
    "resets_at": iso(LATER),
    "used_percent": 100.0,
    "source": "test double",
    "written_by": "usage_snapshot.py",
}
PM = us.PmEdit
TRANSITIONS: list[tuple[str, Any, Any, Any, Any]] = [
    # (row and case, block, reading, pm edit, next state)
    ("1 a blocked reading records", None, reading(blocked=True), None, RECORDED | {"observed_at": iso(NOW - 50)}),
    ("1 a low reading records nothing", None, reading(), None, None),
    (
        "2 pm records, observed now",
        None,
        None,
        PM(KEY, {"source": "desktop"}, [], False),
        {"provider": "codex", "window": "weekly", "observed_at": iso(NOW), "written_by": "pm", "source": "desktop"},
    ),
    (
        "3 a blocked reading at the block's instant re-asserts",
        ONE_BLOCK,
        reading(blocked=True, at=NOW - 100),
        None,
        RECORDED | {"observed_at": iso(NOW - 100), "wake_note": "pm"},
    ),
    (
        "3 a blocked reading re-asserts a legacy block",
        LEGACY,
        reading(blocked=True, at=NOW - 500),
        None,
        RECORDED | {"observed_at": iso(NOW - 500), "wake_note": "pm"},
    ),
    (
        "4 pm sets resets_at: observed now (RT6)",
        ONE_BLOCK,
        None,
        PM(KEY, {"resets_at": iso(LATER + 9), "source": "new exhaustion"}, [], False),
        ONE_BLOCK | {"resets_at": iso(LATER + 9), "source": "new exhaustion", "observed_at": iso(NOW)},
    ),
    (
        "4 pm sets the same resets_at: still observed now",
        ONE_BLOCK,
        None,
        PM(KEY, {"resets_at": iso(LATER)}, [], False),
        ONE_BLOCK | {"observed_at": iso(NOW)},
    ),
    (
        "4 pm sets resets_at and observed_at: the given time, in UTC",
        ONE_BLOCK,
        None,
        PM(KEY, {"resets_at": "2027-01-15T16:00:00+07:00", "observed_at": "2027-01-15T14:59:00+07:00"}, [], False),
        ONE_BLOCK | {"resets_at": iso(LATER), "observed_at": iso(NOW - 60)},
    ),
    (
        "5 pm sets a metadata field: observed_at untouched",
        ONE_BLOCK,
        None,
        PM(KEY, {"wake_scheduled": True, "wake_note": "task x"}, [], False),
        ONE_BLOCK | {"wake_scheduled": True, "wake_note": "task x"},
    ),
    ("5 pm removes a metadata field", ONE_BLOCK, None, PM(KEY, {}, ["wake_note"], False), block_with(wake_note=...)),
    ("6 pm removes the block", ONE_BLOCK, None, PM(KEY, {}, [], True), None),
    ("7 own reset passed, no reading (RT7)", EXPIRED, None, None, None),
    ("7 own reset passed, legacy, no reading (RT7)", LEGACY | {"resets_at": iso(NOW - 1)}, None, None, None),
    ("7 own reset passed, unknown reading", EXPIRED, reading("unknown"), None, None),
    ("7 own reset passed, older low reading", EXPIRED, reading(at=NOW - 900), None, None),
    (
        "7 own reset passed, an older blocked reading still valid records anew",
        EXPIRED,
        reading(blocked=True, at=NOW - 900),
        None,
        RECORDED | {"observed_at": iso(NOW - 900)},
    ),
    ("7 own reset at now is not passed", block_with(resets_at=iso(NOW)), None, None, block_with(resets_at=iso(NOW))),
    ("8 the window reset after the block", ONE_BLOCK, reading("reset", fresh=False, resets=NOW - 30), None, None),
    ("9 a fresh low reading after the block", ONE_BLOCK, reading(), None, None),
    ("10 a low reading before the block", ONE_BLOCK, reading(at=NOW - 200), None, ONE_BLOCK),
    ("10 a low reading at the block's instant", ONE_BLOCK, reading(at=NOW - 100), None, ONE_BLOCK),
    ("10 a reset of an earlier cycle", ONE_BLOCK, reading("reset", resets=NOW - 100), None, ONE_BLOCK),
    ("10 a blocked reading before the block", ONE_BLOCK, reading(blocked=True, at=NOW - 200), None, ONE_BLOCK),
    ("11 an unknown reading", ONE_BLOCK, reading("unknown"), None, ONE_BLOCK),
    ("11 a stale low reading", ONE_BLOCK, reading(fresh=False), None, ONE_BLOCK),
    ("11 a disabled window", ONE_BLOCK, reading("disabled"), None, ONE_BLOCK),
    ("11 no reading", ONE_BLOCK, None, None, ONE_BLOCK),
    ("12 legacy, a later low reading", LEGACY, reading(at=NOW - 1), None, LEGACY),
    ("12 legacy, the window reset", LEGACY, reading("reset", resets=NOW - 30), None, LEGACY),
    ("13 legacy without a reset time", NO_TIMES, None, None, NO_TIMES),
    ("13 legacy, a reset time without its offset", NAIVE_RESET, reading(), None, NAIVE_RESET),
    ("14 not an object", "free text", reading(blocked=True), None, "free text"),
]
TRANSITION_IDS = [row[0] for row in TRANSITIONS]


@pytest.mark.parametrize(("case", "block", "read", "pm", "after"), TRANSITIONS, ids=TRANSITION_IDS)
def test_fix3_block_step_table(case: str, block: Any, read: Any, pm: Any, after: Any) -> None:
    """In the table None stands for "no block"; the step itself takes and returns `NO_BLOCK` (fix round 4), because
    None is the JSON null a list can hold."""
    before = json.dumps([block, read])
    state = us.NO_BLOCK if block is None else block
    result = us.block_step(KEY, state, NOW, reading=read, pm=pm)
    assert (result is us.NO_BLOCK) if after is None else (result == after)
    assert json.dumps([block, read]) == before  # a pure function: its inputs are not changed


# Every row through its writer; a row without a reading runs for a provider that was read (but does not report the
# window) and for one that could not be read at all.
WRITER_CASES = [
    pytest.param(*row, provider_read, id=f"{row[0]}{'' if provider_read else ', provider unread'}")
    for row in TRANSITIONS
    for provider_read in (True, False)
    if provider_read or (row[2] is None and row[3] is None)
]


@pytest.mark.parametrize(("case", "block", "read", "pm", "after", "provider_read"), WRITER_CASES)
def test_fix3_both_writers_follow_the_table(
    tmp_path: Path, case: str, block: Any, read: Any, pm: Any, after: Any, provider_read: bool
) -> None:
    existing = [] if block is None else [block]
    expected = [] if after is None else [after]
    if pm is not None:
        path = tmp_path / "quota-blocks.json"
        raw = json.dumps({"wake_note": "keep", "blocks": existing}).encode("utf-8")
        planned = us.planned_pm_edit(path, raw, pm, NOW)
        assert isinstance(planned, bytes) and json.loads(planned) == {"wake_note": "keep", "blocks": expected}
        return
    if provider_read:  # read, with this window or (no reading) only another one
        snap = snapshot_with([read if read is not None else reading() | {"label": "other window"}], "codex")
    else:
        snap = {"codex": {"status": "unknown", "reason": "x"}}
    if case.startswith("14"):  # the entry stays as it is; the blocked window, having no block, records one
        expected.append(RECORDED | {"observed_at": read["observed_at"]})
    assert us.updated_blocks(existing, snap, NOW) == expected


def test_fix3_a_pm_re_assertion_outlasts_an_older_reading_and_a_metadata_edit_does_not(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """RT6 through the command line: a low reading taken between the block and the PM's call."""
    path = root / QUOTA_REL
    path.parent.mkdir(parents=True)
    between = snapshot_with([reading(at=NOW - 50)], "codex")
    monkeypatch.setattr(us.time, "time", lambda: NOW)
    for argv, kept in (
        (["--text", "resets_at", iso(LATER + 9000), "--text", "source", "new exhaustion"], True),
        (["--text", "resets_at", iso(LATER)], True),  # the same value: still a new statement
        (["--json", "wake_scheduled", "true", "--text", "wake_note", "task"], False),
    ):
        path.write_text(json.dumps({"blocks": [ONE_BLOCK]}), encoding="utf-8")
        code, report = pm_set(root, monkeypatch, capsys, "--block", "codex", "weekly", *argv)
        assert (code, report["status"]) == (0, "written")
        [block] = json.loads(path.read_bytes())["blocks"]
        assert block["observed_at"] == (iso(NOW) if kept else iso(NOW - 100))
        assert us.updated_blocks([block], between, NOW + 1) == ([block] if kept else [])


def test_fix3_an_entry_with_an_unhashable_name_is_kept_and_still_expires() -> None:
    odd = {"provider": ["codex"], "window": {"w": 1}, "resets_at": iso(LATER)}
    snap = snapshot_with([reading(blocked=True)], "codex")
    assert us.updated_blocks([odd], snap, NOW) == [odd, RECORDED | {"observed_at": iso(NOW - 50)}]
    assert us.updated_blocks([odd], {}, LATER + 1) == []


# (d) One error boundary per command: one JSON report, an exit code, the file as it was. RT9.
def blocked_claude(root: Path) -> None:
    """A status-line reading at 97% of the weekly window, so the snapshot has a block to write."""
    week = {"used_percentage": 97, "resets_at": int(time.time()) + 86400}
    write_status(root / STATE_REL / "claude-statusline.json", time.time() - 5, "rate_limits", {"seven_day": week})


def failing_plan(*args: Any) -> Any:
    raise ValueError("plan failed (test double)")


def no_replace(src: Any, dst: Any) -> None:
    raise OSError(95, "operation not supported (test double)")


def io_error(path: Path) -> bytes:
    raise OSError(5, "input/output error (test double)")


def link_appears(real: Any) -> Any:
    """`state_path` that refuses the path once the folder is to be made (a link put there after the first check)."""

    def state_path(relative: str, root: Path, create: bool = False) -> Path:
        if create:
            raise SystemExit(f"quota path {relative!r} passes through a link (test double)")
        return real(relative, root)

    return state_path


TRANSACTION_FAULTS = ["lock held", "lock is a folder", "read fails", "temp is a folder", "replace fails", "plan fails"]
# Before the transaction: the state folder cannot be made, or the path is refused when it is made.
FAULTS = [*TRANSACTION_FAULTS, ".tmp is a file", "link appears before the folder is made"]


def inject(fault: str, path: Path, monkeypatch: pytest.MonkeyPatch, plan_name: str) -> None:
    monkeypatch.setattr(us, "LOCK_WAIT_SECONDS", 0.2)
    if fault == "lock is a folder":
        path.with_name(path.name + ".lock").mkdir()
    elif fault == "temp is a folder":
        path.with_name(path.name + ".tmp").mkdir()
    elif fault == "read fails":
        monkeypatch.setattr(us, "read_bytes", io_error)
    elif fault == "replace fails":
        monkeypatch.setattr(us.os, "replace", no_replace)
    elif fault == "plan fails":
        monkeypatch.setattr(us, plan_name, failing_plan)
    elif fault == "link appears before the folder is made":
        monkeypatch.setattr(us, "state_path", link_appears(us.state_path))


@pytest.mark.parametrize("fault", TRANSACTION_FAULTS)
def test_fix3_transact_reports_every_failure_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    """The transaction is its own boundary: a caller of `write_quota_blocks` or `transact` gets a report, never an
    exception, and the file is as it was."""
    path = blocks_file(tmp_path)
    inject(fault, path, monkeypatch, "planned_blocks")
    snap = snapshot_with([window("weekly", 97, True)])
    if fault == "lock held":
        with held_lock(path):
            report = us.write_quota_blocks(path, snap, NOW)
    else:
        report = us.write_quota_blocks(path, snap, NOW)
    assert report["status"] == "error" and report["path"] == str(path) and report["reason"]
    assert path.read_text(encoding="utf-8") == PM_START


@pytest.mark.parametrize("command", ["snapshot", "pm-set"])
@pytest.mark.parametrize("fault", FAULTS)
def test_fix3_a_failed_transaction_is_one_error_report(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str, fault: str
) -> None:
    path = root / QUOTA_REL
    if fault == ".tmp is a file":
        (root / ".tmp").write_bytes(b"not a folder")
    else:
        blocks_file(root)
        blocked_claude(root)
    inject(fault, path, monkeypatch, "planned_blocks" if command == "snapshot" else "planned_pm_edit")
    own =["--codex-home", str(root / "no-codex")] if command == "snapshot" else ["pm-set", "--text", "wake_note", "NEW"]
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", *own])
    if fault == "lock held":
        with held_lock(path):
            code = us.main()
    else:
        code = us.main()
    captured = capsys.readouterr()
    printed = json.loads(captured.out)  # exactly one JSON document
    report = printed["quota_blocks"] if command == "snapshot" else printed
    assert code == 1 and report["status"] == "error" and report["reason"]
    assert "Traceback" not in captured.err
    if fault == ".tmp is a file":
        assert (root / ".tmp").read_bytes() == b"not a folder"
        return
    if command == "snapshot":
        assert printed["claude"]["blocked"] is True  # the readings are still printed
    assert path.read_text(encoding="utf-8") == PM_START  # the file as it was
    left = {p.name for p in path.parent.iterdir()} - {"claude-statusline.json", "quota-blocks.json"}
    assert left <= {"quota-blocks.json.lock"} | ({"quota-blocks.json.tmp"} if fault == "temp is a folder" else set())


def test_fix3_exit_codes_of_both_commands(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocks_file(root)
    blocked_claude(root)
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "--codex-home", str(root / "no-codex")])
    assert us.main() == 0 and json.loads(capsys.readouterr().out)["quota_blocks"]["status"] == "written"
    assert us.main() == 0 and json.loads(capsys.readouterr().out)["quota_blocks"]["status"] == "unchanged"
    (root / QUOTA_REL).write_text("{broken", encoding="utf-8")  # a file that cannot be read as blocks: not success
    assert us.main() == 1 and json.loads(capsys.readouterr().out)["quota_blocks"]["status"] == "error"
    assert pm_set(root, monkeypatch, capsys, "--text", "wake_note", "x")[0] == 1
    assert (root / QUOTA_REL).read_text(encoding="utf-8") == "{broken"
    # A refused configuration or path: its message, no JSON, nothing touched, for both commands alike.
    bad = profile(root, quota_blocks_file="notes/quota.json")
    for argv in (["--config", str(bad)], ["pm-set", "--config", str(bad), "--text", "wake_note", "x"]):
        monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", *argv])
        with pytest.raises(SystemExit) as stop:
            us.main()
        assert "under .tmp/pm-state/" in str(stop.value.code) and capsys.readouterr().out == ""
    assert not (root / "notes").exists()


@pytest.mark.parametrize("fault", ["lock held", "lock is a folder", "read fails"])
def test_fix3_the_hook_reports_every_fault_and_never_raises(
    hook: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fault: str
) -> None:
    path = tmp_path / QUOTA_REL
    path.write_bytes(ONE_BLOCK_BYTES)
    faulty = {"read_bytes": io_error} if fault == "read fails" else {}
    hook_module_with(hook, monkeypatch, LOCK_WAIT_SECONDS=0.2, **faulty)
    if fault == "lock is a folder":
        path.with_name(path.name + ".lock").mkdir()
    if fault == "lock held":
        with held_lock(path):
            text = session_text(hook, monkeypatch, capsys)
    else:
        text = session_text(hook, monkeypatch, capsys)
    assert f"Quota blocks file {QUOTA_REL} unreadable: " in text and BLOCK_LINE not in text
    assert "- working" in text  # the rest of the session start is still printed


# ---- Fix round 4 (retest 3: RT10-RT13; decision 114) -----------------------------------------------------------
# One validator of the stored quota format (`us.stored_blocks`, `us.stored_status`, over `us.decode_object`); every
# consumer branches on its tag. Each shape below is one row, run through each consumer. Every value is a synthetic
# test double; times are relative to the real clock, which the commands and the status line read.

UTF8_BOM = b"\xef\xbb\xbf"
AHEAD = iso(time.time() + 30 * 86400)
LIVE = {
    "provider": "codex",
    "window": "weekly",
    "resets_at": AHEAD,
    "observed_at": iso(time.time() - 100),
    "wake_note": "pm",
}
LIVE_JSON = json.dumps(LIVE)


def doc(blocks_text: str, more: str = "") -> bytes:
    return ('{"wake_note": "keep", ' + more + '"blocks": ' + blocks_text + "}").encode("utf-8")


# (name, bytes of the file or None for no file, tag of the validator, whether the file holds one readable block)
BLOCKS_SHAPES: list[tuple[str, bytes | None, str, bool]] = [
    ("absent", None, "absent", False),
    ("empty object", b"{}", "valid", False),
    ("object without blocks", b'{"wake_note": "keep"}', "valid", False),
    ("empty list", doc("[]"), "valid", False),
    ("one block", doc(f"[{LIVE_JSON}]"), "valid", True),
    ("one block after a BOM", UTF8_BOM + doc(f"[{LIVE_JSON}]"), "valid", True),
    ("empty file", b"", "malformed", False),
    ("white space only", b" \r\n\t", "malformed", False),
    ("BOM only", UTF8_BOM, "malformed", False),
    ("BOM and white space", UTF8_BOM + b" \r\n", "malformed", False),
    ("two BOMs", UTF8_BOM + UTF8_BOM + doc("[]"), "malformed", False),
    ("invalid UTF-8", b"\xff\xfe\x00", "malformed", False),
    ("UTF-16", doc("[]").decode("utf-8").encode("utf-16"), "malformed", False),
    ("broken JSON", b'{"blocks": [', "malformed", False),
    ("top level is a list", f"[{LIVE_JSON}]".encode(), "malformed", False),
    ("top level is null", b"null", "malformed", False),
    ("top level is text", b'"blocks"', "malformed", False),
    ("two objects", doc("[]") + doc("[]"), "malformed", False),
    ("text after the object", doc("[]") + b" x", "malformed", False),
    ("blocks is null", doc("null"), "malformed", False),
    ("blocks is an object", doc("{}"), "malformed", False),
    ("blocks is false", doc("false"), "malformed", False),
    ("blocks twice", ('{"blocks": [' + LIVE_JSON + '], "blocks": []}').encode(), "malformed", False),
    ("another member twice", doc("[]", '"n": 1, "n": 2, '), "malformed", False),
    (
        "a member twice inside a block",
        doc('[{"provider": "codex", "provider": "claude", "window": "weekly"}]'),
        "malformed",
        False,
    ),
    ("NaN", doc("[]", '"n": NaN, '), "malformed", False),
    (
        "Infinity inside a block",
        doc('[{"provider": "codex", "window": "weekly", "used_percent": Infinity}]'),
        "malformed",
        False,
    ),
    ("a number beyond a float", doc("[]", '"n": 1e999, '), "malformed", False),
    ("entry null", doc("[null]"), "malformed", False),
    ("entries false and a number", doc("[false, 71]"), "malformed", False),
    ("entry text", doc('["truncated entry"]'), "malformed", False),
    ("entry list", doc("[[]]"), "malformed", False),
    ("null beside a block", doc(f"[null, {LIVE_JSON}]"), "malformed", True),
    ("text after a block", doc(f'[{LIVE_JSON}, "truncated entry"]'), "malformed", True),
]
BLOCKS_IDS = [shape[0] for shape in BLOCKS_SHAPES]
BLOCKS_ROW = ("shape", "raw", "tag", "shows_block")


def put(path: Path, raw: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)


def same_as(path: Path, raw: bytes | None) -> bool:
    return path.read_bytes() == raw if raw is not None else not path.exists()


def plain(raw: bytes | None) -> dict[str, Any]:
    return {} if raw is None else json.loads(raw.decode("utf-8-sig"))


@pytest.mark.parametrize(BLOCKS_ROW, BLOCKS_SHAPES, ids=BLOCKS_IDS)
def test_fix4_the_validator_tags_each_shape(shape: str, raw: bytes | None, tag: str, shows_block: bool) -> None:
    stored = us.stored_blocks(raw)
    assert stored.tag == tag
    assert bool(stored.problems) == (tag == "malformed")  # a malformed result always says why
    assert (stored.document is not None) == (tag == "valid" )  # only a valid file gives the writers a document
    assert list(stored.blocks) == ([LIVE] if shows_block else [])  # a readable block is kept beside a bad entry


@pytest.mark.parametrize("wants_write", [False, True], ids=["nothing blocked", "a window blocked"])
@pytest.mark.parametrize(BLOCKS_ROW, BLOCKS_SHAPES, ids=BLOCKS_IDS)
def test_fix4_shapes_through_the_snapshot_command(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    raw: bytes | None,
    tag: str,
    shows_block: bool,
    wants_write: bool,
) -> None:
    path = root / QUOTA_REL
    put(path, raw)
    if wants_write:
        blocked_claude(root)
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "--codex-home", str(root / "no-codex")])
    code = us.main()
    captured = capsys.readouterr()
    report = json.loads(captured.out)["quota_blocks"]  # exactly one JSON document
    assert "Traceback" not in captured.err
    if tag == "malformed":  # RT10, RT12: an error, never "no block"; nothing written whatever the snapshot wanted
        assert (code, report["status"]) == (1, "error") and same_as(path, raw)
        assert report["problems"] and all(problem in report["reason"] for problem in report["problems"])
    elif wants_write:
        assert (code, report["status"]) == (0, "written")
        after = us.stored_blocks(path.read_bytes())
        assert after.tag == "valid" and report["blocks"] == len(after.blocks) == (2 if shows_block else 1)
        assert after.blocks[-1]["provider"] == "claude" and list(after.blocks[:-1]) == ([LIVE] if shows_block else [])
        rest = {name: value for name, value in plain(path.read_bytes()).items() if name != "blocks"}
        assert rest == {name: value for name, value in plain(raw).items() if name != "blocks"}
    else:  # absent stays absent: no file is made for nothing
        assert (code, report["status"]) == (0, "unchanged") and same_as(path, raw)
        assert report["blocks"] == (1 if shows_block else 0)


@pytest.mark.parametrize("edit", [["--text", "wake_note", "NEW"], ["--block", "codex", "weekly", "--remove-block"]])
@pytest.mark.parametrize(BLOCKS_ROW, BLOCKS_SHAPES, ids=BLOCKS_IDS)
def test_fix4_shapes_through_pm_set(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    raw: bytes | None,
    tag: str,
    shows_block: bool,
    edit: list[str],
) -> None:
    path = root / QUOTA_REL
    put(path, raw)
    code, report = pm_set(root, monkeypatch, capsys, *edit)
    if tag == "malformed":  # RT10: the file is not overwritten
        assert (code, report["status"]) == (1, "error") and same_as(path, raw)
        assert report["problems"] and all(problem in report["reason"] for problem in report["problems"])
        return
    assert code == 0
    if "--text" in edit:
        assert report["status"] == "written" and plain(path.read_bytes()) == plain(raw) | {"wake_note": "NEW"}
    elif shows_block:
        assert report["status"] == "written" and plain(path.read_bytes()) == plain(raw) | {"blocks": []}
    else:  # nothing to remove: unchanged, and no file is made for it
        assert report["status"] == "unchanged" and same_as(path, raw)


@pytest.mark.parametrize(BLOCKS_ROW, BLOCKS_SHAPES, ids=BLOCKS_IDS)
def test_fix4_shapes_through_the_session_start_hook(
    hook: Any, tmp_path: Path, shape: str, raw: bytes | None, tag: str, shows_block: bool
) -> None:
    path = tmp_path / QUOTA_REL
    put(path, raw)
    text = hook.quota_block(time.time())
    lines = text.split("\n") if text else []
    assert any(line.startswith(BLOCK_LINE) for line in lines) == shows_block  # a valid block is always shown
    diagnostics = [line for line in lines if "unreadable" in line]
    if tag == "malformed":  # RT11: one line that names every unreadable part, never empty text
        assert len(diagnostics) == 1 and diagnostics[0].startswith(f"Quota blocks file {QUOTA_REL} unreadable: ")
        assert all(problem[:80] in diagnostics[0] for problem in us.stored_blocks(raw).problems)
    else:
        assert diagnostics == [] and (text == "") == (not shows_block)  # empty only for no file or no block
    assert same_as(path, raw)


def test_fix4_every_unreadable_entry_is_listed(
    root: Path, hook: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / QUOTA_REL
    raw = doc(f'[null, {LIVE_JSON}, "truncated entry", 71]')
    put(path, raw)
    code, report = pm_set(root, monkeypatch, capsys, "--text", "wake_note", "NEW")
    assert code == 1 and [problem.split(" ")[0] for problem in report["problems"]] == ["blocks[0]", "blocks[2]", "blocks[3]"]
    assert "null" in report["problems"][0] and "truncated entry" in report["problems"][1]
    text = hook.quota_block(time.time())
    assert text.count("\n") == 2 and all(name in text for name in ("blocks[0]", "blocks[2]", "blocks[3]", BLOCK_LINE))
    assert same_as(path, raw)


# RT12: "no block" is a value of its own, so no JSON value of a list is mistaken for it.
@pytest.mark.parametrize("entry", [None, False, 0, 17, "", "free text", [], [None]])
def test_fix4_an_entry_that_is_no_object_is_never_no_block(entry: Any) -> None:
    for event in ({}, {"reading": reading(blocked=True)}, {"reading": reading()}, {"pm": PM(KEY, {}, [], True)}):
        after = us.block_step(KEY, entry, NOW, **event)  # row 14, whatever the event
        assert after is not us.NO_BLOCK and type(after) is type(entry) and after == entry
    assert us.updated_blocks([entry, ONE_BLOCK], {}, NOW) == [entry, ONE_BLOCK]
    recorded = us.updated_blocks([entry], snapshot_with([reading(blocked=True)], "codex"), NOW)
    assert recorded[0] == entry and len(recorded) == 2  # kept, and the blocked window still records its block


def test_fix4_no_block_is_its_own_value() -> None:
    assert us.block_step(KEY, us.NO_BLOCK, NOW) is us.NO_BLOCK and repr(us.NO_BLOCK) == "NO_BLOCK"
    assert us.block_step(KEY, us.NO_BLOCK, NOW, reading=reading(blocked=True))["provider"] == "codex"  # row 1
    assert us.block_step(KEY, ONE_BLOCK, NOW, pm=PM(KEY, {}, [], True)) is us.NO_BLOCK  # row 6
    assert us.updated_blocks([], {}, NOW) == [] and us.updated_blocks([None], {}, NOW) == [None]


# PM answer 1 of round 4: a write keeps values, not spellings; a malformed file is not written at all (above).
ODD = (
    '{ "n":1.50,"big":1e2 ,  "text":"\\u00e9",\r\n "blocks":[ {"provider":"codex","window":"weekly","resets_at":"'
    + AHEAD
    + '","used_percent":97.0,"x":[1,  2]} ] }'
).encode("utf-8")


def test_fix4_pm_set_writes_a_valid_file_again_with_equal_values(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / QUOTA_REL
    put(path, ODD)
    code, report = pm_set(root, monkeypatch, capsys, "--block", "codex", "weekly", "--text", "wake_note", "task")
    assert (code, report["status"]) == (0, "written")
    written = path.read_bytes()
    expected = plain(ODD)
    expected["blocks"][0]["wake_note"] = "task"
    assert plain(written) == expected  # every value it did not edit is equal
    assert b"1.50" not in written and b'"n": 1.5,' in written and "\u00e9".encode() in written  # canonical spelling
    assert b"\r\n" in written and b"\n" not in written.replace(b"\r\n", b"")  # the file's own line ending
    assert us.stored_blocks(written).tag == "valid"


def test_fix4_the_snapshot_renders_the_blocks_value_and_keeps_the_rest(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / QUOTA_REL
    put(path, ODD)
    blocked_claude(root)
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "--codex-home", str(root / "no-codex")])
    assert us.main() == 0 and json.loads(capsys.readouterr().out)["quota_blocks"]["status"] == "written"
    original, written = ODD.decode("utf-8"), path.read_bytes().decode("utf-8")
    start, end = original.index("[ {"), original.rindex("]") + 1
    assert written.startswith(original[:start]) and written.endswith(original[end:])  # outside the value: as it was
    blocks = json.loads(written)["blocks"]
    assert blocks[0] == plain(ODD)["blocks"][0] and blocks[1]["provider"] == "claude" and len(blocks) == 2
    assert '"x":[1,  2]' not in written  # inside the value: equal values, rendered again


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999", '{"a": 1, "a": 2}', "[1,", ""])
def test_fix4_pm_set_refuses_a_value_the_file_could_not_hold(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as stop:
        pm_set(root, monkeypatch, capsys, "--json", "n", value)
    assert stop.value.code == 2 and not (root / QUOTA_REL).exists()


def test_fix4_the_claude_file_reason_tells_missing_from_malformed(tmp_path: Path) -> None:
    file = tmp_path / "claude-usage.json"
    assert us.claude_snapshot(file, NOW, 60)["reason"] == "file missing"
    for raw in (b"", b" \n", UTF8_BOM, b"\xff", b"{broken", b"[1]", b'{"a": NaN}', b'{"a": 1, "a": 2}'):
        file.write_bytes(raw)
        snap = us.claude_snapshot(file, time.time() + 5, 60)
        assert snap["status"] == "unknown" and snap["reason"].startswith("file is not a JSON object: ")


# The status-line documents. Per source: the member name, a valid window, one with a wrongly typed value, one with
# a value out of range, and the line's text for an empty member.
STATUS_SOURCES = {
    "claude": (
        "rate_limits",
        json.dumps({"used_percentage": 40, "resets_at": int(time.time()) + 86400}),
        '{"used_percentage": "40"}',
        '{"used_percentage": 250}',
        "claude no limit window",
    ),
    "antigravity": (
        "quota",
        '{"remaining_fraction": 0.6}',
        '{"remaining_fraction": "0.6"}',
        '{"remaining_fraction": 7}',
        "agy no quota bucket",
    ),
}
STAMP = json.dumps(iso(time.time() - 5))


def status_cases(source: str) -> list[tuple[str, str, bytes | None, str, Any, str]]:
    """(source, name, bytes of the stored file or None, tag, window states of a valid file by name, what the status
    line prints when the same bytes are its input: `input` unreadable, `yet` for an absent member, `refused` for a
    member that is no object, `empty`, or `windows`)."""
    member, good, mistyped, out_of_range, _ = STATUS_SOURCES[source]

    def stored(value: str, written: str = STAMP) -> bytes:
        return ('{"written_at": ' + written + ', "' + member + '": ' + value + "}").encode("utf-8")

    rows: list[tuple[str, bytes | None, str, Any, str]] = [
        ("absent", None, "absent", None, "yet"),
        ("empty file", b"", "malformed", None, "input"),
        ("white space only", b"\n \t", "malformed", None, "input"),
        ("BOM only", UTF8_BOM, "malformed", None, "input"),
        ("invalid UTF-8", b"\xff" + stored("{}"), "malformed", None, "input"),
        ("broken JSON", stored("{}")[:-1], "malformed", None, "input"),
        ("top level is a list", b"[" + stored("{}") + b"]", "malformed", None, "input"),
        ("NaN in a window", stored('{"w": {"used_percentage": NaN, "remaining_fraction": NaN}}'), "malformed", None, "input"),
        ("member twice", stored("{}")[:-1] + f', "{member}": {{"w": {good}}}}}'.encode(), "malformed", None, "input"),
        ("no written_at", f'{{"{member}": {{}}}}'.encode(), "malformed", None, "empty"),
        ("written_at without its offset", stored("{}", '"2026-11-15T10:00:00"'), "malformed", None, "empty"),
        ("member missing", b'{"written_at": ' + STAMP.encode() + b"}", "malformed", None, "yet"),
        ("member null", stored("null"), "malformed", None, "yet"),
        ("member is a list", stored("[1]"), "malformed", None, "refused"),
        ("member is text", stored('"40%"'), "malformed", None, "refused"),
        ("empty member", stored("{}"), "valid", {}, "empty"),
        ("one valid window", stored(f'{{"w": {good}}}'), "valid", {"w": "ok"}, "windows"),
        ("a window that is no object", stored('{"independent-bucket": []}'), "valid", {"independent-bucket": "unknown"}, "windows"),
        ("a window that is null", stored('{"w": null}'), "valid", {"w": "unknown"}, "windows"),
        ("a wrongly typed value", stored(f'{{"w": {mistyped}}}'), "valid", {"w": "unknown"}, "windows"),
        ("a value out of range", stored(f'{{"w": {out_of_range}}}'), "valid", {"w": "unknown"}, "windows"),
        ("a window without a value", stored('{"w": {}}'), "valid", {"w": "unknown"}, "windows"),
        ("valid beside malformed", stored(f'{{"a": {good}, "b": [], "c": {mistyped}}}'), "valid", {"a": "ok", "b": "unknown", "c": "unknown"}, "windows"),
    ]
    return [(source, *row) for row in rows]


STATUS_CASES = [case for source in STATUS_SOURCES for case in status_cases(source)]
STATUS_IDS = [f"{case[0]}, {case[1]}" for case in STATUS_CASES]
STATUS_ROW = ("source", "shape", "raw", "tag", "states", "line_kind")


def usage_command(argv: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
    """Test double of the Antigravity usage command: another source that reads fine."""
    return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")


def provider_reading(source: str, file: Path, now: float, other_source: bool = False) -> dict[str, Any]:
    if source == "claude":
        if other_source:
            worker_log(us.JOBS, "w1", weekly_event(0.2, resets=now + 86400), now - 20)
        return us.claude_auto_snapshot(file, us.JOBS, now, 60, 8)
    command = ("agy", 5, usage_command) if other_source else (None, None)
    return us.antigravity_auto_snapshot(file, now, 60, *command)


@pytest.mark.parametrize(STATUS_ROW, STATUS_CASES, ids=STATUS_IDS)
def test_fix4_status_shapes_through_the_validator_and_the_snapshot(
    root: Path, source: str, shape: str, raw: bytes | None, tag: str, states: Any, line_kind: str
) -> None:
    member = STATUS_SOURCES[source][0]
    stored = us.stored_status(raw, member)
    assert stored.tag == tag and bool(stored.problems) == (tag == "malformed")
    file = root / STATE_REL / f"{source}-statusline.json"
    put(file, raw)
    now = time.time()
    alone = provider_reading(source, file, now)
    if tag == "absent":  # no source: said, and no finding
        assert alone["status"] == "unknown" and "no status-line file" in alone["reason"] and "refused" not in alone
    elif tag == "malformed":  # never a reading, never "unlimited", and named
        assert alone["status"] == "unknown" and alone.get("windows", []) == [] and "unlimited" not in alone
        assert len(alone["refused"]) == 1 and str(file) in alone["refused"][0]
        assert all(problem in alone["refused"][0] for problem in stored.problems)
    else:  # each window judged on its own: a valid one is read beside a malformed one
        assert {w["label"]: w["state"] for w in alone["windows"]} == states and "refused" not in alone
        assert alone["status"] == ("ok" if not states or "ok" in states.values() else "unknown")
        assert alone.get("unlimited", False) is (not states)  # only the valid empty member states no limit
    # Beside another source that reads fine, a malformed file is still a finding; an absent one is not.
    beside = provider_reading(source, file, now, other_source=True)
    assert ("refused" in beside) == (tag == "malformed")
    if tag != "valid":  # the other source answers
        assert beside["status"] == "ok"
    assert same_as(file, raw)


def status_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: bytes, source: str) -> str:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert sl.main(["--source", source]) == 0
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize(STATUS_ROW, STATUS_CASES, ids=STATUS_IDS)
def test_fix4_status_shapes_through_the_status_line(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    shape: str,
    raw: bytes | None,
    tag: str,
    states: Any,
    line_kind: str,
) -> None:
    member, _, _, _, empty_text = STATUS_SOURCES[source]
    file = root / STATE_REL / f"{source}-statusline.json"
    line = status_line(monkeypatch, capsys, raw if raw is not None else b'{"session_id": "s"}', source)
    # The snapshot reads a second later: `written_at` is rounded to a microsecond, so read in the same clock tick
    # it can lie a fraction of one after `time.time()` and be refused as a future time (.tmp/T-026/fix4/clock.txt).
    later = time.time() + 1
    if line_kind == "yet":  # "not yet" is for an absent member only, never for input that cannot be read
        assert line == f"{source}: no {member} yet" and not file.exists()
        return
    # RT13: the no-limit text is for a valid empty member only.
    assert "yet" not in line and (line_kind == "empty" or ("no limit" not in line and "no quota" not in line))
    if line_kind == "input":
        assert line.startswith(f"{source}: input unreadable: ") and not file.exists()
    elif line_kind == "refused":
        assert line == f"{source}: {member} is not an object, not recorded" and not file.exists()
    elif line_kind == "empty":
        assert line == empty_text
        assert provider_reading(source, file, later)["unlimited"] is True
    else:
        # RT14 order: `<prefix> unreadable: <bad windows>; <valid windows>`.
        _, found, tail = line.partition("unreadable: ")
        unreadable, _, shown = tail.partition("; ") if found else ("", "", line)
        shown = " " + shown
        for name, state in states.items():  # the line and the snapshot judge each window alike
            assert (f"{name} (" in unreadable) == (state == "unknown") and (f" {name} " in shown) == (state == "ok")
        read = provider_reading(source, file, later)
        assert {w["label"]: w["state"] for w in read["windows"]} == states
    if file.exists():  # what it stored is what the validator calls valid, the member verbatim
        stored = us.stored_status(file.read_bytes(), member)
        assert stored.tag == "valid" and stored.member == plain(raw)[member]


def test_fix4_the_status_line_prints_each_kind_of_window(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    quota = {
        "amount": {"remaining_amount": 3},
        "bad-amount": {"remaining_amount": 3, "reset_time": "tomorrow"},
        "off": {"remaining_fraction": 0.5, "disabled": True},
        "spent": {"remaining_amount": 0},
        "weekly": {"remaining_fraction": 0.25},
    }
    line = status_line(monkeypatch, capsys, json.dumps({"quota": quota}).encode(), "antigravity")
    assert line == "agy unreadable: bad-amount (invalid reset_time: \"tomorrow\"); amount 3 left, off disabled, spent 0% left, weekly 25% left"
    limits = {"five_hour": {"used_percentage": 250}, "seven_day": {"used_percentage": 41.2}}
    line = status_line(monkeypatch, capsys, json.dumps({"rate_limits": limits}).encode(), "claude")
    assert line == "claude unreadable: five_hour (invalid used_percentage: 250); seven_day 41% used"
    # `record` called directly (no decoded input in front of it): the same rules, and nothing the validator
    # refuses is stored.
    now = dt.datetime.now(dt.UTC)
    file = root / STATE_REL / "claude-statusline.json"
    before = file.read_bytes()
    assert sl.record("claude", [1], None, now) == "claude: input unreadable: not a JSON object"
    not_json = sl.record("claude", {"rate_limits": {"w": {"used_percentage": float("nan")}}}, None, now)
    assert not_json.startswith("claude: not recorded: not one JSON object: ") and file.read_bytes() == before


# Fix RT14: the diagnostic comes first and is never cut; only whole valid entries give way, ending with `+N more`.
LINE_SOURCES = {
    # source: (member, prefix, a valid window, its text for `name`, reason of a window that is no object)
    "claude": ("rate_limits", "claude", {"used_percentage": 37}, "{} 37% used", "window is not an object"),
    "antigravity": ("quota", "agy", {"remaining_fraction": 0.63}, "{} 63% left", "bucket is not an object"),
}


def parse_fitted(line: str, prefix: str) -> tuple[str, list[str], int]:
    """(diagnostic text, kept valid entries, omitted count) of a line `<prefix> [unreadable: D][; ]E1, E2[, +N more]`,
    read apart from the code under test (names here never hold a comma or semicolon)."""
    rest = line.removeprefix(f"{prefix} ")
    diagnostic = ""
    if rest.startswith("unreadable: "):
        diagnostic, _, rest = rest.removeprefix("unreadable: ").partition("; ")
    entries = [item for item in rest.split(", ") if item]
    omitted = 0
    if entries and entries[-1].endswith(" more") and entries[-1].startswith("+"):
        omitted = int(entries.pop().removeprefix("+").removesuffix(" more"))
    return diagnostic, entries, omitted


LINE_ROW = "source, valid, broken, long_valid, long_broken"
LINE_CASES = [
    ("claude", 0, 1, 0, 0),  # the diagnostic alone
    ("antigravity", 0, 1, 0, 0),
    ("claude", 3, 1, 0, 0),  # fits whole: no `+N more`
    ("antigravity", 3, 1, 0, 0),
    ("claude", 8, 1, 0, 0),  # the reviewer's probe: many valid windows and a diagnostic
    ("antigravity", 8, 1, 0, 0),
    ("claude", 40, 3, 0, 0),
    ("claude", 8, 0, 0, 0),  # many valid windows, no diagnostic
    ("antigravity", 8, 0, 0, 0),
    ("claude", 3, 0, 0, 0),  # fits whole
    ("claude", 1, 0, 300, 0),  # one very long valid name: dropped whole, never cut
    ("antigravity", 2, 0, 300, 0),
    ("claude", 4, 1, 0, 300),  # one very long malformed name: the diagnostic alone exceeds the limit and stays whole
    ("antigravity", 0, 1, 0, 300),
]


@pytest.mark.parametrize(LINE_ROW, LINE_CASES)
def test_fix5_the_status_line_keeps_the_diagnostic_and_cuts_whole_entries(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    valid: int,
    broken: int,
    long_valid: int,
    long_broken: int,
) -> None:
    member, prefix, good, template, reason = LINE_SOURCES[source]
    windows: dict[str, Any] = {}
    for i in range(valid):
        windows[("v" * long_valid if long_valid and i == 0 else "") + f"bucket-{i:02d}"] = dict(good)
    for i in range(broken):
        windows[("b" * long_broken if long_broken and i == 0 else "") + f"broken-{i}"] = []
    line = status_line(monkeypatch, capsys, json.dumps({member: windows}).encode(), source)
    valid_names = sorted(name for name, value in windows.items() if value != [])  # the validator lists by name
    broken_names = sorted(name for name, value in windows.items() if value == [])
    diagnostic, entries, omitted = parse_fitted(line, prefix)
    # Every malformed window is named whole, with its reason, whatever the width.
    assert diagnostic == ", ".join(f"{name} ({reason})" for name in broken_names), line
    # The kept entries are the first ones, whole and in order; the rest are only counted.
    assert entries == [template.format(name) for name in valid_names[: len(entries)]], line
    assert len(entries) + omitted == len(valid_names), line
    assert (omitted > 0) == (line.endswith(f"+{omitted} more")), line
    # Within the limit unless the diagnostic alone is over it (then no entry is kept).
    assert len(line) <= sl.LINE_CHARS or not entries, line
    # Maximal: one more entry would not have fitted.
    if omitted:
        more = len(entries) + 1
        parts = [template.format(name) for name in valid_names[:more]]
        parts += [f"+{len(valid_names) - more} more"] if more < len(valid_names) else []
        head = f"{prefix} " + (f"unreadable: {diagnostic}; " if diagnostic else "")
        assert len(head + ", ".join(parts)) > sl.LINE_CHARS, line
    if not broken_names and not omitted:
        assert "unreadable" not in line


def test_fix5_the_reviewers_probe_shows_the_diagnostic_and_counts_the_rest(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retest 4 `test_full_cli_keeps_diagnostic_after_many_valid_windows`. Its second assertion (every name in the
    line) asks for what the line cannot hold; the PM rule is to count what is dropped, so the names are checked for
    the kept entries and the count for the rest."""
    for source, (member, prefix, good, template, reason) in LINE_SOURCES.items():
        windows: dict[str, Any] = {f"bucket-{i:02d}": dict(good) for i in range(8)}
        windows["broken"] = []
        line = status_line(monkeypatch, capsys, json.dumps({member: windows}).encode(), source)
        assert "unreadable" in line and "broken" in line and len(line) <= sl.LINE_CHARS, line
        _, entries, omitted = parse_fitted(line, prefix)
        assert 0 < omitted < 8 and len(entries) + omitted == 8 and f"bucket-{len(entries) - 1:02d}" in line, line


def test_fix5_a_line_that_is_not_the_quota_line_is_still_one_short_row(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / ("x" * 200 + ".json")  # a refusal that names this path is longer than the limit
    line = run_statusline(monkeypatch, capsys, claude_payload(), "--source", "claude", "--config", str(missing))
    assert line.startswith("quota not recorded:") and len(line) == sl.LINE_CHARS


def test_fix5_fitted_cuts_at_the_limit_by_whole_entries() -> None:
    limit = sl.LINE_CHARS
    assert limit == 160
    fits = ["a" * 50, "b" * 50, "c" * 49]  # "claude " + 149 + two ", " is exactly 160
    assert len(sl.fitted("claude", "", fits)) == limit
    assert sl.fitted("claude", "", fits) == "claude " + ", ".join(fits)
    over = ["a" * 50, "b" * 50, "c" * 50]
    assert sl.fitted("claude", "", over) == f"claude {over[0]}, {over[1]}, +1 more"
    assert sl.fitted("claude", "x (bad)", ["a" * 200]) == "claude unreadable: x (bad); +1 more"
    exact = ["a" * 71, "b" * 71, "c" * 50]  # the cut line with its count is exactly 160 and stays
    assert sl.fitted("claude", "", exact) == f"claude {exact[0]}, {exact[1]}, +1 more" and len(sl.fitted("claude", "", exact)) == limit
    exact[1] += "b"  # one character more: the second entry goes too
    assert sl.fitted("claude", "", exact) == f"claude {exact[0]}, +2 more"
    assert sl.fitted("claude", "", ["a" * 200]) == "claude +1 more"
    assert sl.fitted("claude", "x" * 300, []) == "claude unreadable: " + "x" * 300
    assert sl.fitted("claude", "x" * 300, ["a"]) == "claude unreadable: " + "x" * 300 + "; +1 more"


NAME_CHARS = st.characters(min_codepoint=33, max_codepoint=126, exclude_characters=",;:")
WINDOWS = st.dictionaries(
    st.text(NAME_CHARS, min_size=1, max_size=45),
    st.one_of(st.just(True), st.just(False)),  # True: a valid window, False: a window that is no object
    max_size=40,
)


@pytest.mark.parametrize("source", sorted(LINE_SOURCES))
@settings(max_examples=300, deadline=None)
@given(flags=WINDOWS)
def test_fix5_property_a_malformed_window_is_always_rendered(source: str, flags: dict[str, bool]) -> None:
    """Any mix of valid and malformed windows, at any count: every malformed window is named whole and first, and
    the valid ones are a prefix with a correct count."""
    member, prefix, good, template, reason = LINE_SOURCES[source]
    windows = {name: dict(good) if ok else [] for name, ok in flags.items()}
    line = sl.quota_line(source, windows, us)
    diagnostic, entries, omitted = parse_fitted(line, prefix)
    broken_names = sorted(name for name, ok in flags.items() if not ok)
    valid_names = sorted(name for name, ok in flags.items() if ok)
    if not windows:
        assert line == ("claude no limit window" if source == "claude" else "agy no quota bucket")
        return
    assert diagnostic == ", ".join(f"{name} ({reason})" for name in broken_names)
    assert entries == [template.format(name) for name in valid_names[: len(entries)]]
    assert len(entries) + omitted == len(valid_names)
    assert len(line) <= sl.LINE_CHARS or not entries


# Property: the validator says VALID exactly for the documented shape, judged by an independent strict reading that
# uses the standard library alone. Both directions, so a validator that refused everything would fail too.
def all_finite(value: Any) -> bool:
    if isinstance(value, float):
        return value == value and abs(value) != float("inf")
    if isinstance(value, dict):
        return all(all_finite(item) for item in value.values())
    return all(all_finite(item) for item in value) if isinstance(value, list) else True


def strict_reading(raw: bytes) -> dict[str, Any] | None:
    def members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({name for name, _ in pairs}) != len(pairs):
            raise ValueError("a member twice")
        return dict(pairs)

    def constant(name: str) -> Any:
        raise ValueError(name)

    try:
        data = json.loads(raw.decode("utf-8").removeprefix("\ufeff"), object_pairs_hook=members, parse_constant=constant)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) and all_finite(data) else None


def aware_time(value: Any) -> bool:
    try:
        return isinstance(value, str) and dt.datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


TIMES = st.sampled_from(["2026-11-15T10:00:00Z", "2026-11-15T10:00:00", "2026-11-15T17:00:00+07:00", "2026-11-15"])
LEAVES = st.one_of(
    st.none(), st.booleans(), st.integers(-(10**6), 10**6), st.floats(width=32), st.text(max_size=5), TIMES
)
VALUES = st.recursive(
    LEAVES,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=3), inner, max_size=3)),
    max_leaves=6,
)
OBJECTS = st.dictionaries(st.sampled_from(["provider", "window", "resets_at", "w", "used_percentage"]), LEAVES, max_size=3)
# Members in file order; a name may repeat, and a float may be NaN or infinite, which `json.dumps` spells as constants.
MEMBERS = st.lists(
    st.one_of(
        st.tuples(st.sampled_from(["blocks", "written_at", "rate_limits", "quota", "wake_note"]), VALUES),
        st.tuples(st.just("blocks"), st.lists(st.one_of(OBJECTS, OBJECTS, LEAVES), max_size=3)),
        st.tuples(st.sampled_from(["rate_limits", "quota"]), OBJECTS),
        st.tuples(st.just("written_at"), TIMES),
    ),
    max_size=4,
)


@st.composite
def stored_bytes(draw: Any) -> bytes:
    """Bytes near the documented shape: a rendered object (or another JSON value), then random damage."""
    if draw(st.integers(0, 9)) == 0:
        return draw(st.binary(max_size=12))
    if draw(st.integers(0, 7)) == 0:
        text = json.dumps(draw(VALUES))
    else:
        separator = draw(st.sampled_from([", ", ",", " ,\r\n "]))
        text = "{" + separator.join(f"{json.dumps(name)}: {json.dumps(value)}" for name, value in draw(MEMBERS)) + "}"
    text = draw(st.sampled_from(["", "", "", "\ufeff", " \n", "\ufeff\ufeff"])) + text
    text += draw(st.sampled_from(["", "", "", "\n", " x", "{}", ","]))
    raw = text.encode("utf-8")
    if draw(st.integers(0, 3)) == 0 and raw:  # cut a piece out, or put a byte in
        at = draw(st.integers(0, len(raw) - 1))
        raw = raw[:at] + draw(st.sampled_from([b"", b"\xff", b"}", b'"', b","])) + raw[at + draw(st.integers(0, 3)) :]
    return raw


@settings(max_examples=600, deadline=None)
@given(raw=stored_bytes())
def test_fix4_property_blocks_are_valid_only_in_the_documented_shape(raw: bytes) -> None:
    data = strict_reading(raw)
    entries = data.get("blocks", []) if data is not None else None
    documented = isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries)
    stored = us.stored_blocks(raw)
    assert (stored.tag == "valid") == documented and stored.tag != "absent"
    if not documented:
        assert stored.problems and stored.document is None
        return
    assert data is not None and stored.document is not None
    assert stored.document.data == data and list(stored.blocks) == entries
    for name, (start, end) in stored.document.spans.items():  # each span is that member's value in the text
        assert json.loads(stored.document.text[start:end]) == data[name]


@settings(max_examples=600, deadline=None)
@given(raw=stored_bytes(), member=st.sampled_from(["rate_limits", "quota"]))
def test_fix4_property_status_is_valid_only_in_the_documented_shape(raw: bytes, member: str) -> None:
    data = strict_reading(raw)
    documented = data is not None and aware_time(data.get("written_at")) and isinstance(data.get(member), dict)
    stored = us.stored_status(raw, member)
    assert (stored.tag == "valid") == documented and stored.tag != "absent"
    if documented:
        assert data is not None and stored.member == data[member] and stored.written == us.parse_time(data["written_at"])
    else:
        assert stored.problems and stored.member is None and stored.written is None


@settings(max_examples=400, deadline=None)
@given(raw=stored_bytes())
def test_fix4_property_a_writer_plans_nothing_for_a_malformed_file(raw: bytes) -> None:
    """Both writers as pure plans (no file): an error report for a malformed file, and for a valid one bytes that
    are valid again and keep every value they do not own."""
    path = Path("quota-blocks.json")
    stored = us.stored_blocks(raw)
    recorded = us.planned_blocks(path, raw, snapshot_with([window("weekly", 97, True)]), NOW)
    edited = us.planned_pm_edit(path, raw, PM(None, {"wake_note": "NEW"}, [], False), NOW)
    if stored.tag == "malformed":
        for report in (recorded, edited):
            assert isinstance(report, dict) and report["status"] == "error"
            assert report["problems"] == list(stored.problems)
        return
    assert stored.document is not None
    data, text = stored.document.data, stored.document.text
    if isinstance(edited, bytes):
        assert plain(edited) == data | {"wake_note": "NEW"} and us.stored_blocks(edited).tag == "valid"
    else:
        assert edited["status"] == "unchanged" and data.get("wake_note") == "NEW"
    assert isinstance(recorded, bytes)  # the blocked window always changes the list
    after = us.stored_blocks(recorded)
    assert after.tag == "valid" and after.document is not None
    assert {n: v for n, v in after.document.data.items() if n != "blocks"} == {n: v for n, v in data.items() if n != "blocks"}
    assert all(block in stored.blocks or block["provider"] == "claude" for block in after.blocks)
    if "blocks" in stored.document.spans:  # the bytes outside the value are the file's own
        start, end = stored.document.spans["blocks"]
        written = recorded.decode("utf-8")
        assert written.startswith(text[:start]) and written.endswith(text[end:])
