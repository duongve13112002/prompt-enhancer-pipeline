# CLI wrappers

The same brief goes to every CLI; a short wrapper adapts how it is framed, because the three vendors document different habits for their models (decision 24, improvement D). The PM pastes the wrapper for the worker's CLI at the top of the brief. Evidence: [N127](../../../docs/note/N127-claude-prompting-best-practices.md), [N128](../../../docs/note/N128-codex-prompting-guidance.md), [N129](../../../docs/note/N129-gemini-prompting-guidance.md), [N130](../../../docs/note/N130-antigravity-rules-and-headless.md). These are vendor recommendations, not measurements on this workflow: keep a wrapper line only while the worker ledger shows it helps (PM_POLICY section 6).

| CLI | Wrapper |
| --- | --- |
| Claude Code | [claude.md](claude.md) |
| Codex | [codex.md](codex.md) |
| Antigravity (`agy`) | [antigravity.md](antigravity.md) |

**Brief order (improvement B, T-021):** vendors advise context first and the specific instruction last (N127, N129). The wrapper goes at the top, then the header lines, the context, the role pack and the footer; the brief ends with one `## Task` block (Goal, Done check, Scope, Out of scope, Output). `brief_lint.py` rule `brief-order` checks that structure for every new worker; the layout is in [AGENT_TASK_TEMPLATE](../AGENT_TASK_TEMPLATE.md), Brief header.
