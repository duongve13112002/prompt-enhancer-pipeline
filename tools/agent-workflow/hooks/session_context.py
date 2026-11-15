"""SessionStart hook for the PM session: re-inject the user agreements and report live-document budgets.

Runs on `startup`, `resume` and `compact` (configured by the settings matcher). Reads the hook JSON on stdin and prints
documented SessionStart JSON output (`hookSpecificOutput.additionalContext`) with, in priority order:
- the PM continuation note sections whose heading starts with one of the configured `inject_headings` (agreements,
  `## IN-FLIGHT`, `## NOW`; at most `note_inject_chars`), so they reach the context even if the PM forgets to read the
  note after a compaction. If memory_hooks.json is missing, broken or lacks these two fields, the base behaviour stays
  (`FALLBACK_HEADINGS`, `FALLBACK_NOTE_CHARS`) and the problem is shown on a `not configured` line;
- one line per entry of the `blocks` list in `usage.quota_blocks_file` (codex_profiles.json; written by
  usage_snapshot.py and its `pm-set`), read under the writers' lock and marked when its reset time has passed or is
  not a time with a UTC offset; a file that cannot be read, or that the validator of the stored format
  (`usage_snapshot.stored_blocks`) calls malformed in any part, gets one `unreadable` line beside the blocks it
  still holds, never "no block";
- one line per configuration problem (`not configured: <field>`);
- decisions from `applied_from_decision` on whose index row says `applied: pending` (decisions_applied.py), and a count
  of rows without a valid marker;
- `git branch --no-merged <batch_branch>` and `git worktree list` (read-only, bounded by `git_timeout_seconds`);
- the head of `.tmp/pm-state/settled.md` (written by `recall.py build --settled`);
- a compact index of the newest decisions in `tasks/DECISIONS_INDEX.md` (`--decisions N`, default `decisions_default`,
  at most `decisions_max`, 0 turns it off), with
  the `recall.py search` command for details; a missing or malformed index file just leaves the index out;
- every live document over its character budget in `doc_budgets.json` (project files, the PM continuation note and the
  PM memory directory next to the session transcript).
All settings come from `tools/agent-workflow/memory_hooks.json` (memory_hooks_config.py); a missing or invalid field
switches off the block that needs it. The blocks are assembled in the order above within `total_inject_chars` (at most
10,000, the documented harness cap: a longer text is saved to a file and the model sees only a 2,000-character preview);
a block that does not fit is cut or omitted with a one-line notice and never displaces a block above it. Email-like text
is replaced before output.
Sessions that are cli_worker workers (recorded session id, or the worker-only auto-memory switch) are skipped.
Standard library only (runs with `python -B -I`); never blocks, always exits 0.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR") or ROOT / ".tmp" / "pm-state")
NOTE_NAME = "pm-continuation.md"
SETTLED_NAME = "settled.md"
TOOLS = Path(__file__).resolve().parents[1]
BUDGETS = TOOLS / "doc_budgets.json"
CONFIG = TOOLS / "memory_hooks.json"
# Hooks run with `python -I`, which keeps the script directory off sys.path: sibling modules load by file path.
CONFIG_MODULE = TOOLS / "memory_hooks_config.py"
APPLIED_MODULE = TOOLS / "decisions_applied.py"
CLEANUP_CONFIG = TOOLS / "cleanup_config.json"  # validated by purge_tmp.load_cleanup_config
# `usage.quota_blocks_file` lives in the usage settings, validated by codex_backend.load_usage_settings.
BACKEND_MODULE = TOOLS / "codex_backend.py"
USAGE_CONFIG = TOOLS / "codex_profiles.json"
SNAPSHOT_MODULE = TOOLS / "usage_snapshot.py"  # owns state_path, the contained path of every quota file
WORKER_ENV_FLAG = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
DECISIONS_REL = "tasks/DECISIONS_INDEX.md"
# Fail-safe of the base behaviour (safety-bound): when memory_hooks.json cannot supply the note settings, the agreements
# still reach the session with the headings and cut that the hook had before decision 353. The problem is shown.
FALLBACK_HEADINGS = ("## USER AGREEMENTS", "## IN-FLIGHT", "## NOW")
FALLBACK_NOTE_CHARS = 8000
DECISION_ROW = re.compile(r"^\|\s*(\d+)([a-z]?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|([^|]*)")
# The project .venv interpreter (AGENTS.md section 4); its path differs by OS, so it is named, not spelled.
RECALL_COMMAND = '<.venv python> -B -I tools/agent-workflow/recall.py search "<query>"'
# Layout of the injection, not a deployment choice: room kept for the one-line notice of omitted blocks, the smallest
# useful head of a cut block, and the longest git/worktree line.
NOTICE_RESERVE = 160
MIN_CUT_KEEP = 40
GIT_LINE_CHARS = 200
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_event() -> str:
    """Hook JSON from stdin as UTF-8. `python -I` ignores PYTHONUTF8, so the text layer would use the locale code
    page and mangle or reject non-ASCII paths; a test double without a byte layer falls back to `read()`."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read()
    return stream.read().decode("utf-8-sig", errors="replace")


