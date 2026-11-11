@AGENTS.md

## Claude Code specifics

- The rules above come from `AGENTS.md`, the single rule source shared with Codex and Antigravity; edit that file, not this one. Check what loaded with `/memory`.
- Skills: the canonical copies live in `.agents/skills/`; Claude Code reads the synced copies in `.claude/skills/`. Never edit `.claude/skills/` by hand: change `.agents/skills/`, then run `python tools/agent-workflow/setup_project.py apply`.
- Hooks are registered in the Git-ignored `.claude/settings.local.json`, rendered by `setup_project.py apply` from `tools/agent-workflow/settings.example.json`; run `claude doctor` after it changes.
- Subagents through the Agent tool start with a fresh context and do not see these rules: put what they need in the brief.
