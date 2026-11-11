# N068 Codex AGENTS.md loading

- **Source:** https://learn.chatgpt.com/docs/agent-configuration/agents-md
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** docs/agents/research.md
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

Codex walks from the git root to the working directory, concatenates files root to leaf, `AGENTS.override.md` wins per directory, and `project_doc_max_bytes` defaults to 32 KiB with content beyond it dropped.

## Caveats

Keep AGENTS.md under 32 KiB if Codex is used.
