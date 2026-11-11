# N072 Effective harnesses for long-running agents

- **Source:** https://www.anthropic.com/research/effective-harnesses-for-long-running-agents
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

An initializer creates `init.sh`, a progress file, a git commit and a JSON feature list with every `passes` false; the agent may change only `passes`; one feature at a time with end-to-end verification. JSON is chosen because models are less likely to overwrite it. Failures: one-shotting, premature victory, unverified completion.

## Caveats

-
