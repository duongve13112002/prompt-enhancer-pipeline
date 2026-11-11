---
name: handoff-report
description: Required final report format for every delegated task in this repository, so the PM can review, record and hand off without rereading the history.
---

# Delegated task handoff report

The authoritative rule is section 13 of `AGENTS.md`. Finish every delegated task with this report, in English, and write the same content into the task file's `## Completion report` and `## Handoff` sections.

## Report sections

1. **Work completed:** what you changed, by file.
2. **Result and evidence:** exact commands you ran and their real results (test counts, skips, coverage, static checks, live evidence paths and hashes). Label each result: unit, mock_contract, replay, CPU live, GPU live, cloud live, quality review, performance. Write `not_run` for anything you did not run.
3. **Important changes and reasons:** decisions not specified by the brief, rejected alternatives and why.
4. **Problems, risks and remaining work:** failures, flaky tests, open defects, anything blocked and the exact blocker.
5. **Configuration audit:** for runtime changes, the row-by-row table from the `config-audit` skill. Missing rows mean the task is not complete.
6. **Current status:** one line, using a project state (`IMPLEMENTING`, `TESTING`, `REVIEWING`, `BLOCKED`, ...).
7. **What was already done:** facts the next agent can rely on.
8. **What the next agent must do:** concrete next steps.
9. **What must not be repeated:** approaches that failed, evidence that is now stale, calls that must not be spent again.
10. **Findings index (reviewers, auditors, researchers):** every finding as one line (ID, severity, claim) in the report and also in the final reply, even beyond the short-reply limit; implementers keep the limit. Reviewers also list the `recall.py search` queries they ran; `not_run` is not allowed for recall in a review.
11. **Lessons learned:** any mistake that recurred or needed several attempts to fix, with symptom, root cause and prevention rule. The PM appends these to `tasks/LESSONS_LEARNED.md`.

## Honesty rules

- Never claim a test passed, a service was stopped, or a file was committed or pushed without evidence. The user owns Git delivery (AGENTS.md section 11); do not commit or push.
- An agent review is "agent review under user delegation", never "human review".
- Distinguish user-confirmed requirements, PM proposals, your findings and live-tested evidence.
- Keep secrets, private images and full request payloads out of the report.