def is_worker(session: object) -> bool:
    if os.environ.get(WORKER_ENV_FLAG) == "1":
        return True
    data = load_json(STATE / "cli-workers.json")
    if not isinstance(data, dict) or not session:
        return False
    workers = data.get("workers", data)
    return isinstance(workers, dict) and any(
        isinstance(w, dict) and w.get("session_id") == session for w in workers.values()
    )


def note_sections(text: str, headings: tuple[str, ...], max_chars: int) -> str:
    """Return the sections whose `## ` heading starts with one of `headings`, in note order, cut at `max_chars`."""
    out: list[str] = []
    keep = False
    for line in text.split(
        "\n"
    ):  # not splitlines(): a raw U+2028 in the PM's text must not start a fake heading
        if line.startswith("## "):
            keep = line.startswith(headings)
        if keep:
            out.append(line)
    joined = "\n".join(out).strip()
    if len(joined) > max_chars:
        joined = (
            joined[:max_chars] + f"\n[cut at {max_chars} characters: the section is over budget]"
        )
    return joined


def decision_index(text: str, count: int, text_chars: int) -> str:
    """One line per newest decision row (`| N | date | rule | ... |`): number, date, first characters of the rule."""
    rows: list[tuple[int, str, str, str]] = []
    for line in text.split("\n"):
        match = DECISION_ROW.match(line.strip())
        if match:
            rows.append(
                (int(match[1]), match[2], match[3], " ".join(match[4].replace("**", "").split()))
            )
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    out = []
    for number, suffix, date, rule in rows[: max(count, 0)]:
        clipped = rule if len(rule) <= text_chars else rule[:text_chars].rstrip() + "..."
        out.append(f"- {number}{suffix} ({date}): {clipped}")
    if not out:
        return ""
    return (
        f"Newest decisions (index of {DECISIONS_REL}; details: `{RECALL_COMMAND}`):\n"
        + "\n".join(out)
    )


