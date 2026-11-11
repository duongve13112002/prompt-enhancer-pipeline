# N096 One instruction file and one skills folder for Claude Code, Codex and Antigravity

- **Source:** https://code.claude.com/docs/en/memory ; https://developers.openai.com/codex/skills ; https://antigravity.google/docs/rules ; https://travis.media/blog/claude-md-import-agents-md
- **Class:** assumption (web-search summaries; the official pages were not opened in full)
- **Verified by:** PM web search, 2026-11-10
- **Checked:** 2026-11-10
- **Used in:** CLAUDE.md, AGENTS.md, tools/agent-workflow/setup_project.py
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

- Claude Code reads `CLAUDE.md`; an `@AGENTS.md` line at its top imports `AGENTS.md` (imports resolve relative to the importing file, at most four hops; skipped inside code spans). A symlink also works but needs Administrator or Developer Mode on Windows. Check with `/memory`.
- Codex reads `AGENTS.md` and repository skills from `.agents/skills/<name>/SKILL.md` (frontmatter `name`, `description`).
- Antigravity reads `AGENTS.md` (or `GEMINI.md`, `.agents/rules/*.md`) and workspace skills from `.agents/skills/`.
- Claude Code loads project skills from `.claude/skills/` (N065), so the canonical `.agents/skills/` is copied there by `setup_project.py apply` and compared by `setup_project.py check`.

## Caveats

- Verify with `/memory` (Claude), and by asking Codex and agy which instruction files they loaded, after each CLI upgrade.
