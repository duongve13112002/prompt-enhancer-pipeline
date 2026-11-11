# N130 Antigravity: rule files, modes, permissions, headless runs

- **Source:** https://antigravity.google/docs/rules ; https://antigravity.google/docs/cli/modes/ ; https://antigravity.google/docs/permissions?tab=cli ; https://antigravity.google/docs/cli/headless
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** AGENTS.md size budget, .agents/policy/cli/antigravity.md

## What this project relies on

Reads `AGENTS.md` or `GEMINI.md` in the workspace folder and its parents, and `.agents/rules/*.md` (a rule file without valid frontmatter is silently discarded). Rules are cumulative. A rule file over 24,000 bytes is truncated; global and always-on rules share a 20,000-token budget. Modes: `default`, `accept-edits`, `auto`, `plan`. Permission order: "Deny > Ask > Allow". Headless: `-p`; a tool action that needs approval it cannot obtain is soft-denied; `--output-format` text, json or stream-json.

## Caveats

Agent-read. Precedence between the two instruction file names is not defined on the page.
