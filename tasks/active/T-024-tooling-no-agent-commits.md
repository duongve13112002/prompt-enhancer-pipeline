# T-024 Align the copied tooling with "workers never commit, the orchestrator commits and pushes" and fix the 2026-11-14 tooling defects

Kind: code
R-IDs: R-021, R-025
Task file: tasks/active/T-024-tooling-no-agent-commits.md
Approach note: .tmp/T-024/approach.md

## Brief header

- **Goal:** the copied agent tooling enforces what the user decided (decisions 48, 77): workers never commit, push, merge or rewrite history; the orchestrator commits and pushes integrated work after the pre-push checks, never with force and never rewriting pushed history, and stages inside a worker's worktree to save a patch; Antigravity can review an uncommitted patch, and the tooling defects found on 2026-11-14 are gone (decision 75).
- **Done check:** `<venv python> -B tools/agent-workflow/test_slot.py -- <venv python> -B -m pytest -q -p no:cacheprovider tools/agent-workflow/tests` passes with the new tests listed below, and each item of the list "Work items" has a test or a pasted command output that shows it.
- **Work items** (plan row T-024 plus decision 75):
  1. `hooks/tool_guard.py` denies workers `git commit`, `push`, `merge`, `rebase`, `tag`, `reset --hard`, `cherry-pick`, `revert`, `am`, `branch -D`, `update-ref` and other history-changing commands; it lets the orchestrator record and publish integrated work from the main working tree once the pre-push check of item 5 passes (decision 77), and still denies the orchestrator forced publishing, amending or rebasing published work, deleting remote tags or branches, and any other history rewrite; it allows the orchestrator `git add` inside a worker worktree under `.tmp/worktrees/` to save a patch, and the removal of a finished worker's worktree and its never-committed branch (AGENTS.md section 11, decision 77).
  2. The merge mode of the Git guard follows item 1.
  3. `gemini-ws.ps1` builds an Antigravity workspace from a worktree (its working tree, tracked plus untracked non-ignored files) or from a saved patch applied to a base commit, instead of only `Base..Head` commits; `notes/delta.diff` holds the patch under review. Keep the 2026-11-14 fixes (relative tar path, root files `AGENTS.md` and `README.md` in the allowlist).
  4. Messages and docs that say "the PM commits" or name `.agents/registry.json`: `cli_worker.py`, `CLAUDE_CLI_SUBAGENTS.md`, `audit_brief.md`, `setup_project.py`, `.agents/skills/agent-setup/SKILL.md` (canonical copy; run `setup_project.py apply` to sync `.claude/skills/`), and the tests that pin the old text.
  5. Push guard: it names `.tmp/pm-scripts/prepush_lint.ps1`, which this repository does not have. Replace it with a tracked pre-push check in this repository (for example `tools/agent-workflow/prepush_check.py`) that runs `git diff --check`, `setup_project.py check`, `decisions_applied.py check` and a scan of the commit for `.secrets/`, model files, `database/` and `data/` paths, and writes the marker through `prepush_mark.py` only when every part is clean; the guard message names it.
  6. The force-push deny rules in `settings.example.json` (`PowerShell(git push * -f*)` and the Bash twin) also match a later `-First` in a pipeline (seen 2026-11-14: `git push origin main 2>&1 | Out-String; git status -sb | Select-Object -First 1` was denied). Make the rules match only a force flag of `git push`, or rely on item 1 if it already denies every push; keep a test with that exact command.
  7. `codex_profiles.json` `tested_codex_version`: `codex_hook_probe.py` passed with codex-cli 0.169.0 on 2026-11-14 (canary denied) and a worker turn returned rc 0; run the live exec and resume checklist of `CODEX_JOBS.md` and update the tracked value only if it passes.
