# N071 How we built our multi-agent research system

- **Source:** https://www.anthropic.com/engineering/multi-agent-research-system
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Agents use about 4 times and multi-agent systems about 15 times the tokens of chat; +90.2% over single Opus 4 on an internal evaluation; poor fit where agents must share context ('most coding tasks fall into this group'); briefs need objective, output format, sources and tools, and boundaries.

## Caveats

Internal evaluation of a research task, not coding.
