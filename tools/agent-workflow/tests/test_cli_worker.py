"""Unit tests for cli_worker.py command building (no claude process is started)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cli_worker.py"
spec = importlib.util.spec_from_file_location("cli_worker", SCRIPT)
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


@pytest.fixture(autouse=True)
def no_brief_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests start workers with one-word briefs; the lint gate has its own tests."""
    monkeypatch.setattr(cw, "lint_gate", lambda *args, **kwargs: None)


WORKER = {"model": "haiku", "effort": "low", "session_id": "sid-1", "hooks": {}}


def test_default_command_has_no_mcp_flags() -> None:
    cmd = cw.build_command(WORKER, resume=False)
    assert "--strict-mcp-config" not in cmd and "--mcp-config" not in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"


def test_minimal_mcp_uses_strict_empty_config() -> None:
    cmd = cw.build_command({**WORKER, "mcp": "minimal"}, resume=True)
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert cmd[cmd.index("--resume") + 1] == "sid-1" and "--session-id" not in cmd


def test_advisor_flag_only_when_configured() -> None:
    assert "--advisor" not in cw.build_command(WORKER, resume=False)
    assert "--advisor" not in cw.build_command({**WORKER, "advisor": None}, resume=False)
    cmd = cw.build_command({**WORKER, "advisor": "opus"}, resume=True)
    assert cmd[cmd.index("--advisor") + 1] == "opus"


def test_model_usage_summary_splits_advisor_cost() -> None:
    event = {
        "type": "result",
        "modelUsage": {
            "claude-sonnet-5-5": {
                "inputTokens": 4,
                "outputTokens": 31,
                "costUSD": 0.13,
                "contextWindow": 1,
            },
            "claude-opus-5-5": {"inputTokens": 58781, "outputTokens": 293, "costUSD": 0.24},
        },
    }
    summary = cw.model_usage_summary(event)
    assert summary["claude-opus-5-5"]["inputTokens"] == 58781
    assert summary["claude-opus-5-5"]["costUSD"] == 0.24
    assert "contextWindow" not in summary["claude-sonnet-5-5"]
    assert cw.model_usage_summary({"type": "result"}) == {}
    assert cw.model_usage_summary({"modelUsage": {"m": "bad"}}) == {}


def test_workers_without_hooks_need_no_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise AssertionError("venv_python must not be called without hooks")

    monkeypatch.setattr(cw, "venv_python", boom)
    # Workers without the tool guard (registered before it existed) keep the prefix git denies.
    assert cw.hook_settings({}) is cw.LEGACY_SETTINGS
    assert (
        cw.hook_settings({"process_guard": False, "ctx_nudge": "off", "loop_window": 0})
        is cw.LEGACY_SETTINGS
    )


def test_hook_settings_wires_each_enabled_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    full = cw.hook_settings({"process_guard": True, "ctx_nudge": "10,20", "loop_window": 3})
    pre = full["hooks"]["PreToolUse"][0]["hooks"][0]
    post = full["hooks"]["PostToolUse"][0]["hooks"][0]
    assert pre["args"][-1].endswith("hooks/process_guard.py")
    assert post["args"][-4:] == ["--thresholds", "10,20", "--loop-window", "3"]
    only_guard = cw.hook_settings({"process_guard": True, "ctx_nudge": "off", "loop_window": 0})
    assert "PostToolUse" not in only_guard["hooks"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("off", "off"), ("", "off"), ("1000,2000", "1000,2000")],
)
def test_validate_ctx_nudge_accepts(text: str, expected: str) -> None:
    assert cw.validate_ctx_nudge(text) == expected


@pytest.mark.parametrize("text", ["abc", "0,5", "5,5", "9,3", "-1"])
def test_validate_ctx_nudge_rejects(text: str) -> None:
    with pytest.raises(SystemExit):
        cw.validate_ctx_nudge(text)


def test_permission_path_syntax() -> None:
    assert cw.permission_path(PureWindowsPath("D:/proj/repo")) == "//d/proj/repo"
    assert cw.permission_path(PurePosixPath("/home/dev/repo")) == "//home/dev/repo"


