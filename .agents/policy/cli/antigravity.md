## Wrapper: Antigravity (`agy`) workers

- Put context first and the task-specific instruction last in the one-line prompt and in the notes file.
- Add the grounding sentences: "If the answer is not in the files you were given, say that it is not available" and "Quote the exact lines you rely on."
- Add: "When a command fails, change the approach or the arguments instead of repeating it."
- Use the documented thinking level for agent work (`medium` or higher) so tool use does not stop early.
- Headless runs cannot ask for approval: an action that needs it is denied, so the brief names only actions the runner's permission rules allow.
- `AGENTS.md` must stay well below the size at which `agy` truncates a rule file (N130); the character budget in `tools/agent-workflow/doc_budgets.json` enforces this.
