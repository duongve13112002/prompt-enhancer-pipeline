Updated: 2026-11-13. The user's agent-reuse rules (pasted 2026-10-28), rewritten for this repository's tools (decision 48) and changed to one fresh worker per task (decision 45). Orchestrator only; a worker never needs this file. Models, effort, quota and review routing: [policy/PM_POLICY.md](policy/PM_POLICY.md).

# Agent orchestration

## 0. Why

- **One task, one fresh worker.** A worker that carries a previous task brings its history and its uncommitted, possibly rejected diff into the next one; a fresh worker in a fresh worktree keeps each patch tied to one task. A warm worker in a different worktree misses its cache anyway (N137).
- **Inside a task, keep the warm worker.** Fix rounds and the next phase of the same task go to the worker that already holds the context and the cache.
- **Clean up at once.** A finished worker costs disk, registry lines and attention; it is removed as soon as nothing more is planned for it (section 4).
- **At most 8 running agents** across Claude Code, Codex and Antigravity together; a ceiling, not a target. Idle agents do not count.

## 1. Working mode

The mode is the `CURRENT MODE` line in the `ORCHESTRATION` block of AGENTS.md (CLAUDE.md imports it); if it is missing, the mode is **DELEGATE**.

- **DELEGATE (default):** delegating is allowed and this file applies.
- **SOLO:** the orchestrator does every task itself and starts no agent. A running agent may finish its current work, gets nothing more and is cleaned up.
- **Permanent change** only when the user says so clearly ("chuyển sang SOLO", "quay lại DELEGATE"): confirm in one sentence, edit the `CURRENT MODE` line, tell the user. **One-off:** "task này tự làm" or "task này giao agent" applies to that task only, with no file change. If it is unclear which one is meant, ask one question first. Only the user changes the mode.

## 2. Session start (once per orchestration session)

1. `python tools/agent-workflow/setup_project.py check`; repair a FAIL with `apply` (skill `agent-setup`), report the rest.
2. Confirm with a read-only `gh` call that the GitHub repository is private (AGENTS.md section 11); if it is public and no CI window is open, tell the user at once.
3. **CLI facts, from real commands only:** `claude --version`, `codex --version`, `agy --version`; confirm in `--help` that resuming by ID still works (Claude and Codex through `cli_worker.py send`; Antigravity `agy -p "..." --conversation <ID>`) whether a CLI has an official delete-by-ID command for a session; and which scheduling tool this session has (a Desktop scheduled-task tool, `CronCreate`, or none), found in its own tool list, never asked of the user. Never invent a flag; if unsure, treat it as absent. Write the results with the time to `.tmp/pm-state/platform-check.json`; a new session checks again.
4. **Cache TTL:** Claude workers take their TTL from `tools/agent-workflow/worker_profiles.json`. Codex and Antigravity document no TTL setting (re-check `--help`); use the convention 1 hour for an implementer kept for fix rounds and 5 minutes for a reviewer kept for a retest. If the Claude subscription runs on usage credits, treat the Claude TTL as 5 minutes.
5. **Quota:** `usage_snapshot.py`, then `.tmp/pm-state/quota-blocks.json`: a provider past its reset time is available again; for every provider still blocked make sure a wake is scheduled ([PM_POLICY](policy/PM_POLICY.md) section 1).
6. **Clean up** what the previous session left (section 4, including the orphan sweep).
7. Write `## NOW` in `.tmp/pm-state/pm-continuation.md` ([PM_POLICY](policy/PM_POLICY.md) section 6).

## 3. State: one place for each fact

| Fact | Where | Written by |
| --- | --- | --- |
| Claude and Codex workers: name, backend, model, effort, session, cwd, status, last turn | `.tmp/pm-state/cli-workers.json`, read with `cli_worker.py status --all --json` | `cli_worker.py` only |
| Antigravity jobs | `.tmp/pm-state/agents.json` | the `gemini-*.ps1` scripts |
| Tasks in flight: task, worker, phase, review state, base commit, applied patches, pending cleanup | `## IN-FLIGHT` in `.tmp/pm-state/pm-continuation.md` | PM |
| Accepted patches | `.tmp/pm-state/patches/<task>.patch` | PM |
| Blocked providers and their reset times | `.tmp/pm-state/quota-blocks.json` | PM (task T-026 automates it) |
| Decision log | `.agents/log.md`, one line per decision (`<epoch> ASSIGN T-13 -> codex-dev-02 (new task)`), last 300 lines | PM |

`.agents/registry.json` of the earlier rules is no longer used (decision 48). Time comes from `date +%s`, never from an estimate. `elapsed` = now minus the end of the worker's last turn; an implementer counts as expired when `elapsed > ttl - 60 s`, a reviewer when `elapsed > ttl - 30 s`. Read the state only when a decision needs it.

## 4. Cleanup (decision 43)

