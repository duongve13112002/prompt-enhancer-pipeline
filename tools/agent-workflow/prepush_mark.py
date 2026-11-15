"""Write the pre-push marker for the current HEAD of a repository (decision 503).

prepush_check.py calls this only when every part is clean (decision 77). tool_guard.py (`push_guard`) then lets
`git push` through for exactly that commit: the marker is `<marker_dir>/<full HEAD id>` under the project root.
`--expect <sha>` (prepush_check.py always passes it): the marker is named <sha>, the commit that was checked, and nothing
is written when HEAD is no longer <sha> (T-024 review F7: a HEAD read again may be a commit nobody checked).
Usage: python -B -I tools/agent-workflow/prepush_mark.py <repo> [--expect SHA] [--config FILE]
Exit 0 and one line on success; exit 1 with the reason on stderr otherwise. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import guard_config as gc

COMMIT_ID_LENGTHS = (40, 64)  # protocol-bound: SHA-1 and SHA-256 object ids


def head_id(repo: Path, timeout: int) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"cannot run git in {repo}: {type(exc).__name__}") from None
    sha = proc.stdout.decode("ascii", "replace").strip().lower()
    if (
        proc.returncode != 0
        or len(sha) not in COMMIT_ID_LENGTHS
        or any(c not in "0123456789abcdef" for c in sha)
    ):
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise SystemExit(f"{repo} has no readable HEAD ({detail or 'git rev-parse failed'})")
    return sha


def marker_path(root: Path, cfg: dict, sha: str) -> Path:
    return root / cfg["push_guard"]["marker_dir"] / sha


def remove_marker(root: Path, cfg: dict, sha: str) -> None:
    """Delete the marker of <sha> if there is one (T-024 final round, F23: a failed or interrupted check must not leave
    the success marker of an earlier run). Raises OSError when an existing marker cannot be removed, so the caller
    fails closed instead of leaving it."""
    marker_path(root, cfg, sha).unlink(missing_ok=True)


def write_marker(root: Path, cfg: dict, sha: str, repo: Path) -> None:
    """Create the marker whole or not at all: the content goes to a temporary name the guard never looks up, then is
    renamed onto `<marker_dir>/<sha>`, so a write cut short leaves no marker."""
    target = marker_path(root, cfg, sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".partial-", dir=target.parent)  # protocol-bound: never a commit id
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {repo.resolve()}\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None, root: Path = gc.ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="the repository whose HEAD is marked")
    parser.add_argument(
        "--config", help="guard config (default tools/agent-workflow/guard_config.json)"
    )
    parser.add_argument("--expect", help="the full commit id that was checked; HEAD must still be it")
    args = parser.parse_args(argv)
    try:
        cfg = gc.load(gc.resolve_path(args.config))
        head = head_id(Path(args.repo), cfg["git_timeout_seconds"])
        if args.expect is not None and head != args.expect.lower():
            raise SystemExit(f"HEAD is {head}, not the checked commit {args.expect}; no marker written")
        sha = args.expect.lower() if args.expect is not None else head
        write_marker(root, cfg, sha, Path(args.repo))
    except (gc.ConfigError, OSError, SystemExit) as exc:
        sys.stderr.write(f"prepush_mark: {exc}\n")
        return 1
    print(f"prepush marker written for {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
