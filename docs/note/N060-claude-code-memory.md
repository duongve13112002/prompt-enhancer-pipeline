# N060 Claude Code memory (CLAUDE.md)

- **Source:** https://code.claude.com/docs/en/memory
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Locations: managed policy, `~/.claude/CLAUDE.md`, `./CLAUDE.md` or `./.claude/CLAUDE.md`, `CLAUDE.local.md`; nested directory files load on demand. `@import` is relative, at most 4 hops, skipped inside code spans and fences. Target under 200 lines per file; imports do not reduce context cost. CLAUDE.md is 'context, not enforced configuration'. AGENTS.md is read natively (v2.1.277 or newer) only when no CLAUDE.md or CLAUDE.local.md exists in the working directory or above.

## Caveats

This repository keeps CLAUDE.md and AGENTS.md byte-identical on purpose (CLAUDE.md header rule).