def test_secrets_deny_rules_follow_the_project_root() -> None:
    deny = cw.SETTINGS["permissions"]["deny"]
    assert f"Read({cw.permission_path(cw.ROOT)}/.secrets/**)" in deny


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("off", None),
        ("none", None),
        ("", None),
        ("100000", 100000),
        ("230000", 230000),
        ("1000000", 1000000),
    ],
)
def test_validate_autocompact_window_accepts(text: str, expected: int | None) -> None:
    assert cw.validate_autocompact_window(text) == expected


@pytest.mark.parametrize("text", ["abc", "99999", "1000001", "0", "-5", "2e5", "200k", "1.5"])
def test_validate_autocompact_window_rejects_outside_the_documented_range(text: str) -> None:
    with pytest.raises(SystemExit):
        cw.validate_autocompact_window(text)


def test_default_window_is_in_range_and_above_the_default_nudge() -> None:
    window = cw.validate_autocompact_window(cw.DEFAULT_AUTOCOMPACT_WINDOW)
    assert window is not None and cw.AUTOCOMPACT_MIN <= window <= cw.AUTOCOMPACT_MAX
    assert int(cw.validate_ctx_nudge(cw.DEFAULT_CTX_NUDGE).split(",")[-1]) < window
    assert cw.DEFAULT_CTX_NUDGE == "170000"


def test_child_env_sets_the_window_only_for_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cw.AUTOCOMPACT_ENV, raising=False)
    assert cw.AUTOCOMPACT_ENV not in cw.child_env("1h")
    assert cw.AUTOCOMPACT_ENV not in cw.child_env("1h", None)
    assert cw.child_env("1h", 230000)[cw.AUTOCOMPACT_ENV] == "230000"
    assert (
        cw.AUTOCOMPACT_ENV not in __import__("os").environ
    )  # the launcher's own environment is untouched
    monkeypatch.setenv(cw.AUTOCOMPACT_ENV, "400000")
    assert cw.child_env("1h", 150000)[cw.AUTOCOMPACT_ENV] == "150000"  # overrides an ambient value


def test_nudge_hook_receives_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    cmd = cw.build_command(
        {
            **WORKER,
            "hooks": {"ctx_nudge": "170000", "loop_window": 5},
            "autocompact_window": 230000,
        },
        resume=True,
    )
    args = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PostToolUse"][0]["hooks"][0][
        "args"
    ]
    assert args[args.index("--autocompact-window") + 1] == "230000"
    legacy = cw.build_command({**WORKER, "hooks": {"ctx_nudge": "170000"}}, resume=True)
    assert (
        "--autocompact-window"
        not in json.loads(legacy[legacy.index("--settings") + 1])["hooks"]["PostToolUse"][0][
            "hooks"
        ][0]["args"]
    )


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[dict]]:
    monkeypatch.setattr(cw, "REGISTRY", tmp_path / "cli-workers.json")
    monkeypatch.setattr(cw, "LOCK", tmp_path / "cli-workers.lock")
    monkeypatch.setattr(cw, "venv_python", lambda: "py")
    turns: list[dict] = []
    monkeypatch.setattr(cw, "run_turn", lambda name, worker, *a, **k: turns.append(worker) or 0)
    msg = tmp_path / "brief.md"
    msg.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(cw, "ROOT", tmp_path)
    (tmp_path / "wt").mkdir()
    return msg, turns


def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, msg: Path, *args: str) -> int:
    base = ["cli_worker.py", "start", "--name", "w", "--model", "haiku", "--effort", "low"]
    base += ["--cwd", str(tmp_path / "wt"), "--message-file", str(msg), "--cache-ttl", "5m"]
    monkeypatch.setattr("sys.argv", [*base, *args])
    return cw.main()


def stored(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "cli-workers.json").read_text(encoding="utf-8"))["w"]


