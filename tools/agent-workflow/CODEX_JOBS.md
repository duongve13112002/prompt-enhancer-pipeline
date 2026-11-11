# Codex workers (PM-launched)

Policy (models per role, effort, quota, review routing): [PM_POLICY](../../.agents/policy/PM_POLICY.md) sections 1-3. Mechanism, permissions and setup: [README, Codex workers](README.md#codex-workers). Model facts: [N094](../../docs/note/N094-openai-codex-models.md).

## Launch

- Workers start only through `cli_worker.py start --backend codex --profile <codex-role> --codex-config <local copy> --name <n> --cwd <worktree> --message-file <brief>`; continue with `send --name <n>`, end with `retire`. Never from the project root (the sandbox refuses it) and never with `-s`, `--dangerously-*`, the fast tier or effort `ultra`.
- Roles and models live in [codex_profiles.json](codex_profiles.json) (`codex-dev`, `codex-review`, `codex-research`, `codex-docs`, `codex-scout`, `codex-image`); the local copy under `.tmp/pm-state/` fills `runtime.codex_command` (native `codex.exe` on Windows). Change a model there, never in code.
- Briefs end with the [footer](../../.agents/policy/AGENT_BRIEF_FOOTER.md) and pass `brief_lint.py` (the launcher refuses otherwise). Codex stops at Step 0 unless the brief says the plan is approved.

## Quota

- Codex runs on the user's subscription: before each round run `usage_snapshot.py`; it reads the newest `token_count` row of the recent Codex session files and names each window by its length (5-hour, weekly). When Codex's quota is exhausted, route the task to the same tier of another family (PM_POLICY section 1).
- Every turn appends a row to `.tmp/pm-state/worker-ledger.jsonl` with tokens and the usage windows before and after.

## Stop and clean up

- `cli_worker.py` stops a Codex worker's whole process tree on timeout; the PM never kills by name. On every wake, list background shells and orphan agent processes and stop what has no owner, by recorded PID.
- Finished project `codex exec` sessions may be deleted by exact ID (`retire --purge-transcript`); never delete Codex app data.

## Review routing

A Codex change is reviewed by a fresh agent of another family when possible (Gemini 3.8 Flash for ordinary work, Sonnet 5.5 or Opus 5.5 for high-risk); a reviewer is never weaker than the implementer for high-risk work.
