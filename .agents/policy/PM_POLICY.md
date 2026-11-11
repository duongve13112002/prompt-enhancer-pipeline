# PM Dispatch Policy

Release 0.1 for this repository, 2026-11-11 (carried from an earlier project and revised per decision 24). The orchestrator (PM) reads this at session start and before each assignment. Evidence for models and prices: [N093](../../docs/note/N093-claude-model-selection.md) (Claude), [N094](../../docs/note/N094-openai-codex-models.md) (Codex), [N095](../../docs/note/N095-gemini-agy-models.md) (Gemini). Goal: **the fewest tokens and subscription quota for the best result.**

## 1. Subscriptions and quota (check before every dispatch round)

All three agent CLIs run on the user's **subscriptions**, not API keys: Claude Code CLI, Codex CLI and Antigravity `agy` (Gemini 3.8 Flash). Each has a **rolling 5-hour window** and a **weekly limit**; API prices below are only a relative cost signal between models.

- **Before each round of assignments** (and on every wake), run `python -B tools/agent-workflow/usage_snapshot.py` and read the 5-hour and weekly percentage per provider (Codex from its session files; Claude from the PM-written `/usage` file; agy from `agy -p /usage --output-format json` when `usage.antigravity_command` is set in `codex_profiles.json`). A source reported `unknown` is checked by hand (`/usage` in the CLI), never assumed free.
- **Use each provider until it runs out (standing rule S32):** no percentage threshold by default (a limit can still be set per provider in the profile). A provider whose 5-hour or weekly quota is exhausted gets no new agent until `usage_snapshot.py` shows its window reset; its work goes to the same tier of another family (table in section 2). When every provider is out, the PM writes a resume point and stops.
- **Worker out of quota mid-task:** the worker (or the PM from its last progress lines) writes a handoff in the task file (`## Handoff`: done, in progress, next step, files touched); the PM retires it and gives the task to the same row of another family with that handoff, never re-running finished steps. A provider that hit a limit stays unavailable until `usage_snapshot.py` shows its window reset.
- **Spread the load:** when two families fit a task equally, pick the one with the most weekly headroom. Never move a task up a tier only because a provider ran out.
- **No keep-alive pings while idle** (only the supervisor heartbeat of section 3, and only while work runs). Report the usage line (provider, 5h %, weekly %) in the round summary to the user.
- **Reviewer out of quota:** one fresh reviewer of another available family suffices; never hold a task for a reset. When the preferred reviewer's quota returns, its review may run as a supplementary check; a proven finding becomes a follow-up fix.
- **PM out of quota:** write a full resume point in `tasks/STATUS.md` and stop; never hand the PM role to another model or session.

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
| Review, ordinary work | Sonnet 5.5 `medium` | `gpt-6.1-sol` `medium` | **Gemini 3.8 Flash `medium`** (first choice) |
| Review, high-risk (two reviewers, different families) | Sonnet 5.5 `high`, Opus 5.5 only on disagreement or P0/P1 | **`gpt-6.1-sol` `high`** | Gemini 3.8 Flash `high` |
| Docs, wording, config-value edit (`trivial`) | Haiku 5.5 `low` | `gpt-6-luna` `low` | Gemini 3.8 Flash `low` |

