"""Token usage per model: Claude CLI workers (all .tmp/cli-jobs logs) versus one PM session transcript.

Usage: python -B usage_breakdown.py <pm-transcript.jsonl> --date YYYY-MM-DD
Input-equivalent tokens: cache read 0.1, cache write 2 (1h TTL price), uncached input 1; output is listed separately.
"""

from __future__ import annotations

import argparse
import collections
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

JOBS = Path(__file__).resolve().parents[2] / ".tmp" / "cli-jobs"


def add(totals: dict[str, collections.Counter[str]], key: str, usage: dict[str, int]) -> None:
    c = totals[key]
    c["calls"] += 1
    c["read"] += usage.get("cache_read_input_tokens") or 0
    c["write"] += usage.get("cache_creation_input_tokens") or 0
    c["input"] += usage.get("input_tokens") or 0
    c["output"] += usage.get("output_tokens") or 0


def assistant_messages(path: Path) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield (event, message) once per API message id (a call can be streamed as several events)."""
    seen: set[str] = set()
    # LF only: str.splitlines would also cut a JSON line at a raw U+2028; non-object lines are skipped.
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        yield event, message


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pm_transcript", type=Path)
    ap.add_argument(
        "--date", required=True, help="count PM calls whose timestamp starts with this date"
    )
    a = ap.parse_args()
    totals: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for path in JOBS.glob("*/*.jsonl"):
        for _, message in assistant_messages(path):
            add(totals, "worker:" + message.get("model", "?"), message.get("usage") or {})
    for event, message in assistant_messages(a.pm_transcript):
        if str(event.get("timestamp", ""))[:10] == a.date:
            add(totals, "PM:" + message.get("model", "?"), message.get("usage") or {})
    for key, c in sorted(totals.items()):
        eq = c["read"] * 0.1 + c["write"] * 2 + c["input"]
        print(
            f"{key:40s} calls={c['calls']:5d} read={c['read'] / 1e6:7.1f}M write={c['write'] / 1e6:5.2f}M "
            f"out={c['output'] / 1e6:5.2f}M input_eq={eq / 1e6:6.1f}M"
        )


if __name__ == "__main__":
    main()