**When:** a task is accepted and integrated and its worker has no planned fix round or phase; at session start; when the user says "kết thúc phiên"; on every scheduled wake; and at step 1 of every assignment. Never clean up a `busy` worker.

**Step 0, scratch.** Delete the worker's own trees under `.tmp/` (its task folder, `.tmp/codex-run/<name>`, `.tmp/agy-work/<workspace>` and the `.tmp/agy-jobs` files of its job, test temps, mutant copies, caches it made) after its handoff is read, with `python -B -I tools/agent-workflow/purge_tmp.py archive-remove <dir>`, which keeps small reports in `.tmp/pm-archive/` first. Delete only paths it created (known from the brief, `cli-workers.json` or the worker name). Keep `.tmp/cli-jobs/` (the turn ledger), `models/`, `.venv*/`, `.secrets/`, `runs/` evidence, the data root and anything a running task uses; never recurse through a link.

**Orphan sweep.** At session start, at "kết thúc phiên" and on every wake, list the children of `.tmp/` that no worker, task in flight or job owns; `.tmp/pm-state/` (patches, quota blocks, notes), `.tmp/pm-archive/`, `.tmp/cli-jobs/` and the worktrees of tasks in flight are never orphans. Delete those older than 72 hours with `purge_tmp.py purge-children .tmp <name>...`, which refuses protected trees and trees a busy worker uses. Report what was deleted in one line. Task T-025 makes the age a configured value and adds a dry run.

**Step 1, state.** Remove the task and worker from `## IN-FLIGHT`; log `<epoch> CLEANUP <name> (<reason>)`; never call that worker again. A task still waiting for review stays in flight and a later fix gets a full handoff.

**Step 2, worktree and branch** (when the worker had its own). Eligible when every task on it is done and its patch is saved under `.tmp/pm-state/patches/` and is either committed by the user or discarded. Then `purge_tmp.py archive-remove <worktree>`, `git worktree prune`, `git branch -d <branch>` (never `--force`, never `-D`). If a step refuses, keep it as pending cleanup in `## IN-FLIGHT` and tell the user. Never touch the main worktree or the orchestrator's own.

**Step 3, chat history.** Claude and Codex workers: `cli_worker.py retire --name <worker> --purge-transcript` (deletes the session by its exact ID). An official delete-by-ID command (section 2) is used with the exact ID. Otherwise: Codex, the one session file under `~/.codex/sessions/` that contains the session ID; Antigravity, exactly `~/.gemini/antigravity-cli/brain/<conversation-id>/` of a job this orchestrator started; a separate `claude -p` session, exactly `<session_id>.jsonl` and its `<session_id>/` folder under `~/.claude/projects/<project>/`. Never delete a Claude subagent transcript or the orchestrator's own session. Find the path first, delete only when the path contains the full non-empty ID, and never use a wildcard wider than the ID.

**Step 4.** The server-side prompt cache expires by itself; do nothing.

**Step 5.** Log what was deleted, what was kept and why.

## 5. Assigning work

1. **Clean up** expired workers (section 4).
2. **Continuation of a task in flight** (a fix round, the next phase): give it to the same worker when it is within its TTL, not busy and under about 250K context (S15). Otherwise start a fresh worker of the same row with a full handoff: the task file, the worktree or the saved patch, the findings.
3. **A new task** goes to a fresh worker of the row [PM_POLICY](policy/PM_POLICY.md) section 2 names, in a fresh worktree under `.tmp/worktrees/`, never to a worker that holds another task (decision 45).
4. **Base of the worktree:** the user's latest commit. A task that depends on accepted work that is not yet committed starts from that commit plus the predecessor patches, applied in order with `git apply --check` first; record the base commit and the patch files in the task file.
5. **Parallel start** only when the task is briefed and its Scope shares no file with running work. Workers write decisions, notes and status lines into their own task file; the PM moves them into `docs/implementation-notes.md`, `docs/note/` (number and index) and `tasks/STATUS.md` at integration, so shared documents are never edited in parallel.
6. **Slots:** fewer than 8 busy agents, start it; otherwise queue it in `## IN-FLIGHT` and rerun this procedure when an agent finishes.
7. **Send** with section 8; update `## IN-FLIGHT`; log one line.

## 6. Review results

- **PASS:** the PM integrates ([PM_POLICY](policy/PM_POLICY.md) section 5); the task is done once integrated and the worker is cleaned up.
- **FAIL:** send the findings verbatim to the same implementer when it is within its TTL and not busy (8.2); otherwise a fresh worker of the same row gets a full handoff. Every return counts toward the fix-round cap ([PM_POLICY](policy/PM_POLICY.md) section 5).

## 7. Assigning review

The reviewer is a fresh worker of another family ([PM_POLICY](policy/PM_POLICY.md) section 2) and receives the saved patch and the task file. It is reused only for the retest of the same task within its TTL. Until task T-024 lets Antigravity review an uncommitted patch, reviews go to Claude or Codex.

