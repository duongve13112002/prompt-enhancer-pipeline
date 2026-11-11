"""PM HOST ONLY: version, adapter and fresh-directory sandbox-denial probe.

No model/provider call. This does not prove Codex actually dispatched a hook;
the live exec/resume checklist in README.md is a separate PM requirement.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import codex_backend as backend

ROOT = Path(__file__).resolve().parents[2]


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def dacl_flag(folder: Path, timeout: int) -> str:
    if sys.platform != "win32":
        return "not_applicable (POSIX)"
    # Fixed PowerShell code; the exact owned path is a positional argument.
    result = run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "& { param($p) (Get-Acl -LiteralPath $p).Sddl }",
            str(folder),
        ],
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError(f"cannot inspect DACL of owned folder {folder}: rc={result.returncode}")
    sddl = result.stdout.strip()
    if "D:" not in sddl:
        raise ValueError(f"missing DACL in {folder}")
    return "D:" + sddl.split("D:", 1)[1].split("(", 1)[0]


def probe(config: Path, cwd: Path, role: str, report: Path) -> dict[str, Any]:
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    if not cwd.resolve().is_relative_to(ROOT):
        raise ValueError("probe cwd outside project")
    host = backend.hook_module()
    host.inside(ROOT, report)
    host.no_links(report)
    cfg = backend.load_codex_config(config)
    cx, model, effort = backend.resolve_worker(
        cfg, role, ROOT, cwd, "host-hook-probe", config, os.environ
    )
    worker = {"codex": cx, "cwd": str(cwd), "model": model, "effort": effort}
    env = backend.child_env(worker, os.environ, ROOT)
    host.check_version(cx, env)
    backend.preflight(cx, ROOT)
    # Uses only tracked README denial controls; no auth/session or other private file.
    backend.startup_check(worker, ROOT, env)
    folder = cwd / (".codex-hook-canary-" + uuid.uuid4().hex)
    folder.mkdir()
    canary = folder / "canary.txt"
    canary.write_text("owned non-secret canary", encoding="utf-8")
    reader = folder / "reader.py"
    reader.write_text(backend.READER_SOURCE, encoding="utf-8")
    output: dict[str, Any] = {}
    try:
        output["folder"] = str(folder)
        output["dacl_flag"] = dacl_flag(folder, cx["check_timeout_seconds"])
        print(f"fresh folder {folder}; DACL flag {output['dacl_flag']}", flush=True)

        def read_canary(
            settings: dict[str, Any], echo: bool = False
        ) -> subprocess.CompletedProcess[str]:
            command = ["sandbox"]
            for override in backend.permission_overrides(settings):
                command += ["-c", override]
            command += ["-P", settings["permission_profile"]]
            command += backend.reader_argv(
                backend.reader_interpreter(cx, ROOT), reader, canary, echo
            )
            backend.check_argv(command, "sandbox", cx["write"])
            return run(
                [*cx["command"], *command], cwd=cwd, env=env, timeout=cx["check_timeout_seconds"]
            )

        control = read_canary(cx, echo=True)
        output["canary_control"] = (
            control.returncode == 0 and control.stdout == "owned non-secret canary"
        )
        if not output["canary_control"]:
            raise ValueError(f"fresh-directory canary positive control FAILED: {canary}")
        denied = {**cx, "deny": [*cx["deny"], str(canary)]}
        result = read_canary(denied)
        output["canary_denied"] = result.returncode == 77 and result.stdout == ""
        if not output["canary_denied"]:
            raise ValueError(
                f"fresh-directory canary denial FAILED: {canary}; rc={result.returncode}"
            )
        if sys.platform == "win32" and "AI" not in output["dacl_flag"]:
            raise ValueError(
                f"fresh folder lacks auto-inheritance: {folder}; {output['dacl_flag']}"
            )
        policy = host.prepare(worker)
        body = json.loads(policy.read_text(encoding="utf-8"))
        try:
            event: dict[str, Any] = {
                "hook_event_name": "PreToolUse",
                "session_id": "host-probe",
                "turn_id": "1",
                "cwd": str(cwd),
                "model": model,
                "permission_mode": "default",
                "transcript_path": None,
                "tool_name": "spawn_agent",
                "tool_input": {},
                "tool_use_id": "denied-probe",
            }
            adapter_argv = [
                str(backend.reader_interpreter(cx, ROOT)),
                "-B",
                "-I",
                str(ROOT / "tools/agent-workflow/hooks/codex_guard.py"),
                "--policy",
                str(policy),
                "--digest",
                body["digest"],
                "--event",
                "PreToolUse",
            ]
            adapter = run(
                adapter_argv,
                input=json.dumps(event),
                cwd=cwd,
                env=env,
                timeout=cx["check_timeout_seconds"],
            )
            reply = json.loads(adapter.stdout)
            output["direct_adapter_denied"] = (
                adapter.returncode == 0
                and isinstance(reply, dict)
                and reply.get("hookSpecificOutput")
                == {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "tool name not on the exact role allowlist",
                }
            )
            if not output["direct_adapter_denied"]:
                raise ValueError("direct host adapter did not deny spawn_agent")
            if "Bash" in cx["hook_allowed_tools"]:
                allowed_name, allowed_input = (
                    "Bash",
                    {"command": "echo codex-hook-positive-control"},
                )
            elif "view_image" in cx["hook_allowed_tools"]:
                allowed_name, allowed_input = "view_image", {"path": str(canary)}
            elif "apply_patch" in cx["hook_allowed_tools"]:
                allowed_name = "apply_patch"
                allowed_input = {
                    "command": f"*** Begin Patch\n*** Add File: {folder / 'positive.txt'}\n+control\n*** End Patch\n"
                }
            else:
                raise ValueError("positive control needs an allowed non-image tool")
            event.update(
                tool_name=allowed_name, tool_input=allowed_input, tool_use_id="allowed-probe"
            )
            allowed = run(
                adapter_argv,
                input=json.dumps(event),
                cwd=cwd,
                env=env,
                timeout=cx["check_timeout_seconds"],
            )
            allow_reply = json.loads(allowed.stdout)
            specific = (
                allow_reply.get("hookSpecificOutput") if isinstance(allow_reply, dict) else None
            )
            output["direct_adapter_allowed"] = allowed.returncode == 0 and (
                allow_reply == {}
                or (
                    isinstance(specific, dict)
                    and set(allow_reply) == {"hookSpecificOutput"}
                    and set(specific) == {"hookEventName", "additionalContext"}
                    and specific["hookEventName"] == "PreToolUse"
                    and isinstance(specific["additionalContext"], str)
                )
            )
            if not output["direct_adapter_allowed"]:
                raise ValueError(
                    "direct host adapter rejected or failed the allowed-tool positive control"
                )
        finally:
            policy.unlink()
        output["tested_codex_version"] = cx["tested_codex_version"]
        output["expected_hook_tools"] = cx["hook_allowed_tools"]
        output["disabled_features"] = cx["disabled_features"]
        output["hook_table"] = cx["hook_table"]
        output["live_exec_resume"] = "not_run: PM live checklist required"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(output, indent=2), encoding="utf-8")
        return output
    finally:
        # Exact owned files only, no recursive traversal and no retry on refusal.
        try:
            canary.unlink()
            reader.unlink()
            folder.rmdir()
        except OSError as exc:
            raise ValueError(
                f"probe cleanup refused; inspect {canary} and {folder}: {exc}"
            ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = probe(args.config.resolve(), args.cwd.resolve(), args.role, args.report.resolve())
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"HOST PROBE FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("dacl_flag", "canary_denied", "expected_hook_tools", "live_exec_resume")
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
