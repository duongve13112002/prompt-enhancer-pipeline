"""Codex backend of cli_worker.py (decision 596) against tests/fake_codex.py, a TEST DOUBLE.

Nothing here runs real Codex: the fake records its arguments, stdin and environment and prints scripted `--json`
events, so these tests prove how the launcher builds, supervises and records a turn, not how Codex behaves.
"""

# The refusal table reads best as `RUNTIME + ["field"]` paths, so the list-concatenation rule is off in this file.
# ruff: noqa: RUF005
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_link
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
FAKE = HERE / "fake_codex.py"
spec = importlib.util.spec_from_file_location("cli_worker", TOOLS / "cli_worker.py")
assert spec is not None and spec.loader is not None
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
cb = cw.sibling("codex_backend")
us = cw.sibling("usage_snapshot")

# TEST SEAM: production accepts one executable path as `runtime.codex_command`, but the fake is a Python script that
# needs an interpreter in front of it. The World fixture swaps in this prefix for the config value `str(FAKE)`; the
# refusal tests call the REAL validators (captured here, before any patching).
REAL_VALIDATE_COMMAND = cb.validate_command
REAL_CHECK_PREFIX = cb.check_prefix
FAKE_PREFIX = [sys.executable, "-B", str(FAKE)]


def seam_validate_command(
    owner: str, field: str, value: object, shim_ok: bool = False
) -> list[str]:
    if field == "codex_command" and value == str(FAKE):
        return list(FAKE_PREFIX)
    return REAL_VALIDATE_COMMAND(owner, field, value, shim_ok)  # type: ignore[no-any-return]


def seam_check_prefix(prefix: Any) -> None:
    if list(prefix) != FAKE_PREFIX:
        REAL_CHECK_PREFIX(prefix)


THREAD_A = "11111111-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"
BRIEF = "[brief] do the work; the words error and failed are only text"
USAGE_1 = {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 50}
USAGE_2 = {"input_tokens": 3000, "cached_input_tokens": 200, "output_tokens": 120}
USAGE_3 = {"input_tokens": 6000, "cached_input_tokens": 2900, "output_tokens": 300}


