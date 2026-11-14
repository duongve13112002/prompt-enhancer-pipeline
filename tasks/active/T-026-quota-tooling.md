# T-026 Quota tooling

Kind: code
R-IDs: R-021
Task file: tasks/active/T-026-quota-tooling.md
Approach note: .tmp/T-026/approach.md

## Brief header

- **Goal:** the orchestrator reads every provider's quota from files instead of by hand, records exhaustion and wakes at a known reset without the user's help (decisions 44, 46, 52, 53; standing rule S32).
- **Done check:** `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests` passes with the new tests; `usage_snapshot.py` on the dev machine prints a status for Claude, Codex and Antigravity with no `unknown` caused by a missing file (paste it); the scheduled-wake dry run of item 5 is recorded with its output.
- **Work items** (plan row T-026):
  1. Re-read the sources of [N135](../../docs/note/N135-quota-reset-and-scheduling.md) first and record changes.
  2. A status-line script that writes Claude's `rate_limits` and Antigravity's `quota` objects to `.tmp/pm-state/`.
  3. `usage_snapshot.py` reads those files, stops assuming JSON from `agy -p /usage` (2026-11-14: `agy -p /usage --output-format json` works; verify), treats a provider without a reported limit as unlimited, and flags Claude at 90%.
  4. `quota-blocks.json` is written on exhaustion and printed by the SessionStart hook.
  5. A dry run of the scheduled wake on the dev machine, both as a one-shot Desktop scheduled task (new session, one-orchestrator check) and with `CronCreate` in the terminal CLI. The 2026-11-14 one-shot Desktop task `pm-wake-codex-reset-t002` is a real data point: read its run record.
  6. Project allow rules for the commands a wake session runs (`usage_snapshot.py`, `cli_worker.py status` and `send`, `purge_tmp.py`) in `settings.example.json`, after checking whether Desktop scheduled sessions honour project settings.
  7. The supervisor keeps the computer awake only while workers run, without changing power settings (an execution-state request of the process, not a power plan change).
  8. A source for Claude's own reset time inside Claude Desktop (the `rate_limit_event` of worker turns, or another documented one).
- **Scope:** a new status-line script under `tools/agent-workflow/`, `usage_snapshot.py`, the SessionStart hook (quota line only), `settings.example.json` (allow rules and the status-line entry only), `pm-supervisor.ps1` (keep-awake only), their tests, `docs/note/N135-quota-reset-and-scheduling.md` (source changes only), `.tmp/T-026/`.
- **Out of scope:** guards and deny rules (T-024), cleanup (T-025), any power or system setting.
- **Output:** the patch in the worktree, the completion report in this file, a reply under 15 lines.
- **Step budget:** about 60 tool calls; split into two phases (phase 1 items 1 to 4 and 8; phase 2 items 5 to 7).
- **Run limit:** no GPU, no installs; network only to re-read N135's sources.
- **May assume / Must ask:** may choose file names under `.tmp/pm-state/`; must ask before adding any allow rule not listed in item 6 and before anything that touches power settings.
- **Role pack and CLI wrapper:** implementer; Codex (`.agents/policy/cli/codex.md`).
- **Test sets:** not applicable: the lint's built-in suites belong to the earlier project (T-020 rebinds them); this task keeps `tools/agent-workflow/tests/` green.
- **Trivial:** no. **Small:** no.
- **Base:** the user's latest commit plus the accepted patches of T-024 and T-025 (record them here).

## Task brief

- **Task name:** T-026 quota tooling.
- **Responsible role:** implementer.
- **Task difficulty and assigned model:** moderately hard, multi-file; Codex `gpt-6.1-sol` `high` (PM_POLICY section 2; Claude carries T-024).
- **Reasoning effort and reason:** high; it spans the status line, hooks, scheduling and Windows process APIs.
- **Preloaded skills:** `handoff-report`, `config-audit`.
- **Current status:** the orchestrator reads Claude usage by hand and writes `quota-blocks.json` by hand.
- **What was already done:** nothing for this task.
- **Expected output:** see the brief header.
- **Constraints and security cautions:** allow rules widen what runs without approval: each rule names one command, no wildcard over arguments that could write.
- **What must not be repeated:** none yet.
- **Ignored evidence paths:** `.tmp/pm-state/` quota files, `.tmp/T-026/`.

### Research required before code

1. Claude Code status-line input fields (`rate_limits`, `resets_at`) and whether Claude Desktop runs the status line: Claude Code docs.
2. Antigravity status-line `quota` object: agy docs or changelog.
3. Whether Desktop scheduled sessions read project `.claude/settings*.json`.
4. Windows `SetThreadExecutionState` semantics for keep-awake without a power plan change: Microsoft docs.

### Configuration impact

Fill before the first edit: the 90% Claude line (existing setting or a new key), file paths of the quota files, allow rules.

## PM assignment (2026-11-14, decision 75)

- **Approach note:** a Gemini 3.8 Flash `medium` read-only job writes `.tmp/T-026/approach.md` first.
- **Step 0:** Codex stops at Step 0; the plan touches permissions (allow rules) and the computer's sleep, so the PM sends it to the user and waits.
- **Review:** Sonnet 5.5 `medium` plus Gemini 3.8 Flash `medium` for the allow-rule part.
- **Reassigned 2026-11-14 (PM):** a Codex worker cannot edit `tools/agent-workflow/` (`hooks/codex_guard.py:104-105`), so the implementer is Claude Opus 5.5 `high` (Claude wrapper, `--profile dev-large`) and the reviewers are Codex `gpt-6.1-sol` `high` (read-only) plus Gemini 3.8 Flash `medium` for the allow-rule part. The Codex lines above are superseded.
- **Order:** starts after T-024 and T-025 are accepted.
- **Brief assembly:** `.agents/policy/cli/codex.md`, this file, `.agents/policy/roles/implementer.md`, `.agents/policy/AGENT_BRIEF_FOOTER.md` into `.tmp/T-026/brief.md`; lint it.
