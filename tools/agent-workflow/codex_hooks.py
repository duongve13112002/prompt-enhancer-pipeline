"""Host-owned Codex hook declarations, policy envelopes and receipts (rust-v0.167.0).

No imports or configuration are selected by worker stdin. See README.md, Codex hooks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import unicodedata
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Protocol names, not deployment defaults. Unknown names never broaden admission.
DISABLED_FEATURES = frozenset(
    (
        "apps",
        "computer_use",
        "multi_agent",
        "plugins",
        "remote_plugin",
        "skill_mcp_dependency_install",
        "memories",
    )
)
IMAGE_TOOL = "image_genimagegen"
# Measured on 0.167.0: `web__run` reaches PreToolUse under this flattened name.
WEB_TOOL = "webrun"
TOOLS = frozenset(("Bash", "apply_patch", "view_image", IMAGE_TOOL, WEB_TOOL))
EVENTS = {
    "PreToolUse": "pre_tool_use",
    "Stop": "stop",
    "PostModelRequest": "post_model_request",
    "PostToolUse": "post_tool_use",
}
OPTION_KEYS = (
    "policy_dir",
    "guard_config",
    "timeout_seconds",
    "max_input_bytes",
    "max_patch_bytes",
    "max_patch_targets",
    "max_task_bytes",
    "stop_section",
    "stop_labels",
    "image_output_dir",
)
TRUST_FILES = (
    "codex_hooks.py",
    "guard_config.json",
    "hooks/codex_guard.py",
    "hooks/tool_guard.py",
    "hooks/process_guard.py",
    "hooks/shell_targets.py",
    "hooks/guard_config.py",
    "hooks/tool_health.py",
)


def names(
    value: object, allowed: frozenset[str], field: str, required: str | None = None
) -> list[str]:
    if (
        not isinstance(value, list)
        or any(type(x) is not str or x not in allowed for x in value)
        or len(value) != len(set(value))
        or (required is not None and required not in value)
    ):
        raise ValueError(
            f"{field}: expected unique exact names; required={required}, allowed={sorted(allowed)}"
        )
    return list(value)


def relative(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(c) < 32 or c in '<>:"|?*' for c in value)
        or value.startswith(("/", "\\"))
    ):
        raise ValueError(f"{field}: expected a project-relative path")
    parts = value.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p for p in parts):
        raise ValueError(f"{field}: ambiguous relative path")
    if any(unicodedata.normalize("NFKC", p).casefold() in (".secrets", ".git") for p in parts):
        raise ValueError(f"{field}: protected path refused")
    return str(Path(*parts))


def options(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(OPTION_KEYS):
        raise ValueError(f"runtime.hooks: required keys {OPTION_KEYS}")
    out = dict(value)
    for field in ("policy_dir", "guard_config", "image_output_dir"):
        out[field] = relative(value[field], field)
    for field in (
        "timeout_seconds",
        "max_input_bytes",
        "max_patch_bytes",
        "max_patch_targets",
        "max_task_bytes",
    ):
        if type(value[field]) is not int or value[field] <= 0:
            raise ValueError(f"runtime.hooks.{field}: expected positive integer")
    section, labels = value["stop_section"], value["stop_labels"]
    if not isinstance(section, str) or not section.startswith("## ") or "\n" in section:
        raise ValueError("runtime.hooks.stop_section: expected one level-two Markdown heading")
    if (
        not isinstance(labels, list)
        or not labels
        or any(
            not isinstance(x, str) or not x.strip() or any(c in x for c in "\r\n:") for x in labels
        )
        or len(set(labels)) != len(labels)
    ):
        raise ValueError("runtime.hooks.stop_labels: expected unique checklist labels")
    return out


def inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path must be strictly inside {root}: {path}")
    return resolved


def no_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError(f"linked host policy/artifact path refused: {part}")


def trusted_paths(root: Path, cwd: Path, writes: list[str], opts: Mapping[str, Any]) -> list[Path]:
    if (root / ".git").is_file():
        raise ValueError(
            "run cli_worker.py from the canonical root checkout, not its worktree copy"
        )
    paths = [root / "tools" / "agent-workflow" / x for x in TRUST_FILES]
    paths += [root / opts["guard_config"], root / opts["policy_dir"]]
    for path in paths:
        inside(root, path)
        no_links(path)
        for area in [cwd, *(Path(x) for x in writes)]:
            if path.resolve().is_relative_to(area.resolve()):
                raise ValueError(f"host hook code/config/state is writable by worker: {path}")
    return paths


def image_destination(cx: Mapping[str, Any], thread: str, filename: str) -> Path:
    root = Path(cx["project_root"])
    output = inside(root, root / relative(cx["hooks"]["image_output_dir"], "image_output_dir"))
    no_links(output)
    for protected in (root / ".venv", Path(cx["venv_dir"]), root / "tools" / "agent-workflow"):
        if output.is_relative_to(protected.resolve()) or protected.resolve().is_relative_to(output):
            raise ValueError(f"image output overlaps a protected runtime: {output}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread):
        raise ValueError("invalid image thread identity")
    return inside(root, output / thread / filename)


def fingerprint(root: Path, opts: Mapping[str, Any]) -> str:
    base = root / "tools" / "agent-workflow"
    paths = [base / x for x in TRUST_FILES] + [root / opts["guard_config"]]
    digest = hashlib.sha256()
    for path in paths:
        no_links(path)
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def command_line(argv: list[str], windows: bool) -> str:
    # cmd expands percent/bang even inside quotes. Only host paths and protocol
    # switches enter here; worker text is confined to stdin and the policy JSON.
    if any(not arg or any(c in arg for c in '%!&|<>^"\r\n\0') for arg in argv):
        raise ValueError("unsafe metacharacter in trusted hook command path")
    if windows:
        # Measured on 0.167.0: a hook command with double quotes never runs and
        # Codex reports nothing, so the guard fails open. Unquoted paths only.
        if any(any(c.isspace() for c in arg) for arg in argv):
            raise ValueError("Windows hook command path contains whitespace")
        return " ".join(argv)
    return shlex.join(argv)


def hook_table(command: str, timeout: int, windows: bool) -> dict[str, Any]:
    source = "C:\\<session-flags>\\config.toml" if windows else "/<session-flags>/config.toml"
    table: dict[str, Any] = {"state": {}}
    for event, key in EVENTS.items():
        handler = {
            "type": "command",
            "command": command + " --event " + event,
            "timeout": timeout,
            "async": False,
        }
        identity = {"event_name": key, "hooks": [handler]}
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        table[event] = [{"hooks": [handler]}]
        table["state"][f"{source}:{key}:0:0"] = {
            "enabled": True,
            "trusted_hash": "sha256:" + digest,
        }
    return table


def valid_table(table: object) -> bool:
    if not isinstance(table, dict) or set(table) != {*EVENTS, "state"}:
        return False
    state = table["state"]
    if (
        not isinstance(state, dict)
        or len(state) != len(EVENTS)
        or any(not isinstance(k, str) for k in state)
    ):
        return False
    for event, key in EVENTS.items():
        groups = table[event]
        if (
            not isinstance(groups, list)
            or len(groups) != 1
            or not isinstance(groups[0], dict)
            or set(groups[0]) != {"hooks"}
        ):
            return False
        handlers = groups[0]["hooks"]
        if (
            not isinstance(handlers, list)
            or len(handlers) != 1
            or not isinstance(handlers[0], dict)
        ):
            return False
        handler = handlers[0]
        if (
            set(handler) != {"type", "command", "timeout", "async"}
            or handler["type"] != "command"
            or not isinstance(handler["command"], str)
            or not handler["command"]
            or handler["async"] is not False
            or type(handler["timeout"]) is not int
            or handler["timeout"] <= 0
        ):
            return False
        identity_value = {"event_name": key, "hooks": [handler]}
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    identity_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
        )
        found = [value for name, value in state.items() if name.endswith(":" + key + ":0:0")]
        if found != [{"enabled": True, "trusted_hash": digest}]:
            return False
    return True


def declaration(cx: Mapping[str, Any], cwd: Path, interpreter: Path) -> dict[str, Any]:
    root, opts = Path(cx["project_root"]), cx["hooks"]
    trusted_paths(root, cwd, cx["write"], opts)
    policy = inside(root, root / opts["policy_dir"] / (cx["hook_id"] + ".json"))
    digest = fingerprint(root, opts)
    command = command_line(
        [
            str(interpreter),
            "-B",
            "-I",
            str(root / "tools" / "agent-workflow" / "hooks" / "codex_guard.py"),
            "--policy",
            str(policy),
            "--digest",
            digest,
        ],
        sys.platform == "win32",
    )
    return hook_table(command, opts["timeout_seconds"], sys.platform == "win32")


def toml(value: object) -> str:
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                json.dumps(str(k), ensure_ascii=False) + "=" + toml(v) for k, v in value.items()
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(toml(x) for x in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def prepare(worker: Mapping[str, Any]) -> Path:
    cx = worker["codex"]
    root, cwd, opts = Path(cx["project_root"]), Path(worker["cwd"]), cx["hooks"]
    trusted_paths(root, cwd, cx["write"], opts)
    directory = root / opts["policy_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    policy: Path = directory / (cx["hook_id"] + ".json")
    # A fresh launch id also isolates Stop counters and receipts between sends.
    body = {
        "root": str(root),
        "cwd": str(cwd),
        "options": opts,
        "allowed_tools": cx["hook_allowed_tools"],
        "launch_id": uuid.uuid4().hex,
        "task_file": worker.get("hooks", {}).get("task_file"),
        "home": cx["home"],
        "thread_id": worker.get("thread_id"),
        "digest": fingerprint(root, opts),
    }
    no_links(policy)
    policy.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return policy


def check_version(
    cx: Mapping[str, Any], env: Mapping[str, str], runner: Any = subprocess.run
) -> None:
    expected = cx["tested_codex_version"]
    help_text = "PM must pass tools/agent-workflow/codex_hook_probe.py on the host before updating tested_codex_version"
    try:
        result = runner(
            [*cx["command"], "--version"],
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=cx["check_timeout_seconds"],
            check=False,
        )
        actual = result.stdout.strip()
        if result.returncode != 0 or actual != "codex-cli " + expected:
            raise ValueError(
                f"expected codex-cli {expected}, got {actual!r}, rc={result.returncode}"
            )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Codex version refused: {exc}; {help_text}") from exc


def read_receipts(
    worker: Mapping[str, Any], policy: Path, thread: str | None
) -> tuple[int | None, list[str]]:
    body = json.loads(policy.read_text(encoding="utf-8-sig"))
    directory = policy.parent
    context: int | None = None
    moved: list[str] = []
    expected: set[str] = set()
    received: set[str] = set()
    transfers: list[tuple[Path, Path]] = []
    receipt = directory / (body["launch_id"] + ".jsonl")
    if not receipt.exists():
        # PostModelRequest runs on every turn, so no file means no hook ran at all.
        raise ValueError("no hook receipt: the host hooks did not run in this turn")
    no_links(receipt)
    with receipt.open(encoding="utf-8-sig") as stream:
        for line in stream:
            item = json.loads(line)
            if item.get("type") == "hook_failure":
                raise ValueError(f"host hook failed ({item['event']}): {item['reason']}")
            if item["session_id"] != thread:
                raise ValueError("hook receipt belongs to another thread")
            if item["type"] == "context":
                context = item["input_tokens"]
                if context is not None and (type(context) is not int or context < 0):
                    raise ValueError("invalid context receipt")
            elif item["type"] == "image_expected":
                expected.add(item["call_id"])
            elif item["type"] == "image":
                cx = worker["codex"]
                if IMAGE_TOOL not in cx["hook_allowed_tools"]:
                    raise ValueError("image receipt for non-image role")
                source = Path(item["path"])
                expected_path = image_path(Path(cx["home"]), thread or "", item["call_id"])
                if (
                    source != expected_path
                    or item["call_id"] not in expected
                    or item["call_id"] in received
                ):
                    raise ValueError("image receipt path does not match its call/thread")
                transfers.append((source, image_destination(cx, thread or "", source.name)))
                received.add(item["call_id"])
    if expected - received:
        raise ValueError(
            "image admission has no successful custody receipt: "
            + ", ".join(sorted(expected - received))
        )
    # Every receipt is valid before the first irreversible move.
    for source, destination in transfers:
        try:
            move_image(source, destination)
        except (OSError, ValueError) as exc:
            done = ", ".join(moved) or "none"
            raise CustodyError(f"{exc}; completed transfers: {done}", moved) from exc
        moved.append(str(destination))
    return context, moved


class CustodyError(ValueError):
    """A transfer failed after earlier ones completed; `moved` names their destinations."""

    def __init__(self, message: str, moved: list[str]) -> None:
        super().__init__(message)
        self.moved = list(moved)


def image_path(home: Path, session: str, call: str) -> Path:
    def sanitize(value: str) -> str:
        return (
            "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in value)
            or "generated_image"
        )

    return home / "generated_images" / sanitize(session) / (sanitize(call) + ".png")


def file_digest(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def publish(temp: Path, destination: Path) -> None:
    if sys.platform == "win32":
        os.rename(temp, destination)  # Windows rename refuses an existing destination.
    else:
        # Same-directory link/unlink is an atomic no-replace name publication;
        # unlike POSIX rename it cannot overwrite an unrelated existing file.
        os.link(temp, destination)


def move_image(source: Path, destination: Path) -> None:
    no_links(source)
    no_links(destination)
    original = source.stat()
    if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
        raise ValueError(f"image source must be a regular unshared file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    no_links(destination)
    temporary = destination.with_name("." + uuid.uuid4().hex + ".moving")
    committed = False
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if file_digest(source) != file_digest(temporary) or identity(source.stat()) != identity(
            original
        ):
            raise ValueError(f"image changed during custody transfer: {source}")
        publish(temporary, destination)
        committed = True
        if temporary.exists():
            temporary.unlink()
        if identity(source.stat()) != identity(original) or file_digest(source) != file_digest(
            destination
        ):
            raise ValueError("source identity/content changed before deletion")
        source.unlink()
    except BaseException as exc:
        if not committed and temporary.exists():
            try:
                temporary.unlink()
            except OSError as cleanup_error:
                raise ValueError(
                    f"image staging cleanup refused: {temporary}; source {source}; {cleanup_error}"
                ) from cleanup_error
        if committed:
            raise ValueError(
                f"image NOT moved; source {source}; destination {destination}; staging {temporary}; {exc}"
            ) from exc
        raise


def identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink
