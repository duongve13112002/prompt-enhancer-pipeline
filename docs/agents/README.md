# How agents work here

Overview for people and agents. Rules: [AGENTS.md](../../AGENTS.md). Dispatch: [PM_POLICY](../../.agents/policy/PM_POLICY.md). Orchestration: [ORCHESTRATION](../../.agents/ORCHESTRATION.md). Tools: [tools/agent-workflow/README.md](../../tools/agent-workflow/README.md). Research: [research.md](research.md).

## Where things are

| Need | File |
| --- | --- |
| Rules every agent loads | `AGENTS.md` (Claude through `CLAUDE.md` -> `@AGENTS.md`; Codex and agy directly) |
| Project binding for the tooling | `.agents/project.json` |
| Models per role, quota, reviews, focus | `.agents/policy/PM_POLICY.md` |
| Assignment, state, cleanup | `.agents/ORCHESTRATION.md` |
| Brief template and footer | `.agents/policy/AGENT_TASK_TEMPLATE.md`, `AGENT_BRIEF_FOOTER.md` |
| Rules for one role; framing for one CLI | `.agents/policy/roles/`, `.agents/policy/cli/` |
| Where each older rule went | `docs/agents/rule-map.md` |
| The user's standing rules | `.agents/policy/USER_STANDING_RULES.md` |
| Skills (canonical; Claude copy in `.claude/skills/`) | `.agents/skills/` |
| Project state | `tasks/` (STATUS, REQUIREMENTS, plan, decisions, `active/` briefs) |

## Setup and repair

`setup_project.py check` verifies the binding, venv, instruction files, skills sync, CLIs, Claude hook settings, tool configs, Git-ignore entries, tokens of a previous project and dead links; `apply` repairs what it can. Procedure: skill [agent-setup](../../.agents/skills/agent-setup/SKILL.md).

## Roles

- **PM / orchestrator:** the agent you chat with (mode DELEGATE by default). Plans, writes briefs, delegates, reads every report, updates `tasks/STATUS.md`, proposes commit messages (the user commits and pushes).
- **Workers** (implementer, reviewer, scout, researcher, runner): Claude `claude -p`, Codex or agy sessions; one fresh worker per task, kept only for that task's fix rounds and phases; inside its scope; never commit; Step 0 first.
- **Cheap readers** (Haiku 5.5, `gpt-6-luna`, Gemini 3.8 Flash at `low`): search, digest logs and diffs, read web pages; their numbers are leads until a primary source is re-read.

## Quota

All three CLIs run on subscriptions with a 5-hour window and a weekly limit. Before each round the PM reads the usage; Codex and Antigravity are used until their quota runs out, Claude workers until 90% of Claude's window (the rest is the orchestrator's); a provider that reports no limit is not checked. When a provider runs out and its reset time is known, the PM schedules its own wake for that time and then resumes only work the user already approved (PM_POLICY section 1).

## Life of a task

1. The PM writes the brief from the template: Goal, Done check, Scope, Out of scope, Output cap, Step budget, research questions, configuration impact; appends the footer; `brief_lint.py` checks it.
2. The worker writes Step 0 (unknowns, at most 5 questions, plan with a checkable finish) and stops; the PM answers.
3. The worker builds, writes a progress line about every 8 tool calls, stops when the done check passes, and reports with the [handoff format](../../.agents/skills/handoff-report/SKILL.md).
4. A fresh agent of another family reviews once; the worker fixes the full list in one round (at most three counted rounds, then one question to the user).
5. The PM saves the accepted patch, applies it to the main working tree uncommitted, updates docs once, records decisions and proposes a commit message; the user commits.
6. The PM cleans up the worker, its worktree, its scratch and its chat history, and sweeps orphan `.tmp` trees (ORCHESTRATION section 4).

## Staying on target

Goal anchored in `## NOW` of the PM continuation note (re-injected by the SessionStart hook after every compaction); explicit Out of scope in every brief; phases of about 30 tool calls; condensed results, never raw transcripts; "done" only with a passing check. Details and evidence: PM_POLICY section 6, [N097](../note/N097-agent-focus-and-drift.md).

## Hard rules

Never: start implementation without an explicit user command; invent numbers or APIs; touch `.secrets/`; run Git writes as a worker; edit PM-owned files (`STATUS`, `PROJECT_CONTEXT`, `IMPLEMENTATION_PLAN`, `DECISIONS_INDEX`) unless the brief names the file and the edit.

## Cold read (R-028)

After documentation changes the PM starts fresh Haiku, Sonnet and Opus readers that see only the repository; they report what they understood, what was unclear and what they would ask. The PM fixes the text and relays the questions to the user (PM_POLICY section 8).
