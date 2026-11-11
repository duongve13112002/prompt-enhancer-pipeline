"""CPU contract tests; every Codex event/binary here is a labelled double."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import codex_guard as guard
import pytest
from conftest import make_link
from hypothesis import given
from hypothesis import strategies as st
from test_cli_worker_codex import THREAD_A, World, cb, cw, worker_dict


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


host = cb.hook_module()


def event(worker: dict, name: str = "apply_patch", args: object = None) -> dict:
    return {
        "session_id": THREAD_A,
        "turn_id": "turn-1",
        "transcript_path": None,
        "cwd": worker["cwd"],
        "model": "test-model",
        "hook_event_name": "PreToolUse",
        "permission_mode": "default",
        "tool_name": name,
        "tool_use_id": "call_1",
        "tool_input": {"command": "*** Begin Patch\n*** Add File: safe.txt\n+ok\n*** End Patch\n"}
        if args is None
        else args,
    }


@pytest.fixture
def admitted(world: World, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path, dict]:
    worker = worker_dict(world)
    path = host.prepare(worker)
    body = json.loads(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        guard, "__file__", str(world.root / "tools/agent-workflow/hooks/codex_guard.py")
    )
    return worker, path, body


def invoke(admitted: tuple, payload: object, kind: str = "PreToolUse") -> dict:
    _worker, path, body = admitted
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return guard.run(
        ["--policy", str(path), "--digest", body["digest"], "--event", kind], io.BytesIO(data)
    )


def test_features_and_trust_on_every_exec_resume(world: World, tmp_path: Path) -> None:
    world.write_config(lambda cfg: cfg["runtime"].update(disabled_features=["memories", "apps"]))
    worker = worker_dict(world)
    for resume in (False, True):
        argv = cb.build_command(worker, resume, tmp_path / "result")
        overrides = [argv[i + 1] for i, x in enumerate(argv) if x == "-c"]
        assert "features.apps=false" in overrides and "features.memories=false" in overrides
        assert "features.computer_use=false" not in overrides
        assert "features.hooks=true" in overrides
        table = next(x[6:] for x in overrides if x.startswith("hooks="))
        assert "trusted_hash" in table and "--digest" in table
        assert str(world.cwd) not in table
        assert cb.check_argv(argv[3:], "resume" if resume else "exec", hooks_table=table)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        ["memories"],
        ["apps", "apps"],
        ["apps", "image_generation"],
        ["apps", "new"],
        ["Apps"],
        [True],
    ],
)
def test_feature_refusals(world: World, value: object) -> None:
    world.write_config(lambda cfg: cfg["runtime"].update(disabled_features=value))
    with pytest.raises(SystemExit):
        cb.load_codex_config(world.config)


@pytest.mark.parametrize(
    "override",
    [
        "features.apps=true",
        "features.hooks=false",
        "features.image_generation=false",
        "features.multi_agent=true",
        "hooks={}",
        "features.unknown=false",
    ],
)
def test_override_grammar_is_closed(override: str) -> None:
    with pytest.raises(SystemExit):
        cb.check_override(override)


def test_independent_hash_vector_and_platform_source() -> None:
    table = host.hook_table('"python path" -B -I guard', 13, True)
    for event_name, key in host.EVENTS.items():
        handler = table[event_name][0]["hooks"][0]
        raw = json.dumps(
            {"event_name": key, "hooks": [handler]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert (
            table["state"][f"C:\\<session-flags>\\config.toml:{key}:0:0"]["trusted_hash"]
            == "sha256:" + hashlib.sha256(raw).hexdigest()
        )
        assert handler["async"] is False and handler["timeout"] == 13
    assert all(
        k.startswith("/<session-flags>/") for k in host.hook_table("guard", 7, False)["state"]
    )


@pytest.mark.parametrize("bad", ["x&echo BAD", "%PATH%", "!x!", 'a"b', "a\nb", "a|b", "a^b", "a>b"])
def test_command_quote_refuses_expansion_and_operators(bad: str) -> None:
    with pytest.raises(ValueError):
        host.command_line(["python", bad], True)


def test_command_quote_spaces_and_posix_literals() -> None:
    # A quoted command never ran on 0.167.0 (measured), so a space path is refused.
    with pytest.raises(ValueError, match="whitespace"):
        host.command_line(["C:/Program Files/python.exe", "-B", "guard"], True)
    command = host.command_line(["C:/py/python.exe", "-B", "guard"], True)
    assert command == "C:/py/python.exe -B guard"
    assert host.command_line(["/python space", "-B", "guard"], False) == "'/python space' -B guard"


def test_root_changes_rehash_worker_copy_changes_do_not(world: World, tmp_path: Path) -> None:
    worker = worker_dict(world)
    first = cb.build_command(worker, False, tmp_path / "result")
    copy_path = world.cwd / "tools/agent-workflow/hooks/codex_guard.py"
    copy_path.parent.mkdir(parents=True)
    copy_path.write_text("raise RuntimeError('worker edit')", encoding="utf-8")
    assert first == cb.build_command(worker, False, tmp_path / "result")
    root_path = world.root / "tools/agent-workflow/hooks/codex_guard.py"
    with root_path.open("a", encoding="utf-8") as stream:
        stream.write("\n# root edit\n")
    assert first != cb.build_command(worker, False, tmp_path / "result")


def test_stale_hash_and_missing_stored_hook_table_refuse(admitted: tuple) -> None:
    worker, _path, body = admitted
    root_path = Path(body["root"]) / "tools/agent-workflow/guard_config.json"
    with root_path.open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="changed"):
        invoke(admitted, event(worker))
    del worker["codex"]["hook_table"]
    with pytest.raises(SystemExit, match="retire and restart"):
        cb.require_worker(worker)


@pytest.mark.parametrize(
    "key,value",
    [("policy_dir", "wt/state"), ("guard_config", "wt/guard.json"), ("policy_dir", "../outside")],
)
def test_writable_or_outside_host_config_refused(world: World, key: str, value: str) -> None:
    world.write_config(lambda cfg: cfg["runtime"]["hooks"].update({key: value}))
    with pytest.raises(SystemExit):
        worker_dict(world)


@given(st.text(max_size=60))
def test_exact_name_property(name: str) -> None:
    if name in host.TOOLS:
        assert host.names([name], host.TOOLS, "tools") == [name]
    else:
        with pytest.raises(ValueError):
            host.names([name], host.TOOLS, "tools")


@pytest.mark.parametrize(
    "name",
    [
        "bash",
        "Bash ",
        "apply_patch\u200b",
        "spawn_agent",
        "collaboration.spawn_agent",
        "mcp__codex_apps__gmail",
        "image_gen.imagegen",
        "functions.exec",
    ],
)
def test_unknown_tool_variants_deny_without_side_effect(admitted: tuple, name: str) -> None:
    worker, _path, body = admitted
    before = list(Path(body["cwd"]).iterdir())
    with pytest.raises(ValueError, match="allowlist"):
        invoke(admitted, event(worker, name, {"prompt": "square"}))
    assert list(Path(body["cwd"]).iterdir()) == before


@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b"\xff", b"{}{}", b'{"x":1,"x":2}', b'{"x":NaN}', b"[" * 2000, b"true", b"null"],
)
def test_malformed_stdin_exits_zero_with_typed_deny(
    admitted: tuple, payload: bytes, capsys: pytest.CaptureFixture
) -> None:
    _worker, path, body = admitted
    assert (
        guard.main(
            ["--policy", str(path), "--digest", body["digest"], "--event", "PreToolUse"],
            io.BytesIO(payload),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_oversized_stdin_and_unexpected_check_exception_deny(
    admitted: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    worker, path, body = admitted
    body["options"]["max_input_bytes"] = 1
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="byte limit"):
        invoke(admitted, event(worker))
    body["options"]["max_input_bytes"] = 10000
    path.write_text(json.dumps(body), encoding="utf-8")
    import tool_guard

    def crash(_ctx: object) -> list[str]:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tool_guard, "checks", lambda ctx: [("read_router", crash, None)])
    assert (
        guard.main(
            ["--policy", str(path), "--digest", body["digest"], "--event", "PreToolUse"],
            io.BytesIO(json.dumps(event(worker, "Bash", {"command": "echo safe"})).encode()),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git add safe.txt",
        "taskkill /IM python.exe /F",
        "Stop-Process -Name python",
        'python -c "a\nb"',
        "pytest test_x.py",
        "Set-Content src/file.txt bad",
    ],
)
def test_shell_shared_guards_refuse(admitted: tuple, command: str) -> None:
    with pytest.raises(ValueError):
        invoke(admitted, event(admitted[0], "Bash", {"command": command}))


def test_shell_safe_inspection_and_strict_arguments(admitted: tuple) -> None:
    assert invoke(admitted, event(admitted[0], "Bash", {"command": "echo safe"})) == {}
    for args in (None, "echo safe", {}, {"command": "echo safe", "extra": "x"}, {"command": 1}):
        with pytest.raises((ValueError, TypeError)):
            invoke(admitted, event(admitted[0], "Bash", args if args is not None else "invalid"))


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=50))
def test_patch_parser_property(path: str) -> None:
    patch = f"*** Begin Patch\n*** Update File: {path}\n*** Move to: {path}2\n@@\n-old\n+new\n*** End Patch\n"
    assert guard.patch_targets(patch, 10000, 2) == [path, path + "2"]
    with pytest.raises(ValueError):
        guard.patch_targets(patch, 10000, 1)


@pytest.mark.parametrize(
    "patch",
    [
        "",
        "*** Begin Patch\n*** End Patch",
        "*** Begin Patch\n*** Rename File: x\n*** End Patch",
        "*** Begin Patch\n*** Update File: x\n+bad\n*** End Patch",
        "*** Begin Patch\n*** Add File: x\nunprefixed\n*** End Patch",
        "*** Begin Patch\n*** Delete File: x\n*** Move to: y\n*** End Patch",
        "*** Begin Patch\n*** Add File: x\n+ok\n*** End Patch\ntrailing",
    ],
)
def test_patch_parser_malformed_siblings_refuse(patch: str) -> None:
    with pytest.raises(ValueError):
        guard.patch_targets(patch, 10000, 10)


@pytest.mark.parametrize(
    "path",
    [
        "../out",
        ".git/config",
        ".secrets/file",
        ".\uff27\uff29\uff34/config",
        ".git./config",
        "x:stream",
        "tools/agent-workflow/hooks/codex_guard.py",
        "tools/agent-workflow/guard_config.json",
        "a/../../out",
        "\\\\?\\C:\\file",
    ],
)
def test_patch_target_and_move_attack_siblings(admitted: tuple, path: str) -> None:
    worker = admitted[0]
    for patch in (
        f"*** Begin Patch\n*** Add File: {path}\n+ok\n*** End Patch",
        f"*** Begin Patch\n*** Update File: safe.txt\n*** Move to: {path}\n@@\n-a\n+b\n*** End Patch",
    ):
        with pytest.raises(ValueError):
            invoke(admitted, event(worker, args={"command": patch}))


@pytest.mark.parametrize(
    "path", ["a$b.txt", ".secre?s/x.txt", "a*.txt", "a%b.txt", "a`b.txt", "a|b"]
)
def test_metacharacter_paths_are_refused_with_their_reason(admitted: tuple, path: str) -> None:
    patch = f"*** Begin Patch\n*** Add File: {path}\n+ok\n*** End Patch"
    with pytest.raises(guard.AdmissionRefusalError, match="metacharacter"):
        invoke(admitted, event(admitted[0], args={"command": patch}))


@pytest.mark.parametrize(
    "path", ["SECRET~1/x", ".SECRE~1/x", "GIT~1/x", "tools/agent~1/x", "TOOLS~1/x", "a~12.txt"]
)
def test_short_name_paths_are_refused(admitted: tuple, path: str) -> None:
    with pytest.raises(ValueError, match="short-name"):
        guard.target(path, Path(admitted[0]["cwd"]), Path(admitted[2]["root"]))


def test_web_tool_needs_the_allowlist_and_web_search(admitted: tuple) -> None:
    worker, _policy, body = admitted
    call = event(worker, args={"query": "x"})
    call["tool_name"] = "webrun"
    with pytest.raises(guard.AdmissionRefusalError, match="allowlist"):
        invoke(admitted, call)
    body["allowed_tools"].append("webrun")
    assert guard.admit(call, body) == {}
    role = {
        "model": "gpt-test",
        "effort": "high",
        "web_search": "disabled",
        "timeout_minutes": 5,
        "hook_allowed_tools": ["Bash", "webrun"],
    }
    with pytest.raises(SystemExit, match="web tool"):
        cb.validate_role("codex-scout", role)
    assert cb.validate_role("codex-scout", {**role, "web_search": "live"})["web_search"] == "live"


def test_unicode_crlf_and_resolved_link_targets(admitted: tuple, tmp_path: Path) -> None:
    worker, _path, body = admitted
    patch = "*** Begin Patch\r\n*** Add File: e\u0301.txt\r\n+ok\r\n*** End Patch\r\n"
    assert invoke(admitted, event(worker, args={"command": patch})) == {}
    link = Path(worker["cwd"]) / "linked"
    if not make_link(link, tmp_path):
        pytest.skip("directory link privilege unavailable")
    with pytest.raises(ValueError, match="outside"):
        guard.target("linked/file", Path(worker["cwd"]), Path(body["root"]))


def test_absolute_outside_target_refused(admitted: tuple) -> None:
    worker, _path, body = admitted
    with pytest.raises(ValueError, match="outside"):
        guard.target(
            str(Path(body["root"]) / "outside.txt"), Path(worker["cwd"]), Path(body["root"])
        )


def test_stop_once_even_with_reset_active_turn_id_and_concurrency(admitted: tuple) -> None:
    worker, _path, _body = admitted
    stop_event = {
        k: v
        for k, v in event(worker).items()
        if k not in ("tool_name", "tool_input", "tool_use_id")
    }
    stop_event.update(hook_event_name="Stop", stop_hook_active=False, last_assistant_message=None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: invoke(admitted, stop_event, "Stop"), range(8)))
    assert sum(x.get("decision") == "block" for x in outputs) == 1
    stop_event.update(turn_id="forged-reset", stop_hook_active=False)
    assert invoke(admitted, stop_event, "Stop") == {}
    stop_event["stop_hook_active"] = True
    assert invoke(admitted, stop_event, "Stop") == {}
    fresh = host.prepare(worker)
    new = (worker, fresh, json.loads(fresh.read_text(encoding="utf-8")))
    stop_event["stop_hook_active"] = False
    assert invoke(new, stop_event, "Stop")["decision"] == "block"


def test_stop_nondefault_section_and_labels(admitted: tuple) -> None:
    worker, path, body = admitted
    body["options"].update(stop_section="## Finished", stop_labels=["Proof"])
    task = Path(worker["cwd"]) / "task.md"
    body["task_file"] = str(task)
    task.write_text("## Finished\n- [x] Proof: done\n", encoding="utf-8")
    path.write_text(json.dumps(body), encoding="utf-8")
    e = {k: v for k, v in event(worker).items() if not k.startswith("tool_")}
    e.update(hook_event_name="Stop", stop_hook_active=False, last_assistant_message=None)
    assert invoke(admitted, e, "Stop") == {}


@pytest.mark.parametrize("key", host.OPTION_KEYS)
def test_hook_option_missing_refused(world: World, key: str) -> None:
    world.write_config(lambda cfg: cfg["runtime"]["hooks"].pop(key))
    with pytest.raises(SystemExit):
        cb.load_codex_config(world.config)


@pytest.mark.parametrize(
    "key",
    [
        "timeout_seconds",
        "max_input_bytes",
        "max_patch_bytes",
        "max_patch_targets",
        "max_task_bytes",
    ],
)
def test_positive_options_override_and_refusal(world: World, key: str) -> None:
    world.write_config(lambda cfg: cfg["runtime"]["hooks"].update({key: 17}))
    worker = worker_dict(world)
    assert worker["codex"]["hooks"][key] == 17
    if key == "timeout_seconds":
        assert worker["codex"]["hook_table"]["PreToolUse"][0]["hooks"][0]["timeout"] == 17
    world.write_config(lambda cfg: cfg["runtime"]["hooks"].update({key: 0}))
    with pytest.raises(SystemExit):
        cb.load_codex_config(world.config)


def test_role_subset_override_and_invalid_name_refusal(world: World) -> None:
    world.write_config(
        lambda cfg: cfg["roles"]["codex-dev"].update(hook_allowed_tools=["view_image"])
    )
    assert worker_dict(world)["codex"]["hook_allowed_tools"] == ["view_image"]
    world.write_config(lambda cfg: cfg["roles"]["codex-dev"].update(hook_allowed_tools=["Bash "]))
    with pytest.raises(SystemExit):
        worker_dict(world)


def test_version_nondefault_match_mismatch_failure_and_timeout(world: World) -> None:
    world.write_config(lambda cfg: cfg["runtime"].update(tested_codex_version="0.168.1"))
    cx = worker_dict(world)["codex"]

    def run(argv: list, **kwargs: object) -> SimpleNamespace:
        assert argv[-1] == "--version" and kwargs["timeout"] == 20
        return SimpleNamespace(stdout="codex-cli 0.168.1\n", returncode=0)

    host.check_version(cx, {}, run)
    for output in ("codex-cli 0.167.0", "0.168.1", "codex-cli 0.168.1\nwarning"):
        with pytest.raises(ValueError, match=r"codex_hook_probe\.py"):
            host.check_version(
                cx, {}, lambda *a, output=output, **kw: SimpleNamespace(stdout=output, returncode=0)
            )

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("codex --version", 20)

    with pytest.raises(ValueError, match="host"):
        host.check_version(cx, {}, timeout)


def test_start_and_send_version_refused_before_registry_mutation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> None:
        raise ValueError("version differs; codex_hook_probe.py")

    original = host.check_version
    monkeypatch.setattr(host, "check_version", refuse)
    with pytest.raises(SystemExit, match="version differs"):
        world.start()
    assert not cw.REGISTRY.exists() and not world.execs()
    monkeypatch.setattr(host, "check_version", original)
    assert world.start() == 0
    before = cw.REGISTRY.read_bytes()
    monkeypatch.setattr(host, "check_version", refuse)
    with pytest.raises(SystemExit):
        world.send()
    assert before == cw.REGISTRY.read_bytes() and len(world.execs()) == 1


def test_context_observation_uses_last_response_and_not_cumulative(admitted: tuple) -> None:
    worker, path, _body = admitted
    e = {
        k: v
        for k, v in event(worker).items()
        if k not in ("tool_name", "tool_input", "tool_use_id", "permission_mode")
    }
    e.update(hook_event_name="PostModelRequest", status="completed", error=None)
    for tokens in (301, 407):
        e["token_usage"] = {"input_tokens": tokens, "cached_input_tokens": 300, "output_tokens": 3}
        assert invoke(admitted, e, "PostModelRequest") == {}
    assert host.read_receipts(worker, path, THREAD_A) == (407, [])
    assert (
        cb.parse_log(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 900000}}))[
            "usage"
        ]["input_tokens"]
        == 900000
    )
    e.update(status="failed", token_usage=None)
    invoke(admitted, e, "PostModelRequest")
    assert host.read_receipts(worker, path, THREAD_A) == (None, [])


def test_image_role_exact_receipt_is_moved_to_override_destination(admitted: tuple) -> None:
    worker, path, body = admitted
    worker["codex"]["hook_allowed_tools"].append(host.IMAGE_TOOL)
    worker["codex"]["hooks"]["image_output_dir"] = "owned-images"
    body["allowed_tools"] = worker["codex"]["hook_allowed_tools"]
    path.write_text(json.dumps(body), encoding="utf-8")
    e = event(worker, host.IMAGE_TOOL, {"prompt": "a square"})
    assert invoke(admitted, e) == {}
    source = host.image_path(Path(body["home"]), THREAD_A, "call_1")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tiny image fixture")
    e.update(
        hook_event_name="PostToolUse",
        tool_response=[
            {
                "type": "input_text",
                "text": f"Generated images are saved to {source.parent} as {source} by default.\nextra",
            }
        ],
    )
    assert invoke(admitted, e, "PostToolUse") == {}
    context, moved = host.read_receipts(worker, path, THREAD_A)
    assert context is None and len(moved) == 1 and not source.exists()
    assert Path(moved[0]).read_bytes() == b"tiny image fixture"
    assert Path(moved[0]).is_relative_to(Path(body["root"]) / "owned-images")


def image_turn(admitted: tuple, calls: list[str], receipted: list[str]) -> list[Path]:
    worker, path, body = admitted
    worker["codex"]["hook_allowed_tools"].append(host.IMAGE_TOOL)
    body["allowed_tools"] = worker["codex"]["hook_allowed_tools"]
    path.write_text(json.dumps(body), encoding="utf-8")
    sources = []
    for call in calls:
        e = event(worker, host.IMAGE_TOOL, {"prompt": "a square"})
        e["tool_use_id"] = call
        assert invoke(admitted, e) == {}
        source = host.image_path(Path(body["home"]), THREAD_A, call)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(call.encode())
        sources.append(source)
        if call in receipted:
            text = f"Generated images are saved to {source.parent} as {source} by default."
            e.update(
                hook_event_name="PostToolUse", tool_response=[{"type": "input_text", "text": text}]
            )
            assert invoke(admitted, e, "PostToolUse") == {}
    return sources


def test_custody_validates_every_receipt_before_the_first_move(admitted: tuple) -> None:
    worker, path, _body = admitted
    first, _second = image_turn(admitted, ["call_1", "call_2"], ["call_1"])
    with pytest.raises(ValueError, match="no successful custody receipt: call_2"):
        host.read_receipts(worker, path, THREAD_A)
    assert first.read_bytes() == b"call_1"  # nothing moved
    with (path.parent / (json.loads(path.read_text())["launch_id"] + ".jsonl")).open("a") as log:
        log.write(json.dumps({"type": "hook_failure", "event": "Stop", "reason": "x"}) + "\n")
    with pytest.raises(ValueError, match="host hook failed"):
        host.read_receipts(worker, path, THREAD_A)
    assert first.read_bytes() == b"call_1"


def test_custody_failure_keeps_the_completed_transfers(admitted: tuple) -> None:
    worker, path, _body = admitted
    first, second = image_turn(admitted, ["call_1", "call_2"], ["call_1", "call_2"])
    blocker = host.image_destination(worker["codex"], THREAD_A, second.name)
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b"unrelated")
    with pytest.raises(host.CustodyError) as caught:
        host.read_receipts(worker, path, THREAD_A)
    assert len(caught.value.moved) == 1 and Path(caught.value.moved[0]).read_bytes() == b"call_1"
    assert not first.exists() and second.read_bytes() == b"call_2"
    assert caught.value.moved[0] in str(caught.value)


def test_image_prose_foreign_thread_and_unreceipted_call_refuse(admitted: tuple) -> None:
    worker, path, body = admitted
    e = event(worker, host.IMAGE_TOOL, {"prompt": "square"})
    with pytest.raises(ValueError, match="allowlist"):
        invoke(admitted, e)
    e["session_id"] = "another"
    with pytest.raises(ValueError, match="session"):
        invoke(admitted, e)
    worker["codex"]["hook_allowed_tools"].append(host.IMAGE_TOOL)
    body["allowed_tools"] = worker["codex"]["hook_allowed_tools"]
    path.write_text(json.dumps(body), encoding="utf-8")
    e["session_id"] = THREAD_A
    invoke(admitted, e)
    with pytest.raises(ValueError, match="no successful custody"):
        host.read_receipts(worker, path, THREAD_A)
    e.update(
        hook_event_name="PostToolUse",
        tool_response=[{"type": "input_text", "text": "move C:/unrelated/auth.json"}],
    )
    with pytest.raises(ValueError, match="no exact"):
        invoke(admitted, e, "PostToolUse")


def test_move_collision_preserves_source_and_cleans_staging(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"unrelated")
    with pytest.raises(OSError):
        host.move_image(source, destination)
    assert source.read_bytes() == b"source" and destination.read_bytes() == b"unrelated"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["destination", "source"]


def test_move_delete_failure_names_both_and_never_claims_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"source")
    original = Path.unlink

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == source:
            raise PermissionError("owned dummy refusal")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(ValueError, match="NOT moved") as error:
        host.move_image(source, destination)
    assert str(source) in str(error.value) and str(destination) in str(error.value)
    assert source.exists() and destination.read_bytes() == b"source"


def test_move_flush_or_verification_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"source")
    original = host.file_digest
    monkeypatch.setattr(
        host, "file_digest", lambda path: original(path) if path == source else (1, "bad")
    )
    with pytest.raises(ValueError, match="changed"):
        host.move_image(source, destination)
    assert source.exists() and not destination.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_skill_copies_are_byte_identical() -> None:
    root = Path(__file__).resolve().parents[3]
    for name in ("config-audit", "gpu-live-run", "handoff-report"):
        assert (root / ".agents/skills" / name / "SKILL.md").read_bytes() == (
            root / ".claude/skills" / name / "SKILL.md"
        ).read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_section", "heading"),
        ("stop_labels", []),
        ("stop_labels", ["A", "A"]),
        ("image_output_dir", ".git/images"),
        ("guard_config", ".secrets/config"),
        ("policy_dir", "<placeholder>"),
    ],
)
def test_remaining_option_refusals(world: World, field: str, value: object) -> None:
    world.write_config(lambda cfg: cfg["runtime"]["hooks"].update({field: value}))
    with pytest.raises(SystemExit):
        cb.load_codex_config(world.config)


def test_policy_and_guard_path_overrides_are_used(world: World) -> None:
    custom_guard = world.root / "alternate-guard.json"
    custom_guard.write_bytes((world.root / "tools/agent-workflow/guard_config.json").read_bytes())
    world.write_config(
        lambda cfg: cfg["runtime"]["hooks"].update(
            policy_dir="host-state", guard_config="alternate-guard.json"
        )
    )
    worker = worker_dict(world)
    policy = host.prepare(worker)
    assert policy.parent == world.root / "host-state"
    before = host.fingerprint(world.root, worker["codex"]["hooks"])
    custom_guard.write_text("{}", encoding="utf-8")
    assert host.fingerprint(world.root, worker["codex"]["hooks"]) != before


def test_byte_target_and_task_limits_are_enforced(admitted: tuple) -> None:
    worker, path, body = admitted
    patch = event(worker)["tool_input"]["command"]
    with pytest.raises(ValueError, match="byte limit"):
        guard.patch_targets(patch, 3, 20)
    task = Path(worker["cwd"]) / "task.md"
    task.write_text("xx", encoding="utf-8")
    body.update(task_file=str(task))
    body["options"]["max_task_bytes"] = 1
    path.write_text(json.dumps(body), encoding="utf-8")
    e = {k: v for k, v in event(worker).items() if not k.startswith("tool_")}
    e.update(hook_event_name="Stop", stop_hook_active=False, last_assistant_message=None)
    with pytest.raises(ValueError, match="too large"):
        invoke(admitted, e, "Stop")


@pytest.mark.parametrize("field", ["policy_dir", "guard_config"])
def test_linked_host_policy_refused(world: World, field: str) -> None:
    link = world.root / "alias"
    if not make_link(link, world.cwd):
        pytest.skip("link privilege unavailable")
    value = "alias/state" if field == "policy_dir" else "alias/guard.json"
    world.write_config(lambda cfg: cfg["runtime"]["hooks"].update({field: value}))
    with pytest.raises(SystemExit, match="linked"):
        worker_dict(world)


def test_worktree_launcher_and_write_grant_cannot_supply_host_hooks(world: World) -> None:
    (world.root / ".git").write_text("gitdir: dummy", encoding="utf-8")
    with pytest.raises(SystemExit, match="canonical root"):
        worker_dict(world)
    (world.root / ".git").unlink()
    world.write_config(lambda cfg: cfg["runtime"].update(extra_write_paths=["tools"]))
    with pytest.raises(SystemExit, match="writable"):
        worker_dict(world)


def test_context_reaches_registry_status_from_receipt(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    original = cw.launch_turn

    def launch(*args: object, **kwargs: object) -> int:
        result = original(*args, **kwargs)
        worker = args[1]
        directory = world.root / worker["codex"]["hooks"]["policy_dir"]
        policy = directory / (worker["codex"]["hook_id"] + ".json")
        body = json.loads(policy.read_text(encoding="utf-8"))
        observation = event(worker)
        observation.update(
            hook_event_name="PostModelRequest",
            status="completed",
            error=None,
            token_usage={"input_tokens": 12345},
        )
        guard.observe(observation, body, directory, "PostModelRequest")
        return result

    monkeypatch.setattr(cw, "launch_turn", launch)
    assert world.start() == 0
    assert world.entry()["context_tokens"] == 12345
    assert world.run("status") == 0
    assert "12345" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [None, "", "<version>", "latest", "0.167", True])
def test_tested_version_profile_refusal(world: World, bad: object) -> None:
    world.write_config(lambda cfg: cfg["runtime"].update(tested_codex_version=bad))
    with pytest.raises(SystemExit):
        cb.load_codex_config(world.config)


def test_committed_features_and_image_role_inventory() -> None:
    raw = json.loads(cb.CONFIG.read_text(encoding="utf-8"))
    assert set(raw["runtime"]["disabled_features"]) == host.DISABLED_FEATURES
    assert raw["roles"]["codex-image"]["hook_allowed_tools"] == [
        "Bash",
        "apply_patch",
        "view_image",
        host.IMAGE_TOOL,
    ]
    assert all(
        host.IMAGE_TOOL not in role["hook_allowed_tools"]
        for name, role in raw["roles"].items()
        if name != "codex-image"
    )


@pytest.mark.parametrize("broken", [None, {}, {"PreToolUse": []}, {"state": {}}, {"fake": "table"}])
def test_incompatible_stored_table_refused(world: World, broken: object) -> None:
    worker = worker_dict(world)
    worker["codex"]["hook_table"] = broken
    with pytest.raises(SystemExit, match="retire and restart"):
        cb.require_worker(worker)


@pytest.mark.parametrize(
    "args",
    [
        {"prompt": "square", "unknown": True},
        {"prompt": "square", "transparent_background": "yes"},
        {"prompt": "square", "num_last_images_to_include": 6},
        {"prompt": "square", "referenced_image_paths": ["../outside"]},
        {
            "prompt": "square",
            "referenced_image_paths": ["safe.png"],
            "num_last_images_to_include": 1,
        },
    ],
)
def test_image_arguments_strictly_refuse_siblings(admitted: tuple, args: dict) -> None:
    worker, path, body = admitted
    body["allowed_tools"] = [host.IMAGE_TOOL]
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError):
        invoke(admitted, event(worker, host.IMAGE_TOOL, args))


def test_image_output_cannot_target_runtime_or_link(world: World) -> None:
    for output in (".venv/images", "tools/agent-workflow/images", "wt/../images"):
        world.write_config(
            lambda cfg, output=output: cfg["runtime"]["hooks"].update(image_output_dir=output)
        )
        with pytest.raises(SystemExit):
            worker_dict(world)


@pytest.mark.parametrize("path", ["NUL", "CON.txt", "COM1", "folder/aux.log", "LPT9.txt"])
def test_reserved_patch_devices_refuse_before_resolution(admitted: tuple, path: str) -> None:
    worker, _policy, body = admitted
    with pytest.raises(ValueError, match="reserved"):
        guard.target(path, Path(worker["cwd"]), Path(body["root"]))


def test_lexical_private_target_refuses_before_filesystem_resolution(
    admitted: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, _policy, body = admitted

    def unexpected_resolve(*args: object, **kwargs: object) -> Path:
        raise AssertionError("private lexical path must not reach filesystem resolution")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    with pytest.raises(ValueError, match="protected"):
        guard.target(".secrets/file", Path(worker["cwd"]), Path(body["root"]))


def main_reply(
    admitted: tuple, payload: object, kind: str, capsys: pytest.CaptureFixture
) -> tuple[int, dict]:
    _worker, policy, body = admitted
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    code = guard.main(
        ["--policy", str(policy), "--digest", body["digest"], "--event", kind], io.BytesIO(data)
    )
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("kind", ["PreToolUse", "Stop", "PostToolUse", "PostModelRequest"])
@pytest.mark.parametrize(
    "payload",
    [b"{", b"\xff", b"null", b" " * 65],
    ids=["malformed-json", "invalid-utf8", "non-object", "oversized"],
)
def test_event_failure_reply_and_host_error_contract(
    admitted: tuple, kind: str, payload: bytes, capsys: pytest.CaptureFixture
) -> None:
    worker, policy, body = admitted
    body["options"]["max_input_bytes"] = 64
    policy.write_text(json.dumps(body), encoding="utf-8")
    code, reply = main_reply(admitted, payload, kind, capsys)
    assert code == (1 if kind == "PostModelRequest" else 0)
    if kind == "PreToolUse":
        assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"
        with pytest.raises(ValueError, match="no hook receipt"):
            host.read_receipts(worker, policy, THREAD_A)
    else:
        if kind in ("Stop", "PostToolUse"):
            assert reply["decision"] == "block" and reply["reason"]
        else:
            assert reply["systemMessage"]
        assert "hookSpecificOutput" not in reply
        with pytest.raises(ValueError, match="host hook failed"):
            host.read_receipts(worker, policy, THREAD_A)
    if kind == "Stop":
        assert main_reply(admitted, payload, kind, capsys) == (0, {})


@pytest.mark.parametrize("kind", ["PreToolUse", "Stop", "PostToolUse", "PostModelRequest"])
def test_always_raising_adapter_uses_event_specific_failure(
    admitted: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, kind: str
) -> None:
    def broken(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("deliberately broken adapter")

    monkeypatch.setattr(guard, "run", broken)
    code, reply = main_reply(admitted, b"{}", kind, capsys)
    assert code == (1 if kind == "PostModelRequest" else 0)
    if kind == "PreToolUse":
        assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"
    elif kind == "PostModelRequest":
        assert reply["systemMessage"]
    else:
        assert reply["decision"] == "block"
    if kind == "Stop":
        assert main_reply(admitted, b"{}", kind, capsys) == (0, {})


def test_stop_failure_shares_normal_continuation_marker_and_active_flag(
    admitted: tuple, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    worker, policy, _body = admitted
    e = {k: v for k, v in event(worker).items() if not k.startswith("tool_")}
    e.update(hook_event_name="Stop", stop_hook_active=False, last_assistant_message=None)
    assert invoke(admitted, e, "Stop")["decision"] == "block"

    def broken(*args: object) -> dict:
        raise RuntimeError("broken checklist reader")

    monkeypatch.setattr(guard, "stop", broken)
    assert main_reply(admitted, e, "Stop", capsys) == (0, {})
    fresh_policy = host.prepare(worker)
    fresh = worker, fresh_policy, json.loads(fresh_policy.read_text(encoding="utf-8"))
    e["stop_hook_active"] = True
    assert main_reply(fresh, e, "Stop", capsys) == (0, {})
    assert not (policy.parent / (fresh[2]["launch_id"] + ".stop")).exists()


@pytest.mark.parametrize("kind", ["Stop", "PostToolUse", "PostModelRequest"])
def test_unusable_policy_has_valid_failure_reply_without_continuation_loop(
    admitted: tuple, capsys: pytest.CaptureFixture, kind: str
) -> None:
    _worker, policy, _body = admitted
    policy.write_bytes(b"broken json")
    code, reply = main_reply(admitted, b"{}", kind, capsys)
    if kind == "Stop":
        assert code == 0 and reply["continue"] is False and reply["stopReason"]
        assert main_reply(admitted, b"{}", kind, capsys) == (code, reply)
    else:
        assert code == 1
        assert reply["decision"] == "block" if kind == "PostToolUse" else reply["systemMessage"]


def test_unparseable_event_retains_legacy_deny(
    admitted: tuple, capsys: pytest.CaptureFixture
) -> None:
    code, reply = main_reply(admitted, b"{}", "UnknownEvent", capsys)
    assert code == 0 and reply["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bom_task_first_section_is_complete_and_duplicate_section_is_untrusted(
    admitted: tuple,
) -> None:
    worker, policy, body = admitted
    task = Path(worker["cwd"]) / "completion.md"
    body["task_file"] = str(task)
    body["options"].update(stop_section="## Finished", stop_labels=["Proof"])
    policy.write_text(json.dumps(body), encoding="utf-8-sig")
    contents = "## Finished\n- [x] Proof: verified\n"
    task.write_text(contents, encoding="utf-8-sig")
    e = {k: v for k, v in event(worker).items() if not k.startswith("tool_")}
    e.update(hook_event_name="Stop", stop_hook_active=False, last_assistant_message=None)
    assert invoke(admitted, e, "Stop") == {}
    task.write_text(contents + "\n" + contents, encoding="utf-8-sig")
    assert invoke(admitted, e, "Stop")["decision"] == "block"


def test_observe_parser_contract_and_failure_is_not_success(
    admitted: tuple, capsys: pytest.CaptureFixture
) -> None:
    worker, policy, body = admitted
    observation = event(worker)
    observation.update(
        hook_event_name="PostModelRequest",
        status="completed",
        error=None,
        token_usage={"input_tokens": 2468},
    )
    guard.observe(observation, body, policy.parent, "PostModelRequest")
    assert host.read_receipts(worker, policy, THREAD_A) == (2468, [])
    malformed = {**observation, "token_usage": {"input_tokens": "invalid"}}
    for key in ("tool_name", "tool_input", "tool_use_id", "permission_mode"):
        malformed.pop(key)
    assert main_reply(admitted, malformed, "PostModelRequest", capsys)[1]["systemMessage"]
    with pytest.raises(ValueError, match="host hook failed"):
        host.read_receipts(worker, policy, THREAD_A)


def test_lost_image_receipt_failure_cannot_return_success_without_pending_record(
    admitted: tuple, capsys: pytest.CaptureFixture
) -> None:
    worker, policy, body = admitted
    worker["codex"]["role"] = "codex-image"
    worker["codex"]["hook_allowed_tools"].append(host.IMAGE_TOOL)
    body["allowed_tools"] = worker["codex"]["hook_allowed_tools"]
    policy.write_text(json.dumps(body), encoding="utf-8")
    image_event = event(worker, host.IMAGE_TOOL, {"prompt": "dummy"})
    image_event.update(hook_event_name="PostToolUse", tool_response=[])
    code, reply = main_reply(admitted, image_event, "PostToolUse", capsys)
    assert code == 0 and reply["decision"] == "block"
    with pytest.raises(ValueError, match=r"host hook failed.*PostToolUse"):
        host.read_receipts(worker, policy, THREAD_A)