class World:
    """A project root with a worktree-like cwd, a Codex home OUTSIDE the root, a config and a fake plan."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "root"
        self.cwd = self.root / "wt"
        self.home = tmp_path / "codex-home"
        self.record = tmp_path / "record"
        self.state = self.root / ".tmp" / "pm-state"
        for checkout in (self.root, self.cwd):
            (checkout / ".secrets").mkdir(parents=True)
            (checkout / ".secrets" / "README.md").write_text("tracked readme", encoding="utf-8")
        (self.root / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
        (self.root / ".venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
        (self.root / ".venv" / "bin").mkdir()
        (self.root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        self.venv_dir = self.root / ".venv"
        # Labelled synthetic root: no real root policy or home is modified.
        for relative in cb.hook_module().TRUST_FILES:
            dest = self.root / "tools" / "agent-workflow" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TOOLS / relative, dest)
        (self.venv_dir / "Scripts").mkdir()
        (self.venv_dir / "Scripts" / "python.exe").write_text("", encoding="utf-8")
        self.msg = tmp_path / "brief.md"
        self.msg.write_text(BRIEF, encoding="utf-8")
        self.config = tmp_path / "codex_profiles.json"
        self.plan_file = tmp_path / "plan.json"
        self.write_config()
        self.plan()
        monkeypatch.setattr(cw, "ROOT", self.root)
        monkeypatch.setattr(cw, "REGISTRY", self.state / "cli-workers.json")
        monkeypatch.setattr(cw, "LOCK", self.state / "cli-workers.lock")
        monkeypatch.setattr(cw, "JOBS", self.root / ".tmp" / "cli-jobs")
        monkeypatch.setattr(cw, "LEDGER", self.state / "worker-ledger.jsonl")
        monkeypatch.setattr(cw, "lint_gate", lambda *args, **kwargs: None)
        monkeypatch.setattr(cb, "validate_command", seam_validate_command)
        monkeypatch.setattr(cb, "check_prefix", seam_check_prefix)
        monkeypatch.setenv("FAKE_CODEX_PLAN", str(self.plan_file))
        for name, value in {
            "ANTHROPIC_API_KEY": "ambient-anthropic",
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "OPENAI_API_KEY": "ambient-openai",
            "GITHUB_TOKEN": "ambient-token",
            "SOME_SECRET": "ambient-secret",
            "FAKE_ALLOWED": "allowed-value",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        self.monkeypatch = monkeypatch

    def config_dict(self) -> dict[str, Any]:
        return {
            "runtime": {
                "codex_command": str(FAKE),
                "codex_home": str(self.home),
                "windows_sandbox": "elevated",
                "permission_profile": "project-worker",
                "extra_deny_paths": [],
                "extra_write_paths": [],
                "scratch_dir": ".tmp/codex-run",
                "venv_dir": ".venv",
                "check_timeout_seconds": 20,
                "env_allow": ["FAKE_CODEX_PLAN", "FAKE_ALLOWED"],
                "disabled_features": sorted(cb.hook_module().DISABLED_FEATURES),
                "tested_codex_version": "0.167.0",
                "hooks": json.loads((TOOLS / "codex_profiles.json").read_text(encoding="utf-8"))[
                    "runtime"
                ]["hooks"],
            },
            "roles": {
                "codex-dev": {
                    "model": "test-model",
                    "effort": "high",
                    "web_search": "disabled",
                    "timeout_minutes": 30,
                    "hook_allowed_tools": ["Bash", "apply_patch", "view_image"],
                },
            },
            "usage": {
                "stale_minutes": 60,
                "session_file_limit": 8,
                "cache_warn_ratio": 0.5,
                "antigravity_command": None,
                "command_timeout_seconds": 20,
            },
        }

    def write_config(self, mutate: Any = None) -> dict[str, Any]:
        cfg = self.config_dict()
        if mutate is not None:
            mutate(cfg)
        self.config.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    def plan(self, **kwargs: Any) -> None:
        body = {
            "record": str(self.record),
            "turns": [{"thread_id": THREAD_A, "usage": USAGE_1}],
            **kwargs,
        }
        self.plan_file.write_text(json.dumps(body), encoding="utf-8")

    def run(self, *args: str) -> int:
        self.monkeypatch.setattr("sys.argv", ["cli_worker.py", *args])
        return cw.main()

    def start(self, name: str = "w1", *extra: str) -> int:
        return self.run(
            "start", "--backend", "codex", "--name", name, "--profile", "codex-dev", "--cwd", str(self.cwd),
            "--message-file", str(self.msg), "--codex-config", str(self.config), "--label", "T-TEST", *extra,
        )  # fmt: skip

    def send(self, name: str = "w1", *extra: str) -> int:
        return self.run("send", "--name", name, "--message-file", str(self.msg), *extra)

    def entry(self, name: str = "w1") -> dict[str, Any]:
        return json.loads(cw.REGISTRY.read_text(encoding="utf-8"))[name]

    def calls(self) -> list[dict[str, Any]]:
        files = sorted(self.record.glob("[0-9]*.json"), key=lambda p: int(p.stem))
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def execs(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if c["argv"][0] == "exec"]

    def sandboxes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if c["argv"][0] == "sandbox"]

    def ledger(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in cw.LEDGER.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def worker_dict(world: World, thread: str | None = THREAD_A, **codex: Any) -> dict[str, Any]:
    cfg = cb.load_codex_config(world.config)
    cx, model, effort = cb.resolve_worker(
        cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ
    )
    return {
        "backend": "codex", "model": model, "effort": effort, "cwd": str(world.cwd), "thread_id": thread,
        "codex": {**cx, **codex},
    }  # fmt: skip


# ---- command line (items 1, 4, 6, 12, 13) ---------------------------------------------------------------------


def test_an_unknown_role_is_refused_before_anything_is_registered(world: World) -> None:
    cfg = cb.load_codex_config(world.config)
    with pytest.raises(SystemExit, match="unknown codex role"):
        cb.resolve_worker(
            cfg, "no-such-role", world.root, world.cwd, "w1", world.config, os.environ
        )
    with pytest.raises(SystemExit):
        world.run(
            "start", "--backend", "codex", "--name", "w1", "--profile", "no-such-role",
            "--cwd", str(world.cwd), "--message-file", str(world.msg), "--codex-config", str(world.config),
        )  # fmt: skip
    assert not cw.REGISTRY.exists() and not world.calls()


def test_start_command_shape_prompt_on_stdin_and_inline_permissions(world: World) -> None:
    assert world.start() == 0
    (call,) = world.execs()
    argv = call["argv"]
    assert argv[:4] == ["exec", "-C", str(world.cwd), "--json"]
    assert argv[4:6] == ["-m", "test-model"]
    assert argv[-1] == "-" and argv[-3] == "-o"
    assert call["stdin"] == BRIEF and BRIEF not in " ".join(argv)
    overrides = [argv[i + 1] for i, item in enumerate(argv) if item == "-c"]
    assert 'default_permissions="project-worker"' in overrides
    assert 'permissions.project-worker.extends=":workspace"' in overrides
    assert 'windows.sandbox="elevated"' in overrides
    assert 'model_reasoning_effort="high"' in overrides and 'web_search="disabled"' in overrides
    # An ambient fast tier cannot apply: both pins ride on every call.
    assert cb.SERVICE_TIER_PIN in overrides and cb.FAST_MODE_PIN in overrides
    table = next(o for o in overrides if o.startswith("permissions.project-worker.filesystem="))
    assert '":workspace_roots"={".secrets"="deny"}' in table
    for path in (world.home / "auth.json", world.root / ".secrets", world.cwd / ".secrets"):
        assert cb.toml_string(str(path)) + '="deny"' in table
    assert not {"-s", "--sandbox", "--last", "--add-dir"} & set(argv)
    assert call["cwd"] == str(world.cwd)


def test_a_custody_failure_keeps_completed_images_on_the_worker(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = cb.hook_module()

    def failing(*_args: object) -> tuple[int | None, list[str]]:
        raise hooks.CustodyError("second move failed", ["D:/done/first.png"])

    monkeypatch.setattr(hooks, "read_receipts", failing)
    world.plan(turns=[{"thread_id": THREAD_A, "usage": USAGE_1}])
    assert world.start() == 1
    assert world.entry()["generated_images"] == ["D:/done/first.png"]


def test_a_turn_with_no_hook_receipt_fails_as_hooks_did_not_run(
    world: World, capsys: pytest.CaptureFixture
) -> None:
    world.plan(turns=[{"thread_id": THREAD_A, "usage": USAGE_1, "no_hook_receipt": True}])
    assert world.start() == 1
    assert world.entry()["status"].startswith("error")
    assert "no hook receipt" in capsys.readouterr().out


def test_resume_uses_the_captured_thread_and_none_of_the_forbidden_flags(world: World) -> None:
    world.plan(
        turns=[{"thread_id": THREAD_A, "usage": USAGE_1}, {"thread_id": THREAD_B, "usage": USAGE_2}]
    )
    assert world.start() == 0
    assert world.entry()["thread_id"] == THREAD_A != world.entry()["session_id"]
    assert world.send() == 0
    _first, second = world.execs()
    argv = second["argv"]
    assert argv[:3] == ["exec", "resume", "--json"]
    assert argv[-2:] == [THREAD_A, "-"] and second["stdin"] == BRIEF
    assert not {"--last", "-C", "-s", "--sandbox", "--add-dir"} & set(argv)
    assert second["cwd"] == str(world.cwd)
    assert world.entry()["thread_id"] == THREAD_A  # a later stream never rebinds the worker
    assert "default_permissions" in " ".join(argv) and 'windows.sandbox="elevated"' in " ".join(
        argv
    )
    # The resume call pins the tier too.
    assert cb.SERVICE_TIER_PIN in argv and cb.FAST_MODE_PIN in argv


def test_missing_thread_id_is_an_error_and_not_resumable(world: World) -> None:
    world.plan(turns=[{"mode": "no_thread", "usage": USAGE_1}])
    assert world.start() == 1
    entry = world.entry()
    assert entry["thread_id"] is None and entry["status"].startswith("error")
    assert "no thread id" in world.ledger()[0]["result"]
    with pytest.raises(SystemExit, match="no Codex thread id"):
        world.send()
    assert len(world.execs()) == 1


def test_a_retired_name_restarted_gets_a_fresh_binding(world: World) -> None:
    assert world.start() == 0
    first = world.entry()
    assert world.run("retire", "--name", "w1") == 0
    world.plan(turns=[{"thread_id": THREAD_B, "usage": USAGE_1}])
    assert world.start() == 0
    second = world.entry()
    assert second["session_id"] != first["session_id"]
    assert (first["thread_id"], second["thread_id"]) == (THREAD_A, THREAD_B)


def test_non_default_overrides_reach_the_command_and_the_registry(world: World) -> None:
    assert (
        world.start("w1", "--model", "other-model", "--effort", "low", "--timeout-minutes", "7")
        == 0
    )
    argv = world.execs()[0]["argv"]
    assert argv[argv.index("-m") + 1] == "other-model"
    assert 'model_reasoning_effort="low"' in argv
    assert world.entry()["timeout_minutes"] == 7 and world.entry()["effort"] == "low"
    world.plan(turns=[{"thread_id": THREAD_A, "usage": USAGE_1}])
    assert world.send("w1", "--effort", "xhigh") == 0
    assert 'model_reasoning_effort="xhigh"' in world.execs()[-1]["argv"]


@pytest.mark.parametrize(
    "flags",
    [
        ("--cache-ttl", "5m"),
        ("--ctx-nudge", "100"),
        ("--autocompact-window", "200000"),
        ("--mcp", "minimal"),
        ("--advisor", "opus"),
        ("--git-guard", "merge"),
        ("--effort", "ultra"),
    ],
)
def test_start_refuses_flags_a_codex_worker_cannot_honour(
    world: World, flags: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit):
        world.start("w1", *flags)
    assert not cw.REGISTRY.exists() and not world.record.exists()


def test_start_refuses_an_effort_ultra_from_a_send_and_a_placeholder_model(world: World) -> None:
    assert world.start() == 0
    with pytest.raises(SystemExit):  # argparse: ultra is not an accepted --effort choice
        world.send("w1", "--effort", "ultra")
    with pytest.raises(SystemExit, match="ultra"):
        cb.validate_effort("--effort", "ultra")
    with pytest.raises(SystemExit, match="placeholder"):
        world.start("w2", "--model", "<codex model id>")


def test_notify_is_refused_for_a_codex_worker(world: World) -> None:
    assert world.start() == 0
    data = json.loads(cw.REGISTRY.read_text(encoding="utf-8"))
    data["w1"]["status"] = "busy"
    cw.REGISTRY.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="Codex has no PostToolUse hook"):
        cw.notify("w1", "hello")


@pytest.mark.parametrize(
    "item",
    [
        'sandbox_mode="danger-full-access"',
        "default_permissions=:danger-full-access",
        'default_permissions="has space"',
        'service_tier="fast"',
        "service_tier=fast",
        'service_tier = "fast"',
        'service_tier="default" ',
        "features.fast_mode=true",
        'features.fast_mode="false"',
        'model_reasoning_effort="ultra"',
        "model_reasoning_effort=ultra",
        'web_search="on"',
        'windows.sandbox="off"',
        'permissions.project-worker.extends=":danger-full-access"',
        'permissions.project-worker.filesystem={":root"="write"}',
        'permissions.project-worker.filesystem={"a"={"b"="write"}}',
        "permissions.project-worker.filesystem={}",
        "permissions.project-worker.filesystem=not-toml",
        'approval_policy="never"',
        'profile="fast"',
        "no equals sign",
    ],
)
def test_unsafe_overrides_are_refused_by_the_allowlist(item: str) -> None:
    with pytest.raises(SystemExit, match="refused"):
        cb.check_override(item)


def good_args(world: World, resume: bool, tmp_path: Path) -> list[str]:
    cmd = cb.build_command(worker_dict(world), resume, tmp_path / "r.md")
    assert cmd[: len(FAKE_PREFIX)] == FAKE_PREFIX
    return cmd[len(FAKE_PREFIX) :]


def flag_boundaries(args: list[str]) -> list[int]:
    """Indexes where a flag may start in a well-formed exec / resume argument list."""
    found, index = [], 2 if args[1] == "resume" else 1
    while args[index].startswith("-") and args[index] != "-":
        found.append(index)
        index += 1 if args[index] == "--json" else 2
    return found


FORBIDDEN_INSERTS = [
    ["-s", "read-only"],
    ["--sandbox", "read-only"],
    ["--sandbox=read-only"],
    ["-sread-only"],
    ["--dangerously-bypass-approvals-and-sandbox"],
    ["--dangerously-bypass-hook-trust"],
    ["--full-auto"],
    ["--add-dir", "x"],
    ["--oss"],
    ["-p", "x"],
    ["--profile", "x"],
    ["--last"],
    ["--config", 'service_tier="fast"'],
    ["--config=service_tier=fast"],
    ["-c", 'service_tier="fast"'],
    ["-c", "service_tier=fast"],
    ["-c", "features.fast_mode=true"],
    ["-c", 'sandbox_mode="danger-full-access"'],
    ["-c", 'approval_policy="never"'],
    ["-c", cb.SERVICE_TIER_PIN],  # a repeated key
    ["-c", cb.FAST_MODE_PIN],
    ["-C", "elsewhere"],  # a second working directory (exec) or any one (resume)
    ["-m", "other"],
    ["-o", "other.md"],
    ["-"],
]


def test_the_whole_assembled_argv_is_checked_not_only_a_part(world: World, tmp_path: Path) -> None:
    for resume in (False, True):
        args = good_args(world, resume, tmp_path)
        mode = "resume" if resume else "exec"
        table = next(x[6:] for x in args if x.startswith("hooks="))
        assert cb.check_argv(args, mode, hooks_table=table) == len(args) - (2 if resume else 1)
        for insert in FORBIDDEN_INSERTS:
            for at in flag_boundaries(args):
                with pytest.raises(SystemExit, match="refused"):
                    cb.check_argv([*args[:at], *insert, *args[at:]], mode, hooks_table=table)
        with pytest.raises(SystemExit, match="refused"):
            cb.check_argv([*args, "extra"], mode, hooks_table=table)
        for key in (cb.SERVICE_TIER_PIN, cb.FAST_MODE_PIN):
            cut = args.index(key)
            # The tier pin is mandatory: without its `-c key` pair the command is refused.
            without = [*args[: cut - 1], *args[cut + 1 :]]
            with pytest.raises(SystemExit, match="missing required"):
                cb.check_argv(without, mode, hooks_table=table)
    with pytest.raises(SystemExit, match="refused"):
        cb.check_argv(good_args(world, False, tmp_path)[:1] + ["resume"], "exec")


@given(
    insert=st.sampled_from(FORBIDDEN_INSERTS),
    resume=st.booleans(),
    pick=st.integers(min_value=0, max_value=200),
)
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_no_forbidden_flag_survives_at_any_flag_position(
    world: World, insert: list[str], resume: bool, pick: int
) -> None:
    args = good_args(world, resume, world.tmp)
    spots = flag_boundaries(args)
    at = spots[pick % len(spots)]
    with pytest.raises(SystemExit, match="refused"):
        cb.check_argv([*args[:at], *insert, *args[at:]], "resume" if resume else "exec")


@pytest.mark.parametrize(
    "prefix",
    [
        ["codex.exe", "-c", 'service_tier="fast"'],
        ["codex.exe", "exec"],
        ["codex.exe", "--dangerously-bypass-approvals-and-sandbox"],
        ["-c"],
        [""],
        [],
    ],
)
def test_the_command_prefix_is_exactly_one_executable_path(prefix: list[str]) -> None:
    with pytest.raises(SystemExit, match="refused command prefix"):
        REAL_CHECK_PREFIX(prefix)
    REAL_CHECK_PREFIX(["C:/tools/codex.exe"])


def test_a_prefix_that_smuggles_a_flag_cannot_start_a_worker(world: World) -> None:
    smuggled = ["codex.exe", "-c", 'service_tier="fast"']
    world.write_config(put(RUNTIME + ["codex_command"], smuggled))
    with pytest.raises(SystemExit, match="one executable path"):
        world.start()
    # A single string that merely contains the flags is one (non-existent) path: preflight refuses it, nothing runs.
    world.write_config(put(RUNTIME + ["codex_command"], 'codex.exe -c service_tier="fast"'))
    with pytest.raises(SystemExit, match="codex executable not found"):
        world.start()
    assert not cw.REGISTRY.exists() and world.calls() == []
    # Even a stored registry value is re-checked where the argv is assembled.
    worker = worker_dict(world, command=smuggled)
    with pytest.raises(SystemExit, match="refused command prefix"):
        cb.build_command(worker, False, world.tmp / "r.md")


def test_build_command_never_emits_a_refused_argument(world: World, tmp_path: Path) -> None:
    for resume in (False, True):
        args = good_args(world, resume, tmp_path)
        cb.check_argv(
            args,
            "resume" if resume else "exec",
            hooks_table=next(x[6:] for x in args if x.startswith("hooks=")),
        )
        assert not {"--last", "-s", "--sandbox"} & set(args)
        overrides = [args[i + 1] for i, item in enumerate(args) if item == "-c"]
        assert len(overrides) == len({o.split("=", 1)[0] for o in overrides})  # no key twice


# ---- config validation (items 1, 10, 14; configuration audit refusal rows) ------------------------------------


def drop(path: list[str]) -> Any:
    def apply(cfg: dict[str, Any]) -> None:
        node = cfg
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return apply


def put(path: list[str], value: Any) -> Any:
    def apply(cfg: dict[str, Any]) -> None:
        node = cfg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


RUNTIME = ["runtime"]
ROLE = ["roles", "codex-dev"]
REFUSALS = [
    *[(drop(RUNTIME + [k]), "missing field") for k in cb.RUNTIME_KEYS],
    *[(drop(ROLE + [k]), "missing field") for k in cb.ROLE_KEYS],
    (drop(["usage"]), "expected exactly the keys"),
    (drop(["usage", "stale_minutes"]), "missing field"),
    (drop(["usage", "cache_warn_ratio"]), "missing field"),
    (drop(["usage", "session_file_limit"]), "missing field"),
    (put(ROLE + ["model"], "<codex model id>"), "placeholder"),
    (put(ROLE + ["model"], ""), "non-empty"),
    (put(ROLE + ["model"], "two words"), "white-space"),
    (put(ROLE + ["effort"], "ultra"), "refused"),
    (put(ROLE + ["effort"], "Fast Mode"), "refused|white-space"),
    (put(ROLE + ["effort"], 3), "non-empty string"),
    (put(ROLE + ["service_tier"], "fast"), "fast service tier"),
    (put(ROLE + ["sandbox_mode"], "danger-full-access"), "sandbox_mode"),
    (put(ROLE + ["dangerously_bypass"], True), "refused"),
    (put(ROLE + ["surprise"], 1), "unknown key"),
    (put(ROLE + ["web_search"], "on"), "web_search"),
    (put(ROLE + ["timeout_minutes"], 0), "positive integer"),
    (put(ROLE + ["timeout_minutes"], True), "positive integer"),
    (put(RUNTIME + ["sandbox"], "read-only"), "sandbox is fixed"),
    (put(RUNTIME + ["codex_command"], "C:/x/codex.cmd"), "shim"),
    (put(RUNTIME + ["codex_command"], "C:/x/codex.BAT"), "shim"),
    (put(RUNTIME + ["codex_command"], []), "one executable path"),
    (put(RUNTIME + ["codex_command"], ["codex.exe"]), "one executable path"),
    (
        put(RUNTIME + ["codex_command"], ["codex.exe", "-c", 'service_tier="fast"']),
        "one executable path",
    ),
    (put(RUNTIME + ["codex_command"], "-c"), "cannot start with"),
    (put(RUNTIME + ["codex_command"], ""), "non-empty"),
    (put(RUNTIME + ["codex_command"], "<absolute path>"), "placeholder"),
    (drop(["usage", "antigravity_command"]), "missing field"),
    (drop(["usage", "command_timeout_seconds"]), "missing field"),
    (put(["usage", "command_timeout_seconds"], 0), "positive integer"),
    (put(["usage", "command_timeout_seconds"], True), "positive integer"),
    (put(["usage", "antigravity_command"], ["agy", "-p"]), "one executable path"),
    (put(["usage", "antigravity_command"], "<path of agy>"), "placeholder"),
    (put(["usage", "antigravity_command"], "--help"), "cannot start with"),
    (put(ROLE + ["model"], "--oss"), "cannot start with"),
    (put(RUNTIME + ["windows_sandbox"], "off"), "windows_sandbox"),
    (put(RUNTIME + ["permission_profile"], ":danger-full-access"), "custom profile"),
    (put(RUNTIME + ["permission_profile"], "has space"), "white-space"),
    (put(RUNTIME + ["env_allow"], ["ANTHROPIC_API_KEY"]), "credential-like"),
    (put(RUNTIME + ["env_allow"], ["MY_TOKEN"]), "credential-like"),
    (put(RUNTIME + ["env_allow"], "PATH"), "list of variable names"),
    (put(RUNTIME + ["scratch_dir"], "/abs/scratch"), "relative path"),
    (put(RUNTIME + ["scratch_dir"], "../out"), "relative path"),
    (put(RUNTIME + ["venv_dir"], "..\\up"), "relative path"),
    (put(RUNTIME + ["check_timeout_seconds"], 0), "positive integer"),
    (put(RUNTIME + ["extra_deny_paths"], "x"), "expected a list"),
    (put(["usage", "cache_warn_ratio"], 0), "above 0"),
    (put(["usage", "cache_warn_ratio"], 1.5), "above 0"),
    (put(["usage", "session_file_limit"], 0), "positive integer"),
    (put(["usage", "session_file_limit"], True), "positive integer"),
    (put(["usage", "session_file_limit"], "8"), "positive integer"),
    (put(["usage", "stale_minutes"], 0), "positive integer"),
]


@pytest.mark.parametrize(("change", "match"), REFUSALS)
def test_config_refusals(world: World, change: Any, match: str) -> None:
    world.write_config(change)
    with pytest.raises(SystemExit, match=match):
        cb.load_codex_config(world.config)
    with pytest.raises(
        SystemExit
    ):  # the same refusal reaches the CLI before anything is registered
        world.start()
    assert not cw.REGISTRY.exists()


def test_config_overrides_are_honoured(world: World) -> None:
    def change(cfg: dict[str, Any]) -> None:
        cfg["runtime"].update(
            windows_sandbox=None,
            permission_profile="other-profile",
            extra_deny_paths=["extra/dir"],
            codex_home=None,
        )
        cfg["roles"]["codex-dev"].update(
            model="m2", effort="medium", web_search="live", timeout_minutes=5
        )

    world.write_config(change)
    assert world.start() == 0
    argv = world.execs()[0]["argv"]
    joined = " ".join(argv)
    assert 'default_permissions="other-profile"' in argv and "windows.sandbox" not in joined
    assert 'web_search="live"' in argv and 'model_reasoning_effort="medium"' in argv
    assert cb.toml_string(str(world.root / "extra" / "dir")) in joined
    assert world.entry()["timeout_minutes"] == 5


def test_extra_deny_paths_accept_absolute_and_root_relative_entries(world: World) -> None:
    absolute = world.tmp / "elsewhere" / "private"
    world.write_config(put(RUNTIME + ["extra_deny_paths"], [str(absolute), "extra/dir"]))
    cx = worker_dict(world)["codex"]
    assert str(absolute) in cx["deny"] and str(world.root / "extra" / "dir") in cx["deny"]
    assert world.start() == 0
    argv = world.execs()[0]["argv"]
    table = next(a for a in argv if a.startswith("permissions.project-worker.filesystem="))
    assert cb.toml_string(str(absolute)) + '="deny"' in table


def test_extra_write_paths_grant_only_the_configured_limiter_folder(world: World) -> None:
    world.write_config(put(RUNTIME + ["extra_write_paths"], [".tmp/pm-state/test-slots"]))
    assert world.start() == 0
    cx = world.entry()["codex"]
    granted = str(world.root / ".tmp" / "pm-state" / "test-slots")
    assert cx["write"] == [granted] and cx["project_root"] == str(world.root)
    for call in [world.execs()[0], world.sandboxes()[0]]:
        item = next(
            a for a in call["argv"] if a.startswith("permissions.project-worker.filesystem=")
        )
        table = tomllib.loads(item)["permissions"]["project-worker"]["filesystem"]
        assert table[granted] == "write"
        assert all(table[path] == "deny" for path in cx["deny"])
        assert table[":workspace_roots"] == {".secrets": "deny"}
    unsafe = 'permissions.project-worker.filesystem={"/outside"="write"}'
    with pytest.raises(SystemExit):
        cb.check_override(unsafe, cx["write"])


@pytest.mark.parametrize(
    "paths",
    [
        None,
        "folder",
        [None],
        [True],
        ["../outside"],
        ["/outside"],
        ["C:/outside"],
        [".secrets. /child"],
        [".secrets:stream"],
    ],
)
def test_extra_write_paths_reject_wrong_types_and_non_relative_paths(
    world: World, paths: Any
) -> None:
    world.write_config(put(RUNTIME + ["extra_write_paths"], paths))
    with pytest.raises(SystemExit):
        world.start()
    assert not world.calls() and not cw.REGISTRY.exists()


@pytest.mark.parametrize(
    "path",
    [".", ".secrets", ".secrets/child", "protected", "protected/nested", "protected/nested/child"],
)
def test_extra_write_paths_refuse_every_denied_overlap_direction(world: World, path: str) -> None:
    def change(cfg: dict[str, Any]) -> None:
        cfg["runtime"].update(extra_write_paths=[path], extra_deny_paths=["protected/nested"])

    world.write_config(change)
    with pytest.raises(SystemExit):
        world.start()
    assert not world.calls() and not cw.REGISTRY.exists()


@pytest.mark.parametrize("target_kind", ["outside", "private", "deny-alias"])
def test_extra_write_paths_refuse_link_escapes_and_denied_aliases(
    world: World, target_kind: str
) -> None:
    link = world.root / "write-link"
    target = world.tmp / "outside" if target_kind == "outside" else world.root / "protected"
    if target_kind == "private":
        target = (
            world.root / ".secrets"
        )  # owned dummy fixture, never the repository's private directory
    target.mkdir(exist_ok=True)
    if not make_link(link, target):
        pytest.skip("host cannot create directory links")
    if target_kind == "deny-alias":
        world.write_config(
            lambda cfg: cfg["runtime"].update(
                extra_write_paths=["protected"], extra_deny_paths=["write-link"]
            )
        )
    else:
        world.write_config(put(RUNTIME + ["extra_write_paths"], ["write-link"]))
    with pytest.raises(SystemExit):
        world.start()
    assert not world.calls() and not cw.REGISTRY.exists()


def test_stored_write_grants_are_rechecked_after_a_path_becomes_a_link(world: World) -> None:
    cfg = cb.load_codex_config(world.config)
    cfg["runtime"]["extra_write_paths"] = ["granted"]
    cx, _, _ = cb.resolve_worker(
        cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ
    )
    outside = world.tmp / "outside"
    outside.mkdir()
    if not make_link(world.root / "granted", outside):
        pytest.skip("host cannot create directory links")
    with pytest.raises(SystemExit):
        cb.filesystem_table(cx)


def test_extra_write_paths_cannot_grant_the_parent_of_the_auth_deny(world: World) -> None:
    world.write_config(
        lambda cfg: cfg["runtime"].update(
            codex_home=str(world.root / "codex-home"), extra_write_paths=["codex-home"]
        )
    )
    with pytest.raises(SystemExit):
        world.start()
    assert not world.calls()


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(suffix=st.text(alphabet="abcdef012345", min_size=1, max_size=12))
def test_write_grants_never_admit_a_denied_descendant(world: World, suffix: str) -> None:
    cx = worker_dict(world)["codex"]
    with pytest.raises(SystemExit):
        cb.write_paths(world.root, cx["deny"], [f".secrets/{suffix}"], relative=True)
    accepted = cb.write_paths(world.root, cx["deny"], [f"allowed/{suffix}"], relative=True)
    assert Path(accepted[0]).is_relative_to(world.root)


def test_write_validation_never_resolves_a_private_deny_leaf(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = world.root / "owned-dummy" / "AUTH.JSON"
    resolve = Path.resolve

    def no_private_leaf(path: Path, *args: Any, **kwargs: Any) -> Path:
        assert path != private, "private deny leaf must not be resolved"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", no_private_leaf)
    assert cb.write_paths(world.root, [str(private)], ["allowed"], relative=True)


@pytest.mark.parametrize("which", ["root", "cwd", "auth", "extra", "parent"])
def test_linked_deny_metadata_refuses_the_exact_vault_counterexample(
    world: World, which: str
) -> None:
    vault = world.root / "vault"
    vault.mkdir()
    if which in {"root", "cwd"}:
        leaf = (world.root if which == "root" else world.cwd) / ".secrets"
        (leaf / "README.md").unlink()  # owned dummy, never project secrets
        leaf.rmdir()
    else:
        leaf = world.root / {"auth": "auth.json", "extra": "denied", "parent": "alias"}[which]
    if not make_link(leaf, vault):
        pytest.skip("host cannot create owned directory links")
    deny = leaf / "private" if which == "parent" else leaf
    if which in {"root", "cwd"}:
        world.write_config(put(RUNTIME + ["extra_write_paths"], ["vault"]))
        with pytest.raises(SystemExit, match="linked path"):
            world.start()
        assert not world.calls() and not cw.REGISTRY.exists()
    else:
        with pytest.raises(SystemExit, match="linked path"):
            cb.write_paths(world.root, [str(deny)], ["vault"], relative=True)
    with pytest.raises(SystemExit, match="linked path"):
        cb.write_paths(world.root, [str(deny)], [str(vault)], relative=False)


def test_deny_metadata_failure_refuses_without_reading_contents(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf = world.root / "denied"
    original = Path.lstat

    def metadata(path: Path) -> os.stat_result:
        if path == leaf:
            raise PermissionError("owned metadata denial")
        return original(path)

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(SystemExit, match="cannot inspect path metadata"):
        cb.write_paths(world.root, [str(leaf)], ["allowed"], relative=True)


def test_a_file_symlink_deny_is_refused_by_metadata(world: World) -> None:
    target = world.root / "owned-auth-target"
    target.write_text("dummy", encoding="utf-8")
    alias = world.root / "auth.json"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("host cannot create owned file symlinks")
    with pytest.raises(SystemExit, match="linked path"):
        cb.write_paths(world.root, [str(alias)], [target.name], relative=True)


@pytest.mark.parametrize("which", ["root", "cwd", "auth", "extra", "parent"])
@pytest.mark.parametrize("failure", ["link", "metadata"])
def test_empty_grants_still_validate_every_deny_at_load_and_emission(
    world: World, monkeypatch: pytest.MonkeyPatch, which: str, failure: str
) -> None:
    leaf = {
        "root": world.root / ".secrets",
        "cwd": world.cwd / ".secrets",
        "auth": world.home / "auth.json",
        "extra": world.root / "denied",
        "parent": world.root / "deny-parent",
    }[which]
    extra = leaf / "private" if which == "parent" else leaf
    leaf.parent.mkdir(parents=True, exist_ok=True)
    world.write_config(put(RUNTIME + ["extra_deny_paths"], [str(extra)]))
    cfg = cb.load_codex_config(world.config)
    cx, _, _ = cb.resolve_worker(
        cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ
    )
    assert cx["write"] == []
    if failure == "link":
        if which in {"root", "cwd"}:
            (leaf / "README.md").unlink()  # owned dummy only
            leaf.rmdir()
        leaf.parent.mkdir(parents=True, exist_ok=True)
        vault = world.root / "vault"
        vault.mkdir()
        if not make_link(leaf, vault):
            pytest.skip("host cannot create owned directory links")
        message = "linked path"
    else:
        original = Path.lstat

        def denied(path: Path) -> os.stat_result:
            if path == leaf:
                raise PermissionError("owned metadata refusal")
            return original(path)

        monkeypatch.setattr(Path, "lstat", denied)
        message = "cannot inspect path metadata"
    with pytest.raises(SystemExit, match=message):
        cb.resolve_worker(cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ)
    with pytest.raises(SystemExit, match=message):
        cb.filesystem_table(cx)
    assert not world.calls() and not cw.REGISTRY.exists()


@pytest.mark.parametrize(
    "grant",
    [
        ".venv",
        ".",
        ".venv/Lib/site-packages",
        r".venv\Lib\site-packages",
        ".venv/./Lib",
    ],
)
def test_grants_covering_the_venv_refuse_at_load_and_emission(world: World, grant: str) -> None:
    cfg = cb.load_codex_config(world.config)
    cx, _, _ = cb.resolve_worker(
        cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ
    )
    cfg["runtime"]["extra_write_paths"] = [grant]
    message = "overlaps a denied path" if grant == "." else "overlaps venv_dir"
    with pytest.raises(SystemExit, match=message):
        cb.resolve_worker(cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ)
    cx["write"] = [str((world.root / Path(grant.replace("\\", "/"))).resolve())]
    with pytest.raises(SystemExit, match=message):
        cb.filesystem_table(cx)
    assert not world.calls() and not cw.REGISTRY.exists()


@pytest.mark.parametrize("cwd_kind", ["root", "dotdot", "inside-venv", "venv-alias"])
def test_writable_workspace_must_be_separate_from_the_venv(world: World, cwd_kind: str) -> None:
    cfg = cb.load_codex_config(world.config)
    cwd = {
        "root": world.root,
        "dotdot": world.cwd / "..",
        "inside-venv": world.venv_dir / "Lib",
        "venv-alias": world.cwd,
    }[cwd_kind]
    if cwd_kind == "venv-alias":
        alias = world.root / "venv-alias"
        if not make_link(alias, world.cwd):
            pytest.skip("host cannot create owned directory links")
        cfg["runtime"]["venv_dir"] = "venv-alias"
    with pytest.raises(SystemExit, match="use a separate worktree"):
        cb.resolve_worker(cfg, "codex-dev", world.root, cwd, "w1", world.config, os.environ)
    # Stored rows cannot move into a writable root on send/startup either.
    worker = worker_dict(world)
    worker["cwd"] = str(world.root)
    with pytest.raises(SystemExit, match="overlaps venv_dir"):
        cb.build_command(worker, False, world.tmp / "result.md")
    assert not world.calls() and not cw.REGISTRY.exists()


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(parts=st.lists(st.sampled_from(["Lib", "site-packages", "pkg"]), max_size=4))
def test_no_descendant_of_the_venv_is_a_writable_area(world: World, parts: list[str]) -> None:
    with pytest.raises(SystemExit, match="overlaps venv_dir"):
        cb.refuse_venv_writes(world.venv_dir, [world.venv_dir.joinpath(*parts)], world.root)
    cb.refuse_venv_writes(world.venv_dir, [world.cwd / "outputs"], world.root)


def test_the_usage_command_settings_are_honoured(world: World) -> None:
    def change(cfg: dict[str, Any]) -> None:
        cfg["usage"].update(antigravity_command="C:/tools/agy.cmd", command_timeout_seconds=7)

    world.write_config(change)
    settings_ = cb.load_usage_settings(world.config)
    # A shim is allowed for this command only.
    assert settings_["antigravity_command"] == "C:/tools/agy.cmd"
    assert settings_["command_timeout_seconds"] == 7
    assert cb.load_codex_config(world.config)["runtime"]["check_timeout_seconds"] == 20


def test_a_model_override_cannot_be_read_as_a_flag(world: World) -> None:
    cfg = cb.load_codex_config(world.config)
    with pytest.raises(SystemExit, match="cannot start with"):
        cb.resolve_worker(
            cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ,
            overrides={"model": "--oss"},
        )  # fmt: skip


def test_committed_profiles_are_refused_until_the_placeholders_are_replaced() -> None:
    with pytest.raises(SystemExit, match="placeholder"):
        cb.load_codex_config()
    raw = json.loads(cb.CONFIG.read_text(encoding="utf-8"))
    assert set(raw) - {"_comment"} == set(cb.TOP_KEYS) and set(raw["runtime"]) == set(
        cb.RUNTIME_KEYS
    )
    assert all(set(role) == set(cb.ROLE_KEYS) for role in raw["roles"].values())
    assert cb.load_usage_settings() == {
        "stale_minutes": 60,
        "session_file_limit": 8,
        "cache_warn_ratio": 0.5,
        "antigravity_command": None,
        "command_timeout_seconds": 60,
    }
    assert isinstance(raw["runtime"]["codex_command"], str)
    swapped = copy.deepcopy(raw)
    swapped["runtime"]["codex_command"] = sys.executable
    for role in swapped["roles"].values():
        role["model"] = "some-model"
    assert cb.validate_runtime(swapped["runtime"])["windows_sandbox"] == "elevated"
    assert all(cb.validate_role(r, e)["effort"] for r, e in swapped["roles"].items())


def test_preflight_names_missing_files(world: World) -> None:
    cx = worker_dict(world)["codex"]
    cb.preflight(cx, world.root)
    with pytest.raises(SystemExit, match="codex executable not found"):
        cb.preflight({**cx, "command": [str(world.tmp / "no-such-codex")]}, world.root)
    (world.venv_dir / "Scripts" / "python.exe").unlink()
    (world.venv_dir / "bin" / "python").unlink()
    with pytest.raises(cb.ReaderUnavailableError):
        cb.preflight(cx, world.root)


# ---- environment (items 3, 4) ---------------------------------------------------------------------------------


def test_child_env_is_an_allowlist_and_exports_the_configured_home(world: World) -> None:
    assert world.start() == 0
    env = world.execs()[0]["env"]
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "SOME_SECRET",
    ):
        assert env[name] is None
    names = world.execs()[0]["env_names"]
    assert not [
        n
        for n in names
        if any(w in n.upper() for w in ("KEY", "TOKEN", "SECRET", "ANTHROPIC", "OPENAI"))
    ]
    assert env["FAKE_ALLOWED"] == "allowed-value"  # runtime.env_allow
    assert env["CODEX_HOME"] == str(world.home)  # a home outside the project root is accepted
    scratch = Path(worker_dict(world)["codex"]["scratch"])
    assert scratch.parent == world.cwd / ".tmp" / "codex-run" and scratch.name.startswith("w1-")
    assert env["TEMP"] == env["TMP"] == str(scratch / "tmp")
    assert env["PATH"].split(os.pathsep)[0] == str(cb.venv_layout(world.venv_dir)[0])
    assert env["PYTHONPATH"] == (world.cwd / "src").as_posix()


def test_without_a_configured_home_codex_home_is_not_exported(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write_config(put(RUNTIME + ["codex_home"], None))
    fake_home = world.tmp / "user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert world.start() == 0
    call = world.execs()[0]
    assert call["env"]["CODEX_HOME"] is None
    table = next(a for a in call["argv"] if a.startswith("permissions.project-worker.filesystem="))
    assert cb.toml_string(str(fake_home / ".codex" / "auth.json")) in table


def test_an_ambient_codex_home_is_kept_and_its_auth_file_is_denied(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write_config(put(RUNTIME + ["codex_home"], None))
    ambient = world.tmp / "ambient-home"
    monkeypatch.setenv("CODEX_HOME", str(ambient))
    assert world.start() == 0
    call = world.execs()[0]
    assert call["env"]["CODEX_HOME"] == str(ambient)
    assert cb.toml_string(str(ambient / "auth.json")) in " ".join(call["argv"])


# ---- startup check, fail closed (item 2) ----------------------------------------------------------------------


def test_startup_check_probes_through_codex_sandbox_with_the_job_permissions(world: World) -> None:
    assert world.start() == 0
    first = world.sandboxes()
    assert len(first) == 3  # control read, the root README, the cwd README
    argv = first[0]["argv"]
    assert argv[:3] == ["sandbox", "-C", str(world.cwd)]
    assert (
        'default_permissions="project-worker"' in argv
        and argv[argv.index("-P") + 1] == "project-worker"
    )
    assert not {"-s", "--sandbox"} & set(argv)
    read = [read_target(c) for c in first]
    assert read[1:] == [world.root / ".secrets" / "README.md", world.cwd / ".secrets" / "README.md"]
    assert read[0].name == "control.txt"
    # The reader is the configured project venv running the host-written script (isolated, no site import).
    command = argv[argv.index("-P") + 2 :]
    assert command[0] == str(cb.reader_interpreter(world.entry()["codex"], world.root))
    assert command[1:3] == ["-I", "-S"]
    assert Path(command[3]).parent.name == "check" and Path(command[3]).name == "reader.py"
    assert command[-1] == "echo"  # only the control read echoes content
    assert "echo" not in first[1]["argv"] and "echo" not in first[2]["argv"]
    # Every send checks again, before the registry marks the worker busy.
    world.plan(turns=[{"thread_id": THREAD_A, "usage": USAGE_1}, {"usage": USAGE_2}])
    assert world.send() == 0
    assert len(world.sandboxes()) == 6


def read_target(call: dict[str, Any]) -> Path:
    argv = call["argv"]
    command = argv[argv.index("-P") + 2 :]
    return Path(command[4] if command[1:3] == ["-I", "-S"] else command[-1])


# Both platforms run the typed reader under the configured project venv.


@pytest.mark.parametrize(
    ("sandbox", "match"),
    [
        ({"secrets": "readable"}, "FAILED"),
        ({"secrets": "readable-rc1"}, "FAILED"),
        # rc 1 is not an exception-class code; 3 is a failure of the reader itself; 74 is another OSError.
        ({"secrets": "silent-fail"}, "inconclusive"),
        ({"secrets": "bad-code"}, "inconclusive"),
        ({"secrets": "other-oserror"}, "inconclusive"),
        ({"secrets": "crash"}, "inconclusive"),
        ({"secrets": "timeout"}, "could not run"),
        ({"control": "fail"}, "control read failed"),
    ],
)
def test_startup_check_fails_closed(world: World, sandbox: dict[str, str], match: str) -> None:
    world.write_config(put(RUNTIME + ["check_timeout_seconds"], 2))
    world.plan(sandbox=sandbox)
    with pytest.raises(SystemExit, match=match):
        world.start()
    assert not cw.REGISTRY.exists() and world.execs() == []  # nothing registered, no job process


@pytest.mark.parametrize("mode", ["silent-fail", "other-oserror", "bad-code", "crash"])
def test_posix_venv_refuses_untyped_failures_after_a_good_control(
    world: World, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(cb, "READER_PLATFORM", "linux")
    world.plan(sandbox={"secrets": mode})
    with pytest.raises(SystemExit):
        world.start()
    assert not cw.REGISTRY.exists() and world.execs() == []
    assert (
        len(world.sandboxes()) == 2
    )  # good control, then unrelated error (including OSError rc 74)
    command = world.sandboxes()[0]["argv"]
    assert command[command.index("-P") + 2] == str(world.root / ".venv" / "bin" / "python")


@pytest.mark.parametrize("secrets", ["denied", "denied-missing"])
def test_startup_check_accepts_a_denial_by_exit_code_through_the_interpreter(
    world: World, secrets: str
) -> None:
    # 77 is permission denied, 66 is hidden by the deny while the file is still on disk.
    world.plan(sandbox={"secrets": secrets})
    assert world.start() == 0


@pytest.mark.parametrize("secrets", ["denied", "denied-missing"])
def test_posix_venv_accepts_only_a_typed_denial(
    world: World, monkeypatch: pytest.MonkeyPatch, secrets: str
) -> None:
    monkeypatch.setattr(cb, "READER_PLATFORM", "linux")
    world.plan(sandbox={"secrets": secrets})
    assert world.start() == 0
    argv = world.sandboxes()[0]["argv"]
    assert argv[argv.index("-P") + 2 :][1:3] == ["-I", "-S"]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_missing_typed_reader_is_refused_before_processes_or_registry_changes(
    world: World, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(cb, "READER_PLATFORM", platform)
    cb.venv_layout(world.venv_dir)[1].unlink()
    with pytest.raises(cb.ReaderUnavailableError):
        world.start()
    assert not cw.REGISTRY.exists() and not world.calls()


@pytest.mark.parametrize(
    ("platform", "scripts", "exe"), [("win32", "Scripts", "python.exe"), ("linux", "bin", "python")]
)
def test_venv_override_reaches_reader_and_path(
    world: World, monkeypatch: pytest.MonkeyPatch, platform: str, scripts: str, exe: str
) -> None:
    monkeypatch.setattr(cb, "READER_PLATFORM", platform)
    alternate = world.root / ".tmp" / "alternate-venv"
    (alternate / scripts).mkdir(parents=True)
    (alternate / scripts / exe).write_text("", encoding="utf-8")
    world.write_config(put(RUNTIME + ["venv_dir"], ".tmp/alternate-venv"))
    worker = worker_dict(world)
    assert worker["codex"]["venv_dir"] == str(alternate)
    assert cb.reader_interpreter(worker["codex"], world.root) == alternate / scripts / exe
    env = cb.child_env(worker, os.environ, world.root)
    assert env["PATH"].split(os.pathsep)[0] == str(alternate / scripts)
    assert env["PYTHONPATH"] == (world.cwd / "src").as_posix()


@pytest.mark.parametrize(
    "value",
    [None, "", "<venv>", "../outside", "/outside", r"C:\outside", "env:stream", "env. ", "missing"],
)
def test_required_venv_dir_refuses_missing_placeholder_or_outside_values(
    world: World, value: Any
) -> None:
    raw = world.config_dict()
    if value == "missing":
        del raw["runtime"]["venv_dir"]
    else:
        raw["runtime"]["venv_dir"] = value
    with pytest.raises(SystemExit):
        cb.validate_runtime(raw["runtime"])


@pytest.mark.parametrize("which", ["configured", "canonical"])
def test_venv_link_cannot_escape_the_project(world: World, which: str) -> None:
    cfg = cb.load_codex_config(world.config)
    outside = world.tmp / "outside-venv"
    outside.mkdir()
    alias = world.root / "linked-env"
    if which == "canonical":
        world.venv_dir.rename(world.root / "old-env")
        alias = world.venv_dir
        cfg["runtime"]["venv_dir"] = "old-env"
    else:
        cfg["runtime"]["venv_dir"] = alias.name
    if not make_link(alias, outside):
        pytest.skip("host cannot create owned directory links")
    with pytest.raises(SystemExit, match="venv_dir must be strictly inside"):
        cb.resolve_worker(cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ)
    assert not world.calls()


@pytest.mark.parametrize("area", ["grant", "cwd"])
def test_venv_protection_cannot_be_bypassed_by_a_link(world: World, area: str) -> None:
    cfg = cb.load_codex_config(world.config)
    cx = worker_dict(world)["codex"]
    alias = world.root / "write-alias"
    target = world.venv_dir if area == "grant" else world.root
    if not make_link(alias, target):
        pytest.skip("host cannot create owned directory links")
    if area == "grant":
        cfg["runtime"]["extra_write_paths"] = [alias.name]
        cwd = world.cwd
        message = "linked path"
        cx["write"] = [str(alias)]
        with pytest.raises(SystemExit, match=message):
            cb.filesystem_table(cx)
    else:
        cwd, message = alias, "linked path"
        worker = worker_dict(world)
        worker["cwd"] = str(alias)
        with pytest.raises(SystemExit, match="overlaps venv_dir"):
            cb.build_command(worker, False, world.msg)
    with pytest.raises(SystemExit, match=message):
        cb.resolve_worker(cfg, "codex-dev", world.root, cwd, "w1", world.config, os.environ)
    assert not world.calls()


def test_venv_override_does_not_make_the_canonical_venv_writable(world: World) -> None:
    cfg = cb.load_codex_config(world.config)
    cfg["runtime"].update(venv_dir="alternative", extra_write_paths=[".venv/Lib"])
    with pytest.raises(SystemExit, match="overlaps venv_dir"):
        cb.resolve_worker(cfg, "codex-dev", world.root, world.cwd, "w1", world.config, os.environ)


def test_stored_row_with_web_tool_and_web_disabled_is_stale(world: World) -> None:
    cx = worker_dict(world)["codex"]
    cx["hook_allowed_tools"] = [*cx["hook_allowed_tools"], "webrun"]
    cx["web_search"] = "live"
    assert cb.stored_codex_problem(cx) is None
    cx["web_search"] = "disabled"
    assert cb.stored_codex_problem(cx) == "the web tool needs web_search other than disabled"


@pytest.mark.parametrize("keep_new_key", [False, True])
def test_stored_python_dir_row_requires_retire_and_restart(
    world: World, keep_new_key: bool
) -> None:
    worker = worker_dict(world)
    cx = worker["codex"]
    cx["python_dir"] = str(world.root / "obsolete")
    if not keep_new_key:
        del cx["venv_dir"]
    for operation in (
        lambda: cb.preflight(cx, world.root),
        lambda: cb.child_env(worker, os.environ, world.root),
        lambda: cb.build_command(worker, False, world.msg),
        lambda: cb.startup_check(worker, world.root, os.environ),
    ):
        with pytest.raises(cb.StaleWorkerError, match="retire and restart"):
            operation()
    assert not world.calls() and not (world.cwd / ".tmp").exists()


def test_venv_startup_failure_names_the_user_owned_read_only_grant(world: World) -> None:
    world.plan(sandbox={"control": "fail"})
    with pytest.raises(SystemExit, match="control read failed") as error:
        world.start()
    assert "read-only grant" in str(error.value) and "the user" in str(error.value)
    assert "never an agent" in str(error.value)
    assert not cw.REGISTRY.exists() and not world.execs()


@pytest.mark.skipif(not cw.NATIVE_WINDOWS, reason="native Windows signal-zero safety")
def test_simulated_posix_reader_never_reaches_os_kill_on_windows(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_platform = sys.platform
    real_os = cw.os

    class NoSignalOs:
        def __getattr__(self, name: str) -> Any:
            if name == "kill":
                raise AssertionError("platform-test path reached os.kill")
            return getattr(real_os, name)

    monkeypatch.setattr(cw, "os", NoSignalOs())
    monkeypatch.setattr(cb, "READER_PLATFORM", "linux")
    assert world.start() == 0
    assert sys.platform == original_platform


@pytest.mark.skipif(not cw.NATIVE_WINDOWS, reason="native Windows process checks")
def test_native_windows_process_checks_ignore_module_local_platform_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = cw.sibling("test_slot")

    def no_signal(*args: Any) -> None:
        raise AssertionError("Windows liveness reached os.kill")

    for module, check in ((cw, cw.pid_alive), (slot, slot._alive)):
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", kill=no_signal))
        assert check(-1) is False


def test_startup_check_writes_exactly_the_reader_source_and_a_control_file(world: World) -> None:
    worker = worker_dict(world)
    seen: list[tuple[str, str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        script = Path(argv[argv.index("-S") + 1])
        seen.append((script.read_text(encoding="utf-8"), Path(argv[argv.index("-S") + 2]).name))
        if argv[-1] == "echo":
            return subprocess.CompletedProcess(argv, 0, cb.CONTROL_TOKEN, "")
        return subprocess.CompletedProcess(argv, cb.READER_DENIED, "", "")

    cb.startup_check(worker, world.root, dict(os.environ), runner=runner)
    assert {source for source, _ in seen} == {cb.READER_SOURCE}
    assert [name for _, name in seen] == ["control.txt", "README.md", "README.md"]


def test_startup_check_needs_the_host_to_still_see_the_file(world: World) -> None:
    """A denial code for a file that vanished meanwhile proves nothing."""
    worker = worker_dict(world)
    readme = world.root / ".secrets" / "README.md"

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "echo":
            return subprocess.CompletedProcess(argv, 0, cb.CONTROL_TOKEN, "")
        readme.unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, cb.READER_DENIED, "", "")

    with pytest.raises(SystemExit, match="inconclusive"):
        cb.startup_check(worker, world.root, dict(os.environ), runner=runner)


def test_startup_check_never_matches_message_text(world: World) -> None:
    """The verdict follows exit code and host evidence only: any wording on stderr leaves it unchanged."""
    worker = worker_dict(world)

    def make(code: int, stderr: str) -> Any:
        def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            if argv[-1] == "echo":
                return subprocess.CompletedProcess(argv, 0, cb.CONTROL_TOKEN, "")
            return subprocess.CompletedProcess(argv, code, "", stderr)

        return runner

    wordings = (
        "",
        "Access is denied.",
        "file not found",
        "Zugriff verweigert",
        "permission denied",
    )
    for text in wordings:
        cb.startup_check(worker, world.root, dict(os.environ), runner=make(cb.READER_DENIED, text))
        with pytest.raises(SystemExit, match="inconclusive"):
            cb.startup_check(worker, world.root, dict(os.environ), runner=make(2, text))


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PermissionError, 77),
        (FileNotFoundError, 66),
        (IsADirectoryError, 74),
        (OSError, 74),
    ],
)
def test_the_reader_script_exits_with_the_exception_class_code(
    monkeypatch: pytest.MonkeyPatch, exc: type[OSError], code: int
) -> None:
    def failing_open(*_a: Any, **_k: Any) -> Any:
        raise exc("boom")

    monkeypatch.setattr("builtins.open", failing_open)
    monkeypatch.setattr(sys, "argv", ["reader.py", "somewhere"])
    # The source the host writes, run in-process.
    program = compile(cb.READER_SOURCE, "reader", "exec")
    with pytest.raises(SystemExit) as stopped:
        exec(program, {})
    assert stopped.value.code == code
    assert (cb.READER_DENIED, cb.READER_MISSING, cb.READER_OTHER) == (77, 66, 74)


def test_the_reader_script_runs_for_real_through_isolated_python(tmp_path: Path) -> None:
    script = tmp_path / "reader.py"
    script.write_text(cb.READER_SOURCE, encoding="utf-8")
    target = tmp_path / "file.txt"
    target.write_text("content", encoding="utf-8")
    base = [sys.executable, "-I", "-S", str(script)]
    options: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
    quiet = subprocess.run([*base, str(target)], **options)
    echoed = subprocess.run([*base, str(target), "echo"], **options)
    missing = subprocess.run([*base, str(tmp_path / "none.txt")], **options)
    assert (quiet.returncode, quiet.stdout) == (0, "")
    assert (echoed.returncode, echoed.stdout) == (0, "content")
    assert (missing.returncode, missing.stdout) == (cb.READER_MISSING, "")


def test_startup_check_refuses_without_a_tracked_readme(world: World) -> None:
    (world.root / ".secrets" / "README.md").unlink()
    (world.cwd / ".secrets" / "README.md").unlink()
    with pytest.raises(SystemExit, match=r"no tracked \.secrets/README\.md"):
        world.start()
    assert world.calls() == []


def test_startup_check_refuses_when_the_codex_command_cannot_start(world: World) -> None:
    world.write_config(put(RUNTIME + ["codex_command"], str(world.tmp / "missing" / "codex.exe")))
    with pytest.raises(SystemExit, match="codex executable not found"):
        world.start()
    worker = worker_dict(world, command=[str(world.tmp / "missing" / "codex.exe")])
    with pytest.raises(SystemExit, match="could not run"):
        cb.startup_check(worker, world.root, dict(os.environ))


def test_startup_check_refuses_a_profile_that_lacks_a_deny(world: World) -> None:
    worker = worker_dict(world)
    worker["codex"]["deny"] = [d for d in worker["codex"]["deny"] if not d.endswith("auth.json")]
    with pytest.raises(SystemExit, match="does not deny"):
        cb.startup_check(worker, world.root, dict(os.environ))
    assert world.calls() == []


def test_the_deny_list_covers_the_main_checkout_of_a_worktree(tmp_path: Path) -> None:
    main_root = tmp_path / "main"
    worktree = main_root / ".tmp" / "worktrees" / "wt"
    assert cb.protected_roots(worktree, worktree / "sub") == [worktree, worktree / "sub", main_root]
    assert cb.protected_roots(main_root, main_root) == [main_root]


# ---- outcome from stream events (items 7, 15, 16) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "ok"),
    [
        ("ok", True),
        ("retry_error", True),
        ("fail", False),
        ("failed_rc0", False),
        ("no_completed", False),
        ("error_after", False),
    ],
)
def test_status_comes_from_events_not_from_the_exit_code(world: World, mode: str, ok: bool) -> None:
    world.plan(
        turns=[
            {
                "thread_id": THREAD_A,
                "usage": USAGE_1,
                "mode": mode,
                "message": "I stopped: the error is failed",
            }
        ]
    )
    assert world.start() == (0 if ok else 1)
    assert world.entry()["status"].startswith("idle" if ok else "error")
    result = world.ledger()[0]["result"]
    assert result == "ok" if ok else result.startswith("error")


def scratch_of(world: World, name: str = "w1") -> dict[str, Any]:
    cfg = cb.load_codex_config(world.config)
    cx, _model, _effort = cb.resolve_worker(
        cfg, "codex-dev", world.root, world.cwd, name, world.config, os.environ
    )
    return cx


def test_scratch_left_in_the_sandbox_is_cleaned_inside_the_workers_own_dir_only(
    world: World,
) -> None:
    outside = world.cwd / "tracked.txt"
    outside.write_text("keep", encoding="utf-8")
    world.plan(
        turns=[
            {"thread_id": THREAD_A, "usage": USAGE_1},
            {"thread_id": THREAD_B, "usage": USAGE_1, "mode": "leave_scratch"},
        ]
    )
    assert world.start() == 0
    first = Path(world.entry()["codex"]["scratch"])
    (first / "keep.txt").write_text("keep", encoding="utf-8")  # another worker's scratch content
    assert world.start("w2") == 0
    second = Path(world.entry("w2")["codex"]["scratch"])
    assert first != second and first.parent == second.parent
    # Only the marker is left: the file the fake wrote into TEMP is gone.
    assert [p.name for p in second.iterdir()] == [cb.SCRATCH_WORKER_MARKER]
    assert (first / "keep.txt").is_file() and outside.is_file()


def test_clean_scratch_refuses_a_directory_outside_the_checkout(tmp_path: Path) -> None:
    checkout, elsewhere = tmp_path / "wt", tmp_path / "elsewhere"
    checkout.mkdir()
    elsewhere.mkdir()
    (elsewhere / "x.txt").write_text("x", encoding="utf-8")
    outside = {
        "scratch": str(elsewhere),
        "scratch_root": str(elsewhere.parent),
        "scratch_owner": "x",
    }
    assert cw.clean_scratch(outside, str(checkout))
    assert (elsewhere / "x.txt").is_file()
    # A registry entry written before the ownership markers existed.
    legacy = {"scratch": str(elsewhere)}
    assert "predates" in cw.clean_scratch(legacy, str(checkout))[0]
    assert (elsewhere / "x.txt").is_file()
    itself = {"scratch": str(checkout), "scratch_root": str(checkout), "scratch_owner": "x"}
    assert cw.clean_scratch(itself, str(checkout))  # the checkout itself


def test_scratch_dir_naming_the_checkout_is_refused_and_deletes_nothing(world: World) -> None:
    """P1 of the live review: `scratch_dir="."` with a worker called `src` used to delete `<cwd>/src`."""
    tracked = world.cwd / "src" / "module.py"
    tracked.parent.mkdir()
    tracked.write_text("tracked", encoding="utf-8")
    for scratch_dir in (".", "sub/.."):
        world.write_config(put(RUNTIME + ["scratch_dir"], scratch_dir))
        with pytest.raises(SystemExit):
            world.start("src")
    assert tracked.is_file() and not cw.REGISTRY.exists() and world.calls() == []


def test_an_existing_unmarked_scratch_directory_is_never_used_or_deleted(world: World) -> None:
    existing = world.cwd / "src"
    existing.mkdir()
    (existing / "module.py").write_text("tracked", encoding="utf-8")
    world.write_config(put(RUNTIME + ["scratch_dir"], "src"))
    with pytest.raises(SystemExit, match="was not created by this launcher"):
        world.start("src")
    assert (existing / "module.py").is_file() and not cw.REGISTRY.exists() and world.calls() == []
    cx = scratch_of(world, "src")
    # A directory at the worker's own path that this launcher did not make.
    lookalike = existing / Path(cx["scratch"]).name
    lookalike.mkdir()
    (lookalike / "data.txt").write_text("keep", encoding="utf-8")
    assert cb.scratch_problem(cx, world.cwd)
    refused = cw.clean_scratch(cx, str(world.cwd))
    assert refused and (lookalike / "data.txt").is_file()  # nothing deleted
    assert cw.clean_scratch(cx, str(world.cwd), drop_directory=True)
    assert (lookalike / "data.txt").is_file() and (existing / "module.py").is_file()


def test_a_worker_directory_without_its_marker_is_refused_at_the_next_turn(world: World) -> None:
    assert world.start() == 0
    scratch = Path(world.entry()["codex"]["scratch"])
    (scratch / cb.SCRATCH_WORKER_MARKER).unlink()
    (scratch / "precious.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit, match="not this worker's scratch directory"):
        world.send()
    assert (scratch / "precious.txt").is_file()
    assert cw.clean_scratch(world.entry()["codex"], str(world.cwd))  # refused, nothing removed
    assert (scratch / "precious.txt").is_file()


def test_a_marker_naming_another_worker_is_refused(world: World) -> None:
    assert world.start() == 0
    scratch = Path(world.entry()["codex"]["scratch"])
    marker = scratch / cb.SCRATCH_WORKER_MARKER
    cx = world.entry()["codex"]
    marker.write_text(json.dumps({"owner": "someone-else"}), encoding="utf-8")
    assert "not this worker's scratch directory" in str(cb.scratch_problem(cx, world.cwd))
    marker.write_text("not json", encoding="utf-8")
    assert cb.scratch_problem(cx, world.cwd)


def test_sanitized_worker_names_never_share_a_scratch_directory(world: World) -> None:
    names = ["a b", "a_b", "a/b", "A_B", "a" * 60 + "x", "a" * 60 + "y"]
    paths = {scratch_of(world, name)["scratch"] for name in names}
    assert len(paths) == len(names)
    root = Path(scratch_of(world)["scratch_root"])
    assert all(Path(p).parent == root for p in paths)  # one level below the root, no escape


def test_clean_scratch_removes_contents_keeps_the_marker_and_never_follows_links(
    world: World, tmp_path: Path
) -> None:
    cx = scratch_of(world)
    scratch = cb.prepare_scratch(cx, world.cwd)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "outside.txt").write_text("keep", encoding="utf-8")
    (scratch / "tmp").mkdir()
    (scratch / "tmp" / "inner.txt").write_text("x", encoding="utf-8")
    (scratch / "top.txt").write_text("x", encoding="utf-8")
    # Where the platform refuses links, the plain removal is still proven.
    make_link(scratch / "link", victim)
    assert cw.clean_scratch(cx, str(world.cwd)) == []
    assert [p.name for p in scratch.iterdir()] == [cb.SCRATCH_WORKER_MARKER]
    assert (victim / "outside.txt").is_file()
    assert cw.clean_scratch(cx, str(world.cwd), drop_directory=True) == []
    assert not scratch.exists() and Path(cx["scratch_root"]).is_dir()


def test_a_linked_scratch_root_is_refused(world: World, tmp_path: Path) -> None:
    real = tmp_path / "real-root"
    real.mkdir()
    (real / "keep.txt").write_text("keep", encoding="utf-8")
    cx = scratch_of(world)
    root = Path(cx["scratch_root"])
    root.parent.mkdir(parents=True, exist_ok=True)
    if not make_link(root, real):
        pytest.skip("this platform refuses directory links")
    (real / cb.SCRATCH_ROOT_MARKER).write_text(json.dumps({"owner": "root"}), encoding="utf-8")
    assert "link" in str(cb.scratch_problem(cx, world.cwd))
    with pytest.raises(SystemExit, match="link"):
        cb.prepare_scratch(cx, world.cwd)
    untouched = sorted(["keep.txt", cb.SCRATCH_ROOT_MARKER])  # nothing was written through the link
    assert sorted(p.name for p in real.iterdir()) == untouched


def test_timeout_stops_the_whole_process_tree(world: World) -> None:
    world.plan(turns=[{"thread_id": THREAD_A, "usage": USAGE_1, "mode": "hang"}])
    real = cw.run_turn
    seen: list[dict[str, Any]] = []
    world.monkeypatch.setattr(
        cw, "run_turn", lambda name, worker, *a, **k: seen.append(worker) or 0
    )
    assert world.start() == 0
    world.monkeypatch.setattr(cw, "run_turn", real)
    started = cw.load()["w1"]
    assert started["status"] == "busy"
    rc = real(
        "w1", {**seen[0], "_usage": cb.load_usage_settings(world.config)}, "brief", False, 0.08, 1
    )
    assert rc == 1
    child = int((world.record / "child.pid").read_text(encoding="utf-8"))
    assert not cw.pid_alive(child)
    assert world.entry()["status"].startswith("error")


STORED_CODEX_KEYS = (
    "role",
    "config_path",
    "command",
    "home",
    "export_home",
    "permission_profile",
    "windows_sandbox",
    "deny",
    "write",
    "project_root",
    "web_search",
    "scratch",
    "scratch_root",
    "scratch_owner",
    "venv_dir",
    "check_timeout_seconds",
    "env_allow",
    "timeout_minutes",
)


@pytest.mark.parametrize("key", STORED_CODEX_KEYS)
@pytest.mark.parametrize("mode", ["missing", "wrong-type"])
def test_stale_codex_fields_refuse_before_side_effects_and_retire_still_works(
    world: World, key: str, mode: str
) -> None:
    worker = {
        **worker_dict(world),
        "session_id": "sid",
        "status": "idle",
        "_usage": {"cache_warn_ratio": 0.5},
    }
    if mode == "missing":
        del worker["codex"][key]
    else:
        worker["codex"][key] = "yes" if key == "export_home" else True
    cw.save({"w1": worker})
    before = cw.REGISTRY.read_bytes()
    for operation in (
        world.send,
        lambda: world.run("status", "--json"),
        lambda: cw.record_ledger(worker, "w1", 1, 1.0, "ok", {}, None, None, "turn"),
        lambda: cw.run_codex_turn("w1", worker, BRIEF, True, 1, 1),
    ):
        with pytest.raises(cb.StaleWorkerError) as refused:
            operation()
        assert "retire" in str(refused.value) and "restart" in str(refused.value)
        assert cw.REGISTRY.read_bytes() == before
        assert not world.calls() and not cw.LEDGER.exists() and not cw.JOBS.exists()
        assert not (world.cwd / ".tmp" / "codex-run").exists()
    assert world.run("retire", "--name", "w1", "--purge-transcript") == 0
    after = world.entry()
    assert after["status"] == "retired"
    assert after["codex"] == worker["codex"]  # no silent repair, including model/effort/deny
    assert (after["model"], after["effort"]) == (worker["model"], worker["effort"])


@pytest.mark.parametrize("cx", [None, [], "old"])
def test_a_non_object_resolved_block_is_a_typed_refusal_and_can_be_retired(
    world: World, cx: Any
) -> None:
    worker = {**worker_dict(world), "codex": cx, "session_id": "sid", "status": "idle"}
    cw.save({"w1": worker})
    with pytest.raises(cb.StaleWorkerError):
        world.send()
    assert world.run("retire", "--name", "w1") == 0


def test_turn_completion_rechecks_the_stored_row_before_registry_or_ledger_writes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = cw.launch_turn
    corrupted: list[bytes] = []

    def corrupt_after_launch(*args: Any, **kwargs: Any) -> int:
        code: int = launch(*args, **kwargs)
        rows = cw.load()
        del rows["w1"]["codex"]["scratch_root"]
        cw.save(rows)
        corrupted.append(cw.REGISTRY.read_bytes())
        return code

    monkeypatch.setattr(cw, "launch_turn", corrupt_after_launch)
    with pytest.raises(cb.StaleWorkerError):
        world.start()
    assert cw.REGISTRY.read_bytes() == corrupted[0]
    assert not cw.LEDGER.exists()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(missing=st.sets(st.sampled_from(STORED_CODEX_KEYS), min_size=1))
def test_incomplete_resolved_rows_are_never_accepted(world: World, missing: set[str]) -> None:
    cx = worker_dict(world)["codex"]
    for key in missing:
        del cx[key]
    original = copy.deepcopy(cx)
    with pytest.raises(cb.StaleWorkerError):
        cb.require_stored_codex(cx)
    assert cx == original


def test_backend_sibling_entrypoints_refuse_stale_rows_before_scratch_creation(
    world: World,
) -> None:
    worker = worker_dict(world)
    del worker["codex"]["scratch_root"]
    cx = worker["codex"]
    for operation in (
        lambda: cb.build_command(worker, True, world.tmp / "result.md"),
        lambda: cb.permission_overrides(cx),
        lambda: cb.filesystem_table(cx),
        lambda: cb.prepare_scratch(cx, world.cwd),
        lambda: cb.child_env(worker, os.environ, world.root),
        lambda: cb.preflight(cx, world.root),
        lambda: cb.startup_check(worker, world.root, os.environ),
        lambda: cb.reader_interpreter(cx, world.root),
    ):
        with pytest.raises(cb.StaleWorkerError):
            operation()
    assert not world.calls() and not (world.cwd / ".tmp" / "codex-run").exists()


@pytest.mark.parametrize("marker", [cb.SCRATCH_ROOT_MARKER, cb.SCRATCH_WORKER_MARKER])
def test_failed_scratch_marker_write_does_not_block_the_next_start(
    world: World, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    world.write_config(put(RUNTIME + ["scratch_dir"], ".tmp/retry-owned-scratch"))
    write = Path.write_text

    def failing_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if path.name == marker:
            write(path, "partial", *args, **kwargs)
            raise OSError("test-only marker write failure")
        return write(path, data, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", failing_write)
        with pytest.raises(OSError):
            world.start()
    assert not cw.REGISTRY.exists() and not world.calls()
    assert world.start() == 0


def test_failed_startup_check_does_not_block_the_next_start(world: World) -> None:
    world.plan(sandbox={"secrets": "other-oserror"})
    with pytest.raises(SystemExit):
        world.start()
    assert not cw.REGISTRY.exists() and not world.execs()
    world.plan()
    assert world.start() == 0


def test_failed_marker_rollback_never_deletes_a_replacement_directory(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = worker_dict(world)
    root = Path(worker["codex"]["scratch_root"])
    write = Path.write_text

    def replacing_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if path.name == cb.SCRATCH_ROOT_MARKER:
            path.parent.rename(path.parent.with_name("moved-owned-root"))
            path.parent.mkdir()
            write(path, "foreign", *args, **kwargs)
            raise OSError("test-only directory replacement")
        return write(path, data, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", replacing_write)
        with pytest.raises(OSError) as failed:
            cb.prepare_scratch(worker["codex"], world.cwd)
    assert (root / cb.SCRATCH_ROOT_MARKER).read_text(encoding="utf-8") == "foreign"
    assert any("identity changed" in note for note in failed.value.__notes__)


# ---- usage, ledger, cache warning (items 18, 19, 20) ----------------------------------------------------------


def test_the_ledger_subtracts_cumulative_usage_and_warns_from_the_second_turn(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.plan(
        turns=[
            {"thread_id": THREAD_A, "usage": USAGE_1},
            {"usage": USAGE_2},
            {"usage": USAGE_3},
        ]
    )
    assert world.start() == 0
    assert capsys.readouterr().err == ""  # turn 1 has cached 0 of 1000 and still no warning
    assert world.send() == 0
    assert (
        "WARNING: w1 turn 2: cached_input_tokens 200 is 10% of input_tokens 2000"
        in capsys.readouterr().err
    )
    assert world.send() == 0
    assert "WARNING" not in capsys.readouterr().err
    one, two, three = world.ledger()
    assert (one["input_tokens"], one["cached_input_tokens"], one["output_tokens"]) == (1000, 0, 50)
    assert (two["input_tokens"], two["cached_input_tokens"], two["output_tokens"]) == (
        2000,
        200,
        70,
    )
    assert (three["input_tokens"], three["cached_input_tokens"]) == (3000, 2700)
    assert [r["usage_basis"] for r in (one, two, three)] == ["first-turn", "delta", "delta"]
    assert one["cache_write_input_tokens"] is None  # absent in the stream: unknown, never 0
    assert two["warnings"] and not one["warnings"] and not three["warnings"]
    for row in (one, two):
        assert (row["worker"], row["backend"], row["model"], row["effort"], row["task"]) == (
            "w1", "codex", "test-model", "high", "T-TEST",
        )  # fmt: skip
        assert row["seconds"] >= 0 and row["result"] == "ok"
        assert (
            row["usage_before"]["status"] == "unknown" and row["usage_after"]["status"] == "unknown"
        )


def test_usage_delta_rules() -> None:
    first = {
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": None,
        "output_tokens": 1,
    }
    second = {
        "input_tokens": 30,
        "cached_input_tokens": 5,
        "cache_write_input_tokens": 4,
        "output_tokens": 3,
    }
    assert cb.usage_delta(second, first) == (
        {
            "input_tokens": 20,
            "cached_input_tokens": 5,
            "cache_write_input_tokens": 4,
            "output_tokens": 2,
        },
        "delta",
    )
    assert cb.usage_delta(first, second) == (first, "reported-not-cumulative")
    assert cb.usage_delta(None, first) == (None, "none")
    assert cb.usage_fields(
        {"input_tokens": True, "output_tokens": -1, "cached_input_tokens": "3"}
    ) == dict.fromkeys(cb.USAGE_FIELDS)


def test_attribute_usage_labels_a_row_that_follows_unreported_turns() -> None:
    first = cb.usage_fields(USAGE_1)
    second = cb.usage_fields(USAGE_2)
    assert cb.attribute_usage(first, None, 0) == (first, "first-turn", 0)
    delta, basis, folded = cb.attribute_usage(second, first, 0)
    assert (basis, folded, delta["input_tokens"]) == ("delta", 0, 2000)
    delta, basis, folded = cb.attribute_usage(second, first, 2)
    # The row holds the failed turns too.
    assert (basis, folded, delta["input_tokens"]) == ("aggregate", 2, 2000)
    assert cb.attribute_usage(first, None, 1)[1:] == ("aggregate", 1)  # the very first turn failed
    assert cb.attribute_usage(None, first, 3) == (None, "none", 0)
    assert cb.attribute_usage(first, second, 1)[1:] == ("reported-not-cumulative", 0)


def test_a_failed_turn_without_usage_is_not_charged_to_the_next_turn(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.plan(
        turns=[
            {"thread_id": THREAD_A, "usage": USAGE_1},
            {"mode": "fail"},  # turn.failed: no usage object at all
            {"usage": USAGE_2},
            {"usage": USAGE_3},
        ]
    )
    assert world.start() == 0
    assert world.send() == 1
    assert world.entry()["codex_usage_gap"] == 1 and world.entry()["status"].startswith("error")
    capsys.readouterr()
    assert world.send() == 0
    assert "WARNING" not in capsys.readouterr().err  # 10% cached is meaningless for a mixed row
    assert "codex_usage_gap" not in world.entry()
    assert world.send() == 0
    one, failed, mixed, clean = world.ledger()
    assert failed["usage_basis"] == "none" and failed["input_tokens"] is None
    assert mixed["usage_basis"] == "aggregate" and mixed["unreported_turns_included"] == 1
    # Failed turn and this turn together, never labelled a single turn.
    assert mixed["input_tokens"] == 2000
    assert mixed["warnings"] == []
    assert (one["unreported_turns_included"], clean["unreported_turns_included"]) == (0, 0)
    assert clean["usage_basis"] == "delta" and clean["input_tokens"] == 3000


def test_an_aggregate_row_never_raises_the_cache_warning(world: World) -> None:
    usage = {"cache_warn_ratio": 0.5}
    worker = {**worker_dict(world), "_usage": usage}
    low = {"input_tokens": 100, "cached_input_tokens": 1}
    cw.record_ledger(worker, "c1", 3, 1.0, "ok", low, None, None, "aggregate", 2)
    cw.record_ledger(worker, "c1", 3, 1.0, "ok", low, None, None, "delta")
    aggregate, single = world.ledger()
    assert aggregate["warnings"] == [] and aggregate["unreported_turns_included"] == 2
    assert single["warnings"] and single["unreported_turns_included"] == 0


def test_cache_warning_uses_codex_field_names_and_skips_the_first_turn() -> None:
    assert us.cache_warning(1, 1000, 0, 0.5) is None
    assert us.cache_warning(2, 1000, 499, 0.5) is not None
    assert us.cache_warning(2, 1000, 500, 0.5) is None
    assert us.cache_warning(3, 0, 0, 0.5) is None and us.cache_warning(3, None, None, 0.5) is None
    assert us.cache_warning(3, 100, None, 0.5) is None


def test_claude_token_totals_count_the_whole_prompt_as_input() -> None:
    totals = cw.claude_token_totals(
        {
            "m": {
                "inputTokens": 5,
                "cacheReadInputTokens": 90,
                "cacheCreationInputTokens": 5,
                "outputTokens": 7,
            }
        }
    )
    assert totals == {
        "input_tokens": 100, "cached_input_tokens": 90, "cache_write_input_tokens": 5, "output_tokens": 7
    }  # fmt: skip
    assert set(cw.claude_token_totals({}).values()) == {None}


def test_record_ledger_writes_claude_rows_and_never_hides_a_ledger_failure(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = {
        "backend": "claude",
        "model": "m",
        "effort": "low",
        "label": "T",
        "_usage": {"cache_warn_ratio": 0.5},
    }
    cw.record_ledger(
        worker,
        "c1",
        2,
        1.26,
        "ok",
        {"input_tokens": 100, "cached_input_tokens": 10},
        None,
        None,
        "turn",
    )
    row = world.ledger()[0]
    assert row["backend"] == "claude" and row["seconds"] == 1.3 and row["warnings"]
    cw.record_ledger(
        {**worker, "_usage": None}, "c1", 2, 1.0, "ok", None, None, None, "turn"
    )  # no settings: skipped
    assert len(world.ledger()) == 1
    monkeypatch.setattr(us, "append_ledger", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    cw.record_ledger(worker, "c1", 1, 1.0, "ok", {}, None, None, "turn")
    assert "ledger row not written (disk full)" in capsys.readouterr().err


# ---- usage snapshot -------------------------------------------------------------------------------------------


def session_row(
    minutes_primary: int | None,
    minutes_secondary: int | None,
    stamp: str,
    reset: int = 4_000_000_000,
) -> str:
    def window(minutes: int | None, used: float) -> dict[str, Any] | None:
        return (
            None
            if minutes is None
            else {"used_percent": used, "window_minutes": minutes, "resets_at": reset}
        )

    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": 9, "cached_input_tokens": 3, "output_tokens": 2}
        },
        "rate_limits": {
            "plan_type": "plus",
            "primary": window(minutes_primary, 11.0),
            "secondary": window(minutes_secondary, 22.0),
        },
    }
    return json.dumps({"timestamp": stamp, "type": "event_msg", "payload": payload})


def put_session(home: Path, lines: list[str], thread: str = THREAD_A) -> Path:
    folder = home / "sessions" / "2026" / "11" / "07"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-07T10-00-00-{thread}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


NOW = us.parse_time("2026-11-07T10:05:00Z")
assert NOW is not None


def test_windows_are_named_by_window_minutes_and_absent_ones_are_not_invented(
    tmp_path: Path,
) -> None:
    put_session(tmp_path, ["{broken", session_row(10080, 300, "2026-11-07T10:00:00Z")])
    snap = us.codex_snapshot(tmp_path, NOW, 60, 8)
    assert snap["status"] == "ok" and snap["plan_type"] == "plus"
    assert {w["label"]: w["window_minutes"] for w in snap["windows"]} == {
        "weekly": 10080,
        "5h": 300,
    }
    assert {w["label"]: w["used_percent"] for w in snap["windows"]} == {"weekly": 11.0, "5h": 22.0}
    assert snap["total_token_usage"]["cached_input_tokens"] == 3
    put_session(tmp_path, [session_row(10080, None, "2026-11-07T10:00:00Z")])
    only = us.codex_snapshot(tmp_path, NOW, 60, 8)
    assert [w["label"] for w in only["windows"]] == ["weekly"]  # no 5h window: none invented
    assert (
        us.window_label(90) == "90m"
        and us.window_label(2880) == "2d"
        and us.window_label(300) == "5h"
    )


def test_absent_data_is_unknown_and_an_idle_provider_is_not_stale_by_row_age(
    tmp_path: Path,
) -> None:
    assert us.codex_snapshot(tmp_path, NOW, 60, 8)["status"] == "unknown"  # no sessions directory
    put_session(tmp_path, [json.dumps({"type": "session_meta", "payload": {}})])
    assert us.codex_snapshot(tmp_path, NOW, 60, 8)["reason"].startswith("no token_count")
    # A row hours old whose windows have not reset yet is still the current usage (a row moves only on a call).
    put_session(tmp_path, [session_row(300, None, "2026-11-07T10:00:00Z")])
    idle = us.codex_snapshot(tmp_path, NOW + 3 * 3600, 60, 8)
    assert idle["status"] == "ok" and idle["age_minutes"] > 180
    assert [(w["state"], w["used_percent"]) for w in idle["windows"]] == [("ok", 11.0)]


def test_a_window_past_its_reset_counts_as_zero_and_one_without_reset_needs_a_fresh_row(
    tmp_path: Path,
) -> None:
    put_session(tmp_path, [session_row(300, 10080, "2026-11-07T10:00:00Z", reset=int(NOW) - 10)])
    reset = us.codex_snapshot(tmp_path, NOW, 60, 8)
    assert [(w["state"], w["used_percent"]) for w in reset["windows"]] == [("reset", 0.0)] * 2
    assert us.ledger_usage(reset)["windows"][0]["used_percent"] == 0.0
    undated = {"primary": {"used_percent": 40, "window_minutes": 300}}
    assert us.windows_of(undated, NOW, fresh=True)[0]["state"] == "ok"
    stale = us.windows_of(undated, NOW, fresh=False)[0]
    assert (stale["state"], stale["used_percent"]) == ("unknown", None)


def test_the_newest_token_row_wins_across_files_not_the_newest_file(tmp_path: Path) -> None:
    """P2 of the live review: a later non-token append moved an old session's mtime ahead of the newest token row."""
    fresh = put_session(tmp_path, [session_row(300, None, "2026-11-07T10:00:00Z")], THREAD_A)
    expired = session_row(300, None, "2026-11-07T09:00:00Z", reset=int(NOW) - 10)
    old = put_session(tmp_path, [expired, json.dumps({"type": "event_msg"})], THREAD_B)
    stamp = NOW
    os.utime(fresh, (stamp - 300, stamp - 300))  # 10:00:00
    os.utime(old, (stamp - 10, stamp - 10))  # a later append: newer mtime, older token row
    snap = us.codex_snapshot(tmp_path, NOW, 60, 8)
    assert snap["observed_at"].startswith("2026-11-07T10:00:00")
    states = [(w["state"], w["used_percent"]) for w in snap["windows"]]
    assert states == [("ok", 11.0)]  # not the expired row's 0%
    os.utime(old, (stamp - 400, stamp - 400))  # the expired file is older too: same answer
    assert us.codex_snapshot(tmp_path, NOW, 60, 8)["observed_at"] == snap["observed_at"]


