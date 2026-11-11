# N073 Writing effective tools for agents

- **Source:** https://www.anthropic.com/engineering/writing-tools-for-agents
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Evaluate tools before trusting them; namespace them; return high-signal output with pagination and truncation; write errors that say how to fix the call.

## Caveats

-