def test_start_stores_the_default_window_and_a_custom_one(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, turns = registry
    cli(monkeypatch, tmp_path, msg)
    assert stored(tmp_path)["autocompact_window"] == 230000
    assert turns[-1]["autocompact_window"] == 230000
    (tmp_path / "cli-workers.json").unlink()
    cli(monkeypatch, tmp_path, msg, "--autocompact-window", "150000", "--ctx-nudge", "100000")
    assert stored(tmp_path)["autocompact_window"] == 150000


def test_start_records_the_brief_order_marker(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T-021: the marker lets later sends hold this worker to the brief order; older entries lack it.
    msg, turns = registry
    cli(monkeypatch, tmp_path, msg)
    assert stored(tmp_path)[cw.BRIEF_ORDER_KEY] == cw.BRIEF_ORDER == 2
    assert turns[-1][cw.BRIEF_ORDER_KEY] == 2


def test_start_off_stores_null_and_out_of_range_is_refused(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = registry
    with pytest.raises(SystemExit):
        cli(monkeypatch, tmp_path, msg, "--autocompact-window", "50000")
    assert not (tmp_path / "cli-workers.json").exists()
    cli(monkeypatch, tmp_path, msg, "--autocompact-window", "off")
    assert stored(tmp_path)["autocompact_window"] is None


def test_start_refuses_a_nudge_at_or_above_the_window(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = registry
    with pytest.raises(SystemExit, match="below"):
        cli(monkeypatch, tmp_path, msg, "--autocompact-window", "150000")  # default nudge 170000
    cli(monkeypatch, tmp_path, msg, "--autocompact-window", "150000", "--ctx-nudge", "off")
    assert stored(tmp_path)["autocompact_window"] == 150000


def test_send_keeps_the_stored_window_and_legacy_workers_stay_unset(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, turns = registry
    # The suite also runs inside a worker, whose ambient environment already carries the variable.
    monkeypatch.delenv(cw.AUTOCOMPACT_ENV, raising=False)

    def set_idle(**drop: bool) -> None:  # the stubbed turn does not move the worker out of `busy`
        data = json.loads((tmp_path / "cli-workers.json").read_text(encoding="utf-8"))
        data["w"]["status"] = "idle"
        data["w"].pop(
            "launcher_pid", None
        )  # the stubbed turn leaves this test process's pid behind
        for key in drop:
            del data["w"][key]
        (tmp_path / "cli-workers.json").write_text(json.dumps(data), encoding="utf-8")

    cli(monkeypatch, tmp_path, msg)
    set_idle()
    monkeypatch.setattr(
        "sys.argv", ["cli_worker.py", "send", "--name", "w", "--message-file", str(msg)]
    )
    assert cw.main() == 0
    assert (
        turns[-1]["autocompact_window"] == 230000
        and stored(tmp_path)["autocompact_window"] == 230000
    )
    # A worker registered before the flag existed has no entry; send must not add one.
    set_idle(autocompact_window=True)
    assert cw.main() == 0
    assert "autocompact_window" not in stored(tmp_path) and "autocompact_window" not in turns[-1]
    assert cw.AUTOCOMPACT_ENV not in cw.child_env("1h", turns[-1].get("autocompact_window"))


def write_registry(tmp_path: Path, **entry: object) -> None:
    base = {
        "session_id": "sid-9",
        "status": "busy",
        "hooks": {"ctx_nudge": "120000", "loop_window": 5},
    }
    (tmp_path / "cli-workers.json").write_text(
        json.dumps({"w": {**base, **entry}}), encoding="utf-8"
    )


def notify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> int:
    msg = tmp_path / "note.md"
    msg.write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["cli_worker.py", "notify", "--name", "w", "--message-file", str(msg)]
    )
    return cw.main()


def test_notify_queues_and_appends_for_a_busy_worker(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cw, "INBOX", tmp_path / "inbox")
    write_registry(tmp_path)
    assert notify(monkeypatch, tmp_path, "first") == 0
    assert notify(monkeypatch, tmp_path, "second") == 0
    text = (tmp_path / "inbox" / "sid-9.md").read_text(encoding="utf-8")
    assert text.index("first") < text.index("second") and "---" in text
    assert [p.name for p in (tmp_path / "inbox").iterdir()] == ["sid-9.md"]  # no temp file left


@pytest.mark.parametrize(
    ("entry", "text", "error"),
    [
        ({"status": "idle"}, "m", "use send"),
        ({"status": "retired"}, "m", "not registered"),
        ({"hooks": {"ctx_nudge": "off", "loop_window": 0}}, "m", "without the PostToolUse hook"),
        ({}, "  ", "empty"),
    ],
)
def test_notify_refuses_what_cannot_be_delivered(
    registry: tuple[Path, list[dict]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: dict,
    text: str,
    error: str,
) -> None:
    monkeypatch.setattr(cw, "INBOX", tmp_path / "inbox")
    write_registry(tmp_path, **entry)
    with pytest.raises(SystemExit, match=error):
        notify(monkeypatch, tmp_path, text)
    assert not (tmp_path / "inbox").exists() or not any((tmp_path / "inbox").iterdir())


PROFILE_TABLE = {
    "dev-large": (400000, "300000,360000", "1h"),
    "dev-medium": (300000, "220000,270000", "1h"),
    "dev-small": (200000, "150000,180000", "1h"),
    "review": (250000, "180000", "5m"),  # QA TTL 5 min (decision 500)
    "spike": (300000, "220000", "1h"),
    "scout": (150000, "120000", "5m"),
    "helper": (150000, "120000", "5m"),
}
GOOD = {"autocompact_window": 200000, "ctx_nudge": "150000", "cache_ttl": "1h"}


def write_profiles(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_committed_profiles_match_decision_379() -> None:
    profiles = cw.load_worker_profiles()
    assert set(profiles) == set(PROFILE_TABLE)
    for role, (window, nudge, ttl) in PROFILE_TABLE.items():
        assert (
            profiles[role]["autocompact_window"],
            profiles[role]["ctx_nudge"],
            profiles[role]["cache_ttl"],
        ) == (window, nudge, ttl)
        assert "model" not in profiles[role] and "effort" not in profiles[role]


def test_profile_loader_accepts_optional_model_effort_and_off(tmp_path: Path) -> None:
    path = write_profiles(
        tmp_path,
        {"r": {**GOOD, "model": "sonnet", "effort": "high", "autocompact_window": "off"}},
    )
    assert cw.resolve_profile("r", path) == {
        "autocompact_window": None,
        "ctx_nudge": "150000",
        "cache_ttl": "1h",
        "model": "sonnet",
        "effort": "high",
    }


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({k: v for k, v in GOOD.items() if k != "cache_ttl"}, "'r'.*missing field 'cache_ttl'"),
        ({k: v for k, v in GOOD.items() if k != "ctx_nudge"}, "'r'.*missing field 'ctx_nudge'"),
        (
            {k: v for k, v in GOOD.items() if k != "autocompact_window"},
            "'r'.*missing field 'autocompact_window'",
        ),
        ({**GOOD, "extra": 1}, "'r'.*unknown key.*extra"),
        ({**GOOD, "cache_ttl": "2h"}, "'r'.*'cache_ttl'"),
        ({**GOOD, "autocompact_window": 50000}, "'r'.*'autocompact_window'"),
        ({**GOOD, "autocompact_window": True}, "'r'.*'autocompact_window'"),
        ({**GOOD, "ctx_nudge": "5,3"}, "'r'.*'ctx_nudge'"),
        ({**GOOD, "ctx_nudge": 150000}, "'r'.*'ctx_nudge'"),
        ({**GOOD, "ctx_nudge": "200000"}, "'r'.*'ctx_nudge'.*below"),
        ({**GOOD, "effort": "ultra"}, "'r'.*'effort'"),
        ({**GOOD, "model": ""}, "'r'.*'model'"),
        ("not-an-object", "'r'.*expected an object"),
    ],
)
def test_profile_loader_refuses_invalid_entries(tmp_path: Path, entry: object, match: str) -> None:
    with pytest.raises(SystemExit, match=match):
        cw.load_worker_profiles(write_profiles(tmp_path, {"r": entry}))


def test_profile_loader_refuses_unknown_role_and_bad_files(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, {"r": GOOD})
    with pytest.raises(SystemExit, match=r"unknown worker profile 'x'.*known: r"):
        cw.resolve_profile("x", path)
    with pytest.raises(SystemExit, match="non-empty object"):
        cw.load_worker_profiles(write_profiles(tmp_path, {}))
    with pytest.raises(SystemExit, match="unreadable"):
        cw.load_worker_profiles(tmp_path / "missing.json")


def profile_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, msg: Path, *args: str) -> int:
    base = ["cli_worker.py", "start", "--name", "w", "--cwd", str(tmp_path / "wt")]
    monkeypatch.setattr("sys.argv", [*base, "--message-file", str(msg), *args])
    return cw.main()


@pytest.mark.parametrize("role", sorted(PROFILE_TABLE))
def test_start_with_a_profile_records_its_values(
    registry: tuple[Path, list[dict]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    msg, turns = registry
    profile_cli(
        monkeypatch, tmp_path, msg, "--profile", role, "--model", "haiku", "--effort", "low"
    )
    window, nudge, ttl = PROFILE_TABLE[role]
    entry = stored(tmp_path)
    assert entry["autocompact_window"] == window and entry["cache_ttl"] == ttl
    assert entry["hooks"]["ctx_nudge"] == nudge and entry["profile"] == role
    assert turns[-1]["autocompact_window"] == window


def test_explicit_flags_override_the_profile(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = registry
    common = ["--profile", "dev-large", "--model", "haiku", "--effort", "low"]
    # The profile's last nudge (360000) must stay below an overriding window, so a lone 350000 is refused.
    with pytest.raises(SystemExit, match=r"below.*explicit lower --ctx-nudge"):
        profile_cli(monkeypatch, tmp_path, msg, *common, "--autocompact-window", "350000")
    assert not (tmp_path / "cli-workers.json").exists()
    profile_cli(
        monkeypatch,
        tmp_path,
        msg,
        *common,
        "--autocompact-window",
        "350000",
        "--ctx-nudge",
        "300000",
        "--cache-ttl",
        "5m",
    )
    entry = stored(tmp_path)
    assert entry["autocompact_window"] == 350000 and entry["cache_ttl"] == "5m"
    assert entry["hooks"]["ctx_nudge"] == "300000"


def test_profile_supplies_model_and_effort_when_present(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = registry
    path = write_profiles(tmp_path, {"r": {**GOOD, "model": "sonnet", "effort": "high"}})
    monkeypatch.setattr(cw, "PROFILES", path)
    profile_cli(monkeypatch, tmp_path, msg, "--profile", "r")
    entry = stored(tmp_path)
    assert entry["model"] == "sonnet" and entry["effort"] == "high"
    (tmp_path / "cli-workers.json").unlink()
    profile_cli(monkeypatch, tmp_path, msg, "--profile", "r", "--effort", "max")
    assert stored(tmp_path)["effort"] == "max"


def test_start_refuses_missing_settings_and_unknown_profile(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = registry
    with pytest.raises(SystemExit):  # no model, effort or TTL and no profile
        profile_cli(monkeypatch, tmp_path, msg)
    with pytest.raises(SystemExit):  # the committed profile has no model or effort, flags absent
        profile_cli(monkeypatch, tmp_path, msg, "--profile", "dev-small")
    with pytest.raises(SystemExit, match="unknown worker profile"):
        profile_cli(
            monkeypatch, tmp_path, msg, "--profile", "nope", "--model", "haiku", "--effort", "low"
        )
    with pytest.raises(SystemExit, match="unknown worker profile ''"):  # empty is not "no profile"
        profile_cli(
            monkeypatch, tmp_path, msg, "--profile", "", "--model", "haiku", "--effort", "low"
        )
    with pytest.raises(SystemExit):  # no profile: --cache-ttl stays required
        profile_cli(monkeypatch, tmp_path, msg, "--model", "haiku", "--effort", "low")
    assert not (tmp_path / "cli-workers.json").exists()


def test_start_without_a_profile_is_unchanged_and_status_shows_the_profile(
    registry: tuple[Path, list[dict]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    msg, _ = registry
    cli(monkeypatch, tmp_path, msg)
    entry = stored(tmp_path)
    assert entry["profile"] is None and entry["autocompact_window"] == 230000
    assert entry["hooks"]["ctx_nudge"] == "170000" and entry["cache_ttl"] == "5m"
    assert "profile=" not in cw.format_row(cw.status_rows(False)[0])
    (tmp_path / "cli-workers.json").unlink()
    profile_cli(
        monkeypatch, tmp_path, msg, "--profile", "scout", "--model", "haiku", "--effort", "low"
    )
    assert cw.status_rows(False)[0]["profile"] == "scout"
    assert "profile=scout" in cw.format_row(cw.status_rows(False)[0])


SID = "90242319-0ed5-47a6-bc3c-5b01859c96dd"


def test_purge_transcript_deletes_only_the_exact_session_file(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    target = projects / "D--wt-a" / f"{SID}.jsonl"
    keep = [
        projects / "D--wt-a" / f"{SID}.jsonl.bak",
        projects / "D--wt-a" / f"x{SID}.jsonl",
        projects / "D--wt-b" / "11111111-2222-3333-4444-555555555555.jsonl",
        tmp_path / f"{SID}.jsonl",
    ]
    for path in [target, *keep]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    assert cw.purge_transcript(SID, projects) == [target.resolve()]
    assert not target.exists() and all(path.exists() for path in keep)
    assert cw.purge_transcript(SID, projects) == []  # idempotent: nothing left to delete


def make_session(projects: Path, slug: str, other: bool = False) -> tuple[Path, Path]:
    folder = projects / slug
    (folder / SID / "subagents").mkdir(parents=True)
    (folder / SID / "subagents" / "a.jsonl").write_text("{}", encoding="utf-8")
    (folder / f"{SID}.jsonl").write_text("{}", encoding="utf-8")
    if other:
        (folder / "22222222-2222-3333-4444-555555555555.jsonl").write_text("{}", encoding="utf-8")
    return folder, folder / SID


def test_purge_transcript_removes_session_dir_and_the_emptied_slug_folder(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    folder, session_dir = make_session(projects, "D--wt-a")
    removed = cw.purge_transcript(SID, projects)
    assert folder.resolve() in removed and session_dir.resolve() in removed
    assert (folder / f"{SID}.jsonl").resolve() in removed
    assert not folder.exists() and projects.exists()


def test_purge_transcript_keeps_a_slug_folder_with_another_session(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    folder, session_dir = make_session(projects, "D--wt-a", other=True)
    (
        projects / "D--wt-empty"
    ).mkdir()  # an untouched empty folder of another project is not ours to remove
    removed = cw.purge_transcript(SID, projects)
    assert folder.resolve() not in removed and (projects / "D--wt-empty").is_dir()
    assert (
        not session_dir.exists()
        and (folder / "22222222-2222-3333-4444-555555555555.jsonl").exists()
    )


def test_purge_transcript_keeps_the_main_project_folder_and_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    worktree = main / ".tmp" / "worktrees" / "wt"
    monkeypatch.setattr(cw, "ROOT", worktree)
    projects = tmp_path / "projects"
    for slug in (cw.project_slug(main), cw.project_slug(worktree)):
        make_session(projects, slug)
    memory = projects / "memory"
    memory.mkdir()
    cw.purge_transcript(SID, projects)
    assert (projects / cw.project_slug(main)).is_dir() and memory.is_dir()
    assert not (projects / cw.project_slug(main) / SID).exists()
    assert (projects / cw.project_slug(worktree)).is_dir()  # protected too: ROOT itself


def test_purge_transcript_never_follows_links(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    _, session_dir = make_session(projects, "D--wt-a")
    other = projects / "D--wt-b"
    other.mkdir()

    def link_dir(link: Path) -> None:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                pytest.skip("symlinks unavailable")
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                check=True,
                capture_output=True,
            )

    # a session dir that is itself a link is refused, a nested link is dropped without entering it
    link_dir(session_dir / "lnk")
    link_dir(other / SID)
    cw.purge_transcript(SID, projects)
    assert (outside / "keep.txt").exists() and not session_dir.exists()
    assert cw.is_link(other / SID) and (other / SID).exists()


def test_purge_transcript_keeps_a_slug_folder_that_still_has_memory(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    folder, _ = make_session(projects, "D--wt-a")
    (folder / "memory").mkdir()
    (folder / "memory" / "m.md").write_text("note", encoding="utf-8")
    removed = cw.purge_transcript(SID, projects)
    assert folder.resolve() not in removed and (folder / "memory" / "m.md").is_file()
    assert not (folder / SID).exists() and not (folder / f"{SID}.jsonl").exists()


def test_purge_transcript_never_removes_a_top_level_memory_folder(tmp_path: Path) -> None:
    # the `memory` name guard matters: a transcript there empties the folder, which must still stay
    projects = tmp_path / "projects"
    (projects / "memory").mkdir(parents=True)
    log = projects / "memory" / f"{SID}.jsonl"
    log.write_text("{}", encoding="utf-8")
    assert cw.purge_transcript(SID, projects) == [log.resolve()]
    assert (projects / "memory").is_dir()


def make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlinks unavailable")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )


def test_purge_transcript_never_enters_a_linked_project_folder(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    real = tmp_path / "elsewhere" / "D--wt-real"
    (real / SID).mkdir(parents=True)
    (real / SID / "keep.txt").write_text("x", encoding="utf-8")
    (real / f"{SID}.jsonl").write_text("{}", encoding="utf-8")
    projects.mkdir()
    make_dir_link(projects / "D--wt-alias", real)
    assert cw.purge_transcript(SID, projects) == []
    assert (real / SID / "keep.txt").is_file() and (real / f"{SID}.jsonl").is_file()
    assert cw.is_link(projects / "D--wt-alias")


def fail_unlink_of(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    original = Path.unlink

    def unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == name:
            raise PermissionError(13, "Access is denied")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", unlink)


def test_purge_transcript_continues_past_a_failed_entry_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    folder, session_dir = make_session(projects, "D--wt-a")
    (session_dir / "locked.json").write_text("{}", encoding="utf-8")
    (session_dir / "z.json").write_text("{}", encoding="utf-8")
    fail_unlink_of(monkeypatch, "locked.json")
    failed: list[str] = []
    removed = cw.purge_transcript(SID, projects, failed=failed)
    assert (session_dir / "locked.json").exists() and not (session_dir / "z.json").exists()
    assert (folder / f"{SID}.jsonl").resolve() in removed  # the transcript itself still went
    assert len(failed) == 1 and "locked.json" in failed[0] and "PermissionError" in failed[0]
    assert session_dir.resolve() not in removed and folder.resolve() not in removed
    assert folder.is_dir()  # not emptied by this call, so it stays


def test_retire_purge_transcript_exits_non_zero_listing_both_kinds(
    registry: tuple[Path, list[dict]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    msg, _ = registry
    cli(monkeypatch, tmp_path, msg)
    data = json.loads((tmp_path / "cli-workers.json").read_text(encoding="utf-8"))
    data["w"]["session_id"] = SID
    data["w"]["status"] = "idle"
    for key in cw.PROCESS_KEYS:
        data["w"].pop(key, None)  # the fake turn recorded this test process as the launcher
    (tmp_path / "cli-workers.json").write_text(json.dumps(data), encoding="utf-8")
    projects = tmp_path / "projects"
    _, session_dir = make_session(projects, "D--wt-a")
    (session_dir / "locked.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cw, "CLAUDE_PROJECTS", projects)
    fail_unlink_of(monkeypatch, "locked.json")
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.argv", ["cli_worker.py", "retire", "--name", "w", "--purge-transcript"]
    )
    assert cw.main() == 1
    out = capsys.readouterr().out
    assert f"deleted {(projects / 'D--wt-a' / f'{SID}.jsonl').resolve()}" in out
    assert "NOT removed" in out and "locked.json" in out
    assert stored(tmp_path)["status"] == "retired"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "*",
        "sid-1",
        SID.upper(),
        f"{{{SID}}}",
        SID.replace("-", ""),
        f"{SID}/..",
        f"../{SID}",
        " " + SID,
    ],
)
def test_purge_transcript_refuses_a_non_canonical_id(tmp_path: Path, bad: str) -> None:
    projects = tmp_path / "projects"
    (projects / "D--wt-a").mkdir(parents=True)
    victim = projects / "D--wt-a" / f"{SID}.jsonl"
    victim.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="not a canonical UUID"):
        cw.purge_transcript(bad, projects)
    assert victim.exists()


def task_file_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    root = tmp_path.resolve()
    monkeypatch.setattr(cw, "ROOT", root)
    worktrees = root / ".tmp" / "worktrees"
    wt = worktrees / "a"
    for folder in (root / "tasks", wt / "tasks"):
        folder.mkdir(parents=True)
    root_task, wt_task = root / "tasks" / "T.md", wt / "tasks" / "T.md"
    root_task.write_text("x", encoding="utf-8")
    wt_task.write_text("x", encoding="utf-8")
    return worktrees, wt, root_task, wt_task


def test_task_file_inside_the_workers_own_checkout_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktrees, wt, root_task, wt_task = task_file_tree(tmp_path, monkeypatch)
    assert cw.validate_task_file(str(wt_task), wt, worktrees) == wt_task.as_posix()
    assert cw.validate_task_file(str(root_task), cw.ROOT, worktrees) == root_task.as_posix()
    assert cw.validate_task_file(None, wt, worktrees) is None


def test_task_file_of_another_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PM mistake #30: a worktree worker given the root copy wrote progress into the root task file.
    worktrees, wt, root_task, wt_task = task_file_tree(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="inside the worker's checkout"):
        cw.validate_task_file(str(root_task), wt, worktrees)
    with pytest.raises(SystemExit, match="inside the worker's checkout"):
        cw.validate_task_file(str(wt_task), cw.ROOT, worktrees)
