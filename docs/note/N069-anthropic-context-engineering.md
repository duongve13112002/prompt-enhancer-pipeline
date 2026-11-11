# N069 Effective context engineering for AI agents

- **Source:** https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Context is a finite resource with diminishing returns; use just-in-time retrieval, compaction and notes outside context; sub-agents return summaries of about 1,000 to 2,000 tokens; start minimal and add rules from observed failures.

## Caveats

-
