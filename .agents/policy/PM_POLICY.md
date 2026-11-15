# PM Dispatch Policy

Release 0.2 for this repository, 2026-11-13 (carried from an earlier project; revised per decisions 24 and 43 to 48). The orchestrator (PM) reads this at session start and before each assignment. Evidence for models and prices: [N093](../../docs/note/N093-claude-model-selection.md) (Claude), [N094](../../docs/note/N094-openai-codex-models.md) (Codex), [N095](../../docs/note/N095-gemini-agy-models.md) (Gemini). Goal: **the fewest tokens and subscription quota for the best result.**

## 1. Subscriptions and quota (check before every dispatch round)

All three agent CLIs run on the user's **subscriptions**, not API keys: Claude Code, Codex and Antigravity `agy` (Gemini 3.8 Flash). Each paid plan has a 5-hour window and a weekly limit; API prices below are only a relative cost signal between models. Evidence for every source and mechanism below: [N135](../../docs/note/N135-quota-reset-and-scheduling.md).

- **Read the usage before each round** and on every wake: `python -B tools/agent-workflow/usage_snapshot.py` (Codex from its session files, which carry exact `resets_at` times). Claude: the status line's `rate_limits` (used percentage and `resets_at` per window); until task T-026 writes them to a file, read `/usage` and write the PM file by hand. Antigravity: the status line's `quota` object (`reset_time`); `agy -p /usage` prints text, not JSON. A source reported `unknown` is checked by hand, never assumed free. **A provider that reports no limit is not checked at all** (decision 44).
- **How far each provider is used (S32, decision 44):** Codex and Antigravity until their quota runs out. Claude workers until Claude's 5-hour or weekly window reaches **90%**; the rest is kept for the orchestrator, which shares that window. Move the work to the same row of another family (section 2); never move a task up a tier only because a provider ran out.
- **Quota exhausted, reset time known (decision 46):**
  1. Record `{provider, window, resets_at, source}` in `.tmp/pm-state/quota-blocks.json`. Take the time from the exact field (`resets_at`, `reset_time`), never from an error message's local clock text.
  2. Schedule one wake at the reset time plus 3 minutes, on a minute other than :00 or :30 (if it lands on one, add a minute), **with whichever scheduling tool this session has** (recorded at session start, ORCHESTRATION section 2); the user sets up nothing (decisions 52, 53; N135):
     - **Desktop scheduled-task tool** (Claude Desktop): a one-shot task, only on this computer, in this project folder. It starts a **new session**, so the prompt is self-contained: `Orchestrator wake for this repository: quota reset <provider>. Read AGENTS.md, tasks/STATUS.md, .tmp/pm-state/pm-continuation.md and .tmp/pm-state/quota-blocks.json; follow PM_POLICY section 1, step 3.` Creating it may ask the user for one approval.
     - **`CronCreate`** (terminal CLI): a one-shot task in this session with `quota reset <provider>: read usage, resume started work, clean up .tmp`.
     - **Neither:** report the reset time; the next session start reads the file.
     A wake runs only while the app or session is open and the computer awake; a sleeping computer delays it (Desktop then makes one catch-up run). The PM never changes power or system settings; task T-026 lets the supervisor keep the computer awake only while workers run.
  3. On the wake: **one orchestrator at a time** — if another orchestrator session is active (live supervisor heartbeat in `.tmp/pm-state/pm-supervisor.json`, or `## NOW` updated in the last 15 minutes), write one line under `## NOW` ("<provider> free since <time>") and stop. Otherwise read the usage; if the window is free, remove the block and continue **only tasks the user already started** (a plan task whose Step 0 the user approved, a code task the user started with "BẮT ĐẦU IMPLEMENTATION"); a task or plan still waiting for the user stays waiting and is listed in the next report; run the cleanup (ORCHESTRATION section 4); re-arm the supervisor if agents run.
