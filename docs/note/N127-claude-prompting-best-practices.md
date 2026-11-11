# N127 Claude prompting best practices

- **Source:** https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** A web-fetch agent read the whole page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** AGENTS.md, .agents/policy/roles/, .agents/policy/cli/claude.md, docs/agents/research.md

## What this project relies on

"Tell Claude what to do instead of what not to do." Giving the reason behind an instruction helps: "Claude can generalize from the explanation." Long inputs: "Put long-form data at the top"; "Queries at the end can improve response quality by up to 30 percent in tests"; ask for quotes first. Over-triggering: "The fix is to soften any aggressive language." Coding: "Only make changes that are directly requested or clearly necessary"; "Never speculate about code you have not opened."; "Do not hard-code values or create solutions that only work for specific test inputs." Thinking: "Prefer general instructions over prescriptive steps." Subagents suit parallel or isolated work, not simple sequential tasks.

## Caveats

"Where a technique names a specific model, treat it as measured on that model and re-check it against your own evals".
