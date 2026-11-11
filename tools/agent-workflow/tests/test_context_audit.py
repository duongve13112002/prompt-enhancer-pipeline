"""Unit tests for context_audit.py on a synthetic worker log."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "context_audit.py"
spec = importlib.util.spec_from_file_location("context_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def make_log(tmp: Path) -> Path:
    plugin = tmp / "plug" / "skills" / "alpha"
    plugin.mkdir(parents=True)
    (plugin / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: >\n  Does alpha things\n  over two lines\n---\nbody\n",
        encoding="utf-8",
    )
    init = {
        "type": "system",
        "subtype": "init",
        "cwd": str(tmp),
        "model": "m",
        "claude_code_version": "0",
        "tools": ["Bash", "mcp__x__y"],
        "skills": ["plug:alpha", "plug:ghost"],
        "agents": ["plug:bot"],
        "plugins": [
            {"name": "plug", "path": str(tmp / "plug")},
            {"name": "unused", "path": str(tmp / "unused")},
        ],
        "mcp_servers": [
            {"name": "plugin:plug:srv", "status": "needs-auth"},
            {"name": "claude.ai Docs", "status": "connected"},
        ],
        "slash_commands": ["a", "b"],
    }
    assistant = {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": 1,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 300,
            },
            "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "plug:alpha"}}],
        },
    }
    log = tmp / "jobs" / "w" / "001.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(json.dumps(m) for m in (init, assistant)) + "\n", encoding="utf-8")
    return log


def test_frontmatter_block_scalar_and_report(tmp_path: Path, monkeypatch) -> None:
    log = make_log(tmp_path)
    monkeypatch.setattr(audit, "JOBS", tmp_path / "jobs")
    desc = audit.frontmatter_description(tmp_path / "plug" / "skills" / "alpha" / "SKILL.md")
    assert desc == "Does alpha things over two lines"
    report = audit.build(log, 10)
    assert report["measured_first_call_tokens"] == 321
    assert report["tools"] == {"total": 2, "mcp": 1, "mcp_names": ["mcp__x__y"]}
    assert report["skills"]["total"] == 2 and report["skills"]["unresolved_descriptions"] == 1
    assert report["mcp_servers"]["unusable_headless"] == ["plugin:plug:srv"]
    names = [c["plugin"] for c in report["candidates"]]
    assert "plug" not in names  # its skill was used in the log
    assert "unused" in names
    text = audit.render(report)
    assert "Candidates to disable" in text and "unused" in text
