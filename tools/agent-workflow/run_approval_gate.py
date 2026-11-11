"""Approved-profile gate for live runs: refuse a run whose resolved profile differs from what the user approved.

Usage (PM tooling; needs the project `.venv` and `src` on the path, no network, no model loading):
  python -B run_approval_gate.py record  --run R --profile configs/... --approved-by "<date and user words>"
  python -B run_approval_gate.py approve --run R --key /vlm/roles/planner/model --to '"x"' --source "<date, words>"
  python -B run_approval_gate.py check   --run R --profile configs/...
Exit 0: every difference is listed as approved. Exit 1: refused (unapproved difference, missing manifest, wrong
profile). Exit 2: the gate could not run (profile does not load, bad arguments).

`record` stores the flattened resolved profile (`load_runtime_settings(...).settings`, defaults filled in, so a
changed source default shows as a difference too) after the user approved it. `check` loads the same profile again
and lists every added, removed or changed key; each must have an `approve` entry whose `to` equals the current value
(a removal is approved with `--removed`). Keys are JSON pointers (`~0`, `~1` escaped); a list is one leaf. The
manifest lives under `.tmp/pm-state/approvals/<run>.json` (untracked, like the `*.local.yaml` profiles it guards).
This complements, and does not replace, scripts/gpu_preflight.py (artifacts, GPU, leases) and
scripts/r3_e2e/preflight_check.py (one job's record). Secrets are never read: the resolved settings hold
credential paths only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import posixpath
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_config  # noqa: E402  (sibling module; the gate runs as a script)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "run-approval/1"
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
# Layout of the report, not a deployment choice: longest value shown per line and most refused keys listed.
VALUE_CHARS = 120
MAX_LISTED = 60
REMOVED = {"removed": True}


class Diff(NamedTuple):
    key: str
    kind: str  # added | removed | changed
    approved: Any
    current: Any


class GateError(Exception):
    """The gate could not run (exit 2)."""


class RefusedError(GateError):
    """There is no usable approval to check against (exit 1): the run is not approved."""


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """JSON pointer -> leaf. Mappings recurse; scalars, lists and empty mappings are leaves."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, child in value.items():
            out.update(flatten(child, f"{prefix}/{escape(str(key))}"))
        return out
    return {prefix or "/": value}


def diff(approved: dict[str, Any], current: dict[str, Any]) -> list[Diff]:
    rows = [
        Diff(key, "removed", approved[key], None)
        for key in sorted(approved.keys() - current.keys())
    ]
    rows += [
        Diff(key, "added", None, current[key]) for key in sorted(current.keys() - approved.keys())
    ]
    rows += [
        Diff(key, "changed", approved[key], current[key])
        for key in sorted(approved.keys() & current.keys())
        if json.dumps(approved[key], sort_keys=True) != json.dumps(current[key], sort_keys=True)
    ]
    return sorted(rows, key=lambda row: row.key)


def unapproved(rows: list[Diff], changes: list[dict[str, Any]]) -> list[Diff]:
    """The differences that no `approve` entry covers with the exact current value (or a removal)."""
    by_key = {str(c.get("key")): c for c in changes if isinstance(c, dict)}
    refused: list[Diff] = []
    for row in rows:
        entry = by_key.get(row.key)
        if entry is None or not str(entry.get("source", "")).strip():
            refused.append(row)
        elif row.kind == "removed":
            if entry.get("to") != REMOVED:
                refused.append(row)
        elif json.dumps(entry.get("to"), sort_keys=True) != json.dumps(row.current, sort_keys=True):
            refused.append(row)
    return refused


def manifest_path(approvals: Path, run: str) -> Path:
    if not RUN_NAME.fullmatch(run):
        raise GateError(f"run name {run!r} must match {RUN_NAME.pattern}")
    return approvals / f"{run}.json"


def approvals_dir(root: Path, text: str | None) -> Path:
    base = (root / ".tmp" / "pm-state" / "approvals") if text is None else Path(text)
    resolved = base.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise GateError(f"approvals directory {resolved} is outside the project root")
    return resolved


def canonical_text(profile: str) -> str:
    """Spelling-independent form of a profile path: `/` only, no `.` or `..` steps, case as the file system sees it
    (folded on Windows only). `configs\\x.yaml`, `./configs/x.yaml` and `configs/x.yaml` are one profile."""
    text = posixpath.normpath(profile.strip().replace("\\", "/"))
    return os.path.normcase(text).replace("\\", "/")


def normalize_profile(root: Path, profile: str) -> str:
    """The profile as a canonical path relative to the project root; a path outside the root is an error (exit 2)."""
    if not profile.strip():
        raise GateError("--profile is empty")
    base = root.resolve()
    target = Path(os.path.normpath(base / profile.strip().replace("\\", "/")))
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise GateError(f"profile {profile!r} is outside the project root") from None
    return canonical_text(relative.as_posix())