@pytest.mark.parametrize("newer_mtime", [90, 120])
def test_restored_or_coarse_mtime_cannot_hide_the_newest_active_row(
    tmp_path: Path, newer_mtime: int
) -> None:
    # Exact counterexample: B mtime=120/row=100 expired; A mtime=90/row=110 active 40%.
    expired = session_row(300, None, "1970-01-01T00:01:40Z", reset=115)
    newer = json.loads(session_row(300, None, "1970-01-01T00:01:50Z", reset=500))
    newer["payload"]["rate_limits"]["primary"]["used_percent"] = 40
    b = put_session(tmp_path, [expired], THREAD_B)
    a = put_session(tmp_path, [json.dumps(newer)], THREAD_A)
    os.utime(b, (120, 120))
    os.utime(a, (newer_mtime, newer_mtime))
    snap = us.codex_snapshot(tmp_path, 125, 60, 8)
    assert us.parse_time(snap["observed_at"]) == 110
    assert [(w["state"], w["used_percent"]) for w in snap["windows"]] == [("ok", 40)]


def test_session_file_limit_changes_selection_and_is_forwarded_by_all_consumers(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world.write_config(put(["usage", "session_file_limit"], 1))
    latest = put_session(world.home, [session_row(300, None, "2026-11-07T10:00:00Z")], THREAD_A)
    older = put_session(world.home, [session_row(300, None, "2026-11-07T09:00:00Z")], THREAD_B)
    os.utime(latest, (NOW - 800, NOW - 800))
    os.utime(older, (NOW, NOW))
    one = us.codex_snapshot(world.home, NOW, 60, 1)
    two = us.codex_snapshot(world.home, NOW, 60, 2)
    assert one["observed_at"] != two["observed_at"]
    limits: list[int] = []
    real_snapshot = us.codex_snapshot

    def snapshot(home: Path, stamp: float, stale: int, limit: int) -> dict[str, Any]:
        limits.append(limit)
        return real_snapshot(home, stamp, stale, limit)

    monkeypatch.setattr(us, "codex_snapshot", snapshot)
    assert world.start() == 0
    assert limits == [1, 1]  # before and after ledger snapshots
    monkeypatch.setattr(sys, "argv", ["usage_snapshot.py", "--config", str(world.config)])
    capsys.readouterr()
    assert us.main() == 0
    assert json.loads(capsys.readouterr().out)["codex"]["observed_at"] == one["observed_at"]
    assert limits == [1, 1, 1]


@pytest.mark.parametrize("limit", [None, True, 0, -1, "8"])
def test_snapshot_refuses_an_invalid_candidate_limit(tmp_path: Path, limit: Any) -> None:
    with pytest.raises(SystemExit):
        us.codex_snapshot(tmp_path, NOW, 60, limit)


def test_a_file_without_a_token_row_is_skipped_and_an_undated_row_uses_the_file_time(
    tmp_path: Path,
) -> None:
    none = put_session(tmp_path, [json.dumps({"type": "session_meta"})], THREAD_B)
    payload = json.loads(session_row(300, None, "x"))["payload"]
    undated = put_session(tmp_path, [json.dumps({"payload": payload})], THREAD_A)
    os.utime(none, (NOW - 5, NOW - 5))
    os.utime(undated, (NOW - 120, NOW - 120))
    snap = us.codex_snapshot(tmp_path, NOW, 60, 8)
    assert snap["status"] == "ok" and snap["observed_from"].startswith("file mtime")
    assert round(snap["age_minutes"]) == 2


def test_the_antigravity_usage_command_runs_from_an_argument_list_and_passes_json_through() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    shape = {"five_hour": {"used_percent": 3}}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(shape), "")

    snap = us.antigravity_snapshot("agy", 15, runner)
    assert snap == {"status": "ok", "source": "usage command", "data": shape}
    ((argv, kwargs),) = calls
    # An argument list, never a shell string, and no model prompt.
    assert argv == ["agy", "-p", "/usage", "--output-format", "json"]
    assert kwargs["timeout"] == 15 and kwargs["stdin"] is subprocess.DEVNULL
    assert not kwargs.get("shell")


