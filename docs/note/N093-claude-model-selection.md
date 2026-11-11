# N093 Which Claude model to build with (Anthropic blog, 2026-11-09)

- **Source:** https://claude.dev/blog/which-claude-model-should-you-build-with/
- **Class:** documented
- **Verified by:** Research agent read the primary page (2026-11-10); the PM did not re-read it
- **Checked:** 2026-11-10
- **Used in:** .agents/policy/PM_POLICY.md section 2
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

- Generally available: Haiku 5.5, Sonnet 5.5, Opus 5.5, Fable 5.2 (Fable 5.1 is the previous Fable).
- Roles: Opus 5.5 at `medium` is the default for most coding and when unsure; Fable 5.2 for "major refactors and migrations, architecture planning, and leading many subagents", deep research and quantitative analysis; Sonnet 5.5 for "well-scoped everyday work, like a bug fix or a feature built against a spec" and, at `medium`, for repeated agent tasks "such as investigation, review, or drafting"; Haiku 5.5 for subagents that "search files, read logs, or summarize", classification and routing.
- Combining: "Keep code edits and judgment calls on the main model"; give small models only work "where a mistake is cheap to spot"; "keep the expensive model's context for the decisions, and let cheaper subagents do the verbose reading". An Opus 5.5 lead with Sonnet 5.5 workers cost about a third less than Opus alone at about the same score (internal defect-search benchmark).
- Effort before model: add self-verification, raise effort one level, then move up a model at a lower effort. Auto-checkable tasks: run at `low`, re-run only failures at `high` (SWE-bench Pro: about 97% for about $0.17 per task versus 95.3% for $0.29 all at `high`).
- Opus 5.5 effort on SWE-bench Pro: `medium` about 2.5 points below `high` at about 70% of its cost; `low` about 8 points below at about a third; `xhigh` about 1.4 points above at 2.5 times the cost.
- Fable 5.2 at `high` is about level with Opus 5.5 at `max` on FrontierCode 2.0 (45.3% vs 45.5%) for roughly half the cost per task: test Fable before raising Opus past `high`.
- API list prices per 1M tokens (input / output / cache read): Haiku 5.5 $0.10 / $0.50 / $0.01 up to a 100K prompt ($0.50 / $2.50 / $0.05 above); Sonnet 5.5 $2 / $10 / $0.10; Opus 5.5 $4 / $20 / $0.20; Fable 5.2 $8 / $40 / $0.20.
- Switching models loses the prompt cache; Fable 5.2 requires preserved thinking and moving from it to another model drops its reasoning.

## Caveats

- Benchmark figures are Anthropic's own; the page gives no API model ID strings (the IDs `claude-*-5-5` and `claude-fable-5-2` come from the session environment, not this page).
