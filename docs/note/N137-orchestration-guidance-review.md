# N137 Public guidance on multi-agent coding workflows, compared with this repository

- **Source:** https://code.claude.com/docs/en/best-practices ; https://code.claude.com/docs/en/costs ; https://code.claude.com/docs/en/prompt-caching ; https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents (2025-11-26) ; https://www.anthropic.com/engineering/multi-agent-research-system (2025-06-13) ; https://www.humanlayer.dev/blog/writing-a-good-claude-md (2025-11-25) ; github.com/obra/superpowers (subagent-driven development, main, read 2026-11-13) ; github.com/github/spec-kit (tasks template, main) ; github.com/bmad-code-org/BMAD-METHOD (README, main) ; github.com/humanlayer/12-factor-agents (factor 5, main)
- **Class:** documented
- **Verified by:** an independent review agent on 2026-11-13; the PM spot-checked every repository-side quote against the files, not the web quotes
- **Checked:** 2026-11-13
- **Used in:** decisions 44 to 48, .agents/ORCHESTRATION.md, .agents/policy/PM_POLICY.md, AGENTS.md, tasks T-024 to T-026

## What this project relies on

- One fresh worker per task: "Fresh subagent per task (no context pollution) instead of one context doing every task" (superpowers); "`/clear` between unrelated tasks" (Claude Code best practices); "Two sessions in different directories therefore build different prefixes and miss each other's cache" (prompt caching), so moving a warm worker to a new worktree loses its cache anyway.
- One shared subscription window: a limit is "a seat-based usage window on a subscription plan, shared across all models" (costs); subagents' requests "count against the same usage as your main conversation" (best practices).
- Parallel work on separate files: "[P] tasks = different files, no dependencies" (spec-kit); "Two teammates editing the same file leads to overwrites" (Claude Code agent teams).
- Fewer, sharper rules: "Frontier thinking LLMs can follow ~ 150-200 instructions with reasonable consistency" (HumanLayer); "If something has to happen every time, make it a hook, a deny rule, or a managed setting" (best practices).
- Size the process to the task: "Small changes go straight to build. Complex work gets the depth it needs." (BMAD).
- One source of truth for state: "If possible, SIMPLIFY - unify these as much as possible." (12-factor agents, factor 5).
- Recovery points: "This allowed the model to use git to revert bad code changes and recover working states of the code base." (Anthropic harnesses); with no agent commits, saved patches take that role here.

## Caveats

The web quotes were extracted by the review agent and not re-read by the PM. The "150-200 instructions" figure is one author's estimate, used here as a direction, not a limit.