def test_the_antigravity_usage_command_failures_are_unknown_never_zero() -> None:
    def returning(code: int, out: str) -> Any:
        return lambda argv, **_k: subprocess.CompletedProcess(argv, code, out, "")

    def raising(exc: BaseException) -> Any:
        def runner(*_a: Any, **_k: Any) -> Any:
            raise exc

        return runner

    # Not configured: no command, or no timeout to bound it.
    assert us.antigravity_snapshot(None, 15, returning(0, "{}"))["status"] == "unknown"
    assert us.antigravity_snapshot("agy", None, returning(0, "{}"))["status"] == "unknown"
    cases = [
        returning(1, "{}"),
        returning(0, "not json"),
        returning(0, "[1, 2]"),
        returning(0, ""),
        raising(FileNotFoundError("agy")),
        raising(subprocess.TimeoutExpired("agy", 15)),
    ]
    for runner in cases:
        assert us.antigravity_snapshot("agy", 15, runner)["status"] == "unknown"
    # `collect` forwards the configured command and keeps one failing source from stopping the others.
    home = Path("no-codex-home")
    result = us.collect(home, None, 60, 8, NOW, "agy", 15, returning(0, '{"ok": 1}'))
    assert result["antigravity"]["data"] == {"ok": 1} and result["codex"]["status"] == "unknown"
    broken = us.collect(home, None, 60, 8, NOW, "agy", 15, raising(SystemExit("gone")))
    assert broken["antigravity"]["status"] == "unknown"
    assert "SystemExit" in broken["antigravity"]["reason"]