def resolve_values(root: Path, profile: str) -> dict[str, Any]:
    """The flattened resolved settings of one project profile (imports the project package lazily)."""
    try:
        binding = project_config.load(root)
    except project_config.ProjectConfigError as exc:
        raise GateError(f"project binding: {exc}") from exc
    if binding.runtime_config_module is None:
        raise GateError(
            "no runtime configuration module is bound: set python.package and python.runtime_config_module "
            f"in {project_config.REL_PATH} once the project has code"
        )
    src = str(root / binding.src_dir)
    if src not in sys.path:
        sys.path.insert(0, src)
    module = binding.runtime_config_module
    try:
        load_runtime_settings = getattr(importlib.import_module(module), binding.runtime_config_loader)
    except (ImportError, AttributeError) as exc:
        raise GateError(f"{module}.{binding.runtime_config_loader} is not importable from {src}: {exc}") from exc
    try:
        resolved = load_runtime_settings(profile, root)
    except Exception as exc:  # ConfigurationError and friends: the profile itself is the problem
        raise GateError(f"profile {profile} does not load: {type(exc).__name__}: {exc}") from exc
    return flatten(resolved.settings.model_dump(mode="json"))


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RefusedError(
            f"no approval manifest {path.name}: record it after the user approved the profile"
        ) from None
    except (OSError, ValueError) as exc:
        raise RefusedError(
            f"approval manifest {path.name} is unreadable: {type(exc).__name__}"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") != SCHEMA
        or not isinstance(data.get("values"), dict)
        or not isinstance(data.get("approved_changes"), list)
    ):
        raise RefusedError(f"approval manifest {path.name} does not match {SCHEMA}")
    return data


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".part")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def shown(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS] + "..."


def check(manifest: dict[str, Any], profile: str, current: dict[str, Any]) -> tuple[int, list[str]]:
    """(exit code, report lines) for one resolved profile against its manifest."""
    approved = manifest.get("profile")
    if not isinstance(approved, str) or canonical_text(approved) != canonical_text(profile):
        return 1, [f"REFUSED: the manifest approves {approved!r}, not {profile!r}"]
    rows = diff(manifest["values"], current)
    refused = unapproved(rows, manifest["approved_changes"])
    lines = [
        f"{len(current)} keys compared with the approval of {manifest.get('recorded')}: "
        f"{len(rows)} difference(s), {len(rows) - len(refused)} approved, {len(refused)} NOT approved"
    ]
    for row in refused[:MAX_LISTED]:
        lines.append(
            f"REFUSED {row.kind} {row.key}: approved {shown(row.approved)}, now {shown(row.current)}"
        )
    if len(refused) > MAX_LISTED:
        lines.append(f"... {len(refused) - MAX_LISTED} more")
    return (1 if refused else 0), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--approvals-dir", help="default: <project root>/.tmp/pm-state/approvals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="store the approved resolved profile")
    rec.add_argument("--run", required=True)
    rec.add_argument("--profile", required=True, help="project-relative profile under configs/")
    rec.add_argument(
        "--approved-by", required=True, help="date and the user's words, or a decision number"
    )
    rec.add_argument(
        "--replace", action="store_true", help="overwrite an existing manifest of this run"
    )
    app = sub.add_parser("approve", help="list one approved difference")
    app.add_argument("--run", required=True)
    app.add_argument("--key", required=True, help="JSON pointer, as printed by check")
    group = app.add_mutually_exclusive_group(required=True)
    group.add_argument("--to", help="the approved new value as JSON")
    group.add_argument("--removed", action="store_true", help="the key is approved to disappear")
    app.add_argument(
        "--source", required=True, help="date and the user's words, or a decision number"
    )
    chk = sub.add_parser("check", help="refuse unless every difference is approved")
    chk.add_argument("--run", required=True)
    chk.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    try:
        path = manifest_path(approvals_dir(root, args.approvals_dir), args.run)
        if args.cmd == "record":
            if not args.approved_by.strip():
                raise GateError("--approved-by must name the approval")
            if path.exists() and not args.replace:
                raise GateError(f"{path.name} exists; pass --replace to record a new approval")
            profile = normalize_profile(root, args.profile)
            values = resolve_values(root, profile)
            write_manifest(
                path,
                {
                    "schema": SCHEMA,
                    "run": args.run,
                    "profile": profile,
                    "recorded": dt.datetime.now().isoformat(timespec="seconds"),
                    "approved_by": args.approved_by.strip(),
                    "values": values,
                    "approved_changes": [],
                },
            )
            print(f"recorded {len(values)} keys of {profile} in {path}")
            return 0
        manifest = read_manifest(path)
        if args.cmd == "approve":
            if not args.source.strip():
                raise GateError("--source must name the approval")
            try:
                target = REMOVED if args.removed else json.loads(args.to)
            except ValueError as exc:
                raise GateError(f"--to is not JSON: {exc}") from exc
            changes = [c for c in manifest["approved_changes"] if c.get("key") != args.key]
            changes.append({"key": args.key, "to": target, "source": args.source.strip()})
            manifest["approved_changes"] = changes
            write_manifest(path, manifest)
            print(f"approved {args.key} -> {shown(target)} ({len(changes)} listed)")
            return 0
        profile = normalize_profile(root, args.profile)
        code, lines = check(manifest, profile, resolve_values(root, profile))
        print("\n".join(lines))
        return code
    except GateError as exc:
        refusal = isinstance(exc, RefusedError)
        print(f"{'REFUSED' if refusal else 'ERROR'}: {exc}", file=sys.stderr)
        return 1 if refusal else 2


if __name__ == "__main__":
    sys.exit(main())