- **The orchestrator's own Claude limit:** before the limit, write a full resume point in `## NOW` and `tasks/STATUS.md`. Without automatic continuation (Claude Desktop documents none), schedule the wake of step 2 for Claude's own reset time (from the worker ledger's rate-limit events or the user's usage page until task T-026 automates it). In the terminal CLI, the setting `autoContinueAtUsageLimit` (on by default for subscription sessions, per N135) waits and continues up to 3 times. Never hand the PM role to another model, and never resume a running PM session from a second window (the transcripts mix).
- **Worker out of quota mid-task:** the worker, or the PM from its last progress lines, writes `## Handoff` in the task file (done, in progress, next step, files touched); the PM saves the worktree state as `.tmp/pm-state/patches/<task>.partial.patch` (`git -C <worktree> add -A`, then `git -C <worktree> diff --cached --binary`). The next worker continues in the same worktree (Claude or Codex) or applies that patch; finished steps are never re-run.
- **Reviewer out of quota:** one fresh reviewer of another available family suffices; never hold a task for a reset. When no other family is available (for example Codex implemented and Claude is past 90%), a fresh session of the same family on a different model reviews and the report says so; high-risk work waits for a second family.
- **Spread the load:** when two families fit a task equally, pick the one with the most weekly headroom. No keep-alive pings while idle. Report the usage line (provider, 5h %, weekly %, blocked until) in the round summary.

## 2. Models per role

The orchestrator is whatever session the user opens; this policy does not choose its model. Rule of thumb from N093: keep code edits and judgment on capable models; give cheap models only work where a mistake is cheap to spot (reading, searching, summarizing, enumerating); **raise effort before changing model**; for auto-checkable tasks run `low` first and re-run only the failures at `high`.

| Role | Claude Code (`cli_worker.py`) | Codex (`--backend codex`) | Antigravity (`agy`) |
| --- | --- | --- | --- |
| Read, search, digest logs and diffs, research reading, checker | Haiku 5.5 `low` | `gpt-6-luna` `low` | Gemini 3.8 Flash `low` (1M context for long digests) |
| Pre-mortem, edge-case scout, approach note | Haiku 5.5 `medium` | `gpt-6.1-terra` `medium` | **Gemini 3.8 Flash `medium`** (first choice) |
| Implement: easy or medium, clear spec | Sonnet 5.5 `medium` | **`gpt-6.1-terra` `medium`** | Gemini 3.8 Flash `medium` |
| Implement: moderately hard, multi-file | **Sonnet 5.5 `high`** | `gpt-6.1-sol` `high` | - |
| Implement: hard, high-risk, migration | **Opus 5.5 `high`** | `gpt-6.1-sol` `xhigh` | - |
| Implement: Opus `high` failed or hardest architecture | Fable 5.2 `high` (before raising Opus past `high`) | `gpt-6-astra` `high` | - |
| Review, ordinary work | Sonnet 5.5 `medium` | **`gpt-6.1-sol` `medium`** (until T-024) | Gemini 3.8 Flash `medium` (first choice once T-024 lets it review an uncommitted patch) |
| Review, high-risk (two reviewers, different families) | Sonnet 5.5 `high`, Opus 5.5 only on disagreement or P0/P1 | **`gpt-6.1-sol` `high`** | Gemini 3.8 Flash `high` |
| Docs, wording, config-value edit (`trivial`) | Haiku 5.5 `low` | `gpt-6-luna` `low` | Gemini 3.8 Flash `low` |