def parse_options(argv: list[str]) -> int | None:
    """The explicit `--decisions N`, or None; an invalid option must never fail the hook (None = use the default)."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--decisions", type=int, default=None)
    try:
        return parser.parse_known_args(argv)[0].decisions  # type: ignore[no-any-return]
    except SystemExit:
        return None


def over_budget(memory_dir: Path | None) -> list[str]:
    spec = load_json(BUDGETS)
    if not isinstance(spec, dict):
        return [f"budget file unreadable: {BUDGETS.name}"]
    found: list[str] = []
    targets: list[tuple[str, Path, int]] = []
    project = spec.get("project")
    for rel, limit in project.items() if isinstance(project, dict) else ():
        if isinstance(limit, int):
            targets.append((rel, ROOT / rel, limit))
    note_limit = spec.get("pm_note")
    if isinstance(note_limit, int):
        targets.append((f".tmp/pm-state/{NOTE_NAME}", STATE / NOTE_NAME, note_limit))
    memory = spec.get("memory")
    memory = memory if isinstance(memory, dict) else {}
    if memory_dir is not None and memory_dir.is_dir():
        index_limit, topic_limit = memory.get("index"), memory.get("topic")
        for path in sorted(memory_dir.glob("*.md")):
            limit = index_limit if path.name == "MEMORY.md" else topic_limit
            if isinstance(limit, int):
                targets.append((f"memory/{path.name}", path, limit))
    for label, path, limit in targets:
        try:
            size = len(path.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        if size > limit:
            found.append(f"{label} {size} > {limit} characters")
    return found


def run_git(args: list[str], deadline: float) -> tuple[str | None, str]:
    """(stdout, "") on success, (None, reason) otherwise; read-only commands only, bounded by the shared deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "timed out"
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=remaining,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as exc:
        return None, type(exc).__name__
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or ["failed"])[0]
        return None, first[:GIT_LINE_CHARS]
    return done.stdout, ""


def clipped_lines(lines: list[str], limit: int) -> list[str]:
    shown = [line[:GIT_LINE_CHARS] for line in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"... {len(lines) - limit} more")
    return shown


def worktree_lines(porcelain: str, unmerged: frozenset[str] = frozenset()) -> list[str]:
    """`worktree list --porcelain` records (blank-line separated) as `path [branch|detached] [locked] [prunable]`.

    Worktrees on an unmerged branch come first (the ones that still need a ledger row), then the others, last
    registered first; git does not promise a creation order, so the unmerged-branch rule is the one that matters."""
    open_work: list[str] = []
    others: list[str] = []
    for record in porcelain.split("\n\n"):
        fields = [line for line in record.split("\n") if line.strip()]
        if not fields or not fields[0].startswith("worktree "):
            continue
        path, flags, branch = fields[0][len("worktree ") :], [], ""
        for line in fields[1:]:
            if line.startswith("branch "):
                branch = line[len("branch ") :].removeprefix("refs/heads/")
                flags.append(branch)
            elif line == "detached" or line.split(" ")[0] in ("bare", "locked", "prunable"):
                flags.append(line.split(" ")[0])
        text = f"{path} [{', '.join(flags)}]" if flags else path
        (open_work if branch in unmerged else others).append(text)
    return open_work + others[::-1]


def git_block(branch: str, timeout: int, max_branches: int, max_worktrees: int) -> str:
    deadline = time.monotonic() + timeout
    parts = []
    unmerged: frozenset[str] = frozenset()
    branches, why = run_git(
        ["branch", "--no-merged", branch, "--format=%(refname:short)"], deadline
    )
    if branches is None:
        parts.append(f"Unmerged branches: `git branch --no-merged {branch}` unavailable ({why}).")
    else:
        names = [line.strip() for line in branches.split("\n") if line.strip()]
        unmerged = frozenset(names)
        parts.append(
            f"Branches not merged into {branch} (check each has a ledger row):\n"
            + "\n".join(f"- {line}" for line in clipped_lines(names, max_branches))
            if names
            else f"No branch is unmerged into {branch}."
        )
    worktrees, why = run_git(["worktree", "list", "--porcelain"], deadline)
    if worktrees is None:
        parts.append(f"Worktrees: `git worktree list` unavailable ({why}).")
    else:
        lines = worktree_lines(worktrees.replace("\r\n", "\n"), unmerged)
        parts.append(
            "Worktrees:\n" + "\n".join(f"- {line}" for line in clipped_lines(lines, max_worktrees))
            if lines
            else "Worktrees: none listed."
        )
    return "\n".join(parts)


