## Wrapper: Antigravity (`agy`) workers

- Put context first and the task-specific instruction last in the one-line prompt and in the notes file.
- Add the grounding sentences: "If the answer is not in the files you were given, say that it is not available" and "Quote the exact lines you rely on."
- Add: "When a command fails, change the approach or the arguments instead of repeating it."
- Use the documented thinking level for agent work (`medium` or higher) so tool use does not stop early.
- Headless runs cannot ask for approval: an action that needs it is denied, so the brief names only actions the runner's permission rules allow.
- `AGENTS.md` must stay well below the size at which `agy` truncates a rule file (N130); the character budget in `tools/agent-workflow/doc_budgets.json` enforces this.
- **Prompt form** (moved here from PM_POLICY on 2026-11-13): one line, without double quotes, ending with: *Work and read only inside your workspace; no subagents, file explorers or web, URL or network tools; read the exact files the task names; an implementation job first writes its unknowns, at most 5 ranked questions and a short plan with a finish check, then stops until approved; other jobs finish in this session; never read or mention .secrets; run no commands unless told; do not assume - cite the file or primary source for every claim, label the unverified as assumption; if ambiguous, write the question, do not guess; report truthfully, listing what you could not check; keep the final reply short; leave nothing running.*