def test_a_failing_provider_reports_unknown_and_does_not_stop_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_cli(*_args: Any) -> dict[str, Any]:
        raise SystemExit("no executable found")  # not an Exception subclass

    monkeypatch.setattr(us, "codex_snapshot", missing_cli)
    result = us.collect(tmp_path, None, 60, 8, NOW)
    assert result["codex"]["status"] == "unknown" and "SystemExit" in result["codex"]["reason"]
    assert result["claude"]["status"] == "unknown" and result["antigravity"]["status"] == "unknown"
    assert result["collected_at"]


def test_collect_reports_claude_from_the_pm_file_and_antigravity_unknown(tmp_path: Path) -> None:
    claude = tmp_path / "claude-usage.json"
    assert us.collect(tmp_path, None, 60, 8, NOW)["claude"]["status"] == "unknown"
    assert us.collect(tmp_path, claude, 60, 8, NOW)["claude"]["reason"].startswith("file missing")
    claude.write_text("[1]", encoding="utf-8")
    assert (
        us.collect(tmp_path, claude, 60, 8, NOW)["claude"]["reason"] == "file is not a JSON object"
    )
    claude.write_text(json.dumps({"five_hour": 12}), encoding="utf-8")
    fresh = us.collect(tmp_path, claude, 60, 8, claude.stat().st_mtime + 60)
    assert fresh["claude"]["status"] == "ok" and fresh["claude"]["data"] == {"five_hour": 12}
    assert (
        us.collect(tmp_path, claude, 60, 8, claude.stat().st_mtime + 7200)["claude"]["reason"]
        == "stale"
    )
    result = us.collect(tmp_path, claude, 60, 8, NOW)
    assert result["antigravity"]["status"] == "unknown" and result["collected_at"].endswith(
        "+00:00"
    )
    assert set(result) == {"collected_at", "claude", "codex", "antigravity"}


