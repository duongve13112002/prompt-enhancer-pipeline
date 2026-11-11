"""Prompt-cache check for Claude Code CLI worker logs (.tmp/cli-jobs/<name>/NNN.jsonl).

Each assistant event carries the API usage of one model call. A call is a cache miss when
the cached prefix shrinks: cache_read is well below the previous call's full context, so
the prefix was rewritten (cache_creation) instead of read. The first call of a session is
an expected write. Usage: python -B cache_check.py [--since-hours 24] [--name w1]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

JOBS = Path(__file__).resolve().parents[2] / ".tmp" / "cli-jobs"
MISS_TOKENS = 4096  # a prefix shrink below this is noise (system reminders, tool output)


def calls(log: Path) -> list[dict[str, int]]:
    out: list[dict[str, int]] = []
    seen: set[str] = set()
    # LF only: str.splitlines would also cut a JSON line at a raw U+2028; non-object lines are skipped.
    for line in log.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        # One API call can be streamed as several assistant events with the same id.
        message_id = str(message.get("id"))
        if message_id in seen:
            continue
        seen.add(message_id)
        usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        out.append(
            {
                "input": usage.get("input_tokens") or 0,
                "read": usage.get("cache_read_input_tokens") or 0,
                "write": usage.get("cache_creation_input_tokens") or 0,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--name")
    a = ap.parse_args()
    cutoff = dt.datetime.now().timestamp() - a.since_hours * 3600
    total_read = total_write = total_input = 0
    misses: list[str] = []
    for worker in sorted(p for p in (JOBS.iterdir() if JOBS.is_dir() else ()) if p.is_dir()):
        if a.name and worker.name != a.name:
            continue
        logs = sorted(worker.glob("*.jsonl"))
        previous_context = 0
        for log in logs:
            mtime = log.stat().st_mtime
            seq = calls(log)
            if mtime < cutoff:
                if seq:
                    last = seq[-1]
                    previous_context = last["input"] + last["read"] + last["write"]
                continue
            read = sum(c["read"] for c in seq)
            write = sum(c["write"] for c in seq)
            total_read += read
            total_write += write
            total_input += sum(c["input"] for c in seq)
            for index, c in enumerate(seq):
                context = c["input"] + c["read"] + c["write"]
                if previous_context and c["read"] + MISS_TOKENS < previous_context:
                    where = "turn start (resume)" if index == 0 else f"call {index + 1}"
                    gap = ""
                    if index == 0 and logs.index(log) > 0:
                        before = logs[logs.index(log) - 1].stat().st_mtime
                        start = log.stat().st_ctime
                        gap = f", {round((start - before) / 60)} min after previous turn"
                    misses.append(
                        f"{worker.name}/{log.name} {where}: read {c['read']} of previous "
                        f"{previous_context}, rewrote {c['write']}{gap}"
                    )
                previous_context = context
            ratio = read / (read + write) if read + write else 0.0
            print(
                f"{worker.name}/{log.name}: calls={len(seq)} read={read} write={write} hit={ratio:.1%}"
            )
    denominator = total_read + total_write + total_input
    print(
        f"TOTAL read={total_read} write={total_write} uncached_input={total_input} "
        f"hit={total_read / denominator if denominator else 0:.1%}"
    )
    print(f"MISSES {len(misses)}")
    for line in misses:
        print("  " + line)
    # Gemini (agy): usage per job; input_tokens is the uncached part, cumulative per conversation.
    results = JOBS.parent / "agy-work" / "_results"
    g_read = g_input = 0
    for path in sorted(results.glob("*.json"), key=lambda p: p.stat().st_mtime):
        if path.stat().st_mtime < cutoff:
            continue
        try:
            usage = json.loads(path.read_text(encoding="utf-8-sig")).get("usage") or {}
        except ValueError:
            continue
        read, uncached = usage.get("cache_read_tokens") or 0, usage.get("input_tokens") or 0
        if read + uncached:
            g_read += read
            g_input += uncached
            print(
                f"gemini {path.stem}: read={read} uncached={uncached} hit={read / (read + uncached):.1%}"
            )
    if g_read + g_input:
        print(
            f"GEMINI TOTAL read={g_read} uncached={g_input} hit={g_read / (g_read + g_input):.1%}"
        )


if __name__ == "__main__":
    main()
