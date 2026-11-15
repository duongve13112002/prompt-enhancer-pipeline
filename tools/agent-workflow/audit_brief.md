[NEW TASK: AUDIT-<DATE>]
Kind: scout
Task file: .agents/policy/USER_STANDING_RULES.md

<!-- PM: copy to .tmp/pm-state/briefs/audit-<DATE>.msg.md, replace <DATE> (YYYY-MM-DD, from Get-Date) and <SINCE>,
     run audit_extract.py first, replace the FOOTER placeholder line with .agents/policy/AGENT_BRIEF_FOOTER.md (the task block
     stays last, brief order of T-021), then start with
     --profile scout --model claude-haiku-5-5 --effort high --cache-ttl 5m. Delete this comment block. -->

# Periodic audit, report only (standing rule S22)

You check whether the PM kept the user's rules, using the user's own words and the repository, not the PM's notes.
You change nothing. The PM verifies every "not met" you report, so give evidence a person can open.

## Inputs (read only)

1. `.tmp/audit/user-words-<DATE>.md`: what the user typed since <SINCE>. This is the only place you read the
   user's words. Never open a raw transcript `*.jsonl` and never open `.secrets/`.
2. `.agents/policy/USER_STANDING_RULES.md`: the rules to check, one per line `Sxx | rule | source | check`.
3. `tasks/DECISIONS_INDEX.md` (newest 15 rows; for a condensed row ending in "..." open the verbatim row in
   `tasks/archive/DECISIONS_INDEX-*`).
4. Artifacts: code, `configs/`, `tasks/`, `cli_worker.py status --all --json`, `## IN-FLIGHT` of `.tmp/pm-state/pm-continuation.md`, `.agents/log.md`, `git log -20 --stat`,
   `git branch --no-merged <git.integration_branch from .agents/project.json>`, `tools/agent-workflow/`. Git is read-only for you.
5. Optional: the PM memory directory `~/.claude/projects/<project-folder>/memory/`
   (read only). If a read is denied, write "memory not readable" and go on.

## Evidence rules

- Evidence is a path with a line number, a commit id, a test name or a file under `runs/`. "met" needs one.
- These are NOT evidence: `tasks/STATUS.md`, `.tmp/pm-state/pm-continuation.md`, memory notes, the PM's chat. They may
  point you to an artifact; open the artifact.
- A rule whose check is `remembered only` has no gate. Judge it from behaviour in the artifacts (git log, task files,
  run records). If the artifacts say nothing either way, write `cannot tell`.
- For each user message in the digest that states a new rule, correction or decision, check that it is in
  `.agents/policy/USER_STANDING_RULES.md` or `tasks/DECISIONS_INDEX.md`. Missing = a gap.

<FOOTER>

## Task

### Output

Write ONE file, `.tmp/audit/audit-<DATE>.md`, in English (quote the user's words verbatim and unchanged):

```
# Audit <DATE> (auditor: Haiku 5.5, read-only; user words since <SINCE>)
Step 0: <3 lines: what you read, what you could not read, what you will not judge>

## Standing rules
| Rule | Verdict | Evidence | Note |
|---|---|---|---|
| S01 | met / not met / cannot tell | path:line or commit | one sentence |

## Recent decisions (newest 15)
| Decision | Applied in artifacts? | Evidence | Note |

## User words not yet in a rule or decision
- [timestamp] "verbatim words" -> where it should live

## Gaps: candidate new rules or gates
- candidate rule or gate, which mistake or message it answers, and the check that could enforce it

## Not read or not judged
- what and why
```

One row per rule S01 to the last; never skip a rule. A "not met" row names the exact artifact that contradicts the
rule and what the user said. Do not guess: `cannot tell` is a valid verdict.

### Hard limits

- Write only the one output file. No edits elsewhere, no Git writes, no network, no installs, no GPU, no model calls.
- Do not start processes that stay running. Do not open `.secrets/`, `.venv/`, `models/` or raw transcripts.
- Final reply: at most 8 lines: output path, counts (met / not met / cannot tell), the three most important gaps.