- **Bold** is the default pick when quota allows; otherwise use the same row in another column.
- **Spread the work (S43, decision 83):** pick by each CLI's strengths; where Codex and Claude fit a row equally, alternate between them or draw one (log the pick in `.agents/log.md`), so no single agent does all the implementation. Codex workers cannot edit `tools/agent-workflow/` (`hooks/codex_guard.py:104-105`), so tooling implementation goes to Claude and Codex reviews it; pipeline tasks alternate from T-005 (decision 91): odd ones Codex builds and Claude reviews, even ones the reverse.
- **Escalation order (N093):** when a result is not good enough, first make sure the worker can verify its own work (a test, a build, a script in the done check); then raise effort one level; only then move to the next model up, starting it at a lower effort than the smaller model used.
- **Ladder:** the PM names the task's row in the brief from its difficulty; a task starts there and moves one row down after a failed attempt or a recorded blocker; the fix-round cap of section 5 still applies. Effort `max` or Codex `ultra` is never used (Codex `ultra` is refused by `codex_backend.py`).
- **Never** let a model review its own change or a change from its own session; prefer a reviewer from another family. Haiku never reviews code.
- **Model identifiers:** Claude `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-2` (aliases `haiku`, `sonnet`, `opus`, `fable` in `cli_worker.py --model`). Codex roles in `tools/agent-workflow/codex_profiles.json`. agy: the runner passes `--model gemini-3.8-flash-high` (used in practice, not listed in Google's docs, which name the label "Gemini 3.8 Flash" and a separate `/effort low|medium|high`; N095); confirm with `agy models` after each update.
- **Check models at session start:** confirm every model in this table is still offered (`claude` `/model`, `codex` `/model`, `agy models`); report a missing one and use the same row of another family. Re-check the model lists when a CLI updates (`/model`, `codex /model`, `agy models`) and update this table and the notes in the same change.

## 3. Workers and slots

- **Claude workers** are persistent `claude -p` sessions started and continued only through `tools/agent-workflow/cli_worker.py` (`start --profile <role>`, `send`, `status`, `retire`); window, nudge and cache TTL come from `worker_profiles.json`. The launcher locks out Agent, MCP, `.secrets` and Git writes. Never `--dangerously-skip-permissions`.
- **Codex workers:** `cli_worker.py start --backend codex` with a `codex-*` role; setup and limits in [CODEX_JOBS.md](../../tools/agent-workflow/CODEX_JOBS.md). Codex stops at Step 0 unless the brief says the plan is approved.
- **Antigravity:** read-only jobs (review, pre-mortem, digest) run in parallel through `gemini-review.ps1` (headless `agy -p`, default permission policy, never `--dangerously-skip-permissions`); jobs that write files use the restricted `AgyRunner` through `gemini-launch.ps1`, one at a time, workspace built by `gemini-ws.ps1 -Sanitize`. Both need the Windows runner scripts in `scripts/agy-runner/` (`setup_project.py check` reports them). Never trust `SUCCESS` alone: `gemini-launch.ps1` exits 2 `INCOMPLETE` on a denial, an empty reply or a missing `REVIEW.md`. Continue an agy conversation only within about 20 minutes and under about 400K context, else start anew with a short handoff. Implements only work that is not high-risk. Prompts follow the Antigravity wrapper ([cli/antigravity.md](cli/antigravity.md)).
- **Worker profiles and hooks:** start Claude workers with `cli_worker.py start --profile <role>` (`dev-large`, `dev-medium`, `dev-small`, `review`, `spike`, `scout`, `helper`); window, nudge and cache TTL come from `worker_profiles.json` (change values there, not in code). Guard hooks are on by default (`--process-guard on`, `--loop-warning 5`); turning one off needs a recorded reason. Workers get the normal tool set plus read-only documentation connectors; orchestration, scheduling, publishing and connector writes are withheld. A hook is a safety net, not a sandbox.
- **Slots:** at most 8 running agents across the three CLIs; a ceiling, not a target. Start a parallel task only if it is briefed and its Scope shares no file with running work (ORCHESTRATION section 5).
- **PM heartbeat (standing rule S26):** while any agent works, the PM keeps `tools/agent-workflow/pm-supervisor.ps1 -MaxMinutes 45` running under the Monitor tool (or as a background job with `-ExitOnEvent`). Each event line and the closing `HEARTBEAT` line wakes the PM, which then checks `cli_worker.py status` and the usage, reads new results, answers waiting Step 0 plans, fills free slots, updates `## NOW` and `tasks/STATUS.md`, and re-arms the supervisor. 45 minutes stays under the 1-hour prompt-cache TTL, so each wake reads the PM's cache instead of rewriting it; never above 50, never 120. With no agent running, do not arm it: let the cache lapse rather than pay for idle wakes. `hooks/pm_invariants.py` warns when work runs without a live supervisor. The PM session's real cache TTL is checked in ORCHESTRATION section 2; if it is 5 minutes, the heartbeat still serves status checks but no longer saves cache.
- **Cleanup:** the orchestrator cleans up on its own, without being reminded: after each integrated task, at session start and end, on every wake, plus the orphan sweep (ORCHESTRATION section 4, decision 43). Log it in the round summary.
- **Reuse and cache (decision 45):** a worker continues only its own task (fix rounds, next phase) within its TTL; a new task gets a fresh worker and worktree. Never change a continuing worker's model or tools (ORCHESTRATION section 9).
- **Delegate economically:** the PM does small low-risk work itself (about 15 tool calls or fewer, read-only or a single-file docs edit, not high-risk); delegate only for real parallelism, independent review or expertise. Multi-agent runs cost several times the tokens of one agent (N071).

- **Advisory consultation:** once per hard-to-reverse, high-impact decision (architecture, security, evidence, contract break) that Opus 5.5 cannot settle, the PM may consult Fable 5.2 (`cli_worker.py --advisor fable`); the answer is advice, never a user decision.

## 4. Briefs and the plan gate

- **Small tasks (decision 49):** one area, not high-risk, a change a reviewer reads in a few minutes: `Small: yes`, `Approach note: none: small task`, no hazard map; the worker puts Step 0 atop its report and continues. The done check and the review stay. Codex still gets the plan and the go as two messages (cli/codex.md).
- **Brief assembly:** CLI wrapper for the worker's CLI ([cli/](cli/README.md)), the template header and body, the role pack for the worker's role ([roles/](roles/README.md)), then the footer. A worker receives only its own role pack.
- **Evidence in reports:** accept a report only when each claim has a pointer (file and line, command with output, or URL with the quoted words) and the done-check output is pasted; a claim without a pointer is returned or recorded as an assumption.
- Every brief opens with the [template](AGENT_TASK_TEMPLATE.md) header (`Kind:`, `R-IDs:`, `Task file:`, `Approach note:` for code), quotes the user's decision text ("do not narrow"), and ends with the verbatim [footer](AGENT_BRIEF_FOOTER.md); lint it first: `python -B -I tools/agent-workflow/brief_lint.py --require-footer --brief <file>`.
- Before a brief: open [REQUIREMENTS](../../tasks/REQUIREMENTS.md) and cite the R-IDs; run `recall.py search` for the topic; never re-ask a settled point. Never narrow, defer or make optional a requirement on PM initiative; correct narrowing found in agent specs or pre-mortems in the brief; conflicting decisions go to the user. Review briefs name the ignored evidence paths (Git-ignored files the verdict depends on).
- The PM reads every Step 0 and answers in the worker's next turn. A plan that touches product semantics, permissions or spend also goes to the user; only that task waits.
- **Hazards first:** before a non-trivial, non-small implementation or fix round a cheap scout (section 2, rotating families) writes `.tmp/<task>/research.md` with file:line facts and hazards; the implementer re-reads the cited lines (R-021). High-risk work, before attempt 1 and any fix round, also gets a red-team audit by two families listing every failure class, fixed in one round (decision 92).

## 5. Review and integration

- **Review:** a fresh agent of another family that did not write the change reviews the delta once: correctness, failure paths, security, configuration audit, lifecycle, tests, docs (reviewer pack). Only proven P0 to P2 defects block. High-risk work (security, evidence binding, authorization, GPU or process lifecycle, architecture) gets a pre-mortem and two reviewers of different families.
- **Single pass:** the PM deduplicates findings, rejects unproven or style-only ones and sets severity; the implementer fixes the whole list in one round; the re-review covers the delta. A finding an earlier review missed in unchanged code blocks only at P0/P1. A finding names its bug class and root cause, and the fix covers the class. Wording, doc or claim findings the PM fixes at integration.
- **Fix-round cap (one number):** a fix round is one return to a builder after a failed review or verification, counted per task across tiers; **at most three**, then the task is `BLOCKED` and the user gets one question. Run each check once on the same bytes.
- **Read results, not summaries:** read the worker's task file and the diff, rerun lint once; relay every finding to the user, conflicts with decisions first. Keep user requirements, PM proposals, agent findings and live evidence apart; only the user promotes a proposal; state the model and effort actually set.
- **Integration:** workers never commit, push or merge; the orchestrator commits and pushes integrated work itself (AGENTS.md section 11, decision 77). A worker whose `cli_worker.py status --json` row shows `git_history_changed` or `git_audit_open` is not integrated until the PM has reviewed the listed refs. The PM saves the accepted worktree state as `.tmp/pm-state/patches/<task>.patch` (`git -C <worktree> add -A`, then `git -C <worktree> diff --cached --binary --output=<file>`; never pipe a patch through PowerShell, which rewrites its line ends), applies it to the main working tree after `git apply --check`, merges the task file's decisions and notes into the shared documents (ORCHESTRATION section 5), updates docs once, runs the pre-push checks, commits and pushes (decision 77), and reports the commit, the changed files and the checks run. A saved patch is the recovery point until a pushed commit contains it. Locally run lint, type check and the delta's tests; the full suite runs at code freeze and in CI. Use absolute paths and never change the shell's working directory; scripts use `Push-Location`/`Pop-Location` in `try`/`finally`.
- **States:** `INIT`, `DISCOVERY`, `PLANNING`, `IMPLEMENTING`, `TESTING`, `REVIEWING`, `READY TO RELEASE`, `DONE`, `BLOCKED`. Keep [PROJECT_CONTEXT](../../tasks/PROJECT_CONTEXT.md) current after each milestone.

## 6. Focus and drift control

Evidence: [N097](../../docs/note/N097-agent-focus-and-drift.md). Most wasted work comes from unclear stop conditions, spec drift and weak verification, and quality falls with the number of steps more than with context length; forgotten context is the rarer cause.

- **Anchor the goal outside the chat.** Keep `## NOW` in the PM continuation note `.tmp/pm-state/pm-continuation.md`: the wave goal in one line, its done check, the task IDs in flight, and an explicit "not now" list. The SessionStart hook re-injects it on startup, resume and after every compaction; a goal stated only in chat is lost to compaction. Update it whenever the wave changes.
- **Compact on purpose:** at task boundaries run `/compact` with a focus naming the goal, the running workers and the pending questions; between unrelated waves write `## NOW`, then `/clear`. After two failed corrections of the same worker, rewrite the brief instead of a third correction; the rewritten brief still counts as a fix round (section 5).
- **Brief contract** (template header): Goal, Done check (a runnable command), Scope, **Out of scope**, **Output** (format and size cap), **Step budget**. A brief that cannot name its done check is not ready to send.
- **Phases, not marathons:** split work that needs more than about 30 tool calls into phases; each phase ends with a 2-5 line handoff, and the next phase goes to the same warm worker (ORCHESTRATION section 5).
- **Condensed returns only:** pass a worker's verified result and its short report to other agents, never its raw transcript or long logs (drift spreads through context). Long logs and diffs are digested first by a cheap reader (section 2) into one page.
- **Verification decides "done":** accept only with the done-check command and its real output; "looks right" is not done. Reject reports without the footer's progress and stop discipline.
- **Keep the PM context for decisions:** the PM reads summaries, task files and diffs of the changed lines; verbose reading (search, logs, web pages) goes to a cheap reader. Watch `/context`; at about 60% of the window plan the next compaction point.
- **Measure, do not assume:** compare tokens per accepted task in the worker ledger (`workflow_metrics.py`) before and after a process change; keep a change only if it lowers tokens at the same acceptance rate.

## 7. PM self-checks (before each reply to the user)

(Section 8, cold read, follows this list.)

0. Supervisor armed while any agent runs (section 3); quota line read; a wake scheduled for every blocked provider (section 1).
1. Verify, never assume: check files, ledgers and `git log` before saying something is missing or pending.
2. End with **"Việc bạn cần làm"** (the user's open actions), even when unchanged.
3. List background shells and agent processes; stop what has no owner; `.tmp` cleaned up (ORCHESTRATION section 4).
4. A crash, error dialog or hang is a finding: keep the traceback and assign it.
5. No worker ran a commit or push; the orchestrator's commits carry no attribution trailer and were pushed without force.
6. Include the quota line from section 1.

## 8. Cold read after documentation changes (R-028, decision 27)

After documents are written or restructured, and before the user is told they are ready:

1. Start three fresh read-only scouts, one each on Haiku 5.5, Sonnet 5.5 and Opus 5.5 (scout role pack, cold-read bullet). Each gets only the repository, as a new agent would: no chat history, no summary from the PM.
2. Each reports: what the project is and what it would do first; every place it found unclear, contradictory or missing, with file and line; the questions it would ask the user.
3. The PM fixes what is a defect of the text, lists what the readers disagreed on, and relays the open questions to the user with a recommendation for each. A point two readers of different sizes both misread counts as a defect of the text, not of the reader.
4. Record the round in the task file: readers, findings, fixes, questions sent.

Measure the workflow changes of decision 24 the same way they were justified: compare rule violations per accepted task and tokens per accepted task in the worker ledger (`workflow_metrics.py`) before and after, and keep a change only when the numbers support it (improvement H).