def test_snapshot_cli_needs_no_filled_in_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    put_session(tmp_path / "h", [session_row(300, None, "2026-11-07T10:00:00Z")])
    monkeypatch.setattr("sys.argv", ["usage_snapshot.py", "--codex-home", str(tmp_path / "h")])
    assert us.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["codex"]["status"] in {"ok", "unknown"} and out["antigravity"]["status"] == "unknown"


# ---- purge (item 5) -------------------------------------------------------------------------------------------


def test_purge_removes_only_the_exact_codex_session_file(world: World, tmp_path: Path) -> None:
    mine = put_session(world.home, ["x"], THREAD_A)
    sibling_file = put_session(world.home, ["x"], THREAD_B)
    other_day = world.home / "sessions" / "2026" / "11" / "06"
    other_day.mkdir(parents=True)
    different = other_day / f"rollout-2026-11-06T09-00-00-{THREAD_B}.jsonl"
    different.write_text("x", encoding="utf-8")
    lookalike = mine.with_name(f"notes-{THREAD_A}.jsonl")
    lookalike.write_text("x", encoding="utf-8")
    archived = world.home / "archived_sessions"
    archived.mkdir()
    (archived / mine.name).write_text("x", encoding="utf-8")
    other_project_home = tmp_path / "other-home"
    foreign = put_session(other_project_home, ["x"], THREAD_A)
    claude_log = tmp_path / ".claude" / "projects" / "slug" / f"{THREAD_A}.jsonl"
    claude_log.parent.mkdir(parents=True)
    claude_log.write_text("x", encoding="utf-8")
    assert world.start() == 0
    assert world.run("retire", "--name", "w1", "--purge-transcript") == 0
    assert not mine.exists()
    for kept in (
        sibling_file,
        different,
        lookalike,
        archived / mine.name,
        foreign,
        claude_log,
        world.home / "sessions",
    ):
        assert kept.exists(), kept
    assert world.entry()["status"] == "retired"


