# Agent setup: research and design choices

Why the agent setup looks the way it does. Sources are research notes in [docs/note/](../note/README.md) (N060 to N079, N093 to N097); most were read by research agents and not re-read by the PM (class `documented`). Sections 1 and 2 were written for the project this tooling came from and are kept as the reasons behind the carried setup; section 0 lists what changed for this repository.

## 0. Changes made on 2026-11-11 (decision 24)

Vendor guidance, not measurements on this workflow; improvement H measures them.

| Change | Reason | Evidence |
| --- | --- | --- |
| A. Core rules in `AGENTS.md`, role rules in `.agents/policy/roles/` | Fewer simultaneous instructions per worker; `agy` truncates a rule file above 24,000 bytes | [N130](../note/N130-antigravity-rules-and-headless.md), [N077](../note/N077-ifscale.md) |
| B. Context first, task-specific instruction last (waits for task T-021) | Two vendors advise placing the instruction after the context | [N127](../note/N127-claude-prompting-best-practices.md), [N129](../note/N129-gemini-prompting-guidance.md) |
| C. Rules say what to do and why; plain wording | Emphatic wording makes current models over-apply a rule; a stated reason generalises | [N127](../note/N127-claude-prompting-best-practices.md) |
| D. One wrapper per CLI | The vendors document different defaults: one model family proceeds and assumes, another asks for instructions last and explicit grounding | [N127](../note/N127-claude-prompting-best-practices.md), [N128](../note/N128-codex-prompting-guidance.md), [N129](../note/N129-gemini-prompting-guidance.md) |
| E. Every claim carries an evidence pointer; quote first | Grounding instructions and quote-first extraction are the documented ways to reduce invented content | [N127](../note/N127-claude-prompting-best-practices.md), [N129](../note/N129-gemini-prompting-guidance.md), [N079](../note/N079-agent-provenance-failures.md) |
| F. Scope guard and report lint as tools (tasks T-021) | A rule that must hold needs a hook | [N066](../note/N066-claude-code-best-practices.md) |
| G. Escalation order: self-verification, then effort, then model | The model-selection guide's order | [N093](../note/N093-claude-model-selection.md) |
| H. Measure violations and tokens per accepted task before and after | No source gives an effect size for this workflow | [N097](../note/N097-agent-focus-and-drift.md) |

## 1. Choices made on 2026-11-10

| Choice | Reason | Evidence |
| --- | --- | --- |
| `AGENTS.md` is the single rule file; `CLAUDE.md` holds `@AGENTS.md` plus Claude-only notes | Codex and Antigravity read `AGENTS.md`; Claude Code reads `CLAUDE.md` and follows the import; two byte-identical copies drifted and cost double context | [N096](../note/N096-agent-instruction-files.md), [N060](../note/N060-claude-code-memory.md) |
| Skills canonical in `.agents/skills/`, copied to `.claude/skills/` by `setup_project.py apply` | Codex and agy read `.agents/skills/`; Claude Code reads `.claude/skills/`; a copy works on Windows without symlink rights | N096, [N065](../note/N065-claude-code-skills.md) |
| One binding file `.agents/project.json` plus `setup_project.py check/apply` | The tools derive the root from their location; only a few values differ per repository; a checker finds stale bindings instead of a manual inventory | this refactor |
| Models per role across three subscriptions, quota checked before each round | The user runs Claude Code, Codex and agy on subscriptions with 5-hour and weekly limits; cheaper models for reading, capable ones for edits and judgment | [N093](../note/N093-claude-model-selection.md), [N094](../note/N094-openai-codex-models.md), [N095](../note/N095-gemini-agy-models.md) |
| Focus rules: goal anchored outside chat, Out of scope and step budget in briefs, phases, condensed returns, done only by a check | Most multi-agent failures are unclear stop conditions, spec drift and weak verification; quality falls with step count | [N097](../note/N097-agent-focus-and-drift.md) |

## 2. Earlier findings (2026-11-09)

