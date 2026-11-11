# N061 Claude Code subagents

- **Source:** https://code.claude.com/docs/en/sub-agents
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Required frontmatter `name` and `description`; optional `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `skills`, `hooks`, `isolation: worktree`, `omitClaudeMd`. Unknown keys are ignored silently. Model order: per-invocation argument, frontmatter, `CLAUDE_CODE_SUBAGENT_MODEL`, main model. Explore and Plan subagents do not load CLAUDE.md. Subagents see none of the parent conversation. Default nesting 3 layers, 20 concurrent.

## Caveats

Whether the full model ID is accepted in `model:` is unverified; use the `haiku` alias.
