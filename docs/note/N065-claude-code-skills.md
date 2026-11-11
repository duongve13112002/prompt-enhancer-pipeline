# N065 Claude Code skills

- **Source:** https://code.claude.com/docs/en/skills
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

`.claude/skills/<name>/SKILL.md` with frontmatter `description`, `disable-model-invocation`, `allowed-tools`, `model`, `context: fork`. Skills load on demand while CLAUDE.md loads every session.

## Caveats

The three skills in `.agents/skills/` live outside `.claude/skills/`; check how they are loaded before relying on them.