def test_purge_session_refuses_a_non_canonical_id_and_never_follows_links(
    world: World, tmp_path: Path
) -> None:
    for bad in ("", "../x", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper(), THREAD_A + "0", "*"):
        with pytest.raises(SystemExit, match="canonical UUID"):
            cb.purge_session(world.home, bad)
    real = tmp_path / "real-sessions"
    victim = put_session(real, ["x"], THREAD_A)
    (world.home / "sessions").mkdir(parents=True)
    if make_link(world.home / "sessions" / "linked", real / "sessions"):
        assert cb.purge_session(world.home, THREAD_A) == []
        assert victim.exists()
    failed: list[str] = []
    assert cb.purge_session(world.home / "missing", THREAD_A, failed) == [] and failed == []


def test_retire_without_a_captured_thread_deletes_nothing(world: World) -> None:
    keep = put_session(world.home, ["x"], THREAD_A)
    world.plan(turns=[{"mode": "no_thread", "usage": USAGE_1}])
    assert world.start() == 1
    assert world.run("retire", "--name", "w1", "--purge-transcript") == 0
    assert keep.exists()


def test_retire_cleans_the_scratch_directory_of_the_worker(world: World) -> None:
    assert world.start() == 0
    scratch = Path(world.entry()["codex"]["scratch"])
    assert (scratch / cb.SCRATCH_WORKER_MARKER).is_file()  # created and marked by the launcher
    (scratch / "left.txt").write_text("x", encoding="utf-8")
    assert world.run("retire", "--name", "w1") == 0
    assert not scratch.exists() and Path(world.entry()["codex"]["scratch_root"]).is_dir()


def test_status_shows_the_backend(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert world.start() == 0
    capsys.readouterr()
    assert world.run("status", "--json") == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["backend"] == "codex" and row["model"] == "test-model"
    assert world.run("status") == 0
    assert "w1: codex:test-model/high idle" in capsys.readouterr().out


def test_the_readme_records_the_live_facts_and_the_new_rules() -> None:
    text = (TOOLS / "README.md").read_text(encoding="utf-8")
    for needed in (
        "status.showUntrackedFiles=no",  # live fact 1: git status hits the protected .secrets
        "standard-library",  # the user owns the base interpreter read grant
        "never reads error text",
        cb.SERVICE_TIER_PIN,
        "features.fast_mode=false",
        cb.SCRATCH_WORKER_MARKER,
        "usage_basis: aggregate",
        "usage.antigravity_command",
        "absolute or relative to the project root",
    ):
        assert needed in text, needed


def test_the_fake_codex_double_is_labelled_and_never_real() -> None:
    assert "TEST DOUBLE" in FAKE.read_text(encoding="utf-8").splitlines()[0]
    done = subprocess.run(
        [sys.executable, "-B", str(FAKE)], capture_output=True, text=True, check=False
    )
    assert done.returncode != 0  # without a plan it cannot do anything