- **Scope:** `tools/agent-workflow/hooks/tool_guard.py`, `hooks/guard_config.py`, `guard_config.json`, `gemini-ws.ps1`, `gemini-review.ps1`, `gemini-launch.ps1`, `prepush_mark.py`, `cli_worker.py` (messages only), `CLAUDE_CLI_SUBAGENTS.md`, `audit_brief.md`, `setup_project.py` (messages only), `settings.example.json` (deny rules only), `codex_profiles.json` (`tested_codex_version` only), `.agents/skills/agent-setup/SKILL.md`, their tests under `tools/agent-workflow/tests/`, and `.tmp/T-024/`.
- **Out of scope:** quota tooling and allow rules (T-026), cleanup tooling and the SessionStart hook (T-025), brief lint (T-020, T-021), CI (T-022). Report other findings under `Noticed, not done`.
- **Output:** the patch in the worktree, the completion report in this file, a reply under 15 lines.
- **Step budget:** about 60 tool calls; split into two phases if more (phase 1 items 1, 2, 5, 6; phase 2 items 3, 4, 7).
- **Run limit:** no network except what the Codex checklist of item 7 needs, no GPU, no installs, no paid calls.
- **May assume / Must ask:** may choose the test layout and helper names; must ask before changing any deny or allow rule beyond items 1, 2 and 6, before touching `.secrets`, and before any Git write in the main working tree.
- **Role pack and CLI wrapper:** implementer; Claude (`.agents/policy/cli/claude.md`).
- **Test sets:** not applicable: the lint's built-in suites belong to the earlier project (T-020 rebinds them); this task keeps the whole tool suite `tools/agent-workflow/tests/` green (10 tests are known to fail on Windows before this task, see tasks/STATUS.md; report any change in that list).
- **Trivial:** no. **Small:** no.
- **Base:** the user's latest commit on `main` at assignment (record it here).

## Task brief

- **Task name:** T-024 no agent commits and tooling defects.
- **Responsible role:** implementer.
- **Task difficulty and assigned model:** hard and high-risk (it changes safety guards); Claude Opus 5.5 `high` through `cli_worker.py start --profile dev-large` (PM_POLICY section 2, row "Implement: hard, high-risk").
- **Reasoning effort and reason:** high; guard regular expressions and Windows command parsing fail in edge cases.
- **Preloaded skills:** `handoff-report`, `config-audit`.
- **Current status:** the guard lets the orchestrator commit and push with a pre-push marker written by hand; whether it denies workers every history change is to be verified.
- **What was already done:** 2026-11-14 (decision 72): `gemini-ws.ps1` relative tar path and root files; `gemini-review.ps1` creates `_results` and passes a session allow-rule `read_file(<workspace>)`; test `test_review_job_allows_workspace_reads_and_keeps_its_result`.
- **Expected output:** see the brief header.
- **Constraints and security cautions:** a guard change can lock every agent out or let a dangerous command through; every deny has a test that it denies and a test that a near-miss safe command passes. Never `--dangerously-skip-permissions`.
- **What must not be repeated:** shell-pattern rules that match text later in a pipeline (item 6).
- **Ignored evidence paths:** `.tmp/T-024/` (approach note, pre-mortem, probe output).

### Research required before code

1. How Claude Code matches `Bash(...)` and `PowerShell(...)` permission rules (prefix, wildcard, whole command or each statement): Claude Code settings documentation.
2. Which Git commands change history or refs, from the Git documentation, so the deny list is complete rather than guessed.
3. `agy` workspace rules: what a `read_file(<dir>)` allow rule covers (directory recursion), from `agy` docs or a probe in `.tmp/T-024/`.
4. Codex exec and resume checklist in `tools/agent-workflow/CODEX_JOBS.md`.

### Configuration impact

Fill before the first edit: `guard_config.json` keys touched, `settings.example.json` rules touched (rendered into `.claude/settings.local.json` by `setup_project.py apply`), `codex_profiles.json` `tested_codex_version`.

## PM assignment (2026-11-14, decision 75)

- **Hazards first (PM_POLICY section 4):** before the implementer starts, a Gemini 3.8 Flash `medium` read-only job (`gemini-review.ps1`) writes the approach note and pre-mortem to `.tmp/T-024/approach.md`; the PM turns its failure cases into attempt-1 tests named in the brief.
- **Step 0:** the implementer writes Step 0 and stops. The plan touches permissions, so the PM sends it to the user and waits for the user's approval (PM_POLICY section 4).
- **Review:** high-risk, two reviewers of different families: Codex `gpt-6.1-sol` `high` and Gemini 3.8 Flash `high` (once item 3 lets Gemini read the uncommitted patch; until then the second reviewer gets the patch as `notes/delta.diff` through `gemini-ws.ps1` of the old flow on a temporary commit-free workspace, or Sonnet 5.5 `high`).
- **Parallel:** runs beside T-025 (no shared file); T-026 waits for T-024 and T-025 because it touches `settings.example.json` and the SessionStart hook.
- **Brief assembly:** `.agents/policy/cli/claude.md`, this file, `.agents/policy/roles/implementer.md`, `.agents/policy/AGENT_BRIEF_FOOTER.md` into `.tmp/T-024/brief.md`; lint with `brief_lint.py --require-footer --brief .tmp/T-024/brief.md`.