def tmp_line() -> str:
    """One read-only line: size of `.tmp` and orphans past the configured age (decision 43). The size scan stops at
    `scan_timeout_seconds`, shared with the ownership reads and the planning, and then says so; the orphan count skips the protected-tree walk (`sweep --dry-run` is exact)."""
    purge = load_module(TOOLS / "purge_tmp.py", "purge_tmp")
    purge.PROJECT = ROOT  # the module derives its root from its own path; the hook's root may differ (tests)
    purge.ARCHIVE = ROOT / ".tmp" / "pm-archive"
    try:
        config = purge.load_cleanup_config(CLEANUP_CONFIG)
    except purge.CleanupConfigError as exc:
        return f".tmp cleanup not configured: {exc}"
    tmp = ROOT / ".tmp"
    if not tmp.is_dir():
        return ".tmp: absent"
    # One deadline for the size scan, the ownership reads and the candidate planning; past it the figures are partial.
    deadline = time.monotonic() + config.scan_timeout_seconds
    total, complete = purge._size_bounded(tmp, deadline)
    gb = f"{total / 2**30:.2f} GB"
    size = gb if complete else f">= {gb} (partial: scan stopped after {config.scan_timeout_seconds:g} s)"
    items, planned = purge.plan_sweep_bounded(config.sweep_age_hours, refusals=False, deadline=deadline)
    if any(item.status == "unknown" for item in items):
        orphans = "unknown (ownership state unreadable)"
    else:
        count = sum(1 for item in items if item.status == "orphan")
        orphans = str(count) if planned else f">= {count} (partial: time bound reached)"
    return (
        f".tmp size {size}; orphans older than {config.sweep_age_hours:g} h: {orphans} "
        "(`purge_tmp.py sweep --dry-run` lists them)."
    )


def pending_block(index_text: str, from_decision: int, max_rows: int) -> str:
    rows = load_module(APPLIED_MODULE, "decisions_applied").scan(index_text, from_decision, ROOT)
    pending = [row.ident for row in rows if row.state == "pending"]
    bad = [row.ident for row in rows if row.state not in ("applied", "pending")]
    lines = []
    if pending:
        shown = ", ".join(pending[:max_rows])
        more = f" (+{len(pending) - max_rows} more)" if len(pending) > max_rows else ""
        lines.append(f"Decisions decided but not yet applied (`applied: pending`): {shown}{more}.")
    if bad:
        lines.append(
            f"{len(bad)} decision row(s) from {from_decision} on have no valid `applied:` marker "
            f"(first: {bad[0]}); run tools/agent-workflow/decisions_applied.py check."
        )
    return "\n".join(lines)


