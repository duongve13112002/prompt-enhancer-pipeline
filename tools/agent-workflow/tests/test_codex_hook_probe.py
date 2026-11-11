"""Host-probe orchestration with labelled process/DACL doubles; never live Codex."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import codex_guard as guard
import pytest
from test_cli_worker_codex import TOOLS, World, cb, cw


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def test_probe_fresh_canary_denial_and_dacl_are_required(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    probe = cw.sibling("codex_hook_probe")
    monkeypatch.setattr(probe, "ROOT", world.root)
    monkeypatch.setattr(probe, "backend", cb)
    monkeypatch.setattr(cb, "startup_check", lambda *args: None)
    monkeypatch.setattr(cb.hook_module(), "check_version", lambda *args: None)
    folders: list[Path] = []

    def dacl(folder: Path, timeout: int) -> str:
        assert (
            folder.parent == world.cwd
            and (folder / "canary.txt").read_bytes() == b"owned non-secret canary"
        )
        assert timeout == 20
        folders.append(folder)
        return "D:AI"

    monkeypatch.setattr(probe, "dacl_flag", dacl)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "--event" in argv:
            payload = json.loads(kwargs["input"])
            if payload["tool_name"] == "Bash":
                assert payload["tool_input"] == {"command": "echo codex-hook-positive-control"}
                return SimpleNamespace(returncode=0, stdout="{}")
            assert payload["tool_name"] == "spawn_agent"
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "tool name not on the exact role allowlist",
                        }
                    }
                ),
            )
        assert "sandbox" in argv and "-P" in argv and "-I" in argv
        assert any("canary.txt" in x for x in argv)
        if argv[-1] == "echo":
            assert Path(argv[-2]).read_text(encoding="utf-8") == "owned non-secret canary"
            return SimpleNamespace(returncode=0, stdout="owned non-secret canary")
        return SimpleNamespace(returncode=77, stdout="")

    monkeypatch.setattr(probe, "run", run)
    report = world.root / "probe-report.json"
    result = probe.probe(world.config, world.cwd, "codex-dev", report)
    assert (
        result["canary_denied"]
        and result["direct_adapter_denied"]
        and result["direct_adapter_allowed"]
    )
    assert result["dacl_flag"] == "D:AI" and len(folders) == 1 and not folders[0].exists()
    assert result["live_exec_resume"].startswith("not_run")
    assert "image_genimagegen" not in result["expected_hook_tools"]
    monkeypatch.setattr(
        probe,
        "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout="owned non-secret canary" if argv[-1] == "echo" else "readable"
        ),
    )
    with pytest.raises(ValueError, match="canary denial FAILED"):
        probe.probe(world.config, world.cwd, "codex-dev", report)
    assert len(folders) == 2 and all(not folder.exists() for folder in folders)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL wire only")
def test_dacl_flag_reports_auto_inheritance_without_assuming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    probe = cw.sibling("codex_hook_probe")
    for flag in ("D:AI", "D:", "D:PAI"):
        monkeypatch.setattr(
            probe,
            "run",
            lambda *args, flag=flag, **kw: SimpleNamespace(
                returncode=0, stdout="O:SYG:SY" + flag + "(A;;FA;;;SY)"
            ),
        )
        assert probe.dacl_flag(tmp_path, 5) == flag


@pytest.mark.parametrize(
    "broken", ["crash", "crash_negative_only", "deny_all", "wrong_event", "exit_failure", "garbage"]
)
def test_probe_rejects_a_deliberately_broken_adapter(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, broken: str
) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    probe = cw.sibling("codex_hook_probe")
    monkeypatch.setattr(probe, "ROOT", world.root)
    monkeypatch.setattr(probe, "backend", cb)
    monkeypatch.setattr(cb, "startup_check", lambda *args: None)
    monkeypatch.setattr(cb.hook_module(), "check_version", lambda *args: None)
    monkeypatch.setattr(probe, "dacl_flag", lambda *args: "D:AI")
    monkeypatch.setattr(
        guard, "__file__", str(world.root / "tools/agent-workflow/hooks/codex_guard.py")
    )
    original_run = guard.run

    def fail(*args: object, **kwargs: object) -> dict:
        if broken == "deny_all":
            raise guard.AdmissionRefusalError("tool name not on the exact role allowlist")
        if broken == "crash_negative_only":
            stream = args[1]
            payload = stream.read()
            if json.loads(payload)["tool_name"] != "spawn_agent":
                return original_run(args[0], io.BytesIO(payload), args[2])
        raise RuntimeError("deliberately broken adapter")

    monkeypatch.setattr(guard, "run", fail)

    def dispatch(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "--event" not in argv:
            return (
                SimpleNamespace(returncode=0, stdout="owned non-secret canary")
                if argv[-1] == "echo"
                else SimpleNamespace(returncode=77, stdout="")
            )
        capsys.readouterr()
        code = guard.main(argv[4:], io.BytesIO(kwargs["input"].encode("utf-8")))
        reply = json.loads(capsys.readouterr().out)
        if broken in ("wrong_event", "exit_failure", "garbage"):
            reply = guard.deny("tool name not on the exact role allowlist")
        if broken == "wrong_event":
            reply["hookSpecificOutput"]["hookEventName"] = "Stop"
        return SimpleNamespace(
            returncode=1 if broken == "exit_failure" else code,
            stdout="not-json" if broken == "garbage" else json.dumps(reply),
        )

    monkeypatch.setattr(probe, "run", dispatch)
    report = world.root / "broken-probe.json"
    with pytest.raises(ValueError):
        probe.probe(world.config, world.cwd, "codex-dev", report)
    assert not report.exists()


def test_probe_wrong_canary_control_path_cannot_pass(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    probe = cw.sibling("codex_hook_probe")
    monkeypatch.setattr(probe, "ROOT", world.root)
    monkeypatch.setattr(probe, "backend", cb)
    monkeypatch.setattr(cb, "startup_check", lambda *args: None)
    monkeypatch.setattr(cb.hook_module(), "check_version", lambda *args: None)
    monkeypatch.setattr(probe, "dacl_flag", lambda *args: "D:AI")
    monkeypatch.setattr(
        probe,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="some other file"),
    )
    with pytest.raises(ValueError, match="positive control FAILED"):
        probe.probe(world.config, world.cwd, "codex-dev", world.root / "wrong-path.json")
