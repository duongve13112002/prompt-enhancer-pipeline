"""Pre-push check of the orchestrator (decision 77, T-024): every part clean, then the push marker.

Parts, run on the commits the push would publish (`<base>..<sha>` of <repo>, where <sha> is HEAD read once at the
start and <base> the commit id of `refs/remotes/<git_guard.push_remote>/<git_guard.push_branch>`, read once; every part
checks those ids, never a later HEAD):
- destination: that remote-tracking ref exists (T-024 round 4, R14: never the branch's upstream, which may track any
  other branch), and HEAD is `refs/heads/<push_branch>` at <sha>, since the one allowed push sends that branch's tip;
  without the ref there is no range, and the first push of a branch is the user's decision, so the check fails.
- whitespace: `git diff --check <upstream>...<sha>`.
- paths: no commit of the range adds or changes a path under `push_guard.forbidden_path_prefixes` (secrets, models,
  database, working data) or ending in one of `push_guard.forbidden_suffixes` (model files). Case-insensitive, because
  Windows checkouts treat `.Secrets/` and `.secrets/` as one directory.
- setup: `setup_project.py check` exits 0 and prints no `FAIL` line (warnings pass).
- decisions: `decisions_applied.py check` exits 0.
Only when every part is clean it asks prepush_mark.py to mark exactly <sha> (`--expect <sha>`): the marker names the
checked commit, and a HEAD that differs from <sha> at write time writes nothing (T-024 review F7), so `tool_guard.py`
(`push_guard`) lets `git push` through only for the commit that was checked.
The marker of <sha> exists only after this run passed every part: it is removed before the parts run and again on every
exit that wrote none (a failed part, an interruption, an error), so an earlier success never outlives a failed check
(T-024 final round, F23).
Usage: python -B -I tools/agent-workflow/prepush_check.py <repo> [--config FILE]
Exit 0 when the marker is written; 1 with one `FAIL` line per failing part otherwise. Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "hooks"))
import guard_config as gc

sys.path.insert(0, str(HERE))
import prepush_mark

# Project tools run as sub-checks, relative to <root>/tools/agent-workflow (protocol-bound: their CLI is `check`).
SUBCHECKS = (("setup", "setup_project.py"), ("decisions", "decisions_applied.py"))
TOOLS_DIR = Path("tools") / "agent-workflow"  # artifact-bound: where the tooling lives under the project root


class CheckError(Exception):
    """A part that cannot run (git missing, timeout); it counts as a failure."""


def _git(repo: Path, timeout: int, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"git {args[0]}: {type(exc).__name__}") from None


def _text(raw: bytes, limit: int = 300) -> str:
    return raw.decode("utf-8", "replace").strip()[:limit]


def destination(repo: Path, timeout: int, remote: str, branch: str, head: str) -> str:
    """The commit id of `refs/remotes/<push_remote>/<push_branch>`: the base of what `git push <remote> <branch>`
    publishes (T-024 round 4, R14: the branch's upstream may be anything else). The checked commit must also be the
    tip of `refs/heads/<branch>` with HEAD on it, because that tip is what the push sends."""
    ref = f"refs/remotes/{remote}/{branch}"
    proc = _git(repo, timeout, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    base = _text(proc.stdout).lower()
    if proc.returncode != 0 or not base:
        raise CheckError(
            f"`{ref}` does not exist, so there is no range to check (fetch `{remote}` first); the first push of a "
            "branch is the user's decision"
        )
    row = _git(repo, timeout, "for-each-ref", "--format=%(HEAD) %(objectname)", f"refs/heads/{branch}")
    mark, _, tip = _text(row.stdout).lower().rpartition(" ")
    if row.returncode != 0 or mark.strip() != "*" or tip != head.lower():
        raise CheckError(
            f"HEAD is not `refs/heads/{branch}` at the checked commit {head}; `git push {remote} {branch}` sends that "
            "branch's tip, so check it out and check again"
        )
    return base


def forbidden_paths(paths: list[str], prefixes: list[str], suffixes: list[str]) -> list[str]:
    """Paths (repository-relative, `/`-separated) under a forbidden prefix or with a forbidden suffix."""
    folded_prefixes = [p.casefold().rstrip("/") for p in prefixes]
    folded_suffixes = [s.casefold() for s in suffixes]
    bad = []
    for path in paths:
        folded = path.casefold()
        if any(folded == p or folded.startswith(p + "/") for p in folded_prefixes) or any(
            folded.endswith(s) for s in folded_suffixes
        ):
            bad.append(path)
    return list(dict.fromkeys(bad))


def changed_paths(repo: Path, timeout: int, base: str, head: str = "HEAD") -> list[str]:
    """Every path added, copied, modified, renamed to or retyped by any commit of `<base>..<head>` (merges against each
    parent), so a file added and removed again inside the range is still found."""
    proc = _git(
        repo,
        timeout,
        "log",
        "--no-renames",
        "-m",
        "--format=",
        "--name-only",
        "--diff-filter=ACMRT",
        "-z",
        f"{base}..{head}",
    )
    if proc.returncode != 0:
        raise CheckError(f"git log failed: {_text(proc.stderr)}")
    # `-z` ends every name with NUL and never quotes it (git-log(1): "do not munge pathnames and use NULs as output
    # field terminators"), so a name may hold LF, tab or non-ASCII text: split on NUL only (T-024 final round, F24).
    names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    return list(dict.fromkeys(n for n in names if n))


def run_subcheck(root: Path, script: str, timeout: int) -> tuple[bool, str]:
    path = root / TOOLS_DIR / script
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-I", str(path), "check"],
            capture_output=True,
            timeout=timeout,
            cwd=root,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{script} check: {type(exc).__name__}"
    out = proc.stdout.decode("utf-8", "replace")
    failing = [line.strip() for line in out.splitlines() if line.startswith("FAIL")]
    if proc.returncode != 0 or failing:
        detail = "; ".join(failing[:3]) or _text(proc.stderr) or out.strip()[-300:]
        return False, f"{script} check exit {proc.returncode}: {detail}"
    return True, f"{script} check clean"


def run(repo: Path, cfg: dict, root: Path, head: str = "HEAD") -> list[tuple[str, bool, str]]:
    """(part, clean, detail) for every part on `<destination>..<head>`; parts that need the range are skipped after a
    missing destination. A part that cannot run (timeout, OSError) fails."""
    pg = cfg["push_guard"]
    gg = cfg["git_guard"]
    timeout = cfg["git_timeout_seconds"]
    results: list[tuple[str, bool, str]] = []
    if head == "HEAD":
        head = prepush_mark.head_id(repo, timeout)
    try:
        base = destination(repo, timeout, gg["push_remote"], gg["push_branch"], head)
        results.append(("destination", True, f"{gg['push_remote']}/{gg['push_branch']} at {base}"))
    except CheckError as exc:
        base = None
        results.append(("destination", False, str(exc)))
    if base is not None:
        try:
            proc = _git(repo, timeout, "diff", "--check", f"{base}...{head}")
            results.append(
                ("whitespace", proc.returncode == 0, _text(proc.stdout) or "no whitespace errors")
            )
        except CheckError as exc:
            results.append(("whitespace", False, str(exc)))
        try:
            paths = changed_paths(repo, timeout, base, head)
            bad = forbidden_paths(paths, pg["forbidden_path_prefixes"], pg["forbidden_suffixes"])
            detail = ", ".join(bad[:10]) if bad else f"{len(paths)} paths, none forbidden"
            results.append(("paths", not bad, detail))
        except CheckError as exc:
            results.append(("paths", False, str(exc)))
    for part, script in SUBCHECKS:
        clean, detail = run_subcheck(root, script, pg["check_timeout_seconds"])
        results.append((part, clean, detail))
    return results


def main(argv: list[str] | None = None, root: Path = gc.ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="the repository whose HEAD would be pushed (the main working tree)")
    parser.add_argument(
        "--config", help="guard config (default tools/agent-workflow/guard_config.json)"
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    try:
        cfg = gc.load(gc.resolve_path(args.config))
    except gc.ConfigError as exc:
        sys.stderr.write(f"prepush_check: {exc}\n")
        return 1
    try:
        sha = prepush_mark.head_id(repo, cfg["git_timeout_seconds"])  # the one commit every part checks
    except SystemExit as exc:
        sys.stderr.write(f"prepush_check: {exc}\n")
        return 1
    # F23 (T-024 final round): the marker of <sha> exists only after this run passed every part. An earlier run's
    # marker is removed first, and again on any exit that did not write one (failed part, interruption, crash), so a
    # stale success never vouches for a commit whose check failed. A marker that cannot be removed fails the run.
    try:
        prepush_mark.remove_marker(root, cfg, sha)
    except OSError as exc:
        sys.stderr.write(f"prepush_check: cannot remove the earlier marker of {sha}: {exc}\n")
        return 1
    marked = False
    try:
        results = run(repo, cfg, root, sha)
        for part, clean, detail in results:
            print(f"{'OK  ' if clean else 'FAIL'} {part:<10} {detail}")
        if not all(clean for _, clean, _ in results):
            print("prepush_check: not clean, no marker written")
            return 1
        mark_args = [str(repo), "--expect", sha] + (["--config", args.config] if args.config else [])
        marked = prepush_mark.main(mark_args, root=root) == 0
        return 0 if marked else 1
    finally:
        if not marked:
            try:
                prepush_mark.remove_marker(root, cfg, sha)
            except OSError as exc:
                sys.stderr.write(f"prepush_check: cannot remove the marker of {sha}: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
