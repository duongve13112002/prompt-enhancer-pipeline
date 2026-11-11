"""Context-budget audit for `claude -p` worker sessions (read-only; changes nothing).

Reads the first `system/init` event and the first assistant `usage` of a worker stream-json log (written by
cli_worker.py under .tmp/cli-jobs/<worker>/NNN.jsonl) and reports what a fresh worker session loads (MCP servers,
plugins, skills, agents, tools), the measured first-call context size, an approximate token cost per plugin from the
skill and agent descriptions on disk (about 4 characters per token; an assumption, not a measurement), and candidates
to disable. The init event carries names only, so the per-item costs are estimates. Turning anything off is the
user's decision; this script never edits settings.

Usage: python -B -I context_audit.py [--worker NAME | --log FILE] [--out report.md] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
JOBS = ROOT / ".tmp" / "cli-jobs"
CHARS_PER_TOKEN = 4


def find_log(worker: str | None, log: str | None) -> Path:
    if log:
        return Path(log)
    if worker:
        return JOBS / worker / "001.jsonl"
    candidates = sorted(JOBS.glob("*/001.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        if first_events(path)[0] is not None:
            return path
    raise SystemExit(f"no worker log with an init event under {JOBS}")


def first_events(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    init = usage = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if init is None and m.get("type") == "system" and m.get("subtype") == "init":
                init = m
            elif init is not None and m.get("type") == "assistant":
                usage = m.get("message", {}).get("usage") or {}
                break
    return init, usage


def frontmatter_description(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:6000]
    except OSError:
        return None
    m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
    if not m:
        return None
    lines = m.group(1).splitlines()
    for i, line in enumerate(lines):
        d = re.match(r"description:\s*(.*)", line)
        if not d:
            continue
        value = d.group(1).strip()
        if value in (">", "|", ">-", "|-", ""):
            block = []
            for nxt in lines[i + 1 :]:
                if nxt and not nxt[0].isspace():
                    break
                block.append(nxt.strip())
            value = " ".join(block)
        return value.strip("\"'")
    return None


def locate(kind: str, name: str, plugin_paths: dict[str, Path], cwd: Path) -> Path | None:
    plugin, _, short = name.rpartition(":")
    bases = (
        [plugin_paths[plugin]] if plugin in plugin_paths else [cwd / ".claude", ROOT / ".claude"]
    )
    for base in bases:
        options = (
            [base / "skills" / short / "SKILL.md", base / "commands" / f"{short}.md"]
            if kind == "skill"
            else [base / "agents" / f"{short}.md"]
        )
        for option in options:
            if option.is_file():
                return option
    return None


def estimate(
    kind: str, names: list[str], plugin_paths: dict[str, Path], cwd: Path
) -> tuple[dict[str, int], int]:
    """Per-name token estimates; names whose file is not found get the average of the resolved ones."""
    resolved: dict[str, int] = {}
    unresolved = 0
    for name in names:
        path = locate(kind, name, plugin_paths, cwd)
        desc = frontmatter_description(path) if path else None
        if desc is None:
            unresolved += 1
            continue
        resolved[name] = max(1, (len(name) + len(desc) + 4) // CHARS_PER_TOKEN)
    average = round(sum(resolved.values()) / len(resolved)) if resolved else 60
    return {n: resolved.get(n, average) for n in names}, unresolved


def usage_evidence(limit: int) -> tuple[Counter[str], Counter[str]]:
    """Skill and MCP-tool use counted over the newest worker logs."""
    skills: Counter[str] = Counter()
    mcp: Counter[str] = Counter()
    files = sorted(JOBS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"tool_use"' not in line:
                        continue
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    for c in (
                        (m.get("message", {}).get("content") or [])
                        if m.get("type") == "assistant"
                        else []
                    ):
                        if c.get("type") != "tool_use":
                            continue
                        if c.get("name") == "Skill":
                            skills[str((c.get("input") or {}).get("skill", ""))] += 1
                        elif str(c.get("name", "")).startswith("mcp__"):
                            mcp[c["name"]] += 1
        except OSError:
            continue
    return skills, mcp


def plugin_of(skill: str) -> str:
    return skill.partition(":")[0] if ":" in skill else ""


def build(log: Path, log_limit: int) -> dict[str, Any]:
    init, usage = first_events(log)
    if init is None:
        raise SystemExit(f"{log}: no system/init event")
    cwd = Path(init.get("cwd") or ROOT)
    plugins = {
        p["name"]: Path(p["path"])
        for p in init.get("plugins", [])
        if p.get("path") not in (None, "builtin")
    }
    skills = list(init.get("skills", []))
    agents = list(init.get("agents", []))
    skill_cost, skill_unres = estimate("skill", skills, plugins, cwd)
    agent_cost, agent_unres = estimate("agent", agents, plugins, cwd)
    mcp = init.get("mcp_servers", [])
    tools = list(init.get("tools", []))
    mcp_tools = [t for t in tools if t.startswith("mcp__")]
    used_skills, used_mcp = usage_evidence(log_limit)

    per_plugin: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"skills": 0, "agents": 0, "tokens": 0, "used": 0, "mcp": [], "mcp_used": 0}
    )
    for s in skills:
        p = plugin_of(s)
        if p:
            per_plugin[p]["skills"] += 1
            per_plugin[p]["tokens"] += skill_cost[s]
            per_plugin[p]["used"] += used_skills.get(s, 0)
    for a in agents:
        p = plugin_of(a)
        if p:
            per_plugin[p]["agents"] += 1
            per_plugin[p]["tokens"] += agent_cost[a]
    for server in mcp:
        name = server["name"]
        if name.startswith("plugin:"):
            p = name.split(":")[1]
            per_plugin[p]["mcp"].append(f"{name.split(':', 2)[2]} ({server['status']})")
            key = "mcp__plugin_" + p + "_"
            per_plugin[p]["mcp_used"] += sum(n for t, n in used_mcp.items() if t.startswith(key))
    for p in plugins:
        per_plugin[p]  # plugins that ship no skills or agents still appear

    claude_md = [
        (str(p), p.stat().st_size // CHARS_PER_TOKEN)
        for p in {cwd / "CLAUDE.md", ROOT / "CLAUDE.md"}
        if p.is_file()
    ]
    measured = None
    if usage:
        measured = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
    est_skills, est_agents = sum(skill_cost.values()), sum(agent_cost.values())
    status = Counter(s["status"] for s in mcp)

    unusable = [s["name"] for s in mcp if s["status"] in ("failed", "needs-auth", "pending")]
    candidates = []
    for p, d in per_plugin.items():
        if d["used"] == 0 and d["mcp_used"] == 0:
            candidates.append(
                {
                    "plugin": p,
                    "est_tokens": d["tokens"],
                    "skills": d["skills"],
                    "agents": d["agents"],
                    "mcp": d["mcp"],
                }
            )
    candidates.sort(key=lambda c: c["est_tokens"], reverse=True)
    return {
        "log": str(log),
        "cwd": str(cwd),
        "claude_code_version": init.get("claude_code_version"),
        "model": init.get("model"),
        "measured_first_call_tokens": measured,
        "claude_md_files": claude_md,
        "tools": {"total": len(tools), "mcp": len(mcp_tools), "mcp_names": mcp_tools},
        "mcp_servers": {
            "total": len(mcp),
            "by_status": dict(status),
            "unusable_headless": unusable,
        },
        "plugins": {
            "total": len(init.get("plugins", [])),
            "per_plugin": dict(per_plugin),
        },
        "skills": {
            "total": len(skills),
            "est_tokens": est_skills,
            "unresolved_descriptions": skill_unres,
        },
        "agents": {
            "total": len(agents),
            "est_tokens": est_agents,
            "unresolved_descriptions": agent_unres,
        },
        "slash_commands": len(init.get("slash_commands", [])),
        "usage_logs_scanned": log_limit,
        "most_used_skills": used_skills.most_common(8),
        "candidates": candidates,
        "candidate_tokens": sum(c["est_tokens"] for c in candidates),
    }


def render(r: dict[str, Any]) -> str:
    out = [
        f"# Context-budget audit ({r['model']}, Claude Code {r['claude_code_version']})",
        f"Source log: `{r['log']}`; cwd `{r['cwd']}`. Read-only report; nothing was changed.",
        "",
        "## Measured",
        f"- First assistant call context (input + cache read + cache creation): **{r['measured_first_call_tokens']}** tokens.",
        *[f"- {p}: about {t} tokens (size/4)" for p, t in r["claude_md_files"]],
        "",
        "## Loaded by a new worker session (names from the init event; costs are estimates)",
        f"- Tools: {r['tools']['total']} ({r['tools']['mcp']} MCP tools: {', '.join(r['tools']['mcp_names']) or 'none'})",
        f"- MCP servers: {r['mcp_servers']['total']} {r['mcp_servers']['by_status']}",
        f"- Plugins: {r['plugins']['total']}; skills: {r['skills']['total']} (about {r['skills']['est_tokens']} tokens of listing, "
        f"{r['skills']['unresolved_descriptions']} descriptions not found on disk, averaged); "
        f"agents: {r['agents']['total']} (about {r['agents']['est_tokens']} tokens, {r['agents']['unresolved_descriptions']} averaged); "
        f"slash commands: {r['slash_commands']}",
        "",
        "## Candidates to disable (report only; the user decides)",
        f"- {len(r['mcp_servers']['unusable_headless'])} MCP servers are failed, need auth or pending and cannot work in a headless worker.",
        f"- {len(r['candidates'])} plugins have no recorded skill or MCP use in the newest {r['usage_logs_scanned']} worker logs; "
        f"together about **{r['candidate_tokens']}** tokens of skill and agent descriptions (estimate).",
        "",
        "| Plugin | est. tokens | skills | agents | MCP servers (status) |",
        "|---|---|---|---|---|",
    ]
    for c in r["candidates"]:
        out.append(
            f"| {c['plugin']} | {c['est_tokens']} | {c['skills']} | {c['agents']} | {', '.join(c['mcp']) or '-'} |"
        )
    out += [
        "",
        "Most used skills in the scanned logs: "
        + (", ".join(f"{n} x{c}" for n, c in r["most_used_skills"]) or "none"),
        "",
        "## Notes",
        "- `claude --help` documents `--strict-mcp-config` (only `--mcp-config` servers) and `--disable-slash-commands` (all skills); "
        "the latter also removes the project skills workers use (config-audit, handoff-report).",
        "- A per-session `enabledPlugins` override through `--settings` is not verified; check before relying on it.",
        "- Token figures from descriptions use 4 characters per token (assumption); MCP server instructions, built-in tool schemas and "
        "the system prompt are part of the measured total but are not itemised (the init event does not carry them).",
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worker")
    ap.add_argument("--log")
    ap.add_argument(
        "--usage-logs",
        type=int,
        default=300,
        help="newest worker logs scanned for skill and MCP use",
    )
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    report = build(find_log(a.worker, a.log), a.usage_logs)
    text = json.dumps(report, indent=1) if a.json else render(report)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
