"""Write the pre-push marker for the current HEAD of a repository (decision 503).

The PM's prepush lint calls this only when every section is clean. tool_guard.py (`push_guard`) then lets
`git push` through for exactly that commit: the marker is `<marker_dir>/<full HEAD id>` under the project root.
Usage: python -B -I tools/agent-workflow/prepush_mark.py <repo> [--config FILE]
Exit 0 and one line on success; exit 1 with the reason on stderr otherwise. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
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


def main(argv: list[str] | None = None, root: Path = gc.ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="the repository whose HEAD is marked")
    parser.add_argument(
        "--config", help="guard config (default tools/agent-workflow/guard_config.json)"
    )
    args = parser.parse_args(argv)
    try:
        cfg = gc.load(gc.resolve_path(args.config))
        sha = head_id(Path(args.repo), cfg["git_timeout_seconds"])
        marker_dir = root / cfg["push_guard"]["marker_dir"]
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / sha).write_text(
            f"{dt.datetime.now().isoformat(timespec='seconds')} {Path(args.repo).resolve()}\n",
            encoding="utf-8",
        )
    except (gc.ConfigError, OSError, SystemExit) as exc:
        sys.stderr.write(f"prepush_mark: {exc}\n")
        return 1
    print(f"prepush marker written for {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