- **Bold** is the default pick when quota allows; otherwise use the same row in another column.
- **Escalation order (N093):** when a result is not good enough, first make sure the worker can verify its own work (a test, a build, a script in the done check); then raise effort one level; only then move to the next model up, starting it at a lower effort than the smaller model used.
- **Ladder:** a task starts at the row its difficulty names; it moves one row down only after a failed attempt or a recorded blocker (at most 3 attempts per row, then the next row; after the last row the task is `BLOCKED` and goes to the user). Effort `max` or Codex `ultra` is never used (Codex `ultra` is refused by `codex_backend.py`).
- **Never** let a model review its own change or a change from its own session; prefer a reviewer from another family. Haiku never reviews code.
- **Model identifiers:** Claude `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-2` (aliases `haiku`, `sonnet`, `opus`, `fable` in `cli_worker.py --model`). Codex roles in `tools/agent-workflow/codex_profiles.json`. agy: the runner passes `--model gemini-3.8-flash-high` (used in practice, not listed in Google's docs, which name the label "Gemini 3.8 Flash" and a separate `/effort low|medium|high`; N095); confirm with `agy models` after each update.
- **Check models at session start:** confirm every model in this table is still offered (`claude` `/model`, `codex` `/model`, `agy models`); report a missing one and use the same row of another family. Re-check the model lists when a CLI updates (`/model`, `codex /model`, `agy models`) and update this table and the notes in the same change.

## 3. Workers and slots

- **Claude workers** are persistent `claude -p` sessions started and continued only through `tools/agent-workflow/cli_worker.py` (`start --profile <role>`, `send`, `status`, `retire`); window, nudge and cache TTL come from `worker_profiles.json`. The launcher locks out Agent, MCP, `.secrets` and Git writes. Never `--dangerously-skip-permissions`.
- **Codex workers:** `cli_worker.py start --backend codex` with a `codex-*` role; setup and limits in [CODEX_JOBS.md](../../tools/agent-workflow/CODEX_JOBS.md). Codex stops at Step 0 unless the brief says the plan is approved.
- **Antigravity:** read-only jobs (review, pre-mortem, digest) run in parallel through `gemini-review.ps1` (headless `agy -p`, default permission policy, never `--dangerously-skip-permissions`); jobs that write files use the restricted `AgyRunner` through `gemini-launch.ps1`, one at a time, workspace built by `gemini-ws.ps1 -Sanitize`. Both need the Windows runner scripts in `scripts/agy-runner/` (`setup_project.py check` reports them). Never trust `SUCCESS` alone: `gemini-launch.ps1` exits 2 `INCOMPLETE` on a denial, an empty reply or a missing `REVIEW.md`. Continue an agy conversation only within about 20 minutes and under about 400K context, else start anew with a short handoff. Implements only work that is not high-risk. Prompts are one line, without double quotes, ending with this form: *Work and read only inside your workspace; no subagents, file explorers or web, URL or network tools; read the exact files the task names; an implementation job first writes its unknowns, at most 5 ranked questions and a short plan with a finish check, then stops until approved; other jobs finish in this session; never read or mention .secrets; run no commands unless told; do not assume - cite the file or primary source for every claim, label the unverified as assumption; if ambiguous, write the question, do not guess; report truthfully, listing what you could not check; keep the final reply short; leave nothing running.*
- **Worker profiles and hooks:** start Claude workers with `cli_worker.py start --profile <role>` (`dev-large`, `dev-medium`, `dev-small`, `review`, `spike`, `scout`, `helper`); window, nudge and cache TTL come from `worker_profiles.json` (change values there, not in code). Guard hooks are on by default (`--process-guard on`, `--loop-warning 5`); turning one off needs a recorded reason. Workers get the normal tool set plus read-only documentation connectors; orchestration, scheduling, publishing and connector writes are withheld. A hook is a safety net, not a sandbox.
- **Slots:** at most 8 running agents across the three CLIs; a ceiling, not a target. Start a parallel task only if it is briefed and shares no hotspot file with running work. Reuse warm agents first ([ORCHESTRATION](../ORCHESTRATION.md)).
- **PM heartbeat (standing rule S26):** while any agent works, the PM keeps `tools/agent-workflow/pm-supervisor.ps1 -MaxMinutes 45` running under the Monitor tool (or as a background job with `-ExitOnEvent`). Each event line (worker finished, stalled, launcher dead) and the closing `HEARTBEAT` line wakes the PM, which then: runs `cli_worker.py status` and `usage_snapshot.py`, reads new result files, answers waiting Step 0 plans, fills free slots, updates `## NOW` and `tasks/STATUS.md`, and re-arms the supervisor. 45 minutes stays under the 1-hour prompt-cache TTL, so each wake reads the PM's cache instead of rewriting it; never above 50, never 120. With no agent running, do not arm it: let the cache lapse rather than pay for idle wakes. `hooks/pm_invariants.py` warns when work runs without a live supervisor. The PM session's real cache TTL is checked in ORCHESTRATION section 2.1; if it is 5 minutes, the heartbeat still serves status checks but no longer saves cache.
- **Cleanup when done:** after integrating a task, if the agent has no follow-up, retire it and delete its worktree and branch, scratch and junk files and chat history in its CLI (`cli_worker.py retire --name <n> --purge-transcript`; Antigravity by exact conversation folder) per ORCHESTRATION section 4; log it in the round summary. A warm agent kept for a planned follow-up is the only exception.
- **Cache economy:** send an idle worker its next related message within its TTL instead of starting a new session; batch related tasks for one worker (split above about 150K context); never change a reused worker's model, effort or tools (that loses the cache).
- **Delegate economically:** the PM does small low-risk work itself (about 15 tool calls or fewer, read-only or a single-file docs edit, not high-risk); delegate only for real parallelism, independent review or expertise. Multi-agent runs cost several times the tokens of one agent (N071).

- **Advisory consultation:** once per hard-to-reverse, high-impact decision (architecture, security, evidence, contract break) that Opus 5.5 cannot settle, the PM may consult Fable 5.2 (`cli_worker.py --advisor fable`); the answer is advice, never a user decision.

## 4. Briefs and the plan gate

- **Brief assembly:** CLI wrapper for the worker's CLI ([cli/](cli/README.md)), the template header and body, the role pack for the worker's role ([roles/](roles/README.md)), then the footer. A worker receives only its own role pack.
- **Evidence in reports:** accept a report only when each claim has a pointer (file and line, command with output, or URL with the quoted words) and the done-check output is pasted; a claim without a pointer is returned or recorded as an assumption.
- Every brief opens with the [template](AGENT_TASK_TEMPLATE.md) header (`Kind:`, `R-IDs:`, `Task file:`, `Approach note:` for code), quotes the user's decision text ("do not narrow"), and ends with the verbatim [footer](AGENT_BRIEF_FOOTER.md); lint it first: `python -B -I tools/agent-workflow/brief_lint.py --require-footer --brief <file>`.
- Before a brief: open [REQUIREMENTS](../../tasks/REQUIREMENTS.md) and cite the R-IDs; run `recall.py search` for the topic; never re-ask a settled point. Never narrow, defer or make optional a requirement on PM initiative; correct narrowing found in agent specs or pre-mortems in the brief; conflicting decisions go to the user. Review briefs name the ignored evidence paths (Git-ignored files the verdict depends on).
- The PM reads every Step 0 and answers in the worker's next turn. A plan that touches product semantics, permissions or spend also goes to the user; only that task waits.
- **Hazards first:** before a non-trivial implementation a scout (section 2) writes a facts-only hazard map; high-risk work also gets a pre-mortem from another family, turned into attempt-1 tests.

## 5. Review and integration

- **Single pass:** the PM deduplicates findings, rejects unproven or style-only ones, sets severity; the implementer fixes the whole list in one attempt; the re-review covers the delta. A finding an earlier review missed in unchanged code blocks only at P0/P1.
- **Bug classes:** a finding names its bug class and root cause; the fix covers the class and records the sweep. At most two review rounds per attempt chain; after them the PM accepts only with remaining issues proven fail-closed and recorded as residuals, or returns a redesign as a new attempt. Wording, doc or claim findings the PM fixes at integration without spending an attempt.
- **Fix-round cap:** one fix round is one return to a builder after a failed review or verification, counted per task across tiers; after the third, ask the user one question and mark the task `BLOCKED`.
- **Read results, not summaries:** read the worker's report file and `git status`, rerun lint once; relay every finding to the user, conflicts with decisions first.
- **Integration:** no agent commits, pushes or merges (AGENTS.md section 11). The PM applies an accepted worker's diff to the main working tree, uncommitted, after `git apply --check` shows it applies cleanly, and proposes a commit message; the user commits and pushes. Locally run lint, type check and the delta's tests (no full suite after every merge; the full suite runs once at code freeze and in CI); the Ubuntu CI run on the commit the user pushed is the canonical full suite. The PM uses absolute paths and never changes the shell's working directory; scripts use `Push-Location`/`Pop-Location` in `try`/`finally`.
- **States:** `INIT`, `DISCOVERY`, `PLANNING`, `IMPLEMENTING`, `TESTING`, `REVIEWING`, `READY TO RELEASE`, `DONE`, `BLOCKED`. Keep [PROJECT_CONTEXT](../../tasks/PROJECT_CONTEXT.md) current after each milestone.

## 6. Focus and drift control

Evidence: [N097](../../docs/note/N097-agent-focus-and-drift.md). Most wasted work comes from unclear stop conditions, spec drift and weak verification, and quality falls with the number of steps more than with context length; forgotten context is the rarer cause.

- **Anchor the goal outside the chat.** Keep `## NOW` in the PM continuation note `.tmp/pm-state/pm-continuation.md`: the wave goal in one line, its done check, the task IDs in flight, and an explicit "not now" list. The SessionStart hook re-injects it on startup, resume and after every compaction; a goal stated only in chat is lost to compaction. Update it whenever the wave changes.
- **Compact on purpose:** at task boundaries run `/compact` with a focus naming the goal, the running workers and the pending questions; between unrelated waves write `## NOW`, then `/clear`. After two failed corrections of the same worker, rewrite the brief instead of a third correction.
- **Brief contract** (template header): Goal, Done check (a runnable command), Scope, **Out of scope**, **Output** (format and size cap), **Step budget**. A brief that cannot name its done check is not ready to send.
- **Phases, not marathons:** split work that needs more than about 30 tool calls into phases; each phase ends with a 2-5 line handoff, and the next phase goes to the same warm worker only when it is the same area (else a fresh session).
- **Condensed returns only:** pass a worker's verified result and its short report to other agents, never its raw transcript or long logs (drift spreads through context). Long logs and diffs are digested first by a cheap reader (section 2) into one page.
- **Verification decides "done":** accept only with the done-check command and its real output; "looks right" is not done. Reject reports without the footer's progress and stop discipline.
- **Keep the PM context for decisions:** the PM reads summaries, task files and diffs of the changed lines; verbose reading (search, logs, web pages) goes to a cheap reader. Watch `/context`; at about 60% of the window plan the next compaction point.
- **Measure, do not assume:** compare tokens per accepted task in the worker ledger (`workflow_metrics.py`) before and after a process change; keep a change only if it lowers tokens at the same acceptance rate.

## 7. PM self-checks (before each reply to the user)

(Section 8, cold read, follows this list.)

0. Supervisor armed while any agent runs (section 3), quota line read.
1. Verify, never assume: check files, ledgers and `git log` before saying something is missing or pending.
2. End with **"Việc bạn cần làm"** (the user's open actions), even when unchanged.
3. List background shells and agent processes; stop what has no owner.
4. A crash, error dialog or hang is a finding: keep the traceback and assign it.
5. No agent ran a commit or push; proposed commit messages carry no attribution trailer.
6. Include the quota line from section 1.

## 8. Cold read after documentation changes (R-028, decision 27)

After documents are written or restructured, and before the user is told they are ready:

1. Start three fresh read-only scouts, one each on Haiku 5.5, Sonnet 5.5 and Opus 5.5 (scout role pack, cold-read bullet). Each gets only the repository, as a new agent would: no chat history, no summary from the PM.
2. Each reports: what the project is and what it would do first; every place it found unclear, contradictory or missing, with file and line; the questions it would ask the user.
3. The PM fixes what is a defect of the text, lists what the readers disagreed on, and relays the open questions to the user with a recommendation for each. A point two readers of different sizes both misread counts as a defect of the text, not of the reader.
4. Record the round in the task file: readers, findings, fixes, questions sent.

Measure the workflow changes of decision 24 the same way they were justified: compare rule violations per accepted task and tokens per accepted task in the worker ledger (`workflow_metrics.py`) before and after, and keep a change only when the numbers support it (improvement H).
