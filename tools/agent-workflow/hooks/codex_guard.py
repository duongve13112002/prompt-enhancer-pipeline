"""Bounded host hook adapter. Any admission error emits a typed deny with exit zero.

Only this trusted file selects imports. Codex worker content arrives on stdin.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO

PATH_METACHARACTERS = frozenset('<>"|?*$%`')


class AdmissionRefusalError(ValueError):
    """An expected role refusal, distinguishable from a broken adapter."""


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def strict_json(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def invalid(value: str) -> Any:
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(
        data.decode("utf-8-sig", errors="strict"), object_pairs_hook=pairs, parse_constant=invalid
    )


def text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)
    ):
        raise ValueError("expected nonempty plain text")
    return value


def target(raw: str, cwd: Path, root: Path) -> Path:
    text(raw)
    # Safety-bound: wildcard or expansion spellings of a path are never needed here.
    if any(c in PATH_METACHARACTERS for c in raw):
        raise ValueError("metacharacter in path")
    # A short 8.3 alias names a folder without its long name, so word checks miss it.
    if re.search(r"~\d", raw):
        raise ValueError("short-name path component")
    # Check both spellings: normalizing only one can authorize a different file
    # on filesystems that preserve decomposed Unicode names.
    for spelling in dict.fromkeys((raw, unicodedata.normalize("NFKC", raw))):
        parts = spelling.replace("\\", "/").split("/")
        if any(p.casefold() in (".secrets", ".git", ".agents", ".claude") for p in parts):
            raise ValueError("protected target")
        if any(PureWindowsPath(p).is_reserved() for p in parts):
            raise ValueError("reserved device target")
        # Absolute native paths are checked component by component below.
        if (
            any(p in ("..", "") or p.rstrip(" .") != p or ":" in p for p in parts)
            and not Path(spelling).is_absolute()
        ):
            raise ValueError("ambiguous patch/image path")
        path = Path(spelling)
        if any(p == ".." or ":" in p or p.rstrip(" .") != p for p in path.parts[1:]):
            raise ValueError("ambiguous path component")
        if "\\" in spelling and sys.platform != "win32":
            raise ValueError("foreign path separator")
        if spelling.startswith(("\\\\", "//")):
            raise ValueError("device/UNC path refused")
        resolved = (cwd / path).resolve()
        if resolved == cwd.resolve() or not resolved.is_relative_to(cwd.resolve()):
            raise ValueError("target outside worker cwd")
        for candidate in (cwd / path, resolved):
            folded = [unicodedata.normalize("NFKC", p).casefold() for p in candidate.parts]
            if any(p in (".secrets", ".git", ".agents", ".claude") for p in folded):
                raise ValueError("protected target")
            # Worktree copies are protected too, even though they cannot affect
            # the root policy. Deny the whole guard directory/config by structure.
            relative = (
                candidate.relative_to(cwd.resolve())
                if candidate.is_relative_to(cwd.resolve())
                else candidate
            )
            components = [p.casefold() for p in relative.parts]
            if components[:2] == ["tools", "agent-workflow"]:
                raise ValueError("hook/launcher files are protected")
            if candidate == root or candidate.is_relative_to(root / "tools" / "agent-workflow"):
                raise ValueError("host hook path is protected")
        if resolved.exists() and resolved.is_file() and resolved.stat().st_nlink > 1:
            raise ValueError("hard-linked write/read target refused")
    return (cwd / raw).resolve()


def patch_targets(patch: str, limit_bytes: int, limit_targets: int) -> list[str]:
    if len(patch.encode("utf-8")) > limit_bytes:
        raise ValueError("patch exceeds configured byte limit")
    if "\r" in patch.replace("\r\n", "") or "\0" in patch:
        raise ValueError("invalid patch line ending")
    lines = patch.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("invalid patch envelope or zero targets")
    targets: list[str] = []
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        kind, sep, path = header.partition(": ")
        if not sep or kind not in ("*** Add File", "*** Update File", "*** Delete File"):
            raise ValueError("unrecognized patch header")
        targets.append(text(path))
        index += 1
        if kind == "*** Update File" and lines[index].startswith("*** Move to: "):
            targets.append(text(lines[index][len("*** Move to: ") :]))
            index += 1
        if len(targets) > limit_targets:
            raise ValueError("patch exceeds configured target limit")
        body_start = index
        in_chunk = False
        changes = 0
        while index < len(lines) - 1 and not lines[index].startswith("*** "):
            line = lines[index]
            if kind == "*** Add File":
                if not line.startswith("+"):
                    raise ValueError("invalid add-file body")
            elif kind == "*** Update File":
                if line == "@@" or line.startswith("@@ "):
                    in_chunk = True
                elif not in_chunk or not line or line[0] not in " +-":
                    raise ValueError("invalid update chunk")
                elif line[0] in "+-":
                    changes += 1
            else:
                raise ValueError("delete-file must have no body")
            index += 1
        if (
            kind == "*** Update File"
            and index < len(lines) - 1
            and lines[index] == "*** End of File"
        ):
            index += 1
        if kind != "*** Delete File" and index == body_start:
            raise ValueError("empty patch operation")
        if kind == "*** Update File" and (not in_chunk or not changes):
            raise ValueError("update has no changes")
    if not targets or len(targets) != len(set(targets)):
        raise ValueError("zero or duplicate patch targets")
    return targets


def shape(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ValueError("unexpected argument/input shape")
    return value


def identity(event: dict[str, Any], policy: dict[str, Any], kind: str) -> None:
    common = {"session_id", "turn_id", "transcript_path", "cwd", "hook_event_name", "model"}
    extra = {
        "PreToolUse": {"permission_mode", "tool_name", "tool_input", "tool_use_id"},
        "PostToolUse": {
            "permission_mode",
            "tool_name",
            "tool_input",
            "tool_use_id",
            "tool_response",
        },
        "Stop": {"permission_mode", "stop_hook_active", "last_assistant_message"},
        "PostModelRequest": {"status", "error", "token_usage"},
    }[kind]
    shape(event, common | extra, {"agent_id", "agent_type"} if kind != "Stop" else set())
    for key in ("session_id", "turn_id", "cwd", "model"):
        text(event[key])
    if event["hook_event_name"] != kind or event.get("agent_id") or event.get("agent_type"):
        raise ValueError("unexpected event/subagent")
    cwd = Path(event["cwd"]).resolve()
    if not Path(event["cwd"]).is_absolute():
        raise ValueError("event cwd must be absolute")
    # Shell tools may expose a per-call subdirectory; other tools remain bound to
    # the admitted workspace. Never select host configuration using event.cwd.
    if not cwd.is_relative_to(Path(policy["cwd"]).resolve()):
        raise ValueError("event cwd escaped worker")
    if policy["thread_id"] is not None and event["session_id"] != policy["thread_id"]:
        raise ValueError("unexpected session")
    if event["transcript_path"] is not None:
        text(event["transcript_path"])  # validate only, never open a transcript
    if "permission_mode" in event:
        text(event["permission_mode"])
    if "tool_use_id" in event:
        text(event["tool_use_id"])
    if (
        "last_assistant_message" in event
        and event["last_assistant_message"] is not None
        and not isinstance(event["last_assistant_message"], str)
    ):
        raise ValueError("invalid assistant message metadata")


def admit(event: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    try:
        return check_tool(event, policy)
    except AdmissionRefusalError:
        raise
    except ValueError as exc:
        # The worker must see why it was refused, or it retries blind.
        raise AdmissionRefusalError(str(exc)) from exc


def check_tool(event: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    name, args = event["tool_name"], event["tool_input"]
    if type(name) is not str or name not in policy["allowed_tools"]:
        raise AdmissionRefusalError("tool name not on the exact role allowlist")
    import codex_hooks as host

    if name == host.WEB_TOOL:
        # No local side effect; what a role may reach is its web_search setting.
        return {}
    cwd, root, opts = Path(event["cwd"]), Path(policy["root"]), policy["options"]
    if name == "Bash":
        shape(args, {"command"})
        command = args["command"]
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("invalid shell command")
        import guard_config as gc
        import process_guard as pg
        import tool_guard as tg

        cfg = gc.load(root / opts["guard_config"])
        if len(command) > cfg["max_command_chars"] or any(
            unicodedata.category(c) == "Cf" for c in command
        ):
            raise ValueError("oversized or invisible shell command")
        native_tool = "PowerShell" if sys.platform == "win32" else "Bash"
        # Use the pure classifier under our exception boundary; its public wrapper
        # intentionally swallows errors for Claude's narrower trigger semantics.
        pg.CLASSIFY_CACHE.cache_clear()
        reason = pg._classify(command, "ps" if native_tool == "PowerShell" else "bash", 0)
        if reason:
            raise ValueError(reason)
        ctx = tg.Context(
            {**event, "tool_name": native_tool},
            cfg,
            argparse.Namespace(git_mode="worker", worktree=None),
            root,
        )
        warnings: list[str] = []
        for section, check, _trigger in tg.checks(ctx):
            mode = cfg[section]["mode"]
            if mode != "off":
                found = check(ctx)  # exception always denies, regardless of trigger/mode
                if found and mode == "block":
                    raise ValueError("; ".join(found))
                warnings.extend(found)
        return (
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(warnings),
                }
            }
            if warnings
            else {}
        )
    if name == "apply_patch":
        shape(args, {"command"})
        if not isinstance(args["command"], str):
            raise ValueError("patch must be text")
        for path in patch_targets(
            args["command"], opts["max_patch_bytes"], opts["max_patch_targets"]
        ):
            target(path, cwd, root)
    elif name == "view_image":
        shape(args, {"path"}, {"detail"})
        if "detail" in args and args["detail"] not in ("high", "original"):
            raise ValueError("invalid image detail")
        target(args["path"], cwd, root)
    else:
        shape(
            args,
            {"prompt"},
            {"transparent_background", "referenced_image_paths", "num_last_images_to_include"},
        )
        text(args["prompt"])
        if "transparent_background" in args and type(args["transparent_background"]) is not bool:
            raise ValueError("invalid transparent_background")
        refs, count = args.get("referenced_image_paths"), args.get("num_last_images_to_include")
        if refs is not None:
            if not isinstance(refs, list) or not 1 <= len(refs) <= 5 or count is not None:
                raise ValueError("invalid image references")
            for path in refs:
                target(path, cwd, root)
        if count is not None and (type(count) is not int or not 1 <= count <= 5):
            raise ValueError("invalid image history count")
    return {}


def stop(event: dict[str, Any], policy: dict[str, Any], directory: Path) -> dict[str, Any]:
    if type(event["stop_hook_active"]) is not bool:
        raise ValueError("invalid stop_hook_active")
    if event["stop_hook_active"]:
        return {}
    opts, complete = policy["options"], False
    if policy["task_file"]:
        path = target(policy["task_file"], Path(policy["cwd"]), Path(policy["root"]))
        try:
            with path.open("rb") as stream:
                data = stream.read(opts["max_task_bytes"] + 1)
        except FileNotFoundError:
            data = b""
        if len(data) > opts["max_task_bytes"]:
            raise ValueError("task file too large")
        lines = data.decode("utf-8-sig").splitlines()
        if lines.count(opts["stop_section"]) == 1:
            section = lines[lines.index(opts["stop_section"]) + 1 :]
            end = next(
                (i for i, line in enumerate(section) if line.startswith("## ")), len(section)
            )
            complete = all(
                any(
                    line.startswith("- [x] " + label + ":") and line.split(":", 1)[1].strip()
                    for line in section[:end]
                )
                for label in opts["stop_labels"]
            )
    if complete:
        return {}
    return request_continuation(
        policy,
        directory,
        "Fill "
        + opts["stop_section"]
        + " with checked, nonempty labels: "
        + ", ".join(opts["stop_labels"]),
    )


def request_continuation(policy: dict[str, Any], directory: Path, reason: str) -> dict[str, Any]:
    # Launch-scoped atomic claim: even a forged/reset active flag or changed turn
    # id cannot cause a second continuation within this host-launched turn.
    marker = directory / (policy["launch_id"] + ".stop")
    try:
        with marker.open("xb") as stream:
            stream.write(b"continued")
    except FileExistsError:
        return {}
    return {"decision": "block", "reason": reason}


def observe(event: dict[str, Any], policy: dict[str, Any], directory: Path, kind: str) -> None:
    import codex_hooks as host

    receipt: dict[str, Any] | None = None
    if kind == "PostModelRequest":
        usage = event["token_usage"]
        if event["status"] == "completed" and isinstance(usage, dict):
            count = usage.get("input_tokens")
            if type(count) is not int or count < 0:
                raise ValueError("invalid observed input_tokens")
            receipt = {"type": "context", "input_tokens": count}
        else:
            receipt = {"type": "context", "input_tokens": None}
    elif event["tool_name"] == host.IMAGE_TOOL:
        if host.IMAGE_TOOL not in policy["allowed_tools"]:
            raise ValueError("image response outside role admission")
        call = text(event["tool_use_id"])
        path = host.image_path(Path(policy["home"]), event["session_id"], call)
        response = event["tool_response"]
        first_line = f"Generated images are saved to {path.parent} as {path} by default."
        if not isinstance(response, list) or not any(
            isinstance(item, dict)
            and item.get("type") == "input_text"
            and isinstance(item.get("text"), str)
            and item["text"].split("\n", 1)[0] == first_line
            for item in response
        ):
            raise ValueError("image output has no exact generated-path receipt")
        receipt = {"type": "image", "call_id": call, "path": str(path)}
    if receipt is not None:
        receipt.update(session_id=event["session_id"], turn_id=event["turn_id"])
        path = directory / (policy["launch_id"] + ".jsonl")
        host.no_links(path)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(exit_on_error=False)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument(
        "--event", required=True, choices=("PreToolUse", "Stop", "PostToolUse", "PostModelRequest")
    )
    return parser.parse_args(argv)


def load_policy(
    args: argparse.Namespace, *, check_digest: bool = True
) -> tuple[Path, dict[str, Any]]:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import codex_hooks as host

    path = Path(args.policy)
    host.no_links(path)
    policy = strict_json(path.read_bytes())
    root = Path(__file__).resolve().parents[3]
    if root != Path(policy["root"]).resolve():
        raise ValueError("policy is not from the canonical launcher root")
    opts = host.options(policy["options"])
    if path.parent.resolve() != (root / opts["policy_dir"]).resolve():
        raise ValueError("policy outside root-owned state directory")
    if check_digest and (
        policy["digest"] != args.digest or host.fingerprint(root, opts) != args.digest
    ):
        raise ValueError("host definitions changed after launch")
    host.names(policy["allowed_tools"], host.TOOLS, "hook_allowed_tools")
    launch_id = text(policy["launch_id"])
    if len(launch_id) != 32 or any(c not in "0123456789abcdef" for c in launch_id):
        raise ValueError("invalid host launch identity")
    return path, policy


def run(argv: list[str], stream: BinaryIO, context: dict[str, Any] | None = None) -> dict[str, Any]:
    # Worker input is read only after the root-owned policy is validated.
    args = parse_args(argv)
    path, policy = load_policy(args)
    opts = policy["options"]
    import codex_hooks as host

    data = stream.read(opts["max_input_bytes"] + 1)
    if len(data) > opts["max_input_bytes"]:
        raise ValueError("hook stdin exceeds configured byte limit")
    event = strict_json(data)
    if context is not None:
        context["event"] = event
    identity(event, policy, args.event)
    if args.event == "PreToolUse":
        output = admit(event, policy)
        if event["tool_name"] == host.IMAGE_TOOL:
            receipt = path.parent / (policy["launch_id"] + ".jsonl")
            host.no_links(receipt)
            with receipt.open("a", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {
                            "type": "image_expected",
                            "call_id": event["tool_use_id"],
                            "session_id": event["session_id"],
                            "turn_id": event["turn_id"],
                        }
                    )
                    + "\n"
                )
        return output
    if args.event == "Stop":
        return stop(event, policy, path.parent)
    observe(event, policy, path.parent, args.event)
    return {}


def failure_reply(
    argv: list[str], exc: BaseException, event: Any = None
) -> tuple[dict[str, Any], int]:
    reason = "host guard refused unexpected/invalid state: " + type(exc).__name__
    try:
        args = parse_args(argv)
    except BaseException:
        return deny(reason), 0
    if args.event == "PreToolUse":
        return deny(str(exc) if isinstance(exc, AdmissionRefusalError) else reason), 0
    try:
        # Definitions may have changed, causing the original failure. The policy
        # must still pass root/path/schema/launch checks; never read worker config.
        path, policy = load_policy(args, check_digest=False)
        import codex_hooks as host

        receipt = path.parent / (policy["launch_id"] + ".jsonl")
        host.no_links(receipt)
        with receipt.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps({"type": "hook_failure", "event": args.event, "reason": reason}) + "\n"
            )
        if args.event == "Stop":
            if isinstance(event, dict) and event.get("stop_hook_active") is True:
                return {}, 0
            return request_continuation(policy, path.parent, reason), 0
    except BaseException as reporting_error:
        # No safe marker means no continuation: valid Stop termination cannot
        # loop. Observer reporting failure is loud on stderr and nonzero too.
        sys.stderr.write(
            reason + "; failure reporting unavailable: " + type(reporting_error).__name__ + "\n"
        )
        if args.event == "Stop":
            return {"continue": False, "stopReason": reason}, 0
        if args.event == "PostToolUse":
            return {"decision": "block", "reason": reason}, 1
        return {"systemMessage": reason}, 1
    if args.event == "PostToolUse":
        return {"decision": "block", "reason": reason}, 0
    # PostModelRequest treats stdout as diagnostics, not control output.
    # A nonzero status plus stderr marks the native observer failed too.
    sys.stderr.write(reason + "\n")
    return {"systemMessage": reason}, 1


def main(argv: list[str] | None = None, stream: BinaryIO | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    code = 0
    context: dict[str, Any] = {}
    try:
        output = run(arguments, sys.stdin.buffer if stream is None else stream, context)
    except BaseException as exc:
        output, code = failure_reply(arguments, exc, context.get("event"))
    print(json.dumps(output, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