| Finding | Source | Implication for this repository |
| --- | --- | --- |
| CLAUDE.md is "context, not enforced configuration"; rules that must hold need a hook; prose lines that are still missed belong in a hook | [N060](../note/N060-claude-code-memory.md), [N066](../note/N066-claude-code-best-practices.md) | Enforce the few hard rules by tool: `setup_project.py check`, `fit_from_gt` refusal (T-005), brief lint |
| Claude Code reads AGENTS.md only when no CLAUDE.md exists above the working directory; Codex concatenates root to leaf and drops content past 32 KiB; Gemini CLI defaults to GEMINI.md | [N060](../note/N060-claude-code-memory.md), [N068](../note/N068-codex-agents-md.md), [N067](../note/N067-agents-md.md) | Superseded: one AGENTS.md (about 16K characters) imported by CLAUDE.md |
| Target under 200 lines per CLAUDE.md; longer files reduce adherence; imports do not cut cost | [N060](../note/N060-claude-code-memory.md) | AGENTS.md was condensed from 27K to about 16K characters; do not add rules without a recorded failure |
| Subagents start with a fresh context and see nothing of the parent; Explore and Plan subagents load no CLAUDE.md; a per-call `model` overrides a pinned frontmatter model | [N061](../note/N061-claude-code-sub-agents.md) | Put the needed rules in every brief; the sibling policy uses `claude -p` workers through `cli_worker.py` instead of the Agent tool |
| Exit code 2 blocks only on PreToolUse, UserPromptSubmit, Stop and a few others, not PostToolUse; `@file` mentions skip PreToolUse; an invalid hook is skipped with a warning | [N062](../note/N062-claude-code-hooks.md), [N063](../note/N063-claude-code-settings-permissions.md) | Test every hook with a deliberate violation (a hook that never fires looks like a pass); deny `.secrets` with a Read rule |
| `claude -p` loads CLAUDE.md, settings and hooks; `--bare` skips them; settings.json errors skip the file silently in `-p` | [N064](../note/N064-claude-code-headless.md), [N063](../note/N063-claude-code-settings-permissions.md) | Run `claude doctor` after editing settings; never start workers with `--bare` |
| Multi-agent systems use about 15 times the tokens of chat and are a poor fit for tightly coupled coding; briefs need objective, output format, sources, boundaries | [N071](../note/N071-anthropic-multi-agent-research.md), [N070](../note/N070-anthropic-building-effective-agents.md) | Keep the sibling rule: PM does small work itself, delegates only for real parallelism, one agent per related batch |
| Long-running work needs a progress file, a feature list the agent may only mark, and one feature at a time | [N072](../note/N072-anthropic-long-running-harness.md) | [STATUS](../../tasks/STATUS.md) and the plan table play this role; workers never edit PM-owned files (footer rule 2) |
| Models cannot reliably self-correct without external feedback and favour their own output | [N074](../note/N074-self-correction-limits.md), [N075](../note/N075-self-preference.md) | A reviewer is always a different, fresh agent and must reproduce findings; keep the sibling single-pass review rule |
| Fabricated references and plausible-but-wrong results are a main failure; verify against the source | [N079](../note/N079-agent-provenance-failures.md) | R-021 and the note classes; the first research round already contained three wrong numbers (an audit recorded in the project this text came from) |
| Spec-driven workflows have no measured benefit in the sources found | [N076](../note/N076-spec-kit.md) | Keep the Step 0 plan gate, but track review rejections to see whether it helps |
| Instruction-following degrades with many simultaneous instructions (synthetic test); information in the middle of long input is used worse | [N077](../note/N077-ifscale.md), [N078](../note/N078-lost-in-the-middle.md) | Evidence is thin for about ten rules; the response is to start with few rules and add one only with a failure on record |

Pitfalls to remember (all from N060 to N066): an Edit or Write hook does not stop shell writes; a Bash allow rule is text matching, not a boundary; project `allow` rules wait for workspace trust on a fresh clone; a `CLAUDE.local.md` in the working directory or above stops AGENTS.md being read; unknown subagent frontmatter keys are ignored silently, so smoke-test each definition; `--allowedTools "Bash(git diff*)"` without a space also matches `git diff-index`; `--tools` restricts which tools exist while `--allowedTools` only pre-approves.

