# T-025 Cleanup tooling

Kind: code
R-IDs: R-021
Task file: tasks/active/T-025-cleanup-tooling.md
Approach note: .tmp/T-025/approach.md

## Brief header

- **Goal:** `.tmp` cleanup becomes one safe command instead of hand work (decision 43, standing rule S40): a dry run, an orphan sweep by configured age, a retention for `.tmp/pm-archive`, and a SessionStart line that shows the size of `.tmp` and the number of orphans.
- **Done check:** `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests/test_purge_tmp.py tools/agent-workflow/tests/test_session_context.py` passes with the new tests, and `purge_tmp.py sweep --dry-run` on the real checkout prints the list it would delete without deleting anything (paste the output).
- **Work items** (plan row T-025):
  1. `purge_tmp.py` gains `--dry-run` for every deleting command and a `sweep` command: list the children of `.tmp/` that no worker (`cli-workers.json`), task in flight (`## IN-FLIGHT` of `.tmp/pm-state/pm-continuation.md`) or Antigravity job (`.tmp/pm-state/agents.json`) owns and that are older than a configured age, then delete them; never `.tmp/pm-state/`, `.tmp/pm-archive/`, `.tmp/cli-jobs/`, worktrees of tasks in flight, trees a busy worker uses, or anything reached through a link.
  2. The age is configuration (plan default 72 hours) with a validated key and a refusal for a missing or invalid value (AGENTS.md section 5); a retention for `.tmp/pm-archive` (age or size) is configuration too.
  3. The SessionStart hook prints one line: size of `.tmp` and number of orphans past the age (read-only, bounded time).
  4. ORCHESTRATION section 4 text that says "task T-025 makes the age a configured value" is updated by the PM at integration, not by the worker.
- **Scope:** `tools/agent-workflow/purge_tmp.py`, the configuration file that holds tool settings (find where `purge_tmp.py` and the hooks read their settings; add the keys there), `tools/agent-workflow/hooks/session_context.py` (or the hook that prints the SessionStart lines; find it), their tests, `.tmp/T-025/`.
- **Out of scope:** guards and Git (T-024), quota files and the quota line of the SessionStart hook (T-026), deleting anything for real outside tests during the task (the dry run on the real checkout is the only real run).
- **Output:** the patch in the worktree, the completion report in this file, a reply under 15 lines.
- **Step budget:** about 40 tool calls.
- **Run limit:** no network, no GPU, no installs; tests use temporary trees only.
- **May assume / Must ask:** may choose names and the configuration file layout inside existing conventions; must ask before deleting any real path and before changing a protected-tree rule.
- **Role pack and CLI wrapper:** implementer; Codex (`.agents/policy/cli/codex.md`).
- **Test sets:** not applicable: the lint's built-in suites belong to the earlier project (T-020 rebinds them); this task keeps `tools/agent-workflow/tests/` green (report changes in the 10 known Windows failures).
- **Trivial:** no. **Small:** no.
- **Base:** the user's latest commit on `main` at assignment (record it here).

## Task brief

- **Task name:** T-025 cleanup tooling.
- **Responsible role:** implementer.
- **Task difficulty and assigned model:** medium, clear spec; Codex `gpt-6.1-terra` `medium`, role `codex-dev` (PM_POLICY section 2).
- **Reasoning effort and reason:** medium; the risk is deleting the wrong tree, which tests on temporary trees cover.
- **Preloaded skills:** `handoff-report`, `config-audit`.
- **Current status:** `purge_tmp.py` has `archive-remove` and `purge-children`; the orchestrator sweeps by hand.
- **What was already done:** nothing for this task.
- **Expected output:** see the brief header.
- **Constraints and security cautions:** deletion code: every delete path is tested against links, junctions on Windows, protected trees and busy workers; a dry run deletes nothing.
- **What must not be repeated:** none yet.
- **Ignored evidence paths:** `.tmp/T-025/`.

### Research required before code

1. How Python's `shutil.rmtree` and `os.scandir` treat Windows junctions and symlinks at the installed Python 3.12 version (source or docs), so a link is never followed.
2. Where the existing hooks read their configuration (`project_config.py`, `.agents/project.json`, `guard_config.json`): source-verified.

### Configuration impact

Fill before the first edit: the new keys (sweep age, archive retention), their file, validation, example value, override test and refusal test.

## PM assignment (2026-11-14, decision 75)

- **Approach note:** a Gemini 3.8 Flash `medium` read-only job writes `.tmp/T-025/approach.md` first.
- **Step 0:** Codex stops at Step 0 (two messages, cli/codex.md); the PM approves it (no permission or product meaning involved) and sends the go.
- **Review:** ordinary work: Sonnet 5.5 `medium` (Antigravity first once T-024 lets it read an uncommitted patch).
- **Parallel:** beside T-024; T-026 waits for this task (shared SessionStart hook).
- **Brief assembly:** `.agents/policy/cli/codex.md`, this file, `.agents/policy/roles/implementer.md`, `.agents/policy/AGENT_BRIEF_FOOTER.md` into `.tmp/T-025/brief.md`; lint it.