## 8. Messages (always say clearly what the message is)

### 8.1 Next phase of the same task

```
[NEW TASK: T-13 phase 2]
Phase 1: <accepted | under review; findings will follow separately>.
Goal of this phase: ... | Scope: <files> | Out of scope: ... | Done check: <command> | Step budget: ...
Report in the template's Completion report format.
```

### 8.2 Review findings to the implementer

```
[QA FEEDBACK: T-12, round <n>] Verdict: FAIL.
Findings (verbatim from the reviewer):
1. ... (reproduction, expected, actual, file and line)
Fix exactly these findings, run the done check, report in the template's Completion report format.
```

### 8.3 Evaluation or retest for a reviewer

```
[EVALUATE: T-13 | RETEST: T-12 round <n>]
Check: ... | PASS criteria: ... | Retest: confirm only that the listed findings are fixed and nothing near them broke.
Report: PASS or FAIL with findings (reproduction, expected, actual, file and line, severity).
```

### 8.4 One report format

Every worker reports with the [template](policy/AGENT_TASK_TEMPLATE.md)'s Completion report and Handoff in the task file, and replies with about 10 short lines (footer rule 11). No other format is used.

## 9. Keeping the cache

- Never change the model or the tool set of a worker that continues a task; that loses the cache. On Claude, changing effort keeps the cache for Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1 or later (N135); on Codex and Antigravity treat any change as a loss.
- Keep the same working directory, flags and configuration; send only what is new.
- Retire a worker over about 250K context or after several compactions; the next phase goes to a fresh worker with a handoff.
- Antigravity `-p` runs may have a wait limit: check `--help` and raise the timeout for long jobs.

## 10. Checklist before every assignment (self-check, not printed)

- [ ] DELEGATE mode, or the user allowed delegation for this task?
- [ ] Time from `date +%s`; expired workers cleaned up?
- [ ] A new task goes to a fresh worker and worktree; a continuation to its own warm worker?
- [ ] Base commit and predecessor patches recorded; no Scope overlap with running work?
- [ ] Quota read and the provider allowed ([PM_POLICY](policy/PM_POLICY.md) section 1)?
- [ ] At most 8 running agents?
- [ ] The message says new phase, feedback or retest; `## IN-FLIGHT` and `.agents/log.md` updated?

## 11. Reporting to the user

After each round, 1-3 lines: which task went to which worker; new or continuing, with the reason; running agents out of 8; what was cleaned up and why anything was kept.

## 12. Where these rules live

- This file is the single source; it carries its update date on the first line and is never copied into AGENTS.md, CLAUDE.md or memory, and never imported (that would load it into every agent).
- AGENTS.md holds only the two marker blocks `ORCHESTRATION` (with the `CURRENT MODE` line) and `AGENT-WORKER`; replace only the text between their markers.
- After a rule change, report the files changed and the current mode; a change takes effect after `/clear`, `/compact` or a restart. The orchestrator commits rule files like any other change (decision 77).

## 13. Project bindings

- **Heartbeat:** while any agent works, `pm-supervisor.ps1 -MaxMinutes 45` runs under the Monitor tool and wakes the PM on every worker event and at least every 45 minutes ([PM_POLICY](policy/PM_POLICY.md) section 3); re-arm it after each wake.
- **Claude workers:** `claude -p` sessions started and continued only through `tools/agent-workflow/cli_worker.py` (`start --profile <role>`, `send --name`, `status`, `retire`), never through the Agent tool.
- **Codex workers:** `cli_worker.py start --backend codex` with a `codex-*` role from `codex_profiles.json`; Codex stops at Step 0 unless the brief says the plan is approved. Never delete Codex app data.
- **Antigravity:** read-only jobs run in parallel through `gemini-review.ps1`; jobs that write files run one at a time through `gemini-launch.ps1` and the `AgyRunner` (`scripts/agy-runner/`, Windows only); workspaces come from `gemini-ws.ps1 -Sanitize`, which today needs a committed head (task T-024). The orchestrator never reads the runner account's home. Every rule for a Claude worker binds an agy job too.
- **Approach scout:** before a non-trivial, non-small implementation a read-only scout writes a short approach note; it is advice, never a decision; `brief_lint` refuses a feature brief without an `Approach note:` path ([PM_POLICY](policy/PM_POLICY.md) section 4).
- **Tests** run through `tools/agent-workflow/test_slot.py`.
- **Workers never commit, push or merge; the orchestrator commits and pushes integrated work** (AGENTS.md section 11, decision 77). Files outside `.tmp` are deleted only after asking the user or a written analysis that they are garbage.
- **PID identity:** a recorded worker PID counts as alive only with the same process creation time (`cli_worker.py status`).
- **GPU peers:** [policy/AGENT_PEER_RULES.md](policy/AGENT_PEER_RULES.md).