def settled_block(path: Path, head_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return f"Settled-decision index {SETTLED_NAME} missing: run `recall.py build --settled`."
    if len(text) > head_chars:
        text = text[:head_chars]
        text = text[: text.rfind("\n")] if "\n" in text else text
    return f"Settled decisions (head of .tmp/pm-state/{SETTLED_NAME}):\n{text}"


def quota_block(now: float) -> str:
    """One line per entry of the `blocks` list in `usage.quota_blocks_file` (written by usage_snapshot.py and the PM):
    provider, window, reset time, and whether that time has passed or is no time at all. The file is judged by the
    one validator of the stored format (`usage_snapshot.stored_blocks`), and this function branches on its tag:
    empty text only for ABSENT (`read_quota_file` proved there is no file: missing while the writers' lock is held)
    and for a VALID file without blocks; a MALFORMED file prints every block it still holds plus one `unreadable`
    line naming each unreadable part; a file that cannot be read, a busy lock included, is `unreadable` too."""
    try:
        settings = load_module(BACKEND_MODULE, "codex_backend").load_usage_settings(USAGE_CONFIG)
        relative = settings["quota_blocks_file"]
    except (Exception, SystemExit) as exc:  # a refused config is shown, never raised
        return f"not configured: usage.quota_blocks_file ({str(exc)[:200]})"
    try:
        # The same contained path as the writers: under .tmp/pm-state, refused when a link or junction lies on it.
        snapshot = load_module(SNAPSHOT_MODULE, "usage_snapshot")
        path = snapshot.state_path(relative, ROOT)
    except (Exception, SystemExit) as exc:
        return f"Quota blocks file {relative} refused: {str(exc)[:200]}"
    try:
        # Under the writers' lock: None is a proven absence, which the validator tags ABSENT.
        stored = snapshot.stored_blocks(snapshot.read_quota_file(path))
    except (Exception, SystemExit) as exc:
        return f"Quota blocks file {relative} unreadable: {type(exc).__name__}: {str(exc)[:160]}"
    if stored.tag == snapshot.ABSENT:
        return ""
    lines = []
    for block in stored.blocks:
        resets = block.get("resets_at")
        ends = snapshot.parse_time(resets)  # the tool's one parser: a time without a UTC offset is no time
        if ends is None:
            passed = " (no valid reset time: set `resets_at` with its UTC offset through usage_snapshot.py pm-set)"
        elif ends < now:
            passed = " (reset time passed: the next usage_snapshot.py run drops the block)"
        else:
            passed = ""
        used = block.get("used_percent")
        used_text = f", {used}% used" if isinstance(used, (int, float)) else ""
        lines.append(
            f"- {block.get('provider')} {block.get('window')} blocked until {resets}{used_text}{passed}"
        )
    parts = [f"Quota blocks ({relative}; PM_POLICY section 1):\n" + "\n".join(lines)] if lines else []
    if stored.tag == snapshot.MALFORMED:
        parts.append(
            f"Quota blocks file {relative} unreadable: {'; '.join(stored.problems)[:300]} "
            "(the blocks are not fully known; repair the file by hand)"
        )
    return "\n".join(parts)  # empty only for a valid file without blocks


def assemble(blocks: list[tuple[str, str]], limit: int) -> str:
    """Join the (label, text) blocks in priority order into at most `limit` characters.

    A block that does not fit is cut at the space left with an inline marker, or omitted (a head under MIN_CUT_KEEP
    characters is useless) and named in one final notice, for which `NOTICE_RESERVE` characters stay free.
    """
    reserve = min(NOTICE_RESERVE, limit // 4)
    room = limit - reserve
    out: list[str] = []
    used = 0
    omitted: list[str] = []
    for label, raw in blocks:
        text = EMAIL.sub("[email]", raw).strip()
        if not text:
            continue
        sep = 2 if out else 0
        free = room - used - sep
        if len(text) <= free:
            out.append(text)
            used += len(text) + sep
            continue
        marker = f"\n[cut: {label} over the injection budget]"
        keep = free - len(marker)
        if keep >= MIN_CUT_KEEP:
            out.append(text[:keep].rstrip() + marker)
            used += keep + len(marker) + sep
        else:
            omitted.append(label)
    if omitted:
        notice = f"[not injected, injection budget: {', '.join(omitted)}]"[: max(reserve - 2, 0)]
        if notice:
            out.append(notice)
    return "\n\n".join(out)


def main(argv: list[str] | None = None) -> int:
    requested = parse_options(argv or [])
    try:
        event = json.loads(read_event() or "{}")
    except ValueError:
        event = {}
    if not isinstance(event, dict) or is_worker(event.get("session_id")):
        return 0
    transcript = event.get("transcript_path")
    memory_dir = (
        Path(transcript).parent / "memory" if isinstance(transcript, str) and transcript else None
    )
    try:
        settings: Any = load_module(CONFIG_MODULE, "memory_hooks_config").load(CONFIG)
    except Exception as exc:  # a broken loader must not drop the agreements
        settings = None
        notes = [f"memory_hooks_config unusable: {type(exc).__name__}"]
    else:
        notes = settings.notes()

    def setting(kind: str, field: str) -> Any:
        if settings is None:
            return None
        try:
            return getattr(settings, kind)(field)
        except (
            Exception
        ) as exc:  # NotConfigured: the dependent block is skipped, the problem is shown
            notes.append(str(exc))
            return None

    blocks: list[tuple[str, str]] = []
    source = event.get("source", "?")
    headings = setting("tuple_", "inject_headings") or FALLBACK_HEADINGS
    note_chars = setting("int_", "note_inject_chars") or FALLBACK_NOTE_CHARS
    total = setting("int_", "total_inject_chars")
    if total is None:
        # Protocol-bound ceiling (memory_hooks_config.HARD_INJECT_CAP): the harness shows nothing beyond it.
        total = 10000
    room = total - min(NOTICE_RESERVE, total // 4)
    if note_chars > room:
        notes.append(
            f"note_inject_chars {note_chars} is above the room in total_inject_chars ({room}): "
            "the note is cut by the total budget"
        )
    try:
        # utf-8-sig: an editor (Notepad, PowerShell 5.1 `Set-Content`) may have saved the note with a BOM, which
        # would otherwise glue to the first heading; errors="replace": a stray byte must not drop the agreements.
        sections = note_sections(
            (STATE / NOTE_NAME).read_text(encoding="utf-8-sig", errors="replace"),
            headings,
            note_chars,
        )
    except OSError:
        sections = ""
    blocks.append(
        (
            "continuation note",
            f"Session start ({source}). The PM continuation note .tmp/pm-state/{NOTE_NAME} records these "
            f"user agreements and current state:\n{sections}"
            if sections
            else f"Session start ({source}). The PM continuation note has no agreements or NOW section.",
        )
    )
    try:
        quota_text = quota_block(time.time())
    except Exception as exc:  # never blocks the session start
        quota_text = f"Quota blocks check failed: {type(exc).__name__}"
    blocks.append(("quota blocks", quota_text))
    index_text = ""
    try:
        index_text = (ROOT / DECISIONS_REL).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        index_text = ""  # missing or unreadable index: the agreements and budgets still go out
    from_decision = setting("int_", "applied_from_decision")
    max_pending = setting("int_", "pending_rows_max")
    pending = ""
    if index_text and from_decision is not None and max_pending is not None:
        try:
            pending = pending_block(index_text, from_decision, max_pending)
        except Exception as exc:
            pending = f"Pending-decision check failed: {type(exc).__name__}"
    branch = setting("str_", "batch_branch")
    git_args = (
        branch,
        setting("int_", "git_timeout_seconds"),
        setting("int_", "max_branch_lines"),
        setting("int_", "max_worktree_lines"),
    )
    git_text = ""
    if all(value is not None for value in git_args):
        try:
            git_text = git_block(*git_args)
        except Exception as exc:
            git_text = f"Unmerged-branch check failed: {type(exc).__name__}"
    head_chars = setting("int_", "settled_head_chars")
    settled = settled_block(STATE / SETTLED_NAME, head_chars) if head_chars is not None else ""
    newest = ""
    default_count = setting("int_", "decisions_default")
    max_count = setting("int_", "decisions_max")
    text_chars = setting("int_", "decision_text_chars")
    if index_text and None not in (default_count, max_count, text_chars):
        count = min(max(default_count if requested is None else requested, 0), max_count)
        try:
            newest = decision_index(index_text, count, text_chars) if count else ""
        except Exception:
            newest = ""
    try:
        budgets = over_budget(memory_dir)
    except (
        Exception
    ) as exc:  # the agreements above must reach the session even if the budget check breaks
        budgets = [f"budget check failed: {type(exc).__name__}"]
    try:
        tmp_text = tmp_line()
    except Exception as exc:  # never blocks the session start
        tmp_text = f".tmp size check failed: {type(exc).__name__}"
    blocks += [
        ("configuration problems", "\n".join(dict.fromkeys(notes))),
        ("pending decisions", pending),
        ("branches and worktrees", git_text),
        ("tmp cleanup", tmp_text),
        ("settled decisions", settled),
        ("newest decisions", newest),
        (
            "document budgets",
            "Live documents over their size budget (decision 165): " + "; ".join(budgets) + "."
            if budgets
            else "",
        ),
    ]
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": assemble(blocks, total),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
